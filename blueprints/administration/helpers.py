"""Private helpers used only by admin account creation/editing in this
package.
"""

from sqlalchemy import delete as sa_delete

from models import AdminRoleAssignment, db
from core.db_helpers import _ignore_insert


def _sync_admin_roles(admin_id, role_ids, granted_by, now):
    ids=[]
    for rid in role_ids:
        try: rid=int(rid)
        except (TypeError,ValueError): continue
        if rid not in ids: ids.append(rid)
    db.session.execute(sa_delete(AdminRoleAssignment).where(AdminRoleAssignment.admin_id==admin_id))
    _ignore_insert(AdminRoleAssignment, [
        {'admin_id':admin_id,'admin_type_id':rid,'assigned_at':now,'assigned_by':granted_by}
        for rid in ids
    ])
    return ids

def _admin_contact_fields(form):
    return {
        'email': form.get('email','').strip().lower(),
        'phone': form.get('phone','').strip(),
        'whatsapp': form.get('whatsapp','').strip(),
    }

def _validate_admin_contact_fields(contact):
    errors=[]
    email=contact['email']
    if email and (len(email)>254 or '@' not in email or '.' not in email.rsplit('@',1)[-1]): errors.append('Enter a valid email address.')
    for label,key in (('Phone','phone'),('WhatsApp','whatsapp')):
        value=contact[key]
        if value and len(''.join(ch for ch in value if ch.isdigit())) < 7: errors.append(f'Enter a valid {label} number or leave it blank.')
    return errors


# ---------------- teaching duties (models/auth.py, TeachingDuty) ----------------

def _duty_form_options(admin=None):
    """The classes and subjects a duty can name, for the duty editor (templates/includes/_teaching_duties.html).
    ``admin``, when given and not the School Admin, limits the classes to those within their own reach."""
    from sqlalchemy import select
    from models import ClassSubject, SchoolClass, SchoolSubject
    from core.school_structure import DEPARTMENTS, is_senior, parse_departments
    from core.security import admin_scope_allows
    classes=[{'id':c.id,'name':c.name,'senior':is_senior(c.stage,c.name)}
             for c in db.session.scalars(select(SchoolClass).where(SchoolClass.active==1)
                                         .order_by(SchoolClass.level_order,SchoolClass.name)).all()]
    if admin is not None and not admin['admin_type_system']:
        classes=[c for c in classes if admin_scope_allows(admin['id'],'class',c['name'])]
    # Which classes take each subject (class_subjects), so the editor lists only a class's own subjects.
    offered={}
    for class_id,subject_id in db.session.execute(select(ClassSubject.class_id,ClassSubject.subject_id)).all():
        offered.setdefault(subject_id,[]).append(class_id)
    # Each subject's SSS departments (Subjects page): none listed means every department takes it.
    subjects=[{'id':s.id,'name':s.name,'class_ids':sorted(offered.get(s.id,[])),
               'departments':parse_departments(s.departments) or list(DEPARTMENTS)} for s in db.session.scalars(
        select(SchoolSubject).where(SchoolSubject.active==1).order_by(SchoolSubject.name)).all()]
    # A subdivided JSS/SSS class offers each subdivision as a class-teacher duty of its own (core/subdivisions.py).
    from core.subdivisions import all_subdivisions, label as subdivision_label
    subdivided=all_subdivisions()
    for c in classes:
        c['subdivisions']=[{'id':s.id,'name':subdivision_label(s)} for s in subdivided.get(c['id'],[])]
    return {'duty_classes':classes,'duty_subjects':subjects,'duty_departments':DEPARTMENTS}

def _duties_of(admin_id):
    """An account's duties as the editor shows them: class-teacher class ids, and subject rows."""
    from sqlalchemy import select
    from models import TeachingDuty
    rows=db.session.scalars(select(TeachingDuty).where(TeachingDuty.admin_id==admin_id)
                            .order_by(TeachingDuty.id)).all()
    return {'class_teacher':[r.class_id for r in rows if r.subject_id is None and not r.subdivision_id],
            'class_teacher_subdivisions':[r.subdivision_id for r in rows if r.subject_id is None and r.subdivision_id],
            'subjects':[{'class_id':r.class_id,'subject_id':r.subject_id,'department':r.department or ''}
                        for r in rows if r.subject_id is not None]}

