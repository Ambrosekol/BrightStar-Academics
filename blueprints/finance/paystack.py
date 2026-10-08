"""Paying a fee online: a school's own Paystack account (settings), and the parent-facing flow
that turns a completed Paystack checkout into an ordinary FinancePayment.

A payment made this way is, from the moment it lands, indistinguishable from one a member of
staff typed in by hand: same table, same receipt, same notification, same allocation step
afterwards. What is different is how it gets there, and that is what lives in this file and in
core/payments.py:

1. A parent, looking at their child's fee account, starts a payment for an amount up to what is
   outstanding. A row is written here first (FinanceOnlinePayment, 'pending') with a reference
   that is ours, then Paystack is asked to open a transaction for it, and the parent's browser is
   sent to the address Paystack gives back.
2. The parent pays on Paystack's own page - card, bank transfer, USSD, whatever Paystack offers;
   none of that is built here.
3. Two independent things can report it back: Paystack redirects the browser to our callback URL
   (fast, but a parent can close the tab before it loads), and Paystack calls our webhook URL
   directly, server to server (slower, but does not depend on the parent's browser at all).
   Either path calls the same _finalize function, which asks Paystack itself what really happened
   (never trusts the callback's query string or the webhook's body on its own) and only the first
   of the two to arrive is allowed to act - a conditional UPDATE that must still find the row
   'pending' is what makes that safe even if both arrive at close to the same moment.
"""

import json
from datetime import datetime, timedelta, timezone

from flask import abort, current_app, flash, redirect, render_template, request, url_for
from sqlalchemy import select

from app import app, _school_current_session
from blueprints.finance.helpers import _finance_can_view_all, _next_receipt_no
from blueprints.parents.helpers import parent_required, _parent_owns_student
from core import payments
from core.db_helpers import obj, one_scalar
from core.idempotency import idempotent_write
from core.jobs import enqueue
from core.notifications import _notify_parents_payment_recorded
from core.security import admin_access_error, admin_required, audit_log, csrf_protect, current_admin, is_school_admin
from models import FinanceOnlinePayment, FinancePayment, FinanceRefund, Student, db


# =================================================================== the school's own settings

@app.route('/admin/finance/paystack')
@admin_required
def admin_finance_paystack_settings():
    if not is_school_admin(): return admin_access_error('Online Payments')
    settings = payments.payment_settings()
    return render_template('admin_finance_paystack.html', settings=settings,
                           webhook_url=url_for('paystack_webhook', _external=True))


@app.post('/admin/finance/paystack/save')
@admin_required
@csrf_protect
def admin_finance_paystack_save():
    if not is_school_admin(): return admin_access_error('Online Payments')
    public_key = request.form.get('public_key', '').strip()
    secret_key = request.form.get('secret_key', '').strip()
    if not public_key.startswith(('pk_test_', 'pk_live_')):
        flash('Enter a valid Paystack public key (it starts with pk_test_ or pk_live_).', 'error')
        return redirect(url_for('admin_finance_paystack_settings'))
    existing = payments.payment_settings()
    if not secret_key and existing is None:
        flash('Enter your Paystack secret key (it starts with sk_test_ or sk_live_).', 'error')
        return redirect(url_for('admin_finance_paystack_settings'))
    if secret_key and not secret_key.startswith(('sk_test_', 'sk_live_')):
        flash('Enter a valid Paystack secret key (it starts with sk_test_ or sk_live_).', 'error')
        return redirect(url_for('admin_finance_paystack_settings'))
    payments.set_payment_settings(public_key, secret_key, current_admin()['id'])
    audit_log('school_paystack_updated', 'finance', 'settings', 'paystack', {'public_key': public_key})
    flash('Your Paystack settings have been saved. Test the connection to check them, then paste '
          'the webhook address below into your Paystack dashboard.', 'success')
    return redirect(url_for('admin_finance_paystack_settings'))


@app.post('/admin/finance/paystack/clear')
@admin_required
@csrf_protect
def admin_finance_paystack_clear():
    if not is_school_admin(): return admin_access_error('Online Payments')
    payments.clear_payment_settings()
    audit_log('school_paystack_cleared', 'finance', 'settings', 'paystack')
    flash('Your Paystack settings were removed. Parents can no longer pay online until they are set up again.', 'success')
    return redirect(url_for('admin_finance_paystack_settings'))


