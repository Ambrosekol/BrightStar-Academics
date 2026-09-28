"""Create, register and upgrade the schools served by this deployment.

Every school gets its own PostgreSQL database, created here and named after
the school. Pass an explicit ``db_url`` (and optionally ``db_schema``) to place
a school somewhere else, such as a separate server.

The functions that build a school's tables import the Flask application
lazily, so importing this module (or running registry-only commands such as
``list`` or ``suspend``) never starts the app.
"""

import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import make_url
from werkzeug.security import generate_password_hash

from core import branding as branding_core
from core import numbering

from . import config
from .banks import add_starter_banks
from .context import tenant_context
from .models import (
    DOMAIN_CUSTOM, DOMAIN_PORTAL, PlatformAdmin, PlatformAuditLog, ROLE_ADMIN, ROLE_SUPER, Tenant,
    TenantDomain, TENANT_ACTIVE, TENANT_SUSPENDED,
)
from .registry import (
    clear_cache, get_tenant, init_platform_db, now_iso, platform_session, to_info,
    validate_hostname, validate_slug,
)
from .routing import build_engine, engine_for, ensure_database_exists, resolve_db_url


class ProvisioningError(Exception):
    """A request that cannot be carried out; the message is safe to show."""


def _app_module():
    import app as A  # deferred: the app imports this package at start-up
    return A


def audit(session, action, detail=None, tenant_id=None, actor='cli', admin_id=None, ip=None):
    """Add one entry to the platform audit trail.

    Most callers know only the actor's username, so the admin's id is looked up
    here: an entry that names an admin must always be attributable to that
    admin's account, or their activity log would silently miss it.
    """
    if admin_id is None and actor not in (None, 'cli', 'system'):
        admin_id = session.scalars(sa.select(PlatformAdmin.id).where(PlatformAdmin.username == actor)).first()
    session.add(PlatformAuditLog(platform_admin_id=admin_id, actor_username=actor, tenant_id=tenant_id,
                                 action=action, detail=detail, ip_address=ip, created_at=now_iso()))


def suggest_slug(name):
    """A safe school code derived from a school's name ("St. Mary's" -> "st-marys")."""
    slug = re.sub(r'[^a-z0-9]+', '-', (name or '').lower()).strip('-')[:40].strip('-')
    return slug or 'school'


# Every school's files live in one folder of their own: its question banks and
# everything anyone uploads. Nothing is shared with another school.
TENANT_SUBFOLDERS = ('data', 'uploads')


def tenant_folder(slug_or_info):
    """The folder that contains everything belonging to one school."""
    slug = getattr(slug_or_info, 'storage_key', slug_or_info)
    validate_slug(slug)
    return Path(config.tenants_dir()).resolve() / slug


def ensure_tenant_folders(slug_or_info):
    """Create the school's container folder and its standard subfolders.

    The school's numbering rules (``numbering.json``) are not made here: a school that has no
    such file numbers by the default pattern, and a new school is given its file, from the form
    the operator filled in, by ``create_tenant``.
    """
    root = tenant_folder(slug_or_info)
    for sub in TENANT_SUBFOLDERS:
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def tenant_folder_listing(info):
    """What is actually on disk for one school, for the platform console."""
    root = tenant_folder(info)
    entries = []
    if root.is_dir():
        for child in sorted(root.iterdir(), key=lambda p: (p.is_file(), p.name)):
            if child.is_dir():
                files = sum(1 for _ in child.rglob('*') if _.is_file())
                entries.append({'name': child.name + '/', 'detail': f'{files} file(s)'})
            else:
                entries.append({'name': child.name, 'detail': f'{child.stat().st_size / 1024:.0f} KB'})
    return {'root': str(root), 'exists': root.is_dir(), 'entries': entries}


def _default_db_url(slug):
    """Where a new school's database lives when none was specified."""
    ensure_tenant_folders(slug)
    return config.school_db_url(slug)


def _check_hostnames(session, hostnames, taken=()):
    cleaned = []
    for raw in hostnames:
        host = validate_hostname(raw)
        if host in config.platform_hosts():
            raise ProvisioningError(f'{host} is reserved for the platform console.')
        if host == config.portal_domain():
            raise ProvisioningError(f"{host} is the platform's own portal domain.")
        if host in taken or session.scalars(
                sa.select(TenantDomain).where(TenantDomain.hostname == host)).first():
            raise ProvisioningError(f'{host} already belongs to another school.')
        if host not in cleaned:
            cleaned.append(host)
    return cleaned


