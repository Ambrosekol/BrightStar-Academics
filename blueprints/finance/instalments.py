"""Finance > Instalments: letting a family pay a billing period's fees in parts, each by its own date.

A plan belongs to one session and one billing period (Full Session, or a term): "First Term in two parts: 60% by 15
September, 40% by 15 October". Students are put on it one by one or a whole class at once, from here or from a student's
fee account. For a student on a plan, what is overdue for that billing period is the share of everything charged for
it whose date has passed, less what has been applied to those fees; a part not yet due is not overdue, however much is
owed. A student on no plan is overdue as before: each fee from the day after its own due date.

``overdue_and_next`` is the one place this is worked out. Student accounts, the fee reminders and the report card hold
all use it, so a family on a plan is never chased, or held back, for a part that is not due yet.
"""

import json
from datetime import date, datetime, timezone

from flask import abort, flash, redirect, render_template, request, url_for
from sqlalchemy import and_, delete, func, select

from app import ACADEMIC_TERMS, _active_sessions, _school_current_session, app
from blueprints.finance.helpers import _finance_student_outstanding, _money
from core.db_helpers import all_rows, obj, tuples
from core.security import admin_required, audit_log, csrf_protect, current_admin
from models import (
    AcademicSession, FinanceInstalmentPlan, FinanceStudentPlan, SchoolClass, Student, StudentEnrolment, db,
)

PERIODS = ['Full Session'] + ACADEMIC_TERMS
MAX_PARTS = 6


def parts_of(plan):
    """``[(percent, 'YYYY-MM-DD'), ...]`` in date order, from a plan row (or its parts_json)."""
    raw = plan.parts_json if hasattr(plan, 'parts_json') else plan
    try:
        parts = [(float(p), str(d)[:10]) for p, d in json.loads(raw or '[]')]
    except (TypeError, ValueError):
        return []
    return sorted(parts, key=lambda x: x[1])


def describe(parts):
    """'60% by 15 Sep · 40% by 15 Oct'."""
    out = []
    for p, d in parts:
        try:
            when = date.fromisoformat(d).strftime('%d %b').lstrip('0')
        except ValueError:
            when = d
        out.append(f'{p:g}% by {when}')
    return ' · '.join(out)


def plans_by_student(session_id, student_ids=None):
    """``{student_id: {term: (plan id, plan name, parts)}}`` for the session's active plans, in one query."""
    scope = [FinanceInstalmentPlan.session_id == session_id, FinanceInstalmentPlan.active == 1]
    if student_ids is not None:
        scope.append(FinanceStudentPlan.student_id.in_(list(student_ids) or [0]))
    out = {}
    for sid, pid, name, term, raw in tuples(
            select(FinanceStudentPlan.student_id, FinanceInstalmentPlan.id, FinanceInstalmentPlan.name,
                   FinanceInstalmentPlan.term, FinanceInstalmentPlan.parts_json)
            .join(FinanceInstalmentPlan, FinanceInstalmentPlan.id == FinanceStudentPlan.plan_id).where(*scope)):
        out.setdefault(int(sid), {})[term] = (pid, name, parts_of(raw))
    return out


def plans_for_students(student_ids=None):
    """``{student_id: {session_id: {term: (plan id, plan name, parts)}}}`` for every active plan in every session, in
    one query: what the reminders and the report card hold need across sessions. ``student_ids`` None is everyone."""
    scope = [FinanceInstalmentPlan.active == 1]
    if student_ids is not None:
        scope.append(FinanceStudentPlan.student_id.in_(list(student_ids) or [0]))
    out = {}
    for sid, session_id, pid, name, term, raw in tuples(
            select(FinanceStudentPlan.student_id, FinanceInstalmentPlan.session_id, FinanceInstalmentPlan.id,
                   FinanceInstalmentPlan.name, FinanceInstalmentPlan.term, FinanceInstalmentPlan.parts_json)
            .join(FinanceInstalmentPlan, FinanceInstalmentPlan.id == FinanceStudentPlan.plan_id).where(*scope)):
        out.setdefault(int(sid), {}).setdefault(int(session_id), {})[term] = (pid, name, parts_of(raw))
    return out


