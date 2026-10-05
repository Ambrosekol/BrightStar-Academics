"""Admin governance: notifications, controls/resource-locks, direct
messaging between administrators, and the administration workspace (staff
accounts, roles/permissions, scopes, audit log).

The plan's target layout lists messaging/ and administration/ as separate
folders, but the source code interleaves them tightly — admin_controls, for
instance, is at once the notifications/governance-review inbox and the
entrance-workspace bank-lock panel. Splitting them would mean passing
shared state between two blueprint modules for no real separation of
concerns, so they're kept together here as one package, matching how the
plan's own execution order already treats them as a single work item.
"""

import os
import time
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from flask import (
    Response, abort, flash, jsonify, redirect, render_template, request,
    session, url_for,
)
from sqlalchemy import and_, or_, select, func, delete as sa_delete, update as sa_update
from werkzeug.security import generate_password_hash
from werkzeug.utils import secure_filename

from app import (
    app, _admin_role_options, _admin_scope_label, _contact_columns,
    _create_control_item, _lock_resource, _new_admin_password,
    _thread_between, _unlock_resource,
    _unread_admin_messages, admin_can_delegate_roles, csrf_check_request,
)
from core.entrance import bank, load_banks
from models import (
    Admin, AdminControlItem, AdminMessage, AdminNotification, AdminPermission,
    AdminResourceLock, AdminRoleAssignment, AdminScope, AdminType,
    AdminTypePermission, AcademicSession, AuditLog, ClassSubject, Permission,
    SchoolClass, SchoolStudentResult, SchoolSubject, db,
)
from core.db_helpers import all_rows, group_concat, obj, one, one_scalar, tuples, _flatten, _ignore_insert
from core.security import (
    admin_access_error, admin_has_permission, admin_required, admin_scope_allows,
    audit_display_detail, audit_log, current_admin, csrf_protect, is_school_admin,
    _notify_school_admins,
)
from core.storage import delete_upload, read_upload_bytes, save_upload_bytes
from core.uploads import ATTACHMENT_EXTENSIONS, _save_image_upload
from blueprints.school.helpers import _school_class_allowed
from blueprints.administration.helpers import (
    _admin_contact_fields, _sync_admin_roles, _validate_admin_contact_fields,
)


@app.route('/admin/administration/notifications')
@admin_required
def admin_notifications():
    """Personal notification inbox. Unlike governance controls, every signed-in administrator can view their own notifications."""
    me=current_admin()
    notifications=db.session.scalars(select(AdminNotification)
        .where(AdminNotification.admin_id==me['id'])
        .order_by(AdminNotification.id.desc()).limit(100)).all()
    unread=one_scalar(select(func.count()).select_from(AdminNotification)
        .where(AdminNotification.admin_id==me['id'],
               AdminNotification.read_at.is_(None)), 0)
    return render_template('admin_notifications.html',notifications=notifications,unread=unread)

@app.route('/admin/administration/controls')
@admin_required
def admin_controls():
    me=current_admin()
    if not admin_has_permission(me['id'],'audit.view'): return admin_access_error('audit.view')
    controls=[_flatten(r,'AdminControlItem','requester_name','requester_username')
              for r in all_rows(
        select(AdminControlItem,Admin.display_name.label('requester_name'),
               Admin.username.label('requester_username'))
        .outerjoin(Admin,Admin.id==AdminControlItem.requested_by)
        .where(AdminControlItem.status=='open')
        .order_by(AdminControlItem.id.desc()).limit(100))]
    notifications=db.session.scalars(select(AdminNotification)
        .where(AdminNotification.admin_id==me['id'])
        .order_by(AdminNotification.id.desc()).limit(50)).all()
    workspace=session.get('admin_workspace') or 'school'
    if workspace=='school':
        subjects=all_rows(
            select(ClassSubject.id,SchoolSubject.id.label('subject_id'),
                   SchoolSubject.name.label('subject_name'),
                   SchoolClass.id.label('class_id'),SchoolClass.name.label('class_name'),
                   ClassSubject.locked,ClassSubject.final_locked,ClassSubject.final_locked_at)
            .join(SchoolSubject,SchoolSubject.id==ClassSubject.subject_id)
            .join(SchoolClass,SchoolClass.id==ClassSubject.class_id)
            .where(SchoolSubject.active==1,SchoolClass.active==1)
            .order_by(SchoolClass.level_order,SchoolSubject.name))
        if not me['admin_type_system']:
            subjects=[r for r in subjects if _school_class_allowed(me['id'],r['class_id'])]

        def pending(status):
            return one_scalar(select(func.count()).select_from(SchoolStudentResult)
                              .where(SchoolStudentResult.status==status), 0)

        return render_template('admin_controls.html',controls=controls,notifications=notifications,subjects=subjects,is_school_admin=is_school_admin(me),workspace='school',pending_results=pending('entered'),pending_approval=pending('verified'),pending_release=pending('approved'))
    # Entrance workspace: show each bank's School Admin lock state. One query
    # covers every bank rather than one per bank.
    locks={rid:(reason,locked_at) for rid,reason,locked_at in tuples(
        select(AdminResourceLock.resource_id,AdminResourceLock.reason,
               AdminResourceLock.locked_at)
        .where(AdminResourceLock.resource_type=='bank',
               AdminResourceLock.unlocked_at.is_(None)))}
    banks=[]
    for b in load_banks().values():
        lock=locks.get(b['id'])
        banks.append({'id':b['id'],'name':b.get('name') or b['id'],
                      'level':b.get('level') or '—','locked':bool(lock),
                      'lock_reason':lock[0] if lock else None})
    return render_template('admin_controls.html',controls=controls,notifications=notifications,banks=banks,subjects=[],is_school_admin=is_school_admin(me),workspace='entrance',pending_results=0,pending_approval=0,pending_release=0)