def _prepare_database(db_url, db_schema):
    """Make sure a school's database — and its schema — exist before its tables
    are built."""
    try:
        ensure_database_exists(db_url)
    except Exception as exc:
        raise ProvisioningError(
            f"Could not reach PostgreSQL to create this school's database: {exc}") from exc
    url = make_url(resolve_db_url(db_url))
    if db_schema and url.get_backend_name() == 'postgresql':
        engine = build_engine(db_url)  # deliberately without the school's search_path
        try:
            with engine.begin() as conn:
                conn.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{db_schema}"'))
        finally:
            engine.dispose()


def upgrade_tenant(info):
    """Create/upgrade one school's tables and reference data. Idempotent.

    Existing school rows are adopted as they are, and no account is ever
    created inside a school: the platform's own operators are the super admins.
    """
    A = _app_module()
    ensure_tenant_folders(info)
    _prepare_database(info.db_url, info.db_schema)
    with A.app.app_context(), tenant_context(info):
        A.init_db(school={'code': info.slug.upper(), 'name': info.name,
                          'motto': None, 'adopt_existing': True})


def list_tenant_infos():
    """Every school, as the plain description the rest of the code works with."""
    with platform_session() as session:
        return [to_info(t) for t in session.scalars(sa.select(Tenant).order_by(Tenant.id))]


def upgrade_all_tenants():
    """Bring every school's schema up to date (run at start-up)."""
    infos = list_tenant_infos()
    for info in infos:
        upgrade_tenant(info)
    return infos


def create_school_admin(info, username, display_name=None):
    """Give a school its first administrator, on the school's top-level role.

    Returns the one-time temporary password; the account must change it at first
    sign-in, exactly like accounts created through the school's own admin UI.
    """
    A = _app_module()
    username = (username or '').strip().lower()
    if not username:
        raise ProvisioningError('An admin username is required.')
    with A.app.app_context(), tenant_context(info):
        role = A.db.session.scalars(
            sa.select(A.AdminType).where(A.AdminType.is_system == 1).order_by(A.AdminType.id)).first()
        if role is None:
            raise ProvisioningError('The school has no top-level role yet; run "upgrade" first.')
        if A.db.session.scalars(sa.select(A.Admin).where(A.Admin.username == username)).first():
            raise ProvisioningError(f'{username} already exists in {info.slug}.')
        password = A._new_admin_password()
        A.db.session.add(A.Admin(
            username=username, display_name=display_name or username,
            password_hash=generate_password_hash(password), admin_type_id=role.id,
            active=1, password_must_change=1, created_at=now_iso()))
        A.db.session.commit()
    return password


