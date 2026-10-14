"""ID cards: what a card design may contain, the school's starting designs, the details each card shows, and the
QR code and barcode on it.

A design (version 2) is::

    {"v": 2, "orientation": "landscape"|"portrait",
     "background": {"color", "gradient", "image", "fit", "opacity"},
     "sides": {"front": [element, ...], "back": [element, ...]}}

on a standard bank card (CR80, 85.6 x 54 mm). Each element has a type (ELEMENT_TYPES), a box in millimetres, a
rotation, an opacity and a shadow, and the settings its type allows: text with a font, weight, spacing and alignment;
shapes with a fill or a gradient and an outline; pictures (the person's photograph, the logo, an image the school
uploaded, a signature) with a fit, a clip shape and a frame; the QR code; a barcode.

Nothing from the browser is trusted. clean_layout() rebuilds a design from known keys only: every number is clamped
to its range, every choice is checked against its list (fonts, shapes, clips, fields), colours are only #rrggbb or
the school's own 'brand'/'accent' (and 'white', 'ink', 'none'), images are only the school's own uploaded ones,
text is cut to length, and a side holds at most MAX_ELEMENTS items. A design is drawn by static/id-card-render.js
from these values with textContent and style properties only, so nothing in it can run.

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
ELEMENT_TYPES = ('text', 'field', 'shape', 'photo', 'logo', 'image', 'signature', 'qr', 'barcode')
SIDES = ('front', 'back')
MAX_ELEMENTS = 60
MAX_TEXT = 300
MAX_LINES = 8
MAX_ASSETS = 60
COLOUR = re.compile(r'^#[0-9a-fA-F]{6}$')
NAMED_COLOURS = ('brand', 'accent', 'white', 'ink')
FONTS = {'sans': 'Arial', 'helvetica': 'Helvetica', 'verdana': 'Verdana', 'tahoma': 'Tahoma', 'trebuchet': 'Trebuchet MS',
         'georgia': 'Georgia', 'times': 'Times New Roman', 'garamond': 'Garamond', 'mono': 'Courier New',
         'impact': 'Impact', 'black': 'Arial Black'}
WEIGHTS = (300, 400, 500, 600, 700, 800, 900)
SHAPES = ('rect', 'ellipse', 'triangle', 'diamond', 'hexagon', 'pentagon', 'star', 'chevron', 'arrow', 'wave',
          'corner', 'line')
CLIPS = ('none', 'circle', 'ellipse', 'rounded', 'hexagon', 'diamond', 'shield', 'star', 'arch')
SHADOWS = ('none', 'soft', 'strong')
DASHES = ('solid', 'dashed', 'dotted')
FITS = ('cover', 'contain', 'fill')
ID = re.compile(r'^[a-z0-9]{1,12}$')

# The details a card can show, by kind. The label is what the designer offers; the value comes from card_data().
# What a card may show about its person. Never the HR file's private notes, never a health history beyond the blood group
# and genotype an emergency needs; and a printed card carries only the details its design shows (blueprints/school/
# id_cards.py, _card_view).
FIELDS = {
    'student': {'name': 'Full name', 'surname': 'Surname', 'first_name': 'First name', 'middle_name': 'Middle name',
                'admission_no': 'Admission number', 'class': 'Class', 'department': 'Department (SSS)', 'session': 'Session',
                'gender': 'Gender', 'date_of_birth': 'Date of birth', 'age': 'Age', 'blood_group': 'Blood group',
                'genotype': 'Genotype', 'state_of_origin': 'State of origin', 'religion': 'Religion',
                'guardian_name': "Parent/guardian's name", 'guardian_phone': "Parent's phone"},
    'staff': {'name': 'Full name', 'title': 'Title (Mr, Mrs, Dr)', 'surname': 'Surname', 'first_name': 'First name',
              'role': 'Role', 'job_title': 'Job title', 'department': 'Department', 'category': 'Teaching or non-teaching',
              'employment_type': 'Full-time or part-time', 'employed_since': 'Employed since', 'staff_no': 'Staff number',
              'gender': 'Gender', 'date_of_birth': 'Date of birth', 'state_of_origin': 'State of origin', 'phone': 'Phone',
              'email': 'Email', 'nok_name': 'Emergency contact', 'nok_phone': "Emergency contact's phone"},
}
SCHOOL_FIELDS = {'school_name': 'School name', 'school_motto': 'Motto', 'school_address': 'Address',
                 'school_phone': 'School phone', 'school_email': 'School email', 'serial': 'Card number',
                 'issued': 'Date issued'}
BARCODE_FIELDS = {'student': {'serial': 'Card number', 'admission_no': 'Admission number'},
                  'staff': {'serial': 'Card number', 'staff_no': 'Staff number'}}


def fields_for(kind):
    return {**FIELDS.get(kind, {}), **SCHOOL_FIELDS}


def _now():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- checking a design
def _number(value, low, high, default, places=1):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float('inf'), float('-inf')):
        return default
    number = min(high, max(low, number))
    return round(number, places) if places else int(round(number))


def _colour(value, default, allow_none=False):
    value = str(value or '').strip()
    if value in NAMED_COLOURS or COLOUR.match(value) or (allow_none and value == 'none'):
        return value.lower() if value.startswith('#') else value
    return default


def _choice(value, allowed, default):
    return value if value in allowed else default


def _gradient(raw):
    if not isinstance(raw, dict):
        return None
    kind = _choice(raw.get('type'), ('linear', 'radial'), None)
    if kind is None:
        return None
    return {'type': kind, 'from': _colour(raw.get('from'), 'brand'), 'to': _colour(raw.get('to'), 'accent'),
            'angle': _number(raw.get('angle'), 0, 360, 135, 0)}


def _text(value, limit=MAX_TEXT, lines=MAX_LINES):
    """Plain text: control characters dropped, at most ``lines`` lines, each line's spaces tidied, cut to ``limit``."""
    text = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', str(value or '')).replace('\r', '')
    kept = [' '.join(line.split()) for line in text.split('\n')][:lines]
    return '\n'.join(kept).strip()[:limit]


