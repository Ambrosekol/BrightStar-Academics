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
    Response, abort, flash, g, jsonify, redirect, render_template, request, send_from_directory, session, url_for,
)
from werkzeug.utils import secure_filename

from app import app
from core import numbering, theme
from core.security import csrf_protect
from core.session_guard import refresh_password_stamp, stamp_session, take_sign_out_reason
from core.uploads import IMAGE_EXTENSIONS

from . import config
from . import provisioning as pv
from . import team
from .entry import (
    authenticate_platform_admin, enter_school, mint_entry_token,
    platform_admin_by_id, purge_expired_entry_tokens, school_admins,
    set_platform_password, tenant_glance, tenant_stats,
)
from .models import Tenant, TENANT_ACTIVE, TENANT_SUSPENDED
from .ratelimit import allow
from .registry import SLUG_RE, get_tenant, platform_session, to_info

SESSION_KEY = 'platform_admin_id'


def stamp_platform_sign_in(admin_id):
    """Mark a console session that has just been opened: this launch of the server, and the
    password the account has now (core/session_guard.py)."""
    from core.session_guard import _current_fingerprint

    stamp_session(fingerprint=_current_fingerprint('platform', admin_id))


app.add_template_filter(team.describe, 'describe_action')


@app.template_filter('platform_time')
def _platform_time(value):
    """An ISO-8601 UTC timestamp as "21 Sep 2026, 14:05 UTC" for the console."""
    from datetime import datetime

    try:
        return datetime.fromisoformat(value).strftime('%d %b %Y, %H:%M').lstrip('0') + ' UTC'
    except (TypeError, ValueError):
        return value or ''


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
        # An account created or reset by the super admin starts on a temporary
        # password, which must be replaced before the console can be used.
        if admin['password_must_change'] and request.endpoint not in ('platform_password', 'platform_logout'):
            flash('Choose your own password to continue.', 'error')
            return redirect(url_for('platform_password'))
        return fn(*args, **kwargs)
    return wrapper


def superadmin_required(fn):
    """Only the super admin. Use after ``platform_required``.

    Platform admins are trusted with every school, but the team itself — who is
    an admin, and what each has done — is the super admin's alone.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not g.platform_admin['is_super']:
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


def delete_access_required(fn):
    """Only the super admin, or an admin the super admin has explicitly trusted to delete
    schools. Use after ``platform_required``.

    Permanently deleting a school is far more severe than anything else a platform admin can do
    to one (create, brand, suspend, enter - all either routine or reversible), so it needs its
    own, narrower grant rather than riding on ordinary platform-admin status.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not (g.platform_admin['is_super'] or g.platform_admin.get('can_delete_schools')):
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


def allow_branding_upload(fn):
    """Raise the request-size limit for a view that takes a logo and photographs.

    Must sit outside ``csrf_protect``, which reads the form, and with it the
    limit is enforced, before the view itself runs.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        request.max_content_length = pv.max_branding_request_bytes()
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


# ---------------- a school's numbering rules (patterns; see core/numbering.py) ----------------

NUMBERING_FIELDS = ('candidate_pattern', 'student_pattern', 'first_number')
MAX_NUMBERING_INPUT = 200  # longer than any pattern may be, so an over-long one is refused, not cut short


def _numbering_defaults():
    return {'candidate_pattern': numbering.DEFAULT_CANDIDATE_PATTERN,
            'student_pattern': numbering.DEFAULT_STUDENT_PATTERN,
            'first_number': str(numbering.DEFAULT_FIRST_NUMBER)}


def _numbering_posted(require_all):
    """The numbering fields of a submitted form, exactly as typed (never trimmed, so the position
    of a problem is the position the person sees). A field the form does not carry is the default
    when a school is created, and a refusal when a school's rules are saved."""
    values, missing = _numbering_defaults(), []
    for key in NUMBERING_FIELDS:
        if key in request.form:
            values[key] = request.form.get(key, '')[:MAX_NUMBERING_INPUT]
        elif require_all:
            missing.append(key)
    return values, missing


