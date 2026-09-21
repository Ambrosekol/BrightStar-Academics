"""Access to the platform registry database."""

import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.orm import Session

from . import config
from .context import TenantInfo
from .models import (
    PlatformAdmin, PlatformAuditLog, PlatformBase, ROLE_SUPER, Tenant, TenantDomain,
)
from .routing import build_engine, ensure_database_exists

SLUG_RE = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$')
_LABEL = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
HOSTNAME_RE = re.compile(rf'^(?=.{{1,253}}$){_LABEL}(?:\.{_LABEL})*$')

_engine = None
_engine_url = None
_engine_lock = threading.Lock()

_host_cache = {}
_HOST_CACHE_MAX = 2048


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def platform_engine():
    global _engine, _engine_url
    url = config.platform_db_url()
    with _engine_lock:
        if _engine is None or _engine_url != url:
            if _engine is not None:
                _engine.dispose()
            _engine = build_engine(url)
            _engine_url = url
        return _engine


def dispose_platform_engine():
    global _engine, _engine_url
    with _engine_lock:
        if _engine is not None:
            _engine.dispose()
        _engine = _engine_url = None
    clear_cache()


def init_platform_db():
    """Create the registry database and its tables. Idempotent."""
    try:
        ensure_database_exists(config.platform_db_url())
    except Exception as exc:  # a clear message beats a driver stack trace
        raise RuntimeError(
            f'Could not reach PostgreSQL to create the platform registry: {exc}') from exc
    engine = platform_engine()
    PlatformBase.metadata.create_all(engine)
    _add_missing_columns(engine)
    ensure_superadmin()


def _add_missing_columns(engine):
    """Add columns the models declare but an older registry lacks.

    ``create_all()`` creates missing tables and never alters existing ones, so a
    registry created by an earlier release keeps its old columns. Every column
    added here carries a default, so existing rows are valid straight away.
    """
    inspector = sa.inspect(engine)
    existing = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in PlatformBase.metadata.sorted_tables:
            if table.name not in existing:
                continue
            have = {c['name'] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in have:
                    continue
                spec = column.type.compile(engine.dialect)
                default = column.server_default
                if default is not None:
                    if not column.nullable:
                        spec += ' NOT NULL'
                    spec += f' DEFAULT {default.arg.text}'
                elif not column.nullable:
                    raise RuntimeError(f'Cannot add NOT NULL column {table.name}.{column.name} '
                                       f'without a default to an existing registry.')
                conn.execute(sa.text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {spec}'))


def ensure_superadmin():
    """Make sure that, once any platform admin exists, one of them is the super admin.

    A registry from before roles existed has admins and no super admin; the
    earliest active admin — the person who set the platform up — becomes it.
    Everyone else stays an ordinary platform admin. Does nothing when a super
    admin already exists or there are no admins yet.
    """
    with platform_session() as session:
        if session.scalars(sa.select(PlatformAdmin.id).where(
                PlatformAdmin.role == ROLE_SUPER, PlatformAdmin.active == 1)).first():
            return None
        first = session.scalars(sa.select(PlatformAdmin).where(PlatformAdmin.active == 1)
                                .order_by(PlatformAdmin.id)).first()
        if first is None:
            return None
        first.role = ROLE_SUPER
        session.add(PlatformAuditLog(
            platform_admin_id=first.id, actor_username='system', action='platform_admin.promote',
            detail=f'{first.username} became the super admin: the platform had none.',
            created_at=now_iso()))
        session.commit()
        return first.username


@contextmanager
def platform_session():
    """A registry session that always rolls back unless the caller committed."""
    session = Session(platform_engine(), expire_on_commit=False)
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def normalise_host(host):
    """Lower-case a Host header value and drop the port and any trailing dot."""
    host = (host or '').strip().lower()
    if host.startswith('['):  # IPv6 literal, e.g. [::1]:5000
        host = host[1:].split(']', 1)[0]
    else:
        host = host.split(':', 1)[0]
    return host.rstrip('.')


def validate_slug(slug):
    if not SLUG_RE.match(slug or ''):
        raise ValueError('School code must be 1-40 characters: lower-case letters, digits and hyphens, '
                         'starting and ending with a letter or digit.')
    return slug


def validate_hostname(hostname):
    """A bare hostname for registration. Unlike :func:`normalise_host` (which
    tidies a request's Host header) this refuses a scheme, port or path outright
    rather than quietly reducing "http://x.test/a" to "http"."""
    raw = (hostname or '').strip().lower().rstrip('.')
    if not HOSTNAME_RE.match(raw):
        raise ValueError(f'"{hostname}" is not a valid hostname (no scheme, port or path).')
    return raw


def to_info(tenant):
    return TenantInfo(id=tenant.id, slug=tenant.slug, name=tenant.name, status=tenant.status,
                      db_url=tenant.db_url, db_schema=tenant.db_schema, storage_key=tenant.storage_key)


def clear_cache():
    _host_cache.clear()


def tenant_for_host(host):
    """The school served at ``host``, or None if no school owns that hostname.

    Only positive matches are cached: caching misses would let anyone grow the
    cache without bound by sending arbitrary Host headers.
    """
    host = normalise_host(host)
    if not host:
        return None
    ttl = config.registry_cache_seconds()
    hit = _host_cache.get(host)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    with platform_session() as session:
        tenant = session.scalars(
            sa.select(Tenant).join(TenantDomain, TenantDomain.tenant_id == Tenant.id)
              .where(TenantDomain.hostname == host)).first()
        info = to_info(tenant) if tenant else None
    if info and ttl:
        if len(_host_cache) >= _HOST_CACHE_MAX:
            _host_cache.clear()
        _host_cache[host] = (time.monotonic() + ttl, info)
    return info


def get_tenant(session, slug):
    return session.scalars(sa.select(Tenant).where(Tenant.slug == slug)).first()
