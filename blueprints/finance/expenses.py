"""Finance > Expenses and Finance > Profit & loss.

Expenses (finance.expenses): money the school spent, other than staff pay, each with the date, what it was for (a fixed
list of categories, so the profit and loss can add them up), who was paid, how, and a photograph of the receipt if
there is one. An expense is never deleted: a mistake is voided with a reason and stays on the record, like a payment.
Receipt photographs are kept in the 'expenses' uploads folder, which the public upload route never serves
(core/upload_access.py); only ``admin_finance_expense_receipt`` does, to someone who may see expenses.

Profit & loss (finance.profit_loss, the School Admin's by default): for a month, a session or any dates, what came in
and what went out, counted when the money moved (cash basis):

* in: fee payments received (by the fee they were applied to, and what is not yet applied), and store sales;
* out: staff pay from every payroll marked paid in the period (gross pay plus the school's own pension contribution),
  and every expense not voided, by category.

Fees charged but not yet paid are not income until they are paid; the page shows them apart, as what is still owed.
"""

import calendar
import re
from datetime import date, datetime, timedelta, timezone

from flask import Response, abort, flash, redirect, render_template, request, url_for
from sqlalchemy import func, select

from app import _active_sessions, _school_current_session, app
from blueprints.finance.helpers import _money
from core.db_helpers import obj, one_scalar, tuples
from core.idempotency import idempotent_write
from core.security import admin_required, audit_log, csrf_protect, current_admin
from core.storage import read_upload_bytes
from core.uploads import _save_image_upload
from models import (
    AcademicSession, Admin, FinanceExpense, FinanceFeeAssessment, FinancePayment, FinancePaymentAllocation, PayrollRun,
    StorePurchase, db,
)

CATEGORIES = ['Maintenance & repairs', 'Power & fuel (NEPA, diesel)', 'Water & utilities', 'Teaching materials',
              'Books & stationery', 'Printing & photocopying', 'Food & kitchen', 'Transport & vehicles',
              'Rent & premises', 'Cleaning & sanitation', 'Security', 'Internet & phones', 'Events & excursions',
              'Exams & registration fees', 'Bank charges', 'Taxes & levies', 'Professional fees', 'Other']
METHODS = ['Cash', 'Bank Transfer', 'POS', 'Cheque', 'Other']


def _naira(v):
    return f'₦{float(v or 0):,.2f}'


def _r(v):
    return round(float(v or 0), 2)


def _month_bounds(month):
    """('YYYY-MM-01', 'YYYY-MM-last') for 'YYYY-MM', or None."""
    if not re.fullmatch(r'\d{4}-\d{2}', month or ''):
        return None
    y, m = int(month[:4]), int(month[5:])
    if not 1 <= m <= 12:
        return None
    return f'{y:04d}-{m:02d}-01', f'{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}'


def _shift_month(month, by):
    y, m = int(month[:4]), int(month[5:]) + by
    while m < 1:
        y, m = y - 1, m + 12
    while m > 12:
        y, m = y + 1, m - 12
    return f'{y:04d}-{m:02d}'


