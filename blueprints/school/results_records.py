"""Results & Records: choose a class, then work through one student's term in a dialog.

The page is a single address that reads where the person has got to from its query string, so
every step can be reloaded, bookmarked and reached with the back button:

    ?class=JSS 1                               the class's students, with a search box
    ?class=JSS 1&student=7                     that student: which session and term?
    ?class=JSS 1&student=7&session=2&term=First Term
                                               that term's results, subject by subject, with
                                               one button to release them all

Only results of the official record are shown; practice tests never are (see report_card_data).
"""

from datetime import datetime, timezone

from flask import abort, flash, redirect, render_template, request, url_for
from sqlalchemy import and_, case, func, or_, select

from app import ACADEMIC_TERMS, _release_due_school_results, _school_current_session, app
from blueprints.school.helpers import _school_class_allowed, _school_subject_allowed
from blueprints.school.result_notices import announce_ready_report_cards
from core.db_helpers import all_rows, one_scalar, tuples, _flatten
from core.security import (
    admin_access_error, admin_has_permission, admin_required, admin_scope_allows, audit_log,
    csrf_protect, current_admin,
)
from models import (
    AcademicSession, ResultWorkflowEvent, SchoolAssessment, SchoolClass, SchoolStudentResult,
    SchoolSubject, Student, StudentEnrolment, db,
)

TERMS = [*ACADEMIC_TERMS, 'Full Session']


def _official():
    """Practice tests are a self-study tool, not part of a student's record."""
    return or_(SchoolAssessment.assessment_type.is_(None), SchoolAssessment.assessment_type != 'practice')


def _term_of():
    return func.coalesce(SchoolStudentResult.term, 'Full Session')


def results_return_url(default):
    """Where to go back to after acting on a result: the step the person was on, when it is one
    of this page's own addresses (never anything a form could aim elsewhere)."""
    target = request.form.get('return_to', '')
    if target.startswith('/admin/school/results') and not target.startswith('//') and '\\' not in target:
        return target
    return default


@app.route('/admin/school/results')
@admin_required
def admin_school_results():
    _release_due_school_results()
    admin = current_admin()
    classes = db.session.scalars(select(SchoolClass).where(SchoolClass.active == 1)
                                 .order_by(SchoolClass.level_order)).all()
    if admin and not admin['admin_type_system']:
        classes = [c for c in classes if admin_scope_allows(admin['id'], 'class', c['name'])]
    counts = {name: n for name, n in tuples(
        select(SchoolClass.name, func.count(SchoolStudentResult.id))
        .select_from(SchoolStudentResult)
        .join(StudentEnrolment, and_(StudentEnrolment.student_id == SchoolStudentResult.student_id,
                                     StudentEnrolment.active == 1))
        .join(SchoolClass, SchoolClass.id == StudentEnrolment.class_id)
        .outerjoin(SchoolAssessment, SchoolAssessment.id == SchoolStudentResult.assessment_id)
        .where(_official())
        .group_by(SchoolClass.name))}
    cards = [{'name': c['name'], 'id': c['id'], 'count': counts.get(c['name'], 0)} for c in classes]

    selected_class = request.args.get('class', '').strip()
    class_row = next((c for c in classes if c['name'] == selected_class), None)
    students, student, periods, period, rows, summary = [], None, [], None, [], None
    if class_row:
        students = [dict(r) for r in all_rows(
            select(Student.id, Student.admission_no, Student.first_name, Student.last_name)
            .join(StudentEnrolment, and_(StudentEnrolment.student_id == Student.id,
                                         StudentEnrolment.active == 1,
                                         StudentEnrolment.class_id == class_row.id))
            .where(Student.active == 1).distinct()
            .order_by(Student.last_name, Student.first_name, Student.id))]
        student = next((s for s in students if s['id'] == request.args.get('student', type=int)), None)
    if student:
        periods = _periods(student['id'])
        session_id, term = request.args.get('session', type=int), request.args.get('term', '')
        period = next(({'session': p, 'term': term} for p in periods
                       if p['id'] == session_id and term in TERMS), None)
        if period:
            rows = _term_rows(student['id'], period['session']['id'], term, admin)
            summary = _summary(rows)
    return render_template('school_results.html', class_cards=cards, selected_class=selected_class,
                           class_row=class_row, students=students, student=student, periods=periods,
                           period=period, rows=rows, summary=summary,
                           current_session=_school_current_session(), terms=TERMS)


