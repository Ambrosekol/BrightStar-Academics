"""The receipt studio's rules: what a school's own receipt design may contain, its starting templates, the details a
receipt can show, and a sample receipt to preview a design with.

A design is the same kind of thing as a report card design (core/report_card_design.py), on a receipt-sized page::

    {"v": 1, "page": "A5"|"A4", "orientation": "landscape"|"portrait",
     "background": {"color", "gradient", "image", "fit", "opacity"}, "sides": {"page1": [element, ...]}}

Its elements are the studio's own (text, details, shapes, the logo, the school's images, the authorised signature, a
barcode of the receipt number) and three blocks only a receipt has: the amount paid (in figures and in words), the
balance (fees for the session, paid so far, balance due) and a PAID stamp. A receipt that has been voided is always
marked VOID across the page by the drawing code, whatever the design: that is never left to a design.

Nothing from the browser is trusted: clean_layout() rebuilds a design from known keys only, through the same checks
as the ID card and report card studios. Drawn by core/receipt_design_pdf.py (the PDF) and static/receipt-render.js
(the studio and the receipt page), from the same values.
"""

import json

from core.id_cards import _choice, _clean_element, _colour, _gradient, _number, _text
from core.report_card_design import FONTS, _block_style, _f

PAGES = {('A5', 'landscape'): (210.0, 148.0), ('A5', 'portrait'): (148.0, 210.0),
         ('A4', 'portrait'): (210.0, 297.0), ('A4', 'landscape'): (297.0, 210.0)}
PAGE_CHOICES = [('A5', 'landscape', 'A5 landscape (210 × 148 mm)'), ('A5', 'portrait', 'A5 portrait (148 × 210 mm)'),
                ('A4', 'portrait', 'A4 portrait (210 × 297 mm)'), ('A4', 'landscape', 'A4 landscape (297 × 210 mm)')]
SIDES = ('page1',)
MAX_ELEMENTS = 80
BLOCKS = ('amount', 'summary', 'stamp')
TYPES = ('text', 'field', 'shape', 'logo', 'image', 'signature', 'barcode') + BLOCKS
WHO = ('authorised',)
FIELDS = {
    'title': 'Title (Official Receipt)', 'receipt_no': 'Receipt number', 'date': 'Date paid', 'payer': 'Received from',
    'student_name': "Student's name", 'admission_no': 'Admission number', 'class': 'Class', 'session': 'Session',
    'student_line': 'Student (name, number and class)', 'purpose': 'Paid for', 'category': 'Fee type', 'method': 'Paid by',
    'reference': 'Payment reference', 'amount': 'Amount paid', 'amount_words': 'Amount in words', 'notes': "Bursar's note",
    'received_by': 'Received by', 'charged': 'Fees for the session', 'paid_to_date': 'Paid so far', 'balance': 'Balance due',
    'status': 'Status (Paid / Voided)',
    'school_name': 'School name', 'school_motto': 'Motto', 'school_address': 'Address', 'school_phone': 'School phone',
    'school_email': 'School email',
}
BARCODE_FIELDS = {'receipt_no': 'Receipt number'}
SUMMARY_ITEMS = {'charged': 'Fees for the session', 'paid': 'Paid so far', 'balance': 'Balance due'}


def spec():
    return {'types': TYPES, 'fields': FIELDS, 'barcode_fields': BARCODE_FIELDS, 'fonts': FONTS, 'default_font': 'dejavu',
            'who': WHO}


def page_size(layout):
    return PAGES.get((layout.get('page'), layout.get('orientation')), PAGES[('A5', 'landscape')])


def _clean_block(raw, base):
    el = dict(base, **_block_style(raw))
    t = el['type']
    if t == 'amount':
        el['label'] = _text(raw.get('label'), 40, 1)
        el['words'] = bool(raw.get('words', True))
        el['value_size'] = _number(raw.get('value_size'), 8, 40, 20)
    elif t == 'summary':
        asked = raw.get('items') if isinstance(raw.get('items'), list) else list(SUMMARY_ITEMS)
        el['items'] = [c for c in SUMMARY_ITEMS if c in asked] or ['balance']
        el['layout'] = _choice(raw.get('layout'), ('row', 'column'), 'row')
        el['value_size'] = _number(raw.get('value_size'), 6, 24, 11)
    elif t == 'stamp':
        el['text'] = _text(raw.get('text'), 16, 1) or 'PAID'
        el['shape'] = _choice(raw.get('shape'), ('round', 'box'), 'round')
        el['show_date'] = bool(raw.get('show_date', True))
    return el