@app.post('/admin/finance/paystack/test')
@admin_required
@csrf_protect
def admin_finance_paystack_test():
    if not is_school_admin(): return admin_access_error('Online Payments')
    settings = payments.payment_settings()
    if settings is None:
        flash('Paystack is not set up yet. Save your keys first.', 'error')
        return redirect(url_for('admin_finance_paystack_settings'))
    ok, detail = payments.check_connection(settings)
    audit_log('school_paystack_test', 'finance', 'settings', 'paystack', {'ok': ok}, success=ok)
    flash(detail, 'success' if ok else 'error')
    return redirect(url_for('admin_finance_paystack_settings'))


@app.post('/admin/finance/payments/<int:payment_id>/refund')
@admin_required
@csrf_protect
def admin_finance_payment_refund(payment_id):
    """Refund a payment that was made online, through Paystack, back to the payer - the online
    complement to admin_finance_payment_void for a payment recorded by hand. Same permission and
    reason requirement as a void; unlike a void, this actually moves money, through Paystack's own
    /refund endpoint, and the payment is only marked voided once Paystack's webhook confirms the
    refund really went through (see FinanceRefund and paystack_webhook, below).
    """
    me = current_admin()
    if not _finance_can_view_all(me): return admin_access_error('finance.manage')
    reason = request.form.get('reason', '').strip()
    if not reason:
        flash('A reason is required to refund a payment.', 'error')
        return redirect(url_for('admin_finance_receipt', payment_id=payment_id))
    payment = obj(FinancePayment, payment_id)
    if not payment: abort(404)
    if payment.status != 'posted':
        flash('Only a posted payment can be refunded.', 'error')
        return redirect(url_for('admin_finance_receipt', payment_id=payment_id))
    online = db.session.scalars(select(FinanceOnlinePayment).where(
        FinanceOnlinePayment.payment_id == payment.id, FinanceOnlinePayment.status == 'success')).first()
    if not online:
        flash('This payment was not made through online payments, so it cannot be refunded here - '
              'void it instead and refund the payer by hand.', 'error')
        return redirect(url_for('admin_finance_receipt', payment_id=payment_id))
    already = db.session.scalars(select(FinanceRefund).where(
        FinanceRefund.payment_id == payment.id, FinanceRefund.status.in_(('pending', 'processed')))).first()
    if already:
        flash(f'A refund for this payment already exists (status: {already.status}).', 'error')
        return redirect(url_for('admin_finance_receipt', payment_id=payment_id))
    try:
        amount = float(request.form.get('amount', '') or payment.amount)
    except (TypeError, ValueError):
        amount = payment.amount
    if amount <= 0 or amount > payment.amount + 0.01:
        flash('Enter a refund amount up to the amount of the payment.', 'error')
        return redirect(url_for('admin_finance_receipt', payment_id=payment_id))
    settings = payments.payment_settings()
    if settings is None:
        flash('Online payments are not set up for this school any more, so Paystack cannot be asked for a refund.', 'error')
        return redirect(url_for('admin_finance_receipt', payment_id=payment_id))
    try:
        data = payments.request_refund(settings, online.reference, amount_naira=amount,
                                        customer_note=reason, merchant_note=reason)
    except payments.PaystackError as exc:
        audit_log('finance_payment_refund_failed', 'finance', 'payment', payment.id, {'reason': reason, 'detail': exc.detail}, False)
        flash(f'Could not start the refund: {exc.detail}', 'error')
        return redirect(url_for('admin_finance_receipt', payment_id=payment_id))
    now = datetime.now(timezone.utc).isoformat()
    refund = FinanceRefund(
        payment_id=payment.id, online_payment_id=online.id, amount=amount, reason=reason,
        status='pending', paystack_refund_id=str(data.get('id') or ''),
        requested_by=me['id'], requested_at=now)
    db.session.add(refund)
    db.session.commit()
    audit_log('finance_payment_refund_requested', 'finance', 'payment', payment.id,
              {'refund_id': refund.id, 'paystack_refund_id': refund.paystack_refund_id, 'amount': amount, 'reason': reason})
    flash('Refund requested from Paystack. The payment will be marked refunded automatically once '
          'Paystack confirms the money has actually moved.', 'success')
    return redirect(url_for('admin_finance_receipt', payment_id=payment_id))


# =================================================================== a parent paying online

def _outstanding_for(student_id):
    from blueprints.finance.helpers import _finance_student_lifetime_totals
    return _finance_student_lifetime_totals(student_id)['outstanding']


