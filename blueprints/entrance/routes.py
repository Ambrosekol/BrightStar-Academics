"""Entrance examination admin: the dashboard, periodized entrance-bank
configuration, candidate registration/records, question-bank management,
attempts, results/rankings/exports, and retake/regrade controls.
"""

import json
import os
from datetime import datetime, timezone
from urllib.parse import quote

from flask import Response, abort, flash, jsonify, redirect, render_template, request, send_file, url_for
from sqlalchemy import and_, func, select, delete as sa_delete, update as sa_update
from werkzeug.security import generate_password_hash

from app import (
    app, BASE, ENTRANCE_CONFIG_EXTRA, ENTRANCE_SUBJECT_LABELS,
    _active_sessions, _answers_for_attempt, _candidate_papers,
    _chrome_result_png, _create_control_item, _entrance_config_row,
    _new_candidate_code, _new_candidate_password, _resource_locked,
    _result_file_data_uri, _school_current_session,
    _valid_question_configuration, bank, bank_entry_group, bank_subject,
    candidate_cumulative, entrance_paper_label, entrance_subject_label,
    get_attempt, grade, load_banks, normalize_entry_group,
    premium_result_metrics, required_papers_for_target, sync_examinations,
    _entrance_config_select,
)
from models import (
    AcademicSession, AdminResourceLock, Answer, Attempt, Candidate,
    CandidatePaper, EntranceBankConfig, Examination, RetakeGrant, db,
)
from core.db_helpers import all_rows, obj, one, one_scalar, tuples, _flatten
from core.security import (
    admin_access_error, admin_has_permission, admin_required, admin_scope_allows,
    audit_log, current_admin, csrf_protect, is_super_admin, _notify_super_admins,
)
from core.uploads import _save_image_upload
from core.presence import online_presence
from blueprints.entrance.helpers import (
    _candidate_results_summary, _csv_response,
    _entrance_banks_for, _entrance_config_rows, _generate_bank_id,
    _grade_label, _result_rows, next_qid, save_bank, validate_bank_payload,
)


@app.route('/admin/examination')
@admin_required
def admin_dashboard():
    banks=load_banks(); sync_examinations()
    stats={
        'banks':len(banks),
        'questions':sum(len(b.get('questions',[])) for b in banks.values()),
        'attempts':one_scalar(select(func.count()).select_from(Attempt),0),
        'completed':one_scalar(select(func.count()).select_from(Attempt)
                               .where(Attempt.status.in_(('submitted','expired'))),0),
        'candidates':one_scalar(select(func.count()).select_from(Candidate)
                                .where(Candidate.active==1),0)
    }
    recent=db.session.scalars(select(Attempt).order_by(Attempt.id.desc()).limit(12)).all()
    current_session=_school_current_session()
    states={bank_id:active for bank_id,active in tuples(
        select(EntranceBankConfig.bank_id,func.max(EntranceBankConfig.active).label('active'))
        .where(EntranceBankConfig.session_id==(current_session['id'] if current_session else -1))
        .group_by(EntranceBankConfig.bank_id))}
    online_counts,_=online_presence()

    summaries=_candidate_results_summary()
    subject_labels=[('mathematics','Mathematics'),('english','English'),('general_knowledge','General Knowledge')]
    subject_analytics=[]
    for key,label in subject_labels:
        attempted=[r[key] for r in summaries if r[key]['percentage'] is not None]
        passed=[d for d in attempted if d['percentage'] >= 50]
        average=(sum(d['percentage'] for d in attempted)/len(attempted)) if attempted else 0
        subject_analytics.append({
            'key':key,'label':label,'attempted':len(attempted),'average':average,
            'passed':len(passed),'pass_rate':(len(passed)/len(attempted)*100) if attempted else 0
        })

    class_analytics=[]
    for key,label in (('year7','JSS 1'),('year10','SSS 1')):
        group=[r for r in summaries if normalize_entry_group(r['target_class'])==key]
        complete=[r for r in group if r['complete']]
        percentages=[r['overall_percentage'] for r in complete]
        class_analytics.append({
            'key':key,'label':label,'candidates':len(group),'completed':len(complete),
            'average':(sum(percentages)/len(percentages)) if percentages else None
        })

    top_candidates=sorted([r for r in summaries if r['complete']], key=lambda r:(r['overall_percentage'],r['total_score']), reverse=True)[:5]
    for i,r in enumerate(top_candidates,1): r['rank']=i
    return render_template('admin_dashboard.html',banks=banks.values(),stats=stats,recent=recent,exam_states=states,subject_analytics=subject_analytics,class_analytics=class_analytics,top_candidates=top_candidates,online_counts=online_counts)

@app.route('/admin/practice-tests')
@admin_required
def admin_practice_tests():
    me=current_admin()
    if not admin_has_permission(me['id'],'entrance.config.view'):
        return admin_access_error('entrance.config.view')
    current=_school_current_session()
    configs=[_flatten(r,'EntranceBankConfig',*ENTRANCE_CONFIG_EXTRA) for r in all_rows(
        _entrance_config_select().where(EntranceBankConfig.practice_enabled==1)
        .order_by(AcademicSession.id.desc(),EntranceBankConfig.entry_group,
                  EntranceBankConfig.subject))]
    return render_template('admin_entrance_practice.html',configs=configs,current_session=current)

@app.route('/admin/entrance-config')
@admin_required
def admin_entrance_config():
    me=current_admin()
    if not admin_has_permission(me['id'],'entrance.config.view'):
        return admin_access_error('entrance.config.view')
    return render_template('admin_entrance_config.html',configs=_entrance_config_rows(),
                           banks=_entrance_banks_for(me),sessions=_active_sessions(),
                           current_session=_school_current_session())