def clean_layout(raw, assets=None):
    """A receipt design from the studio (a dict or JSON text), rebuilt from known keys only. ``assets`` is the set of the
    school's own uploaded image ids. Raises ValueError for something that is not a design at all, or too many items."""
    if isinstance(raw, str):
        if len(raw) > 400_000:
            raise ValueError('The design is too large.')
        try:
            raw = json.loads(raw)
        except ValueError:
            raise ValueError('The design could not be read.')
    if not isinstance(raw, dict):
        raise ValueError('The design could not be read.')
    assets = set(assets or ())
    page = raw.get('page') if raw.get('page') in ('A5', 'A4') else 'A5'
    orientation = raw.get('orientation') if raw.get('orientation') in ('landscape', 'portrait') else 'landscape'
    width, height = PAGES[(page, orientation)]
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
    clean = {'v': 1, 'page': page, 'orientation': orientation, 'background': background, 'sides': {}}
    for side in SIDES:
        items = sides.get(side) if isinstance(sides.get(side), list) else []
        if len(items) > MAX_ELEMENTS:
            raise ValueError(f'A receipt can hold at most {MAX_ELEMENTS} items.')
        out, seen = [], set()
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            if item.get('type') in BLOCKS:
                base = _clean_element(dict(item, type='shape'), 'receipt', width, height, assets, i, spec())
                if base is None:
                    continue
                for key in ('shape', 'fill', 'gradient', 'stroke', 'stroke_w', 'dash', 'radius'):
                    base.pop(key, None)
                base['type'] = item['type']
                el = _clean_block(item, base)
            else:
                el = _clean_element(item, 'receipt', width, height, assets, i, spec())
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


# ---------------------------------------------------------------- what a receipt shows
def _money(value):
    try:
        return f'₦{float(value):,.2f}'
    except (TypeError, ValueError):
        return ''


def view_data(sheet):
    """What static/receipt-render.js and core/receipt_design_pdf.py draw one receipt from: ``{'data': {field: text},
    'report': {'amount', 'words', 'summary', 'date', 'voided'}, 'barcodes'}``. ``sheet`` is
    blueprints/finance/helpers._receipt_sheet()."""
    from core.id_cards import barcode_path
    school = sheet.get('school') or {}
    summary = sheet.get('summary') or {}
    data = {
        'title': 'Official Receipt', 'receipt_no': sheet.get('number') or '', 'date': sheet.get('date') or '',
        'payer': sheet.get('payer') or '', 'student_name': sheet.get('student_name') or '',
        'admission_no': sheet.get('admission_no') or '', 'class': sheet.get('class_name') or '',
        'session': sheet.get('session_name') or '', 'student_line': sheet.get('student') or '',
        'purpose': sheet.get('purpose') or '', 'category': sheet.get('category') or '',
        'method': sheet.get('method_only') or '', 'reference': sheet.get('reference') or '',
        'amount': _money(sheet.get('amount')), 'amount_words': sheet.get('amount_words') or '',
        'notes': sheet.get('notes') or '', 'received_by': sheet.get('received_by') or '',
        'charged': _money(summary['charged']) if summary else '', 'paid_to_date': _money(summary['paid']) if summary else '',
        'balance': _money(summary['balance']) if summary else '',
        'status': 'Voided' if sheet.get('voided') else 'Paid',
        'school_name': school.get('name') or '', 'school_motto': school.get('motto') or '',
        'school_address': school.get('address') or '', 'school_phone': school.get('phone') or '',
        'school_email': school.get('email') or '',
    }
    report = {'amount': data['amount'], 'words': data['amount_words'], 'date': data['date'], 'voided': bool(sheet.get('voided')),
              'summary': {'charged': data['charged'], 'paid': data['paid_to_date'], 'balance': data['balance']}}
    code = barcode_path(data['receipt_no'])
    return {'data': data, 'report': report, 'barcodes': {'receipt_no': code} if code else {}}


