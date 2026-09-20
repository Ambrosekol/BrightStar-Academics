"""Request hooks that decide which school a request belongs to.

The school is chosen from the request's Host header alone (the school's own
domain), never from anything the visitor can send in the URL, a form or a
cookie. A hostname that no school owns is refused before any application code
runs.
"""

from flask import Response, g, request, session

from . import config
from .context import reset_current_tenant, set_current_tenant
from .registry import init_platform_db, normalise_host, tenant_for_host

# Health checks come from load balancers that address the server by IP or an
# internal name, and the handler touches no database.
_ANYWHERE = ('/health',)

# The platform's own public website, served only on the platform hostnames.
PLATFORM_SITE_PATHS = ('/',)


def _plain(status, title, message):
    body = (f'<!doctype html><meta charset="utf-8"><title>{title}</title>'
            f'<body style="font-family:system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 1rem">'
            f'<h1>{title}</h1><p>{message}</p></body>')
    return Response(body, status=status, mimetype='text/html')


def resolve_tenant():
    if request.path in _ANYWHERE:
        return None
    host = normalise_host(request.host)

    if host in config.platform_hosts():
        g.on_platform_host = True
        # Only the platform console and the shared static assets live here. No
        # school page is ever served without a school, and school uploads are
        # never served from the platform host.
        if request.path == '/platform' or request.path.startswith('/platform/'):
            return None
        if request.path in PLATFORM_SITE_PATHS:
            return None
        if request.path.startswith('/static/') and not request.path.startswith('/static/uploads/'):
            return None
        return _plain(404, 'Not found', 'The requested page does not exist.')

    tenant = tenant_for_host(host)
    if tenant is None:
        return _plain(404, 'Site not found', 'This address is not registered with Brightstars Academics.')
    if not tenant.is_active:
        return _plain(503, 'Temporarily unavailable',
                      'This school portal is currently unavailable. Please contact the school.')

    # A school's address is a portal, not a website. The public marketing pages
    # belong to the platform's own site and are not served for any school.
    if request.path == '/school' or request.path.startswith('/school/'):
        return _plain(404, 'Not found', 'The requested page does not exist.')

    g._tenant_token = set_current_tenant(tenant)
    g.tenant = tenant

    # A signed session cookie is valid on every school because they share one
    # secret key. Stamp each session with its school and discard any that was
    # issued for a different one, so an admin/parent/student cookie copied from
    # school A can never authenticate against school B's differently-numbered
    # accounts.
    if not request.path.startswith('/static/') and session.get('tenant_id') != tenant.id:
        session.clear()
        session['tenant_id'] = tenant.id
    return None


def release_tenant(exc=None):
    token = g.pop('_tenant_token', None)
    if token is None:
        return
    # Close the session while its school is still selected. Flask-SQLAlchemy
    # removes the session on app-context teardown, which runs *after* this; by
    # then the school would be deselected and any last flush/rollback would ask
    # for a bind with no school and raise inside teardown.
    from models.base import db  # deferred: models.base imports this package

    try:
        db.session.remove()
    finally:
        reset_current_tenant(token)


def install(app):
    """Turn on multi-tenancy for ``app``.

    Must run before any other ``before_request`` hook is registered, because
    those hooks query the database and need the school already selected.
    """
    init_platform_db()
    _warn_about_shadowed_domains(app)
    app.before_request(resolve_tenant)
    app.teardown_request(release_tenant)

    @app.context_processor
    def _inject_tenant():
        return {'tenant': g.get('tenant')}


def _warn_about_shadowed_domains(app):
    """A platform hostname always wins over a school's, which would silently
    take that school offline. Say so loudly at start-up rather than leaving it
    to be discovered by the school."""
    for host in sorted(config.platform_hosts()):
        if tenant_for_host(host) is not None:
            app.logger.warning(
                'BRIGHTSTARS_PLATFORM_HOSTS contains %s, which is also registered to a school. '
                'The platform console wins, so that school is unreachable on that hostname.', host)
