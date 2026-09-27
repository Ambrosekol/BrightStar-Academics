"""A child's attendance, for the parent: a summary and a day-by-day history, one term at a time.

A parent reaches a child's attendance only through the link between their account and that child (the
same check every other parent page makes).
"""

import sqlalchemy as sa
from flask import abort, render_template, request, session, url_for

from app import ACADEMIC_TERMS, app
from blueprints.parents.helpers import _parent_owns_student, parent_required
from blueprints.school.attendance_data import STATUSES, student_history, student_summary
from models import AcademicSession, Student, StudentEnrolment, db
from sqlalchemy import select


def _sessions():
    return db.session.scalars(select(AcademicSession).where(AcademicSession.active == 1).order_by(AcademicSession.id.desc())).all()


def _current_enrolled_session(student_id):
    return db.session.scalar(
        select(StudentEnrolment.session_id).where(StudentEnrolment.student_id == student_id, StudentEnrolment.active == 1)
        .join(AcademicSession, AcademicSession.id == StudentEnrolment.session_id)
        .order_by(sa.case((AcademicSession.is_current == 1, 0), else_=1), StudentEnrolment.id.desc()).limit(1))


def _choice(current_session_id):
    sessions = _sessions()
    wanted = request.values.get('session_id', type=int) or current_session_id
    session_row = next((s for s in sessions if s.id == wanted),
                       next((s for s in sessions if s.id == current_session_id), sessions[0] if sessions else None))
    term = request.values.get('term', '').strip()
    term = term if term in ACADEMIC_TERMS else ACADEMIC_TERMS[0]
    return sessions, session_row, term


@app.route('/parent/children/<int:student_id>/attendance')
@parent_required
def parent_child_attendance(student_id):
    if not _parent_owns_student(session['parent_id'], student_id):
        abort(404)
    child = db.session.get(Student, student_id)
    sessions, session_row, term = _choice(_current_enrolled_session(student_id))
    summary = student_summary(student_id, session_row.id, term) if session_row else None
    history = student_history(student_id, session_row.id, term) if session_row else []
    return render_template('attendance_view.html', sessions=sessions, session_row=session_row, term=term,
                           terms=ACADEMIC_TERMS, summary=summary, history=history, statuses=STATUSES,
                           back_url=url_for('parent_child_detail', student_id=student_id) + '#attendance',
                           back_label='Back to my child', title=f'{child.first_name}’s attendance',
                           home_url=url_for('parent_dashboard'), home_kind='Parent Portal', person_name=child.first_name)
