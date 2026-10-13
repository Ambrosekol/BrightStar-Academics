"""The student portal: dashboard, password change, assignments (written and
CBT-style quiz), and school assessments (tests/practice/examinations).
"""

from datetime import datetime, timedelta, timezone

import random

import sqlalchemy as sa
from flask import abort, flash, jsonify, make_response, redirect, render_template, request, session, url_for
from sqlalchemy import or_, func, select, update as sa_update
from werkzeug.security import check_password_hash, generate_password_hash

from app import app, csrf_token, _release_due_school_results, _student_with_enrolment
from blueprints.school.report_card_data import student_periods
from core.entrance import apply_connectivity_grace, remaining
from models import (
    AssignmentQuestion, AssignmentStudent, ProjectStudent,
    SchoolAssessment, SchoolAssessmentAnswer, SchoolAssessmentAttempt,
    SchoolAssessmentAttemptQuestion, SchoolAssignment, SchoolAssignmentAnswer,
    SchoolAssignmentAttempt, SchoolAssignmentAttemptQuestion, SchoolClass,
    SchoolNotification, SchoolProject, SchoolStudentResult,
    SchoolSubject, Student, db,
)
from core.accounts import _clear_identity_sessions
from core.db_helpers import all_rows, insert_stmt, one, one_scalar, tuples, _flatten
from core.security import audit_log, csrf_protect
from core.theory_marking import OBJECTIVE, THEORY, THEORY_ANSWER_LIMIT
from core import exam_integrity
from core.school_structure import class_is_early_years, student_department_filter
from core.session_guard import refresh_password_stamp
from blueprints.student_portal.helpers import (
    _student_assessment_context, _student_practice_context,
    student_assessment_grade, student_required,
)


@app.route('/student/password',methods=['GET','POST'])
@student_required
@csrf_protect
def student_password_change():
    """Allow an authenticated student to replace a temporary password."""
    sid=session.get('student_id')
    student=db.session.scalars(select(Student).where(
        Student.id==sid,Student.active==1,Student.account_active==1)).first()
    if not student:
        _clear_identity_sessions(); return redirect(url_for('login'))

    errors=[]
    if request.method=='POST':
        current_password=request.form.get('current_password','')
        new_password=request.form.get('new_password','')
        confirm_password=request.form.get('confirm_password','')
        if not check_password_hash(student['login_password_hash'] or '',current_password):
            errors.append('Your current password is incorrect.')
        from core.passwords import password_problem
        problem=password_problem(new_password,usernames=(student['login_username'],student['admission_no']))
        if problem:
            errors.append(problem)
        if new_password!=confirm_password:
            errors.append('The new password and confirmation do not match.')
        if not errors:
            db.session.execute(sa_update(Student).where(Student.id==sid).values(
                login_password_hash=generate_password_hash(new_password),password_must_change=0))
            db.session.commit()
            refresh_password_stamp()   # this sign-in stays; every other one made with the old password ends
            flash('Your password has been changed successfully.','success')
            return redirect(url_for('student_dashboard'))
    return render_template('student_password.html',student=student,errors=errors)

