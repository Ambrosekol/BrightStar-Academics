"""Payroll: each member of staff's pay, the school's statutory settings and pay items, the monthly run with its
payslips, the bank schedule and statutory returns, and each person's own payslips. The rules are core/payroll.py and
the tables models/payroll.py; who is on the payroll comes from the staff records (models/hr.py).

Who may do what.
* payroll.view: the runs, every payslip in them, and each person's pay. Salaries are seen nowhere else.
* payroll.manage: set people's pay, the pay items and the statutory settings; prepare a run, add one-off lines, work
  it out again, cancel a draft, mark an approved run paid.
* payroll.approve: approve a run, which locks it, and reopen an approved run that is not yet paid (and has had no
  salary sent through Paystack).
* Paying an approved run through the school's Paystack is the School Admin's alone: blueprints/school/payroll_paystack.py.
Four eyes. A run is approved by someone other than whoever last prepared or changed it. Nobody sets their own pay or
adds a line to their own payslip. A paid run is final.
Bank account numbers, pension PINs and tax IDs are decrypted only for the bank schedule and the statutory returns of an
approved run, for someone who may manage or approve payroll; each download is logged. Every page with salaries is
never cached, and every opening of a run or a payslip is logged.
Everyone sees their own payslips, once their run is approved, on My payslips.
"""

import csv
import io
import json
import re
from datetime import date, datetime, timedelta, timezone

from flask import Response, abort, flash, g, make_response, redirect, render_template, request, url_for
from sqlalchemy import delete as sa_delete, func, select

from app import app
from blueprints.entrance.helpers import csv_safe
from control_plane.config import badge_cache_seconds
from core import hr
from core import payroll as pr
from core.security import admin_access_error, admin_has_permission, admin_required, audit_log, csrf_protect, current_admin
from core.short_cache import forget_here, remember
from models import (
    Admin, LeaveRequest, LeaveType, PayItem, PayrollAdjustment, PayrollRun, PayrollSettings, Payslip, StaffPay,
    StaffPayItem, StaffRecord, db,
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _may(code, admin=None):
    admin = admin or current_admin()
    return bool(admin and admin_has_permission(admin['id'], code))


def _private(response):
    response.headers['Cache-Control'] = 'no-store, private'
    return response


def _logged(response, action, target_type, target_id, details=None):
    """Log a page after it is drawn. Logging commits, and a commit before drawing would make the database read every
    row on the page again, one at a time."""
    audit_log(action, 'payroll', target_type, target_id, details or {})
    return response


def _settings():
    row = db.session.scalars(select(PayrollSettings).order_by(PayrollSettings.id)).first()
    if row is None:
        row = PayrollSettings(paye_enabled=1, paye_bands_json=json.dumps(pr.DEFAULT_PAYE_BANDS), rent_relief_bp=2000,
                              rent_relief_cap=50_000_000, pension_enabled=1, pension_employee_bp=800, pension_employer_bp=1000,
                              nhf_enabled=0, nhf_bp=250, deduct_unpaid_leave=0)
        db.session.add(row)
        db.session.flush()
    return row


def _own_record(admin_id):
    return db.session.scalars(select(StaffRecord).where(StaffRecord.admin_id == admin_id)).first()


def _items(active_only=False):
    stmt = select(PayItem).order_by(PayItem.kind.desc(), PayItem.sort_order, PayItem.name)
    if active_only:
        stmt = stmt.where(PayItem.active == 1)
    return db.session.scalars(stmt).all()


def _names(ids):
    ids = {i for i in ids if i}
    return {a.id: a.display_name for a in db.session.scalars(select(Admin).where(Admin.id.in_(ids or {0}))).all()}


def payroll_menu(admin=None):
    """For the menu: {'payroll': may see payroll, 'mine': has a payslip to see, 'waiting': runs waiting for this
    person's approval}. Read once a request."""
    admin = admin or current_admin()
    empty = {'payroll': False, 'mine': False, 'waiting': 0}
    if not admin:
        return empty
    cached = getattr(g, '_payroll_menu', None)
    if cached is not None:
        return cached

    def compute(admin_id):
        mine = db.session.execute(
            select(Payslip.id).join(PayrollRun, PayrollRun.id == Payslip.run_id).join(StaffRecord, StaffRecord.id == Payslip.staff_id)
            .where(StaffRecord.admin_id == admin_id, PayrollRun.status.in_(('approved', 'paid'))).limit(1)).first()
        waiting = 0
        if admin_has_permission(admin_id, 'payroll.approve'):
            waiting = db.session.execute(select(func.count(PayrollRun.id)).where(
                PayrollRun.status == 'draft', PayrollRun.prepared_by != admin_id)).scalar() or 0
        return {'payroll': admin_has_permission(admin_id, 'payroll.view'), 'mine': mine is not None, 'waiting': int(waiting)}

    try:
        g._payroll_menu = remember('payroll-menu', badge_cache_seconds(), compute, admin['id'])
    except Exception:
        db.session.rollback()
        g._payroll_menu = empty
    return g._payroll_menu


app.jinja_env.globals.update(payroll_menu=payroll_menu)
app.jinja_env.filters['naira'] = pr.naira


@app.after_request
def _forget_payroll_menu(response):
    """The menu's payroll counts and My payslips change only through these pages, so only a change made here drops the counts kept for the
    menu (app.py's forget_counts_after_a_change drops the others on any write, which would recount these on every staff
    page after every student's autosave)."""
    if request.method == 'POST' and (request.endpoint or '').startswith(('admin_payroll',)) and g.get('tenant') is not None:
        forget_here('payroll-menu')
    return response


# ---------------------------------------------------------------- the runs
@app.route('/admin/payroll')
@admin_required
def admin_payroll():
    runs = db.session.scalars(select(PayrollRun).order_by(PayrollRun.period.desc(), PayrollRun.id.desc()).limit(60)).all()
    on = db.session.execute(select(func.count(StaffPay.id), func.coalesce(func.sum(StaffPay.basic), 0))
                            .join(StaffRecord, StaffRecord.id == StaffPay.staff_id)
                            .where(StaffPay.on_payroll == 1, StaffRecord.status != 'left')).one()
    settings = _settings()
    db.session.commit()
    today = date.today()
    return _logged(_private(make_response(render_template(
        'school_payroll.html', runs=runs, people=on[0], basic_total=on[1], settings=settings, pr=pr,
        names=_names({r.prepared_by for r in runs} | {r.approved_by for r in runs}), next_period=today.strftime('%Y-%m'),
        can_manage=_may('payroll.manage'), can_approve=_may('payroll.approve')))), 'payroll_opened', 'runs', None)


def _period_bounds(period):
    year, month = (int(p) for p in period.split('-'))
    first = date(year, month, 1)
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return first, last


def _unpaid_days(first, last):
    """{staff_id: working days of approved unpaid leave in the month}, for everyone at once."""
    days = {}
    for staff_id, start, end in db.session.execute(
            select(LeaveRequest.staff_id, LeaveRequest.start_date, LeaveRequest.end_date)
            .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id)
            .where(LeaveType.paid == 0, LeaveRequest.status == 'approved',
                   LeaveRequest.start_date <= last.isoformat(), LeaveRequest.end_date >= first.isoformat())).all():
        days[staff_id] = days.get(staff_id, 0) + hr.working_days(max(date.fromisoformat(start), first), min(date.fromisoformat(end), last))
    return days