def create_tenant(slug, name, hostnames=(), db_url=None, db_schema=None, admin_username=None,
                  admin_display_name=None, branding=None, logo=None, gallery=(), starter_banks=True,
                  actor='cli', numbering_rules=None):
    """Register a new school and build its database.

    The school's portal hostname is generated here and works immediately; any
    ``hostnames`` given are the school's own addresses, which reach it once
    their DNS points a CNAME at that portal hostname.

    ``logo`` and ``gallery`` (the photographs shown on the school's sign-in page)
    are uploaded files. Every choice that can be checked without a school — the
    colours and the images — is checked first, so a bad one is reported before a
    database and a folder have been created for the school. That includes the school's
    numbering rules: ``numbering_rules`` is a dict with ``candidate_pattern``, ``student_pattern``
    and ``first_number`` (anything left out is the default), or None for the defaults.

    Returns ``(info, admin_password)``.
    """
    validate_slug(slug)
    name = (name or '').strip()
    if not name:
        raise ProvisioningError('A school name is required.')
    branding, gallery = check_branding_inputs(branding, logo, gallery)
    rules, rule_facts = check_numbering_inputs(slug, name, numbering_rules)
    init_platform_db()
    portal_host = config.portal_hostname(slug)
    with platform_session() as session:
        if get_tenant(session, slug):
            raise ProvisioningError(f'A school with code "{slug}" already exists.')
        portal_host = _check_hostnames(session, [portal_host])[0]
        customs = _check_hostnames(session, hostnames, taken=[portal_host])
        db_url = db_url or _default_db_url(slug)
        tenant = Tenant(slug=slug, name=name, status=TENANT_ACTIVE, db_url=db_url,
                        db_schema=db_schema or None, storage_key=slug, created_at=now_iso())
        tenant.domains = [TenantDomain(hostname=portal_host, kind=DOMAIN_PORTAL, is_primary=1,
                                       created_at=now_iso())]
        tenant.domains += [TenantDomain(hostname=h, kind=DOMAIN_CUSTOM, is_primary=0,
                                        created_at=now_iso()) for h in customs]
        session.add(tenant)
        session.flush()
        audit(session, 'tenant.create', f'{slug} at {portal_host}', tenant.id, actor)
        session.commit()
        info = to_info(tenant)
    clear_cache()
    try:
        upgrade_tenant(info)
        apply_branding(info, branding or {'school_name': name}, logo, gallery)
        try:
            numbering.write_rules(tenant_folder(info), rules, rule_facts)  # written even if it is the default
        except numbering.NumberingRuleError as exc:
            raise ProvisioningError(str(exc)) from None
        if not numbering.is_default(rules):
            record('tenant.numbering_update', _describe_rules(None, rules), info.id, actor)
        if starter_banks:
            add_starter_banks(info)  # the standard entrance banks, copied into the school's own folder
        password = create_school_admin(info, admin_username, admin_display_name) if admin_username else None
    except Exception:
        # Do not leave a registered school that has no usable database.
        with platform_session() as session:
            tenant = get_tenant(session, slug)
            if tenant:
                session.delete(tenant)
                session.commit()
        clear_cache()
        raise
    return info, password


# A school's own identity — name, colours, logo, photographs — is written by
# core/branding.py, which the school's own admin area uses as well, so the two
# can never disagree about what is allowed. These are the platform-side wrappers:
# they select the school and turn a refused choice into a ProvisioningError.
BRANDING_FIELDS = branding_core.BRANDING_FIELDS
LOGO_SETTING_KEY = branding_core.LOGO_SETTING_KEY
max_branding_request_bytes = branding_core.max_branding_request_bytes


def check_branding_inputs(branding, logo=None, gallery=()):
    """Validate a school's colours and images without touching any school.

    Raises :class:`ProvisioningError` with a message safe to show.
    """
    try:
        return branding_core.check_branding_inputs(branding, logo, gallery)
    except ValueError as exc:
        raise ProvisioningError(str(exc)) from None


def apply_branding(info, branding=None, logo=None, gallery=(), remove_gallery=(), partial=False):
    """Write a school's name, colours and contact details into its own database,
    and store its logo and sign-in photographs inside its own folder.

    ``partial`` is for changing a school that already exists: nothing is
    defaulted, and a field that is not passed is left exactly as it is.
    """
    A = _app_module()
    branding = dict(branding or {})
    if not partial and not (branding.get('school_name') or '').strip():
        # A school must always carry its own name, even when it was created from
        # the command line with no branding at all — otherwise its portal would
        # fall back to the name of whichever school this code base started life as.
        branding['school_name'] = info.name
    with A.app.app_context(), tenant_context(info):
        try:
            branding_core.store_branding(info.slug, branding, logo, gallery, remove_gallery)
        except ValueError as exc:
            A.db.session.rollback()
            raise ProvisioningError(str(exc)) from None


def set_display_name(slug, name):
    """Make the platform's own record of a school carry the name the school now uses.

    The console lists schools by the registry's name, so when a school renames itself the two
    must not drift apart. The school's code (its address and folder) never changes.
    """
    name = (name or '').strip()
    if not name:
        return
    with platform_session() as session:
        tenant = get_tenant(session, slug)
        if tenant is not None and tenant.name != name:
            tenant.name = name
            tenant.updated_at = now_iso()
            session.commit()
    clear_cache()


