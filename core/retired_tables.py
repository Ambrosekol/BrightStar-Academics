"""Tables that belonged to features which no longer exist.

The school website editor was removed (a school's address is a portal, not a website), but a
school database made before that still holds the editor's three tables. Nothing reads or writes
them now. They are cleared away like this:

* When a school is upgraded, a retired table that is EMPTY is dropped. Nothing can be lost.
* One that still holds rows is kept, and a warning says so. Someone decides about data.
  ``python -m control_plane drop-retired-tables`` shows what each school still holds; adding
  ``--yes`` drops them for good.
"""

import logging

import sqlalchemy as sa

from control_plane.context import tenant_context
from models import db

logger = logging.getLogger(__name__)

# The website editor's pages, news posts and enquiries.
RETIRED_TABLES = ('school_public_pages', 'school_public_news', 'school_public_enquiries')


def counts():
    """``{table: number of rows}`` for each retired table this school still has."""
    inspector = sa.inspect(db.session.connection())
    found = {}
    for table in RETIRED_TABLES:
        if inspector.has_table(table):
            # The names above are constants, never user input, so building the query is safe.
            found[table] = db.session.execute(sa.text(f'SELECT COUNT(*) FROM "{table}"')).scalar() or 0
    return found


def _drop(tables):
    for table in tables:
        db.session.execute(sa.text(f'DROP TABLE IF EXISTS "{table}"'))


def drop_empty():
    """Drop the retired tables that hold nothing. Returns the names dropped."""
    held = counts()
    empty = [table for table, rows in held.items() if rows == 0]
    _drop(empty)
    for table, rows in held.items():
        if rows:
            logger.warning('The retired table %s still holds %s row(s) and was left alone. '
                           'See "python -m control_plane drop-retired-tables".', table, rows)
    return empty


def for_school(info, drop=False):
    """What one school still holds, and (with ``drop``) remove every retired table it has.

    Returns ``{table: number of rows it held}``: what was found, before any drop.
    """
    import app as A  # deferred: the app imports this package at start-up

    with A.app.app_context(), tenant_context(info):
        held = counts()
        if drop and held:
            _drop(held)
            db.session.commit()
        return held
