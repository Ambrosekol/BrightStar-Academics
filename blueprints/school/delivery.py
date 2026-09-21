"""A school setting up its own email and WhatsApp delivery.

What parents receive — payment receipts, password-recovery emails, alerts about school work —
should come from the school. This page lets the school's administrators enter their own mail
server and WhatsApp Business account, check that they work, and remove them again (the school
then goes back to the platform's shared account, if there is one).

Guarded by the ``delivery.manage`` permission. The rules, the encryption of the secrets and the
limits on where the server may connect are in core/delivery.py; nothing on this page ever shows a
saved password or token again.
"""

import re

from flask import flash, redirect, render_template, request, url_for

from app import app
from control_plane import config
from control_plane.context import current_tenant
from core import delivery
from core.accounts import _rate_limit
from core.branding import school_name
from core.security import admin_required, audit_log, csrf_protect, current_admin
from models import db

_ADDRESS = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _back():
    return redirect(url_for('admin_school_delivery'))


@app.route('/admin/school/delivery')
@admin_required
def admin_school_delivery():
    return render_template('admin_school_delivery.html', state=delivery.status(),
                           security_choices=delivery.SECURITY_CHOICES, ports=delivery.ALLOWED_SMTP_PORTS,
                           production=config.is_production(), default_version=delivery.DEFAULT_GRAPH_VERSION)


@app.post('/admin/school/delivery/email/save')
@admin_required
@csrf_protect
def admin_school_delivery_email_save():
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
    delivery.clear_channel('email')
    audit_log('school_delivery_cleared', 'school', 'settings', 'email')
    flash("Your own email settings were removed. The school now uses the platform's shared account, if there is one.",
          'success')
    return _back()


@app.post('/admin/school/delivery/email/test')
@admin_required
@csrf_protect
def admin_school_delivery_email_test():
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


@app.post('/admin/school/delivery/whatsapp/save')
@admin_required
@csrf_protect
def admin_school_delivery_whatsapp_save():
    try:
        fields = delivery.save_whatsapp(request.form, current_admin()['id'])
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'error')
    else:
        audit_log('school_delivery_updated', 'school', 'settings', 'whatsapp', {'fields': fields})
        flash('Your WhatsApp settings have been saved. Check the connection to be sure they work.', 'success')
    return _back()


@app.post('/admin/school/delivery/whatsapp/clear')
@admin_required
@csrf_protect
def admin_school_delivery_whatsapp_clear():
    delivery.clear_channel('whatsapp')
    audit_log('school_delivery_cleared', 'school', 'settings', 'whatsapp')
    flash("Your own WhatsApp settings were removed. The school now uses the platform's shared account, if there is one.",
          'success')
    return _back()


@app.post('/admin/school/delivery/whatsapp/check')
@admin_required
@csrf_protect
def admin_school_delivery_whatsapp_check():
    if not _rate_limit(f'delivery-test:{current_tenant().slug}', limit=5, window=600):
        flash('Too many checks just now. Please wait a few minutes.', 'error')
        return _back()
    settings = delivery.whatsapp_settings()
    if settings is None:
        flash('WhatsApp is not set up yet. Save your WhatsApp settings first.', 'error')
        return _back()
    ok, detail = delivery.check_whatsapp(settings)
    audit_log('school_delivery_test', 'school', 'settings', 'whatsapp', {'ok': ok, 'account': settings.source}, success=ok)
    flash(detail, 'success' if ok else 'error')
    return _back()