def overdue_and_next(fees, paid_by_fee, plans, today=None):
    """What one student owes past its date, and what falls due next, for one session.

    ``fees``: their bills (each with id, amount, due_date and term); ``paid_by_fee``: {bill id: applied};
    ``plans``: {term: (id, name, parts)} for this student. Returns ``(overdue, next)`` where ``next`` is
    ``(date, amount)`` of the next amount to pay by a date, or None."""
    today = (today or date.today()).isoformat()
    overdue, upcoming = 0.0, []
    by_term = {}
    for f in fees:
        by_term.setdefault(f['term'] or 'Full Session', []).append(f)
    for term, items in by_term.items():
        charged = round(sum(float(f['amount'] or 0) for f in items), 2)
        paid = round(sum(min(float(f['amount'] or 0), float(paid_by_fee.get(f['id'], 0) or 0)) for f in items), 2)
        plan = plans.get(term)
        if plan and plan[2]:
            # Each part's date is the last day to pay it: the share up to a date is overdue from the day after.
            share = due_now = 0.0
            for percent, when in plan[2]:
                share = min(share + percent, 100.0)
                due = round(charged * share / 100, 2)
                if when < today:
                    due_now = due
                elif due - paid > 0.004:
                    upcoming.append((when, round(due - max(paid, due_now), 2)))
                    break
            overdue += max(0.0, due_now - paid)
        else:
            for f in items:
                owed = max(0.0, float(f['amount'] or 0) - float(paid_by_fee.get(f['id'], 0) or 0))
                if owed <= 0.004:
                    continue
                if f['due_date'] and str(f['due_date'])[:10] < today:
                    overdue += owed
                elif f['due_date']:
                    upcoming.append((str(f['due_date'])[:10], round(owed, 2)))
    nxt = None
    if upcoming:
        first = min(w for w, _ in upcoming)
        nxt = (first, round(sum(a for w, a in upcoming if w == first), 2))
    return round(overdue, 2), nxt


def schedule(student_id, session_id, today=None):
    """For the fee account pages: each billing period the student is charged in this session, with its plan (if any)
    and each part's amount and state ('paid', 'overdue' or 'to pay')."""
    today = (today or date.today()).isoformat()
    fees = _finance_student_outstanding(student_id, session_id)
    plans = plans_by_student(session_id, [student_id]).get(int(student_id), {})
    out = []
    for term in sorted({f['term'] or 'Full Session' for f in fees}, key=lambda t: PERIODS.index(t) if t in PERIODS else 9):
        items = [f for f in fees if (f['term'] or 'Full Session') == term]
        charged = round(sum(f['assessed'] for f in items), 2)
        paid = round(sum(f['paid'] for f in items), 2)
        plan = plans.get(term)
        parts = []
        if plan:
            share = 0.0
            for percent, when in plan[2]:
                share += percent
                upto = round(charged * min(share, 100.0) / 100, 2)
                amount = round(charged * percent / 100, 2)
                state = 'paid' if paid >= upto - 0.004 else ('overdue' if when < today else 'to pay')
                parts.append({'percent': percent, 'date': when, 'amount': amount, 'state': state})
        out.append({'term': term, 'charged': charged, 'paid': paid, 'plan_id': plan[0] if plan else None,
                    'plan_name': plan[1] if plan else None, 'parts': parts})
    return out


def session_plans(session_id, term=None):
    scope = [FinanceInstalmentPlan.session_id == session_id, FinanceInstalmentPlan.active == 1]
    if term:
        scope.append(FinanceInstalmentPlan.term == term)
    return db.session.scalars(select(FinanceInstalmentPlan).where(*scope).order_by(FinanceInstalmentPlan.name)).all()


