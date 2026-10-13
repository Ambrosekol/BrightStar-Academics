"""To mark: the subject teacher marks the written (theory) answers of their tests and examinations, and shares
each paper's marks between its objective and theory parts. The scoring itself is core/theory_marking.py.

Who may mark. A paper is marked by whoever may edit it - school.tests.edit for a test, school.examinations.edit
for an examination - and teaches that subject in that class (_school_pair_allowed). Each student is checked on
their own as well (_student_subject_allowed): in SSS a teacher may teach a subject to one department only, and
sees, marks and counts only those students. None of these pages is in ADMIN_ENDPOINT_PERMISSIONS, because a
paper's type decides the permission; every route checks it itself, and a refusal is logged like any other.

What is locked. A student's released result is never re-marked, and a paper's split cannot move once any of its
results is released. A mark that changes after the result was checked sends the result back to be checked
again (core/theory_marking.write_result). Every mark and every split change is in the activity log.

Printed papers (core/printed_papers.py) are marked here too: their page is a marks sheet for the whole class, and
the question paper and the answer booklets are printed from here, by the same people. A printed question paper is
the exam itself, so printing it is recorded in the activity log, and the pages are never kept in a browser cache.
"""

import math
from datetime import datetime, timezone

from flask import abort, flash, make_response, redirect, render_template, request, url_for
from sqlalchemy import select

from app import app
from blueprints.school.helpers import _school_pair_allowed, _student_subject_allowed
from control_plane.config import badge_cache_seconds
from core import printed_papers as pp
from core import theory_marking as tm
from core.db_helpers import all_rows, one, tuples
from core.security import admin_access_error, admin_has_permission, admin_required, audit_log, csrf_protect, current_admin
from core.short_cache import forget_here, remember
from models import (
    AcademicSession, SchoolAssessment, SchoolAssessmentAnswer, SchoolAssessmentAttempt, SchoolAssessmentAttemptQuestion,
    SchoolClass, SchoolQuestion, SchoolStudentResult, SchoolSubject, Student, db,
)

EDIT_PERMISSION = {'test': 'school.tests.edit', 'examination': 'school.examinations.edit'}
KIND_LABEL = {'test': 'Test', 'examination': 'Examination'}
CACHE = 'theory-to-mark'


def _may_mark_any(admin_id):
    return any(admin_has_permission(admin_id, perm) for perm in EDIT_PERMISSION.values())


def _may_mark_paper(admin_id, paper):
    perm = EDIT_PERMISSION.get(paper['assessment_type'])
    return bool(perm and admin_has_permission(admin_id, perm)
                and _school_pair_allowed(admin_id, paper['class_id'], paper['subject_id']))


def _students_i_mark(admin_id, paper, student_ids):
    return [sid for sid in student_ids
            if _student_subject_allowed(admin_id, sid, paper['class_id'], paper['subject_id'])]


def _my_papers(admin_id):
    """The papers with theory this teacher marks, each with how many of their students' scripts wait."""
    papers = [dict(p) for p in tm.papers_with_theory() if _may_mark_paper(admin_id, p)]
    scripts = tm.script_counts([p['id'] for p in papers if p['delivery'] != pp.PRINTED])
    for p in papers:
        if p['delivery'] == pp.PRINTED:
            mine = _students_i_mark(admin_id, p, [s['id'] for s in pp.roster(p)])
            states = pp.paper_states(p['id'], mine)
            p['pending'] = sum(1 for st in states.values() if st == 'pending')
            p['marked'] = sum(1 for st in states.values() if st in ('marked', 'absent'))
            p['scripts'] = len(mine)
            continue
        rows = scripts.get(p['id'], [])
        mine = set(_students_i_mark(admin_id, p, [sid for sid, _ in rows]))
        p['pending'] = sum(1 for sid, status in rows if sid in mine and status == 'pending')
        p['marked'] = sum(1 for sid, status in rows if sid in mine and status == 'marked')
        p['scripts'] = len(mine)
    return papers


def _pending_total(admin_id):
    return sum(p['pending'] for p in _my_papers(admin_id))