def spec_for(kind):
    """What an ID card design may use: its element types, details, barcode details, fonts and signatures. The report
    card studio (core/report_card_design.py) passes its own."""
    return {'types': ELEMENT_TYPES, 'fields': fields_for(kind), 'barcode_fields': BARCODE_FIELDS.get(kind, {}),
            'fonts': FONTS, 'default_font': 'sans', 'who': ('head', 'holder')}


def _clean_element(raw, kind, width, height, assets, index, spec=None):
    """One element, rebuilt from known keys only, or None. ``spec`` (spec_for) says which types, details, fonts and
    signatures are allowed."""
    spec = spec or spec_for(kind)
    if not isinstance(raw, dict):
        return None
    t = raw.get('type')
    if t == 'rect':                                     # a version 1 colour block is a rectangle shape
        raw = dict(raw, type='shape', shape='rect')
        t = 'shape'
    if t not in spec['types']:
        return None
    el = {'type': t,
          'id': raw['id'] if isinstance(raw.get('id'), str) and ID.match(raw['id']) else f'e{index}',
          'name': _text(raw.get('name'), 40, 1),
          'x': _number(raw.get('x'), -width, width, 2), 'y': _number(raw.get('y'), -height, height, 2),
          'w': _number(raw.get('w'), 0.5, width * 2, 20), 'h': _number(raw.get('h'), 0.5, height * 2, 8),
          'rotate': _number(raw.get('rotate'), -180, 180, 0),
          'opacity': _number(raw.get('opacity'), 0, 100, 100, 0),
          'shadow': _choice(raw.get('shadow'), SHADOWS, 'none'),
          'locked': bool(raw.get('locked')), 'hidden': bool(raw.get('hidden'))}
    if t in ('text', 'field'):
        el.update(font=_choice(raw.get('font'), spec['fonts'], spec['default_font']), size=_number(raw.get('size'), 3, 72, 8),
                  weight=_choice(_number(raw.get('weight'), 300, 900, 400, 0), WEIGHTS, 700 if raw.get('bold') else 400),
                  italic=bool(raw.get('italic')), underline=bool(raw.get('underline')), upper=bool(raw.get('upper')),
                  align=_choice(raw.get('align'), ('left', 'center', 'right', 'justify'), 'left'),
                  valign=_choice(raw.get('valign'), ('top', 'middle', 'bottom'), 'top'),
                  color=_colour(raw.get('color'), 'ink'), letter=_number(raw.get('letter'), -20, 100, 0, 0),
                  line=_number(raw.get('line'), 80, 250, 118, 0), fill=_colour(raw.get('fill'), 'none', allow_none=True),
                  radius=_number(raw.get('radius'), 0, 20, 0), pad=_number(raw.get('pad'), 0, 5, 0))
        if t == 'text':
            el['text'] = _text(raw.get('text')) or 'Text'
        else:
            field = raw.get('field')
            if field not in spec['fields']:
                return None
            el['field'] = field
            el['label'] = _text(raw.get('label'), 40, 1)
    elif t == 'shape':
        el.update(shape=_choice(raw.get('shape'), SHAPES, 'rect'), fill=_colour(raw.get('fill'), 'brand', allow_none=True),
                  gradient=_gradient(raw.get('gradient')), stroke=_colour(raw.get('stroke'), 'none', allow_none=True),
                  stroke_w=_number(raw.get('stroke_w'), 0, 5, 0.4), dash=_choice(raw.get('dash'), DASHES, 'solid'),
                  radius=_number(raw.get('radius'), 0, 40, 0))
    elif t in ('photo', 'logo', 'image', 'signature'):
        el.update(fit=_choice(raw.get('fit'), FITS, 'cover' if t == 'photo' else 'contain'),
                  clip=_choice(raw.get('clip'), CLIPS, 'none'), radius=_number(raw.get('radius'), 0, 40, 0),
                  border=_colour(raw.get('border'), 'none', allow_none=True), border_w=_number(raw.get('border_w'), 0, 3, 0.5),
                  grayscale=bool(raw.get('grayscale')))
        if t == 'image':
            try:
                asset = int(raw.get('asset'))
            except (TypeError, ValueError):
                return None
            if assets is None or asset not in assets:
                return None
            el['asset'] = asset
        if t == 'signature':
            el['who'] = _choice(raw.get('who'), spec['who'], spec['who'][0])
    elif t == 'qr':
        el.update(fg=_colour(raw.get('fg'), 'ink'), bg=_colour(raw.get('bg'), 'white', allow_none=True),
                  radius=_number(raw.get('radius'), 0, 10, 0))
    elif t == 'barcode':
        field = raw.get('field') if raw.get('field') in spec['barcode_fields'] else next(iter(spec['barcode_fields']), None)
        if field is None:
            return None
        el.update(field=field, fg=_colour(raw.get('fg'), 'ink'), bg=_colour(raw.get('bg'), 'white', allow_none=True),
                  show_text=bool(raw.get('show_text', True)))
    return el