@app.post('/admin/entrance-config/save')
@admin_required
@csrf_protect
def admin_entrance_config_save():
    me=current_admin()
    perm='entrance.config.edit' if request.form.get('config_id') else 'entrance.config.create'
    if not admin_has_permission(me['id'],perm):
        return admin_access_error(perm)
    bank_id=request.form.get('bank_id','').strip()
    entry_group=request.form.get('entry_group','').strip().lower()
    subject=request.form.get('subject','').strip().lower()
    term=request.form.get('term','').strip() or 'Full Session'
    try: session_id=int(request.form.get('session_id',''))
    except (TypeError,ValueError): session_id=0
    try: count=int(request.form.get('questions_to_serve',''))
    except (TypeError,ValueError): count=0
    errors=[]
    b=bank(bank_id)
    if not b: errors.append('Select a valid entrance question bank.')
    if entry_group not in ('year7','year10'): errors.append('Select JSS 1 or SSS 1.')
    if subject not in ENTRANCE_SUBJECT_LABELS: errors.append('Select a valid entrance subject.')
    if b and not admin_scope_allows(me['id'],'bank',bank_id): errors.append('This question bank is outside your authorised scope.')
    if b and bank_subject(b)!=subject: errors.append('The selected subject does not match the question bank.')
    if b and subject!='general_knowledge' and bank_entry_group(b) not in (entry_group, None): errors.append('The selected question bank is not for this entry class.')
    session_row=db.session.scalars(select(AcademicSession).where(
        AcademicSession.id==session_id,AcademicSession.active==1)).first()
    if not session_row: errors.append('Select an active academic session.')
    valid,marks=_valid_question_configuration(count)
    if not valid: errors.append('This question count cannot produce a clean 100-point score under the current equal-weight scoring rules.')
    if b and count>len(b.get('questions',[])): errors.append(f'The selected bank contains only {len(b.get("questions",[]))} questions.')
    config_id=request.form.get('config_id','').strip()
    reason=request.form.get('reason','').strip()
    if session_row and not session_row.is_current and not reason:
        errors.append('A reason is required when assigning an entrance bank to a previous academic session.')
    if errors:
        return render_template('admin_entrance_config.html',configs=_entrance_config_rows(),
                               banks=_entrance_banks_for(me),sessions=_active_sessions(),
                               current_session=session_row,errors=errors,form=request.form),400
    now=datetime.now(timezone.utc).isoformat()

    def paper_count(cid):
        """A configuration already issued to a candidate is frozen."""
        return one_scalar(select(func.count()).select_from(CandidatePaper)
                          .where(CandidatePaper.config_id==cid),0)

    if config_id:
        row=obj(EntranceBankConfig,int(config_id))
        if not row: abort(404)
        if paper_count(int(config_id)):
            flash('This entrance configuration is already assigned to a candidate paper and cannot be edited. Create a new configuration for a changed setup, then activate it explicitly.','error')
            return redirect(url_for('admin_entrance_config'))
        row.bank_id=bank_id; row.entry_group=entry_group; row.subject=subject
        row.session_id=session_id; row.term=term; row.questions_to_serve=count
        row.marks_per_question=marks; row.updated_at=now; row.updated_by=me['id']
        action='entrance_config_updated'; target_id=int(config_id)
    else:
        existing=db.session.scalars(select(EntranceBankConfig).where(
            EntranceBankConfig.bank_id==bank_id,EntranceBankConfig.entry_group==entry_group,
            EntranceBankConfig.subject==subject,EntranceBankConfig.session_id==session_id,
            EntranceBankConfig.term==term)).first()
        if existing:
            if paper_count(existing.id):
                flash('This entrance configuration is already assigned to a candidate paper. Use a new academic period/term configuration for a changed future setup.','error')
                return redirect(url_for('admin_entrance_config'))
            existing.questions_to_serve=count; existing.marks_per_question=marks
            existing.updated_at=now; existing.updated_by=me['id']
            target_id=existing.id; action='entrance_config_updated'
        else:
            created=EntranceBankConfig(bank_id=bank_id,entry_group=entry_group,subject=subject,
                session_id=session_id,term=term,questions_to_serve=count,
                marks_per_question=marks,active=0,practice_enabled=0,
                created_at=now,created_by=me['id'],updated_at=now,updated_by=me['id'])
            db.session.add(created); db.session.flush()
            target_id=created.id; action='entrance_config_created'
    db.session.commit()
    audit_log(action,'assessment','entrance_config',target_id,{'bank_id':bank_id,'entry_group':entry_group,'subject':subject,'session_id':session_id,'questions_to_serve':count,'reason':reason or None})
    flash('Entrance examination configuration saved. It is not active unless you explicitly activate it.','success')
    return redirect(url_for('admin_entrance_config'))

@app.post('/admin/entrance-config/<int:config_id>/activate')
@admin_required
@csrf_protect
def admin_entrance_config_activate(config_id):
    me=current_admin()
    reason=request.form.get('reason','').strip()
    if not admin_has_permission(me['id'],'entrance.config.activate'):
        return admin_access_error('entrance.config.activate')
    cfg=obj(EntranceBankConfig,config_id)
    if not cfg: abort(404)
    if not admin_scope_allows(me['id'],'bank',cfg.bank_id): return admin_access_error('entrance.config.activate')
    valid,marks=_valid_question_configuration(cfg.questions_to_serve)
    b=bank(cfg.bank_id)
    if not b or not valid or cfg.questions_to_serve>len(b.get('questions',[])):
        flash('This configuration cannot be activated because its question set is not valid for a 100-point examination.','error'); return redirect(url_for('admin_entrance_config'))
    now=datetime.now(timezone.utc).isoformat()
    # Exactly one configuration may be live per group/subject/session/term.
    db.session.execute(sa_update(EntranceBankConfig)
        .where(EntranceBankConfig.entry_group==cfg.entry_group,
               EntranceBankConfig.subject==cfg.subject,
               EntranceBankConfig.session_id==cfg.session_id,
               EntranceBankConfig.term==cfg.term)
        .values(active=0,updated_at=now,updated_by=me['id']))
    db.session.execute(sa_update(EntranceBankConfig)
        .where(EntranceBankConfig.id==config_id)
        .values(active=1,practice_enabled=0,updated_at=now,updated_by=me['id']))
    db.session.execute(sa_update(Examination)
        .where(Examination.bank_id==cfg.bank_id).values(active=1))
    db.session.commit()
    audit_log('entrance_config_activated','assessment','entrance_config',config_id,{'bank_id':cfg.bank_id,'entry_group':cfg.entry_group,'subject':cfg.subject,'session_id':cfg.session_id,'reason':reason or None})
    flash('Entrance examination configuration activated.','success')
    return redirect(url_for('admin_entrance_config'))

@app.post('/admin/entrance-config/<int:config_id>/practice')
@admin_required
@csrf_protect
def admin_entrance_config_practice(config_id):
    me=current_admin()
    if not admin_has_permission(me['id'],'entrance.practice.manage'):
        return admin_access_error('entrance.practice.manage')
    cfg=obj(EntranceBankConfig,config_id)
    if not cfg: abort(404)
    current=_school_current_session()
    if current and cfg.session_id==current['id']:
        flash('The current academic entrance configuration cannot be enabled for practice.','error'); return redirect(url_for('admin_entrance_config'))
    if not admin_scope_allows(me['id'],'bank',cfg.bank_id):
        return admin_access_error('entrance.practice.manage')
    new=0 if cfg.practice_enabled else 1
    reason=request.form.get('reason','').strip()
    if new and not reason:
        flash('A reason is required when enabling historical practice for an entrance configuration.','error'); return redirect(url_for('admin_entrance_config'))
    cfg.practice_enabled=new; cfg.updated_at=datetime.now(timezone.utc).isoformat(); cfg.updated_by=me['id']
    db.session.commit()
    audit_log('entrance_practice_eligibility_changed','assessment','entrance_config',config_id,{'enabled':bool(new),'reason':reason or None})
    flash('Historical entrance practice eligibility updated.','success')
    return redirect(url_for('admin_entrance_config'))

