"""Data protection (Nigeria Data Protection Act 2023): consents, families' requests, and erasure.

The school is the controller of its families' data and the platform its processor (templates/privacy.html), so a family
asks the school, and the school decides. Three things are kept here:

* **Consents.** Each decision is a new ``ConsentRecord`` row and never changes, so the latest row for a child (or, for
  the privacy notice, a parent) and a purpose is the consent in force and the rows before it are its history. A parent
  gives or withdraws one in the portal; staff record one from a signed paper form, which is how a family that never
  signs in gives it. Each purpose has a default for a child nobody has decided for (``PURPOSES``): publicity photos and
  sharing with an exam body are asked for first (opt-in); optional messages are sent until withdrawn (opt-out), since
  they are part of the school's ordinary contact with a family and the parent can stop them at any time.
* **Requests.** A parent asks for a copy of a child's data, or for it to be erased. The school has thirty days
  (``DUE_DAYS``). A copy is built in the background (``family_export``) and downloaded from the portal; an erasure is
  carried out at once when the School Admin approves it.
* **Erasure** (``erase_student``) anonymises rather than deletes, because a school must keep results and fee records:
  the child's name becomes a placeholder, and everything else that identifies them - contact details, health, religion,
  the photograph, sign-in details, messages, alerts, the admission form, the entrance candidate record - is removed.
  Results, attendance, fees and payments stay, under the placeholder name. A parent with no other child at the school is
  anonymised too. It is never undone.
"""

import csv
import io
import json
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, func, select, update
from werkzeug.security import generate_password_hash

from core.db_helpers import tuples
from models import (
    AcademicSession, AttendanceRecord, Candidate, ConsentRecord, DataRequest, FinanceFeeAssessment,
    FinancePayment, IssuedDocument, NotificationDeliveryLog, ParentAccount, ParentFeedback, ParentFeedbackReply,
    ParentStudentLink, SchoolClass, SchoolNotification, SchoolStudentResult, SchoolSubject, StorePurchase, Student,
    StudentAdmissionContact, StudentAdmissionProfile, StudentEnrolment, db,
)

# The privacy notice's version is the date on templates/privacy.html ("Last updated"); a parent who confirmed an older
# one is asked again. tests/current checks the two agree.
NOTICE_VERSION = '2026-10-15'

PURPOSES = {
    'privacy_notice': {'title': 'Privacy notice', 'per': 'parent', 'default': False,
                       'text': 'I have read how the school and the platform use my family\'s information.'},
    'photos_media': {'title': 'Photos in publicity', 'per': 'student', 'default': False,
                     'text': "The school may use my child's photograph in its publicity: the website, social media, "
                             "brochures and banners. (ID cards and report cards use it regardless.)"},
    'optional_messages': {'title': 'Optional messages', 'per': 'student', 'default': True,
                          'text': 'Birthday wishes and other optional texts and emails. Messages about fees, results, '
                                  'attendance and safety are always sent.'},
    'exam_sharing': {'title': 'Exam registration', 'per': 'student', 'default': False,
                     'text': "The school may share my child's details and photograph with WAEC, NECO or the BECE "
                             "examining body to register them for an external examination."},
}
CHILD_PURPOSES = tuple(k for k, v in PURPOSES.items() if v['per'] == 'student')
KINDS = {'export': 'A copy of the data', 'erasure': 'Erase the data'}
DUE_DAYS = 30
OPEN = ('pending', 'approved')
ERASED_REASON = "Personal data erased at the family's request."


def _now():
    return datetime.now(timezone.utc)


def _iso(dt=None):
    return (dt or _now()).isoformat()


# ---------------------------------------------------------------- consents
def _latest(column, ids, purposes):
    """``{(id, purpose): ConsentRecord}``: the decision in force, for many children or parents in one query."""
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    newest = (select(func.max(ConsentRecord.id)).where(column.in_(ids), ConsentRecord.purpose.in_(purposes))
              .group_by(column, ConsentRecord.purpose))
    rows = db.session.scalars(select(ConsentRecord).where(ConsentRecord.id.in_(newest))).all()
    return {(getattr(r, column.key), r.purpose): r for r in rows}


