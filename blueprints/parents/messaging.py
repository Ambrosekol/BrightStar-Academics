"""The staff side of conversations with parents, shown on the Messages page.

A conversation is a ``ParentFeedback`` thread and its ``ParentFeedbackReply`` rows. A parent starts
one from the parent portal (the first message is the thread's own ``body``); a member of staff can
start one too, to reach a student's parent first. A school-started thread has an empty ``body`` and
its first message is simply the first reply, written by that administrator, so there is no separate
"who started it" column to keep in step.

Any administrator who may manage parent messages can answer any conversation they can see. The
thread remembers who is *currently* responding (``assigned_admin_id``): whoever replied last, or
whoever chose "I'll respond", and every reply carries its writer's name. The status stays what it
has always been: open, in progress or resolved.
"""

from datetime import datetime, timezone

import sqlalchemy as sa
from flask import abort, flash, jsonify, redirect, request, url_for
from sqlalchemy import and_, or_, select

from app import app
from blueprints.parents.helpers import _feedback_replies
from blueprints.school.helpers import _school_class_allowed
from core.branding import school_name
from core.db_helpers import _flatten, all_rows, obj, one
from core.notifications import _notify_guardian_email, _notify_guardian_sms
from core.security import admin_access_error, admin_has_permission, admin_required, audit_log, csrf_protect, current_admin
from models import (
    Admin, ParentAccount, ParentFeedback, ParentFeedbackReply, ParentStudentLink, SchoolClass, SchoolNotification,
    Student, StudentEnrolment, db,
)

STATUSES = ('open', 'in_progress', 'resolved')
STATUS_LABELS = {'open': 'Open', 'in_progress': 'In progress', 'resolved': 'Resolved'}
MAX_BODY = 5000
MAX_SUBJECT = 150
THREAD_LIMIT = 300


def messages_url(thread_id=None, **extra):
    """The Messages page, on its Parents tab, optionally with one conversation open."""
    if thread_id:
        extra['thread'] = thread_id
    return url_for('admin_messages', tab='parents', **extra)


def can_view(me):
    return bool(me) and admin_has_permission(me['id'], 'parent.feedback.view')


def can_manage(me):
    return bool(me) and admin_has_permission(me['id'], 'parent.feedback.manage')


def _class_id_of(student_id):
    if not student_id:
        return None
    return db.session.scalar(select(StudentEnrolment.class_id).where(
        StudentEnrolment.student_id == student_id, StudentEnrolment.active == 1).order_by(StudentEnrolment.id.desc()).limit(1))


def in_scope(me, student_id):
    """Whether this administrator may deal with a conversation about this student: school-wide
    administrators always, class-bound staff only for the classes they cover. A general conversation
    (no student) is open to everyone who may see parent messages."""
    if me['admin_type_system'] or not student_id:
        return True
    class_id = _class_id_of(student_id)
    return class_id is None or _school_class_allowed(me['id'], class_id)


def _full_name(first, last):
    return ' '.join(p for p in (first, last) if p and str(p).strip())


def _thread_rows(thread_id=None):
    Assigned = sa.orm.aliased(Admin)
    stmt = (select(ParentFeedback,
                   ParentAccount.display_name.label('parent_name'), ParentAccount.username.label('parent_username'),
                   ParentAccount.email.label('parent_email'), ParentAccount.phone.label('parent_phone'),
                   Student.first_name, Student.last_name,
                   SchoolClass.id.label('class_id'), SchoolClass.name.label('class_name'),
                   Assigned.display_name.label('assigned_name'))
            .join(ParentAccount, ParentAccount.id == ParentFeedback.parent_id)
            .outerjoin(Student, Student.id == ParentFeedback.student_id)
            .outerjoin(StudentEnrolment, and_(StudentEnrolment.student_id == Student.id, StudentEnrolment.active == 1))
            .outerjoin(SchoolClass, SchoolClass.id == StudentEnrolment.class_id)
            .outerjoin(Assigned, Assigned.id == ParentFeedback.assigned_admin_id))
    if thread_id:
        return all_rows(stmt.where(ParentFeedback.id == thread_id))
    return all_rows(stmt.order_by(ParentFeedback.updated_at.desc()).limit(THREAD_LIMIT * 2))


