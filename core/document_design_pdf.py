"""Draws a testimonial, transcript or leaving certificate as a PDF from the school's design (core/document_design.py),
with the report card studio's drawing (core/report_card_design_pdf.draw_page) and two blocks of its own: the paragraph
(the school's words with the details filled in) and the transcript table (the results table's drawing, one column per
session).

    pdf_bytes = render_document(doc, layout, assets)

``doc`` is a document's details (blueprints/school/documents.py) plus the stored paths of the pictures it prints
(``photo_path``, ``logo_path``, ``signature_path``). A revoked document is marked REVOKED across the whole page, over
everything the design draws.
"""
import io

import core.pdf_setup  # noqa: F401  (how every PDF is written)
from reportlab.lib import colors
from reportlab.pdfgen import canvas as rl_canvas

from core import report_card_design_pdf as rd
from core.document_design import SIDES, fill, page_size, view_data
from core.report_card_pdf import _Pictures

PT = rd.PT


def _paragraph(c, box, el, rep, paint):
    """The school's words, with the document's details put in, wrapped to the box (smaller only if they do not fit)."""
    radius = rd._block_frame(c, box, el, paint)
    c.restoreState()
    text = fill(el.get('text'), rep.get('data') or {})
    style = {'font': el.get('font'), 'size': el.get('size'), 'italic': el.get('italic'), 'align': el.get('align'),
             'valign': el.get('valign'), 'line': el.get('leading'), 'color': el.get('color'), 'pad': el.get('pad')}
    rd._text(box, style, text, '', paint)
    rd._block_outline(c, box, el, paint, radius)


def transcript_columns(el, transcript):
    """``(columns, headings)`` of the transcript table: the subject, each session (headed with its class and session),
    the average and, if wanted, the grade."""
    sessions = transcript.get('sessions') or []
    cols = ['subject'] + [s['key'] for s in sessions] + ['average'] + (['grade'] if el.get('show_grade', True) else [])
    heads = {s['key']: ' '.join(p for p in (s.get('class'), s.get('name')) if p) for s in sessions}
    heads.update(subject='Subject', average='Average', grade='Grade')
    return cols, heads


def _transcript(c, box, el, rep, paint):
    t = rep.get('transcript') or {}
    cols, heads = transcript_columns(el, t)
    rd._results(c, box, dict(el, columns=cols), {'subjects': t.get('rows') or [], 'maxes': {}, 'heads': heads}, paint)


BLOCKS = {'paragraph': _paragraph, 'transcript': _transcript}


def _revoked(c, width_mm, height_mm):
    """REVOKED across the whole page, over everything: a revoked document can never pass for a valid one."""
    c.saveState()
    c.setFillColor(colors.HexColor('#b3261e')); c.setFillAlpha(0.16)
    c.translate(width_mm * PT / 2, height_mm * PT / 2); c.rotate(35)
    font = rd._fonts()[1]
    size = min(width_mm, height_mm) * PT * 0.22
    c.setFont(font, size); c.drawCentredString(0, -size * 0.35, 'REVOKED')
    c.setFillAlpha(0.8); c.setFont(font, size * 0.12)
    c.drawCentredString(0, -size * 0.8, 'This document has been withdrawn by the school')
    c.restoreState()


def render_document(doc, layout, assets=None, revoked=False):
    """PDF bytes of one document in the school's design."""
    assets = assets or {}
    width, height = page_size(layout)
    buffer = io.BytesIO()
    c = rl_canvas.Canvas(buffer, pagesize=(width * PT, height * PT), pageCompression=1)
    c.setTitle(f"{doc.get('school_name') or ''} - {doc.get('document_title') or 'Document'} {doc.get('serial_no') or ''}".strip(' -'))
    c.setAuthor(doc.get('school_name') or '')
    c.setCreator('Brightstars Academics')
    paint = rd._Paint({'primary': doc.get('primary'), 'accent': doc.get('accent')})
    view = view_data(doc)
    report = dict(view['report'], data=view['data'])
    initials = ''.join(p[:1] for p in (doc.get('student_name') or '').split()[:2]).upper()

    def picture_of(el):
        if el['type'] == 'photo':
            return doc.get('photo_path'), True, initials
        if el['type'] == 'logo':
            return doc.get('logo_path'), False, ''
        return doc.get('signature_path'), False, ''        # the head's signature

    rd.draw_page(c, (width, height), layout, SIDES[0], view['data'], report, _Pictures(), assets, paint, picture_of, BLOCKS)
    if revoked:
        _revoked(c, width, height)
    c.showPage()
    c.save()
    return buffer.getvalue()
