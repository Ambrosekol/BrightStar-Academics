"""Parent portal (dashboard, child detail, finance, feedback) plus the admin
side of parent account management and feedback triage.

_release_due_school_results still lives in app.py and is defined further
down in that file than the point where this module gets imported — so it's
imported inside the function body that needs it rather than at module load
time, to avoid a forward-reference ImportError. _school_current_session is
defined earlier in app.py than this import point, so it's safe to import at
the top. The finance helpers (_receipt_pdf and friends) and
_school_class_allowed moved out of app.py entirely into
blueprints/finance/helpers.py and blueprints/school/helpers.py, neither of
which depends on app.py, so those import safely at the top regardless of
order.
"""

import re
from datetime import datetime, timezone

import sqlalchemy as sa
from flask import Response, abort, flash, redirect, render_template, request, session, url_for
from sqlalchemy import and_, func, select, update as sa_update
from werkzeug.security import check_password_hash, generate_password_hash

from app import app, _active_sessions, _school_current_session
from models import (
    Admin, AdminNotification, AdminScope, AcademicSession, FinancePayment,
    ParentAccount, ParentFeedback, ParentFeedbackReply, ParentStudentLink,
    SchoolClass, SchoolNotification, Student, StudentEnrolment, db,
)
from core.branding import school_name
from core.db_helpers import all_rows, obj, one, one_scalar, tuples, _flatten
from core.security import admin_access_error, admin_has_permission, admin_required, audit_log, current_admin, csrf_protect, is_school_admin
from core.notifications import _notify_guardian_email, _notify_guardian_whatsapp
from blueprints.finance.helpers import (
    _finance_student_lifetime_totals, _finance_student_outstanding,
    _finance_student_sessions_with_balance, _receipt_pdf,
)
from blueprints.school.helpers import _school_class_allowed
from blueprints.parents.helpers import (
    _assignment_metrics, _feedback_replies, _new_parent_password,
    _parent_children, _parent_form_context, _parent_owns_student,
    _released_results, _student_assignments, _student_projects,
    parent_required,
)


@app.route('/parent/dashboard')
@parent_required
def parent_dashboard():
    from app import _release_due_school_results
    pid=session['parent_id']
    parent=obj(ParentAccount,pid)
    _release_due_school_results()
    child_data=[]
    total_outstanding_all=0.0
    for child in _parent_children(pid,with_session=True):
        assignments=_student_assignments(child['id'],limit=50)
        avg,completion,trend=_assignment_metrics(assignments)
        # Across every session, not just the current one — a balance carried
        # over from a prior session must never silently disappear here.
        fee_summary=_finance_student_lifetime_totals(child['id'])
        total_outstanding_all+=fee_summary['outstanding']
        child_data.append({'child':child,
                           'results':_released_results(child['id'],limit=50),
                           'assignments':assignments,
                           'projects':_student_projects(child['id'],limit=50),
                           'avg':avg,'completion':completion,'trend':trend,
                           'fee_summary':fee_summary})
    notifications=db.session.scalars(select(SchoolNotification).where(
        SchoolNotification.recipient_type=='parent',SchoolNotification.recipient_id==pid)
        .order_by(SchoolNotification.id.desc()).limit(15)).all()
    feedback=[_flatten(r,'ParentFeedback','first_name','last_name') for r in all_rows(
        select(ParentFeedback,Student.first_name,Student.last_name)
        .outerjoin(Student,Student.id==ParentFeedback.student_id)
        .where(ParentFeedback.parent_id==pid)
        .order_by(ParentFeedback.id.desc()).limit(10))]
    return render_template('parent_dashboard.html',parent=parent,children=child_data,
        notifications=notifications,feedback=feedback,
        total_outstanding_all=total_outstanding_all)

