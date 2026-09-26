"""Admin finance: payment recording/allocation, receipts (view/print/PDF/
email/WhatsApp), the receipt signature settings page, fee items, and fee
assessments.
"""

import json
import os
from datetime import datetime, timezone

from flask import Response, abort, flash, jsonify, redirect, render_template, request, url_for
from sqlalchemy import and_, func, select, update as sa_update

from app import app, FINANCE_FEE_APPLICABILITY, FINANCE_FEE_CATEGORIES, _active_sessions, _school_current_session
from models import (
    AcademicSession, FinanceFeeAssessment, FinanceFeeItem, FinanceFeeItemClass,
    FinancePayment, FinancePaymentAllocation, SchoolClass, Student,
    StudentEnrolment, db,
)
from core.db_helpers import all_rows, insert_stmt, obj, one, one_scalar, tuples, _flatten
from core.background import run_in_background
from core.notifications import _notify_parents_fee_assessed, _notify_parents_payment_recorded
from core.security import admin_access_error, admin_required, audit_log, current_admin, csrf_protect
from core.uploads import _save_image_upload
from blueprints.finance.helpers import (
    _active_classes, _class_group, _finance_assessment_allocated,
    _finance_can_view_all, _finance_payment_allocated,
    _finance_student_lifetime_totals, _finance_student_outstanding,
    _legacy_stage_for, _log_receipt_delivery, _money, _next_receipt_no, _payment_status,
    _receipt_payload, _receipt_pdf, _receipt_sheet, _receipt_signature_abspath,
    _send_payment_receipt_to_guardian,
    _receipt_signature_relpath, _save_signature_data_url, _send_email_receipt,
    _send_whatsapp_receipt, _set_receipt_signature, RECEIPT_SIGNATURE_SETTING_KEY,
)

# The billing periods a charge can be raised for: the fee-picker's choices.
ASSESSMENT_TERMS=('Full Session','First Term','Second Term','Third Term')


