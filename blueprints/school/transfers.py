"""Class transfers: moving a JSS or SSS student to another subdivision of their class (JSS 1A to JSS 1B), or an SSS
student to another department (and so to a subdivision of that department), by request and approval.

The class teacher of the student's subdivision asks; nothing changes until the class teacher of the subdivision
the student would join approves it (core/subdivisions.py, approvers). Until then the student stays with the
teacher who asked. The School Admin and head teachers see every transfer and may approve, decline or cancel any of
them. Nobody approves their own request, so a department change in a class without subdivisions, where the same
class teacher would ask and approve, waits for the School Admin or a head teacher.
"""

from datetime import datetime, timezone

from flask import abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import select

from app import _school_current_session, app
from control_plane.config import badge_cache_seconds
from core.short_cache import remember
from core import subdivisions as sd
from core.school_structure import DEPARTMENTS, clean_department, is_senior
from core.security import (
    _admin_level_codes, admin_access_error, admin_required, audit_log, csrf_protect, current_admin, is_school_admin,
    teaching_duties,
)
from models import Admin, AdminNotification, ClassSubdivision, ClassTransfer, SchoolClass, Student, StudentEnrolment, db

PAGE = 'admin_school_transfers'
MAX_REASON = 500


# ---------------------------------------------------------------- who may do what
def _overseer(admin):
    """The School Admin or a head teacher: sees every transfer and may decide any of them."""
    if not admin:
        return False
    return is_school_admin(admin) or 'head_teacher' in _admin_level_codes(admin['id'])


def _my_classes(admin):
    """{class_id: SchoolClass} of the JSS and SSS classes this person may ask transfers for: every one for an
    overseer, else the ones they are class teacher of (of the whole class or of a subdivision)."""
    classes = {c.id: c for c in sd.secondary_classes()}
    if _overseer(admin):
        return classes
    mine = {d['class_id'] for d in teaching_duties(admin['id']) if d['subject_id'] is None}
    return {cid: c for cid, c in classes.items() if cid in mine}


def transfers_access(admin=None):
    """For the menu: {'show': bool, 'waiting': transfers waiting for this person's decision}. Read once a request."""
    admin = admin or current_admin()
    if not admin:
        return {'show': False, 'waiting': 0}
    cached = getattr(g, '_transfers_access', None)
    if cached is not None:
        return cached
    def compute(admin_id):
        show = bool(_my_classes(admin))
        return {'show': show, 'waiting': len(_waiting_for(admin)) if show else 0}

    try:
        # Asked on every staff page (the menu), so kept for a few seconds like the other menu counts and dropped
        # whenever anything changes (app.py, forget_counts_after_a_change).
        g._transfers_access = remember('transfers-menu', badge_cache_seconds(), compute, admin['id'])
    except Exception:
        db.session.rollback()
        g._transfers_access = {'show': False, 'waiting': 0}
    return g._transfers_access


def _may_decide(admin, transfer):
    if transfer.status != 'pending':
        return False
    if _overseer(admin):
        return True
    return admin['id'] != transfer.requested_by and admin['id'] in sd.approvers(transfer)


def _waiting_for(admin):
    pending = db.session.scalars(select(ClassTransfer).where(ClassTransfer.status == 'pending')
                                 .order_by(ClassTransfer.id)).all()
    return [t for t in pending if _may_decide(admin, t) and t.requested_by != admin['id']]


def _involves(admin, transfer, classes):
    """Whether a transfer belongs on this person's page: they asked, they may decide, or it is about a student of
    theirs (their subdivision, or their whole class)."""
    if _overseer(admin) or transfer.requested_by == admin['id'] or admin['id'] in sd.approvers(transfer):
        return True
    if transfer.class_id not in classes:
        return False
    limit = sd.teacher_subdivisions(admin['id'], transfer.class_id)
    return limit is None or transfer.from_subdivision_id in limit or transfer.to_subdivision_id in limit