def _parse_duties(form, options):
    """Read the duty editor's fields. Returns (duties, errors): duties is the same shape _duties_of gives,
    with anything outside ``options`` (classes not offered, unknown subjects) refused."""
    from core.school_structure import clean_department
    class_ids={c['id']:c for c in options['duty_classes']}
    subject_ids={s['id'] for s in options['duty_subjects']}
    errors=[]
    def ints(name):
        out=[]
        for raw in form.getlist(name):
            try: out.append(int(raw))
            except (TypeError,ValueError): out.append(None)
        return out
    class_teacher=[]
    for cid in ints('duty_class_teacher'):
        if cid in class_ids and cid not in class_teacher: class_teacher.append(cid)
        elif cid is not None and cid not in class_ids: errors.append('Choose class teacher classes from the list.')
    # A class teacher of one subdivision: its id, checked only if it belongs to an offered class.
    subdivision_class={s['id']:c['id'] for c in options['duty_classes'] for s in c.get('subdivisions',[])}
    class_teacher_subdivisions=[]
    for sid in ints('duty_class_teacher_subdivision'):
        if sid in subdivision_class and sid not in class_teacher_subdivisions: class_teacher_subdivisions.append(sid)
        elif sid is not None and sid not in subdivision_class: errors.append('Choose class teacher subdivisions from the list.')
    subjects=[]
    seen=set()
    subject_departments={s['id']:s.get('departments') or [] for s in options['duty_subjects']}
    subject_names={s['id']:s['name'] for s in options['duty_subjects']}
    rows=zip(ints('duty_class'),ints('duty_subject'),form.getlist('duty_department'))
    for cid,sid,dept in rows:
        if cid is None and sid is None: continue   # a blank row the person added and left empty
        if cid not in class_ids or sid not in subject_ids:
            errors.append('Each subject row needs a class and a subject from the lists.'); continue
        department=clean_department(dept) if class_ids[cid]['senior'] else None
        if department and department not in subject_departments.get(sid,()):
            errors.append(f"{subject_names.get(sid,'That subject')} is not taken by the {department} department "
                          "(Subjects). Choose one of its departments, or All departments.")
            continue
        key=(cid,sid,department)
        if key in seen: continue
        seen.add(key)
        subjects.append({'class_id':cid,'subject_id':sid,'department':department or ''})
    return {'class_teacher':class_teacher,'class_teacher_subdivisions':class_teacher_subdivisions,'subjects':subjects},sorted(set(errors))

def _has_duties(duties):
    return bool(duties['class_teacher'] or duties.get('class_teacher_subdivisions') or duties['subjects'])

def _save_duties(admin_id, duties, actor_id, now, only_class_ids=None):
    """Replace an account's duties with ``duties``. ``only_class_ids``, when given, replaces only the duties
    in those classes and leaves the rest alone (a head teacher editing within their own section)."""
    from sqlalchemy import select
    from models import TeachingDuty
    stmt=sa_delete(TeachingDuty).where(TeachingDuty.admin_id==admin_id)
    if only_class_ids is not None:
        stmt=stmt.where(TeachingDuty.class_id.in_(list(only_class_ids) or [0]))
    db.session.execute(stmt)
    for cid in duties['class_teacher']:
        db.session.add(TeachingDuty(admin_id=admin_id,class_id=cid,subject_id=None,department=None,
                                    created_at=now,created_by=actor_id))
    if duties.get('class_teacher_subdivisions'):
        from models import ClassSubdivision
        for sub in db.session.scalars(select(ClassSubdivision).where(ClassSubdivision.id.in_(duties['class_teacher_subdivisions']))).all():
            if only_class_ids is not None and sub.class_id not in only_class_ids: continue
            db.session.add(TeachingDuty(admin_id=admin_id,class_id=sub.class_id,subject_id=None,department=None,
                                        subdivision_id=sub.id,created_at=now,created_by=actor_id))
    for row in duties['subjects']:
        db.session.add(TeachingDuty(admin_id=admin_id,class_id=row['class_id'],subject_id=row['subject_id'],
                                    department=row['department'] or None,created_at=now,created_by=actor_id))

def _duty_labels(duties, options):
    """Plain words for the audit log: 'Class teacher: JSS 1', 'Mathematics — SSS 1 (Science)'."""
    cname={c['id']:c['name'] for c in options['duty_classes']}
    sname={s['id']:s['name'] for s in options['duty_subjects']}
    out=[f"Class teacher: {cname.get(cid,cid)}" for cid in duties['class_teacher']]
    sname_sub={s['id']:s['name'] for c in options['duty_classes'] for s in c.get('subdivisions',[])}
    out+=[f"Class teacher: {sname_sub.get(sid,sid)}" for sid in duties.get('class_teacher_subdivisions',[])]
    out+=[f"{sname.get(r['subject_id'],r['subject_id'])} — {cname.get(r['class_id'],r['class_id'])}"
          +(f" ({r['department']})" if r['department'] else '') for r in duties['subjects']]
    return out
