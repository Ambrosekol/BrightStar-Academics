"""A school's own record of every guardian notice attempted - a new assignment, a fee charged, a
released report card or exam timetable, a store purchase collected - by email or SMS, successful or not.

Payment receipts already have exactly this (finance_delivery_logs, blueprints/finance/routes.py);
this is everything else, recorded by core/notifications.py's two guardian-contact senders as each
attempt happens (core/notifications.py's _log_notification_delivery). The point, per
recommendations.html's product recommendations, is that a school can answer "did she get it?" from
this page itself, without asking a developer to read a server log.

It is shown two ways, fifty at a time, searched and filtered on the server:

* for one student, in a pop-up from the student's record (admin_notification_log, which also still
  opens as a page of its own, for every student);
* as the Notices tab beside the Activity log in Settings (admin_audit_notices). The two tabs are two
  addresses loaded in place, so opening one never reads the other's records.
"""

from flask import render_template, request
from sqlalchemy import func, or_, select

from app import app
from core.db_helpers import all_rows, one_scalar
from core.security import admin_access_error, admin_has_permission, admin_required, current_admin
from models import NotificationDeliveryLog, Student

PER_PAGE = 50

KIND_LABELS = {
    'work': 'New assignment/project',
    'fee_assessed': 'Fee charged',
    'payment_recorded': 'Payment recorded',
    'report_card_ready': 'Report card ready',
    'timetable_released': 'Exam timetable released',
    'store_claimed': 'Store purchase collected',
}


def _int(name, default=0):
    try:
        return int(request.args.get(name, '') or default)
    except (TypeError, ValueError):
        return default


def notice_page(student_id=0):
    """One page of the notice log, with the search and filter it was asked for."""
    q = request.args.get('q', '').strip()[:80]
    failed = request.args.get('only') == 'failed'
    conditions = []
    if student_id:
        conditions.append(NotificationDeliveryLog.student_id == student_id)
    if failed:
        conditions.append(NotificationDeliveryLog.status != 'sent')
    if q:
        kinds = [k for k, label in KIND_LABELS.items() if q.lower() in label.lower()]
        conditions.append(or_(Student.first_name.icontains(q, autoescape=True), Student.last_name.icontains(q, autoescape=True),
                              Student.admission_no.icontains(q, autoescape=True),
                              NotificationDeliveryLog.recipient.icontains(q, autoescape=True),
                              NotificationDeliveryLog.channel.icontains(q, autoescape=True),
                              NotificationDeliveryLog.kind.in_(kinds) if kinds else NotificationDeliveryLog.kind == q))
    total = one_scalar(select(func.count()).select_from(NotificationDeliveryLog)
                       .outerjoin(Student, Student.id == NotificationDeliveryLog.student_id).where(*conditions), 0)
    pages = max(1, -(-total // PER_PAGE))
    page = min(max(1, _int('page', 1)), pages)
    rows = all_rows(
        select(NotificationDeliveryLog.id, NotificationDeliveryLog.kind, NotificationDeliveryLog.channel,
               NotificationDeliveryLog.recipient, NotificationDeliveryLog.status,
               NotificationDeliveryLog.detail, NotificationDeliveryLog.created_at,
               NotificationDeliveryLog.student_id, Student.first_name, Student.middle_name, Student.last_name,
               Student.admission_no)
        .outerjoin(Student, Student.id == NotificationDeliveryLog.student_id)
        .where(*conditions)
        .order_by(NotificationDeliveryLog.id.desc()).limit(PER_PAGE).offset((page - 1) * PER_PAGE))
    logs = [dict(row, kind_label=KIND_LABELS.get(row['kind'], row['kind'])) for row in rows]
    return {'logs': logs, 'q': q, 'failed': failed, 'page': page, 'pages': pages, 'total': total}


@app.route('/admin/school/notice-log')
@admin_required
def admin_notification_log():
    """The notice log for one student (a pop-up from their record) or for everyone."""
    me = current_admin()
    if not admin_has_permission(me['id'], 'audit.view'):
        return admin_access_error('audit.view')
    student_id = _int('student_id')
    student = None
    if student_id:
        student = all_rows(select(Student.id, Student.first_name, Student.last_name, Student.admission_no)
                           .where(Student.id == student_id))
        student = student[0] if student else None
    return render_template('admin_notification_log.html', student_id=student_id or None, student=student,
                           **notice_page(student_id))


@app.route('/admin/administration/audit-logs/notices')
@admin_required
def admin_audit_notices():
    """The Notices tab beside the Activity log in Settings."""
    me = current_admin()
    if not admin_has_permission(me['id'], 'audit.view'):
        return admin_access_error('audit.view')
    return render_template('admin_audit_logs.html', tab='notices', **notice_page())
