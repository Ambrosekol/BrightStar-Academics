"""Senior secondary departments in the entrance examination: the department a candidate wants, the questions that
test each department, and the department the results point to.

A candidate entering SSS 1 chooses a department when registered (Candidate.department_choice: Science, Art or
Commercial, core/school_structure.DEPARTMENTS). A question in any bank may be tagged with a department
(``"department": "Science"`` in the bank file, or the Department field on the question form); an untagged question
is general. When a paper's bank has tagged questions, each candidate is served an equal share from every department
(pick_questions), so every SSS candidate answers questions from all three, whichever they chose. The tag is frozen
with the served question (AttemptQuestion.department), like its text and answer, so a later edit to the bank cannot
change what a finished paper says about a candidate.

Before admission, department_fits() works out how well the candidate did in each department: the share of the marks
they earned on that department's questions, together with the paper closest to it (Mathematics for Science and
Commercial, English for Art). recommend() then keeps their own choice when it is as good as any (within FIT_MARGIN
points of their best, and at least FIT_PASS), and otherwise names the department they did best in, saying why. The
admissions officer sees all of it on the waitlist and can choose any department when admitting
(blueprints/entrance/admissions.py); the recommendation never decides on its own.
"""

import random

from sqlalchemy import func, select

from core.school_structure import DEPARTMENTS, clean_department
from models import Answer, Attempt, AttemptQuestion, CandidatePaper, db

# The paper that speaks to each department besides its own questions.
RELATED_PAPER = {'Science': 'mathematics', 'Commercial': 'mathematics', 'Art': 'english'}
RELATED_PAPER_LABEL = {'mathematics': 'Mathematics', 'english': 'English'}
# How much the department's own questions count against its related paper, when both are there.
OWN_QUESTIONS_WEIGHT = 0.6
FIT_MARGIN = 5.0     # the choice is kept when it is within this many points of the best department...
FIT_PASS = 50.0      # ...and the candidate scored at least this much in it
DONE = ('submitted', 'expired')


def question_department(question):
    """The department a bank question is tagged with, or None for a general question."""
    return clean_department((question or {}).get('department'))


def pick_questions(questions, count, rng=random):
    """``count`` questions drawn at random from a bank. When the bank has questions tagged with departments, a
    quarter of the paper (rounded down) comes from each of the three departments, as far as each has enough, and the
    rest from the general questions (then from whatever is left), in a random order. A bank with no tags is drawn
    from as a whole, exactly as before."""
    questions = list(questions)
    count = min(count, len(questions))
    by_department = {d: [q for q in questions if question_department(q) == d] for d in DEPARTMENTS}
    if not any(by_department.values()):
        return rng.sample(questions, count)
    share = count // 4
    chosen = []
    for department in DEPARTMENTS:
        pool = by_department[department]
        chosen += rng.sample(pool, min(share, len(pool)))
    taken = {id(q) for q in chosen}
    left = [q for q in questions if id(q) not in taken]
    general = [q for q in left if question_department(q) is None]
    need = count - len(chosen)
    fill = rng.sample(general, min(need, len(general)))
    taken |= {id(q) for q in fill}
    need -= len(fill)
    if need > 0:
        fill += rng.sample([q for q in left if id(q) not in taken], need)
    chosen += fill
    rng.shuffle(chosen)
    return chosen


def _latest_attempts(candidate_ids):
    """{candidate_id: [attempt ids]}: each candidate's latest finished attempt at each of their papers."""
    if not candidate_ids:
        return {}
    latest = (select(func.max(Attempt.id))
              .where(Attempt.candidate_id == CandidatePaper.candidate_id, Attempt.bank_id == CandidatePaper.bank_id)
              .correlate(CandidatePaper).scalar_subquery())
    rows = db.session.execute(
        select(CandidatePaper.candidate_id, Attempt.id)
        .select_from(CandidatePaper)
        .join(Attempt, Attempt.id == latest)
        .where(CandidatePaper.candidate_id.in_(candidate_ids), Attempt.status.in_(DONE))).all()
    out = {}
    for candidate_id, attempt_id in rows:
        out.setdefault(candidate_id, []).append(attempt_id)
    return out