def _numbering_view(values, slug='', problem=None):
    """Everything the numbering editor (templates/platform/_numbering.html) needs."""
    return {
        'fields': values, 'defaults': _numbering_defaults(), 'slug': slug, 'problem': problem,
        'preview_url': url_for('platform_numbering_preview'), 'max_pattern': numbering.MAX_PATTERN_LENGTH,
        'palette': [{'token': token, 'meaning': meaning, 'example': example,
                     'candidate': numbering.CANDIDATE in kinds, 'student': numbering.STUDENT in kinds}
                    for token, meaning, example, kinds in numbering.PALETTE],
    }


# ---------------- sign in / out ----------------

@app.route('/platform/login', methods=['GET', 'POST'])
@platform_host_only
@csrf_protect
def platform_login():
    if request.method == 'POST':
        username = request.form.get('username', '')
        # Platform accounts can enter every school, so guessing at them is limited the way a
        # school's sign-in is: per address and username, counted in the registry so that
        # every worker shares one count.
        if not allow(f"platform-login:{request.remote_addr or 'unknown'}:{username.strip().lower()[:120]}",
                     limit=8, window=300):
            return render_template('platform/login.html',
                                   error='Too many sign-in attempts. Please wait a few minutes and try again.'), 429
        admin = authenticate_platform_admin(username, request.form.get('password', ''),
                                            request.remote_addr)
        if not admin:
            return render_template('platform/login.html',
                                   error='Those platform credentials were not recognised.'), 401
        # A platform session shares nothing with a school session.
        session.clear()
        session[SESSION_KEY] = admin['id']
        stamp_platform_sign_in(admin['id'])
        return redirect(_safe_next(request.args.get('next'), url_for('platform_dashboard')))
    # The sign-in page has no place for queued messages, so the one reason a sign-in was ended
    # (a restart, a changed password) is shown in its own alert box instead.
    return render_template('platform/login.html', error=take_sign_out_reason())


@app.post('/platform/logout')
@platform_host_only
@csrf_protect
def platform_logout():
    admin = current_platform_admin()
    if admin:
        pv.record('platform_admin.logout', None, None, admin['username'], admin['id'])
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
            refresh_password_stamp()  # this sign-in stays; every other one made with the old password ends
            flash('Your platform password has been changed.', 'success')
            return redirect(url_for('platform_dashboard'))
    return render_template('platform/password.html', error=error,
                           forced=bool(g.platform_admin['password_must_change']))


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
        glance = tenant_glance(school['info'])
        school.update(stats=glance['stats'], colour=glance['colour'], logo=glance['logo'])
    known = [s['stats']['students'] for s in schools if s['stats'] and s['stats']['students'] is not None]
    totals = {
        'schools': len(schools),
        'active': sum(1 for s in schools if s['info'].is_active),
        'suspended': sum(1 for s in schools if not s['info'].is_active),
        'students': sum(known),
        'unreadable': sum(1 for s in schools if not s['stats']),
    }
    # The super admin sees the whole platform's latest movements; anyone else, their own.
    me = g.platform_admin
    # The platform's own log only: the dashboard should not open school databases to draw a feed.
    recent = team.activity(None if me['is_super'] else me['id'], per_page=8, inside=False)['rows']
    return render_template('platform/dashboard.html', schools=schools, totals=totals, recent=recent)