def consents_for(student_ids):
    """``{student_id: {purpose: {'granted', 'decided', 'at', 'source'}}}`` for every child purpose: what is in force,
    and whether anyone has decided (``decided`` False means the default applies)."""
    found = _latest(ConsentRecord.student_id, student_ids, CHILD_PURPOSES)
    out = {}
    for sid in student_ids:
        out[sid] = {}
        for purpose in CHILD_PURPOSES:
            row = found.get((sid, purpose))
            out[sid][purpose] = ({'granted': bool(row.granted), 'decided': True, 'at': row.created_at, 'source': row.source}
                                 if row else {'granted': PURPOSES[purpose]['default'], 'decided': False, 'at': None,
                                              'source': None})
    return out


def allowed(student_ids, purpose):
    """The set of these children for whom ``purpose`` is allowed now (decided, or the default)."""
    state = consents_for(list(student_ids))
    return {sid for sid, purposes in state.items() if purposes[purpose]['granted']}


def notice_confirmed(parent_id):
    """Whether this parent has confirmed the current privacy notice."""
    row = _latest(ConsentRecord.parent_id, [parent_id], ('privacy_notice',)).get((parent_id, 'privacy_notice'))
    return bool(row and row.granted and row.notice_version == NOTICE_VERSION)


def record(purpose, granted, *, student_id=None, parent_id=None, source='parent', admin_id=None, note=None, ip=None):
    """Add a decision (not committed). A decision equal to the one in force is not written again."""
    if purpose not in PURPOSES:
        raise ValueError('Unknown purpose.')
    if PURPOSES[purpose]['per'] == 'student' and not student_id:
        raise ValueError('A child is needed.')
    column, key = (ConsentRecord.student_id, student_id) if student_id else (ConsentRecord.parent_id, parent_id)
    current = _latest(column, [key], (purpose,)).get((key, purpose))
    if current and bool(current.granted) == bool(granted) and (purpose != 'privacy_notice'
                                                               or current.notice_version == NOTICE_VERSION):
        return None
    row = ConsentRecord(purpose=purpose, student_id=student_id, parent_id=parent_id, granted=1 if granted else 0,
                        notice_version=NOTICE_VERSION, source=source, recorded_by=admin_id,
                        note=(' '.join(str(note or '').split())[:300] or None), ip_address=(ip or '')[:64] or None,
                        created_at=_iso())
    db.session.add(row)
    return row


def history(student_id):
    """Every decision about one child, newest first, with who recorded it."""
    from models import Admin
    return [dict(purpose=p, title=PURPOSES.get(p, {}).get('title', p), granted=bool(g), source=s, at=at, note=n,
                 by=(by or parent or '')) for p, g, s, at, n, by, parent in tuples(
        select(ConsentRecord.purpose, ConsentRecord.granted, ConsentRecord.source, ConsentRecord.created_at,
               ConsentRecord.note, Admin.display_name, ParentAccount.display_name)
        .outerjoin(Admin, Admin.id == ConsentRecord.recorded_by)
        .outerjoin(ParentAccount, ParentAccount.id == ConsentRecord.parent_id)
        .where(ConsentRecord.student_id == student_id).order_by(ConsentRecord.id.desc()).limit(100))]


# ---------------------------------------------------------------- requests
def open_requests(parent_id=None):
    stmt = select(DataRequest).where(DataRequest.status.in_(OPEN)).order_by(DataRequest.due_at, DataRequest.id)
    if parent_id:
        stmt = stmt.where(DataRequest.parent_id == parent_id)
    return db.session.scalars(stmt).all()


def ask(parent_id, student_id, kind, reason=''):
    """A parent's request (not committed). Raises ValueError saying what is wrong."""
    if kind not in KINDS:
        raise ValueError('Choose what you are asking for.')
    linked = db.session.scalar(select(ParentStudentLink.id).where(
        ParentStudentLink.parent_id == parent_id, ParentStudentLink.student_id == student_id, ParentStudentLink.active == 1))
    if not linked:
        raise ValueError('Choose one of your children.')
    if db.session.scalar(select(Student.erased_at).where(Student.id == student_id)):
        raise ValueError("This child's data has already been erased.")
    if db.session.scalar(select(DataRequest.id).where(
            DataRequest.student_id == student_id, DataRequest.kind == kind, DataRequest.status.in_(OPEN))):
        raise ValueError('There is already a request like this waiting for the school.')
    now = _now()
    row = DataRequest(kind=kind, student_id=student_id, parent_id=parent_id, status='pending',
                      reason=' '.join(str(reason or '').split())[:500] or None, requested_at=_iso(now),
                      due_at=_iso(now + timedelta(days=DUE_DAYS)))
    db.session.add(row)
    return row


