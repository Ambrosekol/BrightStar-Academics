"""Report cards: when a student's is ready, and everything that goes on it.

A report card is worked out from the term results when someone asks for it, never stored, so it
can never disagree with the results it is made from.

**When is one ready?** For one student and one term, the moment ALL of that student's results in the
term have been released. One result still waiting (entered, verified or approved) holds the card
back, so a card is always complete and never shows a mark the student has not been given. Practice
tests are not part of the official record, so they neither count nor hold a card back.

**What is on it?** The school's own logo, name, address and contact details in its own colours; the
student's details; each subject as CA (out of 40) plus Exam (out of 60); a grade and remark; the
overall percentage; the class average; affective and psychomotor trait ratings, once a teacher has
rated at least one; the class teacher's comment with that teacher's own signature; and the head's
title, name and signature. The marks are the term result the school already works out
(blueprints/school/helpers.py), so the arithmetic exists in one place only.

``build_cards`` returns plain dictionaries. The PDF (core/report_card_pdf.py) and the web page
(templates/report_card.html) are both drawn from the same dictionary, so they say the same thing.
"""

import json
import re
from datetime import datetime, timezone

from sqlalchemy import case, func, or_, select

from app import ACADEMIC_TERMS
from blueprints.school.helpers import _primary_school_id, _term_reports_bulk
from core.branding import school_brand
from core.db_helpers import tuples
from core.storage import upload_exists
from models import (
    AcademicSession, Admin, ReportCardComment, ReportCardTrait, SchoolAssessment, SchoolClass, SchoolSetting,
    SchoolStudentResult, SchoolSubject, Student, StudentEnrolment, db,
)

# ---------------------------------------------------------------------------------------------
# Grades. One scale for every school, shown on the card so nobody has to guess what a grade means.
GRADE_SCALE = (
    (70, 'A', 'Excellent'),
    (60, 'B', 'Very Good'),
    (50, 'C', 'Good'),
    (45, 'D', 'Fair'),
    (40, 'E', 'Pass'),
    (0, 'F', 'Fail'),
)

HEAD_TITLES = ('Head Teacher', 'Headmistress', 'Headmaster', 'Principal', 'Proprietress', 'Proprietor', 'Director')

SETTING_HEAD_TITLE = 'report_head_title'
SETTING_HEAD_NAME = 'report_head_name'
SETTING_HEAD_SIGNATURE = 'report_head_signature'
SETTING_NEXT_TERM = 'report_next_term_begins'

MAX_COMMENT_LENGTH = 1000

_KNOWN_TERMS = (*ACADEMIC_TERMS, 'Full Session')

# ---------------------------------------------------------------------------------------------
# Affective and psychomotor traits. A fixed catalogue (unlike the free-text comment) so every
# report card in the school rates the same things, and so a rating can be stored as a small
# {key: 1..5} JSON object rather than a row per trait.
TRAIT_SCALE = ((5, 'Excellent'), (4, 'Very Good'), (3, 'Good'), (2, 'Fair'), (1, 'Poor'))

TRAIT_GROUPS = (
    ('Affective Domain', (
        ('punctuality', 'Punctuality'),
        ('attentiveness', 'Attentiveness'),
        ('honesty', 'Honesty'),
        ('neatness', 'Neatness'),
        ('politeness', 'Politeness'),
        ('cooperation', 'Cooperation with others'),
        ('leadership', 'Leadership'),
        ('self_control', 'Self-control'),
    )),
    ('Psychomotor Domain', (
        ('handwriting', 'Handwriting'),
        ('verbal_fluency', 'Verbal fluency'),
        ('sports_games', 'Sports & games'),
        ('musical_skills', 'Musical skills'),
        ('drawing_painting', 'Drawing & painting'),
        ('handling_tools', 'Handling of tools/instruments'),
    )),
)

_TRAIT_LABELS = {key: label for _, items in TRAIT_GROUPS for key, label in items}
_TRAIT_KEYS = set(_TRAIT_LABELS)
_TRAIT_RATING_LABELS = dict(TRAIT_SCALE)


