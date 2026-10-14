"""Teachers: who is class teacher of each class (and of each subdivision), and who teaches each subject.

The page has two tabs. **Class teachers**: every class, with a row for the whole class and one for each of its
subdivisions (an SSS class is split by department: Science, Commercial, Art - core/subdivisions.py), each with its
class teacher. **Subject teachers**: every teacher with the subjects they teach, and the subjects nobody teaches yet
(in SSS, the departments of a subject still without a teacher). Any of these can be removed, replaced by someone else
at once, or given a teacher, in a small pop-up; each teacher's whole set of duties still opens in its own editor.

The School Admin can change anyone's duties (as on the staff form). A head teacher - anyone holding
``school.staff.assign`` - can change the duties of staff below them in the chain of authority
(core/school_structure.py), and only in the classes within their own reach, so a head of the nursery section assigns
nursery teachers and nobody else. A subject is taught by one teacher in a class (in SSS, one per department): giving it
to someone else replaces whoever had it. Creating accounts and choosing roles stays with the School Admin (the staff
form), because that is how access is granted.
"""

from datetime import datetime, timezone

from flask import flash, redirect, render_template, request, url_for
from sqlalchemy import delete as sa_delete, select

from app import _school_current_session, app
from blueprints.administration.helpers import (
    _duties_of, _duty_form_options, _duty_labels, _held, _parse_duties, _save_duties, duty_clash,
)
from core import subdivisions as sd
from core.db_helpers import tuples
from core.school_structure import LEVEL_LABELS, LEVEL_RANK, TEACHING_LEVELS, clean_department, level_or_default
from core.security import (
    admin_access_error, admin_has_permission, admin_rank, admin_required, audit_log, csrf_protect,
    current_admin, is_school_admin,
)
from models import (
    Admin, AdminRoleAssignment, AdminType, ClassSubdivision, ClassSubject, SchoolSubject, TeachingDuty, db,
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


def _teachers(me, duties):
    """The teachers this person may give duties to: [{'id', 'name', 'level', 'roles'}], by name."""
    names = {aid: name for aid, name in tuples(select(Admin.id, Admin.display_name).where(Admin.active == 1))}
    levels = _staff_levels()
    with_duties = {d[0] for d in duties}
    out = []
    for aid, name in sorted(names.items(), key=lambda kv: kv[1].casefold()):
        roles = levels.get(aid, [])
        if not (any(level in TEACHING_LEVELS for _n, level in roles) or aid in with_duties) or not _may_edit(me, aid):
            continue
        rank = min((LEVEL_RANK[level] for _n, level in roles), default=LEVEL_RANK['executive'])
        out.append({'id': aid, 'name': name, 'roles': [n for n, _l in roles],
                    'level': next((LEVEL_LABELS[c] for c, r in LEVEL_RANK.items() if r == rank), '')})
    return out


def _page_data(me):
    options = _duty_form_options(me)
    if is_school_admin(me):
        current = _school_current_session()
        if sd.ensure_sss_defaults(current['id'] if current else None):
            db.session.commit()
            options = _duty_form_options(me)
    class_ids = [c['id'] for c in options['duty_classes']]
    names = {aid: name for aid, name in tuples(select(Admin.id, Admin.display_name).where(Admin.active == 1))}
    duties = [d for d in tuples(select(TeachingDuty.admin_id, TeachingDuty.class_id, TeachingDuty.subject_id,
                                       TeachingDuty.department, TeachingDuty.subdivision_id)
                                .where(TeachingDuty.class_id.in_(class_ids or [0]))) if d[0] in names]
    teachers = _teachers(me, duties)
    editable = {t['id'] for t in teachers}
    subjects = {s['id']: s for s in options['duty_subjects']}
    subdivisions = sd.all_subdivisions()

    def person(aid):
        return {'id': aid, 'name': names[aid], 'editable': aid in editable}

    # Class teachers: the whole class, then each subdivision.
    classes = []
    for c in options['duty_classes']:
        slots = [{'label': 'Whole class', 'subdivision': None, 'department': None,
                  'teachers': [person(d[0]) for d in duties if d[1] == c['id'] and d[2] is None and not d[4]]}]
        for s in subdivisions.get(c['id'], []):
            slots.append({'label': sd.label(s), 'subdivision': s.id, 'department': s.department,
                          'teachers': [person(d[0]) for d in duties if d[2] is None and d[4] == s.id]})
        classes.append({'id': c['id'], 'name': c['name'], 'senior': c['senior'], 'slots': slots,
                        'gap': not any(slot['teachers'] for slot in slots)})

    # Subject teachers: each teacher with their subjects, then what nobody teaches.
    cname = {c['id']: c['name'] for c in options['duty_classes']}
    senior = {c['id']: c['senior'] for c in options['duty_classes']}
    by_teacher = {}
    for aid, cid, sid, dept, _sub in duties:
        if sid is None or sid not in subjects:
            continue
        by_teacher.setdefault(aid, []).append({'class_id': cid, 'class_name': cname.get(cid, ''), 'subject_id': sid,
                                               'subject_name': subjects[sid]['name'], 'department': dept or ''})
    rows = []
    listed = {t['id'] for t in teachers}
    for t in teachers + [{'id': aid, 'name': names[aid], 'roles': [], 'level': ''} for aid in by_teacher if aid not in listed]:
        mine = sorted(by_teacher.get(t['id'], []), key=lambda r: (r['class_name'], r['subject_name'], r['department']))
        rows.append(dict(t, subjects=mine, editable=t['id'] in editable))
    offered = {}
    for cid, sid in tuples(select(ClassSubject.class_id, ClassSubject.subject_id)
                           .join(SchoolSubject, SchoolSubject.id == ClassSubject.subject_id)
                           .where(SchoolSubject.active == 1, ClassSubject.class_id.in_(class_ids or [0]))):
        offered.setdefault(cid, []).append(sid)
    taken = {}
    for _aid, cid, sid, dept, _sub in duties:
        if sid is not None:
            taken.setdefault((cid, sid), set()).add(dept or '')
    untaught = []
    for c in options['duty_classes']:
        for sid in sorted(offered.get(c['id'], []), key=lambda i: subjects[i]['name'] if i in subjects else ''):
            if sid not in subjects:
                continue
            have = taken.get((c['id'], sid), set())
            if '' in have:
                continue
            if c['senior']:
                missing = [d for d in subjects[sid]['departments'] if d not in have]
                if have and missing:
                    untaught += [{'class_id': c['id'], 'class_name': c['name'], 'subject_id': sid,
                                  'subject_name': subjects[sid]['name'], 'department': d} for d in missing]
                    continue
                if have:
                    continue
            untaught.append({'class_id': c['id'], 'class_name': c['name'], 'subject_id': sid,
                             'subject_name': subjects[sid]['name'], 'department': ''})
    return {'classes': classes, 'rows': rows, 'untaught': untaught, 'teachers': teachers,
            'class_gaps': sum(1 for c in classes for s in c['slots'] if not s['teachers']),
            'senior': senior}


@app.route('/admin/school/teaching')
@admin_required
def admin_school_teaching():
    me = current_admin()
    if not _may_assign(me):
        return admin_access_error('school.staff.assign')
    tab = 'subjects' if request.args.get('tab') == 'subjects' else 'classes'
    return render_template('admin_school_teaching.html', tab=tab, **_page_data(me))


def _int(name):
    try:
        return int(request.form.get(name) or 0) or None
    except (TypeError, ValueError):
        return None


@app.post('/admin/school/teaching/change')
@admin_required
@csrf_protect
def admin_school_teaching_change():
    """One duty at a time: remove a class or subject teacher, replace them with someone else, or give a teacher to a
    class, subdivision or subject that has none. ``kind`` is 'class' or 'subject'."""
    me = current_admin()
    if not _may_assign(me):
        return admin_access_error('school.staff.assign')
    kind = 'subject' if request.form.get('kind') == 'subject' else 'class'
    back = redirect(url_for('admin_school_teaching', tab='subjects' if kind == 'subject' else None))
    options = _duty_form_options(me)
    classes = {c['id']: c for c in options['duty_classes']}
    class_id, subdivision_id, subject_id = _int('class_id'), _int('subdivision_id'), _int('subject_id')
    from_id, to_id = _int('from_admin'), _int('to_admin')
    cls = classes.get(class_id)
    if cls is None:
        flash('That class is not one you look after.', 'error')
        return back
    if not from_id and not to_id:
        flash('Choose who teaches it.', 'error')
        return back
    for aid in (from_id, to_id):
        if aid and not _may_edit(me, aid):
            return admin_access_error('Changing the duties of staff at or above your own level')
    if to_id and to_id not in {t['id'] for t in _teachers(me, [])} and not db.session.scalar(
            select(TeachingDuty.id).where(TeachingDuty.admin_id == to_id).limit(1)):
        flash('Choose a teacher: give them the Class Teacher or Subject Teacher role on their staff record first.', 'error')
        return back
    department = None
    if kind == 'class':
        if subdivision_id and not db.session.scalar(select(ClassSubdivision.id).where(
                ClassSubdivision.id == subdivision_id, ClassSubdivision.class_id == class_id, ClassSubdivision.active == 1)):
            flash('That subdivision is not part of the class.', 'error')
            return back
        match = [TeachingDuty.class_id == class_id, TeachingDuty.subject_id.is_(None),
                 TeachingDuty.subdivision_id == subdivision_id if subdivision_id else TeachingDuty.subdivision_id.is_(None)]
        what = (sd.label(db.session.get(ClassSubdivision, subdivision_id)) if subdivision_id else cls['name'])
        what = f'class teacher of {what}'
    else:
        subject = next((s for s in options['duty_subjects'] if s['id'] == subject_id), None)
        if subject is None or class_id not in subject['class_ids'] and not from_id:
            flash('That subject is not offered to the class.', 'error')
            return back
        department = clean_department(request.form.get('department')) if cls['senior'] else None
        if department and department not in subject['departments']:
            flash(f"{subject['name']} is not taken by the {department} department.", 'error')
            return back
        match = [TeachingDuty.class_id == class_id, TeachingDuty.subject_id == subject_id,
                 TeachingDuty.department == department if department else TeachingDuty.department.is_(None)]
        what = f"{subject['name']} in {cls['name']}" + (f' ({department})' if department else '')
    held_by = [aid for (aid,) in tuples(select(TeachingDuty.admin_id).where(*match))]
    if from_id and from_id not in held_by:
        flash('That duty has changed since the page was opened. Look again.', 'error')
        return back
    if to_id and not from_id and held_by:
        flash(f'{what[0].upper() + what[1:]} already has a teacher. Replace them instead.', 'error')
        return back
    if to_id and to_id == from_id:
        flash('Nothing changed: that is the same teacher.', 'success')
        return back
    if kind == 'subject' and to_id and not from_id and duty_clash(options['duty_taken'], class_id, subject_id, department):
        flash(f'{what} already has a teacher (in SSS, maybe for one of its departments). Replace them instead.', 'error')
        return back
    now = datetime.now(timezone.utc).isoformat()
    if from_id:
        db.session.execute(sa_delete(TeachingDuty).where(TeachingDuty.admin_id == from_id, *match))
    if to_id and to_id not in held_by:
        db.session.add(TeachingDuty(admin_id=to_id, class_id=class_id, subject_id=subject_id if kind == 'subject' else None,
                                    department=department, subdivision_id=subdivision_id if kind == 'class' else None,
                                    created_at=now, created_by=me['id']))
    db.session.commit()
    who = {aid: name for aid, name in tuples(select(Admin.id, Admin.display_name).where(Admin.id.in_([a for a in (from_id, to_id) if a])))}
    action = 'teaching_duty_replaced' if from_id and to_id else ('teaching_duty_removed' if from_id else 'teaching_duty_assigned')
    audit_log(action, 'school', 'teaching_duty', class_id, {'duty': what, 'from': who.get(from_id), 'to': who.get(to_id)})
    if from_id and to_id:
        flash(f'{who.get(to_id)} is now {what if kind == "class" else "teaching " + what}, instead of {who.get(from_id)}.', 'success')
    elif from_id:
        flash(f'{who.get(from_id)} no longer {"is " + what if kind == "class" else "teaches " + what}.', 'success')
    else:
        flash(f'{who.get(to_id)} is now {what if kind == "class" else "teaching " + what}.', 'success')
    return back


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
    options = _duty_form_options(me, exclude=aid)
    reach = {c['id'] for c in options['duty_classes']}
    current = _duties_of(aid)
    # Duties in classes outside this editor's reach are kept as they are, and listed so nobody is surprised.
    sub_in_reach = {s['id'] for c in options['duty_classes'] for s in c.get('subdivisions', [])}
    outside = {'class_teacher': [c for c in current['class_teacher'] if c not in reach],
               'class_teacher_subdivisions': [x for x in current['class_teacher_subdivisions'] if x not in sub_in_reach],
               'subjects': [r for r in current['subjects'] if r['class_id'] not in reach]}
    duties = {'class_teacher': [c for c in current['class_teacher'] if c in reach],
              'class_teacher_subdivisions': [x for x in current['class_teacher_subdivisions'] if x in sub_in_reach],
              'subjects': [r for r in current['subjects'] if r['class_id'] in reach]}
    all_options = _duty_form_options()
    errors = []
    if request.method == 'POST':
        duties, errors = _parse_duties(request.form, options, keep=_held(current))
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