@app.route('/platform/schools/new', methods=['GET', 'POST'])
@platform_host_only
@platform_required
@allow_branding_upload
@csrf_protect
def platform_school_new():
    form = {'name': '', 'code': '', 'domains': '', 'admin_username': '',
            'admin_display_name': '', 'db_url': '', 'db_schema': '',
            'school_motto': '', 'school_tagline': '', 'school_phone': '',
            'school_email': '', 'school_address': '',
            theme.PRIMARY_KEY: theme.DEFAULT_PRIMARY, theme.ACCENT_KEY: theme.DEFAULT_ACCENT}
    errors = []
    starter_banks = True  # ticked unless the operator unticks it
    numbering_values = _numbering_defaults()  # the editor starts filled with the default rules
    if request.method == 'POST':
        starter_banks = request.form.get('starter_banks') == '1'
        numbering_values, _ = _numbering_posted(require_all=False)
        form = {k: request.form.get(k, '').strip() for k in form}
        domains = [line.strip() for line in form['domains'].replace(',', '\n').splitlines() if line.strip()]
        if not form['name']:
            errors.append('The school name is required.')
        code = (form['code'] or pv.suggest_slug(form['name'])).lower()
        form['code'] = code
        branding = {key: form[key] for key in pv.BRANDING_FIELDS if key in form}
        branding['school_name'] = form['name']
        logo = request.files.get('logo')
        gallery = request.files.getlist('gallery')
        if not errors:
            try:
                slug, password = pv.start_tenant_creation(
                    code, form['name'], domains,
                    db_url=form['db_url'] or None, db_schema=form['db_schema'] or None,
                    admin_username=form['admin_username'] or None,
                    admin_display_name=form['admin_display_name'] or None,
                    branding=branding, logo=logo, gallery=gallery, starter_banks=starter_banks,
                    actor=g.platform_admin['username'], numbering_rules=numbering_values)
            except (pv.ProvisioningError, ValueError) as exc:
                errors.append(str(exc))
            else:
                # Building the school takes longer than a browser or proxy will wait, so it runs in
                # the background and this page follows it. The one-time password is chosen already
                # and shown here, the only place it ever appears.
                return render_template('platform/school_creating.html', slug=slug, name=form['name'],
                                       portal=pv.config.portal_hostname(slug),
                                       admin_username=form['admin_username'], password=password)
    return render_template('platform/school_new.html', form=form, errors=errors,
                           starter_banks=starter_banks,
                           numbering=_numbering_view(numbering_values),
                           portal_domain=pv.config.portal_domain(),
                           max_gallery=theme.MAX_GALLERY_IMAGES,
                           min_contrast=theme.MIN_CONTRAST_WITH_WHITE)


@app.get('/platform/schools/<slug>/creation-status')
@platform_host_only
@platform_required
def platform_school_creation_status(slug):
    """Where a school that is being built in the background has got to."""
    if not SLUG_RE.match(slug):
        abort(404)
    return jsonify(pv.provision_state(slug))


@app.route('/platform/schools/<slug>')
@platform_host_only
@platform_required
def platform_school(slug):
    return _school_page(slug)


def _school_page(slug, numbering_values=None, numbering_errors=()):
    """A school's page. ``numbering_values`` are what the numbering editor shows instead of the
    school's saved rules (the values just typed, when a save was refused)."""
    info, domains, suspended_reason = _tenant_or_404(slug)
    portal = next((d['hostname'] for d in domains if d['portal']), None)
    branding = pv.branding_of(info)
    logo_path = branding.get(pv.LOGO_SETTING_KEY) or ''
    rules, numbering_problem = pv.numbering_of(info)
    if numbering_values is None:
        numbering_values = {'candidate_pattern': rules.candidate_pattern,
                            'student_pattern': rules.student_pattern,
                            'first_number': str(rules.first_number)}
    return render_template('platform/school.html', info=info, domains=domains, portal=portal,
                           numbering=_numbering_view(numbering_values, info.slug, numbering_problem),
                           numbering_errors=list(numbering_errors),
                           suspended_reason=suspended_reason, stats=tenant_stats(info),
                           admins=school_admins(info), folder=pv.tenant_folder_listing(info),
                           branding=branding,
                           gallery=[p.rsplit('/', 1)[-1] for p in
                                    theme.parse_gallery(branding.get(theme.GALLERY_KEY))],
                           logo_file=(logo_path.rsplit('/', 1)[-1]
                                      if logo_path.startswith(theme.GALLERY_FOLDER) else ''),
                           primary=branding.get(theme.PRIMARY_KEY) or theme.DEFAULT_PRIMARY,
                           accent=branding.get(theme.ACCENT_KEY) or theme.DEFAULT_ACCENT,
                           max_gallery=theme.MAX_GALLERY_IMAGES,
                           audit=pv.recent_audit(tenant_id=info.id, limit=15))