def trait_rating_label(value):
    """'Excellent', ... for 1..5, or '' for anything else (unrated)."""
    return _TRAIT_RATING_LABELS.get(value, '')


def _parse_ratings(raw):
    """A trait row's JSON text as ``{key: 1..5}``, dropping anything that is not a known trait or a
    valid rating. Never raises: bad or missing JSON is simply "nothing rated yet"."""
    try:
        parsed = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    out = {}
    for key, value in parsed.items():
        if key in _TRAIT_KEYS and isinstance(value, int) and 1 <= value <= 5:
            out[key] = value
    return out


def grade_for(percentage):
    """``(letter, remark)`` for a percentage."""
    for low, letter, remark in GRADE_SCALE:
        if percentage >= low:
            return letter, remark
    return GRADE_SCALE[-1][1], GRADE_SCALE[-1][2]


def grading_key():
    """The scale as it is printed on the card: ``[{'range': '70 - 100', 'grade': 'A', 'remark': ...}]``."""
    key, upper = [], 100
    for low, letter, remark in GRADE_SCALE:
        key.append({'range': f'{low} - {upper}', 'grade': letter, 'remark': remark})
        upper = low - 1
    return key


def term_slug(term):
    """A term as it appears in a web address: "First Term" -> "first-term"."""
    return re.sub(r'[^a-z0-9]+', '-', str(term).lower()).strip('-')


def term_from_slug(slug):
    """The term a web address names, or None if it names nothing the school has."""
    for term in _KNOWN_TERMS:
        if term_slug(term) == slug:
            return term
    return None


def term_title(term):
    return 'Annual Report Card' if term == 'Full Session' else f'{term} Report Card'


# ---------------------------------------------------------------------------------------------
# Is a card ready?
def _official_result_counts(student_ids, session_id=None, term=None):
    """``{(student_id, session_id, term): {'released': n, 'pending': n, 'released_at': latest}}``.

    Counts each student's results by whether they have been released. Practice tests are left out.
    """
    student_ids = list(student_ids)
    if not student_ids:
        return {}
    term_of = func.coalesce(SchoolStudentResult.term, 'Full Session')
    stmt = (select(SchoolStudentResult.student_id, SchoolStudentResult.session_id, term_of,
                   func.sum(case((SchoolStudentResult.status == 'released', 1), else_=0)),
                   func.sum(case((SchoolStudentResult.status != 'released', 1), else_=0)),
                   func.max(SchoolStudentResult.released_at))
            .select_from(SchoolStudentResult)
            .outerjoin(SchoolAssessment, SchoolAssessment.id == SchoolStudentResult.assessment_id)
            .where(SchoolStudentResult.student_id.in_(student_ids),
                   SchoolStudentResult.session_id.is_not(None),
                   or_(SchoolAssessment.assessment_type.is_(None), SchoolAssessment.assessment_type != 'practice'))
            .group_by(SchoolStudentResult.student_id, SchoolStudentResult.session_id, term_of))
    if session_id is not None:
        stmt = stmt.where(SchoolStudentResult.session_id == session_id)
    if term is not None:
        stmt = stmt.where(term_of == term)
    return {(sid, sess, t): {'released': int(released or 0), 'pending': int(pending or 0), 'released_at': at}
            for sid, sess, t, released, pending, at in tuples(stmt)}


def _is_ready(counts):
    return bool(counts) and counts['released'] > 0 and counts['pending'] == 0


