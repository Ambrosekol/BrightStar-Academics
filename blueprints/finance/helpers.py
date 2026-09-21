"""Private helpers used only by the finance routes in this package: fee
balance math, receipt rendering/delivery, and the receipt signature image.

_finance_payment_balance and _finance_assessment_balance were found to be
dead code during this extraction (defined, never called anywhere in the
repo) and were dropped rather than moved.
"""

import base64
import json
import math
import os
import secrets
import textwrap
import urllib.error
import urllib.request
from datetime import datetime, timezone

from flask import abort
from sqlalchemy import and_, func, select

from models import (
    AcademicSession, FinanceDeliveryLog, FinanceFeeAssessment,
    FinancePayment, FinancePaymentAllocation, School, SchoolClass,
    SchoolPublicSetting, SchoolSetting, Student, StudentEnrolment, db,
)
from core.branding import receipt_prefix, school_brand, school_name
from core.db_helpers import all_rows, one, one_scalar, _flatten
from core.delivery import GRAPH_URL, email_settings, send_email, whatsapp_settings
from core.notifications import _ng_phone
from core.security import admin_has_permission, current_admin, is_school_admin
from core.storage import stored_upload_path, uploads_dir
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

def _school_logo_path():
    """The school's own logo, stored when the school was created, or None."""
    try:
        stored=one_scalar(select(SchoolPublicSetting.setting_value)
                          .where(SchoolPublicSetting.setting_key=='school_logo'))
    except Exception:
        db.session.rollback(); return None
    path=stored_upload_path(stored) if stored else None
    return path if path and os.path.exists(path) else None


def _receipt_signature_relpath():
    row=_receipt_signature_setting_row()
    return (row.setting_value or '').strip() if row and row.setting_value else ''

def _receipt_signature_abspath():
    """Filesystem path to the configured authorised-signature image, or None."""
    rel=_receipt_signature_relpath()
    if not rel: return None
    path=stored_upload_path(rel)
    return path if path and os.path.exists(path) else None

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
    max_bytes=int(os.environ.get('BRIGHTSTARS_MAX_UPLOAD_BYTES', 5 * 1024 * 1024))
    if len(raw) > max_bytes: raise ValueError('Signature image is too large.')
    if not raw.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('The drawn signature could not be read. Please try drawing it again.')
    folder=os.path.join(uploads_dir(),'signatures'); os.makedirs(folder,exist_ok=True)
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
        try: pdfmetrics.registerFont(TTFont('ReceiptBody',font_regular)); pdfmetrics.registerFont(TTFont('ReceiptBold',font_bold))
        except Exception: pass
    regular='ReceiptBody' if 'ReceiptBody' in pdfmetrics.getRegisteredFontNames() else 'Helvetica'; bold='ReceiptBold' if 'ReceiptBold' in pdfmetrics.getRegisteredFontNames() else 'Helvetica-Bold'
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

    # The school's own logo, or, when it has none, its own name in the logo's place. Never another school's mark.
    logo=_school_logo_path(); drawn=False
    if logo:
        try: c.drawImage(ImageReader(logo),8*mm,H-42*mm,width=88*mm,height=36*mm,preserveAspectRatio=True,mask='auto'); drawn=True
        except Exception: pass
    if not drawn:
        c.setFillColorRGB(0.02,0.28,0.55); c.setFont(bold,14)
        for i,line in enumerate(_wrap_text(school_name(),26,3)): c.drawString(9*mm,H-19*mm-i*7*mm,line)

    # The school's own address and contact details, as it entered them; a block it left blank stays blank.
    brand=school_brand(); y=H-10*mm
    c.setFillColorRGB(0.12,0.12,0.12); c.setFont(regular,7.2)
    for line in _wrap_text(brand.get('address'),36,3): c.drawString(140*mm,y,line); y-=5*mm
    if brand.get('phone'): c.setFont(bold,7.2); c.drawString(140*mm,y,f"Tel: {brand['phone']}"[:44]); y-=5*mm
    if brand.get('email'): c.setFont(regular,7.2); c.drawString(140*mm,y,str(brand['email'])[:44])

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
        ('Being payment for:',row['purpose']),
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
    c.drawString(left,band_h*0.32,f'For: {school_name().upper()}')

    if row.get('status')=='voided':
        # A voided payment's receipt must never pass for a valid one, on paper, by email or in the parent portal.
        c.saveState(); c.translate(W/2,H/2-5*mm); c.rotate(18)
        c.setStrokeColorRGB(0.69,0.09,0.09); c.setFillColorRGB(0.69,0.09,0.09); c.setLineWidth(1*mm)
        c.rect(-42*mm,-8*mm,84*mm,20*mm,fill=0,stroke=1); c.setFont(bold,40); c.drawCentredString(0,-3*mm,'VOIDED'); c.restoreState()

    c.showPage(); c.save(); buf.seek(0); return buf.getvalue(),row


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
    msg=EmailMessage(); msg['Subject']=f'{school} Payment Receipt {row["receipt_no"]}'; msg['From']=settings.sender; msg['To']=recipient; msg.set_content(f'Dear Parent/Guardian,\n\nPlease find attached the official payment receipt {row["receipt_no"]} for {_student_display(row)}.\n\nAmount paid: {_format_money(row["amount"])}\nPurpose: {row["category"]}\n\n{school}'); msg.add_attachment(pdf,maintype='application',subtype='pdf',filename=f'{row["receipt_no"]}.pdf')
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

def _log_receipt_delivery(payment_id, channel, recipient, ok, msg, actor_id):
    """Record every delivery attempt, successful or not."""
    db.session.add(FinanceDeliveryLog(
        payment_id=payment_id,channel=channel,recipient=recipient,
        status='sent' if ok else 'failed',
        provider_reference=msg if ok else None,
        error_message=None if ok else msg,
        sent_by=actor_id,sent_at=datetime.now(timezone.utc).isoformat()))
    db.session.commit()

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