def _periods(student_id):
    """Every session, newest first, each with its terms and how many results the student has in it."""
    term_of = _term_of()   # one expression, so PostgreSQL sees the SELECT and the GROUP BY as the same thing
    stat = {(sid, t): {'total': int(n), 'released': int(released or 0)}
            for sid, t, n, released in tuples(
                select(SchoolStudentResult.session_id, term_of, func.count(),
                       func.sum(case((SchoolStudentResult.status == 'released', 1), else_=0)))
                .select_from(SchoolStudentResult)
                .outerjoin(SchoolAssessment, SchoolAssessment.id == SchoolStudentResult.assessment_id)
                .where(SchoolStudentResult.student_id == student_id,
                       SchoolStudentResult.session_id.is_not(None), _official())
                .group_by(SchoolStudentResult.session_id, term_of))}
    sessions = db.session.scalars(select(AcademicSession).where(AcademicSession.active == 1)
                                  .order_by(AcademicSession.id.desc())).all()
    periods = []
    for s in sessions:
        terms = [t for t in TERMS if t != 'Full Session' or (s.id, t) in stat]
        periods.append({'id': s.id, 'name': s.name, 'is_current': s.is_current,
                        'terms': [{'term': t, **stat.get((s.id, t), {'total': 0, 'released': 0})} for t in terms]})
    return periods


def _term_rows(student_id, session_id, term, admin):
    rows = [_flatten(r, 'SchoolStudentResult', 'subject_name', 'assessment_title') for r in all_rows(
        select(SchoolStudentResult, SchoolSubject.name.label('subject_name'),
               SchoolAssessment.title.label('assessment_title'))
        .select_from(SchoolStudentResult)
        .join(SchoolSubject, SchoolSubject.id == SchoolStudentResult.subject_id)
        .outerjoin(SchoolAssessment, SchoolAssessment.id == SchoolStudentResult.assessment_id)
        .where(SchoolStudentResult.student_id == student_id, SchoolStudentResult.session_id == session_id,
               _term_of() == term, _official())
        .order_by(SchoolSubject.name, SchoolStudentResult.id))]
    if admin and not admin['admin_type_system']:
        rows = [r for r in rows if admin_scope_allows(admin['id'], 'subject', r['subject_name'])]
    return rows


def _summary(rows):
    by_status = {}
    for r in rows:
        by_status[r['status']] = by_status.get(r['status'], 0) + 1
    released, approved = by_status.get('released', 0), by_status.get('approved', 0)
    waiting = len(rows) - released - approved
    return {'total': len(rows), 'released': released, 'approved': approved, 'waiting': waiting,
            'subjects': len({r['subject_id'] for r in rows}), 'ready': bool(rows) and released == len(rows)}


@app.post('/admin/school/results/release-term')
@admin_required
@csrf_protect
def admin_school_results_release_term():
    """Release every approved result of one student's term at once, then tell the parents."""
    me = current_admin()
    if not admin_has_permission(me['id'], 'school.results.release'):
        return admin_access_error('school.results.release')
    student_id = request.form.get('student_id', type=int)
    session_id = request.form.get('session_id', type=int)
    term = request.form.get('term', '')
    class_id = one_scalar(select(StudentEnrolment.class_id).where(
        StudentEnrolment.student_id == student_id, StudentEnrolment.active == 1)
        .order_by(StudentEnrolment.id.desc()).limit(1)) if student_id else None
    if not student_id or not session_id or term not in TERMS or not class_id:
        abort(404)
    if not _school_class_allowed(me['id'], class_id):
        return admin_access_error('school.results.release')
    back = results_return_url(url_for('admin_school_results'))

    due = db.session.scalars(
        select(SchoolStudentResult)
        .outerjoin(SchoolAssessment, SchoolAssessment.id == SchoolStudentResult.assessment_id)
        .where(SchoolStudentResult.student_id == student_id, SchoolStudentResult.session_id == session_id,
               _term_of() == term, SchoolStudentResult.status == 'approved', _official())).all()
    due = [r for r in due if _school_subject_allowed(me['id'], r.subject_id)]
    if not due:
        flash('There are no approved results to release for this term.', 'error')
        return redirect(back)
    now = datetime.now(timezone.utc).isoformat()
    for r in due:
        r.status, r.released_at, r.updated_at, r.updated_by = 'released', now, now, me['id']
        db.session.add(ResultWorkflowEvent(result_id=r.id, from_status='approved', to_status='released',
                                           actor_admin_id=me['id'], reason='Released with the whole term',
                                           created_at=now))
    db.session.commit()
    audit_log('school_results_term_released', 'school', 'student', student_id,
              {'session_id': session_id, 'term': term, 'released': len(due)})
    announce_ready_report_cards([(student_id, session_id, term)], me['id'])
    waiting = one_scalar(
        select(func.count()).select_from(SchoolStudentResult)
        .outerjoin(SchoolAssessment, SchoolAssessment.id == SchoolStudentResult.assessment_id)
        .where(SchoolStudentResult.student_id == student_id, SchoolStudentResult.session_id == session_id,
               _term_of() == term, SchoolStudentResult.status != 'released', _official()), 0)
    if waiting:
        flash(f'Released {len(due)} result{"" if len(due) == 1 else "s"}. {waiting} more still '
              f'need{"s" if waiting == 1 else ""} to be verified or approved, so the report card is not ready yet.', 'success')
    else:
        flash(f'Released all {len(due)} result{"" if len(due) == 1 else "s"} for the term. The report card '
              'is ready, and the parents are being told by email and SMS.', 'success')
    return redirect(back)


