"""Generic SQLAlchemy query helpers used throughout every domain.

The application and its templates were written against ``sqlite3.Row``, so
the read helpers here return ``RowMapping`` objects, which still support
``row['column']`` access.

This module also holds the few constructs that SQLAlchemy does not spell the
same way on every backend — upserts and string aggregation. They pick their
form from the database the current school actually lives on, so the same code
runs on PostgreSQL (production) and on SQLite.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as _pg_insert
from sqlalchemy.dialects.sqlite import insert as _sqlite_insert

from models import db


def dialect_name():
    """The backend the current school's database is on."""
    return db.session.get_bind().dialect.name


def insert_stmt(model):
    """An INSERT that supports ``.on_conflict_do_nothing()`` and
    ``.on_conflict_do_update()``.

    PostgreSQL and SQLite both offer ON CONFLICT with the same SQLAlchemy API,
    but each lives in its own dialect module, so the construct has to be chosen
    for the database actually in use.
    """
    return _pg_insert(model) if dialect_name() == 'postgresql' else _sqlite_insert(model)


def group_concat(column, separator=','):
    """Join a column's values across a GROUP BY into one delimited string.

    SQLite spells this ``group_concat``; PostgreSQL spells it ``string_agg`` and
    insists on text, so the column is cast for it.
    """
    if dialect_name() == 'postgresql':
        return sa.func.string_agg(sa.cast(column, sa.Text), separator)
    return sa.func.group_concat(column, separator)


def one(stmt):
    """First row as a mapping, or None."""
    return db.session.execute(stmt).mappings().first()


def one_scalar(stmt, default=None):
    """First column of the first row, or `default` when there is no value."""
    value = db.session.execute(stmt).scalar()
    return default if value is None else value


def all_rows(stmt):
    """Every row as a mapping."""
    return db.session.execute(stmt).mappings().all()


def tuples(stmt):
    """Every row as a plain tuple, for call sites that destructure."""
    return db.session.execute(stmt).all()


def obj(model, pk):
    """Load a single mapped instance by primary key."""
    if pk is None:
        return None
    return db.session.get(model, pk)


def _flatten(row, entity_key, *extra_keys):
    """Flatten a (entity, extra columns...) row into a single dict.

    Queries that select a whole model alongside a few joined columns produce a
    row whose first element is the instance. Templates expect one flat mapping,
    so merge the instance's columns with the extras.
    """
    if row is None:
        return None
    entity = row[entity_key]
    merged = {c.key: getattr(entity, c.key) for c in entity.__mapper__.column_attrs}
    for key in extra_keys:
        merged[key] = row[key]
    return merged


def _ignore_insert(model, rows):
    """INSERT ... ON CONFLICT DO NOTHING, the SQLAlchemy form of INSERT OR IGNORE."""
    if not rows:
        return
    db.session.execute(insert_stmt(model).on_conflict_do_nothing(), rows)