@app.route('/student/dashboard')
def student_dashboard():
    sid=session.get('student_id')
    if not sid: return redirect(url_for('login'))
    # The helper already restricts to active students with an active account.
    student=_student_with_enrolment(sid)
    if not student or class_is_early_years(student['class_id']):
        # Crèche and Nursery pupils do not use the portal; their parents do.
        _clear_identity_sessions(); return redirect(url_for('login'))
    if student['password_must_change']: return redirect(url_for('student_password_change'))
    _release_due_school_results()
    # Undated work sorts last, then by due date.
    undated=sa.case((or_(SchoolAssignment.due_date.is_(None),SchoolAssignment.due_date==''),1),else_=0)
    assignments=all_rows(
        select(SchoolAssignment.id,SchoolAssignment.title,SchoolAssignment.instructions,
               SchoolAssignment.due_date,SchoolAssignment.date_given,
               SchoolAssignment.assignment_type,SchoolAssignment.timing_mode,
               SchoolAssignment.time_limit_seconds,SchoolAssignment.per_question_seconds,
               AssignmentStudent.status,AssignmentStudent.score,AssignmentStudent.max_score,
               SchoolSubject.name.label('subject_name'))
        .select_from(AssignmentStudent)
        .join(SchoolAssignment,SchoolAssignment.id==AssignmentStudent.assignment_id)
        .join(SchoolSubject,SchoolSubject.id==SchoolAssignment.subject_id)
        .where(AssignmentStudent.student_id==sid,SchoolAssignment.active==1)
        .order_by(undated,SchoolAssignment.due_date,SchoolAssignment.id.desc()).limit(20))
    undated_p=sa.case((or_(SchoolProject.due_date.is_(None),SchoolProject.due_date==''),1),else_=0)
    projects=all_rows(
        select(SchoolProject.id,SchoolProject.title,SchoolProject.instructions,
               SchoolProject.date_given,SchoolProject.due_date,SchoolProject.max_score,
               ProjectStudent.status,ProjectStudent.score,ProjectStudent.remark,
               SchoolSubject.name.label('subject_name'))
        .select_from(ProjectStudent)
        .join(SchoolProject,SchoolProject.id==ProjectStudent.project_id)
        .join(SchoolSubject,SchoolSubject.id==SchoolProject.subject_id)
        .where(ProjectStudent.student_id==sid,SchoolProject.active==1)
        .order_by(undated_p,SchoolProject.due_date,SchoolProject.id.desc()).limit(20))
    notifications=db.session.scalars(select(SchoolNotification).where(
        SchoolNotification.recipient_type=='student',SchoolNotification.recipient_id==sid)
        .order_by(SchoolNotification.id.desc()).limit(12)).all()
    results=all_rows(
        select(SchoolStudentResult.id,SchoolStudentResult.score,SchoolStudentResult.max_score,
               SchoolStudentResult.term,SchoolStudentResult.status,
               SchoolSubject.name.label('subject_name'),
               SchoolAssessment.title.label('assessment_title'))
        .join(SchoolSubject,SchoolSubject.id==SchoolStudentResult.subject_id)
        .outerjoin(SchoolAssessment,SchoolAssessment.id==SchoolStudentResult.assessment_id)
        .where(SchoolStudentResult.student_id==sid,SchoolStudentResult.status=='released',
               or_(SchoolAssessment.assessment_type.is_(None),SchoolAssessment.assessment_type!='practice'))
        .order_by(SchoolStudentResult.id.desc()).limit(10))
    assessments=[]
    if student['class_id'] and student['session_id']:
        kind=sa.case((SchoolAssessment.assessment_type=='test',1),
                     (SchoolAssessment.assessment_type=='examination',2),else_=3)
        assessments=all_rows(
            select(SchoolAssessment.id,SchoolAssessment.assessment_type,SchoolAssessment.title,
                   SchoolAssessment.instructions,SchoolAssessment.duration_minutes,
                   SchoolAssessment.question_count,SchoolSubject.name.label('subject_name'))
            .join(SchoolSubject,SchoolSubject.id==SchoolAssessment.subject_id)
            .where(SchoolAssessment.class_id==student['class_id'],SchoolAssessment.active==1,
                   student_department_filter(student,SchoolSubject.departments),
                   SchoolAssessment.question_count>0,SchoolAssessment.assessment_type!='practice',
                   or_(SchoolAssessment.session_id==student['session_id'],
                       SchoolAssessment.session_id.is_(None)))
            .order_by(kind,SchoolAssessment.id.desc()).limit(12))
    counts={k:sum(1 for a in assessments if a['assessment_type']==k) for k in ('practice','test','examination')}
    counts['practice']=(one_scalar(select(func.count()).select_from(SchoolAssessment)
        .where(SchoolAssessment.assessment_type=='practice',SchoolAssessment.active==1,
               SchoolAssessment.question_count>0,
               SchoolAssessment.class_id==student['class_id']),0) if student['class_id'] else 0)
    return render_template('student_dashboard.html',student=student,report_periods=student_periods(sid),assignments=assignments,projects=projects,notifications=notifications,results=results,assessments=assessments,assessment_counts=counts)

