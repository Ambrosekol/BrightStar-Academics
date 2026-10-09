"""A school's own record of every guardian notice attempted - a new assignment, a fee charged, a
released report card or exam timetable - by email or SMS, successful or not.

Payment receipts already have exactly this (finance_delivery_logs, blueprints/finance/routes.py);
this is everything else, recorded by core/notifications.py's two guardian-contact senders as each
attempt happens (core/notifications.py's _log_notification_delivery). The point, per
recommendations.html's product recommendations, is that a school can answer "did she get it?" from
this page itself, without asking a developer to read a server log.
"""

from flask import render_template, request
from sqlalchemy import select

from app import app
from core.db_helpers import all_rows
from core.security import admin_access_error, admin_has_permission, admin_required, current_admin
from models import NotificationDeliveryLog, Student

KIND_LABELS = {
    'work': 'New assignment/project',
    'fee_assessed': 'Fee charged',
    'payment_recorded': 'Payment recorded',
    'report_card_ready': 'Report card ready',
    'timetable_released': 'Exam timetable released',
}


@app.route('/admin/school/notice-log')
@admin_required
def admin_notification_log():
    me = current_admin()
    if not admin_has_permission(me['id'], 'audit.view'):
        return admin_access_error('audit.view')
    try:
        student_id = int(request.args.get('student_id', '') or 0)
    except (TypeError, ValueError):
        student_id = 0
    conditions = []
    if student_id:
        conditions.append(NotificationDeliveryLog.student_id == student_id)
    rows = all_rows(
        select(NotificationDeliveryLog.id, NotificationDeliveryLog.kind, NotificationDeliveryLog.channel,
               NotificationDeliveryLog.recipient, NotificationDeliveryLog.status,
               NotificationDeliveryLog.detail, NotificationDeliveryLog.created_at,
               NotificationDeliveryLog.student_id, Student.first_name, Student.middle_name, Student.last_name,
               Student.admission_no)
        .outerjoin(Student, Student.id == NotificationDeliveryLog.student_id)
        .where(*conditions)
        .order_by(NotificationDeliveryLog.id.desc()).limit(250))
    logs = [dict(row, kind_label=KIND_LABELS.get(row['kind'], row['kind'])) for row in rows]
    return render_template('admin_notification_log.html', logs=logs, student_id=student_id or None)
