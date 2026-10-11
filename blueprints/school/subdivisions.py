"""Settings, Class subdivisions: splitting a JSS or SSS class into subdivisions (JSS 1A, JSS 2 Gold, SS 1A Science),
and locking the set-up once it is right.

The School Admin adds, renames and removes the subdivisions of each class, and gives an SSS subdivision a department
when it is for one department only. Saving a class spreads its students who are in no subdivision yet across them, in
turn (core/subdivisions.py). Removing a subdivision moves its students into the class's other subdivisions, ends its
class teachers' duties and cancels transfers into or out of it. Locking stops every change until the School Admin
unlocks it again, so a set-up that classes and class teachers depend on is not changed by accident.
"""

import re
from datetime import datetime, timezone

from flask import flash, redirect, render_template, request, url_for
from sqlalchemy import delete as sa_delete, select, update as sa_update

from app import _school_current_session, app
from core import subdivisions as sd
from core.school_structure import DEPARTMENTS, clean_department, is_senior
from core.security import admin_access_error, admin_required, audit_log, csrf_protect, current_admin, is_school_admin
from models import Admin, ClassSubdivision, ClassTransfer, SchoolClass, TeachingDuty, db

PAGE = 'admin_school_subdivisions'
MAX_PER_CLASS = 12
NAME_RULE = re.compile(r"^[\w .,'&()/-]{1,40}$", re.UNICODE)


def _teachers_by_subdivision():
    rows = db.session.execute(select(TeachingDuty.subdivision_id, Admin.display_name)
                              .join(Admin, Admin.id == TeachingDuty.admin_id)
                              .where(TeachingDuty.subdivision_id.is_not(None), TeachingDuty.subject_id.is_(None),
                                     Admin.active == 1)).all()
    out = {}
    for sid, name in rows:
        out.setdefault(sid, []).append(name)
    return out


def _page(errors=None, status=200):
    session_row = _school_current_session()
    counts = sd.counts(session_row['id']) if session_row else {}
    teachers = _teachers_by_subdivision()
    subdivided = sd.all_subdivisions()
    classes = []
    for c in sd.secondary_classes():
        subs = subdivided.get(c.id, [])
        class_total = 0
        if session_row:
            from models import Student, StudentEnrolment
            from sqlalchemy import func
            class_total = db.session.scalar(select(func.count()).select_from(StudentEnrolment)
                                            .join(Student, Student.id == StudentEnrolment.student_id)
                                            .where(StudentEnrolment.class_id == c.id, StudentEnrolment.active == 1,
                                                   StudentEnrolment.session_id == session_row['id'], Student.active == 1)) or 0
        placed = sum(counts.get(s.id, 0) for s in subs)
        classes.append({'id': c.id, 'name': c.name, 'senior': is_senior(c.stage, c.name), 'students': class_total,
                        'unplaced': class_total - placed if subs else 0,
                        'subdivisions': [{'id': s.id, 'name': s.name, 'department': s.department or '',
                                          'students': counts.get(s.id, 0), 'teachers': teachers.get(s.id, [])}
                                         for s in subs]})
    lock = sd.lock_state()
    locked_by = db.session.get(Admin, lock['by']).display_name if lock['locked'] and lock['by'] and db.session.get(Admin, lock['by']) else ''
    return render_template('admin_school_subdivisions.html', classes=classes, lock=lock, locked_by=locked_by,
                           departments=DEPARTMENTS, session_row=session_row, errors=errors or [],
                           max_per_class=MAX_PER_CLASS), status


@app.route('/admin/school/subdivisions')
@admin_required
def admin_school_subdivisions():
    me = current_admin()
    if not is_school_admin(me):
        return admin_access_error('School Admin only: class subdivisions')
    return _page()