def _work_out(run):
    """(Re)make every payslip of a draft run from people's pay as it is now, with the run's one-off lines."""
    settings = _settings()
    first, last = _period_bounds(run.period)
    working = hr.working_days(first, last)
    items = _items()
    own_items = {}
    for row in db.session.scalars(select(StaffPayItem)).all():
        own_items.setdefault(row.staff_id, {})[row.item_id] = row
    adjustments = {}
    for adj in db.session.scalars(select(PayrollAdjustment).where(PayrollAdjustment.run_id == run.id)).all():
        adjustments.setdefault(adj.staff_id, []).append(adj)
    db.session.execute(sa_delete(Payslip).where(Payslip.run_id == run.id))
    unpaid = _unpaid_days(first, last) if settings.deduct_unpaid_leave else {}
    people = db.session.execute(select(StaffPay, StaffRecord).join(StaffRecord, StaffRecord.id == StaffPay.staff_id)
                                .where(StaffPay.on_payroll == 1).order_by(StaffRecord.last_name, StaffRecord.first_name)).all()
    totals = {'gross': 0, 'deductions': 0, 'net': 0, 'employer_pension': 0, 'headcount': 0}
    for pay, person in people:
        if person.status == 'left' and person.exit_date and person.exit_date < first.isoformat():
            continue
        if person.employment_date and person.employment_date > last.isoformat():
            continue
        mine = own_items.get(person.id, {})
        given = [(item, mine.get(item.id)) for item in items if item.id in mine]
        slip = pr.payslip(pay, given, adjustments.get(person.id, []), settings, unpaid.get(person.id, 0), working)
        warnings = list(slip['warnings'])
        if person.status == 'suspended':
            warnings.append('Suspended')
        if person.status == 'left':
            warnings.append(f'Left on {person.exit_date}')
        db.session.add(Payslip(
            run_id=run.id, staff_id=person.id, name=hr.full_name(person, False), job_title=person.job_title, staff_no=person.staff_no,
            bank_name=pay.bank_name, account_name=pay.account_name, account_last4=pay.account_last4, pfa=pay.pfa,
            lines_json=json.dumps(slip['lines'], separators=(',', ':')), basic=slip['basic'], gross=slip['gross'],
            deductions=slip['deductions'], net=slip['net'], paye=slip['paye'], pension_employee=slip['pension_employee'],
            pension_employer=slip['pension_employer'], nhf=slip['nhf'], warnings='; '.join(warnings) or None))
        totals['gross'] += slip['gross']
        totals['deductions'] += slip['deductions']
        totals['net'] += slip['net']
        totals['employer_pension'] += slip['pension_employer']
        totals['headcount'] += 1
    for key, value in totals.items():
        setattr(run, key, value)
    run.settings_json = pr.settings_snapshot(settings)


