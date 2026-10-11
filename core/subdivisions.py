"""Class subdivisions and class transfers: the rules, in one place.

A JSS or SSS class may be split into subdivisions (models/subdivisions.py), each with its own class teacher. The class
stays one class for its subjects, subject teachers, timetable, fees and results; a subdivision groups its students for
their class teacher only.

* **Setting up** (blueprints/school/subdivisions.py, under Settings): the School Admin adds, renames and removes a
  class's subdivisions, and locks the set-up when it is right. While locked nobody can change it; the School Admin
  unlocks it to change it again. Saving spreads the class's students who are not in a subdivision yet across them.
* **Placing a student**: every enrolment that is new, that moves to another class, or whose student changes
  department, is placed by ``place()`` - in the subdivision of its class with the fewest students this session (so
  students are dealt out in turn, round robin), only among those of the student's department (or with no department)
  in SSS. It runs on every save (``_place_on_flush``), so admitting from the entrance examination, adding or importing
  students, promotion and history edits all place students without each knowing about it.
* **Who sees whom**: a class teacher of one or more subdivisions (and of no whole class, and teaching no subject in the
  class) reaches only those subdivisions' students: ``teacher_subdivisions()`` says which; everyone else is unaffected.
* **Transfers** (blueprints/school/transfers.py): the class teacher of a student's subdivision asks to move them to
  another subdivision of the class, or to another department, or both; nothing changes until the class teacher of the
  subdivision they would join approves (or the School Admin or a head teacher does).
"""

from datetime import datetime, timezone

from sqlalchemy import event, func, inspect as sa_inspect, select

from core.school_structure import clean_department, is_secondary, is_senior
from models import (
    ClassSubdivision, ClassTransfer, School, SchoolClass, SchoolSetting, Student, StudentEnrolment, db,
)

LOCK_KEY = 'class_subdivisions_locked'


# ---------------------------------------------------------------- the set-up and its lock
def _setting(key):
    row = db.session.scalars(select(SchoolSetting).where(SchoolSetting.setting_key == key)).first()
    return row


def lock_state():
    """{'locked': bool, 'by': admin id or None, 'at': iso time or None}."""
    row = _setting(LOCK_KEY)
    if not row or not row.setting_value:
        return {'locked': False, 'by': None, 'at': None}
    return {'locked': True, 'by': row.updated_by, 'at': row.updated_at}


def set_lock(locked, admin_id):
    row = _setting(LOCK_KEY)
    now = datetime.now(timezone.utc).isoformat()
    if row is None:
        school_id = db.session.scalar(select(School.id).where(School.active == 1).limit(1))
        row = SchoolSetting(school_id=school_id, setting_key=LOCK_KEY)
        db.session.add(row)
    row.setting_value = '1' if locked else ''
    row.updated_at = now
    row.updated_by = admin_id


def secondary_classes():
    """The active JSS and SSS classes, in order: the ones a school may subdivide."""
    rows = db.session.scalars(select(SchoolClass).where(SchoolClass.active == 1).order_by(SchoolClass.level_order)).all()
    return [c for c in rows if is_secondary(c.stage, c.name)]


def subdivisions_of(class_id, include_inactive=False):
    stmt = select(ClassSubdivision).where(ClassSubdivision.class_id == class_id)
    if not include_inactive:
        stmt = stmt.where(ClassSubdivision.active == 1)
    return db.session.scalars(stmt.order_by(ClassSubdivision.position, ClassSubdivision.id)).all()


def all_subdivisions():
    """{class_id: [ClassSubdivision, ...]} for every class with active subdivisions."""
    out = {}
    for s in db.session.scalars(select(ClassSubdivision).where(ClassSubdivision.active == 1)
                                .order_by(ClassSubdivision.class_id, ClassSubdivision.position, ClassSubdivision.id)):
        out.setdefault(s.class_id, []).append(s)
    return out


def counts(session_id):
    """{subdivision_id: number of active students in it this session} (and None: students of subdivided classes
    in no subdivision)."""
    rows = db.session.execute(select(StudentEnrolment.subdivision_id, func.count())
                              .join(Student, Student.id == StudentEnrolment.student_id)
                              .where(StudentEnrolment.session_id == session_id, StudentEnrolment.active == 1,
                                     Student.active == 1)
                              .group_by(StudentEnrolment.subdivision_id)).all()
    return {sid: n for sid, n in rows}


def label(subdivision, class_name=None):
    """'JSS 1A', 'SS 1A (Science)': how a subdivision is named on a page."""
    if subdivision is None:
        return class_name or ''
    text = subdivision.name
    if subdivision.department:
        text += f' ({subdivision.department})'
    return text