def _shape(raw, replies):
    row = _flatten(raw, 'ParentFeedback', 'parent_name', 'parent_username', 'parent_email', 'parent_phone',
                   'first_name', 'last_name', 'class_id', 'class_name', 'assigned_name')
    row['student_name'] = _full_name(row['first_name'], row['last_name'])
    messages = []
    if (row['body'] or '').strip():
        messages.append({'from_parent': True, 'author': row['parent_name'], 'body': row['body'], 'at': row['created_at'],
                         'attachment': bool(row['attachment_path']), 'attachment_name': row['attachment_name']})
    for r in replies:
        messages.append({'from_parent': r['is_parent'], 'author': row['parent_name'] if r['is_parent'] else (r['admin_name'] or 'School staff'),
                         'admin_id': r['admin_id'], 'body': r['body'], 'at': r['created_at'], 'attachment': False,
                         'attachment_name': None})
    last = messages[-1] if messages else None
    row['messages'] = messages
    row['last_at'] = last['at'] if last else row['created_at']
    row['preview'] = (last['body'] if last else '').strip().replace('\n', ' ')
    row['last_from_parent'] = bool(last and last['from_parent'])
    # What needs an answer: anything not resolved whose latest word came from the parent.
    row['awaiting'] = row['status'] != 'resolved' and row['last_from_parent']
    row['staff_started'] = not (row['body'] or '').strip()
    seen = []
    for m in messages:
        if not m['from_parent'] and m['author'] not in seen:
            seen.append(m['author'])
    row['staff_participants'] = seen
    row['status_label'] = STATUS_LABELS.get(row['status'], row['status'])
    return row


def staff_threads(me):
    """Every conversation this administrator may see, most in need of an answer first, plus counts.

    Returns ``(threads, counts)`` where counts has ``all``, each status, and ``awaiting``."""
    unique = {}
    for r in _thread_rows():       # a student with two active enrolments would otherwise list twice
        unique.setdefault(r['ParentFeedback'].id, r)
    raws = [r for r in unique.values()
            if me['admin_type_system'] or not r['class_id'] or _school_class_allowed(me['id'], r['class_id'])]
    raws = raws[:THREAD_LIMIT]
    replies = _feedback_replies([r['ParentFeedback'].id for r in raws])
    threads = [_shape(r, replies.get(r['ParentFeedback'].id, [])) for r in raws]
    threads.sort(key=lambda t: t['last_at'], reverse=True)
    threads.sort(key=lambda t: (t['status'] == 'resolved', not t['awaiting']))
    counts = {'all': len(threads), 'awaiting': sum(1 for t in threads if t['awaiting'])}
    for status in STATUSES:
        counts[status] = sum(1 for t in threads if t['status'] == status)
    return threads, counts


def filter_threads(threads, status='', q=''):
    q = (q or '').strip().lower()
    out = []
    for t in threads:
        if status in STATUSES and t['status'] != status:
            continue
        if q and q not in ' '.join((t['parent_name'] or '', t['student_name'], t['subject'] or '', t['class_name'] or '')).lower():
            continue
        out.append(t)
    return out


def staff_thread(me, thread_id):
    """One conversation in full, or None when it does not exist or is outside this administrator's classes."""
    raws = _thread_rows(thread_id)
    if not raws:
        return None
    raw = raws[0]
    if not me['admin_type_system'] and raw['class_id'] and not _school_class_allowed(me['id'], raw['class_id']):
        return None
    return _shape(raw, _feedback_replies([thread_id]).get(thread_id, []))


# ---------------------------------------------------------------------------------------------
# Telling the parent
def notify_parent(thread, body, admin_id, *, started=False):
    """Queue the in-portal notice for a message from the school. The caller commits; then calls
    :func:`deliver_parent_alerts` for the email and SMS copies."""
    db.session.add(SchoolNotification(
        recipient_type='parent', recipient_id=thread.parent_id, student_id=thread.student_id, category='feedback',
        title='The school sent you a message' if started else 'School replied to your feedback', message=body,
        action_url=url_for('parent_feedback'), created_at=datetime.now(timezone.utc).isoformat(), created_by=admin_id))


def deliver_parent_alerts(thread, body, *, started=False):
    """Best-effort email copy, and an SMS when the school has started the conversation. A blank contact or a channel that is down must never
    break the in-portal message that has just been saved."""
    contact = one(select(ParentAccount.email, ParentAccount.phone, ParentAccount.display_name)
                  .where(ParentAccount.id == thread.parent_id))
    if not contact:
        return
    about = f' ({thread.subject})' if thread.subject else ''
    subject_line = (f'Message from the school: {thread.subject}' if started else f'School replied: {thread.subject}') \
        if thread.subject else ('A message from the school' if started else 'School replied to your message')
    intro = 'The school has sent you a message' if started else 'The school has replied to your message'
    try:
        _notify_guardian_email(contact['email'], subject_line,
                               f"Dear {contact['display_name'] or 'Parent/Guardian'},\n\n{intro}{about}:\n\n{body}\n\n"
                               "Sign in to the parent portal to continue the conversation.\n\n" + school_name())
    except Exception:
        app.logger.exception('Parent message email notification failed for thread %s', thread.id)
    # Only the first message of a conversation the school starts earns a text; replies go by email and the portal.
    if started:
        try:
            _notify_guardian_sms(contact['phone'], f"{school_name()}: you have a new message from the school{about}. Sign in to the parent portal to read it.",
                                 student_id=thread.student_id, kind='school_message')
        except Exception:
            app.logger.exception('Parent message SMS notification failed for thread %s', thread.id)


