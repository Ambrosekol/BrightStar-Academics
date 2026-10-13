"""Theory questions in school tests and examinations: what each part is worth, and turning a subject
teacher's marks into the student's result.

A test or examination can mix objective questions (four options, marked by the system) with theory
questions (a written answer the subject teacher marks). Practice tests stay objective only.

How a score is made. Each part has the marks its questions add up to (the "raw" maximum). The subject
teacher may share the paper's total out differently - say objective 20 and theory 40 of 60 - so each part
is scaled to its share: 15 of 30 objective marks is 10 of 20, 31 of 40 theory marks is 31 of 40, 41 of 60.
With no split chosen, each part keeps its own raw marks.

When the result exists. When a paper with theory is submitted, its result is written at once with no
score: a blank score never counts towards a term result (blueprints/school/helpers.py), cannot be sent on
or released (the result workflow refuses it), and keeps the report card from being ready. The moment the
last written answer is marked, the score is filled in. A released result is never changed: its marks and
the paper's split are locked.

Nothing here decides who may mark: the routes check the permission and that the teacher teaches that
student the subject (blueprints/school/marking.py).
"""

from datetime import datetime, timezone

from sqlalchemy import func, select

from core.db_helpers import all_rows, one, one_scalar, tuples
from models import (
    AcademicSession, ResultWorkflowEvent, SchoolAssessment, SchoolAssessmentAnswer, SchoolAssessmentAttempt,
    SchoolAssessmentAttemptQuestion, SchoolQuestion, SchoolStudentResult, db,
)

OBJECTIVE = 'objective'
THEORY = 'theory'
QUESTION_TYPES = (OBJECTIVE, THEORY)
THEORY_ANSWER_LIMIT = 5000      # characters in one written answer
MARKING_GUIDE_LIMIT = 5000      # characters in a question's marking guide
COMMENT_LIMIT = 500             # characters in a teacher's comment on one answer
MAX_QUESTION_MARKS = 100        # marks one theory question may carry
FINISHED = ('submitted', 'expired')


def _now():
    return datetime.now(timezone.utc).isoformat()


def _scaled(raw, raw_max, share):
    if not raw_max or not share:
        return 0.0
    return raw / raw_max * share


# ---------------------------------------------------------------- what a paper is worth
def question_totals(assessment_id):
    """(objective raw marks, theory raw marks) of the paper's questions as they stand."""
    totals = {OBJECTIVE: 0, THEORY: 0}
    for kind, marks in tuples(select(SchoolQuestion.question_type, func.coalesce(func.sum(SchoolQuestion.points), 0))
                              .where(SchoolQuestion.assessment_id == assessment_id)
                              .group_by(SchoolQuestion.question_type)):
        totals[kind if kind in totals else OBJECTIVE] += int(marks or 0)
    return totals[OBJECTIVE], totals[THEORY]


def has_theory(assessment_id):
    return one_scalar(select(SchoolQuestion.id).where(SchoolQuestion.assessment_id == assessment_id,
                                                      SchoolQuestion.question_type == THEORY).limit(1)) is not None


def split_for(assessment_id):
    """``{'objective', 'theory', 'total', 'objective_raw', 'theory_raw', 'chosen'}`` for one paper: the marks
    each part is worth (the teacher's split, or the questions' own marks when none was chosen)."""
    a = one(select(SchoolAssessment.objective_marks, SchoolAssessment.theory_marks)
            .where(SchoolAssessment.id == assessment_id))
    objective_raw, theory_raw = question_totals(assessment_id)
    chosen = bool(a and a['objective_marks'] is not None and a['theory_marks'] is not None)
    objective = float(a['objective_marks']) if chosen else float(objective_raw)
    theory = float(a['theory_marks']) if chosen else float(theory_raw)
    return {'objective': objective, 'theory': theory, 'total': objective + theory,
            'objective_raw': objective_raw, 'theory_raw': theory_raw, 'chosen': chosen}


