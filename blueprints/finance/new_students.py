"""Finance > New students to bill: every student who joined the school - admitted from the entrance exam, registered
by hand or imported - until the bursar has billed them and the bill is paid.

A row is written for each new student the moment they are created (``_note_new_students``, a SQLAlchemy after_flush
listener), so every way a student comes in is covered without any of them knowing about this list; students already
in the school when this list arrived are not on it. Admissions and the bulk import say how the student came in
(``student._joined_by``); anything else is 'registered'.

A student is on the list in one of two stages, worked out from their fees each time it is read:

    Not billed yet       nothing billed to them (no active fee assessment)
    Waiting for payment  billed, and not yet paid in full

"Paid" means what it means everywhere in Finance: applied to the fee (finance_payment_allocations), so a payment
recorded but not yet applied shows as "received, not yet applied" and the student stays until it is. Once everything
billed is paid the row is closed ('paid') the next time the list is read. Someone with finance.manage can also take a
student off by hand, with a note (a scholarship, a staff child), and put them back.
"""

from datetime import datetime, timezone

from flask import abort, flash, redirect, render_template, request, url_for
from sqlalchemy import event, func, insert, select

from app import _school_current_session, app
from core.security import (
    admin_access_error, admin_has_permission, admin_required, audit_log, csrf_protect, current_admin, is_school_admin,
)
from core.short_cache import forget_here, remember
from control_plane.config import badge_cache_seconds
from models import (
    Admin, Candidate, ClassSubdivision, FinanceFeeAssessment, FinanceNewStudent, FinancePayment,
    FinancePaymentAllocation, SchoolClass, Student, StudentEnrolment, db,
)

PAGE = 'admin_finance_new_students'
VIEWS = (('unbilled', 'Not billed yet'), ('awaiting', 'Waiting for payment'), ('cleared', 'Done'))
SOURCES = {'admitted': 'Admitted from the entrance exam', 'registered': 'Registered', 'imported': 'Imported'}
PER_PAGE = 50


# ---------------------------------------------------------------- noting a new student
@event.listens_for(db.session, 'after_flush')
def _note_new_students(session, _context):
    """Put every student created in this save on the list (an active one, not one created already archived). A new
    student or a new bill changes the count on the menu, so its saved copy is dropped."""
    if any(isinstance(obj, (Student, FinanceFeeAssessment)) for obj in session.new):
        forget_here('finance-new-students')
    fresh = [obj for obj in session.new if isinstance(obj, Student) and obj.id and (obj.active in (None, 1))
             and not obj.archived_at]
    if not fresh:
        return
    now = datetime.now(timezone.utc).isoformat()
    session.connection().execute(insert(FinanceNewStudent.__table__), [
        {'student_id': s.id, 'source': getattr(s, '_joined_by', None) or 'registered', 'added_at': now}
        for s in fresh])


# ---------------------------------------------------------------- reading the list
def _sums():
    """(billed per student, applied per student, received per student) as subqueries, over every session."""
    billed = (select(FinanceFeeAssessment.student_id.label('sid'), func.sum(FinanceFeeAssessment.amount).label('v'))
              .where(FinanceFeeAssessment.active == 1, FinanceFeeAssessment.voided_at.is_(None))
              .group_by(FinanceFeeAssessment.student_id).subquery())
    applied = (select(FinanceFeeAssessment.student_id.label('sid'), func.sum(FinancePaymentAllocation.amount).label('v'))
               .join(FinanceFeeAssessment, FinanceFeeAssessment.id == FinancePaymentAllocation.assessment_id)
               .join(FinancePayment, FinancePayment.id == FinancePaymentAllocation.payment_id)
               .where(FinancePaymentAllocation.voided_at.is_(None), FinancePayment.status == 'posted',
                      FinanceFeeAssessment.active == 1, FinanceFeeAssessment.voided_at.is_(None))
               .group_by(FinanceFeeAssessment.student_id).subquery())
    received = (select(FinancePayment.student_id.label('sid'), func.sum(FinancePayment.amount).label('v'))
                .where(FinancePayment.status == 'posted').group_by(FinancePayment.student_id).subquery())
    return billed, applied, received


def _stage(billed, applied):
    if billed <= 0:
        return 'unbilled'
    return 'paid' if round(applied, 2) >= round(billed, 2) else 'awaiting'


