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
from blueprints.finance.helpers import _next_receipt_no
from blueprints.parents.helpers import parent_required, _parent_owns_student
from core import payments
from core.db_helpers import obj, one_scalar
from core.idempotency import idempotent_write
from core.jobs import enqueue
from core.notifications import _notify_parents_payment_recorded
from core.security import admin_required, audit_log, csrf_protect, current_admin
from models import FinanceOnlinePayment, FinancePayment, Student, db


# =================================================================== the school's own settings

@app.route('/admin/finance/paystack')
@admin_required
def admin_finance_paystack_settings():
    settings = payments.payment_settings()
    return render_template('admin_finance_paystack.html', settings=settings,
                           webhook_url=url_for('paystack_webhook', _external=True))


@app.post('/admin/finance/paystack/save')
@admin_required
@csrf_protect
def admin_finance_paystack_save():
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
    payments.clear_payment_settings()
    audit_log('school_paystack_cleared', 'finance', 'settings', 'paystack')
    flash('Your Paystack settings were removed. Parents can no longer pay online until they are set up again.', 'success')
    return redirect(url_for('admin_finance_paystack_settings'))


@app.post('/admin/finance/paystack/test')
@admin_required
@csrf_protect
def admin_finance_paystack_test():
    settings = payments.payment_settings()
    if settings is None:
        flash('Paystack is not set up yet. Save your keys first.', 'error')
        return redirect(url_for('admin_finance_paystack_settings'))
    ok, detail = payments.check_connection(settings)
    audit_log('school_paystack_test', 'finance', 'settings', 'paystack', {'ok': ok}, success=ok)
    flash(detail, 'success' if ok else 'error')
    return redirect(url_for('admin_finance_paystack_settings'))


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
    try:
        amount = float(request.form.get('amount', '0'))
    except (TypeError, ValueError):
        amount = 0
    outstanding = _outstanding_for(student_id)
    if amount <= 0 or amount > outstanding + 0.01:  # a few kobo of float slack, never more
        flash('Enter an amount up to what is outstanding.', 'error')
        return redirect(url_for('parent_child_finance', student_id=student_id))

    from models import ParentAccount
    parent = obj(ParentAccount, pid)
    email = (parent.email or '').strip() or f'guardian{pid}@{request.host.split(":")[0]}'

    reference = payments.new_reference()
    now = datetime.now(timezone.utc).isoformat()
    db.session.add(FinanceOnlinePayment(
        reference=reference, student_id=student_id, parent_id=pid, session_id=current['id'],
        amount=amount, status='pending', created_at=now))
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
    reference = (event.get('data') or {}).get('reference', '')
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
    payment = FinancePayment(
        receipt_no=receipt, student_id=row.student_id, session_id=row.session_id,
        amount=paid_naira, category='School Fees', method='Paystack', reference=row.reference,
        paid_at=now, recorded_by=admin_id, status='posted',
        notes='Paid online by the parent through Paystack.', created_at=now)
    db.session.add(payment)
    db.session.flush()
    row.payment_id = payment.id
    row.status = 'success'
    db.session.commit()

    audit_log('finance_payment_recorded', 'finance', 'payment', payment.id,
              {'receipt_no': receipt, 'amount': paid_naira, 'student_id': row.student_id,
               'method': 'Paystack', 'online_payment_id': row.id})
    try:
        _notify_parents_payment_recorded(row.student_id, receipt, paid_naira, 'School Fees', admin_id, external=False)
    except Exception:
        current_app.logger.exception('Parent payment-recorded notification failed for student %s', row.student_id)
    enqueue('send_payment_receipt', payment_id=payment.id, actor_id=admin_id)


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