def assign(student_ids, plan, admin_id):
    """Put students on a plan, taking each off any other plan for the same session and billing period. Does not
    commit. Returns how many were not on it before."""
    ids = list({int(s) for s in student_ids})
    if not ids:
        return 0
    others = select(FinanceInstalmentPlan.id).where(FinanceInstalmentPlan.session_id == plan.session_id,
                                                    FinanceInstalmentPlan.term == plan.term,
                                                    FinanceInstalmentPlan.id != plan.id)
    db.session.execute(delete(FinanceStudentPlan).where(FinanceStudentPlan.student_id.in_(ids),
                                                        FinanceStudentPlan.plan_id.in_(others)))
    have = {int(s) for (s,) in tuples(select(FinanceStudentPlan.student_id).where(
        FinanceStudentPlan.plan_id == plan.id, FinanceStudentPlan.student_id.in_(ids)))}
    now = datetime.now(timezone.utc).isoformat()
    new = [s for s in ids if s not in have]
    db.session.add_all([FinanceStudentPlan(student_id=s, plan_id=plan.id, assigned_by=admin_id, assigned_at=now)
                        for s in new])
    return len(new)


def _parse_parts(form):
    """The plan's parts from the form, checked: ``(parts, errors)``."""
    parts, errors = [], []
    for raw_pct, raw_date in zip(form.getlist('part_percent'), form.getlist('part_date')):
        if not str(raw_pct).strip() and not str(raw_date).strip():
            continue
        pct = _money(raw_pct)
        try:
            when = date.fromisoformat(str(raw_date).strip()).isoformat()
        except ValueError:
            when = None
        if pct is None or pct <= 0 or not when:
            errors.append('Give every part a share above 0% and a date.')
            break
        parts.append([pct, when])
    parts.sort(key=lambda x: x[1])
    if not errors:
        if len(parts) < 2:
            errors.append('A plan needs at least two parts. With one, use the fee\'s own due date instead.')
        elif len(parts) > MAX_PARTS:
            errors.append(f'A plan can have at most {MAX_PARTS} parts.')
        elif abs(sum(p for p, _ in parts) - 100) > 0.01:
            errors.append(f'The parts add up to {sum(p for p, _ in parts):g}%. They must add up to 100%.')
        elif len({w for _, w in parts}) != len(parts):
            errors.append('Two parts fall on the same date. Give each part its own date.')
    return parts, errors


def _plan_rows(session_id):
    """The session's plans with how many students each has and what they owe now."""
    from blueprints.finance import analytics
    plans = db.session.scalars(select(FinanceInstalmentPlan).where(FinanceInstalmentPlan.session_id == session_id)
                               .order_by(FinanceInstalmentPlan.active.desc(), FinanceInstalmentPlan.term,
                                         FinanceInstalmentPlan.name)).all()
    members = {}
    for pid, sid in tuples(select(FinanceStudentPlan.plan_id, FinanceStudentPlan.student_id)
                           .where(FinanceStudentPlan.plan_id.in_([p.id for p in plans] or [0]))):
        members.setdefault(pid, set()).add(sid)
    accounts = {r['id']: r for r in analytics.accounts(session_id)} if plans else {}
    rows = []
    for p in plans:
        ids = members.get(p.id, set())
        rows.append({'plan': p, 'parts': parts_of(p), 'text': describe(parts_of(p)), 'students': len(ids),
                     'overdue': round(sum(accounts[s]['overdue'] for s in ids if s in accounts), 2),
                     'balance': round(sum(accounts[s]['balance'] for s in ids if s in accounts), 2)})
    return rows


def _requested_session():
    sessions = _active_sessions()
    wanted = request.values.get('session_id', type=int)
    chosen = next((s for s in sessions if s.id == wanted), None)
    if not chosen:
        current = _school_current_session()
        chosen = next((s for s in sessions if current and s.id == current['id']), None) or (sessions[0] if sessions else None)
    return sessions, chosen


@app.route('/admin/finance/instalments')
@admin_required
def admin_finance_instalments():
    sessions, chosen = _requested_session()
    rows = _plan_rows(chosen.id) if chosen else []
    return render_template('finance_instalments.html', sessions=sessions, fin_session=chosen, rows=rows,
                           on_plans=sum(r['students'] for r in rows if r['plan'].active))


