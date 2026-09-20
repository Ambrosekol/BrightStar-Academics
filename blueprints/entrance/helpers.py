"""Private helpers used only by the entrance-examination admin routes in
this package: question-bank file I/O, the entrance configuration catalogue,
and the candidate results summary/export math.
"""

import json
import os
import re

import sqlalchemy as sa
from flask import Response
from sqlalchemy import func, select

from models import AcademicSession, Attempt, Candidate, CandidatePaper, Examination, EntranceBankConfig, db
from core.db_helpers import all_rows, _flatten
from core.security import admin_scope_allows
from app import _entrance_config_select
from core.entrance import bank, bank_subject, load_banks, sync_examinations
from core.storage import data_dir


def _generate_bank_id(name, level=''):
    base=re.sub(r'[^a-z0-9]+','_',f'{name}_{level}'.lower()).strip('_') or 'question_bank'
    candidate=base; i=2
    while bank(candidate):
        candidate=f'{base}_{i}'; i+=1
    return candidate

def validate_bank_payload(data, existing_id=None):
    errors=[]
    bid=str(data.get('id','')).strip()
    name=str(data.get('name','')).strip()
    version=str(data.get('version','1.0')).strip() or '1.0'
    try:
        duration=int(data.get('duration_minutes'))*60 if data.get('duration_minutes') not in (None,'') else int(data.get('duration_seconds',3600))
    except: duration=0
    if not bid or not bid.replace('_','').replace('-','').isalnum(): errors.append('Bank ID must contain only letters, numbers, hyphens or underscores.')
    if not name: errors.append('Bank name is required.')
    if duration<60: errors.append('Duration must be at least 60 seconds.')
    if existing_id is None and bank(bid): errors.append('A question bank with this ID already exists.')
    return errors

def save_bank(b):
    path=os.path.join(data_dir(),b['id']+'.json')
    tmp=path+'.tmp'
    with open(tmp,'w',encoding='utf-8') as f: json.dump(b,f,ensure_ascii=False,indent=2)
    os.replace(tmp,path)
    sync_examinations()

def next_qid(b): return max([int(q['id']) for q in b.get('questions',[])],default=0)+1

def _entrance_config_rows():
    """Every configuration, current session first, JSS 1 before SSS 1."""
    group_order=sa.case((EntranceBankConfig.entry_group=='year7',1),else_=2)
    return [_flatten(r,'EntranceBankConfig','exam_name','session_name','question_count','is_current')
            for r in all_rows(
        _entrance_config_select(Examination.question_count,AcademicSession.is_current)
        .order_by(AcademicSession.is_current.desc(),AcademicSession.id.desc(),
                  group_order,EntranceBankConfig.subject,EntranceBankConfig.id.desc()))]

def _entrance_banks_for(me):
    banks=sorted(load_banks().values(),key=lambda b:str(b.get('name') or b.get('id')).lower())
    if not me['admin_type_system']:
        banks=[b for b in banks if admin_scope_allows(me['id'],'bank',b['id'])]
    return banks

def _grade_label(pct):
    if pct is None: return '—'
    if pct >= 80: return 'Excellent'
    if pct >= 70: return 'Very Good'
    if pct >= 60: return 'Good'
    if pct >= 50: return 'Pass'
    return 'Below Pass'

def _result_rows(bank_id=None):
    stmt=select(Attempt).where(Attempt.status.in_(('submitted','expired')))
    if bank_id:
        stmt=stmt.where(Attempt.bank_id==bank_id)
    return db.session.scalars(stmt.order_by(Attempt.percentage.desc(),
                                            Attempt.submitted_at.asc(),
                                            Attempt.id.asc())).all()

