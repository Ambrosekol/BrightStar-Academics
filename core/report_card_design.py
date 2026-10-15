"""The report card studio's rules: what a school's own report card design may contain, its starting templates, the
details a card can show, and a sample card to preview a design with.

A design is the same kind of thing as an ID card design (core/id_cards.py), on an A4 page instead of a bank card::

    {"v": 1, "page": "A4", "orientation": "portrait"|"landscape",
     "background": {"color", "gradient", "image", "fit", "opacity"},
     "sides": {"page1": [element, ...], "page2": [element, ...]}}

Its elements are the ID card's (text, details, shapes, the photograph, the logo, the school's own images,
signatures, a barcode) and five blocks that only a report card has: the results table, the summary, the trait
ratings, the grading key and the class teacher's comment, each with its own styling. A second page is printed only
when it has something on it.

As with the ID card, nothing from the browser is trusted: clean_layout() rebuilds a design from known keys only,
clamps every number, checks every choice against its list, and accepts colours only as #rrggbb or the school's own.
The page is drawn by core/report_card_design_pdf.py (the PDF) and static/report-card-render.js (the studio's canvas
and the report card web page), from the same values.
"""

import copy
import json

from core.id_cards import _choice, _clean_element, _colour, _gradient, _number, _text, barcode_path

PAGE_MM = {'portrait': (210.0, 297.0), 'landscape': (297.0, 210.0)}
SIDES = ('page1', 'page2')
MAX_ELEMENTS = 80
# Only fonts the PDF can draw exactly as the studio shows them: DejaVu Sans ships with the app (and covers the Naira
# sign and accented names); Helvetica, Times and Courier are built into every PDF reader.
FONTS = {'dejavu': 'DejaVu Sans', 'helvetica': 'Helvetica', 'times': 'Times', 'mono': 'Courier'}
BLOCKS = ('results', 'summary', 'traits', 'grading', 'comment', 'cumulative')
# The "session so far" table on a Third Term card: each subject's three terms, its average and grade.
CUMULATIVE_COLUMNS = ['subject', 'first', 'second', 'third', 'avg', 'grade']
TYPES = ('text', 'field', 'shape', 'photo', 'logo', 'image', 'signature', 'barcode') + BLOCKS
WHO = ('head', 'teacher')
FIELDS = {
    'student_name': "Student's name", 'admission_no': 'Admission number', 'class': 'Class', 'department': 'Department (SSS)',
    'gender': 'Gender', 'title': 'Report title', 'term': 'Term', 'session': 'Session', 'issued_on': 'Date issued',
    'next_term_begins': 'Next term begins', 'total': 'Total obtained', 'total_max': 'Total obtainable',
    'percentage': 'Overall percentage', 'grade': 'Overall grade', 'remark': 'Overall remark',
    'class_average': 'Class average', 'class_size': 'Class size', 'subjects_count': 'Number of subjects',
    'position': 'Position in class (3rd)', 'position_of': 'Position of class size (3rd of 34)',
    'class_highest': 'Highest in class', 'class_lowest': 'Lowest in class',
    'days_opened': 'Times school opened', 'days_present': 'Days present', 'days_absent': 'Days absent',
    'days_late': 'Days late', 'days_excused': 'Days excused', 'attendance_percentage': 'Attendance (%)',
    'date_of_birth': 'Date of birth', 'age': 'Age', 'guardian_name': "Parent/guardian's name",
    'guardian_phone': "Parent/guardian's phone", 'guardian_email': "Parent/guardian's email", 'blood_group': 'Blood group',
    'genotype': 'Genotype', 'state_of_origin': 'State of origin', 'religion': 'Religion',
    'session_average': 'Session average (Third Term)', 'session_grade': 'Session grade (Third Term)',
    'session_position': 'Position for the session (Third Term)',
    'teacher_name': "Class teacher's name", 'head_name': "Head's name", 'head_title': "Head's title",
    'school_name': 'School name', 'school_motto': 'Motto', 'school_address': 'Address', 'school_phone': 'School phone',
    'school_email': 'School email',
}
BARCODE_FIELDS = {'admission_no': 'Admission number'}
RESULT_COLUMNS = {'subject': 'Subject', 'part1': 'CA part 1', 'part2': 'CA part 2', 'part3': 'CA part 3',
                  'part4': 'CA part 4', 'test': 'Test', 'assignment': 'Assignment', 'project': 'Project', 'ca': 'CA',
                  'exam': 'Exam', 'total': 'Total', 'average': 'Class avg', 'grade': 'Grade', 'remark': 'Remark'}