@app.route('/student/assignments/<int:assignment_id>',methods=['GET','POST'])
@student_required
@csrf_protect
def student_assignment_detail(assignment_id):
    sid=session.get('student_id')
    # The assignment_students row is what authorises access.
    raw=one(select(SchoolAssignment,SchoolSubject.name.label('subject_name'),
                   SchoolClass.name.label('class_name'),AssignmentStudent.status,
                   AssignmentStudent.score,AssignmentStudent.max_score,
                   AssignmentStudent.started_at,AssignmentStudent.submitted_at)
            .select_from(AssignmentStudent)
            .join(SchoolAssignment,SchoolAssignment.id==AssignmentStudent.assignment_id)
            .join(SchoolSubject,SchoolSubject.id==SchoolAssignment.subject_id)
            .join(SchoolClass,SchoolClass.id==SchoolAssignment.class_id)
            .where(AssignmentStudent.student_id==sid,SchoolAssignment.id==assignment_id,
                   SchoolAssignment.active==1))
    if not raw: abort(404)
    a=_flatten(raw,'SchoolAssignment','subject_name','class_name','status','score',
               'max_score','started_at','submitted_at')
    if request.method=='POST' and request.form.get('action')=='start':
        if a['assignment_type']!='quiz': return redirect(url_for('student_assignment_detail',assignment_id=assignment_id))
        existing=db.session.scalars(select(SchoolAssignmentAttempt).where(
            SchoolAssignmentAttempt.assignment_id==assignment_id,
            SchoolAssignmentAttempt.student_id==sid)).first()
        if existing and existing.status=='submitted': return redirect(url_for('student_assignment_detail',assignment_id=assignment_id))
        if existing and existing.status=='active':
            # The student started but never finished: resume at the first
            # unanswered question instead of falling through to a page with
            # no action, which is what silently made "Continue" a no-op.
            answered=one_scalar(select(func.count(func.distinct(SchoolAssignmentAnswer.question_id)))
                .where(SchoolAssignmentAnswer.attempt_id==existing.id),0)
            total_qs=one_scalar(select(func.count()).select_from(SchoolAssignmentAttemptQuestion)
                .where(SchoolAssignmentAttemptQuestion.attempt_id==existing.id),0)
            session['assignment_attempt_id']=existing.id
            session['assignment_question_index']=min(answered+1,max(total_qs,1))
            session['assignment_question_started_at']=datetime.now(timezone.utc).isoformat()
            return redirect(url_for('student_assignment_take',assignment_id=assignment_id))
        if not existing:
            qs=db.session.scalars(select(AssignmentQuestion)
                .where(AssignmentQuestion.assignment_id==assignment_id)
                .order_by(AssignmentQuestion.sort_order,AssignmentQuestion.id)).all()
            if not qs: return render_template('student_assignment_detail.html',assignment=a,questions=[],error='This CBT assignment has not been prepared with questions yet.')
            now=datetime.now(timezone.utc)
            expires=(now+timedelta(seconds=a['time_limit_seconds'])).isoformat() if a['timing_mode']=='overall' and a['time_limit_seconds'] else None
            attempt=SchoolAssignmentAttempt(assignment_id=assignment_id,student_id=sid,
                started_at=now.isoformat(),expires_at=expires,status='active')
            db.session.add(attempt); db.session.flush()
            # Freeze the questions so later edits cannot change a live attempt.
            for i,q in enumerate(qs,1):
                db.session.add(SchoolAssignmentAttemptQuestion(
                    attempt_id=attempt.id,question_id=q.id,question_order=i,
                    question_text=q.question_text,option_a=q.option_a,option_b=q.option_b,
                    option_c=q.option_c,option_d=q.option_d,instruction=q.instruction,
                    image_path=q.image_path,correct_option=q.correct_option,points=q.points))
            db.session.execute(sa_update(AssignmentStudent)
                .where(AssignmentStudent.assignment_id==assignment_id,
                       AssignmentStudent.student_id==sid)
                .values(status='in_progress',started_at=now.isoformat()))
            db.session.commit()
            session['assignment_attempt_id']=attempt.id
            session['assignment_question_index']=1
            session['assignment_question_started_at']=now.isoformat()
            return redirect(url_for('student_assignment_take',assignment_id=assignment_id))
    questions=db.session.scalars(select(AssignmentQuestion)
        .where(AssignmentQuestion.assignment_id==assignment_id)
        .order_by(AssignmentQuestion.sort_order,AssignmentQuestion.id)).all() if a['assignment_type']=='quiz' else []
    return render_template('student_assignment_detail.html',student_id=sid,assignment=a,questions=questions,error=None)

