"""Printed papers: a test or examination sat on paper instead of in the student portal.

The school prints the question paper and one answer booklet per student (name, admission number, class and passport
photograph filled in), the students write, and the subject teacher enters the marks: the objective total they counted
and a mark for each theory question, or that the student was absent. The marks become the student's result exactly
as a CBT paper's do - through the paper's objective/theory split and core/theory_marking.write_result, with the same
locks (a released result is never changed; a changed mark on a checked result goes back to be checked).

A printed paper is never opened to students online (blueprints/school/routes.py refuses to), so its questions can
only be read on paper. Who may print or mark it is decided by the routes (blueprints/school/marking.py).
"""

import math
from datetime import datetime, timezone

from sqlalchemy import and_, select

from core.db_helpers import all_rows, one_scalar, tuples
from core.school_structure import class_is_senior, takes_subject
from core.theory_marking import OBJECTIVE, THEORY, split_for, write_result
from models import (
    SchoolAssessment, SchoolPrintedMark, SchoolPrintedScript, SchoolQuestion, SchoolStudentResult, SchoolSubject,
    Student, StudentEnrolment, db,
)

PRINTED = 'printed'
CBT = 'cbt'
DELIVERIES = (CBT, PRINTED)


def _now():
    return datetime.now(timezone.utc).isoformat()


def is_printed(assessment_id):
    return one_scalar(select(SchoolAssessment.delivery).where(SchoolAssessment.id == assessment_id)) == PRINTED


def paper_questions(assessment_id):
    """The paper's questions in order, objective and theory, as the paper and booklets print them."""
    return all_rows(select(SchoolQuestion.id, SchoolQuestion.question_type, SchoolQuestion.points,
                           SchoolQuestion.question_text, SchoolQuestion.instruction, SchoolQuestion.image_path,
                           SchoolQuestion.option_a, SchoolQuestion.option_b, SchoolQuestion.option_c, SchoolQuestion.option_d)
                    .where(SchoolQuestion.assessment_id == assessment_id)
                    .order_by(SchoolQuestion.sort_order, SchoolQuestion.id))


def roster(paper):
    """The students who sit the paper: active, enrolled in its class for its session, and (in SSS) taking its
    subject. The routes narrow this to the students the teacher teaches. Ordered by surname."""
    rows = all_rows(select(Student.id, Student.first_name, Student.middle_name, Student.last_name, Student.admission_no,
                           Student.photo_path, Student.department)
                    .join(StudentEnrolment, and_(StudentEnrolment.student_id == Student.id, StudentEnrolment.active == 1,
                                                 StudentEnrolment.class_id == paper['class_id'],
                                                 StudentEnrolment.session_id == paper['session_id']))
                    .where(Student.active == 1, Student.archived_at.is_(None))
                    .order_by(Student.last_name, Student.first_name, Student.id))
    if class_is_senior(paper['class_id']):
        departments = one_scalar(select(SchoolSubject.departments).where(SchoolSubject.id == paper['subject_id']))
        rows = [r for r in rows if takes_subject(r['department'], departments)]
    out, seen = [], set()
    for r in rows:
        if r['id'] in seen:
            continue
        seen.add(r['id'])
        name = ' '.join(p for p in (r['first_name'], r['middle_name'], r['last_name']) if p)
        out.append({**r, 'name': name, 'list_name': f"{r['last_name'] or ''} {r['first_name'] or ''}".strip()})
    return out


def scripts(assessment_id, student_ids):
    """``{student_id: {'id', 'objective_score', 'absent', 'marks': {question_id: awarded}}}`` of entered scripts."""
    out = {}
    if not student_ids:
        return out
    for r in all_rows(select(SchoolPrintedScript.id, SchoolPrintedScript.student_id, SchoolPrintedScript.objective_score,
                             SchoolPrintedScript.absent)
                      .where(SchoolPrintedScript.assessment_id == assessment_id,
                             SchoolPrintedScript.student_id.in_(list(student_ids)))):
        out[r['student_id']] = {'id': r['id'], 'objective_score': r['objective_score'], 'absent': bool(r['absent']), 'marks': {}}
    by_script = {s['id']: s for s in out.values()}
    if by_script:
        for script_id, qid, awarded in tuples(select(SchoolPrintedMark.script_id, SchoolPrintedMark.question_id,
                                                     SchoolPrintedMark.awarded)
                                              .where(SchoolPrintedMark.script_id.in_(list(by_script)))):
            by_script[script_id]['marks'][qid] = awarded
    return out


def _layout(assessment_id):
    questions = paper_questions(assessment_id)
    theory = [q for q in questions if (q['question_type'] or OBJECTIVE) == THEORY]
    objective_raw = sum(int(q['points'] or 1) for q in questions if (q['question_type'] or OBJECTIVE) != THEORY)
    return theory, objective_raw


def script_state(script, theory, objective_raw):
    """'absent', 'marked' (every mark entered) or 'pending' for one student's script (None: nothing entered)."""
    if script is None:
        return 'pending'
    if script['absent']:
        return 'absent'
    if objective_raw and script['objective_score'] is None:
        return 'pending'
    if any(script['marks'].get(q['id']) is None for q in theory):
        return 'pending'
    return 'marked'