PART_COLUMNS = ('part1', 'part2', 'part3', 'part4')    # each headed with its own name from the grading scheme
SUMMARY_ITEMS = {'total': 'Total obtained', 'percentage': 'Percentage', 'grade': 'Overall grade',
                 'average': 'Class average', 'size': 'Class size', 'subjects': 'Subjects', 'position': 'Position',
                 'highest': 'Highest in class', 'lowest': 'Lowest in class', 'opened': 'School opened',
                 'present': 'Days present', 'absent': 'Days absent', 'late': 'Days late', 'attendance': 'Attendance'}


def spec():
    return {'types': TYPES, 'fields': FIELDS, 'barcode_fields': BARCODE_FIELDS, 'fonts': FONTS, 'default_font': 'dejavu',
            'who': WHO}


def _block_style(raw):
    """What every block shares: its type, sizes and colours."""
    return {'font': _choice(raw.get('font'), FONTS, 'dejavu'), 'size': _number(raw.get('size'), 5, 16, 8.5),
            'color': _colour(raw.get('color'), 'ink'), 'head_fill': _colour(raw.get('head_fill'), 'brand', allow_none=True),
            'head_color': _colour(raw.get('head_color'), 'white'), 'line': _colour(raw.get('line'), '#c9d2de', allow_none=True),
            'line_w': _number(raw.get('line_w'), 0, 2, 0.3), 'fill': _colour(raw.get('fill'), 'none', allow_none=True),
            'zebra': _colour(raw.get('zebra'), 'none', allow_none=True), 'radius': _number(raw.get('radius'), 0, 10, 0),
            'pad': _number(raw.get('pad'), 0.5, 5, 1.6), 'accent': _colour(raw.get('accent'), 'brand')}


def _clean_block(raw, base):
    el = dict(base, **_block_style(raw))
    t = el['type']
    if t == 'cumulative':
        el['columns'] = list(CUMULATIVE_COLUMNS)
        el['numbers'] = _choice(raw.get('numbers'), ('center', 'right'), 'center')
        el['head_size'] = _number(raw.get('head_size'), 5, 16, el['size'])
    elif t == 'results':
        asked = raw.get('columns') if isinstance(raw.get('columns'), list) else list(RESULT_COLUMNS)
        el['columns'] = [c for c in RESULT_COLUMNS if c in asked] or ['subject', 'total', 'grade']
        if 'subject' not in el['columns']:
            el['columns'].insert(0, 'subject')
        el['numbers'] = _choice(raw.get('numbers'), ('center', 'right'), 'center')
        el['head_size'] = _number(raw.get('head_size'), 5, 16, el['size'])
    elif t == 'summary':
        asked = raw.get('items') if isinstance(raw.get('items'), list) else ['total', 'percentage', 'grade', 'average', 'size', 'subjects']
        el['items'] = [c for c in SUMMARY_ITEMS if c in asked] or ['total', 'percentage', 'grade']
        el['layout'] = _choice(raw.get('layout'), ('row', 'column'), 'row')
        el['value_size'] = _number(raw.get('value_size'), 6, 24, 11)
    elif t == 'traits':
        el['layout'] = _choice(raw.get('layout'), ('side', 'stack'), 'side')
    elif t == 'grading':
        el['per_row'] = _number(raw.get('per_row'), 1, 6, 3, 0)
        el['title'] = _text(raw.get('title'), 60, 1)
    elif t == 'comment':
        el['label'] = _text(raw.get('label'), 60, 1)
        el['lines'] = bool(raw.get('lines', True))
        el['comment_size'] = _number(raw.get('comment_size'), 5, 16, 9.5)
    return el


def clean_layout(raw, assets=None):
    """A report card design from the studio (a dict or JSON text), rebuilt from known keys only. ``assets`` is the set of
    the school's own uploaded image ids. Raises ValueError for something that is not a design at all, or a page with
    too many items."""
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
            raise ValueError(f'A page of the report card can hold at most {MAX_ELEMENTS} items.')
        out, seen = [], set()
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            if item.get('type') in BLOCKS:
                # The common part (box, turn, opacity...) through the ID card's own checks, as a plain shape.
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


# ---------------------------------------------------------------- the details on a card
def _fmt(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value or '')
    return str(int(number)) if number == int(number) else f'{number:.1f}'


def _ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _pct(value):
    return (_fmt(value) + '%') if value not in (None, '') else ''