def _run_or_404(rid, lock=False):
    stmt = select(PayrollRun).where(PayrollRun.id == rid)
    run = db.session.scalars(stmt.with_for_update() if lock else stmt).first()
    if run is None:
        abort(404)
    return run


@app.post('/admin/payroll/runs')
@admin_required
@csrf_protect
def admin_payroll_run_new():
    me = current_admin()
    period = str(request.form.get('period', '')).strip()
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', period):
        flash('Choose the month to pay.', 'error')
        return redirect(url_for('admin_payroll'))
    try:
        first, _ = _period_bounds(period)
    except (ValueError, TypeError):
        flash('Choose the month to pay.', 'error')
        return redirect(url_for('admin_payroll'))
    if not (date.today().year - 2 <= first.year <= date.today().year + 1):
        flash('Choose a month near now.', 'error')
        return redirect(url_for('admin_payroll'))
    if db.session.execute(select(PayrollRun.id).where(PayrollRun.period == period, PayrollRun.status != 'cancelled')).first():
        flash(f'There is already a payroll for {first.strftime("%B %Y")}. Open it, or cancel it to start again.', 'error')
        return redirect(url_for('admin_payroll'))
    run = PayrollRun(period=period, status='draft', prepared_by=me['id'], prepared_at=_now())
    db.session.add(run)
    db.session.flush()
    _work_out(run)
    db.session.commit()
    audit_log('payroll_run_prepared', 'payroll', 'payroll_run', run.id, {'period': period, 'people': run.headcount})
    flash(f'Payroll for {first.strftime("%B %Y")} prepared: {run.headcount} payslips. Check them, then someone else approves it.', 'success')
    return redirect(url_for('admin_payroll_run', rid=run.id))


@app.route('/admin/payroll/runs/<int:rid>')
@admin_required
def admin_payroll_run(rid):
    me = current_admin()
    run = _run_or_404(rid)
    slips = db.session.scalars(select(Payslip).where(Payslip.run_id == rid).order_by(Payslip.name)).all()
    adjustments = db.session.scalars(select(PayrollAdjustment).where(PayrollAdjustment.run_id == rid).order_by(PayrollAdjustment.id)).all()
    by_staff = {}
    for adj in adjustments:
        by_staff.setdefault(adj.staff_id, []).append(adj)
    own = _own_record(me['id'])
    first, _ = _period_bounds(run.period)
    # Four eyes need something to look at: each person's net pay against their last approved month, so a raise nobody
    # agreed (a changed allowance, a new item) stands out before it is approved.
    before = db.session.scalars(select(PayrollRun).where(PayrollRun.status.in_(('approved', 'paid')), PayrollRun.period < run.period)
                                .order_by(PayrollRun.period.desc())).first()
    last_net = dict(db.session.execute(select(Payslip.staff_id, Payslip.net).where(Payslip.run_id == before.id)).all()) if before else {}
    totals = {'paye': sum(s.paye for s in slips), 'pension': sum(s.pension_employee + s.pension_employer for s in slips),
              'nhf': sum(s.nhf for s in slips)}
    return _logged(_private(make_response(render_template(
        'school_payroll_run.html', run=run, slips=slips, adjustments=by_staff, label=first.strftime('%B %Y'), pr=pr, totals=totals,
        last_net=last_net, last_label=_period_bounds(before.period)[0].strftime('%b %Y') if before else None,
        own_staff_id=own.id if own else None, names=_names({run.prepared_by, run.approved_by, run.paid_by, run.cancelled_by}),
        can_manage=_may('payroll.manage'), can_approve=_may('payroll.approve') and run.prepared_by != me['id'],
        can_export=_may('payroll.manage') or _may('payroll.approve'), can_reopen=_may('payroll.approve'),
        paystack=_paystack_state(slips),
        lines={s.id: json.loads(s.lines_json) for s in slips}))), 'payroll_run_opened', 'payroll_run', rid)


