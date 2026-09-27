"""Exam and test timetables: entries, releasing them, and what each audience is shown.

An entry is one subject's paper for one class, on one date and time. It is a draft until the
timetable it belongs to is released (``released_at`` set); a student or parent only ever sees
released entries, while staff see everything so a mistake can be fixed before anyone is told.
"""

from datetime import datetime, timezone

from sqlalchemy import select

from models import Admin, ExamTimetableEntry, SchoolClass, SchoolSubject, db

EXAM_TYPES = (('test', 'Test'), ('examination', 'Examination'))
_EXAM_TYPE_KEYS = {key for key, _ in EXAM_TYPES}


def entry_rows(class_ids, session_id, term, released_only=False):
    """Every entry for these classes, this session and term, earliest date first.

    ``[{'id', 'class_id', 'class_name', 'subject_id', 'subject_name', 'exam_type', 'date',
    'start_time', 'end_time', 'venue', 'released', 'created_by_name'}]``.
    """
    class_ids = list(class_ids)
    if not class_ids:
        return []
    stmt = (select(ExamTimetableEntry, SchoolClass.name.label('class_name'), SchoolSubject.name.label('subject_name'))
           .join(SchoolClass, SchoolClass.id == ExamTimetableEntry.class_id)
           .join(SchoolSubject, SchoolSubject.id == ExamTimetableEntry.subject_id)
           .where(ExamTimetableEntry.class_id.in_(class_ids), ExamTimetableEntry.session_id == session_id,
                  ExamTimetableEntry.term == term))
    if released_only:
        stmt = stmt.where(ExamTimetableEntry.released_at.is_not(None))
    rows = db.session.execute(stmt.order_by(ExamTimetableEntry.date, ExamTimetableEntry.start_time)).all()
    authors = {a.id: a.display_name for a in db.session.scalars(select(Admin).where(
        Admin.id.in_({r.ExamTimetableEntry.created_by for r in rows if r.ExamTimetableEntry.created_by})))} if rows else {}
    out = []
    for r in rows:
        e = r.ExamTimetableEntry
        out.append({'id': e.id, 'class_id': e.class_id, 'class_name': r.class_name, 'subject_id': e.subject_id,
                    'subject_name': r.subject_name, 'exam_type': e.exam_type, 'date': e.date,
                    'start_time': e.start_time, 'end_time': e.end_time or '', 'venue': e.venue or '',
                    'released': bool(e.released_at), 'created_by_name': authors.get(e.created_by, '')})
    return out


def save_entry(entry_id, values, admin_id):
    """Create or update one entry. ``values`` has 'class_id', 'subject_id', 'session_id', 'term',
    'exam_type', 'date', 'start_time', 'end_time', 'venue'. Raises ValueError with a message safe to
    show for anything wrong. Does not commit."""
    if values['exam_type'] not in _EXAM_TYPE_KEYS:
        raise ValueError('Choose whether this is a test or an examination.')
    if not values['date'] or not values['start_time']:
        raise ValueError('A date and a start time are required.')
    now = datetime.now(timezone.utc).isoformat()
    if entry_id:
        entry = db.session.get(ExamTimetableEntry, entry_id)
        if entry is None:
            raise ValueError('That timetable entry no longer exists.')
        entry.class_id, entry.subject_id = values['class_id'], values['subject_id']
        entry.session_id, entry.term, entry.exam_type = values['session_id'], values['term'], values['exam_type']
        entry.date, entry.start_time = values['date'], values['start_time']
        entry.end_time, entry.venue = values['end_time'] or None, values['venue'] or None
        entry.updated_at = now
    else:
        db.session.add(ExamTimetableEntry(
            class_id=values['class_id'], subject_id=values['subject_id'], session_id=values['session_id'],
            term=values['term'], exam_type=values['exam_type'], date=values['date'], start_time=values['start_time'],
            end_time=values['end_time'] or None, venue=values['venue'] or None,
            created_by=admin_id, created_at=now, updated_at=now))


def release(class_ids, session_id, term):
    """Release every not-yet-released entry for these classes, this session and term. Returns how
    many were released. Does not commit."""
    now = datetime.now(timezone.utc).isoformat()
    entries = db.session.scalars(select(ExamTimetableEntry).where(
        ExamTimetableEntry.class_id.in_(list(class_ids) or [0]), ExamTimetableEntry.session_id == session_id,
        ExamTimetableEntry.term == term, ExamTimetableEntry.released_at.is_(None))).all()
    for entry in entries:
        entry.released_at = now
    return len(entries)