def card_fields(card):
    """``{field: text}`` for one card (core/report_card_pdf.py documents the card)."""
    school, student = card.get('school') or {}, card.get('student') or {}
    attendance = card.get('attendance') or {}
    summary, comment, head = card.get('summary') or {}, card.get('comment') or {}, card.get('head') or {}
    cumulative = card.get('cumulative') or {}
    percentage = summary.get('percentage')
    average = summary.get('class_average_percentage')
    return {
        'student_name': student.get('name') or '', 'admission_no': student.get('admission_no') or '',
        'class': student.get('class_name') or '', 'department': student.get('department') or '',
        'gender': student.get('gender') or '', 'title': card.get('title') or 'Report Card', 'term': card.get('term') or '',
        'session': card.get('session') or '', 'issued_on': card.get('issued_on') or '',
        'next_term_begins': card.get('next_term_begins') or '', 'total': _fmt(summary.get('total')),
        'total_max': _fmt(summary.get('total_max')),
        'percentage': (_fmt(percentage) + '%') if percentage not in (None, '') else '',
        'grade': summary.get('grade') or '', 'remark': summary.get('remark') or '',
        'class_average': (_fmt(average) + '%') if average not in (None, '') else '',
        'class_size': str(summary.get('class_size') or '') if summary.get('class_size') else '',
        'subjects_count': str(summary.get('subjects_count') or ''), 'teacher_name': comment.get('teacher_name') or '',
        'head_name': head.get('name') or '', 'head_title': head.get('title') or 'Head of School',
        'school_name': school.get('name') or '', 'school_motto': school.get('motto') or '',
        'school_address': school.get('address') or '', 'school_phone': school.get('phone') or '',
        'school_email': school.get('email') or '',
        'position': _ordinal(summary['position']) if summary.get('position') else '',
        'position_of': (_ordinal(summary['position']) + (f" of {summary['class_size']}" if summary.get('class_size') else ''))
                       if summary.get('position') else '',
        'class_highest': _pct(summary.get('class_highest')), 'class_lowest': _pct(summary.get('class_lowest')),
        'days_opened': str(attendance['opened']) if attendance.get('opened') else '',
        'days_present': str(attendance.get('present', '')) if attendance.get('opened') else '',
        'days_absent': str(attendance.get('absent', '')) if attendance.get('opened') else '',
        'days_late': str(attendance.get('late', '')) if attendance.get('opened') else '',
        'days_excused': str(attendance.get('excused', '')) if attendance.get('opened') else '',
        'attendance_percentage': _pct(attendance.get('percentage')),
        'date_of_birth': student.get('date_of_birth') or '', 'age': str(student['age']) if student.get('age') else '',
        'guardian_name': student.get('guardian_name') or '', 'guardian_phone': student.get('guardian_phone') or '',
        'guardian_email': student.get('guardian_email') or '', 'blood_group': student.get('blood_group') or '',
        'genotype': student.get('genotype') or '', 'state_of_origin': student.get('state_of_origin') or '',
        'religion': student.get('religion') or '',
        'session_average': _pct(cumulative.get('average')),
        'session_grade': ' - '.join(p for p in (cumulative.get('grade'), cumulative.get('remark')) if p),
        'session_position': (_ordinal(cumulative['position']) + (f" of {cumulative['class_size']}" if cumulative.get('class_size') else ''))
                            if cumulative.get('position') else '',
    }