# ---------------------------------------------------------------- Expenses
@app.route('/admin/finance/expenses')
@admin_required
def admin_finance_expenses():
    month = request.args.get('month') or date.today().strftime('%Y-%m')
    bounds = _month_bounds(month) or _month_bounds(date.today().strftime('%Y-%m'))
    month = bounds[0][:7]
    category = request.args.get('category', '')
    category = category if category in CATEGORIES else ''
    show = 'voided' if request.args.get('show') == 'voided' else 'posted'
    q = request.args.get('q', '').strip()
    in_month = [FinanceExpense.spent_on >= bounds[0], FinanceExpense.spent_on <= bounds[1]]
    names = dict(tuples(select(Admin.id, Admin.display_name)))
    rows = db.session.scalars(select(FinanceExpense).where(*in_month, FinanceExpense.status == show)
                              .order_by(FinanceExpense.spent_on.desc(), FinanceExpense.id.desc())).all()
    by_cat = {}
    for e in rows:
        if e.status == 'posted':
            by_cat[e.category] = _r(by_cat.get(e.category, 0) + e.amount)
    shown = [e for e in rows if not category or e.category == category]
    if q:
        needle = q.lower()
        shown = [e for e in shown if needle in f'{e.paid_to or ""} {e.description or ""} {e.reference or ""}'.lower()]
    total = _r(sum(e.amount for e in rows)) if show == 'posted' else 0.0
    prev = _month_bounds(_shift_month(month, -1))
    prev_total = _r(one_scalar(select(func.coalesce(func.sum(FinanceExpense.amount), 0)).where(
        FinanceExpense.status == 'posted', FinanceExpense.spent_on >= prev[0], FinanceExpense.spent_on <= prev[1]), 0))
    voided = one_scalar(select(func.count()).select_from(FinanceExpense).where(*in_month, FinanceExpense.status == 'voided'), 0)
    return render_template('finance_expenses.html', rows=shown, month=month, month_label=date.fromisoformat(bounds[0]).strftime('%B %Y'),
                           prev_month=_shift_month(month, -1), next_month=_shift_month(month, 1),
                           this_month=date.today().strftime('%Y-%m'), category=category, categories=CATEGORIES,
                           by_cat=sorted(by_cat.items(), key=lambda kv: -kv[1]), total=total, prev_total=prev_total,
                           count=len(rows), show=show, voided=voided, q=q, names=names,
                           shown_total=_r(sum(e.amount for e in shown)))


@app.route('/admin/finance/expenses/new', methods=['GET', 'POST'])
@admin_required
@csrf_protect
@idempotent_write('finance.record_expense')
def admin_finance_expense_new():
    me = current_admin()
    errors, form = [], request.form if request.method == 'POST' else {}
    if request.method == 'POST':
        amount = _money(form.get('amount', ''))
        category = form.get('category', '')
        spent_on = form.get('spent_on', '').strip() or date.today().isoformat()
        paid_to = ' '.join(form.get('paid_to', '').split())[:120]
        description = ' '.join(form.get('description', '').split())[:300]
        method = form.get('method', 'Cash')
        reference = ' '.join(form.get('reference', '').split())[:80]
        try:
            when = date.fromisoformat(spent_on)
            if when > date.today() + timedelta(days=1):
                errors.append('An expense cannot be dated in the future.')
        except ValueError:
            errors.append('Enter the date the money was spent.')
        if amount is None or amount <= 0:
            errors.append('Enter the amount spent, as a number of naira, for example 45000.')
        if category not in CATEGORIES:
            errors.append('Choose what it was for.')
        if not description and not paid_to:
            errors.append('Say what was bought or who was paid, so it can be recognised later.')
        if method not in METHODS:
            method = 'Other'
        receipt = None
        if not errors and request.files.get('receipt') and request.files['receipt'].filename:
            try:
                receipt = _save_image_upload(request.files['receipt'], 'expenses', 'receipt')
            except ValueError as exc:
                errors.append(str(exc))
        if not errors:
            e = FinanceExpense(spent_on=spent_on, category=category, amount=amount, paid_to=paid_to or None,
                               description=description or None, method=method, reference=reference or None,
                               receipt_path=receipt, status='posted', recorded_by=me['id'],
                               created_at=datetime.now(timezone.utc).isoformat())
            db.session.add(e)
            db.session.flush()
            audit_log('finance_expense_recorded', 'finance', 'expense', e.id,
                      {'amount': amount, 'category': category, 'spent_on': spent_on}, commit=False)
            db.session.commit()
            flash(f'{_naira(amount)} recorded under {category}.', 'success')
            return redirect(url_for('admin_finance_expenses', month=spent_on[:7]))
    return render_template('finance_expense_form.html', errors=errors, form=form, categories=CATEGORIES, methods=METHODS,
                           today=date.today().isoformat())


@app.post('/admin/finance/expenses/<int:expense_id>/void')
@admin_required
@csrf_protect
def admin_finance_expense_void(expense_id):
    me = current_admin()
    e = obj(FinanceExpense, expense_id)
    if not e:
        abort(404)
    reason = ' '.join(request.form.get('reason', '').split())[:300]
    if e.status != 'posted':
        flash('That expense is already voided.', 'error')
    elif not reason:
        flash('Give a reason for voiding the expense.', 'error')
    else:
        e.status, e.voided_at, e.voided_by, e.void_reason = 'voided', datetime.now(timezone.utc).isoformat(), me['id'], reason
        audit_log('finance_expense_voided', 'finance', 'expense', e.id, {'amount': e.amount, 'reason': reason}, commit=False)
        db.session.commit()
        flash('Expense voided. It stays on the record, marked voided, and no longer counts.', 'success')
    return redirect(url_for('admin_finance_expenses', month=e.spent_on[:7]))