def _rows(open_only=True):
    """The list's rows, each with its stage and money; one query."""
    billed, applied, received = _sums()
    current = _school_current_session()
    enrol = (select(StudentEnrolment.student_id, StudentEnrolment.class_id, StudentEnrolment.subdivision_id)
             .where(StudentEnrolment.active == 1, StudentEnrolment.session_id == (current['id'] if current else 0))
             .subquery())
    stmt = (select(FinanceNewStudent, Student.first_name, Student.middle_name, Student.last_name, Student.admission_no,
                   Student.student_number, SchoolClass.name.label('class_name'), ClassSubdivision.name.label('sub_name'),
                   func.coalesce(billed.c.v, 0), func.coalesce(applied.c.v, 0), func.coalesce(received.c.v, 0))
            .join(Student, Student.id == FinanceNewStudent.student_id)
            .outerjoin(enrol, enrol.c.student_id == Student.id)
            .outerjoin(SchoolClass, SchoolClass.id == enrol.c.class_id)
            .outerjoin(ClassSubdivision, ClassSubdivision.id == enrol.c.subdivision_id)
            .outerjoin(billed, billed.c.sid == Student.id)
            .outerjoin(applied, applied.c.sid == Student.id)
            .outerjoin(received, received.c.sid == Student.id))
    if open_only:
        stmt = stmt.where(FinanceNewStudent.cleared_at.is_(None), Student.active == 1, Student.archived_at.is_(None))
    out = []
    for row, first, middle, last, adm, number, class_name, sub_name, b, a, r in db.session.execute(
            stmt.order_by(FinanceNewStudent.added_at.desc(), FinanceNewStudent.id.desc())).all():
        b, a, r = round(float(b or 0), 2), round(float(a or 0), 2), round(float(r or 0), 2)
        out.append({'row': row, 'id': row.student_id,
                    'name': ' '.join(p for p in (first, middle, last) if p and str(p).strip()),
                    'number': number or adm or '', 'class_name': class_name or '', 'sub_name': sub_name or '',
                    'billed': b, 'applied': a, 'owed': round(max(0.0, b - a), 2),
                    'unapplied': round(max(0.0, r - a), 2), 'stage': _stage(b, a)})
    return out


def _close_paid(rows):
    """Close the rows whose bill is now paid in full. Returns how many."""
    now = datetime.now(timezone.utc).isoformat()
    done = [r for r in rows if r['stage'] == 'paid' and r['row'].cleared_at is None]
    for r in done:
        r['row'].cleared_at, r['row'].cleared_reason = now, 'paid'
    if done:
        db.session.commit()
        forget_here('finance-new-students')
    return len(done)


def unbilled_count():
    """New students with nothing billed yet: the number on the menu."""
    billed = (select(FinanceFeeAssessment.student_id).where(FinanceFeeAssessment.active == 1,
                                                            FinanceFeeAssessment.voided_at.is_(None)))
    return db.session.scalar(select(func.count()).select_from(FinanceNewStudent)
                             .join(Student, Student.id == FinanceNewStudent.student_id)
                             .where(FinanceNewStudent.cleared_at.is_(None), Student.active == 1,
                                    Student.archived_at.is_(None), FinanceNewStudent.student_id.not_in(billed))) or 0


def _may_see(admin):
    return bool(admin and (is_school_admin(admin) or admin_has_permission(admin['id'], 'finance.record')
                           or admin_has_permission(admin['id'], 'finance.manage')))


def new_students_badge():
    """For the menu: how many new students are not billed yet (0 for someone who may not see the list)."""
    admin = current_admin()
    if not _may_see(admin):
        return 0
    try:
        return remember('finance-new-students', badge_cache_seconds(), unbilled_count)
    except Exception:
        db.session.rollback()
        app.logger.exception('Could not count the new students to bill')
        return 0


