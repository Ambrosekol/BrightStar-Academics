"""The platform's Settings area: system-wide environment variables, and the ``python -m
control_plane`` commands that otherwise need a terminal, made available in the console itself.

Two things stay CLI-only on purpose: ``init`` (there is no registry yet for a web page to sign
into) and creating the *super* admin (a one-time bootstrap trust decision nobody should be able to
grant themselves through a web form). Every other command a platform admin could already run from a
terminal is here.

Two access flags on :class:`~control_plane.models.PlatformAdmin` gate all of it (see
``control_plane/team.py``'s ``set_settings_access``):

* ``settings_access`` - may open this area at all, view everything, edit Low/Medium-risk
  variables (``control_plane/settings_registry.py``) and run the actions that are not about a key
  or a database connection string.
* ``settings_high_trust`` - additionally may edit High/Critical-risk variables and run the three
  key-related actions: rotating the delivery key, creating/rotating a school's own database role,
  and pointing a school at a different database.

The super admin always has both. Every change here - a variable edited, or an action run - is
written to the same platform audit trail every other console action already uses, so it shows up
on ``/platform/activity`` exactly like anything else a platform admin does.
"""

from functools import wraps

from flask import abort, flash, g, redirect, render_template, request, url_for

from app import app
from core.security import csrf_protect

from . import config as cp_config
from . import env_file, launch, settings_registry
from . import provisioning as pv
from .console import platform_host_only, platform_required, superadmin_required
from .context import tenant_context
from .registry import get_tenant, platform_session, to_info


# ---------------- guards ----------------

def settings_required(fn):
    """A signed-in platform admin with access to Settings, and nobody else.

    Not signed in: sent to the console's sign-in page. Signed in without access: a plain 404, the
    same treatment ``docs_required`` gives ``/docs`` - the page is indistinguishable from one that
    does not exist, rather than visibly refusing someone.
    """
    @wraps(fn)
    @platform_host_only
    @platform_required
    def wrapper(*args, **kwargs):
        if not g.platform_admin.get('settings_access'):
            abort(404)
        return fn(*args, **kwargs)
    return wrapper


