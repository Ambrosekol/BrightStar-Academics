"""Teachers: who is class teacher of each class, and who teaches each subject in it.

One page shows every class with its class teacher and subject teachers, and the subjects offered in
the class that nobody teaches yet. Beside it, each teacher's duties open for editing.

The School Admin can edit anyone's duties (as on the staff form). A head teacher - anyone holding
``school.staff.assign`` - can edit the duties of staff below them in the chain of authority
(core/school_structure.py), and only in the classes within their own reach, so a head of the
nursery section assigns nursery teachers and nobody else. Creating accounts and choosing roles stays
with the School Admin (the staff form), because that is how access is granted.
"""

from datetime import datetime, timezone

from flask import abort, flash, redirect, render_template, request, url_for
from sqlalchemy import select

from app import app
from blueprints.administration.helpers import (
    _duties_of, _duty_form_options, _duty_labels, _parse_duties, _save_duties,
)
from core.db_helpers import tuples
from core.school_structure import LEVEL_LABELS, LEVEL_RANK, TEACHING_LEVELS, level_or_default
from core.security import (
    admin_access_error, admin_has_permission, admin_rank, admin_required, audit_log, csrf_protect,
    current_admin, is_school_admin,
)
from models import (
    Admin, AdminRoleAssignment, AdminType, ClassSubject, SchoolClass, SchoolSubject, TeachingDuty, db,
)


def _may_assign(me):
    return is_school_admin(me) or admin_has_permission(me['id'], 'school.staff.assign')


def _may_edit(me, target_id):
    """The School Admin edits anyone but themselves; others only staff strictly below them."""
    if target_id == me['id']:
        return False
    target = db.session.get(Admin, target_id)
    if target is None or not target.active or (target.admin_type and target.admin_type.is_system):
        return False
    return is_school_admin(me) or admin_rank(target_id) > admin_rank(me['id'])


def _staff_levels():
    """{admin id: [(role name, level), ...]} for every active, non-system account."""
    out = {}
    via_roles = tuples(select(AdminRoleAssignment.admin_id, AdminType.name, AdminType.level)
                       .join(AdminType, AdminType.id == AdminRoleAssignment.admin_type_id)
                       .where(AdminType.active == 1, AdminType.is_system == 0))
    for aid, name, level in via_roles:
        out.setdefault(aid, []).append((name, level_or_default(level)))
    return out


@app.route('/admin/school/teaching')
@admin_required
def admin_school_teaching():
    me = current_admin()
    if not _may_assign(me):
        return admin_access_error('school.staff.assign')
    options = _duty_form_options(me)
    class_ids = [c['id'] for c in options['duty_classes']]
    names = {aid: name for aid, name in tuples(select(Admin.id, Admin.display_name).where(Admin.active == 1))}
    duties = tuples(select(TeachingDuty.admin_id, TeachingDuty.class_id, TeachingDuty.subject_id,
                           TeachingDuty.department)
                    .where(TeachingDuty.class_id.in_(class_ids or [0])))
    offered = {}
    for cid, sid, sname in tuples(select(ClassSubject.class_id, ClassSubject.subject_id, SchoolSubject.name)
                                  .join(SchoolSubject, SchoolSubject.id == ClassSubject.subject_id)
                                  .where(SchoolSubject.active == 1, ClassSubject.class_id.in_(class_ids or [0]))
                                  .order_by(SchoolSubject.name)):
        offered.setdefault(cid, []).append((sid, sname))
    subject_names = {s['id']: s['name'] for s in options['duty_subjects']}

    board = []
    for c in options['duty_classes']:
        mine = [d for d in duties if d[1] == c['id'] and d[0] in names]
        class_teachers = [names[aid] for aid, _cid, sid, _dept in mine if sid is None]
        taught = {}
        for aid, _cid, sid, dept in mine:
            if sid is None:
                continue
            taught.setdefault(sid, []).append(names[aid] + (f' ({dept})' if dept else ''))
        subjects = [{'name': subject_names.get(sid, '?'), 'teachers': teachers}
                    for sid, teachers in sorted(taught.items(), key=lambda kv: subject_names.get(kv[0], ''))]
        untaught = [sname for sid, sname in offered.get(c['id'], []) if sid not in taught]
        board.append({'class': c, 'class_teachers': class_teachers, 'subjects': subjects, 'untaught': untaught})

    levels = _staff_levels()
    with_duties = {d[0] for d in duties}
    teachers = []
    for aid, name in sorted(names.items(), key=lambda kv: kv[1].casefold()):
        roles = levels.get(aid, [])
        is_teacher = any(level in TEACHING_LEVELS for _n, level in roles) or aid in with_duties
        if not is_teacher or not _may_edit(me, aid):
            continue
        rank = min((LEVEL_RANK[level] for _n, level in roles), default=LEVEL_RANK['executive'])
        mine = {'class_teacher': [d[1] for d in duties if d[0] == aid and d[2] is None],
                'subjects': [{'class_id': d[1], 'subject_id': d[2], 'department': d[3] or ''}
                             for d in duties if d[0] == aid and d[2] is not None]}
        teachers.append({'id': aid, 'name': name, 'roles': [n for n, _l in roles],
                         'level': next((LEVEL_LABELS[c] for c, r in LEVEL_RANK.items() if r == rank), ''),
                         'duties': _duty_labels(mine, options)})
    return render_template('admin_school_teaching.html', board=board, teachers=teachers)


@app.route('/admin/school/teaching/<int:aid>', methods=['GET', 'POST'])
@admin_required
@csrf_protect
def admin_school_teaching_edit(aid):
    me = current_admin()
    if not _may_assign(me):
        return admin_access_error('school.staff.assign')
    if not _may_edit(me, aid):
        return admin_access_error('Assigning duties to staff at or above your own level')
    target = db.session.get(Admin, aid)
    options = _duty_form_options(me)
    reach = {c['id'] for c in options['duty_classes']}
    current = _duties_of(aid)
    # Duties in classes outside this editor's reach are kept as they are, and listed so nobody is surprised.
    outside = {'class_teacher': [c for c in current['class_teacher'] if c not in reach],
               'subjects': [r for r in current['subjects'] if r['class_id'] not in reach]}
    duties = {'class_teacher': [c for c in current['class_teacher'] if c in reach],
              'subjects': [r for r in current['subjects'] if r['class_id'] in reach]}
    all_options = _duty_form_options()
    errors = []
    if request.method == 'POST':
        duties, errors = _parse_duties(request.form, options)
        if not errors:
            now = datetime.now(timezone.utc).isoformat()
            _save_duties(aid, duties, me['id'], now, only_class_ids=None if is_school_admin(me) else reach)
            db.session.commit()
            audit_log('teaching_duties_updated', 'school', 'admin', aid,
                      {'teacher': target.display_name, 'duties': _duty_labels(duties, options)})
            flash(f'Teaching duties for {target.display_name} saved. They take effect on their next page.', 'success')
            return redirect(url_for('admin_school_teaching'))
    return render_template('admin_school_teaching_edit.html', target=target, duties=duties, errors=errors,
                           outside=_duty_labels(outside, all_options), **options)