def sample_sheet(school, primary, accent):
    """A made-up receipt to preview a design with."""
    return {'school': school, 'primary': primary, 'accent': accent, 'number': 'RCT/2026/00042', 'date': '14/10/2026',
            'payer': 'Mrs Ngozi Okafor', 'student_name': 'Adaeze Okafor', 'admission_no': 'BSA/2024/0117', 'class_name': 'JSS 2A',
            'session_name': '2026/2027', 'student': 'Adaeze Okafor · BSA/2024/0117 · JSS 2A',
            'purpose': 'School Fees — 2026/2027 session', 'category': 'School Fees', 'method_only': 'Bank transfer',
            'reference': 'TRF-88213045', 'method': 'Bank transfer · Ref TRF-88213045', 'amount': 85000.0,
            'amount_words': 'Eighty-five thousand naira only', 'notes': 'First instalment. Thank you.',
            'received_by': 'Mr Tunde Bakare', 'summary': {'charged': 150000.0, 'paid': 85000.0, 'balance': 65000.0},
            'voided': False}


# ---------------------------------------------------------------- starting templates (A5 landscape, 210 x 148 mm)
TEMPLATES = {'classic': 'Classic: the school band across the top, the details in rows and a boxed amount',
             'modern': 'Modern: a coloured side panel with the logo and the amount, details beside it',
             'elegant': 'Elegant: serif type, a fine double border and a centred heading',
             'bold': 'Bold: a sweeping gradient header and a large amount'}


def _row(field, label, x, y, w=118, size=8.5, **kw):
    return _f(field, x, y, w, 5.5, size, label=label, **kw)


DETAIL_ROWS = (('payer', 'Received from'), ('student_line', 'Student'), ('purpose', 'Paid for'), ('method', 'Paid by'),
               ('reference', 'Reference'), ('date', 'Date'))


def _details(x, y, w=118, color='ink', step=7, rows=DETAIL_ROWS):
    out = []
    for i, (field, label) in enumerate(rows):
        out.append(_row(field, label, x, y + i * step, w, color=color))
        out.append({'type': 'shape', 'shape': 'line', 'name': f'{label} rule', 'x': x, 'y': y + i * step + 6, 'w': w, 'h': 1,
                    'stroke': '#d5dbe3', 'stroke_w': 0.2})
    return out


def _signature(x, y, color='ink'):
    return [
        {'type': 'signature', 'who': 'authorised', 'x': x + 4, 'y': y, 'w': 44, 'h': 11},
        {'type': 'shape', 'shape': 'line', 'name': 'Signature line', 'x': x, 'y': y + 11, 'w': 52, 'h': 1, 'stroke': color, 'stroke_w': 0.3},
        {'type': 'text', 'text': 'Authorised signature', 'x': x, 'y': y + 12.5, 'w': 52, 'h': 4, 'size': 7, 'align': 'center', 'font': 'dejavu', 'color': color},
        _f('received_by', x, y + 16.5, 52, 4, 7, align='center', color=color, label='Received by'),
    ]