@app.route('/student/assignments/<int:assignment_id>/take',methods=['GET','POST'])
@student_required
@csrf_protect
def student_assignment_take(assignment_id):
    sid=session.get('student_id')
    attempt=db.session.scalars(select(SchoolAssignmentAttempt).where(
        SchoolAssignmentAttempt.assignment_id==assignment_id,
        SchoolAssignmentAttempt.student_id==sid,
        SchoolAssignmentAttempt.status=='active')).first()
    a=db.session.scalars(select(SchoolAssignment).where(
        SchoolAssignment.id==assignment_id,SchoolAssignment.active==1)).first()
    if not attempt or not a: return redirect(url_for('student_assignment_detail',assignment_id=assignment_id))
    qs=db.session.scalars(select(SchoolAssignmentAttemptQuestion)
        .where(SchoolAssignmentAttemptQuestion.attempt_id==attempt.id)
        .order_by(SchoolAssignmentAttemptQuestion.question_order)).all()
    answers={qid:opt for qid,opt in tuples(
        select(SchoolAssignmentAnswer.question_id,SchoolAssignmentAnswer.option_index)
        .where(SchoolAssignmentAnswer.attempt_id==attempt.id))}
    if not qs: return redirect(url_for('student_assignment_detail',assignment_id=assignment_id))
    try: idx=max(1,min(len(qs),int(session.get('assignment_question_index',1))))
    except (TypeError,ValueError): idx=1
    now=datetime.now(timezone.utc); timed_out=False
    if attempt.expires_at:
        try: timed_out=now>=datetime.fromisoformat(attempt.expires_at)
        except ValueError: timed_out=False
    current_started=session.get('assignment_question_started_at')
    if a.timing_mode=='per_question' and current_started:
        try: timed_out=timed_out or now>=datetime.fromisoformat(current_started)+timedelta(seconds=int(a.per_question_seconds or 0))
        except (TypeError,ValueError): pass
    if request.method=='POST':
        qid=request.form.get('question_id',type=int)
        q=next((x for x in qs if x.question_id==qid and x.question_order==idx),None)
        if not q: abort(400)
        if not timed_out:
            try: option=int(request.form.get('option_index')) if request.form.get('option_index') not in (None,'') else None
            except (TypeError,ValueError): option=None
            if option not in (0,1,2,3): option=None
            stmt=insert_stmt(SchoolAssignmentAnswer).values(
                attempt_id=attempt.id,question_id=qid,option_index=option,
                answered_at=now.isoformat())
            db.session.execute(stmt.on_conflict_do_update(
                index_elements=['attempt_id','question_id'],
                set_={'option_index':stmt.excluded.option_index,
                      'answered_at':stmt.excluded.answered_at}))
            answers[qid]=option
        if idx>=len(qs) or timed_out:
            # Grade from the frozen snapshot, never from the live question bank.
            score=sum(float(x.points) for x in qs if answers.get(x.question_id)==x.correct_option)
            max_score=sum(float(x.points) for x in qs)
            pct=round(score/max_score*100,2) if max_score else 0
            attempt.status='submitted'; attempt.submitted_at=now.isoformat()
            attempt.score=score; attempt.max_score=max_score; attempt.percentage=pct
            db.session.execute(sa_update(AssignmentStudent)
                .where(AssignmentStudent.assignment_id==assignment_id,
                       AssignmentStudent.student_id==sid)
                .values(status='done',score=score,max_score=max_score,
                        submitted_at=now.isoformat()))
            db.session.commit()
            session.pop('assignment_attempt_id',None); session.pop('assignment_question_index',None); session.pop('assignment_question_started_at',None)
            return redirect(url_for('student_assignment_detail',assignment_id=assignment_id))
        session['assignment_question_index']=idx+1; session.pop('assignment_question_started_at',None)
        db.session.commit()
    idx=max(1,min(len(qs),int(session.get('assignment_question_index',idx)))); q=qs[idx-1]; now=datetime.now(timezone.utc)
    if a.timing_mode=='per_question':
        started=session.get('assignment_question_started_at')
        if not started: session['assignment_question_started_at']=now.isoformat(); started=now.isoformat()
        try: timed_out=now>=datetime.fromisoformat(started)+timedelta(seconds=int(a.per_question_seconds or 0))
        except (TypeError,ValueError): timed_out=False
    return render_template('student_assignment_take.html',assignment=a,question=q,index=idx,total=len(qs),selected=answers.get(q.question_id),timed_out=timed_out,expires_at=attempt.expires_at,per_question_seconds=a.per_question_seconds)

@app.route('/student/<string:kind>')
@student_required
def student_assessment_list(kind):
    if kind not in ('practice','tests','examinations'):
        abort(404)
    assessment_type={'practice':'practice','tests':'test','examinations':'examination'}[kind]
    sid=session.get('student_id')
    student=_student_with_enrolment(sid)
    rows=[]
    if student and student['class_id']:
        stmt=(select(SchoolAssessment,SchoolSubject.name.label('subject_name'))
              .join(SchoolSubject,SchoolSubject.id==SchoolAssessment.subject_id)
              .where(SchoolAssessment.assessment_type==assessment_type,
                     SchoolAssessment.class_id==student['class_id'],
                     student_department_filter(student,SchoolSubject.departments),
                     SchoolAssessment.active==1,SchoolAssessment.question_count>0))
        if assessment_type!='practice':
            # Practice is a standing question bank; tests and examinations belong to a session.
            stmt=stmt.where(or_(SchoolAssessment.session_id==student['session_id'],
                                SchoolAssessment.session_id.is_(None)))
        rows=[_flatten(r,'SchoolAssessment','subject_name')
              for r in all_rows(stmt.order_by(SchoolSubject.name,SchoolAssessment.id.desc()))]
    title={'practice':'Practice Tests','tests':'Tests','examinations':'Examinations'}[kind]
    return render_template('student_assessment_list.html',student=student,assessments=rows,title=title,assessment_type=assessment_type)