@app.post('/platform/schools/<slug>/numbering')
@platform_host_only
@platform_required
@csrf_protect
def platform_school_numbering(slug):
    """Change a school's numbering rules. Every platform admin may, like the school's other
    settings; everything is checked here again whatever the page's preview said, and the old and
    new patterns are written to the audit trail."""
    info, _, _ = _tenant_or_404(slug)
    values, missing = _numbering_posted(require_all=True)
    if missing:
        return _school_page(slug, values, ['The numbering form was incomplete, so nothing was saved.']), 400
    try:
        pv.update_numbering(info, values['candidate_pattern'], values['student_pattern'],
                            values['first_number'], g.platform_admin['username'], g.platform_admin['id'])
    except (pv.ProvisioningError, ValueError) as exc:
        return _school_page(slug, values, [str(exc)]), 400
    flash("The school's numbering rules have been saved. Codes already issued are unchanged.", 'success')
    return redirect(url_for('platform_school', slug=slug) + '#numbering')


@app.post('/platform/numbering/preview')
@platform_host_only
@platform_required
@csrf_protect
def platform_numbering_preview():
    """Show what a set of numbering rules would make. Writes nothing, anywhere: it only reads.

    For a school that exists, the numbers are its real next ones; while a school is being
    created, the code and name typed on the form are used for examples."""
    def field(key, size=MAX_NUMBERING_INPUT):
        return request.form.get(key, '')[:size]

    slug = field('slug', 64)
    if slug and not SLUG_RE.match(slug):
        abort(404)  # never look up (or send to the database) anything that is not a school code
    info = _tenant_or_404(slug)[0] if slug else None
    name = field('name', 120).strip()
    code = field('code', 60).strip()
    code = pv.suggest_slug(code) if code else (pv.suggest_slug(name) if name else 'code')
    result = pv.preview_numbering(field('candidate_pattern'), field('student_pattern'),
                                  field('first_number', 20), code, name, info)
    response = jsonify(result)
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.post('/platform/schools/<slug>/branding')
@platform_host_only
@platform_required
@allow_branding_upload
@csrf_protect
def platform_school_branding(slug):
    """Change a school's colours, logo and sign-in photographs."""
    info, _, _ = _tenant_or_404(slug)
    colours = {key: request.form.get(key, '') for key in (theme.PRIMARY_KEY, theme.ACCENT_KEY)}
    remove = [f'{theme.GALLERY_FOLDER}{name}' for name in request.form.getlist('remove_photo')]
    try:
        pv.update_branding(info, colours, logo=request.files.get('logo'),
                           gallery=request.files.getlist('gallery'), remove_gallery=remove)
    except (pv.ProvisioningError, ValueError) as exc:
        flash(str(exc), 'error')
    else:
        pv.record('tenant.branding_update', slug, info.id, g.platform_admin['username'],
                  g.platform_admin['id'])
        flash("The school's branding has been updated.", 'success')
    return redirect(url_for('platform_school', slug=slug))


@app.route('/platform/schools/<slug>/branding/<filename>')
@platform_host_only
@platform_required
def platform_school_photo(slug, filename):
    """Show a platform admin a logo or photograph a school uploaded.

    A school's uploads are never served from the platform host, so this is the
    one way to look at them from the console: signed-in platform admins only,
    one folder, image files only.
    """
    info, _, _ = _tenant_or_404(slug)
    if (filename != secure_filename(filename)
            or filename.rsplit('.', 1)[-1].lower() not in IMAGE_EXTENSIONS):
        abort(404)
    if config.storage_backend() == 's3':
        from core import object_store
        from core import storage as _storage
        data = object_store.get_bytes(f'{_storage.tenant_root(info)}/uploads/branding/{filename}')
        if data is None:
            abort(404)
        import mimetypes
        mime = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        response = Response(data, mimetype=mime)
    else:
        folder = pv.tenant_folder(info) / 'uploads' / 'branding'
        response = send_from_directory(folder, filename)
    # This route itself requires a signed-in platform admin, so 'private' regardless of the fact
    # that the same file is also public at /static/uploads/branding/... on the school's own
    # address; its stored name is random and never reused, so the bytes never change.
    response.headers['Cache-Control'] = 'private, max-age=31536000, immutable'
    return response


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