def default_layout(template='classic'):
    """A complete receipt to start from, in one of TEMPLATES."""
    bg = {'color': 'white'}
    if template == 'modern':
        page = [
            {'type': 'shape', 'shape': 'rect', 'name': 'Side panel', 'x': 0, 'y': 0, 'w': 62, 'h': 148, 'fill': 'brand',
             'gradient': {'type': 'linear', 'from': 'brand', 'to': 'accent', 'angle': 160}},
            {'type': 'logo', 'x': 21, 'y': 10, 'w': 20, 'h': 20, 'clip': 'circle', 'border': 'white', 'border_w': 0.8},
            _f('school_name', 5, 33, 52, 12, 10.5, weight=800, color='white', align='center', upper=True, line=105),
            _f('school_address', 5, 46, 52, 10, 6.5, color='white', align='center', opacity=85),
            {'type': 'amount', 'name': 'Amount paid', 'x': 6, 'y': 66, 'w': 50, 'h': 30, 'label': 'Amount paid', 'words': False,
             'value_size': 15, 'fill': 'white', 'radius': 3, 'color': 'ink', 'accent': 'brand', 'line': 'none', 'line_w': 0},
            {'type': 'stamp', 'name': 'Paid stamp', 'x': 13, 'y': 104, 'w': 36, 'h': 36, 'text': 'PAID', 'shape': 'round',
             'accent': 'white', 'color': 'white', 'rotate': -12, 'opacity': 90},
            _f('title', 70, 9, 90, 8, 15, weight=800, color='brand', upper=True),
            _row('receipt_no', 'No.', 160, 10, 44, 8.5, align='right', weight=700, color='brand'),
            {'type': 'shape', 'shape': 'rect', 'name': 'Title rule', 'x': 70, 'y': 19.5, 'w': 134, 'h': 0.8, 'fill': 'accent'},
            *_details(70, 25, 134),
            _f('amount_words', 70, 69, 134, 9, 8.5, italic=True, color='ink', label='The sum of'),
            {'type': 'summary', 'name': 'Balance', 'x': 70, 'y': 82, 'w': 134, 'h': 16, 'items': ['charged', 'paid', 'balance'],
             'layout': 'row', 'value_size': 10, 'line': '#c9d2de', 'line_w': 0.3, 'radius': 2, 'accent': 'brand', 'size': 7},
            *_signature(150, 112),
            _f('notes', 70, 112, 74, 14, 7.5, italic=True, label='Note'),
            {'type': 'barcode', 'field': 'receipt_no', 'x': 70, 'y': 130, 'w': 50, 'h': 11, 'show_text': True},
        ]
    elif template == 'elegant':
        bg = {'color': '#fffdf8'}
        serif = dict(font='times')
        page = [
            {'type': 'shape', 'shape': 'rect', 'name': 'Outer border', 'x': 5, 'y': 5, 'w': 200, 'h': 138, 'fill': 'none', 'stroke': 'brand', 'stroke_w': 0.8},
            {'type': 'shape', 'shape': 'rect', 'name': 'Inner border', 'x': 7, 'y': 7, 'w': 196, 'h': 134, 'fill': 'none', 'stroke': 'brand', 'stroke_w': 0.25},
            {'type': 'logo', 'x': 97, 'y': 10, 'w': 16, 'h': 16},
            _f('school_name', 20, 27, 170, 8, 15, weight=700, color='brand', align='center', upper=True, letter=8, **serif),
            _f('school_address', 30, 35, 150, 5, 7.5, align='center', italic=True, **serif),
            _f('title', 60, 43, 90, 7, 12, align='center', letter=20, upper=True, color='ink', **serif),
            _row('receipt_no', 'No.', 16, 44, 44, 8, **serif),
            _row('date', 'Date', 150, 44, 44, 8, align='right', **serif),
            *[dict(e, font='times') if e['type'] == 'field' else e for e in _details(16, 55, 118, rows=DETAIL_ROWS[:5])],
            {'type': 'amount', 'name': 'Amount paid', 'x': 142, 'y': 56, 'w': 54, 'h': 30, 'label': 'Amount', 'words': False,
             'value_size': 14, 'fill': 'none', 'radius': 0, 'color': 'ink', 'accent': 'brand', 'line': 'brand', 'line_w': 0.3, 'font': 'times'},
            _f('amount_words', 16, 92, 180, 6, 9, italic=True, label='The sum of', **serif),
            {'type': 'summary', 'name': 'Balance', 'x': 16, 'y': 102, 'w': 110, 'h': 14, 'items': ['charged', 'paid', 'balance'],
             'layout': 'row', 'value_size': 9.5, 'line': '#c9b98f', 'line_w': 0.25, 'accent': 'brand', 'size': 7, 'font': 'times'},
            *_signature(145, 112),
            {'type': 'stamp', 'name': 'Paid stamp', 'x': 112, 'y': 112, 'w': 26, 'h': 26, 'text': 'PAID', 'shape': 'round', 'accent': 'brand',
             'color': 'brand', 'rotate': -10, 'opacity': 75},
        ]
    elif template == 'bold':
        page = [
            {'type': 'shape', 'shape': 'corner', 'name': 'Header sweep', 'x': 0, 'y': 0, 'w': 240, 'h': 42, 'fill': 'brand',
             'gradient': {'type': 'linear', 'from': 'brand', 'to': 'accent', 'angle': 120}},
            {'type': 'logo', 'x': 9, 'y': 6, 'w': 18, 'h': 18, 'clip': 'circle', 'border': 'white', 'border_w': 0.8},
            _f('school_name', 31, 7, 120, 9, 13, weight=800, color='white', upper=True),
            _f('school_motto', 31, 16, 120, 5, 7.5, color='white', italic=True),
            _f('title', 140, 26, 64, 8, 13, weight=800, color='brand', align='right', upper=True),
            _row('receipt_no', 'No.', 140, 34, 64, 8.5, align='right', color='brand', weight=700),
            *_details(10, 46, 120),
            {'type': 'amount', 'name': 'Amount paid', 'x': 138, 'y': 46, 'w': 62, 'h': 42, 'label': 'Amount paid', 'words': True,
             'value_size': 20, 'fill': 'brand', 'radius': 4, 'color': 'white', 'accent': 'white', 'line': 'none', 'line_w': 0, 'shadow': 'soft'},
            {'type': 'summary', 'name': 'Balance', 'x': 10, 'y': 92, 'w': 120, 'h': 16, 'items': ['charged', 'paid', 'balance'],
             'layout': 'row', 'value_size': 10.5, 'fill': '#f1f5fa', 'radius': 3, 'line': 'none', 'line_w': 0, 'accent': 'brand', 'size': 7},
            {'type': 'stamp', 'name': 'Paid stamp', 'x': 150, 'y': 94, 'w': 34, 'h': 20, 'text': 'PAID', 'shape': 'box', 'accent': 'accent',
             'color': 'accent', 'rotate': -8, 'opacity': 85},
            *_signature(146, 118),
            {'type': 'barcode', 'field': 'receipt_no', 'x': 10, 'y': 126, 'w': 52, 'h': 12, 'show_text': True},
            _f('school_address', 66, 128, 74, 10, 6.5, color='ink'),
        ]
    else:  # classic
        page = [
            {'type': 'shape', 'shape': 'rect', 'name': 'Header band', 'x': 0, 'y': 0, 'w': 210, 'h': 28, 'fill': 'brand'},
            {'type': 'logo', 'x': 8, 'y': 4, 'w': 20, 'h': 20},
            _f('school_name', 32, 5, 120, 9, 14, weight=800, color='white', upper=True),
            _f('school_address', 32, 14, 120, 5, 7, color='white'),
            _f('school_phone', 32, 19, 120, 5, 7, color='white', label='Tel'),
            _f('title', 150, 5, 52, 7, 11, weight=800, color='white', align='right', upper=True),
            _row('receipt_no', 'No.', 150, 13, 52, 8.5, align='right', color='white', weight=700),
            {'type': 'shape', 'shape': 'rect', 'name': 'Accent rule', 'x': 0, 'y': 28, 'w': 210, 'h': 1.2, 'fill': 'accent'},
            *_details(10, 35, 124),
            {'type': 'amount', 'name': 'Amount paid', 'x': 142, 'y': 35, 'w': 60, 'h': 36, 'label': 'Amount paid', 'words': False,
             'value_size': 16, 'fill': '#eef3f9', 'radius': 3, 'color': 'ink', 'accent': 'brand', 'line': 'brand', 'line_w': 0.4},
            _f('amount_words', 10, 80, 192, 6, 9, italic=True, label='The sum of'),
            {'type': 'summary', 'name': 'Balance', 'x': 10, 'y': 90, 'w': 124, 'h': 15, 'items': ['charged', 'paid', 'balance'],
             'layout': 'row', 'value_size': 10, 'line': '#c9d2de', 'line_w': 0.3, 'radius': 2, 'accent': 'brand', 'size': 7},
            {'type': 'stamp', 'name': 'Paid stamp', 'x': 152, 'y': 76, 'w': 30, 'h': 30, 'text': 'PAID', 'shape': 'round',
             'accent': '#19734a', 'color': '#19734a', 'rotate': -12, 'opacity': 80},
            _f('notes', 10, 110, 124, 10, 7.5, italic=True, label='Note'),
            *_signature(146, 112),
            {'type': 'barcode', 'field': 'receipt_no', 'x': 10, 'y': 126, 'w': 50, 'h': 12, 'show_text': True},
        ]
    return clean_layout({'page': 'A5', 'orientation': 'landscape', 'background': bg, 'sides': {'page1': page}})