@app.post('/student/assessments/<int:assessment_id>/start')
@student_required
@csrf_protect
def student_assessment_start(assessment_id):
    sid=session.get('student_id')
    if _is_practice(assessment_id):
        return redirect(url_for('student_practice_take',assessment_id=assessment_id))
    student,a,questions=_student_assessment_context(assessment_id,sid)
    if not a: abort(404)
    existing=db.session.scalars(select(SchoolAssessmentAttempt).where(
        SchoolAssessmentAttempt.student_id==sid,
        SchoolAssessmentAttempt.assessment_id==assessment_id)).first()
    if existing:
        if existing['status']=='active':
            return redirect(url_for('student_assessment_take',assessment_id=assessment_id,q=1))
        return redirect(url_for('student_assessment_result',assessment_id=assessment_id))
    now=datetime.now(timezone.utc)
    expires=now+timedelta(minutes=int(a['duration_minutes']))
    device_raw,device_hash=exam_integrity.new_device_tag()
    try:
        attempt=SchoolAssessmentAttempt(student_id=sid,assessment_id=assessment_id,
            started_at=now.isoformat(),expires_at=expires.isoformat(),status='active',device_tag=device_hash)
        db.session.add(attempt); db.session.flush()
        attempt_id=attempt.id
        # The objective questions come first and the theory ones after. With shuffling on (the default), each student
        # gets both in their own order, and each objective question's options too: the frozen copy keeps the
        # shuffled options and where the right one moved to, so grading reads it exactly as the student saw it.
        objective=[q for q in questions if q['question_type']!=THEORY]
        theory=[q for q in questions if q['question_type']==THEORY]
        shuffle=bool(a.get('shuffle',1))
        if shuffle:
            random.shuffle(objective); random.shuffle(theory)
        frozen=[]
        for order,q in enumerate(objective+theory,1):
            if q['question_type']==THEORY:
                options=[None,None,None,None]; correct=-1
            else:
                options=[q['option_a'],q['option_b'],q['option_c'],q['option_d']]
                order_of=list(range(4))
                if shuffle: random.shuffle(order_of)
                correct=order_of.index(int(q['correct_option']))
                options=[options[i] for i in order_of]
            # A theory question is frozen with no options and no correct answer; its marking guide stays with the
            # teacher and is never copied here.
            frozen.append(SchoolAssessmentAttemptQuestion(
                attempt_id=attempt_id,question_id=q['id'],question_order=order,
                question_text=q['question_text'],
                option_a=options[0],option_b=options[1],option_c=options[2],option_d=options[3],
                instruction=q['instruction'],image_path=q['image_path'],
                correct_option=correct,points=int(q['points'] or 1),question_type=q['question_type'] or OBJECTIVE))
        db.session.add_all(frozen)
        db.session.commit()
    except sa.exc.IntegrityError:
        # The UNIQUE(student_id, assessment_id) constraint protects against
        # double-clicks or two browser tabs starting the same assessment.
        db.session.rollback()
        existing=db.session.scalars(select(SchoolAssessmentAttempt).where(
            SchoolAssessmentAttempt.student_id==sid,
            SchoolAssessmentAttempt.assessment_id==assessment_id)).first()
        if existing:
            return redirect(url_for('student_assessment_take',assessment_id=assessment_id,q=1))
        raise
    except sa.exc.SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Failed to start student assessment %s", assessment_id)
        raise
    audit_log('school_assessment_started','school_assessment','assessment',assessment_id,
              {'student_id':sid,'attempt_id':attempt_id},True)
    return exam_integrity.set_device_cookie(redirect(url_for('student_assessment_take',assessment_id=assessment_id,q=1)),
                                            exam_integrity.SCHOOL,attempt_id,device_raw)