def _tagged_marks(attempt_ids):
    """{attempt_id: {department: [earned, possible, questions]}} from the frozen, tagged questions of these attempts."""
    if not attempt_ids:
        return {}
    rows = db.session.execute(
        select(AttemptQuestion.attempt_id, AttemptQuestion.department, AttemptQuestion.points,
               AttemptQuestion.correct_option, Answer.option_index)
        .select_from(AttemptQuestion)
        .outerjoin(Answer, (Answer.attempt_id == AttemptQuestion.attempt_id)
                   & (Answer.question_id == AttemptQuestion.question_id))
        .where(AttemptQuestion.attempt_id.in_(attempt_ids), AttemptQuestion.department.is_not(None))).all()
    out = {}
    for attempt_id, department, points, correct, chosen in rows:
        department = clean_department(department)
        if not department:
            continue
        cell = out.setdefault(attempt_id, {}).setdefault(department, [0.0, 0.0, 0])
        cell[1] += float(points or 0)
        cell[2] += 1
        if chosen is not None and correct is not None and chosen == correct:
            cell[0] += float(points or 0)
    return out


def department_fits(summaries):
    """{candidate_id: fit} for result summaries (blueprints/entrance/helpers._candidate_results_summary rows).

    A fit is {'departments': {name: {'score', 'own', 'own_questions', 'paper', 'paper_label'}}, 'evidence': bool},
    where 'own' is the percentage earned on that department's questions (None when none were answered), 'paper' the
    related paper's percentage (None until it is sat) and 'score' the two together (None without either)."""
    summaries = list(summaries)
    latest = _latest_attempts([r['id'] for r in summaries])
    marks = _tagged_marks([a for ids in latest.values() for a in ids])
    fits = {}
    for r in summaries:
        totals = {d: [0.0, 0.0, 0] for d in DEPARTMENTS}
        for attempt_id in latest.get(r['id'], []):
            for department, (earned, possible, n) in marks.get(attempt_id, {}).items():
                totals[department][0] += earned
                totals[department][1] += possible
                totals[department][2] += n
        departments = {}
        for department in DEPARTMENTS:
            earned, possible, n = totals[department]
            own = (100.0 * earned / possible) if possible else None
            paper_key = RELATED_PAPER[department]
            paper = (r.get(paper_key) or {}).get('percentage')
            if own is not None and paper is not None:
                score = OWN_QUESTIONS_WEIGHT * own + (1 - OWN_QUESTIONS_WEIGHT) * paper
            else:
                score = own if own is not None else paper
            departments[department] = {'score': score, 'own': own, 'own_questions': n, 'paper': paper,
                                       'paper_label': RELATED_PAPER_LABEL[paper_key]}
        fits[r['id']] = {'departments': departments,
                         'evidence': any(v['own'] is not None for v in departments.values())}
    return fits


def recommend(choice, fit):
    """{'department', 'reason', 'matches_choice'}: the department to admit into, as a suggestion.

    The candidate's own choice stands when it is as good as any (within FIT_MARGIN points of their best department
    and at least FIT_PASS); otherwise the department they did best in is suggested. With nothing to go on yet, the
    choice is kept (or nothing is suggested, when there is no choice either)."""
    choice = clean_department(choice)
    scored = {d: v['score'] for d, v in (fit or {}).get('departments', {}).items() if v['score'] is not None}
    if not scored:
        return {'department': choice, 'matches_choice': bool(choice),
                'reason': 'No results to judge by yet, so their own choice is kept.' if choice else 'No results to judge by yet.'}
    best = max(scored, key=lambda d: (scored[d], d == choice))
    basis = '' if fit.get('evidence') else ' (judged from the Mathematics and English papers only: no departmental questions were answered)'
    # Departments that came out level with the best one: worth saying, so a close call reads as one.
    level = [d for d in scored if d != best and abs(scored[d] - scored[best]) < 0.5]
    if level:
        basis += f'; {" and ".join(level)} {"is" if len(level) == 1 else "are"} level with {best}'
    if choice and choice in scored and scored[choice] >= scored[best] - FIT_MARGIN and scored[choice] >= FIT_PASS:
        return {'department': choice, 'matches_choice': True,
                'reason': f'Fits their choice: {choice} {scored[choice]:.0f}%, as strong as any department{basis}.'}
    if choice and best == choice:
        return {'department': choice, 'matches_choice': True,
                'reason': f'Their choice, {choice}, is their strongest department ({scored[choice]:.0f}%), '
                          f'though below {FIT_PASS:.0f}%{basis}.'}
    if not choice:
        return {'department': best, 'matches_choice': False,
                'reason': f'Strongest in {best} ({scored[best]:.0f}%); no department was chosen{basis}.'}
    mine = f'{scored[choice]:.0f}%' if choice in scored else 'no result'
    return {'department': best, 'matches_choice': False,
            'reason': f'Stronger in {best} ({scored[best]:.0f}%) than in their choice, {choice} ({mine}){basis}.'}