@app.route('/admin/candidates')
@admin_required
def admin_candidates():
    """Entrance candidate register with deliberate class-first segmentation."""
    selected_group=request.args.get('level','').strip().lower()
    if selected_group not in ('year7','year10'):
        selected_group=''
    search=request.args.get('q','').strip()
    all_candidates=db.session.scalars(select(Candidate).where(Candidate.active==1)
                                      .order_by(Candidate.id.desc())).all()
    # One pass over every attempt status, rather than a query per candidate.
    statuses_by_candidate={}
    for cand_id,status in tuples(select(Attempt.candidate_id,Attempt.status)):
        statuses_by_candidate.setdefault(cand_id,[]).append(status)
    # Class cards are intentionally derived from the two supported entrance sets.
    groups=[('year7','JSS 1'),('year10','SSS 1')]
    cards=[]
    for key,label in groups:
        subset=[c for c in all_candidates if normalize_entry_group(c['target_class'])==key]
        completed_count=0; active_count=0
        for c in subset:
            statuses=statuses_by_candidate.get(c['id'],[])
            if any(st in ('submitted','expired') for st in statuses): completed_count+=1
            if any(st=='active' for st in statuses): active_count+=1
        cards.append({'key':key,'label':label,'count':len(subset),'completed':completed_count,'in_progress':active_count})
    if selected_group:
        candidates=[]
        for c in all_candidates:
            if normalize_entry_group(c['target_class'])!=selected_group: continue
            if search and search.casefold() not in ' '.join(str(c[k] or '') for k in ('candidate_name','candidate_code','school_attended','parent_guardian_name')).casefold(): continue
            candidates.append((c,_candidate_papers(c['id'])))
    else:
        candidates=[]
    return render_template('admin_candidates.html', candidates=candidates, class_cards=cards, selected_group=selected_group, search=search)

