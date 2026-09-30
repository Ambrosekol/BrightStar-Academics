"""Per-request database routing: every school has its own database.

``TenantSession`` replaces Flask-SQLAlchemy's session class. When multi-tenancy
is on, it hands each query the engine of the school selected for the request,
so the existing ``db.session`` code throughout the blueprints needs no change
and no query can accidentally reach another school's data.

PostgreSQL: a school is either its own database (``db_url`` only) or a schema
inside a shared database (``db_url`` + ``db_schema``, applied as the
connection's ``search_path``). Both are built here; nothing else in the code
base needs to know which one a school uses.
"""

import os
import re
import threading

import sqlalchemy as sa
from flask_sqlalchemy.session import Session
from sqlalchemy.engine import make_url

from . import config
from .context import current_tenant

_SCHEMA_RE = re.compile(r'^[a-z_][a-z0-9_]{0,62}$')

_engines = {}
_engines_lock = threading.Lock()


def resolve_db_url(value):
    """Return a usable URL, reading ``env:NAME`` values from the environment."""
    value = (value or '').strip()
    if value.lower().startswith('env:'):
        name = value[4:].strip()
        resolved = os.environ.get(name, '').strip()
        if not resolved:
            raise RuntimeError(f'Environment variable {name} is not set; it holds a school database URL.')
        return resolved
    return value


def build_engine(db_url, db_schema=None, connect_timeout=None, pool_size=None, max_overflow=None):
    """Create an engine with the options appropriate to the database backend.

    ``connect_timeout`` (seconds, PostgreSQL only) stops a database that is not
    answering from holding a request for minutes: without it the operating system
    decides how long a connection attempt may hang, which can be over two minutes.

    One engine (with its own connection pool) is cached per school - see ``engine_for`` - so
    SQLAlchemy's own single-app defaults (a pool of 5, up to 10 more on top) would let a hundred
    schools open a thousand-plus connections per worker process just sitting idle. Every school
    here is one admin office at a time, not a high-traffic site of its own, so a school's own pool
    defaults small (BRIGHTSTARS_DB_POOL_SIZE / BRIGHTSTARS_DB_MAX_OVERFLOW raise it for a deployment
    that genuinely needs more); the one shared registry engine, asked on nearly every request
    regardless of school (control_plane/registry.py), passes its own larger numbers explicitly
    instead. Per recommendations.html's Scale table.
    """
    url = make_url(resolve_db_url(db_url))
    options = {}
    if url.get_backend_name() == 'sqlite':
        # Same options app.py applies to the single-school database.
        options['connect_args'] = {'timeout': 10, 'check_same_thread': False}
    else:
        options['pool_pre_ping'] = True
        options['pool_size'] = pool_size if pool_size is not None else int(
            os.environ.get('BRIGHTSTARS_DB_POOL_SIZE', '2'))
        options['max_overflow'] = max_overflow if max_overflow is not None else int(
            os.environ.get('BRIGHTSTARS_DB_MAX_OVERFLOW', '3'))
        if db_schema:
            if not _SCHEMA_RE.match(db_schema):
                raise ValueError(f'Invalid database schema name: {db_schema!r}')
            if url.get_backend_name() == 'postgresql':
                options['connect_args'] = {'options': f'-csearch_path={db_schema}'}
        if connect_timeout and url.get_backend_name() == 'postgresql':
            options.setdefault('connect_args', {})['connect_timeout'] = int(connect_timeout)
    return sa.create_engine(url, **options)


def ensure_database_exists(db_url):
    """Create the PostgreSQL database named in ``db_url`` if it is missing.

    Tried directly first: the large majority of the time the database is already
    there (every deployment's registry database, and a school's own database once
    it has been created once), and connecting to it needs nothing else to exist on
    the server. Only a database that is genuinely missing falls through to
    CREATE DATABASE, which cannot run inside a transaction, so that goes through a
    separate connection to the server's maintenance database, in autocommit.

    A stock PostgreSQL install always has one built in, named ``postgres`` (the
    default here), but a managed provider does not always ship one under that
    name - Aiven's default database is ``defaultdb``, for instance - so
    ``BRIGHTSTARS_PG_MAINTENANCE_DB`` names it when it is not called ``postgres``.
    Every database name here is built from our own validated slug, and is quoted
    regardless.
    """
    url = make_url(resolve_db_url(db_url))
    if url.get_backend_name() != 'postgresql':
        return
    try:
        probe = sa.create_engine(url, isolation_level='AUTOCOMMIT')
        try:
            with probe.connect():
                return  # already there - no maintenance connection needed at all
        finally:
            probe.dispose()
    except sa.exc.OperationalError:
        pass
    engine = sa.create_engine(url.set(database=config.pg_maintenance_db()), isolation_level='AUTOCOMMIT')
    try:
        with engine.connect() as conn:
            exists = conn.execute(sa.text('SELECT 1 FROM pg_database WHERE datname = :n'),
                                  {'n': url.database}).scalar()
            if not exists:
                conn.execute(sa.text(f'CREATE DATABASE "{url.database}"'))
    finally:
        engine.dispose()


def engine_for(tenant):
    """The (cached) engine for one school."""
    key = (tenant.db_url, tenant.db_schema)
    engine = _engines.get(key)
    if engine is None:
        with _engines_lock:
            engine = _engines.get(key)
            if engine is None:
                engine = _engines[key] = build_engine(tenant.db_url, tenant.db_schema)
    return engine


def dispose_engines():
    """Close every cached school engine (tests, and graceful shutdown)."""
    with _engines_lock:
        for engine in _engines.values():
            engine.dispose()
        _engines.clear()


class TenantSession(Session):
    """Flask-SQLAlchemy session that binds to the current school's database."""

    def get_bind(self, mapper=None, clause=None, bind=None, **kwargs):
        if bind is not None:
            return bind
        # No fallback: with no school selected this raises rather than quietly
        # using the default bind, which is the platform registry and holds no
        # school data at all.
        return engine_for(current_tenant())


def current_engine():
    """The engine of the school being served (or the single database when
    multi-tenancy is off). Use instead of ``db.engine``, which always names the
    default database."""
    from models.base import db  # deferred: models.base imports this module

    return db.session.get_bind()