def to_mark_badge():
    """For the menu: how many scripts wait for this teacher's marks (0 for someone who cannot mark)."""
    admin = current_admin()
    if not admin or not _may_mark_any(admin['id']):
        return 0
    try:
        return remember(CACHE, badge_cache_seconds(), _pending_total, admin['id'])
    except Exception:
        db.session.rollback()
        app.logger.exception('Could not count the scripts waiting to be marked')
        return 0


def may_open_to_mark():
    admin = current_admin()
    return bool(admin and _may_mark_any(admin['id']))


def _paper(assessment_id):
    return one(select(SchoolAssessment.id, SchoolAssessment.title, SchoolAssessment.assessment_type,
                      SchoolAssessment.class_id, SchoolAssessment.subject_id, SchoolAssessment.term,
                      SchoolAssessment.session_id, SchoolAssessment.active, SchoolAssessment.delivery,
                      SchoolAssessment.duration_minutes, SchoolAssessment.instructions,
                      SchoolClass.name.label('class_name'), SchoolSubject.name.label('subject_name'),
                      AcademicSession.name.label('session_name'))
               .join(SchoolClass, SchoolClass.id == SchoolAssessment.class_id)
               .join(SchoolSubject, SchoolSubject.id == SchoolAssessment.subject_id)
               .outerjoin(AcademicSession, AcademicSession.id == SchoolAssessment.session_id)
               .where(SchoolAssessment.id == assessment_id))


def _open_paper(assessment_id):
    """(admin, paper, None) for a paper this person may mark, or (admin, None, the refusal to send)."""
    me = current_admin()
    paper = _paper(assessment_id)
    if not paper or paper['assessment_type'] not in EDIT_PERMISSION:
        abort(404)
    if paper['delivery'] != pp.PRINTED and not tm.has_theory(assessment_id):
        abort(404)
    if not _may_mark_paper(me['id'], paper):
        perm = EDIT_PERMISSION[paper['assessment_type']]
        audit_log('authorization_denied', 'school', 'marking', assessment_id, {'permission': perm}, False, me)
        return me, None, admin_access_error(perm)
    return me, dict(paper), None


# ---------------------------------------------------------------- the list
@app.route('/admin/school/marking')
@admin_required
def admin_school_marking():
    me = current_admin()
    if not _may_mark_any(me['id']):
        return admin_access_error('school.tests.edit')
    view = 'all' if request.args.get('view') == 'all' else 'waiting'
    papers = _my_papers(me['id'])
    classes = {cid: name for cid, name in tuples(select(SchoolClass.id, SchoolClass.name)
                                                .where(SchoolClass.id.in_({p['class_id'] for p in papers} or {0})))}
    subjects = {sid: name for sid, name in tuples(select(SchoolSubject.id, SchoolSubject.name)
                                                 .where(SchoolSubject.id.in_({p['subject_id'] for p in papers} or {0})))}
    for p in papers:
        p['class_name'], p['subject_name'] = classes.get(p['class_id'], '—'), subjects.get(p['subject_id'], '—')
    waiting = sum(1 for p in papers if p['pending'])
    shown = papers if view == 'all' else [p for p in papers if p['pending']]
    shown.sort(key=lambda p: (p['class_name'].casefold(), -p['pending'], p['subject_name'].casefold(), p['title'].casefold()))
    groups = []
    for p in shown:
        if not groups or groups[-1]['class_name'] != p['class_name']:
            groups.append({'class_name': p['class_name'], 'papers': []})
        groups[-1]['papers'].append(p)
    return render_template('school_marking.html', groups=groups, view=view, waiting=waiting, total=len(papers),
                           scripts_waiting=sum(p['pending'] for p in papers), kinds=KIND_LABEL)