@app.route('/parent/password',methods=['GET','POST'])
@parent_required
@csrf_protect
def parent_password_change():
    pid=session['parent_id']; parent=obj(ParentAccount,pid)
    errors=[]
    if request.method=='POST':
        current=request.form.get('current_password',''); new=request.form.get('new_password',''); confirm=request.form.get('confirm_password','')
        if not check_password_hash(parent['password_hash'] or '',current): errors.append('Your current password is incorrect.')
        if len(new)<8: errors.append('Your new password must be at least 8 characters long.')
        if new!=confirm: errors.append('The new password and confirmation do not match.')
        if not errors:
            parent.password_hash=generate_password_hash(new); parent.password_must_change=0
            db.session.commit()
            audit_log('parent_password_changed','authentication','parent',pid)
            flash('Your password has been changed successfully.','success')
            return redirect(url_for('parent_dashboard'))
    return render_template('parent_password.html',parent=parent,errors=errors)

@app.route('/parent/children/<int:student_id>')
@parent_required
def parent_child_detail(student_id):
    pid=session['parent_id']
    # The link is what authorises access: a parent can only open a child of
    # their own. Prefer the enrolment in the current session.
    row=one(select(Student,ParentStudentLink.relationship,
                   SchoolClass.name.label('class_name'),
                   AcademicSession.name.label('session_name'))
            .select_from(ParentStudentLink)
            .join(Student,and_(Student.id==ParentStudentLink.student_id,Student.active==1))
            .outerjoin(StudentEnrolment,and_(StudentEnrolment.student_id==Student.id,
                                             StudentEnrolment.active==1))
            .outerjoin(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
            .outerjoin(AcademicSession,AcademicSession.id==StudentEnrolment.session_id)
            .where(ParentStudentLink.parent_id==pid,
                   ParentStudentLink.student_id==student_id,
                   ParentStudentLink.active==1)
            .order_by(sa.case((AcademicSession.is_current==1,0),else_=1),
                      StudentEnrolment.id.desc()).limit(1))
    if not row: abort(404)
    child=_flatten(row,'Student','relationship','class_name','session_name')
    assignments=_student_assignments(student_id,with_id=False)
    projects=_student_projects(student_id,with_id=False)
    results=_released_results(student_id)
    feedback=db.session.scalars(select(ParentFeedback).where(
        ParentFeedback.parent_id==pid,ParentFeedback.student_id==student_id)
        .order_by(ParentFeedback.id.desc()).limit(10)).all()
    replies=_feedback_replies([x.id for x in feedback])
    avg,completion,trend=_assignment_metrics(assignments)
    fee_summary=_finance_student_lifetime_totals(student_id)

    notif_rows=db.session.scalars(select(SchoolNotification).where(
        SchoolNotification.recipient_type=='parent',SchoolNotification.recipient_id==pid,
        SchoolNotification.student_id==student_id)
        .order_by(SchoolNotification.id.desc()).limit(20)).all()
    notifications=[{'id':n.id,'title':n.title,'message':n.message,'category':n.category,
                    'action_url':n.action_url,'created_at':n.created_at,
                    'is_new':n.read_at is None} for n in notif_rows]
    # Viewing this page is what "reads" these notifications: they show as
    # New once here, then as Old on every visit after this.
    unread_ids=[n.id for n in notif_rows if n.read_at is None]
    if unread_ids:
        now=datetime.now(timezone.utc).isoformat()
        db.session.execute(sa_update(SchoolNotification)
            .where(SchoolNotification.id.in_(unread_ids)).values(read_at=now))
        db.session.commit()

    return render_template('parent_child_detail.html',child=child,assignments=assignments,
        projects=projects,results=results,feedback=feedback,replies=replies,avg=avg,
        completion=completion,trend=trend,fee_summary=fee_summary,notifications=notifications)

@app.route('/parent/children/<int:student_id>/finance')
@parent_required
def parent_child_finance(student_id):
    pid=session['parent_id']
    if not _parent_owns_student(pid,student_id): abort(404)
    student=one(select(Student.id,Student.admission_no,Student.first_name,
                       Student.middle_name,Student.last_name).where(Student.id==student_id))
    if not student: abort(404)

    sessions=_active_sessions()
    requested_session=request.args.get('session_id','').strip()
    try: session_id=int(requested_session) if requested_session else None
    except (TypeError,ValueError): session_id=None
    if not session_id:
        current=_school_current_session()
        session_id=current['id'] if current else None
    session_row=obj(AcademicSession,session_id) if session_id else None

    account=_finance_student_outstanding(student_id,session_id) if session_row else []
    # The headline balance is always the lifetime, all-sessions figure, so a
    # balance left over from a previous session is never hidden just because
    # the session selector below happens to be pointed at a newer one.
    lifetime=_finance_student_lifetime_totals(student_id)
    other_sessions_with_balance=_finance_student_sessions_with_balance(student_id,exclude_session_id=session_id)

    payments=db.session.scalars(select(FinancePayment)
        .where(FinancePayment.student_id==student_id)
        .order_by(FinancePayment.paid_at.desc(),FinancePayment.id.desc())).all()

    return render_template(
        'parent_child_finance.html',
        student=student,sessions=sessions,session=session_row,account=account,
        total_assessed=lifetime['assessed'],total_paid=lifetime['paid'],
        total_outstanding=lifetime['outstanding'],total_unallocated=lifetime['unallocated'],
        other_sessions_with_balance=other_sessions_with_balance,
        payments=payments)

@app.route('/parent/children/<int:student_id>/receipts/<int:payment_id>/pdf')
@parent_required
def parent_receipt_pdf(student_id, payment_id):
    pid=session['parent_id']
    if not _parent_owns_student(pid,student_id): abort(404)
    payment=one(select(FinancePayment.id,FinancePayment.student_id,FinancePayment.receipt_no)
                .where(FinancePayment.id==payment_id))
    if not payment or int(payment['student_id'])!=int(student_id): abort(404)
    pdf,_=_receipt_pdf(payment_id)
    return Response(pdf,mimetype='application/pdf',
                    headers={'Content-Disposition':f'inline; filename={payment["receipt_no"]}.pdf'})

@app.route('/parent/feedback',methods=['GET','POST'])
@parent_required
@csrf_protect
def parent_feedback():
    pid=session['parent_id']
    children=[_flatten(r,'Student','class_name') for r in all_rows(
        select(Student,SchoolClass.name.label('class_name'))
        .select_from(ParentStudentLink)
        .join(Student,and_(Student.id==ParentStudentLink.student_id,Student.active==1))
        .outerjoin(StudentEnrolment,and_(StudentEnrolment.student_id==Student.id,
                                         StudentEnrolment.active==1))
        .outerjoin(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
        .where(ParentStudentLink.parent_id==pid,ParentStudentLink.active==1)
        # PostgreSQL wants every selected column grouped: the student's columns follow from its id,
        # but the class name is a different table's, so it is grouped too.
        .group_by(Student.id,SchoolClass.name).order_by(Student.first_name,Student.last_name))]
    parent=obj(ParentAccount,pid)
    if request.method=='POST':
        try: student_id=int(request.form.get('student_id')) if request.form.get('student_id') else None
        except (TypeError,ValueError): student_id=None
        subject=request.form.get('subject','').strip(); body=request.form.get('body','').strip()
        allowed={x['id'] for x in children}; errors=[]
        if student_id is not None and student_id not in allowed: errors.append('Select a child linked to your account.')
        if not subject: errors.append('Enter a subject for your message.')
        if not body: errors.append('Enter your feedback or message.')
        if len(body)>5000: errors.append('Please keep the message under 5,000 characters.')
        if errors:
            return render_template('parent_feedback.html',parent=parent,children=children,errors=errors,form=request.form)
        now=datetime.now(timezone.utc).isoformat(); target_class=None
        if student_id:
            target_class=one(select(SchoolClass.id,SchoolClass.name)
                .select_from(StudentEnrolment)
                .join(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
                .where(StudentEnrolment.student_id==student_id,StudentEnrolment.active==1)
                .order_by(StudentEnrolment.id.desc()).limit(1))
        # Route the message to staff who may manage it, respecting class scope.
        scoped={aid for (aid,) in tuples(select(AdminScope.admin_id).where(
            AdminScope.scope_type=='class').distinct())}
        candidates=[]
        for (aid,) in tuples(select(Admin.id).where(Admin.active==1)):
            if not admin_has_permission(aid,'parent.feedback.manage'):
                continue
            if target_class and not is_school_admin({'id':aid,'admin_type_system':0}):
                # Class-bound staff must cover this class; unscoped staff still qualify.
                if aid in scoped and not _school_class_allowed(aid,target_class['id']):
                    continue
            candidates.append(aid)
        assigned=candidates[0] if candidates else None
        thread=ParentFeedback(parent_id=pid,student_id=student_id,subject=subject,body=body,
                              status='open',assigned_admin_id=assigned,created_at=now,updated_at=now)
        db.session.add(thread); db.session.flush()
        sender_name=parent.display_name if parent else 'A parent'
        notification_message=f'{sender_name}: {subject}'
        exact_feedback_url=url_for('admin_school_parent_feedback_detail',feedback_id=thread.id)
        if assigned:
            targets=[assigned]
        else:
            targets=[aid for (aid,) in tuples(select(Admin.id).where(Admin.active==1))
                     if admin_has_permission(aid,'parent.feedback.view')]
        for aid in targets:
            db.session.add(AdminNotification(admin_id=aid,title='New parent message',
                message=notification_message,severity='info',
                action_url=exact_feedback_url,created_at=now))
        db.session.commit(); flash('Your message has been sent to the school.','success'); return redirect(url_for('parent_feedback'))
    feedback=[_flatten(r,'ParentFeedback','first_name','last_name') for r in all_rows(
        select(ParentFeedback,Student.first_name,Student.last_name)
        .outerjoin(Student,Student.id==ParentFeedback.student_id)
        .where(ParentFeedback.parent_id==pid)
        .order_by(ParentFeedback.id.desc()).limit(20))]
    replies=_feedback_replies([x['id'] for x in feedback])
    return render_template('parent_feedback.html',parent=parent,children=children,feedback=feedback,replies=replies,errors=[],form={})

@app.post('/parent/feedback/<int:feedback_id>/reply')
@parent_required
@csrf_protect
def parent_feedback_reply(feedback_id):
    pid=session['parent_id']
    row=obj(ParentFeedback,feedback_id)
    if not row or row.parent_id!=pid: abort(404)
    if row.status=='resolved':
        flash('This conversation has been marked resolved. It can no longer be replied to.','error')
        return redirect(url_for('parent_feedback'))
    body=request.form.get('body','').strip()
    if not body:
        flash('Enter a message before sending.','error'); return redirect(url_for('parent_feedback'))
    if len(body)>5000:
        flash('Please keep the message under 5,000 characters.','error'); return redirect(url_for('parent_feedback'))
    now=datetime.now(timezone.utc).isoformat()
    db.session.add(ParentFeedbackReply(feedback_id=feedback_id,admin_id=None,body=body,created_at=now))
    row.updated_at=now
    parent=obj(ParentAccount,pid)
    sender_name=parent.display_name if parent else 'A parent'
    notify_targets=[row.assigned_admin_id] if row.assigned_admin_id else [
        aid for (aid,) in tuples(select(Admin.id).where(Admin.active==1))
        if admin_has_permission(aid,'parent.feedback.view')]
    for aid in notify_targets:
        db.session.add(AdminNotification(admin_id=aid,title='New reply from a parent',
            message=f'{sender_name}: {row.subject}',severity='info',
            action_url=url_for('admin_school_parent_feedback_detail',feedback_id=feedback_id),
            created_at=now))
    db.session.commit()
    audit_log('parent_feedback_followup','school','parent_feedback',feedback_id)
    flash('Your message has been sent to the school.','success')
    return redirect(url_for('parent_feedback'))

@app.route('/admin/school/parent-feedback')
@admin_required
def admin_school_parent_feedback():
    me=current_admin()
    if not admin_has_permission(me['id'],'parent.feedback.view'): return admin_access_error('parent.feedback.view')
    rows=[_flatten(r,'ParentFeedback','parent_name','parent_username','first_name',
                   'last_name','class_id','class_name') for r in all_rows(
        select(ParentFeedback,
               ParentAccount.display_name.label('parent_name'),
               ParentAccount.username.label('parent_username'),
               Student.first_name,Student.last_name,
               SchoolClass.id.label('class_id'),SchoolClass.name.label('class_name'))
        .join(ParentAccount,ParentAccount.id==ParentFeedback.parent_id)
        .outerjoin(Student,Student.id==ParentFeedback.student_id)
        .outerjoin(StudentEnrolment,and_(StudentEnrolment.student_id==Student.id,
                                         StudentEnrolment.active==1))
        .outerjoin(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
        .order_by(sa.case((ParentFeedback.status=='open',0),
                          (ParentFeedback.status=='in_progress',1),else_=2),
                  ParentFeedback.id.desc()))]
    if not me['admin_type_system']:
        rows=[r for r in rows if not r['class_id'] or _school_class_allowed(me['id'],r['class_id'])]
    replies=_feedback_replies([r['id'] for r in rows])
    return render_template('admin_parent_feedback.html',feedback=rows,replies=replies)

@app.route('/admin/school/parent-feedback/<int:feedback_id>')
@admin_required
def admin_school_parent_feedback_detail(feedback_id):
    me=current_admin()
    if not admin_has_permission(me['id'],'parent.feedback.view'): return admin_access_error('parent.feedback.view')
    raw=one(select(ParentFeedback,
                   ParentAccount.display_name.label('parent_name'),
                   ParentAccount.username.label('parent_username'),
                   ParentAccount.email.label('parent_email'),
                   ParentAccount.phone.label('parent_phone'),
                   Student.first_name,Student.last_name,
                   SchoolClass.id.label('class_id'),SchoolClass.name.label('class_name'))
            .join(ParentAccount,ParentAccount.id==ParentFeedback.parent_id)
            .outerjoin(Student,Student.id==ParentFeedback.student_id)
            .outerjoin(StudentEnrolment,and_(StudentEnrolment.student_id==Student.id,
                                             StudentEnrolment.active==1))
            .outerjoin(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
            .where(ParentFeedback.id==feedback_id))
    if not raw: abort(404)
    row=_flatten(raw,'ParentFeedback','parent_name','parent_username','parent_email',
                 'parent_phone','first_name','last_name','class_id','class_name')
    if not me['admin_type_system'] and row['class_id'] and not _school_class_allowed(me['id'],row['class_id']): return admin_access_error('parent feedback scope')
    replies=_feedback_replies([feedback_id]).get(feedback_id,[])
    return render_template('admin_parent_feedback_detail.html',feedback=row,replies=replies)

@app.post('/admin/school/parent-feedback/<int:feedback_id>/reply')
@admin_required
@csrf_protect
def admin_school_parent_feedback_reply(feedback_id):
    me=current_admin()
    if not admin_has_permission(me['id'],'parent.feedback.manage'): return admin_access_error('parent.feedback.manage')
    body=request.form.get('body','').strip()
    if not body: flash('Enter a reply.','error'); return redirect(url_for('admin_school_parent_feedback'))
    row=obj(ParentFeedback,feedback_id)
    if not row: abort(404)
    now=datetime.now(timezone.utc).isoformat()
    db.session.add(ParentFeedbackReply(feedback_id=feedback_id,admin_id=me['id'],body=body,created_at=now))
    row.status='in_progress'; row.assigned_admin_id=me['id']; row.updated_at=now
    db.session.add(SchoolNotification(
        recipient_type='parent',recipient_id=row.parent_id,student_id=row.student_id,
        category='feedback',title='School replied to your feedback',message=body,
        action_url=url_for('parent_feedback'),created_at=now,created_by=me['id']))
    db.session.commit()
    # Best-effort: a parent contact that's blank or a channel that's down must
    # never break the in-app reply that just succeeded.
    parent_contact=one(select(ParentAccount.email,ParentAccount.phone,ParentAccount.display_name)
                       .where(ParentAccount.id==row.parent_id))
    if parent_contact:
        subject_line=f'School replied: {row.subject}' if row.subject else 'School replied to your message'
        try:
            _notify_guardian_email(parent_contact['email'],subject_line,
                f"Dear {parent_contact['display_name'] or 'Parent/Guardian'},\n\n"
                f"The school has replied to your message"
                f"{f' ({row.subject})' if row.subject else ''}:\n\n{body}\n\n"
                "Sign in to the parent portal to continue the conversation.\n\n"
                f"{school_name()}")
        except Exception: app.logger.exception('Parent feedback email notification failed for feedback %s',feedback_id)
        try:
            _notify_guardian_whatsapp(parent_contact['phone'],
                f"{school_name()}: You have a reply to your message"
                f"{f' ({row.subject})' if row.subject else ''}. "
                "Sign in to the parent portal to view it.")
        except Exception: app.logger.exception('Parent feedback WhatsApp notification failed for feedback %s',feedback_id)
    audit_log('parent_feedback_replied','school','parent_feedback',feedback_id)
    return redirect(url_for('admin_school_parent_feedback'))

@app.post('/admin/school/parent-feedback/<int:feedback_id>/status')
@admin_required
@csrf_protect
def admin_school_parent_feedback_status(feedback_id):
    me=current_admin()
    if not admin_has_permission(me['id'],'parent.feedback.manage'): return admin_access_error('parent.feedback.manage')
    status=request.form.get('status','open')
    if status not in ('open','in_progress','resolved'): status='open'
    row=obj(ParentFeedback,feedback_id)
    if not row: abort(404)
    now=datetime.now(timezone.utc).isoformat()
    row.status=status; row.updated_at=now; row.closed_at=now if status=='resolved' else None
    db.session.commit()
    audit_log('parent_feedback_status_changed','school','parent_feedback',feedback_id,{'status':status})
    return redirect(url_for('admin_school_parent_feedback'))

@app.route('/admin/school/parents')
@admin_required
def admin_school_parents():
    me=current_admin()
    if not admin_has_permission(me['id'],'parent.view'):
        return admin_access_error('parent.view')
    parents=[_flatten(r,'ParentAccount','child_count') for r in all_rows(
        select(ParentAccount,func.count(ParentStudentLink.id).label('child_count'))
        .outerjoin(ParentStudentLink,and_(ParentStudentLink.parent_id==ParentAccount.id,
                                          ParentStudentLink.active==1))
        .group_by(ParentAccount.id).order_by(ParentAccount.display_name))]
    if not me['admin_type_system']:
        allowed_classes={cid for (cid,) in tuples(
            select(SchoolClass.id).where(SchoolClass.active==1))
            if _school_class_allowed(me['id'],cid)}
        if not allowed_classes:
            parents=[]
        else:
            # One query finds every parent with a child in an authorised class,
            # instead of one query per parent.
            visible={pid for (pid,) in tuples(
                select(ParentStudentLink.parent_id).distinct()
                .join(StudentEnrolment,and_(StudentEnrolment.student_id==ParentStudentLink.student_id,
                                            StudentEnrolment.active==1))
                .where(ParentStudentLink.active==1,
                       StudentEnrolment.class_id.in_(list(allowed_classes))))}
            parents=[p for p in parents if p['id'] in visible]
    return render_template('admin_parents.html',parents=parents)

@app.route('/admin/school/parents/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_parent_new():
    me=current_admin()
    if not admin_has_permission(me['id'],'parent.manage'):
        return admin_access_error('parent.manage')
    classes,students=_parent_form_context(me)
    if request.method=='POST':
        display=request.form.get('display_name','').strip()
        email=request.form.get('email','').strip().lower()
        phone=request.form.get('phone','').strip()
        username=request.form.get('username','').strip().lower()
        selected=[int(x) for x in request.form.getlist('student_ids') if x.isdigit()]
        relationship=request.form.get('relationship','').strip()
        errors=[]
        if not display: errors.append('Parent / guardian name is required.')
        if not username: username=(re.sub(r'[^a-z0-9]+','.',display.lower()).strip('.') or 'parent')
        if one_scalar(select(ParentAccount.id).where(ParentAccount.username==username)): errors.append('That parent username is already in use.')
        if email and ('@' not in email or '.' not in email.split('@')[-1]): errors.append('Enter a valid email address.')
        if not selected: errors.append('Link at least one student to this parent account.')
        allowed={st['id'] for st in students}
        if any(sid not in allowed for sid in selected): errors.append('One or more selected students are outside your authorised school scope.')
        if errors:
            return render_template('admin_parent_form.html',students=students,classes=classes,errors=errors,form=request.form)
        password=_new_parent_password()
        now=datetime.now(timezone.utc).isoformat()
        parent=ParentAccount(username=username,display_name=display,email=email or None,
            phone=phone or None,password_hash=generate_password_hash(password),
            active=1,password_must_change=1,created_at=now)
        db.session.add(parent); db.session.flush(); pid=parent.id
        for sid in selected:
            db.session.add(ParentStudentLink(parent_id=pid,student_id=sid,
                relationship=relationship or None,active=1,created_at=now,created_by=me['id']))
        db.session.commit()
        audit_log('parent_account_created','school','parent',pid,{'linked_students':len(selected)})
        return render_template('admin_parent_created.html',parent={'id':pid,'username':username,'display_name':display},password=password)
    return render_template('admin_parent_form.html',students=students,classes=classes,errors=[],form={})

@app.route('/admin/school/parents/<int:pid>/edit',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_parent_edit(pid):
    me=current_admin()
    if not admin_has_permission(me['id'],'parent.manage'): return admin_access_error('parent.manage')
    parent=obj(ParentAccount,pid)
    if not parent: abort(404)
    classes,allowed_students=_parent_form_context(me)
    links=db.session.scalars(select(ParentStudentLink).where(
        ParentStudentLink.parent_id==pid,ParentStudentLink.active==1)).all()
    linked={x.student_id:x.relationship or '' for x in links}
    if request.method=='POST':
        display=request.form.get('display_name','').strip(); email=request.form.get('email','').strip().lower(); phone=request.form.get('phone','').strip(); selected=[int(x) for x in request.form.getlist('student_ids') if x.isdigit()]; relationship=request.form.get('relationship','').strip(); errors=[]
        if not display: errors.append('Parent / guardian name is required.')
        if email and ('@' not in email or '.' not in email.rsplit('@',1)[-1]): errors.append('Enter a valid email address.')
        allowed={x['id'] for x in allowed_students}
        if not selected: errors.append('Link at least one student to this parent account.')
        if any(x not in allowed for x in selected): errors.append('One or more selected students are outside your authorised school scope.')
        if errors: return render_template('admin_parent_form.html',students=allowed_students,classes=classes,errors=errors,form=request.form,editing=parent,linked=linked)
        now=datetime.now(timezone.utc).isoformat()
        parent.display_name=display; parent.email=email or None; parent.phone=phone or None
        # Retire every link, then bring the selected ones back, so an unlinked child stops
        # being visible to this parent. A parent and a child have one link row between them
        # (a unique key), so an existing row is reactivated rather than inserted a second time.
        db.session.execute(sa_update(ParentStudentLink)
            .where(ParentStudentLink.parent_id==pid).values(active=0))
        have={x.student_id:x for x in db.session.scalars(select(ParentStudentLink).where(
            ParentStudentLink.parent_id==pid)).all()}
        for sid in selected:
            link=have.get(sid)
            if link:
                link.active=1; link.relationship=relationship or None
            else:
                db.session.add(ParentStudentLink(parent_id=pid,student_id=sid,
                    relationship=relationship or None,active=1,created_at=now,created_by=me['id']))
        db.session.commit()
        audit_log('parent_account_updated','school','parent',pid,{'linked_students':len(selected)}); flash('Parent account updated.','success'); return redirect(url_for('admin_school_parents'))
    form={'display_name':parent.display_name,'email':parent.email or '','phone':parent.phone or '','relationship':next(iter(linked.values()),'')}
    return render_template('admin_parent_form.html',students=allowed_students,classes=classes,errors=[],form=form,editing=parent,linked=linked)

@app.post('/admin/school/parents/<int:pid>/credentials/reset')
@admin_required
@csrf_protect
def admin_school_parent_credentials_reset(pid):
    me=current_admin()
    if not admin_has_permission(me['id'],'parent.manage'): return admin_access_error('parent.manage')
    row=db.session.scalars(select(ParentAccount).where(
        ParentAccount.id==pid,ParentAccount.active==1)).first()
    if not row: abort(404)
    temporary=_new_parent_password()
    username=row.username
    payload={c.key:getattr(row,c.key) for c in row.__mapper__.column_attrs}
    row.password_hash=generate_password_hash(temporary)
    row.password_must_change=1
    db.session.commit()
    audit_log('parent_credentials_reset','authentication','parent',pid,{'username':username,'reset_by':me['username']},True,me); flash('A new temporary parent password was generated. The previous password no longer works.','success'); return render_template('admin_parent_created.html',parent=payload,password=temporary,reset=True)