@app.route('/admin/finance/instalments/new', methods=['GET', 'POST'])
@app.route('/admin/finance/instalments/<int:plan_id>/edit', methods=['GET', 'POST'], endpoint='admin_finance_instalment_edit')
@admin_required
@csrf_protect
def admin_finance_instalment_new(plan_id=None):
    me = current_admin()
    plan = obj(FinanceInstalmentPlan, plan_id) if plan_id else None
    if plan_id and not plan:
        abort(404)
    sessions, chosen = _requested_session()
    errors = []
    if request.method == 'POST':
        name = ' '.join(request.form.get('name', '').split())[:80]
        term = request.form.get('term', 'Full Session')
        session_id = plan.session_id if plan else request.form.get('session_id', type=int)
        parts, errors = _parse_parts(request.form)
        if not name:
            errors.insert(0, 'Name the plan, for example "First Term in two parts".')
        if term not in PERIODS:
            errors.append('Choose the billing period the plan is for.')
        if not plan and session_id not in {s.id for s in sessions}:
            errors.append('Choose the session.')
        # Students are on a plan for its billing period, one plan each; moving the plan to another period would
        # leave some of them on two.
        if plan and term != plan.term and db.session.scalar(
                select(func.count()).select_from(FinanceStudentPlan).where(FinanceStudentPlan.plan_id == plan.id)):
            errors.append(f'Students are on this plan for {plan.term}. Make a new plan for {term} instead.')
        if not errors:
            now = datetime.now(timezone.utc).isoformat()
            if plan:
                plan.name, plan.term, plan.parts_json, plan.updated_at = name, term, json.dumps(parts), now
            else:
                plan = FinanceInstalmentPlan(name=name, session_id=session_id, term=term, parts_json=json.dumps(parts),
                                             active=1, created_by=me['id'], created_at=now)
                db.session.add(plan)
            db.session.flush()
            audit_log('finance_instalment_plan_saved', 'finance', 'instalment_plan', plan.id,
                      {'name': name, 'term': term, 'parts': parts, 'new': not plan_id}, commit=False)
            db.session.commit()
            flash(f'"{name}" saved: {describe(parts_of(plan))}.'
                  + ('' if plan_id else ' Now put students on it.'), 'success')
            return redirect(url_for('admin_finance_instalments', session_id=plan.session_id))
        form_parts = list(zip(request.form.getlist('part_percent'), request.form.getlist('part_date')))
    else:
        form_parts = [(f'{p:g}', d) for p, d in parts_of(plan)] if plan else [('50', ''), ('50', '')]
    form = request.form if request.method == 'POST' else ({'name': plan.name, 'term': plan.term} if plan else {})
    return render_template('finance_instalment_form.html', plan=plan, form=form, plan_parts=form_parts, errors=errors,
                           sessions=sessions, fin_session=chosen, periods=PERIODS, max_parts=MAX_PARTS,
                           plan_session_name=db.session.get(AcademicSession, plan.session_id).name if plan else '')


@app.post('/admin/finance/instalments/<int:plan_id>/toggle')
@admin_required
@csrf_protect
def admin_finance_instalment_toggle(plan_id):
    plan = obj(FinanceInstalmentPlan, plan_id)
    if not plan:
        abort(404)
    plan.active = 0 if plan.active else 1
    audit_log('finance_instalment_plan_status', 'finance', 'instalment_plan', plan.id, {'active': plan.active},
              commit=False)
    db.session.commit()
    flash(f'"{plan.name}" ' + ('is in use again.' if plan.active else
                               'is no longer used. Its students fall back to each fee\'s own due date.'), 'success')
    return redirect(url_for('admin_finance_instalments', session_id=plan.session_id))


