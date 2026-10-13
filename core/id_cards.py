"""ID cards: what a card design may contain, the school's default designs, the details each card shows, and the
QR code on it.

A design is ``{"orientation": "landscape"|"portrait", "background": colour, "sides": {"front": [...], "back": [...]}}``
where every element is one of ELEMENT_TYPES at a position and size in millimetres on a standard bank-card (CR80,
85.6 x 54 mm). Nothing from the browser is trusted: clean_layout() rebuilds the design from known keys only, clamps
every number, accepts colours only as #rrggbb (or the school's own 'brand'/'accent'), fields only from FIELDS, and
caps the number of elements and the length of any text. A design is printed as plain text and positioned boxes
(templates/includes/_id_card.html), so nothing in it can run.

A card's QR code holds only the address of its verification page (/id/<token>), never a person's details. The token
is random (secrets.token_urlsafe), so cards cannot be found by guessing.
"""

import json
import re
import secrets
from datetime import datetime, timezone

from reportlab.graphics.barcode import qrencoder

KINDS = ('student', 'staff')
CARD_MM = {'landscape': (85.6, 54.0), 'portrait': (54.0, 85.6)}
ELEMENT_TYPES = ('text', 'field', 'photo', 'logo', 'qr', 'rect', 'signature')
SIDES = ('front', 'back')
MAX_ELEMENTS = 30
MAX_TEXT = 120
COLOUR = re.compile(r'^#[0-9a-fA-F]{6}$')
NAMED_COLOURS = ('brand', 'accent', 'white', 'ink')

# The details a card can show, by kind. The label is what the designer offers; the value comes from card_data().
FIELDS = {
    'student': {'name': 'Full name', 'surname': 'Surname', 'first_name': 'First name', 'admission_no': 'Admission number',
                'class': 'Class', 'department': 'Department (SSS)', 'session': 'Session', 'gender': 'Gender',
                'blood_group': 'Blood group', 'guardian_phone': "Parent's phone"},
    'staff': {'name': 'Full name', 'role': 'Role', 'phone': 'Phone', 'email': 'Email'},
}
SCHOOL_FIELDS = {'school_name': 'School name', 'school_motto': 'Motto', 'school_address': 'Address',
                 'school_phone': 'School phone', 'school_email': 'School email', 'serial': 'Card number',
                 'issued': 'Date issued'}


def fields_for(kind):
    return {**FIELDS.get(kind, {}), **SCHOOL_FIELDS}