def split_locked(assessment_id):
    """Once any result of the paper has been released its split can no longer move."""
    return one_scalar(select(SchoolStudentResult.id).where(SchoolStudentResult.assessment_id == assessment_id,
                                                           SchoolStudentResult.status == 'released').limit(1)) is not None


def check_split(assessment_id, theory_marks):
    """The (objective, theory) a teacher asked for, or raise ValueError saying why it cannot be."""
    current = split_for(assessment_id)
    total = current['total']
    if not current['theory_raw']:
        raise ValueError('This paper has no theory questions, so there is nothing to share out.')
    if theory_marks != theory_marks or theory_marks < 0 or theory_marks > total:   # NaN fails the first test
        raise ValueError(f'Give theory between 0 and {total:g} marks.')
    if not current['objective_raw'] and theory_marks != total:
        raise ValueError('This paper has no objective questions, so theory carries all of its marks.')
    if current['objective_raw'] and theory_marks == total:
        raise ValueError('Leave some marks for the objective questions, or remove them from the paper.')
    if not theory_marks:
        raise ValueError('Theory must carry some marks while the paper has theory questions.')
    return round(total - theory_marks, 2), round(theory_marks, 2)


# ---------------------------------------------------------------- one student's paper
def attempt_parts(attempt_id):
    """The marks of one submitted attempt, from its frozen questions:
    ``{'objective', 'objective_raw', 'theory', 'theory_raw', 'pending', 'has_theory'}``. ``theory`` is None
    while any written answer is unmarked. An unanswered theory question scores 0 and needs no marking."""
    questions = all_rows(select(SchoolAssessmentAttemptQuestion.question_id, SchoolAssessmentAttemptQuestion.question_type,
                                SchoolAssessmentAttemptQuestion.correct_option, SchoolAssessmentAttemptQuestion.points)
                         .where(SchoolAssessmentAttemptQuestion.attempt_id == attempt_id))
    answers = {r['question_id']: r for r in all_rows(
        select(SchoolAssessmentAnswer.question_id, SchoolAssessmentAnswer.option_index,
               SchoolAssessmentAnswer.answer_text, SchoolAssessmentAnswer.awarded)
        .where(SchoolAssessmentAnswer.attempt_id == attempt_id))}
    objective = objective_raw = theory_raw = 0
    theory = 0.0
    pending = 0
    for q in questions:
        points = int(q['points'] or 1)
        answer = answers.get(q['question_id'])
        if (q['question_type'] or OBJECTIVE) == THEORY:
            theory_raw += points
            if answer and (answer['answer_text'] or '').strip():
                if answer['awarded'] is None:
                    pending += 1
                else:
                    theory += min(float(answer['awarded']), points)
        else:
            objective_raw += points
            if answer and answer['option_index'] is not None and answer['option_index'] == q['correct_option']:
                objective += points
    return {'objective': objective, 'objective_raw': objective_raw,
            'theory': None if pending else theory, 'theory_raw': theory_raw,
            'pending': pending, 'has_theory': theory_raw > 0}


def final_score(parts, split):
    """The paper's score out of its total, or None while theory is unmarked."""
    if parts['theory'] is None:
        return None
    return round(_scaled(parts['objective'], parts['objective_raw'], split['objective'])
                 + _scaled(parts['theory'], parts['theory_raw'], split['theory']), 2)


def _first_status(assessment_id, at):
    """What a newly scored result starts as, by the same rule as an objective-only paper: 'approved' when the
    session's release time has already passed (so it is released with the rest), otherwise 'entered'."""
    release = one_scalar(select(AcademicSession.result_release_at)
                         .join(SchoolAssessment, SchoolAssessment.session_id == AcademicSession.id)
                         .where(SchoolAssessment.id == assessment_id))
    return 'approved' if release and release <= at else 'entered'


