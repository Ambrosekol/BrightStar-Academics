"""Draws a payment receipt as a PDF, in the school's own colours.

Self-contained on purpose, like ``report_card_pdf``: it knows nothing about Flask or the database.
Everything on the receipt comes in through one dictionary, so the same design can be drawn for any
school, and looks like that school's own paper because the colours, logo, name, address, phone,
email, signature and the bursar's note are all the school's.

    pdf_bytes = render_receipt_pdf(receipt)

The design is written once in units of ``em`` (1 em = 1% of the sheet's width) and the printable
web page (templates/includes/_receipt_sheet.html, static/receipt.css) uses the very same numbers,
so the page on screen, the printout and the PDF sent to a parent are the same receipt.

``receipt`` keys: ``school`` (name, motto, address, phone, email), ``primary`` and ``accent`` (#rrggbb),
``logo`` and ``signature`` (image bytes or None), ``number``, ``date``, ``payer``, ``student`` (one line),
``purpose``, ``method`` (one line), ``amount`` (float), ``amount_words``, ``notes``, ``received_by``,
``summary`` (None, or ``{'charged', 'paid', 'balance'}``) and ``voided`` (bool).
"""

import io
import os

from reportlab.lib.pagesizes import A5, landscape
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

PAGE_W, PAGE_H = landscape(A5)          # 210 mm x 148 mm
EM = PAGE_W / 100.0                     # one em, in points
INK = '#1b2733'
MUTED = '#66788d'
GREEN = '#19734a'
_FONTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'fonts')


def _mix(colour, amount):
    """Mix ``#rrggbb`` toward white (positive ``amount``) or black (negative)."""
    target = 255 if amount > 0 else 0
    channels = [int(colour[i:i + 2], 16) for i in (1, 3, 5)]
    return '#' + ''.join(f'{round(c + (target - c) * abs(amount)):02x}' for c in channels)


def _fonts():
    """(regular, bold) names. DejaVu carries the naira sign; Helvetica is the fallback."""
    try:
        if 'ReceiptBody' not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont('ReceiptBody', os.path.join(_FONTS, 'DejaVuSans.ttf')))
            pdfmetrics.registerFont(TTFont('ReceiptBold', os.path.join(_FONTS, 'DejaVuSans-Bold.ttf')))
        return 'ReceiptBody', 'ReceiptBold'
    except Exception:
        return 'Helvetica', 'Helvetica-Bold'


def _initials(name):
    words = [w for w in str(name or '').replace('-', ' ').split() if w[:1].isalnum()]
    small = {'of', 'the', 'and', 'for', 'school', 'schools'}
    picked = [w for w in words if w.lower() not in small] or words
    return ''.join(w[0] for w in picked[:2]).upper() or '•'


def _fit(text, font, size, width):
    """``text`` on one line, cut with an ellipsis if it will not fit ``width``."""
    text = ' '.join(str(text or '').split())
    if stringWidth(text, font, size) <= width:
        return text
    while text and stringWidth(text + '…', font, size) > width:
        text = text[:-1]
    return text.rstrip() + '…'