# ---------------------------------------------------------------- pages
@app.route('/admin/finance/new-students')
@admin_required
def admin_finance_new_students():
    me = current_admin()
    if not _may_see(me):
        return admin_access_error('finance.record')
    view = request.args.get('view', 'unbilled')
    view = view if view in dict(VIEWS) else 'unbilled'
    q = request.args.get('q', '').strip()[:80]
    rows = _rows(open_only=True)
    _close_paid(rows)
    rows = [r for r in rows if r['stage'] != 'paid']
    counts = {'unbilled': sum(1 for r in rows if r['stage'] == 'unbilled'),
              'awaiting': sum(1 for r in rows if r['stage'] == 'awaiting')}
    if view == 'cleared':
        shown = [r for r in _rows(open_only=False) if r['row'].cleared_at]
        shown.sort(key=lambda r: r['row'].cleared_at or '', reverse=True)
    else:
        shown = [r for r in rows if r['stage'] == view]
    counts['cleared'] = db.session.scalar(select(func.count()).select_from(FinanceNewStudent)
                                          .where(FinanceNewStudent.cleared_at.is_not(None))) or 0
    if q:
        needle = q.casefold()
        shown = [r for r in shown if needle in f"{r['name']} {r['number']} {r['class_name']} {r['sub_name']}".casefold()]
    try:
        page = max(1, int(request.args.get('page', '1')))
    except ValueError:
        page = 1
    pages = max(1, -(-len(shown) // PER_PAGE))
    page = min(page, pages)
    shown_page = shown[(page - 1) * PER_PAGE:page * PER_PAGE]
    people = {r['row'].cleared_by for r in shown_page if r['row'].cleared_by}
    names = {a.id: a.display_name for a in db.session.scalars(select(Admin).where(Admin.id.in_(people or {0})))}
    admitted = dict(db.session.execute(select(Candidate.admitted_student_id, Candidate.candidate_code)
                                       .where(Candidate.admitted_student_id.in_([r['id'] for r in shown_page] or [0]))).all())
    return render_template('finance_new_students.html', rows=shown_page, view=view, views=VIEWS, counts=counts, q=q,
                           page=page, pages=pages, total=len(shown), sources=SOURCES, names=names, admitted=admitted,
                           owed_total=round(sum(r['owed'] for r in rows if r['stage'] == 'awaiting'), 2),
                           can_bill=is_school_admin(me) or admin_has_permission(me['id'], 'finance.manage'),
                           can_record=is_school_admin(me) or admin_has_permission(me['id'], 'finance.record'),
                           can_accounts=is_school_admin(me) or admin_has_permission(me['id'], 'finance.view_all'))


@app.post('/admin/finance/new-students/remove')
@admin_required
@csrf_protect
def admin_finance_new_students_remove():
    """Take students off the list by hand (the school does not bill them), with a note saying why."""
    me = current_admin()
    if not (is_school_admin(me) or admin_has_permission(me['id'], 'finance.manage')):
        return admin_access_error('finance.manage')
    ids = {int(v) for v in request.form.getlist('student_id') if str(v).isdigit()}
    note = ' '.join(request.form.get('note', '').split())[:300]
    back = url_for(PAGE, view=request.form.get('view', 'unbilled'))
    if not ids:
        flash('Tick the students to take off the list first.', 'error')
        return redirect(back)
    if not note:
        flash('Say why they are taken off the list (for example, a scholarship), so the bursar knows later.', 'error')
        return redirect(back)
    rows = db.session.scalars(select(FinanceNewStudent).where(FinanceNewStudent.student_id.in_(ids),
                                                              FinanceNewStudent.cleared_at.is_(None))).all()
    now = datetime.now(timezone.utc).isoformat()
    for row in rows:
        row.cleared_at, row.cleared_by, row.cleared_reason, row.note = now, me['id'], 'removed', note
    db.session.commit()
    forget_here('finance-new-students')
    audit_log('finance_new_students_removed', 'finance', 'student', None,
              {'student_ids': sorted(r.student_id for r in rows), 'note': note})
    flash(f'{len(rows)} student{"" if len(rows) == 1 else "s"} taken off the list.', 'success')
    return redirect(back)


@app.post('/admin/finance/new-students/<int:student_id>/restore')
@admin_required
@csrf_protect
def admin_finance_new_students_restore(student_id):
    """Put a student taken off by hand back on the list (one closed because they paid stays closed)."""
    me = current_admin()
    if not (is_school_admin(me) or admin_has_permission(me['id'], 'finance.manage')):
        return admin_access_error('finance.manage')
    row = db.session.scalars(select(FinanceNewStudent).where(FinanceNewStudent.student_id == student_id)).first()
    if row is None:
        abort(404)
    if row.cleared_reason != 'removed':
        flash('Only a student taken off the list by hand can be put back.', 'error')
        return redirect(url_for(PAGE, view='cleared'))
    row.cleared_at = row.cleared_by = row.cleared_reason = row.note = None
    db.session.commit()
    forget_here('finance-new-students')
    audit_log('finance_new_students_restored', 'finance', 'student', student_id, {})
    flash('Put back on the list.', 'success')
    return redirect(url_for(PAGE))


app.jinja_env.globals.update(finance_new_students_badge=new_students_badge)