def clean_layout(raw, kind, assets=None):
    """A design from the designer (a dict or JSON text), rebuilt from known keys only. ``assets`` is the set of the
    school's own uploaded image ids; an image that is not one of them is dropped. Raises ValueError for something that
    is not a design at all, or a side with too many items."""
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
    orientation = raw.get('orientation') if raw.get('orientation') in CARD_MM else 'landscape'
    width, height = CARD_MM[orientation]
    bg = raw.get('background')
    if not isinstance(bg, dict):                         # version 1: just a colour
        bg = {'color': bg}
    image = bg.get('image')
    try:
        image = int(image) if image not in (None, '') else None
    except (TypeError, ValueError):
        image = None
    background = {'color': _colour(bg.get('color'), 'white'), 'gradient': _gradient(bg.get('gradient')),
                  'image': image if image in assets else None, 'fit': _choice(bg.get('fit'), FITS, 'cover'),
                  'opacity': _number(bg.get('opacity'), 0, 100, 100, 0)}
    sides = raw.get('sides') if isinstance(raw.get('sides'), dict) else {}
    clean = {'v': 2, 'orientation': orientation, 'background': background, 'sides': {}}
    for side in SIDES:
        items = sides.get(side) if isinstance(sides.get(side), list) else []
        if len(items) > MAX_ELEMENTS:
            raise ValueError(f'A side of the card can hold at most {MAX_ELEMENTS} items.')
        out, seen = [], set()
        for i, item in enumerate(items):
            el = _clean_element(item, kind, width, height, assets, i)
            if el is None:
                continue
            if el['id'] in seen:
                el['id'] = f'e{i}'
            seen.add(el['id'])
            out.append(el)
        clean['sides'][side] = out
    return clean


