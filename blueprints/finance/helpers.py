"""Private helpers used only by the finance routes in this package: fee
balance math, receipt rendering/delivery, and the receipt signature image.

_finance_payment_balance and _finance_assessment_balance were found to be
dead code during this extraction (defined, never called anywhere in the
repo) and were dropped rather than moved.
"""

import base64
import json
import math
import secrets
import textwrap
import urllib.error
import urllib.request
from datetime import datetime, timezone

from flask import abort, current_app
from sqlalchemy import Numeric, and_, cast, func, select

from models import (
    AcademicSession, Admin, FinanceDeliveryLog, FinanceFeeAssessment,
    FinancePayment, FinancePaymentAllocation, School, SchoolClass,
    SchoolPublicSetting, SchoolSetting, Student, StudentEnrolment, db,
)
from core import theme
from core.branding import receipt_prefix, school_brand, school_name
from core.db_helpers import all_rows, one, one_scalar, _flatten
from core.delivery import GRAPH_URL, email_settings, send_email, whatsapp_settings
from core.jobs import job_handler
from core.notifications import _ng_phone
from core.security import admin_has_permission, current_admin, is_school_admin
from core.storage import read_upload_bytes, save_upload_bytes
from core.uploads import STATIC


def _finance_can_view_all(admin=None):
    admin=admin or current_admin()
    return bool(admin and (is_school_admin(admin) or admin_has_permission(admin['id'],'finance.view_all')))