def view_data(card):
    """What static/report-card-render.js draws one card from: the details, the results, the summary, the traits, the
    grading key and the comment, as plain values (pictures are added by the caller as addresses)."""
    subjects, heads = [], {}
    for s in card.get('subjects') or []:
        row = {'subject': s.get('name') or '', 'test': _fmt(s.get('test')), 'assignment': _fmt(s.get('assignment')),
               'project': _fmt(s.get('project')), 'ca': _fmt(s.get('ca')), 'exam': _fmt(s.get('exam')),
               'total': _fmt(s.get('total')), 'average': _fmt(s.get('class_average')), 'grade': s.get('grade') or '',
               'remark': s.get('remark') or ''}
        for column, part in zip(PART_COLUMNS, s.get('parts') or []):
            row[column] = _fmt(part.get('score'))
            heads.setdefault(column, part.get('label') or '')
        subjects.append(row)
    data = card_fields(card)
    summary = {'total': f"{data['total']} / {data['total_max']}" if data['total_max'] else data['total'],
               'percentage': data['percentage'], 'grade': ' - '.join(p for p in (data['grade'], data['remark']) if p),
               'average': data['class_average'], 'size': data['class_size'], 'subjects': data['subjects_count'],
               'position': data['position_of'], 'highest': data['class_highest'], 'lowest': data['class_lowest'],
               'opened': data['days_opened'], 'present': data['days_present'], 'absent': data['days_absent'],
               'late': data['days_late'], 'attendance': data['attendance_percentage']}
    comment = (card.get('comment') or {}).get('text') or ''
    traits = [{'name': g.get('name') or '', 'entries': [{'label': e.get('label') or '', 'rating': e.get('rating_label') or ''}
                                                        for e in g.get('entries') or []]}
              for g in card.get('traits') or []]
    grading = [{'grade': k.get('grade') or '', 'range': k.get('range') or '', 'remark': k.get('remark') or ''}
               for k in card.get('grading_key') or []]
    maxes = {}
    for key, column in (('ca_max', 'ca'), ('exam_max', 'exam'), ('total_max', 'total')):
        seen = {_fmt(s.get(key)) for s in card.get('subjects') or [] if s.get(key) not in (None, '')}
        if len(seen) == 1:
            maxes[column] = seen.pop()
    for i, column in enumerate(PART_COLUMNS):
        seen = {_fmt(s['parts'][i].get('max')) for s in card.get('subjects') or [] if len(s.get('parts') or []) > i}
        if len(seen) == 1:
            maxes[column] = seen.pop()
    cumulative = [{'subject': s.get('name') or '', 'first': _fmt(s.get('first')) if s.get('first') is not None else '-',
                   'second': _fmt(s.get('second')) if s.get('second') is not None else '-',
                   'third': _fmt(s.get('third')) if s.get('third') is not None else '-',
                   'avg': _fmt(s.get('average')), 'grade': s.get('grade') or ''}
                  for s in (card.get('cumulative') or {}).get('subjects') or []]
    code = barcode_path(data['admission_no'])
    return {'data': data, 'report': {'subjects': subjects, 'summary': summary, 'comment': comment, 'traits': traits,
                                     'grading': grading, 'maxes': maxes, 'heads': heads, 'cumulative': cumulative},
            'barcodes': {'admission_no': code} if code else {}}


def sample_card(school, head, grading):
    """A made-up card to preview a design with, when the school has no released report card yet."""
    names = ['English Language', 'Mathematics', 'Basic Science', 'Basic Technology', 'Social Studies', 'Civic Education',
             'Computer Studies', 'Agricultural Science', 'French', 'Yoruba', 'Christian Religious Studies', 'Physical & Health Education']
    marks = [(12, 18, 51), (14, 16, 46), (10, 15, 40), (13, 17, 38), (15, 19, 44), (11, 14, 35), (14, 18, 50), (9, 13, 33),
             (12, 15, 41), (13, 17, 45), (14, 18, 47), (15, 19, 52)]
    subjects = []
    for name, (test, ass, exam) in zip(names, marks):
        total = test + ass + exam
        grade = next((g['grade'] for g in grading if _within(total, g.get('range'))), '')
        remark = next((g['remark'] for g in grading if g.get('grade') == grade), '')
        subjects.append({'name': name, 'test': test, 'assignment': ass, 'project': None, 'ca': test + ass, 'ca_max': 40,
                         'parts': [{'label': 'CA1', 'score': test, 'max': 20}, {'label': 'CA2', 'score': ass, 'max': 20}],
                         'exam': exam, 'exam_max': 60, 'total': total, 'total_max': 100, 'grade': grade, 'remark': remark,
                         'class_average': round(total * 0.88, 1)})
    total = sum(s['total'] for s in subjects)
    pct = round(total / (len(subjects) * 100) * 100, 1)
    session = [{'name': s['name'], 'first': s['total'] - 4, 'second': s['total'] + 3, 'third': s['total'],
                'average': round((3 * s['total'] - 1) / 3, 1), 'grade': s['grade'], 'remark': s['remark']} for s in subjects]
    cumulative = {'subjects': session, 'average': round(pct - 0.3, 1), 'grade': 'B', 'remark': 'Very good', 'position': 4,
                  'class_size': 34}
    return {'school': school, 'title': 'First Term Report', 'term': 'First Term', 'session': '2026/2027',
            'student': {'name': 'Adaeze Chukwuemeka Okafor', 'admission_no': 'BSA/2024/0117', 'class_name': 'JSS 2A',
                        'gender': 'Female', 'department': '', 'photo_path': None, 'date_of_birth': '14 March 2014',
                        'age': 12, 'guardian_name': 'Mr Emeka Okafor', 'guardian_phone': '0803 123 4567',
                        'guardian_email': 'emeka.okafor@example.com', 'blood_group': 'O+', 'genotype': 'AA',
                        'state_of_origin': 'Anambra', 'religion': 'Christianity'},
            'attendance': {'opened': 62, 'present': 59, 'late': 3, 'absent': 3, 'excused': 0, 'percentage': 95.2},
            'subjects': subjects, 'cumulative': cumulative,
            'summary': {'total': total, 'total_max': len(subjects) * 100, 'percentage': pct, 'grade': 'B', 'remark': 'Very good',
                        'class_average_percentage': 61.4, 'class_size': 34, 'subjects_count': len(subjects),
                        'position': 3, 'class_highest': 88.6, 'class_lowest': 41.2},
            'comment': {'text': 'Adaeze is attentive and hardworking. She should keep it up and read more in Basic Science.',
                        'teacher_name': 'Mrs Funmi Bello', 'teacher_signature_path': None},
            'traits': [{'name': 'Affective', 'entries': [{'label': 'Punctuality', 'rating_label': 'Excellent'},
                                                         {'label': 'Neatness', 'rating_label': 'Very good'},
                                                         {'label': 'Politeness', 'rating_label': 'Excellent'},
                                                         {'label': 'Attentiveness', 'rating_label': 'Good'}]},
                       {'name': 'Psychomotor', 'entries': [{'label': 'Handwriting', 'rating_label': 'Very good'},
                                                           {'label': 'Sports', 'rating_label': 'Good'},
                                                           {'label': 'Drawing', 'rating_label': 'Very good'},
                                                           {'label': 'Musical skills', 'rating_label': 'Fair'}]}],
            'head': head, 'grading_key': grading, 'issued_on': '12 December 2026', 'next_term_begins': '6 January 2027'}


