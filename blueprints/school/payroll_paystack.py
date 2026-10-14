"""Paying an approved payroll through the school's own Paystack account. The rules are core/payroll_transfers.py.

Who. Only the School Admin (the proprietor or proprietress) sees the review and sends the money: not the Payroll
Officer, not the bursar, whatever permission they hold. Anyone who may see the payroll sees how each transfer went on
the run's own page, and someone who may manage or approve payroll can ask Paystack about the ones still pending.

How.
1. The review (GET): each payslip to pay, the bank's own name for the account, whether it matches, the total and the
   school's Paystack balance. Someone whose bank details changed since approval, or whose account cannot be found, is
   not offered. A name that does not match is offered unticked.
2. Sending (POST) needs the proprietor's own password again. Everything is checked again on the server (the run is
   approved, the account is the approved one, the bank still knows it, the balance covers the total); nothing from
   the review page is trusted but which payslips were ticked. Each payslip is marked pending with its own reference
   before anything is sent, under a lock on the run, so two clicks never pay anyone twice.
3. Paystack's signed webhook (blueprints/finance/paystack.py) and the Check transfers button settle each one by asking
   Paystack. When everyone with pay to receive has been paid, the run is marked paid.
Every step is in the activity log, without account numbers.
"""

from datetime import datetime, timezone

from flask import abort, current_app, flash, make_response, redirect, render_template, request, url_for
from sqlalchemy import select
from werkzeug.security import check_password_hash

from app import app
from core import payments
from core import payroll as pr
from core import payroll_transfers as pt
from control_plane.ratelimit import allow as _count_try, reached as _too_many
from core.accounts import _rate_limit
from core.security import admin_access_error, admin_has_permission, admin_required, audit_log, csrf_protect, current_admin, is_school_admin
from models import Admin, PayrollRun, Payslip, StaffPay, db


def _now():
    return datetime.now(timezone.utc).isoformat()


def _run(rid, lock=False):
    stmt = select(PayrollRun).where(PayrollRun.id == rid)
    if lock:
        stmt = stmt.with_for_update()
    run = db.session.scalars(stmt).first()
    if run is None or run.status == 'cancelled':
        abort(404)
    return run


def _back(rid):
    return redirect(url_for('admin_payroll_run', rid=rid))


def _proprietor_only():
    """The school's own School Admin, and nobody else: not a Payroll Officer or a bursar, and not a platform operator
    visiting the school as its School Admin (they never move a school's money)."""
    me = current_admin()
    if not is_school_admin() or str(me['username'] or '').startswith('platform@'):
        audit_log('authorization_denied', 'payroll', 'payroll_transfers', None, {'reason': 'only the School Admin pays'}, False, me)
        return admin_access_error("paying salaries (the school's own School Admin only)")
    return None


def _candidates(run, settings, bank_list):
    """Each payslip with pay to receive and its state for paying: ``[{'slip', 'state', 'why', 'bank_name_on_account',
    'match', 'account', 'code'}]``. ``state``: 'paid' or 'pending' (already sent), 'ready', or 'blocked'."""
    slips = db.session.scalars(select(Payslip).where(Payslip.run_id == run.id, Payslip.net > 0).order_by(Payslip.name)).all()
    pays = {p.staff_id: p for p in db.session.scalars(select(StaffPay).where(
        StaffPay.staff_id.in_([s.staff_id for s in slips] or [0]))).all()}
    out = []
    for s in slips:
        row = {'slip': s, 'state': 'ready', 'why': '', 'bank_name_on_account': '', 'match': False, 'account': None, 'code': None}
        out.append(row)
        if s.transfer_status in pt.LIVE:
            row['state'] = 'paid' if s.transfer_status == 'success' else 'pending'
            continue
        pay = pays.get(s.staff_id)
        account = pr.decrypt(pay.account_number_enc) if pay and pay.account_number_enc else ''
        if not account or not s.account_last4:
            row.update(state='blocked', why='No bank account on the approved payslip.')
            continue
        if pay.bank_name != s.bank_name or account[-4:] != s.account_last4:
            row.update(state='blocked', why='Bank details changed after approval. Reopen the payroll and approve it again.')
            continue
        code = pt.bank_code(s.bank_name, bank_list)
        if not code:
            row.update(state='blocked', why=f'Paystack does not list {s.bank_name or "this bank"}. Pay this person by bank transfer.')
            continue
        held, why = pt.resolve(settings, account, code)
        if not held:
            row.update(state='blocked', why=why)
            continue
        row.update(bank_name_on_account=held, match=pt.names_match(held, s.name, s.account_name), account=account, code=code)
    return out


def _reason(run):
    first = datetime.strptime(run.period + '-01', '%Y-%m-%d')
    return f'Salary {first.strftime("%b %Y")}'


