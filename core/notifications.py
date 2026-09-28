"""Best-effort guardian/parent notification senders (email, WhatsApp, and the
in-app SchoolNotification feed) plus the low-level SMTP primitives they share.

Every sender here swallows its own exceptions: a missing channel, unset
contact detail, or delivery failure must never block the database commit
that triggered the notification (a fee assessment, a new assignment, a
payment). Callers invoke these only after their own commit has succeeded.
"""

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

from flask import current_app, url_for
from sqlalchemy import select
import os

from core.branding import school_name
from core.delivery import GRAPH_URL, email_settings, send_email, whatsapp_settings
from models import NotificationDeliveryLog, ParentStudentLink, SchoolNotification, Student, db
from core.db_helpers import all_rows, one, tuples


def _ng_phone(value):
    raw=''.join(ch for ch in str(value or '') if ch.isdigit() or ch=='+')
    if raw.startswith('0') and len(raw)>=10: return '+234'+raw[1:]
    if raw.startswith('234'): return '+'+raw
    return raw


def _parent_ids_for_student(student_id):
    """Every parent account actively linked to a student, for in-app alerts."""
    return [pid for (pid,) in tuples(
        select(ParentStudentLink.parent_id)
        .where(ParentStudentLink.student_id==student_id,ParentStudentLink.active==1))]


def _log_notification_delivery(student_id,kind,channel,recipient,ok,detail):
    """Record one guardian-notification delivery attempt, so a school can answer "did she get
    it?" itself from the notice log (blueprints/school/notice_log.py) instead of asking a
    developer to read a server log. Never raises: a logging failure must not turn a delivered
    notice into a lost one."""
    if not kind: return
    try:
        db.session.add(NotificationDeliveryLog(
            student_id=student_id,kind=kind,channel=channel,recipient=recipient,
            status='sent' if ok else 'failed',detail=None if ok else str(detail)[:500],
            created_at=datetime.now(timezone.utc).isoformat()))
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Could not record a notification-delivery log entry')


def _notify_guardian_email(guardian_email, subject, body, *, student_id=None, kind=None):
    """Best-effort plain-text email to a parent/guardian. Never raises."""
    settings=email_settings()
    if settings is None:
        _log_notification_delivery(student_id,kind,'email',guardian_email,False,'Email delivery is not configured.')
        return False,'Email delivery is not configured.'
    recipient=(guardian_email or '').strip()
    if not recipient:
        _log_notification_delivery(student_id,kind,'email',recipient,False,'No guardian email address on file.')
        return False,'No guardian email address on file.'
    from email.message import EmailMessage
    msg=EmailMessage(); msg['Subject']=subject; msg['From']=settings.sender; msg['To']=recipient; msg.set_content(body)
    try:
        send_email(settings,msg)
        _log_notification_delivery(student_id,kind,'email',recipient,True,None)
        return True,recipient
    except Exception as exc:
        from core.alerting import note_delivery_failure
        note_delivery_failure('email',exc)
        _log_notification_delivery(student_id,kind,'email',recipient,False,exc)
        return False,f'Email delivery failed: {exc}'