def _paystack_state(slips):
    """How paying through Paystack stands for a run: ``{'connected', 'can_pay' (the School Admin), 'can_check', 'counts'}``."""
    from core.payments import payment_settings
    from core.security import is_school_admin
    counts = {}
    for s in slips:
        if s.transfer_status:
            counts[s.transfer_status] = counts.get(s.transfer_status, 0) + 1
    me = current_admin()
    own_admin = is_school_admin() and not str(me['username'] or '').startswith('platform@')
    return {'connected': payment_settings() is not None, 'can_pay': own_admin, 'counts': counts,
            'can_check': bool(counts.get('pending')) and (is_school_admin() or _may('payroll.manage'))}



def _changed(run, me):
    """A draft was changed: whoever changed it is now its preparer, so they cannot approve it."""
    run.prepared_by, run.prepared_at = me['id'], _now()


@app.post('/admin/payroll/runs/<int:rid>/refresh')
@admin_required
@csrf_protect
def admin_payroll_run_refresh(rid):
    me = current_admin()
    run = _run_or_404(rid, lock=True)
    if run.status != 'draft':
        db.session.rollback()
        flash('Only a draft is worked out again. Reopen it first.', 'error')
        return redirect(url_for('admin_payroll_run', rid=rid))
    _work_out(run)
    _changed(run, me)
    db.session.commit()
    audit_log('payroll_run_refreshed', 'payroll', 'payroll_run', rid, {'people': run.headcount})
    flash('Payslips worked out again from everyone\'s pay as it is now.', 'success')
    return redirect(url_for('admin_payroll_run', rid=rid))


@app.post('/admin/payroll/runs/<int:rid>/adjust')
@admin_required
@csrf_protect
def admin_payroll_run_adjust(rid):
    me = current_admin()
    run = _run_or_404(rid, lock=True)
    back = redirect(url_for('admin_payroll_run', rid=rid) + '#slips')
    if run.status != 'draft':
        db.session.rollback()
        flash('An approved payroll cannot change. Reopen it first.', 'error')
        return back
    remove = request.form.get('remove', type=int)
    if remove:
        adj = db.session.get(PayrollAdjustment, remove)
        if adj is None or adj.run_id != rid:
            abort(404)
        if _is_own(me, adj.staff_id):
            db.session.rollback()
            flash('Someone else changes your own payslip.', 'error')
            return back
        db.session.delete(adj)
        what = {'removed': remove}
    else:
        staff_id = request.form.get('staff_id', type=int)
        kind = request.form.get('kind')
        label = hr.one_line(request.form.get('label'), 60)
        amount = pr.parse_naira(request.form.get('amount'))
        if not db.session.execute(select(Payslip.id).where(Payslip.run_id == rid, Payslip.staff_id == staff_id)).first():
            abort(400)
        if _is_own(me, staff_id):
            db.session.rollback()
            flash('Someone else changes your own payslip.', 'error')
            return back
        if kind not in ('earning', 'deduction') or not label or not amount:
            db.session.rollback()
            flash('Give the line a name, say whether it is paid or taken, and the amount.', 'error')
            return back
        db.session.add(PayrollAdjustment(run_id=rid, staff_id=staff_id, kind=kind, label=label, amount=amount,
                                         taxable=1 if request.form.get('taxable') else 0, created_at=_now(), created_by=me['id']))
        what = {'staff_id': staff_id, 'kind': kind}
    db.session.flush()
    _work_out(run)
    _changed(run, me)
    db.session.commit()
    audit_log('payroll_run_adjusted', 'payroll', 'payroll_run', rid, what)
    flash('Payslip changed.', 'success')
    return back


def _is_own(me, staff_id):
    own = _own_record(me['id'])
    return bool(own and own.id == staff_id)


@app.post('/admin/payroll/runs/<int:rid>/approve')
@admin_required
@csrf_protect
def admin_payroll_run_approve(rid):
    me = current_admin()
    run = _run_or_404(rid, lock=True)
    back = redirect(url_for('admin_payroll_run', rid=rid))
    if run.status != 'draft':
        db.session.rollback()
        flash('That payroll is not a draft waiting for approval.', 'error')
        return back
    if run.prepared_by == me['id']:
        db.session.rollback()
        flash('You prepared or last changed this payroll, so someone else approves it.', 'error')
        return back
    if not run.headcount:
        db.session.rollback()
        flash('There is nobody on this payroll.', 'error')
        return back
    if db.session.execute(select(Payslip.id).where(Payslip.run_id == rid, Payslip.net < 0)).first():
        db.session.rollback()
        flash('A payslip takes more than it pays. Put it right before approving.', 'error')
        return back
    run.status, run.approved_by, run.approved_at = 'approved', me['id'], _now()
    db.session.commit()
    audit_log('payroll_run_approved', 'payroll', 'payroll_run', rid, {'period': run.period, 'net': run.net, 'people': run.headcount})
    flash('Payroll approved and locked. Staff can now see their payslips; download the bank schedule to pay them.', 'success')
    return back