def update_branding(info, branding=None, logo=None, gallery=(), remove_gallery=()):
    """Change an existing school's colours, logo and sign-in photographs.

    Validated as thoroughly as at creation, and nothing else about the school
    is touched.
    """
    branding, gallery = check_branding_inputs(branding, logo, gallery)
    apply_branding(info, branding, logo, gallery, remove_gallery, partial=True)


# A school's numbering rules (numbering.json in its own folder) are patterns that are read and
# checked, never run: see core/numbering.py. These are the platform-side wrappers: they turn a
# refused pattern into a ProvisioningError, and read a school's own database, read-only, for the
# console's previews.

def check_numbering_inputs(slug, name, given=None):
    """Validate the numbering rules chosen for a school without touching any school.

    ``given`` is a dict with ``candidate_pattern``, ``student_pattern`` and ``first_number``;
    whatever is missing is the default. Returns ``(rules, facts)``; raises
    :class:`ProvisioningError` with a message safe to show.
    """
    given = given or {}
    facts = numbering.Facts(school_code=(slug or '').upper(), school_name=name or '')
    try:
        rules = numbering.check_rules(
            given.get('candidate_pattern', numbering.DEFAULT_CANDIDATE_PATTERN),
            given.get('student_pattern', numbering.DEFAULT_STUDENT_PATTERN),
            given.get('first_number', numbering.DEFAULT_FIRST_NUMBER), facts)
    except numbering.NumberingRuleError as exc:
        raise ProvisioningError(str(exc)) from None
    return rules, facts


def _describe_rules(old, new):
    def shown(pattern):
        return pattern or '(the numbering policy)'

    if old is None:
        return (f'candidate code: {new.candidate_pattern}; student number: {shown(new.student_pattern)}; '
                f'first number: {new.first_number}')
    return (f'candidate code: {old.candidate_pattern} -> {new.candidate_pattern}; '
            f'student number: {shown(old.student_pattern)} -> {shown(new.student_pattern)}; '
            f'first number: {old.first_number} -> {new.first_number}')


def numbering_of(info):
    """``(rules, problem)``: a school's numbering rules, or the default and a plain message if its
    numbering.json cannot be used (which the school's page shows, and never hides)."""
    try:
        return numbering.read_rules(tenant_folder(info)), None
    except numbering.NumberingRuleError as exc:
        return numbering.DEFAULT_RULES, str(exc)


def _read_school(info):
    """The school's own code and name, read from its database without writing anything. Falls back
    to what the registry knows if the database cannot be read."""
    found = {'code': (info.slug or '').upper(), 'name': info.name or ''}
    try:
        with engine_for(info).connect() as conn:
            conn.execution_options(postgresql_readonly=True)
            row = conn.execute(sa.text('SELECT code, name FROM schools ORDER BY id LIMIT 1')).first()
        if row:
            found = {'code': (row[0] or found['code']).upper(), 'name': row[1] or found['name']}
    except Exception:
        pass
    return found


def _read_policy(info):
    with engine_for(info).connect() as conn:
        conn.execution_options(postgresql_readonly=True)
        row = conn.execute(sa.text(
            'SELECT prefix, include_year, padding, next_sequence FROM school_numbering_policies '
            'ORDER BY id LIMIT 1')).first()
    return ({'prefix': row[0], 'include_year': row[1], 'padding': row[2], 'next_sequence': row[3]}
            if row else {})


def _read_candidate_codes(info, prefix):
    """Existing candidate codes that begin with ``prefix`` (all of them if it is empty), read-only."""
    statement = 'SELECT candidate_code FROM candidates'
    params = {}
    if prefix:
        statement += ' WHERE left(candidate_code, :size) = :prefix'
        params = {'size': len(prefix), 'prefix': prefix}
    with engine_for(info).connect() as conn:
        conn.execution_options(postgresql_readonly=True)
        return [code for (code,) in conn.execute(sa.text(statement), params)]


def update_numbering(info, candidate_pattern, student_pattern, first_number, actor='cli', admin_id=None):
    """Change a school's numbering rules. Everything is checked first, the file is replaced whole,
    and the old and new patterns go into the platform audit trail. Codes already issued are not
    touched. Returns the new :class:`numbering.Rules`."""
    school = _read_school(info)
    facts = numbering.Facts(school_code=school['code'], school_name=school['name'])
    try:
        old, unreadable = numbering.save_rules(
            tenant_folder(info), numbering.Rules(candidate_pattern, student_pattern, first_number), facts)
        new = numbering.read_rules(tenant_folder(info))
    except numbering.NumberingRuleError as exc:
        raise ProvisioningError(str(exc)) from None
    detail = _describe_rules(old, new) if old is not None else (
        'the old file could not be read and was replaced (a copy was kept); ' + _describe_rules(None, new))
    record('tenant.numbering_update', detail, info.id, actor, admin_id)
    return new