@app.route('/student/assessments/<int:assessment_id>',methods=['GET'])
@student_required
def student_assessment_take(assessment_id):
    sid=session.get('student_id')
    if _is_practice(assessment_id):
        return redirect(url_for('student_practice_take',assessment_id=assessment_id))
    student,a,questions=_student_assessment_context(assessment_id,sid)
    if not a: abort(404)
    attempt=db.session.scalars(select(SchoolAssessmentAttempt).where(
        SchoolAssessmentAttempt.student_id==sid,
        SchoolAssessmentAttempt.assessment_id==assessment_id)).first()
    if not attempt:
        return render_template('student_assessment_start.html',student=student,assessment=a)
    if attempt['status']=='active': apply_connectivity_grace(attempt)
    if attempt['status']=='active' and remaining(attempt)<=0:
        student_assessment_grade(attempt['id'], auto=True)
        return redirect(url_for('student_assessment_result',assessment_id=assessment_id))
    if attempt['status']!='active':
        return redirect(url_for('student_assessment_result',assessment_id=assessment_id))
    rows=db.session.scalars(select(SchoolAssessmentAttemptQuestion)
        .where(SchoolAssessmentAttemptQuestion.attempt_id==attempt['id'])
        .order_by(SchoolAssessmentAttemptQuestion.question_order)).all()
    saved={}; saved_text={}
    for qid,opt,text in tuples(select(SchoolAssessmentAnswer.question_id,SchoolAssessmentAnswer.option_index,
                                      SchoolAssessmentAnswer.answer_text)
            .where(SchoolAssessmentAnswer.attempt_id==attempt['id'])):
        if opt is not None: saved[qid]=opt
        if text: saved_text[qid]=text
    if not rows: abort(409,description='This assessment attempt has no frozen question set.')
    try: current=int(request.args.get('q',1))
    except (TypeError,ValueError): current=1
    current=max(1,min(len(rows),current))
    # The whole paper is sent at once, so moving between questions never reloads the page (and never drops full
    # screen). Never the correct answer: each question is copied without it.
    paper=[]
    for q in rows:
        public_q=dict(q)
        public_q.pop('correct_option',None)
        paper.append(public_q)
    exam_mode=bool(a.get('exam_mode',1))
    if exam_mode and not exam_integrity.same_device(exam_integrity.SCHOOL,attempt['id'],attempt['device_tag']):
        exam_integrity.record(exam_integrity.SCHOOL,attempt['id'],'new_device')
    response=make_response(render_template('student_assessment_take.html',
        student=student,assessment=a,questions=paper,current=current,
        total=len(rows),saved=saved,saved_text=saved_text,remaining=remaining(attempt),
        csrf_token_value=csrf_token(),attempt_id=attempt['id'],answer_limit=THEORY_ANSWER_LIMIT,
        exam_mode=exam_mode,leave_limit=a.get('exam_leave_limit'),
        left=exam_integrity.count_of(exam_integrity.SCHOOL,attempt['id'],'left_page') if exam_mode else 0))
    response.headers['Cache-Control']='no-store, no-cache, must-revalidate, max-age=0'
    return response