# ---------------------------------------------------------------- one paper
def _script(attempt_id, assessment_id):
    """One student's theory questions (as they saw them), their answers and the marks given so far."""
    questions = all_rows(select(SchoolAssessmentAttemptQuestion.question_id, SchoolAssessmentAttemptQuestion.question_order,
                                SchoolAssessmentAttemptQuestion.question_text, SchoolAssessmentAttemptQuestion.instruction,
                                SchoolAssessmentAttemptQuestion.image_path, SchoolAssessmentAttemptQuestion.points)
                         .where(SchoolAssessmentAttemptQuestion.attempt_id == attempt_id,
                                SchoolAssessmentAttemptQuestion.question_type == tm.THEORY)
                         .order_by(SchoolAssessmentAttemptQuestion.question_order))
    answers = {r['question_id']: r for r in all_rows(
        select(SchoolAssessmentAnswer.question_id, SchoolAssessmentAnswer.answer_text, SchoolAssessmentAnswer.awarded,
               SchoolAssessmentAnswer.marker_comment)
        .where(SchoolAssessmentAnswer.attempt_id == attempt_id))}
    guides = dict(tuples(select(SchoolQuestion.id, SchoolQuestion.marking_guide)
                         .where(SchoolQuestion.assessment_id == assessment_id,
                                SchoolQuestion.id.in_([q['question_id'] for q in questions] or [0]))))
    out = []
    for q in questions:
        a = answers.get(q['question_id']) or {}
        text = (a.get('answer_text') or '')
        out.append({**q, 'answer': text, 'answered': bool(text.strip()), 'awarded': a.get('awarded'),
                    'comment': a.get('marker_comment') or '', 'guide': guides.get(q['question_id']) or ''})
    return out


@app.route('/admin/school/marking/<int:assessment_id>')
@admin_required
def admin_school_marking_paper(assessment_id):
    me, paper, refused = _open_paper(assessment_id)
    if refused is not None:
        return refused
    if paper['delivery'] == pp.PRINTED:
        return _printed_sheet(me, paper)
    attempts = all_rows(select(SchoolAssessmentAttempt.id, SchoolAssessmentAttempt.student_id,
                               SchoolAssessmentAttempt.theory_status, SchoolAssessmentAttempt.score,
                               SchoolAssessmentAttempt.max_score, SchoolAssessmentAttempt.submitted_at)
                        .where(SchoolAssessmentAttempt.assessment_id == assessment_id,
                               SchoolAssessmentAttempt.status.in_(tm.FINISHED)))
    mine = set(_students_i_mark(me['id'], paper, [a['student_id'] for a in attempts]))
    attempts = [a for a in attempts if a['student_id'] in mine]
    people = {r['id']: r for r in all_rows(select(Student.id, Student.first_name, Student.last_name, Student.admission_no)
                                           .where(Student.id.in_(mine or {0})))}
    released = {sid for (sid,) in tuples(select(SchoolStudentResult.student_id).where(
        SchoolStudentResult.assessment_id == assessment_id, SchoolStudentResult.status == 'released'))}
    statuses = dict(tuples(select(SchoolStudentResult.student_id, SchoolStudentResult.status)
                           .where(SchoolStudentResult.assessment_id == assessment_id)
                           .order_by(SchoolStudentResult.id)))
    students = []
    for a in attempts:
        person = people.get(a['student_id']) or {}
        students.append({**a, 'name': f"{person.get('last_name', '')} {person.get('first_name', '')}".strip() or 'Student',
                         'admission_no': person.get('admission_no') or '', 'released': a['student_id'] in released,
                         'result_status': statuses.get(a['student_id'])})
    students.sort(key=lambda s: (s['theory_status'] != 'pending', s['name'].casefold()))
    split = tm.split_for(assessment_id)
    current = None
    wanted = request.args.get('student', type=int)
    if students:
        current = next((s for s in students if s['student_id'] == wanted), None) if wanted else None
        current = current or students[0]
        current = {**current, 'questions': _script(current['id'], assessment_id),
                   'parts': tm.attempt_parts(current['id'])}
        current['score'] = tm.final_score(current['parts'], split)
    return render_template('school_marking_paper.html', paper=paper, students=students, current=current, split=split,
                           split_locked=tm.split_locked(assessment_id), kinds=KIND_LABEL,
                           pending=sum(1 for s in students if s['theory_status'] == 'pending'),
                           comment_limit=tm.COMMENT_LIMIT)