def student_periods(student_id, ready_only=True):
    """The terms this student has results in, newest first.

    Each is ``{'session_id', 'session_name', 'term', 'slug', 'ready', 'released', 'pending'}``. With
    ``ready_only`` (what a student or parent sees) only terms whose card is ready are returned.
    """
    # A release date that has passed releases the approved results. Whoever asks (a student's dashboard, a parent's
    # page for a child) must see the card the moment the date passes, not only after someone else has looked.
    from app import _release_due_school_results  # deferred: app imports this module
    _release_due_school_results()
    counts = _official_result_counts([student_id])
    if not counts:
        return []
    names = {sid: name for sid, name in tuples(select(AcademicSession.id, AcademicSession.name)
                                               .where(AcademicSession.id.in_({k[1] for k in counts})))}
    order = {t: i for i, t in enumerate(_KNOWN_TERMS)}
    periods = []
    for (sid, session_id, term), c in counts.items():
        ready = _is_ready(c)
        if ready_only and not ready:
            continue
        periods.append({'session_id': session_id, 'session_name': names.get(session_id, '—'), 'term': term,
                        'slug': term_slug(term), 'ready': ready, 'released': c['released'], 'pending': c['pending']})
    periods.sort(key=lambda p: (-p['session_id'], order.get(p['term'], 9)))
    return periods


# ---------------------------------------------------------------------------------------------
# The school's report card settings: the head's title, name and signature, and when term begins.
def report_settings():
    """``{'head_title', 'head_name', 'head_signature' (a stored path or ''), 'next_term_begins'}``."""
    school_id = _primary_school_id()
    keys = (SETTING_HEAD_TITLE, SETTING_HEAD_NAME, SETTING_HEAD_SIGNATURE, SETTING_NEXT_TERM)
    stored = {}
    if school_id:
        stored = dict(tuples(select(SchoolSetting.setting_key, SchoolSetting.setting_value)
                             .where(SchoolSetting.school_id == school_id, SchoolSetting.setting_key.in_(keys))))
    return {'head_title': (stored.get(SETTING_HEAD_TITLE) or '').strip(),
            'head_name': (stored.get(SETTING_HEAD_NAME) or '').strip(),
            'head_signature': (stored.get(SETTING_HEAD_SIGNATURE) or '').strip(),
            'next_term_begins': (stored.get(SETTING_NEXT_TERM) or '').strip()}


def save_report_settings(values, admin_id):
    """Save some of the report card settings. ``values`` maps a setting key to its new text ('' clears it).
    Does not commit."""
    school_id = _primary_school_id()
    if not school_id:
        raise ValueError('No active school is configured.')
    now = datetime.now(timezone.utc).isoformat()
    for key, value in values.items():
        row = db.session.scalars(select(SchoolSetting).where(
            SchoolSetting.school_id == school_id, SchoolSetting.setting_key == key)).first()
        if row:
            row.setting_value = value
            row.updated_at = now
            row.updated_by = admin_id
        else:
            db.session.add(SchoolSetting(school_id=school_id, setting_key=key, setting_value=value,
                                         updated_at=now, updated_by=admin_id))


def _file(stored_path):
    """A stored upload value that actually points at a real file inside this school's own
    folder, or None."""
    return stored_path if stored_path and upload_exists(stored_path) else None


# ---------------------------------------------------------------------------------------------
# The class: everyone's term, so a card can say how the class did.
def class_term_stats(class_id, session_id, term):
    """How each student with a ready card in one class did, and the class averages.

    ``{'students': {student_id: {'subjects': {subject_id: report}, 'total', 'total_max', 'percentage'}},
    'subject_average': {subject_id: float}, 'class_average': float or None, 'class_size': int}``.

    Only students whose own card is ready are counted, so the class average never depends on a
    result that has not been released yet. Worked out for the whole class in a few queries.
    """
    enrolled = [sid for (sid,) in tuples(select(StudentEnrolment.student_id).join(Student, Student.id == StudentEnrolment.student_id)
                                         .where(StudentEnrolment.class_id == class_id, StudentEnrolment.session_id == session_id,
                                                StudentEnrolment.active == 1, Student.active == 1))]
    counts = _official_result_counts(enrolled, session_id, term)
    ready = [sid for sid in enrolled if _is_ready(counts.get((sid, session_id, term)))]
    reports = _term_reports_bulk(ready, session_id, term) if ready else {}
    students = {}
    for (sid, subject_id), report in reports.items():
        if not report['has_data']:
            continue
        entry = students.setdefault(sid, {'subjects': {}, 'total': 0.0, 'total_max': 0.0})
        entry['subjects'][subject_id] = report
        entry['total'] += report['total_score']
        entry['total_max'] += report['total_max']
    for entry in students.values():
        entry['total'] = round(entry['total'], 2)
        entry['percentage'] = round(entry['total'] / entry['total_max'] * 100, 2) if entry['total_max'] else 0.0
    per_subject = {}
    for entry in students.values():
        for subject_id, report in entry['subjects'].items():
            per_subject.setdefault(subject_id, []).append(report['total_score'])
    percentages = [e['percentage'] for e in students.values() if e['total_max']]
    return {'students': students,
            'subject_average': {s: round(sum(v) / len(v), 2) for s, v in per_subject.items()},
            'class_average': round(sum(percentages) / len(percentages), 2) if percentages else None,
            'class_size': len(students)}


