"""A student's own attendance: a summary and a day-by-day history, one term at a time.

A student only ever sees their own (the account in the session is the only one asked about), and
starts on their own current class's session; they may look at any other active session and term too.
"""

from flask import render_template, request, session, url_for

from app import ACADEMIC_TERMS, _student_with_enrolment, app
from blueprints.school.attendance_data import STATUSES, student_history, student_summary
from blueprints.student_portal.helpers import student_required
from models import AcademicSession, db
from sqlalchemy import select


def _sessions():
    return db.session.scalars(select(AcademicSession).where(AcademicSession.active == 1).order_by(AcademicSession.id.desc())).all()


def _choice(current_session_id):
    sessions = _sessions()
    wanted = request.values.get('session_id', type=int) or current_session_id
    session_row = next((s for s in sessions if s.id == wanted),
                       next((s for s in sessions if s.id == current_session_id), sessions[0] if sessions else None))
    term = request.values.get('term', '').strip()
    term = term if term in ACADEMIC_TERMS else ACADEMIC_TERMS[0]
    return sessions, session_row, term


@app.route('/student/attendance')
@student_required
def student_attendance():
    student = _student_with_enrolment(session['student_id'])
    sessions, session_row, term = _choice(student['session_id'])
    summary = student_summary(student['id'], session_row.id, term) if session_row else None
    history = student_history(student['id'], session_row.id, term) if session_row else []
    return render_template('attendance_view.html', sessions=sessions, session_row=session_row, term=term,
                           terms=ACADEMIC_TERMS, summary=summary, history=history, statuses=STATUSES,
                           back_url=url_for('student_dashboard'), back_label='Back to my dashboard', title='My attendance',
                           home_url=url_for('student_dashboard'), home_kind='Student Portal', person_name=student['first_name'])