def assets_used(layout):
    """The uploaded image ids a design draws."""
    used = {layout['background']['image']} if layout.get('background', {}).get('image') else set()
    for side in SIDES:
        used |= {el['asset'] for el in layout['sides'].get(side, []) if el['type'] == 'image'}
    return used


# ---------------------------------------------------------------- the school's starting designs
TEMPLATES = {'classic': 'Classic: a band in the school colour, photo on the left',
             'modern': 'Modern: upright, a curved header and a round photo',
             'minimal': 'Minimal: white, a thin accent line, plenty of space',
             'bold': 'Bold: a diagonal sweep of colour with a gradient'}


def _details(kind, x, y, step=5, size=7, w=34, align='left', color='ink'):
    rows = (('admission_no', 'Adm. no'), ('class', 'Class'), ('session', 'Session')) if kind == 'student' else \
           (('job_title', ''), ('staff_no', 'Staff no'), ('phone', 'Phone'))
    return [{'type': 'field', 'field': f, 'label': label, 'x': x, 'y': y + i * step, 'w': w, 'h': step - 0.5, 'size': size,
             'align': align, 'color': color} for i, (f, label) in enumerate(rows)]


def _back(kind):
    return [
        {'type': 'shape', 'shape': 'rect', 'name': 'Top band', 'x': 0, 'y': 0, 'w': 85.6, 'h': 8, 'fill': 'brand'},
        {'type': 'field', 'field': 'school_name', 'x': 4, 'y': 1.5, 'w': 78, 'h': 5, 'size': 8, 'weight': 700, 'upper': True,
         'align': 'center', 'color': 'white'},
        {'type': 'text', 'text': 'This card is the property of the school. If found, please return it to the school.',
         'x': 6, 'y': 12, 'w': 74, 'h': 9, 'size': 7, 'align': 'center', 'color': 'ink'},
        {'type': 'field', 'field': 'school_address', 'x': 6, 'y': 22, 'w': 74, 'h': 8, 'size': 6.5, 'align': 'center', 'color': 'ink'},
        {'type': 'field', 'field': 'school_phone', 'label': 'Tel', 'x': 6, 'y': 30, 'w': 74, 'h': 4, 'size': 6.5, 'align': 'center', 'color': 'ink'},
        {'type': 'signature', 'who': 'head', 'x': 30, 'y': 35, 'w': 26, 'h': 9},
        {'type': 'text', 'text': 'Head of school', 'x': 20, 'y': 44.5, 'w': 46, 'h': 4, 'size': 6, 'align': 'center', 'color': 'ink'},
        {'type': 'shape', 'shape': 'rect', 'name': 'Bottom band', 'x': 0, 'y': 50, 'w': 85.6, 'h': 4, 'fill': 'accent'},
    ]