# ---------------------------------------------------------------- reading
def _students(admin, classes, session_id):
    """The students this person may ask a transfer for, with their class, subdivision and department."""
    if not classes or not session_id:
        return []
    rows = db.session.execute(
        select(Student.id, Student.first_name, Student.middle_name, Student.last_name, Student.admission_no,
               Student.student_number, Student.department, StudentEnrolment.class_id, StudentEnrolment.subdivision_id)
        .join(StudentEnrolment, StudentEnrolment.student_id == Student.id)
        .where(StudentEnrolment.session_id == session_id, StudentEnrolment.active == 1, Student.active == 1,
               StudentEnrolment.class_id.in_(list(classes)))).all()
    rows = sd.filter_rows(admin['id'], [r._asdict() for r in rows]) if not _overseer(admin) else [r._asdict() for r in rows]
    for r in rows:
        r['name'] = ' '.join(p for p in (r['first_name'], r['middle_name'], r['last_name']) if p and str(p).strip())
        r['number'] = r['student_number'] or r['admission_no'] or ''
    rows.sort(key=lambda r: (classes[r['class_id']].level_order or 0, r['name'].casefold()))
    return rows


def _row(transfer, names, subs, classes, students, admin):
    sub = subs.get
    student = students.get(transfer.student_id)
    approvers = [a for a in sd.approvers(transfer) if a != transfer.requested_by]
    return {
        'id': transfer.id, 'status': transfer.status, 'reason': transfer.reason or '', 'note': transfer.decision_note or '',
        'student': student['name'] if student else 'A student no longer on roll',
        'number': student['number'] if student else '',
        'class': classes.get(transfer.class_id, ''),
        'from': sd.label(sub(transfer.from_subdivision_id)) if transfer.from_subdivision_id else classes.get(transfer.class_id, ''),
        'to': sd.label(sub(transfer.to_subdivision_id)) if transfer.to_subdivision_id else classes.get(transfer.class_id, ''),
        'from_department': transfer.from_department or '', 'to_department': transfer.to_department or '',
        'department_change': (transfer.to_department or '') != (transfer.from_department or ''),
        'requested_by': names.get(transfer.requested_by, ''), 'requested_at': (transfer.requested_at or '')[:16].replace('T', ' '),
        'decided_by': names.get(transfer.decided_by, ''), 'decided_at': (transfer.decided_at or '')[:16].replace('T', ' '),
        'approvers': ', '.join(names.get(a, '') for a in approvers if names.get(a)) or 'the School Admin or a head teacher',
        'mine': transfer.requested_by == admin['id'],
        'can_decide': _may_decide(admin, transfer),
        'can_cancel': transfer.status == 'pending' and (transfer.requested_by == admin['id'] or _overseer(admin)),
    }


def _page(errors=None, form=None, status=200):
    me = current_admin()
    classes = _my_classes(me)
    session_row = _school_current_session()
    students = _students(me, classes, session_row['id'] if session_row else None)
    all_classes = {c.id: c.name for c in db.session.scalars(select(SchoolClass)).all()}
    subs = {s.id: s for s in db.session.scalars(select(ClassSubdivision)).all()}
    transfers = [t for t in db.session.scalars(select(ClassTransfer).order_by(ClassTransfer.id.desc()).limit(400)).all()
                 if _involves(me, t, classes)]
    student_ids = {t.student_id for t in transfers}
    on_roll = {s.id: {'name': ' '.join(p for p in (s.first_name, s.middle_name, s.last_name) if p and str(p).strip()),
                      'number': s.student_number or s.admission_no or ''}
               for s in db.session.scalars(select(Student).where(Student.id.in_(student_ids or {0}))).all()}
    people = {t.requested_by for t in transfers} | {t.decided_by for t in transfers if t.decided_by}
    for t in transfers:
        if t.status == 'pending':
            people |= set(sd.approvers(t))
    names = {a.id: a.display_name for a in db.session.scalars(select(Admin).where(Admin.id.in_(people or {0}))).all()}
    rows = [_row(t, names, subs, all_classes, on_roll, me) for t in transfers]
    pending_by_student = {t.student_id for t in transfers if t.status == 'pending'}
    class_options = []
    for cid, c in classes.items():
        senior = is_senior(c.stage, c.name)
        options = [{'id': s.id, 'name': s.name, 'department': s.department or ''} for s in sd.subdivisions_of(cid)]
        if options or senior:
            class_options.append({'id': cid, 'name': c.name, 'senior': senior, 'subdivisions': options})
    movable = {c['id'] for c in class_options}
    students = [dict(s, subdivision=sd.label(subs.get(s['subdivision_id'])) if s['subdivision_id'] else '',
                     pending=s['id'] in pending_by_student)
                for s in students if s['class_id'] in movable]
    return render_template(
        'admin_school_transfers.html', incoming=[r for r in rows if r['can_decide']],
        outgoing=[r for r in rows if r['status'] == 'pending' and not r['can_decide']],
        history=[r for r in rows if r['status'] != 'pending'][:100], students=students, classes=class_options,
        departments=DEPARTMENTS, overseer=_overseer(me), session_row=session_row, errors=errors or [],
        form=form or {}, max_reason=MAX_REASON), status


