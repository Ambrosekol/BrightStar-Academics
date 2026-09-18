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


STATIC=os.path.join(BASE,'static')
UPLOADS=os.path.join(STATIC,'uploads')
IMAGE_EXTENSIONS={'png','jpg','jpeg','gif','webp'}


# ---------------- admin RBAC / audit ----------------
# Permission catalogue, role presets, admin_required, current_admin,
# audit_log and friends moved to core/security.py.
from core.security import (  # noqa: E402
    ADMIN_PERMISSION_DEFS, ADMIN_ROLE_PRESETS, ADMIN_ENDPOINT_PERMISSIONS,
    admin_required, current_admin, is_super_admin, admin_has_permission,
    admin_permission_codes, admin_scope_allows, audit_display_detail,
    audit_log, admin_scope_for_request, admin_access_error,
    _notify_super_admins,
)


# ---------------- promotion decisions ----------------

PROMOTION_ACTIONS = {
    "promote": "Promote",
    "repeat": "Repeat",
    "transfer": "Transfer / Reassign",
    "graduate": "Graduate",
    "withdraw": "Withdraw",
    "hold": "Hold",
}

PROMOTION_DECISIONS = set(PROMOTION_ACTIONS.keys())


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


def _save_image_upload(file_obj, subdir, prefix='image'):
    if not file_obj or not getattr(file_obj, 'filename', ''):
        return None
    original=secure_filename(file_obj.filename)
    ext=original.rsplit('.',1)[-1].lower() if '.' in original else ''
    if ext not in IMAGE_EXTENSIONS:
        raise ValueError('Please upload a PNG, JPG, JPEG, GIF or WEBP image.')
    # Defense in depth: enforce a conservative upload limit and validate the
    # actual image signature before persisting the file.
    max_bytes=int(os.environ.get('CRAINBOW_MAX_UPLOAD_BYTES', 5 * 1024 * 1024))
    stream=getattr(file_obj,'stream',None)
    if stream is None:
        raise ValueError('Invalid upload.')
    pos=stream.tell()
    stream.seek(0,2); size=stream.tell(); stream.seek(pos)
    if size > max_bytes:
        raise ValueError(f'Image uploads must be {max_bytes // (1024*1024)} MB or smaller.')
    header=stream.read(16); stream.seek(pos)
    # Validate the file signature, not merely the filename extension.
    # The previous hardening patch accidentally escaped the hexadecimal
    # signatures twice, which rejected genuine JPEG/GIF/WEBP files.
    signatures={
        'png': header.startswith(b'\x89PNG\r\n\x1a\n'),
        'jpg': header.startswith(b'\xff\xd8\xff'),
        'jpeg': header.startswith(b'\xff\xd8\xff'),
        'gif': header.startswith((b'GIF87a',b'GIF89a')),
        'webp': header.startswith(b'RIFF') and len(header)>=12 and header[8:12]==b'WEBP',
    }
    if not signatures.get(ext,False):
        raise ValueError('The uploaded file does not appear to be a valid image.')
    folder=os.path.join(UPLOADS,subdir)
    os.makedirs(folder,exist_ok=True)
    safe_prefix=secure_filename(str(prefix))[:80] or 'image'
    filename=f"{safe_prefix}_{secrets.token_hex(10)}.{ext}"
    path=os.path.join(folder,filename)
    file_obj.save(path)
    return f"uploads/{subdir}/{filename}"

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

def _sync_admin_roles(admin_id, role_ids, granted_by, now):
    ids=[]
    for rid in role_ids:
        try: rid=int(rid)
        except (TypeError,ValueError): continue
        if rid not in ids: ids.append(rid)
    db.session.execute(sa_delete(AdminRoleAssignment).where(AdminRoleAssignment.admin_id==admin_id))
    _ignore_insert(AdminRoleAssignment, [
        {'admin_id':admin_id,'admin_type_id':rid,'assigned_at':now,'assigned_by':granted_by}
        for rid in ids
    ])
    return ids

def _admin_contact_fields(form):
    return {
        'email': form.get('email','').strip().lower(),
        'phone': form.get('phone','').strip(),
        'whatsapp': form.get('whatsapp','').strip(),
    }

def _validate_admin_contact_fields(contact):
    errors=[]
    email=contact['email']
    if email and (len(email)>254 or '@' not in email or '.' not in email.rsplit('@',1)[-1]): errors.append('Enter a valid email address.')
    for label,key in (('Phone','phone'),('WhatsApp','whatsapp')):
        value=contact[key]
        if value and len(''.join(ch for ch in value if ch.isdigit())) < 7: errors.append(f'Enter a valid {label} number or leave it blank.')
    return errors


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


def _finance_can_view_all(admin=None):
    admin=admin or current_admin()
    return bool(admin and (is_super_admin(admin) or admin_has_permission(admin['id'],'finance.view_all')))

def _next_receipt_no():
    """Next sequential receipt number for the current year."""
    prefix=f'CRS-{datetime.now().year}-'
    last=one_scalar(select(FinancePayment.receipt_no)
                    .where(FinancePayment.receipt_no.like(prefix+'%'))
                    .order_by(FinancePayment.id.desc()).limit(1))
    n=1
    if last:
        try: n=int(str(last).rsplit('-',1)[1])+1
        except (IndexError,ValueError): n=1
    while one_scalar(select(FinancePayment.id)
                     .where(FinancePayment.receipt_no==f'{prefix}{n:05d}')):
        n+=1
    return f'{prefix}{n:05d}'

def _student_display(row): return ' '.join(x for x in [row['first_name'],row['middle_name'],row['last_name']] if x).strip()

def _format_money(value): return f'₦{float(value or 0):,.2f}'

def _ng_phone(value):
    raw=''.join(ch for ch in str(value or '') if ch.isdigit() or ch=='+')
    if raw.startswith('0') and len(raw)>=10: return '+234'+raw[1:]
    if raw.startswith('234'): return '+'+raw
    return raw

def _num_words_under_1000(n):
    ones=['zero','one','two','three','four','five','six','seven','eight','nine','ten','eleven','twelve','thirteen','fourteen','fifteen','sixteen','seventeen','eighteen','nineteen']
    tens=['','','twenty','thirty','forty','fifty','sixty','seventy','eighty','ninety']
    n=int(n or 0)
    if n<20: return ones[n]
    if n<100: return tens[n//10] + (f'-{ones[n%10]}' if n%10 else '')
    return ones[n//100]+' hundred'+(f' and {_num_words_under_1000(n%100)}' if n%100 else '')

def _amount_in_words(value):
    amount=round(float(value or 0),2); naira=int(amount); kobo=int(round((amount-naira)*100))
    parts=[]; remainder=naira
    for scale,name in [(10**9,'billion'),(10**6,'million'),(10**3,'thousand')]:
        if remainder>=scale:
            count=remainder//scale; remainder%=scale; parts.append(_num_words_under_1000(count)+' '+name)
    if remainder or not parts: parts.append(_num_words_under_1000(remainder))
    text=' '.join(parts)+' naira'
    if kobo: text += ' and '+_num_words_under_1000(kobo)+' kobo'
    return text+' only'

def _receipt_payload(payment_id):
    raw=one(select(FinancePayment,Student.admission_no,Student.first_name,
                   Student.middle_name,Student.last_name,Student.guardian_name,
                   Student.guardian_phone,Student.guardian_email,
                   StudentEnrolment.class_id,SchoolClass.name.label('class_name'),
                   AcademicSession.name.label('session_name'))
            .join(Student,Student.id==FinancePayment.student_id)
            .outerjoin(StudentEnrolment,and_(StudentEnrolment.student_id==Student.id,
                                             StudentEnrolment.session_id==FinancePayment.session_id,
                                             StudentEnrolment.active==1))
            .outerjoin(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
            .outerjoin(AcademicSession,AcademicSession.id==FinancePayment.session_id)
            .where(FinancePayment.id==payment_id))
    if not raw: return None
    row=_flatten(raw,'FinancePayment','admission_no','first_name','middle_name','last_name',
                 'guardian_name','guardian_phone','guardian_email','class_id',
                 'class_name','session_name')
    amount=float(row.get('amount') or 0)
    row['payer_name']=(row.get('payer_name') or row.get('guardian_name') or _student_display(row)).strip()
    row['amount_words']=_amount_in_words(amount)
    row['amount_naira']=int(amount)
    row['amount_kobo']=int(round((amount-row['amount_naira'])*100))
    return row

RECEIPT_SIGNATURE_SETTING_KEY='receipt_authorised_signature'

def _primary_school_id():
    return one_scalar(select(School.id).where(School.active==1).order_by(School.id))

def _receipt_signature_setting_row():
    school_id=_primary_school_id()
    if not school_id: return None
    return db.session.scalars(select(SchoolSetting).where(
        SchoolSetting.school_id==school_id,
        SchoolSetting.setting_key==RECEIPT_SIGNATURE_SETTING_KEY)).first()

def _receipt_signature_relpath():
    row=_receipt_signature_setting_row()
    return (row.setting_value or '').strip() if row and row.setting_value else ''

def _receipt_signature_abspath():
    """Filesystem path to the configured authorised-signature image, or None."""
    rel=_receipt_signature_relpath()
    if not rel: return None
    path=os.path.join(STATIC,rel)
    return path if os.path.exists(path) else None

def _set_receipt_signature(rel_path, admin_id):
    school_id=_primary_school_id()
    if not school_id: raise ValueError('No active school is configured.')
    row=_receipt_signature_setting_row()
    now=datetime.now(timezone.utc).isoformat()
    if row:
        row.setting_value=rel_path; row.updated_at=now; row.updated_by=admin_id
    else:
        db.session.add(SchoolSetting(school_id=school_id,setting_key=RECEIPT_SIGNATURE_SETTING_KEY,
                                      setting_value=rel_path,updated_at=now,updated_by=admin_id))
    db.session.commit()

def _save_signature_data_url(data_url):
    """Persist a canvas-drawn signature (a data:image/png;base64,... URL) as a PNG file."""
    if not data_url or not data_url.startswith('data:image/png;base64,'):
        raise ValueError('The drawn signature could not be read. Please try drawing it again.')
    try: raw=base64.b64decode(data_url.split(',',1)[1])
    except Exception: raise ValueError('The drawn signature could not be read. Please try drawing it again.')
    max_bytes=int(os.environ.get('CRAINBOW_MAX_UPLOAD_BYTES', 5 * 1024 * 1024))
    if len(raw) > max_bytes: raise ValueError('Signature image is too large.')
    if not raw.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('The drawn signature could not be read. Please try drawing it again.')
    folder=os.path.join(UPLOADS,'signatures'); os.makedirs(folder,exist_ok=True)
    filename=f"authorised_{secrets.token_hex(10)}.png"; path=os.path.join(folder,filename)
    with open(path,'wb') as fh: fh.write(raw)
    return f"uploads/signatures/{filename}"

def _receipt_pdf(payment_id):
    row=_receipt_payload(payment_id)
    if not row: abort(404)
    from reportlab.lib.pagesizes import A5, landscape
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.utils import ImageReader
    import io
    font_regular=os.path.join(STATIC,'fonts','DejaVuSans.ttf'); font_bold=os.path.join(STATIC,'fonts','DejaVuSans-Bold.ttf')
    if os.path.exists(font_regular):
        try: pdfmetrics.registerFont(TTFont('CrainbowReceipt',font_regular)); pdfmetrics.registerFont(TTFont('CrainbowReceiptBold',font_bold))
        except Exception: pass
    regular='CrainbowReceipt' if 'CrainbowReceipt' in pdfmetrics.getRegisteredFontNames() else 'Helvetica'; bold='CrainbowReceiptBold' if 'CrainbowReceiptBold' in pdfmetrics.getRegisteredFontNames() else 'Helvetica-Bold'
    W,H=landscape(A5); buf=io.BytesIO(); c=canvas.Canvas(buf,pagesize=(W,H))

    # Whole sheet: white, with a smooth blue wave and a gold trim line tracing
    # its crest along the bottom, matching the school's printed receipt pad.
    import math
    c.setFillColorRGB(1,1,1); c.rect(0,0,W,H,fill=1,stroke=0)
    band_h=20*mm; amp=4.5*mm; wavelength=90*mm
    def wave_y(x): return band_h+amp*math.sin(2*math.pi*x/wavelength+0.6)
    steps=90
    wave_pts=[(W*i/steps,wave_y(W*i/steps)) for i in range(steps+1)]
    c.saveState()
    clip=c.beginPath(); clip.moveTo(0,0)
    for x,y in wave_pts: clip.lineTo(x,y)
    clip.lineTo(W,0); clip.close()
    c.clipPath(clip,stroke=0,fill=0)
    c.setFillColorRGB(0.08,0.22,0.46); c.rect(0,0,W,band_h+amp,fill=1,stroke=0)
    for x,r,g,b in [(10,0.10,0.30,0.62),(55,0.07,0.24,0.52),(105,0.12,0.36,0.72),(155,0.08,0.27,0.58),(200,0.11,0.33,0.66)]:
        c.setFillColorRGB(r,g,b); c.circle(x*mm,2*mm,30*mm,fill=1,stroke=0)
    c.restoreState()
    c.setStrokeColorRGB(0.95,0.76,0.13); c.setLineWidth(1.6*mm); c.setLineJoin(1)
    trim=c.beginPath(); trim.moveTo(*wave_pts[0])
    for x,y in wave_pts[1:]: trim.lineTo(x,y)
    c.drawPath(trim,stroke=1,fill=0)

    logo=os.path.join(STATIC,'images','school_logo.png')
    if os.path.exists(logo):
        try: c.drawImage(ImageReader(logo),8*mm,H-42*mm,width=88*mm,height=36*mm,preserveAspectRatio=True,mask='auto')
        except Exception: pass
    else:
        c.setFillColorRGB(0.02,0.28,0.55); c.setFont(bold,16); c.drawString(9*mm,H-19*mm,'CREATIVE')
        c.setFont(bold,12); c.drawString(9*mm,H-27*mm,'RAINBOW MONTESSORI SCHOOLS'); c.setFont(bold,7); c.drawString(31*mm,H-34*mm,'NURSERY  |  PRIMARY  |  COLLEGE')

    c.setFillColorRGB(0.12,0.12,0.12); c.setFont(regular,7.2)
    c.drawString(140*mm,H-10*mm,'20/21 Charles Okeke Street,')
    c.drawString(140*mm,H-15*mm,'Alahun-Ozumba,')
    c.drawString(140*mm,H-20*mm,'Off Benster Close,')
    c.drawString(140*mm,H-25*mm,'Maza-Maza, Lagos.')
    c.setFont(bold,7.2); c.drawString(140*mm,H-30*mm,'Tel: 0803 123 4567, 0810 987 6543')

    c.setFillColorRGB(0.78,0.10,0.08); c.roundRect(72*mm,H-59*mm,66*mm,11*mm,2.5*mm,fill=1,stroke=0)
    c.setFillColorRGB(1,1,1); c.setFont(bold,11); c.drawCentredString(105*mm,H-55.5*mm,'OFFICIAL RECEIPT')

    c.setFillColorRGB(0.12,0.12,0.12); c.setFont(bold,8); c.drawString(140*mm,H-37*mm,'No:'); c.setFont(regular,8); c.drawString(150*mm,H-37*mm,str(row['receipt_no']))
    c.setFillColorRGB(0.83,0.91,0.95); c.rect(140*mm,H-49*mm,62*mm,9*mm,fill=1,stroke=0)
    try: date_text=datetime.fromisoformat(str(row.get('paid_at')).replace('Z','+00:00')).strftime('%d/%m/%Y')
    except Exception: date_text=str(row.get('paid_at') or '')[:10]
    c.setFillColorRGB(0.02,0.16,0.24); c.setFont(bold,8.5); c.drawString(143*mm,H-45*mm,'Date:'); c.setFont(regular,8.5); c.drawString(156*mm,H-45*mm,date_text)

    left=8*mm; right=W-8*mm; y_top=H-64*mm; row_h=8.6*mm; c.setStrokeColorRGB(0.70,0.80,0.84); c.setLineWidth(0.6)
    method=(row.get('method') or '').strip()
    labels=[
        ('Received from:',row.get('payer_name') or _student_display(row)),
        ('the sum of:',row.get('amount_words','')),
        ('Being payment for:',row.get('category') or 'School Fees'),
        ('Cash/Cheque No.:',row.get('reference') or ('Cash' if method.lower()=='cash' else '—')),
        ('Bank:',method or '—'),
    ]
    for i,(label,value) in enumerate(labels):
        yy=y_top-i*row_h; c.setFillColorRGB(0.80,0.91,0.96); c.rect(left,yy-row_h+1*mm,right-left,row_h-1.4*mm,fill=1,stroke=0); c.setStrokeColorRGB(0.72,0.82,0.87); c.rect(left,yy-row_h+1*mm,right-left,row_h-1.4*mm,fill=0,stroke=1)
        c.setFillColorRGB(0.02,0.15,0.20); c.setFont(bold,8); c.drawString(left+3*mm,yy-5.3*mm,label); c.setFont(regular,8.2); c.drawString(left+42*mm,yy-5.3*mm,str(value)[:105])

    amount=float(row.get('amount') or 0); naira=int(amount); kobo=int(round((amount-naira)*100))
    ay=y_top-len(labels)*row_h-3*mm
    c.setFillColorRGB(0.98,0.90,0.55); c.rect(left,ay-13*mm,100*mm,13*mm,fill=1,stroke=0)
    c.setFillColorRGB(0.95,0.76,0.13); c.rect(left,ay-13*mm,10*mm,13*mm,fill=1,stroke=0); c.rect(left+90*mm,ay-13*mm,10*mm,13*mm,fill=1,stroke=0)
    c.setFillColorRGB(0.05,0.05,0.05); c.setFont(bold,13); c.drawCentredString(left+5*mm,ay-9*mm,'N'); c.drawCentredString(left+95*mm,ay-9*mm,'K')
    c.setFont(bold,14); c.drawCentredString(left+50*mm,ay-9*mm,f'₦{naira:,}.{kobo:02d}')

    sig_path=_receipt_signature_abspath()
    sig_line_x1,sig_line_x2,sig_line_y=right-58*mm,right,ay-13*mm+4*mm
    if sig_path:
        try: c.drawImage(ImageReader(sig_path),sig_line_x1+6*mm,sig_line_y+1*mm,width=42*mm,height=11*mm,preserveAspectRatio=True,anchor='sw',mask='auto')
        except Exception: pass
    c.setStrokeColorRGB(0.30,0.30,0.30); c.setLineWidth(0.7); c.line(sig_line_x1,sig_line_y,sig_line_x2,sig_line_y)
    c.setFillColorRGB(0.12,0.12,0.12); c.setFont(regular,6.5); c.drawCentredString((sig_line_x1+sig_line_x2)/2,sig_line_y-3.2*mm,'Authorised Signature')

    c.setFillColorRGB(1,1,1); c.setFont(bold,7.4)
    c.drawString(left,band_h*0.32,'For: CREATIVE RAINBOW MONTESSORI SCHOOLS & KIDS PARK')

    c.showPage(); c.save(); buf.seek(0); return buf.getvalue(),row

def _smtp_use_ssl(port):
    """Should the connection start TLS immediately, rather than upgrade via STARTTLS?

    Port 465 is the long-standing convention for implicit TLS/SSL (SMTPS): the
    server expects a TLS handshake as the very first bytes on the connection.
    Port 587 (and 25) are plaintext-first, upgrading to TLS via STARTTLS after
    the initial handshake. Connecting to port 465 with plain SMTP()+starttls()
    sends a plaintext EHLO a TLS-only server never answers, which is exactly
    what previously made receipt/recovery emails hang until they timed out.
    CRAINBOW_SMTP_SSL overrides the auto-detection when a host doesn't follow
    the convention.
    """
    override=os.environ.get('CRAINBOW_SMTP_SSL','').strip()
    if override:
        return override != '0'
    return port == 465


def _smtp_send(host, port, user, password, msg):
    """Connect to the configured SMTP server and send a prepared message."""
    if _smtp_use_ssl(port):
        with smtplib.SMTP_SSL(host, port, timeout=20) as smtp:
            if user: smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            if os.environ.get('CRAINBOW_SMTP_STARTTLS','1') != '0':
                smtp.starttls()
            if user: smtp.login(user, password)
            smtp.send_message(msg)


def _send_email_receipt(payment_id):
    row=_receipt_payload(payment_id)
    if not row: return False,'Receipt not found.'
    host=os.environ.get('CRAINBOW_SMTP_HOST','').strip(); user=os.environ.get('CRAINBOW_SMTP_USER','').strip(); password=os.environ.get('CRAINBOW_SMTP_PASSWORD',''); sender=os.environ.get('CRAINBOW_SMTP_FROM',user).strip(); port=int(os.environ.get('CRAINBOW_SMTP_PORT','587') or 587)
    if not host or not sender: return False,'Email delivery is not configured. Set CRAINBOW_SMTP_HOST and CRAINBOW_SMTP_FROM.'
    recipient=(row['guardian_email'] or '').strip()
    if not recipient: return False,'This student has no parent/guardian email address.'
    pdf,_=_receipt_pdf(payment_id)
    from email.message import EmailMessage
    msg=EmailMessage(); msg['Subject']=f'Crainbow School Payment Receipt {row["receipt_no"]}'; msg['From']=sender; msg['To']=recipient; msg.set_content(f'Dear Parent/Guardian,\n\nPlease find attached the official payment receipt {row["receipt_no"]} for {_student_display(row)}.\n\nAmount paid: {_format_money(row["amount"])}\nPurpose: {row["category"]}\n\nCreative Rainbow Montessori School'); msg.add_attachment(pdf,maintype='application',subtype='pdf',filename=f'{row["receipt_no"]}.pdf')
    try:
        _smtp_send(host,port,user,password,msg)
        return True,recipient
    except Exception as exc: return False,f'Email delivery failed: {exc}'

def _send_whatsapp_receipt(payment_id):
    row=_receipt_payload(payment_id)
    if not row: return False,'Receipt not found.'
    token=os.environ.get('CRAINBOW_WHATSAPP_TOKEN','').strip(); phone_id=os.environ.get('CRAINBOW_WHATSAPP_PHONE_NUMBER_ID','').strip(); version=os.environ.get('CRAINBOW_WHATSAPP_GRAPH_VERSION','v23.0').strip(); recipient=_ng_phone(row['guardian_phone'])
    if not token or not phone_id: return False,'WhatsApp Business Cloud API is not configured. Set CRAINBOW_WHATSAPP_TOKEN and CRAINBOW_WHATSAPP_PHONE_NUMBER_ID.'
    if not recipient: return False,'This student has no valid parent/guardian WhatsApp number.'
    pdf,_=_receipt_pdf(payment_id); boundary='----CrainbowBoundary'+secrets.token_hex(8); url=f'https://graph.facebook.com/{version}/{phone_id}/media'
    body=(f'--{boundary}\r\nContent-Disposition: form-data; name="messaging_product"\r\n\r\nwhatsapp\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{row["receipt_no"]}.pdf"\r\nContent-Type: application/pdf\r\n\r\n').encode()+pdf+(f'\r\n--{boundary}--\r\n').encode()
    try:
        req=urllib.request.Request(url,data=body,method='POST',headers={'Authorization':f'Bearer {token}','Content-Type':f'multipart/form-data; boundary={boundary}'})
        with urllib.request.urlopen(req,timeout=30) as resp: media=json.loads(resp.read().decode())
        media_id=media.get('id')
        if not media_id: return False,'WhatsApp media upload returned no media ID.'
        payload=json.dumps({'messaging_product':'whatsapp','to':recipient,'type':'document','document':{'id':media_id,'caption':f'Official payment receipt {row["receipt_no"]} — {_student_display(row)}','filename':f'{row["receipt_no"]}.pdf'}}).encode(); req2=urllib.request.Request(f'https://graph.facebook.com/{version}/{phone_id}/messages',data=payload,method='POST',headers={'Authorization':f'Bearer {token}','Content-Type':'application/json'})
        with urllib.request.urlopen(req2,timeout=30) as resp: result=json.loads(resp.read().decode())
        return True,result.get('messages',[{}])[0].get('id',recipient)
    except urllib.error.HTTPError as exc: return False,f'WhatsApp API error {exc.code}: {exc.read().decode(errors="replace")[:500]}'
    except Exception as exc: return False,f'WhatsApp delivery failed: {exc}'

def _notify_guardian_email(guardian_email, subject, body):
    """Best-effort plain-text email to a parent/guardian. Never raises."""
    host=os.environ.get('CRAINBOW_SMTP_HOST','').strip(); user=os.environ.get('CRAINBOW_SMTP_USER','').strip(); password=os.environ.get('CRAINBOW_SMTP_PASSWORD',''); sender=os.environ.get('CRAINBOW_SMTP_FROM',user).strip(); port=int(os.environ.get('CRAINBOW_SMTP_PORT','587') or 587)
    if not host or not sender: return False,'Email delivery is not configured.'
    recipient=(guardian_email or '').strip()
    if not recipient: return False,'No guardian email address on file.'
    from email.message import EmailMessage
    msg=EmailMessage(); msg['Subject']=subject; msg['From']=sender; msg['To']=recipient; msg.set_content(body)
    try:
        _smtp_send(host,port,user,password,msg)
        return True,recipient
    except Exception as exc: return False,f'Email delivery failed: {exc}'

def _notify_guardian_whatsapp(guardian_phone, text):
    """Best-effort plain-text WhatsApp message to a parent/guardian. Never raises."""
    token=os.environ.get('CRAINBOW_WHATSAPP_TOKEN','').strip(); phone_id=os.environ.get('CRAINBOW_WHATSAPP_PHONE_NUMBER_ID','').strip(); version=os.environ.get('CRAINBOW_WHATSAPP_GRAPH_VERSION','v23.0').strip(); recipient=_ng_phone(guardian_phone)
    if not token or not phone_id: return False,'WhatsApp Business Cloud API is not configured.'
    if not recipient: return False,'No valid guardian WhatsApp number on file.'
    payload=json.dumps({'messaging_product':'whatsapp','to':recipient,'type':'text','text':{'body':text}}).encode()
    req=urllib.request.Request(f'https://graph.facebook.com/{version}/{phone_id}/messages',data=payload,method='POST',
        headers={'Authorization':f'Bearer {token}','Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=8) as resp: result=json.loads(resp.read().decode())
        return True,result.get('messages',[{}])[0].get('id',recipient)
    except urllib.error.HTTPError as exc: return False,f'WhatsApp API error {exc.code}: {exc.read().decode(errors="replace")[:500]}'
    except Exception as exc: return False,f'WhatsApp delivery failed: {exc}'

def _notify_guardians_of_school_work(student_ids, kind, title, due_date):
    """Email + WhatsApp every assigned student's guardian about new work.

    Best-effort and non-blocking to the caller's transaction: a missing
    channel, unset guardian contact, or a delivery failure for one student
    must never prevent the assignment/project itself from being saved for
    everyone else, so every failure is swallowed and logged rather than
    raised. Call this only after the assignment/project has been committed.
    """
    if not student_ids: return
    rows=all_rows(select(Student.id,Student.first_name,Student.last_name,
                         Student.guardian_email,Student.guardian_phone)
                  .where(Student.id.in_(student_ids)))
    due_text=due_date or 'no due date set'
    for r in rows:
        child=f"{r['first_name']} {r['last_name']}".strip()
        subject=f'New {kind} for {child}'
        body=(f'Dear Parent/Guardian,\n\n{child} has been given a new {kind}: "{title}".\n'
              f'Due: {due_text}.\n\nPlease check the student/parent portal for details.\n\n'
              'Creative Rainbow Montessori School')
        text=f'Crainbow School: {child} has a new {kind} - "{title}". Due: {due_text}.'
        try: _notify_guardian_email(r['guardian_email'],subject,body)
        except Exception: app.logger.exception('Guardian email notification failed for student %s',r['id'])
        try: _notify_guardian_whatsapp(r['guardian_phone'],text)
        except Exception: app.logger.exception('Guardian WhatsApp notification failed for student %s',r['id'])

def _parent_ids_for_student(student_id):
    """Every parent account actively linked to a student, for in-app alerts."""
    return [pid for (pid,) in tuples(
        select(ParentStudentLink.parent_id)
        .where(ParentStudentLink.student_id==student_id,ParentStudentLink.active==1))]

def _notify_parents_fee_assessed(student_id, fee_names, total_amount, term, session_name, admin_id):
    """Alert a student's parents that a new fee obligation has been charged.

    Best-effort on every channel — an in-app notification per linked parent
    account, plus email/WhatsApp to the guardian contact on the student
    record. A missing channel or delivery failure never blocks the
    assessment that was already committed. Call only after that commit.
    """
    student=one(select(Student.first_name,Student.last_name,Student.guardian_email,
                       Student.guardian_phone).where(Student.id==student_id))
    if not student: return
    child=f"{student['first_name']} {student['last_name']}".strip()
    items_text=', '.join(fee_names)
    now=datetime.now(timezone.utc).isoformat()
    title=f'New fee charged for {child}'
    message=f'{items_text} — ₦{total_amount:,.2f} for {term} ({session_name}).'
    try:
        for pid in _parent_ids_for_student(student_id):
            db.session.add(SchoolNotification(
                recipient_type='parent',recipient_id=pid,student_id=student_id,
                category='finance',title=title,message=message,
                action_url=url_for('parent_child_finance',student_id=student_id),
                created_at=now,created_by=admin_id))
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception('In-app fee-assessed notification failed for student %s',student_id)
    subject=f'New fee charged — {child}'
    body=(f'Dear Parent/Guardian,\n\n{child} has been charged a new fee: {items_text}.\n'
          f'Amount: ₦{total_amount:,.2f} — {term} ({session_name}).\n\n'
          'Please check the parent portal for your full fee account and outstanding balance.\n\n'
          'Creative Rainbow Montessori School')
    text=f'Crainbow School: {child} has been charged {items_text} — ₦{total_amount:,.2f} for {term}. Check the parent portal for details.'
    try: _notify_guardian_email(student['guardian_email'],subject,body)
    except Exception: app.logger.exception('Guardian email (fee assessed) failed for student %s',student_id)
    try: _notify_guardian_whatsapp(student['guardian_phone'],text)
    except Exception: app.logger.exception('Guardian WhatsApp (fee assessed) failed for student %s',student_id)

def _notify_parents_payment_recorded(student_id, receipt_no, amount, category, admin_id):
    """Alert a student's parents that a payment has been recorded for them.

    Same best-effort contract as _notify_parents_fee_assessed. Call only
    after the payment has been committed.
    """
    student=one(select(Student.first_name,Student.last_name,Student.guardian_email,
                       Student.guardian_phone).where(Student.id==student_id))
    if not student: return
    child=f"{student['first_name']} {student['last_name']}".strip()
    now=datetime.now(timezone.utc).isoformat()
    title=f'Payment received for {child}'
    message=f'₦{amount:,.2f} received for {category} — Receipt {receipt_no}.'
    try:
        for pid in _parent_ids_for_student(student_id):
            db.session.add(SchoolNotification(
                recipient_type='parent',recipient_id=pid,student_id=student_id,
                category='finance',title=title,message=message,
                action_url=url_for('parent_child_finance',student_id=student_id),
                created_at=now,created_by=admin_id))
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception('In-app payment notification failed for student %s',student_id)
    subject=f'Payment received — {child}'
    body=(f'Dear Parent/Guardian,\n\nWe have received a payment of ₦{amount:,.2f} for {category} '
          f'on behalf of {child}. Receipt number: {receipt_no}.\n\n'
          'Please check the parent portal for your full fee account and outstanding balance.\n\n'
          'Thank you,\nCreative Rainbow Montessori School')
    text=f'Crainbow School: payment of ₦{amount:,.2f} received for {child} ({category}). Receipt {receipt_no}.'
    try: _notify_guardian_email(student['guardian_email'],subject,body)
    except Exception: app.logger.exception('Guardian email (payment recorded) failed for student %s',student_id)
    try: _notify_guardian_whatsapp(student['guardian_phone'],text)
    except Exception: app.logger.exception('Guardian WhatsApp (payment recorded) failed for student %s',student_id)

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

def csrf_protect(fn):
    """Require a valid session-bound CSRF token only for state-changing requests."""
    @wraps(fn)
    def wrapper(*args,**kwargs):
        # GET/HEAD/OPTIONS are safe navigation requests and must be allowed to
        # render protected forms.  The previous Phase 6H implementation
        # validated the token on every request, which meant clicking a normal
        # GET link such as "Edit bank" or "Register candidate" produced a
        # 403 before the form could even be displayed.
        if request.method in ('POST','PUT','PATCH','DELETE'):
            token=request.form.get('_csrf_token','') or request.headers.get('X-CSRF-Token','')
            expected=session.get('_csrf_token')
            if not expected or not token or not secrets.compare_digest(token,expected):
                abort(403, description='Invalid or missing CSRF token.')
        return fn(*args,**kwargs)
    return wrapper

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

def _public_settings():
    # Public pages must still render on a partially-upgraded database where the
    # settings table does not exist yet, hence the broad guard.
    try:
        return {key: value for key, value in tuples(
            select(SchoolPublicSetting.setting_key, SchoolPublicSetting.setting_value))}
    except Exception:
        db.session.rollback()
        return {}

def _public_page(slug):
    return db.session.scalars(select(SchoolPublicPage).where(
        SchoolPublicPage.slug==slug, SchoolPublicPage.published==1)).first()

@app.route('/school')
def public_school_home():
    # / is the public school's front door; /school is a stable alias for the same environment.
    return redirect(url_for('index'))

@app.route('/school/about')
def public_school_about():
    return render_template('public_page.html',settings=_public_settings(),page=_public_page('about'))

@app.route('/school/academics')
def public_school_academics():
    return render_template(
        'public_academics.html',
        settings=_public_settings(),
        public_settings=_public_settings()
    )

@app.route('/school/school-life')
def public_school_life():
    return render_template(
        'public_school_life.html',
        settings=_public_settings(),
        public_settings=_public_settings()
    )

@app.route('/school/admissions')
def public_school_admissions():
    return render_template(
        'public_admissions.html',
        settings=_public_settings(),
        public_settings=_public_settings()
    )

@app.route('/school/news')
def public_school_news():
    news=db.session.scalars(select(SchoolPublicNews).where(SchoolPublicNews.published==1)
        .order_by(func.coalesce(SchoolPublicNews.published_at,SchoolPublicNews.created_at).desc(),
                  SchoolPublicNews.id.desc())).all()
    return render_template('public_news.html',settings=_public_settings(),news=news)

@app.route('/school/news/<slug>')
def public_school_news_detail(slug):
    news=db.session.scalars(select(SchoolPublicNews).where(
        SchoolPublicNews.slug==slug, SchoolPublicNews.published==1)).first()
    if not news: abort(404)
    return render_template('public_news_detail.html',settings=_public_settings(),news=news)

@app.route('/school/contact',methods=['GET','POST'])
def public_school_contact():
    errors=[]
    if request.method=='POST':
        name=request.form.get('name','').strip(); email=request.form.get('email','').strip().lower(); phone=request.form.get('phone','').strip(); subject=request.form.get('subject','').strip(); message=request.form.get('message','').strip()
        if not name: errors.append('Your name is required.')
        if email and ('@' not in email or '.' not in email.rsplit('@',1)[-1]): errors.append('Enter a valid email address.')
        if not message: errors.append('Please enter your message.')
        if len(message)>5000: errors.append('Please keep your message under 5,000 characters.')
        if not errors:
            now=datetime.now(timezone.utc).isoformat()
            enquiry=SchoolPublicEnquiry(name=name,email=email or None,phone=phone or None,
                                        subject=subject or None,message=message,created_at=now)
            db.session.add(enquiry); db.session.flush()
            admin_ids=[aid for (aid,) in tuples(
                select(Admin.id).join(AdminType,AdminType.id==Admin.admin_type_id)
                .where(Admin.active==1,AdminType.active==1))]
            for aid in admin_ids:
                if admin_has_permission(aid,'website.view'):
                    db.session.add(AdminNotification(
                        admin_id=aid,title='New website enquiry',
                        message=f'{name} sent a message{(" about " + subject) if subject else ""}.',
                        severity='info',
                        action_url=url_for('admin_school_enquiry_detail',eid=enquiry.id),
                        created_at=now))
            db.session.commit(); flash('Thank you. Your message has been sent to the school.','success'); return redirect(url_for('public_school_contact'))
    return render_template('public_contact.html',settings=_public_settings(),errors=errors,form=request.form)

def _account_recovery_target(raw):
    value=(raw or '').strip().lower()
    if not value: return None
    admin=one(select(Admin.id,Admin.email,Admin.display_name).where(
        Admin.active==1,
        or_(func.lower(Admin.username)==value,
            func.lower(func.coalesce(Admin.email,''))==value)))
    if admin: return ('admin',admin['id'],admin['email'],admin['display_name'])
    parent=one(select(ParentAccount.id,ParentAccount.email,ParentAccount.display_name).where(
        ParentAccount.active==1,
        or_(func.lower(ParentAccount.username)==value,
            func.lower(func.coalesce(ParentAccount.email,''))==value)))
    if parent: return ('parent',parent['id'],parent['email'],parent['display_name'])
    # Student recovery is keyed to the student's own login ID/admission username.
    # Guardian email is deliberately not used here because one guardian can have multiple children.
    student=one(select(Student.id,Student.guardian_email,Student.first_name,Student.last_name).where(
        Student.active==1, func.lower(func.coalesce(Student.login_username,''))==value))
    if student: return ('student',student['id'],student['guardian_email'],f"{student['first_name']} {student['last_name']}")
    return None

def _send_recovery_email(recipient,name,reset_url):
    host=os.environ.get('CRAINBOW_SMTP_HOST','').strip(); user=os.environ.get('CRAINBOW_SMTP_USER','').strip(); password=os.environ.get('CRAINBOW_SMTP_PASSWORD',''); sender=os.environ.get('CRAINBOW_SMTP_FROM',user).strip(); port=int(os.environ.get('CRAINBOW_SMTP_PORT','587') or 587)
    if not host or not sender: return False,'Email recovery is not configured by the school yet.'
    from email.message import EmailMessage
    msg=EmailMessage(); msg['Subject']='Creative Rainbow Schools — Password reset'; msg['From']=sender; msg['To']=recipient; msg.set_content(f'Dear {name},\n\nA password reset was requested for your Creative Rainbow Schools account. Use this link within 30 minutes:\n\n{reset_url}\n\nIf you did not request this, you can ignore this message.\n\nCreative Rainbow Schools')
    try:
        _smtp_send(host,port,user,password,msg)
        return True,'sent'
    except Exception as exc: return False,f'Password recovery email could not be sent: {exc}'

@app.route('/forgot-password',methods=['GET','POST'])
def forgot_password():
    if request.method=='POST':
        if not _rate_limit(f'forgot-password:{request.remote_addr or "unknown"}', limit=5, window=900):
            flash('Too many password-recovery requests. Please wait a few minutes and try again.','error')
            return redirect(url_for('forgot_password'))
        raw=request.form.get('identifier','').strip(); target=_account_recovery_target(raw)
        generic='If the account exists and has a registered recovery email, instructions will be sent. If no message arrives, contact the school administrator.'
        if target:
            account_type,account_id,email,name=target
            if email:
                token=secrets.token_urlsafe(32); token_hash=hashlib.sha256(token.encode()).hexdigest(); now=datetime.now(timezone.utc); expires=(now+timedelta(minutes=30)).isoformat()
                db.session.execute(sa_update(PasswordResetToken)
                    .where(PasswordResetToken.account_type==account_type,
                           PasswordResetToken.account_id==account_id,
                           PasswordResetToken.used_at.is_(None))
                    .values(used_at=now.isoformat()))
                db.session.add(PasswordResetToken(
                    account_type=account_type,account_id=account_id,token_hash=token_hash,
                    expires_at=expires,created_at=now.isoformat(),requested_ip=request.remote_addr))
                db.session.commit()
                ok,_=_send_recovery_email(email,name,url_for('password_reset',token=token,_external=True))
                # Do not reveal whether an account exists or whether its email is configured.
                # The same response is used for every identifier.
                _ = ok
        flash(generic,'success'); return redirect(url_for('forgot_password'))
    return render_template('forgot_password.html')

@app.route('/reset-password/<token>',methods=['GET','POST'])
def password_reset(token):
    token_hash=hashlib.sha256((token or '').encode()).hexdigest()
    row=db.session.scalars(select(PasswordResetToken).where(
        PasswordResetToken.token_hash==token_hash,
        PasswordResetToken.used_at.is_(None),
        PasswordResetToken.expires_at>datetime.now(timezone.utc).isoformat())).first()
    if not row: return render_template('password_reset.html',errors=['This password reset link is invalid or has expired.'],valid=False)
    errors=[]
    if request.method=='POST':
        new=request.form.get('new_password',''); confirm=request.form.get('confirm_password','')
        if len(new)<8: errors.append('Your new password must be at least 8 characters long.')
        if new!=confirm: errors.append('The new password and confirmation do not match.')
        if not errors:
            now=datetime.now(timezone.utc).isoformat(); pw=generate_password_hash(new)
            if row['account_type']=='admin':
                db.session.execute(sa_update(Admin).where(Admin.id==row['account_id'])
                                   .values(password_hash=pw,password_must_change=0))
            elif row['account_type']=='parent':
                db.session.execute(sa_update(ParentAccount).where(ParentAccount.id==row['account_id'])
                                   .values(password_hash=pw,password_must_change=0))
            elif row['account_type']=='student':
                db.session.execute(sa_update(Student).where(Student.id==row['account_id'])
                                   .values(login_password_hash=pw,password_must_change=0))
            else: abort(400)
            row.used_at=now
            db.session.commit(); flash('Your password has been reset successfully. You can now sign in.','success'); return redirect(url_for('login'))
    return render_template('password_reset.html',errors=errors,valid=True)

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

_RATE_BUCKETS={}
_RATE_LIMIT_LOCK=None

def _rate_limit(key, limit=10, window=300):
    """Small single-process guard for development/single-worker deployments.
    Production must place rate limiting at the reverse proxy/shared store layer.
    """
    now=time.monotonic()
    bucket=_RATE_BUCKETS.get(key,[])
    bucket=[t for t in bucket if now-t < window]
    if len(bucket) >= limit:
        _RATE_BUCKETS[key]=bucket
        return False
    bucket.append(now); _RATE_BUCKETS[key]=bucket
    return True


PRESENCE_TIMEOUT_SECONDS=90

def _presence_identity():
    if session.get('admin_id'):
        return 'admin', int(session['admin_id'])
    if session.get('student_id'):
        return 'student', int(session['student_id'])
    if session.get('parent_id'):
        return 'parent', int(session['parent_id'])
    if session.get('candidate_id'):
        return 'candidate', int(session['candidate_id'])
    return None,None

def _presence_token():
    token=session.get('_presence_token')
    if not token:
        token=secrets.token_urlsafe(32)
        session['_presence_token']=token
    return token

def touch_presence():
    account_type,account_id=_presence_identity()
    if not account_type or not account_id:
        return
    now=datetime.now(timezone.utc).isoformat()
    token_hash=hashlib.sha256(_presence_token().encode()).hexdigest()
    stmt=sqlite_insert(PresenceSession).values(
        account_type=account_type,account_id=account_id,session_key_hash=token_hash,
        first_seen=now,last_seen=now,active=1,
        user_agent=request.headers.get('User-Agent','')[:500])
    db.session.execute(stmt.on_conflict_do_update(
        index_elements=['session_key_hash'],
        set_={'last_seen':stmt.excluded.last_seen,'active':1,
              'user_agent':stmt.excluded.user_agent}))
    db.session.execute(sa_update(PresenceSession)
        .where(PresenceSession.last_seen <
               (datetime.now(timezone.utc)-timedelta(seconds=PRESENCE_TIMEOUT_SECONDS)).isoformat())
        .values(active=0))
    db.session.commit()

def end_presence():
    token=session.get('_presence_token')
    if not token:
        return
    token_hash=hashlib.sha256(token.encode()).hexdigest()
    db.session.execute(sa_update(PresenceSession)
        .where(PresenceSession.session_key_hash==token_hash)
        .values(active=0,last_seen=datetime.now(timezone.utc).isoformat()))
    db.session.commit()
    session.pop('_presence_token',None)

def online_presence():
    """Live counts and the roster of who is currently signed in."""
    cutoff=(datetime.now(timezone.utc)-timedelta(seconds=PRESENCE_TIMEOUT_SECONDS)).isoformat()
    db.session.execute(sa_update(PresenceSession)
        .where(PresenceSession.last_seen < cutoff).values(active=0))

    def live(kind):
        return one_scalar(
            select(func.count(func.distinct(PresenceSession.account_id)))
            .where(PresenceSession.account_type==kind,PresenceSession.active==1,
                   PresenceSession.last_seen>=cutoff), 0)

    counts={'admins':live('admin'),'students':live('student'),'parents':live('parent')}
    display=sa.case(
        (PresenceSession.account_type=='admin', Admin.display_name),
        (PresenceSession.account_type=='student',
         func.trim(Student.first_name+' '+func.coalesce(Student.middle_name,'')+' '+Student.last_name)),
        (PresenceSession.account_type=='parent', ParentAccount.display_name))
    identifier=sa.case(
        (PresenceSession.account_type=='admin', Admin.username),
        (PresenceSession.account_type=='student', Student.admission_no),
        (PresenceSession.account_type=='parent', ParentAccount.username))
    stmt=(select(PresenceSession.account_type,PresenceSession.account_id,
                 func.max(PresenceSession.last_seen).label('last_seen'),
                 display.label('display_name'),identifier.label('identifier'),
                 SchoolClass.name.label('class_name'))
          .select_from(PresenceSession)
          .outerjoin(Admin,and_(Admin.id==PresenceSession.account_id,
                                PresenceSession.account_type=='admin'))
          .outerjoin(Student,and_(Student.id==PresenceSession.account_id,
                                  PresenceSession.account_type=='student'))
          .outerjoin(ParentAccount,and_(ParentAccount.id==PresenceSession.account_id,
                                        PresenceSession.account_type=='parent'))
          .outerjoin(StudentEnrolment,and_(StudentEnrolment.student_id==Student.id,
                                           StudentEnrolment.active==1,
                                           PresenceSession.account_type=='student'))
          .outerjoin(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
          .where(PresenceSession.active==1,PresenceSession.last_seen>=cutoff)
          .group_by(PresenceSession.account_type,PresenceSession.account_id)
          .order_by(PresenceSession.account_type,func.max(PresenceSession.last_seen).desc()))
    rows=all_rows(stmt)
    db.session.commit()
    return counts,rows

def _clear_identity_sessions():
    for key in ('admin_id','admin_logged_in','admin_workspace','student_id','parent_id','candidate_id','attempt_id','_presence_token'):
        session.pop(key,None)

def _authenticate_unified(identifier, password):
    """Identify the account type from the supplied login identifier.

    Admin, candidate, parent and student credentials all arrive through one
    form, so each store is tried in turn. The Admin model exposes
    admin_type_name/admin_type_system as properties, which is what the join in
    the pre-ORM query was for.
    """
    raw=(identifier or '').strip()
    if not raw or not password: return None, None
    admin=db.session.scalars(
        select(Admin).join(AdminType,AdminType.id==Admin.admin_type_id)
        .where(func.lower(Admin.username)==raw.lower(),
               Admin.active==1,AdminType.active==1)).first()
    if admin and check_password_hash(admin.password_hash,password):
        return 'admin',admin
    candidate=db.session.scalars(select(Candidate).where(
        Candidate.candidate_code==raw.upper(),Candidate.active==1)).first()
    if candidate and check_password_hash(candidate.password_hash,password):
        return 'candidate',candidate
    parent=db.session.scalars(select(ParentAccount).where(
        or_(func.lower(ParentAccount.username)==raw.lower(),
            func.lower(func.coalesce(ParentAccount.email,''))==raw.lower()),
        ParentAccount.active==1)).first()
    if parent and check_password_hash(parent.password_hash,password):
        return 'parent',parent
    student=db.session.scalars(select(Student).where(
        func.lower(Student.login_username)==raw.lower(),
        Student.active==1,Student.account_active==1)).first()
    if student and student.login_password_hash and check_password_hash(student.login_password_hash,password):
        return 'student',student
    return None, None

@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        identifier=request.form.get('username','').strip()
        password=request.form.get('password','')
        rate_key=f"login:{request.remote_addr or 'unknown'}:{identifier.lower()[:120]}"
        if not _rate_limit(rate_key, limit=8, window=300):
            return render_template('login.html',error='Too many sign-in attempts. Please wait a few minutes and try again.'), 429
        if not identifier or not password:
            return render_template('login.html',error='Enter your username, Student ID or Candidate ID and password.')
        kind, account=_authenticate_unified(identifier,password)
        if not account:
            return render_template('login.html',error='We could not verify those login details. Please check your ID/username and password.')
        _clear_identity_sessions()
        if kind=='admin':
            session['admin_id']=account['id']; session['admin_logged_in']=True
            db.session.execute(sa_update(Admin).where(Admin.id==account['id'])
                .values(last_login_at=datetime.now(timezone.utc).isoformat()))
            db.session.commit()
            audit_log('admin_login','authentication','admin',account['id'],{'username':account['username']},True,account)
            if account['password_must_change']:
                return redirect(url_for('admin_password_change'))
            return redirect(url_for('admin_workspace_home'))
        if kind=='candidate':
            session['candidate_id']=account['id']
            audit_log('candidate_login','authentication','candidate',account['id'])
            return redirect(url_for('candidate_dashboard'))
        if kind=='parent':
            session['parent_id']=account['id']
            db.session.execute(sa_update(ParentAccount).where(ParentAccount.id==account['id'])
                .values(last_login_at=datetime.now(timezone.utc).isoformat()))
            db.session.commit()
            audit_log('parent_login','authentication','parent',account['id'])
            if account['password_must_change']:
                return redirect(url_for('parent_password_change'))
            return redirect(url_for('parent_dashboard'))
        session['student_id']=account['id']
        db.session.execute(sa_update(Student).where(Student.id==account['id'])
            .values(last_login_at=datetime.now(timezone.utc).isoformat()))
        db.session.commit()
        audit_log('student_login','authentication','student',account['id'])
        return redirect(url_for('student_dashboard'))
    # Login is intentionally a child environment of the public school site.
    return render_template('login.html', public_settings=_public_settings())

@app.route('/admin/login',methods=['GET','POST'])
def admin_login():
    # Compatibility URL: all account types now use the single login surface.
    if request.method=='GET':
        return redirect(url_for('login',next=request.args.get('next','')))
    return login()

@app.route('/logout',methods=['GET','POST'])
def logout():
    if request.method=='POST' and not csrf_check_request():
        abort(403, description='Invalid or missing CSRF token.')
    kind,account_id=_presence_identity()
    if kind:
        if kind=='admin':
            me=current_admin(); audit_log('admin_logout','authentication','admin',account_id,{'username':me['username'] if me else None},True,me)
        elif kind=='student':
            audit_log('student_logout','authentication','student',account_id)
        elif kind=='parent':
            audit_log('parent_logout','authentication','parent',account_id)
        elif kind=='candidate':
            audit_log('candidate_logout','authentication','candidate',account_id)
    end_presence()
    _clear_identity_sessions(); return redirect(url_for('index'))

@app.route('/admin/logout')
def admin_logout():
    return redirect(url_for('logout'))

@app.route('/candidate/logout')
def candidate_logout():
    return redirect(url_for('logout'))

def _parent_children(pid, with_session=False):
    """Active children linked to a parent, with their current class."""
    cols=[Student,ParentStudentLink.relationship,SchoolClass.name.label('class_name')]
    if with_session:
        cols.append(AcademicSession.name.label('session_name'))
    stmt=(select(*cols)
          .select_from(ParentStudentLink)
          .join(Student,and_(Student.id==ParentStudentLink.student_id,Student.active==1))
          .outerjoin(StudentEnrolment,and_(StudentEnrolment.student_id==Student.id,
                                           StudentEnrolment.active==1))
          .outerjoin(SchoolClass,SchoolClass.id==StudentEnrolment.class_id))
    if with_session:
        stmt=stmt.outerjoin(AcademicSession,AcademicSession.id==StudentEnrolment.session_id)
    stmt=stmt.where(ParentStudentLink.parent_id==pid,ParentStudentLink.active==1)
    stmt=stmt.order_by(Student.first_name,Student.last_name)
    extra=('relationship','class_name')+(('session_name',) if with_session else ())
    return [_flatten(r,'Student',*extra) for r in all_rows(stmt)]


def _released_results(student_id, limit=None):
    stmt=(select(SchoolStudentResult.score,SchoolStudentResult.max_score,
                 SchoolStudentResult.term,SchoolStudentResult.status,
                 SchoolSubject.name.label('subject_name'),
                 func.coalesce(SchoolStudentResult.component_name,SchoolAssessment.title,
                               'Academic Result').label('component_name'))
          .join(SchoolSubject,SchoolSubject.id==SchoolStudentResult.subject_id)
          .outerjoin(SchoolAssessment,SchoolAssessment.id==SchoolStudentResult.assessment_id)
          .where(SchoolStudentResult.student_id==student_id,
                 SchoolStudentResult.status=='released')
          .order_by(SchoolStudentResult.id.desc()))
    if limit:
        stmt=stmt.limit(limit)
    return all_rows(stmt)


def _student_assignments(student_id, limit=None, with_id=True):
    cols=([SchoolAssignment.id] if with_id else [])+[
        SchoolAssignment.title,SchoolAssignment.date_given,SchoolAssignment.due_date,
        SchoolAssignment.assignment_type,AssignmentStudent.status,
        AssignmentStudent.score,AssignmentStudent.max_score,AssignmentStudent.remark,
        SchoolSubject.name.label('subject_name')]
    stmt=(select(*cols).select_from(AssignmentStudent)
          .join(SchoolAssignment,SchoolAssignment.id==AssignmentStudent.assignment_id)
          .join(SchoolSubject,SchoolSubject.id==SchoolAssignment.subject_id)
          .where(AssignmentStudent.student_id==student_id,SchoolAssignment.active==1)
          .order_by(SchoolAssignment.date_given.desc(),SchoolAssignment.id.desc()))
    if limit:
        stmt=stmt.limit(limit)
    return all_rows(stmt)


def _student_projects(student_id, limit=None, with_id=True):
    cols=([SchoolProject.id] if with_id else [])+[
        SchoolProject.title,SchoolProject.date_given,SchoolProject.due_date,
        SchoolProject.max_score,ProjectStudent.status,ProjectStudent.score,
        ProjectStudent.remark,SchoolSubject.name.label('subject_name')]
    stmt=(select(*cols).select_from(ProjectStudent)
          .join(SchoolProject,SchoolProject.id==ProjectStudent.project_id)
          .join(SchoolSubject,SchoolSubject.id==SchoolProject.subject_id)
          .where(ProjectStudent.student_id==student_id,SchoolProject.active==1)
          .order_by(SchoolProject.date_given.desc(),SchoolProject.id.desc()))
    if limit:
        stmt=stmt.limit(limit)
    return all_rows(stmt)


def _feedback_replies(feedback_ids):
    """Replies for a set of feedback threads, grouped by thread.

    One query for every thread rather than one per thread.
    """
    replies={}
    if not feedback_ids:
        return replies
    rows=all_rows(select(ParentFeedbackReply,Admin.display_name.label('admin_name'))
                  .outerjoin(Admin,Admin.id==ParentFeedbackReply.admin_id)
                  .where(ParentFeedbackReply.feedback_id.in_(list(feedback_ids)))
                  .order_by(ParentFeedbackReply.created_at))
    for row in rows:
        flat=_flatten(row,'ParentFeedbackReply','admin_name')
        flat['is_parent']=flat['admin_id'] is None
        replies.setdefault(flat['feedback_id'],[]).append(flat)
    return replies


def _assignment_metrics(assignments):
    """Average score, completion rate and direction of travel."""
    graded=[float(x['score'])/float(x['max_score'])*100 for x in assignments
            if x['score'] is not None and x['max_score'] and float(x['max_score'])>0]
    avg=round(sum(graded)/len(graded),1) if graded else None
    completed=sum(1 for x in assignments if x['status']=='done')
    completion=round(completed/len(assignments)*100,1) if assignments else None
    trend='Not enough data'
    if len(graded)>=3:
        recent=sum(graded[:3])/3; older=sum(graded[-3:])/3
        trend='Improving' if recent>older+3 else ('Needs attention' if recent<older-3 else 'Stable')
    return avg,completion,trend


def parent_required(fn):
    @wraps(fn)
    def wrapper(*args,**kwargs):
        pid=session.get('parent_id')
        if not pid:
            return redirect(url_for('login',next=request.path))
        row=db.session.scalars(select(ParentAccount).where(
            ParentAccount.id==pid,ParentAccount.active==1)).first()
        if not row:
            _clear_identity_sessions(); return redirect(url_for('login'))
        if row['password_must_change'] and request.endpoint != 'parent_password_change':
            return redirect(url_for('parent_password_change'))
        return fn(*args,**kwargs)
    return wrapper

def _parent_owns_student(pid, student_id):
    return bool(one(select(ParentStudentLink.id).where(
        ParentStudentLink.parent_id==pid,ParentStudentLink.student_id==student_id,
        ParentStudentLink.active==1)))

@app.route('/parent/dashboard')
@parent_required
def parent_dashboard():
    pid=session['parent_id']
    parent=obj(ParentAccount,pid)
    _release_due_school_results()
    child_data=[]
    total_outstanding_all=0.0
    for child in _parent_children(pid,with_session=True):
        assignments=_student_assignments(child['id'],limit=50)
        avg,completion,trend=_assignment_metrics(assignments)
        # Across every session, not just the current one — a balance carried
        # over from a prior session must never silently disappear here.
        fee_summary=_finance_student_lifetime_totals(child['id'])
        total_outstanding_all+=fee_summary['outstanding']
        child_data.append({'child':child,
                           'results':_released_results(child['id'],limit=50),
                           'assignments':assignments,
                           'projects':_student_projects(child['id'],limit=50),
                           'avg':avg,'completion':completion,'trend':trend,
                           'fee_summary':fee_summary})
    notifications=db.session.scalars(select(SchoolNotification).where(
        SchoolNotification.recipient_type=='parent',SchoolNotification.recipient_id==pid)
        .order_by(SchoolNotification.id.desc()).limit(15)).all()
    feedback=[_flatten(r,'ParentFeedback','first_name','last_name') for r in all_rows(
        select(ParentFeedback,Student.first_name,Student.last_name)
        .outerjoin(Student,Student.id==ParentFeedback.student_id)
        .where(ParentFeedback.parent_id==pid)
        .order_by(ParentFeedback.id.desc()).limit(10))]
    return render_template('parent_dashboard.html',parent=parent,children=child_data,
        notifications=notifications,feedback=feedback,
        total_outstanding_all=total_outstanding_all)

@app.route('/parent/password',methods=['GET','POST'])
@parent_required
@csrf_protect
def parent_password_change():
    pid=session['parent_id']; parent=obj(ParentAccount,pid)
    errors=[]
    if request.method=='POST':
        current=request.form.get('current_password',''); new=request.form.get('new_password',''); confirm=request.form.get('confirm_password','')
        if not check_password_hash(parent['password_hash'] or '',current): errors.append('Your current password is incorrect.')
        if len(new)<8: errors.append('Your new password must be at least 8 characters long.')
        if new!=confirm: errors.append('The new password and confirmation do not match.')
        if not errors:
            parent.password_hash=generate_password_hash(new); parent.password_must_change=0
            db.session.commit()
            audit_log('parent_password_changed','authentication','parent',pid)
            flash('Your password has been changed successfully.','success')
            return redirect(url_for('parent_dashboard'))
    return render_template('parent_password.html',parent=parent,errors=errors)

@app.route('/parent/children/<int:student_id>')
@parent_required
def parent_child_detail(student_id):
    pid=session['parent_id']
    # The link is what authorises access: a parent can only open a child of
    # their own. Prefer the enrolment in the current session.
    row=one(select(Student,ParentStudentLink.relationship,
                   SchoolClass.name.label('class_name'),
                   AcademicSession.name.label('session_name'))
            .select_from(ParentStudentLink)
            .join(Student,and_(Student.id==ParentStudentLink.student_id,Student.active==1))
            .outerjoin(StudentEnrolment,and_(StudentEnrolment.student_id==Student.id,
                                             StudentEnrolment.active==1))
            .outerjoin(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
            .outerjoin(AcademicSession,AcademicSession.id==StudentEnrolment.session_id)
            .where(ParentStudentLink.parent_id==pid,
                   ParentStudentLink.student_id==student_id,
                   ParentStudentLink.active==1)
            .order_by(sa.case((AcademicSession.is_current==1,0),else_=1),
                      StudentEnrolment.id.desc()).limit(1))
    if not row: abort(404)
    child=_flatten(row,'Student','relationship','class_name','session_name')
    assignments=_student_assignments(student_id,with_id=False)
    projects=_student_projects(student_id,with_id=False)
    results=_released_results(student_id)
    feedback=db.session.scalars(select(ParentFeedback).where(
        ParentFeedback.parent_id==pid,ParentFeedback.student_id==student_id)
        .order_by(ParentFeedback.id.desc()).limit(10)).all()
    replies=_feedback_replies([x.id for x in feedback])
    avg,completion,trend=_assignment_metrics(assignments)
    fee_summary=_finance_student_lifetime_totals(student_id)

    notif_rows=db.session.scalars(select(SchoolNotification).where(
        SchoolNotification.recipient_type=='parent',SchoolNotification.recipient_id==pid,
        SchoolNotification.student_id==student_id)
        .order_by(SchoolNotification.id.desc()).limit(20)).all()
    notifications=[{'id':n.id,'title':n.title,'message':n.message,'category':n.category,
                    'action_url':n.action_url,'created_at':n.created_at,
                    'is_new':n.read_at is None} for n in notif_rows]
    # Viewing this page is what "reads" these notifications: they show as
    # New once here, then as Old on every visit after this.
    unread_ids=[n.id for n in notif_rows if n.read_at is None]
    if unread_ids:
        now=datetime.now(timezone.utc).isoformat()
        db.session.execute(sa_update(SchoolNotification)
            .where(SchoolNotification.id.in_(unread_ids)).values(read_at=now))
        db.session.commit()

    return render_template('parent_child_detail.html',child=child,assignments=assignments,
        projects=projects,results=results,feedback=feedback,replies=replies,avg=avg,
        completion=completion,trend=trend,fee_summary=fee_summary,notifications=notifications)

@app.route('/parent/children/<int:student_id>/finance')
@parent_required
def parent_child_finance(student_id):
    pid=session['parent_id']
    if not _parent_owns_student(pid,student_id): abort(404)
    student=one(select(Student.id,Student.admission_no,Student.first_name,
                       Student.middle_name,Student.last_name).where(Student.id==student_id))
    if not student: abort(404)

    sessions=_active_sessions()
    requested_session=request.args.get('session_id','').strip()
    try: session_id=int(requested_session) if requested_session else None
    except (TypeError,ValueError): session_id=None
    if not session_id:
        current=_school_current_session()
        session_id=current['id'] if current else None
    session_row=obj(AcademicSession,session_id) if session_id else None

    account=_finance_student_outstanding(student_id,session_id) if session_row else []
    # The headline balance is always the lifetime, all-sessions figure, so a
    # balance left over from a previous session is never hidden just because
    # the session selector below happens to be pointed at a newer one.
    lifetime=_finance_student_lifetime_totals(student_id)
    other_sessions_with_balance=_finance_student_sessions_with_balance(student_id,exclude_session_id=session_id)

    payments=db.session.scalars(select(FinancePayment)
        .where(FinancePayment.student_id==student_id)
        .order_by(FinancePayment.paid_at.desc(),FinancePayment.id.desc())).all()

    return render_template(
        'parent_child_finance.html',
        student=student,sessions=sessions,session=session_row,account=account,
        total_assessed=lifetime['assessed'],total_paid=lifetime['paid'],
        total_outstanding=lifetime['outstanding'],total_unallocated=lifetime['unallocated'],
        other_sessions_with_balance=other_sessions_with_balance,
        payments=payments)

@app.route('/parent/children/<int:student_id>/receipts/<int:payment_id>/pdf')
@parent_required
def parent_receipt_pdf(student_id, payment_id):
    pid=session['parent_id']
    if not _parent_owns_student(pid,student_id): abort(404)
    payment=one(select(FinancePayment.id,FinancePayment.student_id,FinancePayment.receipt_no)
                .where(FinancePayment.id==payment_id))
    if not payment or int(payment['student_id'])!=int(student_id): abort(404)
    pdf,_=_receipt_pdf(payment_id)
    return Response(pdf,mimetype='application/pdf',
                    headers={'Content-Disposition':f'inline; filename={payment["receipt_no"]}.pdf'})

@app.route('/parent/feedback',methods=['GET','POST'])
@parent_required
@csrf_protect
def parent_feedback():
    pid=session['parent_id']
    children=[_flatten(r,'Student','class_name') for r in all_rows(
        select(Student,SchoolClass.name.label('class_name'))
        .select_from(ParentStudentLink)
        .join(Student,and_(Student.id==ParentStudentLink.student_id,Student.active==1))
        .outerjoin(StudentEnrolment,and_(StudentEnrolment.student_id==Student.id,
                                         StudentEnrolment.active==1))
        .outerjoin(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
        .where(ParentStudentLink.parent_id==pid,ParentStudentLink.active==1)
        .group_by(Student.id).order_by(Student.first_name,Student.last_name))]
    parent=obj(ParentAccount,pid)
    if request.method=='POST':
        try: student_id=int(request.form.get('student_id')) if request.form.get('student_id') else None
        except (TypeError,ValueError): student_id=None
        subject=request.form.get('subject','').strip(); body=request.form.get('body','').strip()
        allowed={x['id'] for x in children}; errors=[]
        if student_id is not None and student_id not in allowed: errors.append('Select a child linked to your account.')
        if not subject: errors.append('Enter a subject for your message.')
        if not body: errors.append('Enter your feedback or message.')
        if len(body)>5000: errors.append('Please keep the message under 5,000 characters.')
        if errors:
            return render_template('parent_feedback.html',parent=parent,children=children,errors=errors,form=request.form)
        now=datetime.now(timezone.utc).isoformat(); target_class=None
        if student_id:
            target_class=one(select(SchoolClass.id,SchoolClass.name)
                .select_from(StudentEnrolment)
                .join(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
                .where(StudentEnrolment.student_id==student_id,StudentEnrolment.active==1)
                .order_by(StudentEnrolment.id.desc()).limit(1))
        # Route the message to staff who may manage it, respecting class scope.
        scoped={aid for (aid,) in tuples(select(AdminScope.admin_id).where(
            AdminScope.scope_type=='class').distinct())}
        candidates=[]
        for (aid,) in tuples(select(Admin.id).where(Admin.active==1)):
            if not admin_has_permission(aid,'parent.feedback.manage'):
                continue
            if target_class and not is_super_admin({'id':aid,'admin_type_system':0}):
                # Class-bound staff must cover this class; unscoped staff still qualify.
                if aid in scoped and not _school_class_allowed(aid,target_class['id']):
                    continue
            candidates.append(aid)
        assigned=candidates[0] if candidates else None
        thread=ParentFeedback(parent_id=pid,student_id=student_id,subject=subject,body=body,
                              status='open',assigned_admin_id=assigned,created_at=now,updated_at=now)
        db.session.add(thread); db.session.flush()
        sender_name=parent.display_name if parent else 'A parent'
        notification_message=f'{sender_name}: {subject}'
        exact_feedback_url=url_for('admin_school_parent_feedback_detail',feedback_id=thread.id)
        if assigned:
            targets=[assigned]
        else:
            targets=[aid for (aid,) in tuples(select(Admin.id).where(Admin.active==1))
                     if admin_has_permission(aid,'parent.feedback.view')]
        for aid in targets:
            db.session.add(AdminNotification(admin_id=aid,title='New parent message',
                message=notification_message,severity='info',
                action_url=exact_feedback_url,created_at=now))
        db.session.commit(); flash('Your message has been sent to the school.','success'); return redirect(url_for('parent_feedback'))
    feedback=[_flatten(r,'ParentFeedback','first_name','last_name') for r in all_rows(
        select(ParentFeedback,Student.first_name,Student.last_name)
        .outerjoin(Student,Student.id==ParentFeedback.student_id)
        .where(ParentFeedback.parent_id==pid)
        .order_by(ParentFeedback.id.desc()).limit(20))]
    replies=_feedback_replies([x['id'] for x in feedback])
    return render_template('parent_feedback.html',parent=parent,children=children,feedback=feedback,replies=replies,errors=[],form={})

@app.post('/parent/feedback/<int:feedback_id>/reply')
@parent_required
@csrf_protect
def parent_feedback_reply(feedback_id):
    pid=session['parent_id']
    row=obj(ParentFeedback,feedback_id)
    if not row or row.parent_id!=pid: abort(404)
    if row.status=='resolved':
        flash('This conversation has been marked resolved. It can no longer be replied to.','error')
        return redirect(url_for('parent_feedback'))
    body=request.form.get('body','').strip()
    if not body:
        flash('Enter a message before sending.','error'); return redirect(url_for('parent_feedback'))
    if len(body)>5000:
        flash('Please keep the message under 5,000 characters.','error'); return redirect(url_for('parent_feedback'))
    now=datetime.now(timezone.utc).isoformat()
    db.session.add(ParentFeedbackReply(feedback_id=feedback_id,admin_id=None,body=body,created_at=now))
    row.updated_at=now
    parent=obj(ParentAccount,pid)
    sender_name=parent.display_name if parent else 'A parent'
    notify_targets=[row.assigned_admin_id] if row.assigned_admin_id else [
        aid for (aid,) in tuples(select(Admin.id).where(Admin.active==1))
        if admin_has_permission(aid,'parent.feedback.view')]
    for aid in notify_targets:
        db.session.add(AdminNotification(admin_id=aid,title='New reply from a parent',
            message=f'{sender_name}: {row.subject}',severity='info',
            action_url=url_for('admin_school_parent_feedback_detail',feedback_id=feedback_id),
            created_at=now))
    db.session.commit()
    audit_log('parent_feedback_followup','school','parent_feedback',feedback_id)
    flash('Your message has been sent to the school.','success')
    return redirect(url_for('parent_feedback'))

@app.route('/admin/school/parent-feedback')
@admin_required
def admin_school_parent_feedback():
    me=current_admin()
    if not admin_has_permission(me['id'],'parent.feedback.view'): return admin_access_error('parent.feedback.view')
    rows=[_flatten(r,'ParentFeedback','parent_name','parent_username','first_name',
                   'last_name','class_id','class_name') for r in all_rows(
        select(ParentFeedback,
               ParentAccount.display_name.label('parent_name'),
               ParentAccount.username.label('parent_username'),
               Student.first_name,Student.last_name,
               SchoolClass.id.label('class_id'),SchoolClass.name.label('class_name'))
        .join(ParentAccount,ParentAccount.id==ParentFeedback.parent_id)
        .outerjoin(Student,Student.id==ParentFeedback.student_id)
        .outerjoin(StudentEnrolment,and_(StudentEnrolment.student_id==Student.id,
                                         StudentEnrolment.active==1))
        .outerjoin(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
        .order_by(sa.case((ParentFeedback.status=='open',0),
                          (ParentFeedback.status=='in_progress',1),else_=2),
                  ParentFeedback.id.desc()))]
    if not me['admin_type_system']:
        rows=[r for r in rows if not r['class_id'] or _school_class_allowed(me['id'],r['class_id'])]
    replies=_feedback_replies([r['id'] for r in rows])
    return render_template('admin_parent_feedback.html',feedback=rows,replies=replies)

@app.route('/admin/school/parent-feedback/<int:feedback_id>')
@admin_required
def admin_school_parent_feedback_detail(feedback_id):
    me=current_admin()
    if not admin_has_permission(me['id'],'parent.feedback.view'): return admin_access_error('parent.feedback.view')
    raw=one(select(ParentFeedback,
                   ParentAccount.display_name.label('parent_name'),
                   ParentAccount.username.label('parent_username'),
                   ParentAccount.email.label('parent_email'),
                   ParentAccount.phone.label('parent_phone'),
                   Student.first_name,Student.last_name,
                   SchoolClass.id.label('class_id'),SchoolClass.name.label('class_name'))
            .join(ParentAccount,ParentAccount.id==ParentFeedback.parent_id)
            .outerjoin(Student,Student.id==ParentFeedback.student_id)
            .outerjoin(StudentEnrolment,and_(StudentEnrolment.student_id==Student.id,
                                             StudentEnrolment.active==1))
            .outerjoin(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
            .where(ParentFeedback.id==feedback_id))
    if not raw: abort(404)
    row=_flatten(raw,'ParentFeedback','parent_name','parent_username','parent_email',
                 'parent_phone','first_name','last_name','class_id','class_name')
    if not me['admin_type_system'] and row['class_id'] and not _school_class_allowed(me['id'],row['class_id']): return admin_access_error('parent feedback scope')
    replies=_feedback_replies([feedback_id]).get(feedback_id,[])
    return render_template('admin_parent_feedback_detail.html',feedback=row,replies=replies)

@app.post('/admin/school/parent-feedback/<int:feedback_id>/reply')
@admin_required
@csrf_protect
def admin_school_parent_feedback_reply(feedback_id):
    me=current_admin()
    if not admin_has_permission(me['id'],'parent.feedback.manage'): return admin_access_error('parent.feedback.manage')
    body=request.form.get('body','').strip()
    if not body: flash('Enter a reply.','error'); return redirect(url_for('admin_school_parent_feedback'))
    row=obj(ParentFeedback,feedback_id)
    if not row: abort(404)
    now=datetime.now(timezone.utc).isoformat()
    db.session.add(ParentFeedbackReply(feedback_id=feedback_id,admin_id=me['id'],body=body,created_at=now))
    row.status='in_progress'; row.assigned_admin_id=me['id']; row.updated_at=now
    db.session.add(SchoolNotification(
        recipient_type='parent',recipient_id=row.parent_id,student_id=row.student_id,
        category='feedback',title='School replied to your feedback',message=body,
        action_url=url_for('parent_feedback'),created_at=now,created_by=me['id']))
    db.session.commit()
    # Best-effort: a parent contact that's blank or a channel that's down must
    # never break the in-app reply that just succeeded.
    parent_contact=one(select(ParentAccount.email,ParentAccount.phone,ParentAccount.display_name)
                       .where(ParentAccount.id==row.parent_id))
    if parent_contact:
        subject_line=f'School replied: {row.subject}' if row.subject else 'School replied to your message'
        try:
            _notify_guardian_email(parent_contact['email'],subject_line,
                f"Dear {parent_contact['display_name'] or 'Parent/Guardian'},\n\n"
                f"The school has replied to your message"
                f"{f' ({row.subject})' if row.subject else ''}:\n\n{body}\n\n"
                "Sign in to the parent portal to continue the conversation.\n\n"
                "Creative Rainbow Montessori School")
        except Exception: app.logger.exception('Parent feedback email notification failed for feedback %s',feedback_id)
        try:
            _notify_guardian_whatsapp(parent_contact['phone'],
                "Crainbow School: You have a reply to your message"
                f"{f' ({row.subject})' if row.subject else ''}. "
                "Sign in to the parent portal to view it.")
        except Exception: app.logger.exception('Parent feedback WhatsApp notification failed for feedback %s',feedback_id)
    audit_log('parent_feedback_replied','school','parent_feedback',feedback_id)
    return redirect(url_for('admin_school_parent_feedback'))

@app.post('/admin/school/parent-feedback/<int:feedback_id>/status')
@admin_required
@csrf_protect
def admin_school_parent_feedback_status(feedback_id):
    me=current_admin()
    if not admin_has_permission(me['id'],'parent.feedback.manage'): return admin_access_error('parent.feedback.manage')
    status=request.form.get('status','open')
    if status not in ('open','in_progress','resolved'): status='open'
    row=obj(ParentFeedback,feedback_id)
    if not row: abort(404)
    now=datetime.now(timezone.utc).isoformat()
    row.status=status; row.updated_at=now; row.closed_at=now if status=='resolved' else None
    db.session.commit()
    audit_log('parent_feedback_status_changed','school','parent_feedback',feedback_id,{'status':status})
    return redirect(url_for('admin_school_parent_feedback'))

def student_required(fn):
    @wraps(fn)
    def wrapper(*args,**kwargs):
        sid=session.get('student_id')
        if not sid:
            return redirect(url_for('login',next=request.path))
        row=one(select(Student.id,Student.active,Student.account_active,
                       Student.password_must_change).where(Student.id==sid))
        if not row or not row['active'] or not row['account_active']:
            _clear_identity_sessions(); return redirect(url_for('login'))
        if row['password_must_change'] and request.endpoint != 'student_password_change':
            return redirect(url_for('student_password_change'))
        return fn(*args,**kwargs)
    return wrapper

@app.route('/student/password',methods=['GET','POST'])
@student_required
@csrf_protect
def student_password_change():
    """Allow an authenticated student to replace a temporary password."""
    sid=session.get('student_id')
    student=db.session.scalars(select(Student).where(
        Student.id==sid,Student.active==1,Student.account_active==1)).first()
    if not student:
        _clear_identity_sessions(); return redirect(url_for('login'))

    errors=[]
    if request.method=='POST':
        current_password=request.form.get('current_password','')
        new_password=request.form.get('new_password','')
        confirm_password=request.form.get('confirm_password','')
        if not check_password_hash(student['login_password_hash'] or '',current_password):
            errors.append('Your current password is incorrect.')
        if len(new_password)<8:
            errors.append('Your new password must be at least 8 characters long.')
        if new_password!=confirm_password:
            errors.append('The new password and confirmation do not match.')
        if not errors:
            db.session.execute(sa_update(Student).where(Student.id==sid).values(
                login_password_hash=generate_password_hash(new_password),password_must_change=0))
            db.session.commit()
            flash('Your password has been changed successfully.','success')
            return redirect(url_for('student_dashboard'))
    return render_template('student_password.html',student=student,errors=errors)

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

@app.route('/student/dashboard')
def student_dashboard():
    sid=session.get('student_id')
    if not sid: return redirect(url_for('login'))
    # The helper already restricts to active students with an active account.
    student=_student_with_enrolment(sid)
    if not student:
        _clear_identity_sessions(); return redirect(url_for('login'))
    if student['password_must_change']: return redirect(url_for('student_password_change'))
    _release_due_school_results()
    # Undated work sorts last, then by due date.
    undated=sa.case((or_(SchoolAssignment.due_date.is_(None),SchoolAssignment.due_date==''),1),else_=0)
    assignments=all_rows(
        select(SchoolAssignment.id,SchoolAssignment.title,SchoolAssignment.instructions,
               SchoolAssignment.due_date,SchoolAssignment.date_given,
               SchoolAssignment.assignment_type,SchoolAssignment.timing_mode,
               SchoolAssignment.time_limit_seconds,SchoolAssignment.per_question_seconds,
               AssignmentStudent.status,AssignmentStudent.score,AssignmentStudent.max_score,
               SchoolSubject.name.label('subject_name'))
        .select_from(AssignmentStudent)
        .join(SchoolAssignment,SchoolAssignment.id==AssignmentStudent.assignment_id)
        .join(SchoolSubject,SchoolSubject.id==SchoolAssignment.subject_id)
        .where(AssignmentStudent.student_id==sid,SchoolAssignment.active==1)
        .order_by(undated,SchoolAssignment.due_date,SchoolAssignment.id.desc()).limit(20))
    undated_p=sa.case((or_(SchoolProject.due_date.is_(None),SchoolProject.due_date==''),1),else_=0)
    projects=all_rows(
        select(SchoolProject.id,SchoolProject.title,SchoolProject.instructions,
               SchoolProject.date_given,SchoolProject.due_date,SchoolProject.max_score,
               ProjectStudent.status,ProjectStudent.score,ProjectStudent.remark,
               SchoolSubject.name.label('subject_name'))
        .select_from(ProjectStudent)
        .join(SchoolProject,SchoolProject.id==ProjectStudent.project_id)
        .join(SchoolSubject,SchoolSubject.id==SchoolProject.subject_id)
        .where(ProjectStudent.student_id==sid,SchoolProject.active==1)
        .order_by(undated_p,SchoolProject.due_date,SchoolProject.id.desc()).limit(20))
    notifications=db.session.scalars(select(SchoolNotification).where(
        SchoolNotification.recipient_type=='student',SchoolNotification.recipient_id==sid)
        .order_by(SchoolNotification.id.desc()).limit(12)).all()
    results=all_rows(
        select(SchoolStudentResult.id,SchoolStudentResult.score,SchoolStudentResult.max_score,
               SchoolStudentResult.term,SchoolStudentResult.status,
               SchoolSubject.name.label('subject_name'),
               SchoolAssessment.title.label('assessment_title'))
        .join(SchoolSubject,SchoolSubject.id==SchoolStudentResult.subject_id)
        .outerjoin(SchoolAssessment,SchoolAssessment.id==SchoolStudentResult.assessment_id)
        .where(SchoolStudentResult.student_id==sid,SchoolStudentResult.status=='released')
        .order_by(SchoolStudentResult.id.desc()).limit(10))
    assessments=[]
    if student['class_id'] and student['session_id']:
        kind=sa.case((SchoolAssessment.assessment_type=='test',1),
                     (SchoolAssessment.assessment_type=='examination',2),else_=3)
        assessments=all_rows(
            select(SchoolAssessment.id,SchoolAssessment.assessment_type,SchoolAssessment.title,
                   SchoolAssessment.instructions,SchoolAssessment.duration_minutes,
                   SchoolAssessment.question_count,SchoolSubject.name.label('subject_name'))
            .join(SchoolSubject,SchoolSubject.id==SchoolAssessment.subject_id)
            .where(SchoolAssessment.class_id==student['class_id'],SchoolAssessment.active==1,
                   SchoolAssessment.question_count>0,
                   or_(SchoolAssessment.session_id==student['session_id'],
                       SchoolAssessment.session_id.is_(None)))
            .order_by(kind,SchoolAssessment.id.desc()).limit(12))
    counts={k:sum(1 for a in assessments if a['assessment_type']==k) for k in ('practice','test','examination')}
    return render_template('student_dashboard.html',student=student,assignments=assignments,projects=projects,notifications=notifications,results=results,assessments=assessments,assessment_counts=counts)

@app.route('/student/assignments/<int:assignment_id>',methods=['GET','POST'])
@student_required
@csrf_protect
def student_assignment_detail(assignment_id):
    sid=session.get('student_id')
    # The assignment_students row is what authorises access.
    raw=one(select(SchoolAssignment,SchoolSubject.name.label('subject_name'),
                   SchoolClass.name.label('class_name'),AssignmentStudent.status,
                   AssignmentStudent.score,AssignmentStudent.max_score,
                   AssignmentStudent.started_at,AssignmentStudent.submitted_at)
            .select_from(AssignmentStudent)
            .join(SchoolAssignment,SchoolAssignment.id==AssignmentStudent.assignment_id)
            .join(SchoolSubject,SchoolSubject.id==SchoolAssignment.subject_id)
            .join(SchoolClass,SchoolClass.id==SchoolAssignment.class_id)
            .where(AssignmentStudent.student_id==sid,SchoolAssignment.id==assignment_id,
                   SchoolAssignment.active==1))
    if not raw: abort(404)
    a=_flatten(raw,'SchoolAssignment','subject_name','class_name','status','score',
               'max_score','started_at','submitted_at')
    if request.method=='POST' and request.form.get('action')=='start':
        if a['assignment_type']!='quiz': return redirect(url_for('student_assignment_detail',assignment_id=assignment_id))
        existing=db.session.scalars(select(SchoolAssignmentAttempt).where(
            SchoolAssignmentAttempt.assignment_id==assignment_id,
            SchoolAssignmentAttempt.student_id==sid)).first()
        if existing and existing.status=='submitted': return redirect(url_for('student_assignment_detail',assignment_id=assignment_id))
        if existing and existing.status=='active':
            # The student started but never finished: resume at the first
            # unanswered question instead of falling through to a page with
            # no action, which is what silently made "Continue" a no-op.
            answered=one_scalar(select(func.count(func.distinct(SchoolAssignmentAnswer.question_id)))
                .where(SchoolAssignmentAnswer.attempt_id==existing.id),0)
            total_qs=one_scalar(select(func.count()).select_from(SchoolAssignmentAttemptQuestion)
                .where(SchoolAssignmentAttemptQuestion.attempt_id==existing.id),0)
            session['assignment_attempt_id']=existing.id
            session['assignment_question_index']=min(answered+1,max(total_qs,1))
            session['assignment_question_started_at']=datetime.now(timezone.utc).isoformat()
            return redirect(url_for('student_assignment_take',assignment_id=assignment_id))
        if not existing:
            qs=db.session.scalars(select(AssignmentQuestion)
                .where(AssignmentQuestion.assignment_id==assignment_id)
                .order_by(AssignmentQuestion.sort_order,AssignmentQuestion.id)).all()
            if not qs: return render_template('student_assignment_detail.html',assignment=a,questions=[],error='This CBT assignment has not been prepared with questions yet.')
            now=datetime.now(timezone.utc)
            expires=(now+timedelta(seconds=a['time_limit_seconds'])).isoformat() if a['timing_mode']=='overall' and a['time_limit_seconds'] else None
            attempt=SchoolAssignmentAttempt(assignment_id=assignment_id,student_id=sid,
                started_at=now.isoformat(),expires_at=expires,status='active')
            db.session.add(attempt); db.session.flush()
            # Freeze the questions so later edits cannot change a live attempt.
            for i,q in enumerate(qs,1):
                db.session.add(SchoolAssignmentAttemptQuestion(
                    attempt_id=attempt.id,question_id=q.id,question_order=i,
                    question_text=q.question_text,option_a=q.option_a,option_b=q.option_b,
                    option_c=q.option_c,option_d=q.option_d,instruction=q.instruction,
                    image_path=q.image_path,correct_option=q.correct_option,points=q.points))
            db.session.execute(sa_update(AssignmentStudent)
                .where(AssignmentStudent.assignment_id==assignment_id,
                       AssignmentStudent.student_id==sid)
                .values(status='in_progress',started_at=now.isoformat()))
            db.session.commit()
            session['assignment_attempt_id']=attempt.id
            session['assignment_question_index']=1
            session['assignment_question_started_at']=now.isoformat()
            return redirect(url_for('student_assignment_take',assignment_id=assignment_id))
    questions=db.session.scalars(select(AssignmentQuestion)
        .where(AssignmentQuestion.assignment_id==assignment_id)
        .order_by(AssignmentQuestion.sort_order,AssignmentQuestion.id)).all() if a['assignment_type']=='quiz' else []
    return render_template('student_assignment_detail.html',student_id=sid,assignment=a,questions=questions,error=None)

@app.route('/student/assignments/<int:assignment_id>/take',methods=['GET','POST'])
@student_required
@csrf_protect
def student_assignment_take(assignment_id):
    sid=session.get('student_id')
    attempt=db.session.scalars(select(SchoolAssignmentAttempt).where(
        SchoolAssignmentAttempt.assignment_id==assignment_id,
        SchoolAssignmentAttempt.student_id==sid,
        SchoolAssignmentAttempt.status=='active')).first()
    a=db.session.scalars(select(SchoolAssignment).where(
        SchoolAssignment.id==assignment_id,SchoolAssignment.active==1)).first()
    if not attempt or not a: return redirect(url_for('student_assignment_detail',assignment_id=assignment_id))
    qs=db.session.scalars(select(SchoolAssignmentAttemptQuestion)
        .where(SchoolAssignmentAttemptQuestion.attempt_id==attempt.id)
        .order_by(SchoolAssignmentAttemptQuestion.question_order)).all()
    answers={qid:opt for qid,opt in tuples(
        select(SchoolAssignmentAnswer.question_id,SchoolAssignmentAnswer.option_index)
        .where(SchoolAssignmentAnswer.attempt_id==attempt.id))}
    if not qs: return redirect(url_for('student_assignment_detail',assignment_id=assignment_id))
    try: idx=max(1,min(len(qs),int(session.get('assignment_question_index',1))))
    except (TypeError,ValueError): idx=1
    now=datetime.now(timezone.utc); timed_out=False
    if attempt.expires_at:
        try: timed_out=now>=datetime.fromisoformat(attempt.expires_at)
        except ValueError: timed_out=False
    current_started=session.get('assignment_question_started_at')
    if a.timing_mode=='per_question' and current_started:
        try: timed_out=timed_out or now>=datetime.fromisoformat(current_started)+timedelta(seconds=int(a.per_question_seconds or 0))
        except (TypeError,ValueError): pass
    if request.method=='POST':
        qid=request.form.get('question_id',type=int)
        q=next((x for x in qs if x.question_id==qid and x.question_order==idx),None)
        if not q: abort(400)
        if not timed_out:
            try: option=int(request.form.get('option_index')) if request.form.get('option_index') not in (None,'') else None
            except (TypeError,ValueError): option=None
            if option not in (0,1,2,3): option=None
            stmt=sqlite_insert(SchoolAssignmentAnswer).values(
                attempt_id=attempt.id,question_id=qid,option_index=option,
                answered_at=now.isoformat())
            db.session.execute(stmt.on_conflict_do_update(
                index_elements=['attempt_id','question_id'],
                set_={'option_index':stmt.excluded.option_index,
                      'answered_at':stmt.excluded.answered_at}))
            answers[qid]=option
        if idx>=len(qs) or timed_out:
            # Grade from the frozen snapshot, never from the live question bank.
            score=sum(float(x.points) for x in qs if answers.get(x.question_id)==x.correct_option)
            max_score=sum(float(x.points) for x in qs)
            pct=round(score/max_score*100,2) if max_score else 0
            attempt.status='submitted'; attempt.submitted_at=now.isoformat()
            attempt.score=score; attempt.max_score=max_score; attempt.percentage=pct
            db.session.execute(sa_update(AssignmentStudent)
                .where(AssignmentStudent.assignment_id==assignment_id,
                       AssignmentStudent.student_id==sid)
                .values(status='done',score=score,max_score=max_score,
                        submitted_at=now.isoformat()))
            db.session.commit()
            session.pop('assignment_attempt_id',None); session.pop('assignment_question_index',None); session.pop('assignment_question_started_at',None)
            return redirect(url_for('student_assignment_detail',assignment_id=assignment_id))
        session['assignment_question_index']=idx+1; session.pop('assignment_question_started_at',None)
        db.session.commit()
    idx=max(1,min(len(qs),int(session.get('assignment_question_index',idx)))); q=qs[idx-1]; now=datetime.now(timezone.utc)
    if a.timing_mode=='per_question':
        started=session.get('assignment_question_started_at')
        if not started: session['assignment_question_started_at']=now.isoformat(); started=now.isoformat()
        try: timed_out=now>=datetime.fromisoformat(started)+timedelta(seconds=int(a.per_question_seconds or 0))
        except (TypeError,ValueError): timed_out=False
    return render_template('student_assignment_take.html',assignment=a,question=q,index=idx,total=len(qs),selected=answers.get(q.question_id),timed_out=timed_out,expires_at=attempt.expires_at,per_question_seconds=a.per_question_seconds)

def _student_assessment_context(assessment_id, sid):
    student=_student_with_enrolment(sid)
    a=_flatten(one(select(SchoolAssessment,SchoolClass.name.label('class_name'),
                          SchoolSubject.name.label('subject_name'),
                          AcademicSession.name.label('session_name'))
        .join(SchoolClass,SchoolClass.id==SchoolAssessment.class_id)
        .join(SchoolSubject,SchoolSubject.id==SchoolAssessment.subject_id)
        .outerjoin(AcademicSession,AcademicSession.id==SchoolAssessment.session_id)
        .where(SchoolAssessment.id==assessment_id,SchoolAssessment.active==1,
               SchoolAssessment.question_count>0)),
        'SchoolAssessment','class_name','subject_name','session_name')
    if not student or not a or not student['class_id'] or a['class_id']!=student['class_id']:
        return None,None,[]
    if a['session_id'] is not None and a['session_id']!=student['session_id']:
        return None,None,[]
    questions=db.session.scalars(select(SchoolQuestion)
        .where(SchoolQuestion.assessment_id==assessment_id)
        .order_by(SchoolQuestion.sort_order,SchoolQuestion.id)).all()
    return student,a,questions

@app.route('/student/<string:kind>')
@student_required
def student_assessment_list(kind):
    if kind not in ('practice','tests','examinations'):
        abort(404)
    assessment_type={'practice':'practice','tests':'test','examinations':'examination'}[kind]
    sid=session.get('student_id')
    student=_student_with_enrolment(sid)
    rows=[]
    if student and student['class_id']:
        rows=[_flatten(r,'SchoolAssessment','subject_name') for r in all_rows(
            select(SchoolAssessment,SchoolSubject.name.label('subject_name'))
                .join(SchoolSubject,SchoolSubject.id==SchoolAssessment.subject_id)
                .where(SchoolAssessment.assessment_type==assessment_type,
                       SchoolAssessment.class_id==student['class_id'],
                       SchoolAssessment.active==1,SchoolAssessment.question_count>0,
                       or_(SchoolAssessment.session_id==student['session_id'],
                           SchoolAssessment.session_id.is_(None)))
                .order_by(SchoolAssessment.id.desc()))]
    title={'practice':'Practice Tests','tests':'Tests','examinations':'Examinations'}[kind]
    return render_template('student_assessment_list.html',student=student,assessments=rows,title=title,assessment_type=assessment_type)

@app.post('/student/assessments/<int:assessment_id>/start')
@student_required
@csrf_protect
def student_assessment_start(assessment_id):
    sid=session.get('student_id')
    student,a,questions=_student_assessment_context(assessment_id,sid)
    if not a: abort(404)
    existing=db.session.scalars(select(SchoolAssessmentAttempt).where(
        SchoolAssessmentAttempt.student_id==sid,
        SchoolAssessmentAttempt.assessment_id==assessment_id)).first()
    if existing:
        if existing['status']=='active':
            return redirect(url_for('student_assessment_take',assessment_id=assessment_id,q=1))
        return redirect(url_for('student_assessment_result',assessment_id=assessment_id))
    now=datetime.now(timezone.utc)
    expires=now+timedelta(minutes=int(a['duration_minutes']))
    try:
        attempt=SchoolAssessmentAttempt(student_id=sid,assessment_id=assessment_id,
            started_at=now.isoformat(),expires_at=expires.isoformat(),status='active')
        db.session.add(attempt); db.session.flush()
        attempt_id=attempt.id
        db.session.add_all([
            SchoolAssessmentAttemptQuestion(
                attempt_id=attempt_id,question_id=q['id'],question_order=order,
                question_text=q['question_text'],option_a=q['option_a'],option_b=q['option_b'],
                option_c=q['option_c'],option_d=q['option_d'],instruction=q['instruction'],
                image_path=q['image_path'],correct_option=int(q['correct_option']),
                points=int(q['points'] or 1))
            for order,q in enumerate(questions,1)])
        db.session.commit()
    except sa.exc.IntegrityError:
        # The UNIQUE(student_id, assessment_id) constraint protects against
        # double-clicks or two browser tabs starting the same assessment.
        db.session.rollback()
        existing=db.session.scalars(select(SchoolAssessmentAttempt).where(
            SchoolAssessmentAttempt.student_id==sid,
            SchoolAssessmentAttempt.assessment_id==assessment_id)).first()
        if existing:
            return redirect(url_for('student_assessment_take',assessment_id=assessment_id,q=1))
        raise
    except sa.exc.SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Failed to start student assessment %s", assessment_id)
        raise
    audit_log('school_assessment_started','school_assessment','assessment',assessment_id,
              {'student_id':sid,'attempt_id':attempt_id},True)
    return redirect(url_for('student_assessment_take',assessment_id=assessment_id,q=1))

@app.route('/student/assessments/<int:assessment_id>',methods=['GET'])
@student_required
def student_assessment_take(assessment_id):
    sid=session.get('student_id')
    student,a,questions=_student_assessment_context(assessment_id,sid)
    if not a: abort(404)
    attempt=db.session.scalars(select(SchoolAssessmentAttempt).where(
        SchoolAssessmentAttempt.student_id==sid,
        SchoolAssessmentAttempt.assessment_id==assessment_id)).first()
    if not attempt:
        return render_template('student_assessment_start.html',student=student,assessment=a)
    if attempt['status']=='active' and remaining(attempt)<=0:
        student_assessment_grade(attempt['id'], auto=True)
        return redirect(url_for('student_assessment_result',assessment_id=assessment_id))
    if attempt['status']!='active':
        return redirect(url_for('student_assessment_result',assessment_id=assessment_id))
    rows=db.session.scalars(select(SchoolAssessmentAttemptQuestion)
        .where(SchoolAssessmentAttemptQuestion.attempt_id==attempt['id'])
        .order_by(SchoolAssessmentAttemptQuestion.question_order)).all()
    saved=dict(tuples(select(SchoolAssessmentAnswer.question_id,SchoolAssessmentAnswer.option_index)
        .where(SchoolAssessmentAnswer.attempt_id==attempt['id'])))
    if not rows: abort(409,description='This assessment attempt has no frozen question set.')
    try: current=int(request.args.get('q',1))
    except (TypeError,ValueError): current=1
    current=max(1,min(len(rows),current))
    q=rows[current-1]
    public_q=dict(q)
    public_q.pop('correct_option',None)
    return render_template('student_assessment_take.html',
        student=student,assessment=a,question=public_q,current=current,
        total=len(rows),saved=saved,remaining=remaining(attempt),
        csrf_token_value=csrf_token(),attempt_id=attempt['id'],
        rows_answered={r['question_order'] for r in rows if r['question_id'] in saved})

def student_assessment_grade(attempt_id,auto=False):
    """Grade a frozen student assessment attempt safely and idempotently."""
    try:
        attempt=obj(SchoolAssessmentAttempt,attempt_id)
        if not attempt:
            return None
        if attempt.status in ('submitted','expired'):
            return attempt

        # Grade from the frozen snapshot, never from the live question bank.
        questions=all_rows(
            select(SchoolAssessmentAttemptQuestion.question_id,
                   SchoolAssessmentAttemptQuestion.correct_option,
                   SchoolAssessmentAttemptQuestion.points)
            .where(SchoolAssessmentAttemptQuestion.attempt_id==attempt_id)
            .order_by(SchoolAssessmentAttemptQuestion.question_order))
        answers={qid:opt for qid,opt in tuples(
            select(SchoolAssessmentAnswer.question_id,SchoolAssessmentAnswer.option_index)
            .where(SchoolAssessmentAnswer.attempt_id==attempt_id))}

        score=sum(int(q['points'] or 1) for q in questions
                  if answers.get(q['question_id'])==q['correct_option'])
        max_score=sum(int(q['points'] or 1) for q in questions)
        pct=(score/max_score*100) if max_score else 0
        status='expired' if auto else 'submitted'
        submitted=datetime.now(timezone.utc).isoformat()

        assessment_meta=one(select(SchoolAssessment.assessment_type,
                                   SchoolAssessment.session_id,
                                   SchoolAssessment.subject_id,
                                   SchoolAssessment.term)
                            .where(SchoolAssessment.id==attempt.assessment_id))
        # Practice results are visible immediately; graded work waits for the
        # session's release schedule.
        result_status='released' if assessment_meta and assessment_meta['assessment_type']=='practice' else 'entered'
        if assessment_meta and assessment_meta['assessment_type']!='practice':
            release=one_scalar(select(AcademicSession.result_release_at)
                               .where(AcademicSession.id==assessment_meta['session_id']))
            if release and release <= submitted:
                result_status='approved'

        # Conditional on status='active' so two concurrent submissions cannot
        # both post a result.
        updated=db.session.execute(sa_update(SchoolAssessmentAttempt)
            .where(SchoolAssessmentAttempt.id==attempt_id,
                   SchoolAssessmentAttempt.status=='active')
            .values(submitted_at=submitted,score=score,max_score=max_score,
                    percentage=pct,status=status)).rowcount
        if not updated:
            db.session.rollback()
            return obj(SchoolAssessmentAttempt,attempt_id)

        # Never overwrite a result that has already been released.
        already_released=one_scalar(select(SchoolStudentResult.id).where(
            SchoolStudentResult.student_id==attempt.student_id,
            SchoolStudentResult.assessment_id==attempt.assessment_id,
            SchoolStudentResult.status=='released'))
        if not already_released and assessment_meta:
            db.session.add(SchoolStudentResult(
                student_id=attempt.student_id,
                assessment_id=attempt.assessment_id,
                subject_id=assessment_meta['subject_id'],
                score=score,max_score=max_score,
                term=assessment_meta['term'] or 'Full Session',
                session_id=assessment_meta['session_id'],
                status=result_status,created_at=submitted))
        db.session.commit()
        db.session.refresh(attempt)
        return attempt
    except sa.exc.SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Failed to grade student assessment attempt %s", attempt_id)
        raise

@app.post('/student/assessments/<int:assessment_id>/answer')
@student_required
@csrf_protect
def student_assessment_answer(assessment_id):
    sid=session.get('student_id')
    try: requested_attempt=int(request.form.get('attempt_id','0'))
    except (TypeError,ValueError): abort(400)
    attempt=db.session.scalars(select(SchoolAssessmentAttempt).where(
        SchoolAssessmentAttempt.id==requested_attempt,
        SchoolAssessmentAttempt.student_id==sid,
        SchoolAssessmentAttempt.assessment_id==assessment_id)).first()
    if not attempt:
        abort(404)
    if attempt['status']!='active':
        return redirect(url_for('student_assessment_result',assessment_id=assessment_id))
    if remaining(attempt)<=0:
        student_assessment_grade(attempt['id'],auto=True)
        return redirect(url_for('student_assessment_result',assessment_id=assessment_id))
    try: qid=int(request.form.get('question_id','0')); opt=int(request.form.get('option_index','-1'))
    except (TypeError,ValueError):
        abort(400)
    q=db.session.scalars(select(SchoolAssessmentAttemptQuestion).where(
        SchoolAssessmentAttemptQuestion.attempt_id==attempt['id'],
        SchoolAssessmentAttemptQuestion.question_id==qid)).first()
    if not q:
        abort(400)
    option_count=sum(1 for x in (q['option_a'],q['option_b'],q['option_c'],q['option_d']) if x is not None)
    if opt<0 or opt>=option_count:
        abort(400)
    answer_stmt=sqlite_insert(SchoolAssessmentAnswer).values(
        attempt_id=attempt['id'],question_id=qid,option_index=opt,
        answered_at=datetime.now(timezone.utc).isoformat())
    db.session.execute(answer_stmt.on_conflict_do_update(
        index_elements=['attempt_id','question_id'],
        set_={'option_index':answer_stmt.excluded.option_index,
              'answered_at':answer_stmt.excluded.answered_at}))
    db.session.commit()
    next_q=request.form.get('next_q')
    try: next_q=int(next_q)
    except (TypeError,ValueError): next_q=1
    if request.form.get('submit_assessment')=='1':
        student_assessment_grade(attempt['id'],auto=False)
        return redirect(url_for('student_assessment_result',assessment_id=assessment_id))
    return redirect(url_for('student_assessment_take',assessment_id=assessment_id,q=max(1,next_q)))

@app.route('/student/assessments/<int:assessment_id>/result')
@student_required
def student_assessment_result(assessment_id):
    sid=session.get('student_id')
    _release_due_school_results()
    attempt=db.session.scalars(select(SchoolAssessmentAttempt).where(
        SchoolAssessmentAttempt.student_id==sid,
        SchoolAssessmentAttempt.assessment_id==assessment_id)).first()
    if not attempt: abort(404)
    if attempt['status']=='active' and remaining(attempt)<=0:
        attempt=student_assessment_grade(attempt['id'],auto=True)
    if attempt['status']=='active': return redirect(url_for('student_assessment_take',assessment_id=assessment_id,q=1))
    student,a,_=_student_assessment_context(assessment_id,sid)
    result_row=one(select(SchoolStudentResult.status,SchoolStudentResult.score,
                          SchoolStudentResult.max_score)
        .where(SchoolStudentResult.student_id==sid,
               SchoolStudentResult.assessment_id==assessment_id,
               SchoolStudentResult.status=='released')
        .order_by(SchoolStudentResult.id.desc()).limit(1))
    visible=bool(result_row) if a and a['assessment_type']!='practice' else True
    return render_template('student_assessment_result.html',student=student,assessment=a,
                           result=(result_row or attempt),percentage=(result_row['score']/result_row['max_score']*100 if result_row and result_row['max_score'] else attempt['percentage']),submitted=True,visible_to_student=visible)

def _entrance_config_select(*extra):
    """Entrance configs joined to their bank and session."""
    return (select(EntranceBankConfig,
                   Examination.name.label('exam_name'),
                   AcademicSession.name.label('session_name'),*extra)
            .join(Examination,Examination.bank_id==EntranceBankConfig.bank_id)
            .join(AcademicSession,AcademicSession.id==EntranceBankConfig.session_id))


ENTRANCE_CONFIG_EXTRA=('exam_name','session_name')


@app.route('/entrance-practice')
def entrance_practice():
    current=_school_current_session()
    stmt=_entrance_config_select().where(EntranceBankConfig.practice_enabled==1)
    if current:
        # The live session is never offered as practice material.
        stmt=stmt.where(EntranceBankConfig.session_id!=current['id'])
    stmt=stmt.order_by(EntranceBankConfig.session_id.desc(),
                       EntranceBankConfig.entry_group,EntranceBankConfig.subject)
    configs=[_flatten(r,'EntranceBankConfig',*ENTRANCE_CONFIG_EXTRA) for r in all_rows(stmt)]
    return render_template('entrance_practice.html',configs=configs)

@app.route('/entrance-practice/<int:config_id>',methods=['GET','POST'])
def entrance_practice_take(config_id):
    current=_school_current_session()
    raw=one(_entrance_config_select().where(EntranceBankConfig.id==config_id,
                                            EntranceBankConfig.practice_enabled==1))
    cfg=_flatten(raw,'EntranceBankConfig',*ENTRANCE_CONFIG_EXTRA) if raw else None
    if not cfg or (current and cfg['session_id']==current['id']):
        abort(404)
    b=bank(cfg['bank_id'])
    valid,marks=_valid_question_configuration(cfg['questions_to_serve'])
    if not b or not valid or cfg['questions_to_serve']>len(b.get('questions',[])):
        return render_template('entrance_practice.html',configs=[],error='This practice set is not currently available.')
    if request.method=='GET':
        chosen=random.sample(list(b['questions']),int(cfg['questions_to_serve']))
        session['entrance_practice_config']=config_id
        session['entrance_practice_questions']=[int(q['id']) for q in chosen]
        public=[dict(q,answer=None,points=marks) for q in chosen]
        for q in public: q.pop('answer',None)
        return render_template('entrance_practice_take.html',config=cfg,questions=public)
    if session.get('entrance_practice_config')!=config_id:
        return redirect(url_for('entrance_practice_take',config_id=config_id))
    ids={int(x) for x in session.get('entrance_practice_questions',[])}
    selected=[q for q in b['questions'] if int(q['id']) in ids]
    score=0
    results=[]
    for q in selected:
        raw_choice=request.form.get(f"q_{q['id']}")
        try: choice=int(raw_choice) if raw_choice is not None else -1
        except ValueError: choice=-1
        correct=choice==int(q.get('answer',-1))
        if correct: score+=marks
        results.append({'text':q.get('text',''),'selected':choice,'correct':int(q.get('answer',-1)),'options':q.get('options',[])})
    score=round(score,2)
    max_score=round(marks*len(selected),2)
    pct=round(score/max_score*100,2) if max_score else 0
    session.pop('entrance_practice_config',None); session.pop('entrance_practice_questions',None)
    return render_template('entrance_practice_result.html',config=cfg,score=score,max_score=max_score,percentage=pct,results=results)

@app.route('/practice',methods=['GET','POST'])
@csrf_protect
def practice():
    current=_school_current_session()
    # Practice tests from the current session are reserved for enrolled students;
    # the public practice area only offers assessments from past sessions.
    stmt=(select(SchoolAssessment,SchoolClass.name.label('class_name'),
                 SchoolSubject.name.label('subject_name'),
                 AcademicSession.name.label('session_name'))
        .join(SchoolClass,SchoolClass.id==SchoolAssessment.class_id)
        .join(SchoolSubject,SchoolSubject.id==SchoolAssessment.subject_id)
        .outerjoin(AcademicSession,AcademicSession.id==SchoolAssessment.session_id)
        .where(SchoolAssessment.assessment_type=='practice',SchoolAssessment.active==1,
               SchoolAssessment.session_id.is_not(None)))
    if current:
        stmt=stmt.where(SchoolAssessment.session_id!=current['id'])
    rows=[_flatten(r,'SchoolAssessment','class_name','subject_name','session_name')
          for r in all_rows(stmt.order_by(SchoolClass.level_order,SchoolSubject.name,
                                          SchoolAssessment.id.desc()))]
    classes=db.session.scalars(select(SchoolClass).where(SchoolClass.active==1)
                               .order_by(SchoolClass.level_order)).all()
    subjects=db.session.scalars(select(SchoolSubject).where(SchoolSubject.active==1)
                                .order_by(SchoolSubject.name)).all()
    if request.method=='POST':
        try: cid=int(request.form.get('class_id','0')); sid=int(request.form.get('subject_id','0'))
        except (TypeError,ValueError): cid=sid=0
        matches=[r for r in rows if r['class_id']==cid and r['subject_id']==sid]
        if not matches:
            return render_template('practice.html',classes=classes,subjects=subjects,assessments=rows,error='No eligible practice test is available for that class and subject yet.')
        return redirect(url_for('practice_take',assessment_id=matches[0]['id']))
    return render_template('practice.html',classes=classes,subjects=subjects,assessments=rows,error=None)

@app.route('/practice/<int:assessment_id>',methods=['GET','POST'])
@csrf_protect
def practice_take(assessment_id):
    current=_school_current_session()
    a=_flatten(one(select(SchoolAssessment,SchoolClass.name.label('class_name'),
                          SchoolSubject.name.label('subject_name'),
                          AcademicSession.name.label('session_name'))
        .join(SchoolClass,SchoolClass.id==SchoolAssessment.class_id)
        .join(SchoolSubject,SchoolSubject.id==SchoolAssessment.subject_id)
        .outerjoin(AcademicSession,AcademicSession.id==SchoolAssessment.session_id)
        .where(SchoolAssessment.id==assessment_id,
               SchoolAssessment.assessment_type=='practice',SchoolAssessment.active==1)),
        'SchoolAssessment','class_name','subject_name','session_name')
    if not a or (current and a['session_id']==current['id']): abort(404)
    questions=db.session.scalars(select(SchoolQuestion)
        .where(SchoolQuestion.assessment_id==assessment_id)
        .order_by(SchoolQuestion.sort_order,SchoolQuestion.id)).all()
    if request.method=='GET':
        if not questions: return render_template('practice.html',classes=[],subjects=[],assessments=[],error='This practice test has no questions yet.')
        chosen=random.sample(list(questions),min(len(questions),20))
        session['practice_questions']=[q['id'] for q in chosen]; session['practice_assessment']=assessment_id
        return render_template('practice_take.html',assessment=a,questions=chosen)
    ids=session.get('practice_questions',[])
    if session.get('practice_assessment')!=assessment_id or not ids:
        return redirect(url_for('practice_take',assessment_id=assessment_id))
    answer_map={}
    for qid in ids:
        raw=request.form.get(f'q_{qid}')
        try: answer_map[qid]=int(raw) if raw is not None else -1
        except ValueError: answer_map[qid]=-1
    selected={q['id']:q for q in questions if q['id'] in ids}
    score=sum(1 for qid,q in selected.items() if answer_map.get(qid)==q['correct_option'])
    max_score=len(selected); pct=(score/max_score*100) if max_score else 0
    result_questions=[{'text':q['question_text'],'selected':answer_map.get(q['id'],-1),'correct':q['correct_option']} for q in selected.values()]
    session.pop('practice_questions',None); session.pop('practice_assessment',None)
    return render_template('practice_result.html',assessment=a,score=score,max_score=max_score,percentage=pct,results=result_questions)

@app.route('/candidate/dashboard')
def candidate_dashboard():
    c=candidate_record(session.get('candidate_id'))
    if not c: session.pop('candidate_id',None); return redirect(url_for('login'))
    rows,total_score,total_max,pct,completed=candidate_cumulative(c['id'])
    expired_any=False
    for r in rows:
        if r['status']=='active' and r['attempt_id']:
            attempt=get_attempt(r['attempt_id'])
            if attempt and remaining(attempt)<=0:
                grade(attempt['id'],auto=True); expired_any=True
    if expired_any:
        rows,total_score,total_max,pct,completed=candidate_cumulative(c['id'])
    papers=[]
    for r in rows:
        status=r['status'] or 'not_started'
        label='In Progress' if status=='active' else ('Completed' if status in ('submitted','expired') else 'Not Started')
        retake=bool(candidate_has_unused_retake(c['id'],c['candidate_name'],r['bank_id'])) if status in ('submitted','expired') else False
        # Do not pass score, max_score or percentage into the candidate template context.
        subject_label = entrance_subject_label(r['bank_id'], r['exam_name'] or '')
        papers.append({'id':r['paper_id'],'slot':r['slot'],'bank_id':r['bank_id'],
                       'exam_name':r['exam_name'],'subject_label':subject_label,
                       'paper_label':entrance_paper_label(r['slot'], r['bank_id'], r['exam_name'] or ''),
                       'status':status,'display_status':label, 'retake_available':retake})
    response=Response(render_template('candidate_dashboard.html',candidate=c,papers=papers,completed=completed))
    response.headers['Cache-Control']='no-store, no-cache, must-revalidate, max-age=0'; response.headers['Pragma']='no-cache'
    return response

@app.post('/candidate/papers/<int:paper_id>/start')
@csrf_protect
def candidate_start_paper(paper_id):
    c=candidate_record(session.get('candidate_id'))
    if not c: return redirect(url_for('login'))
    # The candidate_id predicate is what stops one candidate starting another's paper.
    raw=one(select(CandidatePaper,
                   Examination.id.label('exam_id'),
                   Examination.active.label('exam_active'),
                   EntranceBankConfig.questions_to_serve,
                   EntranceBankConfig.marks_per_question,
                   EntranceBankConfig.active.label('config_active'),
                   EntranceBankConfig.entry_group,EntranceBankConfig.subject,
                   EntranceBankConfig.session_id.label('config_session_id'))
            .join(Examination,Examination.bank_id==CandidatePaper.bank_id)
            .outerjoin(EntranceBankConfig,EntranceBankConfig.id==CandidatePaper.config_id)
            .where(CandidatePaper.id==paper_id,CandidatePaper.candidate_id==c['id']))
    if not raw: abort(404)
    paper=_flatten(raw,'CandidatePaper','exam_id','exam_active','questions_to_serve',
                   'marks_per_question','config_active','entry_group','subject',
                   'config_session_id')
    b=bank(paper['bank_id'])
    if not b or not b.get('questions'):
        flash('This paper is not ready because its question bank is empty.','error'); return redirect(url_for('candidate_dashboard'))
    if not paper['config_id']:
        flash('This paper is not linked to a verified examination configuration. Please contact the school administrator.','error'); return redirect(url_for('candidate_dashboard'))
    serve_count=int(paper['questions_to_serve'] or len(b['questions']))
    valid,marks=_valid_question_configuration(serve_count)
    if not valid or serve_count>len(b['questions']):
        flash('This paper has an invalid examination configuration. Please contact the school administrator.','error'); return redirect(url_for('candidate_dashboard'))

    # Close out anything that has run out of time before deciding what is open.
    active_attempts=db.session.scalars(select(Attempt).where(
        Attempt.candidate_id==c['id'],Attempt.status=='active')
        .order_by(Attempt.id.desc())).all()
    for active_attempt in active_attempts:
        if remaining(active_attempt)<=0:
            grade(active_attempt.id,auto=True)
    other_active=one(select(Attempt.id,Attempt.bank_id).where(
        Attempt.candidate_id==c['id'],Attempt.status=='active',
        Attempt.bank_id!=paper['bank_id']).order_by(Attempt.id.desc()).limit(1))
    if other_active:
        flash('You must submit your current paper before starting another paper.','error')
        return redirect(url_for('candidate_dashboard'))

    existing=db.session.scalars(select(Attempt).where(
        Attempt.candidate_id==c['id'],Attempt.bank_id==paper['bank_id'])
        .order_by(Attempt.id.desc()).limit(1)).first()
    if existing and existing.status=='active':
        db.session.commit(); session['attempt_id']=existing.id; return redirect(url_for('exam',q=1))
    if existing and existing.status in ('submitted','expired'):
        grant_id=one_scalar(select(RetakeGrant.id).where(
            RetakeGrant.bank_id==paper['bank_id'],RetakeGrant.candidate_id==c['id'],
            RetakeGrant.used_at.is_(None)).order_by(RetakeGrant.id.asc()).limit(1))
        if not grant_id:
            flash('You have already taken this paper. Please contact the school administrator if another attempt is required.','error'); return redirect(url_for('candidate_dashboard'))
        # Claim the grant conditionally so two requests cannot both consume it.
        claimed=db.session.execute(sa_update(RetakeGrant)
            .where(RetakeGrant.id==grant_id,RetakeGrant.used_at.is_(None))
            .values(used_at=datetime.now(timezone.utc).isoformat()))
        if claimed.rowcount != 1:
            db.session.rollback(); flash('The retake permission has already been used.','error'); return redirect(url_for('candidate_dashboard'))

    now=datetime.now(timezone.utc); exp=now+timedelta(seconds=int(b.get('duration_seconds',3600)))
    attempt=Attempt(candidate=c['candidate_name'],exam_id=paper['exam_id'],
                    bank_id=paper['bank_id'],started_at=now.isoformat(),
                    expires_at=exp.isoformat(),status='active',candidate_id=c['id'])
    db.session.add(attempt); db.session.flush()
    # Freeze the served questions so later bank edits cannot alter this attempt.
    chosen=random.sample(list(b['questions']),serve_count)
    for order,q in enumerate(chosen,1):
        db.session.add(AttemptQuestion(
            attempt_id=attempt.id,question_id=int(q['id']),question_order=order,
            question_text=str(q.get('text','')),
            options_json=json.dumps(q.get('options',[]),ensure_ascii=False),
            instruction=q.get('instruction'),image_path=q.get('image_path'),
            correct_option=int(q.get('answer')) if q.get('answer') is not None else -1,
            points=float(marks)))
    db.session.commit(); session['attempt_id']=attempt.id
    return redirect(url_for('exam',q=1))

@app.route('/exam')
def exam():
    a=get_attempt(session.get('attempt_id'))
    if not a: return redirect(url_for('candidate_dashboard') if session.get('candidate_id') else url_for('login'))
    if session.get('candidate_id') and a['candidate_id'] != session.get('candidate_id'): abort(403)
    if a['status']!='active': return redirect(url_for('result'))
    if remaining(a)<=0: grade(a['id'],auto=True); return redirect(url_for('result'))
    rows=db.session.scalars(select(AttemptQuestion)
        .where(AttemptQuestion.attempt_id==a['id'])
        .order_by(AttemptQuestion.question_order)).all()
    slot_row=one(select(CandidatePaper.slot)
        .where(CandidatePaper.candidate_id==a['candidate_id'],
               CandidatePaper.bank_id==a['bank_id']).limit(1)) if a['candidate_id'] else None
    saved=_answers_for_attempt(a['id'])
    if not rows: abort(409, description='This examination attempt has no frozen question set.')
    exam_questions=[]
    for r in rows:
        exam_questions.append({'id':r['question_id'],'text':r['question_text'],'options':json.loads(r['options_json'] or '[]'),
                               'instruction':r['instruction'],'image_path':r['image_path']})
    paper_label=entrance_paper_label(slot_row['slot'],a['bank_id'],'') if slot_row else 'Examination'
    try: requested_q=int(request.args.get('q',1))
    except (TypeError,ValueError): requested_q=1
    n=max(1,min(len(exam_questions),requested_q))
    response=Response(render_template('exam.html',candidate=a['candidate'],exam={'questions':exam_questions},current=n,saved=saved,remaining=remaining(a),paper_label=paper_label,csrf_token_value=csrf_token()))
    response.headers['Cache-Control']='no-store, no-cache, must-revalidate, max-age=0'; response.headers['Pragma']='no-cache'; return response

@app.post('/answer')
@csrf_protect
def answer():
    a=get_attempt(session.get('attempt_id'))
    if session.get('candidate_id') and (not a or a['candidate_id'] != session.get('candidate_id')): return jsonify(ok=False,error='Invalid candidate session.'),403
    if not a or a['status']!='active': return jsonify(ok=False,error='Session is no longer active.'),403
    if remaining(a)<=0: grade(a['id'],auto=True); return jsonify(ok=False,expired=True),410
    qid=int(request.form.get('question_id',0)); opt=int(request.form.get('option_index',-1))
    q=db.session.scalars(select(AttemptQuestion).where(
        AttemptQuestion.attempt_id==a['id'],AttemptQuestion.question_id==qid)).first()
    if not q: return jsonify(ok=False,error='Invalid question.'),400
    options=json.loads(q['options_json'] or '[]')
    if opt<0 or opt>=len(options): return jsonify(ok=False,error='Invalid answer.'),400
    answer_stmt=sqlite_insert(Answer).values(attempt_id=a['id'],question_id=qid,option_index=opt,
                                             answered_at=datetime.now(timezone.utc).isoformat())
    db.session.execute(answer_stmt.on_conflict_do_update(
        index_elements=['attempt_id','question_id'],
        set_={'option_index':answer_stmt.excluded.option_index,
              'answered_at':answer_stmt.excluded.answered_at}))
    db.session.commit(); return jsonify(ok=True)

@app.post('/submit')
@csrf_protect
def submit():
    a=get_attempt(session.get('attempt_id'))
    if not a: return redirect(url_for('login'))
    if session.get('candidate_id') and a['candidate_id'] != session.get('candidate_id'): abort(403)
    grade(a['id'],auto=remaining(a)<=0)
    return redirect(url_for('candidate_dashboard') if session.get('candidate_id') else url_for('result'))

@app.route('/result')
def result():
    a=get_attempt(session.get('attempt_id'))
    if not a: return redirect(url_for('candidate_dashboard') if session.get('candidate_id') else url_for('login'))
    if session.get('candidate_id') and a['candidate_id'] != session.get('candidate_id'): abort(403)
    if a['status']=='active' and remaining(a)<=0: a=grade(a['id'],auto=True)
    if a['status']=='active': return redirect(url_for('exam'))
    if session.get('candidate_id'): return redirect(url_for('candidate_dashboard'))
    response=Response(render_template('result.html'))
    response.headers['Cache-Control']='no-store, no-cache, must-revalidate, max-age=0'; response.headers['Pragma']='no-cache'; return response

@app.route('/review')
def review(): return redirect(url_for('candidate_dashboard') if session.get('candidate_id') else url_for('result'))

@app.route('/reset')
def reset(): session.clear(); return redirect(url_for('login'))

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

@app.route('/admin/examination')
@admin_required
def admin_dashboard():
    banks=load_banks(); sync_examinations()
    stats={
        'banks':len(banks),
        'questions':sum(len(b.get('questions',[])) for b in banks.values()),
        'attempts':one_scalar(select(func.count()).select_from(Attempt),0),
        'completed':one_scalar(select(func.count()).select_from(Attempt)
                               .where(Attempt.status.in_(('submitted','expired'))),0),
        'candidates':one_scalar(select(func.count()).select_from(Candidate)
                                .where(Candidate.active==1),0)
    }
    recent=db.session.scalars(select(Attempt).order_by(Attempt.id.desc()).limit(12)).all()
    current_session=_school_current_session()
    states={bank_id:active for bank_id,active in tuples(
        select(EntranceBankConfig.bank_id,func.max(EntranceBankConfig.active).label('active'))
        .where(EntranceBankConfig.session_id==(current_session['id'] if current_session else -1))
        .group_by(EntranceBankConfig.bank_id))}
    online_counts,_=online_presence()

    summaries=_candidate_results_summary()
    subject_labels=[('mathematics','Mathematics'),('english','English'),('general_knowledge','General Knowledge')]
    subject_analytics=[]
    for key,label in subject_labels:
        attempted=[r[key] for r in summaries if r[key]['percentage'] is not None]
        passed=[d for d in attempted if d['percentage'] >= 50]
        average=(sum(d['percentage'] for d in attempted)/len(attempted)) if attempted else 0
        subject_analytics.append({
            'key':key,'label':label,'attempted':len(attempted),'average':average,
            'passed':len(passed),'pass_rate':(len(passed)/len(attempted)*100) if attempted else 0
        })

    class_analytics=[]
    for key,label in (('year7','JSS 1'),('year10','SSS 1')):
        group=[r for r in summaries if normalize_entry_group(r['target_class'])==key]
        complete=[r for r in group if r['complete']]
        percentages=[r['overall_percentage'] for r in complete]
        class_analytics.append({
            'key':key,'label':label,'candidates':len(group),'completed':len(complete),
            'average':(sum(percentages)/len(percentages)) if percentages else None
        })

    top_candidates=sorted([r for r in summaries if r['complete']], key=lambda r:(r['overall_percentage'],r['total_score']), reverse=True)[:5]
    for i,r in enumerate(top_candidates,1): r['rank']=i
    return render_template('admin_dashboard.html',banks=banks.values(),stats=stats,recent=recent,exam_states=states,subject_analytics=subject_analytics,class_analytics=class_analytics,top_candidates=top_candidates,online_counts=online_counts)

@app.route('/admin/school')
@admin_required
def admin_school_home():
    def count(model, *where):
        return one_scalar(select(func.count()).select_from(model).where(*where), 0)
    stats={
        'students':count(Student, Student.active==1),
        'classes':count(SchoolClass, SchoolClass.active==1),
        'subjects':count(SchoolSubject, SchoolSubject.active==1),
        'assignments':count(SchoolAssignment, SchoolAssignment.active==1),
        'projects':count(SchoolProject, SchoolProject.active==1),
        'tests':count(SchoolAssessment, SchoolAssessment.assessment_type=='test'),
        'examinations':count(SchoolAssessment, SchoolAssessment.assessment_type=='examination'),
    }
    return render_template('admin_school_home.html',stats=stats)

@app.route('/admin/practice-tests')
@admin_required
def admin_practice_tests():
    me=current_admin()
    if not admin_has_permission(me['id'],'entrance.config.view'):
        return admin_access_error('entrance.config.view')
    current=_school_current_session()
    configs=[_flatten(r,'EntranceBankConfig',*ENTRANCE_CONFIG_EXTRA) for r in all_rows(
        _entrance_config_select().where(EntranceBankConfig.practice_enabled==1)
        .order_by(AcademicSession.id.desc(),EntranceBankConfig.entry_group,
                  EntranceBankConfig.subject))]
    return render_template('admin_entrance_practice.html',configs=configs,current_session=current)

def _school_class_allowed(admin_id,class_id):
    admin=current_admin()
    if admin and admin['admin_type_system']: return True
    name=one_scalar(select(SchoolClass.name).where(SchoolClass.id==class_id))
    return bool(name and admin_scope_allows(admin_id,'class',name))

def _school_subject_allowed(admin_id,subject_id):
    admin=current_admin()
    if admin and admin['admin_type_system']: return True
    name=one_scalar(select(SchoolSubject.name).where(SchoolSubject.id==subject_id))
    any_subject_scope=one(select(AdminScope.id).where(
        AdminScope.admin_id==admin_id,AdminScope.scope_type=='subject').limit(1))
    # Primary class teachers normally receive class scope and manage all subjects
    # offered by that class. College subject teachers can additionally receive a
    # subject scope, which restricts them to their own subject.
    return bool(name and (not any_subject_scope or admin_scope_allows(admin_id,'subject',name)))

def _school_pair_allowed(admin_id,class_id,subject_id):
    return _school_class_allowed(admin_id,class_id) and _school_subject_allowed(admin_id,subject_id)

def _school_form_context():
    classes=db.session.scalars(select(SchoolClass).where(SchoolClass.active==1)
                               .order_by(SchoolClass.level_order)).all()
    subjects=db.session.scalars(select(SchoolSubject).where(SchoolSubject.active==1)
                                .order_by(SchoolSubject.name)).all()
    session_row=_school_current_session()
    students=_students_with_class(session_row['id'] if session_row else 0)
    admin=current_admin()
    if admin and not admin['admin_type_system']:
        classes=[x for x in classes if admin_scope_allows(admin['id'],'class',x['name'])]
        subjects=[x for x in subjects if admin_scope_allows(admin['id'],'subject',x['name'])]
        students=[x for x in students if x['class_name'] and admin_scope_allows(admin['id'],'class',x['class_name'])]
    return classes,subjects,students,session_row

def _school_student_visible(sid):
    admin=current_admin()
    if admin and admin['admin_type_system']: return True
    class_name=one_scalar(select(SchoolClass.name)
        .select_from(StudentEnrolment)
        .join(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
        .where(StudentEnrolment.student_id==sid,StudentEnrolment.active==1)
        .order_by(StudentEnrolment.id.desc()).limit(1))
    return bool(class_name and admin_scope_allows(admin['id'],'class',class_name))

@app.route('/admin/school/students')
@admin_required
def admin_school_students():
    selected_class=request.args.get('class','').strip()
    search=request.args.get('q','').strip()
    session_row=_school_current_session()
    # Keep Flask's global `session` object reserved for authentication/workspace
    # state.  Normalize database rows at the template boundary so presentation
    # code can safely use dictionary helpers such as `.get()`.
    school_session=dict(session_row) if session_row else None
    classes=db.session.scalars(select(SchoolClass).where(SchoolClass.active==1)
                               .order_by(SchoolClass.level_order)).all()
    rows=_students_with_class(school_session['id'] if school_session else 0,
                              include_session_id=True)
    admin=current_admin()
    if admin and not admin['admin_type_system']:
        classes=[r for r in classes if admin_scope_allows(admin['id'],'class',r['name'])]
        rows=[r for r in rows if r['class_name'] and admin_scope_allows(admin['id'],'class',r['class_name'])]
    class_cards=[]
    for cls in classes:
        class_rows=[r for r in rows if r['class_name']==cls['name']]
        class_cards.append({'name':cls['name'],'count':len(class_rows),'id':cls['id']})
    if selected_class:
        rows=[r for r in rows if r['class_name']==selected_class]
        if search:
            needle=search.casefold(); rows=[r for r in rows if needle in f"{r['first_name']} {r['middle_name'] or ''} {r['last_name']} {r['admission_no']} {r['guardian_name'] or ''} {r['guardian_email'] or ''}".casefold()]
    return render_template('school_students.html',students=rows,school_session=school_session,class_cards=class_cards,selected_class=selected_class,search=search)

@app.route('/admin/school/students/<int:sid>')
@admin_required
def admin_school_student_detail(sid):
    student=obj(Student,sid)
    if not student: abort(404)
    session_row=_school_current_session()
    enrol=[_flatten(r,'StudentEnrolment','class_name','session_name') for r in all_rows(
        select(StudentEnrolment,SchoolClass.name.label('class_name'),
               AcademicSession.name.label('session_name'))
        .join(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
        .join(AcademicSession,AcademicSession.id==StudentEnrolment.session_id)
        .where(StudentEnrolment.student_id==sid)
        .order_by(StudentEnrolment.id.desc()))]
    current_enrol=next((r for r in enrol if session_row and r['session_id']==session_row['id'] and r['active']), enrol[0] if enrol else None)
    history=[_flatten(r,'StudentEnrollmentHistory','session_name','class_name') for r in all_rows(
        select(StudentEnrollmentHistory,AcademicSession.name.label('session_name'),
               SchoolClass.name.label('class_name'))
        .outerjoin(AcademicSession,AcademicSession.id==StudentEnrollmentHistory.session_id)
        .outerjoin(SchoolClass,SchoolClass.id==StudentEnrollmentHistory.class_id)
        .where(StudentEnrollmentHistory.student_id==sid)
        .order_by(func.coalesce(StudentEnrollmentHistory.enrolled_at,'').desc(),
                  StudentEnrollmentHistory.id.desc()))]
    sessions=db.session.scalars(select(AcademicSession).where(AcademicSession.active==1)
        .order_by(AcademicSession.id.desc())).all()
    classes_for_history=db.session.scalars(select(SchoolClass).where(SchoolClass.active==1)
        .order_by(SchoolClass.level_order)).all()
    if current_enrol and not _school_class_allowed(current_admin()['id'],current_enrol['class_id']): return admin_access_error('school.students.view')
    results=[_flatten(r,'SchoolStudentResult','subject_name','assessment_title','class_name')
             for r in all_rows(
        select(SchoolStudentResult,SchoolSubject.name.label('subject_name'),
               SchoolAssessment.title.label('assessment_title'),
               SchoolClass.name.label('class_name'))
        .join(SchoolSubject,SchoolSubject.id==SchoolStudentResult.subject_id)
        .outerjoin(SchoolAssessment,SchoolAssessment.id==SchoolStudentResult.assessment_id)
        .outerjoin(StudentEnrolment,and_(StudentEnrolment.student_id==SchoolStudentResult.student_id,
                                         StudentEnrolment.session_id==SchoolStudentResult.session_id,
                                         StudentEnrolment.active==1))
        .outerjoin(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
        .where(SchoolStudentResult.student_id==sid)
        .order_by(SchoolStudentResult.id.desc()))]
    assignments=[_flatten(r,'SchoolAssignment','subject_name','class_name','assignment_status',
                          'assignment_score','assignment_max_score','assignment_remark')
                 for r in all_rows(
        select(SchoolAssignment,SchoolSubject.name.label('subject_name'),
               SchoolClass.name.label('class_name'),
               AssignmentStudent.status.label('assignment_status'),
               AssignmentStudent.score.label('assignment_score'),
               AssignmentStudent.max_score.label('assignment_max_score'),
               AssignmentStudent.remark.label('assignment_remark'))
        .select_from(AssignmentStudent)
        .join(SchoolAssignment,SchoolAssignment.id==AssignmentStudent.assignment_id)
        .join(SchoolSubject,SchoolSubject.id==SchoolAssignment.subject_id)
        .join(SchoolClass,SchoolClass.id==SchoolAssignment.class_id)
        .where(AssignmentStudent.student_id==sid)
        .order_by(SchoolAssignment.date_given.desc(),SchoolAssignment.id.desc()))]
    projects=[_flatten(r,'SchoolProject','subject_name','class_name','project_status',
                       'project_score','project_remark') for r in all_rows(
        select(SchoolProject,SchoolSubject.name.label('subject_name'),
               SchoolClass.name.label('class_name'),
               ProjectStudent.status.label('project_status'),
               ProjectStudent.score.label('project_score'),
               ProjectStudent.remark.label('project_remark'))
        .select_from(ProjectStudent)
        .join(SchoolProject,SchoolProject.id==ProjectStudent.project_id)
        .join(SchoolSubject,SchoolSubject.id==SchoolProject.subject_id)
        .join(SchoolClass,SchoolClass.id==SchoolProject.class_id)
        .where(ProjectStudent.student_id==sid)
        .order_by(SchoolProject.date_given.desc(),SchoolProject.id.desc()))]
    completed=[r for r in results if r['score'] is not None and r['max_score']]
    total_score=sum(float(r['score']) for r in completed); total_max=sum(float(r['max_score']) for r in completed); pct=(total_score/total_max*100) if total_max else 0
    assignment_graded=[r for r in assignments if r['assignment_score'] is not None and r['assignment_max_score']]
    assignment_avg=(sum(float(r['assignment_score'])/float(r['assignment_max_score'])*100 for r in assignment_graded)/len(assignment_graded)) if assignment_graded else None
    term_reports=[{**period,'subjects':_student_term_subjects(sid,period['session_id'],period['term'])}
                  for period in _student_term_periods(sid)]
    term_reports=[t for t in term_reports if t['subjects']]
    return render_template('admin_school_student_detail.html',student=student,enrolments=enrol,enrollment_history=history,sessions=sessions,classes_for_history=classes_for_history,current_enrol=current_enrol,results=results,assignments=assignments,projects=projects,total_score=total_score,total_max=total_max,pct=pct,completed=len(completed),assignment_avg=assignment_avg,term_reports=term_reports)

@app.route('/admin/school/students/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_student_new():
    classes,subjects,students,session_row=_school_form_context()

    def back_with(errors):
        return render_template(
            'school_student_form.html',
            mode='new',
            student=request.form,
            classes=classes,
            errors=errors,
            nigerian_states=NIGERIAN_STATES,
            school_session=dict(session_row) if session_row else None
        )

    if request.method=='POST':
        first=request.form.get('first_name','').strip()
        middle=request.form.get('middle_name','').strip()
        last=request.form.get('last_name','').strip()
        gender=request.form.get('gender','').strip()
        dob=request.form.get('date_of_birth','').strip()
        state_of_origin=request.form.get('state_of_origin','').strip()
        blood_group=request.form.get('blood_group','').strip()
        genotype=request.form.get('genotype','').strip()
        guardian=request.form.get('guardian_name','').strip()
        phone=request.form.get('guardian_phone','').strip()
        email=request.form.get('guardian_email','').strip()
        photo_path=None

        try:
            class_id=int(request.form.get('class_id',''))
        except (TypeError,ValueError):
            class_id=0

        errors=[]

        if email and ('@' not in email or '.' not in email.split('@')[-1]):
            errors.append('Enter a valid guardian email address.')

        if state_of_origin not in NIGERIAN_STATES:
            errors.append('Select a valid state of origin.')

        try:
            photo_path=_save_image_upload(
                request.files.get('photo'),
                'students',
                f'student_{first}_{last}'.replace(' ','_').lower()
            )
        except ValueError as exc:
            errors.append(str(exc))

        if not first or not last:
            errors.append('First name and surname are required.')

        if not class_id or not _school_class_allowed(current_admin()['id'],class_id):
            errors.append('Select a class within your authorised school scope.')

        if errors:
            return back_with(errors)

        sid=None
        school_id=None
        login_username=None
        temporary_password=None
        generated_number=None

        # The whole registration is one transaction: identity allocation,
        # student record, account, enrolment, admission profile, parent
        # accounts and contacts either all commit or none do.
        try:
            active_schools=[row_id for (row_id,) in tuples(
                select(School.id).where(School.active==1).order_by(School.id))]

            if len(active_schools)!=1:
                raise StudentNumberAllocationError(
                    'Student registration requires exactly one active school configuration.'
                )

            school_id=int(active_schools[0])

            # Allocate first; the generator shares this transaction.
            allocation=allocate_student_number(
                school_id=school_id,
                student_id=None,
                allocation_year=None,
                allocated_by=str(current_admin()['id'])
            )

            generated_number=allocation['student_number']

            # The generated number is the authoritative student identity.
            # admission_no stays populated for legacy compatibility.
            student=Student(
                admission_no=generated_number,
                first_name=first,
                middle_name=middle,
                last_name=last,
                gender=gender,
                date_of_birth=dob,
                state_of_origin=state_of_origin,
                blood_group=blood_group,
                genotype=genotype,
                guardian_name=guardian,
                guardian_phone=phone,
                guardian_email=email,
                photo_path=photo_path,
                created_at=datetime.now(timezone.utc).isoformat(),
                active=1,
                school_id=school_id,
                student_number=generated_number,
                student_number_source='generated'
            )
            db.session.add(student)
            db.session.flush()
            sid=student.id

            # Bind the allocation ledger row to the new student record.
            linked=db.session.execute(sa_update(StudentNumberAllocation)
                .where(StudentNumberAllocation.school_id==school_id,
                       StudentNumberAllocation.student_number==generated_number,
                       StudentNumberAllocation.student_id.is_(None),
                       StudentNumberAllocation.source=='generated')
                .values(student_id=sid)).rowcount

            if linked!=1:
                raise StudentNumberAllocationError(
                    'Generated student number could not be safely linked to the student record.'
                )

            login_username,temporary_password=_provision_student_account(sid,generated_number)

            db.session.add(StudentEnrolment(
                student_id=sid,
                class_id=class_id,
                session_id=session_row['id'],
                enrolled_at=datetime.now(timezone.utc).isoformat(),
                active=1
            ))

            now_admission=datetime.now(timezone.utc).isoformat()
            admin_id=int(current_admin()['id'])

            def _admission_value(name):
                return request.form.get(name,'').strip()

            db.session.add(StudentAdmissionProfile(
                student_id=sid,
                previous_school=_admission_value('previous_school') or None,
                reason_for_leaving=_admission_value('reason_for_leaving') or None,
                religion=_admission_value('religion') or None,
                denomination=_admission_value('denomination') or None,
                blood_group=_admission_value('blood_group') or None,
                genotype=_admission_value('genotype') or None,
                convulsion_history=_admission_value('convulsion_history') or None,
                asthma_history=_admission_value('asthma_history') or None,
                medical_frequency=_admission_value('medical_frequency') or None,
                medical_treatment=_admission_value('medical_treatment') or None,
                immunization=_admission_value('immunization') or None,
                food_allergies=_admission_value('food_allergies') or None,
                drug_allergies=_admission_value('drug_allergies') or None,
                other_health_challenges=_admission_value('other_health_challenges') or None,
                disability=_admission_value('disability') or None,
                disability_indication=_admission_value('disability_indication') or None,
                parent_signature=_admission_value('parent_signature') or None,
                parent_signature_date=_admission_value('parent_signature_date') or None,
                updated_at=now_admission,
                updated_by=admin_id
            ))

            father_name=_admission_value('father_guardian_name')
            father_address=_admission_value('father_guardian_address')
            father_office=_admission_value('father_guardian_office_phone')
            father_mobile=_admission_value('father_guardian_mobile')
            father_email=_admission_value('father_guardian_email').lower()

            mother_name=_admission_value('mother_name')
            mother_address=_admission_value('mother_address')
            mother_office=_admission_value('mother_office_phone')
            mother_email=_admission_value('mother_email').lower()
            mother_occupation=_admission_value('mother_occupation')

            def _digits(value):
                return ''.join(ch for ch in str(value or '') if ch.isdigit())

            def _find_parent(mobile,email_value):
                """Match an existing parent conservatively.

                Normalised mobile first, then normalised email. A name alone is
                NEVER sufficient to reuse an account.
                """
                mobile_norm=_digits(mobile)
                email_norm=(email_value or '').strip().lower()
                if mobile_norm:
                    for row in db.session.scalars(select(ParentAccount)
                            .where(ParentAccount.phone.is_not(None))).all():
                        if _digits(row.phone)==mobile_norm:
                            return row
                if email_norm:
                    return db.session.scalars(select(ParentAccount)
                        .where(func.lower(ParentAccount.email)==email_norm)).first()
                return None

            def _ensure_parent(name,mobile,email_value,relationship):
                if not name and not mobile and not email_value:
                    return None
                parent=_find_parent(mobile,email_value)
                if parent is not None:
                    pid=int(parent.id)
                else:
                    display=name or 'Parent / Guardian'
                    base=re.sub(r'[^a-z0-9]+','.',display.lower()).strip('.') or 'parent'
                    username=base
                    suffix=1
                    while one_scalar(select(ParentAccount.id)
                                     .where(ParentAccount.username==username)):
                        username=f"{base}.{suffix}"
                        suffix+=1
                    password=_new_parent_password()
                    created=ParentAccount(
                        username=username,
                        display_name=display,
                        email=email_value or None,
                        phone=mobile or None,
                        password_hash=generate_password_hash(password),
                        active=1,
                        password_must_change=1,
                        created_at=now_admission
                    )
                    db.session.add(created)
                    db.session.flush()
                    pid=int(created.id)
                    audit_log(
                        'parent_account_created',
                        'school',
                        'parent',
                        pid,
                        {'created_from_student_admission':sid,'relationship':relationship}
                    )
                existing_link=db.session.scalars(select(ParentStudentLink).where(
                    ParentStudentLink.parent_id==pid,
                    ParentStudentLink.student_id==sid)).first()
                if existing_link:
                    existing_link.relationship=relationship or None
                    existing_link.active=1
                    existing_link.created_by=admin_id
                else:
                    db.session.add(ParentStudentLink(
                        parent_id=pid,student_id=sid,relationship=relationship or None,
                        active=1,created_at=now_admission,created_by=admin_id))
                return pid

            father_parent_id=_ensure_parent(
                father_name,father_mobile,father_email,'father_guardian')

            # The official admission form has no mother mobile field, so
            # email-only matching is permitted for the mother record.
            mother_parent_id=_ensure_parent(
                mother_name,'',mother_email,'mother')

            contacts_to_save=[
                ('father_guardian',father_parent_id,father_name,father_address,
                 father_office,father_mobile,father_email,None),
                ('mother',mother_parent_id,mother_name,mother_address,
                 mother_office,'',mother_email,mother_occupation)
            ]

            for contact_role,parent_id,contact_name,address,office_phone,mobile,contact_email,occupation in contacts_to_save:
                if not any([parent_id,contact_name,address,office_phone,mobile,contact_email,occupation]):
                    continue
                stmt=sqlite_insert(StudentAdmissionContact).values(
                    student_id=sid,parent_id=parent_id,role=contact_role,
                    name=contact_name or None,address=address or None,
                    office_phone=office_phone or None,mobile=mobile or None,
                    email=contact_email or None,occupation=occupation or None,
                    created_at=now_admission,updated_at=now_admission,
                    updated_by=admin_id)
                db.session.execute(stmt.on_conflict_do_update(
                    index_elements=['student_id','role'],
                    set_={'parent_id':stmt.excluded.parent_id,
                          'name':stmt.excluded.name,
                          'address':stmt.excluded.address,
                          'office_phone':stmt.excluded.office_phone,
                          'mobile':stmt.excluded.mobile,
                          'email':stmt.excluded.email,
                          'occupation':stmt.excluded.occupation,
                          'updated_at':stmt.excluded.updated_at,
                          'updated_by':stmt.excluded.updated_by}))

            db.session.commit()

        except StudentNumberAllocationError as exc:
            db.session.rollback()
            return back_with([str(exc)])

        except sa.exc.IntegrityError:
            db.session.rollback()
            return back_with([
                'Student registration could not be completed because '
                'the generated student identity conflicted with existing data. '
                'No student record was created.'
            ])

        except Exception:
            db.session.rollback()
            raise

        audit_log(
            'school_student_created',
            'school',
            'student',
            sid,
            {
                'student_number':generated_number,
                'admission_no':generated_number,
                'school_id':school_id,
                'class_id':class_id,
                'account_created':True,
                'student_number_source':'generated'
            }
        )

        return render_template(
            'student_credentials.html',
            student={
                'id':sid,
                'first_name':first,
                'last_name':last,
                'admission_no':generated_number,
                'student_number':generated_number
            },
            login_username=login_username,
            temporary_password=temporary_password
        )

    return render_template(
        'school_student_form.html',
        mode='new',
        student=None,
        classes=classes,
        errors=[],
        nigerian_states=NIGERIAN_STATES,
        school_session=dict(session_row) if session_row else None
    )

@app.route('/admin/school/students/<int:sid>/edit',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_student_edit(sid):
    student=obj(Student,sid)
    if not student: abort(404)
    session_row=_school_current_session()
    enrol=db.session.scalars(select(StudentEnrolment).where(
        StudentEnrolment.student_id==sid,
        StudentEnrolment.session_id==(session_row['id'] if session_row else 0),
        StudentEnrolment.active==1)).first()
    if enrol and not _school_class_allowed(current_admin()['id'],enrol.class_id): return admin_access_error('school.students.edit')
    classes,_,_,_= _school_form_context()
    student_dict={c.key:getattr(student,c.key) for c in student.__mapper__.column_attrs}
    session_dict=dict(session_row) if session_row else None
    if request.method=='POST':
        admission=request.form.get('admission_no','').strip(); first=request.form.get('first_name','').strip(); middle=request.form.get('middle_name','').strip(); last=request.form.get('last_name','').strip(); gender=request.form.get('gender','').strip(); dob=request.form.get('date_of_birth','').strip(); blood_group=request.form.get('blood_group','').strip(); genotype=request.form.get('genotype','').strip(); guardian=request.form.get('guardian_name','').strip(); phone=request.form.get('guardian_phone','').strip(); email=request.form.get('guardian_email','').strip(); photo_path=student.photo_path
        try: class_id=int(request.form.get('class_id',''))
        except (TypeError,ValueError): class_id=0
        errors=[]
        if email and ('@' not in email or '.' not in email.split('@')[-1]): errors.append('Enter a valid guardian email address.')
        try:
            new_photo_path=_save_image_upload(request.files.get('photo'),'students',f'student_{first}_{last}'.replace(' ','_').lower())
            if new_photo_path: photo_path=new_photo_path
        except ValueError as exc:
            errors.append(str(exc))
        if not admission or not first or not last: errors.append('Admission number, first name and surname are required.')
        if not class_id or not _school_class_allowed(current_admin()['id'],class_id): errors.append('Select a class within your authorised school scope.')
        if errors: return render_template('school_student_form.html',mode='edit',student={**student_dict,**request.form},classes=classes,errors=errors,school_session=session_dict)
        try:
            student.admission_no=admission; student.first_name=first; student.middle_name=middle
            student.last_name=last; student.gender=gender; student.date_of_birth=dob
            student.blood_group=blood_group; student.genotype=genotype
            student.guardian_name=guardian; student.guardian_phone=phone
            student.guardian_email=email; student.photo_path=photo_path
            now=datetime.now(timezone.utc).isoformat()
            if enrol:
                enrol.class_id=class_id
            elif session_row:
                db.session.add(StudentEnrolment(student_id=sid,class_id=class_id,
                    session_id=session_row['id'],enrolled_at=now,active=1))
            db.session.commit()
        except sa.exc.IntegrityError:
            db.session.rollback()
            return render_template('school_student_form.html',mode='edit',student={**student_dict,**request.form},classes=classes,errors=['Admission number already exists.'],school_session=session_dict)
        audit_log('school_student_updated','school','student',sid,{'class_id':class_id}); flash('Student record updated.','success'); return redirect(url_for('admin_school_students'))
    data=dict(student_dict); data['class_id']=enrol.class_id if enrol else ''
    return render_template('school_student_form.html',mode='edit',student=data,classes=classes,errors=[],school_session=session_dict)

@app.route('/admin/school/students/<int:sid>/account',methods=['POST'])
@admin_required
@csrf_protect
def admin_school_student_account_reset(sid):
    if not _school_student_visible(sid): return admin_access_error('school.students.edit')
    student=obj(Student, sid)
    if not student: abort(404)
    login_username,temporary_password=_provision_student_account(sid,student['admission_no'])
    db.session.commit()
    audit_log('school_student_account_reset','school','student',sid,{'login_username':login_username})
    return render_template('student_credentials.html',student=dict(student),login_username=login_username,temporary_password=temporary_password,reset=True)

@app.post('/admin/school/students/<int:sid>/account/toggle')
@admin_required
@csrf_protect
def admin_school_student_account_toggle(sid):
    if not _school_student_visible(sid): return admin_access_error('school.students.edit')
    row=one(select(Student.id,Student.account_active).where(Student.id==sid))
    if not row: abort(404)
    new=0 if row['account_active'] else 1
    db.session.execute(sa_update(Student).where(Student.id==sid).values(account_active=new))
    db.session.commit()
    audit_log('school_student_account_status_changed','school','student',sid,{'account_active':new})
    flash('Student login account '+('activated.' if new else 'disabled.'),'success')
    return redirect(url_for('admin_school_student_detail',sid=sid))

@app.route('/admin/school/students/<int:sid>/account/print')
@admin_required
def admin_school_student_account_print(sid):
    if not _school_student_visible(sid): return admin_access_error('school.students.view')
    student=obj(Student, sid)
    if not student: abort(404)
    return render_template('student_credentials_print.html',student=student)

@app.post('/admin/school/students/<int:sid>/toggle')
@admin_required
@csrf_protect
def admin_school_student_toggle(sid):
    row=obj(Student, sid)
    if not row: abort(404)
    if not _school_student_visible(sid): return admin_access_error('school.students.delete')
    new=0 if row['active'] else 1
    db.session.execute(sa_update(Student).where(Student.id==sid).values(active=new))
    db.session.commit(); audit_log('school_student_status_changed','school','student',sid,{'active':new}); flash('Student record '+('activated.' if new else 'deactivated.'),'success'); return redirect(url_for('admin_school_students'))

@app.route('/admin/school/classes')
@admin_required
def admin_school_classes():
    rows=db.session.scalars(select(SchoolClass)
        .order_by(SchoolClass.level_order)).all()
    counts={class_id:n for class_id,n in tuples(
        select(StudentEnrolment.class_id,func.count().label('n'))
        .join(Student,Student.id==StudentEnrolment.student_id)
        .where(StudentEnrolment.active==1,Student.active==1)
        .group_by(StudentEnrolment.class_id))}
    admin=current_admin()
    if admin and not admin['admin_type_system']:
        rows=[r for r in rows if admin_scope_allows(admin['id'],'class',r.name)]
    return render_template('school_classes.html',classes=rows,counts=counts)

@app.route('/admin/school/classes/<int:class_id>/edit',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_class_edit(class_id):
    if not is_super_admin(): return admin_access_error('Super Admin control')
    row=obj(SchoolClass,class_id)
    if not row: abort(404)
    errors=[]
    if request.method=='POST':
        name=request.form.get('name','').strip(); stage=request.form.get('stage','').strip()
        optional=1 if request.form.get('optional')=='1' else 0
        try: level_order=int(request.form.get('level_order','0'))
        except (TypeError,ValueError): level_order=0
        if not name: errors.append('Class name is required.')
        if not stage: errors.append('Stage/category is required.')
        if not errors:
            duplicate=one_scalar(select(SchoolClass.id).where(
                SchoolClass.name==name,SchoolClass.id!=class_id))
            if duplicate: errors.append('Another class already uses that name.')
            if not errors:
                old={'name':row.name,'stage':row.stage,'level_order':row.level_order}
                row.name=name; row.stage=stage; row.level_order=level_order; row.optional=optional
                db.session.commit()
                audit_log('school_class_updated','school','class',class_id,{'old':old,'new':{'name':name,'stage':stage,'level_order':level_order}})
                flash('Class details updated. Existing historical enrolments keep their class record; new entries use the updated class name.','success')
                return redirect(url_for('admin_school_classes'))
    form={c.key:getattr(row,c.key) for c in row.__mapper__.column_attrs}
    if request.method=='POST': form.update(request.form)
    return render_template('school_class_form.html',class_row=form,errors=errors)

@app.post('/admin/school/classes/<int:class_id>/toggle')
@admin_required
@csrf_protect
def admin_school_class_toggle(class_id):
    if not is_super_admin(): return admin_access_error('Super Admin control')
    row=obj(SchoolClass, class_id)
    if not row: abort(404)
    new=0 if row['active'] else 1
    db.session.execute(sa_update(SchoolClass).where(SchoolClass.id==class_id).values(active=new))
    db.session.commit(); audit_log('school_class_status_changed','school','class',class_id,{'active':new}); flash(f'{row["name"]} '+('activated.' if new else 'deactivated.'),'success'); return redirect(url_for('admin_school_classes'))

@app.route('/admin/school/subjects')
@admin_required
def admin_school_subjects():
    rows=all_rows(
        select(SchoolSubject.id,SchoolSubject.name,SchoolSubject.code,SchoolSubject.active,
               func.group_concat(SchoolClass.name,', ').label('classes'),
               func.sum(sa.case((ClassSubject.locked==1,1),else_=0)).label('locked_count'),
               func.sum(sa.case((ClassSubject.final_locked==1,1),else_=0)).label('final_locked_count'))
        .select_from(SchoolSubject)
        .outerjoin(ClassSubject,ClassSubject.subject_id==SchoolSubject.id)
        .outerjoin(SchoolClass,SchoolClass.id==ClassSubject.class_id)
        .where(SchoolSubject.active==1)
        .group_by(SchoolSubject.id).order_by(SchoolSubject.name))
    classes=db.session.scalars(select(SchoolClass).where(SchoolClass.active==1)
        .order_by(SchoolClass.level_order)).all()
    admin=current_admin()
    if admin and not admin['admin_type_system']:
        rows=[r for r in rows if any(admin_scope_allows(admin['id'],'class',x.strip())
                                     for x in (r['classes'] or '').split(','))]
        classes=[c for c in classes if admin_scope_allows(admin['id'],'class',c.name)]
    return render_template('school_subjects.html',subjects=rows,classes=classes)

@app.route('/admin/school/subjects/quick-create',methods=['POST'])
@admin_required
@csrf_protect
def admin_school_subject_quick_create():
    me=current_admin(); name=request.form.get('name','').strip(); code=request.form.get('code','').strip()
    try: class_id=int(request.form.get('class_id',''))
    except (TypeError,ValueError): class_id=0
    if not name or not class_id or not _school_class_allowed(me['id'],class_id):
        return jsonify(ok=False,error='Enter a subject name and choose an authorised class.'),400
    now=datetime.now(timezone.utc).isoformat()
    subject=db.session.scalars(select(SchoolSubject)
        .where(func.lower(SchoolSubject.name)==name.lower()).limit(1)).first()
    if subject:
        link=db.session.scalars(select(ClassSubject).where(
            ClassSubject.subject_id==subject.id,ClassSubject.class_id==class_id)).first()
        if link and link.final_locked:
            return jsonify(ok=False,error='That subject is permanently locked for this class.'),409
        if link:
            return jsonify(ok=False,error='That subject already exists for the selected class.'),409
        # The subject exists but is not attached to this class: reactivate and link.
        subject.active=1
        db.session.add(ClassSubject(class_id=class_id,subject_id=subject.id,locked=0,
                                    final_locked=0,created_by=me['id'],created_at=now))
        sid=subject.id; created=False
        resolved_name=subject.name
    else:
        new_subject=SchoolSubject(name=name,code=code,created_at=now,active=1)
        db.session.add(new_subject); db.session.flush()
        sid=new_subject.id
        db.session.add(ClassSubject(class_id=class_id,subject_id=sid,locked=0,
                                    final_locked=0,created_by=me['id'],created_at=now))
        created=True
        resolved_name=name
    db.session.commit()
    audit_log('school_subject_created','school','subject',sid,{'class_id':class_id,'quick_create':True,'new_subject':created})
    return jsonify(ok=True,subject_id=sid,name=resolved_name,created=created)

@app.route('/admin/school/subjects/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_subject_new():
    classes,_,_,_= _school_form_context()
    if request.method=='POST':
        name=request.form.get('name','').strip(); code=request.form.get('code','').strip(); selected=[]
        for v in request.form.getlist('class_ids'):
            try: selected.append(int(v))
            except (TypeError,ValueError): pass
        errors=[]
        if not name: errors.append('Subject name is required.')
        if not selected: errors.append('Select at least one class.')
        if any(not _school_class_allowed(current_admin()['id'],cid) for cid in selected): errors.append('One or more selected classes are outside your authorised scope.')
        if errors: return render_template('school_subject_form.html',mode='new',subject=request.form,classes=classes,selected=selected,errors=errors)
        now=datetime.now(timezone.utc).isoformat()
        existing=db.session.scalars(select(SchoolSubject)
            .where(func.lower(SchoolSubject.name)==name.lower()).limit(1)).first()
        try:
            if existing:
                sid=existing.id
                existing.active=1
                if code: existing.code=code
                for cid in selected:
                    link=one(select(ClassSubject.locked,ClassSubject.final_locked)
                             .where(ClassSubject.class_id==cid,ClassSubject.subject_id==sid))
                    if link and link['final_locked']: raise ValueError('That subject is permanently locked for one of the selected classes.')
                    if link: raise ValueError('That subject already exists for one of the selected classes.')
                    db.session.add(ClassSubject(class_id=cid,subject_id=sid,locked=0,final_locked=0,
                                                created_by=current_admin()['id'],created_at=now))
            else:
                created=SchoolSubject(name=name,code=code,created_at=now,active=1)
                db.session.add(created); db.session.flush(); sid=created.id
                for cid in selected:
                    db.session.add(ClassSubject(class_id=cid,subject_id=sid,locked=0,final_locked=0,
                                                created_by=current_admin()['id'],created_at=now))
            db.session.commit()
        except (ValueError,sa.exc.IntegrityError) as exc:
            db.session.rollback()
            msg=str(exc) if isinstance(exc,ValueError) else 'The subject could not be created because the name is already in use.'
            return render_template('school_subject_form.html',mode='new',subject=request.form,classes=classes,selected=selected,errors=[msg])
        audit_log('school_subject_created','school','subject',sid,{'classes':selected,'existing_subject_reused':bool(existing)}); flash('Subject created/attached to the selected classes.','success'); return redirect(url_for('admin_school_subjects'))
    return render_template('school_subject_form.html',mode='new',subject=None,classes=classes,selected=[],errors=[])

@app.route('/admin/school/subjects/<int:subject_id>/edit',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_subject_edit(subject_id):
    subject=obj(SchoolSubject,subject_id)
    links=all_rows(select(ClassSubject.class_id,ClassSubject.locked,ClassSubject.final_locked,
                          ClassSubject.final_locked_by,ClassSubject.final_locked_at)
                   .where(ClassSubject.subject_id==subject_id))
    if not subject: abort(404)
    if not is_super_admin() and not any(_school_class_allowed(current_admin()['id'],x['class_id']) for x in links): return admin_access_error('school.subjects.edit')
    classes,_,_,_= _school_form_context(); selected=[x['class_id'] for x in links]; has_final=any(x['final_locked'] for x in links)
    subject_dict={c.key:getattr(subject,c.key) for c in subject.__mapper__.column_attrs}
    locked_map={r['class_id']:bool(r['locked']) for r in links}
    final_map={r['class_id']:bool(r['final_locked']) for r in links}
    if request.method=='POST':
        if has_final:
            flash('This subject has been permanently locked by Super Admin and can no longer be edited.','error'); return redirect(url_for('admin_school_subjects'))
        if any(x['locked'] for x in links):
            flash('Unlock the affected class-subject connection before editing this subject.','error'); return redirect(url_for('admin_school_subject_edit',subject_id=subject_id))
        name=request.form.get('name','').strip(); code=request.form.get('code','').strip(); new_selected=[]
        for v in request.form.getlist('class_ids'):
            try: new_selected.append(int(v))
            except (TypeError,ValueError): pass
        errors=[]
        if not name: errors.append('Subject name is required.')
        if not new_selected: errors.append('Select at least one class.')
        if any(not _school_class_allowed(current_admin()['id'],cid) for cid in new_selected): errors.append('One or more selected classes are outside your authorised scope.')
        locked_class_ids={x['class_id'] for x in links if x['locked']}
        if locked_class_ids and not is_super_admin() and not locked_class_ids.issubset(set(new_selected)): errors.append('A locked class-subject connection cannot be removed by an ordinary administrator.')
        if errors: return render_template('school_subject_form.html',mode='edit',subject={**subject_dict,**request.form},classes=classes,selected=new_selected or selected,locked_map=locked_map,final_map=final_map,errors=errors)
        try:
            existing={x['class_id']:x for x in links}
            duplicate=one_scalar(select(SchoolSubject.id).where(
                func.lower(SchoolSubject.name)==name.lower(),
                SchoolSubject.id!=subject_id).limit(1))
            if duplicate: raise ValueError('Another subject with that name already exists. Use that subject and attach it to the required class instead.')
            subject.name=name; subject.code=code
            now=datetime.now(timezone.utc).isoformat()
            for cid in new_selected:
                if cid not in existing:
                    db.session.add(ClassSubject(class_id=cid,subject_id=subject_id,locked=0,
                                                final_locked=0,created_by=current_admin()['id'],
                                                created_at=now))
            for cid,row in existing.items():
                # A locked link is only removable by a Super Admin; a finally
                # locked one is never removable.
                if cid not in new_selected and (not row['locked'] or is_super_admin()) and not row['final_locked']:
                    db.session.execute(sa_delete(ClassSubject).where(
                        ClassSubject.class_id==cid,ClassSubject.subject_id==subject_id))
            db.session.commit()
        except (ValueError,sa.exc.IntegrityError) as exc:
            db.session.rollback()
            return render_template('school_subject_form.html',mode='edit',subject={**subject_dict,**request.form},classes=classes,selected=new_selected,locked_map=locked_map,final_map=final_map,errors=[str(exc) if isinstance(exc,ValueError) else 'The subject could not be saved because that name is already in use.'])
        audit_log('school_subject_updated','school','subject',subject_id,{'classes':new_selected}); flash('Subject updated.','success'); return redirect(url_for('admin_school_subjects'))
    return render_template('school_subject_form.html',mode='edit',subject=subject,classes=classes,selected=selected,locked_map=locked_map,final_map=final_map,errors=[])

@app.post('/admin/school/subjects/<int:subject_id>/delete')
@admin_required
@csrf_protect
def admin_school_subject_delete(subject_id):
    subject=obj(SchoolSubject,subject_id)
    links=all_rows(select(ClassSubject.locked,ClassSubject.final_locked)
                   .where(ClassSubject.subject_id==subject_id))
    if not subject: abort(404)
    if any(x['final_locked'] for x in links): flash('This subject has been permanently locked by Super Admin and cannot be deleted.','error'); return redirect(url_for('admin_school_subjects'))
    if any(x['locked'] for x in links) and not is_super_admin(): flash('This subject has a locked class assignment. An authorized administrator must resolve the lock before it can be removed.','error'); return redirect(url_for('admin_school_subjects'))
    if not is_super_admin() and not _school_subject_allowed(current_admin()['id'],subject_id): return admin_access_error('school.subjects.delete')
    subject.active=0
    db.session.commit()
    audit_log('school_subject_deactivated','school','subject',subject_id); flash('Subject deactivated.','success'); return redirect(url_for('admin_school_subjects'))

@app.post('/admin/school/subjects/<int:subject_id>/lock/<int:class_id>')
@admin_required
@csrf_protect
def admin_school_subject_lock(subject_id,class_id):
    me=current_admin()
    if not _school_class_allowed(me['id'],class_id): return admin_access_error('school.subjects.lock')
    row=db.session.scalars(select(ClassSubject).where(
        ClassSubject.subject_id==subject_id,ClassSubject.class_id==class_id)).first()
    if not row: abort(404)
    if row.final_locked:
        flash('This class-subject connection is permanently locked by Super Admin.','error'); return redirect(url_for('admin_school_subject_edit',subject_id=subject_id))
    new=0 if row.locked else 1
    row.locked=new
    db.session.commit()
    audit_log('school_subject_lock_changed','school','class_subject',f'{class_id}:{subject_id}',{'locked':new}); flash('Class subject '+('locked.' if new else 'unlocked.'),'success'); return redirect(url_for('admin_school_subject_edit',subject_id=subject_id))

@app.post('/admin/school/subjects/<int:subject_id>/final-lock/<int:class_id>')
@admin_required
@csrf_protect
def admin_school_subject_final_lock(subject_id,class_id):
    me=current_admin()
    if not is_super_admin(me): return admin_access_error('Super Admin control')
    row=db.session.scalars(select(ClassSubject).where(
        ClassSubject.subject_id==subject_id,ClassSubject.class_id==class_id)).first()
    if not row: abort(404)
    if row.final_locked:
        flash('This subject is already permanently locked.','error'); return redirect(url_for('admin_school_subject_edit',subject_id=subject_id))
    row.locked=1; row.final_locked=1; row.final_locked_by=me['id']
    row.final_locked_at=datetime.now(timezone.utc).isoformat()
    db.session.commit()
    audit_log('school_subject_final_locked','school','class_subject',f'{class_id}:{subject_id}',{'final_locked':True},True,me); flash('The class-subject connection is now permanently locked.','success'); return redirect(url_for('admin_school_subject_edit',subject_id=subject_id))

@app.route('/admin/school/assignments')
@admin_required
def admin_school_assignments():
    selected_class=request.args.get('class','').strip(); search=request.args.get('q','').strip()
    question_count=(select(func.count()).select_from(AssignmentQuestion)
                    .where(AssignmentQuestion.assignment_id==SchoolAssignment.id)
                    .correlate(SchoolAssignment).scalar_subquery())
    rows=[_flatten(r,'SchoolAssignment','class_name','subject_name','student_count','question_count')
          for r in all_rows(
        select(SchoolAssignment,SchoolClass.name.label('class_name'),
               SchoolSubject.name.label('subject_name'),
               func.count(AssignmentStudent.student_id).label('student_count'),
               question_count.label('question_count'))
        .join(SchoolClass,SchoolClass.id==SchoolAssignment.class_id)
        .join(SchoolSubject,SchoolSubject.id==SchoolAssignment.subject_id)
        .outerjoin(AssignmentStudent,AssignmentStudent.assignment_id==SchoolAssignment.id)
        .where(SchoolAssignment.active==1)
        .group_by(SchoolAssignment.id).order_by(SchoolAssignment.id.desc()))]
    classes=db.session.scalars(select(SchoolClass).where(SchoolClass.active==1)
        .order_by(SchoolClass.level_order)).all()
    admin=current_admin()
    if admin and not admin['admin_type_system']:
        classes=[c for c in classes if admin_scope_allows(admin['id'],'class',c.name)]
        rows=[r for r in rows if _school_pair_allowed(admin['id'],r['class_id'],r['subject_id'])]
    cards=[{'name':c.name,'count':sum(1 for r in rows if r['class_name']==c.name),'id':c.id} for c in classes]
    if selected_class: rows=[r for r in rows if r['class_name']==selected_class]
    if search:
        needle=search.casefold(); rows=[r for r in rows if needle in f"{r['title']} {r['class_name']} {r['subject_name']}".casefold()]
    return render_template('school_assignments.html',assignments=rows,class_cards=cards,selected_class=selected_class,search=search)

def _assignment_students(assignment_id):
    return [_flatten(r,'AssignmentStudent','first_name','middle_name','last_name','admission_no')
            for r in all_rows(
        select(AssignmentStudent,Student.first_name,Student.middle_name,
               Student.last_name,Student.admission_no)
        .join(Student,Student.id==AssignmentStudent.student_id)
        .where(AssignmentStudent.assignment_id==assignment_id)
        .order_by(Student.last_name,Student.first_name))]

def _notify_school_work(student_ids, category, title, message, action_url, created_by):
    """Notify each student and every linked parent about new or graded work."""
    now=datetime.now(timezone.utc).isoformat()
    ids=sorted(set(student_ids))
    if not ids:
        return
    names={sid:' '.join(x for x in (first,middle,last) if x)
           for sid,first,middle,last in tuples(
        select(Student.id,Student.first_name,Student.middle_name,Student.last_name)
        .where(Student.id.in_(ids)))}
    parents={}
    for sid,pid in tuples(select(ParentStudentLink.student_id,ParentStudentLink.parent_id)
                          .where(ParentStudentLink.student_id.in_(ids),
                                 ParentStudentLink.active==1)):
        parents.setdefault(sid,[]).append(pid)
    for sid in ids:
        child_name=names.get(sid,'your child')
        db.session.add(SchoolNotification(
            recipient_type='student',recipient_id=sid,student_id=sid,category=category,
            title=title,message=message,action_url=action_url,
            created_at=now,created_by=created_by))
        parent_url=url_for('parent_child_detail',student_id=sid)+'#assignments'
        for pid in parents.get(sid,[]):
            db.session.add(SchoolNotification(
                recipient_type='parent',recipient_id=pid,student_id=sid,category=category,
                title=title,message=f'{child_name}: {message}',action_url=parent_url,
                created_at=now,created_by=created_by))

def _assignment_form_data(admin_id):
    classes=db.session.scalars(select(SchoolClass).where(SchoolClass.active==1)
        .order_by(SchoolClass.level_order)).all()
    subjects=[_flatten(r,'SchoolSubject','class_ids') for r in all_rows(
        select(SchoolSubject,func.group_concat(ClassSubject.class_id).label('class_ids'))
        .join(ClassSubject,ClassSubject.subject_id==SchoolSubject.id)
        .where(SchoolSubject.active==1)
        .group_by(SchoolSubject.id).order_by(SchoolSubject.name))]
    students=all_rows(
        select(Student.id,Student.admission_no,Student.first_name,Student.middle_name,
               Student.last_name,SchoolClass.id.label('class_id'),
               SchoolClass.name.label('class_name'))
        .join(StudentEnrolment,and_(StudentEnrolment.student_id==Student.id,
                                    StudentEnrolment.active==1))
        .join(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
        .where(Student.active==1)
        .order_by(SchoolClass.level_order,Student.last_name,Student.first_name))
    if not current_admin()['admin_type_system']:
        classes=[x for x in classes if _school_class_allowed(admin_id,x.id)]
        subjects=[x for x in subjects if _school_subject_allowed(admin_id,x['id'])]
        students=[x for x in students if _school_class_allowed(admin_id,x['class_id'])]
    return classes,subjects,students

@app.route('/admin/school/assignments/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_assignment_new():
    me=current_admin(); classes,subjects,students=_assignment_form_data(me['id'])
    sessions=_school_sessions()
    if request.method=='POST':
        title=request.form.get('title','').strip(); instructions=request.form.get('instructions','').strip(); due=request.form.get('due_date','').strip(); date_given=request.form.get('date_given','').strip() or datetime.now(timezone.utc).date().isoformat()
        assignment_type=request.form.get('assignment_type','written').strip().lower(); timing_mode=request.form.get('timing_mode','untimed').strip().lower()
        term=request.form.get('term','').strip() or None
        try: cid=int(request.form.get('class_id','')); sid=int(request.form.get('subject_id','')); session_id=int(request.form.get('session_id') or 0); time_limit=int(request.form.get('time_limit_minutes','0') or 0)*60; per_q=int(request.form.get('per_question_seconds','0') or 0); max_score=float(request.form.get('max_score','0') or 0)
        except (TypeError,ValueError): cid=sid=session_id=time_limit=per_q=0; max_score=0
        student_ids=[int(v) for v in request.form.getlist('student_ids') if v.isdigit()]; errors=[]
        if not title: errors.append('Assignment title is required.')
        if assignment_type not in ('written','quiz'): errors.append('Choose a valid assignment type.')
        if not _school_pair_allowed(me['id'],cid,sid): errors.append('Select a class and subject within your authorised scope.')
        if not session_id or not one_scalar(select(AcademicSession.id).where(AcademicSession.id==session_id)): errors.append('Select a valid academic session.')
        if term not in ACADEMIC_TERMS: errors.append('Select a valid term.')
        if assignment_type=='quiz' and timing_mode=='overall' and time_limit<=0: errors.append('Enter an overall time limit for this timed assignment.')
        if assignment_type=='quiz' and timing_mode=='per_question' and per_q<=0: errors.append('Enter the time allowed for each question.')
        if timing_mode not in ('untimed','overall','per_question'): errors.append('Choose a valid timing mode.')
        allowed={r['id'] for r in students if r['class_id']==cid}
        if not student_ids: errors.append('Select at least one student.')
        if any(x not in allowed for x in student_ids): errors.append('One or more selected students are outside the selected class.')
        if max_score<0: errors.append('Maximum score cannot be negative.')
        if errors:
            return render_template('school_assignment_form.html',mode='new',assignment=request.form,classes=classes,subjects=subjects,students=students,selected_students=student_ids,sessions=sessions,terms=ACADEMIC_TERMS,errors=errors)
        now=datetime.now(timezone.utc).isoformat()
        assignment=SchoolAssignment(title=title,instructions=instructions,class_id=cid,
            subject_id=sid,due_date=due,created_by=me['id'],created_at=now,active=1,
            assignment_type=assignment_type,date_given=date_given,timing_mode=timing_mode,
            time_limit_seconds=time_limit or None,per_question_seconds=per_q or None,
            max_score=max_score or None,session_id=session_id,term=term)
        db.session.add(assignment); db.session.flush(); aid=assignment.id
        for stid in student_ids:
            db.session.add(AssignmentStudent(assignment_id=aid,student_id=stid,
                                             status='undone',max_score=max_score or None))
        _notify_school_work(student_ids,'assignment','New assignment',f'{title} has been assigned. Due: {due or "No due date"}.',url_for('student_assignment_detail',assignment_id=aid),me['id'])
        db.session.commit()
        _notify_guardians_of_school_work(student_ids,'assignment',title,due)
        audit_log('school_assignment_created','school','assignment',aid,{'class_id':cid,'subject_id':sid,'students':student_ids,'assignment_type':assignment_type,'session_id':session_id,'term':term}); flash('Assignment created. Add questions if this is a CBT-style assignment.','success'); return redirect(url_for('admin_school_assignment_detail',assignment_id=aid))
    return render_template('school_assignment_form.html',mode='new',assignment=None,classes=classes,subjects=subjects,students=students,selected_students=[],sessions=sessions,terms=ACADEMIC_TERMS,errors=[])

def _work_with_class_subject(model, work_id):
    """A project or assignment joined to its class and subject names."""
    row=one(select(model,SchoolClass.name.label('class_name'),
                   SchoolSubject.name.label('subject_name'))
            .join(SchoolClass,SchoolClass.id==model.class_id)
            .join(SchoolSubject,SchoolSubject.id==model.subject_id)
            .where(model.id==work_id))
    return _flatten(row,model.__name__,'class_name','subject_name') if row else None


@app.route('/admin/school/assignments/<int:assignment_id>')
@admin_required
def admin_school_assignment_detail(assignment_id):
    a=_work_with_class_subject(SchoolAssignment,assignment_id)
    if not a: abort(404)
    if not _school_pair_allowed(current_admin()['id'],a['class_id'],a['subject_id']): return admin_access_error('school.assignments.view')
    questions=db.session.scalars(select(AssignmentQuestion)
        .where(AssignmentQuestion.assignment_id==assignment_id)
        .order_by(AssignmentQuestion.sort_order,AssignmentQuestion.id)).all()
    assigned=_assignment_students(assignment_id)
    return render_template('school_assignment_detail.html',assignment=a,questions=questions,assigned=assigned)

@app.post('/admin/school/assignments/<int:assignment_id>/questions')
@admin_required
@csrf_protect
def admin_school_assignment_question_new(assignment_id):
    me=current_admin(); a=obj(SchoolAssignment,assignment_id)
    if not a: abort(404)
    if a.assignment_type!='quiz' or not _school_pair_allowed(me['id'],a.class_id,a.subject_id): return admin_access_error('school.assignments.edit')
    q=request.form.get('question_text','').strip(); options=[request.form.get(k,'').strip() for k in ('option_a','option_b','option_c','option_d')]
    try: correct=int(request.form.get('correct_option','0')); points=float(request.form.get('points','1') or 1)
    except (TypeError,ValueError): correct=0; points=1
    if not q or any(not x for x in options) or correct not in range(4) or points<=0:
        flash('Enter a question, all four options, a correct option and positive points.','error'); return redirect(url_for('admin_school_assignment_detail',assignment_id=assignment_id))
    image_path=None
    try: image_path=_save_image_upload(request.files.get('image'),'assignments',f'assignment_{assignment_id}_{secrets.token_hex(4)}')
    except ValueError as exc: flash(str(exc),'error'); return redirect(url_for('admin_school_assignment_detail',assignment_id=assignment_id))
    sort=one_scalar(select(func.coalesce(func.max(AssignmentQuestion.sort_order),0)+1)
                    .where(AssignmentQuestion.assignment_id==assignment_id), 1)
    db.session.add(AssignmentQuestion(assignment_id=assignment_id,question_text=q,
        instruction=request.form.get('instruction','').strip() or None,image_path=image_path,
        option_a=options[0],option_b=options[1],option_c=options[2],option_d=options[3],
        correct_option=correct,points=points,sort_order=sort))
    db.session.flush()
    total=one_scalar(select(func.coalesce(func.sum(AssignmentQuestion.points),0))
                     .where(AssignmentQuestion.assignment_id==assignment_id), 0)
    a.max_score=total
    # Students who have already started keep the maximum their attempt was built on.
    started=select(SchoolAssignmentAttempt.student_id).where(
        SchoolAssignmentAttempt.assignment_id==assignment_id).scalar_subquery()
    db.session.execute(sa_update(AssignmentStudent)
        .where(AssignmentStudent.assignment_id==assignment_id,
               AssignmentStudent.student_id.not_in(started))
        .values(max_score=total))
    db.session.commit()
    audit_log('school_assignment_question_added','school','assignment',assignment_id,{'points':points}); flash('Question added.','success'); return redirect(url_for('admin_school_assignment_detail',assignment_id=assignment_id))

@app.post('/admin/school/assignments/<int:assignment_id>/students/<int:student_id>')
@admin_required
@csrf_protect
def admin_school_assignment_student_update(assignment_id,student_id):
    me=current_admin(); status=request.form.get('status','undone').strip().lower(); remark=request.form.get('remark','').strip()
    try: score=float(request.form.get('score','')) if request.form.get('score','').strip() else None
    except (TypeError,ValueError): score=None
    if status not in ('done','undone','in_progress','overdue'): status='undone'
    a=obj(SchoolAssignment,assignment_id)
    row=db.session.scalars(select(AssignmentStudent).where(
        AssignmentStudent.assignment_id==assignment_id,
        AssignmentStudent.student_id==student_id)).first()
    if not a or not row: abort(404)
    if not _school_pair_allowed(me['id'],a.class_id,a.subject_id): return admin_access_error('school.assignments.edit')
    max_score=a.max_score or row.max_score
    if score is not None and (score<0 or (max_score is not None and score>max_score)):
        flash('Enter a score within the assignment maximum.','error'); return redirect(url_for('admin_school_assignment_detail',assignment_id=assignment_id))
    row.status=status; row.score=score; row.remark=remark or None
    row.graded_at=datetime.now(timezone.utc).isoformat(); row.graded_by=me['id']
    db.session.commit()
    audit_log('school_assignment_student_updated','school','assignment',assignment_id,{'student_id':student_id,'status':status,'score':score}); return redirect(url_for('admin_school_assignment_detail',assignment_id=assignment_id))

@app.route('/admin/school/assignments/<int:assignment_id>/edit',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_assignment_edit(assignment_id):
    a=obj(SchoolAssignment,assignment_id)
    if not a: abort(404)
    selected=[sid for (sid,) in tuples(select(AssignmentStudent.student_id)
        .where(AssignmentStudent.assignment_id==assignment_id))]
    if not _school_pair_allowed(current_admin()['id'],a.class_id,a.subject_id): return admin_access_error('school.assignments.edit')
    classes,subjects,students=_assignment_form_data(current_admin()['id'])
    sessions=_school_sessions()
    if request.method=='POST':
        title=request.form.get('title','').strip(); instructions=request.form.get('instructions','').strip(); due=request.form.get('due_date','').strip(); date_given=request.form.get('date_given','').strip() or a.date_given; assignment_type=request.form.get('assignment_type',a.assignment_type).strip().lower(); timing_mode=request.form.get('timing_mode',a.timing_mode).strip().lower()
        term=request.form.get('term','').strip() or None
        try: cid=int(request.form.get('class_id','')); sid=int(request.form.get('subject_id','')); session_id=int(request.form.get('session_id') or 0); time_limit=int(request.form.get('time_limit_minutes','0') or 0)*60; per_q=int(request.form.get('per_question_seconds','0') or 0); max_score=float(request.form.get('max_score','0') or 0)
        except (TypeError,ValueError): cid=sid=session_id=time_limit=per_q=0; max_score=0
        student_ids=[int(v) for v in request.form.getlist('student_ids') if v.isdigit()]; errors=[]
        if not title: errors.append('Assignment title is required.')
        if not _school_pair_allowed(current_admin()['id'],cid,sid): errors.append('Select a valid class and subject.')
        if not session_id or not one_scalar(select(AcademicSession.id).where(AcademicSession.id==session_id)): errors.append('Select a valid academic session.')
        if term not in ACADEMIC_TERMS: errors.append('Select a valid term.')
        if assignment_type=='quiz' and timing_mode=='overall' and time_limit<=0: errors.append('Enter an overall time limit.')
        if assignment_type=='quiz' and timing_mode=='per_question' and per_q<=0: errors.append('Enter a per-question time limit.')
        if not student_ids: errors.append('Select at least one student.')
        if errors:
            form={c.key:getattr(a,c.key) for c in a.__mapper__.column_attrs}
            return render_template('school_assignment_form.html',mode='edit',assignment={**form,**request.form},classes=classes,subjects=subjects,students=students,selected_students=student_ids or selected,sessions=sessions,terms=ACADEMIC_TERMS,errors=errors)
        a.title=title; a.instructions=instructions; a.class_id=cid; a.subject_id=sid
        a.due_date=due; a.assignment_type=assignment_type; a.date_given=date_given
        a.timing_mode=timing_mode; a.time_limit_seconds=time_limit or None
        a.per_question_seconds=per_q or None; a.session_id=session_id; a.term=term
        effective_max=max_score or a.max_score
        a.max_score=effective_max
        old=set(selected); new=set(student_ids)
        for stid in new-old:
            _ignore_insert(AssignmentStudent, [{'assignment_id':assignment_id,'student_id':stid,
                                                'status':'undone','max_score':effective_max}])
        # A student who has already started the CBT attempt keeps their record.
        started=select(SchoolAssignmentAttempt.student_id).where(
            SchoolAssignmentAttempt.assignment_id==assignment_id).scalar_subquery()
        for stid in old-new:
            db.session.execute(sa_delete(AssignmentStudent).where(
                AssignmentStudent.assignment_id==assignment_id,
                AssignmentStudent.student_id==stid,
                AssignmentStudent.student_id.not_in(started)))
        db.session.commit()
        audit_log('school_assignment_updated','school','assignment',assignment_id,{'class_id':cid,'subject_id':sid}); flash('Assignment updated.','success'); return redirect(url_for('admin_school_assignment_detail',assignment_id=assignment_id))
    return render_template('school_assignment_form.html',mode='edit',assignment=a,classes=classes,subjects=subjects,students=students,selected_students=selected,sessions=sessions,terms=ACADEMIC_TERMS,errors=[])

@app.post('/admin/school/assignments/<int:assignment_id>/delete')
@admin_required
@csrf_protect
def admin_school_assignment_delete(assignment_id):
    a=obj(SchoolAssignment,assignment_id)
    if not a: abort(404)
    if not _school_pair_allowed(current_admin()['id'],a.class_id,a.subject_id): return admin_access_error('school.assignments.delete')
    a.active=0
    db.session.commit()
    audit_log('school_assignment_deleted','school','assignment',assignment_id); flash('Assignment removed.','success'); return redirect(url_for('admin_school_assignments'))

@app.route('/admin/school/projects')
@admin_required
def admin_school_projects():
    selected_class=request.args.get('class','').strip(); search=request.args.get('q','').strip()
    rows=[_flatten(r,'SchoolProject','class_name','subject_name','student_count')
          for r in all_rows(
        select(SchoolProject,SchoolClass.name.label('class_name'),
               SchoolSubject.name.label('subject_name'),
               func.count(ProjectStudent.student_id).label('student_count'))
        .join(SchoolClass,SchoolClass.id==SchoolProject.class_id)
        .join(SchoolSubject,SchoolSubject.id==SchoolProject.subject_id)
        .outerjoin(ProjectStudent,ProjectStudent.project_id==SchoolProject.id)
        .where(SchoolProject.active==1)
        .group_by(SchoolProject.id).order_by(SchoolProject.id.desc()))]
    classes=db.session.scalars(select(SchoolClass).where(SchoolClass.active==1)
        .order_by(SchoolClass.level_order)).all()
    me=current_admin()
    if not me['admin_type_system']:
        classes=[c for c in classes if _school_class_allowed(me['id'],c.id)]
        rows=[r for r in rows if _school_pair_allowed(me['id'],r['class_id'],r['subject_id'])]
    cards=[{'name':c.name,'count':sum(1 for r in rows if r['class_name']==c.name),'id':c.id} for c in classes]
    if selected_class: rows=[r for r in rows if r['class_name']==selected_class]
    if search:
        n=search.casefold(); rows=[r for r in rows if n in f"{r['title']} {r['class_name']} {r['subject_name']}".casefold()]
    return render_template('school_projects.html',projects=rows,class_cards=cards,selected_class=selected_class,search=search)

@app.route('/admin/school/projects/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_project_new():
    me=current_admin(); classes,subjects,students=_assignment_form_data(me['id'])
    sessions=_school_sessions()
    if request.method=='POST':
        title=request.form.get('title','').strip(); instructions=request.form.get('instructions','').strip(); date_given=request.form.get('date_given','').strip() or datetime.now(timezone.utc).date().isoformat(); due=request.form.get('due_date','').strip()
        term=request.form.get('term','').strip() or None
        try: cid=int(request.form.get('class_id','')); sid=int(request.form.get('subject_id','')); session_id=int(request.form.get('session_id') or 0); max_score=float(request.form.get('max_score','0') or 0)
        except (TypeError,ValueError): cid=sid=session_id=0; max_score=0
        student_ids=[int(v) for v in request.form.getlist('student_ids') if v.isdigit()]; errors=[]
        if not title: errors.append('Project title is required.')
        if not _school_pair_allowed(me['id'],cid,sid): errors.append('Select a class and subject within your authorised scope.')
        if not session_id or not one_scalar(select(AcademicSession.id).where(AcademicSession.id==session_id)): errors.append('Select a valid academic session.')
        if term not in ACADEMIC_TERMS: errors.append('Select a valid term.')
        if not student_ids: errors.append('Select at least one student.')
        allowed={r['id'] for r in students if r['class_id']==cid}
        if any(x not in allowed for x in student_ids): errors.append('One or more selected students are outside the selected class.')
        if max_score<0: errors.append('Maximum score cannot be negative.')
        if errors: return render_template('school_project_form.html',mode='new',project=request.form,classes=classes,subjects=subjects,students=students,selected_students=student_ids,sessions=sessions,terms=ACADEMIC_TERMS,errors=errors)
        now=datetime.now(timezone.utc).isoformat()
        project=SchoolProject(title=title,instructions=instructions,class_id=cid,subject_id=sid,
            date_given=date_given,due_date=due,max_score=max_score or None,
            created_by=me['id'],created_at=now,active=1,session_id=session_id,term=term)
        db.session.add(project); db.session.flush(); pid=project.id
        for stid in student_ids:
            db.session.add(ProjectStudent(project_id=pid,student_id=stid,status='not_done'))
        _notify_school_work(student_ids,'project','New project',f'{title} has been assigned. Required: {due or "No due date"}.',url_for('student_dashboard')+'#projects',me['id'])
        db.session.commit()
        _notify_guardians_of_school_work(student_ids,'project',title,due)
        audit_log('school_project_created','school','project',pid,{'class_id':cid,'subject_id':sid,'students':student_ids,'session_id':session_id,'term':term}); flash('Project created and assigned.','success'); return redirect(url_for('admin_school_project_detail',project_id=pid))
    return render_template('school_project_form.html',mode='new',project=None,classes=classes,subjects=subjects,students=students,selected_students=[],sessions=sessions,terms=ACADEMIC_TERMS,errors=[])

@app.route('/admin/school/projects/<int:project_id>')
@admin_required
def admin_school_project_detail(project_id):
    p=_work_with_class_subject(SchoolProject,project_id)
    if not p: abort(404)
    if not _school_pair_allowed(current_admin()['id'],p['class_id'],p['subject_id']): return admin_access_error('school.projects.view')
    assigned=[_flatten(r,'ProjectStudent','first_name','middle_name','last_name','admission_no')
              for r in all_rows(
        select(ProjectStudent,Student.first_name,Student.middle_name,
               Student.last_name,Student.admission_no)
        .join(Student,Student.id==ProjectStudent.student_id)
        .where(ProjectStudent.project_id==project_id)
        .order_by(Student.last_name,Student.first_name))]
    return render_template('school_project_detail.html',project=p,assigned=assigned)

@app.post('/admin/school/projects/<int:project_id>/students/<int:student_id>')
@admin_required
@csrf_protect
def admin_school_project_student_update(project_id,student_id):
    me=current_admin(); status=request.form.get('status','not_done'); remark=request.form.get('remark','').strip()
    try: score=float(request.form.get('score','')) if request.form.get('score','').strip() else None
    except (TypeError,ValueError): score=None
    if status not in ('done','not_done'): status='not_done'
    p=obj(SchoolProject,project_id)
    row=db.session.scalars(select(ProjectStudent).where(
        ProjectStudent.project_id==project_id,
        ProjectStudent.student_id==student_id)).first()
    if not p or not row: abort(404)
    if not _school_pair_allowed(me['id'],p.class_id,p.subject_id): return admin_access_error('school.projects.edit')
    if score is not None and (score<0 or (p.max_score is not None and score>p.max_score)):
        flash('Enter a score within the project maximum.','error'); return redirect(url_for('admin_school_project_detail',project_id=project_id))
    row.status=status; row.score=score; row.remark=remark or None
    row.graded_by=me['id']; row.graded_at=datetime.now(timezone.utc).isoformat()
    db.session.commit()
    audit_log('school_project_student_updated','school','project',project_id,{'student_id':student_id,'status':status,'score':score}); return redirect(url_for('admin_school_project_detail',project_id=project_id))

@app.route('/admin/school/projects/<int:project_id>/edit',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_project_edit(project_id):
    me=current_admin(); p=obj(SchoolProject,project_id)
    if not p: abort(404)
    selected=[sid for (sid,) in tuples(select(ProjectStudent.student_id)
        .where(ProjectStudent.project_id==project_id))]
    if not _school_pair_allowed(me['id'],p.class_id,p.subject_id): return admin_access_error('school.projects.edit')
    classes,subjects,students=_assignment_form_data(me['id'])
    sessions=_school_sessions()
    if request.method=='POST':
        title=request.form.get('title','').strip(); instructions=request.form.get('instructions','').strip(); date_given=request.form.get('date_given','').strip() or p.date_given; due=request.form.get('due_date','').strip()
        term=request.form.get('term','').strip() or None
        try: cid=int(request.form.get('class_id','')); sid=int(request.form.get('subject_id','')); session_id=int(request.form.get('session_id') or 0); max_score=float(request.form.get('max_score','0') or 0)
        except (TypeError,ValueError): cid=sid=session_id=0; max_score=0
        student_ids=[int(v) for v in request.form.getlist('student_ids') if v.isdigit()]; errors=[]
        if not title: errors.append('Project title is required.')
        if not _school_pair_allowed(me['id'],cid,sid): errors.append('Select a valid class and subject.')
        if not session_id or not one_scalar(select(AcademicSession.id).where(AcademicSession.id==session_id)): errors.append('Select a valid academic session.')
        if term not in ACADEMIC_TERMS: errors.append('Select a valid term.')
        if not student_ids: errors.append('Select at least one student.')
        if errors:
            form={c.key:getattr(p,c.key) for c in p.__mapper__.column_attrs}
            return render_template('school_project_form.html',mode='edit',project={**form,**request.form},classes=classes,subjects=subjects,students=students,selected_students=student_ids or selected,sessions=sessions,terms=ACADEMIC_TERMS,errors=errors)
        p.title=title; p.instructions=instructions; p.class_id=cid; p.subject_id=sid
        p.date_given=date_given; p.due_date=due; p.max_score=max_score or p.max_score
        p.session_id=session_id; p.term=term
        old=set(selected); new=set(student_ids)
        for stid in new-old:
            _ignore_insert(ProjectStudent, [{'project_id':project_id,'student_id':stid,
                                             'status':'not_done'}])
        for stid in old-new:
            db.session.execute(sa_delete(ProjectStudent).where(
                ProjectStudent.project_id==project_id,ProjectStudent.student_id==stid))
        db.session.commit()
        audit_log('school_project_updated','school','project',project_id,{'class_id':cid,'subject_id':sid}); flash('Project updated.','success'); return redirect(url_for('admin_school_project_detail',project_id=project_id))
    return render_template('school_project_form.html',mode='edit',project=p,classes=classes,subjects=subjects,students=students,selected_students=selected,sessions=sessions,terms=ACADEMIC_TERMS,errors=[])

@app.post('/admin/school/projects/<int:project_id>/delete')
@admin_required
@csrf_protect
def admin_school_project_delete(project_id):
    me=current_admin(); p=obj(SchoolProject,project_id)
    if not p: abort(404)
    if not _school_pair_allowed(me['id'],p.class_id,p.subject_id): return admin_access_error('school.projects.delete')
    p.active=0
    db.session.commit()
    audit_log('school_project_deleted','school','project',project_id); flash('Project removed.','success'); return redirect(url_for('admin_school_projects'))

def _school_assessments(kind):
    rows=[_flatten(r,'SchoolAssessment','class_name','subject_name','question_count') for r in all_rows(
        select(SchoolAssessment,SchoolClass.name.label('class_name'),
               SchoolSubject.name.label('subject_name'),
               func.count(SchoolQuestion.id).label('question_count'))
            .join(SchoolClass,SchoolClass.id==SchoolAssessment.class_id)
            .join(SchoolSubject,SchoolSubject.id==SchoolAssessment.subject_id)
            .outerjoin(SchoolQuestion,SchoolQuestion.assessment_id==SchoolAssessment.id)
            .where(SchoolAssessment.assessment_type==kind)
            .group_by(SchoolAssessment.id).order_by(SchoolAssessment.id.desc()))]
    classes=db.session.scalars(select(SchoolClass).where(SchoolClass.active==1)
                               .order_by(SchoolClass.level_order)).all()
    admin=current_admin()
    if admin and not admin['admin_type_system']:
        classes=[c for c in classes if admin_scope_allows(admin['id'],'class',c['name'])]
        rows=[r for r in rows if _school_pair_allowed(admin['id'],r['class_id'],r['subject_id'])]
    return rows,classes

def _school_assessment_title(kind): return {'test':'Tests','practice':'Practice Tests','examination':'Examinations'}[kind]

def _school_assessment_description(kind): return {'test':'Teacher-created assessments connected to a school subject and class.','practice':'Practice assessments managed separately from official academic examinations.','examination':'Formal school examinations whose results belong to the student academic record.'}[kind]

def _school_assessment_list(kind):
    selected_class=request.args.get('class','').strip(); search=request.args.get('q','').strip(); assessments,classes=_school_assessments(kind)
    cards=[{'name':c['name'],'count':sum(1 for a in assessments if a['class_name']==c['name']),'id':c['id']} for c in classes]
    if selected_class: assessments=[a for a in assessments if a['class_name']==selected_class]
    if search:
        needle=search.casefold(); assessments=[a for a in assessments if needle in f"{a['title']} {a['class_name']} {a['subject_name']}".casefold()]
    return render_template('school_assessment_list.html',assessment_type=kind,title=_school_assessment_title(kind),description=_school_assessment_description(kind),assessments=assessments,class_cards=cards,selected_class=selected_class,search=search)

@app.route('/admin/school/tests')
@admin_required
def admin_school_tests(): return _school_assessment_list('test')

@app.route('/admin/school/practice-tests')
@admin_required
def admin_school_practice_tests(): return _school_assessment_list('practice')

@app.route('/admin/school/examinations')
@admin_required
def admin_school_examinations(): return _school_assessment_list('examination')

def _school_sessions():
    return db.session.scalars(select(AcademicSession).where(AcademicSession.active==1)
                              .order_by(AcademicSession.id.desc())).all()

def _ca_weights():
    """How CA_MAX_SCORE (40) is split between tests, assignments and projects.

    Stored per-school in school_settings; falls back to CA_DEFAULT_WEIGHTS for
    any component that has never been set. Always returns exactly the three
    keys in CA_DEFAULT_WEIGHTS, as floats.
    """
    school_id=_primary_school_id()
    weights=dict(CA_DEFAULT_WEIGHTS)
    if not school_id: return weights
    rows=all_rows(select(SchoolSetting.setting_key,SchoolSetting.setting_value)
                  .where(SchoolSetting.school_id==school_id,
                         SchoolSetting.setting_key.in_([f'ca_weight_{k}' for k in CA_DEFAULT_WEIGHTS])))
    for r in rows:
        key=r['setting_key'].removeprefix('ca_weight_')
        try: weights[key]=float(r['setting_value'])
        except (TypeError,ValueError): pass
    return weights

def _set_ca_weights(weights, admin_id):
    school_id=_primary_school_id()
    if not school_id: raise ValueError('No active school is configured.')
    now=datetime.now(timezone.utc).isoformat()
    for key,value in weights.items():
        setting_key=f'ca_weight_{key}'
        row=db.session.scalars(select(SchoolSetting).where(
            SchoolSetting.school_id==school_id,SchoolSetting.setting_key==setting_key)).first()
        if row:
            row.setting_value=str(value); row.updated_at=now; row.updated_by=admin_id
        else:
            db.session.add(SchoolSetting(school_id=school_id,setting_key=setting_key,
                                          setting_value=str(value),updated_at=now,updated_by=admin_id))
    db.session.commit()

def _term_subject_report(student_id, session_id, term, subject_id):
    """A student's Exam(60) + CA(40) = 100 breakdown for one subject in one term.

    Practice tests never contribute (they're a self-study tool, not part of
    the official record). Multiple items in the same category (e.g. two
    tests) are combined by summing their raw score and raw max together, then
    scaling the combined total to that category's share of CA_MAX_SCORE -
    never simply added on top of each other, so one extra test never lets a
    subject exceed its 100-mark ceiling.
    """
    weights=_ca_weights()

    def scaled(raw_score, raw_max, cap):
        if not raw_max: return 0.0
        return min(cap, round(raw_score/raw_max*cap,2))

    exam_raw=tuples(select(func.coalesce(func.sum(SchoolStudentResult.score),0),
                        func.coalesce(func.sum(SchoolStudentResult.max_score),0))
        .select_from(SchoolStudentResult)
        .outerjoin(SchoolAssessment,SchoolAssessment.id==SchoolStudentResult.assessment_id)
        .where(SchoolStudentResult.student_id==student_id,SchoolStudentResult.subject_id==subject_id,
               SchoolStudentResult.session_id==session_id,
               func.coalesce(SchoolStudentResult.term,'Full Session')==term,
               SchoolStudentResult.score.is_not(None),
               or_(SchoolAssessment.assessment_type=='examination',
                   and_(SchoolStudentResult.assessment_id.is_(None),
                        SchoolStudentResult.component_name.like('Exam%')))))[0]
    test_raw=tuples(select(func.coalesce(func.sum(SchoolStudentResult.score),0),
                        func.coalesce(func.sum(SchoolStudentResult.max_score),0))
        .select_from(SchoolStudentResult)
        .outerjoin(SchoolAssessment,SchoolAssessment.id==SchoolStudentResult.assessment_id)
        .where(SchoolStudentResult.student_id==student_id,SchoolStudentResult.subject_id==subject_id,
               SchoolStudentResult.session_id==session_id,
               func.coalesce(SchoolStudentResult.term,'Full Session')==term,
               SchoolStudentResult.score.is_not(None),
               or_(SchoolAssessment.assessment_type=='test',
                   and_(SchoolStudentResult.assessment_id.is_(None),
                        SchoolStudentResult.component_name.like('Test%')))))[0]
    assignment_raw=tuples(select(func.coalesce(func.sum(AssignmentStudent.score),0),
                              func.coalesce(func.sum(func.coalesce(AssignmentStudent.max_score,SchoolAssignment.max_score)),0))
        .select_from(AssignmentStudent)
        .join(SchoolAssignment,SchoolAssignment.id==AssignmentStudent.assignment_id)
        .where(AssignmentStudent.student_id==student_id,SchoolAssignment.subject_id==subject_id,
               SchoolAssignment.session_id==session_id,SchoolAssignment.term==term,
               AssignmentStudent.score.is_not(None)))[0]
    project_raw=tuples(select(func.coalesce(func.sum(ProjectStudent.score),0),
                           func.coalesce(func.sum(SchoolProject.max_score),0))
        .select_from(ProjectStudent)
        .join(SchoolProject,SchoolProject.id==ProjectStudent.project_id)
        .where(ProjectStudent.student_id==student_id,SchoolProject.subject_id==subject_id,
               SchoolProject.session_id==session_id,SchoolProject.term==term,
               ProjectStudent.score.is_not(None)))[0]

    exam_score=scaled(exam_raw[0],exam_raw[1],EXAM_MAX_SCORE)
    test_score=scaled(test_raw[0],test_raw[1],weights['test'])
    assignment_score=scaled(assignment_raw[0],assignment_raw[1],weights['assignment'])
    project_score=scaled(project_raw[0],project_raw[1],weights['project'])
    ca_score=round(test_score+assignment_score+project_score,2)
    return {
        'exam_score':exam_score,'exam_max':EXAM_MAX_SCORE,
        'test_score':test_score,'test_max':weights['test'],
        'assignment_score':assignment_score,'assignment_max':weights['assignment'],
        'project_score':project_score,'project_max':weights['project'],
        'ca_score':ca_score,'ca_max':CA_MAX_SCORE,
        'total_score':round(exam_score+ca_score,2),'total_max':EXAM_MAX_SCORE+CA_MAX_SCORE,
        'has_data':bool(exam_raw[1] or test_raw[1] or assignment_raw[1] or project_raw[1]),
    }

def _student_term_subjects(student_id, session_id, term):
    """Every subject this student has any exam/test/assignment/project record
    for in this session+term, so the summary table only lists subjects that
    actually have something recorded."""
    subject_ids=set()
    for row in tuples(select(SchoolStudentResult.subject_id).distinct()
            .where(SchoolStudentResult.student_id==student_id,
                   SchoolStudentResult.session_id==session_id,
                   func.coalesce(SchoolStudentResult.term,'Full Session')==term)):
        subject_ids.add(row[0])
    for row in tuples(select(SchoolAssignment.subject_id).distinct()
            .join(AssignmentStudent,AssignmentStudent.assignment_id==SchoolAssignment.id)
            .where(AssignmentStudent.student_id==student_id,
                   SchoolAssignment.session_id==session_id,SchoolAssignment.term==term)):
        subject_ids.add(row[0])
    for row in tuples(select(SchoolProject.subject_id).distinct()
            .join(ProjectStudent,ProjectStudent.project_id==SchoolProject.id)
            .where(ProjectStudent.student_id==student_id,
                   SchoolProject.session_id==session_id,SchoolProject.term==term)):
        subject_ids.add(row[0])
    if not subject_ids: return []
    names={sid:name for sid,name in tuples(select(SchoolSubject.id,SchoolSubject.name)
                                           .where(SchoolSubject.id.in_(subject_ids)))}
    reports=[]
    for sid in subject_ids:
        report=_term_subject_report(student_id,session_id,term,sid)
        if report['has_data']:
            reports.append({'subject_id':sid,'subject_name':names.get(sid,'—'),**report})
    reports.sort(key=lambda r:r['subject_name'])
    return reports

def _student_term_periods(student_id):
    """Every (session, term) combination this student has any record in,
    newest first."""
    periods=set()
    for sid,term in tuples(select(SchoolStudentResult.session_id,
                                  func.coalesce(SchoolStudentResult.term,'Full Session'))
            .where(SchoolStudentResult.student_id==student_id,
                   SchoolStudentResult.session_id.is_not(None)).distinct()):
        periods.add((sid,term))
    for sid,term in tuples(select(SchoolAssignment.session_id,SchoolAssignment.term)
            .join(AssignmentStudent,AssignmentStudent.assignment_id==SchoolAssignment.id)
            .where(AssignmentStudent.student_id==student_id,
                   SchoolAssignment.session_id.is_not(None),SchoolAssignment.term.is_not(None)).distinct()):
        periods.add((sid,term))
    for sid,term in tuples(select(SchoolProject.session_id,SchoolProject.term)
            .join(ProjectStudent,ProjectStudent.project_id==SchoolProject.id)
            .where(ProjectStudent.student_id==student_id,
                   SchoolProject.session_id.is_not(None),SchoolProject.term.is_not(None)).distinct()):
        periods.add((sid,term))
    if not periods: return []
    session_names={row[0]:row[1] for row in tuples(select(AcademicSession.id,AcademicSession.name)
                                                    .where(AcademicSession.id.in_({p[0] for p in periods})))}
    term_order={t:i for i,t in enumerate(ACADEMIC_TERMS)}
    out=[{'session_id':sid,'session_name':session_names.get(sid,'—'),'term':term}
         for sid,term in periods]
    out.sort(key=lambda p:(-p['session_id'],-term_order.get(p['term'],-1)))
    return out

def _school_assessment_new(kind):
    classes,subjects,_,session_row=_school_form_context()
    if request.method=='POST':
        title=request.form.get('title','').strip(); instructions=request.form.get('instructions','').strip()
        term=request.form.get('term','').strip() or None
        try: cid=int(request.form.get('class_id','')); sid=int(request.form.get('subject_id','')); duration=max(1,int(request.form.get('duration_minutes','30') or 30)); session_id=int(request.form.get('session_id') or session_row['id'])
        except: cid=sid=session_id=0; duration=30
        errors=[]
        if not title: errors.append('Title is required.')
        if not _school_pair_allowed(current_admin()['id'],cid,sid): errors.append('Select a class and subject within your authorised scope.')
        if not session_id: errors.append('An academic session is required.')
        if kind!='practice' and term not in ACADEMIC_TERMS: errors.append('Select a valid term.')
        elif kind=='practice' and term and term not in ACADEMIC_TERMS: errors.append('Select a valid term.')
        if errors: return render_template('school_assessment_form.html',mode='new',assessment_type=kind,assessment=request.form,classes=classes,subjects=subjects,sessions=_school_sessions(),terms=ACADEMIC_TERMS,errors=errors)
        now=datetime.now(timezone.utc).isoformat()
        assessment=SchoolAssessment(assessment_type=kind,title=title,instructions=instructions,
                                    class_id=cid,subject_id=sid,session_id=session_id,
                                    duration_minutes=duration,question_count=0,active=0,
                                    created_by=current_admin()['id'],created_at=now,
                                    term=term or 'Full Session')
        db.session.add(assessment); db.session.flush(); aid=assessment.id
        db.session.commit(); audit_log('school_assessment_created','school',kind,aid,{'class_id':cid,'subject_id':sid,'term':term}); flash(f'{_school_assessment_title(kind)} item created. Add questions next.','success'); return redirect(url_for('admin_school_assessment_detail',assessment_id=aid))
    return render_template('school_assessment_form.html',mode='new',assessment_type=kind,assessment=None,classes=classes,subjects=subjects,sessions=_school_sessions(),terms=ACADEMIC_TERMS,errors=[])

@app.route('/admin/school/tests/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_test_new(): return _school_assessment_new('test')

@app.route('/admin/school/practice-tests/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_practice_new(): return _school_assessment_new('practice')

@app.route('/admin/school/examinations/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_examination_new(): return _school_assessment_new('examination')

@app.route('/admin/school/assessments/<int:assessment_id>')
@admin_required
def admin_school_assessment_detail(assessment_id):
    a=_flatten(one(select(SchoolAssessment,SchoolClass.name.label('class_name'),
                          SchoolSubject.name.label('subject_name'),
                          AcademicSession.name.label('session_name'))
        .join(SchoolClass,SchoolClass.id==SchoolAssessment.class_id)
        .join(SchoolSubject,SchoolSubject.id==SchoolAssessment.subject_id)
        .outerjoin(AcademicSession,AcademicSession.id==SchoolAssessment.session_id)
        .where(SchoolAssessment.id==assessment_id)),
        'SchoolAssessment','class_name','subject_name','session_name')
    questions=db.session.scalars(select(SchoolQuestion)
        .where(SchoolQuestion.assessment_id==assessment_id)
        .order_by(SchoolQuestion.sort_order,SchoolQuestion.id)).all()
    if not a: abort(404)
    perm='school.practice.edit' if a['assessment_type']=='practice' else ('school.examinations.edit' if a['assessment_type']=='examination' else 'school.tests.edit')
    if not admin_has_permission(current_admin()['id'],perm): return admin_access_error(perm)
    if not _school_pair_allowed(current_admin()['id'],a['class_id'],a['subject_id']): return admin_access_error(perm)
    return render_template('school_assessment_detail.html',assessment=a,questions=questions,sessions=_school_sessions(),terms=ACADEMIC_TERMS)

@app.post('/admin/school/assessments/<int:assessment_id>/edit')
@admin_required
@csrf_protect
def admin_school_assessment_edit(assessment_id):
    a=obj(SchoolAssessment, assessment_id)
    if not a: abort(404)
    perm='school.practice.edit' if a['assessment_type']=='practice' else ('school.examinations.edit' if a['assessment_type']=='examination' else 'school.tests.edit')
    if not admin_has_permission(current_admin()['id'],perm): return admin_access_error(perm)
    if not _school_pair_allowed(current_admin()['id'],a['class_id'],a['subject_id']): return admin_access_error(perm)
    title=request.form.get('title','').strip(); instructions=request.form.get('instructions','').strip()
    term=request.form.get('term','').strip() or None
    try: cid=int(request.form.get('class_id','')); sid=int(request.form.get('subject_id','')); duration=max(1,int(request.form.get('duration_minutes','30') or 30)); session_id=int(request.form.get('session_id'))
    except: cid=sid=session_id=0; duration=30
    if not title or not _school_pair_allowed(current_admin()['id'],cid,sid): flash('Enter a valid title, class and subject within your scope.','error'); return redirect(url_for('admin_school_assessment_detail',assessment_id=assessment_id))
    if a['assessment_type']!='practice' and term not in ACADEMIC_TERMS:
        flash('Select a valid term.','error'); return redirect(url_for('admin_school_assessment_detail',assessment_id=assessment_id))
    if term and term not in ACADEMIC_TERMS:
        flash('Select a valid term.','error'); return redirect(url_for('admin_school_assessment_detail',assessment_id=assessment_id))
    db.session.execute(sa_update(SchoolAssessment).where(SchoolAssessment.id==assessment_id)
        .values(title=title,instructions=instructions,class_id=cid,subject_id=sid,
                session_id=session_id,duration_minutes=duration,term=term or 'Full Session'))
    db.session.commit(); audit_log('school_assessment_updated','school',a['assessment_type'],assessment_id,{'class_id':cid,'subject_id':sid,'term':term}); flash('Assessment updated.','success'); return redirect(url_for('admin_school_assessment_detail',assessment_id=assessment_id))

@app.post('/admin/school/assessments/<int:assessment_id>/toggle')
@admin_required
@csrf_protect
def admin_school_assessment_toggle(assessment_id):
    a=obj(SchoolAssessment, assessment_id)
    if not a: abort(404)
    perm='school.practice.edit' if a['assessment_type']=='practice' else ('school.examinations.edit' if a['assessment_type']=='examination' else 'school.tests.edit')
    if not admin_has_permission(current_admin()['id'],perm): return admin_access_error(perm)
    if not _school_pair_allowed(current_admin()['id'],a['class_id'],a['subject_id']): return admin_access_error(perm)
    new=0 if a['active'] else 1
    db.session.execute(sa_update(SchoolAssessment).where(SchoolAssessment.id==assessment_id)
                       .values(active=new))
    db.session.commit(); audit_log('school_assessment_status_changed','school',a['assessment_type'],assessment_id,{'active':new}); flash('Assessment '+('activated.' if new else 'closed.'),'success'); return redirect(url_for('admin_school_assessment_detail',assessment_id=assessment_id))

@app.post('/admin/school/assessments/<int:assessment_id>/questions/new')
@admin_required
@csrf_protect
def admin_school_assessment_question_new(assessment_id):
    a=obj(SchoolAssessment, assessment_id)
    if not a: abort(404)
    perm='school.practice.edit' if a['assessment_type']=='practice' else ('school.examinations.edit' if a['assessment_type']=='examination' else 'school.tests.edit')
    if not admin_has_permission(current_admin()['id'],perm): return admin_access_error(perm)
    if not _school_pair_allowed(current_admin()['id'],a['class_id'],a['subject_id']): return admin_access_error(perm)
    text=request.form.get('question_text','').strip(); instruction=request.form.get('instruction','').strip(); opts=[request.form.get(f'option_{x}','').strip() for x in 'abcd']
    try: correct=int(request.form.get('correct_option','-1')); points=max(1,int(request.form.get('points','1') or 1))
    except: correct=-1; points=1
    try:
        image_path=_save_image_upload(request.files.get('image'),'questions',f'school_{assessment_id}')
    except ValueError as exc:
        flash(str(exc),'error'); return redirect(url_for('admin_school_assessment_detail',assessment_id=assessment_id))
    if not text or any(not x for x in opts) or correct not in range(4): flash('Question text, four options and a correct answer are required.','error'); return redirect(url_for('admin_school_assessment_detail',assessment_id=assessment_id))
    n=one_scalar(select(func.coalesce(func.max(SchoolQuestion.sort_order),0)+1)
                 .where(SchoolQuestion.assessment_id==assessment_id),1)
    db.session.add(SchoolQuestion(assessment_id=assessment_id,question_text=text,
        instruction=instruction,image_path=image_path,option_a=opts[0],option_b=opts[1],
        option_c=opts[2],option_d=opts[3],correct_option=correct,points=points,sort_order=n))
    db.session.flush()
    _resync_question_count(assessment_id)
    db.session.commit(); audit_log('school_assessment_question_added','school',a['assessment_type'],assessment_id,{'question_number':n}); flash('Question added.','success'); return redirect(url_for('admin_school_assessment_detail',assessment_id=assessment_id))

@app.route('/admin/school/assessments/<int:assessment_id>/questions/<int:question_id>/edit',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_assessment_question_edit(assessment_id,question_id):
    a=obj(SchoolAssessment, assessment_id)
    q=db.session.scalars(select(SchoolQuestion).where(
        SchoolQuestion.id==question_id,
        SchoolQuestion.assessment_id==assessment_id)).first()
    if not a or not q: abort(404)
    perm='school.practice.edit' if a['assessment_type']=='practice' else ('school.examinations.edit' if a['assessment_type']=='examination' else 'school.tests.edit')
    if not admin_has_permission(current_admin()['id'],perm): return admin_access_error(perm)
    if not _school_pair_allowed(current_admin()['id'],a['class_id'],a['subject_id']): return admin_access_error(perm)

    if request.method=='GET':
        return render_template('school_question_form.html',assessment=a,question=q,errors=[])

    text=request.form.get('question_text','').strip()
    instruction=request.form.get('instruction','').strip()
    opts=[request.form.get(f'option_{x}','').strip() for x in 'abcd']
    try:
        correct=int(request.form.get('correct_option','-1'))
        points=max(1,int(request.form.get('points','1') or 1))
    except (TypeError,ValueError):
        correct=-1; points=1
    errors=[]
    image_path=q['image_path']
    if request.form.get('remove_image')=='1':
        image_path=None
    try:
        uploaded=_save_image_upload(request.files.get('image'),'questions',f'school_{assessment_id}')
        if uploaded: image_path=uploaded
    except ValueError as exc:
        errors.append(str(exc))
    if not text: errors.append('Question text is required.')
    if any(not x for x in opts): errors.append('All four options are required.')
    if correct not in range(4): errors.append('Select the correct option.')
    if points<1: errors.append('Points must be at least 1.')
    if errors:
        return render_template('school_question_form.html',assessment=a,question={
            'id':question_id,'question_text':text,'instruction':instruction,'image_path':image_path,
            'option_a':opts[0],'option_b':opts[1],'option_c':opts[2],'option_d':opts[3],
            'correct_option':correct,'points':points
        },errors=errors)

    db.session.execute(sa_update(SchoolQuestion)
        .where(SchoolQuestion.id==question_id,SchoolQuestion.assessment_id==assessment_id)
        .values(question_text=text,instruction=instruction,image_path=image_path,
                option_a=opts[0],option_b=opts[1],option_c=opts[2],option_d=opts[3],
                correct_option=correct,points=points))
    db.session.commit()
    audit_log('school_assessment_question_updated','school',a['assessment_type'],assessment_id,{'question_id':question_id})
    flash(f'Question {question_id} updated.','success')
    return redirect(url_for('admin_school_assessment_detail',assessment_id=assessment_id))

@app.post('/admin/school/assessments/<int:assessment_id>/questions/reorder')
@admin_required
@csrf_protect
def admin_school_assessment_question_reorder(assessment_id):
    a=obj(SchoolAssessment, assessment_id)
    questions=all_rows(select(SchoolQuestion.id)
        .where(SchoolQuestion.assessment_id==assessment_id)
        .order_by(SchoolQuestion.sort_order,SchoolQuestion.id))
    if not a: abort(404)
    perm='school.practice.edit' if a['assessment_type']=='practice' else ('school.examinations.edit' if a['assessment_type']=='examination' else 'school.tests.edit')
    if not admin_has_permission(current_admin()['id'],perm): return admin_access_error(perm)
    if not _school_pair_allowed(current_admin()['id'],a['class_id'],a['subject_id']): return admin_access_error(perm)
    try:
        ids=[int(x) for x in request.form.get('order','').split(',') if x.strip()]
    except (TypeError,ValueError):
        ids=[]
    current_ids=[int(q['id']) for q in questions]
    if set(ids) != set(current_ids) or len(ids) != len(current_ids):
        return jsonify(ok=False,error='Order must contain every question exactly once.'),400
    for position,qid in enumerate(ids,1):
        db.session.execute(sa_update(SchoolQuestion)
            .where(SchoolQuestion.id==qid,SchoolQuestion.assessment_id==assessment_id)
            .values(sort_order=position))
    db.session.commit()
    audit_log('school_assessment_questions_reordered','school',a['assessment_type'],assessment_id,{})
    return jsonify(ok=True)

@app.post('/admin/school/assessments/<int:assessment_id>/questions/<int:question_id>/delete')
@admin_required
@csrf_protect
def admin_school_assessment_question_delete(assessment_id,question_id):
    a=obj(SchoolAssessment, assessment_id)
    q=one(select(SchoolQuestion.id).where(SchoolQuestion.id==question_id,
                                          SchoolQuestion.assessment_id==assessment_id))
    if not a or not q: abort(404)
    perm='school.practice.edit' if a['assessment_type']=='practice' else ('school.examinations.edit' if a['assessment_type']=='examination' else 'school.tests.edit')
    if not admin_has_permission(current_admin()['id'],perm): return admin_access_error(perm)
    if not _school_pair_allowed(current_admin()['id'],a['class_id'],a['subject_id']): return admin_access_error(perm)
    db.session.execute(sa_delete(SchoolQuestion).where(SchoolQuestion.id==question_id))
    db.session.flush()
    _resync_question_count(assessment_id)
    db.session.commit(); audit_log('school_assessment_question_deleted','school',a['assessment_type'],assessment_id,{'question_id':question_id}); flash('Question deleted.','success'); return redirect(url_for('admin_school_assessment_detail',assessment_id=assessment_id))

@app.post('/admin/school/tests/<int:assessment_id>/delete')
@app.post('/admin/school/practice-tests/<int:assessment_id>/delete')
@app.post('/admin/school/examinations/<int:assessment_id>/delete')
@admin_required
@csrf_protect
def admin_school_assessment_delete(assessment_id):
    a=obj(SchoolAssessment,assessment_id)
    if not a: abort(404)
    perm='school.practice.delete' if a.assessment_type=='practice' else ('school.examinations.delete' if a.assessment_type=='examination' else 'school.tests.delete')
    if not admin_has_permission(current_admin()['id'],perm): return admin_access_error(perm)
    if not _school_pair_allowed(current_admin()['id'],a.class_id,a.subject_id): return admin_access_error(perm)
    assessment_type=a.assessment_type
    db.session.delete(a)
    db.session.commit()
    audit_log('school_assessment_deleted','school',assessment_type,assessment_id); flash('Assessment deleted.','success'); return redirect(url_for('admin_school_home'))

@app.route('/admin/school/parents')
@admin_required
def admin_school_parents():
    me=current_admin()
    if not admin_has_permission(me['id'],'parent.view'):
        return admin_access_error('parent.view')
    parents=[_flatten(r,'ParentAccount','child_count') for r in all_rows(
        select(ParentAccount,func.count(ParentStudentLink.id).label('child_count'))
        .outerjoin(ParentStudentLink,and_(ParentStudentLink.parent_id==ParentAccount.id,
                                          ParentStudentLink.active==1))
        .group_by(ParentAccount.id).order_by(ParentAccount.display_name))]
    if not me['admin_type_system']:
        allowed_classes={cid for (cid,) in tuples(
            select(SchoolClass.id).where(SchoolClass.active==1))
            if _school_class_allowed(me['id'],cid)}
        if not allowed_classes:
            parents=[]
        else:
            # One query finds every parent with a child in an authorised class,
            # instead of one query per parent.
            visible={pid for (pid,) in tuples(
                select(ParentStudentLink.parent_id).distinct()
                .join(StudentEnrolment,and_(StudentEnrolment.student_id==ParentStudentLink.student_id,
                                            StudentEnrolment.active==1))
                .where(ParentStudentLink.active==1,
                       StudentEnrolment.class_id.in_(list(allowed_classes))))}
            parents=[p for p in parents if p['id'] in visible]
    return render_template('admin_parents.html',parents=parents)

def _parent_form_context(me):
    """Classes and enrolled students this administrator may link."""
    classes=db.session.scalars(select(SchoolClass).where(SchoolClass.active==1)
        .order_by(SchoolClass.level_order)).all()
    students=all_rows(
        select(Student.id,Student.admission_no,Student.first_name,Student.last_name,
               SchoolClass.id.label('class_id'),SchoolClass.name.label('class_name'))
        .join(StudentEnrolment,and_(StudentEnrolment.student_id==Student.id,
                                    StudentEnrolment.active==1))
        .join(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
        .where(Student.active==1)
        .order_by(SchoolClass.level_order,Student.first_name,Student.last_name))
    if not me['admin_type_system']:
        classes=[c for c in classes if _school_class_allowed(me['id'],c.id)]
        students=[st for st in students if _school_class_allowed(me['id'],st['class_id'])]
    return classes,students


def _new_parent_password():
    return 'PAR-'+secrets.token_urlsafe(8).replace('-','').replace('_','')[:8].upper()


@app.route('/admin/school/parents/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_parent_new():
    me=current_admin()
    if not admin_has_permission(me['id'],'parent.manage'):
        return admin_access_error('parent.manage')
    classes,students=_parent_form_context(me)
    if request.method=='POST':
        display=request.form.get('display_name','').strip()
        email=request.form.get('email','').strip().lower()
        phone=request.form.get('phone','').strip()
        username=request.form.get('username','').strip().lower()
        selected=[int(x) for x in request.form.getlist('student_ids') if x.isdigit()]
        relationship=request.form.get('relationship','').strip()
        errors=[]
        if not display: errors.append('Parent / guardian name is required.')
        if not username: username=(re.sub(r'[^a-z0-9]+','.',display.lower()).strip('.') or 'parent')
        if one_scalar(select(ParentAccount.id).where(ParentAccount.username==username)): errors.append('That parent username is already in use.')
        if email and ('@' not in email or '.' not in email.split('@')[-1]): errors.append('Enter a valid email address.')
        if not selected: errors.append('Link at least one student to this parent account.')
        allowed={st['id'] for st in students}
        if any(sid not in allowed for sid in selected): errors.append('One or more selected students are outside your authorised school scope.')
        if errors:
            return render_template('admin_parent_form.html',students=students,classes=classes,errors=errors,form=request.form)
        password=_new_parent_password()
        now=datetime.now(timezone.utc).isoformat()
        parent=ParentAccount(username=username,display_name=display,email=email or None,
            phone=phone or None,password_hash=generate_password_hash(password),
            active=1,password_must_change=1,created_at=now)
        db.session.add(parent); db.session.flush(); pid=parent.id
        for sid in selected:
            db.session.add(ParentStudentLink(parent_id=pid,student_id=sid,
                relationship=relationship or None,active=1,created_at=now,created_by=me['id']))
        db.session.commit()
        audit_log('parent_account_created','school','parent',pid,{'linked_students':len(selected)})
        return render_template('admin_parent_created.html',parent={'id':pid,'username':username,'display_name':display},password=password)
    return render_template('admin_parent_form.html',students=students,classes=classes,errors=[],form={})

@app.route('/admin/school/parents/<int:pid>/edit',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_parent_edit(pid):
    me=current_admin()
    if not admin_has_permission(me['id'],'parent.manage'): return admin_access_error('parent.manage')
    parent=obj(ParentAccount,pid)
    if not parent: abort(404)
    classes,allowed_students=_parent_form_context(me)
    links=db.session.scalars(select(ParentStudentLink).where(
        ParentStudentLink.parent_id==pid,ParentStudentLink.active==1)).all()
    linked={x.student_id:x.relationship or '' for x in links}
    if request.method=='POST':
        display=request.form.get('display_name','').strip(); email=request.form.get('email','').strip().lower(); phone=request.form.get('phone','').strip(); selected=[int(x) for x in request.form.getlist('student_ids') if x.isdigit()]; relationship=request.form.get('relationship','').strip(); errors=[]
        if not display: errors.append('Parent / guardian name is required.')
        if email and ('@' not in email or '.' not in email.rsplit('@',1)[-1]): errors.append('Enter a valid email address.')
        allowed={x['id'] for x in allowed_students}
        if not selected: errors.append('Link at least one student to this parent account.')
        if any(x not in allowed for x in selected): errors.append('One or more selected students are outside your authorised school scope.')
        if errors: return render_template('admin_parent_form.html',students=allowed_students,classes=classes,errors=errors,form=request.form,editing=parent,linked=linked)
        now=datetime.now(timezone.utc).isoformat()
        parent.display_name=display; parent.email=email or None; parent.phone=phone or None
        # Retire every link, then re-create the selected ones, so an unlinked
        # child stops being visible to this parent.
        db.session.execute(sa_update(ParentStudentLink)
            .where(ParentStudentLink.parent_id==pid).values(active=0))
        for sid in selected:
            db.session.add(ParentStudentLink(parent_id=pid,student_id=sid,
                relationship=relationship or None,active=1,created_at=now,created_by=me['id']))
        db.session.commit()
        audit_log('parent_account_updated','school','parent',pid,{'linked_students':len(selected)}); flash('Parent account updated.','success'); return redirect(url_for('admin_school_parents'))
    form={'display_name':parent.display_name,'email':parent.email or '','phone':parent.phone or '','relationship':next(iter(linked.values()),'')}
    return render_template('admin_parent_form.html',students=allowed_students,classes=classes,errors=[],form=form,editing=parent,linked=linked)

@app.post('/admin/school/parents/<int:pid>/credentials/reset')
@admin_required
@csrf_protect
def admin_school_parent_credentials_reset(pid):
    me=current_admin()
    if not admin_has_permission(me['id'],'parent.manage'): return admin_access_error('parent.manage')
    row=db.session.scalars(select(ParentAccount).where(
        ParentAccount.id==pid,ParentAccount.active==1)).first()
    if not row: abort(404)
    temporary=_new_parent_password()
    username=row.username
    payload={c.key:getattr(row,c.key) for c in row.__mapper__.column_attrs}
    row.password_hash=generate_password_hash(temporary)
    row.password_must_change=1
    db.session.commit()
    audit_log('parent_credentials_reset','authentication','parent',pid,{'username':username,'reset_by':me['username']},True,me); flash('A new temporary parent password was generated. The previous password no longer works.','success'); return render_template('admin_parent_created.html',parent=payload,password=temporary,reset=True)

@app.route('/admin/school/results/manual/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_result_manual_new():
    me=current_admin()
    sessions=db.session.scalars(select(AcademicSession).where(AcademicSession.active==1)
        .order_by(AcademicSession.id.desc())).all()
    classes=db.session.scalars(select(SchoolClass).where(SchoolClass.active==1)
        .order_by(SchoolClass.level_order)).all()
    if not me['admin_type_system']: classes=[c for c in classes if _school_class_allowed(me['id'],c.id)]
    current=_school_current_session(); session_id=request.values.get('session_id',type=int) or (current['id'] if current else 0); term=request.values.get('term','Full Session').strip() or 'Full Session'; class_id=request.values.get('class_id',type=int) or 0; student_id=request.values.get('student_id',type=int) or 0; subject_id=request.values.get('subject_id',type=int) or 0
    subjects=all_rows(select(SchoolSubject.id,SchoolSubject.name,SchoolSubject.code,
                             func.group_concat(ClassSubject.class_id).label('class_ids'))
        .join(ClassSubject,ClassSubject.subject_id==SchoolSubject.id)
        .where(SchoolSubject.active==1)
        .group_by(SchoolSubject.id).order_by(SchoolSubject.name))
    students=all_rows(select(Student.id,Student.admission_no,Student.first_name,
                             Student.middle_name,Student.last_name,
                             SchoolClass.id.label('class_id'),
                             SchoolClass.name.label('class_name'))
        .join(StudentEnrolment,and_(StudentEnrolment.student_id==Student.id,
                                    StudentEnrolment.session_id==session_id,
                                    StudentEnrolment.active==1))
        .join(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
        .where(Student.active==1)
        .order_by(Student.last_name,Student.first_name))
    if not me['admin_type_system']:
        subjects=[x for x in subjects if _school_subject_allowed(me['id'],x['id'])]; students=[x for x in students if _school_class_allowed(me['id'],x['class_id'])]
    if class_id: students=[x for x in students if x['class_id']==class_id]
    if student_id:
        selected_student=next((x for x in students if x['id']==student_id),None)
        if selected_student: class_id=selected_student['class_id']
    if class_id: subjects=[x for x in subjects if str(class_id) in (x['class_ids'] or '').split(',')]

    def existing_rows():
        if not (student_id and subject_id and session_id):
            return []
        return [_flatten(r,'SchoolStudentResult','subject_name') for r in all_rows(
            select(SchoolStudentResult,SchoolSubject.name.label('subject_name'))
            .join(SchoolSubject,SchoolSubject.id==SchoolStudentResult.subject_id)
            .where(SchoolStudentResult.student_id==student_id,
                   SchoolStudentResult.subject_id==subject_id,
                   SchoolStudentResult.session_id==session_id,
                   func.coalesce(SchoolStudentResult.term,'Full Session')==term)
            .order_by(SchoolStudentResult.id.desc()))]

    existing=existing_rows()
    errors=[]
    if request.method=='POST':
        try: class_id=int(request.form.get('class_id')); session_id=int(request.form.get('session_id')); student_id=int(request.form.get('student_id')); subject_id=int(request.form.get('subject_id'))
        except (TypeError,ValueError): class_id=session_id=student_id=subject_id=0
        term=request.form.get('term','Full Session').strip() or 'Full Session'; took=request.form.get('took_test','yes').lower()=='yes'; absence=request.form.get('absence_reason','').strip()
        def num(name):
            raw=request.form.get(name,'').strip(); return float(raw) if raw else None
        try: test_score=num('test_score'); test_max=num('test_max'); exam_score=num('exam_score'); exam_max=num('exam_max')
        except (TypeError,ValueError): test_score=test_max=exam_score=exam_max=None
        student=next((x for x in students if x['id']==student_id),None); subject=next((x for x in subjects if x['id']==subject_id),None)
        if not student: errors.append('Select a student from the selected class.')
        if not subject: errors.append('Select a subject that has been created for the selected class.')
        if not _school_class_allowed(me['id'],class_id): errors.append('The selected class is outside your authorised scope.')
        if not _school_subject_allowed(me['id'],subject_id): errors.append('The selected subject is outside your authorised scope.')
        if not one_scalar(select(ClassSubject.id).where(ClassSubject.class_id==class_id,
                                                        ClassSubject.subject_id==subject_id)):
            errors.append('That subject has not been created for the selected class.')
        if not took:
            if not absence: errors.append('State why the student did not take the test.')
        else:
            if test_score is None or test_max is None or test_max<=0 or test_score<0 or test_score>test_max: errors.append('Enter a valid Test score and maximum score.')
        if exam_score is None or exam_max is None or exam_max<=0 or exam_score<0: errors.append('Enter a valid Exam score and maximum score.')
        exam_reason=request.form.get('exam_override_reason','').strip()
        if exam_score is not None and exam_max is not None and exam_score>exam_max and not exam_reason: errors.append('An Exam score above its selected maximum requires an approved exception reason.')
        if errors:
            return render_template('school_result_manual_form.html',sessions=sessions,classes=classes,subjects=subjects,students=students,existing=existing_rows(),errors=errors,form=request.form)
        now=datetime.now(timezone.utc).isoformat(); actor=me['id']
        components=[('Test',test_score,test_max,None)] if took else [('Test — Absent',None,None,absence)]
        components.append(('Exam',exam_score,exam_max,exam_reason or None))
        for component,score,max_score,reason in components:
            row=db.session.scalars(select(SchoolStudentResult).where(
                SchoolStudentResult.student_id==student_id,
                SchoolStudentResult.subject_id==subject_id,
                SchoolStudentResult.session_id==session_id,
                func.coalesce(SchoolStudentResult.term,'Full Session')==term,
                SchoolStudentResult.component_name==component)
                .order_by(SchoolStudentResult.id.desc()).limit(1)).first()
            if row and row.status in ('approved','released'):
                errors.append(f'{component} already has an approved/released record. Use the authorised correction process.'); break
            if row:
                frm=row.status; rid=row.id
                # Re-entering a mark sends it back to the start of the workflow.
                row.score=score; row.max_score=max_score; row.status='entered'
                row.entered_by=actor; row.override_reason=reason; row.updated_at=now
                row.updated_by=actor; row.verified_by=None; row.approved_by=None
                row.released_at=None
            else:
                created=SchoolStudentResult(student_id=student_id,assessment_id=None,
                    assignment_id=None,subject_id=subject_id,score=score,max_score=max_score,
                    term=term,session_id=session_id,status='entered',created_at=now,
                    source_type='manual',component_name=component,entered_by=actor,
                    override_reason=reason,updated_at=now,updated_by=actor)
                db.session.add(created); db.session.flush(); rid=created.id; frm='draft'
            db.session.add(ResultWorkflowEvent(result_id=rid,from_status=frm,to_status='entered',
                                               actor_admin_id=actor,reason=reason,created_at=now))
        if errors:
            db.session.rollback()
            return render_template('school_result_manual_form.html',sessions=sessions,classes=classes,subjects=subjects,students=students,existing=existing,errors=errors,form=request.form)
        db.session.commit()
        audit_log('school_manual_result_entered','school','result',student_id,{'subject_id':subject_id,'session_id':session_id,'term':term,'took_test':took}); flash('The offline Test/Exam record has been entered and is awaiting verification.','success'); return redirect(url_for('admin_school_results',**{'class':student['class_name']}))
    return render_template('school_result_manual_form.html',sessions=sessions,classes=classes,subjects=subjects,students=students,existing=existing,errors=errors,form=request.args)

def _result_with_context(result_id, with_subject=True):
    """A result row plus the student, class and (optionally) subject names."""
    cols=[SchoolStudentResult,Student.first_name,Student.last_name,
          SchoolClass.id.label('class_id'),SchoolClass.name.label('class_name')]
    extra=('first_name','last_name','class_id','class_name')
    if with_subject:
        cols.append(SchoolSubject.name.label('subject_name'))
        extra=extra+('subject_name',)
    stmt=(select(*cols)
          .join(Student,Student.id==SchoolStudentResult.student_id))
    if with_subject:
        stmt=stmt.join(SchoolSubject,SchoolSubject.id==SchoolStudentResult.subject_id)
    stmt=(stmt.outerjoin(StudentEnrolment,
                         and_(StudentEnrolment.student_id==SchoolStudentResult.student_id,
                              StudentEnrolment.session_id==SchoolStudentResult.session_id,
                              StudentEnrolment.active==1))
              .outerjoin(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
              .where(SchoolStudentResult.id==result_id))
    row=one(stmt)
    return _flatten(row,'SchoolStudentResult',*extra) if row else None


@app.route('/admin/school/results/<int:result_id>/edit',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_result_edit(result_id):
    me=current_admin()
    if not admin_has_permission(me['id'],'school.results.enter'):
        return admin_access_error('school.results.enter')
    row=_result_with_context(result_id)
    if not row: abort(404)
    if not _school_class_allowed(me['id'],row['class_id']): return admin_access_error('school.results.enter')
    if row['status'] in ('approved','released'):
        flash('Approved or released results cannot be edited here. Use the authorised correction process.','error'); return redirect(url_for('admin_school_results',**{'class':row['class_name']}))
    if request.method=='POST':
        try: score=float(request.form.get('score','')); max_score=float(request.form.get('max_score',''))
        except (TypeError,ValueError): score=max_score=-1
        component=request.form.get('component_name','').strip() or row['component_name'] or row.get('assessment_title') or 'Academic Assessment'
        term=request.form.get('term','').strip() or row['term'] or 'Full Session'
        reason=request.form.get('reason','').strip()
        errors=[]
        if max_score<=0: errors.append('Maximum score must be greater than zero.')
        if score<0: errors.append('Score cannot be negative.')
        if score>max_score and not reason: errors.append('A score above the selected maximum requires an authorised exception reason.')
        if errors:
            return render_template('school_result_edit_form.html',result=row,errors=errors,form=request.form)
        old_status=row['status']; new_status='entered' if old_status!='entered' else old_status
        now=datetime.now(timezone.utc).isoformat()
        record=obj(SchoolStudentResult,result_id)
        record.score=score; record.max_score=max_score; record.term=term
        record.component_name=component; record.override_reason=reason or None
        record.status=new_status; record.verified_by=None; record.approved_by=None
        record.released_at=None; record.updated_at=now; record.updated_by=me['id']
        db.session.add(ResultWorkflowEvent(result_id=result_id,from_status=old_status,
            to_status=new_status,actor_admin_id=me['id'],
            reason=reason or 'Result edited',created_at=now))
        db.session.commit()
        audit_log('school_result_edited','school','result',result_id,{'previous_status':old_status,'status':new_status})
        flash('Result updated. It must pass verification again before approval.','success')
        return redirect(url_for('admin_school_results',**{'class':row['class_name']}))
    return render_template('school_result_edit_form.html',result=row,errors=[],form={})

@app.post('/admin/school/results/<int:result_id>/workflow')
@admin_required
@csrf_protect
def admin_school_result_workflow(result_id):
    me=current_admin()
    action=request.form.get('action','').strip().lower()
    required={'verify':'school.results.verify','approve':'school.results.approve','release':'school.results.release'}.get(action)
    if not required or not admin_has_permission(me['id'],required):
        return admin_access_error(required or 'school.results.verify')
    row=_result_with_context(result_id,with_subject=False)
    if not row: abort(404)
    if not _school_class_allowed(me['id'],row['class_id']): return admin_access_error(required)
    transitions={'verify':('entered','verified'),'approve':('verified','approved'),'release':('approved','released')}
    frm,to=transitions[action]
    # The workflow is strictly ordered: entered -> verified -> approved -> released.
    if row['status']!=frm:
        flash(f'This result must be {frm} before it can be {to}.','error'); return redirect(url_for('admin_school_results',**{'class':row['class_name']}))
    reason=request.form.get('reason','').strip()
    now=datetime.now(timezone.utc).isoformat()
    record=obj(SchoolStudentResult,result_id)
    record.status=to
    if action=='verify': record.verified_by=me['id']
    elif action=='approve': record.approved_by=me['id']
    else: record.released_at=now
    record.updated_at=now; record.updated_by=me['id']
    db.session.add(ResultWorkflowEvent(result_id=result_id,from_status=frm,to_status=to,
        actor_admin_id=me['id'],reason=reason or None,created_at=now))
    db.session.commit()
    audit_log(f'school_result_{action}','school','result',result_id,{'from':frm,'to':to})
    flash(f'Result {to}.','success')
    return redirect(url_for('admin_school_results',**{'class':row['class_name']}))

@app.route('/admin/school/results')
@admin_required
def admin_school_results():
    selected_class=request.args.get('class','').strip(); search=request.args.get('q','').strip()
    _release_due_school_results(); db.session.commit()
    current_session=_school_current_session()
    rows=[_flatten(r,'SchoolStudentResult','admission_no','first_name','last_name',
                   'subject_name','class_name') for r in all_rows(
        select(SchoolStudentResult,Student.admission_no,Student.first_name,Student.last_name,
               SchoolSubject.name.label('subject_name'),SchoolClass.name.label('class_name'))
            .select_from(SchoolStudentResult)
            .join(Student,Student.id==SchoolStudentResult.student_id)
            .join(SchoolSubject,SchoolSubject.id==SchoolStudentResult.subject_id)
            .outerjoin(StudentEnrolment,and_(StudentEnrolment.student_id==Student.id,
                                             StudentEnrolment.active==1))
            .outerjoin(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
            .order_by(SchoolStudentResult.id.desc()).limit(250))]
    classes=db.session.scalars(select(SchoolClass).where(SchoolClass.active==1)
                               .order_by(SchoolClass.level_order)).all()
    admin=current_admin()
    if admin and not admin['admin_type_system']:
        classes=[c for c in classes if admin_scope_allows(admin['id'],'class',c['name'])]
        rows=[r for r in rows if r['class_name'] and admin_scope_allows(admin['id'],'class',r['class_name']) and admin_scope_allows(admin['id'],'subject',r['subject_name'])]
    cards=[{'name':c['name'],'count':sum(1 for r in rows if r['class_name']==c['name']),'id':c['id']} for c in classes]
    if selected_class: rows=[r for r in rows if r['class_name']==selected_class]
    if search:
        needle=search.casefold(); rows=[r for r in rows if needle in f"{r['first_name']} {r['last_name']} {r['admission_no']} {r['subject_name']}".casefold()]
    return render_template('school_results.html',results=rows,class_cards=cards,selected_class=selected_class,search=search,current_session=current_session)

@app.post('/admin/school/results/release-schedule')
@admin_required
@csrf_protect
def admin_school_results_release():
    me=current_admin()
    if not admin_has_permission(me['id'],'school.results.release'): return admin_access_error('school.results.release')
    raw=request.form.get('result_release_at','').strip()
    current=_school_current_session()
    if not current:
        flash('Create or activate an academic session before setting a result release date.','error'); return redirect(url_for('admin_school_results'))
    release=None
    if raw:
        try: release=datetime.fromisoformat(raw).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            flash('Enter a valid release date and time.','error'); return redirect(url_for('admin_school_results'))
    db.session.execute(sa_update(AcademicSession)
        .where(AcademicSession.id==current['id']).values(result_release_at=release))
    # A release date in the past takes effect immediately.
    if release and release <= datetime.now(timezone.utc).isoformat():
        db.session.execute(sa_update(SchoolStudentResult)
            .where(SchoolStudentResult.session_id==current['id'],
                   SchoolStudentResult.status=='approved')
            .values(status='released',released_at=datetime.now(timezone.utc).isoformat()))
    db.session.commit()
    audit_log('school_results_release_schedule_changed','school','academic_session',current['id'],{'release_at':release},True,me)
    flash('Result release schedule updated.','success'); return redirect(url_for('admin_school_results'))

@app.route('/admin/school/website')
@admin_required
def admin_school_website():
    me=current_admin()
    if not admin_has_permission(me['id'],'website.view'): return admin_access_error('website.view')
    settings={key:(value or '') for key,value in tuples(
        select(SchoolPublicSetting.setting_key,SchoolPublicSetting.setting_value))}
    pages=db.session.scalars(select(SchoolPublicPage).order_by(SchoolPublicPage.slug)).all()
    news=db.session.scalars(select(SchoolPublicNews)
        .order_by(SchoolPublicNews.id.desc()).limit(50)).all()
    enquiries=db.session.scalars(select(SchoolPublicEnquiry)
        .order_by(SchoolPublicEnquiry.id.desc()).limit(50)).all()
    return render_template('admin_school_website.html',settings=settings,pages=pages,news=news,enquiries=enquiries)

@app.post('/admin/school/website/save')
@admin_required
@csrf_protect
def admin_school_website_save():
    me=current_admin()
    if not admin_has_permission(me['id'],'website.manage'): return admin_access_error('website.manage')
    now=datetime.now(timezone.utc).isoformat()
    keys=['school_name','school_motto','school_tagline','school_phone','school_email','school_address','homepage_headline','homepage_intro','homepage_cta']
    for key in keys:
        stmt=sqlite_insert(SchoolPublicSetting).values(
            setting_key=key,setting_value=request.form.get(key,'').strip(),
            updated_at=now,updated_by=me['id'])
        db.session.execute(stmt.on_conflict_do_update(
            index_elements=['setting_key'],
            set_={'setting_value':stmt.excluded.setting_value,
                  'updated_at':stmt.excluded.updated_at,
                  'updated_by':stmt.excluded.updated_by}))
    for page in db.session.scalars(select(SchoolPublicPage)).all():
        slug=page.slug
        title=request.form.get(f'page_title_{slug}','').strip()
        content=request.form.get(f'page_content_{slug}','').strip()
        published=1 if request.form.get(f'page_published_{slug}')=='1' else 0
        if title:
            page.title=title; page.content=content; page.published=published
            page.updated_at=now; page.updated_by=me['id']
    db.session.commit()
    audit_log('public_website_updated','website','settings',None,{'keys':keys}); flash('Public school website settings saved.','success'); return redirect(url_for('admin_school_website'))

def handle_news_image_upload(req_file):
    if not req_file or not req_file.filename:
        return None
    import os, uuid
    from werkzeug.utils import secure_filename
    ext = os.path.splitext(req_file.filename)[1].lower()
    allowed = ['.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg']
    if ext not in allowed:
        return False
    os.makedirs('static/uploads/news', exist_ok=True)
    filename = f"{uuid.uuid4().hex[:12]}_{secure_filename(req_file.filename)}"
    save_path = os.path.join('static', 'uploads', 'news', filename)
    req_file.save(save_path)
    return f"/static/uploads/news/{filename}"

@app.route('/admin/school/website/news/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_news_new():
    me=current_admin()
    if not admin_has_permission(me['id'],'website.manage'): return admin_access_error('website.manage')
    if request.method=='POST':
        title=request.form.get('title','').strip()
        slug=re.sub(r'[^a-z0-9]+','-',title.lower()).strip('-')
        excerpt=request.form.get('excerpt','').strip()
        body=request.form.get('body','').strip()
        published=1 if request.form.get('published')=='1' else 0
        errors=[]
        if not title: errors.append('News title is required.')
        if not body: errors.append('News body is required.')

        img_file = request.files.get('featured_image')
        image_url = None
        if img_file and img_file.filename:
            uploaded = handle_news_image_upload(img_file)
            if uploaded is False:
                errors.append('Invalid image format. Allowed formats: PNG, JPG, JPEG, WEBP, GIF, SVG.')
            else:
                image_url = uploaded

        if not errors:
            now=datetime.now(timezone.utc).isoformat(); base=slug or 'news'; n=1
            while one_scalar(select(SchoolPublicNews.id).where(SchoolPublicNews.slug==slug)):
                slug=f'{base}-{n}'; n+=1
            db.session.add(SchoolPublicNews(slug=slug,title=title,excerpt=excerpt,body=body,
                published=published,published_at=now if published else None,
                created_at=now,updated_at=now,created_by=me['id'],updated_by=me['id'],
                image_url=image_url))
            db.session.commit()
            audit_log('public_news_created','website','news',slug,{'published':published})
            flash('News article saved.','success')
            return redirect(url_for('admin_school_website'))
        return render_template('admin_school_news_form.html',errors=errors,form=request.form)
    return render_template('admin_school_news_form.html',errors=[],form={})

@app.route('/admin/school/website/news/<int:news_id>/edit',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_news_edit(news_id):
    me=current_admin()
    if not admin_has_permission(me['id'],'website.manage'): return admin_access_error('website.manage')
    row=obj(SchoolPublicNews,news_id)
    if not row: abort(404)
    if request.method=='POST':
        title=request.form.get('title','').strip()
        excerpt=request.form.get('excerpt','').strip()
        body=request.form.get('body','').strip()
        published=1 if request.form.get('published')=='1' else 0
        errors=[]
        if not title: errors.append('News title is required.')
        if not body: errors.append('News body is required.')

        img_file = request.files.get('featured_image')
        image_url = row.image_url
        if img_file and img_file.filename:
            uploaded = handle_news_image_upload(img_file)
            if uploaded is False:
                errors.append('Invalid image format. Allowed formats: PNG, JPG, JPEG, WEBP, GIF, SVG.')
            else:
                image_url = uploaded

        if not errors:
            now=datetime.now(timezone.utc).isoformat()
            row.title=title; row.excerpt=excerpt; row.body=body; row.published=published
            # Stamp the publication date the first time it goes live; clear it
            # when it is withdrawn; otherwise leave the original date alone.
            if published and row.published_at is None: row.published_at=now
            elif not published: row.published_at=None
            row.updated_at=now; row.updated_by=me['id']; row.image_url=image_url
            db.session.commit()
            audit_log('public_news_updated','website','news',news_id,{'published':published})
            flash('News article updated.','success')
            return redirect(url_for('admin_school_website'))
        return render_template('admin_school_news_form.html',errors=errors,form=request.form,editing=row)
    return render_template('admin_school_news_form.html',errors=[],
                           form={c.key:getattr(row,c.key) for c in row.__mapper__.column_attrs},
                           editing=row)

@app.route('/admin/school/website/enquiries')
@admin_required
def admin_school_enquiries():
    me=current_admin()
    if not admin_has_permission(me['id'],'website.view'): return admin_access_error('website.view')
    return redirect(url_for('admin_school_website'))

@app.route('/admin/school/website/enquiries/<int:eid>')
@admin_required
def admin_school_enquiry_detail(eid):
    me=current_admin()
    if not admin_has_permission(me['id'],'website.view'): return admin_access_error('website.view')
    enquiry=obj(SchoolPublicEnquiry,eid)
    if not enquiry: abort(404)
    # Opening a notification also acknowledges the enquiry.
    if enquiry.status=='new':
        enquiry.status='in_progress'; enquiry.handled_by=me['id']
        enquiry.handled_at=datetime.now(timezone.utc).isoformat()
        db.session.commit()
    row={c.key:getattr(enquiry,c.key) for c in enquiry.__mapper__.column_attrs}
    row['handled_by_name']=one_scalar(select(Admin.display_name)
                                      .where(Admin.id==enquiry.handled_by))
    return render_template('admin_school_enquiry_detail.html',enquiry=row)

@app.post('/admin/school/website/enquiries/<int:eid>/status')
@admin_required
@csrf_protect
def admin_school_enquiry_status(eid):
    me=current_admin()
    if not admin_has_permission(me['id'],'website.manage'): return admin_access_error('website.manage')
    status=request.form.get('status','new'); status=status if status in ('new','in_progress','resolved') else 'new'
    db.session.execute(sa_update(SchoolPublicEnquiry).where(SchoolPublicEnquiry.id==eid)
        .values(status=status,handled_by=me['id'],
                handled_at=datetime.now(timezone.utc).isoformat()))
    db.session.commit()
    audit_log('public_enquiry_status_changed','website','enquiry',eid,{'status':status}); return redirect(url_for('admin_school_website'))

@app.route('/admin/administration/notifications')
@admin_required
def admin_notifications():
    """Personal notification inbox. Unlike governance controls, every signed-in administrator can view their own notifications."""
    me=current_admin()
    notifications=db.session.scalars(select(AdminNotification)
        .where(AdminNotification.admin_id==me['id'])
        .order_by(AdminNotification.id.desc()).limit(100)).all()
    unread=one_scalar(select(func.count()).select_from(AdminNotification)
        .where(AdminNotification.admin_id==me['id'],
               AdminNotification.read_at.is_(None)), 0)
    return render_template('admin_notifications.html',notifications=notifications,unread=unread)

@app.route('/admin/administration/controls')
@admin_required
def admin_controls():
    me=current_admin()
    if not admin_has_permission(me['id'],'audit.view'): return admin_access_error('audit.view')
    controls=[_flatten(r,'AdminControlItem','requester_name','requester_username')
              for r in all_rows(
        select(AdminControlItem,Admin.display_name.label('requester_name'),
               Admin.username.label('requester_username'))
        .outerjoin(Admin,Admin.id==AdminControlItem.requested_by)
        .where(AdminControlItem.status=='open')
        .order_by(AdminControlItem.id.desc()).limit(100))]
    notifications=db.session.scalars(select(AdminNotification)
        .where(AdminNotification.admin_id==me['id'])
        .order_by(AdminNotification.id.desc()).limit(50)).all()
    workspace=session.get('admin_workspace') or 'school'
    if workspace=='school':
        subjects=all_rows(
            select(ClassSubject.id,SchoolSubject.id.label('subject_id'),
                   SchoolSubject.name.label('subject_name'),
                   SchoolClass.id.label('class_id'),SchoolClass.name.label('class_name'),
                   ClassSubject.locked,ClassSubject.final_locked,ClassSubject.final_locked_at)
            .join(SchoolSubject,SchoolSubject.id==ClassSubject.subject_id)
            .join(SchoolClass,SchoolClass.id==ClassSubject.class_id)
            .where(SchoolSubject.active==1,SchoolClass.active==1)
            .order_by(SchoolClass.level_order,SchoolSubject.name))
        if not me['admin_type_system']:
            subjects=[r for r in subjects if _school_class_allowed(me['id'],r['class_id'])]

        def pending(status):
            return one_scalar(select(func.count()).select_from(SchoolStudentResult)
                              .where(SchoolStudentResult.status==status), 0)

        return render_template('admin_controls.html',controls=controls,notifications=notifications,subjects=subjects,is_super_admin=is_super_admin(me),workspace='school',pending_results=pending('entered'),pending_approval=pending('verified'),pending_release=pending('approved'))
    # Entrance workspace: show each bank's Super Admin lock state. One query
    # covers every bank rather than one per bank.
    locks={rid:(reason,locked_at) for rid,reason,locked_at in tuples(
        select(AdminResourceLock.resource_id,AdminResourceLock.reason,
               AdminResourceLock.locked_at)
        .where(AdminResourceLock.resource_type=='bank',
               AdminResourceLock.unlocked_at.is_(None)))}
    banks=[]
    for b in load_banks().values():
        lock=locks.get(b['id'])
        banks.append({'id':b['id'],'name':b.get('name') or b['id'],
                      'level':b.get('level') or '—','locked':bool(lock),
                      'lock_reason':lock[0] if lock else None})
    return render_template('admin_controls.html',controls=controls,notifications=notifications,banks=banks,subjects=[],is_super_admin=is_super_admin(me),workspace='entrance',pending_results=0,pending_approval=0,pending_release=0)

@app.post('/admin/administration/notifications/<int:nid>/read')
@admin_required
@csrf_protect
def admin_notification_read(nid):
    me=current_admin()
    # The admin_id predicate keeps one administrator from marking another's.
    db.session.execute(sa_update(AdminNotification)
        .where(AdminNotification.id==nid,AdminNotification.admin_id==me['id'])
        .values(read_at=datetime.now(timezone.utc).isoformat()))
    db.session.commit()
    return redirect(request.referrer or url_for('admin_controls'))

@app.get('/admin/administration/notifications/<int:nid>/open')
@admin_required
def admin_notification_open(nid):
    me=current_admin()
    action_url=one_scalar(select(AdminNotification.action_url)
        .where(AdminNotification.id==nid,AdminNotification.admin_id==me['id']))
    db.session.execute(sa_update(AdminNotification)
        .where(AdminNotification.id==nid,AdminNotification.admin_id==me['id'])
        .values(read_at=datetime.now(timezone.utc).isoformat()))
    db.session.commit()
    return redirect(action_url or url_for('admin_controls'))

@app.post('/admin/administration/controls/<int:cid>/resolve')
@admin_required
@csrf_protect
def admin_control_resolve(cid):
    me=current_admin()
    if not is_super_admin(me): return admin_access_error('Super Admin control')
    db.session.execute(sa_update(AdminControlItem).where(AdminControlItem.id==cid)
        .values(status='resolved',resolved_by=me['id'],
                resolved_at=datetime.now(timezone.utc).isoformat()))
    db.session.commit(); audit_log('control_item_resolved','administration','control',cid,{},True,me); flash('Review item marked as resolved.','success'); return redirect(request.referrer or url_for('admin_controls'))

@app.post('/admin/administration/resources/<resource_type>/<path:resource_id>/lock')
@admin_required
@csrf_protect
def admin_lock_resource(resource_type,resource_id):
    me=current_admin()
    if not is_super_admin(me): return admin_access_error('Super Admin control')
    if resource_type not in ('bank','examination'): abort(404)
    if resource_type=='bank' and not bank(resource_id): abort(404)
    reason=request.form.get('reason','Locked by Super Admin pending review.').strip() or 'Locked by Super Admin pending review.'
    _lock_resource(resource_type,resource_id,reason,me['id'])
    _create_control_item('Resource locked',reason,'governance',resource_type,resource_id,me['id'])
    audit_log('resource_locked','administration',resource_type,resource_id,{'reason':reason},True,me)
    flash('The item has been locked. Staff can still view it where permitted, but changes are blocked.','success')
    return redirect(request.referrer or url_for('admin_controls'))

@app.post('/admin/administration/resources/<resource_type>/<path:resource_id>/unlock')
@admin_required
@csrf_protect
def admin_unlock_resource(resource_type,resource_id):
    me=current_admin()
    if not is_super_admin(me): return admin_access_error('Super Admin control')
    if resource_type not in ('bank','examination'): abort(404)
    _unlock_resource(resource_type,resource_id,me['id'])
    db.session.execute(sa_update(AdminControlItem)
        .where(AdminControlItem.target_type==resource_type,
               AdminControlItem.target_id==str(resource_id),
               AdminControlItem.status=='open',AdminControlItem.category=='governance')
        .values(status='resolved',resolved_by=me['id'],
                resolved_at=datetime.now(timezone.utc).isoformat()))
    db.session.commit()
    audit_log('resource_unlocked','administration',resource_type,resource_id,{},True,me)
    flash('The item has been unlocked.','success')
    return redirect(request.referrer or url_for('admin_controls'))

@app.route('/admin/administration')
@admin_required
def admin_administration():
    me=current_admin()
    def _count(model, *conditions):
        return one_scalar(select(func.count()).select_from(model).where(*conditions),0)
    counts={
        'admins':_count(Admin),
        'active_admins':_count(Admin,Admin.active==1),
        'roles':_count(AdminType,AdminType.active==1,AdminType.is_system==0,
                       AdminType.name!='Ordinary Admin'),
        'notifications':_count(AdminNotification,AdminNotification.admin_id==me['id'],
                               AdminNotification.read_at.is_(None)),
        'controls':_count(AdminControlItem,AdminControlItem.status=='open'),
    }
    return render_template('admin_administration.html',counts=counts,admin=me,is_super_admin=is_super_admin(me))

@app.route('/admin/administration/admins')
@admin_required
def admin_accounts():
    me=current_admin()
    if not admin_has_permission(me['id'],'admins.view'): return admin_access_error('admins.view')
    # A system Super Admin is unrestricted, so its boundary count reads as zero
    # rather than as however many stale scope rows may exist.
    scope_count=sa.case((AdminType.is_system==1,0),
        else_=select(func.count()).select_from(AdminScope)
              .where(AdminScope.admin_id==Admin.id).scalar_subquery()).label('scope_count')
    direct_permission_count=(select(func.count()).select_from(AdminPermission)
        .where(AdminPermission.admin_id==Admin.id).scalar_subquery()
        .label('direct_permission_count'))
    role_names=(select(func.group_concat(AdminType.name,'|'))
        .select_from(AdminRoleAssignment)
        .join(AdminType,AdminType.id==AdminRoleAssignment.admin_type_id)
        .where(AdminRoleAssignment.admin_id==Admin.id,AdminType.active==1)
        .scalar_subquery().label('role_names'))
    rows=all_rows(select(Admin.id,Admin.username,Admin.display_name,Admin.active,
                         Admin.created_at,Admin.last_login_at,Admin.email,Admin.phone,
                         Admin.whatsapp,Admin.photo_path,
                         AdminType.name.label('admin_type_name'),
                         scope_count,direct_permission_count,role_names)
        .join(AdminType,AdminType.id==Admin.admin_type_id)
        .order_by(Admin.id))
    return render_template('admin_accounts.html',admins=rows)

@app.route('/admin/administration/admins/new',methods=['GET','POST'])
@admin_required
def admin_account_new():
    me=current_admin()
    if not admin_has_permission(me['id'],'admins.create'): return admin_access_error('admins.create')
    roles=_admin_role_options()
    perms=db.session.scalars(select(Permission)
        .order_by(Permission.module,Permission.name)).all() if is_super_admin(me) else []
    banks=sorted(load_banks().values(),key=lambda b:str(b.get('name') or b.get('id') or '').lower())
    if not me['admin_type_system']:
        banks=[b for b in banks if admin_scope_allows(me['id'],'bank',b['id'])]
    classes=all_rows(select(SchoolClass.name).where(SchoolClass.active==1)
        .order_by(SchoolClass.level_order,SchoolClass.name))
    subjects=all_rows(select(SchoolSubject.name).where(SchoolSubject.active==1)
        .order_by(SchoolSubject.name))
    sessions=all_rows(select(AcademicSession.name).where(AcademicSession.active==1)
        .order_by(AcademicSession.name.desc()))
    if request.method=='POST':
        if not csrf_check_request(): abort(403,description='Invalid or missing CSRF token.')
        username=request.form.get('username','').strip().lower(); display=request.form.get('display_name','').strip()
        role_ids=[int(x) for x in request.form.getlist('admin_type_ids') if x.isdigit()]
        contact=_admin_contact_fields(request.form)
        scope_type=request.form.get('scope_type','global').strip() or 'global'
        scope_values=request.form.getlist('scope_value') or [request.form.get('scope_value','*').strip() or '*']
        selected_scope_types=[x.strip() for x in request.form.getlist('scope_types') if x.strip()]
        if not selected_scope_types and scope_type!='global': selected_scope_types=[scope_type]
        scope_groups={}
        for st in selected_scope_types:
            vals=[v.strip() for v in request.form.getlist(f'scope_values_{st}') if v.strip()]
            if vals: scope_groups[st]=vals
        selected=[int(x) for x in request.form.getlist('permissions') if x.isdigit()] if is_super_admin(me) else []
        errors=[]
        # The Super Admin role is never assignable through this form.
        protected_super_admin=any(r['id']==rid and r['name']=='Super Admin'
                                  for r in roles for rid in role_ids)
        if not username or not username.replace('.','').replace('_','').replace('-','').isalnum(): errors.append('Username must contain letters, numbers, dots, hyphens or underscores.')
        if not display: errors.append('Display name is required.')
        if protected_super_admin:
            pass
        elif not role_ids or not all(any(r['id']==rid for r in roles) for rid in role_ids): errors.append('Select at least one valid staff job role.')
        elif not is_super_admin(me) and not admin_can_delegate_roles(me['id'],role_ids): errors.append('You can only assign job roles whose permissions are already within your own authorised capabilities.')
        errors += _validate_admin_contact_fields(contact)
        valid_scope={'academic_session','class','subject','bank'}
        if scope_type not in valid_scope|{'global'}: errors.append('Invalid access boundary.')
        if not selected_scope_types:
            scope_groups={}
        allowed_values={
            'academic_session':{r['name'] for r in sessions},
            'class':{r['name'] for r in classes},
            'subject':{r['name'] for r in subjects},
            'bank':{b['id'] for b in banks},
        }
        if not is_super_admin(me):
            for st,vals in list(scope_groups.items()):
                vals=[v for v in vals if admin_scope_allows(me['id'],st,v)]
                scope_groups[st]=vals
        for st,vals in list(scope_groups.items()):
            vals=[v for v in vals if v in allowed_values.get(st,set())]
            scope_groups[st]=vals
            if not vals: errors.append(f'Select at least one valid {st.replace("_"," ")} restriction value.')
        form=dict(request.form); form['admin_type_ids']=role_ids; form.update(contact); form['scope_groups']=scope_groups
        if errors:
            return render_template('admin_account_form.html',roles=roles,errors=errors,form=form,mode='new',permissions=perms,show_advanced=is_super_admin(me),banks=banks,classes=classes,subjects=subjects,sessions=sessions,selected_role_ids=set(role_ids),selected_scope_values=scope_values)
        now=datetime.now(timezone.utc).isoformat()
        try:
            primary_role=role_ids[0]
            temporary_password=_new_admin_password()
            created=Admin(username=username,display_name=display,
                password_hash=generate_password_hash(temporary_password),
                admin_type_id=primary_role,active=1,created_at=now,created_by=me['id'],
                email=contact['email'] or None,phone=contact['phone'] or None,
                whatsapp=contact['whatsapp'] or None,photo_path=None,
                password_must_change=1)
            db.session.add(created); db.session.flush(); aid=created.id
            photo=request.files.get('photo')
            if photo and photo.filename:
                created.photo_path=_save_image_upload(photo,'admins',f'admin_{username}')
            _sync_admin_roles(aid,role_ids,me['id'],now)
            for st,vals in scope_groups.items():
                for value in vals:
                    _ignore_insert(AdminScope, [{'admin_id':aid,'scope_type':st,
                                                 'scope_value':value,'created_at':now,
                                                 'granted_by':me['id']}])
            for pid in selected:
                _ignore_insert(AdminPermission, [{'admin_id':aid,'permission_id':pid,
                                                  'granted_at':now,'granted_by':me['id']}])
            db.session.commit()
        except (sa.exc.IntegrityError,ValueError) as exc:
            db.session.rollback()
            msg='That username is already in use.' if isinstance(exc,sa.exc.IntegrityError) else str(exc)
            errors=[msg]
            return render_template('admin_account_form.html',roles=roles,errors=errors,form=form,mode='new',permissions=perms,show_advanced=is_super_admin(me),banks=banks,classes=classes,subjects=subjects,sessions=sessions,selected_role_ids=set(role_ids),selected_scope_values=scope_values)
        audit_log('admin_created','administration','admin',aid,{'username':username,'role_ids':role_ids,'access_boundaries':[_admin_scope_label(st,v) for st,vals in scope_groups.items() for v in vals],'direct_permissions':selected})
        flash('Staff administrator created successfully.','success'); return render_template('admin_credentials.html',admin={'id':aid,'display_name':display,'username':username,'email':contact['email'],'phone':contact['phone'],'whatsapp':contact['whatsapp']},temporary_password=temporary_password)
    return render_template('admin_account_form.html',roles=roles,errors=[],form={},mode='new',permissions=perms,show_advanced=is_super_admin(me),banks=banks,classes=classes,subjects=subjects,sessions=sessions,selected_role_ids=set(),selected_scope_values=['*'])

@app.route('/admin/administration/admins/<int:aid>/edit',methods=['GET','POST'])
@admin_required
def admin_account_edit(aid):
    me=current_admin()
    if not admin_has_permission(me['id'],'admins.edit'): return admin_access_error('admins.edit')
    row=db.session.scalars(select(Admin).join(AdminType,AdminType.id==Admin.admin_type_id)
                           .where(Admin.id==aid)).first()
    roles=list(_admin_role_options())
    perms=db.session.scalars(select(Permission).order_by(Permission.module,Permission.name)).all()
    direct=set(db.session.scalars(select(AdminPermission.permission_id)
                                  .where(AdminPermission.admin_id==aid)).all())
    assigned=set(db.session.scalars(select(AdminRoleAssignment.admin_type_id)
                                    .where(AdminRoleAssignment.admin_id==aid)).all())
    scopes=db.session.scalars(select(AdminScope).where(AdminScope.admin_id==aid)
                              .order_by(AdminScope.id)).all()
    banks=sorted(load_banks().values(),key=lambda b:str(b.get('name') or b.get('id') or '').lower())
    banks=[b for b in banks if me['admin_type_system'] or admin_scope_allows(me['id'],'bank',b['id'])]
    classes=all_rows(select(SchoolClass.name).where(SchoolClass.active==1)
                     .order_by(SchoolClass.level_order,SchoolClass.name))
    subjects=all_rows(select(SchoolSubject.name).where(SchoolSubject.active==1)
                      .order_by(SchoolSubject.name))
    sessions=all_rows(select(AcademicSession.name).where(AcademicSession.active==1)
                      .order_by(AcademicSession.name.desc()))
    if not row: abort(404)
    protected_super_admin = bool(row['admin_type_name'] == 'Super Admin')
    # Super Admin is a system authority, not an ordinary scoped staff account.
    # A Super Admin may manage the profile of a Super Admin account, but the
    # protected system role itself cannot be replaced or narrowed by this form.
    if not assigned: assigned={row['admin_type_id']}
    if not any(r['id']==row['admin_type_id'] for r in roles):
        legacy_role=obj(AdminType, row['admin_type_id'])
        if legacy_role: roles.append(legacy_role)
    scope_type=scopes[0]['scope_type'] if scopes else 'global'; scope_values=[r['scope_value'] for r in scopes] or ['*']
    scope_groups={};
    for r in scopes: scope_groups.setdefault(r['scope_type'],[]).append(r['scope_value'])
    form={'username':row['username'],'display_name':row['display_name'],'email':row['email'] or '','phone':row['phone'] or '','whatsapp':row['whatsapp'] or '','scope_type':scope_type,'scope_value':scope_values,'scope_groups':scope_groups}
    if request.method=='POST':
        if not csrf_check_request(): abort(403,description='Invalid or missing CSRF token.')
        display=request.form.get('display_name','').strip(); role_ids=[int(x) for x in request.form.getlist('admin_type_ids') if x.isdigit()]; contact=_admin_contact_fields(request.form); scope_type=request.form.get('scope_type','global').strip() or 'global'; scope_values=request.form.getlist('scope_value') or [request.form.get('scope_value','*').strip() or '*']; errors=[]
        selected_scope_types=[x.strip() for x in request.form.getlist('scope_types') if x.strip()]
        if not selected_scope_types and scope_type!='global': selected_scope_types=[scope_type]
        scope_groups={}
        for st in selected_scope_types:
            vals=[v.strip() for v in request.form.getlist(f'scope_values_{st}') if v.strip()]
            if vals: scope_groups[st]=vals
        if protected_super_admin:
            # System Super Admin accounts are always unrestricted. Ignore any
            # stale/posted boundary or role selections rather than allowing a
            # form submission to accidentally narrow the system authority.
            role_ids=[row['admin_type_id']]
            scope_groups={}
        if not display: errors.append('Display name is required.')
        if protected_super_admin:
            # The system role is preserved automatically and is intentionally
            # absent from the ordinary staff-role selector.
            role_ids=[row['admin_type_id']]
        elif not role_ids or not all(any(r['id']==rid for r in roles) for rid in role_ids): errors.append('Select at least one valid staff job role.')
        elif not is_super_admin(me) and not admin_can_delegate_roles(me['id'],role_ids): errors.append('You can only assign job roles whose permissions are already within your own authorised capabilities.')
        errors += _validate_admin_contact_fields(contact)
        if not protected_super_admin and scope_type not in ('global','academic_session','class','subject','bank'): errors.append('Invalid access boundary.')
        allowed_values={'academic_session':{r['name'] for r in sessions},'class':{r['name'] for r in classes},'subject':{r['name'] for r in subjects},'bank':{b['id'] for b in banks}}
        if not selected_scope_types or protected_super_admin: scope_groups={}
        for st,vals in list(scope_groups.items()):
            if not is_super_admin(me): vals=[v for v in vals if admin_scope_allows(me['id'],st,v)]
            vals=[v for v in vals if v in allowed_values.get(st,set())]
            scope_groups[st]=vals
            if not vals: errors.append(f'Select at least one valid {st.replace("_"," ")} restriction value.')
        form=dict(request.form); form['username']=row['username']; form['admin_type_ids']=role_ids; form.update(contact); form['scope_groups']=scope_groups
        if errors: return render_template('admin_account_form.html',roles=roles,permissions=perms,errors=errors,form=form,mode='edit',editing=row,show_advanced=is_super_admin(me),direct_permissions=direct,banks=banks,classes=classes,subjects=subjects,sessions=sessions,selected_role_ids=set(role_ids),selected_scope_values=scope_values,protected_super_admin=protected_super_admin)
        now=datetime.now(timezone.utc).isoformat()
        db.session.execute(sa_update(Admin).where(Admin.id==aid).values(
            display_name=display,admin_type_id=role_ids[0],email=contact['email'] or None,
            phone=contact['phone'] or None,whatsapp=contact['whatsapp'] or None))
        photo=request.files.get('photo')
        if photo and photo.filename:
            try: photo_path=_save_image_upload(photo,'admins',f'admin_{row["username"]}')
            except ValueError as exc:
                db.session.rollback(); return render_template('admin_account_form.html',roles=roles,permissions=perms,errors=[str(exc)],form=form,mode='edit',editing=row,show_advanced=is_super_admin(me),direct_permissions=direct,banks=banks,classes=classes,subjects=subjects,sessions=sessions,selected_role_ids=set(role_ids),selected_scope_values=scope_values,protected_super_admin=protected_super_admin)
            db.session.execute(sa_update(Admin).where(Admin.id==aid).values(photo_path=photo_path))
        _sync_admin_roles(aid,role_ids,me['id'],now)
        db.session.execute(sa_delete(AdminScope).where(AdminScope.admin_id==aid))
        db.session.add_all([AdminScope(admin_id=aid,scope_type=st,scope_value=value,
                                       created_at=now,granted_by=me['id'])
                            for st,vals in scope_groups.items() for value in vals])
        if is_super_admin(me):
            db.session.execute(sa_delete(AdminPermission).where(AdminPermission.admin_id==aid))
            db.session.flush()
            _ignore_insert(AdminPermission,[{'admin_id':aid,'permission_id':pid,
                                             'granted_at':now,'granted_by':me['id']}
                                            for pid in [int(x) for x in request.form.getlist('permissions') if x.isdigit()]])
        db.session.commit(); audit_log('admin_access_updated','administration','admin',aid,{'role_ids':role_ids,'access_boundaries':[_admin_scope_label(st,v) for st,vals in scope_groups.items() for v in vals]}); flash('Administrator profile and access updated.','success'); return redirect(url_for('admin_accounts'))
    return render_template('admin_account_form.html',roles=roles,permissions=perms,errors=[],form=form,mode='edit',editing=row,show_advanced=is_super_admin(me),direct_permissions=direct,banks=banks,classes=classes,subjects=subjects,sessions=sessions,selected_role_ids=assigned,selected_scope_values=scope_values,protected_super_admin=protected_super_admin)

@app.route('/admin/administration/admins/<int:aid>/credentials/reset',methods=['POST'])
@admin_required
@csrf_protect
def admin_account_credentials_reset(aid):
    me=current_admin()
    if not admin_has_permission(me['id'],'admins.edit'): return admin_access_error('admins.edit')
    row=db.session.scalars(select(Admin).join(AdminType,AdminType.id==Admin.admin_type_id)
                           .where(Admin.id==aid,Admin.active==1)).first()
    if not row:
        abort(404)
    temporary_password=_new_admin_password()
    db.session.execute(sa_update(Admin).where(Admin.id==aid).values(
        password_hash=generate_password_hash(temporary_password),password_must_change=1))
    db.session.commit()
    audit_log('admin_credentials_reset','authentication','admin',aid,{'username':row['username'],'reset_by':me['username']},True,me)
    _notify_super_admins('Administrator login credentials reset',f'Login credentials for {row["username"]} were regenerated.','warning',url_for('admin_controls'),me['id'])
    flash('A new temporary password was generated. The previous password no longer works.','success')
    return render_template('admin_credentials.html',admin=dict(row),temporary_password=temporary_password,reset=True)

@app.route('/admin/administration/admins/<int:aid>/toggle',methods=['POST'])
@admin_required
@csrf_protect
def admin_account_toggle(aid):
    me=current_admin()
    if not admin_has_permission(me['id'],'admins.deactivate'): return admin_access_error('admins.deactivate')
    if aid==me['id']: flash('You cannot deactivate your own administrator account.','error'); return redirect(url_for('admin_accounts'))
    row=one(select(Admin.id,Admin.username,Admin.active).where(Admin.id==aid))
    if not row: abort(404)
    new=0 if row['active'] else 1
    db.session.execute(sa_update(Admin).where(Admin.id==aid).values(active=new))
    db.session.commit(); audit_log('admin_status_changed','administration','admin',aid,{'username':row['username'],'active':new}); _notify_super_admins('Administrator access status changed',f'{row["username"]} was {"activated" if new else "suspended"}.','warning',url_for('admin_controls'),me['id']); flash('Administrator '+('activated.' if new else 'suspended.')+' Super Admin has been notified.','success'); return redirect(url_for('admin_accounts'))

@app.route('/admin/administration/messages')
@admin_required
def admin_messages():
    me=current_admin()
    contacts=all_rows(_contact_columns()
        .where(Admin.active==1,Admin.id!=me['id']).order_by(Admin.display_name))
    selected_id=request.args.get('with',type=int)
    selected=one(_contact_columns().where(Admin.active==1,Admin.id==selected_id,
                                    Admin.id!=me['id'])) if selected_id else None
    thread=[]
    if selected:
        thread=[_flatten(r,'AdminMessage','sender_name') for r in all_rows(
            select(AdminMessage,Admin.display_name.label('sender_name'))
                .join(Admin,Admin.id==AdminMessage.sender_admin_id)
                .where(_thread_between(me['id'],selected_id))
                .order_by(AdminMessage.sent_at.asc()))]
        db.session.execute(sa_update(AdminMessage)
            .where(AdminMessage.sender_admin_id==selected_id,
                   AdminMessage.recipient_admin_id==me['id'],
                   AdminMessage.read_at.is_(None))
            .values(read_at=datetime.now(timezone.utc).isoformat()))
        db.session.commit()
    unread=_unread_admin_messages(me['id'])
    unread_map=dict(tuples(select(AdminMessage.sender_admin_id,func.count())
        .where(AdminMessage.recipient_admin_id==me['id'],AdminMessage.read_at.is_(None))
        .group_by(AdminMessage.sender_admin_id)))
    return render_template('admin_messages.html',contacts=contacts,selected=selected,thread=thread,unread_messages=unread,unread_map=unread_map)

@app.get('/admin/administration/messages/unread-state')
@admin_required
def admin_messages_unread_state():
    """Lightweight live-message endpoint used by the admin UI polling layer.

    This deliberately uses ordinary HTTP polling rather than introducing a new
    realtime server dependency. It works reliably on the existing Flask/local
    network deployment and keeps the existing messaging architecture intact.
    """
    me=current_admin()
    selected_id=request.args.get('with',type=int)
    Sender=sa.orm.aliased(Admin)
    unread=one_scalar(select(func.count()).select_from(AdminMessage)
        .where(AdminMessage.recipient_admin_id==me['id'],
               AdminMessage.read_at.is_(None)), 0)
    summaries=[dict(r) for r in all_rows(
        select(AdminMessage.id,AdminMessage.body,AdminMessage.sent_at,
               Sender.id.label('sender_id'),Sender.display_name.label('sender_name'),
               Sender.username.label('sender_username'),
               Sender.photo_path.label('sender_photo'))
        .join(Sender,Sender.id==AdminMessage.sender_admin_id)
        .where(AdminMessage.recipient_admin_id==me['id'],
               AdminMessage.read_at.is_(None))
        .order_by(AdminMessage.sent_at.desc(),AdminMessage.id.desc()).limit(8))]
    payload={'unread_messages':unread,'summaries':summaries}
    if selected_id and selected_id != me['id']:
        contact=one(select(Admin.id,Admin.display_name,Admin.username,Admin.email,
                           Admin.phone,Admin.whatsapp,Admin.photo_path)
                    .where(Admin.id==selected_id,Admin.active==1,Admin.id!=me['id']))
        if contact:
            thread=all_rows(
                select(AdminMessage.id,AdminMessage.sender_admin_id,
                       AdminMessage.recipient_admin_id,AdminMessage.body,
                       AdminMessage.sent_at,AdminMessage.attachment_path,
                       AdminMessage.attachment_type,AdminMessage.attachment_name,
                       Sender.display_name.label('sender_name'))
                .join(Sender,Sender.id==AdminMessage.sender_admin_id)
                .where(or_(and_(AdminMessage.sender_admin_id==me['id'],
                                AdminMessage.recipient_admin_id==selected_id),
                           and_(AdminMessage.sender_admin_id==selected_id,
                                AdminMessage.recipient_admin_id==me['id'])))
                .order_by(AdminMessage.sent_at.asc(),AdminMessage.id.asc()))
            payload['thread']=[dict(r) for r in thread]
    response=jsonify(payload)
    response.headers['Cache-Control']='no-store, no-cache, must-revalidate, max-age=0'
    return response

@app.post('/admin/administration/messages/send')
@admin_required
@csrf_protect
def admin_message_send():
    me=current_admin()
    rid=request.form.get('recipient_id',type=int)
    body=request.form.get('body','').strip()
    file_obj=request.files.get('attachment')

    has_file=bool(file_obj and file_obj.filename)

    if not rid or rid==me['id']:
        flash('Choose a recipient.','error')
        return redirect(url_for('admin_messages'))

    if not body and not has_file:
        flash('Enter a message or attach a file.','error')
        return redirect(url_for('admin_messages', **{'with': rid}))

    if len(body)>5000:
        flash('Message is too long. Please keep messages under 5,000 characters.','error')
        return redirect(url_for('admin_messages', **{'with': rid}))

    attachment_path=None
    attachment_type=None
    attachment_name=None
    saved_file=None

    allowed_extensions={
        '.jpg','.jpeg','.png','.gif','.webp',
        '.pdf','.doc','.docx','.txt','.xls','.xlsx'
    }

    if has_file:
        original_name=secure_filename(file_obj.filename or '')

        if not original_name:
            flash('The selected attachment has an invalid filename.','error')
            return redirect(url_for('admin_messages', **{'with': rid}))

        fext=os.path.splitext(original_name)[1].lower()

        if fext not in allowed_extensions:
            flash('That file type is not supported. Please attach an image, PDF, Word, text or Excel file.','error')
            return redirect(url_for('admin_messages', **{'with': rid}))

        attachment_name=original_name

        if fext in {'.jpg','.jpeg','.png','.gif','.webp'}:
            attachment_type='image'
        elif fext=='.pdf':
            attachment_type='pdf'
        elif fext in {'.doc','.docx'}:
            attachment_type='doc'
        elif fext in {'.xls','.xlsx'}:
            attachment_type='spreadsheet'
        else:
            attachment_type='file'

        uname=f"{uuid.uuid4().hex}{fext}"
        udir=os.path.join(app.static_folder,'uploads','messages')
        os.makedirs(udir,exist_ok=True)

        saved_file=os.path.join(udir,uname)
        file_obj.save(saved_file)
        attachment_path=f"uploads/messages/{uname}"

    def discard_upload():
        """Do not leave an orphaned file behind when the send fails."""
        if saved_file and os.path.exists(saved_file):
            try:
                os.remove(saved_file)
            except OSError:
                pass

    # SQLite serialises writers, so a brief lock is retried rather than failed.
    # The session owns the transaction; there is no explicit BEGIN IMMEDIATE.
    for attempt_no in range(4):
        try:
            recipient=one(select(Admin.id,Admin.active).where(Admin.id==rid))

            if not recipient or not recipient['active']:
                discard_upload()
                flash('That administrator is no longer available.','error')
                return redirect(url_for('admin_messages'))

            db.session.add(AdminMessage(
                sender_admin_id=me['id'],
                recipient_admin_id=rid,
                body=body,
                sent_at=datetime.now(timezone.utc).isoformat(),
                attachment_path=attachment_path,
                attachment_type=attachment_type,
                attachment_name=attachment_name))
            db.session.commit()

            return redirect(url_for('admin_messages', **{'with': rid}))

        except sa.exc.OperationalError as exc:
            db.session.rollback()

            if 'locked' not in str(exc).lower() or attempt_no==3:
                discard_upload()
                flash('The message could not be sent because the database is temporarily busy. Please try again.','error')
                return redirect(url_for('admin_messages', **{'with': rid}))

            time.sleep(0.15*(attempt_no+1))

        except Exception:
            db.session.rollback()
            discard_upload()
            flash('The message could not be sent. Please try again.','error')
            return redirect(url_for('admin_messages', **{'with': rid}))

@app.get('/admin/administration/messages/attachment/<int:message_id>')
@admin_required
def admin_message_attachment(message_id):
    me=current_admin()
    row=one(select(AdminMessage.id,AdminMessage.sender_admin_id,
                   AdminMessage.recipient_admin_id,AdminMessage.attachment_path,
                   AdminMessage.attachment_name,AdminMessage.attachment_type)
            .where(AdminMessage.id==message_id))

    if not row or not row['attachment_path']:
        abort(404)

    # Only the two parties to the message may fetch its attachment.
    if me['id'] not in (row['sender_admin_id'],row['recipient_admin_id']):
        abort(403)

    filename=os.path.basename(row['attachment_path'])
    directory=os.path.join(app.static_folder,'uploads','messages')
    full_path=os.path.join(directory,filename)

    if not filename or not os.path.isfile(full_path):
        abort(404)

    return send_from_directory(
        directory,
        filename,
        as_attachment=False,
        download_name=row['attachment_name'] or filename
    )

@app.route('/admin/administration/roles')
@admin_required
def admin_roles():
    me=current_admin()
    if not admin_has_permission(me['id'],'roles.view'): return admin_access_error('roles.view')
    roles=db.session.scalars(select(AdminType)
        .where(AdminType.active==1,AdminType.is_system==0,AdminType.name!='Ordinary Admin')
        .order_by(AdminType.name)).all()
    # One query for every role's permissions instead of one query per role.
    role_perms={r.id:[] for r in roles}
    for type_id,perm in tuples(
            select(AdminTypePermission.admin_type_id,Permission)
                .join(Permission,Permission.id==AdminTypePermission.permission_id)
                .where(AdminTypePermission.admin_type_id.in_([r.id for r in roles]))
                .order_by(Permission.module,Permission.name)):
        role_perms[type_id].append(perm)
    return render_template('admin_roles.html',roles=roles,role_perms=role_perms)

@app.route('/admin/administration/roles/new',methods=['GET','POST'])
@admin_required
def admin_role_new():
    me=current_admin()
    if not is_super_admin(me) or not admin_has_permission(me['id'],'roles.create'): return admin_access_error('Super Admin role management')
    perms=db.session.scalars(select(Permission).order_by(Permission.module,Permission.name)).all()
    if request.method=='POST':
        if not csrf_check_request(): abort(403,description='Invalid or missing CSRF token.')
        name=request.form.get('name','').strip(); desc=request.form.get('description','').strip(); selected=[int(x) for x in request.form.getlist('permissions') if x.isdigit()]; errors=[]
        if not name: errors.append('Admin type name is required.')
        if errors: return render_template('admin_role_form.html',permissions=perms,errors=errors,form=request.form)
        now=datetime.now(timezone.utc).isoformat()
        try:
            role=AdminType(name=name,description=desc,is_system=0,active=1,created_at=now)
            db.session.add(role); db.session.flush(); rid=role.id
            _ignore_insert(AdminTypePermission,[{'admin_type_id':rid,'permission_id':pid,
                                                 'granted_at':now} for pid in selected])
            db.session.commit()
        except sa.exc.IntegrityError:
            db.session.rollback(); return render_template('admin_role_form.html',permissions=perms,errors=['That admin type already exists.'],form=request.form)
        audit_log('role_created','administration','admin_type',rid,{'name':name,'permissions':selected}); flash('Administrator type created.','success'); return redirect(url_for('admin_roles'))
    return render_template('admin_role_form.html',permissions=perms,errors=[],form={})

@app.route('/admin/administration/roles/<int:rid>/edit',methods=['GET','POST'])
@admin_required
def admin_role_edit(rid):
    me=current_admin()
    if not is_super_admin(me) or not admin_has_permission(me['id'],'roles.edit'): return admin_access_error('Super Admin role management')
    role=obj(AdminType, rid)
    perms=db.session.scalars(select(Permission).order_by(Permission.module,Permission.name)).all()
    selected=set(db.session.scalars(select(AdminTypePermission.permission_id)
                                    .where(AdminTypePermission.admin_type_id==rid)).all())
    if not role or role['is_system'] or role['name']=='Ordinary Admin': abort(404)
    if request.method=='POST':
        if not csrf_check_request(): abort(403,description='Invalid or missing CSRF token.')
        name=request.form.get('name','').strip(); desc=request.form.get('description','').strip(); ids=[int(x) for x in request.form.getlist('permissions') if x.isdigit()]
        if not name: return render_template('admin_role_form.html',role=role,permissions=perms,selected=selected,errors=['Role name is required.'],form=request.form,mode='edit',show_advanced=True)
        now=datetime.now(timezone.utc).isoformat()
        db.session.execute(sa_update(AdminType).where(AdminType.id==rid)
                           .values(name=name,description=desc))
        db.session.execute(sa_delete(AdminTypePermission)
                           .where(AdminTypePermission.admin_type_id==rid))
        db.session.flush()
        db.session.add_all([AdminTypePermission(admin_type_id=rid,permission_id=pid,
                                                granted_at=now) for pid in ids])
        db.session.commit(); audit_log('role_updated','administration','admin_type',rid,{'name':name,'permissions':ids}); _notify_super_admins('Staff role changed',f'The {name} role was updated.','warning',url_for('admin_controls'),me['id']); flash('Staff role updated.','success'); return redirect(url_for('admin_roles'))
    return render_template('admin_role_form.html',role=role,permissions=perms,selected=selected,errors=[],form={},mode='edit',show_advanced=True)

@app.route('/admin/administration/permissions')
@admin_required
def admin_permissions_catalogue():
    me=current_admin()
    if not admin_has_permission(me['id'],'permissions.view'): return admin_access_error('permissions.view')
    perms=db.session.scalars(select(Permission).order_by(Permission.module,Permission.name)).all()
    return render_template('admin_permissions.html',permissions=perms)

@app.route('/admin/administration/scopes')
@admin_required
def admin_scopes():
    me=current_admin()
    if not admin_has_permission(me['id'],'scopes.view'): return admin_access_error('scopes.view')
    scopes=[_flatten(r,'AdminScope','username','display_name') for r in all_rows(
        select(AdminScope,Admin.username,Admin.display_name)
            .join(Admin,Admin.id==AdminScope.admin_id)
            .order_by(AdminScope.id.desc()))]
    return render_template('admin_scopes.html',scopes=scopes)

@app.route('/admin/administration/audit-logs')
@admin_required
def admin_audit_logs():
    me=current_admin()
    if not admin_has_permission(me['id'],'audit.view'): return admin_access_error('audit.view')
    logs=db.session.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(250)).all()
    logs=[dict(row, display_detail=audit_display_detail(row)) for row in logs]
    return render_template('admin_audit_logs.html',logs=logs)

def _generate_bank_id(name, level=''):
    base=re.sub(r'[^a-z0-9]+','_',f'{name}_{level}'.lower()).strip('_') or 'question_bank'
    candidate=base; i=2
    while bank(candidate):
        candidate=f'{base}_{i}'; i+=1
    return candidate

def validate_bank_payload(data, existing_id=None):
    errors=[]
    bid=str(data.get('id','')).strip()
    name=str(data.get('name','')).strip()
    version=str(data.get('version','1.0')).strip() or '1.0'
    try:
        duration=int(data.get('duration_minutes'))*60 if data.get('duration_minutes') not in (None,'') else int(data.get('duration_seconds',3600))
    except: duration=0
    if not bid or not bid.replace('_','').replace('-','').isalnum(): errors.append('Bank ID must contain only letters, numbers, hyphens or underscores.')
    if not name: errors.append('Bank name is required.')
    if duration<60: errors.append('Duration must be at least 60 seconds.')
    if existing_id is None and bank(bid): errors.append('A question bank with this ID already exists.')
    return errors

def save_bank(b):
    path=os.path.join(DATA,b['id']+'.json')
    tmp=path+'.tmp'
    with open(tmp,'w',encoding='utf-8') as f: json.dump(b,f,ensure_ascii=False,indent=2)
    os.replace(tmp,path)
    sync_examinations()

def next_qid(b): return max([int(q['id']) for q in b.get('questions',[])],default=0)+1

def _entrance_config_rows():
    """Every configuration, current session first, JSS 1 before SSS 1."""
    group_order=sa.case((EntranceBankConfig.entry_group=='year7',1),else_=2)
    return [_flatten(r,'EntranceBankConfig','exam_name','session_name','question_count','is_current')
            for r in all_rows(
        _entrance_config_select(Examination.question_count,AcademicSession.is_current)
        .order_by(AcademicSession.is_current.desc(),AcademicSession.id.desc(),
                  group_order,EntranceBankConfig.subject,EntranceBankConfig.id.desc()))]


def _entrance_banks_for(me):
    banks=sorted(load_banks().values(),key=lambda b:str(b.get('name') or b.get('id')).lower())
    if not me['admin_type_system']:
        banks=[b for b in banks if admin_scope_allows(me['id'],'bank',b['id'])]
    return banks


def _active_sessions():
    return db.session.scalars(select(AcademicSession).where(AcademicSession.active==1)
        .order_by(AcademicSession.is_current.desc(),AcademicSession.id.desc())).all()


@app.route('/admin/entrance-config')
@admin_required
def admin_entrance_config():
    me=current_admin()
    if not admin_has_permission(me['id'],'entrance.config.view'):
        return admin_access_error('entrance.config.view')
    return render_template('admin_entrance_config.html',configs=_entrance_config_rows(),
                           banks=_entrance_banks_for(me),sessions=_active_sessions(),
                           current_session=_school_current_session())

@app.post('/admin/entrance-config/save')
@admin_required
@csrf_protect
def admin_entrance_config_save():
    me=current_admin()
    perm='entrance.config.edit' if request.form.get('config_id') else 'entrance.config.create'
    if not admin_has_permission(me['id'],perm):
        return admin_access_error(perm)
    bank_id=request.form.get('bank_id','').strip()
    entry_group=request.form.get('entry_group','').strip().lower()
    subject=request.form.get('subject','').strip().lower()
    term=request.form.get('term','').strip() or 'Full Session'
    try: session_id=int(request.form.get('session_id',''))
    except (TypeError,ValueError): session_id=0
    try: count=int(request.form.get('questions_to_serve',''))
    except (TypeError,ValueError): count=0
    errors=[]
    b=bank(bank_id)
    if not b: errors.append('Select a valid entrance question bank.')
    if entry_group not in ('year7','year10'): errors.append('Select JSS 1 or SSS 1.')
    if subject not in ENTRANCE_SUBJECT_LABELS: errors.append('Select a valid entrance subject.')
    if b and not admin_scope_allows(me['id'],'bank',bank_id): errors.append('This question bank is outside your authorised scope.')
    if b and bank_subject(b)!=subject: errors.append('The selected subject does not match the question bank.')
    if b and subject!='general_knowledge' and bank_entry_group(b) not in (entry_group, None): errors.append('The selected question bank is not for this entry class.')
    session_row=db.session.scalars(select(AcademicSession).where(
        AcademicSession.id==session_id,AcademicSession.active==1)).first()
    if not session_row: errors.append('Select an active academic session.')
    valid,marks=_valid_question_configuration(count)
    if not valid: errors.append('This question count cannot produce a clean 100-point score under the current equal-weight scoring rules.')
    if b and count>len(b.get('questions',[])): errors.append(f'The selected bank contains only {len(b.get("questions",[]))} questions.')
    config_id=request.form.get('config_id','').strip()
    reason=request.form.get('reason','').strip()
    if session_row and not session_row.is_current and not reason:
        errors.append('A reason is required when assigning an entrance bank to a previous academic session.')
    if errors:
        return render_template('admin_entrance_config.html',configs=_entrance_config_rows(),
                               banks=_entrance_banks_for(me),sessions=_active_sessions(),
                               current_session=session_row,errors=errors,form=request.form),400
    now=datetime.now(timezone.utc).isoformat()

    def paper_count(cid):
        """A configuration already issued to a candidate is frozen."""
        return one_scalar(select(func.count()).select_from(CandidatePaper)
                          .where(CandidatePaper.config_id==cid),0)

    if config_id:
        row=obj(EntranceBankConfig,int(config_id))
        if not row: abort(404)
        if paper_count(int(config_id)):
            flash('This entrance configuration is already assigned to a candidate paper and cannot be edited. Create a new configuration for a changed setup, then activate it explicitly.','error')
            return redirect(url_for('admin_entrance_config'))
        row.bank_id=bank_id; row.entry_group=entry_group; row.subject=subject
        row.session_id=session_id; row.term=term; row.questions_to_serve=count
        row.marks_per_question=marks; row.updated_at=now; row.updated_by=me['id']
        action='entrance_config_updated'; target_id=int(config_id)
    else:
        existing=db.session.scalars(select(EntranceBankConfig).where(
            EntranceBankConfig.bank_id==bank_id,EntranceBankConfig.entry_group==entry_group,
            EntranceBankConfig.subject==subject,EntranceBankConfig.session_id==session_id,
            EntranceBankConfig.term==term)).first()
        if existing:
            if paper_count(existing.id):
                flash('This entrance configuration is already assigned to a candidate paper. Use a new academic period/term configuration for a changed future setup.','error')
                return redirect(url_for('admin_entrance_config'))
            existing.questions_to_serve=count; existing.marks_per_question=marks
            existing.updated_at=now; existing.updated_by=me['id']
            target_id=existing.id; action='entrance_config_updated'
        else:
            created=EntranceBankConfig(bank_id=bank_id,entry_group=entry_group,subject=subject,
                session_id=session_id,term=term,questions_to_serve=count,
                marks_per_question=marks,active=0,practice_enabled=0,
                created_at=now,created_by=me['id'],updated_at=now,updated_by=me['id'])
            db.session.add(created); db.session.flush()
            target_id=created.id; action='entrance_config_created'
    db.session.commit()
    audit_log(action,'assessment','entrance_config',target_id,{'bank_id':bank_id,'entry_group':entry_group,'subject':subject,'session_id':session_id,'questions_to_serve':count,'reason':reason or None})
    flash('Entrance examination configuration saved. It is not active unless you explicitly activate it.','success')
    return redirect(url_for('admin_entrance_config'))

@app.post('/admin/entrance-config/<int:config_id>/activate')
@admin_required
@csrf_protect
def admin_entrance_config_activate(config_id):
    me=current_admin()
    reason=request.form.get('reason','').strip()
    if not admin_has_permission(me['id'],'entrance.config.activate'):
        return admin_access_error('entrance.config.activate')
    cfg=obj(EntranceBankConfig,config_id)
    if not cfg: abort(404)
    if not admin_scope_allows(me['id'],'bank',cfg.bank_id): return admin_access_error('entrance.config.activate')
    valid,marks=_valid_question_configuration(cfg.questions_to_serve)
    b=bank(cfg.bank_id)
    if not b or not valid or cfg.questions_to_serve>len(b.get('questions',[])):
        flash('This configuration cannot be activated because its question set is not valid for a 100-point examination.','error'); return redirect(url_for('admin_entrance_config'))
    now=datetime.now(timezone.utc).isoformat()
    # Exactly one configuration may be live per group/subject/session/term.
    db.session.execute(sa_update(EntranceBankConfig)
        .where(EntranceBankConfig.entry_group==cfg.entry_group,
               EntranceBankConfig.subject==cfg.subject,
               EntranceBankConfig.session_id==cfg.session_id,
               EntranceBankConfig.term==cfg.term)
        .values(active=0,updated_at=now,updated_by=me['id']))
    db.session.execute(sa_update(EntranceBankConfig)
        .where(EntranceBankConfig.id==config_id)
        .values(active=1,practice_enabled=0,updated_at=now,updated_by=me['id']))
    db.session.execute(sa_update(Examination)
        .where(Examination.bank_id==cfg.bank_id).values(active=1))
    db.session.commit()
    audit_log('entrance_config_activated','assessment','entrance_config',config_id,{'bank_id':cfg.bank_id,'entry_group':cfg.entry_group,'subject':cfg.subject,'session_id':cfg.session_id,'reason':reason or None})
    flash('Entrance examination configuration activated.','success')
    return redirect(url_for('admin_entrance_config'))

@app.post('/admin/entrance-config/<int:config_id>/practice')
@admin_required
@csrf_protect
def admin_entrance_config_practice(config_id):
    me=current_admin()
    if not admin_has_permission(me['id'],'entrance.practice.manage'):
        return admin_access_error('entrance.practice.manage')
    cfg=obj(EntranceBankConfig,config_id)
    if not cfg: abort(404)
    current=_school_current_session()
    if current and cfg.session_id==current['id']:
        flash('The current academic entrance configuration cannot be enabled for practice.','error'); return redirect(url_for('admin_entrance_config'))
    if not admin_scope_allows(me['id'],'bank',cfg.bank_id):
        return admin_access_error('entrance.practice.manage')
    new=0 if cfg.practice_enabled else 1
    reason=request.form.get('reason','').strip()
    if new and not reason:
        flash('A reason is required when enabling historical practice for an entrance configuration.','error'); return redirect(url_for('admin_entrance_config'))
    cfg.practice_enabled=new; cfg.updated_at=datetime.now(timezone.utc).isoformat(); cfg.updated_by=me['id']
    db.session.commit()
    audit_log('entrance_practice_eligibility_changed','assessment','entrance_config',config_id,{'enabled':bool(new),'reason':reason or None})
    flash('Historical entrance practice eligibility updated.','success')
    return redirect(url_for('admin_entrance_config'))

@app.route('/admin/candidates')
@admin_required
def admin_candidates():
    """Entrance candidate register with deliberate class-first segmentation."""
    selected_group=request.args.get('level','').strip().lower()
    if selected_group not in ('year7','year10'):
        selected_group=''
    search=request.args.get('q','').strip()
    all_candidates=db.session.scalars(select(Candidate).where(Candidate.active==1)
                                      .order_by(Candidate.id.desc())).all()
    # One pass over every attempt status, rather than a query per candidate.
    statuses_by_candidate={}
    for cand_id,status in tuples(select(Attempt.candidate_id,Attempt.status)):
        statuses_by_candidate.setdefault(cand_id,[]).append(status)
    # Class cards are intentionally derived from the two supported entrance sets.
    groups=[('year7','JSS 1'),('year10','SSS 1')]
    cards=[]
    for key,label in groups:
        subset=[c for c in all_candidates if normalize_entry_group(c['target_class'])==key]
        completed_count=0; active_count=0
        for c in subset:
            statuses=statuses_by_candidate.get(c['id'],[])
            if any(st in ('submitted','expired') for st in statuses): completed_count+=1
            if any(st=='active' for st in statuses): active_count+=1
        cards.append({'key':key,'label':label,'count':len(subset),'completed':completed_count,'in_progress':active_count})
    if selected_group:
        candidates=[]
        for c in all_candidates:
            if normalize_entry_group(c['target_class'])!=selected_group: continue
            if search and search.casefold() not in ' '.join(str(c[k] or '') for k in ('candidate_name','candidate_code','school_attended','parent_guardian_name')).casefold(): continue
            candidates.append((c,_candidate_papers(c['id'])))
    else:
        candidates=[]
    return render_template('admin_candidates.html', candidates=candidates, class_cards=cards, selected_group=selected_group, search=search)

@app.route('/admin/candidates/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_new_candidate():
    if request.method=='POST':
        name=request.form.get('candidate_name','').strip()
        target=request.form.get('target_class','').strip()
        school_attended=request.form.get('school_attended','').strip()
        parent_guardian_name=request.form.get('parent_guardian_name','').strip()
        parent_guardian_relationship=request.form.get('parent_guardian_relationship','').strip()
        primary_mobile=request.form.get('primary_mobile','').strip()
        alternative_mobile=request.form.get('alternative_mobile','').strip()
        parent_guardian_email=request.form.get('parent_guardian_email','').strip()
        photo_path=None
        errors=[]
        if not name: errors.append('Candidate name is required.')
        if not school_attended: errors.append('School attended is required.')
        if not parent_guardian_name: errors.append('Parent / Guardian name is required.')
        if not parent_guardian_relationship: errors.append('Parent / Guardian relationship is required.')
        if not primary_mobile: errors.append('Primary mobile / WhatsApp is required.')
        if parent_guardian_email and ('@' not in parent_guardian_email or '.' not in parent_guardian_email.split('@')[-1]): errors.append('Enter a valid parent / guardian email address.')
        try:
            photo_path=_save_image_upload(request.files.get('photo'),'candidates',f'candidate_{name.replace(" ","_").lower() or "candidate"}')
        except ValueError as exc:
            errors.append(str(exc))
        group=normalize_entry_group(target)
        if not group: errors.append('Select a valid entrance level: JSS 1 or SS 1.')
        papers,missing=required_papers_for_target(target) if group else ([],[])
        current_session=_school_current_session()
        paper_configs=[]
        if group and current_session:
            for subject in ('mathematics','english','general_knowledge'):
                cfg=_entrance_config_row(group,subject,current_session['id'])
                if cfg: paper_configs.append(cfg)
        if missing:
            errors.append('The following required paper bank(s) are not available: '+', '.join(missing)+'. Complete the verified question banks before registering candidates.')
        if len(papers)!=3: errors.append('A candidate session must contain exactly three papers: Mathematics, English and General Knowledge.')
        if errors:
            return render_template('candidate_form.html',errors=errors,form=request.form,available_papers=papers,missing=missing)
        now=datetime.now(timezone.utc).isoformat()
        try:
            code=_new_candidate_code()
            password=_new_candidate_password()
            candidate=Candidate(
                candidate_code=code,candidate_name=name,target_class=target,
                password_hash=generate_password_hash(password),created_at=now,active=1,
                school_attended=school_attended,parent_guardian_name=parent_guardian_name,
                parent_guardian_relationship=parent_guardian_relationship,
                primary_mobile=primary_mobile,alternative_mobile=alternative_mobile,
                parent_guardian_email=parent_guardian_email,photo_path=photo_path)
            db.session.add(candidate); db.session.flush()
            cid=candidate.id
            for slot,b in enumerate(papers,1):
                cfg=next((x for x in paper_configs if x['bank_id']==b['id']),None)
                if not cfg:
                    raise ValueError('The entrance configuration changed while this candidate was being registered. Please review the active entrance setup and try again.')
                db.session.add(CandidatePaper(candidate_id=cid,slot=slot,bank_id=b['id'],
                                              config_id=cfg['id'],assigned_at=now))
            db.session.commit()
        except Exception:
            db.session.rollback(); raise
        _notify_super_admins('New student registered',f'{name} was registered for the {target} entrance examination.','info',url_for('admin_controls'))
        return render_template('candidate_registered.html',candidate={'id':cid,'candidate_code':code,'candidate_name':name,'target_class':target},password=password,papers=papers)
    return render_template('candidate_form.html',errors=[],form={},available_papers=[],missing=[])

@app.route('/admin/candidates/<int:cid>')
@admin_required
def admin_candidate_detail(cid):
    c=obj(Candidate,cid)
    if not c: abort(404)
    rows,total_score,total_max,pct,completed=candidate_cumulative(cid)
    latest_attempts=[_flatten(r,'Attempt','exam_name','paper_slot') for r in all_rows(
        select(Attempt,Examination.name.label('exam_name'),
               CandidatePaper.slot.label('paper_slot'))
        .outerjoin(Examination,Examination.bank_id==Attempt.bank_id)
        .outerjoin(CandidatePaper,and_(CandidatePaper.candidate_id==Attempt.candidate_id,
                                       CandidatePaper.bank_id==Attempt.bank_id))
        .where(Attempt.candidate_id==cid).order_by(Attempt.id.desc()))]
    latest_attempts=[dict(a, paper_label=entrance_paper_label(a['paper_slot'], a['bank_id'], a['exam_name'] or '') if a['paper_slot'] is not None else entrance_subject_label(a['bank_id'], a['exam_name'] or '')) for a in latest_attempts]
    banks=load_banks()
    papers=[]
    for row in rows:
        item=dict(row)
        subject=bank_subject(banks.get(row['bank_id'],{}))
        item['subject_label']={'mathematics':'Mathematics','english':'English','general_knowledge':'General Knowledge'}.get(subject,'Paper '+str(row['slot']))
        papers.append(item)
    return render_template('admin_candidate_detail.html',candidate=c,papers=papers,total_score=total_score,total_max=total_max,pct=pct,completed=completed,grade=_grade_label(pct) if completed else '—',latest_attempts=latest_attempts)

@app.route('/admin/candidates/<int:cid>/performance')
@admin_required
def admin_candidate_performance(cid):
    c=obj(Candidate,cid)
    if not c: abort(404)
    rows,total_score,total_max,pct,completed=candidate_cumulative(cid)
    attempts=[_flatten(r,'Attempt','exam_name','paper_slot') for r in all_rows(
        select(Attempt,Examination.name.label('exam_name'),
               CandidatePaper.slot.label('paper_slot'))
        .outerjoin(Examination,Examination.bank_id==Attempt.bank_id)
        .outerjoin(CandidatePaper,and_(CandidatePaper.candidate_id==Attempt.candidate_id,
                                       CandidatePaper.bank_id==Attempt.bank_id))
        .where(Attempt.candidate_id==cid).order_by(Attempt.id.desc()))]
    attempts=[dict(a, paper_label=entrance_paper_label(a['paper_slot'], a['bank_id'], a['exam_name'] or '') if a['paper_slot'] is not None else entrance_subject_label(a['bank_id'], a['exam_name'] or '')) for a in attempts]
    banks=load_banks()
    papers=[]
    for row in rows:
        item=dict(row)
        subject=bank_subject(banks.get(row['bank_id'],{}))
        item['subject_label']={'mathematics':'Mathematics','english':'English','general_knowledge':'General Knowledge'}.get(subject,'Paper '+str(row['slot']))
        papers.append(item)
    return render_template('admin_candidate_performance.html',candidate=c,papers=papers,total_score=total_score,total_max=total_max,pct=pct,completed=completed,grade=_grade_label(pct) if completed else '—',attempts=attempts)

@app.route('/admin/candidates/<int:cid>/result/email')
@admin_required
def admin_candidate_result_email(cid):
    c=obj(Candidate,cid)
    if not c: abort(404)
    email=(c['parent_guardian_email'] or '').strip()
    if not email:
        flash('No parent / guardian email address is available for this candidate.','error')
        return redirect(url_for('admin_candidate_detail',cid=cid))
    rows,total_score,total_max,pct,completed=candidate_cumulative(cid)
    score_line=f'{total_score}/{total_max} ({pct:.1f}%)' if completed else 'Not yet completed'
    subject=f'Entrance Examination Result — {c["candidate_name"]}'
    body=(f'Creative Rainbow Schools — Entrance Examination Result\n\n'
          f'Candidate: {c["candidate_name"]}\n'
          f'Candidate ID: {c["candidate_code"]}\n'
          f'Target class: {c["target_class"]}\n'
          f'Cumulative result: {score_line}\n'
          f'Overall grade: {_grade_label(pct) if completed else "Pending"}\n\n'
          f'This result was prepared by Creative Rainbow Schools.')
    return redirect('mailto:'+quote(email,safe='@.')+'?subject='+quote(subject)+'&body='+quote(body))

@app.route('/admin/candidates/<int:cid>/result/whatsapp')
@admin_required
def admin_candidate_result_whatsapp(cid):
    c=obj(Candidate,cid)
    if not c: abort(404)
    phone=(c['primary_mobile'] or c['alternative_mobile'] or '').strip()
    digits=''.join(ch for ch in phone if ch.isdigit())
    if digits.startswith('0'): digits='234'+digits[1:]
    if not digits:
        flash('No parent / guardian mobile number is available for this candidate.','error')
        return redirect(url_for('admin_candidate_detail',cid=cid))
    rows,total_score,total_max,pct,completed=candidate_cumulative(cid)
    score_line=f'{total_score}/{total_max} ({pct:.1f}%)' if completed else 'Not yet completed'
    message=(f"Creative Rainbow Schools — Entrance Examination Result\n\n"
             f"Candidate: {c['candidate_name']}\n"
             f"Candidate ID: {c['candidate_code']}\n"
             f"Target class: {c['target_class']}\n"
             f"Cumulative result: {score_line}\n"
             f"Overall grade: {_grade_label(pct) if completed else 'Pending'}\n\n"
             f"Thank you for choosing Creative Rainbow Schools.")
    return redirect('https://wa.me/'+digits+'?text='+quote(message))

@app.post('/admin/candidates/<int:cid>/delete')
@admin_required
@csrf_protect
def admin_candidate_delete(cid):
    if not is_super_admin(current_admin()): return admin_access_error('Super Admin approval')
    try:
        if not one_scalar(select(Candidate.id).where(Candidate.id==cid)):
            abort(404)
        # Answers reference attempts, which reference the candidate, so they are
        # removed in dependency order.
        attempt_ids=select(Attempt.id).where(Attempt.candidate_id==cid).scalar_subquery()
        db.session.execute(sa_delete(Answer).where(Answer.attempt_id.in_(attempt_ids)))
        db.session.execute(sa_delete(Attempt).where(Attempt.candidate_id==cid))
        db.session.execute(sa_delete(CandidatePaper).where(CandidatePaper.candidate_id==cid))
        db.session.execute(sa_delete(Candidate).where(Candidate.id==cid))
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    _notify_super_admins('Student record deleted',f'Student record #{cid} was deleted by a staff administrator.','warning',url_for('admin_controls')); flash('Student deleted successfully. Super Admin has been notified.','success')
    return redirect(url_for('admin_candidates'))

@app.route('/admin/candidates/<int:cid>/result/image')
@admin_required
def admin_candidate_result_image(cid):
    candidate=obj(Candidate,cid)

    if not candidate:
        abort(404)

    rows,total_score,total_max,pct,completed=candidate_cumulative(cid)

    if not completed:
        abort(400, description='The candidate has not completed the required result.')

    papers=[dict(r) for r in rows]

    total_questions=sum(
        int(p.get('question_count') or 0)
        for p in papers
        if p.get('status') in ('submitted','expired')
    )

    total_correct=sum(
        int(p.get('correct_count') or 0)
        for p in papers
        if p.get('status') in ('submitted','expired')
    )

    percentages=[
        float(p['percentage'])
        for p in papers
        if p.get('status') in ('submitted','expired')
        and p.get('percentage') is not None
    ]

    average_subject_percentage=(
        sum(percentages)/len(percentages)
        if percentages else 0
    )

    grade=_grade_label(pct)

    # _chrome_result_png returns (png_path, html_path); only the PNG is served.
    # Passing the whole tuple to send_file raised
    # "AttributeError: 'tuple' object has no attribute 'read'".
    image_path,_html_path=_chrome_result_png(
        candidate,
        papers,
        total_score,
        total_max,
        pct,
        total_questions,
        total_correct,
        average_subject_percentage,
        grade
    )

    return send_file(
        image_path,
        mimetype='image/png',
        as_attachment=False,
        download_name=f"{candidate['candidate_code']}_Official_Result.png"
    )

@app.route('/admin/candidates/<int:cid>/result/share')
@admin_required
def admin_candidate_result_share(cid):
    candidate=obj(Candidate,cid)

    if not candidate:
        abort(404)

    rows,total_score,total_max,pct,completed=candidate_cumulative(cid)

    if not completed:
        abort(400, description='The candidate has not completed the required result.')

    image_url=url_for(
        'admin_candidate_result_image',
        cid=cid,
        _external=True
    )

    filename=f"{candidate['candidate_code']}_Official_Result.png"

    logo_path=os.path.join(
        BASE,
        'static',
        'images',
        'school_logo.png'
    )

    logo_data=_result_file_data_uri(logo_path)

    return render_template(
        'admin_candidate_result_share.html',
        candidate=candidate,
        image_url=image_url,
        filename=filename,
        logo_data=logo_data,
        total_score=total_score,
        total_max=total_max,
        pct=pct,
        completed=completed,
        grade=_grade_label(pct)
    )

@app.route('/admin/candidates/<int:cid>/result/print')
@admin_required
def admin_candidate_result_print(cid):
    c=obj(Candidate,cid)

    if not c:
        abort(404)

    rows,total_score,total_max,pct,completed=candidate_cumulative(cid)

    total_questions,total_correct,average_subject_percentage=\
        premium_result_metrics(rows)

    return render_template(
        'admin_candidate_result_print.html',
        candidate=c,
        papers=rows,
        total_score=total_score,
        total_max=total_max,
        pct=pct,
        completed=completed,
        total_questions=total_questions,
        total_correct=total_correct,
        average_subject_percentage=average_subject_percentage,
        grade=_grade_label(pct) if completed else '—'
    )

@app.post('/admin/candidates/<int:cid>/credentials/reset')
@admin_required
@csrf_protect
def admin_candidate_credentials_reset(cid):
    c=one(select(Candidate.id,Candidate.candidate_code,Candidate.candidate_name,
                 Candidate.target_class,Candidate.created_at,Candidate.active)
          .where(Candidate.id==cid))
    if not c: abort(404)
    password=_new_candidate_password()
    db.session.execute(sa_update(Candidate).where(Candidate.id==cid)
                       .values(password_hash=generate_password_hash(password)))
    db.session.commit()
    _notify_super_admins('Student credentials reset',f'Login credentials for {c["candidate_name"]} were reset.','info',url_for('admin_controls'))
    return render_template('candidate_credentials_reset.html',candidate=c,password=password)

@app.route('/admin/candidates/<int:cid>/credentials/print')
@admin_required
def admin_candidate_credentials_print(cid):
    c=one(select(Candidate.id,Candidate.candidate_code,Candidate.candidate_name,
                 Candidate.target_class,Candidate.created_at,Candidate.active)
          .where(Candidate.id==cid))
    if not c: abort(404)
    return render_template('admin_candidate_credentials_print.html',candidate=c)

@app.route('/admin/banks')
@admin_required
def admin_question_banks():
    selected_group=request.args.get('level','').strip().lower()
    if selected_group not in ('year7','year10'): selected_group=''
    search=request.args.get('q','').strip()
    all_banks=sorted(load_banks().values(),key=lambda b:str(b.get('name') or b.get('id') or '').lower())
    current_session=_school_current_session()
    states={bank_id:active for bank_id,active in tuples(
        select(EntranceBankConfig.bank_id,func.max(EntranceBankConfig.active).label('active'))
        .where(EntranceBankConfig.session_id==(current_session['id'] if current_session else -1))
        .group_by(EntranceBankConfig.bank_id))}
    locks={r['resource_id']:r for r in all_rows(
        select(AdminResourceLock.resource_id,AdminResourceLock.reason)
        .where(AdminResourceLock.resource_type=='bank',
               AdminResourceLock.unlocked_at.is_(None)))}
    groups=[('year7','JSS 1'),('year10','SSS 1')]
    cards=[]
    for key,label in groups:
        matching=[b for b in all_banks if bank_entry_group(b)==key or (bank_entry_group(b) is None and bank_subject(b)=='general_knowledge')]
        cards.append({'key':key,'label':label,'count':len(matching),'questions':sum(len(b.get('questions',[])) for b in matching)})
    me=current_admin()
    if me and not me['admin_type_system']:
        all_banks=[b for b in all_banks if admin_scope_allows(me['id'],'bank',b['id'])]
    banks=[b for b in all_banks if selected_group and (bank_entry_group(b)==selected_group or (bank_entry_group(b) is None and bank_subject(b)=='general_knowledge')) and (not search or search.casefold() in str(b.get('name') or b.get('id') or '').casefold())]
    return render_template('admin_question_banks.html',banks=banks,all_banks=all_banks,exam_states=states,locks=locks,class_cards=cards,selected_group=selected_group,search=search)

@app.route('/admin/banks/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_new_bank():
    if request.method=='POST':
        data=request.form.to_dict()
        data['id']=data.get('id','').strip() or _generate_bank_id(data.get('name',''),data.get('level',''))
        errors=validate_bank_payload(data)
        if errors: return render_template('bank_form.html',mode='new',bank=data,errors=errors)
        duration_minutes=int(data.get('duration_minutes',60) or 60)
        b={'id':data['id'].strip(),'name':data['name'].strip(),'level':data.get('level','').strip(),'duration_seconds':duration_minutes*60,'version':data.get('version','1.0').strip(),'source_status':'admin_created','questions':[]}
        save_bank(b)
        # A newly created bank is never live until it is explicitly activated.
        db.session.execute(sa_update(Examination)
            .where(Examination.bank_id==b['id']).values(active=0))
        db.session.commit()
        audit_log('question_bank_created','assessment','bank',b['id'],{'name':b['name']}); _create_control_item('New question bank requires review',f'{b["name"]} was created by a staff administrator. Review it before it is used for an important examination.','question_bank_review','bank',b['id'],current_admin()['id']); _notify_super_admins('New question bank created',f'{b["name"]} was created by a staff administrator.','warning',url_for('admin_controls')); flash('Question bank created. Super Admin has been notified.','success'); return redirect(url_for('admin_bank',bid=b['id']))
    return render_template('bank_form.html',mode='new',bank=None,errors=[])

@app.route('/admin/banks/<bid>')
@admin_required
def admin_bank(bid):
    b=bank(bid)
    if not b: abort(404)
    return render_template('admin_bank.html',bank=b)

@app.route('/admin/banks/<bid>/edit',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_edit_bank(bid):
    b=bank(bid)
    if not b: abort(404)
    if request.method=='POST' and _resource_locked('bank',bid): flash('This question bank is locked by Super Admin. Unlock it from Administration → Controls before making changes.','error'); return redirect(url_for('admin_bank',bid=bid))
    if request.method=='POST':
        name=request.form.get('name','').strip(); level=request.form.get('level','').strip(); version=request.form.get('version','1.0').strip()
        try: duration=int(request.form.get('duration_minutes',0))*60 if request.form.get('duration_minutes') not in (None,'') else int(request.form.get('duration_seconds',3600))
        except: duration=0
        errors=[]
        if not name: errors.append('Bank name is required.')
        if duration<60: errors.append('Duration must be at least 60 seconds.')
        if errors: return render_template('bank_form.html',mode='edit',bank={**b,'name':name,'level':level,'version':version,'duration_seconds':duration},errors=errors)
        b.update(name=name,level=level,version=version,duration_seconds=duration)
        save_bank(b); audit_log('question_bank_updated','assessment','bank',bid,{'name':name,'level':level,'version':version}); _create_control_item('Question bank changed',f'{name} was updated by a staff administrator. Review the change if required.','question_bank_review','bank',bid,current_admin()['id']); _notify_super_admins('Question bank changed',f'{name} was updated by a staff administrator.','warning',url_for('admin_controls')); flash('Bank settings updated. Super Admin has been notified.','success'); return redirect(url_for('admin_bank',bid=bid))
    return render_template('bank_form.html',mode='edit',bank=b,errors=[])

@app.route('/admin/banks/<bid>/questions/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_new_question(bid):
    b=bank(bid)
    if not b: abort(404)
    if request.method=='POST' and _resource_locked('bank',bid): flash('This question bank is locked by Super Admin. Unlock it before adding questions.','error'); return redirect(url_for('admin_bank',bid=bid))
    if request.method=='POST':
        text=request.form.get('text','').strip(); instruction=request.form.get('instruction','').strip(); opts=[request.form.get(f'option{i}','').strip() for i in range(4)]
        try: ans=int(request.form.get('answer','-1')); points=int(request.form.get('points','1'))
        except: ans=-1; points=1
        errors=[]
        image_path=None
        try:
            image_path=_save_image_upload(request.files.get('image'),'questions',f'entrance_{bid}')
        except ValueError as exc:
            errors.append(str(exc))
        if not text: errors.append('Question text is required.')
        if any(not o for o in opts): errors.append('All four options are required.')
        if ans not in range(4): errors.append('Select the correct option.')
        if points<1: errors.append('Points must be at least 1.')
        if errors: return render_template('question_form.html',bank=b,question={'text':text,'instruction':instruction,'image_path':image_path,'options':opts,'answer':ans,'points':points},errors=errors)
        item={'id':next_qid(b),'text':text,'options':opts,'answer':ans,'points':points,'instruction':instruction}
        if image_path: item['image_path']=image_path
        b['questions'].append(item); save_bank(b); audit_log('question_added','assessment','bank',bid,{'question_id':b['questions'][-1]['id']}); _notify_super_admins('Question added to bank',f'A question was added to {b.get("name",bid)}.','info',url_for('admin_controls')); flash('Question added. Super Admin has been notified.','success'); return redirect(url_for('admin_bank',bid=bid))
    return render_template('question_form.html',bank=b,question=None,errors=[])

@app.route('/admin/banks/<bid>/questions/<int:qid>/edit',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_edit_question(bid,qid):
    b=bank(bid)
    if not b: abort(404)
    if request.method=='POST' and _resource_locked('bank',bid): flash('This question bank is locked by Super Admin. Unlock it before editing questions.','error'); return redirect(url_for('admin_bank',bid=bid))
    q=next((x for x in b['questions'] if int(x['id'])==qid),None)
    if not q: abort(404)
    if request.method=='POST':
        text=request.form.get('text','').strip(); instruction=request.form.get('instruction','').strip(); opts=[request.form.get(f'option{i}','').strip() for i in range(4)]
        try: ans=int(request.form.get('answer','-1')); points=int(request.form.get('points','1'))
        except: ans=-1; points=1
        errors=[]
        image_path=q.get('image_path')
        try:
            uploaded=_save_image_upload(request.files.get('image'),'questions',f'entrance_{bid}')
            if uploaded: image_path=uploaded
        except ValueError as exc:
            errors.append(str(exc))
        if not text: errors.append('Question text is required.')
        if any(not o for o in opts): errors.append('All four options are required.')
        if ans not in range(4): errors.append('Select the correct option.')
        if points<1: errors.append('Points must be at least 1.')
        if errors: return render_template('question_form.html',bank=b,question={'id':qid,'text':text,'instruction':instruction,'image_path':image_path,'options':opts,'answer':ans,'points':points},errors=errors)
        q.update(text=text,options=opts,answer=ans,points=points,instruction=instruction)
        if image_path: q['image_path']=image_path
        else: q.pop('image_path',None)
        save_bank(b); audit_log('question_updated','assessment','bank',bid,{'question_id':qid}); _notify_super_admins('Question updated',f'Question {qid} in {b.get("name",bid)} was updated.','info',url_for('admin_controls')); flash(f'Question {qid} updated. Super Admin has been notified.','success'); return redirect(url_for('admin_bank',bid=bid))
    return render_template('question_form.html',bank=b,question=q,errors=[])

@app.post('/admin/banks/<bid>/questions/<int:qid>/delete')
@admin_required
@csrf_protect
def admin_delete_question(bid,qid):
    b=bank(bid)
    if not b: abort(404)
    if _resource_locked('bank',bid): flash('This question bank is locked by Super Admin. Unlock it before deleting questions.','error'); return redirect(url_for('admin_bank',bid=bid))
    before=len(b['questions']); b['questions']=[q for q in b['questions'] if int(q['id'])!=qid]
    if len(b['questions'])==before: abort(404)
    # Deleting a question must not leave an active configuration asking for
    # more questions than the bank still holds.
    invalid=one_scalar(select(func.count()).select_from(EntranceBankConfig)
        .where(EntranceBankConfig.bank_id==bid,EntranceBankConfig.active==1,
               EntranceBankConfig.questions_to_serve>len(b['questions'])), 0)
    if invalid:
        b['questions'].append(next(q for q in bank(bid)['questions'] if int(q['id'])==qid))
        flash('This question cannot be deleted because an active entrance configuration requires the configured number of questions. Change the configuration first.','error')
        return redirect(url_for('admin_bank',bid=bid))
    save_bank(b); audit_log('question_deleted','assessment','bank',bid,{'question_id':qid}); _create_control_item('Question deleted',f'Question {qid} was deleted from {b.get("name",bid)}.','question_review','bank',bid,current_admin()['id']); _notify_super_admins('Question deleted',f'Question {qid} was deleted from {b.get("name",bid)}.','warning',url_for('admin_controls')); flash(f'Question {qid} deleted. Super Admin has been notified.','success'); return redirect(url_for('admin_bank',bid=bid))

@app.post('/admin/banks/<bid>/questions/reorder')
@admin_required
@csrf_protect
def admin_reorder(bid):
    b=bank(bid)
    if not b: abort(404)
    if _resource_locked('bank',bid): flash('This question bank is locked by Super Admin. Unlock it before reordering questions.','error'); return redirect(url_for('admin_bank',bid=bid))
    try: ids=[int(x) for x in request.form.get('order','').split(',') if x.strip()]
    except: ids=[]
    by={int(q['id']):q for q in b['questions']}
    if set(ids)!=set(by): return jsonify(ok=False,error='Order must contain every question exactly once.'),400
    b['questions']=[by[i] for i in ids]
    save_bank(b); audit_log('questions_reordered','assessment','bank',bid,{}); _notify_super_admins('Question order changed',f'The question order in {b.get("name",bid)} was changed.','info',url_for('admin_controls')); return jsonify(ok=True)

@app.route('/admin/attempts')
@admin_required
def admin_attempts():
    rows=[_flatten(r,'Attempt','exam_name') for r in all_rows(
        select(Attempt,Examination.name.label('exam_name'))
            .outerjoin(Examination,Examination.id==Attempt.exam_id)
            .order_by(Attempt.id.desc()))]
    return render_template('admin_attempts.html',attempts=rows)

@app.route('/admin/api/banks/<bid>/export')
@admin_required
def admin_export_bank(bid):
    b=bank(bid)
    if not b: abort(404)
    return jsonify(b)

def _grade_label(pct):
    if pct is None: return '—'
    if pct >= 80: return 'Excellent'
    if pct >= 70: return 'Very Good'
    if pct >= 60: return 'Good'
    if pct >= 50: return 'Pass'
    return 'Below Pass'

def _result_rows(bank_id=None):
    stmt=select(Attempt).where(Attempt.status.in_(('submitted','expired')))
    if bank_id:
        stmt=stmt.where(Attempt.bank_id==bank_id)
    return db.session.scalars(stmt.order_by(Attempt.percentage.desc(),
                                            Attempt.submitted_at.asc(),
                                            Attempt.id.asc())).all()

@app.route('/admin/results')
@admin_required
def admin_results():
    # Results & Analytics is subject-centric: every candidate appears once in
    # each subject, allowing administrators to compare subject performance.
    all_summaries=_candidate_results_summary()
    target=request.args.get('target_class','').strip()
    search=request.args.get('q','').strip()
    summaries=all_summaries
    if target:
        summaries=[r for r in summaries if r['target_class'].strip().casefold()==target.casefold()]
    if search:
        needle=search.casefold(); summaries=[r for r in summaries if needle in r['candidate_name'].casefold() or needle in r['candidate_code'].casefold()]
    subject_defs=[('mathematics','Mathematics'),('english','English'),('general_knowledge','General Knowledge')]
    subject_sections=[]
    for key,label in subject_defs:
        entries=[]
        attempted=[]
        for r in summaries:
            d=dict(r[key]); d.update({'candidate_name':r['candidate_name'],'candidate_code':r['candidate_code'],'target_class':r['target_class']})
            entries.append(d)
            if d['percentage'] is not None: attempted.append(d)
        passed=sum(1 for d in attempted if d['percentage']>=50)
        average=(sum(d['percentage'] for d in attempted)/len(attempted)) if attempted else 0
        entries.sort(key=lambda d:(d['percentage'] is not None,d['percentage'] if d['percentage'] is not None else -1),reverse=True)
        subject_sections.append({'key':key,'label':label,'entries':entries,'attempted':len(attempted),'passed':passed,'average':average,'pass_rate':(passed/len(attempted)*100) if attempted else 0})
    overall_attempted=[d for r in summaries for d in [r['mathematics'],r['english'],r['general_knowledge']] if d['percentage'] is not None]
    overall_average=(sum(d['percentage'] for d in overall_attempted)/len(overall_attempted)) if overall_attempted else 0
    return render_template('admin_results.html',subject_sections=subject_sections,target_classes=sorted({r['target_class'] for r in all_summaries if r['target_class']}),selected_target=target,search=search,total_candidates=len(summaries),overall_average=overall_average)

def _candidate_results_summary():
    """Build one administrative summary row per registered candidate.

    Subject scores are resolved from the candidate's assigned bank, so the
    report remains correct for both Year 7/JSS 1 and SSS 1/Year 10 sessions.
    The latest attempt for each assigned paper is used, matching the existing
    cumulative candidate result logic.

    All papers are fetched in one query rather than one query per candidate.
    """
    banks=load_banks()
    candidates=db.session.scalars(select(Candidate).where(Candidate.active==1)
        .order_by(Candidate.target_class,Candidate.candidate_name)).all()
    latest=(select(func.max(Attempt.id))
            .where(Attempt.candidate_id==CandidatePaper.candidate_id,
                   Attempt.bank_id==CandidatePaper.bank_id)
            .correlate(CandidatePaper).scalar_subquery())
    papers_by_candidate={}
    for row in all_rows(
        select(CandidatePaper.candidate_id,CandidatePaper.slot,CandidatePaper.bank_id,
               Examination.name.label('exam_name'),Attempt.status,Attempt.score,
               Attempt.max_score,Attempt.percentage,Attempt.submitted_at)
        .select_from(CandidatePaper)
        .outerjoin(Examination,Examination.bank_id==CandidatePaper.bank_id)
        .outerjoin(Attempt,Attempt.id==latest)
        .order_by(CandidatePaper.candidate_id,CandidatePaper.slot)):
        papers_by_candidate.setdefault(row['candidate_id'],[]).append(row)

    summaries=[]
    for c in candidates:
        papers=papers_by_candidate.get(c.id,[])
        subject_data={
            'english': {'label':'English', 'score':None, 'max_score':None, 'percentage':None, 'status':None},
            'mathematics': {'label':'Mathematics', 'score':None, 'max_score':None, 'percentage':None, 'status':None},
            'general_knowledge': {'label':'General Knowledge', 'score':None, 'max_score':None, 'percentage':None, 'status':None},
        }
        for paper in papers:
            b=banks.get(paper['bank_id'])
            subject=bank_subject(b or {'id':paper['bank_id'],'name':paper['exam_name'] or ''})
            if subject in subject_data:
                subject_data[subject].update(
                    score=paper['score'], max_score=paper['max_score'],
                    percentage=paper['percentage'], status=paper['status']
                )

        completed=sum(1 for d in subject_data.values() if d['status'] in ('submitted','expired'))
        total_score=sum((d['score'] or 0) for d in subject_data.values() if d['status'] in ('submitted','expired'))
        total_max=sum((d['max_score'] or 0) for d in subject_data.values() if d['status'] in ('submitted','expired'))
        overall_pct=(total_score/total_max*100) if total_max else 0
        complete=completed == len(subject_data)
        subject_percentages=[d['percentage'] for d in subject_data.values() if d['percentage'] is not None]
        subject_average=(sum(subject_percentages)/len(subject_percentages)) if subject_percentages else 0

        summaries.append({
            'id':c.id, 'candidate_code':c.candidate_code, 'candidate_name':c.candidate_name,
            'target_class':c.target_class, 'photo_path':c.photo_path, 'english':subject_data['english'],
            'mathematics':subject_data['mathematics'], 'general_knowledge':subject_data['general_knowledge'],
            'completed':completed, 'total_score':total_score, 'total_max':total_max,
            'overall_percentage':overall_pct, 'subject_average':subject_average,
            'grade':_grade_label(overall_pct) if complete else '—', 'complete':complete
        })
    return summaries

@app.route('/admin/results/summary')
@admin_required
def admin_results_summary():
    all_summaries=_candidate_results_summary()
    summaries=all_summaries
    target=request.args.get('target_class','').strip()
    search=request.args.get('q','').strip()
    if target:
        summaries=[r for r in summaries if r['target_class'].strip().casefold()==target.casefold()]
    if search:
        needle=search.casefold()
        summaries=[r for r in summaries if needle in r['candidate_name'].casefold() or needle in r['candidate_code'].casefold()]
    target_classes=sorted({r['target_class'] for r in all_summaries if r['target_class']})
    return render_template('admin_results_summary.html',summaries=summaries,target_classes=target_classes,selected_target=target,search=search)

@app.route('/admin/results/summary/print')
@admin_required
def admin_results_summary_print():
    summaries=_candidate_results_summary()
    target=request.args.get('target_class','').strip()
    search=request.args.get('q','').strip()
    if target:
        summaries=[r for r in summaries if r['target_class'].strip().casefold()==target.casefold()]
    if search:
        needle=search.casefold()
        summaries=[r for r in summaries if needle in r['candidate_name'].casefold() or needle in r['candidate_code'].casefold()]
    return render_template('admin_results_summary_print.html',summaries=summaries,selected_target=target,search=search,now=datetime.now().strftime('%d %b %Y, %I:%M %p'))

@app.route('/admin/results/<int:aid>')
@admin_required
def admin_result_detail(aid):
    a=get_attempt(aid)
    if not a: abort(404)
    b=bank(a['bank_id'])
    answers=_answers_for_attempt(aid)
    slot_row=one(select(CandidatePaper.slot)
        .where(CandidatePaper.candidate_id==a['candidate_id'],
               CandidatePaper.bank_id==a['bank_id']).limit(1)) if a['candidate_id'] else None
    paper_label=entrance_paper_label(slot_row['slot'],a['bank_id'],b.get('name','') if b else '') if slot_row else entrance_subject_label(a['bank_id'],b.get('name','') if b else '')
    return render_template('admin_result_detail.html',attempt=a,exam=b,answers=answers,grade=_grade_label(a['percentage']),paper_label=paper_label)

@app.route('/admin/results/<int:aid>/print')
@admin_required
def admin_result_print(aid):
    a=get_attempt(aid)
    if not a: abort(404)
    b=bank(a['bank_id'])
    answers=_answers_for_attempt(aid)
    slot_row=one(select(CandidatePaper.slot)
        .where(CandidatePaper.candidate_id==a['candidate_id'],
               CandidatePaper.bank_id==a['bank_id']).limit(1)) if a['candidate_id'] else None
    paper_label=entrance_paper_label(slot_row['slot'],a['bank_id'],b.get('name','') if b else '') if slot_row else entrance_subject_label(a['bank_id'],b.get('name','') if b else '')
    return render_template('admin_result_print.html',attempt=a,exam=b,answers=answers,grade=_grade_label(a['percentage']),paper_label=paper_label)

@app.route('/admin/rankings')
@admin_required
def admin_rankings():
    # Competition ranking is one row per candidate, not one row per paper.
    # Candidates are ranked only after all three assigned papers are complete.
    all_summaries=_candidate_results_summary()
    target=request.args.get('target_class','').strip()
    search=request.args.get('q','').strip()
    summaries=all_summaries
    if target:
        summaries=[r for r in summaries if r['target_class'].strip().casefold()==target.casefold()]
    if search:
        needle=search.casefold(); summaries=[r for r in summaries if needle in r['candidate_name'].casefold() or needle in r['candidate_code'].casefold()]
    ranked_candidates=sorted([r for r in summaries if r['complete']], key=lambda r:(r['overall_percentage'],r['total_score']), reverse=True)
    ranked=[]; last_key=None; rank=0
    for i,r in enumerate(ranked_candidates,1):
        key=(r['overall_percentage'],r['total_score'])
        if key!=last_key: rank=i; last_key=key
        item=dict(r); item['rank']=rank; ranked.append(item)
    target_classes=sorted({r['target_class'] for r in all_summaries if r['target_class']})
    return render_template('admin_rankings.html',ranked=ranked,target_classes=target_classes,selected_target=target,search=search,total_candidates=len(summaries),completed_candidates=len(ranked))

def _csv_response(rows, filename):
    import csv, io
    out=io.StringIO(); w=csv.writer(out)
    w.writerow(['Rank','Candidate','Examination','Bank ID','Status','Score','Max Score','Percentage','Grade','Started At','Submitted At'])
    for i,r in enumerate(rows,1):
        exam_name=r['exam_name'] if ('exam_name' in r.keys() and r['exam_name']) else None
        w.writerow([i,r['candidate'],exam_name or r['bank_id'],r['bank_id'],r['status'],r['score'] if r['score'] is not None else '',r['max_score'] if r['max_score'] is not None else '',f"{r['percentage']:.1f}" if r['percentage'] is not None else '',_grade_label(r['percentage']),r['started_at'],r['submitted_at'] or ''])
    return Response(out.getvalue(),mimetype='text/csv',headers={'Content-Disposition':f'attachment; filename={filename}'})

@app.route('/admin/export/results.csv')
@admin_required
def export_results_csv():
    bid=request.args.get('bank_id','').strip() or None
    stmt=(select(Attempt,Examination.name.label('exam_name'))
        .outerjoin(Examination,Examination.id==Attempt.exam_id)
        .where(Attempt.status.in_(('submitted','expired'))))
    if bid:
        stmt=stmt.where(Attempt.bank_id==bid)
    rows=[_flatten(r,'Attempt','exam_name') for r in all_rows(
        stmt.order_by(Attempt.percentage.desc(),Attempt.id.asc()))]
    return _csv_response(rows,'crainbow-results.csv')

@app.route('/admin/export/rankings.csv')
@admin_required
def export_rankings_csv():
    bid=request.args.get('bank_id','').strip() or None; rows=_result_rows(bid)
    import csv, io
    out=io.StringIO(); w=csv.writer(out); w.writerow(['Rank','Candidate','Examination','Score','Max Score','Percentage','Grade','Status'])
    last=None; rank=0
    for i,r in enumerate(rows,1):
        key=(r['percentage'],r['score'])
        if key!=last: rank=i; last=key
        w.writerow([rank,r['candidate'],r['bank_id'],r['score'],r['max_score'],f"{r['percentage']:.1f}",_grade_label(r['percentage']),r['status']])
    return Response(out.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=crainbow-rankings.csv'})

@app.route('/admin/export/results.json')
@admin_required
def export_results_json():
    bid=request.args.get('bank_id','').strip() or None; rows=_result_rows(bid)
    payload=[{'rank':i,'candidate':r['candidate'],'bank_id':r['bank_id'],'status':r['status'],'score':r['score'],'max_score':r['max_score'],'percentage':r['percentage'],'grade':_grade_label(r['percentage']),'started_at':r['started_at'],'submitted_at':r['submitted_at']} for i,r in enumerate(rows,1)]
    return Response(json.dumps(payload,ensure_ascii=False,indent=2),mimetype='application/json',headers={'Content-Disposition':'attachment; filename=crainbow-results.json'})

@app.post('/admin/examinations/<bid>/toggle')
@admin_required
@csrf_protect
def toggle_exam(bid):
    if not admin_has_permission(current_admin()['id'],'entrance.config.activate'):
        return admin_access_error('entrance.config.activate')
    # Entrance activation is now governed by the periodized configuration layer.
    flash('Entrance examination activation is managed from Exam Configuration.','error')
    return redirect(url_for('admin_entrance_config'))

@app.post('/admin/results/<int:aid>/grant-retake')
@admin_required
@csrf_protect
def admin_grant_retake(aid):
    if not is_super_admin(current_admin()): return admin_access_error('Super Admin approval')
    a=get_attempt(aid)
    if not a: abort(404)
    if a['status'] not in ('submitted','expired'):
        flash('A retake can only be granted after the previous attempt is completed.','error')
        return redirect(url_for('admin_result_detail',aid=aid))
    candidate_key=a['candidate'].strip().casefold()
    # Do not stack unused permissions accidentally. Legacy grants predate
    # candidate_id and are still matched on the normalised candidate name.
    pending=select(RetakeGrant.id).where(RetakeGrant.bank_id==a['bank_id'],
                                         RetakeGrant.used_at.is_(None))
    if a['candidate_id']:
        pending=pending.where(RetakeGrant.candidate_id==a['candidate_id'])
    else:
        pending=pending.where(RetakeGrant.candidate_key==candidate_key)
    if one(pending.limit(1)):
        flash('A retake permission is already pending for this candidate and examination.','error')
        return redirect(url_for('admin_result_detail',aid=aid))
    db.session.add(RetakeGrant(candidate_key=candidate_key,candidate_name=a['candidate'],
        candidate_id=a['candidate_id'],bank_id=a['bank_id'],
        granted_at=datetime.now(timezone.utc).isoformat(),granted_by='admin'))
    db.session.commit()
    _create_control_item('Retake access granted',f'A one-time retake was granted for {a["candidate"]}.','retake_review','attempt',aid,current_admin()['id']); _notify_super_admins('Retake access granted',f'A one-time retake was granted for {a["candidate"]}.','warning',url_for('admin_controls'))
    flash('One-time retake access granted. Super Admin has been notified. The candidate may now log in and take this examination once more.','success')
    return redirect(url_for('admin_result_detail',aid=aid))

@app.post('/admin/results/<int:aid>/regrade')
@admin_required
@csrf_protect
def admin_regrade(aid):
    if not is_super_admin(current_admin()): return admin_access_error('Super Admin approval')
    a=get_attempt(aid)
    if not a: abort(404)
    if a['status'] not in ('submitted','expired'):
        flash('Only completed attempts can be regraded.','error'); return redirect(url_for('admin_result_detail',aid=aid))
    grade(aid,force=True); _create_control_item('Result regraded',f'Attempt #{aid} for {a["candidate"]} was regraded.','regrade_review','attempt',aid,current_admin()['id']); _notify_super_admins('Result regraded',f'Attempt #{aid} for {a["candidate"]} was regraded.','warning',url_for('admin_controls')); flash('Attempt regraded using the current verified answer key. Super Admin has been notified.','success'); return redirect(url_for('admin_result_detail',aid=aid))

ACADEMIC_HISTORY_LEVELS={'Primary 1','Primary 2','Primary 3','Primary 4','Primary 5',
                         'Primary 6','JSS 1','JSS 2','JSS 3','SSS 1','SSS 2','SSS 3'}


def _mark_latest_history_current(sid):
    """The latest-dated history entry is the current one.

    Staff often enter older records after newer ones, so currency is recomputed
    from the dates rather than assumed from insertion order.
    """
    latest=one_scalar(select(StudentEnrollmentHistory.id)
        .where(StudentEnrollmentHistory.student_id==sid)
        .order_by(func.coalesce(StudentEnrollmentHistory.enrolled_at,'').desc(),
                  StudentEnrollmentHistory.id.desc()).limit(1))
    if latest is not None:
        db.session.execute(sa_update(StudentEnrollmentHistory)
            .where(StudentEnrollmentHistory.student_id==sid)
            .values(active=sa.case((StudentEnrollmentHistory.id==latest,1),else_=0)))


def _sync_enrolment_for_history(sid, class_id, session_id, enrolled_at):
    """Keep the live enrolment in step with an academic history entry."""
    existing=db.session.scalars(select(StudentEnrolment).where(
        StudentEnrolment.student_id==sid,
        StudentEnrolment.session_id==session_id)).first()
    if existing:
        existing.class_id=class_id; existing.enrolled_at=enrolled_at; existing.active=1
    else:
        db.session.add(StudentEnrolment(student_id=sid,class_id=class_id,
                                        session_id=session_id,enrolled_at=enrolled_at,active=1))


@app.post('/admin/school/students/<int:sid>/history')
@admin_required
@csrf_protect
def admin_school_student_history_add(sid):
    if not admin_has_permission(current_admin()['id'],'student.history.manage'): return admin_access_error('student.history.manage')
    level=request.form.get('level_name','').strip()
    session_id=request.form.get('session_id',type=int)
    enrolled_at=request.form.get('enrolled_at','').strip()
    completed_at=request.form.get('completed_at','').strip()
    notes=request.form.get('notes','').strip()
    allowed={'Daycare','Toddler 1','Toddler 2','Junior Reception','Middle Reception','Senior Reception','Crèche','Nursery 1','Nursery 2','Nursery 3','Primary 1','Primary 2','Primary 3','Primary 4','Primary 5','Primary 6','JSS 1','JSS 2','JSS 3','SSS 1','SSS 2','SSS 3'}
    if level not in allowed:
        flash('Select a valid enrollment level.','error'); return redirect(url_for('admin_school_student_detail',sid=sid)+'#enrolment-history')
    if not one_scalar(select(Student.id).where(Student.id==sid)): abort(404)
    if session_id and not one_scalar(select(AcademicSession.id).where(
            AcademicSession.id==session_id,AcademicSession.active==1)):
        flash('Select a valid academic session.','error'); return redirect(url_for('admin_school_student_detail',sid=sid)+'#enrolment-history')
    class_id=one_scalar(select(SchoolClass.id).where(SchoolClass.name==level))
    now=datetime.now(timezone.utc).isoformat()
    entry_date=enrolled_at or now[:10]
    # Close any open earlier record before opening this one.
    db.session.execute(sa_update(StudentEnrollmentHistory)
        .where(StudentEnrollmentHistory.student_id==sid,
               StudentEnrollmentHistory.active==1,
               func.coalesce(StudentEnrollmentHistory.enrolled_at,'') <= entry_date)
        .values(active=0,completed_at=func.coalesce(StudentEnrollmentHistory.completed_at,entry_date)))
    db.session.add(StudentEnrollmentHistory(student_id=sid,session_id=session_id,
        level_name=level,class_id=class_id,enrolled_at=entry_date,
        completed_at=completed_at or None,active=1,notes=notes,created_at=now))
    db.session.flush()
    _mark_latest_history_current(sid)
    if class_id and level in ACADEMIC_HISTORY_LEVELS and session_id:
        _sync_enrolment_for_history(sid,class_id,session_id,enrolled_at or now[:10])
    db.session.commit()
    audit_log('student_enrollment_history_added','school','student',sid,{'level':level,'session_id':session_id,'movement':True,'notes':notes})
    flash(f'Enrollment history updated: {level}.','success')
    return redirect(url_for('admin_school_student_detail',sid=sid)+'#enrolment-history')

@app.route('/admin/school/students/<int:sid>/history/<int:history_id>/edit',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_student_history_edit(sid,history_id):
    me=current_admin()
    if not admin_has_permission(me['id'],'student.history.manage'): return admin_access_error('student.history.manage')
    row=db.session.scalars(select(StudentEnrollmentHistory).where(
        StudentEnrollmentHistory.id==history_id,
        StudentEnrollmentHistory.student_id==sid)).first()
    sessions=db.session.scalars(select(AcademicSession).where(AcademicSession.active==1)
        .order_by(AcademicSession.id.desc())).all()
    if not row: abort(404)
    row_dict={c.key:getattr(row,c.key) for c in row.__mapper__.column_attrs}
    levels=['Daycare','Crèche','Toddler 1','Toddler 2','Junior Reception','Middle Reception','Senior Reception'] + [f'Primary {i}' for i in range(1,7)] + [f'JSS {i}' for i in range(1,4)] + [f'SSS {i}' for i in range(1,4)]
    if request.method=='POST':
        level=request.form.get('level_name','').strip()
        session_id=request.form.get('session_id',type=int)
        enrolled=request.form.get('enrolled_at','').strip() or None
        completed=request.form.get('completed_at','').strip() or None
        reason=request.form.get('correction_reason','').strip()
        if level not in levels: return render_template('admin_enrollment_history_edit.html',row=row_dict,sessions=sessions,levels=levels,errors=['Select a valid enrollment level.'])
        if not reason: return render_template('admin_enrollment_history_edit.html',row=row_dict,sessions=sessions,levels=levels,errors=['A correction reason is required when changing historical records.'])
        old_level=row.level_name
        class_id=one_scalar(select(SchoolClass.id).where(SchoolClass.name==level))
        now=datetime.now(timezone.utc).isoformat()
        row.level_name=level; row.session_id=session_id; row.class_id=class_id
        row.enrolled_at=enrolled; row.completed_at=completed; row.corrected_at=now
        row.corrected_by=me['id']; row.correction_reason=reason
        db.session.flush()
        _mark_latest_history_current(sid)
        if class_id and level in ACADEMIC_HISTORY_LEVELS and session_id:
            _sync_enrolment_for_history(sid,class_id,session_id,enrolled or now[:10])
        db.session.commit()
        audit_log('student_enrollment_history_corrected','school','student_history',history_id,{'student_id':sid,'old_level':old_level,'new_level':level,'reason':reason})
        flash('Enrollment history corrected and the change was recorded in the audit trail.','success')
        return redirect(url_for('admin_school_student_detail',sid=sid)+'#enrolment-history')
    return render_template('admin_enrollment_history_edit.html',row=row,sessions=sessions,levels=levels,errors=[])

def _finance_payment_allocated(payment_id):
    """How much of one payment has been applied to fee assessments."""
    return float(one_scalar(
        select(func.coalesce(func.sum(FinancePaymentAllocation.amount),0))
        .where(FinancePaymentAllocation.payment_id==payment_id,
               FinancePaymentAllocation.voided_at.is_(None)), 0))

def _finance_assessment_allocated(assessment_id):
    """Total still-standing allocations against one fee assessment."""
    return float(one_scalar(
        select(func.coalesce(func.sum(FinancePaymentAllocation.amount),0))
        .where(FinancePaymentAllocation.assessment_id==assessment_id,
               FinancePaymentAllocation.voided_at.is_(None)), 0))

def _finance_payment_balance(payment_id):
    row=one(select(FinancePayment.amount,FinancePayment.status)
            .where(FinancePayment.id==payment_id))
    if not row or row['status']!='posted':
        return 0.0
    return max(0.0, float(row['amount'] or 0)-_finance_payment_allocated(payment_id))

def _finance_assessment_balance(assessment_id):
    row=one(select(FinanceFeeAssessment.amount,FinanceFeeAssessment.active)
            .where(FinanceFeeAssessment.id==assessment_id))
    if not row or not row['active']:
        return 0.0
    return max(0.0, float(row['amount'] or 0)-_finance_assessment_allocated(assessment_id))

def _finance_student_outstanding(student_id, session_id=None):
    """Each fee charged to a student, with what is still owed.

    "Paid" here always means allocated to that specific fee — the same
    figure a fee can never show less debt than it genuinely carries just
    because a payment was overpaid or miscategorised against a different
    fee; that money simply doesn't count against this one until a staff
    member actually applies it here.

    session_id=None returns every fee across every session the student has
    ever been charged in. The allocated total is a correlated subquery so
    the whole statement is one round trip rather than one per fee.
    """
    allocated=(select(func.sum(FinancePaymentAllocation.amount))
               .select_from(FinancePaymentAllocation)
               .join(FinancePayment,FinancePayment.id==FinancePaymentAllocation.payment_id)
               .where(FinancePaymentAllocation.assessment_id==FinanceFeeAssessment.id,
                      FinancePayment.status=='posted')
               .correlate(FinanceFeeAssessment).scalar_subquery())
    scope=[FinanceFeeAssessment.student_id==student_id,FinanceFeeAssessment.active==1]
    if session_id is not None:
        scope.append(FinanceFeeAssessment.session_id==session_id)
    rows=all_rows(select(FinanceFeeAssessment.id,FinanceFeeAssessment.student_id,
                         FinanceFeeAssessment.session_id,FinanceFeeAssessment.category,
                         FinanceFeeAssessment.amount,FinanceFeeAssessment.due_date,
                         FinanceFeeAssessment.term,FinanceFeeAssessment.active,
                         FinanceFeeAssessment.fee_item_id,
                         func.coalesce(allocated,0).label('allocated'))
                  .where(*scope)
                  .order_by(FinanceFeeAssessment.id))
    result=[]
    for row in rows:
        assessed=float(row['amount'] or 0)
        paid=float(row['allocated'] or 0)
        item=dict(row)
        item['assessed']=assessed
        item['paid']=paid
        item['outstanding']=max(0.0, assessed-paid)
        if paid>=assessed and assessed>0: item['status']='Paid'
        elif paid>0: item['status']='Part Paid'
        else: item['status']='Unpaid'
        result.append(item)
    return result

def _finance_student_lifetime_totals(student_id, session_id=None):
    """Assessed/paid/outstanding summed from the itemised, allocation-based
    breakdown — the authoritative balance, matching exactly what the
    per-fee table shows so the two numbers can never disagree.

    session_id=None totals across every session the student has ever been
    charged or paid in, so a balance left over from a previous academic
    session is never hidden just because the school has moved on to a new
    one. Also reports how much of the student's posted payments has not yet
    been applied to any specific fee (an unallocated credit) — surfaced
    separately so a just-made payment is never silently invisible, without
    letting it mask real debt on an unrelated fee.
    """
    rows=_finance_student_outstanding(student_id,session_id)
    assessed=sum(r['assessed'] for r in rows)
    paid=sum(r['paid'] for r in rows)
    outstanding=sum(r['outstanding'] for r in rows)
    paid_scope=[FinancePayment.student_id==student_id,FinancePayment.status=='posted']
    if session_id is not None:
        paid_scope.append(FinancePayment.session_id==session_id)
    raw_paid=float(one_scalar(
        select(func.coalesce(func.sum(FinancePayment.amount),0)).where(*paid_scope), 0))
    unallocated=max(0.0, raw_paid-paid)
    return {'assessed':assessed,'paid':paid,'outstanding':outstanding,'unallocated':unallocated}

def _finance_student_sessions_with_balance(student_id, exclude_session_id=None):
    """Which academic sessions still carry an outstanding balance.

    Used to surface old, unresolved balances a parent (or admin) might
    otherwise never see once the school has moved on to a new session.
    """
    rows=all_rows(
        select(AcademicSession.id,AcademicSession.name)
        .join(FinanceFeeAssessment,FinanceFeeAssessment.session_id==AcademicSession.id)
        .where(FinanceFeeAssessment.student_id==student_id,FinanceFeeAssessment.active==1)
        .distinct())
    result=[]
    for row in rows:
        if exclude_session_id is not None and int(row['id'])==int(exclude_session_id):
            continue
        totals=_finance_student_lifetime_totals(student_id,row['id'])
        if totals['outstanding']>0.005:
            result.append({'id':row['id'],'name':row['name'],'outstanding':totals['outstanding']})
    return result

def _promotion_classes(include_inactive=False):
    """School classes are authoritative.

    include_inactive=True is intentionally supported because an inactive
    class remains part of the school's configured structure.
    """
    stmt=select(SchoolClass)
    if not include_inactive:
        stmt=stmt.where(SchoolClass.active==1)
    return db.session.scalars(stmt.order_by(SchoolClass.level_order,SchoolClass.id)).all()

def _promotion_progressions():
    FromClass=sa.orm.aliased(SchoolClass)
    ToClass=sa.orm.aliased(SchoolClass)
    return [_flatten(r,'SchoolClassProgression','from_class_name','from_stage','from_active',
                     'to_class_name','to_stage','to_active') for r in all_rows(
        select(SchoolClassProgression,
               FromClass.name.label('from_class_name'),FromClass.stage.label('from_stage'),
               FromClass.active.label('from_active'),
               ToClass.name.label('to_class_name'),ToClass.stage.label('to_stage'),
               ToClass.active.label('to_active'))
        .join(FromClass,FromClass.id==SchoolClassProgression.from_class_id)
        .outerjoin(ToClass,ToClass.id==SchoolClassProgression.to_class_id)
        .where(SchoolClassProgression.active==1)
        .order_by(FromClass.level_order,FromClass.id))]

def _promotion_next_class(class_id):
    """The explicitly configured destination for a class.

    level_order is NEVER used to infer the next class.
    """
    return one(select(SchoolClassProgression.to_class_id,
                      SchoolClass.name.label('to_class_name'),
                      SchoolClass.active.label('to_class_active'))
               .select_from(SchoolClassProgression)
               .outerjoin(SchoolClass,SchoolClass.id==SchoolClassProgression.to_class_id)
               .where(SchoolClassProgression.from_class_id==class_id,
                      SchoolClassProgression.active==1)
               .order_by(SchoolClassProgression.id.desc()).limit(1))

def _promotion_current_enrolments(session_id):
    return all_rows(
        select(StudentEnrolment.id.label('enrolment_id'),StudentEnrolment.student_id,
               StudentEnrolment.class_id,Student.admission_no,Student.first_name,
               Student.middle_name,Student.last_name,
               Student.active.label('student_active'),
               SchoolClass.name.label('class_name'),SchoolClass.stage,
               SchoolClass.level_order)
        .join(Student,Student.id==StudentEnrolment.student_id)
        .join(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
        .where(StudentEnrolment.session_id==session_id,StudentEnrolment.active==1,
               Student.active==1)
        .order_by(SchoolClass.level_order,SchoolClass.id,
                  Student.last_name,Student.first_name))

def _promotion_run_has_uncommitted(from_session_id, to_session_id):
    return one(select(AcademicPromotionRun.id,AcademicPromotionRun.status)
               .where(AcademicPromotionRun.from_session_id==from_session_id,
                      AcademicPromotionRun.to_session_id==to_session_id,
                      AcademicPromotionRun.status.in_(('draft','approved')))
               .order_by(AcademicPromotionRun.id.desc()).limit(1))

def _promotion_target(class_id):
    return obj(SchoolClass, class_id) if class_id else None

def _promotion_target_status(class_id):
    target=_promotion_target(class_id)
    if not target:
        return False, "Destination class does not exist."
    if int(target.active or 0) != 1:
        return False, f"Destination class '{target.name}' is currently inactive."
    return True, None

def _promotion_existing_target_enrolment(student_id, session_id):
    """Because student_enrolments is unique per student/session, ANY existing
    row must be treated as a conflict."""
    return db.session.scalars(select(StudentEnrolment).where(
        StudentEnrolment.student_id==student_id,
        StudentEnrolment.session_id==session_id).limit(1)).first()

def _promotion_validate_run(run_id):
    """Validate an entire run immediately before approval or commit.

    Returns (True, []) or (False, [error, ...]).
    """
    run=obj(AcademicPromotionRun, run_id)
    if not run:
        return False, ["Promotion run not found."]

    items=db.session.scalars(select(AcademicPromotionItem)
        .where(AcademicPromotionItem.run_id==run_id)
        .order_by(AcademicPromotionItem.id)).all()
    if not items:
        return False, ["Promotion run contains no students."]

    errors=[]
    for item in items:
        decision=(item.decision or item.action or 'promote').strip().lower()

        if decision not in PROMOTION_DECISIONS:
            errors.append(f"Student ID {item.student_id}: invalid decision '{decision}'.")
            continue

        source=obj(SchoolClass, item.source_class_id)
        if not source:
            errors.append(f"Student ID {item.student_id}: source class no longer exists.")
            continue

        if decision=='promote':
            destination_id=item.final_class_id or item.proposed_class_id
            if not destination_id:
                errors.append(f"Student ID {item.student_id}: no progression is configured for "
                              f"'{source.name}'. Review Required.")
                continue
            ok,reason=_promotion_target_status(destination_id)
            if not ok:
                errors.append(f"Student ID {item.student_id}: {reason}")

        elif decision=='repeat':
            ok,reason=_promotion_target_status(item.source_class_id)
            if not ok:
                errors.append(f"Student ID {item.student_id}: {reason}")

        elif decision=='transfer':
            destination_id=item.final_class_id
            if not destination_id:
                errors.append(f"Student ID {item.student_id}: Transfer requires a destination class.")
                continue
            ok,reason=_promotion_target_status(destination_id)
            if not ok:
                errors.append(f"Student ID {item.student_id}: {reason}")

        elif decision in ('hold','withdraw','graduate'):
            # These decisions intentionally create no new enrolment.
            continue

        # Anything that creates a target-session enrolment needs that session
        # to be free for this student.
        if decision in ('promote','repeat','transfer'):
            existing=_promotion_existing_target_enrolment(item.student_id, run.to_session_id)
            if existing:
                errors.append(f"Student ID {item.student_id}: target session already has "
                              f"enrolment {existing.id}.")

    return len(errors)==0, errors

def _promotion_audit(action, entity_id, details):
    try:
        audit_log(
            action,
            "school",
            "academic_promotion",
            entity_id,
            details
        )
    except Exception:
        pass

@app.route('/admin/school/promotion', methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_promotion():
    me=current_admin()
    sessions=db.session.scalars(select(AcademicSession)
        .where(AcademicSession.active==1)
        .order_by(AcademicSession.id.desc())).all()
    classes=_promotion_classes(include_inactive=True)
    progressions=_promotion_progressions()
    current=_school_current_session()

    if request.method=='POST':
        action=request.form.get('action','').strip()
        try:
            from_session_id=int(request.form.get('from_session_id','0'))
            to_session_id=int(request.form.get('to_session_id','0'))
        except (TypeError,ValueError):
            from_session_id=to_session_id=0

        def active_session(sid):
            return db.session.scalars(select(AcademicSession).where(
                AcademicSession.id==sid,AcademicSession.active==1)).first()

        from_session=active_session(from_session_id)
        to_session=active_session(to_session_id)

        if not from_session or not to_session:
            flash('Select valid source and destination academic sessions.','error')
            return redirect(url_for('admin_school_promotion'))

        if from_session_id==to_session_id:
            flash('Source and destination sessions must be different.','error')
            return redirect(url_for('admin_school_promotion'))

        if action=='generate':
            existing=_promotion_run_has_uncommitted(from_session_id,to_session_id)
            if existing:
                flash(f'Promotion run #{existing["id"]} already exists with status '
                      f'{existing["status"]}. Review it before creating another.','error')
                return redirect(url_for('admin_school_promotion_run',run_id=existing['id']))

            now=datetime.now(timezone.utc).isoformat()
            run=AcademicPromotionRun(from_session_id=from_session_id,
                                     to_session_id=to_session_id,status='draft',
                                     created_by=me['id'],created_at=now)
            db.session.add(run); db.session.flush()

            enrolments=_promotion_current_enrolments(from_session_id)
            # Seed every student with the destination their class progression
            # configures; a class with no progression is left for review.
            for row in enrolments:
                nxt=_promotion_next_class(row['class_id'])
                db.session.add(AcademicPromotionItem(
                    run_id=run.id,student_id=row['student_id'],
                    source_enrolment_id=row['enrolment_id'],
                    source_class_id=row['class_id'],
                    proposed_class_id=nxt['to_class_id'] if nxt else None,
                    final_class_id=None,action='promote',reason=None,
                    created_at=now,decision='promote'))
            db.session.commit()

            _promotion_audit('academic_promotion_draft_created',run.id,{
                'from_session_id':from_session_id,
                'to_session_id':to_session_id,
                'student_count':len(enrolments)})

            flash(f'Promotion draft #{run.id} created for {len(enrolments)} student(s).','success')
            return redirect(url_for('admin_school_promotion_run',run_id=run.id))

    return render_template(
        'admin_school_promotion.html',
        sessions=sessions,
        classes=classes,
        progressions=progressions,
        current_session=current)

@app.route('/admin/school/promotion/<int:run_id>', methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_promotion_run(run_id):
    me=current_admin()
    FromSession=sa.orm.aliased(AcademicSession)
    ToSession=sa.orm.aliased(AcademicSession)
    raw=one(select(AcademicPromotionRun,
                   FromSession.name.label('from_session_name'),
                   ToSession.name.label('to_session_name'))
            .join(FromSession,FromSession.id==AcademicPromotionRun.from_session_id)
            .join(ToSession,ToSession.id==AcademicPromotionRun.to_session_id)
            .where(AcademicPromotionRun.id==run_id))
    if not raw:
        abort(404)
    run=_flatten(raw,'AcademicPromotionRun','from_session_name','to_session_name')
    run_row=obj(AcademicPromotionRun,run_id)

    def back():
        return redirect(url_for('admin_school_promotion_run',run_id=run_id))

    if request.method=='POST':
        action=request.form.get('action','').strip()

        if action=='save':
            if run['status'] not in ('draft','approved'):
                flash('This promotion run can no longer be edited.','error'); return back()

            for raw_id in request.form.getlist('item_id'):
                try: item_id=int(raw_id)
                except (TypeError,ValueError): continue

                item=db.session.scalars(select(AcademicPromotionItem).where(
                    AcademicPromotionItem.id==item_id,
                    AcademicPromotionItem.run_id==run_id)).first()
                if not item: continue

                decision=request.form.get(f'action_{item_id}',
                                          item.action or 'promote').strip().lower()
                if decision not in PROMOTION_DECISIONS:
                    decision='promote'

                raw_target=request.form.get(f'target_{item_id}','').strip()
                target_id=None
                if raw_target:
                    try: target_id=int(raw_target)
                    except (TypeError,ValueError): target_id=None

                if decision=='promote':
                    # Normal promotion follows the configured default.
                    target_id=item.proposed_class_id
                elif decision=='repeat':
                    target_id=item.source_class_id
                elif decision in ('graduate','withdraw','hold'):
                    target_id=None
                elif decision=='transfer':
                    # Transfer is the deliberate exception mechanism.
                    if not target_id:
                        target_id=item.proposed_class_id

                reason=request.form.get(f'reason_{item_id}','').strip()

                # Validate a manually selected destination immediately.
                if decision in ('promote','repeat','transfer'):
                    if not target_id:
                        flash(f'Student item #{item_id} requires a destination class.','error')
                        return back()
                    if not _promotion_target(target_id):
                        flash(f'Selected destination for item #{item_id} does not exist.','error')
                        return back()

                item.final_class_id=target_id
                item.action=decision
                item.decision=decision
                item.reason=reason or None
                item.updated_at=datetime.now(timezone.utc).isoformat()

            # Saving a draft returns it to draft state.
            run_row.status='draft'; run_row.approved_by=None; run_row.approved_at=None
            db.session.commit()
            _promotion_audit('academic_promotion_draft_updated',run_id,{'admin_id':me['id']})
            flash('Promotion draft saved.','success')
            return back()

        if action=='approve':
            if run['status']!='draft':
                flash('Only a draft promotion run can be approved.','error'); return back()
            valid,errors=_promotion_validate_run(run_id)
            if not valid:
                flash('Promotion approval blocked. '+' '.join(errors[:8]),'error'); return back()
            run_row.status='approved'; run_row.approved_by=me['id']
            run_row.approved_at=datetime.now(timezone.utc).isoformat()
            db.session.commit()
            _promotion_audit('academic_promotion_approved',run_id,{'admin_id':me['id']})
            flash('Promotion run approved. It is ready to commit.','success')
            return back()

        if action=='commit':
            if run['status']!='approved':
                flash('Only an approved promotion run can be committed.','error'); return back()
            valid,errors=_promotion_validate_run(run_id)
            if not valid:
                flash('Promotion commit blocked. '+' '.join(errors[:8]),'error'); return back()

            now=datetime.now(timezone.utc).isoformat()
            committed_count=0
            decision_counts={}
            try:
                # The session owns one transaction for the whole commit, so any
                # failure below rolls the entire promotion back.
                valid,errors=_promotion_validate_run(run_id)
                if not valid:
                    raise ValueError(' '.join(errors[:8]))

                items=db.session.scalars(select(AcademicPromotionItem)
                    .where(AcademicPromotionItem.run_id==run_id)
                    .order_by(AcademicPromotionItem.id)).all()

                for item in items:
                    decision=(item.decision or item.action or 'promote').strip().lower()
                    decision_counts[decision]=decision_counts.get(decision,0)+1

                    # These decisions do not create a new enrolment.
                    if decision in ('graduate','withdraw','hold'):
                        item.committed_enrolment_id=None
                        continue

                    if decision=='promote':
                        target_id=item.proposed_class_id
                    elif decision=='repeat':
                        target_id=item.source_class_id
                    elif decision=='transfer':
                        target_id=item.final_class_id
                    else:
                        raise ValueError(f'Unsupported promotion decision: {decision}')

                    if not target_id:
                        raise ValueError(f'Student ID {item.student_id} has no destination class.')

                    target=obj(SchoolClass,target_id)
                    if not target:
                        raise ValueError(f'Destination class {target_id} no longer exists.')
                    if int(target.active or 0)!=1:
                        raise ValueError(f"Destination class '{target.name}' is inactive.")

                    # The database has a unique student/session identity.
                    existing=one_scalar(select(StudentEnrolment.id).where(
                        StudentEnrolment.student_id==item.student_id,
                        StudentEnrolment.session_id==run['to_session_id']).limit(1))
                    if existing:
                        raise ValueError(f'Student ID {item.student_id} already has '
                                         f'target-session enrolment {existing}.')

                    enrolment=StudentEnrolment(student_id=item.student_id,class_id=target_id,
                                               session_id=run['to_session_id'],
                                               enrolled_at=now[:10],active=1)
                    db.session.add(enrolment); db.session.flush()

                    # Close the previous session's active history record only
                    # after the new enrolment has been created.
                    db.session.execute(sa_update(StudentEnrollmentHistory)
                        .where(StudentEnrollmentHistory.student_id==item.student_id,
                               StudentEnrollmentHistory.session_id==run['from_session_id'],
                               StudentEnrollmentHistory.class_id==item.source_class_id,
                               StudentEnrollmentHistory.active==1)
                        .values(completed_at=now,active=0))

                    # Preserve a new historical record for the destination.
                    db.session.add(StudentEnrollmentHistory(
                        student_id=item.student_id,session_id=run['to_session_id'],
                        level_name=target.name,class_id=target_id,
                        enrolled_at=now[:10],completed_at=None,active=1,
                        notes=f'Academic promotion run #{run_id}: {decision}',
                        created_at=now))

                    item.committed_enrolment_id=enrolment.id
                    committed_count+=1

                run_row.status='committed'
                run_row.committed_by=me['id']
                run_row.committed_at=now
                db.session.commit()

            except Exception as exc:
                db.session.rollback()
                flash(f'Promotion commit failed and was rolled back: {exc}','error')
                return back()

            _promotion_audit('academic_promotion_committed',run_id,{
                'admin_id':me['id'],
                'committed_students':committed_count,
                'decisions':decision_counts})
            flash(f'Promotion run #{run_id} committed successfully. '
                  f'{committed_count} new enrolment(s) created.','success')
            return back()

        if action=='cancel':
            if run['status'] not in ('draft','approved'):
                flash('This promotion run cannot be cancelled.','error'); return back()
            run_row.status='cancelled'; run_row.cancelled_by=me['id']
            run_row.cancelled_at=datetime.now(timezone.utc).isoformat()
            db.session.commit()
            _promotion_audit('academic_promotion_cancelled',run_id,{'admin_id':me['id']})
            flash('Promotion run cancelled.','success')
            return redirect(url_for('admin_school_promotion'))

    # ---------------------------------------------------------------
    # DISPLAY
    # ---------------------------------------------------------------
    SourceClass=sa.orm.aliased(SchoolClass)
    ProposedClass=sa.orm.aliased(SchoolClass)
    FinalClass=sa.orm.aliased(SchoolClass)
    items=[_flatten(r,'AcademicPromotionItem','admission_no','first_name','middle_name',
                    'last_name','source_class_name','proposed_class_name',
                    'proposed_class_active','final_class_name') for r in all_rows(
        select(AcademicPromotionItem,Student.admission_no,Student.first_name,
               Student.middle_name,Student.last_name,
               SourceClass.name.label('source_class_name'),
               ProposedClass.name.label('proposed_class_name'),
               ProposedClass.active.label('proposed_class_active'),
               FinalClass.name.label('final_class_name'))
        .join(Student,Student.id==AcademicPromotionItem.student_id)
        .join(SourceClass,SourceClass.id==AcademicPromotionItem.source_class_id)
        .outerjoin(ProposedClass,ProposedClass.id==AcademicPromotionItem.proposed_class_id)
        .outerjoin(FinalClass,FinalClass.id==AcademicPromotionItem.final_class_id)
        .where(AcademicPromotionItem.run_id==run_id)
        .order_by(SourceClass.level_order,SourceClass.id,
                  Student.last_name,Student.first_name))]

    classes=_promotion_classes(include_inactive=True)

    summary={'total':len(items),'promote':0,'repeat':0,'transfer':0,
             'graduate':0,'withdraw':0,'hold':0,'review':0}
    for item in items:
        decision=(item['decision'] or item['action'] or 'promote').strip().lower()
        if decision in summary:
            summary[decision]+=1
        if decision=='promote':
            # A promotion with no destination, or one pointing at an inactive
            # class, needs a human decision before this run can commit.
            if not item['proposed_class_id']:
                summary['review']+=1
            elif int(item['proposed_class_active'] or 0)!=1:
                summary['review']+=1

    return render_template(
        'admin_school_promotion_run.html',
        run=run,
        items=items,
        classes=classes,
        summary=summary,
        actions=PROMOTION_ACTIONS)

@app.route('/admin/school/promotion/progressions', methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_promotion_progressions():
    me=current_admin()

    if not is_super_admin():
        return admin_access_error('Promotion progression configuration')

    if request.method=='POST':
        action=request.form.get('action','').strip()

        if action=='save':
            try: from_class_id=int(request.form.get('from_class_id','0'))
            except (TypeError,ValueError): from_class_id=0

            raw_to=request.form.get('to_class_id','').strip()
            try: to_class_id=int(raw_to) if raw_to else None
            except (TypeError,ValueError): to_class_id=None

            if not obj(SchoolClass,from_class_id):
                flash('Select a valid source class.','error')
                return redirect(url_for('admin_school_promotion_progressions'))

            # The destination may be ACTIVE or INACTIVE. Inactive does not mean
            # nonexistent, so only existence is checked here; availability is
            # re-checked when a run is approved or committed.
            if to_class_id and not obj(SchoolClass,to_class_id):
                flash('Select a valid destination class.','error')
                return redirect(url_for('admin_school_promotion_progressions'))

            now=datetime.now(timezone.utc).isoformat()
            existing=db.session.scalars(select(SchoolClassProgression)
                .where(SchoolClassProgression.from_class_id==from_class_id,
                       SchoolClassProgression.active==1)
                .order_by(SchoolClassProgression.id.desc()).limit(1)).first()

            if existing:
                existing.to_class_id=to_class_id
                existing.updated_at=now
                existing.updated_by=me['id']
            else:
                db.session.add(SchoolClassProgression(
                    from_class_id=from_class_id,to_class_id=to_class_id,active=1,
                    created_by=me['id'],created_at=now,updated_at=now,updated_by=me['id']))

            db.session.commit()

            _promotion_audit('academic_promotion_progression_updated',from_class_id,{
                'from_class_id':from_class_id,
                'to_class_id':to_class_id,
                'updated_by':me['id']})

            if to_class_id:
                flash('Progression rule saved. The destination remains subject to its '
                      'current class availability status.','success')
            else:
                flash('Progression cleared. Students from this class will require review.','success')

            return redirect(url_for('admin_school_promotion_progressions'))

    return render_template(
        'admin_school_promotion_progressions.html',
        classes=_promotion_classes(include_inactive=True),
        progressions=_promotion_progressions())

@app.route('/admin/school/sessions', methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_sessions():
    me=current_admin()
    if not is_super_admin():
        return admin_access_error('Academic session management')

    if request.method=='POST':
        action=request.form.get('action','').strip()
        now=datetime.now(timezone.utc).isoformat()

        if action=='create':
            name=request.form.get('name','').strip()
            start_date=request.form.get('start_date','').strip() or None
            end_date=request.form.get('end_date','').strip() or None
            make_current=request.form.get('make_current')=='1'
            errors=[]
            if not name: errors.append('Enter a name for the academic session, e.g. 2027/2028.')
            elif one_scalar(select(AcademicSession.id).where(AcademicSession.name==name)):
                errors.append(f'An academic session named "{name}" already exists.')
            if start_date and end_date and end_date<start_date:
                errors.append('The end date cannot be before the start date.')
            if errors:
                for e in errors: flash(e,'error')
                return redirect(url_for('admin_school_sessions'))
            if make_current:
                db.session.execute(sa_update(AcademicSession).values(is_current=0))
            session_row=AcademicSession(name=name,start_date=start_date,end_date=end_date,
                is_current=1 if make_current else 0,active=1,created_at=now)
            db.session.add(session_row); db.session.flush()
            db.session.commit()
            audit_log('academic_session_created','school','academic_session',session_row.id,
                      {'name':name,'made_current':make_current})
            flash(f'Academic session "{name}" created.'+(' Set as the current session.' if make_current else ''),'success')
            return redirect(url_for('admin_school_sessions'))

        if action=='set_ca_weights':
            try:
                test_w=float(request.form.get('ca_weight_test','0') or 0)
                assignment_w=float(request.form.get('ca_weight_assignment','0') or 0)
                project_w=float(request.form.get('ca_weight_project','0') or 0)
            except (TypeError,ValueError):
                flash('Enter valid numbers for each weight.','error'); return redirect(url_for('admin_school_sessions'))
            total=test_w+assignment_w+project_w
            if test_w<0 or assignment_w<0 or project_w<0:
                flash('Weights cannot be negative.','error'); return redirect(url_for('admin_school_sessions'))
            if abs(total-CA_MAX_SCORE)>0.001:
                flash(f'Test + Assignment + Project weights must add up to exactly {CA_MAX_SCORE:g} (currently {total:g}).','error')
                return redirect(url_for('admin_school_sessions'))
            _set_ca_weights({'test':test_w,'assignment':assignment_w,'project':project_w},me['id'])
            audit_log('ca_weights_updated','school','ca_weights',None,
                      {'test':test_w,'assignment':assignment_w,'project':project_w})
            flash('Continuous assessment weighting updated.','success')
            return redirect(url_for('admin_school_sessions'))

        try: session_id=int(request.form.get('session_id','0'))
        except (TypeError,ValueError): session_id=0
        row=obj(AcademicSession,session_id)
        if not row:
            flash('Academic session not found.','error'); return redirect(url_for('admin_school_sessions'))

        if action=='set_current':
            if not row.active:
                flash('Reactivate this session before making it current.','error')
                return redirect(url_for('admin_school_sessions'))
            db.session.execute(sa_update(AcademicSession).values(is_current=0))
            row.is_current=1
            db.session.commit()
            audit_log('academic_session_set_current','school','academic_session',row.id,{'name':row.name})
            flash(f'"{row.name}" is now the current academic session.','success')
        elif action=='archive':
            if row.is_current:
                flash('Set another session as current before archiving this one.','error')
                return redirect(url_for('admin_school_sessions'))
            row.active=0
            db.session.commit()
            audit_log('academic_session_archived','school','academic_session',row.id,{'name':row.name})
            flash(f'"{row.name}" has been archived.','success')
        elif action=='reactivate':
            row.active=1
            db.session.commit()
            audit_log('academic_session_reactivated','school','academic_session',row.id,{'name':row.name})
            flash(f'"{row.name}" has been reactivated.','success')
        else:
            flash('Unrecognised action.','error')
        return redirect(url_for('admin_school_sessions'))

    sessions=db.session.scalars(select(AcademicSession)
        .order_by(AcademicSession.is_current.desc(),AcademicSession.id.desc())).all()
    return render_template('admin_school_sessions.html',sessions=sessions,
        ca_weights=_ca_weights(),ca_max=CA_MAX_SCORE,exam_max=EXAM_MAX_SCORE,terms=ACADEMIC_TERMS)

@app.route('/admin/finance/payments/<int:payment_id>/allocate',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_finance_payment_allocate(payment_id):
    me=current_admin()
    raw=one(select(FinancePayment,Student.first_name,Student.middle_name,
                   Student.last_name,Student.admission_no)
            .join(Student,Student.id==FinancePayment.student_id)
            .where(FinancePayment.id==payment_id))
    if not raw:
        abort(404)
    payment=_flatten(raw,'FinancePayment','first_name','middle_name','last_name','admission_no')

    if not _finance_can_view_all(me) and payment['recorded_by']!=me['id']:
        return admin_access_error('finance.view_own')

    back=redirect(url_for('admin_finance_payment_allocate',payment_id=payment_id))

    if payment['status']!='posted':
        flash('Only a posted payment can be allocated.','error')
        return redirect(url_for('admin_finance_receipt',payment_id=payment_id))

    if request.method=='POST':
        raw_allocations=request.form.get('allocations','').strip()
        try:
            submitted=json.loads(raw_allocations) if raw_allocations else {}
        except ValueError:
            flash('The allocation data is invalid.','error'); return back

        if not isinstance(submitted,dict):
            flash('The allocation data is invalid.','error'); return back

        payment_amount=float(payment['amount'] or 0)
        existing_allocated=_finance_payment_allocated(payment_id)
        available_payment=payment_amount-existing_allocated

        if available_payment<0:
            flash('This payment already contains invalid allocations.','error'); return back

        clean_allocations=[]
        requested_total=0.0

        for raw_assessment_id,raw_amount in submitted.items():
            try:
                assessment_id=int(raw_assessment_id); amount=float(raw_amount)
            except (TypeError,ValueError):
                flash('One or more allocation amounts are invalid.','error'); return back

            if amount<=0:
                continue

            assessment=obj(FinanceFeeAssessment,assessment_id)
            if not assessment:
                flash('One or more selected assessments could not be found.','error'); return back
            if not assessment.active:
                flash('A selected assessment is inactive.','error'); return back
            # Money may only move between records belonging to one student.
            if int(assessment.student_id)!=int(payment['student_id']):
                flash('A payment can only be allocated to assessments belonging to the same student.','error'); return back

            already_allocated=_finance_assessment_allocated(assessment_id)
            assessment_balance=max(0.0, float(assessment.amount or 0)-already_allocated)

            if assessment_balance<=0.000001:
                flash(f'{assessment.category} has already been fully paid and cannot receive a further allocation.','error'); return back
            if amount>assessment_balance+0.000001:
                flash(f'Allocation for {assessment.category} exceeds its outstanding balance.','error'); return back

            requested_total+=amount
            clean_allocations.append((assessment_id,amount))

        if requested_total>available_payment+0.000001:
            flash('The total allocation exceeds the payment balance.','error'); return back

        if not clean_allocations:
            flash('Enter at least one allocation amount.','error'); return back

        now=datetime.now(timezone.utc).isoformat()
        db.session.add_all([FinancePaymentAllocation(
            payment_id=payment_id,assessment_id=assessment_id,amount=amount,
            created_by=me['id'],created_at=now)
            for assessment_id,amount in clean_allocations])
        db.session.commit()

        remaining=max(0.0, available_payment-requested_total)
        audit_log('finance_payment_allocated','finance','payment',payment_id,{
            'allocated_total':requested_total,
            'allocation_count':len(clean_allocations),
            'remaining_unallocated':remaining})

        if remaining>0.000001:
            flash(f'₦{requested_total:,.2f} allocated successfully. ₦{remaining:,.2f} remains unallocated.','success')
        else:
            flash(f'₦{requested_total:,.2f} allocated successfully. The payment is fully allocated.','success')
        return redirect(url_for('admin_finance_receipt',payment_id=payment_id))

    # GET
    # Only fees still owing are offered here — a fully paid item has nothing
    # left to apply this payment to, so it must not appear as a choice at all.
    outstanding=[item for item in _finance_student_outstanding(payment['student_id'],payment['session_id'])
                 if item['outstanding']>0.000001]
    payment_allocated=_finance_payment_allocated(payment_id)
    available_payment=max(0.0, float(payment['amount'] or 0)-payment_allocated)
    existing=[_flatten(r,'FinancePaymentAllocation','category','assessed_amount')
              for r in all_rows(
        select(FinancePaymentAllocation,FinanceFeeAssessment.category,
               FinanceFeeAssessment.amount.label('assessed_amount'))
        .join(FinanceFeeAssessment,FinanceFeeAssessment.id==FinancePaymentAllocation.assessment_id)
        .where(FinancePaymentAllocation.payment_id==payment_id)
        .order_by(FinancePaymentAllocation.id))]

    return render_template(
        'finance_payment_allocate.html',
        payment=payment,
        outstanding=outstanding,
        existing=existing,
        payment_allocated=payment_allocated,
        available_payment=available_payment)

@app.route('/admin/finance/students/<int:student_id>/account')
@admin_required
def admin_finance_student_account(student_id):
    me = current_admin()
    student = one(select(Student.id,Student.admission_no,Student.first_name,
                         Student.middle_name,Student.last_name)
                  .where(Student.id==student_id,Student.active==1))
    if not student:
        abort(404)

    sessions = _active_sessions()

    requested_session = request.args.get('session_id','').strip()
    try:
        session_id = int(requested_session) if requested_session else None
    except (TypeError,ValueError):
        session_id = None
    if not session_id:
        current = _school_current_session()
        session_id = current['id'] if current else None

    session_row = obj(AcademicSession, session_id) if session_id else None

    account = _finance_student_outstanding(student_id, session_id) if session_row else []

    total_assessed = sum(item['assessed'] for item in account)
    total_paid = sum(item['paid'] for item in account)
    total_outstanding = sum(item['outstanding'] for item in account)

    payments = db.session.scalars(select(FinancePayment)
        .where(FinancePayment.student_id==student_id)
        .order_by(FinancePayment.paid_at.desc(),FinancePayment.id.desc())).all()

    return render_template(
        'finance_student_account.html',
        student=student,
        sessions=sessions,
        session=session_row,
        account=account,
        total_assessed=total_assessed,
        total_paid=total_paid,
        total_outstanding=total_outstanding,
        payments=payments)

@app.route('/admin/finance')
@admin_required
def admin_finance_dashboard():
    me=current_admin()
    own=not _finance_can_view_all(me)
    # A cashier without finance.view_all only ever sees their own takings.
    scope=[FinancePayment.status=='posted']
    if own: scope.append(FinancePayment.recorded_by==me['id'])
    today=datetime.now().strftime('%Y-%m-%d'); month=datetime.now().strftime('%Y-%m')
    day=func.substr(FinancePayment.paid_at,1,10)
    mon=func.substr(FinancePayment.paid_at,1,7)

    def total(*extra):
        return one_scalar(select(func.coalesce(func.sum(FinancePayment.amount),0))
                          .where(*scope,*extra), 0)

    today_total=total(day==today)
    month_total=total(mon==month)
    count_today=one_scalar(select(func.count()).select_from(FinancePayment)
                           .where(*scope,day==today), 0)
    cash=total(day==today,func.lower(FinancePayment.method)=='cash')
    bank=total(day==today,func.lower(FinancePayment.method)!='cash')
    outstanding=None
    if not own:
        assessed=one_scalar(select(func.coalesce(func.sum(FinanceFeeAssessment.amount),0))
                            .where(FinanceFeeAssessment.active==1), 0)
        paid=one_scalar(select(func.coalesce(func.sum(FinancePayment.amount),0))
                        .where(FinancePayment.status=='posted'), 0)
        allocated=one_scalar(
            select(func.coalesce(func.sum(FinancePaymentAllocation.amount),0))
            .select_from(FinancePaymentAllocation)
            .join(FinancePayment,FinancePayment.id==FinancePaymentAllocation.payment_id)
            .join(FinanceFeeAssessment,FinanceFeeAssessment.id==FinancePaymentAllocation.assessment_id)
            .where(FinancePayment.status=='posted',FinanceFeeAssessment.active==1), 0)
        outstanding=max(0, float(assessed)-float(allocated))
        unallocated=max(0, float(paid)-float(allocated))
    rows=[_flatten(r,'FinancePayment','first_name','middle_name','last_name','admission_no')
          for r in all_rows(
        select(FinancePayment,Student.first_name,Student.middle_name,
               Student.last_name,Student.admission_no)
        .join(Student,Student.id==FinancePayment.student_id)
        .where(*scope)
        .order_by(FinancePayment.paid_at.desc(),FinancePayment.id.desc()).limit(20))]
    return render_template('finance_dashboard.html',today_total=today_total,month_total=month_total,count_today=count_today,cash=cash,bank=bank,outstanding=outstanding,rows=rows,view_all=not own)

@app.route('/admin/finance/payments/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_finance_record():
    me=current_admin()
    students=all_rows(select(Student.id,Student.admission_no,Student.first_name,
                             Student.middle_name,Student.last_name,
                             Student.guardian_email,Student.guardian_phone)
                      .where(Student.active==1)
                      .order_by(Student.last_name,Student.first_name))
    sessions=db.session.scalars(select(AcademicSession).where(AcademicSession.active==1)
                                .order_by(AcademicSession.id.desc())).all()
    current=_school_current_session()
    if request.method=='POST':
        try: student_id=int(request.form.get('student_id','')); session_id=int(request.form.get('session_id','')); amount=float(request.form.get('amount','0'))
        except (TypeError,ValueError): student_id=session_id=0; amount=0
        category=request.form.get('category','School Fees').strip() or 'School Fees'; method=request.form.get('method','Bank Transfer').strip(); reference=request.form.get('reference','').strip(); paid_at=request.form.get('paid_at','').strip() or datetime.now().strftime('%Y-%m-%d %H:%M'); notes=request.form.get('notes','').strip(); errors=[]
        if not student_id: errors.append('Select a student.')
        if amount<=0: errors.append('Payment amount must be greater than zero.')
        if not session_id: errors.append('Select an academic session.')
        if errors: return render_template('finance_payment_form.html',students=students,sessions=sessions,form=request.form,errors=errors)
        student=db.session.scalars(select(Student).where(
            Student.id==student_id,Student.active==1)).first()
        if not student: return render_template('finance_payment_form.html',students=students,sessions=sessions,form=request.form,errors=['Student not found.'])
        payer_name=request.form.get('payer_name','').strip()
        receipt=_next_receipt_no(); now=datetime.now(timezone.utc).isoformat()
        payment=FinancePayment(receipt_no=receipt,student_id=student_id,session_id=session_id,
            amount=amount,category=category,method=method,reference=reference,
            paid_at=paid_at,recorded_by=me['id'],status='posted',notes=notes,
            created_at=now,payer_name=payer_name)
        db.session.add(payment); db.session.commit()
        audit_log('finance_payment_recorded','finance','payment',payment.id,{'receipt_no':receipt,'amount':amount,'student_id':student_id,'method':method})
        try:
            _notify_parents_payment_recorded(student_id,receipt,amount,category,me['id'])
        except Exception:
            app.logger.exception('Parent payment-recorded notification failed for student %s',student_id)
        return redirect(url_for('admin_finance_receipt',payment_id=payment.id))
    return render_template('finance_payment_form.html',students=students,sessions=sessions,form=None,errors=[],current_session=dict(current) if current else None)

@app.route('/admin/finance/receipts/<int:payment_id>')
@admin_required
def admin_finance_receipt(payment_id):
    me=current_admin(); row=_receipt_payload(payment_id)
    if not row: abort(404)
    if not _finance_can_view_all(me) and row['recorded_by']!=me['id']: return admin_access_error('finance.view_own')
    return render_template('finance_receipt.html',payment=row,signature_path=_receipt_signature_relpath())

@app.route('/admin/finance/receipts/<int:payment_id>/print')
@admin_required
def admin_finance_receipt_print(payment_id):
    me=current_admin(); row=_receipt_payload(payment_id)
    if not row: abort(404)
    if not _finance_can_view_all(me) and row['recorded_by']!=me['id']: return admin_access_error('finance.view_own')
    return render_template('finance_receipt_print.html',payment=row,signature_path=_receipt_signature_relpath())

@app.route('/admin/finance/receipts/<int:payment_id>/pdf')
@admin_required
def admin_finance_receipt_pdf(payment_id):
    me=current_admin(); row=_receipt_payload(payment_id)
    if not row: abort(404)
    if not _finance_can_view_all(me) and row['recorded_by']!=me['id']: return admin_access_error('finance.view_own')
    pdf,_=_receipt_pdf(payment_id); return Response(pdf,mimetype='application/pdf',headers={'Content-Disposition':f'inline; filename={row["receipt_no"]}.pdf'})

def _log_receipt_delivery(payment_id, channel, recipient, ok, msg, actor_id):
    """Record every delivery attempt, successful or not."""
    db.session.add(FinanceDeliveryLog(
        payment_id=payment_id,channel=channel,recipient=recipient,
        status='sent' if ok else 'failed',
        provider_reference=msg if ok else None,
        error_message=None if ok else msg,
        sent_by=actor_id,sent_at=datetime.now(timezone.utc).isoformat()))
    db.session.commit()


@app.post('/admin/finance/receipts/<int:payment_id>/email')
@admin_required
@csrf_protect
def admin_finance_receipt_email(payment_id):
    me=current_admin(); row=_receipt_payload(payment_id)
    if not row: abort(404)
    if not _finance_can_view_all(me) and row['recorded_by']!=me['id']: return admin_access_error('finance.receipt.send')
    ok,msg=_send_email_receipt(payment_id)
    _log_receipt_delivery(payment_id,'email',row['guardian_email'],ok,msg,me['id'])
    audit_log('finance_receipt_email','finance','payment',payment_id,{'success':ok}); flash('Receipt emailed successfully.' if ok else msg,'success' if ok else 'error'); return redirect(url_for('admin_finance_receipt',payment_id=payment_id))

@app.post('/admin/finance/receipts/<int:payment_id>/whatsapp')
@admin_required
@csrf_protect
def admin_finance_receipt_whatsapp(payment_id):
    me=current_admin(); row=_receipt_payload(payment_id)
    if not row: abort(404)
    if not _finance_can_view_all(me) and row['recorded_by']!=me['id']: return admin_access_error('finance.receipt.send')
    ok,msg=_send_whatsapp_receipt(payment_id)
    _log_receipt_delivery(payment_id,'whatsapp',row['guardian_phone'],ok,msg,me['id'])
    audit_log('finance_receipt_whatsapp','finance','payment',payment_id,{'success':ok}); flash('Receipt sent through WhatsApp Business successfully.' if ok else msg,'success' if ok else 'error'); return redirect(url_for('admin_finance_receipt',payment_id=payment_id))

@app.route('/admin/finance/receipt-settings',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_finance_receipt_settings():
    me=current_admin(); errors=[]
    if request.method=='POST':
        action=request.form.get('action','').strip()
        try:
            if action=='remove':
                old=_receipt_signature_abspath(); _set_receipt_signature('',me['id'])
                if old:
                    try: os.remove(old)
                    except OSError: pass
                flash('Authorised signature removed.','success')
            elif action=='draw':
                rel=_save_signature_data_url(request.form.get('signature_data_url',''))
                old=_receipt_signature_abspath(); _set_receipt_signature(rel,me['id'])
                if old:
                    try: os.remove(old)
                    except OSError: pass
                flash('Signature saved.','success')
            elif action=='upload':
                rel=_save_image_upload(request.files.get('signature_file'),'signatures','authorised')
                if not rel: raise ValueError('Choose an image file to upload.')
                old=_receipt_signature_abspath(); _set_receipt_signature(rel,me['id'])
                if old:
                    try: os.remove(old)
                    except OSError: pass
                flash('Signature saved.','success')
            else:
                errors.append('Unrecognised action.')
        except ValueError as exc:
            errors.append(str(exc))
        if not errors:
            audit_log('finance_receipt_signature_updated','finance','school_setting',RECEIPT_SIGNATURE_SETTING_KEY,{'action':action})
            return redirect(url_for('admin_finance_receipt_settings'))
    return render_template('finance_receipt_settings.html',signature_path=_receipt_signature_relpath(),errors=errors)

def _active_classes():
    return all_rows(select(SchoolClass.id,SchoolClass.name,SchoolClass.stage,
                           SchoolClass.level_order,SchoolClass.optional,SchoolClass.active)
                    .where(SchoolClass.active==1)
                    .order_by(SchoolClass.level_order,SchoolClass.name))


def _class_group(row):
    """Which fee band a class belongs to: nursery, primary or college."""
    stage=(row['stage'] or '').strip().lower()
    name=(row['name'] or '').strip().lower()
    if stage in {'jss','sss','college','secondary'} or name.startswith(('jss ','sss ')):
        return 'college'
    if stage=='primary' or name.startswith('primary '):
        return 'primary'
    return 'nursery'


@app.route('/admin/finance/fee-items')
@admin_required
def admin_finance_fee_items():
    items=db.session.scalars(select(FinanceFeeItem)
        .order_by(FinanceFeeItem.optional,FinanceFeeItem.name,FinanceFeeItem.id)).all()
    sessions=_active_sessions()
    current=_school_current_session()
    current_session_id=int(current['id']) if current else 0

    students=all_rows(
        select(Student.id,Student.admission_no,Student.first_name,Student.middle_name,
               Student.last_name,StudentEnrolment.class_id,
               SchoolClass.name.label('class_name'),SchoolClass.stage.label('class_stage'))
        .outerjoin(StudentEnrolment,and_(StudentEnrolment.student_id==Student.id,
                                         StudentEnrolment.session_id==current_session_id,
                                         StudentEnrolment.active==1))
        .outerjoin(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
        .where(Student.active==1)
        .order_by(Student.last_name,Student.first_name,Student.middle_name))

    classes=_active_classes()

    student_class_map={}
    for row in all_rows(
        select(StudentEnrolment.student_id,StudentEnrolment.session_id,
               StudentEnrolment.class_id,SchoolClass.name.label('class_name'),
               SchoolClass.stage.label('class_stage'))
        .join(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
        .where(StudentEnrolment.active==1,SchoolClass.active==1)):
        student_class_map[f"{row['student_id']}:{row['session_id']}"]={
            'class_id': int(row['class_id']),
            'class_name': row['class_name'],
            'stage': row['class_stage'] or 'General'}

    fee_class_map={}
    for row in all_rows(
        select(FinanceFeeItemClass.fee_item_id,SchoolClass.id.label('class_id'),
               SchoolClass.name.label('class_name'),SchoolClass.stage.label('class_stage'))
        .join(SchoolClass,SchoolClass.id==FinanceFeeItemClass.class_id)
        .where(FinanceFeeItemClass.active==1,SchoolClass.active==1)
        .order_by(SchoolClass.level_order,SchoolClass.name)):
        fee_class_map.setdefault(str(row['fee_item_id']),[]).append({
            'id': int(row['class_id']),
            'name': row['class_name'],
            'stage': row['class_stage'] or 'General'})

    class_groups={g:[dict(r) for r in classes if _class_group(r)==g]
                  for g in ('nursery','primary','college')}

    allocated_sq=(select(func.sum(FinancePaymentAllocation.amount))
                  .select_from(FinancePaymentAllocation)
                  .join(FinancePayment,FinancePayment.id==FinancePaymentAllocation.payment_id)
                  .where(FinancePaymentAllocation.assessment_id==FinanceFeeAssessment.id,
                         FinancePayment.status=='posted')
                  .correlate(FinanceFeeAssessment).scalar_subquery())
    assessed_rows=[]
    for r in all_rows(
        select(FinanceFeeAssessment,Student.admission_no,Student.first_name,
               Student.last_name,AcademicSession.name.label('session_name'),
               FinanceFeeItem.name.label('fee_name'),
               func.coalesce(allocated_sq,0).label('allocated'))
        .join(Student,Student.id==FinanceFeeAssessment.student_id)
        .join(AcademicSession,AcademicSession.id==FinanceFeeAssessment.session_id)
        .outerjoin(FinanceFeeItem,FinanceFeeItem.id==FinanceFeeAssessment.fee_item_id)
        .where(FinanceFeeAssessment.active==1)
        .order_by(Student.last_name,Student.first_name,AcademicSession.id.desc(),
                  FinanceFeeAssessment.term,FinanceFeeAssessment.id.desc())
        .limit(2000)):
        row=_flatten(r,'FinanceFeeAssessment','admission_no','first_name','last_name',
                     'session_name','fee_name','allocated')
        assessed_amount=float(row['amount'] or 0)
        paid=float(row['allocated'] or 0)
        if paid>=assessed_amount and assessed_amount>0: row['payment_status']='Paid'
        elif paid>0: row['payment_status']='Part Paid'
        else: row['payment_status']='Unpaid'
        assessed_rows.append(row)

    # Student -> Session -> Term -> items, so a large assessment history can
    # be browsed the way staff actually think about it (find the student,
    # then the session, then the term) instead of one long flat table.
    assessed_by_student={}
    for row in assessed_rows:
        student_key=row['student_id']
        bucket=assessed_by_student.setdefault(student_key,{
            'student_id':student_key,'admission_no':row['admission_no'],
            'first_name':row['first_name'],'last_name':row['last_name'],
            'item_count':0,'total_assessed':0.0,'sessions':{}})
        bucket['item_count']+=1
        bucket['total_assessed']+=float(row['amount'] or 0)
        session_bucket=bucket['sessions'].setdefault(row['session_name'],{})
        term_bucket=session_bucket.setdefault(row['term'] or 'Full Session',[])
        term_bucket.append(row)
    assessed_students=sorted(assessed_by_student.values(),key=lambda b:(b['last_name'] or '',b['first_name'] or ''))

    return render_template(
        'finance_fee_items.html',
        items=items,
        students=students,
        sessions=sessions,
        current_session=current,
        classes=classes,
        class_groups=class_groups,
        student_class_map=student_class_map,
        fee_class_map=fee_class_map,
        assessed_students=assessed_students)

def _legacy_stage_for(selected_rows, selected_ids, all_ids):
    """The single `stage` column predates per-class fee mapping.

    It is kept in step so older screens and reports still read sensibly:
    'All' when the fee covers every class, the shared stage when the selected
    classes agree, and 'Mixed' otherwise.
    """
    if set(selected_ids)==set(all_ids):
        return 'All'
    stages={str(r['stage'] or 'General') for r in selected_rows}
    return next(iter(stages)) if len(stages)==1 else 'Mixed'


@app.route('/admin/finance/fee-items/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_finance_fee_item_new():
    errors=[]
    selected_class_ids=[]
    classes=_active_classes()
    valid_class_ids={int(r['id']) for r in classes}

    if request.method=='POST':
        name=request.form.get('name','').strip()
        category=request.form.get('category','School Fees').strip() or 'School Fees'
        applicability=request.form.get('applicability','Full Session').strip() or 'Full Session'
        required=1 if request.form.get('required')=='1' else 0
        optional=1 if request.form.get('optional')=='1' else 0
        notes=request.form.get('notes','').strip()
        for raw in request.form.getlist('class_ids'):
            try:
                cid=int(raw)
                if cid in valid_class_ids and cid not in selected_class_ids:
                    selected_class_ids.append(cid)
            except (TypeError,ValueError):
                pass
        try:
            amount=float(request.form.get('amount','0'))
        except (TypeError,ValueError):
            amount=-1

        if not name:
            errors.append('Fee item name is required.')
        if category not in FINANCE_FEE_CATEGORIES:
            errors.append('Select a valid fee category.')
        if applicability not in FINANCE_FEE_APPLICABILITY:
            errors.append('Select a valid billing rule.')
        if amount < 0:
            errors.append('Fee amount cannot be negative.')
        if not selected_class_ids:
            errors.append('Select at least one active class this fee applies to.')

        if not errors:
            selected_rows=[r for r in classes if int(r['id']) in selected_class_ids]
            legacy_stage=_legacy_stage_for(selected_rows,selected_class_ids,valid_class_ids)
            now=datetime.now(timezone.utc).isoformat()
            me=current_admin()
            item=FinanceFeeItem(name=name,category=category,stage=legacy_stage,amount=amount,
                applicability=applicability,required=required,optional=optional,active=1,
                notes=notes,created_at=now,updated_at=now,created_by=me['id'],updated_by=me['id'])
            db.session.add(item); db.session.flush()
            db.session.add_all([FinanceFeeItemClass(fee_item_id=item.id,class_id=cid,active=1,
                                                    created_at=now,created_by=me['id'])
                                for cid in selected_class_ids])
            db.session.commit()
            audit_log('finance_fee_item_created','finance','fee_item',item.id,{
                'name':name,'amount':amount,'class_ids':selected_class_ids})
            flash('Fee item created and assigned to the selected classes.','success')
            return redirect(url_for('admin_finance_fee_items'))

    return render_template(
        'finance_fee_item_form.html',
        errors=errors,
        form=request.form if request.method=='POST' else {},
        editing=None,
        classes=classes,
        selected_class_ids=selected_class_ids,
        fee_categories=FINANCE_FEE_CATEGORIES,
        fee_applicability=FINANCE_FEE_APPLICABILITY)

@app.route('/admin/finance/fee-items/<int:item_id>/edit',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_finance_fee_item_edit(item_id):
    errors=[]
    row=obj(FinanceFeeItem,item_id)
    if not row:
        abort(404)
    classes=_active_classes()
    valid_class_ids={int(r['id']) for r in classes}
    selected_class_ids=sorted(cid for (cid,) in tuples(
        select(FinanceFeeItemClass.class_id)
        .where(FinanceFeeItemClass.fee_item_id==item_id,FinanceFeeItemClass.active==1)))

    if request.method=='POST':
        name=request.form.get('name','').strip()
        category=request.form.get('category','School Fees').strip() or 'School Fees'
        applicability=request.form.get('applicability','Full Session').strip() or 'Full Session'
        required=1 if request.form.get('required')=='1' else 0
        optional=1 if request.form.get('optional')=='1' else 0
        notes=request.form.get('notes','').strip()
        selected_class_ids=[]
        for raw in request.form.getlist('class_ids'):
            try:
                cid=int(raw)
                if cid in valid_class_ids and cid not in selected_class_ids:
                    selected_class_ids.append(cid)
            except (TypeError,ValueError):
                pass
        try:
            amount=float(request.form.get('amount','0'))
        except (TypeError,ValueError):
            amount=-1

        if not name:
            errors.append('Fee item name is required.')
        if category not in FINANCE_FEE_CATEGORIES:
            errors.append('Select a valid fee category.')
        if applicability not in FINANCE_FEE_APPLICABILITY:
            errors.append('Select a valid billing rule.')
        if amount < 0:
            errors.append('Fee amount cannot be negative.')
        if not selected_class_ids:
            errors.append('Select at least one active class this fee applies to.')

        if not errors:
            selected_rows=[r for r in classes if int(r['id']) in selected_class_ids]
            legacy_stage=_legacy_stage_for(selected_rows,selected_class_ids,valid_class_ids)
            now=datetime.now(timezone.utc).isoformat()
            me=current_admin()
            row.name=name; row.category=category; row.stage=legacy_stage; row.amount=amount
            row.applicability=applicability; row.required=required; row.optional=optional
            row.notes=notes; row.updated_at=now; row.updated_by=me['id']
            # Retire every mapping, then re-activate the ones still selected, so
            # a class removed from the fee stops being charged.
            db.session.execute(sa_update(FinanceFeeItemClass)
                .where(FinanceFeeItemClass.fee_item_id==item_id).values(active=0))
            for cid in selected_class_ids:
                stmt=sqlite_insert(FinanceFeeItemClass).values(
                    fee_item_id=item_id,class_id=cid,active=1,created_at=now,created_by=me['id'])
                db.session.execute(stmt.on_conflict_do_update(
                    index_elements=['fee_item_id','class_id'],
                    set_={'active':1,'created_at':stmt.excluded.created_at,
                          'created_by':stmt.excluded.created_by}))
            db.session.commit()
            audit_log('finance_fee_item_updated','finance','fee_item',item_id,{
                'name':name,'amount':amount,'class_ids':selected_class_ids})
            flash('Fee item updated. Existing assessments keep their historical amounts.','success')
            return redirect(url_for('admin_finance_fee_items'))

    current_form=({c.key:getattr(row,c.key) for c in row.__mapper__.column_attrs}
                  if request.method!='POST' else request.form)
    return render_template(
        'finance_fee_item_form.html',
        errors=errors,
        form=current_form,
        editing=row,
        classes=classes,
        selected_class_ids=selected_class_ids,
        fee_categories=FINANCE_FEE_CATEGORIES,
        fee_applicability=FINANCE_FEE_APPLICABILITY,
        # Older rows may carry a value from before the catalogue was fixed
        # (e.g. "Second and third term only"); keep it selectable so saving
        # the form again doesn't silently rewrite it to something else.
        legacy_category=current_form.get('category') if current_form.get('category') not in FINANCE_FEE_CATEGORIES else None,
        legacy_applicability=current_form.get('applicability') if current_form.get('applicability') not in FINANCE_FEE_APPLICABILITY else None)

@app.post('/admin/finance/fee-items/<int:item_id>/toggle')
@admin_required
@csrf_protect
def admin_finance_fee_item_toggle(item_id):
    row=obj(FinanceFeeItem,item_id)
    if not row: abort(404)
    new=0 if row.active else 1
    row.active=new; row.updated_at=datetime.now(timezone.utc).isoformat()
    row.updated_by=current_admin()['id']
    db.session.commit()
    audit_log('finance_fee_item_status_changed','finance','fee_item',item_id,{'active':new}); flash('Fee item '+('activated.' if new else 'archived.')+' Existing assessments remain unchanged.','success'); return redirect(url_for('admin_finance_fee_items'))

@app.get('/admin/finance/students/<int:student_id>/assessed-items.json')
@admin_required
def admin_finance_student_assessed_items(student_id):
    """Which fee items this student already has a live assessment for, by
    term, for the requested session — and whether that assessment has been
    fully paid.

    The Student Billing picker uses this to grey out (and label) a fee item
    the moment it can no longer be legally assessed for the chosen term,
    instead of letting staff pick it and only finding out from a flashed
    error after submitting.
    """
    try: session_id=int(request.args.get('session_id',''))
    except (TypeError,ValueError): return jsonify({})
    if not session_id: return jsonify({})
    account=_finance_student_outstanding(student_id,session_id)
    by_term={}
    for item in account:
        term=item['term'] or 'Full Session'
        by_term.setdefault(term,{})[str(item['fee_item_id'])] = {
            'paid': item['outstanding']<=0.000001,
            'category': item['category']}
    return jsonify(by_term)

@app.post('/admin/finance/assessments/new')
@admin_required
@csrf_protect
def admin_finance_assessment_new():
    'Create independent fee obligations after server-side class applicability validation.'
    me=current_admin()
    try:
        student_id=int((request.form.get('student_id') or '').strip())
        session_id=int((request.form.get('session_id') or '').strip())
    except (TypeError,ValueError):
        student_id=session_id=0

    ids=[]
    for raw in request.form.getlist('fee_item_id'):
        try:
            item_id=int(str(raw).strip())
            if item_id>0 and item_id not in ids:
                ids.append(item_id)
        except (TypeError,ValueError):
            pass

    term=request.form.get('term','Full Session').strip() or 'Full Session'
    due_date=request.form.get('due_date','').strip() or None
    notes=request.form.get('notes','').strip()

    if not student_id:
        flash('Select a student before creating an assessment.','error')
        return redirect(url_for('admin_finance_fee_items'))
    if not session_id:
        flash('Select an academic session before creating an assessment.','error')
        return redirect(url_for('admin_finance_fee_items'))
    if not ids:
        flash('Select at least one fee item before creating an assessment.','error')
        return redirect(url_for('admin_finance_fee_items'))

    student=one(select(Student.id,Student.admission_no,Student.first_name,
                       Student.middle_name,Student.last_name)
                .where(Student.id==student_id,Student.active==1))
    if not student:
        flash('The selected student could not be found.','error')
        return redirect(url_for('admin_finance_fee_items'))

    session_row=db.session.scalars(select(AcademicSession).where(
        AcademicSession.id==session_id,AcademicSession.active==1)).first()
    if not session_row:
        flash('The selected academic session could not be found.','error')
        return redirect(url_for('admin_finance_fee_items'))

    enrolment=one(select(StudentEnrolment.class_id,SchoolClass.name.label('class_name'),
                         SchoolClass.stage.label('class_stage'))
                  .join(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
                  .where(StudentEnrolment.student_id==student_id,
                         StudentEnrolment.session_id==session_id,
                         StudentEnrolment.active==1,SchoolClass.active==1).limit(1))
    if not enrolment:
        flash('The selected student has no active class enrolment for the selected academic session.','error')
        return redirect(url_for('admin_finance_fee_items'))

    items=db.session.scalars(select(FinanceFeeItem)
        .where(FinanceFeeItem.id.in_(ids),FinanceFeeItem.active==1)
        .order_by(FinanceFeeItem.name)).all()
    if len(items)!=len(ids):
        flash('One or more selected fee items are no longer available.','error')
        return redirect(url_for('admin_finance_fee_items'))

    # A fee may only be charged to a class it is actually mapped to.
    allowed_ids={int(fid) for (fid,) in tuples(
        select(FinanceFeeItem.id)
        .join(FinanceFeeItemClass,and_(FinanceFeeItemClass.fee_item_id==FinanceFeeItem.id,
                                       FinanceFeeItemClass.active==1))
        .where(FinanceFeeItem.id.in_(ids),
               FinanceFeeItemClass.class_id==int(enrolment['class_id']),
               FinanceFeeItem.active==1))}
    rejected=[item.name for item in items if int(item.id) not in allowed_ids]
    if rejected:
        flash('Assessment blocked: these fee item(s) are not assigned to '
              + str(enrolment['class_name']) + ': ' + ', '.join(rejected),'error')
        return redirect(url_for('admin_finance_fee_items'))

    by_id={int(x.id):x for x in items}
    ordered=[by_id[i] for i in ids]
    # One live assessment per fee item, per student, per term.
    existing={fid for (fid,) in tuples(
        select(FinanceFeeAssessment.fee_item_id)
        .where(FinanceFeeAssessment.student_id==student_id,
               FinanceFeeAssessment.session_id==session_id,
               FinanceFeeAssessment.fee_item_id.in_(ids),
               FinanceFeeAssessment.term==term,
               FinanceFeeAssessment.active==1))}
    duplicates=[item.name for item in ordered if int(item.id) in existing]
    if duplicates:
        flash('Assessment not created. The following fee item(s) already exist for this student and term: '
              + ', '.join(duplicates),'error')
        return redirect(url_for('admin_finance_fee_items'))

    now=datetime.now(timezone.utc).isoformat()
    rows=[FinanceFeeAssessment(student_id=student_id,session_id=session_id,
                               category=item.category,amount=float(item.amount or 0),
                               due_date=due_date,notes=notes,created_by=me['id'],
                               created_at=now,active=1,fee_item_id=item.id,term=term)
          for item in ordered]
    db.session.add_all(rows)
    db.session.commit()
    created=[r.id for r in rows]
    total=sum(float(item.amount or 0) for item in ordered)

    audit_log('finance_assessment_created','finance','assessment',created[0] if created else 0,{
        'student_id':student_id,
        'session_id':session_id,
        'class_id':int(enrolment['class_id']),
        'class_name':enrolment['class_name'],
        'term':term,
        'assessment_count':len(created),
        'assessment_ids':created,
        'total_assessed':total})

    student_name=' '.join(x for x in (student['first_name'],student['middle_name'],student['last_name']) if x)
    flash(f'{len(created)} assessment(s) created for {student_name} — ₦{total:,.2f} total.','success')
    try:
        _notify_parents_fee_assessed(student_id,[item.name for item in ordered],total,term,
                                     session_row.name,me['id'])
    except Exception:
        app.logger.exception('Parent fee-assessed notification failed for student %s',student_id)
    return redirect(url_for('admin_finance_fee_items'))

@app.post('/admin/finance/payments/<int:payment_id>/void')
@admin_required
@csrf_protect
def admin_finance_payment_void(payment_id):
    me=current_admin()
    if not _finance_can_view_all(me): return admin_access_error('finance.manage')
    reason=request.form.get('reason','').strip()
    if not reason: flash('A reason is required to void a payment.','error'); return redirect(url_for('admin_finance_receipt',payment_id=payment_id))
    row=obj(FinancePayment,payment_id)
    if not row: abort(404)
    if row.status!='posted': flash('Only a posted payment can be voided.','error'); return redirect(url_for('admin_finance_receipt',payment_id=payment_id))
    receipt_no,amount=row.receipt_no,row.amount
    row.status='voided'; row.voided_at=datetime.now(timezone.utc).isoformat()
    row.voided_by=me['id']; row.void_reason=reason
    db.session.commit()
    audit_log('finance_payment_voided','finance','payment',payment_id,{'receipt_no':receipt_no,'amount':amount,'reason':reason},True,me)
    flash('Payment voided. The original receipt remains in the audit history.','success'); return redirect(url_for('admin_finance_receipt',payment_id=payment_id))

@app.route('/admin/library')
@admin_required
def admin_library():
    q=request.args.get('q','').strip(); status=request.args.get('status','all')
    stmt=select(LibraryBook).where(LibraryBook.active==1)
    if q:
        like=f'%{q}%'
        stmt=stmt.where(or_(LibraryBook.title.like(like),LibraryBook.author.like(like),
                            LibraryBook.isbn.like(like),LibraryBook.category.like(like)))
    if status=='available':
        stmt=stmt.where(LibraryBook.available_copies>0)
    books=db.session.scalars(stmt.order_by(LibraryBook.title)).all()
    borrowed=one_scalar(select(func.count()).select_from(LibraryLoan)
                        .where(LibraryLoan.status=='borrowed'),0)
    overdue=one_scalar(select(func.count()).select_from(LibraryLoan)
                       .where(LibraryLoan.status=='borrowed',
                              LibraryLoan.due_at.is_not(None),
                              LibraryLoan.due_at<func.date('now')),0)
    students=all_rows(select(Student.id,Student.admission_no,Student.first_name,
                             Student.middle_name,Student.last_name)
                      .where(Student.active==1)
                      .order_by(Student.last_name,Student.first_name))
    staff=all_rows(select(Admin.id,Admin.display_name,Admin.username)
                   .where(Admin.active==1).order_by(Admin.display_name))
    loans=[_flatten(r,'LibraryLoan','title','first_name','last_name','admission_no','staff_name')
           for r in all_rows(
        select(LibraryLoan,LibraryBook.title,Student.first_name,Student.last_name,
               Student.admission_no,Admin.display_name.label('staff_name'))
        .join(LibraryBook,LibraryBook.id==LibraryLoan.book_id)
        .outerjoin(Student,and_(LibraryLoan.member_type=='student',
                                Student.id==LibraryLoan.member_id))
        .outerjoin(Admin,and_(LibraryLoan.member_type=='staff',
                              Admin.id==LibraryLoan.member_id))
        .where(LibraryLoan.status=='borrowed')
        .order_by(LibraryLoan.due_at,LibraryLoan.id.desc()).limit(30))]
    return render_template('library_dashboard.html',books=books,borrowed=borrowed,overdue=overdue,q=q,status=status,students=students,staff=staff,loans=loans)

@app.post('/admin/library/books/new')
@admin_required
@csrf_protect
def admin_library_book_new():
    title=request.form.get('title','').strip(); author=request.form.get('author','').strip(); isbn=request.form.get('isbn','').strip(); publisher=request.form.get('publisher','').strip(); category=request.form.get('category','').strip(); shelf=request.form.get('shelf','').strip()
    try: year=int(request.form.get('publication_year','') or 0); copies=max(1,int(request.form.get('copies','1') or 1))
    except (TypeError,ValueError): year=0; copies=1
    if not title: flash('Book title is required.','error'); return redirect(url_for('admin_library'))
    db.session.add(LibraryBook(isbn=isbn,title=title,author=author,publisher=publisher,
        publication_year=year or None,category=category,shelf=shelf,
        total_copies=copies,available_copies=copies,active=1,
        created_at=datetime.now(timezone.utc).isoformat(),created_by=current_admin()['id']))
    db.session.commit()
    audit_log('library_book_created','library','book',title,{'copies':copies}); flash('Book added to the library.','success'); return redirect(url_for('admin_library'))

@app.route('/admin/library/books/<int:book_id>/edit',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_library_book_edit(book_id):
    row=obj(LibraryBook,book_id)
    if not row: abort(404)
    errors=[]
    if request.method=='POST':
        title=request.form.get('title','').strip(); author=request.form.get('author','').strip(); isbn=request.form.get('isbn','').strip(); publisher=request.form.get('publisher','').strip(); category=request.form.get('category','').strip(); shelf=request.form.get('shelf','').strip()
        try: year=int(request.form.get('publication_year','') or 0); total=int(request.form.get('total_copies','1') or 1)
        except (TypeError,ValueError): year=0; total=0
        borrowed=int(row.total_copies)-int(row.available_copies)
        if not title: errors.append('Book title is required.')
        if total<borrowed: errors.append(f'Total copies cannot be below {borrowed}, because that many copies are currently on loan.')
        if total<0: errors.append('Total copies cannot be negative.')
        if not errors:
            available=total-borrowed
            row.title=title; row.author=author; row.isbn=isbn; row.publisher=publisher
            row.publication_year=year or None; row.category=category; row.shelf=shelf
            row.total_copies=total; row.available_copies=available
            db.session.commit()
            audit_log('library_book_updated','library','book',book_id,{'total_copies':total,'available_copies':available})
            flash('Library book details updated.','success'); return redirect(url_for('admin_library'))
    form={c.key:getattr(row,c.key) for c in row.__mapper__.column_attrs}
    if request.method=='POST': form.update(request.form)
    return render_template('library_book_form.html',book=form,errors=errors)

@app.post('/admin/library/books/<int:book_id>/toggle')
@admin_required
@csrf_protect
def admin_library_book_toggle(book_id):
    row=obj(LibraryBook,book_id)
    if not row: abort(404)
    new=0 if row.active else 1
    if not new and int(row.available_copies) != int(row.total_copies):
        flash('Return all copies before deactivating this book.','error'); return redirect(url_for('admin_library'))
    row.active=new; db.session.commit()
    audit_log('library_book_status_changed','library','book',book_id,{'active':new}); flash('Book '+('activated.' if new else 'archived.'),'success'); return redirect(url_for('admin_library'))

@app.post('/admin/library/issue')
@admin_required
@csrf_protect
def admin_library_issue():
    try: book_id=int(request.form.get('book_id','')); member_id=int(request.form.get('member_id','')); days=max(1,int(request.form.get('days','14') or 14))
    except (TypeError,ValueError): book_id=member_id=0; days=14
    member_type=request.form.get('member_type','student').strip()
    book=db.session.scalars(select(LibraryBook).where(
        LibraryBook.id==book_id,LibraryBook.active==1)).first()
    if not book or book.available_copies<1: flash('That book is not currently available.','error'); return redirect(url_for('admin_library'))
    if member_type=='student':
        member=one_scalar(select(Student.id).where(Student.id==member_id,Student.active==1))
    else:
        member=one_scalar(select(Admin.id).where(Admin.id==member_id,Admin.active==1))
    if not member: flash('Library member not found.','error'); return redirect(url_for('admin_library'))
    now=datetime.now(timezone.utc); due=(now+timedelta(days=days)).date().isoformat()
    loan=LibraryLoan(book_id=book_id,member_type=member_type,member_id=member_id,
                     borrowed_at=now.isoformat(),due_at=due,status='borrowed',
                     issued_by=current_admin()['id'])
    db.session.add(loan)
    book.available_copies=book.available_copies-1
    db.session.commit()
    audit_log('library_book_issued','library','loan',loan.id,{'book_id':book_id,'member_type':member_type,'member_id':member_id}); flash('Book issued successfully.','success'); return redirect(url_for('admin_library'))

@app.post('/admin/library/loans/<int:loan_id>/return')
@admin_required
@csrf_protect
def admin_library_return(loan_id):
    loan=db.session.scalars(select(LibraryLoan).where(
        LibraryLoan.id==loan_id,LibraryLoan.status=='borrowed')).first()
    if not loan: abort(404)
    now=datetime.now(timezone.utc).isoformat()
    loan.status='returned'; loan.returned_at=now; loan.received_by=current_admin()['id']
    db.session.execute(sa_update(LibraryBook).where(LibraryBook.id==loan.book_id)
        .values(available_copies=func.min(LibraryBook.total_copies,
                                          LibraryBook.available_copies+1)))
    db.session.commit()
    audit_log('library_book_returned','library','loan',loan_id,{'book_id':loan.book_id}); flash('Book returned successfully.','success'); return redirect(url_for('admin_library'))

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
