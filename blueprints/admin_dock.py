"""The panel at the bottom right of every administrator page.

It holds up to two cards, each one minimisable and closable:

* what's new: the staff release notes (core/release_notes.py). The notes are sent with the page and the
  browser decides which ones to show, from the newest version this administrator has dismissed in that
  browser. No database read happens on an ordinary page load.
* the new-school setup checklist. Its progress is read from the school's data only when the browser's
  saved copy is older than the refresh interval (see SETUP_REFRESH_MINUTES in the page script), so the
  database is not asked on every page. Closing it is the school's existing dismiss, so it hides for the
  whole school, as it always did.

Minimising a card is remembered in that browser only.
"""

from flask import has_request_context, render_template

from app import app
from core.release_notes import APP_VERSION, releases_for
from core.security import admin_has_permission, admin_required, current_admin, csrf_protect
from blueprints.school.onboarding import onboarding_status
from models import db


@app.context_processor
def admin_dock_context():
    """The notes and identifiers the dock needs. Nothing here reads the database."""
    if not has_request_context():
        return {'dock': None}
    me = current_admin()
    if not me:
        return {'dock': None}
    return {'dock': {
        'account': me['id'],
        'app_version': APP_VERSION,
        'releases': releases_for('staff'),
        'show_setup': bool(admin_has_permission(me['id'], 'school.view')),
    }}


@app.get('/admin/school/onboarding/card')
@admin_required
def admin_school_onboarding_card():
    """The setup checklist's body. Only asked for when the browser's saved copy has expired."""
    me = current_admin()
    if not admin_has_permission(me['id'], 'school.view'):
        return ('', 403)
    try:
        status = onboarding_status()
    except Exception:
        db.session.rollback()
        app.logger.exception('Could not read the setup checklist')
        return ('', 500)
    return render_template('_setup_card_body.html', ob=status)
