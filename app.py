from flask import (
    Flask, render_template, request, redirect, url_for, session, jsonify,
    abort, flash, Response, send_from_directory, send_file, has_app_context,
)
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import quote
import base64
import hashlib
import json
import mimetypes
import os
import random
import re
import secrets
import smtplib
import sqlite3
import string
import sys
import time
import urllib.error
import urllib.request
import uuid

from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.exceptions import HTTPException

from dotenv import load_dotenv

import sqlalchemy as sa
from sqlalchemy import and_, or_, func, select, delete as sa_delete, update as sa_update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from services.date_format import format_display_date, format_display_date_configured
from services.student_number_generator import (
    allocate_student_number,
    StudentNumberAllocationError,
)
from models import (
    db,
    AcademicPromotionItem, AcademicPromotionRun, AcademicSession, Admin,
    AdminControlItem, AdminMessage, AdminNotification, AdminPermission,
    AdminResourceLock, AdminRoleAssignment, AdminScope, AdminType,
    AdminTypePermission, Answer, AssignmentQuestion, AssignmentStudent,
    Attempt, AttemptQuestion, AuditLog, Candidate, CandidatePaper,
    ClassSubject, EntranceBankConfig, Examination, FinanceDeliveryLog,
    FinanceFeeAssessment, FinanceFeeItem, FinanceFeeItemClass,
    FinancePayment, FinancePaymentAllocation, LibraryBook, LibraryLoan,
    ParentAccount, ParentFeedback, ParentFeedbackReply, ParentStudentLink,
    PasswordResetToken, Permission, PresenceSession, ProjectStudent,
    ResultWorkflowEvent, RetakeGrant, SchemaMigration, School,
    SchoolAssessment, SchoolAssessmentAnswer, SchoolAssessmentAttempt,
    SchoolAssessmentAttemptQuestion, SchoolAssignment,
    SchoolAssignmentAnswer, SchoolAssignmentAttempt,
    SchoolAssignmentAttemptQuestion, SchoolClass, SchoolClassProgression,
    SchoolNotification, SchoolNumberingPolicy, SchoolProject,
    SchoolPublicEnquiry, SchoolPublicNews, SchoolPublicPage,
    SchoolPublicSetting, SchoolQuestion, SchoolSetting, SchoolStudentResult,
    SchoolSubject, SecurityEvent, Student, StudentAdmissionContact,
    StudentAdmissionProfile, StudentEnrollmentHistory, StudentEnrolment,
    StudentNumberAllocation,
)

BASE=os.path.dirname(os.path.abspath(__file__))
# Load .env before anything below reads os.environ. A variable already set in
# the real environment (as every test and verification script sets CRAINBOW_DB)
# always wins over .env — load_dotenv() never overrides an existing variable.
load_dotenv(os.path.join(BASE,'.env'))
# CRAINBOW_DB lets a test or a side-by-side verification run point the app at a
# throwaway copy of the database instead of the live cbt.db.
DB=os.environ.get('CRAINBOW_DB','').strip() or os.path.join(BASE,'cbt.db')
DATA=os.environ.get('CRAINBOW_DATA','').strip() or os.path.join(BASE,'data')
app=Flask(__name__)
app.jinja_env.filters.setdefault('display_date', format_display_date)
# blueprints/*/routes.py do `from app import app` to register their routes on
# this same Flask instance. When this file is run directly (`python app.py`,
# the real entrypoint), it executes as __main__, not as a module named
# "app" — so without this alias, that import would find no cached "app"
# module and re-execute this entire file a second time under a separate
# identity, creating a second Flask/SQLAlchemy setup that contends with the
# first for the same SQLite file. Registering this module under both names
# makes the later import resolve to this exact, already-running instance.
sys.modules.setdefault('app', sys.modules[__name__])

ENVIRONMENT=os.environ.get('CRAINBOW_ENV', os.environ.get('FLASK_ENV','development')).strip().lower()
_config_secret=os.environ.get('CRAINBOW_SECRET','').strip()
if ENVIRONMENT in ('production','prod') and len(_config_secret) < 32:
    raise RuntimeError('CRAINBOW_SECRET must be set to a strong secret (at least 32 characters) in production.')
app.secret_key=_config_secret or secrets.token_hex(32)
ADMIN_PASSWORD=os.environ.get('CRAINBOW_ADMIN_PASSWORD','').strip()

# ---------------- SQLAlchemy ----------------
app.config['SQLALCHEMY_DATABASE_URI']=f'sqlite:///{DB}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS']=False
app.config['SQLALCHEMY_ENGINE_OPTIONS']={
    # Mirrors the previous sqlite3.connect(DB, timeout=10) behaviour so the
    # existing single-file/LAN deployment keeps tolerating brief write locks.
    'connect_args': {'timeout': 10, 'check_same_thread': False},
}
db.init_app(app)


@sa.event.listens_for(sa.engine.Engine, 'connect')
def _sqlite_pragmas(dbapi_connection, connection_record):
    """Preserve the per-connection PRAGMAs the raw sqlite3 layer used to set.

    Foreign keys are OFF by default in SQLite and must be enabled per
    connection; the ON DELETE CASCADE rules in the schema are inert without it.
    """
    if isinstance(dbapi_connection, sqlite3.Connection):
        cur = dbapi_connection.cursor()
        cur.execute('PRAGMA foreign_keys=ON')
        cur.execute('PRAGMA busy_timeout=10000')
        cur.close()


# ---------------- query helpers ----------------
# The application and its templates were written against sqlite3.Row, so the
# read helpers return RowMapping objects, which still support row['column'].
# Moved to core/db_helpers.py; imported back so every existing call site
# (one(...), all_rows(...), etc.) keeps working unchanged.
from core.db_helpers import one, one_scalar, all_rows, tuples, obj, _flatten, _ignore_insert  # noqa: E402


# ---------------- static/uploads paths ----------------
# Moved to core/uploads.py.
from core.uploads import STATIC, UPLOADS, IMAGE_EXTENSIONS, _save_image_upload  # noqa: E402


# ---------------- presence tracking ----------------
# Moved to core/presence.py.
from core.presence import (  # noqa: E402
    PRESENCE_TIMEOUT_SECONDS, _presence_identity, touch_presence,
    end_presence, online_presence,
)


# ---------------- admin RBAC / audit ----------------
# Permission catalogue, role presets, admin_required, current_admin,
# audit_log and friends moved to core/security.py.
from core.security import (  # noqa: E402
    ADMIN_PERMISSION_DEFS, ADMIN_ROLE_PRESETS, ADMIN_ENDPOINT_PERMISSIONS,
    admin_required, current_admin, is_super_admin, admin_has_permission,
    admin_permission_codes, admin_scope_allows, audit_display_detail,
    audit_log, admin_scope_for_request, admin_access_error,
    _notify_super_admins, csrf_protect,
)


# ---------------- shared query helpers ----------------


