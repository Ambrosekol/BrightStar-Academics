"""How a Nigerian school is organised, in one place: who sits above whom, which classes are early
years (Crèche and Nursery), and the senior secondary departments.

Staff levels, top to bottom (the School Admin - the proprietor or proprietress - sits above them all):

    executive        Bursar, admissions officer, secretary, HR, librarian and other office roles
    head_teacher     Head teacher or principal: every class and subject
    class_teacher    In charge of one class: its register, report cards and the release of its results
    subject_teacher  Teaches a subject in a class, records its scores and sends them to the class teacher

Early-years pupils (Crèche and Nursery) do everything on paper: no sign-in, no CBT, no online
assignments. Their teachers record their scores, and parents see fees and report cards.

Senior secondary students (SSS 1-3) each belong to one department. A subject can be taken by one,
several or all departments (SchoolSubject.departments).
"""

from flask import g, has_request_context
from sqlalchemy import select

from models import SchoolClass, db


STAFF_LEVELS = [
    ('executive', 'Executive & office', 'Bursar, admissions, secretary, HR, librarian and other office roles.'),
    ('head_teacher', 'Head teacher', 'Oversees every class and subject: checks, approves and releases any result.'),
    ('class_teacher', 'Class teacher', 'In charge of a class: its register, report card comments and releasing its results.'),
    ('subject_teacher', 'Subject teacher', 'Teaches a subject in a class: records its scores and sends them to the class teacher.'),
]
LEVEL_CODES = [code for code, _label, _help in STAFF_LEVELS]
LEVEL_LABELS = {code: label for code, label, _help in STAFF_LEVELS}
TOP_LEVEL_LABEL = 'Proprietor / School Admin'
# Lower is more senior. The School Admin is 0.
LEVEL_RANK = {code: i + 1 for i, code in enumerate(LEVEL_CODES)}
TEACHING_LEVELS = ('class_teacher', 'subject_teacher')


def level_or_default(level):
    return level if level in LEVEL_RANK else 'executive'


DEPARTMENTS = ('Science', 'Art', 'Commercial')

_EARLY_YEARS_WORDS = ('creche', 'crèche', 'nursery', 'daycare', 'toddler', 'kindergarten',
                      'reception', 'playgroup', 'pre-school', 'preschool', 'early years')


def is_early_years(stage=None, name=None):
    """A Crèche or Nursery class (or a school's own name for one, such as Kindergarten)."""
    text = f'{stage or ""} {name or ""}'.casefold()
    return any(word in text for word in _EARLY_YEARS_WORDS)


def is_senior(stage=None, name=None):
    """A senior secondary class (SSS 1-3, or SS 1-3), where students belong to a department."""
    stage_text = (stage or '').strip().casefold()
    name_text = (name or '').strip().upper()
    return (stage_text in ('sss', 'ss', 'senior secondary', 'senior')
            or name_text.startswith('SSS') or name_text.startswith('SS ') or name_text.startswith('SS1')
            or name_text.startswith('SS2') or name_text.startswith('SS3'))


def _class_kinds():
    """{class_id: (early_years, senior)} for every class, read once per request."""
    def load():
        return {cid: (is_early_years(stage, name), is_senior(stage, name))
                for cid, stage, name in db.session.execute(
                    select(SchoolClass.id, SchoolClass.stage, SchoolClass.name)).all()}
    if not has_request_context():
        return load()
    if '_class_kinds' not in g:
        g._class_kinds = load()
    return g._class_kinds


def class_is_early_years(class_id):
    return bool(class_id) and _class_kinds().get(int(class_id), (False, False))[0]


def class_is_senior(class_id):
    return bool(class_id) and _class_kinds().get(int(class_id), (False, False))[1]


def early_years_class_ids():
    return {cid for cid, (early, _senior) in _class_kinds().items() if early}


def parse_departments(value):
    """'Science,Art' -> ['Science', 'Art'], in the standard order, ignoring anything unknown."""
    chosen = {part.strip().casefold() for part in str(value or '').split(',') if part.strip()}
    return [d for d in DEPARTMENTS if d.casefold() in chosen]


def format_departments(values):
    """The stored form of a list of departments: '' when none (or all) are chosen."""
    chosen = parse_departments(','.join(values or []))
    return '' if len(chosen) in (0, len(DEPARTMENTS)) else ','.join(chosen)


def clean_department(value):
    return next((d for d in DEPARTMENTS if d.casefold() == str(value or '').strip().casefold()), None)


def takes_subject(student_department, subject_departments):
    """Whether a senior student takes a subject. A subject open to every department, and a student
    whose department has not been set yet, always match, so nobody is hidden by missing data."""
    offered = parse_departments(subject_departments)
    return not offered or not student_department or student_department in offered


def subject_open_to(department, subject_departments_column):
    """A SQL condition: the subject is taken by a student of ``department`` (always true without one)."""
    from sqlalchemy import literal, or_, true
    if not department:
        return true()
    return or_(subject_departments_column.is_(None), subject_departments_column == '',
               (literal(',') + subject_departments_column + literal(',')).contains(f',{department},'))


def student_department_filter(student, subject_departments_column):
    """For a senior student, only subjects their department takes; anyone else, every subject.
    ``student`` is a row or dict with 'class_id' and 'department'."""
    from sqlalchemy import true
    if not student or not class_is_senior(student.get('class_id')):
        return true()
    return subject_open_to(student.get('department'), subject_departments_column)


def student_current_class_id(student_id):
    """The class of a student's current enrolment (the current session's, else their latest)."""
    from sqlalchemy import case
    from models import AcademicSession, StudentEnrolment
    return db.session.execute(
        select(StudentEnrolment.class_id)
        .outerjoin(AcademicSession, AcademicSession.id == StudentEnrolment.session_id)
        .where(StudentEnrolment.student_id == student_id, StudentEnrolment.active == 1)
        .order_by(case((AcademicSession.is_current == 1, 0), else_=1), StudentEnrolment.id.desc())
        .limit(1)).scalar()


def student_is_early_years(student_id):
    """A Crèche or Nursery pupil: no sign-in and no online work; parents see fees and report cards."""
    return class_is_early_years(student_current_class_id(student_id))