@app.post('/platform/schools/<slug>/delete')
@platform_host_only
@platform_required
@delete_access_required
@csrf_protect
def platform_school_delete(slug):
    """Export, then permanently delete, one school: its database, its files, and its place in
    the registry. Cannot be undone; the export is the way back."""
    info, _, _ = _tenant_or_404(slug)
    try:
        path = pv.delete_tenant(slug, backup=True, actor=g.platform_admin['username'])
    except pv.ProvisioningError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('platform_school', slug=slug))
    flash(f"{info.name} was exported to {path} on the server, then permanently deleted. Copy the "
          "export somewhere safe and remove it from the server afterwards - it holds that "
          "school's complete data.", 'success')
    return redirect(url_for('platform_dashboard'))


@app.post('/platform/schools/<slug>/force-delete')
@platform_host_only
@platform_required
@delete_access_required
@csrf_protect
def platform_school_force_delete(slug):
    """Permanently delete one school - its database, its files, and its place in the registry -
    with no export taken first. Cannot be undone."""
    info, _, _ = _tenant_or_404(slug)
    try:
        pv.delete_tenant(slug, backup=False, actor=g.platform_admin['username'])
    except pv.ProvisioningError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('platform_school', slug=slug))
    flash(f'{info.name} was permanently deleted, with no backup taken.', 'success')
    return redirect(url_for('platform_dashboard'))


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


# ---------------- the team, and its activity ----------------

@app.route('/platform/audit')
@platform_host_only
def platform_audit_legacy():
    """The activity log used to live here."""
    return redirect(url_for('platform_activity'))


def _int_arg(name, default=None):
    try:
        return int(request.args.get(name, ''))
    except ValueError:
        return default


@app.route('/platform/activity')
@platform_host_only
@platform_required
def platform_activity():
    """Activity logs, arranged as: choose an admin, then read their log.

    The super admin picks any admin (or everyone) from the list; any other
    platform admin sees only their own log, with no list to pick from.
    """
    me = g.platform_admin
    category = request.args.get('category', 'all')
    page = _int_arg('page', 1)
    if me['is_super']:
        admins = team.list_admins()
        if request.args.get('admin') == 'all':
            selected, admin_id = 'all', None
        else:
            admin_id = _int_arg('admin', me['id'])
            selected = next((a for a in admins if a['id'] == admin_id), None)
            if selected is None:
                abort(404)
    else:
        admins, selected, admin_id = [], next(a for a in team.list_admins() if a['id'] == me['id']), me['id']
    log = team.activity(admin_id, category, page)
    return render_template('platform/activity.html', admins=admins, selected=selected, log=log,
                           category=category if category in dict(team.CATEGORIES) else 'all',
                           categories=team.CATEGORIES, is_super=me['is_super'])


@app.route('/platform/team')
@platform_host_only
@platform_required
@superadmin_required
def platform_team():
    return render_template('platform/team.html', admins=team.list_admins(), form={'username': '', 'display_name': '', 'email': ''})


def _credentials_page(username, password, heading, lead):
    return render_template('platform/admin_credentials.html', username=username, password=password,
                           heading=heading, lead=lead)


@app.post('/platform/team/new')
@platform_host_only
@platform_required
@superadmin_required
@csrf_protect
def platform_team_new():
    me = g.platform_admin
    form = {k: request.form.get(k, '').strip() for k in ('username', 'display_name', 'email')}
    try:
        password = team.create_admin(form['username'], form['display_name'], form['email'],
                                     me['username'], me['id'])
    except pv.ProvisioningError as exc:
        return render_template('platform/team.html', admins=team.list_admins(), form=form,
                               error=str(exc)), 400
    return _credentials_page(
        form['username'].lower(), password, 'Platform admin added',
        'They can manage every school, but not the team or its logs. They must choose their own '
        'password the first time they sign in.')