def render_receipt_pdf(receipt):
    regular, bold = _fonts()
    primary, accent = receipt['primary'], receipt['accent']
    tint, soft = _mix(primary, 0.92), _mix(primary, 0.8)
    school = receipt['school']
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))
    c.setTitle(f"Receipt {receipt['number']}")
    c.setAuthor(school['name'])

    def y(em):                         # a distance down from the top edge, in em
        return PAGE_H - em * EM

    def size(em):                      # a font size, in points
        return em * EM

    def fill(hex_colour):
        c.setFillColorRGB(*(int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)))

    def stroke(hex_colour):
        c.setStrokeColorRGB(*(int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)))

    def text(x, baseline_em, value, font, em, colour, align='left'):
        fill(colour)
        c.setFont(font, size(em))
        {'left': c.drawString, 'right': c.drawRightString, 'centre': c.drawCentredString}[align](
            x, y(baseline_em), value)

    M = 3.8 * EM                       # side margin
    right_edge = PAGE_W - M

    # ---- header band: logo, name, motto, contact details
    fill(primary)
    c.rect(0, y(13), PAGE_W, 13 * EM, fill=1, stroke=0)
    fill(accent)
    c.rect(0, y(14), PAGE_W, 1 * EM, fill=1, stroke=0)
    fill('#ffffff')
    c.roundRect(M, y(11.5), 10 * EM, 10 * EM, 1.2 * EM, fill=1, stroke=0)
    drawn = False
    if receipt.get('logo'):
        try:
            c.drawImage(ImageReader(io.BytesIO(receipt['logo'])), M + 0.8 * EM, y(11.5) + 0.8 * EM, width=8.4 * EM,
                        height=8.4 * EM, preserveAspectRatio=True, mask='auto')
            drawn = True
        except Exception:
            pass
    if not drawn:
        text(M + 5 * EM, 7.6, _initials(school['name']), bold, 4.2, primary, 'centre')

    text_x = M + 12.4 * EM
    contact_w = 30 * EM
    text_w = right_edge - contact_w - 2 * EM - text_x
    name_lines = simpleSplit(school['name'], bold, size(3), text_w)[:2]
    baseline = 5.3 if len(name_lines) == 1 else 4.4
    for line in name_lines:
        text(text_x, baseline, line, bold, 3, '#ffffff')
        baseline += 3.4
    if school.get('motto'):
        for line in simpleSplit(school['motto'], regular, size(1.6), text_w)[:2]:
            text(text_x, baseline + 0.2, line, regular, 1.6, _mix(primary, 0.75))
            baseline += 2.1
    contact = simpleSplit(school.get('address') or '', regular, size(1.5), contact_w)[:3]
    if school.get('phone'):
        contact.append(f"Tel: {school['phone']}")
    if school.get('email'):
        contact.append(school['email'])
    line_y = 13 / 2 - (len(contact) * 2.1) / 2 + 1.5
    for line in contact[:5]:
        text(right_edge, line_y, _fit(line, regular, size(1.5), contact_w), regular, 1.5, '#ffffff', 'right')
        line_y += 2.1

    # ---- title row: what it is, its number and date
    text(M, 19.4, 'OFFICIAL RECEIPT', bold, 3.3, primary)
    text(M, 21.6, 'Acknowledgement of payment received', regular, 1.5, MUTED)
    fill(soft)
    c.roundRect(right_edge - 30 * EM, y(21.4), 30 * EM, 6 * EM, 0.8 * EM, fill=1, stroke=0)
    text(right_edge - 1.6 * EM, 18.4, f"No. {receipt['number']}", bold, 1.9, primary, 'right')
    text(right_edge - 1.6 * EM, 20.5, f"Date: {receipt['date']}", regular, 1.7, INK, 'right')

    # ---- the rows, down the left
    left_w = 63 * EM
    rows = [('RECEIVED FROM', receipt['payer']), ('STUDENT', receipt['student']),
            ('BEING PAYMENT FOR', receipt['purpose']), ('PAID BY', receipt['method'])]
    top = 23.4
    stroke(soft)
    c.setLineWidth(0.6)
    for label, value in rows:
        text(M, top + 2.0, label, bold, 1.2, MUTED)
        text(M, top + 4.3, _fit(value or '—', bold, size(1.95), left_w), bold, 1.95, INK)
        c.line(M, y(top + 5.6), M + left_w, y(top + 5.6))
        top += 5.8
    text(M, top + 2.0, 'THE SUM OF', bold, 1.2, MUTED)
    baseline = top + 4.3
    for line in simpleSplit(receipt['amount_words'], regular, size(1.85), left_w)[:2]:
        text(M, baseline, line, regular, 1.85, INK)
        baseline += 2.4
    c.line(M, y(top + 7.4), M + left_w, y(top + 7.4))
    rows_end = top + 7.6

    # ---- the amount, and the account, down the right
    col_x = right_edge - 27 * EM
    fill(primary)
    c.roundRect(col_x, y(23.4 + 11.5), 27 * EM, 11.5 * EM, 1.2 * EM, fill=1, stroke=0)
    text(col_x + 2 * EM, 23.4 + 3.4, 'AMOUNT RECEIVED', bold, 1.25, _mix(primary, 0.75))
    naira = f"₦{receipt['amount']:,.2f}" if regular != 'Helvetica' else f"N{receipt['amount']:,.2f}"
    em_fit = 4.4
    while stringWidth(naira, bold, size(em_fit)) > 23 * EM and em_fit > 2.4:
        em_fit -= 0.2
    text(col_x + 2 * EM, 23.4 + 9, naira, bold, em_fit, '#ffffff')
    if receipt.get('summary'):
        box_top = 23.4 + 11.5 + 1.2
        fill(tint)
        stroke(soft)
        c.roundRect(col_x, y(box_top + 11), 27 * EM, 11 * EM, 1.2 * EM, fill=1, stroke=1)
        text(col_x + 2 * EM, box_top + 2.7, 'ACCOUNT THIS SESSION', bold, 1.2, MUTED)
        summary = receipt['summary']
        lines = (('Fees charged', summary['charged'], INK), ('Paid to date', summary['paid'], INK),
                 ('Balance due', summary['balance'], accent if summary['balance'] > 0.004 else GREEN))
        for i, (label, amount, colour) in enumerate(lines):
            base = box_top + 5.5 + i * 2.3
            heavy = i == 2
            text(col_x + 2 * EM, base, label, bold if heavy else regular, 1.6, INK)
            shown = f"₦{amount:,.2f}" if regular != 'Helvetica' else f"N{amount:,.2f}"
            text(col_x + 25 * EM, base, shown, bold if heavy else regular, 1.6, colour, 'right')

    # ---- the bursar's note
    if receipt.get('notes'):
        note_top = rows_end + 1.0
        note_lines = simpleSplit(receipt['notes'], regular, size(1.5), left_w - 3.4 * EM)[:3]
        fill(tint)
        c.roundRect(M, y(note_top + 8), left_w, 8 * EM, 0.8 * EM, fill=1, stroke=0)
        fill(accent)
        c.rect(M, y(note_top + 8), 0.7 * EM, 8 * EM, fill=1, stroke=0)
        text(M + 2.2 * EM, note_top + 2.2, 'NOTE', bold, 1.15, MUTED)
        for i, line in enumerate(note_lines):
            text(M + 2.2 * EM, note_top + 4.0 + i * 1.7, line, regular, 1.5, INK)

    # ---- signature
    sign_top = 48.6
    if receipt.get('signature'):
        try:
            c.drawImage(ImageReader(io.BytesIO(receipt['signature'])), col_x + 3 * EM, y(sign_top + 6), width=21 * EM,
                        height=6 * EM, preserveAspectRatio=True, anchor='s', mask='auto')
        except Exception:
            pass
    stroke(INK)
    c.setLineWidth(0.7)
    c.line(col_x + 1.5 * EM, y(sign_top + 6.4), right_edge - 1.5 * EM, y(sign_top + 6.4))
    text(col_x + 13.5 * EM, sign_top + 8.4, 'Authorised Signature', regular, 1.3, INK, 'centre')
    if receipt.get('received_by'):
        text(col_x + 13.5 * EM, sign_top + 10.3,
             _fit(f"Received by {receipt['received_by']}", regular, size(1.3), 25 * EM), regular, 1.3, MUTED, 'centre')

    # ---- footer band
    fill(primary)
    c.rect(0, 0, PAGE_W, 6 * EM, fill=1, stroke=0)
    fill(accent)
    c.rect(0, 6 * EM, PAGE_W, 0.5 * EM, fill=1, stroke=0)
    text(M, 68.2, f"For: {school['name'].upper()}", bold, 1.6, '#ffffff')
    text(right_edge, 68.2, 'Thank you for your payment.', regular, 1.5, _mix(primary, 0.75), 'right')

    if receipt.get('voided'):
        # A voided payment's receipt must never pass for a valid one, on paper, by email or in the parent portal.
        c.saveState()
        c.translate(PAGE_W / 2, PAGE_H / 2 - 5 * mm)
        c.rotate(18)
        stroke('#b01818')
        fill('#b01818')
        c.setLineWidth(1 * mm)
        c.rect(-42 * mm, -8 * mm, 84 * mm, 20 * mm, fill=0, stroke=1)
        c.setFont(bold, 40)
        c.drawCentredString(0, -3 * mm, 'VOIDED')
        c.restoreState()

    c.showPage()
    c.save()
    return buf.getvalue()
