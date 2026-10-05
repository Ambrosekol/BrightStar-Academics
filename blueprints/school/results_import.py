"""Bulk import of past results: one row per student, per subject, per term, per session, so a student's
whole academic record from a previous database can be brought across.

A school's old records are a grid - "Ada, 2023/2024, First Term, Mathematics: test 18 of 20, exam 45 of 60" -
and a student has as many rows as they have subjects, terms and sessions. Each valid row becomes
the same records a hand-entered result becomes: a 'Test' component (only when the row gives one) and an
'Exam' component, filed against the student, subject, session and term, so the term report, the
academic record and the report card read them exactly as they read any other result.

The difference is the status. A migrated result is already final, so it is written as released, with
the importing administrator recorded as the entering, verifying and approving person and a workflow entry
that says where it came from. That is what lets a released result show on a report card straight away.

Rules that keep a migration safe:
* a student is matched by admission number whether or not they are still active, because the graduates
  archived by the history import are exactly the students whose past results matter;
* a session must already exist (the enrolment history import creates earlier sessions), and a subject must
  already exist under Subjects, so nothing is invented here;
* a subject, term and session that already has a result for this student is left alone and reported, never
  overwritten, so running a file twice cannot change what is on record;
* each row is committed on its own, so one bad line never holds back the rest.
"""

import csv
import io
from datetime import datetime, timezone

from flask import Response, flash, redirect, render_template, request, url_for
from sqlalchemy import func, select

from app import ACADEMIC_TERMS, app
from blueprints.school.helpers import _finite, _result_term, import_permission_required
from core.security import admin_required, audit_log, csrf_protect, current_admin
from core.uploads import data_upload_limit_bytes, format_limit
from models import AcademicSession, ResultWorkflowEvent, SchoolStudentResult, SchoolSubject, Student, db

MAX_ROWS = 5000
REQUIRED_COLUMNS = ('admission_no', 'session', 'term', 'subject', 'exam_score', 'exam_max')
OPTIONAL_COLUMNS = ('test_score', 'test_max', 'notes')
TEMPLATE_HEADER = ('admission_no', 'session', 'term', 'subject', 'test_score', 'test_max', 'exam_score', 'exam_max', 'notes')
MIGRATION_NOTE = 'Migrated from a previous school database'


def _all_students_by_admission_no():
    """Every student, active or archived: a graduate's past results are the point of the migration."""
    return {s.admission_no.strip().casefold(): s for s in db.session.scalars(select(Student))}


def _subjects_by_name():
    return {s.name.strip().casefold(): s for s in db.session.scalars(
        select(SchoolSubject).where(SchoolSubject.active == 1))}


def _sessions_by_name():
    return {s.name.strip().casefold(): s for s in db.session.scalars(select(AcademicSession))}


def _number(raw):
    """A score or maximum from the file: None when blank, ValueError when it is not a number."""
    text = (raw or '').strip()
    return _finite(float(text)) if text else None


@app.route('/admin/school/students/import-results/template.csv')
@admin_required
@import_permission_required('school.results.release')
def admin_school_results_import_template():
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(TEMPLATE_HEADER)
    writer.writerow(['ADA-0001', '2023/2024', 'First Term', 'Mathematics', '18', '20', '45', '60', ''])
    writer.writerow(['ADA-0001', '2023/2024', 'First Term', 'English Language', '', '', '52', '60', 'Test not recorded'])
    writer.writerow(['ADA-0001', '2023/2024', 'Second Term', 'Mathematics', '16', '20', '48', '60', ''])
    response = Response(buf.getvalue(), mimetype='text/csv')
    response.headers['Content-Disposition'] = 'attachment; filename="past-results-import-template.csv"'
    return response


