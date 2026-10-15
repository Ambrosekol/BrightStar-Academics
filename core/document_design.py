"""The documents studio's rules: what a school's testimonial, transcript and leaving certificate designs may contain,
their starting designs, the details a document can show, and a sample to preview a design with.

A design is the same kind of thing as a report card design (core/report_card_design.py), on an A4 page::

    {"v": 1, "page": "A4", "orientation": "portrait"|"landscape", "background": {...}, "sides": {"page1": [element]}}

Its elements are the report card studio's (text, details, shapes, the photograph, the logo, the school's images, the
head's signature, a barcode of the serial number) and two blocks of its own:

* a **paragraph**: the school's own words with details filled in, "This is to certify that {student_name} was a
  student of this school from {date_admitted} to {date_left}...". Only the details listed in FIELDS are filled in;
  anything else in curly brackets is printed as written, and a value is put in once, never read as a template itself;
* the **transcript**: one row per subject, one column per session the student has results in (headed with the session
  and the class), each the subject's average over that session's terms, then the overall average.

Nothing from the browser is trusted: clean_layout() rebuilds a design from known keys only, through the ID card and
report card studios' own checks. Drawn by core/document_design_pdf.py (the PDF) and static/document-render.js (the
studio), from the same values.
"""

import json
import re

from core.id_cards import _choice, _clean_element, _colour, _gradient, _number, _text, barcode_path
from core.report_card_design import FONTS, PAGE_MM, _block_style, _f

KINDS = {'testimonial': 'Testimonial', 'transcript': 'Transcript', 'leaving': 'Leaving certificate'}
SIDES = ('page1',)
MAX_ELEMENTS = 80
MAX_PARAGRAPH = 1500
BLOCKS = ('paragraph', 'transcript')
TYPES = ('text', 'field', 'shape', 'photo', 'logo', 'image', 'signature', 'barcode') + BLOCKS
WHO = ('head',)
FIELDS = {
    'document_title': 'Document title', 'serial_no': 'Serial number', 'date_issued': 'Date issued',
    'student_name': "Student's name", 'admission_no': 'Admission number', 'gender': 'Gender',
    'pronoun': 'He / She', 'possessive': 'His / Her', 'date_of_birth': 'Date of birth',
    'state_of_origin': 'State of origin', 'religion': 'Religion', 'guardian_name': "Parent/guardian's name",
    'department': 'Department (SSS)', 'first_class': 'Class on joining', 'last_class': 'Last class',
    'sessions_attended': 'Sessions attended (2019/2020 to 2025/2026)', 'date_admitted': 'Date admitted',
    'date_left': 'Date of leaving', 'reason_for_leaving': 'Reason for leaving', 'conduct': 'Conduct',
    'character': 'Character (the head\'s own words)', 'positions_held': 'Positions held', 'final_average': 'Overall average',
    'final_grade': 'Overall grade', 'head_name': "Head's name", 'head_title': "Head's title",
    'school_name': 'School name', 'school_motto': 'Motto', 'school_address': 'Address', 'school_phone': 'School phone',
    'school_email': 'School email',
}
BARCODE_FIELDS = {'serial_no': 'Serial number'}
_TOKEN = re.compile(r'\{([a-z_]+)\}')


def spec():
    return {'types': TYPES, 'fields': FIELDS, 'barcode_fields': BARCODE_FIELDS, 'fonts': FONTS, 'default_font': 'times',
            'who': WHO}


def page_size(layout):
    return PAGE_MM.get(layout.get('orientation'), PAGE_MM['portrait'])


def fill(template, data):
    """A paragraph's words with its details put in, once each. A detail the document has not got is left empty; anything
    in curly brackets that is not a detail is printed as written."""
    return _TOKEN.sub(lambda m: str(data.get(m.group(1)) or '') if m.group(1) in FIELDS else m.group(0), template or '')


