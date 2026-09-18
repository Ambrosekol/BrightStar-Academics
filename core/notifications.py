"""Best-effort guardian/parent notification senders (email, WhatsApp, and the
in-app SchoolNotification feed) plus the low-level SMTP primitives they share.

Every sender here swallows its own exceptions: a missing channel, unset
contact detail, or delivery failure must never block the database commit
that triggered the notification (a fee assessment, a new assignment, a
payment). Callers invoke these only after their own commit has succeeded.
"""

import json
import smtplib
import urllib.error
import urllib.request
from datetime import datetime, timezone

from flask import current_app, url_for
from sqlalchemy import select
import os

from models import ParentStudentLink, SchoolNotification, Student, db
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
        except Exception: current_app.logger.exception('Guardian email notification failed for student %s',r['id'])
        try: _notify_guardian_whatsapp(r['guardian_phone'],text)
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
          'Creative Rainbow Montessori School')
    text=f'Crainbow School: {child} has been charged {items_text} — ₦{total_amount:,.2f} for {term}. Check the parent portal for details.'
    try: _notify_guardian_email(student['guardian_email'],subject,body)
    except Exception: current_app.logger.exception('Guardian email (fee assessed) failed for student %s',student_id)
    try: _notify_guardian_whatsapp(student['guardian_phone'],text)
    except Exception: current_app.logger.exception('Guardian WhatsApp (fee assessed) failed for student %s',student_id)


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
        current_app.logger.exception('In-app payment notification failed for student %s',student_id)
    subject=f'Payment received — {child}'
    body=(f'Dear Parent/Guardian,\n\nWe have received a payment of ₦{amount:,.2f} for {category} '
          f'on behalf of {child}. Receipt number: {receipt_no}.\n\n'
          'Please check the parent portal for your full fee account and outstanding balance.\n\n'
          'Thank you,\nCreative Rainbow Montessori School')
    text=f'Crainbow School: payment of ₦{amount:,.2f} received for {child} ({category}). Receipt {receipt_no}.'
    try: _notify_guardian_email(student['guardian_email'],subject,body)
    except Exception: current_app.logger.exception('Guardian email (payment recorded) failed for student %s',student_id)
    try: _notify_guardian_whatsapp(student['guardian_phone'],text)
    except Exception: current_app.logger.exception('Guardian WhatsApp (payment recorded) failed for student %s',student_id)