# ---------------------------------------------------------------- placing a student
def _eligible(subdivisions, department, senior):
    """The subdivisions a student may be placed in: in SSS those of their department and those with none (a student
    without a department may go to any); in JSS every one."""
    if not senior or not department:
        return list(subdivisions)
    return [s for s in subdivisions if s.department in (None, '', department)]


def choose(class_id, session_id, department, pending=None, senior=None):
    """The subdivision a student of this department joining this class should go to: the eligible one with the
    fewest students this session, the earlier one on a tie - so a class fills them in turn. ``pending`` counts
    placements made in this same save that the database cannot see yet. None when the class is not subdivided."""
    subdivisions = subdivisions_of(class_id)
    if not subdivisions:
        return None
    if senior is None:
        cls = db.session.get(SchoolClass, class_id)
        senior = bool(cls and is_senior(cls.stage, cls.name))
    eligible = _eligible(subdivisions, clean_department(department), senior)
    if not eligible:
        return None
    with db.session.no_autoflush:
        have = dict(db.session.execute(
            select(StudentEnrolment.subdivision_id, func.count())
            .join(Student, Student.id == StudentEnrolment.student_id)
            .where(StudentEnrolment.session_id == session_id, StudentEnrolment.active == 1, Student.active == 1,
                   StudentEnrolment.subdivision_id.in_([s.id for s in eligible]))
            .group_by(StudentEnrolment.subdivision_id)).all())
    pending = pending or {}
    return min(eligible, key=lambda s: (have.get(s.id, 0) + pending.get(s.id, 0), s.position, s.id))


def spread(class_id, session_id):
    """Place every student of the class this session who is not in a subdivision yet (or whose subdivision is no
    longer active, or not of their department), in turn. Returns how many were placed."""
    subdivisions = subdivisions_of(class_id)
    if not subdivisions:
        # Not (or no longer) split: nobody in the class keeps a subdivision.
        stale = db.session.scalars(select(StudentEnrolment).where(
            StudentEnrolment.class_id == class_id, StudentEnrolment.session_id == session_id,
            StudentEnrolment.subdivision_id.is_not(None))).all()
        for enrolment in stale:
            enrolment.subdivision_id = None
        return 0
    active_ids = {s.id for s in subdivisions}
    by_id = {s.id: s for s in subdivisions}
    cls = db.session.get(SchoolClass, class_id)
    senior = bool(cls and is_senior(cls.stage, cls.name))
    rows = db.session.execute(select(StudentEnrolment, Student.department)
                              .join(Student, Student.id == StudentEnrolment.student_id)
                              .where(StudentEnrolment.class_id == class_id, StudentEnrolment.session_id == session_id,
                                     StudentEnrolment.active == 1, Student.active == 1)
                              .order_by(Student.last_name, Student.first_name, Student.id)).all()
    pending, placed = {}, 0
    for enrolment, department in rows:
        current = by_id.get(enrolment.subdivision_id) if enrolment.subdivision_id in active_ids else None
        if current and current in _eligible([current], clean_department(department), senior):
            continue
        chosen = choose(class_id, session_id, department, pending, senior)
        new_id = chosen.id if chosen else None
        if new_id != enrolment.subdivision_id:
            if enrolment.subdivision_id:
                # The database still counts them in the one they leave.
                pending[enrolment.subdivision_id] = pending.get(enrolment.subdivision_id, 0) - 1
            enrolment.subdivision_id = new_id
            if new_id:
                pending[new_id] = pending.get(new_id, 0) + 1
            placed += 1
    return placed


@event.listens_for(db.session, 'before_flush')
def _place_on_flush(session, _context, _instances):
    """Every save: place a new enrolment, one moved to another class, and the enrolments of a student whose department
    changed (when their subdivision is not of the new department), in their class's subdivisions."""
    pending = {}
    todo = []
    for obj in list(session.new) + list(session.dirty):
        if isinstance(obj, StudentEnrolment):
            state = sa_inspect(obj)
            moved = obj in session.new or state.attrs.class_id.history.has_changes()
            if moved and not state.attrs.subdivision_id.history.has_changes():
                todo.append((obj, True))
        elif isinstance(obj, Student) and obj not in session.new:
            if sa_inspect(obj).attrs.department.history.has_changes():
                with session.no_autoflush:
                    for enrolment in session.scalars(select(StudentEnrolment).where(
                            StudentEnrolment.student_id == obj.id, StudentEnrolment.active == 1)).all():
                        todo.append((enrolment, False))
    for enrolment, always in todo:
        if not enrolment.class_id or not enrolment.session_id:
            continue
        with session.no_autoflush:
            student = session.get(Student, enrolment.student_id) if enrolment.student_id else None
            department = student.department if student is not None else None
            if not always and enrolment.subdivision_id:
                current = session.get(ClassSubdivision, enrolment.subdivision_id)
                if current and current.active and current.department in (None, '', clean_department(department)):
                    continue
            chosen = choose(enrolment.class_id, enrolment.session_id, department, pending)
        enrolment.subdivision_id = chosen.id if chosen else None
        if chosen:
            pending[chosen.id] = pending.get(chosen.id, 0) + 1


