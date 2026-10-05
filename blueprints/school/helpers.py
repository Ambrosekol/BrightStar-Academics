"""Private helpers used only by the school-portal routes in this package:
access-scope checks, form-context builders, the CA-weighted term report,
receipt-free assessment listing, and the class-promotion workflow.
"""

import math
import re
from functools import wraps
from datetime import datetime, timezone

import sqlalchemy as sa
from flask import flash, redirect, render_template, request, url_for
from sqlalchemy import and_, or_, func, select, update as sa_update

from models import (
    Admin, AcademicPromotionItem, AcademicPromotionRun, AcademicSession,
    AdminScope, AssignmentQuestion, AssignmentStudent, ClassSubject,
    ParentStudentLink, ProjectStudent, School, SchoolAssessment,
    SchoolAssignment, SchoolAssignmentAttempt, SchoolClass,
    SchoolClassProgression, SchoolNotification, SchoolProject, SchoolQuestion,
    SchoolSetting, SchoolStudentResult, SchoolSubject, Student,
    StudentEnrolment, StudentEnrollmentHistory, PresenceSession, db,
)
from core.db_helpers import all_rows, group_concat, obj, one, one_scalar, tuples, _flatten
from core.security import admin_access_error, admin_has_permission, admin_scope_allows, audit_log, current_admin, is_school_admin
from core.storage import uploads_dir
from blueprints.finance.helpers import _primary_school_id

# _school_current_session/_students_with_class still live in app.py.
from app import _school_current_session, _students_with_class

# CA_DEFAULT_WEIGHTS/CA_MAX_SCORE/EXAM_MAX_SCORE/ACADEMIC_TERMS still live in
# app.py alongside the entrance-domain constants they're declared next to.
from app import ACADEMIC_TERMS, CA_DEFAULT_WEIGHTS, CA_MAX_SCORE, EXAM_MAX_SCORE


PROMOTION_ACTIONS = {
    "promote": "Promote",
    "repeat": "Repeat",
    "transfer": "Transfer / Reassign",
    "graduate": "Graduate",
    "withdraw": "Withdraw",
    "hold": "Hold",
}

PROMOTION_DECISIONS = set(PROMOTION_ACTIONS.keys())

def _school_class_allowed(admin_id,class_id):
    admin=current_admin()
    if admin and admin['admin_type_system']: return True
    name=one_scalar(select(SchoolClass.name).where(SchoolClass.id==class_id))
    return bool(name and admin_scope_allows(admin_id,'class',name))

def _school_subject_allowed(admin_id,subject_id):
    admin=current_admin()
    if admin and admin['admin_type_system']: return True
    name=one_scalar(select(SchoolSubject.name).where(SchoolSubject.id==subject_id))
    any_subject_scope=one(select(AdminScope.id).where(
        AdminScope.admin_id==admin_id,AdminScope.scope_type=='subject').limit(1))
    # Primary class teachers normally receive class scope and manage all subjects
    # offered by that class. College subject teachers can additionally receive a
    # subject scope, which restricts them to their own subject.
    return bool(name and (not any_subject_scope or admin_scope_allows(admin_id,'subject',name)))

def _school_pair_allowed(admin_id,class_id,subject_id):
    return _school_class_allowed(admin_id,class_id) and _school_subject_allowed(admin_id,subject_id)

def _school_form_context():
    classes=db.session.scalars(select(SchoolClass).where(SchoolClass.active==1)
                               .order_by(SchoolClass.level_order)).all()
    subjects=db.session.scalars(select(SchoolSubject).where(SchoolSubject.active==1)
                                .order_by(SchoolSubject.name)).all()
    session_row=_school_current_session()
    students=_students_with_class(session_row['id'] if session_row else 0)
    admin=current_admin()
    if admin and not admin['admin_type_system']:
        classes=[x for x in classes if admin_scope_allows(admin['id'],'class',x['name'])]
        subjects=[x for x in subjects if admin_scope_allows(admin['id'],'subject',x['name'])]
        students=[x for x in students if x['class_name'] and admin_scope_allows(admin['id'],'class',x['class_name'])]
    return classes,subjects,students,session_row

