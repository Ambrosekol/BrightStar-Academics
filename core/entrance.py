"""Entrance-exam / candidate-portal shared domain logic.

Question-bank loading, candidate record/attempt lookups, grading, and result
(PNG) rendering used by both blueprints/entrance/ (the admin side) and
blueprints/candidate_portal/ (the self-service side) - and, for a few names,
by blueprints/finance/ and blueprints/student_portal/ too. Kept out of any
single blueprint package since no one domain owns it.
"""

import base64
import json
import mimetypes
import os
import secrets
import string
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import and_, func, select

from app import _school_current_session
from models import (
    Answer, Attempt, AttemptQuestion, Candidate, CandidatePaper,
    EntranceBankConfig, Examination, RetakeGrant, db,
)
from core.db_helpers import all_rows, insert_stmt, obj, one, one_scalar, tuples, _flatten
from core.marks import total as total_marks
from core.storage import data_dir, generated_dir, stored_upload_path


def _answers_for_attempt(aid):
    return {qid:opt for qid,opt in tuples(
        select(Answer.question_id,Answer.option_index).where(Answer.attempt_id==aid))}

def _candidate_papers(candidate_id):
    """A candidate's assigned papers, each with its latest attempt summary."""
    latest=(select(func.max(Attempt.id))
        .where(Attempt.candidate_id==CandidatePaper.candidate_id,
               Attempt.bank_id==CandidatePaper.bank_id)
        .correlate(CandidatePaper).scalar_subquery())
    return [_flatten(r,'CandidatePaper','exam_name','attempt_status','attempt_id','percentage')
            for r in all_rows(
        select(CandidatePaper,Examination.name.label('exam_name'),
               Attempt.status.label('attempt_status'),
               Attempt.id.label('attempt_id'),Attempt.percentage)
            .select_from(CandidatePaper)
            .outerjoin(Examination,Examination.bank_id==CandidatePaper.bank_id)
            .outerjoin(Attempt,Attempt.id==latest)
            .where(CandidatePaper.candidate_id==candidate_id)
            .order_by(CandidatePaper.slot))]

def load_banks():
    banks={}
    folder=data_dir()
    for fn in os.listdir(folder):
        if fn.endswith('.json') and fn!='manifest.json':
            try:
                with open(os.path.join(folder,fn),encoding='utf-8') as f:
                    b=json.load(f)
                if isinstance(b,dict) and b.get('id') and isinstance(b.get('questions'),list):
                    banks[b['id']]=b
            except (OSError, json.JSONDecodeError):
                continue
    return banks

def bank(bid): return load_banks().get(bid)

def normalize_entry_group(value):
    """Map the administrator's target-class wording to the two entrance sets."""
    v=''.join(ch.lower() for ch in str(value or '') if ch.isalnum())
    if any(token in v for token in ('jss1','year7','primary6','primarysix')):
        return 'year7'
    if any(token in v for token in ('ss1','sss1','year10','jss3','jss3to')):
        return 'year10'
    return None

def bank_subject(bank_obj):
    value=' '.join(str(bank_obj.get(k,'')) for k in ('subject','name','id')).lower()
    if 'general knowledge' in value or 'general_knowledge' in value or 'generalknowledge' in value:
        return 'general_knowledge'
    if 'mathemat' in value:
        return 'mathematics'
    if 'english' in value:
        return 'english'
    return None

ENTRANCE_SUBJECT_LABELS = {
    'mathematics': 'Mathematics',
    'english': 'English',
    'general_knowledge': 'General Knowledge',
}

# Schools name this entry stage differently — some say "Year 7"/"Year 10" (the British-style
# naming), others "JSS 1"/"SSS 1" (the Nigerian naming). Rather than pick one, every candidate-
# and staff-facing label shows both together, so nobody has to translate in their head.
ENTRY_GROUP_LABELS = {
    'year7': 'Year 7 / JSS 1',
    'year10': 'Year 10 / SSS 1',
}

def entry_group_label(group):
    """The candidate-facing name of an entrance set ('year7' or 'year10'); '' if unknown."""
    return ENTRY_GROUP_LABELS.get(group, '')