# ---------------------------------------------------------------- who sees whom
def teacher_subdivisions(admin_id, class_id):
    """The subdivisions of this class whose students this member of staff reaches as class teacher, or None when
    they are not limited by subdivision (the School Admin, a head teacher, the class teacher of the whole class, a
    subject teacher of the class, or anyone without teaching duties: the usual class rules apply to them).

    Limited means: their duties in the class are class-teacher duties of particular subdivisions, and nothing else."""
    from core.security import teaching_duties
    if not admin_id or not class_id:
        return None
    duties = [d for d in teaching_duties(admin_id) if d['class_id'] == class_id]
    if not duties:
        return None
    if any(d['subject_id'] is not None or not d.get('subdivision_id') for d in duties):
        return None
    return {d['subdivision_id'] for d in duties}


def current_limit(class_id):
    """teacher_subdivisions() for the member of staff signed in to this request (None outside a staff request)."""
    from flask import has_request_context, session as login
    if not has_request_context() or not login.get('admin_id'):
        return None
    return teacher_subdivisions(login.get('admin_id'), class_id)


def reaches(admin_id, class_id, subdivision_id):
    """Whether a member of staff reaches a student of this class in this subdivision (see teacher_subdivisions)."""
    limited = teacher_subdivisions(admin_id, class_id)
    return limited is None or subdivision_id in limited


def enrolment_of(student_id, session_id):
    return db.session.scalars(select(StudentEnrolment).where(
        StudentEnrolment.student_id == student_id, StudentEnrolment.session_id == session_id,
        StudentEnrolment.active == 1)).first()


def filter_rows(admin_id, rows, class_key='class_id', subdivision_key='subdivision_id'):
    """Keep the student rows this member of staff reaches, by each row's class and subdivision."""
    cache = {}
    out = []
    for r in rows:
        cid = r.get(class_key)
        if cid not in cache:
            cache[cid] = teacher_subdivisions(admin_id, cid)
        limited = cache[cid]
        if limited is None or r.get(subdivision_key) in limited:
            out.append(r)
    return out


def subdivision_class_teachers(subdivision_id):
    """Admin ids of the class teachers of this subdivision."""
    from models import TeachingDuty
    return [aid for (aid,) in db.session.execute(select(TeachingDuty.admin_id).where(
        TeachingDuty.subdivision_id == subdivision_id, TeachingDuty.subject_id.is_(None))).all()]


def class_teachers_of_class(class_id):
    """Admin ids of the class teachers of the whole class (no subdivision)."""
    from models import TeachingDuty
    return [aid for (aid,) in db.session.execute(select(TeachingDuty.admin_id).where(
        TeachingDuty.class_id == class_id, TeachingDuty.subject_id.is_(None), TeachingDuty.subdivision_id.is_(None))).all()]


# ---------------------------------------------------------------- transfers
def pending_transfer(student_id):
    return db.session.scalars(select(ClassTransfer).where(
        ClassTransfer.student_id == student_id, ClassTransfer.status == 'pending')).first()


def approvers(transfer):
    """Who may approve a transfer besides the School Admin and head teachers: the class teacher of the subdivision the
    student would join, or, when it stays in the same subdivision (a department change only) or the class has none,
    the class teacher of the whole class."""
    if transfer.to_subdivision_id and transfer.to_subdivision_id != transfer.from_subdivision_id:
        return subdivision_class_teachers(transfer.to_subdivision_id)
    if transfer.to_subdivision_id:
        return subdivision_class_teachers(transfer.to_subdivision_id) or class_teachers_of_class(transfer.class_id)
    return class_teachers_of_class(transfer.class_id)


def apply_transfer(transfer):
    """Make an approved transfer true: the student's subdivision this session, and their department."""
    enrolment = enrolment_of(transfer.student_id, transfer.session_id)
    student = db.session.get(Student, transfer.student_id)
    if enrolment is None or student is None or enrolment.class_id != transfer.class_id:
        raise ValueError('The student is no longer in that class this session, so the transfer cannot be made.')
    if transfer.to_department and transfer.to_department != student.department:
        student.department = transfer.to_department
    # Set after the department, so the save keeps this subdivision rather than placing the student again.
    enrolment.subdivision_id = transfer.to_subdivision_id