def default_layout(kind, template='classic'):
    """A tidy design to start from, in one of TEMPLATES; the back says what to do with a found card."""
    title = 'STUDENT IDENTITY CARD' if kind == 'student' else 'STAFF IDENTITY CARD'
    orientation = 'landscape'
    background = {'color': 'white'}
    back = _back(kind)
    if template == 'modern':
        orientation = 'portrait'
        front = [
            {'type': 'shape', 'shape': 'wave', 'name': 'Header', 'x': 0, 'y': 0, 'w': 54, 'h': 30, 'fill': 'brand', 'rotate': 180,
             'gradient': {'type': 'linear', 'from': 'brand', 'to': 'accent', 'angle': 160}},
            {'type': 'logo', 'x': 3.5, 'y': 3, 'w': 8, 'h': 8},
            {'type': 'field', 'field': 'school_name', 'x': 13, 'y': 3.2, 'w': 38, 'h': 8, 'size': 7.5, 'weight': 800, 'upper': True,
             'color': 'white', 'line': 105},
            {'type': 'photo', 'x': 14.5, 'y': 15, 'w': 25, 'h': 25, 'clip': 'circle', 'border': 'white', 'border_w': 1.2, 'shadow': 'soft'},
            {'type': 'field', 'field': 'name', 'x': 3, 'y': 42.5, 'w': 48, 'h': 6, 'size': 10, 'weight': 800, 'align': 'center', 'upper': True},
            *_details(kind, 7, 49.5, step=4.6, size=6.8, w=40, align='center'),
            {'type': 'shape', 'shape': 'rect', 'name': 'Title strip', 'x': 0, 'y': 64.5, 'w': 54, 'h': 5, 'fill': 'brand'},
            {'type': 'text', 'text': title, 'x': 0, 'y': 64.9, 'w': 54, 'h': 4.2, 'size': 6, 'weight': 700, 'align': 'center',
             'color': 'white', 'letter': 20},
            {'type': 'qr', 'x': 4, 'y': 71.5, 'w': 12, 'h': 12},
            {'type': 'barcode', 'field': 'serial', 'x': 19, 'y': 73, 'w': 31.5, 'h': 9, 'show_text': True},
        ]
        back = [
            {'type': 'shape', 'shape': 'rect', 'name': 'Top band', 'x': 0, 'y': 0, 'w': 54, 'h': 10, 'fill': 'brand'},
            {'type': 'field', 'field': 'school_name', 'x': 3, 'y': 2.5, 'w': 48, 'h': 5, 'size': 7.5, 'weight': 700, 'upper': True,
             'align': 'center', 'color': 'white'},
            {'type': 'text', 'text': 'This card is the property of the school.\nIf found, please return it to the school.',
             'x': 4, 'y': 15, 'w': 46, 'h': 12, 'size': 7, 'align': 'center'},
            {'type': 'field', 'field': 'school_address', 'x': 4, 'y': 30, 'w': 46, 'h': 10, 'size': 6.5, 'align': 'center'},
            {'type': 'field', 'field': 'school_phone', 'label': 'Tel', 'x': 4, 'y': 41, 'w': 46, 'h': 4, 'size': 6.5, 'align': 'center'},
            {'type': 'signature', 'who': 'head', 'x': 14, 'y': 55, 'w': 26, 'h': 10},
            {'type': 'shape', 'shape': 'line', 'x': 12, 'y': 66, 'w': 30, 'h': 1, 'stroke': 'ink', 'stroke_w': 0.3},
            {'type': 'text', 'text': 'Head of school', 'x': 7, 'y': 67, 'w': 40, 'h': 4, 'size': 6, 'align': 'center'},
            {'type': 'shape', 'shape': 'rect', 'name': 'Bottom band', 'x': 0, 'y': 81.6, 'w': 54, 'h': 4, 'fill': 'accent'},
        ]
    elif template == 'minimal':
        front = [
            {'type': 'logo', 'x': 4, 'y': 4, 'w': 8, 'h': 8},
            {'type': 'field', 'field': 'school_name', 'x': 14, 'y': 4.5, 'w': 50, 'h': 4.5, 'size': 8, 'weight': 700, 'color': 'brand'},
            {'type': 'text', 'text': title.title(), 'x': 14, 'y': 9, 'w': 50, 'h': 3.5, 'size': 5.5, 'color': 'ink', 'letter': 10},
            {'type': 'shape', 'shape': 'rect', 'name': 'Accent line', 'x': 4, 'y': 15, 'w': 77.6, 'h': 0.6, 'fill': 'accent'},
            {'type': 'photo', 'x': 4, 'y': 19, 'w': 21, 'h': 26, 'clip': 'rounded', 'radius': 2},
            {'type': 'field', 'field': 'name', 'x': 29, 'y': 19.5, 'w': 52, 'h': 6, 'size': 10.5, 'weight': 700},
            *_details(kind, 29, 27, step=4.8, size=6.8, w=34),
            {'type': 'qr', 'x': 68, 'y': 31, 'w': 13.6, 'h': 13.6},
            {'type': 'field', 'field': 'serial', 'x': 4, 'y': 47.5, 'w': 40, 'h': 4, 'size': 6, 'color': 'ink', 'letter': 10},
        ]
    elif template == 'bold':
        background = {'color': 'white'}
        front = [
            {'type': 'shape', 'shape': 'corner', 'name': 'Sweep', 'x': 0, 'y': 0, 'w': 62, 'h': 54, 'fill': 'brand',
             'gradient': {'type': 'linear', 'from': 'brand', 'to': 'accent', 'angle': 135}},
            {'type': 'logo', 'x': 4, 'y': 4, 'w': 10, 'h': 10, 'clip': 'circle', 'border': 'white', 'border_w': 0.6},
            {'type': 'field', 'field': 'school_name', 'x': 16, 'y': 4.5, 'w': 40, 'h': 9, 'size': 9, 'weight': 900, 'upper': True,
             'color': 'white', 'line': 100, 'font': 'impact'},
            {'type': 'photo', 'x': 6, 'y': 18, 'w': 24, 'h': 29, 'clip': 'rounded', 'radius': 3, 'border': 'white',
             'border_w': 1, 'shadow': 'strong'},
            {'type': 'field', 'field': 'name', 'x': 36, 'y': 21, 'w': 46, 'h': 11, 'size': 11, 'weight': 800, 'upper': True,
             'line': 100, 'valign': 'bottom'},
            *_details(kind, 36, 33, step=4.6, size=6.6, w=28),
            {'type': 'qr', 'x': 68, 'y': 37, 'w': 14, 'h': 14, 'radius': 1},
            {'type': 'text', 'text': title, 'x': 36, 'y': 4.5, 'w': 46, 'h': 4, 'size': 5.5, 'weight': 700, 'align': 'right',
             'color': 'brand', 'letter': 15},
        ]
    else:
        template = 'classic'
        front = [
            {'type': 'shape', 'shape': 'rect', 'name': 'Top band', 'x': 0, 'y': 0, 'w': 85.6, 'h': 13, 'fill': 'brand'},
            {'type': 'logo', 'x': 3, 'y': 2, 'w': 9, 'h': 9, 'radius': 2},
            {'type': 'field', 'field': 'school_name', 'x': 14, 'y': 2.5, 'w': 68, 'h': 5, 'size': 9, 'weight': 700, 'upper': True, 'color': 'white'},
            {'type': 'text', 'text': title, 'x': 14, 'y': 7.5, 'w': 68, 'h': 4, 'size': 6, 'color': 'white'},
            {'type': 'photo', 'x': 4, 'y': 16, 'w': 22, 'h': 27, 'clip': 'rounded', 'radius': 1.5, 'border': 'brand', 'border_w': 0.5},
            {'type': 'field', 'field': 'name', 'x': 29, 'y': 17, 'w': 54, 'h': 6, 'size': 10, 'weight': 700, 'upper': True},
            *_details(kind, 29, 24),
            {'type': 'qr', 'x': 66, 'y': 24, 'w': 16, 'h': 16},
            {'type': 'shape', 'shape': 'rect', 'name': 'Bottom band', 'x': 0, 'y': 50, 'w': 85.6, 'h': 4, 'fill': 'accent'},
            {'type': 'field', 'field': 'serial', 'x': 4, 'y': 45, 'w': 40, 'h': 4, 'size': 6, 'color': 'ink'},
        ]
    for side in (front, back):
        for i, el in enumerate(side):
            el.setdefault('id', f'{template[:2]}{i}')
    return clean_layout({'orientation': orientation, 'background': background, 'sides': {'front': front, 'back': back}}, kind)


