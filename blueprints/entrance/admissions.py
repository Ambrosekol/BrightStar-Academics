"""The admissions funnel: entrance exam -> waitlist -> a real, enrolled student.

Every active candidate who has sat at least one paper is on the waitlist, ranked by their overall
percentage, until an admissions officer decides: **admit** (creates exactly what registering a
student by hand creates — the student record, a class enrolment, a generated admission number and a
one-time login, through the very same machinery as everywhere else a student is created — and,
optionally, a parent portal account from the guardian details already collected at candidate
registration) or **decline** (reversible, so a decision made in error can be undone; admitting is not,
because by then a real student and a real login exist).

Guarded by a single permission, ``candidates.admit``, separate from ``candidates.view``: a school
decides who is trusted to make admission decisions, distinct from who may merely see candidates.
"""

import re
from datetime import datetime, timezone

from flask import abort, flash, redirect, render_template, request, url_for
from sqlalchemy import select
from werkzeug.security import generate_password_hash

from app import _provision_student_account, _school_current_session, app
from blueprints.entrance.helpers import _candidate_results_summary
from blueprints.parents.helpers import _new_parent_password
from blueprints.school.helpers import _school_class_allowed
from core.entrance import ENTRY_GROUP_LABELS, entry_group_label, normalize_entry_group
from core.security import admin_required, audit_log, csrf_protect, current_admin
from models import (
    Admin, Candidate, ParentAccount, ParentStudentLink, School, SchoolClass, Student,
    StudentEnrolment, StudentNumberAllocation, db,
)
from services.student_number_generator import StudentNumberAllocationError, allocate_student_number

ADMISSIONS = 'admin_candidate_admissions'


def _classes():
    return db.session.scalars(select(SchoolClass).where(SchoolClass.active == 1).order_by(SchoolClass.level_order)).all()


def _split_name(full_name):
    """A candidate's one-field name as (first, middle, last) — a best-effort guess an admissions
    officer can correct on the admit form; never authoritative on its own."""
    parts = [p for p in full_name.split() if p]
    if not parts:
        return '', '', ''
    if len(parts) == 1:
        return parts[0], '', ''
    if len(parts) == 2:
        return parts[0], '', parts[1]
    return parts[0], ' '.join(parts[1:-1]), parts[-1]


def _waitlist_rows(entry_group=None, status=None):
    """Every candidate who has attempted at least one paper, with their admissions status,
    newest-scoring first within each status. ``[{..._candidate_results_summary row..., 'status',
    'admitted_student_id', 'decided_at', 'decided_by_name', 'note'}]``."""
    summaries = [r for r in _candidate_results_summary() if r['completed'] > 0]
    candidates = {c.id: c for c in db.session.scalars(select(Candidate).where(
        Candidate.id.in_([r['id'] for r in summaries] or [0])))}
    deciders = {a.id: a.display_name for a in db.session.scalars(select(Admin).where(
        Admin.id.in_({c.admission_decided_by for c in candidates.values() if c.admission_decided_by})))}
    rows = []
    for r in summaries:
        c = candidates.get(r['id'])
        if c is None:
            continue
        group = normalize_entry_group(r['target_class'])
        if entry_group and group != entry_group:
            continue
        if status and c.admission_status != status:
            continue
        rows.append({**r, 'entry_group': group, 'entry_group_label': entry_group_label(group) or r['target_class'],
                    'status': c.admission_status, 'admitted_student_id': c.admitted_student_id,
                    'decided_at': c.admission_decided_at, 'decided_by_name': deciders.get(c.admission_decided_by, ''),
                    'note': c.admission_note or ''})
    rows.sort(key=lambda r: (r['status'] != 'pending', -r['overall_percentage']))
    return rows


@app.route('/admin/candidates/admissions')
@admin_required
def admin_candidate_admissions():
    entry_group = request.args.get('entry_group', '').strip() or None
    status = request.args.get('status', '').strip() or None
    rows = _waitlist_rows(entry_group, status)
    return render_template('admin_candidate_admissions.html', rows=rows, classes=_classes(),
                           entry_groups=ENTRY_GROUP_LABELS, selected_group=entry_group, selected_status=status,
                           pending=sum(1 for r in rows if r['status'] == 'pending'))