@app.post('/admin/payroll/runs/<int:rid>/reopen')
@admin_required
@csrf_protect
def admin_payroll_run_reopen(rid):
    me = current_admin()
    run = _run_or_404(rid, lock=True)
    if run.status != 'approved':
        db.session.rollback()
        flash('Only an approved payroll that is not yet paid can be reopened.', 'error')
        return redirect(url_for('admin_payroll_run', rid=rid))
    if db.session.execute(select(Payslip.id).where(Payslip.run_id == rid, Payslip.transfer_status.in_(('pending', 'success')))).first():
        db.session.rollback()
        flash('Salaries from this payroll have been sent through Paystack, so it cannot be reopened. Put a mistake right '
              'in next month\'s payroll.', 'error')
        return redirect(url_for('admin_payroll_run', rid=rid))
    run.status, run.approved_by, run.approved_at = 'draft', None, None
    _changed(run, me)
    db.session.commit()
    audit_log('payroll_run_reopened', 'payroll', 'payroll_run', rid, {'period': run.period})
    flash('Payroll reopened as a draft. It needs approving again, by someone other than you.', 'success')
    return redirect(url_for('admin_payroll_run', rid=rid))


@app.post('/admin/payroll/runs/<int:rid>/paid')
@admin_required
@csrf_protect
def admin_payroll_run_paid(rid):
    me = current_admin()
    run = _run_or_404(rid, lock=True)
    if run.status != 'approved':
        db.session.rollback()
        flash('Only an approved payroll can be marked as paid.', 'error')
        return redirect(url_for('admin_payroll_run', rid=rid))
    if db.session.execute(select(Payslip.id).where(Payslip.run_id == rid, Payslip.transfer_status == 'pending')).first():
        db.session.rollback()
        flash('Some salaries sent through Paystack are still being paid. Check the transfers first.', 'error')
        return redirect(url_for('admin_payroll_run', rid=rid))
    run.status, run.paid_by, run.paid_at = 'paid', me['id'], _now()
    db.session.commit()
    audit_log('payroll_run_paid', 'payroll', 'payroll_run', rid, {'period': run.period, 'net': run.net})
    flash('Payroll marked as paid. It is final.', 'success')
    return redirect(url_for('admin_payroll_run', rid=rid))


@app.post('/admin/payroll/runs/<int:rid>/cancel')
@admin_required
@csrf_protect
def admin_payroll_run_cancel(rid):
    me = current_admin()
    run = _run_or_404(rid, lock=True)
    if run.status != 'draft':
        db.session.rollback()
        flash('Only a draft can be cancelled. Reopen an approved payroll first.', 'error')
        return redirect(url_for('admin_payroll_run', rid=rid))
    run.status, run.cancelled_by, run.cancelled_at = 'cancelled', me['id'], _now()
    db.session.commit()
    audit_log('payroll_run_cancelled', 'payroll', 'payroll_run', rid, {'period': run.period})
    flash('Draft payroll cancelled. A new one can be prepared for that month.', 'success')
    return redirect(url_for('admin_payroll'))


# ---------------------------------------------------------------- exports (approved runs only)
def _export_allowed(run):
    if not (_may('payroll.manage') or _may('payroll.approve')):
        return admin_access_error('payroll.approve')
    if run.status not in ('approved', 'paid'):
        flash('The bank schedule and the returns are ready once the payroll is approved.', 'error')
        return redirect(url_for('admin_payroll_run', rid=run.id))
    return None


def _csv(rows, filename):
    out = io.StringIO()
    writer = csv.writer(out)
    for row in rows:
        writer.writerow([csv_safe(c) if isinstance(c, str) else c for c in row])
    return _private(Response(out.getvalue(), mimetype='text/csv', headers={'Content-Disposition': f'attachment; filename={filename}'}))


@app.route('/admin/payroll/runs/<int:rid>/bank.csv')
@admin_required
def admin_payroll_run_bank(rid):
    run = _run_or_404(rid)
    refused = _export_allowed(run)
    if refused:
        return refused
    slips = db.session.scalars(select(Payslip).where(Payslip.run_id == rid).order_by(Payslip.name)).all()
    pays = {p.staff_id: p for p in db.session.scalars(select(StaffPay).where(StaffPay.staff_id.in_([s.staff_id for s in slips] or [0]))).all()}
    rows = [['Name', 'Staff number', 'Bank', 'Account number', 'Account name', 'Amount (NGN)', 'Narration']]
    first, _ = _period_bounds(run.period)
    for s in slips:
        if s.net <= 0:
            continue
        pay = pays.get(s.staff_id)
        rows.append([s.name, s.staff_no or '', s.bank_name or '', pr.decrypt(pay.account_number_enc) if pay else '', s.account_name or '',
                     pr.naira(s.net, sign=False).replace(',', ''), f'Salary {first.strftime("%b %Y")}'])
    audit_log('payroll_bank_schedule_downloaded', 'payroll', 'payroll_run', rid, {'rows': len(rows) - 1})
    return _csv(rows, f'salaries-{run.period}-bank.csv')