def _school_student_visible(sid):
    admin=current_admin()
    if admin and admin['admin_type_system']: return True
    class_name=one_scalar(select(SchoolClass.name)
        .select_from(StudentEnrolment)
        .join(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
        .where(StudentEnrolment.student_id==sid,StudentEnrolment.active==1)
        .order_by(StudentEnrolment.id.desc()).limit(1))
    return bool(class_name and admin_scope_allows(admin['id'],'class',class_name))

def _assignment_students(assignment_id):
    return [_flatten(r,'AssignmentStudent','first_name','middle_name','last_name','admission_no')
            for r in all_rows(
        select(AssignmentStudent,Student.first_name,Student.middle_name,
               Student.last_name,Student.admission_no)
        .join(Student,Student.id==AssignmentStudent.student_id)
        .where(AssignmentStudent.assignment_id==assignment_id,Student.active==1)
        .order_by(Student.last_name,Student.first_name))]

def _notify_school_work(student_ids, category, title, message, action_url, created_by):
    """Notify each student and every linked parent about new or graded work."""
    now=datetime.now(timezone.utc).isoformat()
    # Archived students are no longer current, so they neither get the notice nor their parents.
    ids=sorted({row[0] for row in tuples(select(Student.id)
        .where(Student.id.in_(set(student_ids)),Student.active==1))})
    if not ids:
        return
    names={sid:' '.join(x for x in (first,middle,last) if x)
           for sid,first,middle,last in tuples(
        select(Student.id,Student.first_name,Student.middle_name,Student.last_name)
        .where(Student.id.in_(ids)))}
    parents={}
    for sid,pid in tuples(select(ParentStudentLink.student_id,ParentStudentLink.parent_id)
                          .where(ParentStudentLink.student_id.in_(ids),
                                 ParentStudentLink.active==1)):
        parents.setdefault(sid,[]).append(pid)
    for sid in ids:
        child_name=names.get(sid,'your child')
        db.session.add(SchoolNotification(
            recipient_type='student',recipient_id=sid,student_id=sid,category=category,
            title=title,message=message,action_url=action_url,
            created_at=now,created_by=created_by))
        parent_url=url_for('parent_child_detail',student_id=sid)+'#assignments'
        for pid in parents.get(sid,[]):
            db.session.add(SchoolNotification(
                recipient_type='parent',recipient_id=pid,student_id=sid,category=category,
                title=title,message=f'{child_name}: {message}',action_url=parent_url,
                created_at=now,created_by=created_by))

def _assignment_form_data(admin_id):
    classes=db.session.scalars(select(SchoolClass).where(SchoolClass.active==1)
        .order_by(SchoolClass.level_order)).all()
    subjects=[_flatten(r,'SchoolSubject','class_ids') for r in all_rows(
        select(SchoolSubject,group_concat(ClassSubject.class_id).label('class_ids'))
        .outerjoin(ClassSubject,ClassSubject.subject_id==SchoolSubject.id)
        .where(SchoolSubject.active==1)
        .group_by(SchoolSubject.id).order_by(SchoolSubject.name))]
    students=all_rows(
        select(Student.id,Student.admission_no,Student.first_name,Student.middle_name,
               Student.last_name,SchoolClass.id.label('class_id'),
               SchoolClass.name.label('class_name'))
        .join(StudentEnrolment,and_(StudentEnrolment.student_id==Student.id,
                                    StudentEnrolment.active==1))
        .join(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
        .where(Student.active==1)
        .order_by(SchoolClass.level_order,Student.last_name,Student.first_name))
    if not current_admin()['admin_type_system']:
        classes=[x for x in classes if _school_class_allowed(admin_id,x.id)]
        subjects=[x for x in subjects if _school_subject_allowed(admin_id,x['id'])]
        students=[x for x in students if _school_class_allowed(admin_id,x['class_id'])]
    return classes,subjects,students

def _work_with_class_subject(model, work_id):
    """A project or assignment joined to its class and subject names."""
    row=one(select(model,SchoolClass.name.label('class_name'),
                   SchoolSubject.name.label('subject_name'))
            .join(SchoolClass,SchoolClass.id==model.class_id)
            .join(SchoolSubject,SchoolSubject.id==model.subject_id)
            .where(model.id==work_id))
    return _flatten(row,model.__name__,'class_name','subject_name') if row else None

def _school_assessments(kind):
    rows=[_flatten(r,'SchoolAssessment','class_name','subject_name','question_count') for r in all_rows(
        select(SchoolAssessment,SchoolClass.name.label('class_name'),
               SchoolSubject.name.label('subject_name'),
               func.count(SchoolQuestion.id).label('question_count'))
            .join(SchoolClass,SchoolClass.id==SchoolAssessment.class_id)
            .join(SchoolSubject,SchoolSubject.id==SchoolAssessment.subject_id)
            .outerjoin(SchoolQuestion,SchoolQuestion.assessment_id==SchoolAssessment.id)
            .where(SchoolAssessment.assessment_type==kind)
            .group_by(SchoolAssessment.id,SchoolClass.id,SchoolSubject.id)
            .order_by(SchoolAssessment.id.desc()))]
    classes=db.session.scalars(select(SchoolClass).where(SchoolClass.active==1)
                               .order_by(SchoolClass.level_order)).all()
    admin=current_admin()
    if admin and not admin['admin_type_system']:
        classes=[c for c in classes if admin_scope_allows(admin['id'],'class',c['name'])]
        rows=[r for r in rows if _school_pair_allowed(admin['id'],r['class_id'],r['subject_id'])]
    return rows,classes

def _school_assessment_title(kind): return {'test':'Tests','practice':'Practice Tests','examination':'Examinations'}[kind]

def _school_assessment_description(kind): return {'test':'Teacher-created assessments connected to a school subject and class.','practice':'Practice assessments managed separately from official academic examinations.','examination':'Formal school examinations whose results belong to the student academic record.'}[kind]

def _school_assessment_list(kind):
    selected_class=request.args.get('class','').strip(); search=request.args.get('q','').strip(); assessments,classes=_school_assessments(kind)
    cards=[{'name':c['name'],'count':sum(1 for a in assessments if a['class_name']==c['name']),'id':c['id']} for c in classes]
    if selected_class: assessments=[a for a in assessments if a['class_name']==selected_class]
    if search:
        needle=search.casefold(); assessments=[a for a in assessments if needle in f"{a['title']} {a['class_name']} {a['subject_name']}".casefold()]
    return render_template('school_assessment_list.html',assessment_type=kind,title=_school_assessment_title(kind),description=_school_assessment_description(kind),assessments=assessments,class_cards=cards,selected_class=selected_class,search=search)

def _school_sessions():
    return db.session.scalars(select(AcademicSession).where(AcademicSession.active==1)
                              .order_by(AcademicSession.id.desc())).all()

def _ca_weights():
    """How CA_MAX_SCORE (40) is split between tests, assignments and projects.

    Stored per-school in school_settings; falls back to CA_DEFAULT_WEIGHTS for
    any component that has never been set. Always returns exactly the three
    keys in CA_DEFAULT_WEIGHTS, as floats.
    """
    school_id=_primary_school_id()
    weights=dict(CA_DEFAULT_WEIGHTS)
    if not school_id: return weights
    rows=all_rows(select(SchoolSetting.setting_key,SchoolSetting.setting_value)
                  .where(SchoolSetting.school_id==school_id,
                         SchoolSetting.setting_key.in_([f'ca_weight_{k}' for k in CA_DEFAULT_WEIGHTS])))
    for r in rows:
        key=r['setting_key'].removeprefix('ca_weight_')
        try: weights[key]=float(r['setting_value'])
        except (TypeError,ValueError): pass
    return weights

def _set_ca_weights(weights, admin_id):
    school_id=_primary_school_id()
    if not school_id: raise ValueError('No active school is configured.')
    now=datetime.now(timezone.utc).isoformat()
    for key,value in weights.items():
        setting_key=f'ca_weight_{key}'
        row=db.session.scalars(select(SchoolSetting).where(
            SchoolSetting.school_id==school_id,SchoolSetting.setting_key==setting_key)).first()
        if row:
            row.setting_value=str(value); row.updated_at=now; row.updated_by=admin_id
        else:
            db.session.add(SchoolSetting(school_id=school_id,setting_key=setting_key,
                                          setting_value=str(value),updated_at=now,updated_by=admin_id))
    db.session.commit()

def _finite(value):
    """A number typed into a form, refused (ValueError) unless it is an ordinary number.

    Python reads "nan" and "inf" as numbers. NaN passes every "is it above the maximum?" test
    (nothing is greater or smaller than NaN), so without this it would be saved and turn a whole
    term result into NaN.
    """
    if not math.isfinite(value):
        raise ValueError('not a finite number')
    return value

def _result_term(raw):
    """The term a hand-entered result belongs to, written the way the term report reads it.

    Blank means the whole session. The older "1st Term" style is accepted and converted, because a
    result filed under a name the report never asks for would silently fall out of the term result.
    Returns None if it is not a term at all.
    """
    text=' '.join(str(raw or '').split())
    if not text: return 'Full Session'
    aliases={'1st term':'First Term','2nd term':'Second Term','3rd term':'Third Term'}
    for name in [*ACADEMIC_TERMS,'Full Session']:
        aliases[name.casefold()]=name
    return aliases.get(text.casefold())

def _term_raw_sums(student_ids, session_id, term, subject_id=None):
    """The raw marks, added up, for each student and subject in one term and one session.

    Returns ``{'exam': {...}, 'test': {...}, 'assignment': {...}, 'project': {...}}`` where each
    inner dict maps ``(student_id, subject_id)`` to ``(marks scored, marks available)``. A pair
    with nothing of that kind recorded is simply absent. This is the ONE place the rules for what
    counts are written:

    * practice tests never contribute (a self-study tool, not part of the official record);
    * work that was removed does not count, and neither does a mark on work with no maximum:
      there is nothing to scale it against, and adding it to the top of the fraction without
      adding to the bottom would inflate the whole share;
    * a mark that was never entered (blank) does not count.

    One query for each kind of work, for however many students are asked about: a class of forty
    costs the same handful of queries as one student.
    """
    student_ids=list(student_ids)
    if not student_ids: return {'exam':{},'test':{},'assignment':{},'project':{}}

    def result_sums(kind, component_prefix):
        stmt=(select(SchoolStudentResult.student_id,SchoolStudentResult.subject_id,
                     func.coalesce(func.sum(SchoolStudentResult.score),0),
                     func.coalesce(func.sum(SchoolStudentResult.max_score),0))
            .select_from(SchoolStudentResult)
            .outerjoin(SchoolAssessment,SchoolAssessment.id==SchoolStudentResult.assessment_id)
            .where(SchoolStudentResult.student_id.in_(student_ids),
                   SchoolStudentResult.session_id==session_id,
                   func.coalesce(SchoolStudentResult.term,'Full Session')==term,
                   SchoolStudentResult.score.is_not(None),
                   or_(SchoolAssessment.assessment_type==kind,
                       and_(SchoolStudentResult.assessment_id.is_(None),
                            SchoolStudentResult.component_name.startswith(component_prefix))))
            .group_by(SchoolStudentResult.student_id,SchoolStudentResult.subject_id))
        if subject_id is not None: stmt=stmt.where(SchoolStudentResult.subject_id==subject_id)
        return {(sid,subj):(score,available) for sid,subj,score,available in tuples(stmt)}

    assignment_stmt=(select(AssignmentStudent.student_id,SchoolAssignment.subject_id,
                            func.coalesce(func.sum(AssignmentStudent.score),0),
                            func.coalesce(func.sum(func.coalesce(AssignmentStudent.max_score,SchoolAssignment.max_score)),0))
        .select_from(AssignmentStudent)
        .join(SchoolAssignment,SchoolAssignment.id==AssignmentStudent.assignment_id)
        .where(AssignmentStudent.student_id.in_(student_ids),
               SchoolAssignment.session_id==session_id,SchoolAssignment.term==term,
               SchoolAssignment.active==1,
               func.coalesce(AssignmentStudent.max_score,SchoolAssignment.max_score)>0,
               AssignmentStudent.score.is_not(None))
        .group_by(AssignmentStudent.student_id,SchoolAssignment.subject_id))
    project_stmt=(select(ProjectStudent.student_id,SchoolProject.subject_id,
                         func.coalesce(func.sum(ProjectStudent.score),0),
                         func.coalesce(func.sum(SchoolProject.max_score),0))
        .select_from(ProjectStudent)
        .join(SchoolProject,SchoolProject.id==ProjectStudent.project_id)
        .where(ProjectStudent.student_id.in_(student_ids),
               SchoolProject.session_id==session_id,SchoolProject.term==term,
               SchoolProject.active==1,SchoolProject.max_score>0,
               ProjectStudent.score.is_not(None))
        .group_by(ProjectStudent.student_id,SchoolProject.subject_id))
    if subject_id is not None:
        assignment_stmt=assignment_stmt.where(SchoolAssignment.subject_id==subject_id)
        project_stmt=project_stmt.where(SchoolProject.subject_id==subject_id)
    return {
        'exam':result_sums('examination','Exam'),
        'test':result_sums('test','Test'),
        'assignment':{(sid,subj):(score,available) for sid,subj,score,available in tuples(assignment_stmt)},
        'project':{(sid,subj):(score,available) for sid,subj,score,available in tuples(project_stmt)},
    }

def _assemble_term_report(weights, exam_raw, test_raw, assignment_raw, project_raw):
    """Turn the four raw (scored, available) pairs of one student's subject into Exam(60) + CA(40)."""
    def scaled(raw_score, raw_max, cap):
        if not raw_max: return 0.0
        return min(cap,round(raw_score/raw_max*cap,2))

    exam_score=scaled(exam_raw[0],exam_raw[1],EXAM_MAX_SCORE)
    test_score=scaled(test_raw[0],test_raw[1],weights['test'])
    assignment_score=scaled(assignment_raw[0],assignment_raw[1],weights['assignment'])
    project_score=scaled(project_raw[0],project_raw[1],weights['project'])
    ca_score=round(test_score+assignment_score+project_score,2)
    return {
        'exam_score':exam_score,'exam_max':EXAM_MAX_SCORE,
        'test_score':test_score,'test_max':weights['test'],
        'assignment_score':assignment_score,'assignment_max':weights['assignment'],
        'project_score':project_score,'project_max':weights['project'],
        'ca_score':ca_score,'ca_max':CA_MAX_SCORE,
        'total_score':round(exam_score+ca_score,2),'total_max':EXAM_MAX_SCORE+CA_MAX_SCORE,
        'has_data':bool(exam_raw[1] or test_raw[1] or assignment_raw[1] or project_raw[1]),
    }

def _term_reports_bulk(student_ids, session_id, term, subject_id=None):
    """Exam(60) + CA(40) = 100 for every student and subject that has anything recorded.

    ``{(student_id, subject_id): report}``. Multiple items in the same category (two tests, say)
    are combined by summing their raw score and raw maximum together, then scaling the combined
    total to that category's share of CA_MAX_SCORE - never simply added on top of each other, so
    one extra test never lets a subject exceed its 100-mark ceiling.
    """
    weights=_ca_weights()
    sums=_term_raw_sums(student_ids,session_id,term,subject_id)
    none=(0,0)
    keys=set().union(*(set(v) for v in sums.values()))
    return {key:_assemble_term_report(weights,sums['exam'].get(key,none),sums['test'].get(key,none),
                                      sums['assignment'].get(key,none),sums['project'].get(key,none))
            for key in keys}

def _term_subject_report(student_id, session_id, term, subject_id):
    """A student's Exam(60) + CA(40) = 100 breakdown for one subject in one term.

    The same arithmetic as ``_term_reports_bulk``, for one student and one subject. A subject with
    nothing recorded gives all zeros and ``has_data`` False.
    """
    found=_term_reports_bulk([student_id],session_id,term,subject_id).get((student_id,subject_id))
    if found is not None: return found
    none=(0,0)
    return _assemble_term_report(_ca_weights(),none,none,none,none)

def _student_term_subjects(student_id, session_id, term):
    """Every subject this student has any exam/test/assignment/project record
    for in this session+term, so the summary table only lists subjects that
    actually have something recorded."""
    subject_ids=set()
    for row in tuples(select(SchoolStudentResult.subject_id).distinct()
            .where(SchoolStudentResult.student_id==student_id,
                   SchoolStudentResult.session_id==session_id,
                   func.coalesce(SchoolStudentResult.term,'Full Session')==term)):
        subject_ids.add(row[0])
    for row in tuples(select(SchoolAssignment.subject_id).distinct()
            .join(AssignmentStudent,AssignmentStudent.assignment_id==SchoolAssignment.id)
            .where(AssignmentStudent.student_id==student_id,
                   SchoolAssignment.session_id==session_id,SchoolAssignment.term==term)):
        subject_ids.add(row[0])
    for row in tuples(select(SchoolProject.subject_id).distinct()
            .join(ProjectStudent,ProjectStudent.project_id==SchoolProject.id)
            .where(ProjectStudent.student_id==student_id,
                   SchoolProject.session_id==session_id,SchoolProject.term==term)):
        subject_ids.add(row[0])
    if not subject_ids: return []
    names={sid:name for sid,name in tuples(select(SchoolSubject.id,SchoolSubject.name)
                                           .where(SchoolSubject.id.in_(subject_ids)))}
    reports=[]
    for sid in subject_ids:
        report=_term_subject_report(student_id,session_id,term,sid)
        if report['has_data']:
            reports.append({'subject_id':sid,'subject_name':names.get(sid,'—'),**report})
    reports.sort(key=lambda r:r['subject_name'])
    return reports

def _student_term_periods(student_id):
    """Every (session, term) combination this student has any record in,
    newest first."""
    periods=set()
    for sid,term in tuples(select(SchoolStudentResult.session_id,
                                  func.coalesce(SchoolStudentResult.term,'Full Session'))
            .where(SchoolStudentResult.student_id==student_id,
                   SchoolStudentResult.session_id.is_not(None)).distinct()):
        periods.add((sid,term))
    for sid,term in tuples(select(SchoolAssignment.session_id,SchoolAssignment.term)
            .join(AssignmentStudent,AssignmentStudent.assignment_id==SchoolAssignment.id)
            .where(AssignmentStudent.student_id==student_id,
                   SchoolAssignment.session_id.is_not(None),SchoolAssignment.term.is_not(None)).distinct()):
        periods.add((sid,term))
    for sid,term in tuples(select(SchoolProject.session_id,SchoolProject.term)
            .join(ProjectStudent,ProjectStudent.project_id==SchoolProject.id)
            .where(ProjectStudent.student_id==student_id,
                   SchoolProject.session_id.is_not(None),SchoolProject.term.is_not(None)).distinct()):
        periods.add((sid,term))
    if not periods: return []
    session_names={row[0]:row[1] for row in tuples(select(AcademicSession.id,AcademicSession.name)
                                                    .where(AcademicSession.id.in_({p[0] for p in periods})))}
    term_order={t:i for i,t in enumerate(ACADEMIC_TERMS)}
    out=[{'session_id':sid,'session_name':session_names.get(sid,'—'),'term':term}
         for sid,term in periods]
    out.sort(key=lambda p:(-p['session_id'],-term_order.get(p['term'],-1)))
    return out

def _school_assessment_new(kind):
    classes,subjects,_,session_row=_school_form_context()
    if request.method=='POST':
        title=request.form.get('title','').strip(); instructions=request.form.get('instructions','').strip()
        term=request.form.get('term','').strip() or None
        try: cid=int(request.form.get('class_id','')); sid=int(request.form.get('subject_id','')); duration=max(1,int(request.form.get('duration_minutes','30') or 30)); session_id=int(request.form.get('session_id') or session_row['id'])
        except: cid=sid=session_id=0; duration=30
        errors=[]
        if not title: errors.append('Title is required.')
        if not _school_pair_allowed(current_admin()['id'],cid,sid): errors.append('Select a class and subject within your authorised scope.')
        if not session_id: errors.append('An academic session is required.')
        if kind!='practice' and term not in ACADEMIC_TERMS: errors.append('Select a valid term.')
        elif kind=='practice' and term and term not in ACADEMIC_TERMS: errors.append('Select a valid term.')
        if errors: return render_template('school_assessment_form.html',mode='new',assessment_type=kind,assessment=request.form,classes=classes,subjects=subjects,sessions=_school_sessions(),terms=ACADEMIC_TERMS,errors=errors)
        now=datetime.now(timezone.utc).isoformat()
        assessment=SchoolAssessment(assessment_type=kind,title=title,instructions=instructions,
                                    class_id=cid,subject_id=sid,session_id=session_id,
                                    duration_minutes=duration,question_count=0,active=0,
                                    created_by=current_admin()['id'],created_at=now,
                                    term=term or 'Full Session')
        db.session.add(assessment); db.session.flush(); aid=assessment.id
        db.session.commit(); audit_log('school_assessment_created','school',kind,aid,{'class_id':cid,'subject_id':sid,'term':term}); flash(f'{_school_assessment_title(kind)} item created. Add questions next.','success'); return redirect(url_for('admin_school_assessment_detail',assessment_id=aid))
    return render_template('school_assessment_form.html',mode='new',assessment_type=kind,assessment=None,classes=classes,subjects=subjects,sessions=_school_sessions(),terms=ACADEMIC_TERMS,errors=[])

def _result_with_context(result_id, with_subject=True):
    """A result row plus the student, class and (optionally) subject names."""
    cols=[SchoolStudentResult,Student.first_name,Student.last_name,
          SchoolClass.id.label('class_id'),SchoolClass.name.label('class_name')]
    extra=('first_name','last_name','class_id','class_name')
    if with_subject:
        cols.append(SchoolSubject.name.label('subject_name'))
        extra=extra+('subject_name',)
    stmt=(select(*cols)
          .join(Student,Student.id==SchoolStudentResult.student_id))
    if with_subject:
        stmt=stmt.join(SchoolSubject,SchoolSubject.id==SchoolStudentResult.subject_id)
    stmt=(stmt.outerjoin(StudentEnrolment,
                         and_(StudentEnrolment.student_id==SchoolStudentResult.student_id,
                              StudentEnrolment.session_id==SchoolStudentResult.session_id,
                              StudentEnrolment.active==1))
              .outerjoin(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
              .where(SchoolStudentResult.id==result_id))
    row=one(stmt)
    return _flatten(row,'SchoolStudentResult',*extra) if row else None

ACADEMIC_HISTORY_LEVELS={'Primary 1','Primary 2','Primary 3','Primary 4','Primary 5',
                         'Primary 6','JSS 1','JSS 2','JSS 3','SSS 1','SSS 2','SSS 3'}

def _mark_latest_history_current(sid):
    """The latest-dated history entry is the current one.

    Staff often enter older records after newer ones, so currency is recomputed
    from the dates rather than assumed from insertion order.
    """
    latest=one_scalar(select(StudentEnrollmentHistory.id)
        .where(StudentEnrollmentHistory.student_id==sid)
        .order_by(func.coalesce(StudentEnrollmentHistory.enrolled_at,'').desc(),
                  StudentEnrollmentHistory.id.desc()).limit(1))
    if latest is not None:
        db.session.execute(sa_update(StudentEnrollmentHistory)
            .where(StudentEnrollmentHistory.student_id==sid)
            .values(active=sa.case((StudentEnrollmentHistory.id==latest,1),else_=0)))

def _sync_enrolment_for_history(sid, class_id, session_id, enrolled_at):
    """Keep the live enrolment in step with an academic history entry."""
    existing=db.session.scalars(select(StudentEnrolment).where(
        StudentEnrolment.student_id==sid,
        StudentEnrolment.session_id==session_id)).first()
    if existing:
        existing.class_id=class_id; existing.enrolled_at=enrolled_at; existing.active=1
    else:
        db.session.add(StudentEnrolment(student_id=sid,class_id=class_id,
                                        session_id=session_id,enrolled_at=enrolled_at,active=1))

def _promotion_classes(include_inactive=False):
    """School classes are authoritative.

    include_inactive=True is intentionally supported because an inactive
    class remains part of the school's configured structure.
    """
    stmt=select(SchoolClass)
    if not include_inactive:
        stmt=stmt.where(SchoolClass.active==1)
    return db.session.scalars(stmt.order_by(SchoolClass.level_order,SchoolClass.id)).all()

def _promotion_progressions():
    FromClass=sa.orm.aliased(SchoolClass)
    ToClass=sa.orm.aliased(SchoolClass)
    return [_flatten(r,'SchoolClassProgression','from_class_name','from_stage','from_active',
                     'to_class_name','to_stage','to_active') for r in all_rows(
        select(SchoolClassProgression,
               FromClass.name.label('from_class_name'),FromClass.stage.label('from_stage'),
               FromClass.active.label('from_active'),
               ToClass.name.label('to_class_name'),ToClass.stage.label('to_stage'),
               ToClass.active.label('to_active'))
        .join(FromClass,FromClass.id==SchoolClassProgression.from_class_id)
        .outerjoin(ToClass,ToClass.id==SchoolClassProgression.to_class_id)
        .where(SchoolClassProgression.active==1)
        .order_by(FromClass.level_order,FromClass.id))]

def _promotion_next_class(class_id):
    """The explicitly configured destination for a class.

    level_order is NEVER used to infer the next class.
    """
    return one(select(SchoolClassProgression.to_class_id,
                      SchoolClass.name.label('to_class_name'),
                      SchoolClass.active.label('to_class_active'))
               .select_from(SchoolClassProgression)
               .outerjoin(SchoolClass,SchoolClass.id==SchoolClassProgression.to_class_id)
               .where(SchoolClassProgression.from_class_id==class_id,
                      SchoolClassProgression.active==1)
               .order_by(SchoolClassProgression.id.desc()).limit(1))

def _promotion_current_enrolments(session_id):
    return all_rows(
        select(StudentEnrolment.id.label('enrolment_id'),StudentEnrolment.student_id,
               StudentEnrolment.class_id,Student.admission_no,Student.first_name,
               Student.middle_name,Student.last_name,
               Student.active.label('student_active'),
               SchoolClass.name.label('class_name'),SchoolClass.stage,
               SchoolClass.level_order)
        .join(Student,Student.id==StudentEnrolment.student_id)
        .join(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
        .where(StudentEnrolment.session_id==session_id,StudentEnrolment.active==1,
               Student.active==1)
        .order_by(SchoolClass.level_order,SchoolClass.id,
                  Student.last_name,Student.first_name))

def _promotion_run_has_uncommitted(from_session_id, to_session_id):
    return one(select(AcademicPromotionRun.id,AcademicPromotionRun.status)
               .where(AcademicPromotionRun.from_session_id==from_session_id,
                      AcademicPromotionRun.to_session_id==to_session_id,
                      AcademicPromotionRun.status.in_(('draft','approved')))
               .order_by(AcademicPromotionRun.id.desc()).limit(1))

def _promotion_target(class_id):
    return obj(SchoolClass, class_id) if class_id else None

def _promotion_target_status(class_id):
    target=_promotion_target(class_id)
    if not target:
        return False, "Destination class does not exist."
    if int(target.active or 0) != 1:
        return False, f"Destination class '{target.name}' is currently inactive."
    return True, None

def _promotion_existing_target_enrolment(student_id, session_id):
    """Because student_enrolments is unique per student/session, ANY existing
    row must be treated as a conflict."""
    return db.session.scalars(select(StudentEnrolment).where(
        StudentEnrolment.student_id==student_id,
        StudentEnrolment.session_id==session_id).limit(1)).first()

def _promotion_validate_run(run_id):
    """Validate an entire run immediately before approval or commit.

    Returns (True, []) or (False, [error, ...]).
    """
    run=obj(AcademicPromotionRun, run_id)
    if not run:
        return False, ["Promotion run not found."]

    items=db.session.scalars(select(AcademicPromotionItem)
        .where(AcademicPromotionItem.run_id==run_id)
        .order_by(AcademicPromotionItem.id)).all()
    if not items:
        return False, ["Promotion run contains no students."]

    errors=[]
    for item in items:
        decision=(item.decision or item.action or 'promote').strip().lower()

        if decision not in PROMOTION_DECISIONS:
            errors.append(f"Student ID {item.student_id}: invalid decision '{decision}'.")
            continue

        source=obj(SchoolClass, item.source_class_id)
        if not source:
            errors.append(f"Student ID {item.student_id}: source class no longer exists.")
            continue

        if decision=='promote':
            destination_id=item.final_class_id or item.proposed_class_id
            if not destination_id:
                errors.append(f"Student ID {item.student_id}: no progression is configured for "
                              f"'{source.name}'. Review Required.")
                continue
            ok,reason=_promotion_target_status(destination_id)
            if not ok:
                errors.append(f"Student ID {item.student_id}: {reason}")

        elif decision=='repeat':
            ok,reason=_promotion_target_status(item.source_class_id)
            if not ok:
                errors.append(f"Student ID {item.student_id}: {reason}")

        elif decision=='transfer':
            destination_id=item.final_class_id
            if not destination_id:
                errors.append(f"Student ID {item.student_id}: Transfer requires a destination class.")
                continue
            ok,reason=_promotion_target_status(destination_id)
            if not ok:
                errors.append(f"Student ID {item.student_id}: {reason}")

        elif decision in ('hold','withdraw','graduate'):
            # These decisions intentionally create no new enrolment.
            continue

        # Anything that creates a target-session enrolment needs that session
        # to be free for this student.
        if decision in ('promote','repeat','transfer'):
            existing=_promotion_existing_target_enrolment(item.student_id, run.to_session_id)
            if existing:
                errors.append(f"Student ID {item.student_id}: target session already has "
                              f"enrolment {existing.id}.")

    return len(errors)==0, errors

def _promotion_audit(action, entity_id, details):
    try:
        audit_log(
            action,
            "school",
            "academic_promotion",
            entity_id,
            details
        )
    except Exception:
        pass


def archive_students(student_ids, reason, admin_id):
    """Take these students out of every current list, count and sign-in, keeping their record.

    The caller decides which students it may touch and commits. Already archived students are left as
    they are, so the first archive's date, reason and administrator are never overwritten. Returns the
    ids that were archived by this call.
    """
    ids = list(student_ids)
    if not ids:
        return []
    to_archive = [s.id for s in db.session.scalars(select(Student).where(
        Student.id.in_(ids), Student.archived_at.is_(None))).all()]
    if not to_archive:
        return []
    now = datetime.now(timezone.utc).isoformat()
    db.session.execute(sa_update(Student).where(Student.id.in_(to_archive)).values(
        active=0, archived_at=now, archived_by_admin_id=admin_id, archive_reason=reason))
    # Ending their open sessions makes the archive take effect on the presence board straight away.
    db.session.execute(sa_update(PresenceSession).where(
        PresenceSession.account_type == 'student', PresenceSession.account_id.in_(to_archive)).values(active=0))
    return to_archive


_SESSION_NAME = re.compile(r'^\s*(\d{4})/(\d{4})\s*$')


def is_earlier_session_name(name, current_name):
    """True when ``name`` is a 'YYYY/YYYY' session that starts before the current session's start year.

    Anything else (another format, the current year, a later year, or no current session to compare
    with) is False, so an import can only ever create a session that is genuinely in the past.
    """
    found = _SESSION_NAME.match(name or '')
    current = _SESSION_NAME.match(current_name or '')
    if not found or not current:
        return False
    start, end = int(found.group(1)), int(found.group(2))
    return end == start + 1 and start < int(current.group(1))


# The permission a school can give any role to run the three bulk imports (students, enrolment history and
# past results). It stands in for each step's own permission, so a staff member needs one or the other.
BULK_IMPORT_PERMISSION = 'school.bulk_import'


def import_allowed(specific_permission):
    """True when the current administrator holds this step's own permission, or the bulk-import permission."""
    me = current_admin()
    return bool(me and (admin_has_permission(me['id'], specific_permission)
                        or admin_has_permission(me['id'], BULK_IMPORT_PERMISSION)))


def import_permission_required(specific_permission):
    """Route guard for an import step: the step's own permission or the bulk-import permission."""
    def decorate(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not import_allowed(specific_permission):
                return admin_access_error(specific_permission)
            return fn(*args, **kwargs)
        return wrapper
    return decorate


def offer_subject(class_id, subject_id, admin_id):
    """Offer a subject to a class if it is not offered already.

    Saving work for a subject in a class is what makes that subject offered to the class, so a subject created
    on the Subjects page can always be chosen by the forms that need it. An existing offer is left as it is.
    """
    if not class_id or not subject_id:
        return
    exists = one_scalar(select(ClassSubject.id).where(
        ClassSubject.class_id == class_id, ClassSubject.subject_id == subject_id))
    if exists:
        return
    db.session.add(ClassSubject(class_id=class_id, subject_id=subject_id, locked=0, final_locked=0,
                                created_by=admin_id, created_at=datetime.now(timezone.utc).isoformat()))


def whole_class_ids(students, class_id):
    """Every student in a class from a form's student list: what "issue to the whole class" selects."""
    return [s['id'] for s in students if s['class_id'] == class_id]


def assignment_has_marks(assignment_id):
    """True when any student has a score, a submission, or a status other than undone for this assignment."""
    return bool(one_scalar(select(func.count()).select_from(AssignmentStudent).where(
        AssignmentStudent.assignment_id == assignment_id,
        sa.or_(AssignmentStudent.score.is_not(None), AssignmentStudent.submitted_at.is_not(None),
               AssignmentStudent.status != 'undone')), 0))


def project_has_marks(project_id):
    """True when any student has a score, or a status other than not_done, for this project."""
    return bool(one_scalar(select(func.count()).select_from(ProjectStudent).where(
        ProjectStudent.project_id == project_id,
        sa.or_(ProjectStudent.score.is_not(None), ProjectStudent.status != 'not_done')), 0))