# ---------------------------------------------------------------------------------------------
# The card itself.
def _issued_on(latest_release):
    try:
        moment = datetime.fromisoformat(str(latest_release).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        moment = datetime.now(timezone.utc)
    return f'{moment.day} {moment.strftime("%B %Y")}'


def build_cards(student_ids, session_id, term):
    """The report cards that are ready, for these students in one term. Students whose card is not ready are left out.

    Each card is a dictionary in the shape core/report_card_pdf.py documents. Class averages are
    worked out once per class, not once per student.
    """
    student_ids = list(dict.fromkeys(student_ids))
    if not student_ids:
        return []
    counts = _official_result_counts(student_ids, session_id, term)
    ready_ids = [sid for sid in student_ids if _is_ready(counts.get((sid, session_id, term)))]
    if not ready_ids:
        return []

    session_name = db.session.scalar(select(AcademicSession.name).where(AcademicSession.id == session_id)) or ''
    people = {row.Student.id: row for row in db.session.execute(
        select(Student, StudentEnrolment.class_id, SchoolClass.name.label('class_name'))
        .outerjoin(StudentEnrolment, (StudentEnrolment.student_id == Student.id) & (StudentEnrolment.session_id == session_id))
        .outerjoin(SchoolClass, SchoolClass.id == StudentEnrolment.class_id)
        .where(Student.id.in_(ready_ids)))}
    comments = {c.student_id: c for c in db.session.scalars(select(ReportCardComment).where(
        ReportCardComment.student_id.in_(ready_ids), ReportCardComment.session_id == session_id,
        ReportCardComment.term == term))}
    trait_rows = {t.student_id: _parse_ratings(t.ratings) for t in db.session.scalars(select(ReportCardTrait).where(
        ReportCardTrait.student_id.in_(ready_ids), ReportCardTrait.session_id == session_id,
        ReportCardTrait.term == term))}
    authors = {a.id: a for a in db.session.scalars(select(Admin).where(
        Admin.id.in_({c.author_admin_id for c in comments.values() if c.author_admin_id})))} if comments else {}
    subject_names = {sid: name for sid, name in tuples(select(SchoolSubject.id, SchoolSubject.name))}

    brand = school_brand()
    school = {'name': brand.get('name') or '', 'motto': brand.get('motto') or '', 'address': brand.get('address') or '',
              'phone': brand.get('phone') or '', 'email': brand.get('email') or '',
              'logo_path': _file(brand.get('logo_path')), 'primary': brand.get('primary') or '',
              'accent': brand.get('accent') or ''}
    settings = report_settings()
    head = {'title': settings['head_title'], 'name': settings['head_name'], 'signature_path': _file(settings['head_signature'])}
    key = grading_key()

    stats_by_class = {}
    cards = []
    for student_id in ready_ids:
        person = people.get(student_id)
        if person is None:
            continue
        student = person.Student
        class_id = person.class_id
        if class_id not in stats_by_class:
            stats_by_class[class_id] = class_term_stats(class_id, session_id, term) if class_id else None
        stats = stats_by_class[class_id]
        mine = (stats or {}).get('students', {}).get(student_id)
        if mine is None:
            # Not in the class figures (no class that session): work this student's own out.
            reports = _term_reports_bulk([student_id], session_id, term)
            subjects_reports = {s: r for (_, s), r in reports.items() if r['has_data']}
            total = round(sum(r['total_score'] for r in subjects_reports.values()), 2)
            total_max = round(sum(r['total_max'] for r in subjects_reports.values()), 2)
            mine = {'subjects': subjects_reports, 'total': total, 'total_max': total_max,
                    'percentage': round(total / total_max * 100, 2) if total_max else 0.0}
        subject_average = (stats or {}).get('subject_average', {})

        rows = []
        for subject_id, report in mine['subjects'].items():
            pct = report['total_score'] / report['total_max'] * 100 if report['total_max'] else 0
            letter, remark = grade_for(pct)
            rows.append({'name': subject_names.get(subject_id, '—'),
                         'test': report['test_score'], 'assignment': report['assignment_score'],
                         'project': report['project_score'], 'ca': report['ca_score'], 'ca_max': report['ca_max'],
                         'exam': report['exam_score'], 'exam_max': report['exam_max'],
                         'total': report['total_score'], 'total_max': report['total_max'],
                         'grade': letter, 'remark': remark, 'class_average': subject_average.get(subject_id)})
        rows.sort(key=lambda r: r['name'])
        letter, remark = grade_for(mine['percentage'])

        comment = comments.get(student_id)
        comment_block = None
        if comment and comment.comment.strip():
            author = authors.get(comment.author_admin_id)
            comment_block = {'text': comment.comment.strip(),
                             'teacher_name': author.display_name if author else '',
                             'teacher_signature_path': _file(author.signature_path) if author else None}

        ratings = trait_rows.get(student_id) or {}
        # Only shown once the school has rated at least one trait for this student: a school that
        # never uses this stays with the card it always had, with no empty grid appearing on it.
        # 'entries', never 'items': a dict key called 'items' collides with Python's own dict.items()
        # method when Jinja resolves 'group.items' in the template.
        traits_block = [{'name': group_name, 'entries': [
                            {'label': label, 'rating_label': trait_rating_label(ratings.get(key))}
                            for key, label in items]}
                        for group_name, items in TRAIT_GROUPS] if ratings else None

        full_name = ' '.join(p for p in (student.first_name, student.middle_name, student.last_name) if p and str(p).strip())
        cards.append({
            'school': school,
            'title': term_title(term), 'term': term, 'session': session_name,
            'student': {'id': student_id, 'name': full_name,
                        'admission_no': student.student_number or student.admission_no or '',
                        'class_name': person.class_name or '', 'gender': student.gender or '',
                        'photo_path': _file(student.photo_path)},
            'subjects': rows,
            'summary': {'total': mine['total'], 'total_max': mine['total_max'], 'percentage': mine['percentage'],
                        'grade': letter, 'remark': remark,
                        'class_average_percentage': (stats or {}).get('class_average'),
                        'class_size': (stats or {}).get('class_size') or 0, 'subjects_count': len(rows)},
            'comment': comment_block,
            'traits': traits_block,
            'head': head,
            'grading_key': key,
            'issued_on': _issued_on(counts[(student_id, session_id, term)]['released_at']),
            'next_term_begins': settings['next_term_begins'],
        })
    cards.sort(key=lambda c: c['student']['name'].casefold())
    return cards


def build_card(student_id, session_id, term):
    """One student's ready report card, or None if it is not ready (or the student has none)."""
    cards = build_cards([student_id], session_id, term)
    return cards[0] if cards else None


def class_overview(class_id, session_id, term):
    """What the staff page shows for a class: each student, whether the card is ready, and the comment.

    ``[{'student_id', 'name', 'admission_no', 'state': 'ready' | 'waiting' | 'none', 'pending',
    'comment': text or '', 'comment_by': name or ''}]`` ordered by name.
    """
    rows = db.session.execute(
        select(Student.id, Student.first_name, Student.middle_name, Student.last_name, Student.admission_no,
               Student.student_number)
        .join(StudentEnrolment, StudentEnrolment.student_id == Student.id)
        .where(StudentEnrolment.class_id == class_id, StudentEnrolment.session_id == session_id,
               StudentEnrolment.active == 1, Student.active == 1)).all()
    ids = [r.id for r in rows]
    counts = _official_result_counts(ids, session_id, term)
    comments = {c.student_id: c for c in db.session.scalars(select(ReportCardComment).where(
        ReportCardComment.student_id.in_(ids), ReportCardComment.session_id == session_id,
        ReportCardComment.term == term))} if ids else {}
    authors = {a.id: a.display_name for a in db.session.scalars(select(Admin).where(
        Admin.id.in_({c.author_admin_id for c in comments.values() if c.author_admin_id})))} if comments else {}
    out = []
    for r in rows:
        c = counts.get((r.id, session_id, term))
        state = 'none' if not c else ('ready' if _is_ready(c) else 'waiting')
        comment = comments.get(r.id)
        out.append({'student_id': r.id,
                    'name': ' '.join(p for p in (r.first_name, r.middle_name, r.last_name) if p and str(p).strip()),
                    'admission_no': r.student_number or r.admission_no or '',
                    'state': state, 'pending': c['pending'] if c else 0,
                    'comment': comment.comment if comment else '',
                    'comment_by': authors.get(comment.author_admin_id, '') if comment else ''})
    out.sort(key=lambda x: x['name'].casefold())
    return out


def traits_overview(class_id, session_id, term):
    """What the traits page shows for a class: each enrolled student and their current ratings.

    ``[{'student_id', 'name', 'admission_no', 'ratings': {key: 1..5}, 'rated_by': name or ''}]``
    ordered by name. A student with no ``ReportCardTrait`` row yet simply has an empty ``ratings``.
    """
    rows = db.session.execute(
        select(Student.id, Student.first_name, Student.middle_name, Student.last_name, Student.admission_no,
               Student.student_number)
        .join(StudentEnrolment, StudentEnrolment.student_id == Student.id)
        .where(StudentEnrolment.class_id == class_id, StudentEnrolment.session_id == session_id,
               StudentEnrolment.active == 1, Student.active == 1)).all()
    ids = [r.id for r in rows]
    traits = {t.student_id: t for t in db.session.scalars(select(ReportCardTrait).where(
        ReportCardTrait.student_id.in_(ids), ReportCardTrait.session_id == session_id,
        ReportCardTrait.term == term))} if ids else {}
    authors = {a.id: a.display_name for a in db.session.scalars(select(Admin).where(
        Admin.id.in_({t.author_admin_id for t in traits.values() if t.author_admin_id})))} if traits else {}
    out = []
    for r in rows:
        t = traits.get(r.id)
        out.append({'student_id': r.id,
                    'name': ' '.join(p for p in (r.first_name, r.middle_name, r.last_name) if p and str(p).strip()),
                    'admission_no': r.student_number or r.admission_no or '',
                    'ratings': _parse_ratings(t.ratings) if t else {},
                    'rated_by': authors.get(t.author_admin_id, '') if t else ''})
    out.sort(key=lambda x: x['name'].casefold())
    return out


def card_for_web(card):
    """The card plus the pictures it needs as data, ready for the web page.

    The page embeds the logo, photograph and signatures instead of linking to them, so the address
    of a signature or a child's photograph is never put in a page, and nothing depends on a file
    URL that has to be served.
    """
    from core.entrance import _result_file_data_uri  # deferred: core.entrance imports the app

    def picture(path):
        return _result_file_data_uri(path) if path else ''

    comment = card.get('comment') or {}
    return {**card, 'web': {'logo': picture(card['school']['logo_path']), 'photo': picture(card['student']['photo_path']),
                            'teacher_signature': picture(comment.get('teacher_signature_path')),
                            'head_signature': picture(card['head']['signature_path'])}}
