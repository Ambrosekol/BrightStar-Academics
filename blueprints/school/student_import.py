"""Bulk student import: a CSV of students, validated and created in one pass.

Each valid row creates exactly what the "Add student" form creates by hand — a Student record, an
enrolment in the named class for the current session, and a login account with a generated student
number and a one-time password — using the very same numbering and account-provisioning machinery as
that form (app.py's ``_provision_student_account``, services/student_number_generator.py's
``allocate_student_number``), so a bulk-imported student is indistinguishable from one added by hand.
A school never types its own admission numbers, in bulk any more than one at a time: the platform's
numbering rules generate every one, so importing existing records still gets numbers that fit the
school's own pattern.

A row that fails validation is skipped, with a reason, and every other row is still tried — one bad
line never holds back the rest. Each valid row is created and committed on its own, so a failure
partway through a large file leaves everything already created in place.

The generated usernames and one-time passwords are shown once, on the results page, and never stored
in plain text anywhere — the same rule a single "add student" or "reset password" follows. This page
is the only place they can ever be read; downloading them as a file happens in the browser, from the
same page, never a second trip to the server.
"""

import csv
import io
from datetime import datetime, timezone

from flask import Response, flash, redirect, render_template, request, url_for
from sqlalchemy import select

from app import _provision_student_account, _school_current_session, app
from blueprints.school.helpers import _school_class_allowed, import_allowed, import_permission_required
from core.security import admin_access_error, admin_has_permission, admin_required, audit_log, csrf_protect, current_admin
from core.uploads import data_upload_limit_bytes, format_limit
from models import School, SchoolClass, Student, StudentEnrolment, StudentNumberAllocation, db
from services.student_number_generator import StudentNumberAllocationError, allocate_student_number

MAX_ROWS = 1000
# Every column is required: the guardian details drive the alerts parents receive and the sign-in a
# parent uses, and a middle name is part of the name the school prints on every record.
REQUIRED_COLUMNS = ('first_name', 'middle_name', 'last_name', 'gender', 'class', 'guardian_name', 'guardian_email', 'guardian_phone')
OPTIONAL_COLUMNS = ()
# A name reads naturally as first, middle, last: the template's column order says so, even though
# validation groups 'required' and 'optional' the other way round.
TEMPLATE_HEADER = ('first_name', 'middle_name', 'last_name', 'gender', 'class', 'guardian_name', 'guardian_email', 'guardian_phone')
_GENDERS = {'male': 'Male', 'female': 'Female'}


def _classes_by_name():
    return {c.name.strip().casefold(): c for c in db.session.scalars(
        select(SchoolClass).where(SchoolClass.active == 1))}


def render_import_page():
    """The bulk-import page: step 1 brings in new students, step 2 brings in history for students already here.

    Each step shows only to the staff member who may run it. The history constants are read from the
    history module here rather than at the top of this file, because that module imports this one.
    """
    from blueprints.school.student_history_import import (
        MAX_ROWS as HISTORY_MAX_ROWS, OPTIONAL_COLUMNS as HISTORY_OPTIONAL, OUTCOMES,
        REQUIRED_COLUMNS as HISTORY_REQUIRED, _ALLOWED_LEVELS,
    )
    from blueprints.school.results_import import MAX_ROWS as RESULTS_MAX_ROWS, OPTIONAL_COLUMNS as RESULTS_OPTIONAL, REQUIRED_COLUMNS as RESULTS_REQUIRED
    me = current_admin()
    can_students = import_allowed('school.students.create')
    can_history = import_allowed('student.history.manage')
    can_results = import_allowed('school.results.release')
    if not (can_students or can_history or can_results):
        # Each step is shown only to whoever may run it; the page itself is for anyone who may run one of them.
        return admin_access_error('school.students.create')
    return render_template('admin_school_students_import.html', columns=TEMPLATE_HEADER,
                           can_import_results=can_results,
                           results_required=RESULTS_REQUIRED, results_optional=RESULTS_OPTIONAL, results_max_rows=RESULTS_MAX_ROWS,
                           required_columns=REQUIRED_COLUMNS, optional_columns=OPTIONAL_COLUMNS, max_rows=MAX_ROWS,
                           can_import_students=can_students,
                           can_import_history=can_history,
                           history_required=HISTORY_REQUIRED, history_optional=HISTORY_OPTIONAL,
                           history_levels=sorted(_ALLOWED_LEVELS), history_max_rows=HISTORY_MAX_ROWS,
                           history_outcomes=OUTCOMES)


@app.route('/admin/school/students/import')
@admin_required
def admin_school_students_import():
    return render_import_page()


@app.route('/admin/school/students/import/template.csv')
@admin_required
@import_permission_required('school.students.create')
def admin_school_students_import_template():
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(TEMPLATE_HEADER)
    writer.writerow(['Ada', 'Nkem', 'Obi', 'Female', 'JSS 1', 'Mrs Obi', 'mrsobi@example.com', '08010000000'])
    response = Response(buf.getvalue(), mimetype='text/csv')
    response.headers['Content-Disposition'] = 'attachment; filename="student-import-template.csv"'
    return response