@app.post('/admin/candidates/<int:cid>/admit')
@admin_required
@csrf_protect
def admin_candidate_admit(cid):
    me = current_admin()
    candidate = db.session.get(Candidate, cid)
    if candidate is None or not candidate.active:
        abort(404)
    if candidate.admission_status == 'admitted':
        flash('This candidate has already been admitted.', 'error')
        return redirect(url_for(ADMISSIONS))
    first = request.form.get('first_name', '').strip()
    middle = request.form.get('middle_name', '').strip()
    last = request.form.get('last_name', '').strip()
    gender = request.form.get('gender', '').strip()
    date_of_birth = request.form.get('date_of_birth', '').strip()
    class_id = request.form.get('class_id', type=int)
    make_parent = request.form.get('create_parent_account') == '1'

    errors = []
    if not first or not last:
        errors.append('First name and surname are required.')
    if gender and gender not in ('Male', 'Female'):
        errors.append('Choose a valid gender.')
    session_row = _school_current_session()
    if not session_row:
        errors.append('There is no active academic session to enrol this student into.')
    class_row = db.session.get(SchoolClass, class_id) if class_id else None
    if not class_row or not class_row.active:
        errors.append('Choose a class to admit this candidate into.')
    elif not _school_class_allowed(me['id'], class_row.id):
        errors.append('Choose a class within your authorised scope.')
    if errors:
        for message in errors:
            flash(message, 'error')
        return redirect(url_for(ADMISSIONS))

    school_id = db.session.scalar(select(School.id).where(School.active == 1))
    now = datetime.now(timezone.utc).isoformat()
    try:
        allocation = allocate_student_number(school_id=school_id, student_id=None, allocated_by=str(me['id']))
        number = allocation['student_number']
        student = Student(admission_no=number, first_name=first, middle_name=middle or None, last_name=last,
                          gender=gender or None, date_of_birth=date_of_birth or None,
                          guardian_name=candidate.parent_guardian_name, guardian_phone=candidate.primary_mobile,
                          guardian_email=candidate.parent_guardian_email, photo_path=candidate.photo_path,
                          created_at=now, active=1, school_id=school_id, student_number=number,
                          student_number_source='generated')
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
                                        enrolled_at=now, active=1))

        parent_username = parent_password = None
        if make_parent and (candidate.parent_guardian_email or candidate.primary_mobile):
            parent_username = ' '.join((candidate.parent_guardian_name or f'{first} {last} guardian').split()).lower()
            parent_username = re.sub(r'[^a-z0-9]+', '.', parent_username).strip('.') or f'parent{student.id}'
            base, suffix = parent_username, 1
            while db.session.scalar(select(ParentAccount.id).where(ParentAccount.username == parent_username)):
                suffix += 1
                parent_username = f'{base}-{suffix:02d}'
            parent_password = _new_parent_password()
            parent = ParentAccount(username=parent_username, display_name=candidate.parent_guardian_name or f'{first} {last}’s guardian',
                                   email=candidate.parent_guardian_email or None, phone=candidate.primary_mobile or None,
                                   password_hash=generate_password_hash(parent_password), active=1, password_must_change=1,
                                   created_at=now)
            db.session.add(parent)
            db.session.flush()
            db.session.add(ParentStudentLink(parent_id=parent.id, student_id=student.id,
                                             relationship=candidate.parent_guardian_relationship or None,
                                             active=1, created_at=now, created_by=me['id']))

        candidate.admission_status = 'admitted'
        candidate.admitted_student_id = student.id
        candidate.admission_decided_at = now
        candidate.admission_decided_by = me['id']
        candidate.admission_note = None
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        flash(f'{candidate.candidate_name} could not be admitted ({exc}).', 'error')
        return redirect(url_for(ADMISSIONS))

    audit_log('candidate_admitted', 'assessment', 'candidate', cid,
              {'student_id': student.id, 'class_id': class_row.id, 'parent_created': bool(parent_username)})
    return render_template('admin_candidate_admitted.html', candidate=candidate, student=student, class_name=class_row.name,
                           username=username, password=temp_password,
                           parent_username=parent_username, parent_password=parent_password)


@app.post('/admin/candidates/<int:cid>/decline')
@admin_required
@csrf_protect
def admin_candidate_decline(cid):
    candidate = db.session.get(Candidate, cid)
    if candidate is None:
        abort(404)
    if candidate.admission_status == 'admitted':
        flash('An admitted candidate cannot be declined.', 'error')
        return redirect(url_for(ADMISSIONS))
    me = current_admin()
    candidate.admission_status = 'declined'
    candidate.admission_decided_at = datetime.now(timezone.utc).isoformat()
    candidate.admission_decided_by = me['id']
    candidate.admission_note = ' '.join(request.form.get('note', '').split())[:300] or None
    db.session.commit()
    audit_log('candidate_declined', 'assessment', 'candidate', cid, {'note': candidate.admission_note})
    flash(f'{candidate.candidate_name} was moved off the waitlist.', 'success')
    return redirect(url_for(ADMISSIONS))


@app.post('/admin/candidates/<int:cid>/admission-reset')
@admin_required
@csrf_protect
def admin_candidate_admission_reset(cid):
    candidate = db.session.get(Candidate, cid)
    if candidate is None:
        abort(404)
    if candidate.admission_status == 'admitted':
        flash('An admitted candidate cannot be put back on the waitlist.', 'error')
        return redirect(url_for(ADMISSIONS))
    candidate.admission_status = 'pending'
    candidate.admission_decided_at = None
    candidate.admission_decided_by = None
    candidate.admission_note = None
    db.session.commit()
    audit_log('candidate_admission_reset', 'assessment', 'candidate', cid, {})
    flash(f'{candidate.candidate_name} is back on the waitlist.', 'success')
    return redirect(url_for(ADMISSIONS))