@app.post('/admin/school/marking/<int:assessment_id>/students/<int:student_id>')
@admin_required
@csrf_protect
def admin_school_marking_save(assessment_id, student_id):
    me, paper, refused = _open_paper(assessment_id)
    if refused is not None:
        return refused
    back = url_for('admin_school_marking_paper', assessment_id=assessment_id, student=student_id)
    if not _students_i_mark(me['id'], paper, [student_id]):
        audit_log('authorization_denied', 'school', 'marking', assessment_id, {'student_id': student_id}, False, me)
        return admin_access_error(EDIT_PERMISSION[paper['assessment_type']])
    attempt = db.session.scalars(select(SchoolAssessmentAttempt).where(
        SchoolAssessmentAttempt.assessment_id == assessment_id, SchoolAssessmentAttempt.student_id == student_id,
        SchoolAssessmentAttempt.status.in_(tm.FINISHED))).first()
    if attempt is None:
        abort(404)
    if db.session.scalars(select(SchoolStudentResult.id).where(
            SchoolStudentResult.assessment_id == assessment_id, SchoolStudentResult.student_id == student_id,
            SchoolStudentResult.status == 'released')).first():
        flash('This result has been released, so its marks are locked. A released result is corrected through the result correction process.', 'error')
        return redirect(back)
    was = db.session.scalars(select(SchoolStudentResult.status).where(
        SchoolStudentResult.assessment_id == assessment_id, SchoolStudentResult.student_id == student_id)
        .order_by(SchoolStudentResult.id.desc())).first()
    questions = [q for q in _script(attempt.id, assessment_id) if q['answered']]
    errors, marks = [], {}
    for q in questions:
        raw = (request.form.get(f"mark_{q['question_id']}") or '').strip()
        comment = ' '.join((request.form.get(f"comment_{q['question_id']}") or '').split())
        if len(comment) > tm.COMMENT_LIMIT:
            errors.append(f"Question {q['question_order']}: keep the comment under {tm.COMMENT_LIMIT} characters.")
            continue
        if raw == '':
            marks[q['question_id']] = (None, comment or None)
            continue
        try:
            value = float(raw)
        except ValueError:
            value = float('nan')
        if not math.isfinite(value) or value < 0 or value > q['points']:
            errors.append(f"Question {q['question_order']}: give a mark from 0 to {q['points']}.")
            continue
        marks[q['question_id']] = (round(value, 2), comment or None)
    if errors:
        for e in errors:
            flash(e, 'error')
        return redirect(back)
    now = datetime.now(timezone.utc).isoformat()
    changed = 0
    for row in db.session.scalars(select(SchoolAssessmentAnswer).where(
            SchoolAssessmentAnswer.attempt_id == attempt.id,
            SchoolAssessmentAnswer.question_id.in_(list(marks) or [0]))):
        awarded, comment = marks[row.question_id]
        if row.awarded != awarded or (row.marker_comment or None) != comment:
            row.awarded, row.marker_comment, row.marked_by, row.marked_at = awarded, comment, me['id'], now
            changed += 1
    status = tm.sync_result(attempt.id, me['id'], 'Theory marks changed')
    parts = tm.attempt_parts(attempt.id)
    db.session.commit()
    forget_here(CACHE)
    audit_log('school_theory_marked', 'school', paper['assessment_type'], assessment_id,
              {'student_id': student_id, 'answers_changed': changed, 'still_unmarked': parts['pending'],
               'result_status': status})
    if parts['pending']:
        flash(f"Saved. {parts['pending']} answer{'' if parts['pending'] == 1 else 's'} still to mark for this student.", 'success')
    else:
        flash('Marked. The score is now part of the result.'
              + (' It had already been checked, so it goes back to be checked again.' if was in ('verified', 'approved') and status == 'entered' else ''),
              'success')
    if request.form.get('then') == 'next' and not parts['pending']:
        nxt = db.session.scalars(select(SchoolAssessmentAttempt.student_id).where(
            SchoolAssessmentAttempt.assessment_id == assessment_id, SchoolAssessmentAttempt.theory_status == 'pending',
            SchoolAssessmentAttempt.status.in_(tm.FINISHED), SchoolAssessmentAttempt.student_id != student_id)).all()
        nxt = _students_i_mark(me['id'], paper, nxt)
        if nxt:
            return redirect(url_for('admin_school_marking_paper', assessment_id=assessment_id, student=nxt[0]))
    return redirect(back)