@app.route('/admin/payroll/runs/<int:rid>/paystack')
@admin_required
def admin_payroll_paystack(rid):
    refused = _proprietor_only()
    if refused:
        return refused
    run = _run(rid)
    if run.status != 'approved':
        flash('Only an approved payroll that is not yet paid is paid from here.', 'error')
        return _back(rid)
    settings = payments.payment_settings()
    if settings is None:
        flash('Connect the school\'s Paystack account first (Settings, Online payments).', 'error')
        return _back(rid)
    problem, rows, held = None, [], None
    try:
        rows = _candidates(run, settings, pt.banks(settings))
        held = pt.balance(settings)
    except payments.PaystackError as exc:
        problem = exc.detail
    ready = [r for r in rows if r['state'] == 'ready']
    response = make_response(render_template(
        'school_payroll_paystack.html', run=run, rows=rows, problem=problem, balance=held, pr=pr,
        label=datetime.strptime(run.period + '-01', '%Y-%m-%d').strftime('%B %Y'),
        ready_total=sum(r['slip'].net for r in ready if r['match']), ready_count=sum(1 for r in ready if r['match'])))
    response.headers['Cache-Control'] = 'no-store, private'
    audit_log('payroll_paystack_reviewed', 'payroll', 'payroll_run', rid, {'people': len(rows)})
    return response


@app.post('/admin/payroll/runs/<int:rid>/paystack/send')
@admin_required
@csrf_protect
def admin_payroll_paystack_send(rid):
    refused = _proprietor_only()
    if refused:
        return refused
    me = current_admin()
    review = redirect(url_for('admin_payroll_paystack', rid=rid))
    # Only a wrong password counts towards the limit, so paying in several goes is never locked out.
    tries = f'payroll-pay-password:{me["id"]}'
    if _too_many(tries, 5, 900):
        flash('Too many wrong passwords. Wait a quarter of an hour and try again. Nothing was sent.', 'error')
        return review
    password_hash = db.session.scalar(select(Admin.password_hash).where(Admin.id == me['id']))
    if not password_hash or not check_password_hash(password_hash, request.form.get('password', '')):
        _count_try(tries, 5, 900)
        audit_log('payroll_paystack_password_refused', 'payroll', 'payroll_run', rid, {}, False)
        flash('That password is not right. Nothing was sent.', 'error')
        return review
    wanted = {int(v) for v in request.form.getlist('slip') if str(v).isdigit()}
    if not wanted:
        flash('Tick the people to pay.', 'error')
        return review
    settings = payments.payment_settings()
    if settings is None:
        flash('Connect the school\'s Paystack account first.', 'error')
        return _back(rid)
    run = _run(rid, lock=True)
    if run.status != 'approved':
        db.session.rollback()
        flash('Only an approved payroll that is not yet paid is paid from here.', 'error')
        return _back(rid)
    try:
        rows = [r for r in _candidates(run, settings, pt.banks(settings)) if r['state'] == 'ready' and r['slip'].id in wanted]
        total = sum(r['slip'].net for r in rows)
        if rows and pt.balance(settings) < total:
            db.session.rollback()
            flash(f'The school\'s Paystack balance does not cover {pr.naira(total)}. Nothing was sent.', 'error')
            return review
        for r in rows:
            r['recipient'] = pt.recipient(settings, r['slip'].name, r['account'], r['code'])
    except payments.PaystackError as exc:
        db.session.rollback()
        flash(f'{exc.detail} Nothing was sent.', 'error')
        return review
    if not rows:
        db.session.rollback()
        flash('None of the ticked people can be paid from here now. Nothing was sent.', 'error')
        return review
    # Every one is marked pending, with its own reference, and committed before anything is sent: a second click, or
    # another window, finds them pending and leaves them alone.
    now = _now()
    for r in rows:
        s = r['slip']
        s.transfer_status, s.transfer_reference, s.transfer_code, s.transfer_error = 'pending', pt.new_reference(run.id, s.id), None, None
        s.transfer_sent_at, s.transfer_sent_by, s.transfer_done_at = now, me['id'], None
    db.session.commit()
    sent = refused_n = unsure = 0
    for start in range(0, len(rows), pt.BULK_LIMIT):
        chunk = rows[start:start + pt.BULK_LIMIT]
        try:
            codes = pt.send_bulk(settings, [{'amount': r['slip'].net, 'recipient': r['recipient'],
                                             'reference': r['slip'].transfer_reference, 'reason': _reason(run)} for r in chunk])
        except payments.PaystackError as exc:
            # Paystack refused the whole request: none of these was sent, and they may be tried again.
            for r in chunk:
                r['slip'].transfer_status, r['slip'].transfer_error = 'failed', exc.detail
            refused_n += len(chunk)
            db.session.commit()
            flash(exc.detail, 'error')
            continue
        except ConnectionError as exc:
            # Not known whether Paystack took them: they stay pending, and are settled by asking Paystack.
            current_app.logger.warning('Payroll transfers for run %s: no clear answer from Paystack (%s)', rid, exc)
            unsure += len(chunk)
            continue
        for r in chunk:
            r['slip'].transfer_code = codes.get(r['slip'].transfer_reference)
        sent += len(chunk)
        db.session.commit()
    audit_log('payroll_paystack_sent', 'payroll', 'payroll_run', rid,
              {'sent': sent, 'refused': refused_n, 'unconfirmed': unsure, 'total': sum(r['slip'].net for r in rows)})
    if sent:
        flash(f'{sent} transfer{"" if sent == 1 else "s"} sent to Paystack. Each shows as paid when the bank confirms it.', 'success')
    if unsure:
        flash(f'Paystack did not answer clearly for {unsure}. They are kept as pending; press Check transfers in a few '
              'minutes. They are never sent twice.', 'error')
    return _back(rid)


