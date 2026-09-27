"""Attendance: the daily register, and the summaries built from it.

A day is recorded once per student, not per subject: whichever class teacher takes the register for
that class on that date sets one status for the whole day. Nothing is written for a day nobody has
taken the register for, so a summary only ever counts days the school actually recorded, never
assumes a student was present by default.
"""

from datetime import datetime, timezone

from sqlalchemy import case, func, select

from models import AcademicSession, Admin, AttendanceRecord, SchoolClass, Student, StudentEnrolment, db

STATUSES = (('present', 'Present'), ('late', 'Late'), ('absent', 'Absent'), ('excused', 'Excused'))
_STATUS_KEYS = {key for key, _ in STATUSES}
_STATUS_LABELS = dict(STATUSES)


def status_label(status):
    return _STATUS_LABELS.get(status, '')


def class_roster(class_id, session_id):
    """Every actively enrolled student of one class in one session, ordered by name."""
    rows = db.session.execute(
        select(Student.id, Student.first_name, Student.middle_name, Student.last_name, Student.admission_no,
               Student.student_number)
        .join(StudentEnrolment, StudentEnrolment.student_id == Student.id)
        .where(StudentEnrolment.class_id == class_id, StudentEnrolment.session_id == session_id,
               StudentEnrolment.active == 1, Student.active == 1)).all()
    out = [{'student_id': r.id,
           'name': ' '.join(p for p in (r.first_name, r.middle_name, r.last_name) if p and str(p).strip()),
           'admission_no': r.student_number or r.admission_no or ''} for r in rows]
    out.sort(key=lambda x: x['name'].casefold())
    return out


def day_register(class_id, session_id, date):
    """The roster for a class and session, each student's status on ``date`` (or '' if not yet marked),
    and who last marked it. ``[{'student_id', 'name', 'admission_no', 'status', 'marked_by'}]``."""
    roster = class_roster(class_id, session_id)
    ids = [r['student_id'] for r in roster]
    marks = {m.student_id: m for m in db.session.scalars(select(AttendanceRecord).where(
        AttendanceRecord.student_id.in_(ids or [0]), AttendanceRecord.date == date))}
    authors = {a.id: a.display_name for a in db.session.scalars(select(Admin).where(
        Admin.id.in_({m.marked_by_admin_id for m in marks.values() if m.marked_by_admin_id})))} if marks else {}
    for row in roster:
        mark = marks.get(row['student_id'])
        row['status'] = mark.status if mark else ''
        row['marked_by'] = authors.get(mark.marked_by_admin_id, '') if mark else ''
    return roster


def save_day_register(class_id, session_id, term, date, statuses, admin_id):
    """Save one day's register. ``statuses`` maps a student id to a status ('' clears the mark).

    Only students actually enrolled in this class and session can be marked, so a tampered student id
    is silently ignored. Returns ``(saved, cleared)``. Does not commit.
    """
    roster_ids = {r['student_id'] for r in class_roster(class_id, session_id)}
    existing = {m.student_id: m for m in db.session.scalars(select(AttendanceRecord).where(
        AttendanceRecord.student_id.in_(list(roster_ids) or [0]), AttendanceRecord.date == date))}
    now = datetime.now(timezone.utc).isoformat()
    saved = cleared = 0
    for student_id, status in statuses.items():
        if student_id not in roster_ids:
            continue
        status = status if status in _STATUS_KEYS else ''
        old = existing.get(student_id)
        if old is None and not status:
            continue          # nothing to clear, nothing to create
        if old is not None and old.status == status:
            continue          # unchanged: saving the page never takes over someone else's mark
        if not status:
            db.session.delete(old)
            cleared += 1
        elif old is None:
            db.session.add(AttendanceRecord(student_id=student_id, class_id=class_id, session_id=session_id,
                                            term=term, date=date, status=status, marked_by_admin_id=admin_id,
                                            created_at=now, updated_at=now))
            saved += 1
        else:
            old.status, old.class_id, old.term, old.marked_by_admin_id, old.updated_at = status, class_id, term, admin_id, now
            saved += 1
    return saved, cleared


def class_summary(class_id, session_id, term):
    """Each enrolled student's attendance counts for a term. ``[{'student_id', 'name', 'admission_no',
    'present', 'late', 'absent', 'excused', 'total', 'percentage'}]``, percentage counting late as present."""
    roster = class_roster(class_id, session_id)
    ids = [r['student_id'] for r in roster]
    counts = {}
    if ids:
        rows = db.session.execute(
            select(AttendanceRecord.student_id, AttendanceRecord.status, func.count())
            .where(AttendanceRecord.student_id.in_(ids), AttendanceRecord.session_id == session_id,
                   AttendanceRecord.term == term)
            .group_by(AttendanceRecord.student_id, AttendanceRecord.status)).all()
        for student_id, status, n in rows:
            counts.setdefault(student_id, {}).__setitem__(status, n)
    for row in roster:
        c = counts.get(row['student_id'], {})
        present, late, absent, excused = (c.get(k, 0) for k in ('present', 'late', 'absent', 'excused'))
        total = present + late + absent + excused
        row.update(present=present, late=late, absent=absent, excused=excused, total=total,
                  percentage=round((present + late) / total * 100, 1) if total else None)
    return roster


def student_summary(student_id, session_id, term):
    """One student's attendance counts for a term: ``{'present', 'late', 'absent', 'excused', 'total', 'percentage'}``."""
    rows = db.session.execute(
        select(AttendanceRecord.status, func.count())
        .where(AttendanceRecord.student_id == student_id, AttendanceRecord.session_id == session_id,
               AttendanceRecord.term == term)
        .group_by(AttendanceRecord.status)).all()
    counts = dict(rows)
    present, late, absent, excused = (counts.get(k, 0) for k in ('present', 'late', 'absent', 'excused'))
    total = present + late + absent + excused
    return {'present': present, 'late': late, 'absent': absent, 'excused': excused, 'total': total,
            'percentage': round((present + late) / total * 100, 1) if total else None}


def student_history(student_id, session_id, term):
    """One student's marked days for a term, newest first: ``[{'date', 'status', 'class_name'}]``."""
    rows = db.session.execute(
        select(AttendanceRecord.date, AttendanceRecord.status, SchoolClass.name)
        .join(SchoolClass, SchoolClass.id == AttendanceRecord.class_id)
        .where(AttendanceRecord.student_id == student_id, AttendanceRecord.session_id == session_id,
               AttendanceRecord.term == term)
        .order_by(AttendanceRecord.date.desc())).all()
    return [{'date': d, 'status': s, 'class_name': c} for d, s, c in rows]