@app.post('/admin/administration/notifications/<int:nid>/read')
@admin_required
@csrf_protect
def admin_notification_read(nid):
    me=current_admin()
    # The admin_id predicate keeps one administrator from marking another's.
    db.session.execute(sa_update(AdminNotification)
        .where(AdminNotification.id==nid,AdminNotification.admin_id==me['id'])
        .values(read_at=datetime.now(timezone.utc).isoformat()))
    db.session.commit()
    return redirect(request.referrer or url_for('admin_controls'))

@app.get('/admin/administration/notifications/<int:nid>/open')
@admin_required
def admin_notification_open(nid):
    me=current_admin()
    action_url=one_scalar(select(AdminNotification.action_url)
        .where(AdminNotification.id==nid,AdminNotification.admin_id==me['id']))
    db.session.execute(sa_update(AdminNotification)
        .where(AdminNotification.id==nid,AdminNotification.admin_id==me['id'])
        .values(read_at=datetime.now(timezone.utc).isoformat()))
    db.session.commit()
    return redirect(action_url or url_for('admin_controls'))

@app.post('/admin/administration/controls/<int:cid>/resolve')
@admin_required
@csrf_protect
def admin_control_resolve(cid):
    me=current_admin()
    if not is_school_admin(me): return admin_access_error('School Admin control')
    db.session.execute(sa_update(AdminControlItem).where(AdminControlItem.id==cid)
        .values(status='resolved',resolved_by=me['id'],
                resolved_at=datetime.now(timezone.utc).isoformat()))
    db.session.commit(); audit_log('control_item_resolved','administration','control',cid,{},True,me); flash('Review item marked as resolved.','success'); return redirect(request.referrer or url_for('admin_controls'))

@app.post('/admin/administration/resources/<resource_type>/<path:resource_id>/lock')
@admin_required
@csrf_protect
def admin_lock_resource(resource_type,resource_id):
    me=current_admin()
    if not is_school_admin(me): return admin_access_error('School Admin control')
    # Only a question bank can be locked: nothing else checks for a lock, so a lock on anything
    # else would say "changes are blocked" while blocking nothing.
    if resource_type!='bank' or not bank(resource_id): abort(404)
    reason=request.form.get('reason','Locked by School Admin pending review.').strip() or 'Locked by School Admin pending review.'
    _lock_resource(resource_type,resource_id,reason,me['id'])
    _create_control_item('Resource locked',reason,'governance',resource_type,resource_id,me['id'])
    audit_log('resource_locked','administration',resource_type,resource_id,{'reason':reason},True,me)
    flash('The item has been locked. Staff can still view it where permitted, but changes are blocked.','success')
    return redirect(request.referrer or url_for('admin_controls'))

@app.post('/admin/administration/resources/<resource_type>/<path:resource_id>/unlock')
@admin_required
@csrf_protect
def admin_unlock_resource(resource_type,resource_id):
    me=current_admin()
    if not is_school_admin(me): return admin_access_error('School Admin control')
    if resource_type not in ('bank','examination'): abort(404)
    _unlock_resource(resource_type,resource_id,me['id'])
    db.session.execute(sa_update(AdminControlItem)
        .where(AdminControlItem.target_type==resource_type,
               AdminControlItem.target_id==str(resource_id),
               AdminControlItem.status=='open',AdminControlItem.category=='governance')
        .values(status='resolved',resolved_by=me['id'],
                resolved_at=datetime.now(timezone.utc).isoformat()))
    db.session.commit()
    audit_log('resource_unlocked','administration',resource_type,resource_id,{},True,me)
    flash('The item has been unlocked.','success')
    return redirect(request.referrer or url_for('admin_controls'))

@app.route('/admin/administration')
@admin_required
def admin_administration():
    me=current_admin()
    if not is_school_admin(me): return admin_access_error('Administration')
    def _count(model, *conditions):
        return one_scalar(select(func.count()).select_from(model).where(*conditions),0)
    counts={
        'admins':_count(Admin),
        'active_admins':_count(Admin,Admin.active==1),
        'roles':_count(AdminType,AdminType.active==1,AdminType.is_system==0,
                       AdminType.name!='Ordinary Admin'),
        'notifications':_count(AdminNotification,AdminNotification.admin_id==me['id'],
                               AdminNotification.read_at.is_(None)),
        'controls':_count(AdminControlItem,AdminControlItem.status=='open'),
    }
    return render_template('admin_administration.html',counts=counts,admin=me,is_school_admin=is_school_admin(me))

ADMIN_ACCOUNTS_PER_PAGE=50