@app.post('/admin/payroll/runs/<int:rid>/paystack/check')
@admin_required
@csrf_protect
def admin_payroll_paystack_check(rid):
    if not (is_school_admin() or admin_has_permission(current_admin()['id'], 'payroll.manage')):
        return admin_access_error('payroll.manage')
    if not _rate_limit(f'payroll-check:{rid}', limit=10, window=300):
        flash('Checked a moment ago. Wait a little and check again.', 'error')
        return _back(rid)
    settings = payments.payment_settings()
    if settings is None:
        flash('Connect the school\'s Paystack account first.', 'error')
        return _back(rid)
    changed = settle_pending(settings, run_ids=[rid])
    flash(f'Checked with Paystack: {changed} transfer{"" if changed == 1 else "s"} updated.' if changed
          else 'Checked with Paystack: nothing has changed yet.', 'success')
    return _back(rid)


# ---------------------------------------------------------------- settling a transfer, from Paystack's own answer
def _settle(settings, slip, now=None):
    """Ask Paystack about one pending transfer and record what it says. Returns True when the payslip changed."""
    state, why = pt.verify(settings, slip.transfer_reference)
    if state == 'pending':
        return False
    if state == 'unknown':
        sent = slip.transfer_sent_at or ''
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(sent)).total_seconds() if sent else 1e9
        if age < pt.NOT_FOUND_AFTER_SECONDS:
            return False
        state = 'failed'
    slip.transfer_status, slip.transfer_error = state, None if state == 'success' else why
    slip.transfer_done_at = now or _now()
    return True


def _finish(run_ids):
    """Mark paid each approved run whose every payslip with pay to receive has been paid through Paystack."""
    for run in db.session.scalars(select(PayrollRun).where(PayrollRun.id.in_(run_ids), PayrollRun.status == 'approved')).all():
        due = db.session.scalars(select(Payslip).where(Payslip.run_id == run.id, Payslip.net > 0)).all()
        if due and all(s.transfer_status == 'success' for s in due):
            senders = [s.transfer_sent_by for s in due if s.transfer_sent_by]
            run.status, run.paid_at, run.paid_by = 'paid', _now(), senders[-1] if senders else None
            audit_log('payroll_run_paid', 'payroll', 'payroll_run', run.id, {'period': run.period, 'net': run.net, 'by': 'paystack'})


def settle_pending(settings, run_ids=None, reference=None):
    """Settle pending transfers by asking Paystack: those of these runs, or the one with this reference (a webhook).
    Returns how many payslips changed."""
    stmt = select(Payslip).where(Payslip.transfer_status == 'pending')
    if reference:
        stmt = stmt.where(Payslip.transfer_reference == reference)
    if run_ids:
        stmt = stmt.where(Payslip.run_id.in_(run_ids))
    changed, touched = 0, set()
    for slip in db.session.scalars(stmt.limit(200)).all():
        try:
            if _settle(settings, slip):
                changed += 1
                touched.add(slip.run_id)
        except payments.PaystackError as exc:
            current_app.logger.warning('Could not check payroll transfer %s: %s', slip.transfer_reference, exc.detail)
    if changed:
        _finish(touched)
        db.session.commit()
        audit_log('payroll_paystack_settled', 'payroll', 'payroll_run', ','.join(map(str, sorted(touched))), {'changed': changed})
    return changed


def transfer_webhook(settings, data):
    """A signed transfer.success, transfer.failed or transfer.reversed: settle the payslip with that reference, by asking
    Paystack (never from the webhook's own body). A reference that is not a payroll's is ignored."""
    reference = str(data.get('reference') or '')
    if not reference.startswith('bsp-'):
        return
    slip = db.session.scalars(select(Payslip).where(Payslip.transfer_reference == reference)).first()
    if slip is None:
        return
    if slip.transfer_status == 'success':
        # Only a reversal can follow a success; ask Paystack, and record it if so.
        state, why = pt.verify(settings, reference)
        if state == 'reversed':
            slip.transfer_status, slip.transfer_error, slip.transfer_done_at = 'reversed', why, _now()
            db.session.commit()
            audit_log('payroll_paystack_reversed', 'payroll', 'payroll_run', slip.run_id, {'payslip': slip.id})
        return
    if slip.transfer_status == 'pending':
        settle_pending(settings, reference=reference)