# ---------------------------------------------------------------- cards
def new_token():
    return secrets.token_urlsafe(16)


def serial_for(kind, card_id):
    return f"{'STU' if kind == 'student' else 'STF'}-{int(card_id):06d}"


def _date_text(value):
    """'2014-03-12' as '12 March 2014' (anything else as it is)."""
    try:
        d = datetime.strptime(str(value or '')[:10], '%Y-%m-%d')
    except ValueError:
        return str(value or '')
    return f'{d.day} {d.strftime("%B %Y")}'


def _age(value):
    try:
        born = datetime.strptime(str(value or '')[:10], '%Y-%m-%d').date()
    except ValueError:
        return ''
    today = datetime.now(timezone.utc).date()
    years = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return str(years) if 0 < years < 120 else ''


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
                    guardian_phone=person.get('guardian_phone') or '', middle_name=person.get('middle_name') or '',
                    date_of_birth=_date_text(person.get('date_of_birth')), age=_age(person.get('date_of_birth')),
                    genotype=person.get('genotype') or '', state_of_origin=person.get('state_of_origin') or '',
                    religion=person.get('religion') or '', guardian_name=person.get('guardian_name') or '')
    else:
        from core.hr import CATEGORIES, EMPLOYMENT_TYPES
        data.update(name=person.get('display_name') or '', role=person.get('role') or '', phone=person.get('phone') or '',
                    email=person.get('email') or '', job_title=person.get('job_title') or person.get('role') or '',
                    department=person.get('department') or '', staff_no=person.get('staff_no') or '',
                    title=person.get('title') or '', first_name=person.get('first_name') or '',
                    surname=person.get('last_name') or '', gender=person.get('gender') or '',
                    date_of_birth=_date_text(person.get('date_of_birth')), state_of_origin=person.get('state_of_origin') or '',
                    category=CATEGORIES.get(person.get('category') or '', ''),
                    employment_type=EMPLOYMENT_TYPES.get(person.get('employment_type') or '', ''),
                    employed_since=_date_text(person.get('employment_date')), nok_name=person.get('nok_name') or '',
                    nok_phone=person.get('nok_phone') or '')
    return data