# ---------------------------------------------------------------------------------------------
# Routes
@app.get('/admin/school/parent-messages/recipients')
@admin_required
def admin_school_parent_recipients():
    """Parents a message can be started with, found by parent, child or class name. One row per
    child-and-parent pair, because a message is always about one child."""
    me = current_admin()
    if not can_manage(me):
        return admin_access_error('parent.feedback.manage')
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify({'results': []})
    like = f'%{q}%'
    rows = all_rows(
        select(ParentAccount.id.label('parent_id'), ParentAccount.display_name.label('parent_name'),
               Student.id.label('student_id'), Student.first_name, Student.last_name,
               SchoolClass.id.label('class_id'), SchoolClass.name.label('class_name'))
        .select_from(ParentStudentLink)
        .join(ParentAccount, and_(ParentAccount.id == ParentStudentLink.parent_id, ParentAccount.active == 1))
        .join(Student, and_(Student.id == ParentStudentLink.student_id, Student.active == 1))
        .outerjoin(StudentEnrolment, and_(StudentEnrolment.student_id == Student.id, StudentEnrolment.active == 1))
        .outerjoin(SchoolClass, SchoolClass.id == StudentEnrolment.class_id)
        .where(ParentStudentLink.active == 1,
               or_(ParentAccount.display_name.ilike(like), Student.first_name.ilike(like), Student.last_name.ilike(like),
                   SchoolClass.name.ilike(like)))
        .order_by(Student.first_name, Student.last_name, ParentAccount.display_name).limit(60))
    results = []
    for r in rows:
        if not me['admin_type_system'] and r['class_id'] and not _school_class_allowed(me['id'], r['class_id']):
            continue
        results.append({'parent_id': r['parent_id'], 'student_id': r['student_id'], 'parent': r['parent_name'],
                        'student': _full_name(r['first_name'], r['last_name']), 'class': r['class_name'] or ''})
        if len(results) == 25:
            break
    return jsonify({'results': results})


@app.post('/admin/school/parent-messages/new')
@admin_required
@csrf_protect
def admin_school_parent_message_new():
    """Start a conversation with a student's parent."""
    me = current_admin()
    if not can_manage(me):
        return admin_access_error('parent.feedback.manage')
    parent_id = request.form.get('parent_id', type=int)
    student_id = request.form.get('student_id', type=int)
    subject = ' '.join(request.form.get('subject', '').split())
    body = request.form.get('body', '').replace('\r\n', '\n').strip()
    back = messages_url(compose=1)
    link = parent_id and student_id and one(select(ParentStudentLink.id).where(
        ParentStudentLink.parent_id == parent_id, ParentStudentLink.student_id == student_id, ParentStudentLink.active == 1))
    if not link:
        flash("Choose a student's parent from the list.", 'error')
        return redirect(back)
    if not in_scope(me, student_id):
        return admin_access_error('parent feedback scope')
    if not subject or not body:
        flash('Enter a subject and a message.', 'error')
        return redirect(back)
    if len(subject) > MAX_SUBJECT or len(body) > MAX_BODY:
        flash(f'Keep the subject under {MAX_SUBJECT} characters and the message under {MAX_BODY:,}.', 'error')
        return redirect(back)
    now = datetime.now(timezone.utc).isoformat()
    thread = ParentFeedback(parent_id=parent_id, student_id=student_id, subject=subject, body='', status='in_progress',
                            assigned_admin_id=me['id'], created_at=now, updated_at=now)
    db.session.add(thread)
    db.session.flush()
    db.session.add(ParentFeedbackReply(feedback_id=thread.id, admin_id=me['id'], body=body, created_at=now))
    notify_parent(thread, body, me['id'], started=True)
    db.session.commit()
    deliver_parent_alerts(thread, body, started=True)
    audit_log('parent_message_started', 'school', 'parent_feedback', thread.id, {'student_id': student_id})
    flash('Your message has been sent to the parent.', 'success')
    return redirect(messages_url(thread.id))


@app.post('/admin/school/parent-feedback/<int:feedback_id>/take')
@admin_required
@csrf_protect
def admin_school_parent_feedback_take(feedback_id):
    """"I'll respond": make this administrator the one currently responding to the conversation."""
    me = current_admin()
    if not can_manage(me):
        return admin_access_error('parent.feedback.manage')
    row = obj(ParentFeedback, feedback_id)
    if not row:
        abort(404)
    if not in_scope(me, row.student_id):
        return admin_access_error('parent feedback scope')
    row.assigned_admin_id = me['id']
    if row.status == 'open':
        row.status = 'in_progress'
    row.updated_at = datetime.now(timezone.utc).isoformat()
    db.session.commit()
    audit_log('parent_feedback_taken', 'school', 'parent_feedback', feedback_id)
    return redirect(messages_url(feedback_id))


@app.route('/admin/school/parent-feedback')
@admin_required
def admin_school_parent_feedback():
    """The old stand-alone list: parent conversations now live on the Messages page."""
    return redirect(messages_url())


@app.route('/admin/school/parent-feedback/<int:feedback_id>')
@admin_required
def admin_school_parent_feedback_detail(feedback_id):
    return redirect(messages_url(feedback_id))
