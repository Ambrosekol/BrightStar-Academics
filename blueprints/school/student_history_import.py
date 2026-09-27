"""Bulk import of enrolment history: a CSV that backfills the classes and sessions a student was
in *before* they ever joined this portal - the deeper migration the bulk student importer
(blueprints/school/student_import.py) does not attempt, since it only ever creates a student's
current record.

A school moving from another system, on paper or another product, usually already has each
student's history - "Ada was in JSS 1 in 2023/2024, JSS 2 in 2024/2025" - and typing that in one
row at a time (the single "Add enrolment history" form on a student's own page) does not scale to
a whole school of it. Each valid row here does exactly what that form does by hand, reusing the
very same helpers (``_mark_latest_history_current``, ``_sync_enrolment_for_history`` in
blueprints/school/helpers.py) so a bulk-imported history entry is indistinguishable from one typed
in one at a time - except that this importer deliberately accepts a session by name whether or not
it is still the school's *active* one, since backfilling history is precisely when an older,
archived session is the point; the hand-entry form's own dropdown only offers active sessions
because it is meant for everyday correction, not migration.

A row that fails validation is skipped, with a reason, and every other row is still tried. Each
valid row is created and committed on its own, so a failure partway through a large file leaves
everything already created in place - the same rule the student importer follows.
"""

import csv
import io
from datetime import datetime, timezone

from flask import Response, flash, redirect, render_template, request, url_for
from sqlalchemy import select

from app import app
from blueprints.school.helpers import ACADEMIC_HISTORY_LEVELS, _mark_latest_history_current, _sync_enrolment_for_history
from core.security import admin_required, audit_log, csrf_protect, current_admin
from core.uploads import data_upload_limit_bytes, format_limit
from models import AcademicSession, SchoolClass, Student, StudentEnrollmentHistory, db

MAX_ROWS = 2000
REQUIRED_COLUMNS = ('admission_no', 'level', 'session')
OPTIONAL_COLUMNS = ('enrolled_at', 'completed_at', 'notes')
TEMPLATE_HEADER = ('admission_no', 'level', 'session', 'enrolled_at', 'completed_at', 'notes')
# The same levels the single "Add enrolment history" form accepts (blueprints/school/routes.py),
# kept identical so a row this importer accepts is a row that form would have accepted too.
_ALLOWED_LEVELS = {'Daycare', 'Toddler 1', 'Toddler 2', 'Junior Reception', 'Middle Reception',
                   'Senior Reception', 'Crèche', 'Nursery 1', 'Nursery 2', 'Nursery 3',
                   'Primary 1', 'Primary 2', 'Primary 3', 'Primary 4', 'Primary 5', 'Primary 6',
                   'JSS 1', 'JSS 2', 'JSS 3', 'SSS 1', 'SSS 2', 'SSS 3'}


def _students_by_admission_no():
    return {s.admission_no.strip().casefold(): s for s in db.session.scalars(
        select(Student).where(Student.active == 1))}


def _sessions_by_name():
    """Every session, active or not: backfilling history is exactly when an older, archived
    session is the point, unlike the hand-entry form's dropdown of only the active ones."""
    return {s.name.strip().casefold(): s for s in db.session.scalars(select(AcademicSession))}


def _classes_by_name():
    return {c.name.strip().casefold(): c for c in db.session.scalars(
        select(SchoolClass).where(SchoolClass.active == 1))}


@app.route('/admin/school/students/import-history')
@admin_required
def admin_school_students_import_history():
    return render_template('admin_school_students_import_history.html', columns=TEMPLATE_HEADER,
                           required_columns=REQUIRED_COLUMNS, optional_columns=OPTIONAL_COLUMNS,
                           levels=sorted(_ALLOWED_LEVELS), max_rows=MAX_ROWS)


@app.route('/admin/school/students/import-history/template.csv')
@admin_required
def admin_school_students_import_history_template():
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(TEMPLATE_HEADER)
    writer.writerow(['ADA-0001', 'JSS 1', '2023/2024', '2023-09-11', '2024-07-19', 'Transferred in from a previous school'])
    response = Response(buf.getvalue(), mimetype='text/csv')
    response.headers['Content-Disposition'] = 'attachment; filename="enrolment-history-import-template.csv"'
    return response