def qr_path(text):
    """An SVG path (and its size in modules) drawing the QR code for ``text``: only 'M', 'h', 'v', 'z' and numbers, so it
    is safe to place in an attribute as it is. The smallest version that holds the text, error correction M.

    The encoder works the version out itself (one code made, not one per size tried), and each row's dark modules
    that touch are one bar, so a card's code is a few hundred short pieces rather than a thousand squares: a smaller
    print sheet, drawn quicker, for the same code."""
    code = qrencoder.QRCode(None, qrencoder.QRErrorCorrectLevel.M)
    code.addData(text)
    try:
        code.make()
    except Exception:
        raise ValueError('That text is too long for a QR code.')
    n = code.getModuleCount()
    if n > 20 * 4 + 17:
        raise ValueError('That text is too long for a QR code.')
    parts = []
    for r in range(n):
        c = 0
        while c < n:
            if code.isDark(r, c):
                start = c
                while c < n and code.isDark(r, c):
                    c += 1
                run = c - start
                parts.append(f'M{start + 4} {r + 4}h{run}v1h-{run}z')
            else:
                c += 1
    return ''.join(parts), n + 8


def barcode_path(text):
    """A Code 128 barcode for ``text`` as an SVG path 100 units high (and its width in units, quiet zones included): only
    'M', 'h', 'v', 'z' and numbers. None for empty text or text a barcode cannot hold."""
    text = str(text or '')[:40]
    if not text or not all(32 <= ord(ch) < 127 for ch in text):
        return None
    from reportlab.graphics.barcode.code128 import Code128
    code = Code128(text, barWidth=1)
    code.validate()
    code.encode()
    x, parts = 10, []
    for ch in code.decompose():
        width = 'abcd'.index(ch.lower()) + 1
        if ch.isupper():
            parts.append(f'M{x} 0h{width}v100h-{width}z')
        x += width
    return ''.join(parts), x + 10