def preview_numbering(candidate_pattern, student_pattern, first_number, code, name, info=None):
    """What a set of numbering rules would make, for the console's live preview. Never writes.

    For a school that exists (``info``) the next numbers are the real ones, read from its own
    candidates and numbering policy; for a school still being created (``code`` and ``name`` as
    typed on the form) they are examples. A school that cannot be read still gets a preview.
    """
    example_policy = None
    if info is not None:
        school = _read_school(info)
        facts = numbering.Facts(school_code=school['code'], school_name=school['name'], target_class='JSS 1')
    else:
        facts = numbering.Facts(school_code=(code or 'CODE').upper(), school_name=name or 'Your school',
                                target_class='JSS 1')
    example_policy = {'prefix': facts.school_code, 'include_year': 1, 'padding': 4, 'next_sequence': 1}
    if info is not None:
        try:
            return numbering.preview(candidate_pattern, student_pattern, first_number, facts,
                                     candidate_codes=lambda prefix: _read_candidate_codes(info, prefix),
                                     policy=_read_policy(info))
        except Exception:
            result = numbering.preview(candidate_pattern, student_pattern, first_number, facts,
                                       policy=example_policy)
            result['unreadable'] = ("This school's candidates could not be read just now, so these are "
                                    "examples rather than its real next numbers.")
            return result
    return numbering.preview(candidate_pattern, student_pattern, first_number, facts, policy=example_policy)


def create_platform_admin(username, display_name, password, actor='cli', superadmin=False):
    """Create a platform admin from the command line.

    The first admin on a platform becomes its super admin automatically; after
    that, pass ``superadmin=True`` to add another. The console only ever creates
    ordinary platform admins (see team.py).
    """
    username = (username or '').strip().lower()
    if not username or len(password or '') < 10:
        raise ProvisioningError('A username and a password of at least 10 characters are required.')
    init_platform_db()
    with platform_session() as session:
        if session.scalars(sa.select(PlatformAdmin).where(PlatformAdmin.username == username)).first():
            raise ProvisioningError(f'Platform admin {username} already exists.')
        has_super = session.scalars(sa.select(PlatformAdmin.id).where(
            PlatformAdmin.role == ROLE_SUPER, PlatformAdmin.active == 1)).first() is not None
        role = ROLE_SUPER if (superadmin or not has_super) else ROLE_ADMIN
        session.add(PlatformAdmin(username=username, display_name=display_name or username,
                                  password_hash=generate_password_hash(password), active=1, role=role,
                                  password_must_change=0, created_at=now_iso()))
        audit(session, 'platform_admin.create', f'{username} ({role})', None, actor)
        session.commit()
    return role


