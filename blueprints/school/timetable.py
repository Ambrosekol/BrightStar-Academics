"""Exam and test timetables: the staff side.

An entry is one subject's paper for one class, on one date and time. Entries are created and edited
freely as drafts (``school.timetable.manage``); nobody outside staff sees them until the whole
session-and-term timetable is released at once (``school.timetable.release``), which notifies every
student in the classes it covers, and their parents (blueprints/school/timetable_notices.py). Seeing
the list at all — draft or released — only needs ``school.timetable.view``. A staff member limited
to some classes only ever sees, creates or releases entries for those classes.
"""

import re

from flask import Response, abort, flash, redirect, render_template, request, url_for
from sqlalchemy import select

from app import ACADEMIC_TERMS, _school_current_session, app
from blueprints.school.helpers import _assignment_form_data, _school_class_allowed, _school_pair_allowed, offer_subject
from blueprints.school.timetable_data import EXAM_TYPES, entry_rows, release, save_entry
from blueprints.school.timetable_notices import announce_timetable_released
from core.branding import school_brand
from core.security import admin_required, audit_log, csrf_protect, current_admin
from core.timetable_pdf import render_timetable_pdf
from models import AcademicSession, ExamTimetableEntry, db

TERMS = ACADEMIC_TERMS


def _sessions():
    return db.session.scalars(select(AcademicSession).where(AcademicSession.active == 1).order_by(AcademicSession.id.desc())).all()


def _context():
    """The classes this staff member may work with, the sessions, the chosen session and term.
    Returns ``(classes, subjects, sessions, session_row, term)``."""
    classes, subjects, _students = _assignment_form_data(current_admin()['id'])
    sessions = _sessions()
    current = _school_current_session()
    wanted = request.values.get('session_id', type=int) or (current['id'] if current else None)
    session_row = next((s for s in sessions if s.id == wanted), sessions[0] if sessions else None)
    term = request.values.get('term', '').strip()
    term = term if term in TERMS else ACADEMIC_TERMS[0]
    return classes, subjects, sessions, session_row, term


def _redirect_back(session_row, term):
    return redirect(url_for('admin_school_timetable', session_id=session_row.id if session_row else '', term=term))


# ---------------------------------------------------------------------------------------------
# The timetable itself
@app.route('/admin/school/timetable')
@admin_required
def admin_school_timetable():
    classes, subjects, sessions, session_row, term = _context()
    class_ids = [c.id for c in classes]
    rows = entry_rows(class_ids, session_row.id, term) if session_row else []
    draft_count = sum(1 for r in rows if not r['released'])
    return render_template('admin_school_timetable.html', classes=classes, subjects=subjects, sessions=sessions,
                           terms=TERMS, session_row=session_row, term=term, rows=rows, exam_types=EXAM_TYPES,
                           draft_count=draft_count)


def _form_values():
    return {'class_id': request.form.get('class_id', type=int),
           'subject_id': request.form.get('subject_id', type=int),
           'exam_type': request.form.get('exam_type', '').strip(),
           'date': request.form.get('date', '').strip(),
           'start_time': request.form.get('start_time', '').strip(),
           'end_time': request.form.get('end_time', '').strip(),
           'venue': ' '.join(request.form.get('venue', '').split())[:120]}


@app.post('/admin/school/timetable/new')
@admin_required
@csrf_protect
def admin_school_timetable_new():
    classes, subjects, sessions, session_row, term = _context()
    if not session_row:
        flash('There is no active academic session to add a timetable entry to.', 'error')
        return _redirect_back(session_row, term)
    values = _form_values()
    if not _school_pair_allowed(current_admin()['id'], values['class_id'], values['subject_id']):
        flash('Select a class and subject within your authorised scope.', 'error')
        return _redirect_back(session_row, term)
    values['session_id'], values['term'] = session_row.id, term
    try:
        save_entry(None, values, current_admin()['id'])
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'error')
        return _redirect_back(session_row, term)
    offer_subject(values['class_id'], values['subject_id'], current_admin()['id'])
    db.session.commit()
    audit_log('timetable_entry_created', 'school', 'class', values['class_id'],
              {'session_id': session_row.id, 'term': term, 'subject_id': values['subject_id']})
    flash('Timetable entry added.', 'success')
    return _redirect_back(session_row, term)


