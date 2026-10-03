"""The entrance-candidate self-service portal: historical practice
(entrance-config-based and school-assessment-based), the candidate
dashboard, and the live entrance-exam-taking flow (start paper, exam,
answer, submit, result) shared with the anonymous single-paper legacy URLs.
"""

import json
import random
from datetime import datetime, timedelta, timezone

from flask import Response, abort, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import select, update as sa_update

from app import app, _school_current_session, csrf_token
from core.entrance import (
    _answers_for_attempt, _valid_question_configuration, apply_connectivity_grace, bank,
    candidate_cumulative, candidate_has_unused_retake, candidate_record,
    ENTRANCE_SUBJECT_LABELS, ENTRY_GROUP_LABELS, entrance_paper_label,
    entrance_practice_paper, entrance_subject_label, get_attempt, grade, remaining,
)
from models import (
    AcademicSession, Answer, Attempt, AttemptQuestion, CandidatePaper,
    EntranceBankConfig, Examination, RetakeGrant, SchoolAssessment,
    SchoolClass, SchoolQuestion, SchoolSubject, Student, db,
)
from core.db_helpers import all_rows, insert_stmt, one, one_scalar, _flatten
from core.security import csrf_protect


@app.route('/entrance-practice')
def entrance_practice():
    """The public entrance practice page: pick the class you are aiming for, then a subject.

    Nobody signs in or registers. What each subject serves is the school's choice (see
    core.entrance.entrance_practice_paper): the last session's paper, or questions set for practice.
    """
    group=request.args.get('group','')
    if group not in ENTRY_GROUP_LABELS: group=''
    subjects=[]
    if group:
        for key,label in ENTRANCE_SUBJECT_LABELS.items():
            paper=entrance_practice_paper(group,key)
            subjects.append({'key':key,'label':label,'available':bool(paper),
                             'count':paper['count'] if paper else 0,
                             'source':paper['label'] if paper else ''})
    return render_template('entrance_practice.html',groups=list(ENTRY_GROUP_LABELS.items()),
                           group=group,group_label=ENTRY_GROUP_LABELS.get(group,''),subjects=subjects)

@app.route('/entrance-practice/<group>/<subject>',methods=['GET','POST'])
@csrf_protect
def entrance_practice_take(group,subject):
    if group not in ENTRY_GROUP_LABELS or subject not in ENTRANCE_SUBJECT_LABELS: abort(404)
    paper=entrance_practice_paper(group,subject)
    if not paper:
        return render_template('entrance_practice.html',groups=list(ENTRY_GROUP_LABELS.items()),
            group=group,group_label=ENTRY_GROUP_LABELS[group],subjects=[],
            error='Practice questions for this subject are not available yet.'),404
    questions=paper['bank']['questions']
    subject_label=ENTRANCE_SUBJECT_LABELS[subject]; group_label=ENTRY_GROUP_LABELS[group]
    if request.method=='GET':
        # A fresh random draw, in a fresh random order, every time.
        chosen=random.sample(list(questions),paper['count'])
        session['entrance_practice']={'group':group,'subject':subject,'ids':[int(q['id']) for q in chosen]}
        public=[{'id':int(q['id']),'text':q.get('text',''),'options':q.get('options',[]),
                 'instruction':q.get('instruction'),'image_path':q.get('image_path')} for q in chosen]
        return render_template('entrance_practice_take.html',group=group,group_label=group_label,
            subject=subject,
            subject_label=subject_label,label=paper['label'],questions=public,
            seconds=int(paper['bank'].get('duration_seconds') or 0))
    state=session.get('entrance_practice') or {}
    if state.get('group')!=group or state.get('subject')!=subject or not state.get('ids'):
        return redirect(url_for('entrance_practice_take',group=group,subject=subject))
    by_id={int(q['id']):q for q in questions}
    results=[]; score=0
    for qid in state['ids']:
        q=by_id.get(int(qid))
        if not q: continue
        options=q.get('options',[])
        try: picked=int(request.form.get(f'q_{qid}'))
        except (TypeError,ValueError): picked=-1
        if picked<0 or picked>=len(options): picked=-1
        correct=int(q.get('answer',-1))
        score+=1 if picked==correct else 0
        results.append({'text':q.get('text',''),'options':options,'selected':picked,'correct':correct})
    session.pop('entrance_practice',None)
    total=len(results)
    return render_template('entrance_practice_result.html',group=group,group_label=group_label,
        subject=subject,subject_label=subject_label,results=results,score=score,total=total,
        percentage=(score/total*100) if total else 0)