@app.route('/admin/school/results/release-class', methods=['GET', 'POST'])
@admin_required
@csrf_protect
def admin_school_results_release_class():
    """Preview, then release, every student's approved results for one class's term at once -
    the bulk complement to the one-student-at-a-time release button above."""
    me = current_admin()
    if not admin_has_permission(me['id'], 'school.results.release'):
        return admin_access_error('school.results.release')
    class_name = request.values.get('class', '').strip()
    class_row = db.session.scalars(select(SchoolClass).where(
        SchoolClass.name == class_name, SchoolClass.active == 1)).first()
    if not class_row:
        abort(404)
    if not _school_class_allowed(me['id'], class_row.id):
        return admin_access_error('school.results.release')
    sessions = db.session.scalars(select(AcademicSession).where(AcademicSession.active == 1)
                                  .order_by(AcademicSession.id.desc())).all()

    session_id = request.values.get('session', type=int)
    term = request.values.get('term', '')
    if not session_id or term not in TERMS:
        # Nothing chosen yet: offer the picker, the same shape as the per-student flow's own.
        return render_template('admin_school_results_release_class.html', class_row=class_row,
                               sessions=sessions, terms=TERMS, session_id=None, term=None, preview=None)

    students = [dict(r) for r in all_rows(
        select(Student.id, Student.admission_no, Student.first_name, Student.last_name)
        .join(StudentEnrolment, and_(StudentEnrolment.student_id == Student.id,
                                     StudentEnrolment.active == 1, StudentEnrolment.class_id == class_row.id))
        .where(Student.active == 1).distinct()
        .order_by(Student.last_name, Student.first_name, Student.id))]

    preview = []
    for s in students:
        rows = _term_rows(s['id'], session_id, term, me)
        if not rows:
            continue
        preview.append({**s, **_summary(rows)})

    if request.method == 'POST':
        released_students = released_total = 0
        for row in preview:
            if not row['approved']:
                continue
            due = db.session.scalars(
                select(SchoolStudentResult)
                .outerjoin(SchoolAssessment, SchoolAssessment.id == SchoolStudentResult.assessment_id)
                .where(SchoolStudentResult.student_id == row['id'], SchoolStudentResult.session_id == session_id,
                       _term_of() == term, SchoolStudentResult.status == 'approved', _official())).all()
            due = [r for r in due if _school_subject_allowed(me['id'], r.subject_id)]
            if not due:
                continue
            now = datetime.now(timezone.utc).isoformat()
            for r in due:
                r.status, r.released_at, r.updated_at, r.updated_by = 'released', now, now, me['id']
                db.session.add(ResultWorkflowEvent(
                    result_id=r.id, from_status='approved', to_status='released', actor_admin_id=me['id'],
                    reason=f'Released with the whole class ({class_name})', created_at=now))
            db.session.commit()
            audit_log('school_results_term_released', 'school', 'student', row['id'],
                      {'session_id': session_id, 'term': term, 'released': len(due), 'bulk_class': class_name})
            announce_ready_report_cards([(row['id'], session_id, term)], me['id'])
            released_students += 1
            released_total += len(due)
        if released_students:
            flash(f'Released {released_total} result(s) across {released_students} student(s) in {class_name}. '
                  'Parents of any student whose term is now fully released are being told.', 'success')
        else:
            flash('Nothing was ready to release for this class and term.', 'error')
        return redirect(url_for('admin_school_results_release_class', **{'class': class_name},
                                session=session_id, term=term))

    return render_template('admin_school_results_release_class.html', class_row=class_row, sessions=sessions,
                           terms=TERMS, session_id=session_id, term=term, preview=preview)
