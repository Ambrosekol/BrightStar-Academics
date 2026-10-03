"""A school's settings (name, contact details, colours, logo) — read from many
places (the sign-in page, error pages, context processors) so kept out of any
single domain.
"""

from flask import g, has_request_context
from sqlalchemy import select

from models import SchoolPublicSetting, db
from core.db_helpers import tuples


def _public_settings():
    # Public pages must still render on a partially-upgraded database where the
    # settings table does not exist yet, hence the broad guard.
    #
    # Read once per request: the branding helpers ask for one setting at a time, and each ask used
    # to be its own query of the whole table. store_branding clears this when it saves a change.
    if has_request_context() and '_public_settings' in g:
        return g._public_settings
    try:
        settings = {key: value for key, value in tuples(
            select(SchoolPublicSetting.setting_key, SchoolPublicSetting.setting_value))}
    except Exception:
        db.session.rollback()
        settings = {}
    if has_request_context():
        g._public_settings = settings
    return settings


def forget_public_settings():
    """Drop this request's copy after a setting has been written, so the next read sees it."""
    if has_request_context():
        g.pop('_public_settings', None)