def _next_receipt_no():
    """Next sequential receipt number for the current year."""
    prefix=f'{receipt_prefix()}-{datetime.now().year}-'
    last=one_scalar(select(FinancePayment.receipt_no)
                    .where(FinancePayment.receipt_no.startswith(prefix,autoescape=True))
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

# No payment or fee is ever this large; the receipt's amount in words stops at the billions.
MAX_MONEY=10**12

def _money(value):
    """An amount of naira as submitted, kept to the kobo, or None when it is not usable.

    NaN, infinity and absurdly large figures are refused: NaN would poison every
    total it was added to, and a figure past the billions breaks the receipt.
    """
    try: amount=float(value)
    except (TypeError,ValueError): return None
    if not math.isfinite(amount) or abs(amount)>=MAX_MONEY: return None
    return round(amount,2)

def _payment_status(assessed, paid):
    """Paid, Part Paid or Unpaid, worked out to the kobo so a floating-point crumb never flips it."""
    assessed=round(float(assessed or 0),2); paid=round(float(paid or 0),2)
    if paid>=assessed and assessed>0: return 'Paid'
    return 'Part Paid' if paid>0 else 'Unpaid'

def _wrap_text(text, width, max_lines):
    """A few short lines of ``text`` for a fixed-size block on the receipt."""
    return textwrap.wrap(' '.join(str(text or '').split()),width)[:max_lines]

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
    # What the payment was for, and whose it was: the student is named on the receipt itself.
    row['purpose']=(f"{row.get('category') or 'School Fees'} — {_student_display(row)}"
                    + (f" ({row['class_name']})" if row.get('class_name') else ''))
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

def _school_logo_bytes():
    """The school's own logo, stored when the school was created, as bytes, or None."""
    try:
        stored=one_scalar(select(SchoolPublicSetting.setting_value)
                          .where(SchoolPublicSetting.setting_key=='school_logo'))
    except Exception:
        db.session.rollback(); return None
    return read_upload_bytes(stored) if stored else None


def _receipt_signature_relpath():
    row=_receipt_signature_setting_row()
    return (row.setting_value or '').strip() if row and row.setting_value else ''

def _receipt_signature_bytes():
    """The configured authorised-signature image, as bytes, or None."""
    rel=_receipt_signature_relpath()
    return read_upload_bytes(rel) if rel else None

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
    from core.uploads import format_limit, format_size_over, image_limit_bytes
    max_bytes=image_limit_bytes()
    if len(raw) > max_bytes: raise ValueError(f'The drawn signature is {format_size_over(len(raw),max_bytes)}. The limit is {format_limit(max_bytes)}. Clear the pad and draw it again, a little smaller.')
    if not raw.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('The drawn signature could not be read. Please try drawing it again.')
    filename=f"authorised_{secrets.token_hex(10)}.png"
    return save_upload_bytes('signatures', filename, raw, 'image/png')

def _receipt_summary(row):
    """What the student owes for the session, as it stood when this receipt was issued.

    Counts payments up to and including this one, so reprinting an old receipt never shows a
    balance that later payments have changed. None when the student has no fees charged.
    """
    charged=round(float(one_scalar(select(func.coalesce(func.sum(FinanceFeeAssessment.amount),0)).where(
        FinanceFeeAssessment.student_id==row['student_id'],FinanceFeeAssessment.session_id==row['session_id'],
        FinanceFeeAssessment.active==1),0)),2)
    if charged<=0: return None
    paid=round(float(one_scalar(select(func.coalesce(func.sum(FinancePayment.amount),0)).where(
        FinancePayment.student_id==row['student_id'],FinancePayment.session_id==row['session_id'],
        FinancePayment.status=='posted',FinancePayment.id<=row['id']),0)),2)
    return {'charged':charged,'paid':paid,'balance':round(max(charged-paid,0.0),2)}

def _receipt_sheet(payment_id):
    """Everything drawn on one receipt, in the school's own identity, or None if there is no such payment.

    The one description of a receipt: the on-screen page, the printout and the PDF sent to parents are
    all made from it (see core/receipt_pdf.py), so they can never differ. Colours are the school's two
    brand colours; a school that has chosen none gets the portal's own.
    """
    row=_receipt_payload(payment_id)
    if not row: return None
    brand=school_brand()
    primary=brand.get('primary') or theme.DEFAULT_PRIMARY
    accent=brand.get('accent') or theme.DEFAULT_ACCENT
    try: date_text=datetime.fromisoformat(str(row.get('paid_at')).replace('Z','+00:00')).strftime('%d/%m/%Y')
    except Exception: date_text=str(row.get('paid_at') or '')[:10]
    method=(row.get('method') or '').strip()
    reference=(row.get('reference') or '').strip()
    student_line=' · '.join(x for x in (_student_display(row),row.get('admission_no'),row.get('class_name')) if x)
    purpose=(row.get('category') or 'School Fees')+(f" — {row['session_name']} session" if row.get('session_name') else '')
    received_by=one_scalar(select(Admin.display_name).where(Admin.id==row['recorded_by']),'')
    logo=_school_logo_bytes()
    from core.receipt_pdf import _initials
    return {
        'payment_id':payment_id,'initials':_initials(school_name()),'status':row['status'],'voided':row['status']=='voided',
        'school':{'name':school_name(),'motto':brand.get('motto') or '','address':brand.get('address') or '',
                  'phone':brand.get('phone') or '','email':brand.get('email') or ''},
        'primary':primary,'accent':accent,'tint':theme.shade(primary,0.92),'soft':theme.shade(primary,0.8),
        'logo':logo,'logo_url':brand.get('logo_url') if logo else None,
        'signature':_receipt_signature_bytes(),'signature_relpath':_receipt_signature_relpath(),
        'number':row['receipt_no'],'date':date_text,'payer':row['payer_name'],'student':student_line,
        'purpose':purpose,'method':method+(f' · Ref {reference}' if reference else '') if method or reference else '—',
        'amount':float(row.get('amount') or 0),'amount_words':row['amount_words'],
        'amount_naira':row['amount_naira'],'amount_kobo':row['amount_kobo'],
        'notes':' '.join(str(row.get('notes') or '').split()),'received_by':received_by or '',
        'summary':_receipt_summary(row),
    }

def _receipt_pdf(payment_id):
    """``(pdf bytes, payment row)``. The design is in core/receipt_pdf.py."""
    row=_receipt_payload(payment_id)
    if not row: abort(404)
    from core.receipt_pdf import render_receipt_pdf
    return render_receipt_pdf(_receipt_sheet(payment_id)),row


def _send_email_receipt(payment_id):
    row=_receipt_payload(payment_id)
    if not row: return False,'Receipt not found.'
    if row['status']=='voided': return False,'This payment has been voided, so its receipt is not sent.'
    settings=email_settings()
    if settings is None: return False,'Email delivery is not set up for this school. A school administrator can add it under Email & WhatsApp.'
    recipient=(row['guardian_email'] or '').strip()
    if not recipient: return False,'This student has no parent/guardian email address.'
    pdf,_=_receipt_pdf(payment_id)
    from email.message import EmailMessage
    school=school_name()
    sheet=_receipt_sheet(payment_id); summary=sheet['summary']
    lines=[f'Dear Parent/Guardian,','',
           f'Thank you. We have received a payment for {_student_display(row)}, and the official receipt {row["receipt_no"]} is attached.','',
           f'Amount paid: {_format_money(row["amount"])}',f'Date: {sheet["date"]}',f'Paid for: {sheet["purpose"]}']
    if summary: lines.append(f'Balance due for the session: {_format_money(summary["balance"])}')
    if sheet['notes']: lines+=['',f'Note from the school: {sheet["notes"]}']
    lines+=['',school]
    msg=EmailMessage(); msg['Subject']=f'{school} Payment Receipt {row["receipt_no"]}'; msg['From']=settings.sender; msg['To']=recipient; msg.set_content('\n'.join(lines)); msg.add_attachment(pdf,maintype='application',subtype='pdf',filename=f'{row["receipt_no"]}.pdf')
    try:
        send_email(settings,msg)
        return True,recipient
    except Exception as exc: return False,f'Email delivery failed: {exc}'

def _send_whatsapp_receipt(payment_id):
    row=_receipt_payload(payment_id)
    if not row: return False,'Receipt not found.'
    if row['status']=='voided': return False,'This payment has been voided, so its receipt is not sent.'
    settings=whatsapp_settings(); token,phone_id,version=((settings.token,settings.phone_id,settings.version) if settings else ('','','')); recipient=_ng_phone(row['guardian_phone'])
    if settings is None: return False,'WhatsApp is not set up for this school. A school administrator can add it under Email & WhatsApp.'
    if not recipient: return False,'This student has no valid parent/guardian WhatsApp number.'
    pdf,_=_receipt_pdf(payment_id); boundary='----BrightstarsBoundary'+secrets.token_hex(8); url=f'{GRAPH_URL}/{version}/{phone_id}/media'
    body=(f'--{boundary}\r\nContent-Disposition: form-data; name="messaging_product"\r\n\r\nwhatsapp\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{row["receipt_no"]}.pdf"\r\nContent-Type: application/pdf\r\n\r\n').encode()+pdf+(f'\r\n--{boundary}--\r\n').encode()
    try:
        req=urllib.request.Request(url,data=body,method='POST',headers={'Authorization':f'Bearer {token}','Content-Type':f'multipart/form-data; boundary={boundary}'})
        with urllib.request.urlopen(req,timeout=30) as resp: media=json.loads(resp.read().decode())
        media_id=media.get('id')
        if not media_id: return False,'WhatsApp media upload returned no media ID.'
        payload=json.dumps({'messaging_product':'whatsapp','to':recipient,'type':'document','document':{'id':media_id,'caption':f'Official payment receipt {row["receipt_no"]} — {_student_display(row)}','filename':f'{row["receipt_no"]}.pdf'}}).encode(); req2=urllib.request.Request(f'{GRAPH_URL}/{version}/{phone_id}/messages',data=payload,method='POST',headers={'Authorization':f'Bearer {token}','Content-Type':'application/json'})
        with urllib.request.urlopen(req2,timeout=30) as resp: result=json.loads(resp.read().decode())
        return True,result.get('messages',[{}])[0].get('id',recipient)
    except urllib.error.HTTPError as exc: return False,f'WhatsApp API error {exc.code}: {exc.read().decode(errors="replace")[:500]}'
    except Exception as exc: return False,f'WhatsApp delivery failed: {exc}'

@job_handler('send_payment_receipt')
def _send_payment_receipt_to_guardian(payment_id, actor_id):
    """Send a newly recorded payment's receipt to the guardian by email and by WhatsApp.

    This is what makes telling the parents automatic: nobody has to remember to press "send". Each
    attempt is logged like a hand-sent one, so the bursar can see on the receipt page whether it
    arrived. Never raises; runs after the payment was committed, as the 'send_payment_receipt'
    job (see core/jobs.py) - durable, so a thread that never finishes leaves it to retry.
    """
    row=_receipt_payload(payment_id)
    if not row or row['status']=='voided': return
    for channel,send,contact in (('email',_send_email_receipt,row['guardian_email']),
                                 ('whatsapp',_send_whatsapp_receipt,row['guardian_phone'])):
        try:
            ok,msg=send(payment_id)
            _log_receipt_delivery(payment_id,channel,contact,ok,msg,actor_id)
        except Exception:
            db.session.rollback()
            current_app.logger.exception('Automatic %s receipt failed for payment %s',channel,payment_id)

def _log_receipt_delivery(payment_id, channel, recipient, ok, msg, actor_id):
    """Record every delivery attempt, successful or not."""
    db.session.add(FinanceDeliveryLog(
        payment_id=payment_id,channel=channel,recipient=recipient,
        status='sent' if ok else 'failed',
        provider_reference=msg if ok else None,
        error_message=None if ok else msg,
        sent_by=actor_id,sent_at=datetime.now(timezone.utc).isoformat()))
    db.session.commit()
    if not ok:
        from core.alerting import note_delivery_failure
        note_delivery_failure(channel, msg)

def _payment_allocated_sq():
    """How much of each payment (the outer query's row) still stands as applied to fee assessments."""
    return (select(func.coalesce(func.sum(FinancePaymentAllocation.amount), 0))
            .where(FinancePaymentAllocation.payment_id == FinancePayment.id,
                   FinancePaymentAllocation.voided_at.is_(None))
            .correlate(FinancePayment).scalar_subquery())


def _unallocated_conditions(scope_admin_id=None):
    conditions = [FinancePayment.status == 'posted']
    if scope_admin_id is not None:
        conditions.append(FinancePayment.recorded_by == scope_admin_id)
    return conditions


def _finance_unallocated_payments(scope_admin_id=None):
    """Every posted payment whose full amount has not yet been applied to a fee assessment,
    oldest first - each one, a school's own money already collected and sitting in the bank, that
    nobody has yet said what it was for. ``scope_admin_id`` narrows this to one officer's own
    takings, the same 'view your own' scope a finance.record-only officer sees everywhere else;
    ``None`` is the whole school, for finance.view_all/finance.manage.
    """
    rows = all_rows(
        select(FinancePayment.id, FinancePayment.receipt_no, FinancePayment.amount,
               FinancePayment.paid_at, FinancePayment.method, FinancePayment.student_id,
               Student.first_name, Student.middle_name, Student.last_name,
               Student.admission_no, _payment_allocated_sq().label('allocated'))
        .join(Student, Student.id == FinancePayment.student_id)
        .where(*_unallocated_conditions(scope_admin_id))
        .order_by(FinancePayment.paid_at, FinancePayment.id))
    out = []
    for r in rows:
        row = dict(r)
        allocated = round(float(row['allocated'] or 0), 2)
        amount = round(float(row['amount'] or 0), 2)
        unallocated = round(amount - allocated, 2)
        if unallocated > 0.005:  # a few kobo of float slack, never a real balance
            row['allocated'] = allocated
            row['unallocated'] = unallocated
            out.append(row)
    return out


def _finance_unallocated_summary(scope_admin_id=None):
    """``{'count', 'total'}`` for the banner and the dashboard card - the same rows
    _finance_unallocated_payments would list, just how many and how much.

    Counted and summed by the database rather than by loading every row: this runs on every page
    render for a finance officer, and a school's payment history only grows. Each amount is rounded
    to pennies before the subtraction, exactly as _finance_unallocated_payments does, so the two
    always agree on which payments are unallocated.
    """
    amount = cast(FinancePayment.amount, Numeric(14, 2))
    allocated = cast(_payment_allocated_sq(), Numeric(14, 2))
    per_payment = (select((func.round(amount, 2) - func.round(allocated, 2)).label('unallocated'))
                   .select_from(FinancePayment)
                   .join(Student, Student.id == FinancePayment.student_id)
                   .where(*_unallocated_conditions(scope_admin_id))
                   .subquery())
    count, total = db.session.execute(
        select(func.count(), func.coalesce(func.sum(per_payment.c.unallocated), 0))
        .where(per_payment.c.unallocated > 0.005)).one()
    return {'count': int(count or 0), 'total': round(float(total or 0), 2)}


def _finance_payment_allocated(payment_id):
    """How much of one payment has been applied to fee assessments."""
    return round(float(one_scalar(
        select(func.coalesce(func.sum(FinancePaymentAllocation.amount),0))
        .where(FinancePaymentAllocation.payment_id==payment_id,
               FinancePaymentAllocation.voided_at.is_(None)), 0)),2)

def _finance_assessment_allocated(assessment_id):
    """Total still-standing allocations against one fee assessment.

    Only money from payments that still stand counts: the allocations of a voided
    payment are not money this fee has received, exactly as the fee's balance shows.
    """
    return round(float(one_scalar(
        select(func.coalesce(func.sum(FinancePaymentAllocation.amount),0))
        .select_from(FinancePaymentAllocation)
        .join(FinancePayment,FinancePayment.id==FinancePaymentAllocation.payment_id)
        .where(FinancePaymentAllocation.assessment_id==assessment_id,
               FinancePaymentAllocation.voided_at.is_(None),
               FinancePayment.status=='posted'), 0)),2)

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
                      FinancePaymentAllocation.voided_at.is_(None),
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
        # Kept to the kobo: three instalments of a fee must not read "part paid" by a floating-point crumb.
        assessed=round(float(row['amount'] or 0),2)
        paid=round(float(row['allocated'] or 0),2)
        item=dict(row)
        item['assessed']=assessed
        item['paid']=paid
        item['outstanding']=round(max(0.0, assessed-paid),2)
        item['status']=_payment_status(assessed,paid)
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
    assessed=round(sum(r['assessed'] for r in rows),2)
    paid=round(sum(r['paid'] for r in rows),2)
    outstanding=round(sum(r['outstanding'] for r in rows),2)
    paid_scope=[FinancePayment.student_id==student_id,FinancePayment.status=='posted']
    if session_id is not None:
        paid_scope.append(FinancePayment.session_id==session_id)
    raw_paid=round(float(one_scalar(
        select(func.coalesce(func.sum(FinancePayment.amount),0)).where(*paid_scope), 0)),2)
    unallocated=round(max(0.0, raw_paid-paid),2)
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