@app.post('/admin/school/subdivisions/<int:class_id>')
@admin_required
@csrf_protect
def admin_school_subdivisions_save(class_id):
    """Save one class's subdivisions: rename (sub_id + sub_name), set an SSS department (sub_department), remove
    (sub_remove), and add (new_name + new_department rows)."""
    me = current_admin()
    if not is_school_admin(me):
        return admin_access_error('School Admin only: class subdivisions')
    if sd.lock_state()['locked']:
        flash('Class subdivisions are locked. Unlock them first to make changes.', 'error')
        return redirect(url_for(PAGE))
    cls = db.session.get(SchoolClass, class_id)
    if cls is None or not cls.active or cls.id not in {c.id for c in sd.secondary_classes()}:
        flash('Only an active JSS or SSS class can be split into subdivisions.', 'error')
        return redirect(url_for(PAGE))
    senior = is_senior(cls.stage, cls.name)
    existing = {s.id: s for s in sd.subdivisions_of(cls.id)}
    errors = []

    def clean_name(raw):
        name = ' '.join((raw or '').split())
        if not name:
            return None
        if not NAME_RULE.match(name):
            errors.append(f'"{name}" is not a usable name: use letters, digits, spaces and . , \' & ( ) / - only, up to 40 characters.')
            return None
        return name

    def clean_dept(raw):
        if not senior or not (raw or '').strip():
            return None
        dept = clean_department(raw)
        if not dept:
            errors.append('Choose Science, Art or Commercial as a subdivision\'s department, or none.')
        return dept

    keep, removed, renamed = [], [], []
    for sid_raw, name_raw, dept_raw in zip(request.form.getlist('sub_id'), request.form.getlist('sub_name'),
                                           request.form.getlist('sub_department') if senior else [''] * len(request.form.getlist('sub_id'))):
        try:
            sub = existing.get(int(sid_raw))
        except (TypeError, ValueError):
            sub = None
        if sub is None:
            continue
        if str(sub.id) in request.form.getlist('sub_remove'):
            removed.append(sub)
            continue
        name = clean_name(name_raw)
        if name is None and not errors:
            errors.append('Every subdivision needs a name. Tick Remove to take one away.')
        keep.append((sub, name, clean_dept(dept_raw)))
    new_rows = [(clean_name(n), clean_dept(d)) for n, d in zip(request.form.getlist('new_name'),
                                                                 request.form.getlist('new_department') if senior else [''] * len(request.form.getlist('new_name')))
                if (n or '').strip()]
    names = [n for _s, n, _d in keep] + [n for n, _d in new_rows]
    if len({(n or '').casefold() for n in names}) != len(names):
        errors.append('Two subdivisions of a class cannot share a name.')
    if len(names) > MAX_PER_CLASS:
        errors.append(f'A class can have at most {MAX_PER_CLASS} subdivisions.')
    if len(names) == 1:
        errors.append('A class split into subdivisions needs at least two. Remove the last one instead to undo the split.')
    if errors:
        return _page(sorted(set(errors)), 400)

    now = datetime.now(timezone.utc).isoformat()
    changes = []
    for sub, name, dept in keep:
        if name != sub.name or (dept or None) != (sub.department or None):
            changes.append(f'{sub.name} → {name}{" (" + dept + ")" if dept else ""}')
            sub.name, sub.department = name, dept
            renamed.append(sub)
    position = max([s.position for s in existing.values()] or [0])
    for name, dept in new_rows:
        position += 1
        db.session.add(ClassSubdivision(class_id=cls.id, name=name, department=dept, position=position, active=1,
                                        created_at=now, created_by=me['id']))
        changes.append(f'added {name}{" (" + dept + ")" if dept else ""}')
    for sub in removed:
        sub.active = 0
        changes.append(f'removed {sub.name}')
        db.session.execute(sa_delete(TeachingDuty).where(TeachingDuty.subdivision_id == sub.id))
        db.session.execute(sa_update(ClassTransfer).where(
            ClassTransfer.status == 'pending',
            (ClassTransfer.to_subdivision_id == sub.id) | (ClassTransfer.from_subdivision_id == sub.id))
            .values(status='cancelled', decided_by=me['id'], decided_at=now,
                    decision_note=f'Cancelled: {sub.name} was removed from Class subdivisions.'))
    db.session.flush()
    session_row = _school_current_session()
    placed = sd.spread(cls.id, session_row['id']) if session_row else 0
    db.session.commit()
    audit_log('class_subdivisions_saved', 'school', 'class', cls.id,
              {'class': cls.name, 'changes': changes, 'students_placed': placed})
    message = f'{cls.name} saved.'
    if placed:
        message += f' {placed} student{"" if placed == 1 else "s"} placed in subdivisions, in turn.'
    flash(message, 'success')
    return redirect(url_for(PAGE) + f'#class-{cls.id}')


@app.post('/admin/school/subdivisions/lock')
@admin_required
@csrf_protect
def admin_school_subdivisions_lock():
    me = current_admin()
    if not is_school_admin(me):
        return admin_access_error('School Admin only: class subdivisions')
    locking = request.form.get('lock') == '1'
    sd.set_lock(locking, me['id'])
    db.session.commit()
    audit_log('class_subdivisions_locked' if locking else 'class_subdivisions_unlocked', 'school', 'settings', None, {})
    flash('Class subdivisions locked. Unlock them to make changes.' if locking else 'Class subdivisions unlocked: they can be changed again.', 'success')
    return redirect(url_for(PAGE))
