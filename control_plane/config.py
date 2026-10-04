"""Environment-driven settings for the Brightstars Academics control plane.

Read lazily (each call) rather than at import time so a test or verification
script can set the environment after importing the package.

Production target is PostgreSQL. Every value here is a full SQLAlchemy URL or a
plain string, never a filesystem path that only makes sense for SQLite, so
moving the platform registry and the school databases to PostgreSQL is a
configuration change, not a code change. See docs/architecture/MULTI_TENANCY.md.
"""

import os

from dotenv import load_dotenv

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Load .env here rather than only in app.py, so `python -m control_plane ...`
# and the verification scripts see the same configuration the server does.
# A variable already set in the real environment still wins.
load_dotenv(os.path.join(BASE, '.env'))


def platform_db_url():
    """Where the platform registry (schools, addresses, platform admins) lives.

    PostgreSQL, e.g. ``postgresql+psycopg://user:pass@host/brightstars_platform``.
    Required: there is no default, because a wrong guess here would silently
    create an empty registry and make every school look as if it did not exist.
    """
    url = os.environ.get('BRIGHTSTARS_PLATFORM_DB', '').strip()
    if not url:
        raise RuntimeError(
            'BRIGHTSTARS_PLATFORM_DB is not set. It is the PostgreSQL URL of the platform '
            'registry, e.g. postgresql+psycopg://user:password@localhost:5432/brightstars_platform. '
            'See docs/architecture/MULTI_TENANCY.md.')
    return url


def school_db_url(slug):
    """The URL for one school's own database.

    Schools live in their own database on the same server as the registry, named
    after the school, unless a per-school URL was given when it was created.
    """
    template = os.environ.get('BRIGHTSTARS_SCHOOL_DB_TEMPLATE', '').strip()
    if template:
        return template.replace('{slug}', slug).replace('{db}', school_db_name(slug))
    from sqlalchemy.engine import make_url

    return make_url(platform_db_url()).set(database=school_db_name(slug)).render_as_string(
        hide_password=False)


def school_db_name(slug):
    """The database name for a school. PostgreSQL identifiers cannot contain a
    hyphen unquoted, so the school's code is normalised."""
    return 'brightstars_' + slug.replace('-', '_')


def pg_maintenance_db():
    """The database CREATE DATABASE runs against when a school's (or the registry's) own
    database does not exist yet (control_plane/routing.py's ``ensure_database_exists``).

    A stock PostgreSQL install always has one called ``postgres``, which is the default
    here. A managed provider does not always ship one under that name - Aiven's default
    database is ``defaultdb``, for instance - so set this to whatever database is
    guaranteed to exist on your server when ``postgres`` is not it.
    """
    return os.environ.get('BRIGHTSTARS_PG_MAINTENANCE_DB', '').strip() or 'postgres'


def branding_cache_seconds():
    """How long a school's computed branding (``core/branding.py``'s ``school_brand()``) is
    cached in each process. Read on every page but rarely changed, so caching it cuts several
    database round-trips off every request. Saving a school's branding clears that one school's
    entry immediately in the worker that handled the save; this bounds how long a *different*
    worker process, which never saw the save, keeps serving the old values. Zero disables it.
    """
    try:
        return max(0.0, float(os.environ.get('BRIGHTSTARS_BRANDING_CACHE_SECONDS', '10')))
    except ValueError:
        return 10.0


def upload_cache_bytes():
    """How many bytes of uploaded-file content each worker process keeps in memory
    (``BRIGHTSTARS_STORAGE_BACKEND=s3`` only), so a second request for the same image - by any
    visitor, not just the one who asked first - is answered without going back to the bucket.

    An upload's own path never changes what it holds (core/storage.py never overwrites one in
    place - a new upload is a new path), so unlike the other two caches there is nothing to
    expire on a timer here: only least-recently-used eviction once this many bytes are held, and
    deleting an upload drops its own entry immediately. 0 disables it.
    """
    try:
        return max(0, int(os.environ.get('BRIGHTSTARS_UPLOAD_CACHE_BYTES', str(64 * 1024 * 1024))))
    except ValueError:
        return 64 * 1024 * 1024