def _notify_guardian_whatsapp(guardian_phone, text, *, student_id=None, kind=None):
    """Best-effort plain-text WhatsApp message to a parent/guardian. Never raises."""
    settings=whatsapp_settings(); token,phone_id,version=((settings.token,settings.phone_id,settings.version) if settings else ('','','')); recipient=_ng_phone(guardian_phone)
    if settings is None:
        _log_notification_delivery(student_id,kind,'whatsapp',recipient,False,'WhatsApp Business Cloud API is not configured.')
        return False,'WhatsApp Business Cloud API is not configured.'
    if not recipient:
        _log_notification_delivery(student_id,kind,'whatsapp',recipient,False,'No valid guardian WhatsApp number on file.')
        return False,'No valid guardian WhatsApp number on file.'
    payload=json.dumps({'messaging_product':'whatsapp','to':recipient,'type':'text','text':{'body':text}}).encode()
    req=urllib.request.Request(f'{GRAPH_URL}/{version}/{phone_id}/messages',data=payload,method='POST',
        headers={'Authorization':f'Bearer {token}','Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=8) as resp: result=json.loads(resp.read().decode())
        _log_notification_delivery(student_id,kind,'whatsapp',recipient,True,None)
        return True,result.get('messages',[{}])[0].get('id',recipient)
    except urllib.error.HTTPError as exc:
        from core.alerting import note_delivery_failure
        note_delivery_failure('whatsapp',exc)
        detail=f'WhatsApp API error {exc.code}: {exc.read().decode(errors="replace")[:500]}'
        _log_notification_delivery(student_id,kind,'whatsapp',recipient,False,detail)
        return False,detail
    except Exception as exc:
        from core.alerting import note_delivery_failure
        note_delivery_failure('whatsapp',exc)
        _log_notification_delivery(student_id,kind,'whatsapp',recipient,False,exc)
        return False,f'WhatsApp delivery failed: {exc}'


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
              f'{school_name()}')
        text=f'{school_name()}: {child} has a new {kind} - "{title}". Due: {due_text}.'
        try: _notify_guardian_email(r['guardian_email'],subject,body,student_id=r['id'],kind='work')
        except Exception: current_app.logger.exception('Guardian email notification failed for student %s',r['id'])
        try: _notify_guardian_whatsapp(r['guardian_phone'],text,student_id=r['id'],kind='work')
        except Exception: current_app.logger.exception('Guardian WhatsApp notification failed for student %s',r['id'])


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
        current_app.logger.exception('In-app fee-assessed notification failed for student %s',student_id)
    subject=f'New fee charged — {child}'
    body=(f'Dear Parent/Guardian,\n\n{child} has been charged a new fee: {items_text}.\n'
          f'Amount: ₦{total_amount:,.2f} — {term} ({session_name}).\n\n'
          'Please check the parent portal for your full fee account and outstanding balance.\n\n'
          f'{school_name()}')
    text=f'{school_name()}: {child} has been charged {items_text} — ₦{total_amount:,.2f} for {term}. Check the parent portal for details.'
    try: _notify_guardian_email(student['guardian_email'],subject,body,student_id=student_id,kind='fee_assessed')
    except Exception: current_app.logger.exception('Guardian email (fee assessed) failed for student %s',student_id)
    try: _notify_guardian_whatsapp(student['guardian_phone'],text,student_id=student_id,kind='fee_assessed')
    except Exception: current_app.logger.exception('Guardian WhatsApp (fee assessed) failed for student %s',student_id)


def _notify_parents_payment_recorded(student_id, receipt_no, amount, category, admin_id, external=True):
    """Alert a student's parents that a payment has been recorded for them.

    Same best-effort contract as _notify_parents_fee_assessed. Call only
    after the payment has been committed. With ``external=False`` only the
    in-app alert is made; the email and WhatsApp message are then the caller's
    to send (the finance routes send the receipt itself).
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
        current_app.logger.exception('In-app payment notification failed for student %s',student_id)
    if not external: return
    subject=f'Payment received — {child}'
    body=(f'Dear Parent/Guardian,\n\nWe have received a payment of ₦{amount:,.2f} for {category} '
          f'on behalf of {child}. Receipt number: {receipt_no}.\n\n'
          'Please check the parent portal for your full fee account and outstanding balance.\n\n'
          f'Thank you,\n{school_name()}')
    text=f'{school_name()}: payment of ₦{amount:,.2f} received for {child} ({category}). Receipt {receipt_no}.'
    try: _notify_guardian_email(student['guardian_email'],subject,body,student_id=student_id,kind='payment_recorded')
    except Exception: current_app.logger.exception('Guardian email (payment recorded) failed for student %s',student_id)
    try: _notify_guardian_whatsapp(student['guardian_phone'],text,student_id=student_id,kind='payment_recorded')
    except Exception: current_app.logger.exception('Guardian WhatsApp (payment recorded) failed for student %s',student_id)


def _notify_parents_report_card_ready(student_id, session_name, term, view_url, admin_id=None):
    """Tell a student's parents that a term's results are released and the report card is ready.

    An in-app alert for every linked parent account, plus an email and a WhatsApp message to the
    guardian contact on the student's record. Same best-effort contract as the senders above: a
    channel that is missing or fails never stops the others. Call only after the release has been
    committed. ``view_url`` is where the parent opens the card once signed in.
    """
    student=one(select(Student.first_name,Student.last_name,Student.guardian_email,
                       Student.guardian_phone).where(Student.id==student_id))
    if not student: return
    child=f"{student['first_name']} {student['last_name']}".strip()
    period='the annual' if term=='Full Session' else f'the {term}'
    label='Annual' if term=='Full Session' else term
    now=datetime.now(timezone.utc).isoformat()
    title=f'{label} report card ready for {child}'
    message=f'{child}’s results for {period} report card ({session_name}) have been released. The report card is ready to view and download.'
    try:
        for pid in _parent_ids_for_student(student_id):
            db.session.add(SchoolNotification(
                recipient_type='parent',recipient_id=pid,student_id=student_id,
                category='results',title=title,message=message,action_url=view_url,
                created_at=now,created_by=admin_id))
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception('In-app report card notification failed for student %s',student_id)
    subject=f'{label} report card ready — {child}'
    body=(f'Dear Parent/Guardian,\n\n{child}’s results for {period} report card ({session_name}) have been released, '
          'and the report card is now ready.\n\n'
          f'Sign in to the parent portal to view and download it:\n{view_url}\n\n'
          f'Thank you,\n{school_name()}')
    text=(f'{school_name()}: {child}’s {label} report card ({session_name}) is ready. '
          f'Sign in to the parent portal to view and download it: {view_url}')
    try: _notify_guardian_email(student['guardian_email'],subject,body,student_id=student_id,kind='report_card_ready')
    except Exception: current_app.logger.exception('Guardian email (report card ready) failed for student %s',student_id)
    try: _notify_guardian_whatsapp(student['guardian_phone'],text,student_id=student_id,kind='report_card_ready')
    except Exception: current_app.logger.exception('Guardian WhatsApp (report card ready) failed for student %s',student_id)
