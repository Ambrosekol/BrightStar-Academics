"""A child's exam/test timetable, for the parent: released entries only, one term at a time.

A parent reaches a child's timetable only through the link between their account and that child.
"""

import re

import sqlalchemy as sa
from flask import Response, abort, render_template, request, session, url_for

from app import ACADEMIC_TERMS, app
from blueprints.parents.helpers import _parent_owns_student, parent_required
from blueprints.school.timetable_data import entry_rows
from blueprints.school.record_views import timetable_view
from core.branding import school_brand
from core.timetable_pdf import render_timetable_pdf
from models import AcademicSession, Student, StudentEnrolment, db
from sqlalchemy import select


def _sessions():
    return db.session.scalars(select(AcademicSession).where(AcademicSession.active == 1).order_by(AcademicSession.id.desc())).all()


def _current_enrolment(student_id):
    """(class_id, session_id) the student is enrolled in right now (or the most recent one)."""
    return db.session.execute(
        select(StudentEnrolment.class_id, StudentEnrolment.session_id)
        .join(AcademicSession, AcademicSession.id == StudentEnrolment.session_id)
        .where(StudentEnrolment.student_id == student_id, StudentEnrolment.active == 1)
        .order_by(sa.case((AcademicSession.is_current == 1, 0), else_=1), StudentEnrolment.id.desc()).limit(1)).first()


def _choice(current_session_id):
    sessions = _sessions()
    wanted = request.values.get('session_id', type=int) or current_session_id
    session_row = next((s for s in sessions if s.id == wanted),
                       next((s for s in sessions if s.id == current_session_id), sessions[0] if sessions else None))
    term = request.values.get('term', '').strip()
    term = term if term in ACADEMIC_TERMS else ACADEMIC_TERMS[0]
    return sessions, session_row, term


@app.route('/parent/children/<int:student_id>/timetable')
@parent_required
def parent_child_timetable(student_id):
    if not _parent_owns_student(session['parent_id'], student_id):
        abort(404)
    child = db.session.get(Student, student_id)
    enrolment = _current_enrolment(student_id)
    class_id, current_session_id = (enrolment[0], enrolment[1]) if enrolment else (None, None)
    sessions, session_row, term = _choice(current_session_id)
    rows = entry_rows([class_id], session_row.id, term, released_only=True) if (session_row and class_id) else []
    return render_template('timetable_view.html', sessions=sessions, session_row=session_row, term=term,
                           terms=ACADEMIC_TERMS, rows=rows, **timetable_view(rows),
                           pdf_url=url_for('parent_child_timetable_pdf', student_id=student_id, session_id=session_row.id, term=term)
                           if (session_row and rows) else None,
                           back_url=url_for('parent_child_detail', student_id=student_id) + '#timetable',
                           back_label='Back to my child', title=f'{child.first_name}’s exam timetable',
                           home_url=url_for('parent_dashboard'), home_kind='Parent Portal', person_name=child.first_name)


@app.route('/parent/children/<int:student_id>/timetable/pdf')
@parent_required
def parent_child_timetable_pdf(student_id):
    if not _parent_owns_student(session['parent_id'], student_id):
        abort(404)
    child = db.session.get(Student, student_id)
    enrolment = _current_enrolment(student_id)
    class_id, current_session_id = (enrolment[0], enrolment[1]) if enrolment else (None, None)
    sessions, session_row, term = _choice(current_session_id)
    rows = entry_rows([class_id], session_row.id, term, released_only=True) if (session_row and class_id) else []
    session_name = session_row.name if session_row else ''
    name = re.sub(r'[^A-Za-z0-9._-]+', '-', f'Timetable-{child.first_name}-{term}-{session_name}').strip('-') + '.pdf'
    response = Response(render_timetable_pdf(term, session_name, rows, school=school_brand()), mimetype='application/pdf')
    response.headers['Content-Disposition'] = f'inline; filename="{name}"'
    response.headers['Cache-Control'] = 'no-store'
    return response