@app.post('/platform/team/<int:admin_id>/remove')
@platform_host_only
@platform_required
@superadmin_required
@csrf_protect
def platform_team_remove(admin_id):
    me = g.platform_admin
    try:
        result = team.remove_admin(admin_id, me['username'], me['id'])
    except pv.ProvisioningError as exc:
        flash(str(exc), 'error')
    else:
        message = f'Access removed. Their reserved account was switched off in {result["schools"]} school(s).'
        if result['failed']:
            flash(message + ' These schools could not be reached and need attention: '
                  + ', '.join(result['failed']) + '.', 'error')
        else:
            flash(message, 'success')
    return redirect(url_for('platform_team'))


@app.post('/platform/team/<int:admin_id>/restore')
@platform_host_only
@platform_required
@superadmin_required
@csrf_protect
def platform_team_restore(admin_id):
    me = g.platform_admin
    try:
        username, password = team.restore_admin(admin_id, me['username'], me['id'])
    except pv.ProvisioningError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('platform_team'))
    return _credentials_page(username, password, 'Platform admin restored',
                             'Their old password was discarded. They must choose a new one when they sign in.')


@app.post('/platform/team/<int:admin_id>/docs-access')
@platform_host_only
@platform_required
@superadmin_required
@csrf_protect
def platform_team_docs_access(admin_id):
    """Grant or withdraw one admin's access to /docs. Only the super admin decides who reads it."""
    me = g.platform_admin
    allowed = request.form.get('allow') == '1'
    try:
        username = team.set_docs_access(admin_id, allowed, me['username'], me['id'])
    except pv.ProvisioningError as exc:
        flash(str(exc), 'error')
    else:
        flash(f'{username} can now read the documentation.' if allowed
              else f'{username} can no longer read the documentation.', 'success')
    return redirect(url_for('platform_team'))


@app.post('/platform/team/<int:admin_id>/delete-access')
@platform_host_only
@platform_required
@superadmin_required
@csrf_protect
def platform_team_delete_access(admin_id):
    """Grant or withdraw one admin's access to permanently delete a school. Only the super admin
    decides who else may."""
    me = g.platform_admin
    allowed = request.form.get('allow') == '1'
    try:
        username = team.set_delete_access(admin_id, allowed, me['username'], me['id'])
    except pv.ProvisioningError as exc:
        flash(str(exc), 'error')
    else:
        flash(f'{username} can now permanently delete a school.' if allowed
              else f'{username} can no longer permanently delete a school.', 'success')
    return redirect(url_for('platform_team'))


@app.post('/platform/team/<int:admin_id>/reset-password')
@platform_host_only
@platform_required
@superadmin_required
@csrf_protect
def platform_team_reset(admin_id):
    me = g.platform_admin
    try:
        username, password = team.reset_password(admin_id, me['username'], me['id'])
    except pv.ProvisioningError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('platform_team'))
    return _credentials_page(username, password, 'Password reset',
                             'Their old password no longer works. They must choose a new one when they sign in.')


def _school_url(hostname, path):
    """An absolute URL on a school's own domain, keeping this request's scheme
    and port so it works behind TLS in production and on :5000 locally."""
    # Never taken from a request header: anyone can send one. Behind a proxy we trust, the scheme is
    # already right (BRIGHTSTARS_TRUSTED_PROXIES, see app.py); in production the portal is https.
    scheme = 'https' if config.is_production() else request.scheme
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
    stamp_session(fingerprint='operator')
    audit_log('platform_admin_entered_school', 'authentication', 'admin', admin_id,
              {'platform_admin': platform_admin['username'], 'school': tenant.slug})
    flash(f"You are in {tenant.name} as a Brightstars Academics operator.", 'success')
    return redirect(url_for('admin_workspace_home'))


# Make the operator banner available to every school template.
@app.context_processor
def _inject_platform_operator():
    return {'platform_operator': session.get('platform_operator')}
