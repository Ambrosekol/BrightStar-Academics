"""The Settings page: one place that leads to every setting, control and record of activity in the portal.

Nothing is stored or edited here. Each entry links to the page that already owns that setting, shows
the one fact worth knowing at a glance (is it connected, which session is current, how many alerts are
open), and appears only for people who may open the page behind it - the same permission and
school-administrator rules the pages themselves enforce. That is what lets the menu carry a single
"Settings" entry instead of a dozen.
"""

from flask import render_template, session, url_for

from app import _admin_workspace_for_path, _notification_counts, _school_current_session, app
from core import delivery, payments
from core.security import admin_has_permission, admin_required, current_admin, is_school_admin

NEUTRAL, GOOD, WARN = 'neutral', 'good', 'warn'


def _safe(fn, default=None):
    """A status is a nicety: if it cannot be worked out the entry simply shows no status."""
    from models import db
    try:
        return fn()
    except Exception:
        db.session.rollback()
        return default


def _delivery_status():
    state = delivery.status()
    email, sms = state['email']['mode'], state['sms']['mode']
    if sms == 'platform' and state['sms']['payer'] == 'platform':
        sms = 'provided'   # the platform pays by design: nothing for the school to set up
    if 'school' in (email, sms):
        return ("Your own account", GOOD)
    if 'platform' in (email, sms):
        return ('Platform default', WARN)
    if 'incomplete' in (email, sms):
        return ('Half set up', WARN)
    return ('Not connected', WARN)


def _payments_status():
    return ('Connected', GOOD) if payments.payment_settings() is not None else ('Not connected', WARN)


def _session_status():
    current = _school_current_session()
    return (current['name'], NEUTRAL) if current else ('No current session', WARN)


def _report_status():
    from blueprints.school.report_card_data import report_settings
    settings = report_settings()
    done = bool(settings['head_name']) and bool(settings['head_signature'])
    return ('Head set', GOOD) if done else ('Head not set', WARN)


def _alerts_status():
    _, open_controls = _notification_counts(current_admin()['id'])
    return (f'{open_controls} open', WARN) if open_controls else ('All clear', GOOD)


def _groups(me, workspace):
    """The groups of settings this person may open, each a list of entries, empty groups dropped.

    A page that belongs to the other workspace is left out: following it would only bounce the person
    back to the workspace chooser."""
    admin = is_school_admin(me)

    def can(permission):
        return bool(admin_has_permission(me['id'], permission))

    def E(title, text, endpoint, icon, show, status=None, keywords=''):
        required = _admin_workspace_for_path(url_for(endpoint))
        show = show and not (required and workspace and required != workspace)
        return {'title': title, 'text': text, 'endpoint': endpoint, 'icon': icon, 'show': show,
                'status': _safe(status) if status else None, 'keywords': keywords} if show else None

    groups = [
        ('school', 'Your school', 'Name, look and the academic calendar.', [
            E('School profile & branding', 'Name, motto, contact details, logo and colours shown on every page, receipt and report card.',
              'admin_school_branding', 'school', admin, keywords='logo colours motto address phone'),
            E('Academic sessions', 'Name each session, set its dates and choose which one is current. Fees, results and attendance are filed against it.',
              'admin_school_sessions', 'calendar', admin, _session_status, 'term year calendar'),
            E('Entrance examination', 'Set up the entrance examination: its papers, timing and scoring.',
              'admin_entrance_config', 'sliders', can('entrance.config.view'), keywords='exam admission candidates'),
        ]),
        ('results', 'Results & report cards', 'What is printed on a report card.', [
            E('Report card settings', "The head's title, name and signature, and the date the next term begins.",
              'admin_school_report_card_settings', 'report', can('report_cards.manage'), _report_status, 'head teacher signature next term'),
        ]),
        ('communication', 'Communication', 'How parents hear from the school.', [
            E('Email & SMS', "Send receipts, results and notices from the school's own email and text-message accounts.",
              'admin_school_delivery', 'send', admin, _delivery_status, 'smtp mail notices sender'),
        ]),
        ('money', 'Fees & payments', 'Taking money and proving it was taken.', [
            E('Online payments', "The school's own Paystack keys, so parents can pay fees from their portal.",
              'admin_finance_paystack_settings', 'wallet', admin, _payments_status, 'paystack card pay fees'),
            E('Receipts', 'The signature and wording printed on every receipt.',
              'admin_finance_receipt_settings', 'receipt', can('finance.manage'), keywords='receipt signature'),
        ]),
        ('people', 'People & access', 'Who can sign in and what they can do.', [
            E('Administration', 'The control centre for staff accounts, roles and access.',
              'admin_administration', 'shield', admin, keywords='staff access governance'),
            E('Administrators', 'Create staff accounts, suspend access and reset passwords.',
              'admin_accounts', 'users', admin and can('admins.view'), keywords='staff accounts password'),
            E('Staff roles', 'Job-based roles that decide what each member of staff can do.',
              'admin_roles', 'lock', admin, keywords='permissions role'),
        ]),
        ('oversight', 'Controls & activity', 'Keeping an eye on what happens.', [
            E('Controls & alerts', 'Important changes that need attention, and locks on examination resources.',
              'admin_controls', 'alert', can('audit.view'), _alerts_status, 'notifications review lock'),
            E('Activity log', 'Who signed in, what they changed and any access that was refused.',
              'admin_audit_logs', 'list', can('audit.view'), keywords='audit security history'),
            E('Online now', 'Who is signed in to the portal at this moment.',
              'admin_presence', 'presence', can('presence.view'), keywords='presence sessions'),
        ]),
    ]
    out = []
    for key, title, blurb, entries in groups:
        entries = [e for e in entries if e]
        if entries:
            out.append({'key': key, 'title': title, 'blurb': blurb, 'entries': entries})
    return out


@app.route('/admin/settings')
@admin_required
def admin_settings():
    me = current_admin()
    workspace = session.get('admin_workspace')
    groups = _groups(me, workspace)
    return render_template('admin_settings.html', groups=groups, total=sum(len(g['entries']) for g in groups),
                           workspace=workspace)