@app.get('/admin/finance/expenses/<int:expense_id>/receipt')
@admin_required
def admin_finance_expense_receipt(expense_id):
    e = obj(FinanceExpense, expense_id)
    if not e or not e.receipt_path:
        abort(404)
    data = read_upload_bytes(e.receipt_path)
    if data is None:
        abort(404)
    ext = e.receipt_path.rsplit('.', 1)[-1].lower()
    mimetype = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp', 'gif': 'image/gif'}.get(ext, 'application/octet-stream')
    return Response(data, mimetype=mimetype, headers={'Cache-Control': 'private, no-store',
                                                      'X-Content-Type-Options': 'nosniff'})


# ---------------------------------------------------------------- Profit & loss
def _months(start, end):
    out, (y, m) = [], (int(start[:4]), int(start[5:7]))
    while f'{y:04d}-{m:02d}' <= end[:7]:
        out.append(f'{y:04d}-{m:02d}')
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def profit_and_loss(start, end):
    """Money in and out from ``start`` to ``end`` (ISO dates, both included), on a cash basis.

    Every time is stored as ISO text beginning with its date, so "on or after the start, before the day after the end"
    is a plain range on the column itself, which the (status, time) indexes answer; the month-by-month table only
    groups the rows already found."""
    after = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
    paid_in = [FinancePayment.status == 'posted', FinancePayment.paid_at >= start, FinancePayment.paid_at < after]
    received = _r(one_scalar(select(func.coalesce(func.sum(FinancePayment.amount), 0)).where(*paid_in), 0))
    fees = [{'label': cat or 'Other', 'value': _r(total)} for cat, total in tuples(
        select(FinanceFeeAssessment.category, func.sum(FinancePaymentAllocation.amount))
        .join(FinancePayment, FinancePayment.id == FinancePaymentAllocation.payment_id)
        .join(FinanceFeeAssessment, FinanceFeeAssessment.id == FinancePaymentAllocation.assessment_id)
        .where(*paid_in, FinancePaymentAllocation.voided_at.is_(None))
        .group_by(FinanceFeeAssessment.category).order_by(func.sum(FinancePaymentAllocation.amount).desc()))]
    unapplied = _r(received - sum(f['value'] for f in fees))
    if unapplied > 0.004:
        fees.append({'label': 'Received, not yet applied to a fee', 'value': unapplied})
    store_in = [StorePurchase.status.in_(('paid', 'claimed')), StorePurchase.created_at >= start,
                StorePurchase.created_at < after]
    store = _r(one_scalar(select(func.coalesce(func.sum(StorePurchase.amount), 0)).where(*store_in), 0))
    runs_in = [PayrollRun.status == 'paid', PayrollRun.paid_at >= start, PayrollRun.paid_at < after]
    gross, pension, runs = db.session.execute(select(
        func.coalesce(func.sum(PayrollRun.gross), 0), func.coalesce(func.sum(PayrollRun.employer_pension), 0), func.count())
        .where(*runs_in)).one()
    expenses = [{'label': cat, 'value': _r(total)} for cat, total in tuples(
        select(FinanceExpense.category, func.sum(FinanceExpense.amount))
        .where(FinanceExpense.status == 'posted', FinanceExpense.spent_on >= start, FinanceExpense.spent_on <= end)
        .group_by(FinanceExpense.category).order_by(func.sum(FinanceExpense.amount).desc()))]
    payroll = [{'label': 'Staff pay (gross)', 'value': _r(gross / 100)}]
    if pension:
        payroll.append({'label': "Pension, the school's part", 'value': _r(pension / 100)})
    income = _r(received + store)
    spending = _r(sum(x['value'] for x in payroll) + sum(x['value'] for x in expenses))

    # Month by month, when the period is longer than one month.
    months = _months(start, end)
    by_month = []
    if len(months) > 1:
        def per_month(stmt):
            return {m: _r(v) for m, v in tuples(stmt)}
        m_pay = func.substr(FinancePayment.paid_at, 1, 7)
        fee_m = per_month(select(m_pay, func.sum(FinancePayment.amount)).where(*paid_in).group_by(m_pay))
        m_store = func.substr(StorePurchase.created_at, 1, 7)
        store_m = per_month(select(m_store, func.sum(StorePurchase.amount)).where(*store_in).group_by(m_store))
        m_run = func.substr(PayrollRun.paid_at, 1, 7)
        pay_m = per_month(select(m_run, func.sum(PayrollRun.gross + PayrollRun.employer_pension) / 100.0)
                          .where(*runs_in).group_by(m_run))
        m_exp = func.substr(FinanceExpense.spent_on, 1, 7)
        exp_m = per_month(select(m_exp, func.sum(FinanceExpense.amount)).where(
            FinanceExpense.status == 'posted', FinanceExpense.spent_on >= start, FinanceExpense.spent_on <= end)
            .group_by(m_exp))
        for m in months:
            i = _r(fee_m.get(m, 0) + store_m.get(m, 0)); o = _r(pay_m.get(m, 0) + exp_m.get(m, 0))
            by_month.append({'month': m, 'label': date(int(m[:4]), int(m[5:]), 1).strftime('%b %Y'),
                             'income': i, 'spending': o, 'net': _r(i - o)})
    return {'start': start, 'end': end, 'fees': fees, 'fees_total': received, 'store': store, 'income': income,
            'payroll': payroll, 'payroll_total': _r(sum(x['value'] for x in payroll)), 'runs': int(runs or 0),
            'expenses': expenses, 'expenses_total': _r(sum(x['value'] for x in expenses)),
            'spending': spending, 'net': _r(income - spending), 'by_month': by_month,
            'max_month': max([max(r['income'], r['spending']) for r in by_month] + [0])}


