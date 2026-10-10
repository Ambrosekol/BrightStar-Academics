"""The Settings workspace: one place that holds every setting, control and record of activity in the portal.

Settings opens on an overview (what needs attention, then every setting by group). Every page it leads to is
shown inside the same frame: the list of settings stays on the left and the page opens beside it, in place
(static/inplace.js), so nobody leaves Settings to change one. Forms that create or edit something (a member of
staff, a role) open in pop-ups over it.

Nothing is stored here. Each entry links to the page that already owns that setting, shows the one fact worth
knowing at a glance (is it connected, which session is current, how many alerts are open), and appears only for
people who may open the page behind it - the same permission and school-administrator rules the pages
themselves enforce. That is what lets the menu carry a single "Settings" entry instead of a dozen.
"""

from flask import g, render_template, request, session, url_for

from app import _admin_workspace_for_path, _notification_counts, _school_current_session, app
from core import delivery, payments
from core.security import admin_has_permission, admin_required, current_admin, is_school_admin

NEUTRAL, GOOD, WARN = 'neutral', 'good', 'warn'
SETTINGS_HOME = '/admin/settings'


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


def _groups(me, workspace, statuses=True):
    """The groups of settings this person may open, each a list of entries, empty groups dropped.

    A page that belongs to the other workspace is left out: following it would only bounce the person
    back to the workspace chooser. With statuses=False nothing is looked up beyond permissions (the menu
    asks on every page only which addresses Settings covers)."""
    admin = is_school_admin(me)

    def can(permission):
        return bool(admin_has_permission(me['id'], permission))

    def E(title, text, endpoint, icon, show, status=None, keywords='', inplace=True, action=None):
        if not show:
            return None
        path = url_for(endpoint)
        required = _admin_workspace_for_path(path)
        if required and workspace and required != workspace:
            return None
        return {'title': title, 'text': text, 'endpoint': endpoint, 'path': path, 'icon': icon,
                'status': _safe(status) if (status and statuses) else None, 'keywords': keywords,
                'inplace': inplace, 'action': action}

    groups = [
        ('school', 'Your school', 'Name, look and the academic calendar.', [
            E('School profile & branding', 'Name, motto, contact details, logo and colours shown on every page, receipt and report card.',
              'admin_school_branding', 'set-branding', admin, keywords='logo colours motto address phone'),
            E('Academic sessions', 'Name each session, set its dates and choose which one is current. Fees, results and attendance are filed against it.',
              'admin_school_sessions', 'set-sessions', admin, _session_status, 'term year calendar weighting'),
            # The entrance examination's set-up is a whole workspace of its own: it opens as its own page.
            E('Entrance examination', 'Set up the entrance examination: its papers, timing and scoring.',
              'admin_entrance_config', 'nav-examconfig', can('entrance.config.view'), keywords='exam admission candidates',
              inplace=False),
        ]),
        ('people', 'Staff & access', 'Who can sign in and what they can do.', [
            E('Staff', 'Add teachers and office staff, give them roles and teaching duties, suspend access and reset passwords.',
              'admin_accounts', 'set-staff', admin and can('admins.view'), keywords='staff teachers accounts password login',
              action=('Add staff', 'admin_account_new') if can('admins.create') else None),
            E('Staff roles', 'Job-based roles that decide what each member of staff can do.',
              'admin_roles', 'set-roles', admin and can('roles.view'), keywords='permissions role access',
              action=('New role', 'admin_role_new') if can('roles.create') else None),
        ]),
        ('results', 'Results & report cards', 'What is printed on a report card.', [
            E('Report card settings', "The head's title, name and signature, and the date the next term begins.",
              'admin_school_report_card_settings', 'nav-reportcard', can('report_cards.manage'), _report_status, 'head teacher signature next term'),
        ]),
        ('communication', 'Communication', 'How parents hear from the school.', [
            E('Email & SMS', "Send receipts, results and notices from the school's own email and text-message accounts.",
              'admin_school_delivery', 'set-delivery', admin, _delivery_status, 'smtp mail notices sender text'),
        ]),
        ('money', 'Fees & payments', 'Taking money and proving it was taken.', [
            E('Online payments', "The school's own Paystack keys, so parents can pay fees from their portal.",
              'admin_finance_paystack_settings', 'nav-payments', admin, _payments_status, 'paystack card pay fees'),
            E('Receipts', 'The signature printed on every receipt.',
              'admin_finance_receipt_settings', 'set-receipt', can('finance.manage'), keywords='receipt signature'),
        ]),
        ('oversight', 'Controls & activity', 'Keeping an eye on what happens.', [
            E('Controls & alerts', 'Important changes that need attention, and locks on examination resources.',
              'admin_controls', 'set-controls', can('audit.view'), _alerts_status, 'notifications review lock'),
            E('Activity & notice logs', 'Who signed in, what they changed and any access refused; and every email and text sent to parents.',
              'admin_audit_logs', 'set-history', can('audit.view'), keywords='audit security history notice sms email delivered'),
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


def settings_scope():
    """The addresses Settings shows in place, for the menu's data-inplace-scope (only those this person may open)."""
    me = current_admin()
    if not me:
        return []
    cached = getattr(g, '_settings_scope', None)
    if cached is None:
        groups = _groups(me, session.get('admin_workspace'), statuses=False)
        cached = ['=' + SETTINGS_HOME] + [e['path'] for grp in groups for e in grp['entries'] if e['inplace']] if groups else []
        g._settings_scope = cached
    return cached


def settings_nav():
    """The Settings frame for this page, or None when the page is not one of Settings' own.

    Gives the groups (with statuses) for the list on the left, which entry is open, and what needs attention."""
    me = current_admin()
    if not me:
        return None
    path = request.path
    paths = settings_scope()
    if path != SETTINGS_HOME and not any(p[0] != '=' and (path == p or path.startswith(p + '/')) for p in paths):
        return None
    cached = getattr(g, '_settings_nav', None)
    if cached is None:
        groups = _groups(me, session.get('admin_workspace'))
        entries = [e for grp in groups for e in grp['entries']]
        current = max((e for e in entries if e['inplace'] and (path == e['path'] or path.startswith(e['path'] + '/'))),
                      key=lambda e: len(e['path']), default=None)
        cached = {'groups': groups, 'current': current, 'total': len(entries),
                  'attention': [e for e in entries if e['status'] and e['status'][1] == WARN]}
        g._settings_nav = cached
    return cached


# Whole words only, and never the School Admin, which keeps its name.
_STAFF_WORDS = [(r'\ban administrator account\b', 'a staff account'), (r'\ban administrator\b', 'a staff member'),
                (r'\bordinary administrator accounts\b', 'staff accounts'), (r'\badministrator accounts\b', 'staff accounts'),
                (r'\badministrator types\b', 'staff roles'), (r'\badmin types\b', 'staff roles'), (r'\badmin type\b', 'staff role'),
                (r'\badministrators\b', 'staff'), (r'\badministrator\b', 'staff member'),
                (r'(?<!school )\badmins\b', 'staff'), (r'(?<!school )\badmin\b', 'staff')]


def staffwords(text):
    """Permission names were written when staff were called administrators; say staff instead, keeping the capital."""
    import re
    out = str(text or '')
    for pattern, word in _STAFF_WORDS:
        out = re.sub(pattern, lambda m, w=word: w[:1].upper() + w[1:] if m.group(0)[:1].isupper() else w, out, flags=re.I)
    return out


app.jinja_env.globals.update(settings_nav=settings_nav, settings_scope=settings_scope, staffwords=staffwords)


@app.route('/admin/settings')
@admin_required
def admin_settings():
    nav = settings_nav() or {'groups': [], 'attention': [], 'total': 0, 'current': None}
    return render_template('admin_settings.html', groups=nav['groups'], total=nav['total'],
                           attention=nav['attention'], workspace=session.get('admin_workspace'))
