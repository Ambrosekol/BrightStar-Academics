"""Request hooks that decide which school a request belongs to.

The school is chosen from the request's Host header alone (the school's own
domain), never from anything the visitor can send in the URL, a form or a
cookie. A hostname that no school owns is refused before any application code
runs.
"""

from urllib.parse import urlsplit

from flask import Response, current_app, g, request, session

from . import config
from .context import reset_current_tenant, set_current_tenant
from .registry import init_platform_db, normalise_host, tenant_for_host

# Health checks come from load balancers that address the server by IP or an
# internal name, and the handler touches no database.
_ANYWHERE = ('/health',)
# The brand images are public and touch no school, so the "no such address" page
# can show the logo on an address that belongs to nobody.
_ANYWHERE_PREFIXES = ('/static/brand/',)

# On a platform hostname "/" only redirects into the console; there is no page.
PLATFORM_ROOT_PATHS = ('/',)


# What to say when a request cannot be served, and why. Each becomes a designed
# page (templates/platform/notice.html) instead of a bare line of text.
_NOTICES = {
    'unknown_host': (404, 'Address not found', "We couldn't find that address.",
                     [('Check the spelling', 'A single wrong letter is enough to land somewhere else.'),
                      ('Use the address your school gave you',
                       'Every school has its own portal address. It is the only place its people sign in.'),
                      ('Still stuck?', 'Ask your school office, or the person who runs Brightstars Academics for you.')]),
    'suspended': (503, 'Portal paused', 'This portal is temporarily unavailable.',
                  [('Nothing has been lost', "The school's records are safe and untouched."),
                   ('Contact the school office', 'They can tell you when the portal will be back.')]),
    'not_found': (404, 'Page not found', "There's nothing at this address.",
                  [('Check the address', 'The page may have moved, or the link may be mistyped.'),
                   ('Start again', 'Go back to the start and follow the links from there.')]),
}


def _notice(kind, host=None):
    """A designed page for a request that can be refused before any school is chosen.

    Rendered straight from the template, deliberately bypassing ``render_template``:
    that runs every context processor, and those look up the signed-in user in a
    school database — which is exactly what does not exist on this kind of request.
    """
    status, eyebrow, title, steps = _NOTICES[kind]
    lead = None
    if kind == 'unknown_host' and host:
        lead = 'The address you asked for is not registered with Brightstars Academics.'
    elif kind == 'suspended':
        lead = "This school's portal has been switched off for the moment."
    dev = None
    if not config.is_production():
        # On a developer's machine the usual cause is typing plain "localhost".
        port = urlsplit(f'//{request.host}').port
        console = sorted(config.platform_hosts())[0] + (f':{port}' if port else '')
        dev = {'console': f'{request.scheme}://{console}/',
               'school': f'{request.scheme}://<school-code>.{config.portal_domain()}' + (f':{port}' if port else '') + '/'}
    html = current_app.jinja_env.get_template('platform/notice.html').render(
        status=status, eyebrow=eyebrow, title=title, lead=lead, host=host if kind == 'unknown_host' else None,
        steps=steps, dev=dev)
    return Response(html, status=status, mimetype='text/html')


def resolve_tenant():
    if request.path in _ANYWHERE or request.path.startswith(_ANYWHERE_PREFIXES):
        return None
    host = normalise_host(request.host)

    if host in config.platform_hosts():
        g.on_platform_host = True
        # Only the platform console and the shared static assets live here. No
        # school page is ever served without a school, and school uploads are
        # never served from the platform host.
        if request.path == '/platform' or request.path.startswith('/platform/'):
            return None
        if request.path in PLATFORM_ROOT_PATHS:
            return None
        if request.path.startswith('/static/') and not request.path.startswith('/static/uploads/'):
            return None
        return _notice('not_found')

    tenant = tenant_for_host(host)
    if tenant is None:
        return _notice('unknown_host', host)
    if not tenant.is_active:
        return _notice('suspended')

    # A school's address is a portal, not a website: no public pages are served
    # for any school.
    if request.path == '/school' or request.path.startswith('/school/'):
        return _notice('not_found')

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