@app.post('/admin/school/students/import-results')
@admin_required
@csrf_protect
@import_permission_required('school.results.release')
def admin_school_results_import_run():
    me = current_admin()
    back = url_for('admin_school_students_import')
    upload = request.files.get('csv_file')
    if not upload or not upload.filename:
        flash('Choose a CSV file to import.', 'error')
        return redirect(back)
    limit = data_upload_limit_bytes()
    raw = upload.read(limit + 1)
    if len(raw) > limit:
        flash(f'That file is larger than the {format_limit(limit)} limit.', 'error')
        return redirect(back)
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        flash('That file is not readable as plain-text CSV. Save it as CSV (UTF-8) from Excel or Google Sheets and try again.', 'error')
        return redirect(back)

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        flash('That file has no header row. Use the template.', 'error')
        return redirect(back)
    fields = {(name or '').strip().casefold(): name for name in reader.fieldnames}
    missing = [c for c in REQUIRED_COLUMNS if c not in fields]
    if missing:
        flash(f'The file is missing required column{"s" if len(missing) != 1 else ""}: {", ".join(missing)}.', 'error')
        return redirect(back)

    students = _all_students_by_admission_no()
    subjects = _subjects_by_name()
    sessions = _sessions_by_name()

    def get(row, col):
        key = fields.get(col)
        return (row.get(key) or '').strip() if key else ''

    outcomes = []
    for line_no, row in enumerate(reader, start=2):
        if line_no - 1 > MAX_ROWS:
            outcomes.append({'line': line_no, 'ok': False, 'label': '',
                             'reason': f'Only the first {MAX_ROWS} rows were processed; the rest of the file was not read.'})
            break

        admission_no = get(row, 'admission_no')
        session_name = get(row, 'session')
        term_raw = get(row, 'term')
        subject_raw = get(row, 'subject')
        notes = get(row, 'notes')
        label = f'{admission_no or "?"} · {session_name or "?"} · {term_raw or "?"} · {subject_raw or "?"}'

        student = students.get(admission_no.casefold()) if admission_no else None
        session_row = sessions.get(session_name.casefold()) if session_name else None
        subject = subjects.get(subject_raw.casefold()) if subject_raw else None
        term = _result_term(term_raw) if term_raw else None

        problems = []
        if not admission_no:
            problems.append('admission number is required')
        elif not student:
            problems.append(f'no student with admission number "{admission_no}"')
        if not session_name:
            problems.append('session is required')
        elif not session_row:
            problems.append(f'no session named "{session_name}" (import the enrolment history first: it creates earlier sessions)')
        if not term_raw:
            problems.append('term is required')
        elif term not in ACADEMIC_TERMS:
            problems.append('term must be First Term, Second Term or Third Term')
        if not subject_raw:
            problems.append('subject is required')
        elif not subject:
            problems.append(f'no subject named "{subject_raw}" (create it under Subjects first)')

        try:
            exam_score, exam_max = _number(get(row, 'exam_score')), _number(get(row, 'exam_max'))
            test_score, test_max = _number(get(row, 'test_score')), _number(get(row, 'test_max'))
        except ValueError:
            exam_score = exam_max = test_score = test_max = None
            problems.append('scores and maximums must be numbers')
        else:
            if exam_score is None or exam_max is None:
                problems.append('exam score and exam maximum are required')
            elif exam_max <= 0 or exam_score < 0 or exam_score > exam_max:
                problems.append('exam score must be between 0 and its maximum')
            if (test_score is None) != (test_max is None):
                problems.append('test score and test maximum must both be given, or both left empty')
            elif test_score is not None and (test_max <= 0 or test_score < 0 or test_score > test_max):
                problems.append('test score must be between 0 and its maximum')

        if not problems:
            already = db.session.scalars(select(SchoolStudentResult.id).where(
                SchoolStudentResult.student_id == student.id,
                SchoolStudentResult.subject_id == subject.id,
                SchoolStudentResult.session_id == session_row.id,
                func.coalesce(SchoolStudentResult.term, 'Full Session') == term,
                SchoolStudentResult.component_name.in_(('Test', 'Exam')))).first()
            if already:
                problems.append('this student already has a result for that subject, term and session; it is left as it is')

        if problems:
            outcomes.append({'line': line_no, 'ok': False, 'label': label, 'reason': '; '.join(problems)})
            continue

        try:
            now = datetime.now(timezone.utc).isoformat()
            components = [('Exam', exam_score, exam_max)]
            if test_score is not None:
                components.append(('Test', test_score, test_max))
            reason = MIGRATION_NOTE + (f': {notes}' if notes else '')
            for component, score, maximum in components:
                result = SchoolStudentResult(
                    student_id=student.id, assessment_id=None, assignment_id=None, subject_id=subject.id,
                    score=score, max_score=maximum, term=term, session_id=session_row.id, status='released',
                    created_at=now, source_type='migration', component_name=component,
                    entered_by=me['id'], verified_by=me['id'], approved_by=me['id'],
                    released_at=now, updated_at=now, updated_by=me['id'])
                db.session.add(result)
                db.session.flush()
                db.session.add(ResultWorkflowEvent(result_id=result.id, from_status=None, to_status='released',
                                                   actor_admin_id=me['id'], reason=reason, created_at=now))
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            outcomes.append({'line': line_no, 'ok': False, 'label': label, 'reason': f'could not be saved ({exc})'})
            continue
        outcomes.append({'line': line_no, 'ok': True, 'label': label})

    created = sum(1 for o in outcomes if o['ok'])
    skipped = len(outcomes) - created
    if created:
        audit_log('academic_results_bulk_imported', 'school', 'result', None,
                  {'created': created, 'skipped': skipped, 'file': upload.filename})
    flash(f'{created} result row{"" if created == 1 else "s"} imported' +
          (f', {skipped} row{"s" if skipped != 1 else ""} skipped.' if skipped else '.'),
          'success' if created else 'error')
    return render_template('admin_school_results_import_report.html', outcomes=outcomes, created=created, skipped=skipped)