# ---------------------------------------------------------------- erasure
PLACEHOLDER_FIRST = 'Former'


def placeholder_last(student_id):
    return f'student #{student_id}'


def erase_student(student_id, admin_id, requesting_parent_id=None):
    """Anonymise one child (not committed: the caller commits, then calls ``remove_files`` with what this returns).

    Returns ``{'removed': {what: count}, 'files': [stored upload paths to delete after the commit], 'parents': n}``."""
    student = db.session.get(Student, student_id)
    if student is None:
        raise ValueError('No such student.')
    if student.erased_at:
        raise ValueError("This child's data has already been erased.")
    now = _iso()
    files, removed = [], {}
    if student.photo_path:
        files.append(student.photo_path)
    keep = {'id', 'admission_no', 'student_number', 'student_number_source', 'created_at', 'school_id', 'department',
            'legacy_student_id', 'archived_at', 'archived_by_admin_id', 'archive_reason', 'active', 'account_active',
            'password_must_change', 'erased_at', 'first_name', 'last_name'}
    for column in Student.__table__.columns:
        if column.key not in keep and not column.primary_key and column.nullable:
            setattr(student, column.key, None)
    student.first_name, student.last_name = PLACEHOLDER_FIRST, placeholder_last(student_id)
    student.active, student.account_active, student.password_must_change = 0, 0, 1
    student.login_username, student.login_password_hash = None, None
    student.archived_at = student.archived_at or now
    student.archived_by_admin_id = student.archived_by_admin_id or admin_id
    student.archive_reason = ERASED_REASON
    student.erased_at = now

    def count(result):
        return result.rowcount if result.rowcount and result.rowcount > 0 else 0

    removed['admission form'] = count(db.session.execute(delete(StudentAdmissionProfile).where(
        StudentAdmissionProfile.student_id == student_id)))
    removed['admission contacts'] = count(db.session.execute(delete(StudentAdmissionContact).where(
        StudentAdmissionContact.student_id == student_id)))
    for cand in db.session.scalars(select(Candidate).where(Candidate.admitted_student_id == student_id)).all():
        if cand.photo_path:
            files.append(cand.photo_path)
        cand.candidate_name = f'{PLACEHOLDER_FIRST} {placeholder_last(student_id)}'
        for name in ('school_attended', 'parent_guardian_name', 'parent_guardian_relationship', 'primary_mobile',
                     'alternative_mobile', 'parent_guardian_email', 'photo_path', 'admission_note', 'released_password'):
            setattr(cand, name, None)
        cand.password_hash = generate_password_hash(secrets.token_urlsafe(32))
        cand.active = 0
        removed['entrance record'] = removed.get('entrance record', 0) + 1
    threads = [t for (t,) in tuples(select(ParentFeedback.id).where(ParentFeedback.student_id == student_id))]
    if threads:
        files += [p for (p,) in tuples(select(ParentFeedback.attachment_path).where(
            ParentFeedback.id.in_(threads), ParentFeedback.attachment_path.is_not(None)))]
        files += [p for (p,) in tuples(select(ParentFeedbackReply.attachment_path).where(
            ParentFeedbackReply.feedback_id.in_(threads), ParentFeedbackReply.attachment_path.is_not(None)))]
        db.session.execute(delete(ParentFeedbackReply).where(ParentFeedbackReply.feedback_id.in_(threads)))
        removed['messages'] = count(db.session.execute(delete(ParentFeedback).where(ParentFeedback.id.in_(threads))))
    removed['alerts'] = count(db.session.execute(delete(SchoolNotification).where(SchoolNotification.student_id == student_id)))
    db.session.execute(update(NotificationDeliveryLog).where(NotificationDeliveryLog.student_id == student_id)
                       .values(recipient=None))
    db.session.execute(update(FinancePayment).where(FinancePayment.student_id == student_id).values(payer_name=None))
    for doc in db.session.scalars(select(IssuedDocument).where(IssuedDocument.student_id == student_id)).all():
        doc.details_json = json.dumps({'student_name': f'{PLACEHOLDER_FIRST} {placeholder_last(student_id)}',
                                       'erased': True})
        if not doc.revoked_at:
            doc.revoked_at, doc.revoked_by, doc.revoke_reason = now, admin_id, ERASED_REASON
        removed['documents'] = removed.get('documents', 0) + 1
    # The family: each linked parent is unlinked; one with no other child at the school is anonymised.
    parents = [p for (p,) in tuples(select(ParentStudentLink.parent_id).where(ParentStudentLink.student_id == student_id))]
    db.session.execute(update(ParentStudentLink).where(ParentStudentLink.student_id == student_id).values(active=0))
    anonymised = 0
    for pid in set(parents) | ({requesting_parent_id} if requesting_parent_id else set()):
        others = db.session.scalar(select(func.count()).select_from(ParentStudentLink).join(
            Student, Student.id == ParentStudentLink.student_id).where(
            ParentStudentLink.parent_id == pid, ParentStudentLink.active == 1, Student.erased_at.is_(None)))
        if others:
            continue
        parent = db.session.get(ParentAccount, pid)
        if parent is None or str(parent.username or '').startswith('erased-'):
            continue
        parent.username = f'erased-{pid}-{secrets.token_hex(4)}'
        parent.display_name = 'Former parent'
        for name in ('email', 'phone', 'address', 'office_phone', 'mobile', 'occupation', 'relationship'):
            if hasattr(parent, name):
                setattr(parent, name, None)
        parent.password_hash = generate_password_hash(secrets.token_urlsafe(32))
        parent.active = 0
        db.session.execute(update(StorePurchase).where(StorePurchase.parent_id == pid).values(claimed_by_name=None))
        anonymised += 1
    return {'removed': {k: v for k, v in removed.items() if v}, 'files': [f for f in files if f], 'parents': anonymised}