def _clean_block(raw, base):
    el = dict(base, **_block_style(raw))
    if el['type'] == 'paragraph':
        text = str(raw.get('text') or '').replace('\r\n', '\n').replace('\r', '\n')
        el['text'] = ''.join(ch for ch in text if ch.isprintable() or ch == '\n')[:MAX_PARAGRAPH] or 'Your words here.'
        el['align'] = _choice(raw.get('align'), ('left', 'center', 'right', 'justify'), 'justify')
        el['valign'] = _choice(raw.get('valign'), ('top', 'middle', 'bottom'), 'top')
        el['leading'] = _number(raw.get('leading'), 100, 250, 150, 0)      # line spacing, in percent of the size
        el['size'] = _number(raw.get('size'), 6, 24, 12)
        el['italic'] = bool(raw.get('italic'))
        for key in ('head_fill', 'head_color', 'zebra'):
            el.pop(key, None)
        el['line'] = _colour(raw.get('line'), 'none', allow_none=True)     # a paragraph has no outline unless given one
    else:   # transcript
        el['numbers'] = _choice(raw.get('numbers'), ('center', 'right'), 'center')
        el['head_size'] = _number(raw.get('head_size'), 5, 16, el['size'])
        el['show_grade'] = bool(raw.get('show_grade', True))
    return el


def clean_layout(raw, assets=None):
    """A document design from the studio (a dict or JSON text), rebuilt from known keys only. Raises ValueError for
    something that is not a design at all, or a page with too many items."""
    if isinstance(raw, str):
        if len(raw) > 600_000:
            raise ValueError('The design is too large.')
        try:
            raw = json.loads(raw)
        except ValueError:
            raise ValueError('The design could not be read.')
    if not isinstance(raw, dict):
        raise ValueError('The design could not be read.')
    assets = set(assets or ())
    orientation = raw.get('orientation') if raw.get('orientation') in PAGE_MM else 'portrait'
    width, height = PAGE_MM[orientation]
    bg = raw.get('background') if isinstance(raw.get('background'), dict) else {}
    image = bg.get('image')
    try:
        image = int(image) if image not in (None, '') else None
    except (TypeError, ValueError):
        image = None
    background = {'color': _colour(bg.get('color'), 'white'), 'gradient': _gradient(bg.get('gradient')),
                  'image': image if image in assets else None, 'fit': _choice(bg.get('fit'), ('cover', 'contain', 'fill'), 'cover'),
                  'opacity': _number(bg.get('opacity'), 0, 100, 100, 0)}
    sides = raw.get('sides') if isinstance(raw.get('sides'), dict) else {}
    clean = {'v': 1, 'page': 'A4', 'orientation': orientation, 'background': background, 'sides': {}}
    for side in SIDES:
        items = sides.get(side) if isinstance(sides.get(side), list) else []
        if len(items) > MAX_ELEMENTS:
            raise ValueError(f'A document can hold at most {MAX_ELEMENTS} items.')
        out, seen = [], set()
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            if item.get('type') in BLOCKS:
                base = _clean_element(dict(item, type='shape'), 'report', width, height, assets, i, spec())
                if base is None:
                    continue
                for key in ('shape', 'fill', 'gradient', 'stroke', 'stroke_w', 'dash', 'radius'):
                    base.pop(key, None)
                base['type'] = item['type']
                el = _clean_block(item, base)
            else:
                el = _clean_element(item, 'report', width, height, assets, i, spec())
            if el is None:
                continue
            if el['id'] in seen:
                el['id'] = f'e{i}'
            seen.add(el['id'])
            out.append(el)
        clean['sides'][side] = out
    return clean


def assets_used(layout):
    used = {layout['background']['image']} if layout.get('background', {}).get('image') else set()
    for side in SIDES:
        used |= {el['asset'] for el in layout['sides'].get(side, []) if el['type'] == 'image'}
    return used


