"""Telling students and their parents that an exam/test timetable has been released.

A release covers every entry in one session and term whose class was included in the release. Every
student currently enrolled in one of those classes gets an in-app alert, as does every parent linked
to them; the guardian contact on the student's record also gets an email and a WhatsApp message. The
message names the term and the kind of timetable, not every entry line by line, so it stays short —
the student and parent open the timetable itself to see the dates.

The messages go out as a durable job (``core.jobs``): releasing a timetable must not make an
administrator wait on a mail server, and a message that cannot be sent - or a thread that never
finishes sending it - must never undo the release, which has already been committed by the time
this is called, nor be lost; it is retried the next time a timetable is next released anywhere.
"""

from datetime import datetime, timezone

from flask import current_app, url_for
from sqlalchemy import select

from core.jobs import job_handler, enqueue
from core.branding import school_name
from core.notifications import _notify_guardian_email, _notify_guardian_whatsapp, _parent_ids_for_student
from models import SchoolNotification, Student, StudentEnrolment, db


def announce_timetable_released(class_ids, session_id, term, exam_type, admin_id=None):
    """Announce a just-released timetable to every student enrolled in ``class_ids`` for
    ``session_id``, and their parents. Call only after the release has been committed."""
    class_ids = sorted({int(c) for c in class_ids})
    if class_ids:
        enqueue('announce_timetable', class_ids=class_ids, session_id=session_id, term=term,
                exam_type=exam_type, admin_id=admin_id)


@job_handler('announce_timetable')
def _announce(class_ids, session_id, term, exam_type, admin_id):
    student_ids = sorted({sid for (sid,) in db.session.execute(
        select(StudentEnrolment.student_id).where(
            StudentEnrolment.class_id.in_(class_ids), StudentEnrolment.session_id == session_id,
            StudentEnrolment.active == 1))})
    if not student_ids:
        return
    students = db.session.execute(select(Student.id, Student.first_name, Student.last_name,
                                         Student.guardian_email, Student.guardian_phone)
                                  .where(Student.id.in_(student_ids))).all()
    kind = 'test' if exam_type == 'test' else 'examination'
    label = f'{term} {kind} timetable'
    now = datetime.now(timezone.utc).isoformat()
    for r in students:
        child = f'{r.first_name} {r.last_name}'.strip()
        title = f'{label} released'
        message = f'The {label} is ready to view and download.'
        try:
            db.session.add(SchoolNotification(recipient_type='student', recipient_id=r.id, student_id=r.id,
                                              category='timetable', title=title, message=message,
                                              action_url=url_for('student_timetable'),
                                              created_at=now, created_by=admin_id))
            for pid in _parent_ids_for_student(r.id):
                db.session.add(SchoolNotification(recipient_type='parent', recipient_id=pid, student_id=r.id,
                                                  category='timetable', title=title, message=f'{child}: {message}',
                                                  action_url=url_for('parent_child_timetable', student_id=r.id),
                                                  created_at=now, created_by=admin_id))
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception('Timetable in-app notice failed for student %s', r.id)
        subject = f'{label} released — {child}'
        body = (f'Dear Parent/Guardian,\n\nThe {label} has been released.\n\n'
               f'Sign in to the parent portal to view and download it.\n\nThank you,\n{school_name()}')
        text = f'{school_name()}: the {label} is ready. Sign in to the parent portal to view and download it.'
        try:
            _notify_guardian_email(r.guardian_email, subject, body, student_id=r.id, kind='timetable_released')
        except Exception:
            current_app.logger.exception('Guardian email (timetable released) failed for student %s', r.id)
        try:
            _notify_guardian_whatsapp(r.guardian_phone, text, student_id=r.id, kind='timetable_released')
        except Exception:
            current_app.logger.exception('Guardian WhatsApp (timetable released) failed for student %s', r.id)