@app.post('/parent/children/<int:student_id>/finance/pay')
@parent_required
@csrf_protect
@idempotent_write('parent.pay_online')
def parent_finance_pay_start(student_id):
    from flask import session

    pid = session['parent_id']
    if not _parent_owns_student(pid, student_id):
        abort(404)
    settings = payments.payment_settings()
    if settings is None:
        flash('This school has not set up online payments yet.', 'error')
        return redirect(url_for('parent_child_finance', student_id=student_id))
    student = obj(Student, student_id)
    current = _school_current_session()
    if not student or not current:
        abort(404)

    # Either specific fees ticked by the parent (each paid in full, whatever is still owing on
    # it), or - the older lump-sum form - one amount up to the whole outstanding balance.
    from blueprints.finance.helpers import _finance_student_outstanding
    chosen_ids = set()
    for raw in request.form.getlist('assessment_ids'):
        try: chosen_ids.add(int(raw))
        except (TypeError, ValueError): pass
    items = []
    session_id = current['id']
    if chosen_ids:
        owing = {i['id']: i for i in _finance_student_outstanding(student_id) if i['outstanding'] > 0.005}
        picked = [owing[i] for i in sorted(chosen_ids) if i in owing]
        if not picked or len(picked) != len(chosen_ids):
            flash('One of the selected fees is no longer outstanding. Please review and try again.', 'error')
            return redirect(url_for('parent_child_finance', student_id=student_id))
        items = [{'assessment_id': i['id'], 'amount': i['outstanding']} for i in picked]
        amount = round(sum(i['amount'] for i in items), 2)
        session_id = picked[0]['session_id']
    else:
        try:
            amount = float(request.form.get('amount', '0'))
        except (TypeError, ValueError):
            amount = 0
        outstanding = _outstanding_for(student_id)
        if amount <= 0 or amount > outstanding + 0.01:  # a few kobo of float slack, never more
            flash('Choose at least one fee to pay.', 'error')
            return redirect(url_for('parent_child_finance', student_id=student_id))

    from models import ParentAccount
    parent = obj(ParentAccount, pid)
    email = (parent.email or '').strip()
    if not email:
        # Paystack insists on a well-formed address; a bare "localhost" host is not one.
        host = request.host.split(':')[0]
        email = f'guardian{pid}@{host if "." in host else "example.com"}'

    reference = payments.new_reference()
    now = datetime.now(timezone.utc).isoformat()
    db.session.add(FinanceOnlinePayment(
        reference=reference, student_id=student_id, parent_id=pid, session_id=session_id,
        amount=amount, status='pending', created_at=now, items=json.dumps(items) if items else None))
    db.session.commit()

    try:
        authorization_url = payments.initialize_transaction(
            settings, email, amount, reference,
            url_for('parent_finance_pay_callback', student_id=student_id, _external=True))
    except payments.PaystackError as exc:
        flash(f'Could not start the payment: {exc.detail}', 'error')
        return redirect(url_for('parent_child_finance', student_id=student_id))
    return redirect(authorization_url)


@app.route('/parent/children/<int:student_id>/finance/pay/callback')
@parent_required
def parent_finance_pay_callback(student_id):
    from flask import session

    pid = session['parent_id']
    if not _parent_owns_student(pid, student_id):
        abort(404)
    reference = request.args.get('reference', '').strip()
    row = db.session.scalars(select(FinanceOnlinePayment).where(
        FinanceOnlinePayment.reference == reference, FinanceOnlinePayment.student_id == student_id)).first()
    if not row:
        abort(404)
    settings = payments.payment_settings()
    if settings is not None and row.status == 'pending':
        _finalize(settings, row)
    if row.status == 'success':
        flash('Payment received. Thank you.', 'success')
    elif row.status == 'failed':
        flash(f'This payment was not successful{": " + row.failure_reason if row.failure_reason else ""}.', 'error')
    else:
        flash('This payment is still being confirmed. Refresh in a moment.', 'error')
    return redirect(url_for('parent_child_finance', student_id=student_id))


@app.post('/paystack/webhook')
def paystack_webhook():
    """Paystack calling us directly, independent of the parent's own browser.

    Not behind @admin_required or @parent_required - Paystack is not signed in to anything here.
    The signature check below is the entire authorisation: without a body signed by this exact
    school's own secret key, nothing in the request is read as fact.
    """
    settings = payments.payment_settings()
    if settings is None:
        return '', 404
    raw_body = request.get_data()
    if not payments.verify_webhook_signature(settings.secret_key, raw_body, request.headers.get('x-paystack-signature', '')):
        current_app.logger.warning('Paystack webhook with a bad or missing signature was refused.')
        return '', 401
    try:
        event = json.loads(raw_body.decode())
    except (ValueError, UnicodeDecodeError):
        return '', 400
    data = event.get('data') or {}
    event_type = event.get('event', '')
    if event_type in ('refund.processed', 'refund.failed'):
        _finalize_refund(event_type, data)
        return '', 200
    reference = data.get('reference', '')
    row = db.session.scalars(select(FinanceOnlinePayment).where(
        FinanceOnlinePayment.reference == reference)).first()
    if row and row.status == 'pending':
        _finalize(settings, row)
    return '', 200  # acknowledged either way, so Paystack does not keep retrying a reference we do not know