def entrance_subject_label(bank_id, fallback=''):
    """Return the candidate-facing subject name; never expose the internal bank ID."""
    subject = bank_subject(bank(bank_id) or {'id': bank_id, 'name': fallback or ''})
    return ENTRANCE_SUBJECT_LABELS.get(subject) or fallback or 'Assessment'

def entrance_paper_label(slot, bank_id, fallback=''):
    """Return a concise human-facing entrance paper label."""
    return f'Paper {slot}: {entrance_subject_label(bank_id, fallback)}'

def entrance_bank_display_name(bank_id, fallback=''):
    """Return the human-facing name for an entrance question bank.

    Internal bank IDs and the verbose stored examination title remain unchanged;
    this helper only controls what administrators see in question-bank contexts.
    """
    b = bank(bank_id) or {'id': bank_id, 'name': fallback or '', 'level': ''}
    subject = entrance_subject_label(bank_id, b.get('name') or fallback or '')
    group = bank_entry_group(b)
    target = entry_group_label(group)
    if target:
        return f'{subject} - Entrance Examination into {target}'
    # Safe fallback for a bank whose entry level cannot be resolved.
    return f'{subject} - Entrance Examination'

def bank_entry_group(bank_obj):
    explicit=normalize_entry_group(bank_obj.get('entry_group') or bank_obj.get('entry_level'))
    if explicit:
        return explicit
    # The existing Phase 6 banks use level/name/id rather than entry_group.
    return normalize_entry_group(' '.join(str(bank_obj.get(k,'')) for k in ('level','name','id')))

def _entrance_config_row(entry_group, subject, session_id=None, term='Full Session', active_only=True):
    if session_id is None:
        current=_school_current_session(); session_id=current['id'] if current else None
    if not session_id: return None
    stmt=(select(EntranceBankConfig,Examination.name.label('exam_name'),
                 Examination.active.label('exam_active'))
          .join(Examination,Examination.bank_id==EntranceBankConfig.bank_id)
          .where(EntranceBankConfig.entry_group==entry_group,
                 EntranceBankConfig.subject==subject,
                 EntranceBankConfig.session_id==session_id,
                 EntranceBankConfig.term==term))
    if active_only:
        stmt=stmt.where(EntranceBankConfig.active==1)
    row=one(stmt.order_by(EntranceBankConfig.id.desc()).limit(1))
    return _flatten(row,'EntranceBankConfig','exam_name','exam_active') if row else None

def _valid_question_configuration(question_count):
    try:
        count=int(question_count)
    except (TypeError,ValueError):
        return False,None
    if count < 1:
        return False,None
    marks=round(100.0/count,2)
    if round(marks*count,2) != 100.0:
        return False,marks
    return True,marks

def required_papers_for_target(target_class):
    """Return active entrance papers for the target class from the current configuration."""
    group=normalize_entry_group(target_class)
    labels={'mathematics':'Mathematics','english':'English','general_knowledge':'General Knowledge'}
    if not group:
        return [], list(labels.values())
    current=_school_current_session()
    ordered=[]; missing=[]
    if current:
        for subject in ('mathematics','english','general_knowledge'):
            cfg=_entrance_config_row(group,subject,current['id'])
            if not cfg:
                missing.append(labels[subject])
            else:
                b=bank(cfg['bank_id'])
                if b and b.get('questions'):
                    ordered.append(b)
                else:
                    missing.append(labels[subject])
    else:
        missing=list(labels.values())
    return ordered,missing

def sync_examinations():
    """Mirror the JSON question banks into the examinations table."""
    now=datetime.now(timezone.utc).isoformat()
    for b in load_banks().values():
        stmt=insert_stmt(Examination).values(
            bank_id=b['id'],name=b.get('name',b['id']),
            duration_seconds=int(b.get('duration_seconds',3600)),
            version=b.get('version','1.0'),question_count=len(b['questions']),created_at=now)
        db.session.execute(stmt.on_conflict_do_update(
            index_elements=['bank_id'],
            set_={'name':stmt.excluded.name,'duration_seconds':stmt.excluded.duration_seconds,
                  'version':stmt.excluded.version,'question_count':stmt.excluded.question_count}))
    db.session.commit()