@app.route('/practice',methods=['GET','POST'])
@csrf_protect
def practice():
    current=_school_current_session()
    # Practice tests from the current session are reserved for enrolled students;
    # the public practice area only offers assessments from past sessions.
    stmt=(select(SchoolAssessment,SchoolClass.name.label('class_name'),
                 SchoolSubject.name.label('subject_name'),
                 AcademicSession.name.label('session_name'))
        .join(SchoolClass,SchoolClass.id==SchoolAssessment.class_id)
        .join(SchoolSubject,SchoolSubject.id==SchoolAssessment.subject_id)
        .outerjoin(AcademicSession,AcademicSession.id==SchoolAssessment.session_id)
        .where(SchoolAssessment.assessment_type=='practice',SchoolAssessment.active==1,
               SchoolAssessment.session_id.is_not(None)))
    if current:
        stmt=stmt.where(SchoolAssessment.session_id!=current['id'])
    rows=[_flatten(r,'SchoolAssessment','class_name','subject_name','session_name')
          for r in all_rows(stmt.order_by(SchoolClass.level_order,SchoolSubject.name,
                                          SchoolAssessment.id.desc()))]
    classes=db.session.scalars(select(SchoolClass).where(SchoolClass.active==1)
                               .order_by(SchoolClass.level_order)).all()
    subjects=db.session.scalars(select(SchoolSubject).where(SchoolSubject.active==1)
                                .order_by(SchoolSubject.name)).all()
    if request.method=='POST':
        try: cid=int(request.form.get('class_id','0')); sid=int(request.form.get('subject_id','0'))
        except (TypeError,ValueError): cid=sid=0
        matches=[r for r in rows if r['class_id']==cid and r['subject_id']==sid]
        if not matches:
            return render_template('practice.html',classes=classes,subjects=subjects,assessments=rows,error='No eligible practice test is available for that class and subject yet.')
        return redirect(url_for('practice_take',assessment_id=matches[0]['id']))
    return render_template('practice.html',classes=classes,subjects=subjects,assessments=rows,error=None)

@app.route('/practice/<int:assessment_id>',methods=['GET','POST'])
@csrf_protect
def practice_take(assessment_id):
    current=_school_current_session()
    a=_flatten(one(select(SchoolAssessment,SchoolClass.name.label('class_name'),
                          SchoolSubject.name.label('subject_name'),
                          AcademicSession.name.label('session_name'))
        .join(SchoolClass,SchoolClass.id==SchoolAssessment.class_id)
        .join(SchoolSubject,SchoolSubject.id==SchoolAssessment.subject_id)
        .outerjoin(AcademicSession,AcademicSession.id==SchoolAssessment.session_id)
        .where(SchoolAssessment.id==assessment_id,
               SchoolAssessment.assessment_type=='practice',SchoolAssessment.active==1)),
        'SchoolAssessment','class_name','subject_name','session_name')
    if not a or (current and a['session_id']==current['id']): abort(404)
    questions=db.session.scalars(select(SchoolQuestion)
        .where(SchoolQuestion.assessment_id==assessment_id)
        .order_by(SchoolQuestion.sort_order,SchoolQuestion.id)).all()
    if request.method=='GET':
        if not questions: return render_template('practice.html',classes=[],subjects=[],assessments=[],error='This practice test has no questions yet.')
        chosen=random.sample(list(questions),min(len(questions),20))
        session['practice_questions']=[q['id'] for q in chosen]; session['practice_assessment']=assessment_id
        return render_template('practice_take.html',assessment=a,questions=chosen)
    ids=session.get('practice_questions',[])
    if session.get('practice_assessment')!=assessment_id or not ids:
        return redirect(url_for('practice_take',assessment_id=assessment_id))
    answer_map={}
    for qid in ids:
        raw=request.form.get(f'q_{qid}')
        try: answer_map[qid]=int(raw) if raw is not None else -1
        except ValueError: answer_map[qid]=-1
    selected={q['id']:q for q in questions if q['id'] in ids}
    score=sum(1 for qid,q in selected.items() if answer_map.get(qid)==q['correct_option'])
    max_score=len(selected); pct=(score/max_score*100) if max_score else 0
    result_questions=[{'text':q['question_text'],'selected':answer_map.get(q['id'],-1),'correct':q['correct_option']} for q in selected.values()]
    session.pop('practice_questions',None); session.pop('practice_assessment',None)
    return render_template('practice_result.html',assessment=a,score=score,max_score=max_score,percentage=pct,results=result_questions)