@app.route('/admin/finance/instalments/<int:plan_id>/students', methods=['GET', 'POST'])
@admin_required
@csrf_protect
def admin_finance_instalment_students(plan_id):
    """Who is on a plan, where each stands, and putting a class or a student on it."""
    me = current_admin()
    plan = obj(FinanceInstalmentPlan, plan_id)
    if not plan:
        abort(404)
    if request.method == 'POST':
        class_id = request.form.get('class_id', type=int)
        student_id = request.form.get('student_id', type=int)
        if class_id:
            ids = [s for (s,) in tuples(select(StudentEnrolment.student_id)
                                        .join(Student, Student.id == StudentEnrolment.student_id)
                                        .where(StudentEnrolment.class_id == class_id, StudentEnrolment.active == 1,
                                               StudentEnrolment.session_id == plan.session_id, Student.active == 1))]
        elif student_id:
            ids = [student_id] if db.session.scalar(select(Student.id).where(Student.id == student_id,
                                                                            Student.active == 1)) else []
        else:
            ids = []
        if not ids:
            flash('Choose a class with students this session, or a student.', 'error')
        else:
            added = assign(ids, plan, me['id'])
            audit_log('finance_instalment_students_added', 'finance', 'instalment_plan', plan.id,
                      {'student_ids': ids, 'class_id': class_id}, commit=False)
            db.session.commit()
            flash(f'{added} student{"" if added == 1 else "s"} put on "{plan.name}".'
                  + (f' {len(ids) - added} already on it.' if len(ids) - added else ''), 'success')
        return redirect(url_for('admin_finance_instalment_students', plan_id=plan.id))
    from blueprints.finance import analytics
    accounts = {r['id']: r for r in analytics.accounts(plan.session_id)}
    ids = [s for (s,) in tuples(select(FinanceStudentPlan.student_id).where(FinanceStudentPlan.plan_id == plan.id))]
    students = sorted([accounts[s] for s in ids if s in accounts], key=lambda r: (-r['overdue'], r['name']))
    classes = all_rows(select(SchoolClass.id, SchoolClass.name, func.count(StudentEnrolment.id).label('n'))
                       .join(StudentEnrolment, and_(StudentEnrolment.class_id == SchoolClass.id,
                                                    StudentEnrolment.session_id == plan.session_id,
                                                    StudentEnrolment.active == 1))
                       .where(SchoolClass.active == 1).group_by(SchoolClass.id, SchoolClass.name, SchoolClass.level_order)
                       .order_by(SchoolClass.level_order, SchoolClass.name))
    on = set(ids)
    pick = [r for r in sorted(accounts.values(), key=lambda r: r['name']) if r['id'] not in on]
    return render_template('finance_instalment_students.html', plan=plan, text=describe(parts_of(plan)),
                           students=students, classes=classes, pick=pick,
                           session_name=db.session.get(AcademicSession, plan.session_id).name)


@app.post('/admin/finance/instalments/<int:plan_id>/students/<int:student_id>/remove')
@admin_required
@csrf_protect
def admin_finance_instalment_student_remove(plan_id, student_id):
    db.session.execute(delete(FinanceStudentPlan).where(FinanceStudentPlan.plan_id == plan_id,
                                                        FinanceStudentPlan.student_id == student_id))
    audit_log('finance_instalment_student_removed', 'finance', 'instalment_plan', plan_id, {'student_id': student_id},
              commit=False)
    db.session.commit()
    flash('Taken off the plan. Their fees are due by each fee\'s own due date again.', 'success')
    if request.form.get('back') == 'account':
        return redirect(url_for('admin_finance_student_account', student_id=student_id))
    return redirect(url_for('admin_finance_instalment_students', plan_id=plan_id))


@app.post('/admin/finance/students/<int:student_id>/plan')
@admin_required
@csrf_protect
def admin_finance_student_plan(student_id):
    """From a student's fee account: choose the plan for one billing period, or none."""
    me = current_admin()
    session_id = request.form.get('session_id', type=int)
    term = request.form.get('term', '')
    plan_id = request.form.get('plan_id', type=int)
    if term not in PERIODS or not session_id:
        abort(400)
    if not db.session.get(Student, student_id):
        abort(404)
    if plan_id:
        plan = obj(FinanceInstalmentPlan, plan_id)
        if not plan or plan.session_id != session_id or plan.term != term or not plan.active:
            abort(400)
        assign([student_id], plan, me['id'])
        flash(f'On "{plan.name}" for {term}: {describe(parts_of(plan))}.', 'success')
    else:
        db.session.execute(delete(FinanceStudentPlan).where(
            FinanceStudentPlan.student_id == student_id,
            FinanceStudentPlan.plan_id.in_(select(FinanceInstalmentPlan.id).where(
                FinanceInstalmentPlan.session_id == session_id, FinanceInstalmentPlan.term == term))))
        flash(f'No plan for {term}: each fee is due by its own date.', 'success')
    audit_log('finance_student_plan_set', 'finance', 'student', student_id,
              {'session_id': session_id, 'term': term, 'plan_id': plan_id}, commit=False)
    db.session.commit()
    return redirect(url_for('admin_finance_student_account', student_id=student_id, session_id=session_id))