def get_attempt(aid):
    return obj(Attempt, aid)

def remaining(a): return max(0,int((datetime.fromisoformat(a['expires_at'])-datetime.now(timezone.utc)).total_seconds()))

def grade(aid, auto=False, force=False):
    a=obj(Attempt, aid)
    if not a: return None
    if a.status in ('submitted','expired') and not force: return a
    snapshot=tuples(select(AttemptQuestion.question_id,AttemptQuestion.correct_option,
                           AttemptQuestion.points).where(AttemptQuestion.attempt_id==aid))
    answers=_answers_for_attempt(aid)
    if snapshot:
        # Marks can be fractions (40 questions of 2.5 marks each make 100), so they are never
        # rounded to whole numbers here.
        score=total_marks((points or 1) for qid,correct,points in snapshot if answers.get(qid)==correct)
        max_score=total_marks((points or 1) for _,_,points in snapshot)
    else:
        # Legacy attempts are graded from the legacy bank only until they are archived;
        # all new attempts are snapshotted at start.
        b=bank(a.bank_id)
        score=total_marks(q.get('points',1) for q in (b or {}).get('questions',[]) if answers.get(q['id'])==q['answer'])
        max_score=total_marks(q.get('points',1) for q in (b or {}).get('questions',[]))
    pct=(score/max_score*100) if max_score else 0
    if not force:
        a.submitted_at=datetime.now(timezone.utc).isoformat()
        a.status='expired' if auto else 'submitted'
    a.score=score; a.max_score=max_score; a.percentage=pct
    db.session.commit()
    return a

def candidate_record(cid):
    if not cid: return None
    return db.session.scalars(
        select(Candidate).where(Candidate.id==cid,Candidate.active==1)).first()

def candidate_has_unused_retake(candidate_id, candidate_name, bank_id):
    return one(select(RetakeGrant.id).where(
        RetakeGrant.bank_id==bank_id,RetakeGrant.candidate_id==candidate_id,
        RetakeGrant.used_at.is_(None)).order_by(RetakeGrant.id.asc()).limit(1))

def _candidate_paper_attempts(cid):
    """Each assigned paper with its most recent attempt."""
    latest=(select(func.max(Attempt.id))
            .where(Attempt.candidate_id==CandidatePaper.candidate_id,
                   Attempt.bank_id==CandidatePaper.bank_id)
            .correlate(CandidatePaper).scalar_subquery())
    return all_rows(
        select(CandidatePaper.id.label('paper_id'),CandidatePaper.slot,
               CandidatePaper.bank_id,Examination.name.label('exam_name'),
               Attempt.id.label('attempt_id'),Attempt.status,Attempt.score,
               Attempt.max_score,Attempt.percentage,Attempt.started_at,
               Attempt.submitted_at)
        .select_from(CandidatePaper)
        .outerjoin(Examination,Examination.bank_id==CandidatePaper.bank_id)
        .outerjoin(Attempt,Attempt.id==latest)
        .where(CandidatePaper.candidate_id==cid)
        .order_by(CandidatePaper.slot))