# ---------------------------------------------------------------- telling people
def _notify(admin_ids, title, message, actor):
    now = datetime.now(timezone.utc).isoformat()
    for aid in {a for a in admin_ids if a and a != actor['id']}:
        db.session.add(AdminNotification(admin_id=aid, title=title, message=message, severity='info',
                                         action_url=url_for(PAGE), actor_admin_id=actor['id'],
                                         actor_username_snapshot=actor['username'],
                                         actor_display_name_snapshot=actor['display_name'], created_at=now))


def _overseer_ids():
    from core.security import admin_rank
    ids = [a for (a,) in db.session.execute(select(Admin.id).where(Admin.active == 1)).all()]
    return [a for a in ids if admin_rank(a) == 0 or 'head_teacher' in _admin_level_codes(a)]


# ---------------------------------------------------------------- pages
@app.route('/admin/school/transfers')
@admin_required
def admin_school_transfers():
    if not transfers_access()['show']:
        return admin_access_error('Class transfers are for the class teachers of JSS and SSS classes')
    return _page()


@app.post('/admin/school/transfers/request')
@admin_required
@csrf_protect
def admin_school_transfers_request():
    me = current_admin()
    classes = _my_classes(me)
    if not classes:
        return admin_access_error('Class transfers are for the class teachers of JSS and SSS classes')
    session_row = _school_current_session()
    form = {k: request.form.get(k, '').strip() for k in ('student_id', 'to_subdivision_id', 'to_department', 'reason')}
    if not session_row:
        return _page(['There is no current session, so nobody can be transferred yet.'], form, 400)
    student_id = request.form.get('student_id', type=int)
    student = next((s for s in _students(me, classes, session_row['id']) if s['id'] == student_id), None)
    if student is None:
        return _page(['Choose a student of your own class or subdivision.'], form, 400)
    if sd.pending_transfer(student_id):
        return _page([f'{student["name"]} already has a transfer waiting. Cancel it first to ask for a different one.'], form, 400)
    cls = classes[student['class_id']]
    senior = is_senior(cls.stage, cls.name)
    subdivisions = {s.id: s for s in sd.subdivisions_of(cls.id)}
    from_dept = clean_department(student['department']) if senior else None
    to_dept = clean_department(form['to_department']) if senior and form['to_department'] else from_dept
    errors = []
    if senior and form['to_department'] and not to_dept:
        errors.append('Choose Science, Art or Commercial as the new department.')
    to_sub = None
    if subdivisions:
        to_sub = subdivisions.get(request.form.get('to_subdivision_id', type=int) or student['subdivision_id'])
        if to_sub is None:
            errors.append(f'Choose the {cls.name} subdivision the student should move to.')
        elif senior and to_dept and to_sub.department not in (None, '', to_dept):
            errors.append(f'{sd.label(to_sub)} is for {to_sub.department} students. Choose a {to_dept} subdivision, or one with no department.')
    reason = ' '.join(form['reason'].split())[:MAX_REASON]
    if not errors and (to_sub.id if to_sub else None) == student['subdivision_id'] and (to_dept or None) == (from_dept or None):
        errors.append('Nothing would change: choose another subdivision' + (' or department.' if senior else '.'))
    if errors:
        return _page(errors, form, 400)
    now = datetime.now(timezone.utc).isoformat()
    transfer = ClassTransfer(student_id=student_id, session_id=session_row['id'], class_id=cls.id,
                             from_subdivision_id=student['subdivision_id'], to_subdivision_id=to_sub.id if to_sub else None,
                             from_department=from_dept, to_department=to_dept, reason=reason or None, status='pending',
                             requested_by=me['id'], requested_at=now)
    db.session.add(transfer)
    db.session.flush()
    approvers = [a for a in sd.approvers(transfer) if a != me['id']] or _overseer_ids()
    move = sd.label(to_sub) if to_sub else cls.name
    if to_dept != from_dept:
        move += f', {to_dept}'
    _notify(approvers, 'Class transfer to approve',
            f'{me["display_name"]} asks to move {student["name"]} to {move}.', me)
    db.session.commit()
    audit_log('class_transfer_requested', 'school', 'student', student_id,
              {'transfer_id': transfer.id, 'class_id': cls.id, 'from_subdivision_id': transfer.from_subdivision_id,
               'to_subdivision_id': transfer.to_subdivision_id, 'from_department': from_dept, 'to_department': to_dept})
    flash(f'Transfer asked for {student["name"]}. They stay with you until it is approved.', 'success')
    return redirect(url_for(PAGE))