@app.route('/admin/candidates/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_new_candidate():
    if request.method=='POST':
        name=request.form.get('candidate_name','').strip()
        target=request.form.get('target_class','').strip()
        school_attended=request.form.get('school_attended','').strip()
        parent_guardian_name=request.form.get('parent_guardian_name','').strip()
        parent_guardian_relationship=request.form.get('parent_guardian_relationship','').strip()
        primary_mobile=request.form.get('primary_mobile','').strip()
        alternative_mobile=request.form.get('alternative_mobile','').strip()
        parent_guardian_email=request.form.get('parent_guardian_email','').strip()
        photo_path=None
        errors=[]
        if not name: errors.append('Candidate name is required.')
        if not school_attended: errors.append('School attended is required.')
        if not parent_guardian_name: errors.append('Parent / Guardian name is required.')
        if not parent_guardian_relationship: errors.append('Parent / Guardian relationship is required.')
        if not primary_mobile: errors.append('Primary mobile / WhatsApp is required.')
        if parent_guardian_email and ('@' not in parent_guardian_email or '.' not in parent_guardian_email.split('@')[-1]): errors.append('Enter a valid parent / guardian email address.')
        try:
            photo_path=_save_image_upload(request.files.get('photo'),'candidates',f'candidate_{name.replace(" ","_").lower() or "candidate"}')
        except ValueError as exc:
            errors.append(str(exc))
        group=normalize_entry_group(target)
        if not group: errors.append('Select a valid entrance level: JSS 1 or SS 1.')
        papers,missing=required_papers_for_target(target) if group else ([],[])
        current_session=_school_current_session()
        paper_configs=[]
        if group and current_session:
            for subject in ('mathematics','english','general_knowledge'):
                cfg=_entrance_config_row(group,subject,current_session['id'])
                if cfg: paper_configs.append(cfg)
        if missing:
            errors.append('The following required paper bank(s) are not available: '+', '.join(missing)+'. Complete the verified question banks before registering candidates.')
        if len(papers)!=3: errors.append('A candidate session must contain exactly three papers: Mathematics, English and General Knowledge.')
        if errors:
            return render_template('candidate_form.html',errors=errors,form=request.form,available_papers=papers,missing=missing)
        now=datetime.now(timezone.utc).isoformat()
        try:
            code=_new_candidate_code()
            password=_new_candidate_password()
            candidate=Candidate(
                candidate_code=code,candidate_name=name,target_class=target,
                password_hash=generate_password_hash(password),created_at=now,active=1,
                school_attended=school_attended,parent_guardian_name=parent_guardian_name,
                parent_guardian_relationship=parent_guardian_relationship,
                primary_mobile=primary_mobile,alternative_mobile=alternative_mobile,
                parent_guardian_email=parent_guardian_email,photo_path=photo_path)
            db.session.add(candidate); db.session.flush()
            cid=candidate.id
            for slot,b in enumerate(papers,1):
                cfg=next((x for x in paper_configs if x['bank_id']==b['id']),None)
                if not cfg:
                    raise ValueError('The entrance configuration changed while this candidate was being registered. Please review the active entrance setup and try again.')
                db.session.add(CandidatePaper(candidate_id=cid,slot=slot,bank_id=b['id'],
                                              config_id=cfg['id'],assigned_at=now))
            db.session.commit()
        except Exception:
            db.session.rollback(); raise
        _notify_super_admins('New student registered',f'{name} was registered for the {target} entrance examination.','info',url_for('admin_controls'))
        return render_template('candidate_registered.html',candidate={'id':cid,'candidate_code':code,'candidate_name':name,'target_class':target},password=password,papers=papers)
    return render_template('candidate_form.html',errors=[],form={},available_papers=[],missing=[])

@app.route('/admin/candidates/<int:cid>')
@admin_required
def admin_candidate_detail(cid):
    c=obj(Candidate,cid)
    if not c: abort(404)
    rows,total_score,total_max,pct,completed=candidate_cumulative(cid)
    latest_attempts=[_flatten(r,'Attempt','exam_name','paper_slot') for r in all_rows(
        select(Attempt,Examination.name.label('exam_name'),
               CandidatePaper.slot.label('paper_slot'))
        .outerjoin(Examination,Examination.bank_id==Attempt.bank_id)
        .outerjoin(CandidatePaper,and_(CandidatePaper.candidate_id==Attempt.candidate_id,
                                       CandidatePaper.bank_id==Attempt.bank_id))
        .where(Attempt.candidate_id==cid).order_by(Attempt.id.desc()))]
    latest_attempts=[dict(a, paper_label=entrance_paper_label(a['paper_slot'], a['bank_id'], a['exam_name'] or '') if a['paper_slot'] is not None else entrance_subject_label(a['bank_id'], a['exam_name'] or '')) for a in latest_attempts]
    banks=load_banks()
    papers=[]
    for row in rows:
        item=dict(row)
        subject=bank_subject(banks.get(row['bank_id'],{}))
        item['subject_label']={'mathematics':'Mathematics','english':'English','general_knowledge':'General Knowledge'}.get(subject,'Paper '+str(row['slot']))
        papers.append(item)
    return render_template('admin_candidate_detail.html',candidate=c,papers=papers,total_score=total_score,total_max=total_max,pct=pct,completed=completed,grade=_grade_label(pct) if completed else '—',latest_attempts=latest_attempts)

@app.route('/admin/candidates/<int:cid>/performance')
@admin_required
def admin_candidate_performance(cid):
    c=obj(Candidate,cid)
    if not c: abort(404)
    rows,total_score,total_max,pct,completed=candidate_cumulative(cid)
    attempts=[_flatten(r,'Attempt','exam_name','paper_slot') for r in all_rows(
        select(Attempt,Examination.name.label('exam_name'),
               CandidatePaper.slot.label('paper_slot'))
        .outerjoin(Examination,Examination.bank_id==Attempt.bank_id)
        .outerjoin(CandidatePaper,and_(CandidatePaper.candidate_id==Attempt.candidate_id,
                                       CandidatePaper.bank_id==Attempt.bank_id))
        .where(Attempt.candidate_id==cid).order_by(Attempt.id.desc()))]
    attempts=[dict(a, paper_label=entrance_paper_label(a['paper_slot'], a['bank_id'], a['exam_name'] or '') if a['paper_slot'] is not None else entrance_subject_label(a['bank_id'], a['exam_name'] or '')) for a in attempts]
    banks=load_banks()
    papers=[]
    for row in rows:
        item=dict(row)
        subject=bank_subject(banks.get(row['bank_id'],{}))
        item['subject_label']={'mathematics':'Mathematics','english':'English','general_knowledge':'General Knowledge'}.get(subject,'Paper '+str(row['slot']))
        papers.append(item)
    return render_template('admin_candidate_performance.html',candidate=c,papers=papers,total_score=total_score,total_max=total_max,pct=pct,completed=completed,grade=_grade_label(pct) if completed else '—',attempts=attempts)

@app.route('/admin/candidates/<int:cid>/result/email')
@admin_required
def admin_candidate_result_email(cid):
    c=obj(Candidate,cid)
    if not c: abort(404)
    email=(c['parent_guardian_email'] or '').strip()
    if not email:
        flash('No parent / guardian email address is available for this candidate.','error')
        return redirect(url_for('admin_candidate_detail',cid=cid))
    rows,total_score,total_max,pct,completed=candidate_cumulative(cid)
    score_line=f'{total_score}/{total_max} ({pct:.1f}%)' if completed else 'Not yet completed'
    subject=f'Entrance Examination Result — {c["candidate_name"]}'
    body=(f'Creative Rainbow Schools — Entrance Examination Result\n\n'
          f'Candidate: {c["candidate_name"]}\n'
          f'Candidate ID: {c["candidate_code"]}\n'
          f'Target class: {c["target_class"]}\n'
          f'Cumulative result: {score_line}\n'
          f'Overall grade: {_grade_label(pct) if completed else "Pending"}\n\n'
          f'This result was prepared by Creative Rainbow Schools.')
    return redirect('mailto:'+quote(email,safe='@.')+'?subject='+quote(subject)+'&body='+quote(body))

@app.route('/admin/candidates/<int:cid>/result/whatsapp')
@admin_required
def admin_candidate_result_whatsapp(cid):
    c=obj(Candidate,cid)
    if not c: abort(404)
    phone=(c['primary_mobile'] or c['alternative_mobile'] or '').strip()
    digits=''.join(ch for ch in phone if ch.isdigit())
    if digits.startswith('0'): digits='234'+digits[1:]
    if not digits:
        flash('No parent / guardian mobile number is available for this candidate.','error')
        return redirect(url_for('admin_candidate_detail',cid=cid))
    rows,total_score,total_max,pct,completed=candidate_cumulative(cid)
    score_line=f'{total_score}/{total_max} ({pct:.1f}%)' if completed else 'Not yet completed'
    message=(f"Creative Rainbow Schools — Entrance Examination Result\n\n"
             f"Candidate: {c['candidate_name']}\n"
             f"Candidate ID: {c['candidate_code']}\n"
             f"Target class: {c['target_class']}\n"
             f"Cumulative result: {score_line}\n"
             f"Overall grade: {_grade_label(pct) if completed else 'Pending'}\n\n"
             f"Thank you for choosing Creative Rainbow Schools.")
    return redirect('https://wa.me/'+digits+'?text='+quote(message))

@app.post('/admin/candidates/<int:cid>/delete')
@admin_required
@csrf_protect
def admin_candidate_delete(cid):
    if not is_super_admin(current_admin()): return admin_access_error('Super Admin approval')
    try:
        if not one_scalar(select(Candidate.id).where(Candidate.id==cid)):
            abort(404)
        # Answers reference attempts, which reference the candidate, so they are
        # removed in dependency order.
        attempt_ids=select(Attempt.id).where(Attempt.candidate_id==cid).scalar_subquery()
        db.session.execute(sa_delete(Answer).where(Answer.attempt_id.in_(attempt_ids)))
        db.session.execute(sa_delete(Attempt).where(Attempt.candidate_id==cid))
        db.session.execute(sa_delete(CandidatePaper).where(CandidatePaper.candidate_id==cid))
        db.session.execute(sa_delete(Candidate).where(Candidate.id==cid))
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    _notify_super_admins('Student record deleted',f'Student record #{cid} was deleted by a staff administrator.','warning',url_for('admin_controls')); flash('Student deleted successfully. Super Admin has been notified.','success')
    return redirect(url_for('admin_candidates'))

@app.route('/admin/candidates/<int:cid>/result/image')
@admin_required
def admin_candidate_result_image(cid):
    candidate=obj(Candidate,cid)

    if not candidate:
        abort(404)

    rows,total_score,total_max,pct,completed=candidate_cumulative(cid)

    if not completed:
        abort(400, description='The candidate has not completed the required result.')

    papers=[dict(r) for r in rows]

    total_questions=sum(
        int(p.get('question_count') or 0)
        for p in papers
        if p.get('status') in ('submitted','expired')
    )

    total_correct=sum(
        int(p.get('correct_count') or 0)
        for p in papers
        if p.get('status') in ('submitted','expired')
    )

    percentages=[
        float(p['percentage'])
        for p in papers
        if p.get('status') in ('submitted','expired')
        and p.get('percentage') is not None
    ]

    average_subject_percentage=(
        sum(percentages)/len(percentages)
        if percentages else 0
    )

    grade=_grade_label(pct)

    # _chrome_result_png returns (png_path, html_path); only the PNG is served.
    # Passing the whole tuple to send_file raised
    # "AttributeError: 'tuple' object has no attribute 'read'".
    image_path,_html_path=_chrome_result_png(
        candidate,
        papers,
        total_score,
        total_max,
        pct,
        total_questions,
        total_correct,
        average_subject_percentage,
        grade
    )

    return send_file(
        image_path,
        mimetype='image/png',
        as_attachment=False,
        download_name=f"{candidate['candidate_code']}_Official_Result.png"
    )

@app.route('/admin/candidates/<int:cid>/result/share')
@admin_required
def admin_candidate_result_share(cid):
    candidate=obj(Candidate,cid)

    if not candidate:
        abort(404)

    rows,total_score,total_max,pct,completed=candidate_cumulative(cid)

    if not completed:
        abort(400, description='The candidate has not completed the required result.')

    image_url=url_for(
        'admin_candidate_result_image',
        cid=cid,
        _external=True
    )

    filename=f"{candidate['candidate_code']}_Official_Result.png"

    logo_path=os.path.join(
        BASE,
        'static',
        'images',
        'school_logo.png'
    )

    logo_data=_result_file_data_uri(logo_path)

    return render_template(
        'admin_candidate_result_share.html',
        candidate=candidate,
        image_url=image_url,
        filename=filename,
        logo_data=logo_data,
        total_score=total_score,
        total_max=total_max,
        pct=pct,
        completed=completed,
        grade=_grade_label(pct)
    )

@app.route('/admin/candidates/<int:cid>/result/print')
@admin_required
def admin_candidate_result_print(cid):
    c=obj(Candidate,cid)

    if not c:
        abort(404)

    rows,total_score,total_max,pct,completed=candidate_cumulative(cid)

    total_questions,total_correct,average_subject_percentage=\
        premium_result_metrics(rows)

    return render_template(
        'admin_candidate_result_print.html',
        candidate=c,
        papers=rows,
        total_score=total_score,
        total_max=total_max,
        pct=pct,
        completed=completed,
        total_questions=total_questions,
        total_correct=total_correct,
        average_subject_percentage=average_subject_percentage,
        grade=_grade_label(pct) if completed else '—'
    )

@app.post('/admin/candidates/<int:cid>/credentials/reset')
@admin_required
@csrf_protect
def admin_candidate_credentials_reset(cid):
    c=one(select(Candidate.id,Candidate.candidate_code,Candidate.candidate_name,
                 Candidate.target_class,Candidate.created_at,Candidate.active)
          .where(Candidate.id==cid))
    if not c: abort(404)
    password=_new_candidate_password()
    db.session.execute(sa_update(Candidate).where(Candidate.id==cid)
                       .values(password_hash=generate_password_hash(password)))
    db.session.commit()
    _notify_super_admins('Student credentials reset',f'Login credentials for {c["candidate_name"]} were reset.','info',url_for('admin_controls'))
    return render_template('candidate_credentials_reset.html',candidate=c,password=password)

@app.route('/admin/candidates/<int:cid>/credentials/print')
@admin_required
def admin_candidate_credentials_print(cid):
    c=one(select(Candidate.id,Candidate.candidate_code,Candidate.candidate_name,
                 Candidate.target_class,Candidate.created_at,Candidate.active)
          .where(Candidate.id==cid))
    if not c: abort(404)
    return render_template('admin_candidate_credentials_print.html',candidate=c)

@app.route('/admin/banks')
@admin_required
def admin_question_banks():
    selected_group=request.args.get('level','').strip().lower()
    if selected_group not in ('year7','year10'): selected_group=''
    search=request.args.get('q','').strip()
    all_banks=sorted(load_banks().values(),key=lambda b:str(b.get('name') or b.get('id') or '').lower())
    current_session=_school_current_session()
    states={bank_id:active for bank_id,active in tuples(
        select(EntranceBankConfig.bank_id,func.max(EntranceBankConfig.active).label('active'))
        .where(EntranceBankConfig.session_id==(current_session['id'] if current_session else -1))
        .group_by(EntranceBankConfig.bank_id))}
    locks={r['resource_id']:r for r in all_rows(
        select(AdminResourceLock.resource_id,AdminResourceLock.reason)
        .where(AdminResourceLock.resource_type=='bank',
               AdminResourceLock.unlocked_at.is_(None)))}
    groups=[('year7','JSS 1'),('year10','SSS 1')]
    cards=[]
    for key,label in groups:
        matching=[b for b in all_banks if bank_entry_group(b)==key or (bank_entry_group(b) is None and bank_subject(b)=='general_knowledge')]
        cards.append({'key':key,'label':label,'count':len(matching),'questions':sum(len(b.get('questions',[])) for b in matching)})
    me=current_admin()
    if me and not me['admin_type_system']:
        all_banks=[b for b in all_banks if admin_scope_allows(me['id'],'bank',b['id'])]
    banks=[b for b in all_banks if selected_group and (bank_entry_group(b)==selected_group or (bank_entry_group(b) is None and bank_subject(b)=='general_knowledge')) and (not search or search.casefold() in str(b.get('name') or b.get('id') or '').casefold())]
    return render_template('admin_question_banks.html',banks=banks,all_banks=all_banks,exam_states=states,locks=locks,class_cards=cards,selected_group=selected_group,search=search)

@app.route('/admin/banks/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_new_bank():
    if request.method=='POST':
        data=request.form.to_dict()
        data['id']=data.get('id','').strip() or _generate_bank_id(data.get('name',''),data.get('level',''))
        errors=validate_bank_payload(data)
        if errors: return render_template('bank_form.html',mode='new',bank=data,errors=errors)
        duration_minutes=int(data.get('duration_minutes',60) or 60)
        b={'id':data['id'].strip(),'name':data['name'].strip(),'level':data.get('level','').strip(),'duration_seconds':duration_minutes*60,'version':data.get('version','1.0').strip(),'source_status':'admin_created','questions':[]}
        save_bank(b)
        # A newly created bank is never live until it is explicitly activated.
        db.session.execute(sa_update(Examination)
            .where(Examination.bank_id==b['id']).values(active=0))
        db.session.commit()
        audit_log('question_bank_created','assessment','bank',b['id'],{'name':b['name']}); _create_control_item('New question bank requires review',f'{b["name"]} was created by a staff administrator. Review it before it is used for an important examination.','question_bank_review','bank',b['id'],current_admin()['id']); _notify_super_admins('New question bank created',f'{b["name"]} was created by a staff administrator.','warning',url_for('admin_controls')); flash('Question bank created. Super Admin has been notified.','success'); return redirect(url_for('admin_bank',bid=b['id']))
    return render_template('bank_form.html',mode='new',bank=None,errors=[])

@app.route('/admin/banks/<bid>')
@admin_required
def admin_bank(bid):
    b=bank(bid)
    if not b: abort(404)
    return render_template('admin_bank.html',bank=b)

@app.route('/admin/banks/<bid>/edit',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_edit_bank(bid):
    b=bank(bid)
    if not b: abort(404)
    if request.method=='POST' and _resource_locked('bank',bid): flash('This question bank is locked by Super Admin. Unlock it from Administration → Controls before making changes.','error'); return redirect(url_for('admin_bank',bid=bid))
    if request.method=='POST':
        name=request.form.get('name','').strip(); level=request.form.get('level','').strip(); version=request.form.get('version','1.0').strip()
        try: duration=int(request.form.get('duration_minutes',0))*60 if request.form.get('duration_minutes') not in (None,'') else int(request.form.get('duration_seconds',3600))
        except: duration=0
        errors=[]
        if not name: errors.append('Bank name is required.')
        if duration<60: errors.append('Duration must be at least 60 seconds.')
        if errors: return render_template('bank_form.html',mode='edit',bank={**b,'name':name,'level':level,'version':version,'duration_seconds':duration},errors=errors)
        b.update(name=name,level=level,version=version,duration_seconds=duration)
        save_bank(b); audit_log('question_bank_updated','assessment','bank',bid,{'name':name,'level':level,'version':version}); _create_control_item('Question bank changed',f'{name} was updated by a staff administrator. Review the change if required.','question_bank_review','bank',bid,current_admin()['id']); _notify_super_admins('Question bank changed',f'{name} was updated by a staff administrator.','warning',url_for('admin_controls')); flash('Bank settings updated. Super Admin has been notified.','success'); return redirect(url_for('admin_bank',bid=bid))
    return render_template('bank_form.html',mode='edit',bank=b,errors=[])

@app.route('/admin/banks/<bid>/questions/new',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_new_question(bid):
    b=bank(bid)
    if not b: abort(404)
    if request.method=='POST' and _resource_locked('bank',bid): flash('This question bank is locked by Super Admin. Unlock it before adding questions.','error'); return redirect(url_for('admin_bank',bid=bid))
    if request.method=='POST':
        text=request.form.get('text','').strip(); instruction=request.form.get('instruction','').strip(); opts=[request.form.get(f'option{i}','').strip() for i in range(4)]
        try: ans=int(request.form.get('answer','-1')); points=int(request.form.get('points','1'))
        except: ans=-1; points=1
        errors=[]
        image_path=None
        try:
            image_path=_save_image_upload(request.files.get('image'),'questions',f'entrance_{bid}')
        except ValueError as exc:
            errors.append(str(exc))
        if not text: errors.append('Question text is required.')
        if any(not o for o in opts): errors.append('All four options are required.')
        if ans not in range(4): errors.append('Select the correct option.')
        if points<1: errors.append('Points must be at least 1.')
        if errors: return render_template('question_form.html',bank=b,question={'text':text,'instruction':instruction,'image_path':image_path,'options':opts,'answer':ans,'points':points},errors=errors)
        item={'id':next_qid(b),'text':text,'options':opts,'answer':ans,'points':points,'instruction':instruction}
        if image_path: item['image_path']=image_path
        b['questions'].append(item); save_bank(b); audit_log('question_added','assessment','bank',bid,{'question_id':b['questions'][-1]['id']}); _notify_super_admins('Question added to bank',f'A question was added to {b.get("name",bid)}.','info',url_for('admin_controls')); flash('Question added. Super Admin has been notified.','success'); return redirect(url_for('admin_bank',bid=bid))
    return render_template('question_form.html',bank=b,question=None,errors=[])

@app.route('/admin/banks/<bid>/questions/<int:qid>/edit',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_edit_question(bid,qid):
    b=bank(bid)
    if not b: abort(404)
    if request.method=='POST' and _resource_locked('bank',bid): flash('This question bank is locked by Super Admin. Unlock it before editing questions.','error'); return redirect(url_for('admin_bank',bid=bid))
    q=next((x for x in b['questions'] if int(x['id'])==qid),None)
    if not q: abort(404)
    if request.method=='POST':
        text=request.form.get('text','').strip(); instruction=request.form.get('instruction','').strip(); opts=[request.form.get(f'option{i}','').strip() for i in range(4)]
        try: ans=int(request.form.get('answer','-1')); points=int(request.form.get('points','1'))
        except: ans=-1; points=1
        errors=[]
        image_path=q.get('image_path')
        try:
            uploaded=_save_image_upload(request.files.get('image'),'questions',f'entrance_{bid}')
            if uploaded: image_path=uploaded
        except ValueError as exc:
            errors.append(str(exc))
        if not text: errors.append('Question text is required.')
        if any(not o for o in opts): errors.append('All four options are required.')
        if ans not in range(4): errors.append('Select the correct option.')
        if points<1: errors.append('Points must be at least 1.')
        if errors: return render_template('question_form.html',bank=b,question={'id':qid,'text':text,'instruction':instruction,'image_path':image_path,'options':opts,'answer':ans,'points':points},errors=errors)
        q.update(text=text,options=opts,answer=ans,points=points,instruction=instruction)
        if image_path: q['image_path']=image_path
        else: q.pop('image_path',None)
        save_bank(b); audit_log('question_updated','assessment','bank',bid,{'question_id':qid}); _notify_super_admins('Question updated',f'Question {qid} in {b.get("name",bid)} was updated.','info',url_for('admin_controls')); flash(f'Question {qid} updated. Super Admin has been notified.','success'); return redirect(url_for('admin_bank',bid=bid))
    return render_template('question_form.html',bank=b,question=q,errors=[])

@app.post('/admin/banks/<bid>/questions/<int:qid>/delete')
@admin_required
@csrf_protect
def admin_delete_question(bid,qid):
    b=bank(bid)
    if not b: abort(404)
    if _resource_locked('bank',bid): flash('This question bank is locked by Super Admin. Unlock it before deleting questions.','error'); return redirect(url_for('admin_bank',bid=bid))
    before=len(b['questions']); b['questions']=[q for q in b['questions'] if int(q['id'])!=qid]
    if len(b['questions'])==before: abort(404)
    # Deleting a question must not leave an active configuration asking for
    # more questions than the bank still holds.
    invalid=one_scalar(select(func.count()).select_from(EntranceBankConfig)
        .where(EntranceBankConfig.bank_id==bid,EntranceBankConfig.active==1,
               EntranceBankConfig.questions_to_serve>len(b['questions'])), 0)
    if invalid:
        b['questions'].append(next(q for q in bank(bid)['questions'] if int(q['id'])==qid))
        flash('This question cannot be deleted because an active entrance configuration requires the configured number of questions. Change the configuration first.','error')
        return redirect(url_for('admin_bank',bid=bid))
    save_bank(b); audit_log('question_deleted','assessment','bank',bid,{'question_id':qid}); _create_control_item('Question deleted',f'Question {qid} was deleted from {b.get("name",bid)}.','question_review','bank',bid,current_admin()['id']); _notify_super_admins('Question deleted',f'Question {qid} was deleted from {b.get("name",bid)}.','warning',url_for('admin_controls')); flash(f'Question {qid} deleted. Super Admin has been notified.','success'); return redirect(url_for('admin_bank',bid=bid))

@app.post('/admin/banks/<bid>/questions/reorder')
@admin_required
@csrf_protect
def admin_reorder(bid):
    b=bank(bid)
    if not b: abort(404)
    if _resource_locked('bank',bid): flash('This question bank is locked by Super Admin. Unlock it before reordering questions.','error'); return redirect(url_for('admin_bank',bid=bid))
    try: ids=[int(x) for x in request.form.get('order','').split(',') if x.strip()]
    except: ids=[]
    by={int(q['id']):q for q in b['questions']}
    if set(ids)!=set(by): return jsonify(ok=False,error='Order must contain every question exactly once.'),400
    b['questions']=[by[i] for i in ids]
    save_bank(b); audit_log('questions_reordered','assessment','bank',bid,{}); _notify_super_admins('Question order changed',f'The question order in {b.get("name",bid)} was changed.','info',url_for('admin_controls')); return jsonify(ok=True)

@app.route('/admin/attempts')
@admin_required
def admin_attempts():
    rows=[_flatten(r,'Attempt','exam_name') for r in all_rows(
        select(Attempt,Examination.name.label('exam_name'))
            .outerjoin(Examination,Examination.id==Attempt.exam_id)
            .order_by(Attempt.id.desc()))]
    return render_template('admin_attempts.html',attempts=rows)

@app.route('/admin/api/banks/<bid>/export')
@admin_required
def admin_export_bank(bid):
    b=bank(bid)
    if not b: abort(404)
    return jsonify(b)

@app.route('/admin/results')
@admin_required
def admin_results():
    # Results & Analytics is subject-centric: every candidate appears once in
    # each subject, allowing administrators to compare subject performance.
    all_summaries=_candidate_results_summary()
    target=request.args.get('target_class','').strip()
    search=request.args.get('q','').strip()
    summaries=all_summaries
    if target:
        summaries=[r for r in summaries if r['target_class'].strip().casefold()==target.casefold()]
    if search:
        needle=search.casefold(); summaries=[r for r in summaries if needle in r['candidate_name'].casefold() or needle in r['candidate_code'].casefold()]
    subject_defs=[('mathematics','Mathematics'),('english','English'),('general_knowledge','General Knowledge')]
    subject_sections=[]
    for key,label in subject_defs:
        entries=[]
        attempted=[]
        for r in summaries:
            d=dict(r[key]); d.update({'candidate_name':r['candidate_name'],'candidate_code':r['candidate_code'],'target_class':r['target_class']})
            entries.append(d)
            if d['percentage'] is not None: attempted.append(d)
        passed=sum(1 for d in attempted if d['percentage']>=50)
        average=(sum(d['percentage'] for d in attempted)/len(attempted)) if attempted else 0
        entries.sort(key=lambda d:(d['percentage'] is not None,d['percentage'] if d['percentage'] is not None else -1),reverse=True)
        subject_sections.append({'key':key,'label':label,'entries':entries,'attempted':len(attempted),'passed':passed,'average':average,'pass_rate':(passed/len(attempted)*100) if attempted else 0})
    overall_attempted=[d for r in summaries for d in [r['mathematics'],r['english'],r['general_knowledge']] if d['percentage'] is not None]
    overall_average=(sum(d['percentage'] for d in overall_attempted)/len(overall_attempted)) if overall_attempted else 0
    return render_template('admin_results.html',subject_sections=subject_sections,target_classes=sorted({r['target_class'] for r in all_summaries if r['target_class']}),selected_target=target,search=search,total_candidates=len(summaries),overall_average=overall_average)

@app.route('/admin/results/summary')
@admin_required
def admin_results_summary():
    all_summaries=_candidate_results_summary()
    summaries=all_summaries
    target=request.args.get('target_class','').strip()
    search=request.args.get('q','').strip()
    if target:
        summaries=[r for r in summaries if r['target_class'].strip().casefold()==target.casefold()]
    if search:
        needle=search.casefold()
        summaries=[r for r in summaries if needle in r['candidate_name'].casefold() or needle in r['candidate_code'].casefold()]
    target_classes=sorted({r['target_class'] for r in all_summaries if r['target_class']})
    return render_template('admin_results_summary.html',summaries=summaries,target_classes=target_classes,selected_target=target,search=search)

@app.route('/admin/results/summary/print')
@admin_required
def admin_results_summary_print():
    summaries=_candidate_results_summary()
    target=request.args.get('target_class','').strip()
    search=request.args.get('q','').strip()
    if target:
        summaries=[r for r in summaries if r['target_class'].strip().casefold()==target.casefold()]
    if search:
        needle=search.casefold()
        summaries=[r for r in summaries if needle in r['candidate_name'].casefold() or needle in r['candidate_code'].casefold()]
    return render_template('admin_results_summary_print.html',summaries=summaries,selected_target=target,search=search,now=datetime.now().strftime('%d %b %Y, %I:%M %p'))

@app.route('/admin/results/<int:aid>')
@admin_required
def admin_result_detail(aid):
    a=get_attempt(aid)
    if not a: abort(404)
    b=bank(a['bank_id'])
    answers=_answers_for_attempt(aid)
    slot_row=one(select(CandidatePaper.slot)
        .where(CandidatePaper.candidate_id==a['candidate_id'],
               CandidatePaper.bank_id==a['bank_id']).limit(1)) if a['candidate_id'] else None
    paper_label=entrance_paper_label(slot_row['slot'],a['bank_id'],b.get('name','') if b else '') if slot_row else entrance_subject_label(a['bank_id'],b.get('name','') if b else '')
    return render_template('admin_result_detail.html',attempt=a,exam=b,answers=answers,grade=_grade_label(a['percentage']),paper_label=paper_label)

@app.route('/admin/results/<int:aid>/print')
@admin_required
def admin_result_print(aid):
    a=get_attempt(aid)
    if not a: abort(404)
    b=bank(a['bank_id'])
    answers=_answers_for_attempt(aid)
    slot_row=one(select(CandidatePaper.slot)
        .where(CandidatePaper.candidate_id==a['candidate_id'],
               CandidatePaper.bank_id==a['bank_id']).limit(1)) if a['candidate_id'] else None
    paper_label=entrance_paper_label(slot_row['slot'],a['bank_id'],b.get('name','') if b else '') if slot_row else entrance_subject_label(a['bank_id'],b.get('name','') if b else '')
    return render_template('admin_result_print.html',attempt=a,exam=b,answers=answers,grade=_grade_label(a['percentage']),paper_label=paper_label)

@app.route('/admin/rankings')
@admin_required
def admin_rankings():
    # Competition ranking is one row per candidate, not one row per paper.
    # Candidates are ranked only after all three assigned papers are complete.
    all_summaries=_candidate_results_summary()
    target=request.args.get('target_class','').strip()
    search=request.args.get('q','').strip()
    summaries=all_summaries
    if target:
        summaries=[r for r in summaries if r['target_class'].strip().casefold()==target.casefold()]
    if search:
        needle=search.casefold(); summaries=[r for r in summaries if needle in r['candidate_name'].casefold() or needle in r['candidate_code'].casefold()]
    ranked_candidates=sorted([r for r in summaries if r['complete']], key=lambda r:(r['overall_percentage'],r['total_score']), reverse=True)
    ranked=[]; last_key=None; rank=0
    for i,r in enumerate(ranked_candidates,1):
        key=(r['overall_percentage'],r['total_score'])
        if key!=last_key: rank=i; last_key=key
        item=dict(r); item['rank']=rank; ranked.append(item)
    target_classes=sorted({r['target_class'] for r in all_summaries if r['target_class']})
    return render_template('admin_rankings.html',ranked=ranked,target_classes=target_classes,selected_target=target,search=search,total_candidates=len(summaries),completed_candidates=len(ranked))

@app.route('/admin/export/results.csv')
@admin_required
def export_results_csv():
    bid=request.args.get('bank_id','').strip() or None
    stmt=(select(Attempt,Examination.name.label('exam_name'))
        .outerjoin(Examination,Examination.id==Attempt.exam_id)
        .where(Attempt.status.in_(('submitted','expired'))))
    if bid:
        stmt=stmt.where(Attempt.bank_id==bid)
    rows=[_flatten(r,'Attempt','exam_name') for r in all_rows(
        stmt.order_by(Attempt.percentage.desc(),Attempt.id.asc()))]
    return _csv_response(rows,'crainbow-results.csv')

@app.route('/admin/export/rankings.csv')
@admin_required
def export_rankings_csv():
    bid=request.args.get('bank_id','').strip() or None; rows=_result_rows(bid)
    import csv, io
    out=io.StringIO(); w=csv.writer(out); w.writerow(['Rank','Candidate','Examination','Score','Max Score','Percentage','Grade','Status'])
    last=None; rank=0
    for i,r in enumerate(rows,1):
        key=(r['percentage'],r['score'])
        if key!=last: rank=i; last=key
        w.writerow([rank,r['candidate'],r['bank_id'],r['score'],r['max_score'],f"{r['percentage']:.1f}",_grade_label(r['percentage']),r['status']])
    return Response(out.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=crainbow-rankings.csv'})

@app.route('/admin/export/results.json')
@admin_required
def export_results_json():
    bid=request.args.get('bank_id','').strip() or None; rows=_result_rows(bid)
    payload=[{'rank':i,'candidate':r['candidate'],'bank_id':r['bank_id'],'status':r['status'],'score':r['score'],'max_score':r['max_score'],'percentage':r['percentage'],'grade':_grade_label(r['percentage']),'started_at':r['started_at'],'submitted_at':r['submitted_at']} for i,r in enumerate(rows,1)]
    return Response(json.dumps(payload,ensure_ascii=False,indent=2),mimetype='application/json',headers={'Content-Disposition':'attachment; filename=crainbow-results.json'})

@app.post('/admin/examinations/<bid>/toggle')
@admin_required
@csrf_protect
def toggle_exam(bid):
    if not admin_has_permission(current_admin()['id'],'entrance.config.activate'):
        return admin_access_error('entrance.config.activate')
    # Entrance activation is now governed by the periodized configuration layer.
    flash('Entrance examination activation is managed from Exam Configuration.','error')
    return redirect(url_for('admin_entrance_config'))

@app.post('/admin/results/<int:aid>/grant-retake')
@admin_required
@csrf_protect
def admin_grant_retake(aid):
    if not is_super_admin(current_admin()): return admin_access_error('Super Admin approval')
    a=get_attempt(aid)
    if not a: abort(404)
    if a['status'] not in ('submitted','expired'):
        flash('A retake can only be granted after the previous attempt is completed.','error')
        return redirect(url_for('admin_result_detail',aid=aid))
    candidate_key=a['candidate'].strip().casefold()
    # Do not stack unused permissions accidentally. Legacy grants predate
    # candidate_id and are still matched on the normalised candidate name.
    pending=select(RetakeGrant.id).where(RetakeGrant.bank_id==a['bank_id'],
                                         RetakeGrant.used_at.is_(None))
    if a['candidate_id']:
        pending=pending.where(RetakeGrant.candidate_id==a['candidate_id'])
    else:
        pending=pending.where(RetakeGrant.candidate_key==candidate_key)
    if one(pending.limit(1)):
        flash('A retake permission is already pending for this candidate and examination.','error')
        return redirect(url_for('admin_result_detail',aid=aid))
    db.session.add(RetakeGrant(candidate_key=candidate_key,candidate_name=a['candidate'],
        candidate_id=a['candidate_id'],bank_id=a['bank_id'],
        granted_at=datetime.now(timezone.utc).isoformat(),granted_by='admin'))
    db.session.commit()
    _create_control_item('Retake access granted',f'A one-time retake was granted for {a["candidate"]}.','retake_review','attempt',aid,current_admin()['id']); _notify_super_admins('Retake access granted',f'A one-time retake was granted for {a["candidate"]}.','warning',url_for('admin_controls'))
    flash('One-time retake access granted. Super Admin has been notified. The candidate may now log in and take this examination once more.','success')
    return redirect(url_for('admin_result_detail',aid=aid))

@app.post('/admin/results/<int:aid>/regrade')
@admin_required
@csrf_protect
def admin_regrade(aid):
    if not is_super_admin(current_admin()): return admin_access_error('Super Admin approval')
    a=get_attempt(aid)
    if not a: abort(404)
    if a['status'] not in ('submitted','expired'):
        flash('Only completed attempts can be regraded.','error'); return redirect(url_for('admin_result_detail',aid=aid))
    grade(aid,force=True); _create_control_item('Result regraded',f'Attempt #{aid} for {a["candidate"]} was regraded.','regrade_review','attempt',aid,current_admin()['id']); _notify_super_admins('Result regraded',f'Attempt #{aid} for {a["candidate"]} was regraded.','warning',url_for('admin_controls')); flash('Attempt regraded using the current verified answer key. Super Admin has been notified.','success'); return redirect(url_for('admin_result_detail',aid=aid))
