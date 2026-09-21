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

from app import (
    app, ENTRANCE_CONFIG_EXTRA, _entrance_config_select,
    _school_current_session, csrf_token,
)
from core.entrance import (
    _answers_for_attempt, _valid_question_configuration, bank,
    candidate_cumulative, candidate_has_unused_retake, candidate_record,
    entrance_paper_label, entrance_subject_label, get_attempt,
    grade, remaining,
)
from models import (
    AcademicSession, Answer, Attempt, AttemptQuestion, CandidatePaper,
    EntranceBankConfig, Examination, RetakeGrant, SchoolAssessment,
    SchoolClass, SchoolQuestion, SchoolSubject, db,
)
from core.db_helpers import all_rows, insert_stmt, one, one_scalar, _flatten
from core.security import csrf_protect


@app.route('/entrance-practice')
def entrance_practice():
    current=_school_current_session()
    stmt=_entrance_config_select().where(EntranceBankConfig.practice_enabled==1)
    if current:
        # The live session is never offered as practice material.
        stmt=stmt.where(EntranceBankConfig.session_id!=current['id'])
    stmt=stmt.order_by(EntranceBankConfig.session_id.desc(),
                       EntranceBankConfig.entry_group,EntranceBankConfig.subject)
    configs=[_flatten(r,'EntranceBankConfig',*ENTRANCE_CONFIG_EXTRA) for r in all_rows(stmt)]
    return render_template('entrance_practice.html',configs=configs)

@app.route('/entrance-practice/<int:config_id>',methods=['GET','POST'])
def entrance_practice_take(config_id):
    current=_school_current_session()
    raw=one(_entrance_config_select().where(EntranceBankConfig.id==config_id,
                                            EntranceBankConfig.practice_enabled==1))
    cfg=_flatten(raw,'EntranceBankConfig',*ENTRANCE_CONFIG_EXTRA) if raw else None
    if not cfg or (current and cfg['session_id']==current['id']):
        abort(404)
    b=bank(cfg['bank_id'])
    valid,marks=_valid_question_configuration(cfg['questions_to_serve'])
    if not b or not valid or cfg['questions_to_serve']>len(b.get('questions',[])):
        return render_template('entrance_practice.html',configs=[],error='This practice set is not currently available.')
    if request.method=='GET':
        chosen=random.sample(list(b['questions']),int(cfg['questions_to_serve']))
        session['entrance_practice_config']=config_id
        session['entrance_practice_questions']=[int(q['id']) for q in chosen]
        public=[dict(q,answer=None,points=marks) for q in chosen]
        for q in public: q.pop('answer',None)
        return render_template('entrance_practice_take.html',config=cfg,questions=public)
    if session.get('entrance_practice_config')!=config_id:
        return redirect(url_for('entrance_practice_take',config_id=config_id))
    ids={int(x) for x in session.get('entrance_practice_questions',[])}
    selected=[q for q in b['questions'] if int(q['id']) in ids]
    score=0
    results=[]
    for q in selected:
        raw_choice=request.form.get(f"q_{q['id']}")
        try: choice=int(raw_choice) if raw_choice is not None else -1
        except ValueError: choice=-1
        correct=choice==int(q.get('answer',-1))
        if correct: score+=marks
        results.append({'text':q.get('text',''),'selected':choice,'correct':int(q.get('answer',-1)),'options':q.get('options',[])})
    score=round(score,2)
    max_score=round(marks*len(selected),2)
    pct=round(score/max_score*100,2) if max_score else 0
    session.pop('entrance_practice_config',None); session.pop('entrance_practice_questions',None)
    return render_template('entrance_practice_result.html',config=cfg,score=score,max_score=max_score,percentage=pct,results=results)

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
    response=Response(render_template('candidate_dashboard.html',candidate=c,papers=papers,completed=completed))
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
    grade(a['id'],auto=remaining(a)<=0)
    return redirect(url_for('candidate_dashboard') if session.get('candidate_id') else url_for('result'))

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
