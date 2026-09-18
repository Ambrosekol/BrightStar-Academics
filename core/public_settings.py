"""The public school website's settings and page lookups — read from many
places (the public site itself, the admin website editor, error pages,
context processors) so kept out of any single domain.
"""

from sqlalchemy import select

from models import SchoolPublicPage, SchoolPublicSetting, db
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


def _public_page(slug):
    return db.session.scalars(select(SchoolPublicPage).where(
        SchoolPublicPage.slug==slug, SchoolPublicPage.published==1)).first()