def candidate_cumulative(cid):
    rows=_candidate_paper_attempts(cid)

    # Per-attempt question/correct counts in one query rather than one per paper.
    attempt_ids=[r['attempt_id'] for r in rows
                 if r['status'] in ('submitted','expired') and r['attempt_id']]
    counts={}
    if attempt_ids:
        for aid,qcount,correct in tuples(
            select(AttemptQuestion.attempt_id,
                   func.count().label('question_count'),
                   func.sum(sa.case(
                       (and_(Answer.option_index.is_not(None),
                             AttemptQuestion.correct_option.is_not(None),
                             Answer.option_index==AttemptQuestion.correct_option), 1),
                       else_=0)).label('correct_count'))
            .select_from(AttemptQuestion)
            .outerjoin(Answer,and_(Answer.attempt_id==AttemptQuestion.attempt_id,
                                   Answer.question_id==AttemptQuestion.question_id))
            .where(AttemptQuestion.attempt_id.in_(attempt_ids))
            .group_by(AttemptQuestion.attempt_id)):
            counts[aid]=(int(qcount or 0),int(correct or 0))

    enriched=[]
    for row in rows:
        item=dict(row)
        exam_name=str(item.get('exam_name') or '').strip()
        subject_label=exam_name or ''
        if not subject_label:
            # Fall back to the bank id when the examination row is missing.
            bank_text=str(item.get('bank_id') or '').lower()
            if 'english' in bank_text:
                subject_label='English'
            elif 'math' in bank_text:
                subject_label='Mathematics'
            elif 'spell' in bank_text or 'vocab' in bank_text:
                subject_label='Spelling & Vocabulary'
            else:
                subject_label='Subject'
        item['subject_label']=subject_label
        item['question_count'],item['correct_count']=counts.get(item.get('attempt_id'),(0,0))
        enriched.append(item)

    done=[r for r in enriched if r['status'] in ('submitted','expired')]
    total_score=sum((r['score'] or 0) for r in done)
    total_max=sum((r['max_score'] or 0) for r in done)
    completed=len(done)
    pct=(total_score/total_max*100) if total_max else 0
    return enriched,total_score,total_max,pct,completed

def _result_file_data_uri(path):
    """Return a local image file as a browser-safe data URI."""
    try:
        p=Path(path)
        if not p.exists() or not p.is_file():
            return ''
        mime=mimetypes.guess_type(str(p))[0] or 'application/octet-stream'
        encoded=base64.b64encode(p.read_bytes()).decode('ascii')
        return f'data:{mime};base64,{encoded}'
    except Exception:
        return ''

def _candidate_photo_filesystem_path(candidate):
    """The candidate's stored photograph, or None. Only ever a file inside the current school's own
    uploads folder: a path that leads anywhere else resolves to nothing."""
    keys=list(candidate.keys())
    for key in ('photo_path','photo','candidate_photo','passport_photo','image_path'):
        if key in keys and candidate[key]:
            found=stored_upload_path(str(candidate[key]).strip())
            if found and os.path.isfile(found):
                return Path(found)
    return None

def _school_logo_data_uri():
    """The current school's own logo as a data URI, or '' if it has not uploaded one.

    Never another school's, and never a file shipped with the platform: a school without a logo
    gets its own name in that place on a result, not somebody else's picture.
    """
    from core.branding import school_brand  # deferred: core.branding imports the app

    found=stored_upload_path(school_brand().get('logo_path') or '')
    return _result_file_data_uri(found) if found else ''

def _find_chrome():
    """The Chrome or Chromium program that draws result images, or None.

    ``BRIGHTSTARS_CHROME`` names it outright; otherwise the usual Windows install folders and then
    the names a Linux or macOS server would have it under are tried.
    """
    import shutil

    named=os.environ.get('BRIGHTSTARS_CHROME','').strip()
    if named:
        return Path(named) if Path(named).is_file() else None
    for path in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
        if Path(path).exists():
            return Path(path)
    for name in ('google-chrome','google-chrome-stable','chromium','chromium-browser','chrome'):
        found=shutil.which(name)
        if found:
            return Path(found)
    return None

