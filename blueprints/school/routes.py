"""The school portal: students, classes, subjects, assignments, projects,
tests/practice/examinations, results (entry/verify/approve/release),
enrollment history, class promotion,
and academic sessions.
"""

import re
import secrets
from datetime import datetime, timezone

import sqlalchemy as sa
from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from sqlalchemy import and_, or_, func, select, delete as sa_delete, update as sa_update
from werkzeug.security import generate_password_hash

from app import (
    app, ACADEMIC_TERMS, CA_MAX_SCORE, EXAM_MAX_SCORE, NIGERIAN_STATES,
    StudentNumberAllocationError,
    _provision_student_account, _release_due_school_results,
    _resync_question_count, _school_current_session, _students_with_class,
    allocate_student_number,
)
from models import (
    Admin, AcademicPromotionItem, AcademicPromotionRun, AcademicSession,
    AssignmentQuestion, AssignmentStudent, ClassSubject, ParentAccount,
    ParentStudentLink, ProjectStudent, School, SchoolAssessment,
    SchoolAssignment, SchoolAssignmentAttempt, SchoolClass,
    SchoolClassProgression, SchoolPublicSetting, SchoolProject, SchoolQuestion,
    SchoolStudentResult, SchoolSubject, Student, StudentAdmissionContact,
    StudentAdmissionProfile, StudentEnrolment, StudentEnrollmentHistory,
    StudentNumberAllocation, ResultWorkflowEvent, PresenceSession, db,
)
from core.db_helpers import all_rows, group_concat, insert_stmt, obj, one, one_scalar, tuples, _flatten, _ignore_insert
from core.security import (
    admin_access_error, admin_has_permission, admin_required, admin_scope_allows,
    audit_log, current_admin, csrf_protect, is_school_admin,
)
from core.uploads import _save_image_upload
from core.notifications import _notify_guardians_of_school_work
from blueprints.school.report_card_data import class_term_stats, grade_for
from blueprints.school.result_notices import announce_ready_report_cards
from blueprints.school.results_records import results_return_url
from blueprints.parents.helpers import _new_parent_password
from blueprints.school.helpers import (
    ACADEMIC_HISTORY_LEVELS, PROMOTION_ACTIONS, PROMOTION_DECISIONS,
    _assignment_form_data, _assignment_students, _ca_weights, _finite, _result_term,
    _mark_latest_history_current, _notify_school_work, _promotion_audit,
    _promotion_classes, _promotion_current_enrolments,
    _promotion_existing_target_enrolment, _promotion_next_class,
    _promotion_progressions, _promotion_run_has_uncommitted,
    _promotion_target, _promotion_target_status, _promotion_validate_run,
    _result_with_context, _school_assessment_description,
    _school_assessment_list, _school_assessment_new, _school_assessment_title,
    _school_assessments, _school_class_allowed, _school_form_context,
    _school_pair_allowed, _school_sessions, _school_student_visible,
    _school_subject_allowed, _set_ca_weights, _student_term_periods,
    _student_term_subjects, _sync_enrolment_for_history, _term_subject_report,
    _work_with_class_subject, archive_students,
)


@app.route('/admin/school')
@admin_required
def admin_school_home():
    from blueprints.school.onboarding import onboarding_status  # deferred: it is imported after this module
    def count(model, *where):
        return one_scalar(select(func.count()).select_from(model).where(*where), 0)
    stats={
        'students':count(Student, Student.active==1),
        'classes':count(SchoolClass, SchoolClass.active==1),
        'subjects':count(SchoolSubject, SchoolSubject.active==1),
        'assignments':count(SchoolAssignment, SchoolAssignment.active==1),
        'projects':count(SchoolProject, SchoolProject.active==1),
        'tests':count(SchoolAssessment, SchoolAssessment.assessment_type=='test'),
        'examinations':count(SchoolAssessment, SchoolAssessment.assessment_type=='examination'),
    }
    # Top of the Class: the highest-scoring student per class this term, scoped the same way
    # every other class-facing page in this portal is (School Admin sees every class; a
    # class-scoped teacher only sees their own). Skipped entirely when there is nothing to show,
    # so a teacher with no classes (or a school with no current session) never pays for it.
    admin=current_admin()
    session_row=_school_current_session()
    term=request.args.get('term','').strip()
    term=term if term in ACADEMIC_TERMS else ACADEMIC_TERMS[0]
    top_of_class=[]
    pending_classes=[]
    if admin and session_row:
        visible_classes=[c for c in db.session.scalars(
            select(SchoolClass).where(SchoolClass.active==1).order_by(SchoolClass.level_order)).all()
            if _school_class_allowed(admin['id'],c.id)]
        for cls in visible_classes:
            class_stats=class_term_stats(cls.id,session_row.id,term)
            best_id,best_pct=None,None
            for sid,entry in class_stats['students'].items():
                pct=entry.get('percentage')
                if pct is None:
                    continue
                if best_pct is None or pct>best_pct:
                    best_id,best_pct=sid,pct
            if best_id is None:
                pending_classes.append(cls.name)
                continue
            student=db.session.get(Student,best_id)
            student_name=' '.join(p for p in (student.first_name,student.middle_name,student.last_name)
                                  if p and str(p).strip()) if student else ''
            letter,_remark=grade_for(best_pct)
            top_of_class.append({'class_id':cls.id,'class_name':cls.name,'student_name':student_name,
                                 'percentage':best_pct,'grade':letter})
        top_of_class.sort(key=lambda r:r['percentage'],reverse=True)
    return render_template('admin_school_home.html',stats=stats,onboarding=onboarding_status(),
                           top_of_class=top_of_class,top_of_class_pending=pending_classes,
                           top_of_class_term=term,
                           top_of_class_session=session_row.name if session_row else '')

@app.route('/admin/school/students')
@admin_required
def admin_school_students():
    selected_class=request.args.get('class','').strip()
    search=request.args.get('q','').strip()
    session_row=_school_current_session()
    # Keep Flask's global `session` object reserved for authentication/workspace
    # state.  Normalize database rows at the template boundary so presentation
    # code can safely use dictionary helpers such as `.get()`.
    school_session=dict(session_row) if session_row else None
    classes=db.session.scalars(select(SchoolClass).where(SchoolClass.active==1)
                               .order_by(SchoolClass.level_order)).all()
    rows=_students_with_class(school_session['id'] if school_session else 0,
                              include_session_id=True)
    admin=current_admin()
    if admin and not admin['admin_type_system']:
        classes=[r for r in classes if admin_scope_allows(admin['id'],'class',r['name'])]
        rows=[r for r in rows if r['class_name'] and admin_scope_allows(admin['id'],'class',r['class_name'])]
    class_cards=[]
    for cls in classes:
        class_rows=[r for r in rows if r['class_name']==cls['name']]
        class_cards.append({'name':cls['name'],'count':len(class_rows),'id':cls['id']})
    if selected_class:
        rows=[r for r in rows if r['class_name']==selected_class]
        if search:
            needle=search.casefold(); rows=[r for r in rows if needle in f"{r['first_name']} {r['middle_name'] or ''} {r['last_name']} {r['admission_no']} {r['guardian_name'] or ''} {r['guardian_email'] or ''}".casefold()]
    archived_count=one_scalar(select(func.count()).select_from(Student).where(Student.archived_at.isnot(None)),0)
    return render_template('school_students.html',students=rows,school_session=school_session,class_cards=class_cards,selected_class=selected_class,search=search,archived_count=archived_count,is_school_admin=is_school_admin(),can_archive=_can_archive_students())