def sync_result(attempt_id, actor_id=None, reason=None):
    """Write the attempt's result as its marks stand now. Returns 'released' when the result is already
    released (and so was left alone), otherwise the result's status. The caller commits."""
    attempt = db.session.get(SchoolAssessmentAttempt, attempt_id)
    if attempt is None or attempt.status not in FINISHED:
        return None
    a = one(select(SchoolAssessment.subject_id, SchoolAssessment.session_id, SchoolAssessment.term)
            .where(SchoolAssessment.id == attempt.assessment_id))
    if a is None:
        return None
    rows = db.session.scalars(select(SchoolStudentResult).where(
        SchoolStudentResult.student_id == attempt.student_id,
        SchoolStudentResult.assessment_id == attempt.assessment_id).order_by(SchoolStudentResult.id.desc())).all()
    if any(r.status == 'released' for r in rows):
        return 'released'
    parts = attempt_parts(attempt_id)
    split = split_for(attempt.assessment_id)
    score = final_score(parts, split)
    attempt.theory_status = 'pending' if parts['pending'] else 'marked'
    now = _now()
    row = rows[0] if rows else None
    if row is None:
        row = SchoolStudentResult(student_id=attempt.student_id, assessment_id=attempt.assessment_id,
                                  subject_id=a['subject_id'], term=a['term'] or 'Full Session',
                                  session_id=a['session_id'], created_at=attempt.submitted_at or now,
                                  status='entered' if score is None else _first_status(attempt.assessment_id, now),
                                  score=score, max_score=split['total'])
        db.session.add(row)
        return row.status
    changed = row.score != score or row.max_score != split['total']
    first_score = row.score is None and score is not None
    row.score, row.max_score = score, split['total']
    if changed:
        row.updated_at, row.updated_by = now, actor_id
        if row.status in ('verified', 'approved'):
            # A mark that changes after it was checked goes back to be checked again, never straight out.
            db.session.add(ResultWorkflowEvent(result_id=row.id, from_status=row.status, to_status='entered',
                                               actor_admin_id=actor_id, reason=reason or 'Theory marks changed',
                                               created_at=now))
            row.status, row.verified_by, row.approved_by = 'entered', None, None
        elif first_score and row.status == 'entered':
            # The paper's first score follows the same rule as an objective-only paper's.
            row.status = _first_status(attempt.assessment_id, now)
    return row.status


def refresh_paper(assessment_id, actor_id=None, reason=None):
    """Re-score every finished attempt of a paper (after its split changed). Returns how many were looked at."""
    ids = [i for (i,) in tuples(select(SchoolAssessmentAttempt.id).where(
        SchoolAssessmentAttempt.assessment_id == assessment_id, SchoolAssessmentAttempt.status.in_(FINISHED)))]
    for attempt_id in ids:
        sync_result(attempt_id, actor_id, reason)
    return len(ids)


# ---------------------------------------------------------------- what is waiting to be marked
def papers_with_theory():
    """Every test and examination with theory questions: ``[{'id','title','assessment_type','class_id',
    'subject_id','term','session_id','active'}]``. Practice tests never have theory."""
    theory = select(SchoolQuestion.assessment_id).where(SchoolQuestion.question_type == THEORY).distinct()
    return all_rows(select(SchoolAssessment.id, SchoolAssessment.title, SchoolAssessment.assessment_type,
                           SchoolAssessment.class_id, SchoolAssessment.subject_id, SchoolAssessment.term,
                           SchoolAssessment.session_id, SchoolAssessment.active)
                    .where(SchoolAssessment.id.in_(theory), SchoolAssessment.assessment_type != 'practice'))


def script_counts(assessment_ids):
    """``{assessment_id: [(student_id, theory_status), ...]}`` of finished attempts."""
    out = {}
    if not assessment_ids:
        return out
    for aid, sid, status in tuples(select(SchoolAssessmentAttempt.assessment_id, SchoolAssessmentAttempt.student_id,
                                          SchoolAssessmentAttempt.theory_status)
                                   .where(SchoolAssessmentAttempt.assessment_id.in_(list(assessment_ids)),
                                          SchoolAssessmentAttempt.status.in_(FINISHED))):
        out.setdefault(aid, []).append((sid, status))
    return out