def _within(score, span):
    try:
        low, high = (float(p) for p in str(span).replace('–', '-').split('-')[:2])
    except (TypeError, ValueError):
        return False
    return low <= score <= high


# ---------------------------------------------------------------- starting templates
TEMPLATES = {'classic': 'Classic: the school band, a boxed student panel and a full results table',
             'modern': 'Modern: a coloured sidebar with the photo and details, results beside it',
             'elegant': 'Elegant: serif type, centred heading and fine rules',
             'bold': 'Bold: a sweeping gradient header and strong blocks'}


def _f(field, x, y, w, h, size=8.5, **kw):
    return dict({'type': 'field', 'field': field, 'x': x, 'y': y, 'w': w, 'h': h, 'size': size, 'font': 'dejavu'}, **kw)


def _signatures(y, color='ink', line='ink'):
    return [
        {'type': 'signature', 'who': 'teacher', 'x': 24, 'y': y, 'w': 52, 'h': 11},
        {'type': 'shape', 'shape': 'line', 'name': "Teacher's line", 'x': 20, 'y': y + 11, 'w': 60, 'h': 1, 'stroke': line, 'stroke_w': 0.3},
        _f('teacher_name', 20, y + 12.5, 60, 4, 8, align='center', weight=700, color=color),
        {'type': 'text', 'text': 'Class Teacher', 'x': 20, 'y': y + 16.5, 'w': 60, 'h': 4, 'size': 7, 'align': 'center', 'font': 'dejavu', 'color': color},
        {'type': 'signature', 'who': 'head', 'x': 134, 'y': y, 'w': 52, 'h': 11},
        {'type': 'shape', 'shape': 'line', 'name': "Head's line", 'x': 130, 'y': y + 11, 'w': 60, 'h': 1, 'stroke': line, 'stroke_w': 0.3},
        _f('head_name', 130, y + 12.5, 60, 4, 8, align='center', weight=700, color=color),
        _f('head_title', 130, y + 16.5, 60, 4, 7, align='center', color=color),
    ]