def _student_enrolment_rows(sid):
    """Every class placement this student has, newest first, with the class and session names."""
    return [_flatten(r,'StudentEnrolment','class_name','session_name') for r in all_rows(
        select(StudentEnrolment,SchoolClass.name.label('class_name'),
               AcademicSession.name.label('session_name'))
        .join(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
        .join(AcademicSession,AcademicSession.id==StudentEnrolment.session_id)
        .where(StudentEnrolment.student_id==sid)
        .order_by(StudentEnrolment.id.desc()))]

def _student_class_history(sid):
    """The school journey: placements recorded by hand or imported, including those from before this school."""
    return [_flatten(r,'StudentEnrollmentHistory','session_name','class_name') for r in all_rows(
        select(StudentEnrollmentHistory,AcademicSession.name.label('session_name'),
               SchoolClass.name.label('class_name'))
        .outerjoin(AcademicSession,AcademicSession.id==StudentEnrollmentHistory.session_id)
        .outerjoin(SchoolClass,SchoolClass.id==StudentEnrollmentHistory.class_id)
        .where(StudentEnrollmentHistory.student_id==sid)
        .order_by(func.coalesce(StudentEnrollmentHistory.enrolled_at,'').desc(),
                  StudentEnrollmentHistory.id.desc()))]

@app.route('/admin/school/students/<int:sid>')
@admin_required
def admin_school_student_detail(sid):
    student=obj(Student,sid)
    if not student: abort(404)
    session_row=_school_current_session()
    enrol=_student_enrolment_rows(sid)
    current_enrol=next((r for r in enrol if session_row and r['session_id']==session_row['id'] and r['active']), enrol[0] if enrol else None)
    history=_student_class_history(sid)
    sessions=db.session.scalars(select(AcademicSession).where(AcademicSession.active==1)
        .order_by(AcademicSession.id.desc())).all()
    classes_for_history=db.session.scalars(select(SchoolClass).where(SchoolClass.active==1)
        .order_by(SchoolClass.level_order)).all()
    if current_enrol and not _school_class_allowed(current_admin()['id'],current_enrol['class_id']): return admin_access_error('school.students.view')
    results=[_flatten(r,'SchoolStudentResult','subject_name','assessment_title','class_name')
             for r in all_rows(
        select(SchoolStudentResult,SchoolSubject.name.label('subject_name'),
               SchoolAssessment.title.label('assessment_title'),
               SchoolClass.name.label('class_name'))
        .join(SchoolSubject,SchoolSubject.id==SchoolStudentResult.subject_id)
        .outerjoin(SchoolAssessment,SchoolAssessment.id==SchoolStudentResult.assessment_id)
        .outerjoin(StudentEnrolment,and_(StudentEnrolment.student_id==SchoolStudentResult.student_id,
                                         StudentEnrolment.session_id==SchoolStudentResult.session_id,
                                         StudentEnrolment.active==1))
        .outerjoin(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
        .where(SchoolStudentResult.student_id==sid)
        .order_by(SchoolStudentResult.id.desc()))]
    assignments=[_flatten(r,'SchoolAssignment','subject_name','class_name','assignment_status',
                          'assignment_score','assignment_max_score','assignment_remark')
                 for r in all_rows(
        select(SchoolAssignment,SchoolSubject.name.label('subject_name'),
               SchoolClass.name.label('class_name'),
               AssignmentStudent.status.label('assignment_status'),
               AssignmentStudent.score.label('assignment_score'),
               AssignmentStudent.max_score.label('assignment_max_score'),
               AssignmentStudent.remark.label('assignment_remark'))
        .select_from(AssignmentStudent)
        .join(SchoolAssignment,SchoolAssignment.id==AssignmentStudent.assignment_id)
        .join(SchoolSubject,SchoolSubject.id==SchoolAssignment.subject_id)
        .join(SchoolClass,SchoolClass.id==SchoolAssignment.class_id)
        .where(AssignmentStudent.student_id==sid)
        .order_by(SchoolAssignment.date_given.desc(),SchoolAssignment.id.desc()))]
    projects=[_flatten(r,'SchoolProject','subject_name','class_name','project_status',
                       'project_score','project_remark') for r in all_rows(
        select(SchoolProject,SchoolSubject.name.label('subject_name'),
               SchoolClass.name.label('class_name'),
               ProjectStudent.status.label('project_status'),
               ProjectStudent.score.label('project_score'),
               ProjectStudent.remark.label('project_remark'))
        .select_from(ProjectStudent)
        .join(SchoolProject,SchoolProject.id==ProjectStudent.project_id)
        .join(SchoolSubject,SchoolSubject.id==SchoolProject.subject_id)
        .join(SchoolClass,SchoolClass.id==SchoolProject.class_id)
        .where(ProjectStudent.student_id==sid)
        .order_by(SchoolProject.date_given.desc(),SchoolProject.id.desc()))]
    completed=[r for r in results if r['score'] is not None and r['max_score']]
    total_score=sum(float(r['score']) for r in completed); total_max=sum(float(r['max_score']) for r in completed); pct=(total_score/total_max*100) if total_max else 0
    assignment_graded=[r for r in assignments if r['assignment_score'] is not None and r['assignment_max_score']]
    assignment_avg=(sum(float(r['assignment_score'])/float(r['assignment_max_score'])*100 for r in assignment_graded)/len(assignment_graded)) if assignment_graded else None
    term_reports=[{**period,'subjects':_student_term_subjects(sid,period['session_id'],period['term'])}
                  for period in _student_term_periods(sid)]
    term_reports=[t for t in term_reports if t['subjects']]
    archived_by=_archived_by_name(student)
    return render_template('admin_school_student_detail.html',student=student,enrolments=enrol,enrollment_history=history,sessions=sessions,classes_for_history=classes_for_history,current_enrol=current_enrol,results=results,assignments=assignments,projects=projects,total_score=total_score,total_max=total_max,pct=pct,completed=len(completed),assignment_avg=assignment_avg,term_reports=term_reports,archived_by=archived_by,is_school_admin=is_school_admin(),can_archive=_can_archive_students())

@app.route('/admin/school/students/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_student_new():
    classes,subjects,students,session_row=_school_form_context()

    def back_with(errors):
        return render_template(
            'school_student_form.html',
            mode='new',
            student=request.form,
            classes=classes,
            errors=errors,
            nigerian_states=NIGERIAN_STATES,
            school_session=dict(session_row) if session_row else None
        )

    if request.method=='POST':
        first=request.form.get('first_name','').strip()
        middle=request.form.get('middle_name','').strip()
        last=request.form.get('last_name','').strip()
        gender=request.form.get('gender','').strip()
        dob=request.form.get('date_of_birth','').strip()
        state_of_origin=request.form.get('state_of_origin','').strip()
        blood_group=request.form.get('blood_group','').strip()
        genotype=request.form.get('genotype','').strip()
        guardian=request.form.get('guardian_name','').strip()
        phone=request.form.get('guardian_phone','').strip()
        email=request.form.get('guardian_email','').strip()
        photo_path=None

        try:
            class_id=int(request.form.get('class_id',''))
        except (TypeError,ValueError):
            class_id=0

        errors=[]

        if email and ('@' not in email or '.' not in email.split('@')[-1]):
            errors.append('Enter a valid guardian email address.')

        if state_of_origin not in NIGERIAN_STATES:
            errors.append('Select a valid state of origin.')

        try:
            photo_path=_save_image_upload(
                request.files.get('photo'),
                'students',
                f'student_{first}_{last}'.replace(' ','_').lower()
            )
        except ValueError as exc:
            errors.append(str(exc))

        if not first or not last:
            errors.append('First name and surname are required.')

        if not class_id or not _school_class_allowed(current_admin()['id'],class_id):
            errors.append('Select a class within your authorised school scope.')

        if errors:
            return back_with(errors)

        sid=None
        school_id=None
        login_username=None
        temporary_password=None
        generated_number=None

        # The whole registration is one transaction: identity allocation,
        # student record, account, enrolment, admission profile, parent
        # accounts and contacts either all commit or none do.
        try:
            active_schools=[row_id for (row_id,) in tuples(
                select(School.id).where(School.active==1).order_by(School.id))]

            if len(active_schools)!=1:
                raise StudentNumberAllocationError(
                    'Student registration requires exactly one active school configuration.'
                )

            school_id=int(active_schools[0])

            # Allocate first; the generator shares this transaction.
            allocation=allocate_student_number(
                school_id=school_id,
                student_id=None,
                allocation_year=None,
                allocated_by=str(current_admin()['id'])
            )

            generated_number=allocation['student_number']

            # The generated number is the authoritative student identity.
            # admission_no stays populated for legacy compatibility.
            student=Student(
                admission_no=generated_number,
                first_name=first,
                middle_name=middle,
                last_name=last,
                gender=gender,
                date_of_birth=dob,
                state_of_origin=state_of_origin,
                blood_group=blood_group,
                genotype=genotype,
                guardian_name=guardian,
                guardian_phone=phone,
                guardian_email=email,
                photo_path=photo_path,
                created_at=datetime.now(timezone.utc).isoformat(),
                active=1,
                school_id=school_id,
                student_number=generated_number,
                student_number_source='generated'
            )
            db.session.add(student)
            db.session.flush()
            sid=student.id

            # Bind the allocation ledger row to the new student record.
            linked=db.session.execute(sa_update(StudentNumberAllocation)
                .where(StudentNumberAllocation.school_id==school_id,
                       StudentNumberAllocation.student_number==generated_number,
                       StudentNumberAllocation.student_id.is_(None),
                       StudentNumberAllocation.source=='generated')
                .values(student_id=sid)).rowcount

            if linked!=1:
                raise StudentNumberAllocationError(
                    'Generated student number could not be safely linked to the student record.'
                )

            login_username,temporary_password=_provision_student_account(sid,generated_number)

            db.session.add(StudentEnrolment(
                student_id=sid,
                class_id=class_id,
                session_id=session_row['id'],
                enrolled_at=datetime.now(timezone.utc).isoformat(),
                active=1
            ))

            now_admission=datetime.now(timezone.utc).isoformat()
            admin_id=int(current_admin()['id'])

            def _admission_value(name):
                return request.form.get(name,'').strip()

            db.session.add(StudentAdmissionProfile(
                student_id=sid,
                previous_school=_admission_value('previous_school') or None,
                reason_for_leaving=_admission_value('reason_for_leaving') or None,
                religion=_admission_value('religion') or None,
                denomination=_admission_value('denomination') or None,
                blood_group=_admission_value('blood_group') or None,
                genotype=_admission_value('genotype') or None,
                convulsion_history=_admission_value('convulsion_history') or None,
                asthma_history=_admission_value('asthma_history') or None,
                medical_frequency=_admission_value('medical_frequency') or None,
                medical_treatment=_admission_value('medical_treatment') or None,
                immunization=_admission_value('immunization') or None,
                food_allergies=_admission_value('food_allergies') or None,
                drug_allergies=_admission_value('drug_allergies') or None,
                other_health_challenges=_admission_value('other_health_challenges') or None,
                disability=_admission_value('disability') or None,
                disability_indication=_admission_value('disability_indication') or None,
                parent_signature=_admission_value('parent_signature') or None,
                parent_signature_date=_admission_value('parent_signature_date') or None,
                updated_at=now_admission,
                updated_by=admin_id
            ))

            father_name=_admission_value('father_guardian_name')
            father_address=_admission_value('father_guardian_address')
            father_office=_admission_value('father_guardian_office_phone')
            father_mobile=_admission_value('father_guardian_mobile')
            father_email=_admission_value('father_guardian_email').lower()

            mother_name=_admission_value('mother_name')
            mother_address=_admission_value('mother_address')
            mother_office=_admission_value('mother_office_phone')
            mother_email=_admission_value('mother_email').lower()
            mother_occupation=_admission_value('mother_occupation')

            def _digits(value):
                return ''.join(ch for ch in str(value or '') if ch.isdigit())

            def _find_parent(mobile,email_value):
                """Match an existing parent conservatively.

                Normalised mobile first, then normalised email. A name alone is
                NEVER sufficient to reuse an account.
                """
                mobile_norm=_digits(mobile)
                email_norm=(email_value or '').strip().lower()
                if mobile_norm:
                    for row in db.session.scalars(select(ParentAccount)
                            .where(ParentAccount.phone.is_not(None))).all():
                        if _digits(row.phone)==mobile_norm:
                            return row
                if email_norm:
                    return db.session.scalars(select(ParentAccount)
                        .where(func.lower(ParentAccount.email)==email_norm)).first()
                return None

            def _ensure_parent(name,mobile,email_value,relationship):
                if not name and not mobile and not email_value:
                    return None
                parent=_find_parent(mobile,email_value)
                if parent is not None:
                    pid=int(parent.id)
                else:
                    display=name or 'Parent / Guardian'
                    base=re.sub(r'[^a-z0-9]+','.',display.lower()).strip('.') or 'parent'
                    username=base
                    suffix=1
                    while one_scalar(select(ParentAccount.id)
                                     .where(ParentAccount.username==username)):
                        username=f"{base}.{suffix}"
                        suffix+=1
                    password=_new_parent_password()
                    created=ParentAccount(
                        username=username,
                        display_name=display,
                        email=email_value or None,
                        phone=mobile or None,
                        password_hash=generate_password_hash(password),
                        active=1,
                        password_must_change=1,
                        created_at=now_admission
                    )
                    db.session.add(created)
                    db.session.flush()
                    pid=int(created.id)
                    audit_log(
                        'parent_account_created',
                        'school',
                        'parent',
                        pid,
                        {'created_from_student_admission':sid,'relationship':relationship}
                    )
                existing_link=db.session.scalars(select(ParentStudentLink).where(
                    ParentStudentLink.parent_id==pid,
                    ParentStudentLink.student_id==sid)).first()
                if existing_link:
                    existing_link.relationship=relationship or None
                    existing_link.active=1
                    existing_link.created_by=admin_id
                else:
                    db.session.add(ParentStudentLink(
                        parent_id=pid,student_id=sid,relationship=relationship or None,
                        active=1,created_at=now_admission,created_by=admin_id))
                return pid

            father_parent_id=_ensure_parent(
                father_name,father_mobile,father_email,'father_guardian')

            # The official admission form has no mother mobile field, so
            # email-only matching is permitted for the mother record.
            mother_parent_id=_ensure_parent(
                mother_name,'',mother_email,'mother')

            contacts_to_save=[
                ('father_guardian',father_parent_id,father_name,father_address,
                 father_office,father_mobile,father_email,None),
                ('mother',mother_parent_id,mother_name,mother_address,
                 mother_office,'',mother_email,mother_occupation)
            ]

            for contact_role,parent_id,contact_name,address,office_phone,mobile,contact_email,occupation in contacts_to_save:
                if not any([parent_id,contact_name,address,office_phone,mobile,contact_email,occupation]):
                    continue
                stmt=insert_stmt(StudentAdmissionContact).values(
                    student_id=sid,parent_id=parent_id,role=contact_role,
                    name=contact_name or None,address=address or None,
                    office_phone=office_phone or None,mobile=mobile or None,
                    email=contact_email or None,occupation=occupation or None,
                    created_at=now_admission,updated_at=now_admission,
                    updated_by=admin_id)
                db.session.execute(stmt.on_conflict_do_update(
                    index_elements=['student_id','role'],
                    set_={'parent_id':stmt.excluded.parent_id,
                          'name':stmt.excluded.name,
                          'address':stmt.excluded.address,
                          'office_phone':stmt.excluded.office_phone,
                          'mobile':stmt.excluded.mobile,
                          'email':stmt.excluded.email,
                          'occupation':stmt.excluded.occupation,
                          'updated_at':stmt.excluded.updated_at,
                          'updated_by':stmt.excluded.updated_by}))

            db.session.commit()

        except StudentNumberAllocationError as exc:
            db.session.rollback()
            return back_with([str(exc)])

        except sa.exc.IntegrityError:
            db.session.rollback()
            return back_with([
                'Student registration could not be completed because '
                'the generated student identity conflicted with existing data. '
                'No student record was created.'
            ])

        except Exception:
            db.session.rollback()
            raise

        audit_log(
            'school_student_created',
            'school',
            'student',
            sid,
            {
                'student_number':generated_number,
                'admission_no':generated_number,
                'school_id':school_id,
                'class_id':class_id,
                'account_created':True,
                'student_number_source':'generated'
            }
        )

        return render_template(
            'student_credentials.html',
            student={
                'id':sid,
                'first_name':first,
                'last_name':last,
                'admission_no':generated_number,
                'student_number':generated_number
            },
            login_username=login_username,
            temporary_password=temporary_password
        )

    return render_template(
        'school_student_form.html',
        mode='new',
        student=None,
        classes=classes,
        errors=[],
        nigerian_states=NIGERIAN_STATES,
        school_session=dict(session_row) if session_row else None
    )

@app.route('/admin/school/students/<int:sid>/edit',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_student_edit(sid):
    student=obj(Student,sid)
    if not student: abort(404)
    session_row=_school_current_session()
    enrol=db.session.scalars(select(StudentEnrolment).where(
        StudentEnrolment.student_id==sid,
        StudentEnrolment.session_id==(session_row['id'] if session_row else 0),
        StudentEnrolment.active==1)).first()
    if enrol and not _school_class_allowed(current_admin()['id'],enrol.class_id): return admin_access_error('school.students.edit')
    classes,_,_,_= _school_form_context()
    student_dict={c.key:getattr(student,c.key) for c in student.__mapper__.column_attrs}
    session_dict=dict(session_row) if session_row else None
    if request.method=='POST':
        admission=request.form.get('admission_no','').strip(); first=request.form.get('first_name','').strip(); middle=request.form.get('middle_name','').strip(); last=request.form.get('last_name','').strip(); gender=request.form.get('gender','').strip(); dob=request.form.get('date_of_birth','').strip(); blood_group=request.form.get('blood_group','').strip(); genotype=request.form.get('genotype','').strip(); guardian=request.form.get('guardian_name','').strip(); phone=request.form.get('guardian_phone','').strip(); email=request.form.get('guardian_email','').strip(); photo_path=student.photo_path
        try: class_id=int(request.form.get('class_id',''))
        except (TypeError,ValueError): class_id=0
        errors=[]
        if email and ('@' not in email or '.' not in email.split('@')[-1]): errors.append('Enter a valid guardian email address.')
        try:
            new_photo_path=_save_image_upload(request.files.get('photo'),'students',f'student_{first}_{last}'.replace(' ','_').lower())
            if new_photo_path: photo_path=new_photo_path
        except ValueError as exc:
            errors.append(str(exc))
        if not admission or not first or not last: errors.append('Admission number, first name and surname are required.')
        if not class_id or not _school_class_allowed(current_admin()['id'],class_id): errors.append('Select a class within your authorised school scope.')
        if errors: return render_template('school_student_form.html',mode='edit',student={**student_dict,**request.form},classes=classes,errors=errors,school_session=session_dict)
        try:
            student.admission_no=admission; student.first_name=first; student.middle_name=middle
            student.last_name=last; student.gender=gender; student.date_of_birth=dob
            student.blood_group=blood_group; student.genotype=genotype
            student.guardian_name=guardian; student.guardian_phone=phone
            student.guardian_email=email; student.photo_path=photo_path
            now=datetime.now(timezone.utc).isoformat()
            if enrol:
                enrol.class_id=class_id
            elif session_row:
                db.session.add(StudentEnrolment(student_id=sid,class_id=class_id,
                    session_id=session_row['id'],enrolled_at=now,active=1))
            db.session.commit()
        except sa.exc.IntegrityError:
            db.session.rollback()
            return render_template('school_student_form.html',mode='edit',student={**student_dict,**request.form},classes=classes,errors=['Admission number already exists.'],school_session=session_dict)
        audit_log('school_student_updated','school','student',sid,{'class_id':class_id}); flash('Student record updated.','success'); return redirect(url_for('admin_school_students'))
    data=dict(student_dict); data['class_id']=enrol.class_id if enrol else ''
    return render_template('school_student_form.html',mode='edit',student=data,classes=classes,errors=[],school_session=session_dict)

@app.route('/admin/school/students/<int:sid>/account',methods=['POST'])
@admin_required
@csrf_protect
def admin_school_student_account_reset(sid):
    if not _school_student_visible(sid): return admin_access_error('school.students.edit')
    student=obj(Student, sid)
    if not student: abort(404)
    login_username,temporary_password=_provision_student_account(sid,student['admission_no'])
    db.session.commit()
    audit_log('school_student_account_reset','school','student',sid,{'login_username':login_username})
    return render_template('student_credentials.html',student=dict(student),login_username=login_username,temporary_password=temporary_password,reset=True)

@app.post('/admin/school/students/<int:sid>/account/toggle')
@admin_required
@csrf_protect
def admin_school_student_account_toggle(sid):
    if not _school_student_visible(sid): return admin_access_error('school.students.edit')
    row=one(select(Student.id,Student.account_active).where(Student.id==sid))
    if not row: abort(404)
    new=0 if row['account_active'] else 1
    db.session.execute(sa_update(Student).where(Student.id==sid).values(account_active=new))
    db.session.commit()
    audit_log('school_student_account_status_changed','school','student',sid,{'account_active':new})
    flash('Student login account '+('activated.' if new else 'disabled.'),'success')
    return redirect(url_for('admin_school_student_detail',sid=sid))

@app.route('/admin/school/students/<int:sid>/account/print')
@admin_required
def admin_school_student_account_print(sid):
    if not _school_student_visible(sid): return admin_access_error('school.students.view')
    student=obj(Student, sid)
    if not student: abort(404)
    return render_template('student_credentials_print.html',student=student)

@app.post('/admin/school/students/<int:sid>/toggle')
@admin_required
@csrf_protect
def admin_school_student_toggle(sid):
    row=obj(Student, sid)
    if not row: abort(404)
    if not _school_student_visible(sid): return admin_access_error('school.students.delete')
    # An archived student is switched on only by restoring them, which also clears the archive
    # details; flipping ``active`` here would bring them back without that.
    if row['archived_at']:
        flash('This student is archived. Restore them from Archived students instead.','error')
        return redirect(url_for('admin_school_student_detail',sid=sid))
    new=0 if row['active'] else 1
    db.session.execute(sa_update(Student).where(Student.id==sid).values(active=new))
    db.session.commit(); audit_log('school_student_status_changed','school','student',sid,{'active':new}); flash('Student record '+('activated.' if new else 'deactivated.'),'success'); return redirect(url_for('admin_school_students'))

def _can_archive_students():
    me=current_admin()
    return bool(me and admin_has_permission(me['id'],'school.students.delete'))

def _archived_by_name(student):
    """Who archived this student, for display. None when the student is not archived or the admin is gone."""
    if not student['archived_by_admin_id']: return None
    return one_scalar(select(Admin.display_name).where(Admin.id==student['archived_by_admin_id']))

def _student_ids_from_form(values):
    ids=[]
    for value in values:
        try: ids.append(int(value))
        except (TypeError,ValueError): continue
    return sorted(set(ids))

@app.route('/admin/school/students/archived')
@admin_required
def admin_school_students_archived():
    # Archived students are a school-admin view only: they stay out of every other list in the portal.
    if not is_school_admin(): return admin_access_error('school.students.view')
    search=request.args.get('q','').strip()
    students=[_flatten(r,'Student','archived_by_name') for r in all_rows(
        select(Student,Admin.display_name.label('archived_by_name'))
        .outerjoin(Admin,Admin.id==Student.archived_by_admin_id)
        .where(Student.archived_at.isnot(None))
        .order_by(Student.archived_at.desc(),Student.id.desc()))]
    last_placement={}
    if students:
        for r in all_rows(
            select(StudentEnrolment.student_id,SchoolClass.name.label('class_name'),
                   AcademicSession.name.label('session_name'))
            .join(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
            .join(AcademicSession,AcademicSession.id==StudentEnrolment.session_id)
            .where(StudentEnrolment.student_id.in_([s['id'] for s in students]))
            .order_by(StudentEnrolment.id.desc())):
            last_placement.setdefault(r['student_id'],r)
    for s in students:
        placement=last_placement.get(s['id'])
        s['class_name']=placement['class_name'] if placement else None
        s['session_name']=placement['session_name'] if placement else None
    if search:
        needle=search.casefold()
        students=[s for s in students if needle in f"{s['first_name']} {s['middle_name'] or ''} {s['last_name']} {s['admission_no']} {s['guardian_name'] or ''} {s['class_name'] or ''}".casefold()]
    archived_total=one_scalar(select(func.count()).select_from(Student).where(Student.archived_at.isnot(None)),0)
    return render_template('admin_school_students_archived.html',students=students,search=search,
                           archived_total=archived_total,archived_count=archived_total,is_school_admin=True)

@app.route('/admin/school/students/archived/<int:sid>/record')
@admin_required
def admin_school_student_archived_record(sid):
    """The academic record of one archived student, loaded into the archived-students modal."""
    if not is_school_admin(): return admin_access_error('school.students.view')
    student=db.session.scalars(select(Student).where(Student.id==sid,Student.archived_at.isnot(None))).first()
    if not student: abort(404)
    term_reports=[{**period,'subjects':_student_term_subjects(sid,period['session_id'],period['term'])}
                  for period in _student_term_periods(sid)]
    term_reports=[t for t in term_reports if t['subjects']]
    return render_template('school_archived_student_record.html',student=student,
                           enrolments=_student_enrolment_rows(sid),enrollment_history=_student_class_history(sid),
                           term_reports=term_reports,archived_by=_archived_by_name(student))

@app.post('/admin/school/students/archive')
@admin_required
@csrf_protect
def admin_school_students_archive():
    """Archive one or many students. Each must be visible to the admin, and an already archived student is left as it is."""
    ids=_student_ids_from_form(request.form.getlist('student_ids'))
    reason=request.form.get('reason','').strip()[:300] or None
    class_name=request.form.get('class','').strip()
    back=url_for('admin_school_students',**({'class':class_name} if class_name else {}))
    if not ids:
        flash('Select at least one student to archive.','error'); return redirect(back)
    visible=[sid for sid in ids if _school_student_visible(sid)]
    skipped=len(ids)-len(visible)
    if not visible:
        return admin_access_error('school.students.delete')
    me=current_admin()
    to_archive=archive_students(visible,reason,me['id'])
    if to_archive:
        db.session.commit()
        for sid in to_archive:
            audit_log('school_student_archived','school','student',sid,{'reason':reason})
    archived=len(to_archive); already=len(visible)-archived
    parts=[f'{archived} student{"" if archived==1 else "s"} archived.' if archived else 'Nothing was archived.']
    if already: parts.append(f'{already} already archived.')
    if skipped: parts.append(f'{skipped} outside your class scope were left alone.')
    flash(' '.join(parts),'success' if archived else 'error')
    return redirect(back)

@app.post('/admin/school/students/archived/restore')
@admin_required
@csrf_protect
def admin_school_students_restore():
    """Bring archived students back into the current register, with their login working again."""
    if not is_school_admin(): return admin_access_error('school.students.delete')
    ids=_student_ids_from_form(request.form.getlist('student_ids'))
    if not ids:
        flash('Select at least one archived student to restore.','error'); return redirect(url_for('admin_school_students_archived'))
    restored=db.session.scalars(select(Student).where(
        Student.id.in_(ids),Student.archived_at.isnot(None))).all()
    restored_ids=[s.id for s in restored]
    if restored_ids:
        db.session.execute(sa_update(Student).where(Student.id.in_(restored_ids)).values(
            active=1,archived_at=None,archived_by_admin_id=None,archive_reason=None))
        db.session.commit()
        for sid in restored_ids:
            audit_log('school_student_restored','school','student',sid,{})
    count=len(restored_ids)
    flash(f'{count} student{"" if count==1 else "s"} restored to the current register.' if count else 'No archived students were selected.',
          'success' if count else 'error')
    return redirect(url_for('admin_school_students_archived'))

@app.route('/admin/school/classes')
@admin_required
def admin_school_classes():
    rows=db.session.scalars(select(SchoolClass)
        .order_by(SchoolClass.level_order)).all()
    counts={class_id:n for class_id,n in tuples(
        select(StudentEnrolment.class_id,func.count().label('n'))
        .join(Student,Student.id==StudentEnrolment.student_id)
        .where(StudentEnrolment.active==1,Student.active==1)
        .group_by(StudentEnrolment.class_id))}
    admin=current_admin()
    if admin and not admin['admin_type_system']:
        rows=[r for r in rows if admin_scope_allows(admin['id'],'class',r.name)]
    return render_template('school_classes.html',classes=rows,counts=counts)

@app.route('/admin/school/classes/<int:class_id>/edit',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_class_edit(class_id):
    if not is_school_admin(): return admin_access_error('School Admin control')
    row=obj(SchoolClass,class_id)
    if not row: abort(404)
    errors=[]
    if request.method=='POST':
        name=request.form.get('name','').strip(); stage=request.form.get('stage','').strip()
        optional=1 if request.form.get('optional')=='1' else 0
        try: level_order=int(request.form.get('level_order','0'))
        except (TypeError,ValueError): level_order=0
        if not name: errors.append('Class name is required.')
        if not stage: errors.append('Stage/category is required.')
        if not errors:
            duplicate=one_scalar(select(SchoolClass.id).where(
                SchoolClass.name==name,SchoolClass.id!=class_id))
            if duplicate: errors.append('Another class already uses that name.')
            if not errors:
                old={'name':row.name,'stage':row.stage,'level_order':row.level_order}
                row.name=name; row.stage=stage; row.level_order=level_order; row.optional=optional
                db.session.commit()
                audit_log('school_class_updated','school','class',class_id,{'old':old,'new':{'name':name,'stage':stage,'level_order':level_order}})
                flash('Class details updated. Existing historical enrolments keep their class record; new entries use the updated class name.','success')
                return redirect(url_for('admin_school_classes'))
    form={c.key:getattr(row,c.key) for c in row.__mapper__.column_attrs}
    if request.method=='POST': form.update(request.form)
    return render_template('school_class_form.html',class_row=form,errors=errors)

@app.post('/admin/school/classes/<int:class_id>/toggle')
@admin_required
@csrf_protect
def admin_school_class_toggle(class_id):
    if not is_school_admin(): return admin_access_error('School Admin control')
    row=obj(SchoolClass, class_id)
    if not row: abort(404)
    new=0 if row['active'] else 1
    db.session.execute(sa_update(SchoolClass).where(SchoolClass.id==class_id).values(active=new))
    db.session.commit(); audit_log('school_class_status_changed','school','class',class_id,{'active':new}); flash(f'{row["name"]} '+('activated.' if new else 'deactivated.'),'success'); return redirect(url_for('admin_school_classes'))

@app.route('/admin/school/subjects')
@admin_required
def admin_school_subjects():
    rows=all_rows(
        select(SchoolSubject.id,SchoolSubject.name,SchoolSubject.code,SchoolSubject.active,
               group_concat(SchoolClass.name,', ').label('classes'),
               func.sum(sa.case((ClassSubject.locked==1,1),else_=0)).label('locked_count'),
               func.sum(sa.case((ClassSubject.final_locked==1,1),else_=0)).label('final_locked_count'))
        .select_from(SchoolSubject)
        .outerjoin(ClassSubject,ClassSubject.subject_id==SchoolSubject.id)
        .outerjoin(SchoolClass,SchoolClass.id==ClassSubject.class_id)
        .where(SchoolSubject.active==1)
        .group_by(SchoolSubject.id).order_by(SchoolSubject.name))
    classes=db.session.scalars(select(SchoolClass).where(SchoolClass.active==1)
        .order_by(SchoolClass.level_order)).all()
    admin=current_admin()
    if admin and not admin['admin_type_system']:
        rows=[r for r in rows if any(admin_scope_allows(admin['id'],'class',x.strip())
                                     for x in (r['classes'] or '').split(','))]
        classes=[c for c in classes if admin_scope_allows(admin['id'],'class',c.name)]
    return render_template('school_subjects.html',subjects=rows,classes=classes)

@app.route('/admin/school/subjects/quick-create',methods=['POST'])
@admin_required
@csrf_protect
def admin_school_subject_quick_create():
    me=current_admin(); name=request.form.get('name','').strip(); code=request.form.get('code','').strip()
    try: class_id=int(request.form.get('class_id',''))
    except (TypeError,ValueError): class_id=0
    if not name or not class_id or not _school_class_allowed(me['id'],class_id):
        return jsonify(ok=False,error='Enter a subject name and choose an authorised class.'),400
    now=datetime.now(timezone.utc).isoformat()
    subject=db.session.scalars(select(SchoolSubject)
        .where(func.lower(SchoolSubject.name)==name.lower()).limit(1)).first()
    if subject:
        link=db.session.scalars(select(ClassSubject).where(
            ClassSubject.subject_id==subject.id,ClassSubject.class_id==class_id)).first()
        if link and link.final_locked:
            return jsonify(ok=False,error='That subject is permanently locked for this class.'),409
        if link:
            return jsonify(ok=False,error='That subject already exists for the selected class.'),409
        # The subject exists but is not attached to this class: reactivate and link.
        subject.active=1
        db.session.add(ClassSubject(class_id=class_id,subject_id=subject.id,locked=0,
                                    final_locked=0,created_by=me['id'],created_at=now))
        sid=subject.id; created=False
        resolved_name=subject.name
    else:
        new_subject=SchoolSubject(name=name,code=code,created_at=now,active=1)
        db.session.add(new_subject); db.session.flush()
        sid=new_subject.id
        db.session.add(ClassSubject(class_id=class_id,subject_id=sid,locked=0,
                                    final_locked=0,created_by=me['id'],created_at=now))
        created=True
        resolved_name=name
    db.session.commit()
    audit_log('school_subject_created','school','subject',sid,{'class_id':class_id,'quick_create':True,'new_subject':created})
    return jsonify(ok=True,subject_id=sid,name=resolved_name,created=created)

@app.route('/admin/school/subjects/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_subject_new():
    classes,_,_,_= _school_form_context()
    if request.method=='POST':
        name=request.form.get('name','').strip(); code=request.form.get('code','').strip(); selected=[]
        for v in request.form.getlist('class_ids'):
            try: selected.append(int(v))
            except (TypeError,ValueError): pass
        errors=[]
        if not name: errors.append('Subject name is required.')
        if not selected: errors.append('Select at least one class.')
        if any(not _school_class_allowed(current_admin()['id'],cid) for cid in selected): errors.append('One or more selected classes are outside your authorised scope.')
        if errors: return render_template('school_subject_form.html',mode='new',subject=request.form,classes=classes,selected=selected,errors=errors)
        now=datetime.now(timezone.utc).isoformat()
        existing=db.session.scalars(select(SchoolSubject)
            .where(func.lower(SchoolSubject.name)==name.lower()).limit(1)).first()
        try:
            if existing:
                sid=existing.id
                existing.active=1
                if code: existing.code=code
                for cid in selected:
                    link=one(select(ClassSubject.locked,ClassSubject.final_locked)
                             .where(ClassSubject.class_id==cid,ClassSubject.subject_id==sid))
                    if link and link['final_locked']: raise ValueError('That subject is permanently locked for one of the selected classes.')
                    if link: raise ValueError('That subject already exists for one of the selected classes.')
                    db.session.add(ClassSubject(class_id=cid,subject_id=sid,locked=0,final_locked=0,
                                                created_by=current_admin()['id'],created_at=now))
            else:
                created=SchoolSubject(name=name,code=code,created_at=now,active=1)
                db.session.add(created); db.session.flush(); sid=created.id
                for cid in selected:
                    db.session.add(ClassSubject(class_id=cid,subject_id=sid,locked=0,final_locked=0,
                                                created_by=current_admin()['id'],created_at=now))
            db.session.commit()
        except (ValueError,sa.exc.IntegrityError) as exc:
            db.session.rollback()
            msg=str(exc) if isinstance(exc,ValueError) else 'The subject could not be created because the name is already in use.'
            return render_template('school_subject_form.html',mode='new',subject=request.form,classes=classes,selected=selected,errors=[msg])
        audit_log('school_subject_created','school','subject',sid,{'classes':selected,'existing_subject_reused':bool(existing)}); flash('Subject created/attached to the selected classes.','success'); return redirect(url_for('admin_school_subjects'))
    return render_template('school_subject_form.html',mode='new',subject=None,classes=classes,selected=[],errors=[])

@app.route('/admin/school/subjects/<int:subject_id>/edit',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_subject_edit(subject_id):
    subject=obj(SchoolSubject,subject_id)
    links=all_rows(select(ClassSubject.class_id,ClassSubject.locked,ClassSubject.final_locked,
                          ClassSubject.final_locked_by,ClassSubject.final_locked_at)
                   .where(ClassSubject.subject_id==subject_id))
    if not subject: abort(404)
    if not is_school_admin() and not any(_school_class_allowed(current_admin()['id'],x['class_id']) for x in links): return admin_access_error('school.subjects.edit')
    classes,_,_,_= _school_form_context(); selected=[x['class_id'] for x in links]; has_final=any(x['final_locked'] for x in links)
    subject_dict={c.key:getattr(subject,c.key) for c in subject.__mapper__.column_attrs}
    locked_map={r['class_id']:bool(r['locked']) for r in links}
    final_map={r['class_id']:bool(r['final_locked']) for r in links}
    if request.method=='POST':
        if has_final:
            flash('This subject has been permanently locked by School Admin and can no longer be edited.','error'); return redirect(url_for('admin_school_subjects'))
        if any(x['locked'] for x in links):
            flash('Unlock the affected class-subject connection before editing this subject.','error'); return redirect(url_for('admin_school_subject_edit',subject_id=subject_id))
        name=request.form.get('name','').strip(); code=request.form.get('code','').strip(); new_selected=[]
        for v in request.form.getlist('class_ids'):
            try: new_selected.append(int(v))
            except (TypeError,ValueError): pass
        errors=[]
        if not name: errors.append('Subject name is required.')
        if not new_selected: errors.append('Select at least one class.')
        if any(not _school_class_allowed(current_admin()['id'],cid) for cid in new_selected): errors.append('One or more selected classes are outside your authorised scope.')
        locked_class_ids={x['class_id'] for x in links if x['locked']}
        if locked_class_ids and not is_school_admin() and not locked_class_ids.issubset(set(new_selected)): errors.append('A locked class-subject connection cannot be removed by an ordinary administrator.')
        if errors: return render_template('school_subject_form.html',mode='edit',subject={**subject_dict,**request.form},classes=classes,selected=new_selected or selected,locked_map=locked_map,final_map=final_map,errors=errors)
        try:
            existing={x['class_id']:x for x in links}
            duplicate=one_scalar(select(SchoolSubject.id).where(
                func.lower(SchoolSubject.name)==name.lower(),
                SchoolSubject.id!=subject_id).limit(1))
            if duplicate: raise ValueError('Another subject with that name already exists. Use that subject and attach it to the required class instead.')
            subject.name=name; subject.code=code
            now=datetime.now(timezone.utc).isoformat()
            for cid in new_selected:
                if cid not in existing:
                    db.session.add(ClassSubject(class_id=cid,subject_id=subject_id,locked=0,
                                                final_locked=0,created_by=current_admin()['id'],
                                                created_at=now))
            for cid,row in existing.items():
                # A locked link is only removable by a School Admin; a finally
                # locked one is never removable.
                if cid not in new_selected and (not row['locked'] or is_school_admin()) and not row['final_locked']:
                    db.session.execute(sa_delete(ClassSubject).where(
                        ClassSubject.class_id==cid,ClassSubject.subject_id==subject_id))
            db.session.commit()
        except (ValueError,sa.exc.IntegrityError) as exc:
            db.session.rollback()
            return render_template('school_subject_form.html',mode='edit',subject={**subject_dict,**request.form},classes=classes,selected=new_selected,locked_map=locked_map,final_map=final_map,errors=[str(exc) if isinstance(exc,ValueError) else 'The subject could not be saved because that name is already in use.'])
        audit_log('school_subject_updated','school','subject',subject_id,{'classes':new_selected}); flash('Subject updated.','success'); return redirect(url_for('admin_school_subjects'))
    return render_template('school_subject_form.html',mode='edit',subject=subject,classes=classes,selected=selected,locked_map=locked_map,final_map=final_map,errors=[])

@app.post('/admin/school/subjects/<int:subject_id>/delete')
@admin_required
@csrf_protect
def admin_school_subject_delete(subject_id):
    subject=obj(SchoolSubject,subject_id)
    links=all_rows(select(ClassSubject.locked,ClassSubject.final_locked)
                   .where(ClassSubject.subject_id==subject_id))
    if not subject: abort(404)
    if any(x['final_locked'] for x in links): flash('This subject has been permanently locked by School Admin and cannot be deleted.','error'); return redirect(url_for('admin_school_subjects'))
    if any(x['locked'] for x in links) and not is_school_admin(): flash('This subject has a locked class assignment. An authorized administrator must resolve the lock before it can be removed.','error'); return redirect(url_for('admin_school_subjects'))
    if not is_school_admin() and not _school_subject_allowed(current_admin()['id'],subject_id): return admin_access_error('school.subjects.delete')
    subject.active=0
    db.session.commit()
    audit_log('school_subject_deactivated','school','subject',subject_id); flash('Subject deactivated.','success'); return redirect(url_for('admin_school_subjects'))

@app.post('/admin/school/subjects/<int:subject_id>/lock/<int:class_id>')
@admin_required
@csrf_protect
def admin_school_subject_lock(subject_id,class_id):
    me=current_admin()
    if not _school_class_allowed(me['id'],class_id): return admin_access_error('school.subjects.lock')
    row=db.session.scalars(select(ClassSubject).where(
        ClassSubject.subject_id==subject_id,ClassSubject.class_id==class_id)).first()
    if not row: abort(404)
    if row.final_locked:
        flash('This class-subject connection is permanently locked by School Admin.','error'); return redirect(url_for('admin_school_subject_edit',subject_id=subject_id))
    new=0 if row.locked else 1
    row.locked=new
    db.session.commit()
    audit_log('school_subject_lock_changed','school','class_subject',f'{class_id}:{subject_id}',{'locked':new}); flash('Class subject '+('locked.' if new else 'unlocked.'),'success'); return redirect(url_for('admin_school_subject_edit',subject_id=subject_id))

@app.post('/admin/school/subjects/<int:subject_id>/final-lock/<int:class_id>')
@admin_required
@csrf_protect
def admin_school_subject_final_lock(subject_id,class_id):
    me=current_admin()
    if not is_school_admin(me): return admin_access_error('School Admin control')
    row=db.session.scalars(select(ClassSubject).where(
        ClassSubject.subject_id==subject_id,ClassSubject.class_id==class_id)).first()
    if not row: abort(404)
    if row.final_locked:
        flash('This subject is already permanently locked.','error'); return redirect(url_for('admin_school_subject_edit',subject_id=subject_id))
    row.locked=1; row.final_locked=1; row.final_locked_by=me['id']
    row.final_locked_at=datetime.now(timezone.utc).isoformat()
    db.session.commit()
    audit_log('school_subject_final_locked','school','class_subject',f'{class_id}:{subject_id}',{'final_locked':True},True,me); flash('The class-subject connection is now permanently locked.','success'); return redirect(url_for('admin_school_subject_edit',subject_id=subject_id))

@app.route('/admin/school/assignments')
@admin_required
def admin_school_assignments():
    selected_class=request.args.get('class','').strip(); search=request.args.get('q','').strip()
    question_count=(select(func.count()).select_from(AssignmentQuestion)
                    .where(AssignmentQuestion.assignment_id==SchoolAssignment.id)
                    .correlate(SchoolAssignment).scalar_subquery())
    rows=[_flatten(r,'SchoolAssignment','class_name','subject_name','student_count','question_count')
          for r in all_rows(
        select(SchoolAssignment,SchoolClass.name.label('class_name'),
               SchoolSubject.name.label('subject_name'),
               func.count(AssignmentStudent.student_id).label('student_count'),
               question_count.label('question_count'))
        .join(SchoolClass,SchoolClass.id==SchoolAssignment.class_id)
        .join(SchoolSubject,SchoolSubject.id==SchoolAssignment.subject_id)
        .outerjoin(AssignmentStudent,AssignmentStudent.assignment_id==SchoolAssignment.id)
        .where(SchoolAssignment.active==1)
        .group_by(SchoolAssignment.id,SchoolClass.id,SchoolSubject.id)
        .order_by(SchoolAssignment.id.desc()))]
    classes=db.session.scalars(select(SchoolClass).where(SchoolClass.active==1)
        .order_by(SchoolClass.level_order)).all()
    admin=current_admin()
    if admin and not admin['admin_type_system']:
        classes=[c for c in classes if admin_scope_allows(admin['id'],'class',c.name)]
        rows=[r for r in rows if _school_pair_allowed(admin['id'],r['class_id'],r['subject_id'])]
    cards=[{'name':c.name,'count':sum(1 for r in rows if r['class_name']==c.name),'id':c.id} for c in classes]
    if selected_class: rows=[r for r in rows if r['class_name']==selected_class]
    if search:
        needle=search.casefold(); rows=[r for r in rows if needle in f"{r['title']} {r['class_name']} {r['subject_name']}".casefold()]
    return render_template('school_assignments.html',assignments=rows,class_cards=cards,selected_class=selected_class,search=search)

@app.route('/admin/school/assignments/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_assignment_new():
    me=current_admin(); classes,subjects,students=_assignment_form_data(me['id'])
    sessions=_school_sessions()
    if request.method=='POST':
        title=request.form.get('title','').strip(); instructions=request.form.get('instructions','').strip(); due=request.form.get('due_date','').strip(); date_given=request.form.get('date_given','').strip() or datetime.now(timezone.utc).date().isoformat()
        assignment_type=request.form.get('assignment_type','written').strip().lower(); timing_mode=request.form.get('timing_mode','untimed').strip().lower()
        term=request.form.get('term','').strip() or None
        try: cid=int(request.form.get('class_id','')); sid=int(request.form.get('subject_id','')); session_id=int(request.form.get('session_id') or 0); time_limit=int(request.form.get('time_limit_minutes','0') or 0)*60; per_q=int(request.form.get('per_question_seconds','0') or 0); max_score=_finite(float(request.form.get('max_score','0') or 0))
        except (TypeError,ValueError): cid=sid=session_id=time_limit=per_q=0; max_score=0
        student_ids=[int(v) for v in request.form.getlist('student_ids') if v.isdigit()]; errors=[]
        if not title: errors.append('Assignment title is required.')
        if assignment_type not in ('written','quiz'): errors.append('Choose a valid assignment type.')
        if not _school_pair_allowed(me['id'],cid,sid): errors.append('Select a class and subject within your authorised scope.')
        if not session_id or not one_scalar(select(AcademicSession.id).where(AcademicSession.id==session_id)): errors.append('Select a valid academic session.')
        if term not in ACADEMIC_TERMS: errors.append('Select a valid term.')
        if assignment_type=='quiz' and timing_mode=='overall' and time_limit<=0: errors.append('Enter an overall time limit for this timed assignment.')
        if assignment_type=='quiz' and timing_mode=='per_question' and per_q<=0: errors.append('Enter the time allowed for each question.')
        if timing_mode not in ('untimed','overall','per_question'): errors.append('Choose a valid timing mode.')
        allowed={r['id'] for r in students if r['class_id']==cid}
        if not student_ids: errors.append('Select at least one student.')
        if any(x not in allowed for x in student_ids): errors.append('One or more selected students are outside the selected class.')
        if max_score<0: errors.append('Maximum score cannot be negative.')
        if errors:
            return render_template('school_assignment_form.html',mode='new',assignment=request.form,classes=classes,subjects=subjects,students=students,selected_students=student_ids,sessions=sessions,terms=ACADEMIC_TERMS,errors=errors)
        now=datetime.now(timezone.utc).isoformat()
        assignment=SchoolAssignment(title=title,instructions=instructions,class_id=cid,
            subject_id=sid,due_date=due,created_by=me['id'],created_at=now,active=1,
            assignment_type=assignment_type,date_given=date_given,timing_mode=timing_mode,
            time_limit_seconds=time_limit or None,per_question_seconds=per_q or None,
            max_score=max_score or None,session_id=session_id,term=term)
        db.session.add(assignment); db.session.flush(); aid=assignment.id
        for stid in student_ids:
            db.session.add(AssignmentStudent(assignment_id=aid,student_id=stid,
                                             status='undone',max_score=max_score or None))
        _notify_school_work(student_ids,'assignment','New assignment',f'{title} has been assigned. Due: {due or "No due date"}.',url_for('student_assignment_detail',assignment_id=aid),me['id'])
        db.session.commit()
        _notify_guardians_of_school_work(student_ids,'assignment',title,due)
        audit_log('school_assignment_created','school','assignment',aid,{'class_id':cid,'subject_id':sid,'students':student_ids,'assignment_type':assignment_type,'session_id':session_id,'term':term}); flash('Assignment created. Add questions if this is a CBT-style assignment.','success'); return redirect(url_for('admin_school_assignment_detail',assignment_id=aid))
    return render_template('school_assignment_form.html',mode='new',assignment=None,classes=classes,subjects=subjects,students=students,selected_students=[],sessions=sessions,terms=ACADEMIC_TERMS,errors=[])

@app.route('/admin/school/assignments/<int:assignment_id>')
@admin_required
def admin_school_assignment_detail(assignment_id):
    a=_work_with_class_subject(SchoolAssignment,assignment_id)
    if not a: abort(404)
    if not _school_pair_allowed(current_admin()['id'],a['class_id'],a['subject_id']): return admin_access_error('school.assignments.view')
    questions=db.session.scalars(select(AssignmentQuestion)
        .where(AssignmentQuestion.assignment_id==assignment_id)
        .order_by(AssignmentQuestion.sort_order,AssignmentQuestion.id)).all()
    assigned=_assignment_students(assignment_id)
    return render_template('school_assignment_detail.html',assignment=a,questions=questions,assigned=assigned)

@app.post('/admin/school/assignments/<int:assignment_id>/questions')
@admin_required
@csrf_protect
def admin_school_assignment_question_new(assignment_id):
    me=current_admin(); a=obj(SchoolAssignment,assignment_id)
    if not a: abort(404)
    if a.assignment_type!='quiz' or not _school_pair_allowed(me['id'],a.class_id,a.subject_id): return admin_access_error('school.assignments.edit')
    q=request.form.get('question_text','').strip(); options=[request.form.get(k,'').strip() for k in ('option_a','option_b','option_c','option_d')]
    try: correct=int(request.form.get('correct_option','0')); points=float(request.form.get('points','1') or 1)
    except (TypeError,ValueError): correct=0; points=1
    if not q or any(not x for x in options) or correct not in range(4) or points<=0:
        flash('Enter a question, all four options, a correct option and positive points.','error'); return redirect(url_for('admin_school_assignment_detail',assignment_id=assignment_id))
    image_path=None
    try: image_path=_save_image_upload(request.files.get('image'),'assignments',f'assignment_{assignment_id}_{secrets.token_hex(4)}')
    except ValueError as exc: flash(str(exc),'error'); return redirect(url_for('admin_school_assignment_detail',assignment_id=assignment_id))
    sort=one_scalar(select(func.coalesce(func.max(AssignmentQuestion.sort_order),0)+1)
                    .where(AssignmentQuestion.assignment_id==assignment_id), 1)
    db.session.add(AssignmentQuestion(assignment_id=assignment_id,question_text=q,
        instruction=request.form.get('instruction','').strip() or None,image_path=image_path,
        option_a=options[0],option_b=options[1],option_c=options[2],option_d=options[3],
        correct_option=correct,points=points,sort_order=sort))
    db.session.flush()
    total=one_scalar(select(func.coalesce(func.sum(AssignmentQuestion.points),0))
                     .where(AssignmentQuestion.assignment_id==assignment_id), 0)
    a.max_score=total
    # Students who have already started keep the maximum their attempt was built on.
    started=select(SchoolAssignmentAttempt.student_id).where(
        SchoolAssignmentAttempt.assignment_id==assignment_id).scalar_subquery()
    db.session.execute(sa_update(AssignmentStudent)
        .where(AssignmentStudent.assignment_id==assignment_id,
               AssignmentStudent.student_id.not_in(started))
        .values(max_score=total))
    db.session.commit()
    audit_log('school_assignment_question_added','school','assignment',assignment_id,{'points':points}); flash('Question added.','success'); return redirect(url_for('admin_school_assignment_detail',assignment_id=assignment_id))

@app.post('/admin/school/assignments/<int:assignment_id>/students/<int:student_id>')
@admin_required
@csrf_protect
def admin_school_assignment_student_update(assignment_id,student_id):
    me=current_admin(); status=request.form.get('status','undone').strip().lower(); remark=request.form.get('remark','').strip()
    try: score=_finite(float(request.form.get('score',''))) if request.form.get('score','').strip() else None
    except (TypeError,ValueError):
        flash('Enter the score as an ordinary number.','error'); return redirect(url_for('admin_school_assignment_detail',assignment_id=assignment_id))
    if status not in ('done','undone','in_progress','overdue'): status='undone'
    a=obj(SchoolAssignment,assignment_id)
    row=db.session.scalars(select(AssignmentStudent).where(
        AssignmentStudent.assignment_id==assignment_id,
        AssignmentStudent.student_id==student_id)).first()
    if not a or not row: abort(404)
    if not _school_pair_allowed(me['id'],a.class_id,a.subject_id): return admin_access_error('school.assignments.edit')
    max_score=a.max_score or row.max_score
    if score is not None and (score<0 or (max_score is not None and score>max_score)):
        flash('Enter a score within the assignment maximum.','error'); return redirect(url_for('admin_school_assignment_detail',assignment_id=assignment_id))
    row.status=status; row.score=score; row.remark=remark or None
    row.graded_at=datetime.now(timezone.utc).isoformat(); row.graded_by=me['id']
    db.session.commit()
    audit_log('school_assignment_student_updated','school','assignment',assignment_id,{'student_id':student_id,'status':status,'score':score}); return redirect(url_for('admin_school_assignment_detail',assignment_id=assignment_id))

@app.route('/admin/school/assignments/<int:assignment_id>/edit',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_assignment_edit(assignment_id):
    a=obj(SchoolAssignment,assignment_id)
    if not a: abort(404)
    selected=[sid for (sid,) in tuples(select(AssignmentStudent.student_id)
        .where(AssignmentStudent.assignment_id==assignment_id))]
    if not _school_pair_allowed(current_admin()['id'],a.class_id,a.subject_id): return admin_access_error('school.assignments.edit')
    classes,subjects,students=_assignment_form_data(current_admin()['id'])
    sessions=_school_sessions()
    if request.method=='POST':
        title=request.form.get('title','').strip(); instructions=request.form.get('instructions','').strip(); due=request.form.get('due_date','').strip(); date_given=request.form.get('date_given','').strip() or a.date_given; assignment_type=request.form.get('assignment_type',a.assignment_type).strip().lower(); timing_mode=request.form.get('timing_mode',a.timing_mode).strip().lower()
        term=request.form.get('term','').strip() or None
        try: cid=int(request.form.get('class_id','')); sid=int(request.form.get('subject_id','')); session_id=int(request.form.get('session_id') or 0); time_limit=int(request.form.get('time_limit_minutes','0') or 0)*60; per_q=int(request.form.get('per_question_seconds','0') or 0); max_score=_finite(float(request.form.get('max_score','0') or 0))
        except (TypeError,ValueError): cid=sid=session_id=time_limit=per_q=0; max_score=0
        student_ids=[int(v) for v in request.form.getlist('student_ids') if v.isdigit()]; errors=[]
        if not title: errors.append('Assignment title is required.')
        if not _school_pair_allowed(current_admin()['id'],cid,sid): errors.append('Select a valid class and subject.')
        if not session_id or not one_scalar(select(AcademicSession.id).where(AcademicSession.id==session_id)): errors.append('Select a valid academic session.')
        if term not in ACADEMIC_TERMS: errors.append('Select a valid term.')
        if assignment_type=='quiz' and timing_mode=='overall' and time_limit<=0: errors.append('Enter an overall time limit.')
        if assignment_type=='quiz' and timing_mode=='per_question' and per_q<=0: errors.append('Enter a per-question time limit.')
        if not student_ids: errors.append('Select at least one student.')
        if errors:
            form={c.key:getattr(a,c.key) for c in a.__mapper__.column_attrs}
            return render_template('school_assignment_form.html',mode='edit',assignment={**form,**request.form},classes=classes,subjects=subjects,students=students,selected_students=student_ids or selected,sessions=sessions,terms=ACADEMIC_TERMS,errors=errors)
        a.title=title; a.instructions=instructions; a.class_id=cid; a.subject_id=sid
        a.due_date=due; a.assignment_type=assignment_type; a.date_given=date_given
        a.timing_mode=timing_mode; a.time_limit_seconds=time_limit or None
        a.per_question_seconds=per_q or None; a.session_id=session_id; a.term=term
        effective_max=max_score or a.max_score
        a.max_score=effective_max
        old=set(selected); new=set(student_ids)
        for stid in new-old:
            _ignore_insert(AssignmentStudent, [{'assignment_id':assignment_id,'student_id':stid,
                                                'status':'undone','max_score':effective_max}])
        # A student who has already started the CBT attempt keeps their record.
        started=select(SchoolAssignmentAttempt.student_id).where(
            SchoolAssignmentAttempt.assignment_id==assignment_id).scalar_subquery()
        for stid in old-new:
            db.session.execute(sa_delete(AssignmentStudent).where(
                AssignmentStudent.assignment_id==assignment_id,
                AssignmentStudent.student_id==stid,
                AssignmentStudent.student_id.not_in(started)))
        db.session.commit()
        audit_log('school_assignment_updated','school','assignment',assignment_id,{'class_id':cid,'subject_id':sid}); flash('Assignment updated.','success'); return redirect(url_for('admin_school_assignment_detail',assignment_id=assignment_id))
    return render_template('school_assignment_form.html',mode='edit',assignment=a,classes=classes,subjects=subjects,students=students,selected_students=selected,sessions=sessions,terms=ACADEMIC_TERMS,errors=[])

@app.post('/admin/school/assignments/<int:assignment_id>/delete')
@admin_required
@csrf_protect
def admin_school_assignment_delete(assignment_id):
    a=obj(SchoolAssignment,assignment_id)
    if not a: abort(404)
    if not _school_pair_allowed(current_admin()['id'],a.class_id,a.subject_id): return admin_access_error('school.assignments.delete')
    a.active=0
    db.session.commit()
    audit_log('school_assignment_deleted','school','assignment',assignment_id); flash('Assignment removed.','success'); return redirect(url_for('admin_school_assignments'))

@app.route('/admin/school/projects')
@admin_required
def admin_school_projects():
    selected_class=request.args.get('class','').strip(); search=request.args.get('q','').strip()
    rows=[_flatten(r,'SchoolProject','class_name','subject_name','student_count')
          for r in all_rows(
        select(SchoolProject,SchoolClass.name.label('class_name'),
               SchoolSubject.name.label('subject_name'),
               func.count(ProjectStudent.student_id).label('student_count'))
        .join(SchoolClass,SchoolClass.id==SchoolProject.class_id)
        .join(SchoolSubject,SchoolSubject.id==SchoolProject.subject_id)
        .outerjoin(ProjectStudent,ProjectStudent.project_id==SchoolProject.id)
        .where(SchoolProject.active==1)
        .group_by(SchoolProject.id,SchoolClass.id,SchoolSubject.id)
        .order_by(SchoolProject.id.desc()))]
    classes=db.session.scalars(select(SchoolClass).where(SchoolClass.active==1)
        .order_by(SchoolClass.level_order)).all()
    me=current_admin()
    if not me['admin_type_system']:
        classes=[c for c in classes if _school_class_allowed(me['id'],c.id)]
        rows=[r for r in rows if _school_pair_allowed(me['id'],r['class_id'],r['subject_id'])]
    cards=[{'name':c.name,'count':sum(1 for r in rows if r['class_name']==c.name),'id':c.id} for c in classes]
    if selected_class: rows=[r for r in rows if r['class_name']==selected_class]
    if search:
        n=search.casefold(); rows=[r for r in rows if n in f"{r['title']} {r['class_name']} {r['subject_name']}".casefold()]
    return render_template('school_projects.html',projects=rows,class_cards=cards,selected_class=selected_class,search=search)

@app.route('/admin/school/projects/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_project_new():
    me=current_admin(); classes,subjects,students=_assignment_form_data(me['id'])
    sessions=_school_sessions()
    if request.method=='POST':
        title=request.form.get('title','').strip(); instructions=request.form.get('instructions','').strip(); date_given=request.form.get('date_given','').strip() or datetime.now(timezone.utc).date().isoformat(); due=request.form.get('due_date','').strip()
        term=request.form.get('term','').strip() or None
        try: cid=int(request.form.get('class_id','')); sid=int(request.form.get('subject_id','')); session_id=int(request.form.get('session_id') or 0); max_score=_finite(float(request.form.get('max_score','0') or 0))
        except (TypeError,ValueError): cid=sid=session_id=0; max_score=0
        student_ids=[int(v) for v in request.form.getlist('student_ids') if v.isdigit()]; errors=[]
        if not title: errors.append('Project title is required.')
        if not _school_pair_allowed(me['id'],cid,sid): errors.append('Select a class and subject within your authorised scope.')
        if not session_id or not one_scalar(select(AcademicSession.id).where(AcademicSession.id==session_id)): errors.append('Select a valid academic session.')
        if term not in ACADEMIC_TERMS: errors.append('Select a valid term.')
        if not student_ids: errors.append('Select at least one student.')
        allowed={r['id'] for r in students if r['class_id']==cid}
        if any(x not in allowed for x in student_ids): errors.append('One or more selected students are outside the selected class.')
        if max_score<0: errors.append('Maximum score cannot be negative.')
        if errors: return render_template('school_project_form.html',mode='new',project=request.form,classes=classes,subjects=subjects,students=students,selected_students=student_ids,sessions=sessions,terms=ACADEMIC_TERMS,errors=errors)
        now=datetime.now(timezone.utc).isoformat()
        project=SchoolProject(title=title,instructions=instructions,class_id=cid,subject_id=sid,
            date_given=date_given,due_date=due,max_score=max_score or None,
            created_by=me['id'],created_at=now,active=1,session_id=session_id,term=term)
        db.session.add(project); db.session.flush(); pid=project.id
        for stid in student_ids:
            db.session.add(ProjectStudent(project_id=pid,student_id=stid,status='not_done'))
        _notify_school_work(student_ids,'project','New project',f'{title} has been assigned. Required: {due or "No due date"}.',url_for('student_dashboard')+'#projects',me['id'])
        db.session.commit()
        _notify_guardians_of_school_work(student_ids,'project',title,due)
        audit_log('school_project_created','school','project',pid,{'class_id':cid,'subject_id':sid,'students':student_ids,'session_id':session_id,'term':term}); flash('Project created and assigned.','success'); return redirect(url_for('admin_school_project_detail',project_id=pid))
    return render_template('school_project_form.html',mode='new',project=None,classes=classes,subjects=subjects,students=students,selected_students=[],sessions=sessions,terms=ACADEMIC_TERMS,errors=[])

@app.route('/admin/school/projects/<int:project_id>')
@admin_required
def admin_school_project_detail(project_id):
    p=_work_with_class_subject(SchoolProject,project_id)
    if not p: abort(404)
    if not _school_pair_allowed(current_admin()['id'],p['class_id'],p['subject_id']): return admin_access_error('school.projects.view')
    assigned=[_flatten(r,'ProjectStudent','first_name','middle_name','last_name','admission_no')
              for r in all_rows(
        select(ProjectStudent,Student.first_name,Student.middle_name,
               Student.last_name,Student.admission_no)
        .join(Student,Student.id==ProjectStudent.student_id)
        .where(ProjectStudent.project_id==project_id,Student.active==1)
        .order_by(Student.last_name,Student.first_name))]
    return render_template('school_project_detail.html',project=p,assigned=assigned)

@app.post('/admin/school/projects/<int:project_id>/students/<int:student_id>')
@admin_required
@csrf_protect
def admin_school_project_student_update(project_id,student_id):
    me=current_admin(); status=request.form.get('status','not_done'); remark=request.form.get('remark','').strip()
    try: score=_finite(float(request.form.get('score',''))) if request.form.get('score','').strip() else None
    except (TypeError,ValueError):
        flash('Enter the score as an ordinary number.','error'); return redirect(url_for('admin_school_project_detail',project_id=project_id))
    if status not in ('done','not_done'): status='not_done'
    p=obj(SchoolProject,project_id)
    row=db.session.scalars(select(ProjectStudent).where(
        ProjectStudent.project_id==project_id,
        ProjectStudent.student_id==student_id)).first()
    if not p or not row: abort(404)
    if not _school_pair_allowed(me['id'],p.class_id,p.subject_id): return admin_access_error('school.projects.edit')
    if score is not None and (score<0 or (p.max_score is not None and score>p.max_score)):
        flash('Enter a score within the project maximum.','error'); return redirect(url_for('admin_school_project_detail',project_id=project_id))
    row.status=status; row.score=score; row.remark=remark or None
    row.graded_by=me['id']; row.graded_at=datetime.now(timezone.utc).isoformat()
    db.session.commit()
    audit_log('school_project_student_updated','school','project',project_id,{'student_id':student_id,'status':status,'score':score}); return redirect(url_for('admin_school_project_detail',project_id=project_id))

@app.route('/admin/school/projects/<int:project_id>/edit',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_project_edit(project_id):
    me=current_admin(); p=obj(SchoolProject,project_id)
    if not p: abort(404)
    selected=[sid for (sid,) in tuples(select(ProjectStudent.student_id)
        .where(ProjectStudent.project_id==project_id))]
    if not _school_pair_allowed(me['id'],p.class_id,p.subject_id): return admin_access_error('school.projects.edit')
    classes,subjects,students=_assignment_form_data(me['id'])
    sessions=_school_sessions()
    if request.method=='POST':
        title=request.form.get('title','').strip(); instructions=request.form.get('instructions','').strip(); date_given=request.form.get('date_given','').strip() or p.date_given; due=request.form.get('due_date','').strip()
        term=request.form.get('term','').strip() or None
        try: cid=int(request.form.get('class_id','')); sid=int(request.form.get('subject_id','')); session_id=int(request.form.get('session_id') or 0); max_score=_finite(float(request.form.get('max_score','0') or 0))
        except (TypeError,ValueError): cid=sid=session_id=0; max_score=0
        student_ids=[int(v) for v in request.form.getlist('student_ids') if v.isdigit()]; errors=[]
        if not title: errors.append('Project title is required.')
        if not _school_pair_allowed(me['id'],cid,sid): errors.append('Select a valid class and subject.')
        if not session_id or not one_scalar(select(AcademicSession.id).where(AcademicSession.id==session_id)): errors.append('Select a valid academic session.')
        if term not in ACADEMIC_TERMS: errors.append('Select a valid term.')
        if not student_ids: errors.append('Select at least one student.')
        if errors:
            form={c.key:getattr(p,c.key) for c in p.__mapper__.column_attrs}
            return render_template('school_project_form.html',mode='edit',project={**form,**request.form},classes=classes,subjects=subjects,students=students,selected_students=student_ids or selected,sessions=sessions,terms=ACADEMIC_TERMS,errors=errors)
        p.title=title; p.instructions=instructions; p.class_id=cid; p.subject_id=sid
        p.date_given=date_given; p.due_date=due; p.max_score=max_score or p.max_score
        p.session_id=session_id; p.term=term
        old=set(selected); new=set(student_ids)
        for stid in new-old:
            _ignore_insert(ProjectStudent, [{'project_id':project_id,'student_id':stid,
                                             'status':'not_done'}])
        for stid in old-new:
            db.session.execute(sa_delete(ProjectStudent).where(
                ProjectStudent.project_id==project_id,ProjectStudent.student_id==stid))
        db.session.commit()
        audit_log('school_project_updated','school','project',project_id,{'class_id':cid,'subject_id':sid}); flash('Project updated.','success'); return redirect(url_for('admin_school_project_detail',project_id=project_id))
    return render_template('school_project_form.html',mode='edit',project=p,classes=classes,subjects=subjects,students=students,selected_students=selected,sessions=sessions,terms=ACADEMIC_TERMS,errors=[])

@app.post('/admin/school/projects/<int:project_id>/delete')
@admin_required
@csrf_protect
def admin_school_project_delete(project_id):
    me=current_admin(); p=obj(SchoolProject,project_id)
    if not p: abort(404)
    if not _school_pair_allowed(me['id'],p.class_id,p.subject_id): return admin_access_error('school.projects.delete')
    p.active=0
    db.session.commit()
    audit_log('school_project_deleted','school','project',project_id); flash('Project removed.','success'); return redirect(url_for('admin_school_projects'))

@app.route('/admin/school/tests')
@admin_required
def admin_school_tests(): return _school_assessment_list('test')

@app.route('/admin/school/practice-tests')
@admin_required
def admin_school_practice_tests(): return _school_assessment_list('practice')

@app.route('/admin/school/examinations')
@admin_required
def admin_school_examinations(): return _school_assessment_list('examination')

@app.route('/admin/school/tests/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_test_new(): return _school_assessment_new('test')

@app.route('/admin/school/practice-tests/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_practice_new(): return _school_assessment_new('practice')

@app.route('/admin/school/examinations/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_examination_new(): return _school_assessment_new('examination')

@app.route('/admin/school/assessments/<int:assessment_id>')
@admin_required
def admin_school_assessment_detail(assessment_id):
    a=_flatten(one(select(SchoolAssessment,SchoolClass.name.label('class_name'),
                          SchoolSubject.name.label('subject_name'),
                          AcademicSession.name.label('session_name'))
        .join(SchoolClass,SchoolClass.id==SchoolAssessment.class_id)
        .join(SchoolSubject,SchoolSubject.id==SchoolAssessment.subject_id)
        .outerjoin(AcademicSession,AcademicSession.id==SchoolAssessment.session_id)
        .where(SchoolAssessment.id==assessment_id)),
        'SchoolAssessment','class_name','subject_name','session_name')
    questions=db.session.scalars(select(SchoolQuestion)
        .where(SchoolQuestion.assessment_id==assessment_id)
        .order_by(SchoolQuestion.sort_order,SchoolQuestion.id)).all()
    if not a: abort(404)
    perm='school.practice.edit' if a['assessment_type']=='practice' else ('school.examinations.edit' if a['assessment_type']=='examination' else 'school.tests.edit')
    if not admin_has_permission(current_admin()['id'],perm): return admin_access_error(perm)
    if not _school_pair_allowed(current_admin()['id'],a['class_id'],a['subject_id']): return admin_access_error(perm)
    return render_template('school_assessment_detail.html',assessment=a,questions=questions,sessions=_school_sessions(),terms=ACADEMIC_TERMS)

@app.post('/admin/school/assessments/<int:assessment_id>/edit')
@admin_required
@csrf_protect
def admin_school_assessment_edit(assessment_id):
    a=obj(SchoolAssessment, assessment_id)
    if not a: abort(404)
    perm='school.practice.edit' if a['assessment_type']=='practice' else ('school.examinations.edit' if a['assessment_type']=='examination' else 'school.tests.edit')
    if not admin_has_permission(current_admin()['id'],perm): return admin_access_error(perm)
    if not _school_pair_allowed(current_admin()['id'],a['class_id'],a['subject_id']): return admin_access_error(perm)
    title=request.form.get('title','').strip(); instructions=request.form.get('instructions','').strip()
    term=request.form.get('term','').strip() or None
    try: cid=int(request.form.get('class_id','')); sid=int(request.form.get('subject_id','')); duration=max(1,int(request.form.get('duration_minutes','30') or 30)); session_id=int(request.form.get('session_id'))
    except: cid=sid=session_id=0; duration=30
    if not title or not _school_pair_allowed(current_admin()['id'],cid,sid): flash('Enter a valid title, class and subject within your scope.','error'); return redirect(url_for('admin_school_assessment_detail',assessment_id=assessment_id))
    if a['assessment_type']!='practice' and term not in ACADEMIC_TERMS:
        flash('Select a valid term.','error'); return redirect(url_for('admin_school_assessment_detail',assessment_id=assessment_id))
    if term and term not in ACADEMIC_TERMS:
        flash('Select a valid term.','error'); return redirect(url_for('admin_school_assessment_detail',assessment_id=assessment_id))
    db.session.execute(sa_update(SchoolAssessment).where(SchoolAssessment.id==assessment_id)
        .values(title=title,instructions=instructions,class_id=cid,subject_id=sid,
                session_id=session_id,duration_minutes=duration,term=term or 'Full Session'))
    db.session.commit(); audit_log('school_assessment_updated','school',a['assessment_type'],assessment_id,{'class_id':cid,'subject_id':sid,'term':term}); flash('Assessment updated.','success'); return redirect(url_for('admin_school_assessment_detail',assessment_id=assessment_id))

@app.post('/admin/school/assessments/<int:assessment_id>/toggle')
@admin_required
@csrf_protect
def admin_school_assessment_toggle(assessment_id):
    a=obj(SchoolAssessment, assessment_id)
    if not a: abort(404)
    perm='school.practice.edit' if a['assessment_type']=='practice' else ('school.examinations.edit' if a['assessment_type']=='examination' else 'school.tests.edit')
    if not admin_has_permission(current_admin()['id'],perm): return admin_access_error(perm)
    if not _school_pair_allowed(current_admin()['id'],a['class_id'],a['subject_id']): return admin_access_error(perm)
    new=0 if a['active'] else 1
    db.session.execute(sa_update(SchoolAssessment).where(SchoolAssessment.id==assessment_id)
                       .values(active=new))
    db.session.commit(); audit_log('school_assessment_status_changed','school',a['assessment_type'],assessment_id,{'active':new}); flash('Assessment '+('activated.' if new else 'closed.'),'success'); return redirect(url_for('admin_school_assessment_detail',assessment_id=assessment_id))

@app.post('/admin/school/assessments/<int:assessment_id>/questions/new')
@admin_required
@csrf_protect
def admin_school_assessment_question_new(assessment_id):
    a=obj(SchoolAssessment, assessment_id)
    if not a: abort(404)
    perm='school.practice.edit' if a['assessment_type']=='practice' else ('school.examinations.edit' if a['assessment_type']=='examination' else 'school.tests.edit')
    if not admin_has_permission(current_admin()['id'],perm): return admin_access_error(perm)
    if not _school_pair_allowed(current_admin()['id'],a['class_id'],a['subject_id']): return admin_access_error(perm)
    text=request.form.get('question_text','').strip(); instruction=request.form.get('instruction','').strip(); opts=[request.form.get(f'option_{x}','').strip() for x in 'abcd']
    try: correct=int(request.form.get('correct_option','-1')); points=max(1,int(request.form.get('points','1') or 1))
    except: correct=-1; points=1
    try:
        image_path=_save_image_upload(request.files.get('image'),'questions',f'school_{assessment_id}')
    except ValueError as exc:
        flash(str(exc),'error'); return redirect(url_for('admin_school_assessment_detail',assessment_id=assessment_id))
    if not text or any(not x for x in opts) or correct not in range(4): flash('Question text, four options and a correct answer are required.','error'); return redirect(url_for('admin_school_assessment_detail',assessment_id=assessment_id))
    n=one_scalar(select(func.coalesce(func.max(SchoolQuestion.sort_order),0)+1)
                 .where(SchoolQuestion.assessment_id==assessment_id),1)
    db.session.add(SchoolQuestion(assessment_id=assessment_id,question_text=text,
        instruction=instruction,image_path=image_path,option_a=opts[0],option_b=opts[1],
        option_c=opts[2],option_d=opts[3],correct_option=correct,points=points,sort_order=n))
    db.session.flush()
    _resync_question_count(assessment_id)
    db.session.commit(); audit_log('school_assessment_question_added','school',a['assessment_type'],assessment_id,{'question_number':n}); flash('Question added.','success'); return redirect(url_for('admin_school_assessment_detail',assessment_id=assessment_id))

@app.route('/admin/school/assessments/<int:assessment_id>/questions/<int:question_id>/edit',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_assessment_question_edit(assessment_id,question_id):
    a=obj(SchoolAssessment, assessment_id)
    q=db.session.scalars(select(SchoolQuestion).where(
        SchoolQuestion.id==question_id,
        SchoolQuestion.assessment_id==assessment_id)).first()
    if not a or not q: abort(404)
    perm='school.practice.edit' if a['assessment_type']=='practice' else ('school.examinations.edit' if a['assessment_type']=='examination' else 'school.tests.edit')
    if not admin_has_permission(current_admin()['id'],perm): return admin_access_error(perm)
    if not _school_pair_allowed(current_admin()['id'],a['class_id'],a['subject_id']): return admin_access_error(perm)

    if request.method=='GET':
        return render_template('school_question_form.html',assessment=a,question=q,errors=[])

    text=request.form.get('question_text','').strip()
    instruction=request.form.get('instruction','').strip()
    opts=[request.form.get(f'option_{x}','').strip() for x in 'abcd']
    try:
        correct=int(request.form.get('correct_option','-1'))
        points=max(1,int(request.form.get('points','1') or 1))
    except (TypeError,ValueError):
        correct=-1; points=1
    errors=[]
    image_path=q['image_path']
    if request.form.get('remove_image')=='1':
        image_path=None
    try:
        uploaded=_save_image_upload(request.files.get('image'),'questions',f'school_{assessment_id}')
        if uploaded: image_path=uploaded
    except ValueError as exc:
        errors.append(str(exc))
    if not text: errors.append('Question text is required.')
    if any(not x for x in opts): errors.append('All four options are required.')
    if correct not in range(4): errors.append('Select the correct option.')
    if points<1: errors.append('Points must be at least 1.')
    if errors:
        return render_template('school_question_form.html',assessment=a,question={
            'id':question_id,'question_text':text,'instruction':instruction,'image_path':image_path,
            'option_a':opts[0],'option_b':opts[1],'option_c':opts[2],'option_d':opts[3],
            'correct_option':correct,'points':points
        },errors=errors)

    db.session.execute(sa_update(SchoolQuestion)
        .where(SchoolQuestion.id==question_id,SchoolQuestion.assessment_id==assessment_id)
        .values(question_text=text,instruction=instruction,image_path=image_path,
                option_a=opts[0],option_b=opts[1],option_c=opts[2],option_d=opts[3],
                correct_option=correct,points=points))
    db.session.commit()
    audit_log('school_assessment_question_updated','school',a['assessment_type'],assessment_id,{'question_id':question_id})
    flash(f'Question {question_id} updated.','success')
    return redirect(url_for('admin_school_assessment_detail',assessment_id=assessment_id))

@app.post('/admin/school/assessments/<int:assessment_id>/questions/reorder')
@admin_required
@csrf_protect
def admin_school_assessment_question_reorder(assessment_id):
    a=obj(SchoolAssessment, assessment_id)
    questions=all_rows(select(SchoolQuestion.id)
        .where(SchoolQuestion.assessment_id==assessment_id)
        .order_by(SchoolQuestion.sort_order,SchoolQuestion.id))
    if not a: abort(404)
    perm='school.practice.edit' if a['assessment_type']=='practice' else ('school.examinations.edit' if a['assessment_type']=='examination' else 'school.tests.edit')
    if not admin_has_permission(current_admin()['id'],perm): return admin_access_error(perm)
    if not _school_pair_allowed(current_admin()['id'],a['class_id'],a['subject_id']): return admin_access_error(perm)
    try:
        ids=[int(x) for x in request.form.get('order','').split(',') if x.strip()]
    except (TypeError,ValueError):
        ids=[]
    current_ids=[int(q['id']) for q in questions]
    if set(ids) != set(current_ids) or len(ids) != len(current_ids):
        return jsonify(ok=False,error='Order must contain every question exactly once.'),400
    for position,qid in enumerate(ids,1):
        db.session.execute(sa_update(SchoolQuestion)
            .where(SchoolQuestion.id==qid,SchoolQuestion.assessment_id==assessment_id)
            .values(sort_order=position))
    db.session.commit()
    audit_log('school_assessment_questions_reordered','school',a['assessment_type'],assessment_id,{})
    return jsonify(ok=True)

@app.post('/admin/school/assessments/<int:assessment_id>/questions/<int:question_id>/delete')
@admin_required
@csrf_protect
def admin_school_assessment_question_delete(assessment_id,question_id):
    a=obj(SchoolAssessment, assessment_id)
    q=one(select(SchoolQuestion.id).where(SchoolQuestion.id==question_id,
                                          SchoolQuestion.assessment_id==assessment_id))
    if not a or not q: abort(404)
    perm='school.practice.edit' if a['assessment_type']=='practice' else ('school.examinations.edit' if a['assessment_type']=='examination' else 'school.tests.edit')
    if not admin_has_permission(current_admin()['id'],perm): return admin_access_error(perm)
    if not _school_pair_allowed(current_admin()['id'],a['class_id'],a['subject_id']): return admin_access_error(perm)
    db.session.execute(sa_delete(SchoolQuestion).where(SchoolQuestion.id==question_id))
    db.session.flush()
    _resync_question_count(assessment_id)
    db.session.commit(); audit_log('school_assessment_question_deleted','school',a['assessment_type'],assessment_id,{'question_id':question_id}); flash('Question deleted.','success'); return redirect(url_for('admin_school_assessment_detail',assessment_id=assessment_id))

@app.post('/admin/school/tests/<int:assessment_id>/delete')
@app.post('/admin/school/practice-tests/<int:assessment_id>/delete')
@app.post('/admin/school/examinations/<int:assessment_id>/delete')
@admin_required
@csrf_protect
def admin_school_assessment_delete(assessment_id):
    a=obj(SchoolAssessment,assessment_id)
    if not a: abort(404)
    perm='school.practice.delete' if a.assessment_type=='practice' else ('school.examinations.delete' if a.assessment_type=='examination' else 'school.tests.delete')
    if not admin_has_permission(current_admin()['id'],perm): return admin_access_error(perm)
    if not _school_pair_allowed(current_admin()['id'],a.class_id,a.subject_id): return admin_access_error(perm)
    assessment_type=a.assessment_type
    # Marks already recorded for it belong to the students' academic record; the database would refuse
    # the delete (they point at this row), so say why instead of failing.
    if one_scalar(select(func.count()).select_from(SchoolStudentResult).where(SchoolStudentResult.assessment_id==assessment_id),0):
        flash(f'This {assessment_type} already has student results recorded, so it cannot be deleted. Close it instead.','error')
        return redirect(url_for('admin_school_assessment_detail',assessment_id=assessment_id))
    db.session.delete(a)
    db.session.commit()
    audit_log('school_assessment_deleted','school',assessment_type,assessment_id); flash('Assessment deleted.','success'); return redirect(url_for('admin_school_home'))

@app.route('/admin/school/results/manual/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_result_manual_new():
    me=current_admin()
    sessions=db.session.scalars(select(AcademicSession).where(AcademicSession.active==1)
        .order_by(AcademicSession.id.desc())).all()
    classes=db.session.scalars(select(SchoolClass).where(SchoolClass.active==1)
        .order_by(SchoolClass.level_order)).all()
    if not me['admin_type_system']: classes=[c for c in classes if _school_class_allowed(me['id'],c.id)]
    current=_school_current_session(); session_id=request.values.get('session_id',type=int) or (current['id'] if current else 0); term=_result_term(request.values.get('term','')) or 'Full Session'; class_id=request.values.get('class_id',type=int) or 0; student_id=request.values.get('student_id',type=int) or 0; subject_id=request.values.get('subject_id',type=int) or 0
    subjects=all_rows(select(SchoolSubject.id,SchoolSubject.name,SchoolSubject.code,
                             group_concat(ClassSubject.class_id).label('class_ids'))
        .join(ClassSubject,ClassSubject.subject_id==SchoolSubject.id)
        .where(SchoolSubject.active==1)
        .group_by(SchoolSubject.id).order_by(SchoolSubject.name))
    students=all_rows(select(Student.id,Student.admission_no,Student.first_name,
                             Student.middle_name,Student.last_name,
                             SchoolClass.id.label('class_id'),
                             SchoolClass.name.label('class_name'))
        .join(StudentEnrolment,and_(StudentEnrolment.student_id==Student.id,
                                    StudentEnrolment.session_id==session_id,
                                    StudentEnrolment.active==1))
        .join(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
        .where(Student.active==1)
        .order_by(Student.last_name,Student.first_name))
    if not me['admin_type_system']:
        subjects=[x for x in subjects if _school_subject_allowed(me['id'],x['id'])]; students=[x for x in students if _school_class_allowed(me['id'],x['class_id'])]
    if class_id: students=[x for x in students if x['class_id']==class_id]
    if student_id:
        selected_student=next((x for x in students if x['id']==student_id),None)
        if selected_student: class_id=selected_student['class_id']
    if class_id: subjects=[x for x in subjects if str(class_id) in (x['class_ids'] or '').split(',')]

    def existing_rows():
        if not (student_id and subject_id and session_id):
            return []
        return [_flatten(r,'SchoolStudentResult','subject_name') for r in all_rows(
            select(SchoolStudentResult,SchoolSubject.name.label('subject_name'))
            .join(SchoolSubject,SchoolSubject.id==SchoolStudentResult.subject_id)
            .where(SchoolStudentResult.student_id==student_id,
                   SchoolStudentResult.subject_id==subject_id,
                   SchoolStudentResult.session_id==session_id,
                   func.coalesce(SchoolStudentResult.term,'Full Session')==term)
            .order_by(SchoolStudentResult.id.desc()))]

    existing=existing_rows()
    errors=[]
    if request.method=='POST':
        try: class_id=int(request.form.get('class_id')); session_id=int(request.form.get('session_id')); student_id=int(request.form.get('student_id')); subject_id=int(request.form.get('subject_id'))
        except (TypeError,ValueError): class_id=session_id=student_id=subject_id=0
        term=_result_term(request.form.get('term','')); took=request.form.get('took_test','yes').lower()=='yes'; absence=request.form.get('absence_reason','').strip()
        def num(name):
            raw=request.form.get(name,'').strip(); return _finite(float(raw)) if raw else None
        try: test_score=num('test_score'); test_max=num('test_max'); exam_score=num('exam_score'); exam_max=num('exam_max')
        except (TypeError,ValueError): test_score=test_max=exam_score=exam_max=None
        student=next((x for x in students if x['id']==student_id),None); subject=next((x for x in subjects if x['id']==subject_id),None)
        if term is None: errors.append('Select a valid term.')
        if not student: errors.append('Select a student from the selected class.')
        if not subject: errors.append('Select a subject that has been created for the selected class.')
        if not _school_class_allowed(me['id'],class_id): errors.append('The selected class is outside your authorised scope.')
        if not _school_subject_allowed(me['id'],subject_id): errors.append('The selected subject is outside your authorised scope.')
        if not one_scalar(select(ClassSubject.id).where(ClassSubject.class_id==class_id,
                                                        ClassSubject.subject_id==subject_id)):
            errors.append('That subject has not been created for the selected class.')
        if not took:
            if not absence: errors.append('State why the student did not take the test.')
        else:
            if test_score is None or test_max is None or test_max<=0 or test_score<0 or test_score>test_max: errors.append('Enter a valid Test score and maximum score.')
        if exam_score is None or exam_max is None or exam_max<=0 or exam_score<0: errors.append('Enter a valid Exam score and maximum score.')
        exam_reason=request.form.get('exam_override_reason','').strip()
        if exam_score is not None and exam_max is not None and exam_score>exam_max and not exam_reason: errors.append('An Exam score above its selected maximum requires an approved exception reason.')
        if errors:
            return render_template('school_result_manual_form.html',sessions=sessions,classes=classes,subjects=subjects,students=students,existing=existing_rows(),errors=errors,form=request.form)
        now=datetime.now(timezone.utc).isoformat(); actor=me['id']
        components=[('Test',test_score,test_max,None)] if took else [('Test — Absent',None,None,absence)]
        components.append(('Exam',exam_score,exam_max,exam_reason or None))
        for component,score,max_score,reason in components:
            row=db.session.scalars(select(SchoolStudentResult).where(
                SchoolStudentResult.student_id==student_id,
                SchoolStudentResult.subject_id==subject_id,
                SchoolStudentResult.session_id==session_id,
                func.coalesce(SchoolStudentResult.term,'Full Session')==term,
                SchoolStudentResult.component_name==component)
                .order_by(SchoolStudentResult.id.desc()).limit(1)).first()
            if row and row.status in ('approved','released'):
                errors.append(f'{component} already has an approved/released record. Use the authorised correction process.'); break
            if row:
                frm=row.status; rid=row.id
                # Re-entering a mark sends it back to the start of the workflow.
                row.score=score; row.max_score=max_score; row.status='entered'
                row.entered_by=actor; row.override_reason=reason; row.updated_at=now
                row.updated_by=actor; row.verified_by=None; row.approved_by=None
                row.released_at=None
            else:
                created=SchoolStudentResult(student_id=student_id,assessment_id=None,
                    assignment_id=None,subject_id=subject_id,score=score,max_score=max_score,
                    term=term,session_id=session_id,status='entered',created_at=now,
                    source_type='manual',component_name=component,entered_by=actor,
                    override_reason=reason,updated_at=now,updated_by=actor)
                db.session.add(created); db.session.flush(); rid=created.id; frm='draft'
            db.session.add(ResultWorkflowEvent(result_id=rid,from_status=frm,to_status='entered',
                                               actor_admin_id=actor,reason=reason,created_at=now))
        if errors:
            db.session.rollback()
            return render_template('school_result_manual_form.html',sessions=sessions,classes=classes,subjects=subjects,students=students,existing=existing,errors=errors,form=request.form)
        db.session.commit()
        audit_log('school_manual_result_entered','school','result',student_id,{'subject_id':subject_id,'session_id':session_id,'term':term,'took_test':took}); flash('The offline Test/Exam record has been entered and is awaiting verification.','success'); return redirect(url_for('admin_school_results',**{'class':student['class_name']}))
    return render_template('school_result_manual_form.html',sessions=sessions,classes=classes,subjects=subjects,students=students,existing=existing,errors=errors,form=request.args)

@app.route('/admin/school/results/<int:result_id>/edit',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_result_edit(result_id):
    me=current_admin()
    if not admin_has_permission(me['id'],'school.results.enter'):
        return admin_access_error('school.results.enter')
    row=_result_with_context(result_id)
    if not row: abort(404)
    if not _school_class_allowed(me['id'],row['class_id']): return admin_access_error('school.results.enter')
    if row['status'] in ('approved','released'):
        flash('Approved or released results cannot be edited here. Use the authorised correction process.','error'); return redirect(url_for('admin_school_results',**{'class':row['class_name']}))
    if request.method=='POST':
        try: score=_finite(float(request.form.get('score',''))); max_score=_finite(float(request.form.get('max_score','')))
        except (TypeError,ValueError): score=max_score=-1
        component=request.form.get('component_name','').strip() or row['component_name'] or row.get('assessment_title') or 'Academic Assessment'
        term=_result_term(request.form.get('term','').strip() or row['term'])
        reason=request.form.get('reason','').strip()
        errors=[]
        if term is None: errors.append('Select a valid term.')
        if max_score<=0: errors.append('Maximum score must be greater than zero.')
        if score<0: errors.append('Score cannot be negative.')
        if score>max_score and not reason: errors.append('A score above the selected maximum requires an authorised exception reason.')
        if errors:
            return render_template('school_result_edit_form.html',result=row,errors=errors,form=request.form)
        old_status=row['status']; new_status='entered' if old_status!='entered' else old_status
        now=datetime.now(timezone.utc).isoformat()
        record=obj(SchoolStudentResult,result_id)
        record.score=score; record.max_score=max_score; record.term=term
        record.component_name=component; record.override_reason=reason or None
        record.status=new_status; record.verified_by=None; record.approved_by=None
        record.released_at=None; record.updated_at=now; record.updated_by=me['id']
        db.session.add(ResultWorkflowEvent(result_id=result_id,from_status=old_status,
            to_status=new_status,actor_admin_id=me['id'],
            reason=reason or 'Result edited',created_at=now))
        db.session.commit()
        audit_log('school_result_edited','school','result',result_id,{'previous_status':old_status,'status':new_status})
        flash('Result updated. It must pass verification again before approval.','success')
        return redirect(url_for('admin_school_results',**{'class':row['class_name']}))
    return render_template('school_result_edit_form.html',result=row,errors=[],form={})

@app.post('/admin/school/results/<int:result_id>/workflow')
@admin_required
@csrf_protect
def admin_school_result_workflow(result_id):
    me=current_admin()
    action=request.form.get('action','').strip().lower()
    required={'verify':'school.results.verify','approve':'school.results.approve','release':'school.results.release'}.get(action)
    if not required or not admin_has_permission(me['id'],required):
        return admin_access_error(required or 'school.results.verify')
    row=_result_with_context(result_id,with_subject=False)
    if not row: abort(404)
    if not _school_class_allowed(me['id'],row['class_id']): return admin_access_error(required)
    transitions={'verify':('entered','verified'),'approve':('verified','approved'),'release':('approved','released')}
    frm,to=transitions[action]
    back=results_return_url(url_for('admin_school_results',**{'class':row['class_name']}))
    # The workflow is strictly ordered: entered -> verified -> approved -> released.
    if row['status']!=frm:
        flash(f'This result must be {frm} before it can be {to}.','error'); return redirect(back)
    reason=request.form.get('reason','').strip()
    now=datetime.now(timezone.utc).isoformat()
    record=obj(SchoolStudentResult,result_id)
    record.status=to
    if action=='verify': record.verified_by=me['id']
    elif action=='approve': record.approved_by=me['id']
    else: record.released_at=now
    record.updated_at=now; record.updated_by=me['id']
    db.session.add(ResultWorkflowEvent(result_id=result_id,from_status=frm,to_status=to,
        actor_admin_id=me['id'],reason=reason or None,created_at=now))
    db.session.commit()
    audit_log(f'school_result_{action}','school','result',result_id,{'from':frm,'to':to})
    flash(f'Result {to}.','success')
    if action=='release':
        announce_ready_report_cards([(row['student_id'],row['session_id'],row['term'])],me['id'])
    return redirect(back)

@app.post('/admin/school/results/release-schedule')
@admin_required
@csrf_protect
def admin_school_results_release():
    me=current_admin()
    if not admin_has_permission(me['id'],'school.results.release'): return admin_access_error('school.results.release')
    raw=request.form.get('result_release_at','').strip()
    current=_school_current_session()
    if not current:
        flash('Create or activate an academic session before setting a result release date.','error'); return redirect(url_for('admin_school_results'))
    release=None
    if raw:
        try: release=datetime.fromisoformat(raw).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            flash('Enter a valid release date and time.','error'); return redirect(url_for('admin_school_results'))
    db.session.execute(sa_update(AcademicSession)
        .where(AcademicSession.id==current['id']).values(result_release_at=release))
    # A release date in the past takes effect immediately.
    if release and release <= datetime.now(timezone.utc).isoformat():
        db.session.execute(sa_update(SchoolStudentResult)
            .where(SchoolStudentResult.session_id==current['id'],
                   SchoolStudentResult.status=='approved')
            .values(status='released',released_at=datetime.now(timezone.utc).isoformat()))
    db.session.commit()
    audit_log('school_results_release_schedule_changed','school','academic_session',current['id'],{'release_at':release},True,me)
    flash('Result release schedule updated.','success'); return redirect(url_for('admin_school_results'))

@app.post('/admin/school/students/<int:sid>/history')
@admin_required
@csrf_protect
def admin_school_student_history_add(sid):
    if not admin_has_permission(current_admin()['id'],'student.history.manage'): return admin_access_error('student.history.manage')
    level=request.form.get('level_name','').strip()
    session_id=request.form.get('session_id',type=int)
    enrolled_at=request.form.get('enrolled_at','').strip()
    completed_at=request.form.get('completed_at','').strip()
    notes=request.form.get('notes','').strip()
    allowed={'Daycare','Toddler 1','Toddler 2','Junior Reception','Middle Reception','Senior Reception','Crèche','Nursery 1','Nursery 2','Nursery 3','Primary 1','Primary 2','Primary 3','Primary 4','Primary 5','Primary 6','JSS 1','JSS 2','JSS 3','SSS 1','SSS 2','SSS 3'}
    if level not in allowed:
        flash('Select a valid enrollment level.','error'); return redirect(url_for('admin_school_student_detail',sid=sid)+'#enrolment-history')
    if not one_scalar(select(Student.id).where(Student.id==sid)): abort(404)
    if session_id and not one_scalar(select(AcademicSession.id).where(
            AcademicSession.id==session_id,AcademicSession.active==1)):
        flash('Select a valid academic session.','error'); return redirect(url_for('admin_school_student_detail',sid=sid)+'#enrolment-history')
    class_id=one_scalar(select(SchoolClass.id).where(SchoolClass.name==level))
    now=datetime.now(timezone.utc).isoformat()
    entry_date=enrolled_at or now[:10]
    # Close any open earlier record before opening this one.
    db.session.execute(sa_update(StudentEnrollmentHistory)
        .where(StudentEnrollmentHistory.student_id==sid,
               StudentEnrollmentHistory.active==1,
               func.coalesce(StudentEnrollmentHistory.enrolled_at,'') <= entry_date)
        .values(active=0,completed_at=func.coalesce(StudentEnrollmentHistory.completed_at,entry_date)))
    db.session.add(StudentEnrollmentHistory(student_id=sid,session_id=session_id,
        level_name=level,class_id=class_id,enrolled_at=entry_date,
        completed_at=completed_at or None,active=1,notes=notes,created_at=now))
    db.session.flush()
    _mark_latest_history_current(sid)
    if class_id and level in ACADEMIC_HISTORY_LEVELS and session_id:
        _sync_enrolment_for_history(sid,class_id,session_id,enrolled_at or now[:10])
    db.session.commit()
    audit_log('student_enrollment_history_added','school','student',sid,{'level':level,'session_id':session_id,'movement':True,'notes':notes})
    flash(f'Enrollment history updated: {level}.','success')
    return redirect(url_for('admin_school_student_detail',sid=sid)+'#enrolment-history')

@app.route('/admin/school/students/<int:sid>/history/<int:history_id>/edit',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_student_history_edit(sid,history_id):
    me=current_admin()
    if not admin_has_permission(me['id'],'student.history.manage'): return admin_access_error('student.history.manage')
    row=db.session.scalars(select(StudentEnrollmentHistory).where(
        StudentEnrollmentHistory.id==history_id,
        StudentEnrollmentHistory.student_id==sid)).first()
    sessions=db.session.scalars(select(AcademicSession).where(AcademicSession.active==1)
        .order_by(AcademicSession.id.desc())).all()
    if not row: abort(404)
    row_dict={c.key:getattr(row,c.key) for c in row.__mapper__.column_attrs}
    levels=['Daycare','Crèche','Toddler 1','Toddler 2','Junior Reception','Middle Reception','Senior Reception'] + [f'Primary {i}' for i in range(1,7)] + [f'JSS {i}' for i in range(1,4)] + [f'SSS {i}' for i in range(1,4)]
    if request.method=='POST':
        level=request.form.get('level_name','').strip()
        session_id=request.form.get('session_id',type=int)
        enrolled=request.form.get('enrolled_at','').strip() or None
        completed=request.form.get('completed_at','').strip() or None
        reason=request.form.get('correction_reason','').strip()
        if level not in levels: return render_template('admin_enrollment_history_edit.html',row=row_dict,sessions=sessions,levels=levels,errors=['Select a valid enrollment level.'])
        if not reason: return render_template('admin_enrollment_history_edit.html',row=row_dict,sessions=sessions,levels=levels,errors=['A correction reason is required when changing historical records.'])
        old_level=row.level_name
        class_id=one_scalar(select(SchoolClass.id).where(SchoolClass.name==level))
        now=datetime.now(timezone.utc).isoformat()
        row.level_name=level; row.session_id=session_id; row.class_id=class_id
        row.enrolled_at=enrolled; row.completed_at=completed; row.corrected_at=now
        row.corrected_by=me['id']; row.correction_reason=reason
        db.session.flush()
        _mark_latest_history_current(sid)
        if class_id and level in ACADEMIC_HISTORY_LEVELS and session_id:
            _sync_enrolment_for_history(sid,class_id,session_id,enrolled or now[:10])
        db.session.commit()
        audit_log('student_enrollment_history_corrected','school','student_history',history_id,{'student_id':sid,'old_level':old_level,'new_level':level,'reason':reason})
        flash('Enrollment history corrected and the change was recorded in the audit trail.','success')
        return redirect(url_for('admin_school_student_detail',sid=sid)+'#enrolment-history')
    return render_template('admin_enrollment_history_edit.html',row=row,sessions=sessions,levels=levels,errors=[])

@app.route('/admin/school/promotion', methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_promotion():
    me=current_admin()
    if not is_school_admin():
        return admin_access_error('Promotion')
    sessions=db.session.scalars(select(AcademicSession)
        .where(AcademicSession.active==1)
        .order_by(AcademicSession.id.desc())).all()
    classes=_promotion_classes(include_inactive=True)
    progressions=_promotion_progressions()
    current=_school_current_session()

    if request.method=='POST':
        action=request.form.get('action','').strip()
        try:
            from_session_id=int(request.form.get('from_session_id','0'))
            to_session_id=int(request.form.get('to_session_id','0'))
        except (TypeError,ValueError):
            from_session_id=to_session_id=0

        def active_session(sid):
            return db.session.scalars(select(AcademicSession).where(
                AcademicSession.id==sid,AcademicSession.active==1)).first()

        from_session=active_session(from_session_id)
        to_session=active_session(to_session_id)

        if not from_session or not to_session:
            flash('Select valid source and destination academic sessions.','error')
            return redirect(url_for('admin_school_promotion'))

        if from_session_id==to_session_id:
            flash('Source and destination sessions must be different.','error')
            return redirect(url_for('admin_school_promotion'))

        if action=='generate':
            existing=_promotion_run_has_uncommitted(from_session_id,to_session_id)
            if existing:
                flash(f'Promotion run #{existing["id"]} already exists with status '
                      f'{existing["status"]}. Review it before creating another.','error')
                return redirect(url_for('admin_school_promotion_run',run_id=existing['id']))

            now=datetime.now(timezone.utc).isoformat()
            run=AcademicPromotionRun(from_session_id=from_session_id,
                                     to_session_id=to_session_id,status='draft',
                                     created_by=me['id'],created_at=now)
            db.session.add(run); db.session.flush()

            enrolments=_promotion_current_enrolments(from_session_id)
            # Seed every student with the destination their class progression
            # configures; a class with no progression is left for review.
            for row in enrolments:
                nxt=_promotion_next_class(row['class_id'])
                db.session.add(AcademicPromotionItem(
                    run_id=run.id,student_id=row['student_id'],
                    source_enrolment_id=row['enrolment_id'],
                    source_class_id=row['class_id'],
                    proposed_class_id=nxt['to_class_id'] if nxt else None,
                    final_class_id=None,action='promote',reason=None,
                    created_at=now,decision='promote'))
            db.session.commit()

            _promotion_audit('academic_promotion_draft_created',run.id,{
                'from_session_id':from_session_id,
                'to_session_id':to_session_id,
                'student_count':len(enrolments)})

            flash(f'Promotion draft #{run.id} created for {len(enrolments)} student(s).','success')
            return redirect(url_for('admin_school_promotion_run',run_id=run.id))

    RunFromSession=sa.orm.aliased(AcademicSession)
    RunToSession=sa.orm.aliased(AcademicSession)
    item_count_sq=(select(func.count(AcademicPromotionItem.id))
        .where(AcademicPromotionItem.run_id==AcademicPromotionRun.id)
        .correlate(AcademicPromotionRun).scalar_subquery())
    runs=[_flatten(r,'AcademicPromotionRun','from_session_name','to_session_name',
                   'created_by_name','student_count')
          for r in all_rows(
        select(AcademicPromotionRun,RunFromSession.name.label('from_session_name'),
               RunToSession.name.label('to_session_name'),
               Admin.display_name.label('created_by_name'),
               item_count_sq.label('student_count'))
        .join(RunFromSession,RunFromSession.id==AcademicPromotionRun.from_session_id)
        .join(RunToSession,RunToSession.id==AcademicPromotionRun.to_session_id)
        .outerjoin(Admin,Admin.id==AcademicPromotionRun.created_by)
        .order_by(AcademicPromotionRun.id.desc()))]

    return render_template(
        'admin_school_promotion.html',
        sessions=sessions,
        classes=classes,
        progressions=progressions,
        current_session=current,
        runs=runs)

@app.route('/admin/school/promotion/<int:run_id>', methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_promotion_run(run_id):
    me=current_admin()
    if not is_school_admin():
        return admin_access_error('Promotion')
    FromSession=sa.orm.aliased(AcademicSession)
    ToSession=sa.orm.aliased(AcademicSession)
    raw=one(select(AcademicPromotionRun,
                   FromSession.name.label('from_session_name'),
                   ToSession.name.label('to_session_name'))
            .join(FromSession,FromSession.id==AcademicPromotionRun.from_session_id)
            .join(ToSession,ToSession.id==AcademicPromotionRun.to_session_id)
            .where(AcademicPromotionRun.id==run_id))
    if not raw:
        abort(404)
    run=_flatten(raw,'AcademicPromotionRun','from_session_name','to_session_name')
    run_row=obj(AcademicPromotionRun,run_id)

    def back():
        return redirect(url_for('admin_school_promotion_run',run_id=run_id))

    if request.method=='POST':
        action=request.form.get('action','').strip()

        if action=='save':
            if run['status'] not in ('draft','approved'):
                flash('This promotion run can no longer be edited.','error'); return back()

            for raw_id in request.form.getlist('item_id'):
                try: item_id=int(raw_id)
                except (TypeError,ValueError): continue

                item=db.session.scalars(select(AcademicPromotionItem).where(
                    AcademicPromotionItem.id==item_id,
                    AcademicPromotionItem.run_id==run_id)).first()
                if not item: continue

                decision=request.form.get(f'action_{item_id}',
                                          item.action or 'promote').strip().lower()
                if decision not in PROMOTION_DECISIONS:
                    decision='promote'

                raw_target=request.form.get(f'target_{item_id}','').strip()
                target_id=None
                if raw_target:
                    try: target_id=int(raw_target)
                    except (TypeError,ValueError): target_id=None

                if decision=='promote':
                    # Normal promotion follows the configured default.
                    target_id=item.proposed_class_id
                elif decision=='repeat':
                    target_id=item.source_class_id
                elif decision in ('graduate','withdraw','hold'):
                    target_id=None
                elif decision=='transfer':
                    # Transfer is the deliberate exception mechanism.
                    if not target_id:
                        target_id=item.proposed_class_id

                reason=request.form.get(f'reason_{item_id}','').strip()

                # Validate a manually selected destination immediately.
                if decision in ('promote','repeat','transfer'):
                    if not target_id:
                        flash(f'Student item #{item_id} requires a destination class.','error')
                        return back()
                    if not _promotion_target(target_id):
                        flash(f'Selected destination for item #{item_id} does not exist.','error')
                        return back()

                item.final_class_id=target_id
                item.action=decision
                item.decision=decision
                item.reason=reason or None
                item.updated_at=datetime.now(timezone.utc).isoformat()

            # Saving a draft returns it to draft state.
            run_row.status='draft'; run_row.approved_by=None; run_row.approved_at=None
            db.session.commit()
            _promotion_audit('academic_promotion_draft_updated',run_id,{'admin_id':me['id']})
            flash('Promotion draft saved.','success')
            return back()

        if action=='approve':
            if run['status']!='draft':
                flash('Only a draft promotion run can be approved.','error'); return back()
            valid,errors=_promotion_validate_run(run_id)
            if not valid:
                flash('Promotion approval blocked. '+' '.join(errors[:8]),'error'); return back()
            run_row.status='approved'; run_row.approved_by=me['id']
            run_row.approved_at=datetime.now(timezone.utc).isoformat()
            db.session.commit()
            _promotion_audit('academic_promotion_approved',run_id,{'admin_id':me['id']})
            flash('Promotion run approved. It is ready to commit.','success')
            return back()

        if action=='commit':
            if run['status']!='approved':
                flash('Only an approved promotion run can be committed.','error'); return back()
            valid,errors=_promotion_validate_run(run_id)
            if not valid:
                flash('Promotion commit blocked. '+' '.join(errors[:8]),'error'); return back()

            now=datetime.now(timezone.utc).isoformat()
            committed_count=0
            decision_counts={}
            try:
                # The session owns one transaction for the whole commit, so any
                # failure below rolls the entire promotion back.
                valid,errors=_promotion_validate_run(run_id)
                if not valid:
                    raise ValueError(' '.join(errors[:8]))

                items=db.session.scalars(select(AcademicPromotionItem)
                    .where(AcademicPromotionItem.run_id==run_id)
                    .order_by(AcademicPromotionItem.id)).all()

                for item in items:
                    decision=(item.decision or item.action or 'promote').strip().lower()
                    decision_counts[decision]=decision_counts.get(decision,0)+1

                    # These decisions do not create a new enrolment.
                    if decision in ('graduate','withdraw','hold'):
                        item.committed_enrolment_id=None
                        continue

                    if decision=='promote':
                        target_id=item.proposed_class_id
                    elif decision=='repeat':
                        target_id=item.source_class_id
                    elif decision=='transfer':
                        target_id=item.final_class_id
                    else:
                        raise ValueError(f'Unsupported promotion decision: {decision}')

                    if not target_id:
                        raise ValueError(f'Student ID {item.student_id} has no destination class.')

                    target=obj(SchoolClass,target_id)
                    if not target:
                        raise ValueError(f'Destination class {target_id} no longer exists.')
                    if int(target.active or 0)!=1:
                        raise ValueError(f"Destination class '{target.name}' is inactive.")

                    # The database has a unique student/session identity.
                    existing=one_scalar(select(StudentEnrolment.id).where(
                        StudentEnrolment.student_id==item.student_id,
                        StudentEnrolment.session_id==run['to_session_id']).limit(1))
                    if existing:
                        raise ValueError(f'Student ID {item.student_id} already has '
                                         f'target-session enrolment {existing}.')

                    enrolment=StudentEnrolment(student_id=item.student_id,class_id=target_id,
                                               session_id=run['to_session_id'],
                                               enrolled_at=now[:10],active=1)
                    db.session.add(enrolment); db.session.flush()

                    # Close the previous session's active history record only
                    # after the new enrolment has been created.
                    db.session.execute(sa_update(StudentEnrollmentHistory)
                        .where(StudentEnrollmentHistory.student_id==item.student_id,
                               StudentEnrollmentHistory.session_id==run['from_session_id'],
                               StudentEnrollmentHistory.class_id==item.source_class_id,
                               StudentEnrollmentHistory.active==1)
                        .values(completed_at=now,active=0))

                    # Preserve a new historical record for the destination.
                    db.session.add(StudentEnrollmentHistory(
                        student_id=item.student_id,session_id=run['to_session_id'],
                        level_name=target.name,class_id=target_id,
                        enrolled_at=now[:10],completed_at=None,active=1,
                        notes=f'Academic promotion run #{run_id}: {decision}',
                        created_at=now))

                    item.committed_enrolment_id=enrolment.id
                    committed_count+=1

                run_row.status='committed'
                run_row.committed_by=me['id']
                run_row.committed_at=now
                db.session.commit()

            except Exception as exc:
                db.session.rollback()
                flash(f'Promotion commit failed and was rolled back: {exc}','error')
                return back()

            _promotion_audit('academic_promotion_committed',run_id,{
                'admin_id':me['id'],
                'committed_students':committed_count,
                'decisions':decision_counts})
            flash(f'Promotion run #{run_id} committed successfully. '
                  f'{committed_count} new enrolment(s) created.','success')
            return back()

        if action=='cancel':
            if run['status'] not in ('draft','approved'):
                flash('This promotion run cannot be cancelled.','error'); return back()
            run_row.status='cancelled'; run_row.cancelled_by=me['id']
            run_row.cancelled_at=datetime.now(timezone.utc).isoformat()
            db.session.commit()
            _promotion_audit('academic_promotion_cancelled',run_id,{'admin_id':me['id']})
            flash('Promotion run cancelled.','success')
            return redirect(url_for('admin_school_promotion'))

    # ---------------------------------------------------------------
    # DISPLAY
    # ---------------------------------------------------------------
    SourceClass=sa.orm.aliased(SchoolClass)
    ProposedClass=sa.orm.aliased(SchoolClass)
    FinalClass=sa.orm.aliased(SchoolClass)
    items=[_flatten(r,'AcademicPromotionItem','admission_no','first_name','middle_name',
                    'last_name','source_class_name','proposed_class_name',
                    'proposed_class_active','final_class_name') for r in all_rows(
        select(AcademicPromotionItem,Student.admission_no,Student.first_name,
               Student.middle_name,Student.last_name,
               SourceClass.name.label('source_class_name'),
               ProposedClass.name.label('proposed_class_name'),
               ProposedClass.active.label('proposed_class_active'),
               FinalClass.name.label('final_class_name'))
        .join(Student,Student.id==AcademicPromotionItem.student_id)
        .join(SourceClass,SourceClass.id==AcademicPromotionItem.source_class_id)
        .outerjoin(ProposedClass,ProposedClass.id==AcademicPromotionItem.proposed_class_id)
        .outerjoin(FinalClass,FinalClass.id==AcademicPromotionItem.final_class_id)
        .where(AcademicPromotionItem.run_id==run_id)
        .order_by(SourceClass.level_order,SourceClass.id,
                  Student.last_name,Student.first_name))]

    classes=_promotion_classes(include_inactive=True)

    summary={'total':len(items),'promote':0,'repeat':0,'transfer':0,
             'graduate':0,'withdraw':0,'hold':0,'review':0}
    for item in items:
        decision=(item['decision'] or item['action'] or 'promote').strip().lower()
        if decision in summary:
            summary[decision]+=1
        if decision=='promote':
            # A promotion with no destination, or one pointing at an inactive
            # class, needs a human decision before this run can commit.
            if not item['proposed_class_id']:
                summary['review']+=1
            elif int(item['proposed_class_active'] or 0)!=1:
                summary['review']+=1

    return render_template(
        'admin_school_promotion_run.html',
        run=run,
        items=items,
        classes=classes,
        summary=summary,
        actions=PROMOTION_ACTIONS)

@app.route('/admin/school/promotion/progressions', methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_promotion_progressions():
    me=current_admin()

    if not is_school_admin():
        return admin_access_error('Promotion progression configuration')

    if request.method=='POST':
        action=request.form.get('action','').strip()

        if action=='save':
            try: from_class_id=int(request.form.get('from_class_id','0'))
            except (TypeError,ValueError): from_class_id=0

            raw_to=request.form.get('to_class_id','').strip()
            try: to_class_id=int(raw_to) if raw_to else None
            except (TypeError,ValueError): to_class_id=None

            if not obj(SchoolClass,from_class_id):
                flash('Select a valid source class.','error')
                return redirect(url_for('admin_school_promotion_progressions'))

            # The destination may be ACTIVE or INACTIVE. Inactive does not mean
            # nonexistent, so only existence is checked here; availability is
            # re-checked when a run is approved or committed.
            if to_class_id and not obj(SchoolClass,to_class_id):
                flash('Select a valid destination class.','error')
                return redirect(url_for('admin_school_promotion_progressions'))

            now=datetime.now(timezone.utc).isoformat()
            existing=db.session.scalars(select(SchoolClassProgression)
                .where(SchoolClassProgression.from_class_id==from_class_id,
                       SchoolClassProgression.active==1)
                .order_by(SchoolClassProgression.id.desc()).limit(1)).first()

            if existing:
                existing.to_class_id=to_class_id
                existing.updated_at=now
                existing.updated_by=me['id']
            else:
                db.session.add(SchoolClassProgression(
                    from_class_id=from_class_id,to_class_id=to_class_id,active=1,
                    created_by=me['id'],created_at=now,updated_at=now,updated_by=me['id']))

            db.session.commit()

            _promotion_audit('academic_promotion_progression_updated',from_class_id,{
                'from_class_id':from_class_id,
                'to_class_id':to_class_id,
                'updated_by':me['id']})

            if to_class_id:
                flash('Progression rule saved. The destination remains subject to its '
                      'current class availability status.','success')
            else:
                flash('Progression cleared. Students from this class will require review.','success')

            return redirect(url_for('admin_school_promotion_progressions'))

    return render_template(
        'admin_school_promotion_progressions.html',
        classes=_promotion_classes(include_inactive=True),
        progressions=_promotion_progressions())

@app.route('/admin/school/sessions', methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_school_sessions():
    me=current_admin()
    if not is_school_admin():
        return admin_access_error('Academic session management')

    if request.method=='POST':
        action=request.form.get('action','').strip()
        now=datetime.now(timezone.utc).isoformat()

        if action=='create':
            name=request.form.get('name','').strip()
            start_date=request.form.get('start_date','').strip() or None
            end_date=request.form.get('end_date','').strip() or None
            make_current=request.form.get('make_current')=='1'
            errors=[]
            if not name: errors.append('Enter a name for the academic session, e.g. 2027/2028.')
            elif one_scalar(select(AcademicSession.id).where(AcademicSession.name==name)):
                errors.append(f'An academic session named "{name}" already exists.')
            if start_date and end_date and end_date<start_date:
                errors.append('The end date cannot be before the start date.')
            if errors:
                for e in errors: flash(e,'error')
                return redirect(url_for('admin_school_sessions'))
            if make_current:
                db.session.execute(sa_update(AcademicSession).values(is_current=0))
            session_row=AcademicSession(name=name,start_date=start_date,end_date=end_date,
                is_current=1 if make_current else 0,active=1,created_at=now)
            db.session.add(session_row); db.session.flush()
            db.session.commit()
            audit_log('academic_session_created','school','academic_session',session_row.id,
                      {'name':name,'made_current':make_current})
            flash(f'Academic session "{name}" created.'+(' Set as the current session.' if make_current else ''),'success')
            return redirect(url_for('admin_school_sessions'))

        if action=='set_ca_weights':
            try:
                test_w=_finite(float(request.form.get('ca_weight_test','0') or 0))
                assignment_w=_finite(float(request.form.get('ca_weight_assignment','0') or 0))
                project_w=_finite(float(request.form.get('ca_weight_project','0') or 0))
            except (TypeError,ValueError):
                flash('Enter valid numbers for each weight.','error'); return redirect(url_for('admin_school_sessions'))
            total=test_w+assignment_w+project_w
            if test_w<0 or assignment_w<0 or project_w<0:
                flash('Weights cannot be negative.','error'); return redirect(url_for('admin_school_sessions'))
            if abs(total-CA_MAX_SCORE)>0.001:
                flash(f'Test + Assignment + Project weights must add up to exactly {CA_MAX_SCORE:g} (currently {total:g}).','error')
                return redirect(url_for('admin_school_sessions'))
            _set_ca_weights({'test':test_w,'assignment':assignment_w,'project':project_w},me['id'])
            audit_log('ca_weights_updated','school','ca_weights',None,
                      {'test':test_w,'assignment':assignment_w,'project':project_w})
            flash('Continuous assessment weighting updated.','success')
            return redirect(url_for('admin_school_sessions'))

        try: session_id=int(request.form.get('session_id','0'))
        except (TypeError,ValueError): session_id=0
        row=obj(AcademicSession,session_id)
        if not row:
            flash('Academic session not found.','error'); return redirect(url_for('admin_school_sessions'))

        if action=='set_current':
            if not row.active:
                flash('Reactivate this session before making it current.','error')
                return redirect(url_for('admin_school_sessions'))
            db.session.execute(sa_update(AcademicSession).values(is_current=0))
            row.is_current=1
            db.session.commit()
            audit_log('academic_session_set_current','school','academic_session',row.id,{'name':row.name})
            flash(f'"{row.name}" is now the current academic session.','success')
        elif action=='archive':
            if row.is_current:
                flash('Set another session as current before archiving this one.','error')
                return redirect(url_for('admin_school_sessions'))
            row.active=0
            db.session.commit()
            audit_log('academic_session_archived','school','academic_session',row.id,{'name':row.name})
            flash(f'"{row.name}" has been archived.','success')
        elif action=='reactivate':
            row.active=1
            db.session.commit()
            audit_log('academic_session_reactivated','school','academic_session',row.id,{'name':row.name})
            flash(f'"{row.name}" has been reactivated.','success')
        else:
            flash('Unrecognised action.','error')
        return redirect(url_for('admin_school_sessions'))

    sessions=db.session.scalars(select(AcademicSession)
        .order_by(AcademicSession.is_current.desc(),AcademicSession.id.desc())).all()
    return render_template('admin_school_sessions.html',sessions=sessions,
        ca_weights=_ca_weights(),ca_max=CA_MAX_SCORE,exam_max=EXAM_MAX_SCORE,terms=ACADEMIC_TERMS)
