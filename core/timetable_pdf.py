"""Draws an exam/test timetable as a PDF: the school's own header, then one table of entries.

Self-contained like ``report_card_pdf`` and ``receipt_pdf``: it knows nothing about Flask or the
database. Everything it draws comes in through its arguments.

    pdf_bytes = render_timetable_pdf(term, session_name, rows, school=None)

``rows`` is what ``blueprints/school/timetable_data.py``'s ``entry_rows`` returns: a list of dicts
with ``class_name``, ``subject_name``, ``exam_type``, ``date``, ``start_time``, ``end_time``,
``venue`` and ``released``. ``school`` is ``None``, or a dict with ``name``, ``logo_path``, ``primary``
(the same shape ``core/branding.py``'s ``school_brand()`` gives).
"""
import io
import os
import re

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import BaseDocTemplate, Frame, HRFlowable, PageTemplate, Paragraph, Spacer, Table, TableStyle

PAGE_W, PAGE_H = landscape(A4)
MARGIN = 14 * mm
CONTENT_W = PAGE_W - 2 * MARGIN
NEUTRAL_PRIMARY = '#0d2b52'
_FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'fonts')
_FONTS = {}


def _fonts():
    if _FONTS:
        return _FONTS['regular'], _FONTS['bold']
    regular, bold = 'Helvetica', 'Helvetica-Bold'
    try:
        pdfmetrics.registerFont(TTFont('TTBody', os.path.join(_FONT_DIR, 'DejaVuSans.ttf')))
        pdfmetrics.registerFont(TTFont('TTBold', os.path.join(_FONT_DIR, 'DejaVuSans-Bold.ttf')))
        regular, bold = 'TTBody', 'TTBold'
    except Exception:
        pass
    _FONTS.update(regular=regular, bold=bold)
    return regular, bold


def _colour(value, default):
    text = str(value or '').strip().lstrip('#')
    if re.fullmatch(r'[0-9a-fA-F]{3}', text):
        text = ''.join(ch * 2 for ch in text)
    if not re.fullmatch(r'[0-9a-fA-F]{6}', text):
        return colors.HexColor(default)
    return colors.HexColor('#' + text)


def _clean(value, limit=None):
    text = '' if value is None else str(value)
    text = ' '.join(text.replace('\r', ' ').replace('\n', ' ').split())
    if limit and len(text) > limit:
        text = text[:limit - 1].rstrip() + '…'
    return text


def render_timetable_pdf(term, session_name, rows, school=None):
    """Render one timetable as a landscape A4 PDF. An empty ``rows`` still gives a valid, one-page PDF."""
    school = school if isinstance(school, dict) else {}
    regular, bold = _fonts()
    primary = _colour(school.get('primary'), NEUTRAL_PRIMARY)
    ink = colors.HexColor('#1a1a1a')
    line = colors.HexColor('#cfd8e3')
    tint = colors.HexColor('#eef3f9')

    styles = {
        'title': ParagraphStyle('title', fontName=bold, fontSize=17, leading=20, textColor=primary),
        'sub': ParagraphStyle('sub', fontName=regular, fontSize=10.5, leading=14, textColor=colors.HexColor('#4d5b69')),
        'th': ParagraphStyle('th', fontName=bold, fontSize=9, leading=11, textColor=colors.white, alignment=TA_LEFT),
        'td': ParagraphStyle('td', fontName=regular, fontSize=9, leading=12, textColor=ink),
        'empty': ParagraphStyle('empty', fontName=regular, fontSize=11, leading=15, textColor=colors.HexColor('#4d5b69'),
                                alignment=TA_CENTER),
    }

    name = _clean(school.get('name'), 120) or 'Exam Timetable'
    title = f"{term} Timetable" if term != 'Full Session' else 'Annual Timetable'
    story = [Paragraph(name, styles['title']), Paragraph(f'{title} — {_clean(session_name, 60)} session', styles['sub']),
            Spacer(1, 3 * mm), HRFlowable(width='100%', thickness=1.4, color=primary, spaceAfter=4 * mm)]

    if rows:
        header = ['Date', 'Time', 'Class', 'Subject', 'Type', 'Venue']
        data = [[Paragraph(h, styles['th']) for h in header]]
        for r in rows:
            time_range = r.get('start_time') or ''
            if r.get('end_time'):
                time_range += f" – {r['end_time']}"
            kind = 'Test' if r.get('exam_type') == 'test' else 'Examination'
            status = '' if r.get('released') else '  (draft)'
            data.append([Paragraph(_clean(r.get('date')), styles['td']), Paragraph(_clean(time_range), styles['td']),
                        Paragraph(_clean(r.get('class_name')), styles['td']), Paragraph(_clean(r.get('subject_name')), styles['td']),
                        Paragraph(_clean(kind) + status, styles['td']), Paragraph(_clean(r.get('venue')) or '—', styles['td'])])
        widths = [0.13, 0.14, 0.15, 0.24, 0.16, 0.18]
        table = Table(data, colWidths=[CONTENT_W * w for w in widths], repeatRows=1)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), primary),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, tint]),
            ('GRID', (0, 0), (-1, -1), 0.5, line),
            ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('LEFTPADDING', (0, 0), (-1, -1), 6), ('RIGHTPADDING', (0, 0), (-1, -1), 6),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        story.append(table)
    else:
        story.append(Spacer(1, 20 * mm))
        story.append(Paragraph('No timetable entry has been recorded for this session and term.', styles['empty']))

    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=(PAGE_W, PAGE_H), leftMargin=MARGIN, rightMargin=MARGIN,
                         topMargin=MARGIN, bottomMargin=MARGIN, title=f'{name} — {title}')
    frame = Frame(MARGIN, MARGIN, CONTENT_W, PAGE_H - 2 * MARGIN, id='main')
    doc.addPageTemplates([PageTemplate(id='main', frames=[frame])])
    doc.build(story)
    return buf.getvalue()