def _candidate_results_summary():
    """Build one administrative summary row per registered candidate.

    Subject scores are resolved from the candidate's assigned bank, so the
    report remains correct for both Year 7/JSS 1 and SSS 1/Year 10 sessions.
    The latest attempt for each assigned paper is used, matching the existing
    cumulative candidate result logic.

    All papers are fetched in one query rather than one query per candidate.
    """
    banks=load_banks()
    candidates=db.session.scalars(select(Candidate).where(Candidate.active==1)
        .order_by(Candidate.target_class,Candidate.candidate_name)).all()
    latest=(select(func.max(Attempt.id))
            .where(Attempt.candidate_id==CandidatePaper.candidate_id,
                   Attempt.bank_id==CandidatePaper.bank_id)
            .correlate(CandidatePaper).scalar_subquery())
    papers_by_candidate={}
    for row in all_rows(
        select(CandidatePaper.candidate_id,CandidatePaper.slot,CandidatePaper.bank_id,
               Examination.name.label('exam_name'),Attempt.status,Attempt.score,
               Attempt.max_score,Attempt.percentage,Attempt.submitted_at)
        .select_from(CandidatePaper)
        .outerjoin(Examination,Examination.bank_id==CandidatePaper.bank_id)
        .outerjoin(Attempt,Attempt.id==latest)
        .order_by(CandidatePaper.candidate_id,CandidatePaper.slot)):
        papers_by_candidate.setdefault(row['candidate_id'],[]).append(row)

    summaries=[]
    for c in candidates:
        papers=papers_by_candidate.get(c.id,[])
        subject_data={
            'english': {'label':'English', 'score':None, 'max_score':None, 'percentage':None, 'status':None},
            'mathematics': {'label':'Mathematics', 'score':None, 'max_score':None, 'percentage':None, 'status':None},
            'general_knowledge': {'label':'General Knowledge', 'score':None, 'max_score':None, 'percentage':None, 'status':None},
        }
        for paper in papers:
            b=banks.get(paper['bank_id'])
            subject=bank_subject(b or {'id':paper['bank_id'],'name':paper['exam_name'] or ''})
            if subject in subject_data:
                subject_data[subject].update(
                    score=paper['score'], max_score=paper['max_score'],
                    percentage=paper['percentage'], status=paper['status']
                )

        completed=sum(1 for d in subject_data.values() if d['status'] in ('submitted','expired'))
        total_score=sum((d['score'] or 0) for d in subject_data.values() if d['status'] in ('submitted','expired'))
        total_max=sum((d['max_score'] or 0) for d in subject_data.values() if d['status'] in ('submitted','expired'))
        overall_pct=(total_score/total_max*100) if total_max else 0
        complete=completed == len(subject_data)
        subject_percentages=[d['percentage'] for d in subject_data.values() if d['percentage'] is not None]
        subject_average=(sum(subject_percentages)/len(subject_percentages)) if subject_percentages else 0

        summaries.append({
            'id':c.id, 'candidate_code':c.candidate_code, 'candidate_name':c.candidate_name,
            'target_class':c.target_class, 'photo_path':c.photo_path, 'english':subject_data['english'],
            'mathematics':subject_data['mathematics'], 'general_knowledge':subject_data['general_knowledge'],
            'completed':completed, 'total_score':total_score, 'total_max':total_max,
            'overall_percentage':overall_pct, 'subject_average':subject_average,
            'grade':_grade_label(overall_pct) if complete else '—', 'complete':complete
        })
    return summaries

def _csv_response(rows, filename):
    import csv, io
    out=io.StringIO(); w=csv.writer(out)
    w.writerow(['Rank','Candidate','Examination','Bank ID','Status','Score','Max Score','Percentage','Grade','Started At','Submitted At'])
    for i,r in enumerate(rows,1):
        exam_name=r['exam_name'] if ('exam_name' in r.keys() and r['exam_name']) else None
        w.writerow([i,r['candidate'],exam_name or r['bank_id'],r['bank_id'],r['status'],r['score'] if r['score'] is not None else '',r['max_score'] if r['max_score'] is not None else '',f"{r['percentage']:.1f}" if r['percentage'] is not None else '',_grade_label(r['percentage']),r['started_at'],r['submitted_at'] or ''])
    return Response(out.getvalue(),mimetype='text/csv',headers={'Content-Disposition':f'attachment; filename={filename}'})