# ---------------------------------------------------------------- what a document shows
def view_data(doc):
    """What static/document-render.js and core/document_design_pdf.py draw one document from: ``{'data': {field: text},
    'report': {'transcript': {...}}, 'barcodes'}``. ``doc`` is a document's details (blueprints/school/documents.py)."""
    data = {k: str(doc.get(k) or '') for k in FIELDS}
    transcript = doc.get('transcript') or {'sessions': [], 'rows': []}
    code = barcode_path(data['serial_no'])
    return {'data': data, 'report': {'transcript': transcript}, 'barcodes': {'serial_no': code} if code else {}}


def sample(kind, school, head):
    """A made-up document to preview a design with."""
    sessions = [{'key': 's0', 'name': '2023/2024', 'class': 'JSS 1'}, {'key': 's1', 'name': '2024/2025', 'class': 'JSS 2'},
                {'key': 's2', 'name': '2025/2026', 'class': 'JSS 3'}]
    names = ['English Language', 'Mathematics', 'Basic Science', 'Basic Technology', 'Social Studies', 'Civic Education',
             'Computer Studies', 'French', 'Yoruba', 'Christian Religious Studies']
    rows = []
    for i, name in enumerate(names):
        marks = [62 + (i * 7) % 25, 58 + (i * 5) % 30, 65 + (i * 3) % 22]
        avg = round(sum(marks) / 3, 1)
        rows.append({'subject': name, 's0': marks[0], 's1': marks[1], 's2': marks[2], 'average': avg,
                     'grade': 'A' if avg >= 70 else 'B' if avg >= 60 else 'C'})
    return {
        'document_title': KINDS[kind].upper() if kind != 'leaving' else 'LEAVING CERTIFICATE', 'serial_no': 'BSA/TST/2026/00042',
        'date_issued': '15 October 2026', 'student_name': 'Adaeze Chukwuemeka Okafor', 'admission_no': 'BSA/2020/0117',
        'gender': 'Female', 'pronoun': 'She', 'possessive': 'Her', 'date_of_birth': '14 March 2008',
        'state_of_origin': 'Anambra', 'religion': 'Christianity', 'guardian_name': 'Mr Emeka Okafor', 'department': 'Science',
        'first_class': 'JSS 1', 'last_class': 'SSS 3', 'sessions_attended': '2020/2021 to 2025/2026',
        'date_admitted': '14 September 2020', 'date_left': '18 July 2026', 'reason_for_leaving': 'Completed her studies',
        'conduct': 'Excellent', 'character': 'She is a diligent, honest and well-mannered student who got on well with others.',
        'positions_held': 'Head Girl; Captain, Debate Club', 'final_average': '71.4%', 'final_grade': 'A',
        'head_name': head.get('name') or 'Mrs A. B. Okoye', 'head_title': head.get('title') or 'Principal',
        'school_name': school.get('name') or '', 'school_motto': school.get('motto') or '',
        'school_address': school.get('address') or '', 'school_phone': school.get('phone') or '',
        'school_email': school.get('email') or '',
        'transcript': {'sessions': sessions, 'rows': rows, 'average': '68.2'},
    }


# ---------------------------------------------------------------- starting designs (A4 portrait, 210 x 297 mm)
WORDS = {
    'testimonial': ('This is to certify that {student_name}, admission number {admission_no}, was a student of this school '
                    'from {date_admitted} to {date_left}, and was last in {last_class}.\n\n'
                    '{pronoun} was of {conduct} conduct. {character}\n\n'
                    'Positions held: {positions_held}.\n\n'
                    'We recommend {student_name} to anyone who may be concerned.'),
    'leaving': ('This is to certify that {student_name}, admission number {admission_no}, born on {date_of_birth}, '
                'was admitted into this school on {date_admitted} and left on {date_left} from {last_class}.\n\n'
                'Reason for leaving: {reason_for_leaving}.\n\nConduct while in school: {conduct}.'),
    'transcript': ('This is the official academic record of {student_name}, admission number {admission_no}, a student of '
                   'this school from {sessions_attended}. Each mark is the average of the terms of that session.'),
}


