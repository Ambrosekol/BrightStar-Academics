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
from .models import PlatformBase, Tenant, TenantDomain
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
    PlatformBase.metadata.create_all(platform_engine())


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
