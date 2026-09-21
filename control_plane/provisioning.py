"""Create, register and upgrade the schools served by this deployment.

Every school gets its own PostgreSQL database, created here and named after
the school. Pass an explicit ``db_url`` (and optionally ``db_schema``) to place
a school somewhere else, such as a separate server.

The functions that build a school's tables import the Flask application
lazily, so importing this module (or running registry-only commands such as
``list`` or ``suspend``) never starts the app.
"""

import re
import shutil
import sqlite3
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import make_url
from werkzeug.security import generate_password_hash

from core import branding as branding_core

from . import config
from .context import tenant_context
from .models import (
    DOMAIN_CUSTOM, DOMAIN_PORTAL, PlatformAdmin, PlatformAuditLog, ROLE_ADMIN, ROLE_SUPER, Tenant,
    TenantDomain, TENANT_ACTIVE, TENANT_SUSPENDED,
)
from .registry import (
    clear_cache, get_tenant, init_platform_db, now_iso, platform_session, to_info,
    validate_hostname, validate_slug,
)
from .routing import build_engine, ensure_database_exists, resolve_db_url


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
    """Create the school's container folder and its standard subfolders."""
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


def upgrade_all_tenants():
    """Bring every school's schema up to date (run at start-up)."""
    with platform_session() as session:
        infos = [to_info(t) for t in session.scalars(sa.select(Tenant).order_by(Tenant.id))]
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
                  admin_display_name=None, branding=None, logo=None, gallery=(), actor='cli'):
    """Register a new school and build its database.

    The school's portal hostname is generated here and works immediately; any
    ``hostnames`` given are the school's own addresses, which reach it once
    their DNS points a CNAME at that portal hostname.

    ``logo`` and ``gallery`` (the photographs shown on the school's sign-in page)
    are uploaded files. Every choice that can be checked without a school — the
    colours and the images — is checked first, so a bad one is reported before a
    database and a folder have been created for the school.

    Returns ``(info, admin_password)``.
    """
    validate_slug(slug)
    name = (name or '').strip()
    if not name:
        raise ProvisioningError('A school name is required.')
    branding, gallery = check_branding_inputs(branding, logo, gallery)
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


def _sqlite_connection(path):
    """Read-only connection to a SQLite database that may still be in use."""
    con = sqlite3.connect(f'{Path(path).resolve().as_uri()}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    return con


def import_sqlite_database(info, source_db):
    """Copy every row of a single-school SQLite database into a school's own
    database, table by table, in dependency order.

    The school's schema must already exist (``upgrade_tenant``). Only columns
    that both sides have are copied, so a source from an older release is fine.
    The source is opened read-only and is never modified.

    Returns ``{table: rows copied}``.
    """
    A = _app_module()
    con = _sqlite_connection(source_db)
    try:
        present = {r['name'] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        copied = {}
        with A.app.app_context(), tenant_context(info):
            engine = A.db.session.get_bind()
            metadata = A.db.metadata
            with engine.begin() as conn:
                # Seed rows created by init_db() would collide with the real
                # ones, so each table is emptied immediately before it is filled.
                for table in reversed(metadata.sorted_tables):
                    if table.name in present:
                        conn.execute(table.delete())
                for table in metadata.sorted_tables:
                    if table.name not in present:
                        continue
                    source_cols = {r['name'] for r in con.execute(
                        f'PRAGMA table_info("{table.name}")')}
                    cols = [c.name for c in table.columns if c.name in source_cols]
                    if not cols:
                        continue
                    quoted = ', '.join(f'"{c}"' for c in cols)
                    rows = [dict(zip(cols, r)) for r in con.execute(
                        f'SELECT {quoted} FROM "{table.name}"')]
                    if rows:
                        conn.execute(table.insert(), rows)
                    copied[table.name] = len(rows)
                _reset_sequences(conn, metadata)
        return copied
    finally:
        con.close()


def _reset_sequences(conn, metadata):
    """Point each PostgreSQL identity sequence past the rows just imported.

    Without this the first insert after an import would reuse id 1 and collide
    with an imported row.
    """
    if conn.dialect.name != 'postgresql':
        return
    for table in metadata.sorted_tables:
        for column in table.primary_key.columns:
            if not isinstance(column.type, sa.Integer):
                continue
            seq = conn.execute(
                sa.text('SELECT pg_get_serial_sequence(:t, :c)'),
                {'t': table.name, 'c': column.name}).scalar()
            if not seq:
                continue
            conn.execute(sa.text(
                f'SELECT setval(:s, COALESCE((SELECT MAX("{column.name}") FROM "{table.name}"), 0) + 1, false)'),
                {'s': seq})


def register_existing_tenant(slug, name, hostnames=(), source_db=None, source_data=None,
                             source_uploads=None, actor='cli'):
    """Bring an existing single-school SQLite installation in as a school.

    The school is created on PostgreSQL like any other, then every row of the
    old database is imported into it and its question banks and uploads are
    copied into the school's own folder. The originals are only ever read, so
    the old installation stays intact as a fallback until it is retired by hand.
    """
    validate_slug(slug)
    source_db = Path(source_db)
    if not source_db.is_file():
        raise ProvisioningError(f'Source database not found: {source_db}')

    info, _ = create_tenant(slug, name, hostnames, actor=actor)
    try:
        folder = tenant_folder(slug)
        for source, kind in ((source_data, 'data'), (source_uploads, 'uploads')):
            if source and Path(source).is_dir():
                shutil.copytree(source, folder / kind, dirs_exist_ok=True)
        copied = import_sqlite_database(info, source_db)
        # The imported rows carry the school's real identity; re-apply the name
        # from the registry only if the old database never had one.
        apply_branding(info, {})
        record('tenant.import_sqlite',
               f'{slug} from {source_db}: {sum(copied.values())} rows', info.id, actor)
    except Exception:
        with platform_session() as session:
            tenant = get_tenant(session, slug)
            if tenant:
                session.delete(tenant)
                session.commit()
        clear_cache()
        raise
    return info


def adopt_superadmins(source_db, username=None, actor='cli'):
    """Copy an existing installation's Super Admin account(s) into the platform.

    They keep their username and password (the hash is copied, never a
    plaintext), and become platform admins who may enter any school. The source
    database is opened read-only and not modified.
    """
    src = sqlite3.connect(f'{Path(source_db).resolve().as_uri()}?mode=ro', uri=True)
    src.row_factory = sqlite3.Row
    try:
        rows = src.execute(
            'SELECT a.username, a.display_name, a.password_hash, a.email, a.phone '
            'FROM admins a JOIN admin_types t ON t.id = a.admin_type_id '
            'WHERE t.is_system = 1 AND a.active = 1 ORDER BY a.id').fetchall()
    finally:
        src.close()
    if username:
        rows = [r for r in rows if r['username'] == username.strip().lower()]
    if not rows:
        raise ProvisioningError('No matching active Super Admin found in the source database.')
    init_platform_db()
    adopted = []
    with platform_session() as session:
        for r in rows:
            if session.scalars(sa.select(PlatformAdmin).where(PlatformAdmin.username == r['username'])).first():
                continue
            session.add(PlatformAdmin(username=r['username'], display_name=r['display_name'],
                                      password_hash=r['password_hash'], email=r['email'], phone=r['phone'],
                                      active=1, password_must_change=0, created_at=now_iso()))
            adopted.append(r['username'])
        audit(session, 'platform_admin.adopt', ', '.join(adopted) or 'none new', None, actor)
        session.commit()
    return adopted


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