def _add_missing_columns():
    """Add columns that models.py declares but an older database lacks.

    ``create_all()`` creates missing *tables* but never alters existing ones, so
    a database created by an earlier release keeps its old column set. This
    closes that gap directly from the model metadata, which means there is no
    separate hand-written ALTER script to keep in step with the models.

    SQLite cannot add a NOT NULL column without a default, so such a column is
    reported rather than attempted; in practice every one of ours carries a
    server default.
    """
    inspector = sa.inspect(db.engine)
    existing = set(inspector.get_table_names())
    with db.engine.begin() as connection:
        for table in db.metadata.sorted_tables:
            if table.name not in existing:
                continue
            have = {c['name'] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in have:
                    continue
                spec = column.type.compile(db.engine.dialect)
                default = column.server_default
                if default is not None:
                    # SQLite accepts NOT NULL on an added column as long as a
                    # default is supplied. Carrying the constraint across keeps
                    # an upgraded database identical to a freshly created one.
                    if not column.nullable:
                        spec += ' NOT NULL'
                    spec += f' DEFAULT {default.arg.text}'
                elif not column.nullable:
                    app.logger.warning(
                        'Cannot add NOT NULL column %s.%s without a default; skipping.',
                        table.name, column.name)
                    continue
                connection.execute(sa.text(
                    f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {spec}'))


def _widen_parent_feedback_reply_admin_id():
    """Let a parent reply into their own feedback thread, on an upgraded database.

    parent_feedback_replies.admin_id used to be NOT NULL, so only staff could
    ever post into a thread; NULL now means the parent who owns the thread
    wrote it. SQLite cannot ALTER COLUMN a NOT NULL constraint away, so this
    rebuilds the table when needed: a fresh install already gets the relaxed
    shape straight from models.py via create_all(), so this is a no-op there.
    Idempotent and safe to run on every startup.
    """
    inspector = sa.inspect(db.engine)
    if 'parent_feedback_replies' not in inspector.get_table_names():
        return
    column = next((c for c in inspector.get_columns('parent_feedback_replies')
                   if c['name'] == 'admin_id'), None)
    if column is None or column['nullable']:
        return
    with db.engine.begin() as connection:
        connection.execute(sa.text("""
            CREATE TABLE parent_feedback_replies_new (
                id INTEGER NOT NULL,
                feedback_id INTEGER NOT NULL,
                admin_id INTEGER,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                file_type TEXT,
                attachment_path TEXT,
                PRIMARY KEY (id),
                FOREIGN KEY(feedback_id) REFERENCES parent_feedback (id) ON DELETE CASCADE,
                FOREIGN KEY(admin_id) REFERENCES admins (id)
            )
        """))
        connection.execute(sa.text("""
            INSERT INTO parent_feedback_replies_new
                (id, feedback_id, admin_id, body, created_at, file_type, attachment_path)
            SELECT id, feedback_id, admin_id, body, created_at, file_type, attachment_path
            FROM parent_feedback_replies
        """))
        old_count=connection.execute(sa.text("SELECT COUNT(*) FROM parent_feedback_replies")).scalar()
        new_count=connection.execute(sa.text("SELECT COUNT(*) FROM parent_feedback_replies_new")).scalar()
        if old_count != new_count:
            connection.execute(sa.text("DROP TABLE parent_feedback_replies_new"))
            raise RuntimeError(
                f"Widening parent_feedback_replies.admin_id would lose rows: "
                f"expected {old_count}, copied {new_count}.")
        connection.execute(sa.text("DROP TABLE parent_feedback_replies"))
        connection.execute(sa.text("ALTER TABLE parent_feedback_replies_new RENAME TO parent_feedback_replies"))
        connection.execute(sa.text(
            "CREATE INDEX IF NOT EXISTS idx_parent_feedback_replies_feedback "
            "ON parent_feedback_replies (feedback_id, created_at)"))

def _record_schema_baseline():
    """Record the historical migrations as applied without replaying them.

    models.py is now the single description of the schema: create_all() builds
    every table and _add_missing_columns() adds any column an older database
    lacks. That makes the migrations under migrations/ redundant for schema
    purposes, and replaying them is actively unsafe:

    * 0003-0011 have empty bodies, so they would do nothing anyway.
    * 0012-0019 are one-shot data repairs written against one specific
      database. 0012 aborts with "expected 5 students; found N" on any other,
      because it asserts a row count rather than a schema shape.

    The seed data 0012 was responsible for (the initial school tenant, its
    numbering policy, and the school_id backfill) is created idempotently by
    _seed_school_tenant() instead, so it works on any database.

    The versions are still written to schema_migrations so the table remains an
    accurate record of which upgrades a database has passed.
    """
    from migrations.runner import MIGRATIONS
    now = datetime.now(timezone.utc).isoformat()
    raw = db.engine.raw_connection()
    try:
        driver_con = raw.driver_connection
        driver_con.execute(
            'CREATE TABLE IF NOT EXISTS schema_migrations('
            'version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)')
        driver_con.executemany(
            'INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(?,?)',
            [(version, now) for version, _ in MIGRATIONS])
        driver_con.commit()
    finally:
        raw.close()


# The school this installation serves. A tenant row has to exist before student
# numbers can be allocated, and every school-scoped table keys off its id.
DEFAULT_SCHOOL_CODE='CRMS'
DEFAULT_SCHOOL_NAME='Creative Rainbow Montessori School'
DEFAULT_SCHOOL_MOTTO='Growing in humility and fear of God'


def _seed_school_tenant(now):
    """Ensure the school tenant, its numbering policy and school_id are set.

    Idempotent: existing rows are left exactly as they are, so editing the
    school's details through the admin UI is never undone by a restart.
    """
    school=db.session.scalars(
        select(School).where(School.code==DEFAULT_SCHOOL_CODE)).first()
    if not school:
        school=School(code=DEFAULT_SCHOOL_CODE,name=DEFAULT_SCHOOL_NAME,
                      motto=DEFAULT_SCHOOL_MOTTO,tagline=DEFAULT_SCHOOL_MOTTO,
                      active=1,created_at=now,updated_at=now)
        db.session.add(school)
        db.session.flush()
    if not db.session.scalars(
            select(SchoolNumberingPolicy)
            .where(SchoolNumberingPolicy.school_id==school.id)).first():
        db.session.add(SchoolNumberingPolicy(
            school_id=school.id,label='Registration Number',
            prefix=DEFAULT_SCHOOL_CODE,include_year=1,sequence_start=1,
            next_sequence=1,padding=4,active=1,created_at=now,updated_at=now))
    # Attach any pre-tenancy rows to this school.
    for model in (Student, SchoolClass, AcademicSession, StudentEnrolment):
        db.session.execute(sa_update(model)
                           .where(model.school_id.is_(None))
                           .values(school_id=school.id))
    # Existing admission numbers are this school's current registration
    # numbers; mark them as pre-existing rather than generated.
    db.session.execute(sa_update(Student)
                       .where(or_(Student.student_number_source.is_(None),
                                  Student.student_number_source==''))
                       .values(student_number_source='existing'))

def _answers_for_attempt(aid):
    return {qid:opt for qid,opt in tuples(
        select(Answer.question_id,Answer.option_index).where(Answer.attempt_id==aid))}

def _student_with_enrolment(sid):
    """Return an active student plus their current class/session enrolment.

    Returns a plain dict of the student's columns with class_id/class_name and
    session_id/session_name merged in, matching the flat row the pre-ORM join
    produced, so templates and callers index it the same way. The enrolment in
    the current academic session wins; otherwise the most recent one.
    """
    row=one(select(Student,
                   SchoolClass.id.label('class_id'),SchoolClass.name.label('class_name'),
                   AcademicSession.id.label('session_id'),AcademicSession.name.label('session_name'))
        .select_from(Student)
        .outerjoin(StudentEnrolment,and_(StudentEnrolment.student_id==Student.id,
                                         StudentEnrolment.active==1))
        .outerjoin(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
        .outerjoin(AcademicSession,AcademicSession.id==StudentEnrolment.session_id)
        .where(Student.id==sid,Student.active==1,Student.account_active==1)
        .order_by(sa.case((AcademicSession.is_current==1,0),else_=1),
                  StudentEnrolment.id.desc())
        .limit(1))
    if not row: return None
    student=row['Student']
    merged={c.key:getattr(student,c.key) for c in student.__mapper__.column_attrs}
    for extra in ('class_id','class_name','session_id','session_name'):
        merged[extra]=row[extra]
    return merged

def _students_with_class(session_id, include_session_id=False):
    """Active students with the class they are enrolled in for one session.

    Replaces the students/enrolments/classes join that several school-portal
    views repeated. Rows are dicts so callers can mutate and use .get() on them
    exactly as they did with the previous dict(row) conversions.
    """
    cols=[Student,SchoolClass.name.label('class_name')]
    if include_session_id:
        cols.append(StudentEnrolment.session_id)
    rows=all_rows(select(*cols)
        .select_from(Student)
        .outerjoin(StudentEnrolment,and_(StudentEnrolment.student_id==Student.id,
                                         StudentEnrolment.active==1,
                                         StudentEnrolment.session_id==session_id))
        .outerjoin(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
        .where(Student.active==1)
        .order_by(Student.last_name,Student.first_name))
    extras=('class_name','session_id') if include_session_id else ('class_name',)
    return [_flatten(r,'Student',*extras) for r in rows]

def _resync_question_count(assessment_id):
    """Refresh the denormalised question_count column on an assessment."""
    total=one_scalar(select(func.count()).select_from(SchoolQuestion)
                     .where(SchoolQuestion.assessment_id==assessment_id),0)
    db.session.execute(sa_update(SchoolAssessment)
        .where(SchoolAssessment.id==assessment_id).values(question_count=total))

def _thread_between(a_id, b_id):
    """Match every message exchanged between two administrators, either way."""
    return or_(and_(AdminMessage.sender_admin_id==a_id,
                    AdminMessage.recipient_admin_id==b_id),
               and_(AdminMessage.sender_admin_id==b_id,
                    AdminMessage.recipient_admin_id==a_id))

def _contact_columns():
    """The administrator fields the messaging views expose as a contact."""
    return select(Admin.id,Admin.display_name,Admin.username,Admin.email,
                  Admin.phone,Admin.whatsapp,Admin.photo_path)

def _candidate_papers(candidate_id):
    """A candidate's assigned papers, each with its latest attempt summary."""
    latest=(select(func.max(Attempt.id))
        .where(Attempt.candidate_id==CandidatePaper.candidate_id,
               Attempt.bank_id==CandidatePaper.bank_id)
        .correlate(CandidatePaper).scalar_subquery())
    return [_flatten(r,'CandidatePaper','exam_name','attempt_status','attempt_id','percentage')
            for r in all_rows(
        select(CandidatePaper,Examination.name.label('exam_name'),
               Attempt.status.label('attempt_status'),
               Attempt.id.label('attempt_id'),Attempt.percentage)
            .select_from(CandidatePaper)
            .outerjoin(Examination,Examination.bank_id==CandidatePaper.bank_id)
            .outerjoin(Attempt,Attempt.id==latest)
            .where(CandidatePaper.candidate_id==candidate_id)
            .order_by(CandidatePaper.slot))]

def _candidate_attempts(candidate_id):
    """Every attempt by a candidate, newest first, with paper slot and exam name."""
    return [_flatten(r,'Attempt','exam_name','paper_slot') for r in all_rows(
        select(Attempt,Examination.name.label('exam_name'),
               CandidatePaper.slot.label('paper_slot'))
            .select_from(Attempt)
            .outerjoin(Examination,Examination.bank_id==Attempt.bank_id)
            .outerjoin(CandidatePaper,and_(CandidatePaper.candidate_id==Attempt.candidate_id,
                                           CandidatePaper.bank_id==Attempt.bank_id))
            .where(Attempt.candidate_id==candidate_id)
            .order_by(Attempt.id.desc()))]


def _student_login_username(admission_no):
    """Use the student's admission number as the human-friendly login ID."""
    return str(admission_no or '').strip().upper()

def _new_student_password():
    alphabet='ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    return 'STU-' + ''.join(secrets.choice(alphabet) for _ in range(6))

def _new_admin_password():
    alphabet='ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'
    return 'ADM-' + ''.join(secrets.choice(alphabet) for _ in range(10))

def _provision_student_account(student_id, admission_no, password=None):
    """Create/reset the authentication layer without changing the student record itself.

    The plaintext password is returned once to the caller and is never stored; only
    the Werkzeug password hash is persisted. Existing login IDs remain stable.
    """
    current=one_scalar(select(Student.login_username).where(Student.id==student_id))
    username=current or _student_login_username(admission_no)
    base=username or 'STUDENT'
    suffix=1
    while one(select(Student.id).where(Student.login_username==username,
                                       Student.id!=student_id)):
        username=f'{base}-{suffix:02d}'; suffix+=1
    password=password or _new_student_password()
    db.session.execute(sa_update(Student).where(Student.id==student_id).values(
        login_username=username,login_password_hash=generate_password_hash(password),
        account_active=1,password_must_change=1))
    return username,password

def _student_account_row(student_id):
    return one(select(Student.id,Student.login_username,Student.login_password_hash,
                      Student.account_active,Student.password_must_change,
                      Student.last_login_at).where(Student.id==student_id))

def load_banks():
    banks={}
    os.makedirs(DATA, exist_ok=True)
    for fn in os.listdir(DATA):
        if fn.endswith('.json') and fn!='manifest.json':
            try:
                with open(os.path.join(DATA,fn),encoding='utf-8') as f:
                    b=json.load(f)
                if isinstance(b,dict) and b.get('id') and isinstance(b.get('questions'),list):
                    banks[b['id']]=b
            except (OSError, json.JSONDecodeError):
                continue
    return banks

def bank(bid): return load_banks().get(bid)

def candidate_usable_banks():
    # Engine verification banks remain available to administrators but are never
    # offered as a real entrance-examination paper assignment.
    return [b for b in load_banks().values() if b.get('questions') and b.get('id') != 'phase6b_test' and str(b.get('level','')).strip().upper() != 'TEST']

def normalize_entry_group(value):
    """Map the administrator's target-class wording to the two entrance sets."""
    v=''.join(ch.lower() for ch in str(value or '') if ch.isalnum())
    if any(token in v for token in ('jss1','year7','primary6','primarysix')):
        return 'year7'
    if any(token in v for token in ('ss1','sss1','year10','jss3','jss3to')):
        return 'year10'
    return None

def bank_subject(bank_obj):
    value=' '.join(str(bank_obj.get(k,'')) for k in ('subject','name','id')).lower()
    if 'general knowledge' in value or 'general_knowledge' in value or 'generalknowledge' in value:
        return 'general_knowledge'
    if 'mathemat' in value:
        return 'mathematics'
    if 'english' in value:
        return 'english'
    return None


ENTRANCE_SUBJECT_LABELS = {
    'mathematics': 'Mathematics',
    'english': 'English',
    'general_knowledge': 'General Knowledge',
}

# Fixed choice list for the "State of Origin" field on student admission, so it
# is a selection rather than free text a data-entry mistake could corrupt.
NIGERIAN_STATES = [
    'Abia','Adamawa','Akwa Ibom','Anambra','Bauchi','Bayelsa','Benue','Borno',
    'Cross River','Delta','Ebonyi','Edo','Ekiti','Enugu','FCT (Abuja)','Gombe',
    'Imo','Jigawa','Kaduna','Kano','Katsina','Kebbi','Kogi','Kwara','Lagos',
    'Nasarawa','Niger','Ogun','Ondo','Osun','Oyo','Plateau','Rivers','Sokoto',
    'Taraba','Yobe','Zamfara',
]

# Every graded test, examination, assignment and project belongs to one of
# these three terms within an academic session. Fixed to match the Nigerian
# school-year structure rather than free text, so a term is always one of
# exactly these three values everywhere it's stored or filtered on.
ACADEMIC_TERMS = ['First Term', 'Second Term', 'Third Term']

# A subject's term result is Exam (60) + Continuous Assessment (40) = 100.
# These two ceilings are fixed; only how CA_MAX_SCORE is split between tests,
# assignments and projects is configurable (see _ca_weights()).
EXAM_MAX_SCORE = 60
CA_MAX_SCORE = 40
CA_DEFAULT_WEIGHTS = {'test': 20, 'assignment': 10, 'project': 10}

# The fixed catalogue of billing categories a fee item can be classified as,
# and the fixed set of billing-rule options (each term stands on its own
# rather than being bundled into combos like "Second and third term only").
FINANCE_FEE_CATEGORIES = [
    'School Fees', 'Tuition', 'Uniform', 'Books & Stationery',
    'Feeding / Meals', 'Transport', 'Boarding / Hostel', 'Examination Fee',
    'Sports & Clubs', 'PTA Levy', 'Development Levy', 'ICT / Computer Fee',
    'Medical / Health Fee', 'Other',
]
FINANCE_FEE_APPLICABILITY = ['Full Session', 'First Term', 'Second Term', 'Third Term', 'One-time']

def entrance_subject_label(bank_id, fallback=''):
    """Return the candidate-facing subject name; never expose the internal bank ID."""
    subject = bank_subject(bank(bank_id) or {'id': bank_id, 'name': fallback or ''})
    return ENTRANCE_SUBJECT_LABELS.get(subject) or fallback or 'Assessment'

def entrance_paper_label(slot, bank_id, fallback=''):
    """Return a concise human-facing entrance paper label."""
    return f'Paper {slot}: {entrance_subject_label(bank_id, fallback)}'

def entrance_bank_display_name(bank_id, fallback=''):
    """Return the human-facing name for an entrance question bank.

    Internal bank IDs and the verbose stored examination title remain unchanged;
    this helper only controls what administrators see in question-bank contexts.
    """
    b = bank(bank_id) or {'id': bank_id, 'name': fallback or '', 'level': ''}
    subject = entrance_subject_label(bank_id, b.get('name') or fallback or '')
    group = bank_entry_group(b)
    target = {'year7': 'Year 7', 'year10': 'SSS 1'}.get(group)
    if target:
        return f'{subject} - Entrance Examination into {target}'
    # Safe fallback for a bank whose entry level cannot be resolved.
    return f'{subject} - Entrance Examination'

def bank_entry_group(bank_obj):
    explicit=normalize_entry_group(bank_obj.get('entry_group') or bank_obj.get('entry_level'))
    if explicit:
        return explicit
    # The existing Phase 6 banks use level/name/id rather than entry_group.
    return normalize_entry_group(' '.join(str(bank_obj.get(k,'')) for k in ('level','name','id')))

def _school_current_session():
    """The session results are filed against.

    Falls back to the most recent active session when none is marked
    current. The pre-merge code defined this function twice with
    different behaviour; Python kept the second, so that fallback is
    the behaviour every caller actually saw.
    """
    row=one(select(AcademicSession).where(AcademicSession.is_current==1,
                                          AcademicSession.active==1)
            .order_by(AcademicSession.id.desc()).limit(1))
    if not row:
        row=one(select(AcademicSession).where(AcademicSession.active==1)
                .order_by(AcademicSession.id.desc()).limit(1))
    return row['AcademicSession'] if row else None

def _active_sessions():
    return db.session.scalars(select(AcademicSession).where(AcademicSession.active==1)
        .order_by(AcademicSession.is_current.desc(),AcademicSession.id.desc())).all()


def _entrance_config_row(entry_group, subject, session_id=None, term='Full Session', active_only=True):
    if session_id is None:
        current=_school_current_session(); session_id=current['id'] if current else None
    if not session_id: return None
    stmt=(select(EntranceBankConfig,Examination.name.label('exam_name'),
                 Examination.active.label('exam_active'))
          .join(Examination,Examination.bank_id==EntranceBankConfig.bank_id)
          .where(EntranceBankConfig.entry_group==entry_group,
                 EntranceBankConfig.subject==subject,
                 EntranceBankConfig.session_id==session_id,
                 EntranceBankConfig.term==term))
    if active_only:
        stmt=stmt.where(EntranceBankConfig.active==1)
    row=one(stmt.order_by(EntranceBankConfig.id.desc()).limit(1))
    return _flatten(row,'EntranceBankConfig','exam_name','exam_active') if row else None

def _valid_question_configuration(question_count):
    try:
        count=int(question_count)
    except (TypeError,ValueError):
        return False,None
    if count < 1:
        return False,None
    marks=round(100.0/count,2)
    if round(marks*count,2) != 100.0:
        return False,marks
    return True,marks

def _entrance_config_for_bank(bank_id, entry_group, subject, session_id, term='Full Session'):
    return db.session.scalars(select(EntranceBankConfig).where(
        EntranceBankConfig.bank_id==bank_id,
        EntranceBankConfig.entry_group==entry_group,
        EntranceBankConfig.subject==subject,
        EntranceBankConfig.session_id==session_id,
        EntranceBankConfig.term==term).limit(1)).first()

def required_papers_for_target(target_class):
    """Return active entrance papers for the target class from the current configuration."""
    group=normalize_entry_group(target_class)
    labels={'mathematics':'Mathematics','english':'English','general_knowledge':'General Knowledge'}
    if not group:
        return [], list(labels.values())
    current=_school_current_session()
    ordered=[]; missing=[]
    if current:
        for subject in ('mathematics','english','general_knowledge'):
            cfg=_entrance_config_row(group,subject,current['id'])
            if not cfg:
                missing.append(labels[subject])
            else:
                b=bank(cfg['bank_id'])
                if b and b.get('questions'):
                    ordered.append(b)
                else:
                    missing.append(labels[subject])
    else:
        missing=list(labels.values())
    return ordered,missing

def init_admin_security():
    """Seed the permission catalogue, system roles and bootstrap Super Admin.

    The table definitions that used to live here as a CREATE TABLE script are now
    declared once in models.py and created by db.create_all(); this function is
    only responsible for seeding reference data.
    """
    now=datetime.now(timezone.utc).isoformat()
    _ignore_insert(Permission, [
        {'code':code,'name':name,'module':module,'description':description}
        for code,name,module,description in ADMIN_PERMISSION_DEFS
    ])
    _ignore_insert(AdminType, [
        {'name':'Super Admin','description':'Full system authority. Unrestricted by ordinary permission or scope checks.','is_system':1,'created_at':now},
        {'name':'Ordinary Admin','description':'Administrator whose access is controlled by assigned permissions and scopes.','is_system':0,'created_at':now},
    ])
    db.session.flush()
    super_role=one_scalar(select(AdminType.id).where(AdminType.name=='Super Admin'))
    # System Super Admin accounts are intrinsically unrestricted. Remove any
    # stale boundaries that may have been created by earlier UI versions so
    # the directory cannot misleadingly report a scoped Super Admin.
    db.session.execute(
        sa_delete(AdminScope).where(
            AdminScope.admin_id.in_(select(Admin.id).where(Admin.admin_type_id==super_role))
        )
    )
    _ignore_insert(AdminTypePermission, [
        {'admin_type_id':super_role,'permission_id':pid,'granted_at':now}
        for pid in db.session.scalars(select(Permission.id)).all()
    ])
    # Seed professional role bundles once. Existing custom roles are preserved.
    _ignore_insert(AdminType, [
        {'name':role_name,'description':spec['description'],'is_system':0,'active':1,'created_at':now}
        for role_name,spec in ADMIN_ROLE_PRESETS.items()
    ])
    db.session.flush()
    role_ids={name:rid for rid,name in tuples(select(AdminType.id,AdminType.name))}
    perm_ids={code:pid for pid,code in tuples(select(Permission.id,Permission.code))}
    _ignore_insert(AdminTypePermission, [
        {'admin_type_id':role_ids[role_name],'permission_id':perm_ids[code],'granted_at':now}
        for role_name,spec in ADMIN_ROLE_PRESETS.items()
        if role_name in role_ids
        for code in spec['permissions']
        if code in perm_ids
    ])
    # CRAINBOW_SUPERADMIN_USERNAME / CRAINBOW_ADMIN_PASSWORD are read only to
    # bootstrap the very first Super Admin. The check is "does a Super Admin
    # already exist", not "does one exist under this exact username" - matching
    # by username would let a later change to CRAINBOW_SUPERADMIN_USERNAME
    # create a second Super Admin instead of being ignored, which is exactly
    # the surprise this guards against. Once any Super Admin exists, the
    # database is authoritative and both .env values are ignored on every
    # subsequent startup, however they're set.
    if not one(select(Admin.id).where(Admin.admin_type_id==super_role)):
        username=os.environ.get('CRAINBOW_SUPERADMIN_USERNAME','superadmin').strip().lower() or 'superadmin'
        bootstrap_password=ADMIN_PASSWORD or secrets.token_urlsafe(18)
        if not ADMIN_PASSWORD:
            print('CRAINBOW: generated a one-time Super Admin bootstrap password. Set CRAINBOW_ADMIN_PASSWORD before creating a fresh database.', file=sys.stderr)
            print(f'CRAINBOW_BOOTSTRAP_PASSWORD={bootstrap_password}', file=sys.stderr)
        db.session.add(Admin(username=username,display_name='Super Admin',
                             password_hash=generate_password_hash(bootstrap_password),
                             admin_type_id=super_role,active=1,created_at=now))


def admin_role_names(admin_id):
    return list(db.session.scalars(
        select(AdminType.name)
            .join(AdminRoleAssignment,AdminRoleAssignment.admin_type_id==AdminType.id)
            .where(AdminRoleAssignment.admin_id==admin_id,AdminType.active==1)
            .order_by(AdminType.name)
    ).all())



def _admin_role_options():
    # System roles and the legacy generic Ordinary Admin role are deliberately
    # hidden from normal account creation. Staff are assigned a job role instead.
    preset_first=sa.case((AdminType.name.in_(tuple(ADMIN_ROLE_PRESETS.keys())),0),else_=1)
    return db.session.scalars(
        select(AdminType)
            .where(AdminType.active==1,AdminType.is_system==0,AdminType.name!='Ordinary Admin')
            .order_by(preset_first,AdminType.name)
    ).all()

def _admin_scope_label(scope_type, scope_value):
    labels={'global':'Whole entrance examination','academic_session':'Academic session','class':'Entry level / class','subject':'Subject','bank':'Question bank'}
    return labels.get(scope_type, scope_type.replace('_',' ').title()) + ('' if scope_value in (None,'','*') else f' — {scope_value}')


def _create_control_item(title, description, category, target_type=None, target_id=None, requested_by=None):
    try:
        item=AdminControlItem(title=title,description=description,category=category,
                              target_type=target_type,
                              target_id=str(target_id) if target_id is not None else None,
                              status='open',requested_by=requested_by,
                              created_at=datetime.now(timezone.utc).isoformat())
        db.session.add(item); db.session.commit()
        return item.id
    except Exception:
        db.session.rollback()
        return None

def _resource_locked(resource_type, resource_id):
    return bool(one(select(AdminResourceLock.id).where(
        AdminResourceLock.resource_type==resource_type,
        AdminResourceLock.resource_id==str(resource_id),
        AdminResourceLock.unlocked_at.is_(None))))

def _lock_resource(resource_type, resource_id, reason, admin_id):
    now=datetime.now(timezone.utc).isoformat()
    stmt=sqlite_insert(AdminResourceLock).values(
        resource_type=resource_type,resource_id=str(resource_id),reason=reason,
        locked_by=admin_id,locked_at=now,unlocked_by=None,unlocked_at=None)
    db.session.execute(stmt.on_conflict_do_update(
        index_elements=['resource_type','resource_id'],
        set_={'reason':stmt.excluded.reason,'locked_by':stmt.excluded.locked_by,
              'locked_at':stmt.excluded.locked_at,'unlocked_by':None,'unlocked_at':None}))
    db.session.commit()

def _unlock_resource(resource_type, resource_id, admin_id):
    now=datetime.now(timezone.utc).isoformat()
    db.session.execute(sa_update(AdminResourceLock).where(
        AdminResourceLock.resource_type==resource_type,
        AdminResourceLock.resource_id==str(resource_id),
        AdminResourceLock.unlocked_at.is_(None)
    ).values(unlocked_by=admin_id,unlocked_at=now))
    db.session.commit()

def _notification_counts(admin_id):
    unread=one_scalar(select(func.count()).select_from(AdminNotification).where(
        AdminNotification.admin_id==admin_id,AdminNotification.read_at.is_(None)),0)
    open_controls=one_scalar(select(func.count()).select_from(AdminControlItem).where(
        AdminControlItem.status=='open'),0)
    return unread,open_controls

def _unread_admin_messages(admin_id):
    if not admin_id: return 0
    return one_scalar(select(func.count()).select_from(AdminMessage).where(
        AdminMessage.recipient_admin_id==admin_id,AdminMessage.read_at.is_(None)),0)

def _unread_admin_message_summaries(admin_id, limit=8):
    """Return unread direct-message notifications with stable sender identity."""
    if not admin_id: return []
    return all_rows(
        select(AdminMessage.id,AdminMessage.body,AdminMessage.sent_at,
               Admin.id.label('sender_id'),Admin.display_name.label('sender_name'),
               Admin.username.label('sender_username'),Admin.photo_path.label('sender_photo'))
            .join(Admin,Admin.id==AdminMessage.sender_admin_id)
            .where(AdminMessage.recipient_admin_id==admin_id,AdminMessage.read_at.is_(None))
            .order_by(AdminMessage.sent_at.desc(),AdminMessage.id.desc())
            .limit(limit))


def admin_can_delegate_roles(actor_id, role_ids):
    """An administrator may only grant roles whose permissions they already hold."""
    actor=current_admin()
    if actor and actor['admin_type_system']: return True
    own=admin_permission_codes(actor_id)
    if not role_ids: return False
    rows=tuples(
        select(AdminTypePermission.admin_type_id,Permission.code)
            .join(Permission,Permission.id==AdminTypePermission.permission_id)
            .where(AdminTypePermission.admin_type_id.in_(list(role_ids))))
    by_role={rid:set() for rid in role_ids}
    for type_id,code in rows: by_role.setdefault(type_id,set()).add(code)
    return all(perms.issubset(own) for perms in by_role.values())



# ---------------- guardian/parent notifications ----------------
# Moved to core/notifications.py.
from core.notifications import _notify_guardians_of_school_work  # noqa: E402

# _ca_weights/_set_ca_weights (school domain, still in this file) also need this.
from blueprints.finance.helpers import _primary_school_id  # noqa: E402



def init_db():
    """Create any missing tables and seed the reference data the app expects.

    The CREATE TABLE / ALTER TABLE script that used to live here is gone: the
    schema is declared once in models.py and realised by create_all(), while
    historical upgrades stay in migrations/.

    Callable with or without an active Flask application context. Every
    SQLAlchemy call below needs one, but `python app.py` and the verify_*.py
    scripts call this before any request exists, so it pushes its own when
    there is none rather than making each caller remember.
    """
    if has_app_context():
        return _init_db()
    with app.app_context():
        return _init_db()


def _init_db():
    db.create_all()
    _add_missing_columns()
    _widen_parent_feedback_reply_admin_id()
    now=datetime.now(timezone.utc).isoformat()
    # Seed the supported class structure. Primary 5 is deliberately optional.
    class_seed=[
        ('Primary 1','Primary',1,0,1),('Primary 2','Primary',2,0,1),('Primary 3','Primary',3,0,1),
        ('Primary 4','Primary',4,0,1),('Primary 5','Primary',5,1,0),('Primary 6','Primary',6,0,1),
        ('JSS 1','JSS',7,0,1),('JSS 2','JSS',8,0,1),('JSS 3','JSS',9,0,1),
        ('SSS 1','SSS',10,0,1),('SSS 2','SSS',11,0,1),('SSS 3','SSS',12,0,1)
    ]
    _ignore_insert(SchoolClass, [
        {'name':name,'stage':stage,'level_order':level,'optional':optional,'active':active,'created_at':now}
        for name,stage,level,optional,active in class_seed
    ])
    if not one(select(AcademicSession.id).order_by(AcademicSession.id).limit(1)):
        db.session.add(AcademicSession(name='2026/2027',is_current=1,active=1,created_at=now))
    _seed_school_tenant(now)
    init_admin_security()
    sync_examinations()
    db.session.commit()
    _record_schema_baseline()

def sync_examinations():
    """Mirror the JSON question banks into the examinations table."""
    now=datetime.now(timezone.utc).isoformat()
    for b in load_banks().values():
        stmt=sqlite_insert(Examination).values(
            bank_id=b['id'],name=b.get('name',b['id']),
            duration_seconds=int(b.get('duration_seconds',3600)),
            version=b.get('version','1.0'),question_count=len(b['questions']),created_at=now)
        db.session.execute(stmt.on_conflict_do_update(
            index_elements=['bank_id'],
            set_={'name':stmt.excluded.name,'duration_seconds':stmt.excluded.duration_seconds,
                  'version':stmt.excluded.version,'question_count':stmt.excluded.question_count}))
    db.session.commit()

def get_attempt(aid):
    return obj(Attempt, aid)

def remaining(a): return max(0,int((datetime.fromisoformat(a['expires_at'])-datetime.now(timezone.utc)).total_seconds()))

def grade(aid, auto=False, force=False):
    a=obj(Attempt, aid)
    if not a: return None
    if a.status in ('submitted','expired') and not force: return a
    snapshot=tuples(select(AttemptQuestion.question_id,AttemptQuestion.correct_option,
                           AttemptQuestion.points).where(AttemptQuestion.attempt_id==aid))
    answers=_answers_for_attempt(aid)
    if snapshot:
        score=sum(int(points or 1) for qid,correct,points in snapshot if answers.get(qid)==correct)
        max_score=sum(int(points or 1) for _,_,points in snapshot)
    else:
        # Legacy attempts are graded from the legacy bank only until they are archived;
        # all new attempts are snapshotted at start.
        b=bank(a.bank_id)
        score=sum(q.get('points',1) for q in (b or {}).get('questions',[]) if answers.get(q['id'])==q['answer'])
        max_score=sum(q.get('points',1) for q in (b or {}).get('questions',[]))
    pct=(score/max_score*100) if max_score else 0
    if not force:
        a.submitted_at=datetime.now(timezone.utc).isoformat()
        a.status='expired' if auto else 'submitted'
    a.score=score; a.max_score=max_score; a.percentage=pct
    db.session.commit()
    return a

def candidate_record(cid):
    if not cid: return None
    return db.session.scalars(
        select(Candidate).where(Candidate.id==cid,Candidate.active==1)).first()

def candidate_has_unused_retake(candidate_id, candidate_name, bank_id):
    return one(select(RetakeGrant.id).where(
        RetakeGrant.bank_id==bank_id,RetakeGrant.candidate_id==candidate_id,
        RetakeGrant.used_at.is_(None)).order_by(RetakeGrant.id.asc()).limit(1))

def _candidate_paper_attempts(cid):
    """Each assigned paper with its most recent attempt."""
    latest=(select(func.max(Attempt.id))
            .where(Attempt.candidate_id==CandidatePaper.candidate_id,
                   Attempt.bank_id==CandidatePaper.bank_id)
            .correlate(CandidatePaper).scalar_subquery())
    return all_rows(
        select(CandidatePaper.id.label('paper_id'),CandidatePaper.slot,
               CandidatePaper.bank_id,Examination.name.label('exam_name'),
               Attempt.id.label('attempt_id'),Attempt.status,Attempt.score,
               Attempt.max_score,Attempt.percentage,Attempt.started_at,
               Attempt.submitted_at)
        .select_from(CandidatePaper)
        .outerjoin(Examination,Examination.bank_id==CandidatePaper.bank_id)
        .outerjoin(Attempt,Attempt.id==latest)
        .where(CandidatePaper.candidate_id==cid)
        .order_by(CandidatePaper.slot))


def candidate_cumulative(cid):
    rows=_candidate_paper_attempts(cid)

    # Per-attempt question/correct counts in one query rather than one per paper.
    attempt_ids=[r['attempt_id'] for r in rows
                 if r['status'] in ('submitted','expired') and r['attempt_id']]
    counts={}
    if attempt_ids:
        for aid,qcount,correct in tuples(
            select(AttemptQuestion.attempt_id,
                   func.count().label('question_count'),
                   func.sum(sa.case(
                       (and_(Answer.option_index.is_not(None),
                             AttemptQuestion.correct_option.is_not(None),
                             Answer.option_index==AttemptQuestion.correct_option), 1),
                       else_=0)).label('correct_count'))
            .select_from(AttemptQuestion)
            .outerjoin(Answer,and_(Answer.attempt_id==AttemptQuestion.attempt_id,
                                   Answer.question_id==AttemptQuestion.question_id))
            .where(AttemptQuestion.attempt_id.in_(attempt_ids))
            .group_by(AttemptQuestion.attempt_id)):
            counts[aid]=(int(qcount or 0),int(correct or 0))

    enriched=[]
    for row in rows:
        item=dict(row)
        exam_name=str(item.get('exam_name') or '').strip()
        subject_label=exam_name or ''
        if not subject_label:
            # Fall back to the bank id when the examination row is missing.
            bank_text=str(item.get('bank_id') or '').lower()
            if 'english' in bank_text:
                subject_label='English'
            elif 'math' in bank_text:
                subject_label='Mathematics'
            elif 'spell' in bank_text or 'vocab' in bank_text:
                subject_label='Spelling & Vocabulary'
            else:
                subject_label='Subject'
        item['subject_label']=subject_label
        item['question_count'],item['correct_count']=counts.get(item.get('attempt_id'),(0,0))
        enriched.append(item)

    done=[r for r in enriched if r['status'] in ('submitted','expired')]
    total_score=sum((r['score'] or 0) for r in done)
    total_max=sum((r['max_score'] or 0) for r in done)
    completed=len(done)
    pct=(total_score/total_max*100) if total_max else 0
    return enriched,total_score,total_max,pct,completed

def _result_file_data_uri(path):
    """Return a local image file as a browser-safe data URI."""
    try:
        p=Path(path)
        if not p.exists() or not p.is_file():
            return ''
        mime=mimetypes.guess_type(str(p))[0] or 'application/octet-stream'
        encoded=base64.b64encode(p.read_bytes()).decode('ascii')
        return f'data:{mime};base64,{encoded}'
    except Exception:
        return ''

def _candidate_photo_filesystem_path(candidate):
    """Resolve the candidate's stored photo path without changing database data."""
    keys=list(candidate.keys())

    raw=''
    for key in ('photo_path','photo','candidate_photo','passport_photo','image_path'):
        if key in keys and candidate[key]:
            raw=str(candidate[key]).strip()
            break

    if not raw:
        return None

    raw=raw.replace('\\','/')

    candidates=[]

    if raw.startswith('/static/'):
        candidates.append(Path(__file__).resolve().parent / raw.lstrip('/'))
    elif raw.startswith('static/'):
        candidates.append(Path(__file__).resolve().parent / raw)
    else:
        p=Path(raw)
        if p.is_absolute():
            candidates.append(p)
        else:
            candidates.append(Path(__file__).resolve().parent / raw)
            candidates.append((Path(__file__).resolve().parent / 'static' / raw.lstrip('/')))

    for p in candidates:
        if p.exists() and p.is_file():
            return p

    return None

def _chrome_result_png(candidate, papers, total_score, total_max, pct,
                       total_questions, total_correct,
                       average_subject_percentage, grade):
    """Render the official result card to PNG using installed Chrome."""

    import subprocess
    import struct
    from pathlib import Path

    from flask import (
        render_template,
        has_request_context,
        has_app_context,
        current_app,
    )

    project_root = Path(__file__).resolve().parent

    chrome_paths = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ]

    chrome = next(
        (p for p in chrome_paths if p.exists()),
        None
    )

    if chrome is None:
        raise RuntimeError(
            "Google Chrome was not found."
        )

    output_dir = (
        project_root /
        "static" /
        "generated" /
        "results"
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    token = datetime.now().strftime(
        "%Y%m%d_%H%M%S_%f"
    )

    png_path = (
        output_dir /
        f"result_{candidate['id']}_{token}.png"
    )

    html_path = (
        output_dir /
        f"result_{candidate['id']}_{token}.html"
    )

    logo_path = (
        project_root /
        "static" /
        "images" /
        "school_logo.png"
    )

    photo_path = _candidate_photo_filesystem_path(
        candidate
    )

    if not logo_path.is_file():
        raise RuntimeError(
            f"School logo not found: {logo_path}"
        )

    if photo_path is None:
        raise RuntimeError(
            "Candidate photograph could not be resolved."
        )

    logo_data = _result_file_data_uri(
        logo_path
    )

    photo_data = _result_file_data_uri(
        photo_path
    )

    render_kwargs = {
        "candidate": candidate,
        "papers": papers,
        "total_score": total_score,
        "total_max": total_max,
        "pct": pct,
        "total_questions": total_questions,
        "total_correct": total_correct,
        "average_subject_percentage": average_subject_percentage,
        "grade": grade,
        "logo_data": logo_data,
        "photo_data": photo_data,
    }

    if has_request_context():

        html = render_template(
            "admin_candidate_result_image.html",
            **render_kwargs
        )

    elif has_app_context():

        app_obj = current_app._get_current_object()

        with app_obj.test_request_context(
            "/admin/candidates/result/image"
        ):
            html = render_template(
                "admin_candidate_result_image.html",
                **render_kwargs
            )

    else:
        raise RuntimeError(
            "Renderer requires a Flask application/request context."
        )

    html_path.write_text(
        html,
        encoding="utf-8"
    )

    try:

        command = [
            str(chrome),
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--allow-file-access-from-files",
            "--run-all-compositor-stages-before-draw",
            "--virtual-time-budget=3000",
            "--window-size=1400,1100",
            f"--screenshot={str(png_path)}",
            "file:///" +
            str(html_path).replace("\\", "/"),
        ]

        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=60,
        )

        if completed.returncode != 0:

            detail = (
                completed.stderr
                or completed.stdout
                or "Unknown Chrome error."
            )

            raise RuntimeError(
                "Chrome failed:\n" + detail
            )

        if not png_path.is_file():
            raise RuntimeError(
                "Chrome did not create the result PNG."
            )

        size = png_path.stat().st_size

        if size < 10000:
            raise RuntimeError(
                f"Generated PNG is too small: {size} bytes."
            )

        with png_path.open("rb") as fh:

            signature = fh.read(8)

            if signature != b"\x89PNG\r\n\x1a\n":
                raise RuntimeError(
                    "Generated file is not a valid PNG."
                )

            ihdr_length = fh.read(4)
            ihdr_type = fh.read(4)

            if (
                len(ihdr_length) != 4
                or ihdr_type != b"IHDR"
            ):
                raise RuntimeError(
                    "PNG IHDR validation failed."
                )

            dimensions = fh.read(8)

            if len(dimensions) != 8:
                raise RuntimeError(
                    "PNG dimensions could not be read."
                )

            width, height = struct.unpack(
                ">II",
                dimensions
            )

        if width < 500 or height < 500:
            raise RuntimeError(
                f"Invalid PNG dimensions: {width}x{height}"
            )

        return png_path, html_path

    except Exception:

        if png_path.exists():
            try:
                png_path.unlink()
            except Exception:
                pass

        raise

    finally:

        if html_path.exists():
            try:
                html_path.unlink()
            except Exception:
                pass

def premium_result_metrics(papers):
    """Calculate cumulative presentation metrics from completed papers."""
    completed_papers = [
        p for p in papers
        if p.get("status") in ("submitted", "expired")
    ]

    total_questions = sum(
        int(p.get("question_count") or 0)
        for p in completed_papers
    )

    total_correct = sum(
        int(p.get("correct_count") or 0)
        for p in completed_papers
    )

    percentages = [
        float(p["percentage"])
        for p in completed_papers
        if p.get("percentage") is not None
    ]

    average_subject_percentage = (
        sum(percentages) / len(percentages)
        if percentages else 0
    )

    return (
        total_questions,
        total_correct,
        average_subject_percentage
    )

def _new_candidate_code():
    year=datetime.now().year; prefix=f'CRMS-{year}-'
    latest=one_scalar(select(Candidate.candidate_code)
                      .where(Candidate.candidate_code.like(prefix+'%'))
                      .order_by(Candidate.id.desc()).limit(1))
    if latest:
        try: n=int(latest.rsplit('-',1)[1])+1
        except (IndexError,ValueError): n=1
    else: n=1
    code=f'{prefix}{n:04d}'
    while one(select(Candidate.id).where(Candidate.candidate_code==code)):
        n+=1; code=f'{prefix}{n:04d}'
    return code

def _new_candidate_password():
    alphabet=string.ascii_uppercase+string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(8))

def csrf_token():
    """Return the session-bound CSRF token used by protected forms."""
    token=session.get('_csrf_token')
    if not token:
        token=secrets.token_urlsafe(32)
        session['_csrf_token']=token
    return token

@app.context_processor
def inject_csrf_token():
    admin=current_admin()
    unread=open_controls=0
    if admin:
        unread,open_controls=_notification_counts(admin['id'])
    return {
        'csrf_token': csrf_token,
        'current_admin': admin,
        'admin_unread_notifications': unread,
        'admin_open_controls': open_controls,
        'admin_unread_messages': _unread_admin_messages(admin['id']) if admin else 0,
        'admin_unread_message_summaries': _unread_admin_message_summaries(admin['id']) if admin else [],
        'admin_role_names': admin_role_names(admin['id']) if admin else [],
        'is_super_admin_ui': is_super_admin(admin),
        'admin_has_permission': admin_has_permission,
        'entrance_subject_label': entrance_subject_label,
        'entrance_paper_label': entrance_paper_label,
        'entrance_bank_display_name': entrance_bank_display_name,
    }


def _admin_workspace_for_path(path):
    """Return the workspace required by an admin URL, or None for shared/auth routes."""
    if path.startswith('/admin/school'):
        return 'school'
    if path == '/admin/examination' or path.startswith(('/admin/candidates', '/admin/banks', '/admin/results', '/admin/rankings', '/admin/attempts', '/admin/practice-tests', '/admin/entrance-config')):
        return 'entrance'
    return None

@app.before_request
def enforce_admin_workspace_boundary():
    """Require deliberate workspace selection and prevent cross-workspace navigation."""
    if not session.get('admin_id') or not request.path.startswith('/admin'):
        return

    
    
    
    if request.path in ('/admin/login', '/admin/logout', '/admin/password', '/admin/workspace/entrance', '/admin/workspace/school'):
        
        
        
        
        
        
        return
    if request.path == '/admin/home':
        session.pop('admin_workspace', None)
        return

    required = _admin_workspace_for_path(request.path)
    selected = session.get('admin_workspace')

    
    
    if not selected:
        return redirect(url_for('admin_workspace_home'))

    if required and selected != required:
        flash('Exit the current workspace and choose the other workspace from Workspace Home.', 'error')
        return redirect(url_for('admin_workspace_home'))

@app.before_request
def track_live_presence():
    if request.path.startswith('/static/'):
        return
    if _presence_identity()[0]:
        try:
            touch_presence()
        except Exception:
            app.logger.exception('Presence update failed')

@app.route('/presence/heartbeat')
def presence_heartbeat():
    if not _presence_identity()[0]:
        return jsonify(ok=False), 401
    touch_presence()
    return jsonify(ok=True)

def csrf_check_request():
    token=request.form.get('_csrf_token','') or request.headers.get('X-CSRF-Token',''); expected=session.get('_csrf_token')
    return bool(expected and token and secrets.compare_digest(token,expected))

# Moved to core/public_settings.py.
from core.public_settings import _public_settings, _public_page  # noqa: E402

# ---------------- public marketing site ----------------
# Moved to blueprints/public/routes.py.
import blueprints.public.routes  # noqa: F401,E402

# ---------------- unified login/logout/password recovery ----------------
# Moved to blueprints/auth/routes.py.
import blueprints.auth.routes  # noqa: F401,E402

@app.route('/')
def index():
    # The public front door also tells an already signed-in visitor who they are
    # and where to continue, without forcing them back through the login form.
    signed_in_kind = None
    signed_in_name = None
    continue_url = url_for('login')
    if session.get('admin_id'):
        signed_in_kind = 'admin'
        me = current_admin()
        signed_in_name = me['display_name'] if me else None
        continue_url = url_for('admin_workspace_home')
    elif session.get('parent_id'):
        signed_in_kind = 'parent'
        row = obj(ParentAccount, session['parent_id'])
        signed_in_name = row.display_name if row else None
        continue_url = url_for('parent_dashboard')
    elif session.get('student_id'):
        signed_in_kind = 'student'
        row = obj(Student, session['student_id'])
        signed_in_name = f"{row.first_name} {row.last_name}" if row else None
        continue_url = url_for('student_dashboard')
    elif session.get('candidate_id'):
        signed_in_kind = 'candidate'
        row = obj(Candidate, session['candidate_id'])
        signed_in_name = row.candidate_name if row else None
        continue_url = url_for('candidate_dashboard')
    public_settings=_public_settings()
    news=db.session.scalars(select(SchoolPublicNews).where(SchoolPublicNews.published==1)
        .order_by(func.coalesce(SchoolPublicNews.published_at,SchoolPublicNews.created_at).desc(),
                  SchoolPublicNews.id.desc()).limit(6)).all()
    return render_template('public_home.html', signed_in_kind=signed_in_kind, signed_in_name=signed_in_name, continue_url=continue_url, public_settings=public_settings, news=news)

@app.after_request
def apply_security_headers(response):
    response.headers.setdefault('X-Content-Type-Options','nosniff')
    response.headers.setdefault('X-Frame-Options','DENY')
    response.headers.setdefault('Referrer-Policy','strict-origin-when-cross-origin')
    response.headers.setdefault('Permissions-Policy','camera=(), microphone=(), geolocation=()')
    response.headers.setdefault('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
    if ENVIRONMENT in ('production','prod'):
        response.headers.setdefault('Strict-Transport-Security','max-age=31536000; includeSubDomains')
    if response.mimetype == 'text/html' and _presence_identity()[0]:
        try:
            body=response.get_data(as_text=True)
            if 'presence_heartbeat' not in body:
                body=body.replace('</body>', "<script>setInterval(function(){fetch('/presence/heartbeat',{credentials:'same-origin'}).catch(function(){});},30000);</script></body>")
                response.set_data(body)
        except Exception:
            pass
    return response


app.config.update(
    MAX_CONTENT_LENGTH=int(os.environ.get('CRAINBOW_MAX_REQUEST_BYTES', 8 * 1024 * 1024)),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=(ENVIRONMENT in ('production','prod')),
)

# ---------------- unified login identity ----------------
# Moved to core/accounts.py.
from core.accounts import _clear_identity_sessions  # noqa: E402


# ---------------- parent portal + admin parent management ----------------
# Moved to blueprints/parents/routes.py.
import blueprints.parents.routes  # noqa: F401,E402
# _ensure_parent (still in this file, part of student admissions) also needs
# this to provision a parent account when registering a new student.
from blueprints.parents.helpers import _new_parent_password  # noqa: E402

def _release_due_school_results():
    """Release every approved result whose session release time has passed."""
    now=datetime.now(timezone.utc).isoformat()
    due=(select(AcademicSession.id)
         .where(AcademicSession.result_release_at.is_not(None),
                AcademicSession.result_release_at <= now)
         .scalar_subquery())
    db.session.execute(sa_update(SchoolStudentResult)
        .where(SchoolStudentResult.status=='approved',
               SchoolStudentResult.session_id.in_(due))
        .values(status='released',released_at=now))


def _entrance_config_select(*extra):
    """Entrance configs joined to their bank and session."""
    return (select(EntranceBankConfig,
                   Examination.name.label('exam_name'),
                   AcademicSession.name.label('session_name'),*extra)
            .join(Examination,Examination.bank_id==EntranceBankConfig.bank_id)
            .join(AcademicSession,AcademicSession.id==EntranceBankConfig.session_id))


ENTRANCE_CONFIG_EXTRA=('exam_name','session_name')

# ---------------- student portal ----------------
# Moved to blueprints/student_portal/routes.py.
import blueprints.student_portal.routes  # noqa: F401,E402

# ---------------- entrance-candidate self-service portal ----------------
# Moved to blueprints/candidate_portal/routes.py.
import blueprints.candidate_portal.routes  # noqa: F401,E402


@app.route('/admin/password',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_password_change():
    """Require a newly-created administrator to replace the one-time password."""
    aid=session.get('admin_id')
    admin=db.session.scalars(select(Admin).where(Admin.id==aid,Admin.active==1)).first()
    if not admin:
        _clear_identity_sessions(); return redirect(url_for('login'))
    errors=[]
    if request.method=='POST':
        current=request.form.get('current_password','')
        new=request.form.get('new_password','')
        confirm=request.form.get('confirm_password','')
        if not check_password_hash(admin['password_hash'] or '',current): errors.append('Your temporary/current password is incorrect.')
        if len(new)<8: errors.append('Your new password must be at least 8 characters long.')
        if new!=confirm: errors.append('The new password and confirmation do not match.')
        if not errors:
            db.session.execute(sa_update(Admin).where(Admin.id==aid).values(
                password_hash=generate_password_hash(new),password_must_change=0))
            db.session.commit()
            audit_log('admin_password_changed','authentication','admin',aid,{'username':admin['username']},True,admin)
            flash('Your administrator password has been changed successfully.','success')
            return redirect(url_for('admin_workspace_home'))
    return render_template('admin_password.html',admin=admin,errors=errors)

# admin_presence (below) still needs this; _school_class_allowed itself
# moved to blueprints/school/helpers.py, which has no dependency on app.py.
from blueprints.school.helpers import _school_class_allowed  # noqa: E402

def admin_has_workspace_access(admin_id, workspace):
    if is_super_admin(): return True
    if workspace=='school':
        return any(admin_has_permission(admin_id,c) for c,_,_,_ in ADMIN_PERMISSION_DEFS if c.startswith('school.'))
    return any(admin_has_permission(admin_id,c) for c,_,_,_ in ADMIN_PERMISSION_DEFS if c in {'dashboard.view','candidates.view','question_banks.view','attempts.view','results.view'})

@app.route('/admin/presence')
@admin_required
def admin_presence():
    me=current_admin()
    if not admin_has_permission(me['id'],'presence.view'):
        return admin_access_error('presence.view')
    counts,rows=online_presence()
    if not me['admin_type_system']:
        # Class-scoped staff only see students within their own classes.
        class_map={name:cid for cid,name in tuples(
            select(SchoolClass.id,SchoolClass.name))}
        rows=[r for r in rows if r['account_type']=='admin'
              or (r['class_name'] and _school_class_allowed(me['id'],class_map.get(r['class_name'])))]
    def _wat(iso_str):
        try:
            dt=datetime.fromisoformat(str(iso_str).replace('Z','+00:00'))
            if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
            return (dt+timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
        except (TypeError,ValueError): return str(iso_str or '—')
    rows=[{**dict(r),'last_seen_wat':_wat(r['last_seen'])} for r in rows]
    return render_template('admin_presence.html',counts=counts,rows=rows)

@app.route('/admin/home')
@admin_required
def admin_workspace_home():
    # Workspace Home is a neutral boundary. No workspace remains selected here.
    session.pop('admin_workspace', None)
    me=current_admin()
    return render_template('admin_workspace_home.html',admin=me,can_entrance=admin_has_workspace_access(me['id'],'entrance'),can_school=admin_has_workspace_access(me['id'],'school'))

@app.route('/admin/workspace/entrance')
@admin_required
def select_entrance_workspace():
    session['admin_workspace']='entrance'
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/workspace/school')
@admin_required
def select_school_workspace():
    session['admin_workspace']='school'
    return redirect(url_for('admin_school_home'))

@app.route('/admin')
@admin_required
def admin_root():
    # Legacy /admin entry point now opens the neutral workspace chooser.
    return redirect(url_for('admin_workspace_home'))

# ---------------- entrance examination admin ----------------
# Moved to blueprints/entrance/routes.py.
import blueprints.entrance.routes  # noqa: F401,E402

# ---------------- school portal ----------------
# Moved to blueprints/school/routes.py.
import blueprints.school.routes  # noqa: F401,E402



# ---------------- admin messaging + administration ----------------
# Moved to blueprints/administration/routes.py.
import blueprints.administration.routes  # noqa: F401,E402



# ---------------- admin finance ----------------
# Moved to blueprints/finance/routes.py.
import blueprints.finance.routes  # noqa: F401,E402

# ---------------- admin library ----------------
# Moved to blueprints/library/routes.py.
import blueprints.library.routes  # noqa: F401,E402

def _error_page_context():
    """Whether the visitor is signed in, and where their "back" should
    fall back to when there's no usable browser history (e.g. a bookmarked
    broken link) — their own dashboard, never the public homepage, since a
    signed-in admin/parent/student/candidate has no reason to be dropped
    back on the public marketing site.
    """
    kind,_=_presence_identity()
    fallback={'admin':'admin_workspace_home','parent':'parent_dashboard',
              'student':'student_dashboard','candidate':'candidate_dashboard'}.get(kind)
    return kind,url_for(fallback) if fallback else url_for('index')

@app.errorhandler(HTTPException)
def handle_http_exception(e):
    """Branded error pages instead of Werkzeug's plain default ones.

    Covers every abort()/routing error (404s, the CSRF-check 403s, etc.).
    Deliberate, permission-checked 403s that already render their own page
    (admin_access_error -> admin_forbidden.html) return a normal response
    rather than raising, so they never reach this handler.
    """
    code=e.code or 500
    kind,fallback_url=_error_page_context()
    if code>=500:
        db.session.rollback()
        app.logger.exception('Server error handling %s %s',request.method,request.path)
        return render_template('error_5xx.html',code=code,public_settings=_public_settings(),
                                logged_in=bool(kind),fallback_url=fallback_url),code
    return render_template('error_4xx.html',code=code,name=e.name,description=e.description,
                            public_settings=_public_settings(),
                            logged_in=bool(kind),fallback_url=fallback_url),code

@app.errorhandler(Exception)
def handle_unexpected_exception(e):
    """Last-resort catch for anything that isn't a deliberate abort().

    Never leaks the exception itself to the visitor; it's only ever logged.
    """
    db.session.rollback()
    app.logger.exception('Unhandled exception handling %s %s',request.method,request.path)
    kind,fallback_url=_error_page_context()
    return render_template('error_5xx.html',code=500,public_settings=_public_settings(),
                            logged_in=bool(kind),fallback_url=fallback_url),500

@app.route('/health')
def health(): return jsonify(status='ok')


if __name__=='__main__': init_db(); app.run(host='0.0.0.0',port=int(os.environ.get('PORT',5000)),debug=False)