def _finalize(settings, row):
    """Ask Paystack what really happened to ``row``'s reference, and act on it exactly once.

    The conditional UPDATE is the actual claim: only the request that wins it goes on to create
    the FinancePayment and send the notifications, so a callback and a webhook arriving for the
    same reference at close to the same moment can never both do it.
    """
    claimed = db.session.execute(
        FinanceOnlinePayment.__table__.update()
        .where(FinanceOnlinePayment.id == row.id, FinanceOnlinePayment.status == 'pending')
        .values(status='verifying')).rowcount == 1
    db.session.commit()
    if not claimed:
        return

    try:
        data = payments.verify_transaction(settings, row.reference)
    except payments.PaystackError as exc:
        row.status = 'pending'  # could not even ask Paystack; leave it to be tried again
        row.failure_reason = exc.detail
        db.session.commit()
        return

    row.paystack_transaction_id = str(data.get('id') or '')
    now = datetime.now(timezone.utc).isoformat()
    row.verified_at = now

    if data.get('status') != 'success':
        row.status = 'failed'
        row.failure_reason = (data.get('gateway_response') or 'Payment not successful')[:500]
        db.session.commit()
        return

    # Paystack's own amount (kobo) is what is trusted, not whatever this row was opened with.
    paid_naira = round((data.get('amount') or 0) / 100, 2)
    admin_id = _payments_admin_id()
    receipt = _next_receipt_no()
    items = _row_items(row)
    category = _items_category(items)
    payment = FinancePayment(
        receipt_no=receipt, student_id=row.student_id, session_id=row.session_id,
        amount=paid_naira, category=category, method='Paystack', reference=row.reference,
        paid_at=now, recorded_by=admin_id, status='posted',
        notes='Paid online by the parent through Paystack.', created_at=now)
    db.session.add(payment)
    db.session.flush()
    _apply_items(payment, items, paid_naira, admin_id, now)
    row.payment_id = payment.id
    row.status = 'success'
    db.session.commit()

    audit_log('finance_payment_recorded', 'finance', 'payment', payment.id,
              {'receipt_no': receipt, 'amount': paid_naira, 'student_id': row.student_id,
               'method': 'Paystack', 'online_payment_id': row.id})
    try:
        _notify_parents_payment_recorded(row.student_id, receipt, paid_naira, category, admin_id, external=False)
    except Exception:
        current_app.logger.exception('Parent payment-recorded notification failed for student %s', row.student_id)
    enqueue('send_payment_receipt', payment_id=payment.id, actor_id=admin_id)


def _row_items(row):
    try:
        items = json.loads(row.items) if row.items else []
    except ValueError:
        return []
    return [i for i in items if isinstance(i, dict) and i.get('assessment_id')]


def _items_category(items):
    """The payment's purpose: the fees the parent chose, or the generic label for a lump sum."""
    from models import FinanceFeeAssessment
    names = []
    for i in items:
        a = obj(FinanceFeeAssessment, i['assessment_id'])
        if a and a.category and a.category not in names:
            names.append(a.category)
    return (', '.join(names) or 'School Fees')[:200]


def _apply_items(payment, items, paid_naira, admin_id, now):
    """Apply the money to the exact fees the parent ticked, so they show as paid straight away
    instead of waiting for the office to allocate it. Never over-applies: what is allocated to a
    fee is capped by what it still owes now (staff may have allocated to it since) and by what
    Paystack really took; any remainder just stays unallocated for the office to place."""
    from blueprints.finance.helpers import _finance_assessment_allocated
    from models import FinanceFeeAssessment, FinancePaymentAllocation
    left = paid_naira
    for i in items:
        a = obj(FinanceFeeAssessment, i['assessment_id'])
        if not a or not a.active or left <= 0.004:
            continue
        owed = round(float(a.amount or 0) - _finance_assessment_allocated(a.id), 2)
        part = round(min(float(i.get('amount') or 0), owed, left), 2)
        if part <= 0:
            continue
        db.session.add(FinancePaymentAllocation(
            payment_id=payment.id, assessment_id=a.id, amount=part, created_by=admin_id, created_at=now))
        left = round(left - part, 2)