@app.route('/admin/finance/payments/<int:payment_id>/allocate',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_finance_payment_allocate(payment_id):
    me=current_admin()
    raw=one(select(FinancePayment,Student.first_name,Student.middle_name,
                   Student.last_name,Student.admission_no)
            .join(Student,Student.id==FinancePayment.student_id)
            .where(FinancePayment.id==payment_id))
    if not raw:
        abort(404)
    payment=_flatten(raw,'FinancePayment','first_name','middle_name','last_name','admission_no')

    if not _finance_can_view_all(me) and payment['recorded_by']!=me['id']:
        return admin_access_error('finance.view_own')

    back=redirect(url_for('admin_finance_payment_allocate',payment_id=payment_id))

    if payment['status']!='posted':
        flash('Only a posted payment can be allocated.','error')
        return redirect(url_for('admin_finance_receipt',payment_id=payment_id))

    if request.method=='POST':
        raw_allocations=request.form.get('allocations','').strip()
        try:
            submitted=json.loads(raw_allocations) if raw_allocations else {}
        except ValueError:
            flash('The allocation data is invalid.','error'); return back

        if not isinstance(submitted,dict):
            flash('The allocation data is invalid.','error'); return back

        payment_amount=round(float(payment['amount'] or 0),2)
        existing_allocated=_finance_payment_allocated(payment_id)
        available_payment=round(payment_amount-existing_allocated,2)

        if available_payment<0:
            flash('This payment already contains invalid allocations.','error'); return back

        clean_allocations=[]
        requested_total=0.0

        for raw_assessment_id,raw_amount in submitted.items():
            try:
                assessment_id=int(raw_assessment_id)
            except (TypeError,ValueError):
                flash('One or more allocation amounts are invalid.','error'); return back
            amount=_money(raw_amount)
            if amount is None:
                flash('One or more allocation amounts are invalid.','error'); return back

            if amount<=0:
                continue

            assessment=obj(FinanceFeeAssessment,assessment_id)
            if not assessment:
                flash('One or more selected assessments could not be found.','error'); return back
            if not assessment.active:
                flash('A selected assessment is inactive.','error'); return back
            # Money may only move between records belonging to one student.
            if int(assessment.student_id)!=int(payment['student_id']):
                flash('A payment can only be allocated to assessments belonging to the same student.','error'); return back

            already_allocated=_finance_assessment_allocated(assessment_id)
            assessment_balance=round(max(0.0, round(float(assessment.amount or 0),2)-already_allocated),2)

            if assessment_balance<=0.000001:
                flash(f'{assessment.category} has already been fully paid and cannot receive a further allocation.','error'); return back
            if amount>assessment_balance+0.000001:
                flash(f'Allocation for {assessment.category} exceeds its outstanding balance.','error'); return back

            requested_total+=amount
            clean_allocations.append((assessment_id,amount))

        if requested_total>available_payment+0.000001:
            flash('The total allocation exceeds the payment balance.','error'); return back

        if not clean_allocations:
            flash('Enter at least one allocation amount.','error'); return back

        now=datetime.now(timezone.utc).isoformat()
        db.session.add_all([FinancePaymentAllocation(
            payment_id=payment_id,assessment_id=assessment_id,amount=amount,
            created_by=me['id'],created_at=now)
            for assessment_id,amount in clean_allocations])
        db.session.commit()

        remaining=max(0.0, available_payment-requested_total)
        audit_log('finance_payment_allocated','finance','payment',payment_id,{
            'allocated_total':requested_total,
            'allocation_count':len(clean_allocations),
            'remaining_unallocated':remaining})

        if remaining>0.000001:
            flash(f'₦{requested_total:,.2f} allocated successfully. ₦{remaining:,.2f} remains unallocated.','success')
        else:
            flash(f'₦{requested_total:,.2f} allocated successfully. The payment is fully allocated.','success')
        return redirect(url_for('admin_finance_receipt',payment_id=payment_id))

    # GET
    # Only fees still owing are offered here — a fully paid item has nothing
    # left to apply this payment to, so it must not appear as a choice at all.
    outstanding=[item for item in _finance_student_outstanding(payment['student_id'],payment['session_id'])
                 if item['outstanding']>0.000001]
    payment_allocated=_finance_payment_allocated(payment_id)
    available_payment=max(0.0, float(payment['amount'] or 0)-payment_allocated)
    existing=[_flatten(r,'FinancePaymentAllocation','category','assessed_amount')
              for r in all_rows(
        select(FinancePaymentAllocation,FinanceFeeAssessment.category,
               FinanceFeeAssessment.amount.label('assessed_amount'))
        .join(FinanceFeeAssessment,FinanceFeeAssessment.id==FinancePaymentAllocation.assessment_id)
        .where(FinancePaymentAllocation.payment_id==payment_id)
        .order_by(FinancePaymentAllocation.id))]

    return render_template(
        'finance_payment_allocate.html',
        payment=payment,
        outstanding=outstanding,
        existing=existing,
        payment_allocated=payment_allocated,
        available_payment=available_payment)

@app.route('/admin/finance/students/<int:student_id>/account')
@admin_required
def admin_finance_student_account(student_id):
    me = current_admin()
    student = one(select(Student.id,Student.admission_no,Student.first_name,
                         Student.middle_name,Student.last_name)
                  .where(Student.id==student_id,Student.active==1))
    if not student:
        abort(404)

    sessions = _active_sessions()

    requested_session = request.args.get('session_id','').strip()
    try:
        session_id = int(requested_session) if requested_session else None
    except (TypeError,ValueError):
        session_id = None
    if not session_id:
        current = _school_current_session()
        session_id = current['id'] if current else None

    session_row = obj(AcademicSession, session_id) if session_id else None

    account = _finance_student_outstanding(student_id, session_id) if session_row else []

    total_assessed = sum(item['assessed'] for item in account)
    total_paid = sum(item['paid'] for item in account)
    total_outstanding = sum(item['outstanding'] for item in account)

    payments = db.session.scalars(select(FinancePayment)
        .where(FinancePayment.student_id==student_id)
        .order_by(FinancePayment.paid_at.desc(),FinancePayment.id.desc())).all()

    return render_template(
        'finance_student_account.html',
        student=student,
        sessions=sessions,
        session=session_row,
        account=account,
        total_assessed=total_assessed,
        total_paid=total_paid,
        total_outstanding=total_outstanding,
        payments=payments)

@app.route('/admin/finance')
@admin_required
def admin_finance_dashboard():
    me=current_admin()
    own=not _finance_can_view_all(me)
    # A cashier without finance.view_all only ever sees their own takings.
    scope=[FinancePayment.status=='posted']
    if own: scope.append(FinancePayment.recorded_by==me['id'])
    today=datetime.now().strftime('%Y-%m-%d'); month=datetime.now().strftime('%Y-%m')
    day=func.substr(FinancePayment.paid_at,1,10)
    mon=func.substr(FinancePayment.paid_at,1,7)

    def total(*extra):
        return one_scalar(select(func.coalesce(func.sum(FinancePayment.amount),0))
                          .where(*scope,*extra), 0)

    today_total=total(day==today)
    month_total=total(mon==month)
    count_today=one_scalar(select(func.count()).select_from(FinancePayment)
                           .where(*scope,day==today), 0)
    cash=total(day==today,func.lower(FinancePayment.method)=='cash')
    bank=total(day==today,func.lower(FinancePayment.method)!='cash')
    outstanding=None
    if not own:
        assessed=one_scalar(select(func.coalesce(func.sum(FinanceFeeAssessment.amount),0))
                            .where(FinanceFeeAssessment.active==1), 0)
        paid=one_scalar(select(func.coalesce(func.sum(FinancePayment.amount),0))
                        .where(FinancePayment.status=='posted'), 0)
        allocated=one_scalar(
            select(func.coalesce(func.sum(FinancePaymentAllocation.amount),0))
            .select_from(FinancePaymentAllocation)
            .join(FinancePayment,FinancePayment.id==FinancePaymentAllocation.payment_id)
            .join(FinanceFeeAssessment,FinanceFeeAssessment.id==FinancePaymentAllocation.assessment_id)
            .where(FinancePayment.status=='posted',FinanceFeeAssessment.active==1), 0)
        outstanding=max(0, float(assessed)-float(allocated))
        unallocated=max(0, float(paid)-float(allocated))
    rows=[_flatten(r,'FinancePayment','first_name','middle_name','last_name','admission_no')
          for r in all_rows(
        select(FinancePayment,Student.first_name,Student.middle_name,
               Student.last_name,Student.admission_no)
        .join(Student,Student.id==FinancePayment.student_id)
        .where(*scope)
        .order_by(FinancePayment.paid_at.desc(),FinancePayment.id.desc()).limit(20))]
    return render_template('finance_dashboard.html',today_total=today_total,month_total=month_total,count_today=count_today,cash=cash,bank=bank,outstanding=outstanding,rows=rows,view_all=not own)

@app.route('/admin/finance/payments/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_finance_record():
    me=current_admin()
    students=all_rows(select(Student.id,Student.admission_no,Student.first_name,
                             Student.middle_name,Student.last_name,
                             Student.guardian_email,Student.guardian_phone)
                      .where(Student.active==1)
                      .order_by(Student.last_name,Student.first_name))
    sessions=db.session.scalars(select(AcademicSession).where(AcademicSession.active==1)
                                .order_by(AcademicSession.id.desc())).all()
    current=_school_current_session()
    if request.method=='POST':
        try: student_id=int(request.form.get('student_id','')); session_id=int(request.form.get('session_id',''))
        except (TypeError,ValueError): student_id=session_id=0
        amount=_money(request.form.get('amount','0'))
        category=request.form.get('category','School Fees').strip() or 'School Fees'; method=request.form.get('method','Bank Transfer').strip(); reference=request.form.get('reference','').strip(); paid_at=request.form.get('paid_at','').strip() or datetime.now().strftime('%Y-%m-%d %H:%M'); notes=request.form.get('notes','').strip(); errors=[]
        if not student_id: errors.append('Select a student.')
        if amount is None: errors.append('Enter the payment amount as a number of naira, for example 12500.50.')
        elif amount<=0: errors.append('Payment amount must be greater than zero.')
        if not session_id: errors.append('Select an academic session.')
        if errors: return render_template('finance_payment_form.html',students=students,sessions=sessions,form=request.form,errors=errors)
        student=db.session.scalars(select(Student).where(
            Student.id==student_id,Student.active==1)).first()
        if not student: return render_template('finance_payment_form.html',students=students,sessions=sessions,form=request.form,errors=['Student not found.'])
        payer_name=request.form.get('payer_name','').strip()
        receipt=_next_receipt_no(); now=datetime.now(timezone.utc).isoformat()
        payment=FinancePayment(receipt_no=receipt,student_id=student_id,session_id=session_id,
            amount=amount,category=category,method=method,reference=reference,
            paid_at=paid_at,recorded_by=me['id'],status='posted',notes=notes,
            created_at=now,payer_name=payer_name)
        db.session.add(payment); db.session.commit()
        audit_log('finance_payment_recorded','finance','payment',payment.id,{'receipt_no':receipt,'amount':amount,'student_id':student_id,'method':method})
        try:
            _notify_parents_payment_recorded(student_id,receipt,amount,category,me['id'],external=False)
        except Exception:
            app.logger.exception('Parent payment-recorded notification failed for student %s',student_id)
        # The parents get the receipt itself by email and WhatsApp, without anyone having to send it.
        run_in_background(_send_payment_receipt_to_guardian,payment.id,me['id'])
        return redirect(url_for('admin_finance_receipt',payment_id=payment.id))
    return render_template('finance_payment_form.html',students=students,sessions=sessions,form=None,errors=[],current_session=dict(current) if current else None)

@app.route('/admin/finance/receipts/<int:payment_id>')
@admin_required
def admin_finance_receipt(payment_id):
    me=current_admin(); row=_receipt_payload(payment_id)
    if not row: abort(404)
    if not _finance_can_view_all(me) and row['recorded_by']!=me['id']: return admin_access_error('finance.view_own')
    return render_template('finance_receipt.html',payment=row,sheet=_receipt_sheet(payment_id),signature_path=_receipt_signature_relpath())

@app.route('/admin/finance/receipts/<int:payment_id>/print')
@admin_required
def admin_finance_receipt_print(payment_id):
    me=current_admin(); row=_receipt_payload(payment_id)
    if not row: abort(404)
    if not _finance_can_view_all(me) and row['recorded_by']!=me['id']: return admin_access_error('finance.view_own')
    return render_template('finance_receipt_print.html',payment=row,sheet=_receipt_sheet(payment_id),signature_path=_receipt_signature_relpath())

@app.route('/admin/finance/receipts/<int:payment_id>/pdf')
@admin_required
def admin_finance_receipt_pdf(payment_id):
    me=current_admin(); row=_receipt_payload(payment_id)
    if not row: abort(404)
    if not _finance_can_view_all(me) and row['recorded_by']!=me['id']: return admin_access_error('finance.view_own')
    pdf,_=_receipt_pdf(payment_id); return Response(pdf,mimetype='application/pdf',headers={'Content-Disposition':f'inline; filename={row["receipt_no"]}.pdf'})

@app.post('/admin/finance/receipts/<int:payment_id>/email')
@admin_required
@csrf_protect
def admin_finance_receipt_email(payment_id):
    me=current_admin(); row=_receipt_payload(payment_id)
    if not row: abort(404)
    if not _finance_can_view_all(me) and row['recorded_by']!=me['id']: return admin_access_error('finance.receipt.send')
    ok,msg=_send_email_receipt(payment_id)
    _log_receipt_delivery(payment_id,'email',row['guardian_email'],ok,msg,me['id'])
    audit_log('finance_receipt_email','finance','payment',payment_id,{'success':ok}); flash('Receipt emailed successfully.' if ok else msg,'success' if ok else 'error'); return redirect(url_for('admin_finance_receipt',payment_id=payment_id))

@app.post('/admin/finance/receipts/<int:payment_id>/whatsapp')
@admin_required
@csrf_protect
def admin_finance_receipt_whatsapp(payment_id):
    me=current_admin(); row=_receipt_payload(payment_id)
    if not row: abort(404)
    if not _finance_can_view_all(me) and row['recorded_by']!=me['id']: return admin_access_error('finance.receipt.send')
    ok,msg=_send_whatsapp_receipt(payment_id)
    _log_receipt_delivery(payment_id,'whatsapp',row['guardian_phone'],ok,msg,me['id'])
    audit_log('finance_receipt_whatsapp','finance','payment',payment_id,{'success':ok}); flash('Receipt sent through WhatsApp Business successfully.' if ok else msg,'success' if ok else 'error'); return redirect(url_for('admin_finance_receipt',payment_id=payment_id))

@app.route('/admin/finance/receipt-settings',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_finance_receipt_settings():
    me=current_admin(); errors=[]
    if request.method=='POST':
        action=request.form.get('action','').strip()
        try:
            if action=='remove':
                old=_receipt_signature_abspath(); _set_receipt_signature('',me['id'])
                if old:
                    try: os.remove(old)
                    except OSError: pass
                flash('Authorised signature removed.','success')
            elif action=='draw':
                rel=_save_signature_data_url(request.form.get('signature_data_url',''))
                old=_receipt_signature_abspath(); _set_receipt_signature(rel,me['id'])
                if old:
                    try: os.remove(old)
                    except OSError: pass
                flash('Signature saved.','success')
            elif action=='upload':
                rel=_save_image_upload(request.files.get('signature_file'),'signatures','authorised')
                if not rel: raise ValueError('Choose an image file to upload.')
                old=_receipt_signature_abspath(); _set_receipt_signature(rel,me['id'])
                if old:
                    try: os.remove(old)
                    except OSError: pass
                flash('Signature saved.','success')
            else:
                errors.append('Unrecognised action.')
        except ValueError as exc:
            errors.append(str(exc))
        if not errors:
            audit_log('finance_receipt_signature_updated','finance','school_setting',RECEIPT_SIGNATURE_SETTING_KEY,{'action':action})
            return redirect(url_for('admin_finance_receipt_settings'))
    return render_template('finance_receipt_settings.html',signature_path=_receipt_signature_relpath(),errors=errors)

@app.route('/admin/finance/fee-items')
@admin_required
def admin_finance_fee_items():
    items=db.session.scalars(select(FinanceFeeItem)
        .order_by(FinanceFeeItem.optional,FinanceFeeItem.name,FinanceFeeItem.id)).all()
    sessions=_active_sessions()
    current=_school_current_session()
    current_session_id=int(current['id']) if current else 0

    students=all_rows(
        select(Student.id,Student.admission_no,Student.first_name,Student.middle_name,
               Student.last_name,StudentEnrolment.class_id,
               SchoolClass.name.label('class_name'),SchoolClass.stage.label('class_stage'))
        .outerjoin(StudentEnrolment,and_(StudentEnrolment.student_id==Student.id,
                                         StudentEnrolment.session_id==current_session_id,
                                         StudentEnrolment.active==1))
        .outerjoin(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
        .where(Student.active==1)
        .order_by(Student.last_name,Student.first_name,Student.middle_name))

    classes=_active_classes()

    student_class_map={}
    for row in all_rows(
        select(StudentEnrolment.student_id,StudentEnrolment.session_id,
               StudentEnrolment.class_id,SchoolClass.name.label('class_name'),
               SchoolClass.stage.label('class_stage'))
        .join(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
        .where(StudentEnrolment.active==1,SchoolClass.active==1)):
        student_class_map[f"{row['student_id']}:{row['session_id']}"]={
            'class_id': int(row['class_id']),
            'class_name': row['class_name'],
            'stage': row['class_stage'] or 'General'}

    fee_class_map={}
    for row in all_rows(
        select(FinanceFeeItemClass.fee_item_id,SchoolClass.id.label('class_id'),
               SchoolClass.name.label('class_name'),SchoolClass.stage.label('class_stage'))
        .join(SchoolClass,SchoolClass.id==FinanceFeeItemClass.class_id)
        .where(FinanceFeeItemClass.active==1,SchoolClass.active==1)
        .order_by(SchoolClass.level_order,SchoolClass.name)):
        fee_class_map.setdefault(str(row['fee_item_id']),[]).append({
            'id': int(row['class_id']),
            'name': row['class_name'],
            'stage': row['class_stage'] or 'General'})

    class_groups={g:[dict(r) for r in classes if _class_group(r)==g]
                  for g in ('nursery','primary','college')}

    allocated_sq=(select(func.sum(FinancePaymentAllocation.amount))
                  .select_from(FinancePaymentAllocation)
                  .join(FinancePayment,FinancePayment.id==FinancePaymentAllocation.payment_id)
                  .where(FinancePaymentAllocation.assessment_id==FinanceFeeAssessment.id,
                         FinancePaymentAllocation.voided_at.is_(None),
                         FinancePayment.status=='posted')
                  .correlate(FinanceFeeAssessment).scalar_subquery())
    assessed_rows=[]
    for r in all_rows(
        select(FinanceFeeAssessment,Student.admission_no,Student.first_name,
               Student.last_name,AcademicSession.name.label('session_name'),
               FinanceFeeItem.name.label('fee_name'),
               func.coalesce(allocated_sq,0).label('allocated'))
        .join(Student,Student.id==FinanceFeeAssessment.student_id)
        .join(AcademicSession,AcademicSession.id==FinanceFeeAssessment.session_id)
        .outerjoin(FinanceFeeItem,FinanceFeeItem.id==FinanceFeeAssessment.fee_item_id)
        .where(FinanceFeeAssessment.active==1)
        .order_by(Student.last_name,Student.first_name,AcademicSession.id.desc(),
                  FinanceFeeAssessment.term,FinanceFeeAssessment.id.desc())
        .limit(2000)):
        row=_flatten(r,'FinanceFeeAssessment','admission_no','first_name','last_name',
                     'session_name','fee_name','allocated')
        row['payment_status']=_payment_status(row['amount'],row['allocated'])
        assessed_rows.append(row)

    # Student -> Session -> Term -> items, so a large assessment history can
    # be browsed the way staff actually think about it (find the student,
    # then the session, then the term) instead of one long flat table.
    assessed_by_student={}
    for row in assessed_rows:
        student_key=row['student_id']
        bucket=assessed_by_student.setdefault(student_key,{
            'student_id':student_key,'admission_no':row['admission_no'],
            'first_name':row['first_name'],'last_name':row['last_name'],
            'item_count':0,'total_assessed':0.0,'sessions':{}})
        bucket['item_count']+=1
        bucket['total_assessed']+=float(row['amount'] or 0)
        session_bucket=bucket['sessions'].setdefault(row['session_name'],{})
        term_bucket=session_bucket.setdefault(row['term'] or 'Full Session',[])
        term_bucket.append(row)
    assessed_students=sorted(assessed_by_student.values(),key=lambda b:(b['last_name'] or '',b['first_name'] or ''))

    return render_template(
        'finance_fee_items.html',
        items=items,
        students=students,
        sessions=sessions,
        current_session=current,
        classes=classes,
        class_groups=class_groups,
        student_class_map=student_class_map,
        fee_class_map=fee_class_map,
        assessed_students=assessed_students)

@app.route('/admin/finance/fee-items/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_finance_fee_item_new():
    errors=[]
    selected_class_ids=[]
    classes=_active_classes()
    valid_class_ids={int(r['id']) for r in classes}

    if request.method=='POST':
        name=request.form.get('name','').strip()
        category=request.form.get('category','School Fees').strip() or 'School Fees'
        applicability=request.form.get('applicability','Full Session').strip() or 'Full Session'
        required=1 if request.form.get('required')=='1' else 0
        optional=1 if request.form.get('optional')=='1' else 0
        notes=request.form.get('notes','').strip()
        for raw in request.form.getlist('class_ids'):
            try:
                cid=int(raw)
                if cid in valid_class_ids and cid not in selected_class_ids:
                    selected_class_ids.append(cid)
            except (TypeError,ValueError):
                pass
        amount=_money(request.form.get('amount','0'))

        if not name:
            errors.append('Fee item name is required.')
        if category not in FINANCE_FEE_CATEGORIES:
            errors.append('Select a valid fee category.')
        if applicability not in FINANCE_FEE_APPLICABILITY:
            errors.append('Select a valid billing rule.')
        if amount is None:
            errors.append('Enter the fee amount as a number of naira, for example 12500.50.')
        elif amount < 0:
            errors.append('Fee amount cannot be negative.')
        if not selected_class_ids:
            errors.append('Select at least one active class this fee applies to.')

        if not errors:
            selected_rows=[r for r in classes if int(r['id']) in selected_class_ids]
            legacy_stage=_legacy_stage_for(selected_rows,selected_class_ids,valid_class_ids)
            now=datetime.now(timezone.utc).isoformat()
            me=current_admin()
            item=FinanceFeeItem(name=name,category=category,stage=legacy_stage,amount=amount,
                applicability=applicability,required=required,optional=optional,active=1,
                notes=notes,created_at=now,updated_at=now,created_by=me['id'],updated_by=me['id'])
            db.session.add(item); db.session.flush()
            db.session.add_all([FinanceFeeItemClass(fee_item_id=item.id,class_id=cid,active=1,
                                                    created_at=now,created_by=me['id'])
                                for cid in selected_class_ids])
            db.session.commit()
            audit_log('finance_fee_item_created','finance','fee_item',item.id,{
                'name':name,'amount':amount,'class_ids':selected_class_ids})
            flash('Fee item created and assigned to the selected classes.','success')
            return redirect(url_for('admin_finance_fee_items'))

    return render_template(
        'finance_fee_item_form.html',
        errors=errors,
        form=request.form if request.method=='POST' else {},
        editing=None,
        classes=classes,
        selected_class_ids=selected_class_ids,
        fee_categories=FINANCE_FEE_CATEGORIES,
        fee_applicability=FINANCE_FEE_APPLICABILITY)

@app.route('/admin/finance/fee-items/<int:item_id>/edit',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_finance_fee_item_edit(item_id):
    errors=[]
    row=obj(FinanceFeeItem,item_id)
    if not row:
        abort(404)
    classes=_active_classes()
    valid_class_ids={int(r['id']) for r in classes}
    selected_class_ids=sorted(cid for (cid,) in tuples(
        select(FinanceFeeItemClass.class_id)
        .where(FinanceFeeItemClass.fee_item_id==item_id,FinanceFeeItemClass.active==1)))

    if request.method=='POST':
        name=request.form.get('name','').strip()
        category=request.form.get('category','School Fees').strip() or 'School Fees'
        applicability=request.form.get('applicability','Full Session').strip() or 'Full Session'
        required=1 if request.form.get('required')=='1' else 0
        optional=1 if request.form.get('optional')=='1' else 0
        notes=request.form.get('notes','').strip()
        selected_class_ids=[]
        for raw in request.form.getlist('class_ids'):
            try:
                cid=int(raw)
                if cid in valid_class_ids and cid not in selected_class_ids:
                    selected_class_ids.append(cid)
            except (TypeError,ValueError):
                pass
        amount=_money(request.form.get('amount','0'))

        if not name:
            errors.append('Fee item name is required.')
        if category not in FINANCE_FEE_CATEGORIES:
            errors.append('Select a valid fee category.')
        if applicability not in FINANCE_FEE_APPLICABILITY:
            errors.append('Select a valid billing rule.')
        if amount is None:
            errors.append('Enter the fee amount as a number of naira, for example 12500.50.')
        elif amount < 0:
            errors.append('Fee amount cannot be negative.')
        if not selected_class_ids:
            errors.append('Select at least one active class this fee applies to.')

        if not errors:
            selected_rows=[r for r in classes if int(r['id']) in selected_class_ids]
            legacy_stage=_legacy_stage_for(selected_rows,selected_class_ids,valid_class_ids)
            now=datetime.now(timezone.utc).isoformat()
            me=current_admin()
            row.name=name; row.category=category; row.stage=legacy_stage; row.amount=amount
            row.applicability=applicability; row.required=required; row.optional=optional
            row.notes=notes; row.updated_at=now; row.updated_by=me['id']
            # Retire every mapping, then re-activate the ones still selected, so
            # a class removed from the fee stops being charged.
            db.session.execute(sa_update(FinanceFeeItemClass)
                .where(FinanceFeeItemClass.fee_item_id==item_id).values(active=0))
            for cid in selected_class_ids:
                stmt=insert_stmt(FinanceFeeItemClass).values(
                    fee_item_id=item_id,class_id=cid,active=1,created_at=now,created_by=me['id'])
                db.session.execute(stmt.on_conflict_do_update(
                    index_elements=['fee_item_id','class_id'],
                    set_={'active':1,'created_at':stmt.excluded.created_at,
                          'created_by':stmt.excluded.created_by}))
            db.session.commit()
            audit_log('finance_fee_item_updated','finance','fee_item',item_id,{
                'name':name,'amount':amount,'class_ids':selected_class_ids})
            flash('Fee item updated. Existing assessments keep their historical amounts.','success')
            return redirect(url_for('admin_finance_fee_items'))

    current_form=({c.key:getattr(row,c.key) for c in row.__mapper__.column_attrs}
                  if request.method!='POST' else request.form)
    return render_template(
        'finance_fee_item_form.html',
        errors=errors,
        form=current_form,
        editing=row,
        classes=classes,
        selected_class_ids=selected_class_ids,
        fee_categories=FINANCE_FEE_CATEGORIES,
        fee_applicability=FINANCE_FEE_APPLICABILITY,
        # Older rows may carry a value from before the catalogue was fixed
        # (e.g. "Second and third term only"); keep it selectable so saving
        # the form again doesn't silently rewrite it to something else.
        legacy_category=current_form.get('category') if current_form.get('category') not in FINANCE_FEE_CATEGORIES else None,
        legacy_applicability=current_form.get('applicability') if current_form.get('applicability') not in FINANCE_FEE_APPLICABILITY else None)

@app.post('/admin/finance/fee-items/<int:item_id>/toggle')
@admin_required
@csrf_protect
def admin_finance_fee_item_toggle(item_id):
    row=obj(FinanceFeeItem,item_id)
    if not row: abort(404)
    new=0 if row.active else 1
    row.active=new; row.updated_at=datetime.now(timezone.utc).isoformat()
    row.updated_by=current_admin()['id']
    db.session.commit()
    audit_log('finance_fee_item_status_changed','finance','fee_item',item_id,{'active':new}); flash('Fee item '+('activated.' if new else 'archived.')+' Existing assessments remain unchanged.','success'); return redirect(url_for('admin_finance_fee_items'))

@app.get('/admin/finance/students/<int:student_id>/assessed-items.json')
@admin_required
def admin_finance_student_assessed_items(student_id):
    """Which fee items this student already has a live assessment for, by
    term, for the requested session — and whether that assessment has been
    fully paid.

    The Student Billing picker uses this to grey out (and label) a fee item
    the moment it can no longer be legally assessed for the chosen term,
    instead of letting staff pick it and only finding out from a flashed
    error after submitting.
    """
    try: session_id=int(request.args.get('session_id',''))
    except (TypeError,ValueError): return jsonify({})
    if not session_id: return jsonify({})
    account=_finance_student_outstanding(student_id,session_id)
    by_term={}
    for item in account:
        term=item['term'] or 'Full Session'
        by_term.setdefault(term,{})[str(item['fee_item_id'])] = {
            'paid': item['outstanding']<=0.000001,
            'category': item['category']}
    return jsonify(by_term)

@app.post('/admin/finance/assessments/new')
@admin_required
@csrf_protect
def admin_finance_assessment_new():
    'Create independent fee obligations after server-side class applicability validation.'
    me=current_admin()
    try:
        student_id=int((request.form.get('student_id') or '').strip())
        session_id=int((request.form.get('session_id') or '').strip())
    except (TypeError,ValueError):
        student_id=session_id=0

    ids=[]
    for raw in request.form.getlist('fee_item_id'):
        try:
            item_id=int(str(raw).strip())
            if item_id>0 and item_id not in ids:
                ids.append(item_id)
        except (TypeError,ValueError):
            pass

    term=request.form.get('term','Full Session').strip() or 'Full Session'
    if term not in ASSESSMENT_TERMS:
        flash('Select a valid billing period for the assessment.','error')
        return redirect(url_for('admin_finance_fee_items'))
    due_date=request.form.get('due_date','').strip() or None
    notes=request.form.get('notes','').strip()

    if not student_id:
        flash('Select a student before creating an assessment.','error')
        return redirect(url_for('admin_finance_fee_items'))
    if not session_id:
        flash('Select an academic session before creating an assessment.','error')
        return redirect(url_for('admin_finance_fee_items'))
    if not ids:
        flash('Select at least one fee item before creating an assessment.','error')
        return redirect(url_for('admin_finance_fee_items'))

    student=one(select(Student.id,Student.admission_no,Student.first_name,
                       Student.middle_name,Student.last_name)
                .where(Student.id==student_id,Student.active==1))
    if not student:
        flash('The selected student could not be found.','error')
        return redirect(url_for('admin_finance_fee_items'))

    session_row=db.session.scalars(select(AcademicSession).where(
        AcademicSession.id==session_id,AcademicSession.active==1)).first()
    if not session_row:
        flash('The selected academic session could not be found.','error')
        return redirect(url_for('admin_finance_fee_items'))

    enrolment=one(select(StudentEnrolment.class_id,SchoolClass.name.label('class_name'),
                         SchoolClass.stage.label('class_stage'))
                  .join(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
                  .where(StudentEnrolment.student_id==student_id,
                         StudentEnrolment.session_id==session_id,
                         StudentEnrolment.active==1,SchoolClass.active==1).limit(1))
    if not enrolment:
        flash('The selected student has no active class enrolment for the selected academic session.','error')
        return redirect(url_for('admin_finance_fee_items'))

    items=db.session.scalars(select(FinanceFeeItem)
        .where(FinanceFeeItem.id.in_(ids),FinanceFeeItem.active==1)
        .order_by(FinanceFeeItem.name)).all()
    if len(items)!=len(ids):
        flash('One or more selected fee items are no longer available.','error')
        return redirect(url_for('admin_finance_fee_items'))

    # A fee may only be charged to a class it is actually mapped to.
    allowed_ids={int(fid) for (fid,) in tuples(
        select(FinanceFeeItem.id)
        .join(FinanceFeeItemClass,and_(FinanceFeeItemClass.fee_item_id==FinanceFeeItem.id,
                                       FinanceFeeItemClass.active==1))
        .where(FinanceFeeItem.id.in_(ids),
               FinanceFeeItemClass.class_id==int(enrolment['class_id']),
               FinanceFeeItem.active==1))}
    rejected=[item.name for item in items if int(item.id) not in allowed_ids]
    if rejected:
        flash('Assessment blocked: these fee item(s) are not assigned to '
              + str(enrolment['class_name']) + ': ' + ', '.join(rejected),'error')
        return redirect(url_for('admin_finance_fee_items'))

    by_id={int(x.id):x for x in items}
    ordered=[by_id[i] for i in ids]
    # One live assessment per fee item, per student, per term.
    existing={fid for (fid,) in tuples(
        select(FinanceFeeAssessment.fee_item_id)
        .where(FinanceFeeAssessment.student_id==student_id,
               FinanceFeeAssessment.session_id==session_id,
               FinanceFeeAssessment.fee_item_id.in_(ids),
               FinanceFeeAssessment.term==term,
               FinanceFeeAssessment.active==1))}
    duplicates=[item.name for item in ordered if int(item.id) in existing]
    if duplicates:
        flash('Assessment not created. The following fee item(s) already exist for this student and term: '
              + ', '.join(duplicates),'error')
        return redirect(url_for('admin_finance_fee_items'))

    now=datetime.now(timezone.utc).isoformat()
    rows=[FinanceFeeAssessment(student_id=student_id,session_id=session_id,
                               category=item.category,amount=float(item.amount or 0),
                               due_date=due_date,notes=notes,created_by=me['id'],
                               created_at=now,active=1,fee_item_id=item.id,term=term)
          for item in ordered]
    db.session.add_all(rows)
    db.session.commit()
    created=[r.id for r in rows]
    total=sum(float(item.amount or 0) for item in ordered)

    audit_log('finance_assessment_created','finance','assessment',created[0] if created else 0,{
        'student_id':student_id,
        'session_id':session_id,
        'class_id':int(enrolment['class_id']),
        'class_name':enrolment['class_name'],
        'term':term,
        'assessment_count':len(created),
        'assessment_ids':created,
        'total_assessed':total})

    student_name=' '.join(x for x in (student['first_name'],student['middle_name'],student['last_name']) if x)
    flash(f'{len(created)} assessment(s) created for {student_name} — ₦{total:,.2f} total.','success')
    try:
        _notify_parents_fee_assessed(student_id,[item.name for item in ordered],total,term,
                                     session_row.name,me['id'])
    except Exception:
        app.logger.exception('Parent fee-assessed notification failed for student %s',student_id)
    return redirect(url_for('admin_finance_fee_items'))

@app.post('/admin/finance/payments/<int:payment_id>/void')
@admin_required
@csrf_protect
def admin_finance_payment_void(payment_id):
    me=current_admin()
    if not _finance_can_view_all(me): return admin_access_error('finance.manage')
    reason=request.form.get('reason','').strip()
    if not reason: flash('A reason is required to void a payment.','error'); return redirect(url_for('admin_finance_receipt',payment_id=payment_id))
    row=obj(FinancePayment,payment_id)
    if not row: abort(404)
    if row.status!='posted': flash('Only a posted payment can be voided.','error'); return redirect(url_for('admin_finance_receipt',payment_id=payment_id))
    receipt_no,amount=row.receipt_no,row.amount
    row.status='voided'; row.voided_at=datetime.now(timezone.utc).isoformat()
    row.voided_by=me['id']; row.void_reason=reason
    db.session.commit()
    audit_log('finance_payment_voided','finance','payment',payment_id,{'receipt_no':receipt_no,'amount':amount,'reason':reason},True,me)
    flash('Payment voided. The original receipt remains in the audit history.','success'); return redirect(url_for('admin_finance_receipt',payment_id=payment_id))
