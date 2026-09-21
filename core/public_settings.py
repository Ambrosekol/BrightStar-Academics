"""A school's settings (name, contact details, colours, logo) — read from many
places (the sign-in page, error pages, context processors) so kept out of any
single domain.
"""

from sqlalchemy import select

from models import SchoolPublicSetting, db
from core.db_helpers import tuples


def _public_settings():
    # Public pages must still render on a partially-upgraded database where the
    # settings table does not exist yet, hence the broad guard.
    try:
        return {key: value for key, value in tuples(
            select(SchoolPublicSetting.setting_key, SchoolPublicSetting.setting_value))}
    except Exception:
        db.session.rollback()
        return {}