@app.route('/admin/payroll/runs/<int:rid>/returns.csv')
@admin_required
def admin_payroll_run_returns(rid):
    run = _run_or_404(rid)
    refused = _export_allowed(run)
    if refused:
        return refused
    slips = db.session.scalars(select(Payslip).where(Payslip.run_id == rid).order_by(Payslip.name)).all()
    pays = {p.staff_id: p for p in db.session.scalars(select(StaffPay).where(StaffPay.staff_id.in_([s.staff_id for s in slips] or [0]))).all()}
    rows = [['Name', 'Staff number', 'Tax ID', 'Gross (NGN)', 'PAYE (NGN)', 'Pension administrator', 'RSA PIN',
             'Pension, employee (NGN)', 'Pension, employer (NGN)', 'NHF (NGN)']]
    for s in slips:
        pay = pays.get(s.staff_id)
        rows.append([s.name, s.staff_no or '', pr.decrypt(pay.tax_id_enc) if pay else '', pr.naira(s.gross, False).replace(',', ''),
                     pr.naira(s.paye, False).replace(',', ''), s.pfa or '', pr.decrypt(pay.rsa_pin_enc) if pay else '',
                     pr.naira(s.pension_employee, False).replace(',', ''), pr.naira(s.pension_employer, False).replace(',', ''),
                     pr.naira(s.nhf, False).replace(',', '')])
    audit_log('payroll_returns_downloaded', 'payroll', 'payroll_run', rid, {'rows': len(rows) - 1})
    return _csv(rows, f'salaries-{run.period}-paye-pension-nhf.csv')


# ---------------------------------------------------------------- payslips
@app.route('/admin/payroll/payslips/<int:pid>')
@admin_required
def admin_payroll_payslip(pid):
    me = current_admin()
    slip = db.session.get(Payslip, pid)
    if slip is None:
        abort(404)
    run = db.session.get(PayrollRun, slip.run_id)
    own = _is_own(me, slip.staff_id) and run.status in ('approved', 'paid')
    if not (own or _may('payroll.view')):
        return admin_access_error('payroll.view')
    first, _ = _period_bounds(run.period)
    lines = json.loads(slip.lines_json)
    return _logged(_private(make_response(render_template(
        'payroll_payslip.html', slip=slip, run=run, label=first.strftime('%B %Y'), pr=pr,
        earnings=[l for l in lines if l['kind'] == 'earning'], deductions=[l for l in lines if l['kind'] == 'deduction']))),
        'payslip_opened', 'payslip', pid, {'own': own})


@app.route('/admin/my-payslips')
@admin_required
def admin_my_payslips():
    me = current_admin()
    own = _own_record(me['id'])
    slips = []
    if own:
        slips = db.session.execute(select(Payslip, PayrollRun).join(PayrollRun, PayrollRun.id == Payslip.run_id)
                                   .where(Payslip.staff_id == own.id, PayrollRun.status.in_(('approved', 'paid')))
                                   .order_by(PayrollRun.period.desc())).all()
    return _private(make_response(render_template('school_my_payslips.html', record=own, slips=slips, pr=pr,
                                                  label=lambda p: _period_bounds(p)[0].strftime('%B %Y'))))


# ---------------------------------------------------------------- people's pay
@app.route('/admin/payroll/staff')
@admin_required
def admin_payroll_staff():
    records = db.session.scalars(select(StaffRecord).where(StaffRecord.status != 'left').order_by(StaffRecord.last_name, StaffRecord.first_name)).all()
    pays = {p.staff_id: p for p in db.session.scalars(select(StaffPay)).all()}
    counts = {}
    for row in db.session.scalars(select(StaffPayItem)).all():
        counts[row.staff_id] = counts.get(row.staff_id, 0) + 1
    return _logged(_private(make_response(render_template('school_payroll_staff.html', records=records, pays=pays, counts=counts, pr=pr,
                                                          hr=hr, can_manage=_may('payroll.manage')))), 'payroll_staff_opened', 'payroll_staff', None)