def _candidate_grade_label(pct):
    if pct is None: return '—'
    if pct >= 80: return 'Excellent'
    if pct >= 70: return 'Very Good'
    if pct >= 60: return 'Good'
    if pct >= 50: return 'Pass'
    return 'Needs Improvement'


@app.route('/candidate/dashboard')
def candidate_dashboard():
    c=candidate_record(session.get('candidate_id'))
    if not c: session.pop('candidate_id',None); return redirect(url_for('login'))
    rows,total_score,total_max,pct,completed=candidate_cumulative(c['id'])
    expired_any=False
    for r in rows:
        if r['status']=='active' and r['attempt_id']:
            attempt=get_attempt(r['attempt_id'])
            if attempt and remaining(attempt)<=0:
                grade(attempt['id'],auto=True); expired_any=True
    if expired_any:
        rows,total_score,total_max,pct,completed=candidate_cumulative(c['id'])
    papers=[]
    for r in rows:
        status=r['status'] or 'not_started'
        label='In Progress' if status=='active' else ('Completed' if status in ('submitted','expired') else 'Not Started')
        retake=bool(candidate_has_unused_retake(c['id'],c['candidate_name'],r['bank_id'])) if status in ('submitted','expired') else False
        # Do not pass score, max_score or percentage into the candidate template context.
        subject_label = entrance_subject_label(r['bank_id'], r['exam_name'] or '')
        papers.append({'id':r['paper_id'],'slot':r['slot'],'bank_id':r['bank_id'],
                       'exam_name':r['exam_name'],'subject_label':subject_label,
                       'paper_label':entrance_paper_label(r['slot'], r['bank_id'], r['exam_name'] or ''),
                       'status':status,'display_status':label, 'retake_available':retake})

    # Nothing below is computed, let alone shown, unless an administrator has released this
    # candidate's result (c.results_released_at) — see blueprints/entrance/admissions.py.
    released_student=None
    if c.results_released_at and c.admission_status=='admitted' and c.admitted_student_id:
        released_student=db.session.get(Student,c.admitted_student_id)
        if released_student and not released_student.password_must_change and c.released_password:
            # The password has already been changed once; it has done its job, so stop keeping
            # a plaintext copy around.
            c.released_password=None; db.session.commit()

    response=Response(render_template('candidate_dashboard.html',candidate=c,papers=papers,completed=completed,
                                      total_score=total_score,total_max=total_max,overall_percentage=pct,
                                      grade_label=_candidate_grade_label(pct) if completed else None,
                                      released_student=released_student))
    response.headers['Cache-Control']='no-store, no-cache, must-revalidate, max-age=0'; response.headers['Pragma']='no-cache'
    return response