@app.route('/admin/finance/profit-loss')
@admin_required
def admin_finance_profit_loss():
    period = request.args.get('period', 'month')
    period = period if period in ('month', 'session', 'range') else 'month'
    sessions = [s for s in _active_sessions() if s.start_date and s.end_date]
    month = request.args.get('month') or date.today().strftime('%Y-%m')
    error, start, end, label = None, None, None, ''
    if period == 'session':
        wanted = request.args.get('session_id', type=int)
        current = _school_current_session()
        chosen = next((s for s in sessions if s.id == wanted), None) or next(
            (s for s in sessions if current and s.id == current['id']), None) or (sessions[0] if sessions else None)
        if chosen:
            start, end, label = str(chosen.start_date)[:10], str(chosen.end_date)[:10], f'{chosen.name} session'
        else:
            error = 'No session has its start and end dates set. Set them under Settings, Academic sessions.'
    elif period == 'range':
        try:
            start = date.fromisoformat(request.args.get('from', '')).isoformat()
            end = date.fromisoformat(request.args.get('to', '')).isoformat()
            if end < start:
                start, end = end, start
            label = f"{date.fromisoformat(start).strftime('%d %b %Y')} to {date.fromisoformat(end).strftime('%d %b %Y')}"
        except ValueError:
            error = 'Choose the first and last day.'
            start = end = None
    if period == 'month' or (period != 'month' and start is None and not error):
        bounds = _month_bounds(month) or _month_bounds(date.today().strftime('%Y-%m'))
        start, end, month = bounds[0], bounds[1], bounds[0][:7]
        label = date.fromisoformat(start).strftime('%B %Y')
    report = profit_and_loss(start, end) if start and not error else None
    from blueprints.finance.helpers import finance_snapshot
    owed = finance_snapshot(current_admin())['outstanding'] if report else None
    return render_template('finance_profit_loss.html', report=report, period=period, sessions=sessions, month=month,
                           prev_month=_shift_month(month, -1), next_month=_shift_month(month, 1),
                           chosen_session=request.args.get('session_id', type=int), label=label, error=error,
                           range_from=request.args.get('from', ''), range_to=request.args.get('to', ''), owed=owed)
