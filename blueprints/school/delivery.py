"""A school setting up its own email and SMS delivery.

What parents receive — payment receipts, password-recovery emails, alerts about school work —
should come from the school. This page lets the school's administrators enter their own mail
server and BulkSMS Nigeria account, check that they work, and remove them again (the school
then goes back to the platform's shared account, if there is one). Whether schools use their own SMS
account or the platform's is decided by the platform (BRIGHTSTARS_SMS_PAYER, see core/delivery.py).

Restricted to the school's own top-level administrator (``is_school_admin``) - not a permission a
custom role can be given, since these accounts can send messages that look like they come from the
school. The rules, the encryption of the secrets and the limits on where the server may connect are
in core/delivery.py; nothing on this page ever shows a saved password or token again.
"""

import re

from flask import flash, redirect, render_template, request, url_for

from app import app
from control_plane import config
from control_plane.context import current_tenant
from core import delivery
from core.accounts import _rate_limit
from core.branding import school_name
from core.security import admin_access_error, admin_required, audit_log, csrf_protect, current_admin, is_school_admin
from models import db

_ADDRESS = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _back():
    return redirect(url_for('admin_school_delivery'))


@app.route('/admin/school/delivery')
@admin_required
def admin_school_delivery():
    if not is_school_admin(): return admin_access_error('Email & SMS settings')
    return render_template('admin_school_delivery.html', state=delivery.status(),
                           security_choices=delivery.SECURITY_CHOICES, ports=delivery.ALLOWED_SMTP_PORTS,
                           production=config.is_production(), sms_gateways=delivery.SMS_GATEWAYS)


@app.post('/admin/school/delivery/email/save')
@admin_required
@csrf_protect
def admin_school_delivery_email_save():
    if not is_school_admin(): return admin_access_error('Email & SMS settings')
    try:
        fields = delivery.save_email(request.form, current_admin()['id'])
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'error')
    else:
        # Only the names of what was set, never a value: a password must not reach the audit log.
        audit_log('school_delivery_updated', 'school', 'settings', 'email', {'fields': fields})
        flash('Your email settings have been saved. Send a test message to check them.', 'success')
    return _back()


@app.post('/admin/school/delivery/email/clear')
@admin_required
@csrf_protect
def admin_school_delivery_email_clear():
    if not is_school_admin(): return admin_access_error('Email & SMS settings')
    delivery.clear_channel('email')
    audit_log('school_delivery_cleared', 'school', 'settings', 'email')
    flash("Your own email settings were removed. The school now uses the platform's shared account, if there is one.",
          'success')
    return _back()


@app.post('/admin/school/delivery/email/test')
@admin_required
@csrf_protect
def admin_school_delivery_email_test():
    if not is_school_admin(): return admin_access_error('Email & SMS settings')
    to = request.form.get('to', '').strip()
    if not _ADDRESS.match(to) or len(to) > 200:
        flash('Enter an email address to send the test message to.', 'error')
        return _back()
    # Each test opens a connection from the server, so it is limited.
    if not _rate_limit(f'delivery-test:{current_tenant().slug}', limit=5, window=600):
        flash('Too many tests just now. Please wait a few minutes.', 'error')
        return _back()
    settings = delivery.email_settings()
    if settings is None:
        flash('Email is not set up yet. Save your mail server settings first.', 'error')
        return _back()
    ok, detail = delivery.check_email(settings, to, school_name())
    audit_log('school_delivery_test', 'school', 'settings', 'email', {'ok': ok, 'account': settings.source}, success=ok)
    flash(detail, 'success' if ok else 'error')
    return _back()


@app.post('/admin/school/delivery/sms/save')
@admin_required
@csrf_protect
def admin_school_delivery_sms_save():
    if not is_school_admin(): return admin_access_error('Email & SMS settings')
    try:
        fields = delivery.save_sms(request.form, current_admin()['id'])
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'error')
    else:
        # Only the names of what was set, never a value: a token must not reach the audit log.
        audit_log('school_delivery_updated', 'school', 'settings', 'sms', {'fields': fields})
        flash('Your SMS settings have been saved. Check the connection to be sure they work.', 'success')
    return _back()


@app.post('/admin/school/delivery/sms/clear')
@admin_required
@csrf_protect
def admin_school_delivery_sms_clear():
    if not is_school_admin(): return admin_access_error('Email & SMS settings')
    delivery.clear_channel('sms')
    audit_log('school_delivery_cleared', 'school', 'settings', 'sms')
    flash("Your own SMS settings were removed. The school now uses the platform's account, if there is one.", 'success')
    return _back()


@app.post('/admin/school/delivery/sms/check')
@admin_required
@csrf_protect
def admin_school_delivery_sms_check():
    if not is_school_admin(): return admin_access_error('Email & SMS settings')
    if not _rate_limit(f'delivery-test:{current_tenant().slug}', limit=5, window=600):
        flash('Too many checks just now. Please wait a few minutes.', 'error')
        return _back()
    settings = delivery.sms_settings()
    if settings is None:
        flash('SMS is not set up yet. Save your SMS settings first.', 'error')
        return _back()
    ok, detail = delivery.check_sms(settings)
    audit_log('school_delivery_test', 'school', 'settings', 'sms', {'ok': ok, 'account': settings.source}, success=ok)
    flash(detail, 'success' if ok else 'error')
    return _back()


@app.post('/admin/school/delivery/sms/balance')
@admin_required
@csrf_protect
def admin_school_delivery_sms_balance():
    """What is left in the school's own BulkSMS Nigeria wallet. Only where the school pays for its own texts:
    when the platform pays, the platform's balance is the platform's business and is never shown here."""
    if not is_school_admin(): return admin_access_error('Email & SMS settings')
    if delivery.sms_payer() == 'platform':
        flash('The platform provides SMS for every school, so there is no balance for the school to check.', 'error')
        return _back()
    if not _rate_limit(f'delivery-balance:{current_tenant().slug}', limit=5, window=600):
        flash('Too many checks just now. Please wait a few minutes.', 'error')
        return _back()
    settings = delivery.sms_settings()
    if settings is None or settings.source != 'school':
        flash("SMS is not connected to your school's own account yet. Save your SMS settings first.", 'error')
        return _back()
    ok, detail, _wallets = delivery.sms_balance(settings)
    audit_log('school_delivery_balance', 'school', 'settings', 'sms', {'ok': ok}, success=ok)
    flash(detail, 'success' if ok else 'error')
    return _back()