def default_layout(template='classic'):
    """A complete report card to start from, in one of TEMPLATES."""
    bg = {'color': 'white'}
    if template == 'modern':
        page = [
            {'type': 'shape', 'shape': 'rect', 'name': 'Sidebar', 'x': 0, 'y': 0, 'w': 62, 'h': 297, 'fill': 'brand',
             'gradient': {'type': 'linear', 'from': 'brand', 'to': 'accent', 'angle': 170}},
            {'type': 'logo', 'x': 21, 'y': 10, 'w': 20, 'h': 20, 'clip': 'circle', 'border': 'white', 'border_w': 0.8},
            _f('school_name', 5, 33, 52, 12, 11, weight=800, color='white', align='center', upper=True, line=105),
            _f('school_motto', 5, 45, 52, 6, 7.5, color='white', align='center', italic=False, opacity=85),
            {'type': 'photo', 'x': 13, 'y': 58, 'w': 36, 'h': 36, 'clip': 'circle', 'border': 'white', 'border_w': 1.2, 'shadow': 'soft'},
            _f('student_name', 5, 98, 52, 12, 11, weight=800, color='white', align='center', line=105),
            _f('admission_no', 5, 112, 52, 5, 8, color='white', align='center', label='Adm. no'),
            _f('class', 5, 118, 52, 5, 8, color='white', align='center', label='Class'),
            _f('gender', 5, 124, 52, 5, 8, color='white', align='center', label='Gender'),
            _f('term', 5, 136, 52, 5, 8.5, color='white', align='center', weight=700),
            _f('session', 5, 142, 52, 5, 8, color='white', align='center', label='Session'),
            {'type': 'summary', 'x': 7, 'y': 156, 'w': 48, 'h': 64, 'layout': 'column', 'color': 'white', 'accent': 'white',
             'line': 'white', 'line_w': 0.2, 'value_size': 13, 'size': 7.5},
            _f('next_term_begins', 5, 226, 52, 9, 8, color='white', align='center', label='Next term begins'),
            {'type': 'barcode', 'field': 'admission_no', 'x': 9, 'y': 270, 'w': 44, 'h': 16, 'bg': 'white', 'show_text': True},
            _f('title', 70, 10, 128, 9, 16, weight=800, color='brand', upper=True),
            _f('school_address', 70, 19, 128, 6, 8, color='ink'),
            {'type': 'results', 'x': 70, 'y': 30, 'w': 128, 'h': 130, 'columns': ['subject', 'ca', 'exam', 'total', 'grade', 'remark'],
             'zebra': '#f1f5fb', 'head_fill': 'brand', 'line': '#d5dce6', 'size': 8, 'radius': 2},
            {'type': 'traits', 'x': 70, 'y': 165, 'w': 128, 'h': 36, 'layout': 'side', 'head_fill': 'accent', 'size': 7.5},
            {'type': 'comment', 'x': 70, 'y': 206, 'w': 128, 'h': 26, 'label': "Class Teacher's Comment", 'head_fill': 'none',
             'fill': '#f6f8fb', 'radius': 2, 'color': 'ink', 'accent': 'brand'},
            {'type': 'signature', 'who': 'teacher', 'x': 72, 'y': 240, 'w': 50, 'h': 11},
            {'type': 'shape', 'shape': 'line', 'x': 70, 'y': 251, 'w': 56, 'h': 1, 'stroke': 'ink', 'stroke_w': 0.3},
            _f('teacher_name', 70, 252.5, 56, 4, 7.5, align='center', weight=700),
            {'type': 'signature', 'who': 'head', 'x': 145, 'y': 240, 'w': 50, 'h': 11},
            {'type': 'shape', 'shape': 'line', 'x': 142, 'y': 251, 'w': 56, 'h': 1, 'stroke': 'ink', 'stroke_w': 0.3},
            _f('head_name', 142, 252.5, 56, 4, 7.5, align='center', weight=700),
            _f('head_title', 142, 256.5, 56, 4, 7, align='center'),
            {'type': 'grading', 'x': 70, 'y': 266, 'w': 128, 'h': 22, 'per_row': 3, 'title': 'Grading key', 'size': 7,
             'fill': '#f6f8fb', 'radius': 2, 'head_fill': 'none'},
        ]
    elif template == 'elegant':
        gold = '#a8822d'
        page = [
            {'type': 'shape', 'shape': 'rect', 'name': 'Frame', 'x': 7, 'y': 7, 'w': 196, 'h': 283, 'fill': 'none', 'stroke': gold, 'stroke_w': 0.8},
            {'type': 'shape', 'shape': 'rect', 'name': 'Inner frame', 'x': 9, 'y': 9, 'w': 192, 'h': 279, 'fill': 'none', 'stroke': gold, 'stroke_w': 0.25},
            {'type': 'logo', 'x': 95, 'y': 13, 'w': 20, 'h': 20},
            _f('school_name', 20, 34, 170, 9, 17, font='times', weight=700, align='center', color='brand', upper=True, letter=8),
            _f('school_motto', 20, 43, 170, 5, 9, font='times', italic=True, align='center', color=gold),
            _f('school_address', 20, 48, 170, 5, 8, font='times', align='center'),
            {'type': 'shape', 'shape': 'line', 'x': 60, 'y': 55, 'w': 90, 'h': 1, 'stroke': gold, 'stroke_w': 0.4},
            _f('title', 20, 57, 170, 7, 12.5, font='times', weight=700, align='center', upper=True, letter=12),
            _f('session', 20, 64, 170, 5, 8.5, font='times', align='center', label='Session'),
            {'type': 'photo', 'x': 172, 'y': 70, 'w': 22, 'h': 27, 'clip': 'rounded', 'radius': 1, 'border': gold, 'border_w': 0.4},
            _f('student_name', 16, 71, 150, 7, 12, font='times', weight=700, label='Name'),
            _f('admission_no', 16, 79, 75, 5, 9, font='times', label='Admission No.'),
            _f('class', 91, 79, 75, 5, 9, font='times', label='Class'),
            _f('term', 16, 85, 75, 5, 9, font='times', label='Term'),
            _f('gender', 91, 85, 75, 5, 9, font='times', label='Gender'),
            {'type': 'results', 'x': 16, 'y': 101, 'w': 178, 'h': 98, 'font': 'times', 'size': 8.5, 'head_fill': 'none',
             'head_color': 'brand', 'line': gold, 'line_w': 0.25, 'zebra': 'none'},
            {'type': 'summary', 'x': 16, 'y': 203, 'w': 178, 'h': 13, 'font': 'times', 'line': gold, 'accent': 'brand', 'value_size': 11},
            {'type': 'traits', 'x': 16, 'y': 219, 'w': 178, 'h': 26, 'font': 'times', 'head_fill': 'none', 'head_color': 'brand', 'line': gold, 'size': 7.5},
            {'type': 'comment', 'x': 16, 'y': 247, 'w': 178, 'h': 16, 'font': 'times', 'label': "Class Teacher's Remarks", 'line': gold,
             'head_fill': 'none', 'accent': 'brand'},
            *[dict(el, font='times') if el['type'] in ('field', 'text') else (dict(el, stroke=gold) if el['type'] == 'shape' else el)
              for el in _signatures(264)],
            {'type': 'grading', 'x': 16, 'y': 282.5, 'w': 178, 'h': 5, 'per_row': 6, 'font': 'times', 'size': 6.5, 'line': 'none',
             'head_fill': 'none', 'title': ''},
        ]
    elif template == 'bold':
        page = [
            {'type': 'shape', 'shape': 'corner', 'name': 'Header sweep', 'x': 0, 'y': 0, 'w': 240, 'h': 64, 'fill': 'brand',
             'gradient': {'type': 'linear', 'from': 'brand', 'to': 'accent', 'angle': 120}},
            {'type': 'logo', 'x': 12, 'y': 9, 'w': 22, 'h': 22, 'clip': 'circle', 'border': 'white', 'border_w': 1},
            _f('school_name', 38, 9, 118, 15, 17, weight=800, color='white', upper=True, line=100),
            _f('school_motto', 38, 25, 118, 5, 8.5, color='white'),
            _f('title', 120, 40, 80, 8, 13, weight=800, color='brand', align='right', upper=True),
            _f('session', 120, 48, 80, 5, 8.5, align='right', label='Session'),
            {'type': 'shape', 'shape': 'rect', 'name': 'Student panel', 'x': 12, 'y': 62, 'w': 186, 'h': 32, 'fill': 'brand', 'opacity': 10, 'radius': 3},
            {'type': 'photo', 'x': 16, 'y': 65, 'w': 22, 'h': 26, 'clip': 'rounded', 'radius': 2, 'border': 'brand', 'border_w': 0.6},
            _f('student_name', 43, 66, 150, 8, 14, weight=800, upper=True),
            _f('admission_no', 43, 75, 75, 5, 8.5, label='Adm. no'), _f('class', 118, 75, 75, 5, 8.5, label='Class'),
            _f('term', 43, 81, 75, 5, 8.5, label='Term'), _f('gender', 118, 81, 75, 5, 8.5, label='Gender'),
            {'type': 'results', 'x': 12, 'y': 98, 'w': 186, 'h': 100, 'zebra': '#eef3fa', 'head_fill': 'brand', 'line': 'none',
             'size': 8.5, 'radius': 2.5},
            {'type': 'summary', 'x': 12, 'y': 201, 'w': 186, 'h': 15, 'fill': 'brand', 'color': 'white', 'accent': 'white',
             'line': 'none', 'radius': 2.5, 'value_size': 12},
            {'type': 'traits', 'x': 12, 'y': 219, 'w': 186, 'h': 26, 'head_fill': 'accent', 'size': 7.5},
            {'type': 'comment', 'x': 12, 'y': 247, 'w': 186, 'h': 16, 'label': "Class Teacher's Comment", 'head_fill': 'none',
             'accent': 'brand', 'line': '#c9d2de'},
            *_signatures(264),
            {'type': 'grading', 'x': 12, 'y': 284, 'w': 186, 'h': 6, 'per_row': 6, 'size': 6.5, 'line': 'none', 'head_fill': 'none', 'title': ''},
        ]
    else:
        template = 'classic'
        page = [
            {'type': 'shape', 'shape': 'rect', 'name': 'Top band', 'x': 0, 'y': 0, 'w': 210, 'h': 6, 'fill': 'brand'},
            {'type': 'shape', 'shape': 'rect', 'name': 'Accent stripe', 'x': 0, 'y': 6, 'w': 210, 'h': 1.2, 'fill': 'accent'},
            {'type': 'logo', 'x': 14, 'y': 11, 'w': 24, 'h': 24},
            _f('school_name', 42, 12, 154, 9, 16, weight=700, color='brand'),
            _f('school_motto', 42, 21, 154, 5, 8.5, color='ink', opacity=80),
            _f('school_address', 42, 26.5, 154, 5, 7.5),
            _f('school_phone', 42, 31, 75, 4.5, 7.5, label='Tel'), _f('school_email', 117, 31, 79, 4.5, 7.5, label='Email'),
            {'type': 'shape', 'shape': 'rect', 'name': 'Rule', 'x': 14, 'y': 38, 'w': 182, 'h': 0.6, 'fill': 'brand'},
            {'type': 'shape', 'shape': 'rect', 'name': 'Title bar', 'x': 14, 'y': 41, 'w': 182, 'h': 8.5, 'fill': 'brand'},
            _f('title', 14, 42.3, 182, 6, 11.5, weight=700, color='white', align='center', upper=True),
            _f('term', 40, 51, 65, 4.5, 8, align='right', label='Term'), _f('session', 108, 51, 65, 4.5, 8, label='Session'),
            {'type': 'shape', 'shape': 'rect', 'name': 'Student panel', 'x': 14, 'y': 57, 'w': 182, 'h': 34, 'fill': 'brand',
             'opacity': 7, 'stroke': '#c9d2de', 'stroke_w': 0.3},
            _f('student_name', 18, 60, 140, 6.5, 11, weight=700, label='Name'),
            _f('admission_no', 18, 69, 70, 5, 8.5, label='Admission no.'), _f('class', 90, 69, 70, 5, 8.5, label='Class'),
            _f('gender', 18, 75.5, 70, 5, 8.5, label='Gender'), _f('department', 90, 75.5, 70, 5, 8.5, label='Department'),
            {'type': 'photo', 'x': 166, 'y': 59, 'w': 26, 'h': 30, 'clip': 'rounded', 'radius': 1, 'border': 'brand', 'border_w': 0.4},
            {'type': 'results', 'x': 14, 'y': 95, 'w': 182, 'h': 100, 'zebra': '#f1f5fb', 'head_fill': 'brand', 'line': '#c9d2de'},
            {'type': 'summary', 'x': 14, 'y': 198, 'w': 182, 'h': 13, 'fill': '#f1f5fb', 'line': 'brand', 'line_w': 0.4, 'accent': 'brand'},
            {'type': 'traits', 'x': 14, 'y': 214, 'w': 182, 'h': 28, 'head_fill': 'none', 'size': 7.5},
            {'type': 'comment', 'x': 14, 'y': 245, 'w': 182, 'h': 17, 'label': "Class Teacher's Comment", 'head_fill': 'none',
             'fill': '#f1f5fb', 'accent': 'brand'},
            *_signatures(264),
            {'type': 'grading', 'x': 14, 'y': 285, 'w': 182, 'h': 7, 'per_row': 6, 'size': 6.5, 'title': '', 'head_fill': 'none',
             'line': 'none'},
        ]
    for i, el in enumerate(page):
        el.setdefault('id', f'{template[:2]}{i}')
    return clean_layout({'orientation': 'portrait', 'background': bg, 'sides': {'page1': copy.deepcopy(page), 'page2': []}})
