"""Draws a receipt as a PDF from a school's own design (core/receipt_design.py), with the report card studio's drawing
(core/report_card_design_pdf.draw_page) and three blocks of its own: the amount paid, the balance (the report card's
summary block, with a receipt's items) and the PAID stamp.

    pdf_bytes = render_designed_receipt(sheet, layout, assets)

``sheet`` is blueprints/finance/helpers._receipt_sheet(); ``layout`` has been through clean_layout. A voided receipt is
always marked VOID across the whole page, over everything the design draws.
"""
import io

import core.pdf_setup  # noqa: F401  (how every PDF is written)
from reportlab.lib import colors
from reportlab.pdfgen import canvas as rl_canvas

from core import report_card_design_pdf as rd
from core.receipt_design import SIDES, page_size, view_data
from core.report_card_pdf import _Pictures

PT = rd.PT
rd.LABELS.update({'charged': 'Fees for the session', 'paid': 'Paid so far', 'balance': 'Balance due'})


def _amount(c, box, el, rep, paint):
    """The amount paid: a small label, the figure as large as the box allows, and the words under it if wanted."""
    radius = rd._block_frame(c, box, el, paint)
    text, accent = paint(el.get('color'), colors.HexColor('#15171a')), paint(el.get('accent'), colors.HexColor('#0d2b52'))
    w, h, pad = el['w'], el['h'], el.get('pad') or 1.6
    label_h = min(h * 0.24, 6) if el.get('label') else 0
    words_h = min(h * 0.3, 9) if el.get('words') and rep.get('words') else 0
    if label_h:
        rd._cell(c, box, el['label'], 0, pad * 0.6, w, label_h, min(el['size'], label_h * PT * 0.7), text, False, 'center', pad,
                 el['font'], 0.8, shrink=True)
    figure_h = h - label_h - words_h - pad
    size = min(el['value_size'], figure_h * PT * 0.72)
    rd._cell(c, box, rep.get('amount', ''), 0, label_h + pad * 0.5, w, figure_h, size, accent, True, 'center', pad, el['font'],
             shrink=True)
    if words_h:
        inner = rd._Inner(c, box.W, box.H, box.alpha)
        c.saveState(); c.translate(pad * PT, (pad * 0.5) * PT)
        inner.W, inner.H = (w - 2 * pad) * PT, (words_h - pad * 0.5) * PT
        rd._text(inner, {'size': min(el['size'], 8), 'align': 'center', 'valign': 'middle', 'italic': True, 'font': el['font'],
                         'color': el.get('color'), 'line': 112}, rep['words'], '', paint)
        c.restoreState()
    c.restoreState()
    rd._block_outline(c, box, el, paint, radius)


def _stamp(c, box, el, rep, paint):
    """A rubber stamp: PAID (or the school's own word) and the date, in a ring or a box. A voided receipt never shows it."""
    if rep.get('voided'):
        return
    W, H = box.W, box.H
    ink = paint(el.get('accent'), colors.HexColor('#19734a'))
    lw = max(0.8, min(W, H) * 0.035)
    rd._strokec(c, ink); c.setLineWidth(lw)
    if el.get('shape') == 'box':
        c.roundRect(lw, lw, W - 2 * lw, H - 2 * lw, min(W, H) * 0.08, stroke=1, fill=0)
        c.setLineWidth(lw * 0.45)
        c.roundRect(lw * 2.6, lw * 2.6, W - 5.2 * lw, H - 5.2 * lw, min(W, H) * 0.06, stroke=1, fill=0)
    else:
        c.ellipse(lw, lw, W - lw, H - lw, stroke=1, fill=0)
        c.setLineWidth(lw * 0.45)
        c.ellipse(lw * 2.8, lw * 2.8, W - lw * 2.8, H - lw * 2.8, stroke=1, fill=0)
    font = rd._font(el.get('font'), True)[0]
    word = rd._safe(el.get('text') or 'PAID', font)
    size = min(H * 0.34, (W * 0.74) / max(rd._width(word, font, 1), 0.01))
    rd._fillc(c, ink); c.setFont(font, size)
    date = rep.get('date') if el.get('show_date') else ''
    c.drawCentredString(W / 2, H / 2 - size * 0.32 + (size * 0.18 if date else 0), word)
    if date:
        small = min(size * 0.32, H * 0.1)
        c.setFont(rd._font(el.get('font'))[0], small)
        c.drawCentredString(W / 2, H / 2 - size * 0.32 - small * 1.3, rd._safe(date, font))


BLOCKS = {'amount': _amount, 'summary': rd._summary, 'stamp': _stamp}


def _void(c, width_mm, height_mm):
    """VOID across the whole page, over everything: a voided receipt can never pass for a paid one."""
    c.saveState()
    c.setFillColor(colors.HexColor('#b3261e')); c.setFillAlpha(0.18)
    c.translate(width_mm * PT / 2, height_mm * PT / 2); c.rotate(28)
    font = rd._fonts()[1]
    size = min(width_mm, height_mm) * PT * 0.42
    c.setFont(font, size); c.drawCentredString(0, -size * 0.35, 'VOID')
    c.setFillAlpha(0.85); c.setFont(font, size * 0.09)
    c.drawCentredString(0, -size * 0.75, 'This receipt was cancelled')
    c.restoreState()


def render_designed_receipt(sheet, layout, assets=None):
    """PDF bytes of one receipt in the school's own design."""
    assets = assets or {}
    width, height = page_size(layout)
    school = sheet.get('school') or {}
    buffer = io.BytesIO()
    c = rl_canvas.Canvas(buffer, pagesize=(width * PT, height * PT), pageCompression=1)
    c.setTitle(f"{school.get('name') or ''} - Receipt {sheet.get('number') or ''}".strip(' -'))
    c.setAuthor(school.get('name') or '')
    c.setCreator('Beacon Academics')
    paint = rd._Paint({'primary': sheet.get('primary'), 'accent': sheet.get('accent')})
    view = view_data(sheet)

    def picture_of(el):
        if el['type'] == 'logo':
            return sheet.get('logo_path'), False, ''
        return sheet.get('signature_relpath'), False, ''        # the authorised signature

    rd.draw_page(c, (width, height), layout, SIDES[0], view['data'], view['report'], _Pictures(), assets, paint, picture_of, BLOCKS)
    if sheet.get('voided'):
        _void(c, width, height)
    c.showPage()
    c.save()
    return buffer.getvalue()