def _now():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- checking a design
def _number(value, low, high, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:   # NaN
        return default
    return round(min(high, max(low, number)), 1)


def _colour(value, default):
    value = str(value or '').strip()
    if value in NAMED_COLOURS or COLOUR.match(value):
        return value
    return default


def _clean_element(raw, kind, width, height):
    if not isinstance(raw, dict) or raw.get('type') not in ELEMENT_TYPES:
        return None
    t = raw['type']
    el = {'type': t,
          'x': _number(raw.get('x'), 0, width - 1, 2), 'y': _number(raw.get('y'), 0, height - 1, 2),
          'w': _number(raw.get('w'), 1, width, 20), 'h': _number(raw.get('h'), 1, height, 8)}
    el['w'] = min(el['w'], round(width - el['x'], 1))
    el['h'] = min(el['h'], round(height - el['y'], 1))
    if t in ('text', 'field'):
        el.update(size=_number(raw.get('size'), 4, 28, 8), bold=bool(raw.get('bold')), upper=bool(raw.get('upper')),
                  align=raw.get('align') if raw.get('align') in ('left', 'center', 'right') else 'left',
                  color=_colour(raw.get('color'), 'ink'))
        if t == 'text':
            el['text'] = ' '.join(str(raw.get('text') or '').split())[:MAX_TEXT] or 'Text'
        else:
            field = raw.get('field')
            if field not in fields_for(kind):
                return None
            el['field'] = field
            el['label'] = ' '.join(str(raw.get('label') or '').split())[:30]
    if t == 'rect':
        el.update(fill=_colour(raw.get('fill'), 'brand'), radius=_number(raw.get('radius'), 0, 10, 0))
    if t in ('photo', 'logo', 'signature', 'qr'):
        el['radius'] = _number(raw.get('radius'), 0, 10, 0)
    if t == 'photo':
        el['border'] = _colour(raw.get('border'), 'white')
    return el


def clean_layout(raw, kind):
    """A design from the designer (a dict or JSON text), rebuilt from known keys only. Raises ValueError for one that
    is not a design at all."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise ValueError('The design could not be read.')
    if not isinstance(raw, dict):
        raise ValueError('The design could not be read.')
    orientation = raw.get('orientation') if raw.get('orientation') in CARD_MM else 'landscape'
    width, height = CARD_MM[orientation]
    sides = raw.get('sides') if isinstance(raw.get('sides'), dict) else {}
    clean = {'orientation': orientation, 'background': _colour(raw.get('background'), 'white'), 'sides': {}}
    for side in SIDES:
        items = sides.get(side) if isinstance(sides.get(side), list) else []
        if len(items) > MAX_ELEMENTS:
            raise ValueError(f'A side of the card can hold at most {MAX_ELEMENTS} items.')
        clean['sides'][side] = [el for el in (_clean_element(item, kind, width, height) for item in items) if el]
    return clean


# ---------------------------------------------------------------- the school's starting designs
def default_layout(kind):
    """A tidy design to start from: a band in the school's colour with the logo and name, the photo, the details, and
    the QR code; the back says what to do with a found card."""
    who = [{'type': 'field', 'field': 'name', 'x': 29, 'y': 17, 'w': 54, 'h': 6, 'size': 10, 'bold': True, 'upper': True, 'color': 'ink'}]
    if kind == 'student':
        who += [
            {'type': 'field', 'field': 'admission_no', 'label': 'Adm. no', 'x': 29, 'y': 24, 'w': 34, 'h': 4.5, 'size': 7, 'color': 'ink'},
            {'type': 'field', 'field': 'class', 'label': 'Class', 'x': 29, 'y': 29, 'w': 34, 'h': 4.5, 'size': 7, 'color': 'ink'},
            {'type': 'field', 'field': 'session', 'label': 'Session', 'x': 29, 'y': 34, 'w': 34, 'h': 4.5, 'size': 7, 'color': 'ink'},
        ]
        title = 'STUDENT IDENTITY CARD'
    else:
        who += [
            {'type': 'field', 'field': 'role', 'label': 'Role', 'x': 29, 'y': 24, 'w': 34, 'h': 4.5, 'size': 7, 'color': 'ink'},
            {'type': 'field', 'field': 'phone', 'label': 'Phone', 'x': 29, 'y': 29, 'w': 34, 'h': 4.5, 'size': 7, 'color': 'ink'},
        ]
        title = 'STAFF IDENTITY CARD'
    front = [
        {'type': 'rect', 'x': 0, 'y': 0, 'w': 85.6, 'h': 13, 'fill': 'brand'},
        {'type': 'logo', 'x': 3, 'y': 2, 'w': 9, 'h': 9, 'radius': 2},
        {'type': 'field', 'field': 'school_name', 'x': 14, 'y': 2.5, 'w': 68, 'h': 5, 'size': 9, 'bold': True, 'upper': True, 'color': 'white'},
        {'type': 'text', 'text': title, 'x': 14, 'y': 7.5, 'w': 68, 'h': 4, 'size': 6, 'color': 'white'},
        {'type': 'photo', 'x': 4, 'y': 16, 'w': 22, 'h': 27, 'radius': 1.5, 'border': 'brand'},
        *who,
        {'type': 'qr', 'x': 66, 'y': 24, 'w': 16, 'h': 16},
        {'type': 'rect', 'x': 0, 'y': 50, 'w': 85.6, 'h': 4, 'fill': 'accent'},
        {'type': 'field', 'field': 'serial', 'x': 4, 'y': 45, 'w': 40, 'h': 4, 'size': 6, 'color': 'ink'},
    ]
    back = [
        {'type': 'rect', 'x': 0, 'y': 0, 'w': 85.6, 'h': 8, 'fill': 'brand'},
        {'type': 'field', 'field': 'school_name', 'x': 4, 'y': 1.5, 'w': 78, 'h': 5, 'size': 8, 'bold': True, 'upper': True, 'align': 'center', 'color': 'white'},
        {'type': 'text', 'text': 'This card is the property of the school. If found, please return it to the school.',
         'x': 6, 'y': 12, 'w': 74, 'h': 9, 'size': 7, 'align': 'center', 'color': 'ink'},
        {'type': 'field', 'field': 'school_address', 'x': 6, 'y': 22, 'w': 74, 'h': 8, 'size': 6.5, 'align': 'center', 'color': 'ink'},
        {'type': 'field', 'field': 'school_phone', 'label': 'Tel', 'x': 6, 'y': 30, 'w': 74, 'h': 4, 'size': 6.5, 'align': 'center', 'color': 'ink'},
        {'type': 'signature', 'x': 30, 'y': 36, 'w': 26, 'h': 9},
        {'type': 'text', 'text': 'Head of school', 'x': 20, 'y': 45.5, 'w': 46, 'h': 4, 'size': 6, 'align': 'center', 'color': 'ink'},
        {'type': 'rect', 'x': 0, 'y': 50, 'w': 85.6, 'h': 4, 'fill': 'accent'},
    ]
    return clean_layout({'orientation': 'landscape', 'background': 'white', 'sides': {'front': front, 'back': back}}, kind)


# ---------------------------------------------------------------- cards
def new_token():
    return secrets.token_urlsafe(16)


def serial_for(kind, card_id):
    return f"{'STU' if kind == 'student' else 'STF'}-{int(card_id):06d}"


def card_data(kind, person, brand, serial='', issued=''):
    """``{field: text}`` for one person's card (person: a dict of the student's or staff member's details)."""
    data = {'school_name': brand.get('name') or '', 'school_motto': brand.get('motto') or '',
            'school_address': brand.get('address') or '', 'school_phone': brand.get('phone') or '',
            'school_email': brand.get('email') or '', 'serial': serial, 'issued': (issued or '')[:10]}
    if kind == 'student':
        name = ' '.join(p for p in (person.get('first_name'), person.get('middle_name'), person.get('last_name')) if p)
        data.update(name=name, surname=person.get('last_name') or '', first_name=person.get('first_name') or '',
                    admission_no=person.get('admission_no') or '', **{'class': person.get('class_name') or ''},
                    department=person.get('department') or '', session=person.get('session_name') or '',
                    gender=person.get('gender') or '', blood_group=person.get('blood_group') or '',
                    guardian_phone=person.get('guardian_phone') or '')
    else:
        data.update(name=person.get('display_name') or '', role=person.get('role') or '', phone=person.get('phone') or '',
                    email=person.get('email') or '')
    return data


def qr_path(text):
    """An SVG path (and its size in modules) drawing the QR code for ``text``: only 'M', 'h', 'v', 'z' and numbers, so it
    is safe to place in an attribute as it is. The smallest version that holds the text, error correction M."""
    for version in range(1, 21):
        code = qrencoder.QRCode(version, qrencoder.QRErrorCorrectLevel.M)
        code.addData(text)
        try:
            code.make()
        except Exception:
            continue
        n = code.getModuleCount()
        parts = []
        for r in range(n):
            for c in range(n):
                if code.isDark(r, c):
                    parts.append(f'M{c + 4} {r + 4}h1v1h-1z')
        return ''.join(parts), n + 8
    raise ValueError('That text is too long for a QR code.')