def create_school_role(slug, rotate=False, password=None):
    """A PostgreSQL role that owns exactly this school's own database, and nothing else - the
    separation recommendations.html's Hardening section asks for, so that a compromised worker
    context (one bad query, one leaked connection string) is limited to one school's data rather
    than reaching every school through the one role every database is created with today.

    ``password`` is optional: leave it out for a strong, randomly generated one (the usual
    choice); give one to set it yourself instead, e.g. to match a policy of your own. Either way it
    is safely escaped as a standard SQL string literal before being sent (PostgreSQL refuses a bound
    parameter in this position, since CREATE/ALTER ROLE is DDL), so a quote in it cannot break out
    of the statement.

    ``ALTER DATABASE ... OWNER TO`` alone only changes who may drop or alter the *database itself*;
    the tables inside it (already created, under ``upgrade_tenant``, by whatever role the school
    used before this) stay owned by that role, and the new role could connect but not read a single
    row. So on first creation (never on a plain ``--rotate``, which only changes an existing role's
    password and already owns everything) this also hands every existing table and sequence to the
    new role individually (``ALTER TABLE/SEQUENCE ... OWNER TO``) - not the coarser
    ``REASSIGN OWNED BY``, which PostgreSQL refuses when the previous owner is a superuser role such
    as the shared ``postgres`` account this codebase's own default setup connects as, since some of
    what it owns is tied to the database system itself.

    Returns ``(role_name, connection_url)``. Nothing is written to the registry: set_db_url does
    that, deliberately as a second, separate step, since the registry's own db_url already avoids
    holding a live password in the clear (an ``env:VARIABLE_NAME`` reference) and this function has
    no way to know what an operator wants to call that variable.
    """
    with platform_session() as session:
        tenant = get_tenant(session, slug)
        if not tenant:
            raise ProvisioningError(f'No school with code "{slug}".')
        current_url = tenant.db_url
        schema = tenant.db_schema or 'public'
    url = make_url(resolve_db_url(current_url))
    if url.get_backend_name() != 'postgresql':
        raise ProvisioningError('A separate database role only applies to a school on PostgreSQL.')
    role_name = 'school_' + re.sub(r'[^a-z0-9]+', '_', slug.lower()).strip('_')
    if password:
        if len(password) < 10:
            raise ProvisioningError('A password you choose yourself must be at least 10 characters.')
        if '\x00' in password:
            raise ProvisioningError('A password cannot contain a NUL character.')
    else:
        import secrets as _secrets
        password = _secrets.token_urlsafe(24)
    admin_engine = sa.create_engine(url.set(database='postgres'), isolation_level='AUTOCOMMIT')
    try:
        with admin_engine.connect() as conn:
            exists = conn.execute(sa.text('SELECT 1 FROM pg_roles WHERE rolname = :n'),
                                  {'n': role_name}).scalar()
            if exists and not rotate:
                raise ProvisioningError(
                    f'Role "{role_name}" already exists. Pass rotate=True to give it a new password.')
            verb = 'ALTER' if exists else 'CREATE'
            # PostgreSQL does not accept a bound parameter in this position (CREATE/ALTER ROLE is
            # DDL - confirmed against a real server, not assumed), so the password is quoted as a
            # standard SQL string literal by hand instead: a single quote is escaped by doubling it,
            # which is all standard_conforming_strings (PostgreSQL's default since 9.1) requires -
            # a backslash is not special there, only inside an E'...' literal, which this never uses.
            escaped = password.replace("'", "''")
            conn.execute(sa.text(f"{verb} ROLE \"{role_name}\" WITH LOGIN PASSWORD '{escaped}'"))
            conn.execute(sa.text(f'GRANT ALL PRIVILEGES ON DATABASE "{url.database}" TO "{role_name}"'))
            conn.execute(sa.text(f'ALTER DATABASE "{url.database}" OWNER TO "{role_name}"'))
    finally:
        admin_engine.dispose()
    if not exists:
        # Connects to the school's actual database (not the "postgres" maintenance one) with the
        # same credentials the school already uses today - needed to hand over ownership of what
        # already exists there.
        db_engine = sa.create_engine(url)
        try:
            with db_engine.begin() as conn:
                conn.execute(sa.text(f'GRANT ALL PRIVILEGES ON SCHEMA "{schema}" TO "{role_name}"'))
                tables = conn.execute(sa.text(
                    'SELECT tablename FROM pg_tables WHERE schemaname = :s'), {'s': schema}).scalars().all()
                for name in tables:
                    conn.execute(sa.text(f'ALTER TABLE "{schema}"."{name}" OWNER TO "{role_name}"'))
                sequences = conn.execute(sa.text(
                    'SELECT sequencename FROM pg_sequences WHERE schemaname = :s'), {'s': schema}).scalars().all()
                for name in sequences:
                    conn.execute(sa.text(f'ALTER SEQUENCE "{schema}"."{name}" OWNER TO "{role_name}"'))
                views = conn.execute(sa.text(
                    'SELECT viewname FROM pg_views WHERE schemaname = :s'), {'s': schema}).scalars().all()
                for name in views:
                    conn.execute(sa.text(f'ALTER VIEW "{schema}"."{name}" OWNER TO "{role_name}"'))
        finally:
            db_engine.dispose()
    new_url = url.set(username=role_name, password=password)
    return role_name, new_url.render_as_string(hide_password=False)