@app.post('/admin/school/marking/<int:assessment_id>/split')
@admin_required
@csrf_protect
def admin_school_marking_split(assessment_id):
    me, paper, refused = _open_paper(assessment_id)
    if refused is not None:
        return refused
    back = url_for('admin_school_marking_paper', assessment_id=assessment_id, student=request.form.get('student', type=int))
    if tm.split_locked(assessment_id):
        flash('Results of this paper have been released, so how its marks are shared can no longer change.', 'error')
        return redirect(back)
    before = tm.split_for(assessment_id)
    if request.form.get('reset') == '1':
        objective = theory = None
    else:
        try:
            objective, theory = tm.check_split(assessment_id, float(request.form.get('theory_marks', '')))
        except (TypeError, ValueError) as exc:
            flash(str(exc) if str(exc) and not str(exc).startswith('could not convert') else 'Enter the theory marks as a number.', 'error')
            return redirect(back)
    record = db.session.get(SchoolAssessment, assessment_id)
    record.objective_marks, record.theory_marks = objective, theory
    db.session.flush()
    looked_at = tm.refresh_paper(assessment_id, me['id'], 'The paper\'s marks were shared out differently')
    db.session.commit()
    after = tm.split_for(assessment_id)
    audit_log('school_theory_split_changed', 'school', paper['assessment_type'], assessment_id,
              {'from': [before['objective'], before['theory']], 'to': [after['objective'], after['theory']],
               'scripts_rescored': looked_at})
    flash(f"Objective is now worth {after['objective']:g} and theory {after['theory']:g} of {after['total']:g}. "
          f"{looked_at} student score{'' if looked_at == 1 else 's'} recalculated.", 'success')
    return redirect(back)





# ---------------------------------------------------------------- printed papers
def _printed_rows(me, paper, posted=None):
    """The marks sheet: each student this teacher marks, with their script as entered (or as just typed)."""
    everyone = pp.roster(paper)
    mine = set(_students_i_mark(me['id'], paper, [r['id'] for r in everyone]))
    students = [s for s in everyone if s['id'] in mine]
    questions = pp.paper_questions(paper['id'])
    theory = [q for q in questions if (q['question_type'] or tm.OBJECTIVE) == tm.THEORY]
    objective_raw = sum(int(q['points'] or 1) for q in questions if (q['question_type'] or tm.OBJECTIVE) != tm.THEORY)
    found = pp.scripts(paper['id'], [s['id'] for s in students])
    released = {sid for (sid,) in tuples(select(SchoolStudentResult.student_id).where(
        SchoolStudentResult.assessment_id == paper['id'], SchoolStudentResult.status == 'released'))}
    split = tm.split_for(paper['id'])
    rows = []
    for st in students:
        script = found.get(st['id'])
        typed = (posted or {}).get(st['id'])
        rows.append({**st, 'script': script, 'state': pp.script_state(script, theory, objective_raw),
                     'score': pp.script_score(script, theory, objective_raw, split), 'released': st['id'] in released,
                     'typed': typed})
    return rows, theory, objective_raw, split


def _printed_sheet(me, paper, posted=None, errors=None, status=200):
    rows, theory, objective_raw, split = _printed_rows(me, paper, posted)
    response = make_response(render_template(
        'school_marking_printed.html', paper=paper, rows=rows, theory=theory, objective_raw=objective_raw, split=split,
        split_locked=tm.split_locked(paper['id']), kinds=KIND_LABEL, errors=errors or {},
        pending=sum(1 for r in rows if r['state'] == 'pending')), status)
    return response