@app.route('/admin/payroll/staff/<int:sid>', methods=['GET', 'POST'])
@admin_required
@csrf_protect
def admin_payroll_staff_edit(sid):
    me = current_admin()
    person = db.session.get(StaffRecord, sid)
    if person is None:
        abort(404)
    pay = db.session.scalars(select(StaffPay).where(StaffPay.staff_id == sid)).first()
    items = _items()
    given = {row.item_id: row for row in db.session.scalars(select(StaffPayItem).where(StaffPayItem.staff_id == sid)).all()}
    if request.method == 'POST':
        if not _may('payroll.manage'):
            return admin_access_error('payroll.manage')
        if _is_own(me, sid):
            flash('Someone else sets your own pay.', 'error')
            return redirect(url_for('admin_payroll_staff_edit', sid=sid))
        errors = []
        basic = pr.parse_naira(request.form.get('basic'))
        if basic is None:
            errors.append('The basic salary must be an amount in naira, such as 150000.')
        rent = pr.parse_naira(request.form.get('annual_rent') or '0')
        if rent is None:
            errors.append('The yearly rent must be an amount in naira, or empty.')
        account_raw = str(request.form.get('account_number', '')).strip()
        account = pr.clean_account_number(account_raw) if account_raw else None
        if account_raw and not account:
            errors.append('A bank account number is ten digits.')
        bank = request.form.get('bank_name', '')
        if bank and bank not in pr.NIGERIAN_BANKS:
            errors.append('Choose the bank from the list.')
        changes = []
        for item in items:
            if not request.form.get(f'item_{item.id}'):
                continue
            raw = str(request.form.get(f'item_amount_{item.id}', '')).strip()
            if not raw:
                changes.append((item, None, None))
            elif item.calc == 'percent_basic':
                rate = pr.parse_percent(raw)
                if rate is None:
                    errors.append(f'{item.name}: a percentage of basic, such as 20.')
                changes.append((item, None, rate))
            else:
                amount = pr.parse_naira(raw)
                if amount is None:
                    errors.append(f'{item.name}: an amount in naira.')
                changes.append((item, amount, None))
        if errors:
            for e in errors:
                flash(e, 'error')
            return redirect(url_for('admin_payroll_staff_edit', sid=sid))
        if pay is None:
            pay = StaffPay(staff_id=sid)
            db.session.add(pay)
        before = {'basic': pay.basic, 'on_payroll': pay.on_payroll, 'bank': pay.account_last4}
        pay.on_payroll = 1 if request.form.get('on_payroll') else 0
        pay.basic, pay.annual_rent = basic, rent
        pay.bank_name = bank or None
        pay.account_name = hr.one_line(request.form.get('account_name'), 80) or None
        if account:
            pay.account_number_enc, pay.account_last4 = pr.encrypt(account), account[-4:]
        elif request.form.get('clear_account'):
            pay.account_number_enc = pay.account_last4 = None
        pay.pfa = hr.one_line(request.form.get('pfa'), 80) or None
        for field, name in (('rsa_pin_enc', 'rsa_pin'), ('tax_id_enc', 'tax_id')):
            raw = hr.one_line(request.form.get(name), 30)
            if raw:
                setattr(pay, field, pr.encrypt(raw))
            elif request.form.get(f'clear_{name}'):
                setattr(pay, field, None)
        for flag in ('paye_exempt', 'pension_exempt', 'nhf_exempt'):
            setattr(pay, flag, 1 if request.form.get(flag) else 0)
        pay.updated_at, pay.updated_by = _now(), me['id']
        keep = set()
        for item, amount, rate in changes:
            row = given.get(item.id) or StaffPayItem(staff_id=sid, item_id=item.id)
            row.amount, row.rate_bp = amount, rate
            db.session.add(row)
            keep.add(item.id)
        for item_id, row in given.items():
            if item_id not in keep:
                db.session.delete(row)
        db.session.commit()
        # What changed, never the amounts or the numbers themselves.
        audit_log('payroll_pay_saved', 'payroll', 'hr_staff', sid,
                  {'basic_changed': before['basic'] != pay.basic, 'bank_changed': before['bank'] != pay.account_last4,
                   'on_payroll': pay.on_payroll, 'items': len(keep)})
        flash(f'Pay saved for {hr.full_name(person, False)}.', 'success')
        return redirect(url_for('admin_payroll_staff'))
    settings = _settings()
    preview = None
    if pay:
        preview = pr.payslip(pay, [(item, given[item.id]) for item in items if item.id in given], [], settings)
    response = _private(make_response(render_template(
        'school_payroll_staff_edit.html', person=person, pay=pay, items=items, given=given, preview=preview, pr=pr, hr=hr,
        own=_is_own(me, sid), can_manage=_may('payroll.manage'), has_pin=bool(pay and pay.rsa_pin_enc), has_tin=bool(pay and pay.tax_id_enc))))
    db.session.commit()
    return _logged(response, 'payroll_pay_opened', 'hr_staff', sid)