def remove_files(paths):
    """Delete uploads named by an erasure, after it has been committed. A missing file is not an error."""
    from core.storage import delete_upload
    for path in paths:
        try:
            delete_upload(path)
        except Exception:
            pass


# ---------------------------------------------------------------- a family's copy of a child's data
def _csv(rows, header):
    from blueprints.entrance.helpers import csv_safe
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(header)
    for r in rows:
        w.writerow([csv_safe(v) for v in r])
    return out.getvalue().encode('utf-8-sig')


def family_files(student_id, parent_id):
    """``[(name in the ZIP, bytes)]``: everything the school holds about one child, and about the parent who asked.
    Another parent's own details are theirs, not the asker's, and are left out. No password, ever."""
    from core.storage import read_upload_bytes
    s = db.session.get(Student, student_id)
    profile = {c.key: getattr(s, c.key) for c in Student.__table__.columns
               if c.key not in ('login_password_hash', 'school_id', 'legacy_student_id', 'student_number_source')}
    prof = db.session.scalars(select(StudentAdmissionProfile).where(StudentAdmissionProfile.student_id == student_id)).first()
    if prof:
        profile['admission_form'] = {c.key: getattr(prof, c.key) for c in StudentAdmissionProfile.__table__.columns
                                     if c.key not in ('id', 'student_id', 'updated_by')}
    files = [('child.json', json.dumps(profile, indent=2, default=str).encode())]
    parent = db.session.get(ParentAccount, parent_id) if parent_id else None
    if parent:
        files.append(('you.json', json.dumps({c.key: getattr(parent, c.key) for c in ParentAccount.__table__.columns
                                              if c.key not in ('password_hash', 'school_id') and not c.key.startswith(
                                                  ('attachment', 'file_type'))}, indent=2, default=str).encode()))
    files.append(('classes.csv', _csv(tuples(
        select(AcademicSession.name, SchoolClass.name, StudentEnrolment.enrolled_at)
        .join(AcademicSession, AcademicSession.id == StudentEnrolment.session_id)
        .join(SchoolClass, SchoolClass.id == StudentEnrolment.class_id)
        .where(StudentEnrolment.student_id == student_id).order_by(StudentEnrolment.id)),
        ['Session', 'Class', 'Enrolled'])))
    files.append(('attendance.csv', _csv(tuples(
        select(AttendanceRecord.date, AttendanceRecord.term, AttendanceRecord.status)
        .where(AttendanceRecord.student_id == student_id).order_by(AttendanceRecord.date)), ['Date', 'Term', 'Status'])))
    files.append(('results.csv', _csv(tuples(
        select(AcademicSession.name, SchoolStudentResult.term, SchoolSubject.name, SchoolStudentResult.component_name,
               SchoolStudentResult.score, SchoolStudentResult.max_score)
        .join(SchoolSubject, SchoolSubject.id == SchoolStudentResult.subject_id)
        .outerjoin(AcademicSession, AcademicSession.id == SchoolStudentResult.session_id)
        .where(SchoolStudentResult.student_id == student_id, SchoolStudentResult.status == 'released')
        .order_by(SchoolStudentResult.session_id, SchoolStudentResult.term, SchoolSubject.name)),
        ['Session', 'Term', 'Subject', 'Part', 'Score', 'Out of'])))
    files.append(('fees.csv', _csv(tuples(
        select(AcademicSession.name, FinanceFeeAssessment.term, FinanceFeeAssessment.category,
               FinanceFeeAssessment.gross_amount, FinanceFeeAssessment.discount_amount, FinanceFeeAssessment.amount,
               FinanceFeeAssessment.due_date)
        .outerjoin(AcademicSession, AcademicSession.id == FinanceFeeAssessment.session_id)
        .where(FinanceFeeAssessment.student_id == student_id, FinanceFeeAssessment.voided_at.is_(None))
        .order_by(FinanceFeeAssessment.id)),
        ['Session', 'Term', 'Fee', 'Full fee', 'Taken off', 'Charged', 'Due'])))
    files.append(('payments.csv', _csv(tuples(
        select(FinancePayment.receipt_no, FinancePayment.paid_at, FinancePayment.amount, FinancePayment.category,
               FinancePayment.method, FinancePayment.payer_name, FinancePayment.status)
        .where(FinancePayment.student_id == student_id).order_by(FinancePayment.id)),
        ['Receipt', 'Paid', 'Amount', 'For', 'How', 'Paid by', 'Status'])))
    files.append(('documents.csv', _csv(tuples(
        select(IssuedDocument.serial_no, IssuedDocument.kind, IssuedDocument.issued_at, IssuedDocument.revoked_at)
        .where(IssuedDocument.student_id == student_id).order_by(IssuedDocument.id)),
        ['Serial number', 'Document', 'Issued', 'Revoked'])))
    files.append(('consents.csv', _csv([(h['title'], 'given' if h['granted'] else 'withdrawn', h['source'], h['at'],
                                         h['note'] or '') for h in history(student_id)],
                                       ['Purpose', 'Decision', 'Recorded by', 'When', 'Note'])))
    if parent_id:
        thread_ids = [t for (t,) in tuples(select(ParentFeedback.id).where(
            ParentFeedback.parent_id == parent_id, ParentFeedback.student_id == student_id))]
        rows = []
        for tid, subject, body, at in tuples(select(ParentFeedback.id, ParentFeedback.subject, ParentFeedback.body,
                                                    ParentFeedback.created_at).where(ParentFeedback.id.in_(thread_ids or [0]))):
            rows.append((tid, at, 'You', subject, body))
        for tid, body, at in tuples(select(ParentFeedbackReply.feedback_id, ParentFeedbackReply.body,
                                           ParentFeedbackReply.created_at)
                                    .where(ParentFeedbackReply.feedback_id.in_(thread_ids or [0]))):
            rows.append((tid, at, 'The school', '', body))
        files.append(('messages.csv', _csv(sorted(rows, key=lambda r: (r[0], r[1] or '')),
                                           ['Conversation', 'When', 'From', 'Subject', 'Message'])))
    if s.photo_path:
        data = read_upload_bytes(s.photo_path)
        if data:
            files.append(('photograph' + (s.photo_path[s.photo_path.rfind('.'):] if '.' in s.photo_path else ''), data))
    files.append(('README.txt', (
        f"Everything {s.first_name} {s.last_name}'s school holds about them in its portal, and your own account details.\n"
        "child.json: the record and admission form. classes, attendance, results (released only), fees, payments,\n"
        "documents (testimonials and the like), consents and messages: one spreadsheet each. Your copy, prepared on\n"
        f"{_now().strftime('%d %B %Y')}. Keep it safe: it is personal data.\n").encode()))
    return files