def _transfer_or_404(tid):
    transfer = db.session.get(ClassTransfer, tid)
    if transfer is None:
        abort(404)
    return transfer


@app.post('/admin/school/transfers/<int:tid>/approve')
@admin_required
@csrf_protect
def admin_school_transfers_approve(tid):
    me = current_admin()
    transfer = _transfer_or_404(tid)
    if not _may_decide(me, transfer):
        flash('This transfer is not waiting for you (it may already have been decided).', 'error')
        return redirect(url_for(PAGE))
    target = db.session.get(ClassSubdivision, transfer.to_subdivision_id) if transfer.to_subdivision_id else None
    if transfer.to_subdivision_id and (target is None or not target.active):
        flash('The subdivision this student would join has been removed, so the transfer cannot be made. Decline it instead.', 'error')
        return redirect(url_for(PAGE))
    try:
        sd.apply_transfer(transfer)
    except ValueError as exc:
        flash(str(exc) + ' Decline it instead.', 'error')
        return redirect(url_for(PAGE))
    now = datetime.now(timezone.utc).isoformat()
    transfer.status, transfer.decided_by, transfer.decided_at = 'approved', me['id'], now
    transfer.decision_note = ' '.join(request.form.get('note', '').split())[:MAX_REASON] or None
    student = db.session.get(Student, transfer.student_id)
    name = ' '.join(p for p in (student.first_name, student.last_name) if p) if student else 'The student'
    _notify([transfer.requested_by], 'Class transfer approved',
            f'{me["display_name"]} approved moving {name} to {sd.label(target) if target else "their new department"}.', me)
    db.session.commit()
    audit_log('class_transfer_approved', 'school', 'student', transfer.student_id, {'transfer_id': transfer.id})
    flash(f'Transfer approved: {name} is now in {sd.label(target) if target else transfer.to_department}.', 'success')
    return redirect(url_for(PAGE))


@app.post('/admin/school/transfers/<int:tid>/decline')
@admin_required
@csrf_protect
def admin_school_transfers_decline(tid):
    me = current_admin()
    transfer = _transfer_or_404(tid)
    if not _may_decide(me, transfer):
        flash('This transfer is not waiting for you (it may already have been decided).', 'error')
        return redirect(url_for(PAGE))
    now = datetime.now(timezone.utc).isoformat()
    transfer.status, transfer.decided_by, transfer.decided_at = 'declined', me['id'], now
    transfer.decision_note = ' '.join(request.form.get('note', '').split())[:MAX_REASON] or None
    student = db.session.get(Student, transfer.student_id)
    name = ' '.join(p for p in (student.first_name, student.last_name) if p) if student else 'the student'
    _notify([transfer.requested_by], 'Class transfer declined',
            f'{me["display_name"]} declined moving {name}.' + (f' “{transfer.decision_note}”' if transfer.decision_note else ''), me)
    db.session.commit()
    audit_log('class_transfer_declined', 'school', 'student', transfer.student_id, {'transfer_id': transfer.id})
    flash(f'Transfer declined: {name} stays where they are.', 'success')
    return redirect(url_for(PAGE))


@app.post('/admin/school/transfers/<int:tid>/cancel')
@admin_required
@csrf_protect
def admin_school_transfers_cancel(tid):
    me = current_admin()
    transfer = _transfer_or_404(tid)
    if transfer.status != 'pending' or not (transfer.requested_by == me['id'] or _overseer(me)):
        flash('Only a waiting transfer can be cancelled, by whoever asked for it.', 'error')
        return redirect(url_for(PAGE))
    transfer.status, transfer.decided_by = 'cancelled', me['id']
    transfer.decided_at = datetime.now(timezone.utc).isoformat()
    transfer.decision_note = 'Cancelled by ' + me['display_name']
    db.session.commit()
    audit_log('class_transfer_cancelled', 'school', 'student', transfer.student_id, {'transfer_id': transfer.id})
    flash('Transfer cancelled.', 'success')
    return redirect(url_for(PAGE))


app.jinja_env.globals.update(class_transfers_access=transfers_access)