def _finalize_refund(event_type, data):
    """Act on a refund's outcome exactly once, whether it arrived by webhook or by
    reconcile_pending_refunds() asking Paystack directly. The conditional UPDATE is the actual
    claim, the same idea as _finalize()'s for a payment: two reports of the same outcome arriving
    close together can never both act. 'settling' is a deliberately distinct in-between state (not
    the final one) so a process that crashed between the claim and finishing the work is visibly
    incomplete rather than silently marked done.
    """
    refund_id = str(data.get('id') or '')
    if not refund_id:
        return
    claimed = db.session.execute(
        FinanceRefund.__table__.update()
        .where(FinanceRefund.paystack_refund_id == refund_id, FinanceRefund.status == 'pending')
        .values(status='settling')).rowcount == 1
    db.session.commit()
    if not claimed:
        return
    row = db.session.scalars(select(FinanceRefund).where(FinanceRefund.paystack_refund_id == refund_id)).first()
    if not row:
        return
    now = datetime.now(timezone.utc).isoformat()
    row.resolved_at = now

    if event_type != 'refund.processed':
        row.status = 'failed'
        row.failure_reason = (data.get('failure_reason') or data.get('message')
                              or 'Paystack could not process this refund.')[:500]
        db.session.commit()
        return

    row.status = 'processed'
    payment = obj(FinancePayment, row.payment_id)
    if payment and payment.status == 'posted':
        payment.status = 'voided'
        payment.voided_at = now
        payment.voided_by = row.requested_by
        payment.void_reason = f'Refunded via Paystack: {row.reason}' if row.reason else 'Refunded via Paystack.'
    db.session.commit()
    audit_log('finance_payment_refunded', 'finance', 'payment', row.payment_id,
              {'refund_id': row.id, 'paystack_refund_id': refund_id, 'amount': row.amount})


def reconcile_pending_refunds(max_age_seconds=120, limit=5):
    """The same self-heal reconcile_pending() gives a payment, for a refund instead: a school
    whose webhook address changed, or was never pasted into the Paystack dashboard at all, would
    otherwise never learn that a refund it started actually finished - or failed."""
    settings = payments.payment_settings()
    if settings is None:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat()
    rows = db.session.scalars(select(FinanceRefund)
        .where(FinanceRefund.status == 'pending', FinanceRefund.requested_at < cutoff)
        .order_by(FinanceRefund.id).limit(limit)).all()
    for row in rows:
        if not row.paystack_refund_id:
            continue
        try:
            data = payments.fetch_refund(settings, row.paystack_refund_id)
        except payments.PaystackError:
            continue
        status = data.get('status')
        if status == 'processed':
            _finalize_refund('refund.processed', data)
        elif status == 'failed':
            _finalize_refund('refund.failed', data)


def reconcile_pending(max_age_seconds=120, limit=5):
    """Self-heal any online payment stuck 'pending' longer than a single browser return or a
    single webhook delivery should ever take.

    Two independent things are supposed to settle every online payment - the parent's own
    browser coming back to the callback URL, and Paystack's webhook calling us directly - and
    ordinarily at least one of them does, within seconds. But a parent can pay by bank transfer
    or USSD and never reopen the tab, and a webhook can fail to reach a school whose address
    changed or whose webhook was never pasted into the Paystack dashboard at all; when both miss,
    money has moved on Paystack's side but nothing here would otherwise ever ask about it again.

    This closes that gap the same way core/jobs.py's own stuck-job retry does: opportunistically,
    from ordinary page loads a finance admin or the paying parent already makes, rather than a
    separate scheduled service with its own failure modes to worry about.
    """
    settings = payments.payment_settings()
    if settings is None:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat()
    rows = db.session.scalars(select(FinanceOnlinePayment)
        .where(FinanceOnlinePayment.status == 'pending', FinanceOnlinePayment.created_at < cutoff)
        .order_by(FinanceOnlinePayment.id).limit(limit)).all()
    for row in rows:
        try:
            _finalize(settings, row)
        except Exception:
            current_app.logger.exception('Reconciling stuck online payment %s failed', row.id)


def _payments_admin_id():
    """Who an automatic, online payment is attributed to: whoever last set up this school's own
    Paystack account, since no member of staff acted on this particular payment. FinancePayment's
    recorded_by is not nullable - it always names a real member of staff - so this is the honest
    answer to "who is responsible for this channel existing", not a claim that they typed it in."""
    from models import SchoolPaymentSetting
    return one_scalar(select(SchoolPaymentSetting.updated_by)
                      .where(SchoolPaymentSetting.setting_key == 'paystack_public_key'))
