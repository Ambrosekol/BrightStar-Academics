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


def is_production():
    return os.environ.get('BRIGHTSTARS_ENV',
                          os.environ.get('FLASK_ENV', 'development')).strip().lower() in ('production', 'prod')


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