@app.post('/student/assessments/<int:assessment_id>/answer')
@student_required
@csrf_protect
def student_assessment_answer(assessment_id):
    sid=session.get('student_id')
    try: requested_attempt=int(request.form.get('attempt_id','0'))
    except (TypeError,ValueError): abort(400)
    attempt=db.session.scalars(select(SchoolAssessmentAttempt).where(
        SchoolAssessmentAttempt.id==requested_attempt,
        SchoolAssessmentAttempt.student_id==sid,
        SchoolAssessmentAttempt.assessment_id==assessment_id)).first()
    if not attempt:
        abort(404)
    # autosave=1 is the take page saving a written answer as it is typed: it is answered with JSON, never a page.
    autosave=request.form.get('autosave')=='1'
    if attempt['status']!='active':
        if autosave: return jsonify(ok=False,closed=True),409
        return redirect(url_for('student_assessment_result',assessment_id=assessment_id))
    apply_connectivity_grace(attempt)
    if remaining(attempt)<=0:
        student_assessment_grade(attempt['id'],auto=True)
        if autosave: return jsonify(ok=False,closed=True),409
        return redirect(url_for('student_assessment_result',assessment_id=assessment_id))
    try: qid=int(request.form.get('question_id','0'))
    except (TypeError,ValueError):
        abort(400)
    q=db.session.scalars(select(SchoolAssessmentAttemptQuestion).where(
        SchoolAssessmentAttemptQuestion.attempt_id==attempt['id'],
        SchoolAssessmentAttemptQuestion.question_id==qid)).first()
    if not q:
        abort(400)
    now=datetime.now(timezone.utc).isoformat()
    if (q['question_type'] or OBJECTIVE)==THEORY:
        text=(request.form.get('answer_text') or '').replace('\r\n','\n').rstrip()
        if len(text)>THEORY_ANSWER_LIMIT:
            if autosave: return jsonify(ok=False,error=f'An answer can be at most {THEORY_ANSWER_LIMIT} characters.'),413
            abort(413)
        if text.strip():
            answer_stmt=insert_stmt(SchoolAssessmentAnswer).values(
                attempt_id=attempt['id'],question_id=qid,option_index=None,answer_text=text,answered_at=now)
            db.session.execute(answer_stmt.on_conflict_do_update(
                index_elements=['attempt_id','question_id'],
                set_={'answer_text':answer_stmt.excluded.answer_text,'answered_at':answer_stmt.excluded.answered_at}))
        else:
            # Cleared: the question is unanswered again.
            db.session.execute(sa.delete(SchoolAssessmentAnswer).where(
                SchoolAssessmentAnswer.attempt_id==attempt['id'],SchoolAssessmentAnswer.question_id==qid))
        db.session.commit()
        if autosave: return jsonify(ok=True,saved_at=now)
    else:
        raw=request.form.get('option_index')
        if raw not in (None,''):
            # No option chosen ("continue without an answer") saves nothing and moves on; a chosen one must be real.
            try: opt=int(raw)
            except (TypeError,ValueError): abort(400)
            option_count=sum(1 for x in (q['option_a'],q['option_b'],q['option_c'],q['option_d']) if x is not None)
            if opt<0 or opt>=option_count:
                abort(400)
            answer_stmt=insert_stmt(SchoolAssessmentAnswer).values(
                attempt_id=attempt['id'],question_id=qid,option_index=opt,answered_at=now)
            db.session.execute(answer_stmt.on_conflict_do_update(
                index_elements=['attempt_id','question_id'],
                set_={'option_index':answer_stmt.excluded.option_index,
                      'answered_at':answer_stmt.excluded.answered_at}))
            db.session.commit()
        if autosave: return jsonify(ok=True,saved_at=now)
    next_q=request.form.get('next_q')
    try: next_q=int(next_q)
    except (TypeError,ValueError): next_q=1
    if request.form.get('submit_assessment')=='1':
        student_assessment_grade(attempt['id'],auto=False)
        return redirect(url_for('student_assessment_result',assessment_id=assessment_id))
    return redirect(url_for('student_assessment_take',assessment_id=assessment_id,q=max(1,next_q)))

@app.post('/student/assessments/<int:assessment_id>/submit')
@student_required
@csrf_protect
def student_assessment_submit(assessment_id):
    """Hand the paper in. Every answer has already been saved as it was given; this only grades the attempt."""
    sid=session.get('student_id')
    attempt=db.session.scalars(select(SchoolAssessmentAttempt).where(
        SchoolAssessmentAttempt.student_id==sid,
        SchoolAssessmentAttempt.assessment_id==assessment_id)).first()
    if not attempt: abort(404)
    if attempt['status']=='active':
        apply_connectivity_grace(attempt)
        student_assessment_grade(attempt['id'],auto=remaining(attempt)<=0)
    return redirect(url_for('student_assessment_result',assessment_id=assessment_id))

@app.post('/student/assessments/<int:assessment_id>/integrity')
@student_required
@csrf_protect
def student_assessment_integrity(assessment_id):
    """Exam mode's report of something that happened on the page (core/exam_integrity.py). Leaving the page past
    the paper's limit ends it here, on the server."""
    sid=session.get('student_id')
    attempt=db.session.scalars(select(SchoolAssessmentAttempt).where(
        SchoolAssessmentAttempt.student_id==sid,
        SchoolAssessmentAttempt.assessment_id==assessment_id)).first()
    if not attempt or attempt['status']!='active': return jsonify(ok=False,ended=True),409
    kind=request.form.get('kind','')
    if kind not in exam_integrity.PAGE_KINDS: return jsonify(ok=False),400
    settings=one(select(SchoolAssessment.exam_mode,SchoolAssessment.exam_leave_limit).where(SchoolAssessment.id==assessment_id))
    if not settings or not settings['exam_mode']: return jsonify(ok=False),409
    exam_integrity.record(exam_integrity.SCHOOL,attempt['id'],kind,request.form.get('question'))
    left=exam_integrity.count_of(exam_integrity.SCHOOL,attempt['id'],'left_page')
    limit=settings['exam_leave_limit']
    if kind=='left_page' and limit and left>limit:
        student_assessment_grade(attempt['id'],auto=False)
        audit_log('school_assessment_ended_by_exam_mode','school_assessment','assessment',assessment_id,
                  {'student_id':sid,'attempt_id':attempt['id'],'left':left,'limit':limit},True)
        return jsonify(ok=True,ended=True,left=left)
    return jsonify(ok=True,ended=False,left=left)