def is_production():
    return os.environ.get('BRIGHTSTARS_ENV',
                          os.environ.get('FLASK_ENV', 'development')).strip().lower() in ('production', 'prod')


def storage_backend():
    """Where a school's uploads, question banks and numbering rules are kept:
    ``'local'`` (the default), a folder on this machine under ``tenants_dir()``, or
    ``'s3'``, an S3-compatible object store (AWS S3 itself, or Cloudflare R2, Backblaze B2,
    DigitalOcean Spaces and MinIO by pointing ``BRIGHTSTARS_S3_ENDPOINT_URL`` at them).
    See docs/architecture/MULTI_TENANCY.md.
    """
    value = os.environ.get('BRIGHTSTARS_STORAGE_BACKEND', 'local').strip().lower()
    if value not in ('local', 's3'):
        raise RuntimeError(
            f'BRIGHTSTARS_STORAGE_BACKEND is "{value}", but only "local" or "s3" is understood.')
    return value


def s3_bucket():
    value = os.environ.get('BRIGHTSTARS_S3_BUCKET', '').strip()
    if not value:
        raise RuntimeError('BRIGHTSTARS_S3_BUCKET is not set. Required when BRIGHTSTARS_STORAGE_BACKEND=s3.')
    return value


def s3_endpoint_url():
    """None for real AWS S3; a URL for an S3-compatible provider (Cloudflare R2, Backblaze
    B2, DigitalOcean Spaces, MinIO, ...)."""
    return os.environ.get('BRIGHTSTARS_S3_ENDPOINT_URL', '').strip() or None


def s3_region():
    return os.environ.get('BRIGHTSTARS_S3_REGION', '').strip() or 'auto'


def s3_access_key_id():
    value = os.environ.get('BRIGHTSTARS_S3_ACCESS_KEY_ID', '').strip()
    if not value:
        raise RuntimeError('BRIGHTSTARS_S3_ACCESS_KEY_ID is not set. Required when BRIGHTSTARS_STORAGE_BACKEND=s3.')
    return value


def s3_secret_access_key():
    value = os.environ.get('BRIGHTSTARS_S3_SECRET_ACCESS_KEY', '').strip()
    if not value:
        raise RuntimeError(
            'BRIGHTSTARS_S3_SECRET_ACCESS_KEY is not set. Required when BRIGHTSTARS_STORAGE_BACKEND=s3.')
    return value


def s3_key_prefix():
    """An optional prefix every school's files are namespaced under in the bucket, so one
    bucket can be shared with other uses. Empty, or ending in '/'."""
    prefix = os.environ.get('BRIGHTSTARS_S3_PREFIX', '').strip().strip('/')
    return f'{prefix}/' if prefix else ''


def platform_hosts():
    """Hostnames that serve the platform console instead of a school.

    Comma-separated, no ports, e.g. ``platform.brightstars.example``. The
    default is a ``.localhost`` name, which browsers resolve to the loopback
    address without any hosts-file edit.
    """
    raw = os.environ.get('BRIGHTSTARS_PLATFORM_HOSTS', '').strip() or 'platform.localhost'
    return {h.strip().lower() for h in raw.split(',') if h.strip()}


def portal_domain():
    """The domain every school's portal URL is generated under.

    A school created with the code ``alpha`` is reachable immediately at
    ``alpha.<portal_domain>``, with no DNS work by the school. The school's own
    address is then pointed at that name with a CNAME record.

    The default suits local development: ``alpha.localhost`` resolves to the
    loopback address in current browsers with no hosts-file edit.
    """
    return os.environ.get('BRIGHTSTARS_PORTAL_DOMAIN', '').strip().lower().strip('.') or 'localhost'


def portal_hostname(slug):
    return f'{slug}.{portal_domain()}'


def tenants_dir():
    """Root folder holding each school's own files (question banks, uploads and
    its numbering rules)."""
    return os.environ.get('BRIGHTSTARS_TENANTS_DIR', '').strip() or os.path.join(BASE, 'tenants')