def _header(color='brand'):
    return [
        {'type': 'logo', 'x': 92, 'y': 12, 'w': 26, 'h': 26},
        _f('school_name', 15, 41, 180, 10, 18, weight=700, align='center', upper=True, color=color, font='times', letter=4),
        _f('school_motto', 25, 51, 160, 6, 9.5, align='center', italic=True, font='times'),
        _f('school_address', 25, 57, 160, 6, 8.5, align='center', font='times'),
        {'type': 'shape', 'shape': 'line', 'name': 'Rule', 'x': 20, 'y': 66, 'w': 170, 'h': 1, 'stroke': color, 'stroke_w': 0.6},
        _f('document_title', 30, 72, 150, 11, 20, weight=700, align='center', upper=True, letter=12, color=color, font='times'),
    ]


def _footer(y=246, color='brand'):
    return [
        {'type': 'signature', 'who': 'head', 'x': 134, 'y': y, 'w': 52, 'h': 14},
        {'type': 'shape', 'shape': 'line', 'name': 'Signature line', 'x': 128, 'y': y + 15, 'w': 64, 'h': 1, 'stroke': 'ink', 'stroke_w': 0.3},
        _f('head_name', 128, y + 17, 64, 5, 9.5, align='center', weight=700, font='times'),
        _f('head_title', 128, y + 22, 64, 5, 8.5, align='center', font='times'),
        _f('serial_no', 18, y + 6, 90, 5, 8.5, label='Serial no.', font='times'),
        _f('date_issued', 18, y + 12, 90, 5, 8.5, label='Date issued', font='times'),
        {'type': 'barcode', 'field': 'serial_no', 'x': 18, 'y': y + 20, 'w': 60, 'h': 12, 'show_text': False},
        {'type': 'shape', 'shape': 'rect', 'name': 'Border', 'x': 8, 'y': 8, 'w': 194, 'h': 281, 'fill': 'none', 'stroke': color, 'stroke_w': 0.8},
        {'type': 'shape', 'shape': 'rect', 'name': 'Inner border', 'x': 10.5, 'y': 10.5, 'w': 189, 'h': 276, 'fill': 'none', 'stroke': color, 'stroke_w': 0.25},
    ]


def default_layout(kind):
    """The starting design of one kind of document."""
    page = _header()
    if kind == 'transcript':
        page += [
            {'type': 'paragraph', 'name': 'Introduction', 'x': 20, 'y': 88, 'w': 170, 'h': 16, 'text': WORDS['transcript'],
             'size': 9.5, 'font': 'times', 'align': 'justify', 'leading': 140},
            _f('student_name', 20, 106, 120, 6, 10, label='Name', weight=700, font='times'),
            _f('date_of_birth', 140, 106, 50, 6, 9, label='Born', font='times'),
            {'type': 'transcript', 'name': 'Transcript', 'x': 20, 'y': 114, 'w': 170, 'h': 116, 'size': 8.5, 'head_size': 8,
             'font': 'dejavu', 'zebra': '#f3f6fa', 'head_fill': 'brand', 'head_color': 'white', 'line': '#c9d2de', 'line_w': 0.25},
            _f('final_average', 20, 233, 80, 6, 10, label='Overall average', weight=700, font='times'),
            _f('final_grade', 105, 233, 60, 6, 10, label='Grade', weight=700, font='times'),
        ]
    else:
        page += [
            {'type': 'paragraph', 'name': 'Wording', 'x': 22, 'y': 96, 'w': 166, 'h': 120, 'text': WORDS[kind], 'size': 12.5,
             'font': 'times', 'align': 'justify', 'leading': 165},
            {'type': 'photo', 'x': 160, 'y': 216, 'w': 28, 'h': 34, 'border': 'brand', 'border_w': 0.4},
        ]
    page += _footer()
    return clean_layout({'page': 'A4', 'orientation': 'portrait', 'background': {'color': '#fffefa'},
                         'sides': {'page1': page}})