@app.post('/admin/school/timetable/<int:entry_id>/edit')
@admin_required
@csrf_protect
def admin_school_timetable_edit(entry_id):
    entry = db.session.get(ExamTimetableEntry, entry_id)
    if entry is None or not _school_class_allowed(current_admin()['id'], entry.class_id):
        abort(404)
    classes, subjects, sessions, session_row, term = _context()
    values = _form_values()
    if not _school_pair_allowed(current_admin()['id'], values['class_id'], values['subject_id']):
        flash('Select a class and subject within your authorised scope.', 'error')
        return _redirect_back(session_row, term)
    values['session_id'], values['term'] = entry.session_id, entry.term
    try:
        save_entry(entry_id, values, current_admin()['id'])
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'error')
        return _redirect_back(session_row, term)
    offer_subject(values['class_id'], values['subject_id'], current_admin()['id'])
    db.session.commit()
    audit_log('timetable_entry_edited', 'school', 'class', values['class_id'],
              {'entry_id': entry_id, 'session_id': entry.session_id, 'term': entry.term})
    flash('Timetable entry updated.', 'success')
    return redirect(url_for('admin_school_timetable', session_id=entry.session_id, term=entry.term))


@app.post('/admin/school/timetable/<int:entry_id>/delete')
@admin_required
@csrf_protect
def admin_school_timetable_delete(entry_id):
    entry = db.session.get(ExamTimetableEntry, entry_id)
    if entry is None or not _school_class_allowed(current_admin()['id'], entry.class_id):
        abort(404)
    session_id, term = entry.session_id, entry.term
    db.session.delete(entry)
    db.session.commit()
    audit_log('timetable_entry_deleted', 'school', 'class', entry.class_id, {'entry_id': entry_id, 'session_id': session_id, 'term': term})
    flash('Timetable entry deleted.', 'success')
    return redirect(url_for('admin_school_timetable', session_id=session_id, term=term))


# ---------------------------------------------------------------------------------------------
# Releasing
@app.post('/admin/school/timetable/release')
@admin_required
@csrf_protect
def admin_school_timetable_release():
    classes, subjects, sessions, session_row, term = _context()
    if not session_row:
        return _redirect_back(session_row, term)
    class_ids = [c.id for c in classes]
    count = release(class_ids, session_row.id, term)
    if not count:
        db.session.rollback()
        flash('Nothing to release: every entry is already released, or there is no entry yet for this session and term.', 'error')
        return _redirect_back(session_row, term)
    db.session.commit()
    audit_log('timetable_released', 'school', 'session', session_row.id, {'term': term, 'entries': count})
    exam_type = request.form.get('exam_type', 'examination')
    announce_timetable_released(class_ids, session_row.id, term, exam_type, current_admin()['id'])
    flash(f'{count} timetable entr{"y" if count == 1 else "ies"} released. Students and parents have been notified.', 'success')
    return _redirect_back(session_row, term)


# ---------------------------------------------------------------------------------------------
# The PDF (staff: every entry; students and parents: only released ones, from their own routes)
@app.route('/admin/school/timetable/pdf')
@admin_required
def admin_school_timetable_pdf():
    classes, subjects, sessions, session_row, term = _context()
    if not session_row:
        abort(404)
    class_ids = [c.id for c in classes]
    rows = entry_rows(class_ids, session_row.id, term)
    if not rows:
        flash('There is no timetable entry for this session and term yet.', 'error')
        return _redirect_back(session_row, term)
    name = re.sub(r'[^A-Za-z0-9._-]+', '-', f'Timetable-{term}-{session_row.name}').strip('-') + '.pdf'
    response = Response(render_timetable_pdf(term, session_row.name, rows, school=school_brand()), mimetype='application/pdf')
    response.headers['Content-Disposition'] = f'inline; filename="{name}"'
    response.headers['Cache-Control'] = 'no-store'
    return response