def _chrome_result_png(candidate, papers, total_score, total_max, pct,
                       total_questions, total_correct,
                       average_subject_percentage, grade):
    """Render the official result card to PNG using installed Chrome."""

    import subprocess
    import struct
    from pathlib import Path

    from flask import (
        render_template,
        has_request_context,
        has_app_context,
        current_app,
    )

    chrome = _find_chrome()

    if chrome is None:
        raise RuntimeError(
            "Google Chrome was not found. Install it, or set BRIGHTSTARS_CHROME to its full path."
        )

    # In the school's own folder, which nothing serves: the route that asks for the image reads
    # it, sends it and deletes it, so it can never be fetched by another school or by anyone else.
    output_dir = Path(generated_dir()) / "results"

    output_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    token = datetime.now().strftime(
        "%Y%m%d_%H%M%S_%f"
    )

    png_path = (
        output_dir /
        f"result_{candidate['id']}_{token}.png"
    )

    html_path = (
        output_dir /
        f"result_{candidate['id']}_{token}.html"
    )

    photo_path = _candidate_photo_filesystem_path(
        candidate
    )

    # A school with no logo, or a candidate with no photograph, still gets a result: the page shows
    # the school's name where the logo would be, and leaves the photograph out.
    logo_data = _school_logo_data_uri()

    photo_data = _result_file_data_uri(
        photo_path
    ) if photo_path else ""

    render_kwargs = {
        "candidate": candidate,
        "papers": papers,
        "total_score": total_score,
        "total_max": total_max,
        "pct": pct,
        "total_questions": total_questions,
        "total_correct": total_correct,
        "average_subject_percentage": average_subject_percentage,
        "grade": grade,
        "logo_data": logo_data,
        "photo_data": photo_data,
    }

    if has_request_context():

        html = render_template(
            "admin_candidate_result_image.html",
            **render_kwargs
        )

    elif has_app_context():

        app_obj = current_app._get_current_object()

        with app_obj.test_request_context(
            "/admin/candidates/result/image"
        ):
            html = render_template(
                "admin_candidate_result_image.html",
                **render_kwargs
            )

    else:
        raise RuntimeError(
            "Renderer requires a Flask application/request context."
        )

    html_path.write_text(
        html,
        encoding="utf-8"
    )

    try:

        command = [
            str(chrome),
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--allow-file-access-from-files",
            "--run-all-compositor-stages-before-draw",
            "--virtual-time-budget=3000",
            "--window-size=1400,1100",
            f"--screenshot={str(png_path)}",
            "file:///" +
            str(html_path).replace("\\", "/"),
        ]

        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=60,
        )

        if completed.returncode != 0:

            detail = (
                completed.stderr
                or completed.stdout
                or "Unknown Chrome error."
            )

            raise RuntimeError(
                "Chrome failed:\n" + detail
            )

        if not png_path.is_file():
            raise RuntimeError(
                "Chrome did not create the result PNG."
            )

        size = png_path.stat().st_size

        if size < 10000:
            raise RuntimeError(
                f"Generated PNG is too small: {size} bytes."
            )

        with png_path.open("rb") as fh:

            signature = fh.read(8)

            if signature != b"\x89PNG\r\n\x1a\n":
                raise RuntimeError(
                    "Generated file is not a valid PNG."
                )

            ihdr_length = fh.read(4)
            ihdr_type = fh.read(4)

            if (
                len(ihdr_length) != 4
                or ihdr_type != b"IHDR"
            ):
                raise RuntimeError(
                    "PNG IHDR validation failed."
                )

            dimensions = fh.read(8)

            if len(dimensions) != 8:
                raise RuntimeError(
                    "PNG dimensions could not be read."
                )

            width, height = struct.unpack(
                ">II",
                dimensions
            )

        if width < 500 or height < 500:
            raise RuntimeError(
                f"Invalid PNG dimensions: {width}x{height}"
            )

        return png_path, html_path

    except Exception:

        if png_path.exists():
            try:
                png_path.unlink()
            except Exception:
                pass

        raise

    finally:

        if html_path.exists():
            try:
                html_path.unlink()
            except Exception:
                pass

def premium_result_metrics(papers):
    """Calculate cumulative presentation metrics from completed papers."""
    completed_papers = [
        p for p in papers
        if p.get("status") in ("submitted", "expired")
    ]

    total_questions = sum(
        int(p.get("question_count") or 0)
        for p in completed_papers
    )

    total_correct = sum(
        int(p.get("correct_count") or 0)
        for p in completed_papers
    )

    percentages = [
        float(p["percentage"])
        for p in completed_papers
        if p.get("percentage") is not None
    ]

    average_subject_percentage = (
        sum(percentages) / len(percentages)
        if percentages else 0
    )

    return (
        total_questions,
        total_correct,
        average_subject_percentage
    )

def _new_candidate_code(candidate_name='',target_class=''):
    """The next candidate code, made by the school's own numbering pattern (tenants/<code>/numbering.json)."""
    from core.numbering import new_candidate_code  # deferred: it reads the school's database

    return new_candidate_code(candidate_name=candidate_name,target_class=target_class)

def _new_candidate_password():
    alphabet=string.ascii_uppercase+string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(8))