def script_score(script, theory, objective_raw, split):
    """The script's score out of the paper's total, or None while any mark is missing."""
    if script is None or script['absent'] or script_state(script, theory, objective_raw) != 'marked':
        return None
    theory_raw = sum(int(q['points'] or 1) for q in theory)
    theory_got = sum(min(float(script['marks'][q['id']]), int(q['points'] or 1)) for q in theory)
    objective_got = min(float(script['objective_score'] or 0), objective_raw)
    part = lambda got, raw, share: (got / raw * share) if raw and share else 0.0  # noqa: E731
    return round(part(objective_got, objective_raw, split['objective']) + part(theory_got, theory_raw, split['theory']), 2)


def is_released(assessment_id, student_id):
    return one_scalar(select(SchoolStudentResult.id).where(
        SchoolStudentResult.assessment_id == assessment_id, SchoolStudentResult.student_id == student_id,
        SchoolStudentResult.status == 'released').limit(1)) is not None


def check_entry(raw_objective, absent, raw_marks, theory, objective_raw):
    """Turn one student's typed marks into (objective, absent, {question_id: mark}) or raise ValueError with why.
    A blank is "not entered yet"."""
    def number(raw, top, label):
        raw = (raw or '').strip()
        if raw == '':
            return None
        try:
            value = float(raw)
        except ValueError:
            value = float('nan')
        if not math.isfinite(value) or value < 0 or value > top:
            raise ValueError(f'{label}: give a mark from 0 to {top:g}.')
        return round(value, 2)

    if absent:
        return None, True, {}
    objective = number(raw_objective, objective_raw, 'Objective') if objective_raw else None
    marks = {}
    for n, q in enumerate(theory, 1):
        value = number(raw_marks.get(q['id']), int(q['points'] or 1), f'Theory question {n}')
        if value is not None:
            marks[q['id']] = value
    return objective, False, marks


def save_script(assessment_id, student_id, objective, absent, marks, actor_id):
    """Store one student's marks and re-score their result. Returns (changed, result status). The caller commits."""
    theory, objective_raw = _layout(assessment_id)
    theory_ids = {q['id'] for q in theory}
    now = _now()
    row = db.session.scalars(select(SchoolPrintedScript).where(SchoolPrintedScript.assessment_id == assessment_id,
                                                               SchoolPrintedScript.student_id == student_id)).first()
    nothing = not absent and objective is None and not marks
    if row is None:
        if nothing:
            return False, None
        row = SchoolPrintedScript(assessment_id=assessment_id, student_id=student_id, updated_at=now, updated_by=actor_id)
        db.session.add(row)
        db.session.flush()
    changed = bool(row.absent) != bool(absent) or row.objective_score != objective
    row.absent, row.objective_score = 1 if absent else 0, objective
    existing = {m.question_id: m for m in db.session.scalars(select(SchoolPrintedMark).where(SchoolPrintedMark.script_id == row.id))}
    for qid, mark in existing.items():
        if qid not in marks or qid not in theory_ids:
            db.session.delete(mark)
            changed = True
    for qid, value in marks.items():
        if qid not in theory_ids:
            continue
        if qid in existing and existing[qid].awarded == value:
            continue
        if qid in existing:
            existing[qid].awarded, existing[qid].marked_by, existing[qid].marked_at = value, actor_id, now
        else:
            db.session.add(SchoolPrintedMark(script_id=row.id, question_id=qid, awarded=value, marked_by=actor_id, marked_at=now))
        changed = True
    if changed:
        row.updated_at, row.updated_by = now, actor_id
    db.session.flush()
    return changed, score_student(assessment_id, student_id, actor_id, 'Printed marks changed')


def score_student(assessment_id, student_id, actor_id=None, reason=None):
    """Write the student's result from their script as it stands. The caller commits."""
    theory, objective_raw = _layout(assessment_id)
    script = scripts(assessment_id, [student_id]).get(student_id)
    if script is None:
        return None
    split = split_for(assessment_id)
    return write_result(student_id, assessment_id, script_score(script, theory, objective_raw, split), split['total'],
                        actor_id, reason, absent=script['absent'])


def refresh_printed(assessment_id, actor_id=None, reason=None):
    """Re-score every entered script of a printed paper (after its split changed). Returns how many."""
    ids = [sid for (sid,) in tuples(select(SchoolPrintedScript.student_id)
                                    .where(SchoolPrintedScript.assessment_id == assessment_id))]
    for sid in ids:
        score_student(assessment_id, sid, actor_id, reason)
    return len(ids)


def paper_states(assessment_id, student_ids):
    """``{student_id: state}`` for the given students of a printed paper."""
    theory, objective_raw = _layout(assessment_id)
    found = scripts(assessment_id, student_ids)
    return {sid: script_state(found.get(sid), theory, objective_raw) for sid in student_ids}


def can_change_delivery(assessment_id):
    """A paper's delivery can change only before anyone has sat it: no attempt, no entered script, no result."""
    from models import SchoolAssessmentAttempt
    for model, column in ((SchoolAssessmentAttempt, SchoolAssessmentAttempt.assessment_id),
                          (SchoolPrintedScript, SchoolPrintedScript.assessment_id),
                          (SchoolStudentResult, SchoolStudentResult.assessment_id)):
        if one_scalar(select(column).where(column == assessment_id).limit(1)) is not None:
            return False
    return True