@app.post('/candidate/papers/<int:paper_id>/start')
@csrf_protect
def candidate_start_paper(paper_id):
    c=candidate_record(session.get('candidate_id'))
    if not c: return redirect(url_for('login'))
    # The candidate_id predicate is what stops one candidate starting another's paper.
    raw=one(select(CandidatePaper,
                   Examination.id.label('exam_id'),
                   Examination.active.label('exam_active'),
                   EntranceBankConfig.questions_to_serve,
                   EntranceBankConfig.marks_per_question,
                   EntranceBankConfig.active.label('config_active'),
                   EntranceBankConfig.entry_group,EntranceBankConfig.subject,
                   EntranceBankConfig.session_id.label('config_session_id'))
            .join(Examination,Examination.bank_id==CandidatePaper.bank_id)
            .outerjoin(EntranceBankConfig,EntranceBankConfig.id==CandidatePaper.config_id)
            .where(CandidatePaper.id==paper_id,CandidatePaper.candidate_id==c['id']))
    if not raw: abort(404)
    paper=_flatten(raw,'CandidatePaper','exam_id','exam_active','questions_to_serve',
                   'marks_per_question','config_active','entry_group','subject',
                   'config_session_id')
    b=bank(paper['bank_id'])
    if not b or not b.get('questions'):
        flash('This paper is not ready because its question bank is empty.','error'); return redirect(url_for('candidate_dashboard'))
    if not paper['config_id']:
        flash('This paper is not linked to a verified examination configuration. Please contact the school administrator.','error'); return redirect(url_for('candidate_dashboard'))
    serve_count=int(paper['questions_to_serve'] or len(b['questions']))
    valid,marks=_valid_question_configuration(serve_count)
    if not valid or serve_count>len(b['questions']):
        flash('This paper has an invalid examination configuration. Please contact the school administrator.','error'); return redirect(url_for('candidate_dashboard'))

    # Close out anything that has run out of time before deciding what is open.
    active_attempts=db.session.scalars(select(Attempt).where(
        Attempt.candidate_id==c['id'],Attempt.status=='active')
        .order_by(Attempt.id.desc())).all()
    for active_attempt in active_attempts:
        if remaining(active_attempt)<=0:
            grade(active_attempt.id,auto=True)
    other_active=one(select(Attempt.id,Attempt.bank_id).where(
        Attempt.candidate_id==c['id'],Attempt.status=='active',
        Attempt.bank_id!=paper['bank_id']).order_by(Attempt.id.desc()).limit(1))
    if other_active:
        flash('You must submit your current paper before starting another paper.','error')
        return redirect(url_for('candidate_dashboard'))

    existing=db.session.scalars(select(Attempt).where(
        Attempt.candidate_id==c['id'],Attempt.bank_id==paper['bank_id'])
        .order_by(Attempt.id.desc()).limit(1)).first()
    if existing and existing.status=='active':
        db.session.commit(); session['attempt_id']=existing.id; return redirect(url_for('exam',q=1))
    if existing and existing.status in ('submitted','expired'):
        grant_id=one_scalar(select(RetakeGrant.id).where(
            RetakeGrant.bank_id==paper['bank_id'],RetakeGrant.candidate_id==c['id'],
            RetakeGrant.used_at.is_(None)).order_by(RetakeGrant.id.asc()).limit(1))
        if not grant_id:
            flash('You have already taken this paper. Please contact the school administrator if another attempt is required.','error'); return redirect(url_for('candidate_dashboard'))
        # Claim the grant conditionally so two requests cannot both consume it.
        claimed=db.session.execute(sa_update(RetakeGrant)
            .where(RetakeGrant.id==grant_id,RetakeGrant.used_at.is_(None))
            .values(used_at=datetime.now(timezone.utc).isoformat()))
        if claimed.rowcount != 1:
            db.session.rollback(); flash('The retake permission has already been used.','error'); return redirect(url_for('candidate_dashboard'))

    now=datetime.now(timezone.utc); exp=now+timedelta(seconds=int(b.get('duration_seconds',3600)))
    attempt=Attempt(candidate=c['candidate_name'],exam_id=paper['exam_id'],
                    bank_id=paper['bank_id'],started_at=now.isoformat(),
                    expires_at=exp.isoformat(),status='active',candidate_id=c['id'])
    db.session.add(attempt); db.session.flush()
    # Freeze the served questions so later bank edits cannot alter this attempt.
    chosen=random.sample(list(b['questions']),serve_count)
    for order,q in enumerate(chosen,1):
        db.session.add(AttemptQuestion(
            attempt_id=attempt.id,question_id=int(q['id']),question_order=order,
            question_text=str(q.get('text','')),
            options_json=json.dumps(q.get('options',[]),ensure_ascii=False),
            instruction=q.get('instruction'),image_path=q.get('image_path'),
            correct_option=int(q.get('answer')) if q.get('answer') is not None else -1,
            points=float(marks)))
    db.session.commit(); session['attempt_id']=attempt.id
    return redirect(url_for('exam',q=1))

@app.route('/exam')
def exam():
    a=get_attempt(session.get('attempt_id'))
    if not a: return redirect(url_for('candidate_dashboard') if session.get('candidate_id') else url_for('login'))
    if session.get('candidate_id') and a['candidate_id'] != session.get('candidate_id'): abort(403)
    if a['status']!='active': return redirect(url_for('result'))
    apply_connectivity_grace(a)
    if remaining(a)<=0: grade(a['id'],auto=True); return redirect(url_for('result'))
    rows=db.session.scalars(select(AttemptQuestion)
        .where(AttemptQuestion.attempt_id==a['id'])
        .order_by(AttemptQuestion.question_order)).all()
    slot_row=one(select(CandidatePaper.slot)
        .where(CandidatePaper.candidate_id==a['candidate_id'],
               CandidatePaper.bank_id==a['bank_id']).limit(1)) if a['candidate_id'] else None
    saved=_answers_for_attempt(a['id'])
    if not rows: abort(409, description='This examination attempt has no frozen question set.')
    exam_questions=[]
    for r in rows:
        exam_questions.append({'id':r['question_id'],'text':r['question_text'],'options':json.loads(r['options_json'] or '[]'),
                               'instruction':r['instruction'],'image_path':r['image_path']})
    paper_label=entrance_paper_label(slot_row['slot'],a['bank_id'],'') if slot_row else 'Examination'
    try: requested_q=int(request.args.get('q',1))
    except (TypeError,ValueError): requested_q=1
    n=max(1,min(len(exam_questions),requested_q))
    response=Response(render_template('exam.html',candidate=a['candidate'],exam={'questions':exam_questions},current=n,saved=saved,remaining=remaining(a),paper_label=paper_label,csrf_token_value=csrf_token()))
    response.headers['Cache-Control']='no-store, no-cache, must-revalidate, max-age=0'; response.headers['Pragma']='no-cache'; return response