def high_trust_required(fn):
    """Only the super admin or an admin marked highly trusted. Use after ``settings_required``.

    A disabled button in the template is a convenience, never the real gate - this decorator is
    checked again on every state-changing request regardless of what the page offered to show.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not g.platform_admin.get('settings_high_trust'):
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


def _may_edit(variable):
    me = g.platform_admin
    if variable.risk in (settings_registry.RISK_HIGH, settings_registry.RISK_CRITICAL):
        return bool(me.get('settings_high_trust'))
    return bool(me.get('settings_access'))


def _log_variable_change(name):
    me = g.platform_admin
    pv.record('platform_config.variable_changed', f'{name}: changed', actor=me['username'], admin_id=me['id'])


def _log_action(detail):
    me = g.platform_admin
    pv.record('platform_config.action_run', detail, actor=me['username'], admin_id=me['id'])


def _all_infos():
    return pv.list_tenant_infos()


def _info_or_404(code):
    with platform_session() as session:
        tenant = get_tenant(session, code)
        if not tenant:
            abort(404)
        return to_info(tenant)


# ---------------- environment variables ----------------

@app.route('/platform/settings')
@settings_required
def platform_settings():
    rows = []
    for variable in settings_registry.VARIABLES:
        rows.append({
            'v': variable,
            'value': env_file.effective_value(variable.name),
            'saved': variable.name in env_file.read_values(),
            'overridden': env_file.host_overridden(variable.name),
            'editable': _may_edit(variable),
        })
    return render_template('platform/settings.html', grouped=settings_registry.grouped(),
                           rows={r['v'].name: r for r in rows}, categories=settings_registry.CATEGORIES)


@app.route('/platform/settings/<name>', methods=['GET', 'POST'])
@settings_required
@csrf_protect
def platform_settings_variable(name):
    variable = settings_registry.get(name)
    if variable is None:
        abort(404)
    if request.method == 'POST':
        if not _may_edit(variable):
            abort(403)
        if variable.name == 'BRIGHTSTARS_DELIVERY_KEY':
            # Never set directly: an unrotated change makes every school's saved secrets
            # unreadable at once. The rotation wizard is the only door to this one.
            abort(400)
        value = request.form.get('value', '')
        env_file.write_value(variable.name, value)
        _log_variable_change(variable.name)
        flash(f'{variable.name} saved.' + (
            ' It takes effect the next time it is read - no restart needed.'
            if variable.effect == settings_registry.EFFECT_IMMEDIATE
            else ' The running application will not see this until it is restarted.'), 'success')
        return redirect(url_for('platform_settings_variable', name=variable.name))
    return render_template('platform/settings_variable.html', v=variable,
                           value=env_file.effective_value(variable.name),
                           saved=variable.name in env_file.read_values(),
                           overridden=env_file.host_overridden(variable.name),
                           editable=_may_edit(variable))


@app.post('/platform/settings/team/<int:admin_id>/access')
@platform_host_only
@platform_required
@superadmin_required
@csrf_protect
def platform_team_settings_access(admin_id):
    """Grant or withdraw one admin's Settings access, and/or their highly-trusted tier. Only the
    super admin decides who may touch system-wide configuration."""
    from . import team as team_mod

    me = g.platform_admin
    field = request.form.get('field')
    allow = request.form.get('allow') == '1'
    try:
        if field == 'high_trust':
            username = team_mod.set_settings_access(admin_id, high_trust=allow, actor=me['username'], actor_id=me['id'])
        else:
            username = team_mod.set_settings_access(admin_id, access=allow, actor=me['username'], actor_id=me['id'])
    except pv.ProvisioningError as exc:
        flash(str(exc), 'error')
    else:
        what = 'highly trusted' if field == 'high_trust' else 'Settings access'
        flash(f'{username} {"now has" if allow else "no longer has"} {what}.', 'success')
    return redirect(url_for('platform_team'))


# ---------------- terminal actions ----------------

@app.route('/platform/settings/actions')
@settings_required
def platform_settings_actions():
    dropcheck = request.args.get('dropcheck', '').strip()
    drop_results = None
    if dropcheck:
        infos = _all_infos() if dropcheck == 'all' else [_info_or_404(dropcheck)]
        drop_results = [(info, retired_tables_for(info)) for info in infos]
    return render_template('platform/settings_actions.html', infos=_all_infos(),
                           high_trust=bool(g.platform_admin.get('settings_high_trust')),
                           dropcheck=dropcheck, drop_results=drop_results)


def retired_tables_for(info):
    from core import retired_tables
    return retired_tables.for_school(info, drop=False)


@app.post('/platform/settings/actions/upgrade')
@settings_required
@csrf_protect
def platform_settings_upgrade():
    code = request.form.get('code', '').strip()
    if code:
        info = _info_or_404(code)
        pv.upgrade_tenant(info)
        _log_action(f'upgrade: {code}')
        flash(f'{code} upgraded.', 'success')
    else:
        infos = pv.upgrade_all_tenants()
        launch.record_new_launch()
        _log_action(f'upgrade: all {len(infos)} school(s), new launch recorded')
        flash(f'Upgraded {len(infos)} school(s). Everyone signed in before now must sign in again, '
              'except people mid-exam.', 'success')
    return redirect(url_for('platform_settings_actions'))


@app.post('/platform/settings/actions/new-launch')
@settings_required
@csrf_protect
def platform_settings_new_launch():
    launch.record_new_launch()
    _log_action('new-launch')
    flash('New launch recorded. Everyone signed in before now must sign in again, except people '
          'mid-exam.', 'success')
    return redirect(url_for('platform_settings_actions'))


@app.post('/platform/settings/actions/sweep-jobs')
@settings_required
@csrf_protect
def platform_settings_sweep_jobs():
    from core import jobs
    import app as A

    code = request.form.get('code', '').strip()
    infos = [_info_or_404(code)] if code else _all_infos()
    total = 0
    for info in infos:
        with A.app.app_context(), tenant_context(info):
            total += jobs.sweep_all_kinds()
    _log_action(f'sweep-jobs: {code or "all"} ({total} restarted)')
    flash(f'{total} stuck job(s) restarted across {len(infos)} school(s).', 'success')
    return redirect(url_for('platform_settings_actions'))


@app.post('/platform/settings/actions/drop-retired-tables')
@settings_required
@csrf_protect
def platform_settings_drop_retired():
    code = request.form.get('code', '').strip()
    infos = _all_infos() if code == 'all' else [_info_or_404(code)]
    from core import retired_tables

    dropped_any = False
    for info in infos:
        held = retired_tables.for_school(info, drop=True)
        if held:
            dropped_any = True
            pv.record('tenant.retired_tables_dropped', ', '.join(sorted(held)), tenant_id=info.id,
                      actor=g.platform_admin['username'], admin_id=g.platform_admin['id'])
    _log_action(f'drop-retired-tables: {code}')
    flash('Retired tables dropped.' if dropped_any else 'Nothing to drop.', 'success')
    return redirect(url_for('platform_settings_actions', dropcheck=code))


@app.post('/platform/settings/actions/export-tenant')
@settings_required
@csrf_protect
def platform_settings_export():
    import os

    code = request.form.get('code', '').strip()
    info = _info_or_404(code)
    output_dir = os.path.join(cp_config.BASE, 'exports')
    os.makedirs(output_dir, exist_ok=True)
    try:
        path = pv.export_tenant_data(code, output_dir)
    except pv.ProvisioningError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('platform_settings_actions'))
    _log_action(f'export-tenant: {code}')
    flash(f'{code} exported to {path} on the server. Copy it somewhere safe and remove it from the '
          'server afterwards - it holds that school\'s complete data.', 'success')
    return redirect(url_for('platform_settings_actions'))


@app.post('/platform/settings/actions/create-db-role')
@settings_required
@high_trust_required
@csrf_protect
def platform_settings_create_db_role():
    code = request.form.get('code', '').strip()
    rotate = request.form.get('rotate') == '1'
    password = request.form.get('password', '').strip()
    try:
        role_name, conn_url = pv.create_school_role(code, rotate=rotate, password=password or None)
    except pv.ProvisioningError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('platform_settings_actions'))
    _log_action(f'create-db-role: {code}{" (rotated)" if rotate else ""}')
    return render_template('platform/settings_secret_reveal.html',
                           heading='Database role ' + ('rotated' if rotate else 'created'),
                           lead=f'Role {role_name} now owns {code}\'s database, and this is its '
                                'connection URL. It is shown once. Set it as an environment '
                                'variable on every worker, then use "Point a school at a database" '
                                'below with env:YOUR_CHOSEN_VARIABLE_NAME to actually switch '
                                f'{code} to it - creating the role alone changes nothing yet.',
                           secret=conn_url, back_url=url_for('platform_settings_actions'))


@app.post('/platform/settings/actions/set-db-url')
@settings_required
@high_trust_required
@csrf_protect
def platform_settings_set_db_url():
    code = request.form.get('code', '').strip()
    url = request.form.get('url', '').strip()
    me = g.platform_admin
    try:
        pv.set_db_url(code, url, actor=me['username'])
    except pv.ProvisioningError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('platform_settings_actions'))
    _log_action(f'set-db-url: {code}')
    flash(f'{code} now uses this connection string on its next resolution.', 'success')
    return redirect(url_for('platform_settings_actions'))


# ---------------- the delivery-key rotation wizard ----------------

@app.route('/platform/settings/rotate-key')
@settings_required
@high_trust_required
def platform_settings_rotate_key():
    import secrets as _secrets

    generated = _secrets.token_urlsafe(32) if request.args.get('generate') else None
    return render_template('platform/settings_rotate_key.html', infos=_all_infos(), generated=generated,
                           result=None)


@app.post('/platform/settings/rotate-key/run')
@settings_required
@high_trust_required
@csrf_protect
def platform_settings_rotate_key_run():
    import app as A
    from core.secrets_rotation import rotate_one_tenant

    old, new = request.form.get('old', ''), request.form.get('new', '')
    code = request.form.get('code', '').strip()
    if not old or not new:
        flash('Both the current and the new key are required.', 'error')
        return redirect(url_for('platform_settings_rotate_key'))
    infos = _all_infos() if not code or code == 'all' else [_info_or_404(code)]
    result = []
    for info in infos:
        try:
            with A.app.app_context(), tenant_context(info):
                rotated, skipped = rotate_one_tenant(old, new)
            result.append({'slug': info.slug, 'name': info.name, 'rotated': rotated, 'skipped': skipped,
                           'error': None})
        except Exception as exc:
            result.append({'slug': info.slug, 'name': info.name, 'rotated': 0, 'skipped': [], 'error': str(exc)})
    _log_action(f'rotate-delivery-key: {code or "all"} (dry-run against every school\'s stored secrets)')
    all_clear = all(not r['skipped'] and not r['error'] for r in result)
    return render_template('platform/settings_rotate_key.html', infos=_all_infos(), generated=None,
                           result=result, new_key=new, all_clear=all_clear)


@app.post('/platform/settings/rotate-key/apply')
@settings_required
@high_trust_required
@csrf_protect
def platform_settings_rotate_key_apply():
    import app as A
    from core.secrets_rotation import rotate_one_tenant

    new = request.form.get('new', '')
    if not new:
        abort(400)
    # Re-verify at this exact moment, against every school, that its secrets are readable under
    # NEW - rather than trusting a flag carried over from an earlier request. rotate_one_tenant with
    # the same key twice is a safe no-op re-encryption when it succeeds, so this doubles as proof.
    all_clear = True
    for info in _all_infos():
        with A.app.app_context(), tenant_context(info):
            _rotated, skipped = rotate_one_tenant(new, new)
        if skipped:
            all_clear = False
    if not all_clear:
        flash('Not every school\'s secrets are readable under the new key yet - run the rotation '
              'against every school first, with 0 skipped everywhere, before setting the '
              'environment variable.', 'error')
        return redirect(url_for('platform_settings_rotate_key'))
    env_file.write_value('BRIGHTSTARS_DELIVERY_KEY', new)
    _log_action('rotate-delivery-key: BRIGHTSTARS_DELIVERY_KEY set to the new key')
    flash('BRIGHTSTARS_DELIVERY_KEY is now the new key. This will not take effect on the running '
          'application until it is restarted - restart it now.', 'success')
    return redirect(url_for('platform_settings'))
