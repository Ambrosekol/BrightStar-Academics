"""Platform-operator identity, and the controlled way into a school.

Two separate things live here:

* **Platform admin sign-in** against the registry database. A platform admin is
  not a school account and never appears in a school's sign-in form.
* **Entering a school.** Each school is on its own domain, so the platform
  console cannot set a session cookie for it. It mints a single-use,
  short-lived token (only its hash is stored) and sends the operator to the
  school's own domain to redeem it. Redemption signs them in against a reserved
  account in that school's database and records the entry on both sides.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from werkzeug.security import check_password_hash, generate_password_hash

from .context import tenant_context
from .models import PlatformAdmin, PlatformEntryToken, Tenant
from .provisioning import audit
from .registry import now_iso, platform_session

# A school's own account form accepts only letters, digits, dots, hyphens and
# underscores, so no school admin can create or impersonate a username
# containing "@". That makes this prefix a namespace only the platform can use.
PLATFORM_ADMIN_USERNAME_PREFIX = 'platform@'

TOKEN_LIFETIME_SECONDS = 120


def _hash_token(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def _as_dict(row):
    return {'id': row.id, 'username': row.username, 'display_name': row.display_name,
            'email': row.email, 'password_must_change': row.password_must_change}


def authenticate_platform_admin(username, password):
    """Return the platform admin for these credentials, or None."""
    username = (username or '').strip().lower()
    if not username or not password:
        return None
    with platform_session() as session:
        admin = session.scalars(sa.select(PlatformAdmin).where(
            PlatformAdmin.username == username, PlatformAdmin.active == 1)).first()
        if not admin or not check_password_hash(admin.password_hash or '', password):
            return None
        admin.last_login_at = now_iso()
        result = _as_dict(admin)
        session.commit()
        return result


def platform_admin_by_id(admin_id):
    with platform_session() as session:
        admin = session.scalars(sa.select(PlatformAdmin).where(
            PlatformAdmin.id == admin_id, PlatformAdmin.active == 1)).first()
        return _as_dict(admin) if admin else None


def set_platform_password(admin_id, current_password, new_password):
    """Change a platform admin's own password. Returns an error message or None."""
    if len(new_password or '') < 10:
        return 'Your new password must be at least 10 characters long.'
    with platform_session() as session:
        admin = session.scalars(sa.select(PlatformAdmin).where(
            PlatformAdmin.id == admin_id, PlatformAdmin.active == 1)).first()
        if not admin:
            return 'Your account is no longer active.'
        if not check_password_hash(admin.password_hash or '', current_password or ''):
            return 'Your current password is incorrect.'
        admin.password_hash = generate_password_hash(new_password)
        admin.password_must_change = 0
        audit(session, 'platform_admin.password_change', admin.username, None, admin.username, admin.id)
        session.commit()
    return None


def mint_entry_token(platform_admin_id, tenant_id, ip=None):
    """Create a one-time ticket into a school and return the raw token."""
    raw = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    with platform_session() as session:
        session.add(PlatformEntryToken(
            token_hash=_hash_token(raw), platform_admin_id=platform_admin_id, tenant_id=tenant_id,
            created_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=TOKEN_LIFETIME_SECONDS)).isoformat(),
            created_ip=ip))
        session.commit()
    return raw


def redeem_entry_token(raw, tenant):
    """Consume a token for ``tenant`` and return the platform admin, or None.

    The token is marked used inside the same transaction that reads it, so a
    token that is replayed (or raced) is only ever accepted once.
    """
    if not raw:
        return None
    now = datetime.now(timezone.utc).isoformat()
    with platform_session() as session:
        token = session.scalars(sa.select(PlatformEntryToken).where(
            PlatformEntryToken.token_hash == _hash_token(raw))).first()
        if token is None or token.used_at or token.tenant_id != tenant.id or token.expires_at <= now:
            return None
        admin = session.scalars(sa.select(PlatformAdmin).where(
            PlatformAdmin.id == token.platform_admin_id, PlatformAdmin.active == 1)).first()
        if admin is None:
            return None
        token.used_at = now
        result = _as_dict(admin)
        audit(session, 'tenant.enter', f'{admin.username} entered {tenant.slug}',
              tenant.id, admin.username, admin.id)
        session.commit()
        return result


def purge_expired_entry_tokens():
    """Drop spent and expired tickets so the table does not grow without bound."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with platform_session() as session:
        session.execute(sa.delete(PlatformEntryToken).where(PlatformEntryToken.created_at < cutoff))
        session.commit()


def school_account_for(platform_admin):
    """Find or refresh this operator's reserved account in the current school.

    The account is a real row in the school's own database so that audit
    entries, messages and every other ``admin_id`` foreign key behave normally.
    Its password is replaced with the hash of a fresh random secret on every
    entry, so it can never be signed into through the school's login form — even
    if a school admin previously reset it.

    Must be called inside ``tenant_context``.
    """
    from app import db  # deferred: the app imports this package at start-up
    from models import Admin, AdminType

    username = f"{PLATFORM_ADMIN_USERNAME_PREFIX}{platform_admin['username']}"
    display = f"{platform_admin['display_name']} (Brightstars Academics)"
    role_id = db.session.scalars(
        sa.select(AdminType.id).where(AdminType.is_system == 1).order_by(AdminType.id)).first()
    if role_id is None:
        raise RuntimeError('This school has no system role; run "upgrade" for it first.')

    admin = db.session.scalars(sa.select(Admin).where(Admin.username == username)).first()
    if admin is None:
        admin = Admin(username=username, display_name=display, admin_type_id=role_id,
                      created_at=now_iso())
        db.session.add(admin)
    admin.display_name = display
    admin.admin_type_id = role_id
    admin.active = 1
    admin.password_must_change = 0
    admin.password_hash = generate_password_hash(secrets.token_urlsafe(64))
    db.session.commit()
    return admin.id


def enter_school(raw_token, tenant):
    """Redeem a token and return ``(platform_admin, school_admin_id)``, or ``(None, None)``."""
    platform_admin = redeem_entry_token(raw_token, tenant)
    if platform_admin is None:
        return None, None
    with tenant_context(tenant):
        return platform_admin, school_account_for(platform_admin)


def tenant_stats(info):
    """A few headline counts for one school, or None if it cannot be read.

    Read-only and defensive: a school whose database is missing or mid-upgrade
    must not take the platform dashboard down with it.
    """
    from .routing import engine_for

    try:
        with engine_for(info).connect() as conn:
            def count(sql):
                try:
                    return conn.execute(sa.text(sql)).scalar()
                except Exception:
                    return None
            return {
                'students': count('SELECT COUNT(*) FROM students WHERE active = 1'),
                'admins': count('SELECT COUNT(*) FROM admins WHERE active = 1'),
                'classes': count('SELECT COUNT(*) FROM school_classes WHERE active = 1'),
            }
    except Exception:
        return None


def school_admins(info):
    """The administrator accounts in one school, for the platform's school page."""
    from .routing import engine_for

    try:
        with engine_for(info).connect() as conn:
            rows = conn.execute(sa.text(
                'SELECT a.username, a.display_name, a.active, a.last_login_at, t.name AS role '
                'FROM admins a LEFT JOIN admin_types t ON t.id = a.admin_type_id '
                'ORDER BY a.id')).mappings().all()
        return [dict(r) for r in rows]
    except Exception:
        return []