@app.post('/student/assessments/<int:assessment_id>/heartbeat')
@student_required
@csrf_protect
def student_assessment_heartbeat(assessment_id):
    """A periodic ping from the test/examination-taking page, used only to measure how long an
    attempt has gone unreachable (see core.entrance.apply_connectivity_grace)."""
    sid=session.get('student_id')
    attempt=db.session.scalars(select(SchoolAssessmentAttempt).where(
        SchoolAssessmentAttempt.student_id==sid,
        SchoolAssessmentAttempt.assessment_id==assessment_id)).first()
    if not attempt or attempt.status!='active':
        return jsonify(ok=False),403
    apply_connectivity_grace(attempt)
    return jsonify(ok=True)

@app.route('/student/assessments/<int:assessment_id>/result')
@student_required
def student_assessment_result(assessment_id):
    sid=session.get('student_id')
    _release_due_school_results()
    attempt=db.session.scalars(select(SchoolAssessmentAttempt).where(
        SchoolAssessmentAttempt.student_id==sid,
        SchoolAssessmentAttempt.assessment_id==assessment_id)).first()
    if not attempt: abort(404)
    if attempt['status']=='active' and remaining(attempt)<=0:
        attempt=student_assessment_grade(attempt['id'],auto=True)
    if attempt['status']=='active': return redirect(url_for('student_assessment_take',assessment_id=assessment_id,q=1))
    student,a,_=_student_assessment_context(assessment_id,sid)
    result_row=one(select(SchoolStudentResult.status,SchoolStudentResult.score,
                          SchoolStudentResult.max_score)
        .where(SchoolStudentResult.student_id==sid,
               SchoolStudentResult.assessment_id==assessment_id,
               SchoolStudentResult.status=='released')
        .order_by(SchoolStudentResult.id.desc()).limit(1))
    visible=bool(result_row) if a and a['assessment_type']!='practice' else True
    return render_template('student_assessment_result.html',student=student,assessment=a,
                           result=(result_row or attempt),percentage=(result_row['score']/result_row['max_score']*100 if result_row and result_row['max_score'] else attempt['percentage']),submitted=True,visible_to_student=visible)


# --------------------------------------------------------------- practice
# Practice is a self-study bank, deliberately kept out of a student's record: taking it writes
# nothing to the database (no attempt, no answer rows, no result), so it can never reach a term
# result or report card. It can be taken and retaken at any time, and every run serves the
# questions in a fresh random order.

PRACTICE_MAX_QUESTIONS=40

def _is_practice(assessment_id):
    return one_scalar(select(SchoolAssessment.id).where(
        SchoolAssessment.id==assessment_id,SchoolAssessment.assessment_type=='practice')) is not None

@app.route('/student/practice/<int:assessment_id>',methods=['GET','POST'])
@student_required
@csrf_protect
def student_practice_take(assessment_id):
    sid=session.get('student_id')
    student,a,questions=_student_practice_context(assessment_id,sid)
    if not a: abort(404)
    if not questions:
        flash('This practice test has no questions yet.','error')
        return redirect(url_for('student_assessment_list',kind='practice'))
    if request.method=='GET':
        chosen=random.sample(list(questions),min(len(questions),PRACTICE_MAX_QUESTIONS))
        session['student_practice']={'assessment_id':assessment_id,'ids':[q.id for q in chosen]}
        minutes=int(a['duration_minutes'] or 0)
        seconds=max(60,round(minutes*60*len(chosen)/max(1,len(questions)))) if minutes else 0
        return render_template('student_practice_take.html',student=student,assessment=a,
                               questions=chosen,seconds=seconds)
    state=session.get('student_practice') or {}
    ids=state.get('ids') or []
    if state.get('assessment_id')!=assessment_id or not ids:
        return redirect(url_for('student_practice_take',assessment_id=assessment_id))
    by_id={q.id:q for q in questions}
    results=[]; score=0
    for qid in ids:
        q=by_id.get(qid)
        if not q: continue
        try: picked=int(request.form.get(f'q_{qid}'))
        except (TypeError,ValueError): picked=-1
        options=[q.option_a,q.option_b,q.option_c,q.option_d]
        if picked not in (0,1,2,3) or options[picked] is None: picked=-1
        right=picked==q.correct_option
        score+=1 if right else 0
        results.append({'text':q.question_text,'options':options,'selected':picked,
                        'correct':q.correct_option,'right':right})
    session.pop('student_practice',None)
    total=len(results)
    return render_template('student_practice_result.html',student=student,assessment=a,
                           results=results,score=score,total=total,
                           percentage=(score/total*100) if total else 0)