@app.post('/admin/school/marking/<int:assessment_id>/printed')
@admin_required
@csrf_protect
def admin_school_marking_printed_save(assessment_id):
    me, paper, refused = _open_paper(assessment_id)
    if refused is not None:
        return refused
    if paper['delivery'] != pp.PRINTED:
        abort(404)
    rows, theory, objective_raw, _ = _printed_rows(me, paper)
    sent = {int(v) for v in request.form.getlist('student') if str(v).isdigit()}
    mine = {r['id']: r for r in rows}
    if sent - set(mine):
        audit_log('authorization_denied', 'school', 'marking', assessment_id, {'students': sorted(sent - set(mine))}, False, me)
        return admin_access_error(EDIT_PERMISSION[paper['assessment_type']])
    entries, errors, posted = {}, {}, {}
    for sid in sent:
        if mine[sid]['released']:
            continue
        absent = request.form.get(f'absent_{sid}') == '1'
        raw_obj = request.form.get(f'obj_{sid}', '')
        raw_marks = {q['id']: request.form.get(f'm_{sid}_{q["id"]}', '') for q in theory}
        posted[sid] = {'absent': absent, 'objective': raw_obj, 'marks': raw_marks}
        try:
            entries[sid] = pp.check_entry(raw_obj, absent, raw_marks, theory, objective_raw)
        except ValueError as exc:
            errors[sid] = str(exc)
    if errors:
        # Nothing is saved until every row is right, so a sheet is never half-entered by mistake.
        flash(f"Nothing was saved: {len(errors)} student{'' if len(errors) == 1 else 's'} need{'s' if len(errors) == 1 else ''} a correction (marked in red).", 'error')
        return _printed_sheet(me, paper, posted, errors, 400)
    changed_students = 0
    sent_back = 0
    for sid, (objective, absent, marks) in entries.items():
        before = db.session.scalars(select(SchoolStudentResult.status).where(
            SchoolStudentResult.assessment_id == assessment_id, SchoolStudentResult.student_id == sid)
            .order_by(SchoolStudentResult.id.desc())).first()
        changed, status = pp.save_script(assessment_id, sid, objective, absent, marks, me['id'])
        if changed:
            changed_students += 1
            if before in ('verified', 'approved') and status == 'entered':
                sent_back += 1
    db.session.commit()
    forget_here(CACHE)
    audit_log('school_printed_marks_entered', 'school', paper['assessment_type'], assessment_id,
              {'students_changed': changed_students, 'sent_back_to_check': sent_back})
    message = f"Saved. {changed_students} student{'' if changed_students == 1 else 's'} updated."
    if sent_back:
        message += f" {sent_back} result{'' if sent_back == 1 else 's'} had already been checked and go{'es' if sent_back == 1 else ''} back to be checked again."
    flash(message, 'success')
    return redirect(url_for('admin_school_marking_paper', assessment_id=assessment_id))


def _no_store(response):
    # The question paper is the exam: never left in a shared computer's cache or a proxy.
    response.headers['Cache-Control'] = 'no-store, private'
    response.headers['Pragma'] = 'no-cache'
    return response


@app.route('/admin/school/marking/<int:assessment_id>/print/paper')
@admin_required
def admin_school_paper_print(assessment_id):
    me, paper, refused = _open_paper(assessment_id)
    if refused is not None:
        return refused
    if paper['delivery'] != pp.PRINTED:
        abort(404)
    questions = pp.paper_questions(assessment_id)
    audit_log('school_paper_printed', 'school', paper['assessment_type'], assessment_id, {'what': 'question paper'})
    return _no_store(make_response(render_template(
        'school_print_paper.html', paper=paper, kinds=KIND_LABEL, split=tm.split_for(assessment_id),
        objective=[q for q in questions if (q['question_type'] or tm.OBJECTIVE) != tm.THEORY],
        theory=[q for q in questions if (q['question_type'] or tm.OBJECTIVE) == tm.THEORY])))


@app.route('/admin/school/marking/<int:assessment_id>/print/booklets')
@admin_required
def admin_school_paper_booklets(assessment_id):
    me, paper, refused = _open_paper(assessment_id)
    if refused is not None:
        return refused
    if paper['delivery'] != pp.PRINTED:
        abort(404)
    students = pp.roster(paper)
    mine = set(_students_i_mark(me['id'], paper, [s['id'] for s in students]))
    students = [s for s in students if s['id'] in mine]
    only = request.args.get('student', type=int)
    if only:
        students = [s for s in students if s['id'] == only]
    questions = pp.paper_questions(assessment_id)
    audit_log('school_paper_printed', 'school', paper['assessment_type'], assessment_id,
              {'what': 'answer booklets', 'students': len(students)})
    return _no_store(make_response(render_template(
        'school_print_booklets.html', paper=paper, kinds=KIND_LABEL, students=students,
        objective=[q for q in questions if (q['question_type'] or tm.OBJECTIVE) != tm.THEORY],
        theory=[q for q in questions if (q['question_type'] or tm.OBJECTIVE) == tm.THEORY])))


app.jinja_env.globals.update(theory_to_mark_badge=to_mark_badge, may_open_to_mark=may_open_to_mark)
