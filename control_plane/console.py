"""The Brightstars Academics platform console.

Served only on the platform hostnames (``BRIGHTSTARS_PLATFORM_HOSTS``): a
school's own domain never exposes any of it. Platform admins sign in here
against the registry database, create and suspend schools, manage their
domains, and enter any school.

Routes are registered on the shared Flask app with plain ``@app.route``, the
same convention the rest of the code base uses. This module is imported only
when multi-tenancy is enabled.
"""

from functools import wraps
from urllib.parse import urlsplit

import sqlalchemy as sa
from flask import (
    abort, flash, g, redirect, render_template, request, session, url_for,
)

from app import app
from core.security import csrf_protect

from . import provisioning as pv
from .entry import (
    authenticate_platform_admin, enter_school, mint_entry_token,
    platform_admin_by_id, purge_expired_entry_tokens, school_admins,
    set_platform_password, tenant_stats,
)
from .models import Tenant, TENANT_ACTIVE, TENANT_SUSPENDED
from .registry import get_tenant, platform_session, to_info

SESSION_KEY = 'platform_admin_id'


# ---------------- guards ----------------

def platform_host_only(fn):
    """404 unless this request arrived on a platform hostname.

    The console must be invisible from a school's domain, so an unauthorised
    visitor cannot even tell that these routes exist.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not g.get('on_platform_host'):
            abort(404)
        return fn(*args, **kwargs)
    return wrapper


def current_platform_admin():
    admin_id = session.get(SESSION_KEY)
    if not admin_id:
        return None
    return platform_admin_by_id(admin_id)


def platform_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        admin = current_platform_admin()
        if not admin:
            session.pop(SESSION_KEY, None)
            return redirect(url_for('platform_login', next=request.path))
        g.platform_admin = admin
        return fn(*args, **kwargs)
    return wrapper


def _tenant_or_404(slug):
    with platform_session() as s:
        tenant = get_tenant(s, slug)
        if not tenant:
            abort(404)
        domains = [{'hostname': d.hostname, 'primary': bool(d.is_primary), 'portal': d.is_portal}
                   for d in tenant.domains]
        return to_info(tenant), domains, tenant.suspended_reason


def _safe_next(target, fallback):
    """Only ever redirect within this site — never to a URL someone supplied."""
    if target and target.startswith('/') and not target.startswith('//'):
        return target
    return fallback


# ---------------- sign in / out ----------------

@app.route('/platform/login', methods=['GET', 'POST'])
@platform_host_only
@csrf_protect
def platform_login():
    if request.method == 'POST':
        username = request.form.get('username', '')
        admin = authenticate_platform_admin(username, request.form.get('password', ''))
        if not admin:
            return render_template('platform/login.html',
                                   error='Those platform credentials were not recognised.'), 401
        # A platform session shares nothing with a school session.
        session.clear()
        session[SESSION_KEY] = admin['id']
        return redirect(_safe_next(request.args.get('next'), url_for('platform_dashboard')))
    return render_template('platform/login.html')


@app.post('/platform/logout')
@platform_host_only
@csrf_protect
def platform_logout():
    session.clear()
    return redirect(url_for('platform_login'))


@app.route('/platform/password', methods=['GET', 'POST'])
@platform_host_only
@platform_required
@csrf_protect
def platform_password():
    error = None
    if request.method == 'POST':
        new = request.form.get('new_password', '')
        if new != request.form.get('confirm_password', ''):
            error = 'The new password and confirmation do not match.'
        else:
            error = set_platform_password(g.platform_admin['id'],
                                          request.form.get('current_password', ''), new)
        if not error:
            flash('Your platform password has been changed.', 'success')
            return redirect(url_for('platform_dashboard'))
    return render_template('platform/password.html', error=error)


# ---------------- schools ----------------

@app.route('/platform/')
@platform_host_only
def platform_index():
    """Keep the trailing-slash form working, with one canonical URL."""
    return redirect(url_for('platform_dashboard'))


@app.route('/platform')
@platform_host_only
@platform_required
def platform_dashboard():
    schools = []
    with platform_session() as s:
        for tenant in s.scalars(sa.select(Tenant).order_by(Tenant.name)):
            schools.append({
                'info': to_info(tenant),
                'portal': next((d.hostname for d in tenant.domains if d.is_portal), None),
                'customs': [d.hostname for d in tenant.domains if not d.is_portal],
                'primary': next((d.hostname for d in tenant.domains if d.is_primary), None),
                'suspended_reason': tenant.suspended_reason,
                'created_at': tenant.created_at,
            })
    for school in schools:
        school['stats'] = tenant_stats(school['info'])
    return render_template('platform/dashboard.html', schools=schools)


@app.route('/platform/schools/new', methods=['GET', 'POST'])
@platform_host_only
@platform_required
@csrf_protect
def platform_school_new():
    form = {'name': '', 'code': '', 'domains': '', 'admin_username': '',
            'admin_display_name': '', 'db_url': '', 'db_schema': '',
            'school_motto': '', 'school_tagline': '', 'school_phone': '',
            'school_email': '', 'school_address': ''}
    errors = []
    if request.method == 'POST':
        form = {k: request.form.get(k, '').strip() for k in form}
        domains = [line.strip() for line in form['domains'].replace(',', '\n').splitlines() if line.strip()]
        if not form['name']:
            errors.append('The school name is required.')
        code = (form['code'] or pv.suggest_slug(form['name'])).lower()
        form['code'] = code
        branding = {key: form[key] for key in pv.BRANDING_FIELDS if key in form}
        branding['school_name'] = form['name']
        logo = request.files.get('logo')
        if not errors:
            try:
                info, password = pv.create_tenant(
                    code, form['name'], domains,
                    db_url=form['db_url'] or None, db_schema=form['db_schema'] or None,
                    admin_username=form['admin_username'] or None,
                    admin_display_name=form['admin_display_name'] or None,
                    branding=branding, logo=logo,
                    actor=g.platform_admin['username'])
            except (pv.ProvisioningError, ValueError) as exc:
                errors.append(str(exc))
            else:
                flash(f'{info.name} has been created.', 'success')
                portal, customs = pv.domains_of(info.slug)
                return render_template('platform/school_created.html', info=info,
                                       portal=portal, customs=customs,
                                       admin_username=form['admin_username'],
                                       password=password,
                                       folder=pv.tenant_folder_listing(info))
    return render_template('platform/school_new.html', form=form, errors=errors,
                           portal_domain=pv.config.portal_domain())


@app.route('/platform/schools/<slug>')
@platform_host_only
@platform_required
def platform_school(slug):
    info, domains, suspended_reason = _tenant_or_404(slug)
    portal = next((d['hostname'] for d in domains if d['portal']), None)
    return render_template('platform/school.html', info=info, domains=domains, portal=portal,
                           suspended_reason=suspended_reason, stats=tenant_stats(info),
                           admins=school_admins(info), folder=pv.tenant_folder_listing(info),
                           branding=pv.branding_of(info),
                           audit=pv.recent_audit(tenant_id=info.id, limit=15))


@app.post('/platform/schools/<slug>/status')
@platform_host_only
@platform_required
@csrf_protect
def platform_school_status(slug):
    info, _, _ = _tenant_or_404(slug)
    wanted = request.form.get('status', '')
    if wanted not in (TENANT_ACTIVE, TENANT_SUSPENDED):
        abort(400)
    pv.set_status(slug, wanted, request.form.get('reason', '').strip() or None,
                  actor=g.platform_admin['username'])
    flash(f'{info.name} is now {wanted}.', 'success')
    return redirect(url_for('platform_school', slug=slug))


@app.post('/platform/schools/<slug>/domains')
@platform_host_only
@platform_required
@csrf_protect
def platform_school_domains(slug):
    _tenant_or_404(slug)
    action = request.form.get('action', 'add')
    hostname = request.form.get('hostname', '').strip()
    try:
        if action == 'remove':
            pv.remove_domain(hostname, actor=g.platform_admin['username'])
            flash(f'{hostname} no longer reaches this school.', 'success')
        else:
            pv.add_domain(slug, hostname, bool(request.form.get('primary')),
                          actor=g.platform_admin['username'])
            flash(f'{hostname} now reaches this school. Point its DNS at this server.', 'success')
    except (pv.ProvisioningError, ValueError) as exc:
        flash(str(exc), 'error')
    return redirect(url_for('platform_school', slug=slug))


@app.post('/platform/schools/<slug>/admins')
@platform_host_only
@platform_required
@csrf_protect
def platform_school_admin_new(slug):
    info, _, _ = _tenant_or_404(slug)
    try:
        password = pv.create_school_admin(info, request.form.get('username', ''),
                                          request.form.get('display_name', '').strip() or None)
    except (pv.ProvisioningError, ValueError) as exc:
        flash(str(exc), 'error')
        return redirect(url_for('platform_school', slug=slug))
    pv.record('tenant.school_admin_create', request.form.get('username', ''), info.id,
              g.platform_admin['username'], g.platform_admin['id'])
    flash('School administrator created.', 'success')
    return render_template('platform/school_admin_created.html', info=info,
                           username=request.form.get('username', '').strip().lower(),
                           password=password)


@app.post('/platform/schools/<slug>/enter')
@platform_host_only
@platform_required
@csrf_protect
def platform_school_enter(slug):
    info, domains, _ = _tenant_or_404(slug)
    if not info.is_active:
        flash('Reactivate the school before entering it.', 'error')
        return redirect(url_for('platform_school', slug=slug))
    # Always enter on the portal hostname: it is issued by the platform and
    # always resolves, whereas a school's own domain depends on its DNS.
    primary = next((d['hostname'] for d in domains if d['portal']), None)
    if not primary:
        flash('This school has no portal address yet, so there is nowhere to enter.', 'error')
        return redirect(url_for('platform_school', slug=slug))
    purge_expired_entry_tokens()
    token = mint_entry_token(g.platform_admin['id'], info.id, request.remote_addr)
    return redirect(_school_url(primary, url_for('platform_entry', token=token)))


@app.route('/platform/audit')
@platform_host_only
@platform_required
def platform_audit():
    return render_template('platform/audit.html', audit=pv.recent_audit(limit=200))


def _school_url(hostname, path):
    """An absolute URL on a school's own domain, keeping this request's scheme
    and port so it works behind TLS in production and on :5000 locally."""
    scheme = request.headers.get('X-Forwarded-Proto', request.scheme).split(',')[0].strip() or 'https'
    port = urlsplit(f'//{request.host}').port
    if port and port not in (80, 443):
        hostname = f'{hostname}:{port}'
    return f'{scheme}://{hostname}{path}'


# ---------------- the school side of "enter school" ----------------

@app.route('/platform-entry/<token>')
def platform_entry(token):
    """Redeem a platform entry ticket, on the school's own domain.

    Deliberately not restricted to the platform host: this runs on the school
    the operator is entering. The token is single-use, expires in two minutes,
    and is only ever accepted for the school it was minted for.
    """
    tenant = g.get('tenant')
    if tenant is None:
        abort(404)
    platform_admin, admin_id = enter_school(token, tenant)
    if not platform_admin:
        flash('That access link has expired or has already been used. Start again from the platform console.',
              'error')
        return redirect(url_for('login'))

    from core.accounts import _clear_identity_sessions
    from core.security import audit_log

    _clear_identity_sessions()
    session['tenant_id'] = tenant.id
    session['admin_id'] = admin_id
    session['admin_logged_in'] = True
    session['platform_operator'] = platform_admin['username']
    audit_log('platform_admin_entered_school', 'authentication', 'admin', admin_id,
              {'platform_admin': platform_admin['username'], 'school': tenant.slug})
    flash(f"You are in {tenant.name} as a Brightstars Academics operator.", 'success')
    return redirect(url_for('admin_workspace_home'))


# Make the operator banner available to every school template.
@app.context_processor
def _inject_platform_operator():
    return {'platform_operator': session.get('platform_operator')}