@app.post('/answer')
@csrf_protect
def answer():
    a=get_attempt(session.get('attempt_id'))
    if session.get('candidate_id') and (not a or a['candidate_id'] != session.get('candidate_id')): return jsonify(ok=False,error='Invalid candidate session.'),403
    if not a or a['status']!='active': return jsonify(ok=False,error='Session is no longer active.'),403
    apply_connectivity_grace(a)
    if remaining(a)<=0: grade(a['id'],auto=True); return jsonify(ok=False,expired=True),410
    try: qid=int(request.form.get('question_id',0)); opt=int(request.form.get('option_index',-1))
    except (TypeError,ValueError): return jsonify(ok=False,error='Invalid answer.'),400
    q=db.session.scalars(select(AttemptQuestion).where(
        AttemptQuestion.attempt_id==a['id'],AttemptQuestion.question_id==qid)).first()
    if not q: return jsonify(ok=False,error='Invalid question.'),400
    options=json.loads(q['options_json'] or '[]')
    if opt<0 or opt>=len(options): return jsonify(ok=False,error='Invalid answer.'),400
    answer_stmt=insert_stmt(Answer).values(attempt_id=a['id'],question_id=qid,option_index=opt,
                                             answered_at=datetime.now(timezone.utc).isoformat())
    db.session.execute(answer_stmt.on_conflict_do_update(
        index_elements=['attempt_id','question_id'],
        set_={'option_index':answer_stmt.excluded.option_index,
              'answered_at':answer_stmt.excluded.answered_at}))
    db.session.commit(); return jsonify(ok=True)

@app.post('/submit')
@csrf_protect
def submit():
    a=get_attempt(session.get('attempt_id'))
    if not a: return redirect(url_for('login'))
    if session.get('candidate_id') and a['candidate_id'] != session.get('candidate_id'): abort(403)
    if a['status']=='active': apply_connectivity_grace(a)
    grade(a['id'],auto=remaining(a)<=0)
    return redirect(url_for('candidate_dashboard') if session.get('candidate_id') else url_for('result'))

@app.post('/exam/heartbeat')
@csrf_protect
def exam_heartbeat():
    """A periodic ping from the exam page, used only to measure how long an attempt has gone
    unreachable (see apply_connectivity_grace) - it carries no exam state of its own."""
    a=get_attempt(session.get('attempt_id'))
    if session.get('candidate_id') and (not a or a['candidate_id'] != session.get('candidate_id')): return jsonify(ok=False),403
    if not a or a['status']!='active': return jsonify(ok=False),403
    apply_connectivity_grace(a)
    return jsonify(ok=True)

@app.route('/result')
def result():
    a=get_attempt(session.get('attempt_id'))
    if not a: return redirect(url_for('candidate_dashboard') if session.get('candidate_id') else url_for('login'))
    if session.get('candidate_id') and a['candidate_id'] != session.get('candidate_id'): abort(403)
    if a['status']=='active' and remaining(a)<=0: a=grade(a['id'],auto=True)
    if a['status']=='active': return redirect(url_for('exam'))
    if session.get('candidate_id'): return redirect(url_for('candidate_dashboard'))
    response=Response(render_template('result.html'))
    response.headers['Cache-Control']='no-store, no-cache, must-revalidate, max-age=0'; response.headers['Pragma']='no-cache'; return response

@app.route('/review')
def review(): return redirect(url_for('candidate_dashboard') if session.get('candidate_id') else url_for('result'))

@app.route('/reset')
def reset(): session.clear(); return redirect(url_for('login'))
