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


# Outside production the platform console also answers on the loopback names.
# Every tunnel and reverse proxy used in development — ngrok, VS Code dev
# tunnels, nginx — forwards to localhost:PORT and rewrites the Host header to
# match, so these are the names the application actually receives. Without
# them, sharing a development server means either a tool-specific flag or an
# environment edit for a hostname that changes on every restart.
LOCAL_PLATFORM_HOSTS = frozenset({'localhost', '127.0.0.1', '::1'})


def platform_hosts():
    """Hostnames that serve the platform console instead of a school.

    Comma-separated, no ports, e.g. ``platform.brightstars.example``. The
    default is a ``.localhost`` name, which browsers resolve to the loopback
    address without any hosts-file edit.

    In production this is exactly what was configured, so a proxy that rewrites
    Host to ``localhost`` is a misconfiguration that shows up immediately rather
    than quietly serving the console somewhere unintended.
    """
    raw = os.environ.get('BRIGHTSTARS_PLATFORM_HOSTS', '').strip() or 'platform.localhost'
    hosts = {h.strip().lower() for h in raw.split(',') if h.strip()}
    if not is_production():
        hosts |= LOCAL_PLATFORM_HOSTS
    return hosts


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
    """Root folder holding each school's own files (question banks, uploads and,
    for SQLite deployments, the database file)."""
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