def password_check_cache_seconds():
    """How long a signed-in person's password fingerprint (``core/session_guard.py``) is cached
    in each process, once read from the database.

    Every request from a signed-in person is checked against this so a changed password ends
    their other sign-ins; done on every single request with no caching, that is a database
    round-trip most pages did not otherwise need. Caching it means a password change (by the
    person, an administrator, or a reset link) can take up to this long to sign out a session
    other than the one that changed it; the person's own session is refreshed the moment they
    change it (``refresh_password_stamp``), so they are never caught by their own change. Zero
    disables the cache.
    """
    try:
        return max(0.0, float(os.environ.get('BRIGHTSTARS_PASSWORD_CHECK_CACHE_SECONDS', '30')))
    except ValueError:
        return 30.0


def pg_dump_override():
    """The full path of a ``pg_dump`` program named by ``BRIGHTSTARS_PG_DUMP``, or ''.

    Used by school exports (control_plane/provisioning.py's ``find_pg_dump``) ahead of the
    automatic search, for a machine whose PostgreSQL client tools are not in a standard place.
    """
    return os.environ.get('BRIGHTSTARS_PG_DUMP', '').strip()


def admin_cache_seconds():
    """How long a signed-in administrator's account row is kept in each process after it is first read.

    Every page an administrator opens asks who they are, and their permissions. Without this cache that
    is a database round-trip per request. A change to an account (a deactivation, a new role) reaches a
    request within this many seconds. Zero reads the row on every request.
    """
    try:
        return max(0.0, float(os.environ.get('BRIGHTSTARS_ADMIN_CACHE_SECONDS', '15')))
    except ValueError:
        return 15.0


def badge_cache_seconds():
    """How long the counts shown on every admin page (unread notifications, open controls, unallocated
    payments) are kept in each process. A count can therefore lag a change by this many seconds.
    Zero counts on every page."""
    try:
        return max(0.0, float(os.environ.get('BRIGHTSTARS_BADGE_CACHE_SECONDS', '10')))
    except ValueError:
        return 10.0


def school_engine_idle_seconds():
    """How long a school's database engine may go unused before its pooled connections are closed.

    Each school has its own engine and pool (control_plane/routing.py). A pool keeps its idle
    connections open indefinitely, so a school that is quiet for a day still holds its connections
    on the database server; across many schools and worker processes that, not the queries, is what
    exhausts PostgreSQL's max_connections. A school's engine is closed and rebuilt on its next use.
    Zero disables this.
    """
    try:
        return max(0.0, float(os.environ.get('BRIGHTSTARS_SCHOOL_ENGINE_IDLE_SECONDS', '600')))
    except ValueError:
        return 600.0


def registry_cache_seconds():
    """How long a hostname -> school lookup is cached in each process.

    This bounds how long a suspension takes to reach a worker that has already
    seen the school. Zero disables the cache.
    """
    try:
        return max(0.0, float(os.environ.get('BRIGHTSTARS_REGISTRY_CACHE_SECONDS', '10')))
    except ValueError:
        return 10.0


MAX_TRUSTED_PROXIES = 5


def trusted_proxies():
    """How many reverse proxies sit in front of the application (``BRIGHTSTARS_TRUSTED_PROXIES``).

    0, the default, means none: the address a request appears to come from is the connection's own,
    and the ``X-Forwarded-*`` headers are ignored, because anyone can send them. A number of 1 or
    more says that many proxies of ours stand in front, so the address and the scheme (http or
    https) they report can be believed (see app.py, ``ProxyFix``). Anything else refuses to start,
    since a wrong guess here either records forged addresses or records the proxy's address for
    every visitor.
    """
    raw = os.environ.get('BRIGHTSTARS_TRUSTED_PROXIES', '').strip()
    if not raw:
        return 0
    try:
        count = int(raw)
    except ValueError:
        count = -1
    if not 0 <= count <= MAX_TRUSTED_PROXIES:
        raise RuntimeError(
            f'BRIGHTSTARS_TRUSTED_PROXIES is "{raw}", which is not a number this application can use. '
            f'Set it to how many reverse proxies stand in front of the application: 0 (the default, '
            f'none) or a whole number up to {MAX_TRUSTED_PROXIES}.')
    return count