@app.route('/admin/administration/admins')
@admin_required
def admin_accounts():
    me=current_admin()
    if not is_school_admin(me): return admin_access_error('Administration')
    if not admin_has_permission(me['id'],'admins.view'): return admin_access_error('admins.view')
    # A system School Admin is unrestricted, so its boundary count reads as zero
    # rather than as however many stale scope rows may exist.
    scope_count=sa.case((AdminType.is_system==1,0),
        else_=select(func.count()).select_from(AdminScope)
              .where(AdminScope.admin_id==Admin.id).scalar_subquery()).label('scope_count')
    direct_permission_count=(select(func.count()).select_from(AdminPermission)
        .where(AdminPermission.admin_id==Admin.id).scalar_subquery()
        .label('direct_permission_count'))
    role_names=(select(group_concat(AdminType.name,'|'))
        .select_from(AdminRoleAssignment)
        .join(AdminType,AdminType.id==AdminRoleAssignment.admin_type_id)
        .where(AdminRoleAssignment.admin_id==Admin.id,AdminType.active==1)
        .scalar_subquery().label('role_names'))
    # Search and the status filter narrow the directory before it is paged.
    q=request.args.get('q','').strip()[:100]
    status=request.args.get('status','all')
    if status not in ('all','active','suspended'): status='all'
    filters=[]
    if q:
        needle=q.casefold()
        filters.append(or_(func.lower(Admin.display_name).contains(needle,autoescape=True),
                           func.lower(Admin.username).contains(needle,autoescape=True),
                           func.lower(func.coalesce(Admin.email,'')).contains(needle,autoescape=True)))
    if status=='active': filters.append(Admin.active==1)
    if status=='suspended': filters.append(Admin.active==0)
    everyone=one_scalar(select(func.count()).select_from(Admin),0)
    active_count=one_scalar(select(func.count()).select_from(Admin).where(Admin.active==1),0)
    # Fine for hundreds without paging at all; this is what a school past a few thousand accounts needs.
    count_stmt=select(func.count()).select_from(Admin)
    if filters: count_stmt=count_stmt.where(and_(*filters))
    total=one_scalar(count_stmt,0)
    total_pages=max(1,-(-total//ADMIN_ACCOUNTS_PER_PAGE))
    try: page=int(request.args.get('page','1'))
    except (TypeError,ValueError): page=1
    page=min(max(page,1),total_pages)
    list_stmt=(select(Admin.id,Admin.username,Admin.display_name,Admin.active,
                      Admin.created_at,Admin.last_login_at,Admin.email,Admin.phone,
                      Admin.whatsapp,Admin.photo_path,
                      AdminType.name.label('admin_type_name'),AdminType.is_system.label('is_system'),
                      scope_count,direct_permission_count,role_names)
               .join(AdminType,AdminType.id==Admin.admin_type_id))
    if filters: list_stmt=list_stmt.where(and_(*filters))
    rows=all_rows(list_stmt.order_by(Admin.display_name,Admin.id)
                  .limit(ADMIN_ACCOUNTS_PER_PAGE).offset((page-1)*ADMIN_ACCOUNTS_PER_PAGE))
    return render_template('admin_accounts.html',admins=rows,page=page,total_pages=total_pages,
                           total_count=total,per_page=ADMIN_ACCOUNTS_PER_PAGE,
                           everyone_count=everyone,active_count=active_count,
                           suspended_count=everyone-active_count,q=q,status=status)

@app.route('/admin/administration/admins/new',methods=['GET','POST'])
@admin_required
def admin_account_new():
    me=current_admin()
    if not is_school_admin(me): return admin_access_error('Administration')
    if not admin_has_permission(me['id'],'admins.create'): return admin_access_error('admins.create')
    roles=_admin_role_options()
    perms=db.session.scalars(select(Permission)
        .order_by(Permission.module,Permission.name)).all() if is_school_admin(me) else []
    banks=sorted(load_banks().values(),key=lambda b:str(b.get('name') or b.get('id') or '').lower())
    if not me['admin_type_system']:
        banks=[b for b in banks if admin_scope_allows(me['id'],'bank',b['id'])]
    classes=all_rows(select(SchoolClass.name).where(SchoolClass.active==1)
        .order_by(SchoolClass.level_order,SchoolClass.name))
    subjects=all_rows(select(SchoolSubject.name).where(SchoolSubject.active==1)
        .order_by(SchoolSubject.name))
    sessions=all_rows(select(AcademicSession.name).where(AcademicSession.active==1)
        .order_by(AcademicSession.name.desc()))
    if request.method=='POST':
        if not csrf_check_request(): abort(403,description='Invalid or missing CSRF token.')
        username=request.form.get('username','').strip().lower(); display=request.form.get('display_name','').strip()
        role_ids=[int(x) for x in request.form.getlist('admin_type_ids') if x.isdigit()]
        contact=_admin_contact_fields(request.form)
        scope_type=request.form.get('scope_type','global').strip() or 'global'
        scope_values=request.form.getlist('scope_value') or [request.form.get('scope_value','*').strip() or '*']
        selected_scope_types=[x.strip() for x in request.form.getlist('scope_types') if x.strip()]
        if not selected_scope_types and scope_type!='global': selected_scope_types=[scope_type]
        scope_groups={}
        for st in selected_scope_types:
            vals=[v.strip() for v in request.form.getlist(f'scope_values_{st}') if v.strip()]
            if vals: scope_groups[st]=vals
        selected=[int(x) for x in request.form.getlist('permissions') if x.isdigit()] if is_school_admin(me) else []
        errors=[]
        # The School Admin role is never assignable through this form.
        protected_school_admin=any(r['id']==rid and r['is_system']
                                  for r in roles for rid in role_ids)
        if not username or not username.replace('.','').replace('_','').replace('-','').isalnum(): errors.append('Username must contain letters, numbers, dots, hyphens or underscores.')
        if not display: errors.append('Display name is required.')
        if protected_school_admin:
            pass
        elif not role_ids or not all(any(r['id']==rid for r in roles) for rid in role_ids): errors.append('Select at least one valid staff job role.')
        elif not is_school_admin(me) and not admin_can_delegate_roles(me['id'],role_ids): errors.append('You can only assign job roles whose permissions are already within your own authorised capabilities.')
        errors += _validate_admin_contact_fields(contact)
        valid_scope={'academic_session','class','subject','bank'}
        if scope_type not in valid_scope|{'global'}: errors.append('Invalid access boundary.')
        if not selected_scope_types:
            scope_groups={}
        allowed_values={
            'academic_session':{r['name'] for r in sessions},
            'class':{r['name'] for r in classes},
            'subject':{r['name'] for r in subjects},
            'bank':{b['id'] for b in banks},
        }
        if not is_school_admin(me):
            for st,vals in list(scope_groups.items()):
                vals=[v for v in vals if admin_scope_allows(me['id'],st,v)]
                scope_groups[st]=vals
        for st,vals in list(scope_groups.items()):
            vals=[v for v in vals if v in allowed_values.get(st,set())]
            scope_groups[st]=vals
            if not vals: errors.append(f'Select at least one valid {st.replace("_"," ")} restriction value.')
        form=dict(request.form); form['admin_type_ids']=role_ids; form.update(contact); form['scope_groups']=scope_groups
        if errors:
            return render_template('admin_account_form.html',roles=roles,errors=errors,form=form,mode='new',permissions=perms,show_advanced=is_school_admin(me),banks=banks,classes=classes,subjects=subjects,sessions=sessions,selected_role_ids=set(role_ids),selected_scope_values=scope_values)
        now=datetime.now(timezone.utc).isoformat()
        try:
            primary_role=role_ids[0]
            temporary_password=_new_admin_password()
            created=Admin(username=username,display_name=display,
                password_hash=generate_password_hash(temporary_password),
                admin_type_id=primary_role,active=1,created_at=now,created_by=me['id'],
                email=contact['email'] or None,phone=contact['phone'] or None,
                whatsapp=contact['whatsapp'] or None,photo_path=None,
                password_must_change=1)
            db.session.add(created); db.session.flush(); aid=created.id
            photo=request.files.get('photo')
            if photo and photo.filename:
                created.photo_path=_save_image_upload(photo,'admins',f'admin_{username}')
            _sync_admin_roles(aid,role_ids,me['id'],now)
            for st,vals in scope_groups.items():
                for value in vals:
                    _ignore_insert(AdminScope, [{'admin_id':aid,'scope_type':st,
                                                 'scope_value':value,'created_at':now,
                                                 'granted_by':me['id']}])
            for pid in selected:
                _ignore_insert(AdminPermission, [{'admin_id':aid,'permission_id':pid,
                                                  'granted_at':now,'granted_by':me['id']}])
            db.session.commit()
        except (sa.exc.IntegrityError,ValueError) as exc:
            db.session.rollback()
            msg='That username is already in use.' if isinstance(exc,sa.exc.IntegrityError) else str(exc)
            errors=[msg]
            return render_template('admin_account_form.html',roles=roles,errors=errors,form=form,mode='new',permissions=perms,show_advanced=is_school_admin(me),banks=banks,classes=classes,subjects=subjects,sessions=sessions,selected_role_ids=set(role_ids),selected_scope_values=scope_values)
        audit_log('admin_created','administration','admin',aid,{'username':username,'role_ids':role_ids,'access_boundaries':[_admin_scope_label(st,v) for st,vals in scope_groups.items() for v in vals],'direct_permissions':selected})
        flash('Staff administrator created successfully.','success'); return render_template('admin_credentials.html',admin={'id':aid,'display_name':display,'username':username,'email':contact['email'],'phone':contact['phone'],'whatsapp':contact['whatsapp']},temporary_password=temporary_password)
    return render_template('admin_account_form.html',roles=roles,errors=[],form={},mode='new',permissions=perms,show_advanced=is_school_admin(me),banks=banks,classes=classes,subjects=subjects,sessions=sessions,selected_role_ids=set(),selected_scope_values=['*'])

@app.route('/admin/administration/admins/<int:aid>/edit',methods=['GET','POST'])
@admin_required
def admin_account_edit(aid):
    me=current_admin()
    if not is_school_admin(me): return admin_access_error('Administration')
    if not admin_has_permission(me['id'],'admins.edit'): return admin_access_error('admins.edit')
    row=db.session.scalars(select(Admin).join(AdminType,AdminType.id==Admin.admin_type_id)
                           .where(Admin.id==aid)).first()
    roles=list(_admin_role_options())
    perms=db.session.scalars(select(Permission).order_by(Permission.module,Permission.name)).all()
    direct=set(db.session.scalars(select(AdminPermission.permission_id)
                                  .where(AdminPermission.admin_id==aid)).all())
    assigned=set(db.session.scalars(select(AdminRoleAssignment.admin_type_id)
                                    .where(AdminRoleAssignment.admin_id==aid)).all())
    scopes=db.session.scalars(select(AdminScope).where(AdminScope.admin_id==aid)
                              .order_by(AdminScope.id)).all()
    banks=sorted(load_banks().values(),key=lambda b:str(b.get('name') or b.get('id') or '').lower())
    banks=[b for b in banks if me['admin_type_system'] or admin_scope_allows(me['id'],'bank',b['id'])]
    classes=all_rows(select(SchoolClass.name).where(SchoolClass.active==1)
                     .order_by(SchoolClass.level_order,SchoolClass.name))
    subjects=all_rows(select(SchoolSubject.name).where(SchoolSubject.active==1)
                      .order_by(SchoolSubject.name))
    sessions=all_rows(select(AcademicSession.name).where(AcademicSession.active==1)
                      .order_by(AcademicSession.name.desc()))
    if not row: abort(404)
    protected_school_admin = bool(row['admin_type_system'])
    # School Admin is a system authority, not an ordinary scoped staff account.
    # A School Admin may manage the profile of a School Admin account, but the
    # protected system role itself cannot be replaced or narrowed by this form.
    if not assigned: assigned={row['admin_type_id']}
    if not any(r['id']==row['admin_type_id'] for r in roles):
        legacy_role=obj(AdminType, row['admin_type_id'])
        if legacy_role: roles.append(legacy_role)
    scope_type=scopes[0]['scope_type'] if scopes else 'global'; scope_values=[r['scope_value'] for r in scopes] or ['*']
    scope_groups={};
    for r in scopes: scope_groups.setdefault(r['scope_type'],[]).append(r['scope_value'])
    form={'username':row['username'],'display_name':row['display_name'],'email':row['email'] or '','phone':row['phone'] or '','whatsapp':row['whatsapp'] or '','scope_type':scope_type,'scope_value':scope_values,'scope_groups':scope_groups}
    if request.method=='POST':
        if not csrf_check_request(): abort(403,description='Invalid or missing CSRF token.')
        display=request.form.get('display_name','').strip(); role_ids=[int(x) for x in request.form.getlist('admin_type_ids') if x.isdigit()]; contact=_admin_contact_fields(request.form); scope_type=request.form.get('scope_type','global').strip() or 'global'; scope_values=request.form.getlist('scope_value') or [request.form.get('scope_value','*').strip() or '*']; errors=[]
        selected_scope_types=[x.strip() for x in request.form.getlist('scope_types') if x.strip()]
        if not selected_scope_types and scope_type!='global': selected_scope_types=[scope_type]
        scope_groups={}
        for st in selected_scope_types:
            vals=[v.strip() for v in request.form.getlist(f'scope_values_{st}') if v.strip()]
            if vals: scope_groups[st]=vals
        if protected_school_admin:
            # System School Admin accounts are always unrestricted. Ignore any
            # stale/posted boundary or role selections rather than allowing a
            # form submission to accidentally narrow the system authority.
            role_ids=[row['admin_type_id']]
            scope_groups={}
        if not display: errors.append('Display name is required.')
        if protected_school_admin:
            # The system role is preserved automatically and is intentionally
            # absent from the ordinary staff-role selector.
            role_ids=[row['admin_type_id']]
        elif not role_ids or not all(any(r['id']==rid for r in roles) for rid in role_ids): errors.append('Select at least one valid staff job role.')
        elif not is_school_admin(me) and not admin_can_delegate_roles(me['id'],role_ids): errors.append('You can only assign job roles whose permissions are already within your own authorised capabilities.')
        errors += _validate_admin_contact_fields(contact)
        if not protected_school_admin and scope_type not in ('global','academic_session','class','subject','bank'): errors.append('Invalid access boundary.')
        allowed_values={'academic_session':{r['name'] for r in sessions},'class':{r['name'] for r in classes},'subject':{r['name'] for r in subjects},'bank':{b['id'] for b in banks}}
        if not selected_scope_types or protected_school_admin: scope_groups={}
        for st,vals in list(scope_groups.items()):
            if not is_school_admin(me): vals=[v for v in vals if admin_scope_allows(me['id'],st,v)]
            vals=[v for v in vals if v in allowed_values.get(st,set())]
            scope_groups[st]=vals
            if not vals: errors.append(f'Select at least one valid {st.replace("_"," ")} restriction value.')
        form=dict(request.form); form['username']=row['username']; form['admin_type_ids']=role_ids; form.update(contact); form['scope_groups']=scope_groups
        if errors: return render_template('admin_account_form.html',roles=roles,permissions=perms,errors=errors,form=form,mode='edit',editing=row,show_advanced=is_school_admin(me),direct_permissions=direct,banks=banks,classes=classes,subjects=subjects,sessions=sessions,selected_role_ids=set(role_ids),selected_scope_values=scope_values,protected_school_admin=protected_school_admin)
        now=datetime.now(timezone.utc).isoformat()
        db.session.execute(sa_update(Admin).where(Admin.id==aid).values(
            display_name=display,admin_type_id=role_ids[0],email=contact['email'] or None,
            phone=contact['phone'] or None,whatsapp=contact['whatsapp'] or None))
        photo=request.files.get('photo')
        if photo and photo.filename:
            try: photo_path=_save_image_upload(photo,'admins',f'admin_{row["username"]}')
            except ValueError as exc:
                db.session.rollback(); return render_template('admin_account_form.html',roles=roles,permissions=perms,errors=[str(exc)],form=form,mode='edit',editing=row,show_advanced=is_school_admin(me),direct_permissions=direct,banks=banks,classes=classes,subjects=subjects,sessions=sessions,selected_role_ids=set(role_ids),selected_scope_values=scope_values,protected_school_admin=protected_school_admin)
            db.session.execute(sa_update(Admin).where(Admin.id==aid).values(photo_path=photo_path))
        _sync_admin_roles(aid,role_ids,me['id'],now)
        db.session.execute(sa_delete(AdminScope).where(AdminScope.admin_id==aid))
        db.session.add_all([AdminScope(admin_id=aid,scope_type=st,scope_value=value,
                                       created_at=now,granted_by=me['id'])
                            for st,vals in scope_groups.items() for value in vals])
        if is_school_admin(me):
            db.session.execute(sa_delete(AdminPermission).where(AdminPermission.admin_id==aid))
            db.session.flush()
            _ignore_insert(AdminPermission,[{'admin_id':aid,'permission_id':pid,
                                             'granted_at':now,'granted_by':me['id']}
                                            for pid in [int(x) for x in request.form.getlist('permissions') if x.isdigit()]])
        db.session.commit(); audit_log('admin_access_updated','administration','admin',aid,{'role_ids':role_ids,'access_boundaries':[_admin_scope_label(st,v) for st,vals in scope_groups.items() for v in vals]}); flash('Administrator profile and access updated.','success'); return redirect(url_for('admin_accounts'))
    return render_template('admin_account_form.html',roles=roles,permissions=perms,errors=[],form=form,mode='edit',editing=row,show_advanced=is_school_admin(me),direct_permissions=direct,banks=banks,classes=classes,subjects=subjects,sessions=sessions,selected_role_ids=assigned,selected_scope_values=scope_values,protected_school_admin=protected_school_admin)

@app.route('/admin/administration/admins/<int:aid>/credentials/reset',methods=['POST'])
@admin_required
@csrf_protect
def admin_account_credentials_reset(aid):
    me=current_admin()
    if not is_school_admin(me): return admin_access_error('Administration')
    if not admin_has_permission(me['id'],'admins.edit'): return admin_access_error('admins.edit')
    row=db.session.scalars(select(Admin).join(AdminType,AdminType.id==Admin.admin_type_id)
                           .where(Admin.id==aid,Admin.active==1)).first()
    if not row:
        abort(404)
    temporary_password=_new_admin_password()
    db.session.execute(sa_update(Admin).where(Admin.id==aid).values(
        password_hash=generate_password_hash(temporary_password),password_must_change=1))
    db.session.commit()
    audit_log('admin_credentials_reset','authentication','admin',aid,{'username':row['username'],'reset_by':me['username']},True,me)
    _notify_school_admins('Administrator login credentials reset',f'Login credentials for {row["username"]} were regenerated.','warning',url_for('admin_controls'),me['id'])
    flash('A new temporary password was generated. The previous password no longer works.','success')
    return render_template('admin_credentials.html',admin=dict(row),temporary_password=temporary_password,reset=True)

@app.route('/admin/administration/admins/<int:aid>/toggle',methods=['POST'])
@admin_required
@csrf_protect
def admin_account_toggle(aid):
    me=current_admin()
    if not is_school_admin(me): return admin_access_error('Administration')
    if not admin_has_permission(me['id'],'admins.deactivate'): return admin_access_error('admins.deactivate')
    if aid==me['id']: flash('You cannot deactivate your own administrator account.','error'); return redirect(url_for('admin_accounts'))
    row=one(select(Admin.id,Admin.username,Admin.active).where(Admin.id==aid))
    if not row: abort(404)
    new=0 if row['active'] else 1
    db.session.execute(sa_update(Admin).where(Admin.id==aid).values(active=new))
    db.session.commit(); audit_log('admin_status_changed','administration','admin',aid,{'username':row['username'],'active':new}); _notify_school_admins('Administrator access status changed',f'{row["username"]} was {"activated" if new else "suspended"}.','warning',url_for('admin_controls'),me['id']); flash('Administrator '+('activated.' if new else 'suspended.')+' School Admin has been notified.','success'); return redirect(url_for('admin_accounts'))

@app.route('/admin/administration/messages')
@admin_required
def admin_messages():
    me=current_admin()
    contacts=all_rows(_contact_columns()
        .where(Admin.active==1,Admin.id!=me['id']).order_by(Admin.display_name))
    selected_id=request.args.get('with',type=int)
    selected=one(_contact_columns().where(Admin.active==1,Admin.id==selected_id,
                                    Admin.id!=me['id'])) if selected_id else None
    thread=[]
    if selected:
        thread=[_flatten(r,'AdminMessage','sender_name') for r in all_rows(
            select(AdminMessage,Admin.display_name.label('sender_name'))
                .join(Admin,Admin.id==AdminMessage.sender_admin_id)
                .where(_thread_between(me['id'],selected_id))
                .order_by(AdminMessage.sent_at.asc()))]
        db.session.execute(sa_update(AdminMessage)
            .where(AdminMessage.sender_admin_id==selected_id,
                   AdminMessage.recipient_admin_id==me['id'],
                   AdminMessage.read_at.is_(None))
            .values(read_at=datetime.now(timezone.utc).isoformat()))
        db.session.commit()
    unread=_unread_admin_messages(me['id'])
    unread_map=dict(tuples(select(AdminMessage.sender_admin_id,func.count())
        .where(AdminMessage.recipient_admin_id==me['id'],AdminMessage.read_at.is_(None))
        .group_by(AdminMessage.sender_admin_id)))
    return render_template('admin_messages.html',contacts=contacts,selected=selected,thread=thread,unread_messages=unread,unread_map=unread_map)

@app.get('/admin/administration/messages/unread-state')
@admin_required
def admin_messages_unread_state():
    """Lightweight live-message endpoint used by the admin UI polling layer.

    This deliberately uses ordinary HTTP polling rather than introducing a new
    realtime server dependency. It works reliably on the existing Flask/local
    network deployment and keeps the existing messaging architecture intact.
    """
    me=current_admin()
    selected_id=request.args.get('with',type=int)
    Sender=sa.orm.aliased(Admin)
    unread=one_scalar(select(func.count()).select_from(AdminMessage)
        .where(AdminMessage.recipient_admin_id==me['id'],
               AdminMessage.read_at.is_(None)), 0)
    summaries=[dict(r) for r in all_rows(
        select(AdminMessage.id,AdminMessage.body,AdminMessage.sent_at,
               Sender.id.label('sender_id'),Sender.display_name.label('sender_name'),
               Sender.username.label('sender_username'),
               Sender.photo_path.label('sender_photo'))
        .join(Sender,Sender.id==AdminMessage.sender_admin_id)
        .where(AdminMessage.recipient_admin_id==me['id'],
               AdminMessage.read_at.is_(None))
        .order_by(AdminMessage.sent_at.desc(),AdminMessage.id.desc()).limit(8))]
    payload={'unread_messages':unread,'summaries':summaries}
    if selected_id and selected_id != me['id']:
        contact=one(select(Admin.id,Admin.display_name,Admin.username,Admin.email,
                           Admin.phone,Admin.whatsapp,Admin.photo_path)
                    .where(Admin.id==selected_id,Admin.active==1,Admin.id!=me['id']))
        if contact:
            thread=all_rows(
                select(AdminMessage.id,AdminMessage.sender_admin_id,
                       AdminMessage.recipient_admin_id,AdminMessage.body,
                       AdminMessage.sent_at,AdminMessage.attachment_path,
                       AdminMessage.attachment_type,AdminMessage.attachment_name,
                       Sender.display_name.label('sender_name'))
                .join(Sender,Sender.id==AdminMessage.sender_admin_id)
                .where(or_(and_(AdminMessage.sender_admin_id==me['id'],
                                AdminMessage.recipient_admin_id==selected_id),
                           and_(AdminMessage.sender_admin_id==selected_id,
                                AdminMessage.recipient_admin_id==me['id'])))
                .order_by(AdminMessage.sent_at.asc(),AdminMessage.id.asc()))
            payload['thread']=[dict(r) for r in thread]
    response=jsonify(payload)
    response.headers['Cache-Control']='no-store, no-cache, must-revalidate, max-age=0'
    return response

@app.post('/admin/administration/messages/send')
@admin_required
@csrf_protect
def admin_message_send():
    me=current_admin()
    rid=request.form.get('recipient_id',type=int)
    body=request.form.get('body','').strip()
    file_obj=request.files.get('attachment')

    has_file=bool(file_obj and file_obj.filename)

    if not rid or rid==me['id']:
        flash('Choose a recipient.','error')
        return redirect(url_for('admin_messages'))

    if not body and not has_file:
        flash('Enter a message or attach a file.','error')
        return redirect(url_for('admin_messages', **{'with': rid}))

    if len(body)>5000:
        flash('Message is too long. Please keep messages under 5,000 characters.','error')
        return redirect(url_for('admin_messages', **{'with': rid}))

    attachment_path=None
    attachment_type=None
    attachment_name=None

    allowed_extensions=ATTACHMENT_EXTENSIONS  # the same list the message box tells the person about

    if has_file:
        original_name=secure_filename(file_obj.filename or '')

        if not original_name:
            flash('The selected attachment has an invalid filename.','error')
            return redirect(url_for('admin_messages', **{'with': rid}))

        fext=os.path.splitext(original_name)[1].lower()

        if fext not in allowed_extensions:
            flash('That file type is not supported. Please attach an image, PDF, Word, text or Excel file.','error')
            return redirect(url_for('admin_messages', **{'with': rid}))

        attachment_name=original_name

        if fext in {'.jpg','.jpeg','.png','.gif','.webp'}:
            attachment_type='image'
        elif fext=='.pdf':
            attachment_type='pdf'
        elif fext in {'.doc','.docx'}:
            attachment_type='doc'
        elif fext in {'.xls','.xlsx'}:
            attachment_type='spreadsheet'
        else:
            attachment_type='file'

        uname=f"{uuid.uuid4().hex}{fext}"
        attachment_path=save_upload_bytes('messages', uname, file_obj.stream.read())

    def discard_upload():
        """Do not leave an orphaned file behind when the send fails."""
        if attachment_path:
            delete_upload(attachment_path)

    # SQLite serialises writers, so a brief lock is retried rather than failed.
    # The session owns the transaction; there is no explicit BEGIN IMMEDIATE.
    for attempt_no in range(4):
        try:
            recipient=one(select(Admin.id,Admin.active).where(Admin.id==rid))

            if not recipient or not recipient['active']:
                discard_upload()
                flash('That administrator is no longer available.','error')
                return redirect(url_for('admin_messages'))

            db.session.add(AdminMessage(
                sender_admin_id=me['id'],
                recipient_admin_id=rid,
                body=body,
                sent_at=datetime.now(timezone.utc).isoformat(),
                attachment_path=attachment_path,
                attachment_type=attachment_type,
                attachment_name=attachment_name))
            db.session.commit()

            return redirect(url_for('admin_messages', **{'with': rid}))

        except sa.exc.OperationalError as exc:
            db.session.rollback()

            if 'locked' not in str(exc).lower() or attempt_no==3:
                discard_upload()
                flash('The message could not be sent because the database is temporarily busy. Please try again.','error')
                return redirect(url_for('admin_messages', **{'with': rid}))

            time.sleep(0.15*(attempt_no+1))

        except Exception:
            db.session.rollback()
            discard_upload()
            flash('The message could not be sent. Please try again.','error')
            return redirect(url_for('admin_messages', **{'with': rid}))

@app.get('/admin/administration/messages/attachment/<int:message_id>')
@admin_required
def admin_message_attachment(message_id):
    me=current_admin()
    row=one(select(AdminMessage.id,AdminMessage.sender_admin_id,
                   AdminMessage.recipient_admin_id,AdminMessage.attachment_path,
                   AdminMessage.attachment_name,AdminMessage.attachment_type)
            .where(AdminMessage.id==message_id))

    if not row or not row['attachment_path']:
        abort(404)

    # Only the two parties to the message may fetch its attachment.
    if me['id'] not in (row['sender_admin_id'],row['recipient_admin_id']):
        abort(403)

    filename=os.path.basename(row['attachment_path'])
    stored=f'uploads/messages/{filename}'
    data=read_upload_bytes(stored) if filename else None
    if data is None:
        abort(404)

    import mimetypes
    mime=mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    response=Response(data,mimetype=mime)
    response.headers['Content-Disposition']=f'inline; filename="{row["attachment_name"] or filename}"'
    # Each attachment's stored name is random and never reused (core/storage.py), so the bytes at
    # this path never change; 'private' since access is re-checked above on every request.
    response.headers['Cache-Control']='private, max-age=31536000, immutable'
    return response

@app.route('/admin/administration/roles')
@admin_required
def admin_roles():
    me=current_admin()
    if not is_school_admin(me): return admin_access_error('Administration')
    if not admin_has_permission(me['id'],'roles.view'): return admin_access_error('roles.view')
    roles=db.session.scalars(select(AdminType)
        .where(AdminType.active==1,AdminType.is_system==0,AdminType.name!='Ordinary Admin')
        .order_by(AdminType.name)).all()
    # One query for every role's permissions instead of one query per role.
    role_perms={r.id:[] for r in roles}
    for type_id,perm in tuples(
            select(AdminTypePermission.admin_type_id,Permission)
                .join(Permission,Permission.id==AdminTypePermission.permission_id)
                .where(AdminTypePermission.admin_type_id.in_([r.id for r in roles]))
                .order_by(Permission.module,Permission.name)):
        role_perms[type_id].append(perm)
    return render_template('admin_roles.html',roles=roles,role_perms=role_perms)

@app.route('/admin/administration/roles/new',methods=['GET','POST'])
@admin_required
def admin_role_new():
    me=current_admin()
    if not is_school_admin(me) or not admin_has_permission(me['id'],'roles.create'): return admin_access_error('School Admin role management')
    perms=db.session.scalars(select(Permission).order_by(Permission.module,Permission.name)).all()
    if request.method=='POST':
        if not csrf_check_request(): abort(403,description='Invalid or missing CSRF token.')
        name=request.form.get('name','').strip(); desc=request.form.get('description','').strip(); selected=[int(x) for x in request.form.getlist('permissions') if x.isdigit()]; errors=[]
        if not name: errors.append('Admin type name is required.')
        if errors: return render_template('admin_role_form.html',permissions=perms,errors=errors,form=request.form)
        now=datetime.now(timezone.utc).isoformat()
        try:
            role=AdminType(name=name,description=desc,is_system=0,active=1,created_at=now)
            db.session.add(role); db.session.flush(); rid=role.id
            _ignore_insert(AdminTypePermission,[{'admin_type_id':rid,'permission_id':pid,
                                                 'granted_at':now} for pid in selected])
            db.session.commit()
        except sa.exc.IntegrityError:
            db.session.rollback(); return render_template('admin_role_form.html',permissions=perms,errors=['That admin type already exists.'],form=request.form)
        audit_log('role_created','administration','admin_type',rid,{'name':name,'permissions':selected}); flash('Administrator type created.','success'); return redirect(url_for('admin_roles'))
    return render_template('admin_role_form.html',permissions=perms,errors=[],form={})

@app.route('/admin/administration/roles/<int:rid>/edit',methods=['GET','POST'])
@admin_required
def admin_role_edit(rid):
    me=current_admin()
    if not is_school_admin(me) or not admin_has_permission(me['id'],'roles.edit'): return admin_access_error('School Admin role management')
    role=obj(AdminType, rid)
    perms=db.session.scalars(select(Permission).order_by(Permission.module,Permission.name)).all()
    selected=set(db.session.scalars(select(AdminTypePermission.permission_id)
                                    .where(AdminTypePermission.admin_type_id==rid)).all())
    if not role or role['is_system'] or role['name']=='Ordinary Admin': abort(404)
    if request.method=='POST':
        if not csrf_check_request(): abort(403,description='Invalid or missing CSRF token.')
        name=request.form.get('name','').strip(); desc=request.form.get('description','').strip(); ids=[int(x) for x in request.form.getlist('permissions') if x.isdigit()]
        if not name: return render_template('admin_role_form.html',role=role,permissions=perms,selected=selected,errors=['Role name is required.'],form=request.form,mode='edit',show_advanced=True)
        now=datetime.now(timezone.utc).isoformat()
        db.session.execute(sa_update(AdminType).where(AdminType.id==rid)
                           .values(name=name,description=desc))
        db.session.execute(sa_delete(AdminTypePermission)
                           .where(AdminTypePermission.admin_type_id==rid))
        db.session.flush()
        db.session.add_all([AdminTypePermission(admin_type_id=rid,permission_id=pid,
                                                granted_at=now) for pid in ids])
        db.session.commit(); audit_log('role_updated','administration','admin_type',rid,{'name':name,'permissions':ids}); _notify_school_admins('Staff role changed',f'The {name} role was updated.','warning',url_for('admin_controls'),me['id']); flash('Staff role updated.','success'); return redirect(url_for('admin_roles'))
    return render_template('admin_role_form.html',role=role,permissions=perms,selected=selected,errors=[],form={},mode='edit',show_advanced=True)

@app.route('/admin/administration/permissions')
@admin_required
def admin_permissions_catalogue():
    me=current_admin()
    if not is_school_admin(me): return admin_access_error('Administration')
    if not admin_has_permission(me['id'],'permissions.view'): return admin_access_error('permissions.view')
    perms=db.session.scalars(select(Permission).order_by(Permission.module,Permission.name)).all()
    return render_template('admin_permissions.html',permissions=perms)

@app.route('/admin/administration/scopes')
@admin_required
def admin_scopes():
    me=current_admin()
    if not is_school_admin(me): return admin_access_error('Administration')
    if not admin_has_permission(me['id'],'scopes.view'): return admin_access_error('scopes.view')
    scopes=[_flatten(r,'AdminScope','username','display_name') for r in all_rows(
        select(AdminScope,Admin.username,Admin.display_name)
            .join(Admin,Admin.id==AdminScope.admin_id)
            .order_by(AdminScope.id.desc()))]
    return render_template('admin_scopes.html',scopes=scopes)

@app.route('/admin/administration/audit-logs')
@admin_required
def admin_audit_logs():
    me=current_admin()
    if not admin_has_permission(me['id'],'audit.view'): return admin_access_error('audit.view')
    logs=db.session.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(250)).all()
    logs=[dict(row, display_detail=audit_display_detail(row)) for row in logs]
    return render_template('admin_audit_logs.html',logs=logs)
