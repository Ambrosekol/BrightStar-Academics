"""A student's own exam/test timetable: released entries only, one session and term at a time.

A student only ever sees their own class's timetable, and only entries that have actually been
released; a draft entry (or one for a class the student is not in) is simply not shown.
"""

import re

from flask import Response, render_template, request, session, url_for

from app import ACADEMIC_TERMS, _student_with_enrolment, app
from blueprints.school.timetable_data import entry_rows
from blueprints.school.record_views import timetable_view
from blueprints.student_portal.helpers import student_required
from core.branding import school_brand
from core.timetable_pdf import render_timetable_pdf
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


@app.route('/student/timetable')
@student_required
def student_timetable():
    student = _student_with_enrolment(session['student_id'])
    sessions, session_row, term = _choice(student['session_id'])
    rows = entry_rows([student['class_id']], session_row.id, term, released_only=True) if (session_row and student['class_id']) else []
    return render_template('timetable_view.html', sessions=sessions, session_row=session_row, term=term,
                           terms=ACADEMIC_TERMS, rows=rows, **timetable_view(rows),
                           pdf_url=url_for('student_timetable_pdf', session_id=session_row.id, term=term) if (session_row and rows) else None,
                           back_url=url_for('student_dashboard'), back_label='Back to my dashboard', title='My exam timetable',
                           home_url=url_for('student_dashboard'), home_kind='Student Portal', person_name=student['first_name'])


@app.route('/student/timetable/pdf')
@student_required
def student_timetable_pdf():
    student = _student_with_enrolment(session['student_id'])
    sessions, session_row, term = _choice(student['session_id'])
    rows = entry_rows([student['class_id']], session_row.id, term, released_only=True) if (session_row and student['class_id']) else []
    session_name = session_row.name if session_row else ''
    name = re.sub(r'[^A-Za-z0-9._-]+', '-', f'Timetable-{term}-{session_name}').strip('-') + '.pdf'
    response = Response(render_timetable_pdf(term, session_name, rows, school=school_brand()), mimetype='application/pdf')
    response.headers['Content-Disposition'] = f'inline; filename="{name}"'
    response.headers['Cache-Control'] = 'no-store'
    return response