@app.post('/admin/school/students/import')
@admin_required
@csrf_protect
@import_permission_required('school.students.create')
def admin_school_students_import_run():
    me = current_admin()
    upload = request.files.get('csv_file')
    if not upload or not upload.filename:
        flash('Choose a CSV file to import.', 'error')
        return redirect(url_for('admin_school_students_import'))
    limit = data_upload_limit_bytes()
    raw = upload.read(limit + 1)
    if len(raw) > limit:
        flash(f'That file is larger than the {format_limit(limit)} limit.', 'error')
        return redirect(url_for('admin_school_students_import'))
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        flash('That file is not readable as plain-text CSV. Save it as CSV (UTF-8) from Excel or Google Sheets and try again.', 'error')
        return redirect(url_for('admin_school_students_import'))

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        flash('That file has no header row. Use the template.', 'error')
        return redirect(url_for('admin_school_students_import'))
    fields = {(name or '').strip().casefold(): name for name in reader.fieldnames}
    missing = [c for c in REQUIRED_COLUMNS if c not in fields]
    if missing:
        flash(f'The file is missing required column{"s" if len(missing) != 1 else ""}: {", ".join(missing)}.', 'error')
        return redirect(url_for('admin_school_students_import'))

    session_row = _school_current_session()
    if not session_row:
        flash('There is no active academic session to enrol students into.', 'error')
        return redirect(url_for('admin_school_students_import'))
    school_id = db.session.scalar(select(School.id).where(School.active == 1))
    classes = _classes_by_name()
    admin_id_str = str(me['id'])

    def get(row, col):
        key = fields.get(col)
        return (row.get(key) or '').strip() if key else ''

    outcomes = []
    for line_no, row in enumerate(reader, start=2):
        if line_no - 1 > MAX_ROWS:
            outcomes.append({'line': line_no, 'ok': False, 'name': '', 'reason': f'Only the first {MAX_ROWS} rows were processed; the rest of the file was not read.'})
            break

        first, middle, last = get(row, 'first_name'), get(row, 'middle_name'), get(row, 'last_name')
        gender = _GENDERS.get(get(row, 'gender').casefold())
        class_raw = get(row, 'class')
        class_row = classes.get(class_raw.casefold())
        guardian_name, guardian_email, guardian_phone = get(row, 'guardian_name'), get(row, 'guardian_email'), get(row, 'guardian_phone')
        name = f'{first} {last}'.strip()

        problems = []
        if not first or not last:
            problems.append('first name and surname are required')
        if not middle:
            problems.append('middle name is required')
        if not guardian_name:
            problems.append('guardian name is required')
        if not guardian_phone:
            problems.append('guardian phone is required')
        if not guardian_email:
            problems.append('guardian email is required')
        if not gender:
            problems.append("gender must be 'Male' or 'Female'")
        if not class_row:
            problems.append(f'class "{class_raw}" was not found' if class_raw else 'class is required')
        elif not _school_class_allowed(me['id'], class_row.id):
            problems.append(f'class "{class_row.name}" is outside your authorised scope')
        if guardian_email and ('@' not in guardian_email or '.' not in guardian_email.split('@')[-1]):
            problems.append('guardian email is not valid')
        if problems:
            outcomes.append({'line': line_no, 'ok': False, 'name': name, 'reason': '; '.join(problems)})
            continue

        try:
            allocation = allocate_student_number(school_id=school_id, student_id=None, allocated_by=admin_id_str)
            number = allocation['student_number']
            student = Student(admission_no=number, first_name=first, middle_name=middle or None, last_name=last,
                              gender=gender, guardian_name=guardian_name or None, guardian_email=guardian_email or None,
                              guardian_phone=guardian_phone or None, created_at=datetime.now(timezone.utc).isoformat(),
                              active=1, school_id=school_id, student_number=number, student_number_source='generated')
            db.session.add(student)
            db.session.flush()
            linked = db.session.execute(StudentNumberAllocation.__table__.update().where(
                StudentNumberAllocation.school_id == school_id, StudentNumberAllocation.student_number == number,
                StudentNumberAllocation.student_id.is_(None), StudentNumberAllocation.source == 'generated'
            ).values(student_id=student.id)).rowcount
            if linked != 1:
                raise StudentNumberAllocationError('the generated student number could not be linked to the new record')
            username, temp_password = _provision_student_account(student.id, number)
            db.session.add(StudentEnrolment(student_id=student.id, class_id=class_row.id, session_id=session_row['id'],
                                            enrolled_at=datetime.now(timezone.utc).isoformat(), active=1))
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            outcomes.append({'line': line_no, 'ok': False, 'name': name, 'reason': f'could not be saved ({exc})'})
            continue
        outcomes.append({'line': line_no, 'ok': True, 'name': name, 'class_name': class_row.name,
                         'admission_no': number, 'username': username, 'password': temp_password})

    created = sum(1 for o in outcomes if o['ok'])
    skipped = len(outcomes) - created
    if created:
        audit_log('students_bulk_imported', 'school', 'session', session_row['id'],
                  {'created': created, 'skipped': skipped, 'file': upload.filename})
    flash(f'{created} student{"" if created == 1 else "s"} imported' +
         (f', {skipped} row{"s" if skipped != 1 else ""} skipped.' if skipped else '.'),
         'success' if created else 'error')
    return render_template('admin_school_students_import_report.html', outcomes=outcomes, created=created, skipped=skipped)