# ---------------------------------------------------------------- settings
@app.route('/admin/payroll/settings', methods=['GET', 'POST'])
@admin_required
@csrf_protect
def admin_payroll_settings():
    me = current_admin()
    settings = _settings()
    if request.method == 'POST':
        errors = []
        bands, error = pr.parse_bands(request.form)
        if error:
            errors.append(error)
        numbers = {}
        for name, label in (('pension_employee_bp', 'Pension from the employee'), ('pension_employer_bp', 'Pension from the school'),
                            ('nhf_bp', 'NHF'), ('rent_relief_bp', 'Rent relief')):
            value = pr.parse_percent(request.form.get(name))
            if value is None:
                errors.append(f'{label} must be a percentage.')
            numbers[name] = value
        cap = pr.parse_naira(request.form.get('rent_relief_cap'))
        if cap is None:
            errors.append('The most rent relief a year must be an amount in naira.')
        checked_by = hr.one_line(request.form.get('checked_by'), 120)
        if errors:
            db.session.rollback()
            for e in errors:
                flash(e, 'error')
            return redirect(url_for('admin_payroll_settings'))
        settings.paye_bands_json = json.dumps(bands)
        for name, value in numbers.items():
            setattr(settings, name, value)
        settings.rent_relief_cap = cap
        for flag in ('paye_enabled', 'pension_enabled', 'nhf_enabled', 'deduct_unpaid_leave'):
            setattr(settings, flag, 1 if request.form.get(flag) else 0)
        if checked_by != (settings.checked_by or ''):
            settings.checked_by, settings.checked_on = checked_by or None, date.today().isoformat() if checked_by else None
        settings.updated_at, settings.updated_by = _now(), me['id']
        db.session.commit()
        audit_log('payroll_settings_saved', 'payroll', 'settings', None, {'snapshot': json.loads(pr.settings_snapshot(settings))})
        flash('Payroll settings saved. Draft payrolls use them once worked out again.', 'success')
        return redirect(url_for('admin_payroll_settings'))
    db.session.commit()
    return _private(make_response(render_template('school_payroll_settings.html', settings=settings, bands=pr.bands_of(settings),
                                                  items=_items(), pr=pr)))


@app.post('/admin/payroll/items')
@admin_required
@csrf_protect
def admin_payroll_items():
    """Change the pay items, and add one."""
    back = redirect(url_for('admin_payroll_settings') + '#items')
    for item in _items():
        if f'name_{item.id}' not in request.form:
            continue
        name = hr.one_line(request.form.get(f'name_{item.id}'), 60)
        value = str(request.form.get(f'value_{item.id}', '')).strip() or '0'
        amount = pr.parse_percent(value) if item.calc == 'percent_basic' else pr.parse_naira(value)
        if not name or amount is None:
            db.session.rollback()
            flash(f'{item.name}: give it a name and an amount.', 'error')
            return back
        if name != item.name and db.session.execute(select(PayItem.id).where(func.lower(PayItem.name) == name.lower(), PayItem.id != item.id)).first():
            db.session.rollback()
            flash(f'There is already an item called {name}.', 'error')
            return back
        item.name = name
        if item.calc == 'percent_basic':
            item.rate_bp = amount
        else:
            item.amount = amount
        item.taxable = 1 if request.form.get(f'taxable_{item.id}') else 0
        item.pensionable = 1 if request.form.get(f'pensionable_{item.id}') and item.kind == 'earning' else 0
        item.active = 1 if request.form.get(f'active_{item.id}') else 0
    name = hr.one_line(request.form.get('new_name'), 60)
    if name:
        kind, calc = request.form.get('new_kind'), request.form.get('new_calc')
        value = str(request.form.get('new_value', '')).strip() or '0'
        amount = pr.parse_percent(value) if calc == 'percent_basic' else pr.parse_naira(value)
        if kind not in ('earning', 'deduction') or calc not in ('fixed', 'percent_basic') or amount is None:
            db.session.rollback()
            flash('The new item needs to be paid or taken, a fixed amount or a percentage of basic, and its amount.', 'error')
            return back
        if db.session.execute(select(PayItem.id).where(func.lower(PayItem.name) == name.lower())).first():
            db.session.rollback()
            flash(f'There is already an item called {name}.', 'error')
            return back
        top = db.session.execute(select(func.max(PayItem.sort_order))).scalar() or 0
        db.session.add(PayItem(name=name, kind=kind, calc=calc, amount=amount if calc == 'fixed' else 0,
                               rate_bp=amount if calc == 'percent_basic' else 0, taxable=1 if kind == 'earning' else 0,
                               pensionable=1 if request.form.get('new_pensionable') and kind == 'earning' else 0, active=1,
                               sort_order=top + 1, created_at=_now()))
    db.session.commit()
    audit_log('payroll_items_saved', 'payroll', 'items', None, {'added': bool(name)})
    flash('Pay items saved.', 'success')
    return back