@app.post('/admin/school/students/import-history')
@admin_required
@csrf_protect
def admin_school_students_import_history_run():
    me = current_admin()
    upload = request.files.get('csv_file')
    if not upload or not upload.filename:
        flash('Choose a CSV file to import.', 'error')
        return redirect(url_for('admin_school_students_import_history'))
    limit = data_upload_limit_bytes()
    raw = upload.read(limit + 1)
    if len(raw) > limit:
        flash(f'That file is larger than the {format_limit(limit)} limit.', 'error')
        return redirect(url_for('admin_school_students_import_history'))
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        flash('That file is not readable as plain-text CSV. Save it as CSV (UTF-8) from Excel or Google Sheets and try again.', 'error')
        return redirect(url_for('admin_school_students_import_history'))

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        flash('That file has no header row. Use the template.', 'error')
        return redirect(url_for('admin_school_students_import_history'))
    fields = {(name or '').strip().casefold(): name for name in reader.fieldnames}
    missing = [c for c in REQUIRED_COLUMNS if c not in fields]
    if missing:
        flash(f'The file is missing required column{"s" if len(missing) != 1 else ""}: {", ".join(missing)}.', 'error')
        return redirect(url_for('admin_school_students_import_history'))

    students = _students_by_admission_no()
    sessions = _sessions_by_name()
    classes = _classes_by_name()

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
        level = get(row, 'level')
        session_name = get(row, 'session')
        enrolled_at = get(row, 'enrolled_at')
        completed_at = get(row, 'completed_at')
        notes = get(row, 'notes')
        label = f'{admission_no} → {level or "?"} ({session_name or "?"})'

        student = students.get(admission_no.casefold())
        problems = []
        if not admission_no:
            problems.append('admission number is required')
        elif not student:
            problems.append(f'no active student with admission number "{admission_no}"')
        if not level:
            problems.append('level is required')
        elif level not in _ALLOWED_LEVELS:
            problems.append(f'"{level}" is not a recognised level')
        session_row = sessions.get(session_name.casefold()) if session_name else None
        if not session_name:
            problems.append('session is required')
        elif not session_row:
            problems.append(f'no session named "{session_name}" (add it under Academic Sessions first)')
        if enrolled_at and not _looks_like_date(enrolled_at):
            problems.append('enrolled_at must be a date (YYYY-MM-DD)')
        if completed_at and not _looks_like_date(completed_at):
            problems.append('completed_at must be a date (YYYY-MM-DD)')
        if problems:
            outcomes.append({'line': line_no, 'ok': False, 'label': label, 'reason': '; '.join(problems)})
            continue

        try:
            class_row = classes.get(level.casefold())
            now = datetime.now(timezone.utc).isoformat()
            entry_date = enrolled_at or now[:10]
            # Close any open earlier record before opening this one, exactly as the hand-entry
            # form does, so importing several sessions for one student in any order still leaves
            # a clean, non-overlapping history.
            db.session.execute(StudentEnrollmentHistory.__table__.update().where(
                StudentEnrollmentHistory.student_id == student.id, StudentEnrollmentHistory.active == 1,
                StudentEnrollmentHistory.enrolled_at <= entry_date
            ).values(active=0, completed_at=db.func.coalesce(StudentEnrollmentHistory.completed_at, entry_date)))
            db.session.add(StudentEnrollmentHistory(
                student_id=student.id, session_id=session_row.id, level_name=level,
                class_id=class_row.id if class_row else None, enrolled_at=entry_date,
                completed_at=completed_at or None, active=1, notes=notes or None, created_at=now))
            db.session.flush()
            _mark_latest_history_current(student.id)
            if class_row and level in ACADEMIC_HISTORY_LEVELS:
                _sync_enrolment_for_history(student.id, class_row.id, session_row.id, entry_date)
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            outcomes.append({'line': line_no, 'ok': False, 'label': label, 'reason': f'could not be saved ({exc})'})
            continue
        outcomes.append({'line': line_no, 'ok': True, 'label': label})

    created = sum(1 for o in outcomes if o['ok'])
    skipped = len(outcomes) - created
    if created:
        audit_log('student_history_bulk_imported', 'school', 'session', None, {'created': created, 'skipped': skipped})
    flash(f'{created} enrolment history row{"" if created == 1 else "s"} imported' +
         (f', {skipped} row{"s" if skipped != 1 else ""} skipped.' if skipped else '.'),
         'success' if created else 'error')
    return render_template('admin_school_students_import_history_report.html', outcomes=outcomes, created=created, skipped=skipped)


def _looks_like_date(value):
    try:
        datetime.strptime(value, '%Y-%m-%d')
        return True
    except ValueError:
        return False