def set_db_url(slug, db_url, actor='cli'):
    """Point a school at a different connection string (a literal URL, or ``env:VARIABLE_NAME``
    to read one from the environment instead of storing it, in the clear, in the registry).

    Takes effect the next time this worker resolves the tenant (control_plane/routing.py caches
    engines per URL); other workers pick it up once their own registry cache expires.
    """
    with platform_session() as session:
        tenant = get_tenant(session, slug)
        if not tenant:
            raise ProvisioningError(f'No school with code "{slug}".')
        tenant.db_url = db_url
        tenant.updated_at = now_iso()
        # An env:NAME reference is shown as-is; a literal URL never shows its password.
        shown = db_url if db_url.lower().startswith('env:') else make_url(db_url).render_as_string(hide_password=True)
        audit(session, 'tenant.db_url_changed', shown, tenant.id, actor)
        session.commit()
    clear_cache()


def export_tenant_data(slug, output_dir):
    """Everything one school owns, in one folder: a database dump plus its own uploads - the "give
    me everything" a school leaving is owed, made straightforward by every school already having
    exactly one database and one folder of its own (recommendations.html's product recommendations).

    Writes ``<output_dir>/<slug>-<date>/database.sql`` (via ``pg_dump``, which must be on the
    machine running this - the same PostgreSQL install already needed to run the application does
    not always ship it on the application server itself) and a copy of the school's own files
    folder, plus a short README explaining what is here and how to bring it back with
    ``create-tenant --db-url ... && psql ... < database.sql``. Returns the export's own folder path.
    """
    with platform_session() as session:
        tenant = get_tenant(session, slug)
        if not tenant:
            raise ProvisioningError(f'No school with code "{slug}".')
        info = to_info(tenant)
    url = make_url(resolve_db_url(info.db_url))
    if url.get_backend_name() != 'postgresql':
        raise ProvisioningError('Exporting only supports a school on PostgreSQL.')
    if shutil.which('pg_dump') is None:
        raise ProvisioningError(
            'pg_dump was not found on this machine. Install the PostgreSQL client tools (the '
            'server package is not required) and try again.')

    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    export_dir = Path(output_dir) / f'{slug}-{stamp}'
    export_dir.mkdir(parents=True, exist_ok=False)

    dump_path = export_dir / 'database.sql'
    env = {**os.environ, 'PGPASSWORD': url.password or ''}
    args = ['pg_dump', '--no-owner', '--no-privileges',
            '-h', url.host or 'localhost', '-p', str(url.port or 5432),
            '-U', url.username or '', '-d', url.database, '-f', str(dump_path)]
    if info.db_schema:
        args[-2:-2] = ['-n', info.db_schema]
    result = subprocess.run(args, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        shutil.rmtree(export_dir, ignore_errors=True)
        raise ProvisioningError(f'pg_dump failed: {result.stderr[-2000:]}')

    from core import storage as _storage
    files_source = _storage.tenant_root(info)
    files_dest = export_dir / 'files'
    if Path(files_source).exists():
        shutil.copytree(files_source, files_dest)
    else:
        files_dest.mkdir()

    (export_dir / 'README.txt').write_text(
        f'Export of {info.name} ({info.slug}), made {stamp} UTC.\n\n'
        'database.sql - a plain-SQL pg_dump of this school\'s own database (--no-owner, so it can\n'
        'be restored into any role\'s database with a plain: psql -d your_new_database -f database.sql\n\n'
        'files/ - an exact copy of this school\'s own uploads folder (question bank images, student,\n'
        'candidate and staff photographs, signatures, branding, receipt attachments).\n\n'
        'To bring this school up again elsewhere: create its database, restore database.sql into it,\n'
        'point a new tenant at that database (control_plane create-tenant --db-url ...), then copy\n'
        'files/ back into that new tenant\'s own folder under BRIGHTSTARS_TENANTS_DIR.\n',
        encoding='utf-8')
    return str(export_dir)


def set_status(slug, status, reason=None, actor='cli'):
    if status not in (TENANT_ACTIVE, TENANT_SUSPENDED):
        raise ProvisioningError('Unknown status.')
    with platform_session() as session:
        tenant = get_tenant(session, slug)
        if not tenant:
            raise ProvisioningError(f'No school with code "{slug}".')
        tenant.status = status
        tenant.suspended_reason = (reason or None) if status == TENANT_SUSPENDED else None
        tenant.updated_at = now_iso()
        audit(session, f'tenant.{status}', reason, tenant.id, actor)
        session.commit()
    clear_cache()


def add_domain(slug, hostname, primary=False, actor='cli'):
    with platform_session() as session:
        tenant = get_tenant(session, slug)
        if not tenant:
            raise ProvisioningError(f'No school with code "{slug}".')
        host = _check_hostnames(session, [hostname])[0]
        if primary:
            for d in tenant.domains:
                d.is_primary = 0
        # Only the platform issues portal hostnames; anything added here is the
        # school's own address, pointed at its portal hostname by a CNAME.
        tenant.domains.append(TenantDomain(hostname=host, kind=DOMAIN_CUSTOM,
                                           is_primary=int(primary or not tenant.domains),
                                           created_at=now_iso()))
        audit(session, 'tenant.domain_add', host, tenant.id, actor)
        session.commit()
    clear_cache()


def remove_domain(hostname, actor='cli'):
    host = validate_hostname(hostname)
    with platform_session() as session:
        domain = session.scalars(sa.select(TenantDomain).where(TenantDomain.hostname == host)).first()
        if not domain:
            raise ProvisioningError(f'{host} is not registered.')
        if domain.kind == DOMAIN_PORTAL:
            raise ProvisioningError(
                f"{host} is this school's portal address and cannot be removed.")
        tenant_id = domain.tenant_id
        was_primary = bool(domain.is_primary)
        session.delete(domain)
        session.flush()
        if was_primary:
            # Never leave a school with no primary address to be entered at.
            remaining = session.scalars(sa.select(TenantDomain).where(
                TenantDomain.tenant_id == tenant_id).order_by(
                    TenantDomain.kind != DOMAIN_PORTAL, TenantDomain.id)).first()
            if remaining is not None:
                remaining.is_primary = 1
        audit(session, 'tenant.domain_remove', host, tenant_id, actor)
        session.commit()
    clear_cache()


def domains_of(slug):
    """``(portal hostname, [the school's own hostnames])`` for one school."""
    with platform_session() as session:
        tenant = get_tenant(session, slug)
        if not tenant:
            return None, []
        portal = next((d.hostname for d in tenant.domains if d.is_portal), None)
        return portal, [d.hostname for d in tenant.domains if not d.is_portal]


def branding_of(info):
    """The identity a school's portal shows, read back from its own database."""
    A = _app_module()
    try:
        with A.app.app_context(), tenant_context(info):
            return branding_core.branding_settings()
    except Exception:
        return {}


def record(action, detail=None, tenant_id=None, actor='cli', admin_id=None):
    """Write one platform audit entry outside any other transaction."""
    with platform_session() as session:
        audit(session, action, detail, tenant_id, actor, admin_id)
        session.commit()


def recent_audit(tenant_id=None, limit=50):
    """The platform audit trail, most recent first, with each school's code."""
    with platform_session() as session:
        stmt = (sa.select(PlatformAuditLog, Tenant.slug)
                  .outerjoin(Tenant, Tenant.id == PlatformAuditLog.tenant_id)
                  .order_by(PlatformAuditLog.id.desc()).limit(limit))
        if tenant_id is not None:
            stmt = stmt.where(PlatformAuditLog.tenant_id == tenant_id)
        return [{'action': row.action, 'detail': row.detail, 'actor': row.actor_username,
                 'created_at': row.created_at, 'slug': slug}
                for row, slug in session.execute(stmt)]


def list_tenants():
    def shown(value):
        # An env:NAME reference is shown as-is; a literal URL never shows its password.
        return value if value.lower().startswith('env:') else make_url(value).render_as_string(hide_password=True)

    with platform_session() as session:
        return [(t.slug, t.name, t.status,
                 next((d.hostname for d in t.domains if d.is_portal), None),
                 [d.hostname for d in t.domains if not d.is_portal], shown(t.db_url))
                for t in session.scalars(sa.select(Tenant).order_by(Tenant.id))]

