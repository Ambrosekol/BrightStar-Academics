"""Platform registry tables: which schools exist, which domains reach them, and
who may administer the platform itself.

These live in their own database (``BRIGHTSTARS_PLATFORM_DB``) with their own
declarative base, entirely separate from the per-school schema in ``models/``.
Nothing here is ever created inside a school's database, and a school's
database never contains a row describing another school.

Column conventions match ``models/``: ISO-8601 UTC text timestamps and 0/1
integer flags.
"""

from sqlalchemy import Float, ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, mapped_column, relationship

TENANT_ACTIVE = 'active'
TENANT_SUSPENDED = 'suspended'


class PlatformBase(DeclarativeBase):
    pass


class Tenant(PlatformBase):
    """One school served by this deployment."""

    __tablename__ = 'tenants'
    __table_args__ = (UniqueConstraint('slug'),)

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Short, URL-safe, permanent identifier. Also names the school's folder
    # under BRIGHTSTARS_TENANTS_DIR, so it must never be reused or renamed.
    slug = mapped_column(Text, nullable=False)
    name = mapped_column(Text, nullable=False)
    status = mapped_column(Text, nullable=False, default=TENANT_ACTIVE, server_default=text("'active'"))
    suspended_reason = mapped_column(Text)
    # SQLAlchemy URL of the school's database, or ``env:VARIABLE_NAME`` to read
    # the URL (and its password) from the environment instead of storing a
    # credential in the registry.
    db_url = mapped_column(Text, nullable=False)
    # PostgreSQL only: the schema that holds this school's tables when several
    # schools share one database.
    db_schema = mapped_column(Text)
    storage_key = mapped_column(Text, nullable=False)
    plan = mapped_column(Text)
    created_at = mapped_column(Text, nullable=False)
    updated_at = mapped_column(Text)

    domains = relationship('TenantDomain', back_populates='tenant',
                           cascade='all, delete-orphan', order_by='TenantDomain.id')


DOMAIN_PORTAL = 'portal'
DOMAIN_CUSTOM = 'custom'


class TenantDomain(PlatformBase):
    """A hostname that reaches a school.

    Every school gets one ``portal`` hostname the platform generates and owns
    (``<code>.<portal domain>``); it works the moment the school is created and
    can never be removed. A school that wants its own address adds a ``custom``
    hostname and points a CNAME record at the portal hostname.
    """

    __tablename__ = 'tenant_domains'
    __table_args__ = (
        UniqueConstraint('hostname'),
        Index('ix_tenant_domains_tenant', 'tenant_id'),
    )

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id = mapped_column(Integer, ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False)
    # Stored lower-case, without port.
    hostname = mapped_column(Text, nullable=False)
    kind = mapped_column(Text, nullable=False, default=DOMAIN_CUSTOM, server_default=text("'custom'"))
    is_primary = mapped_column(Integer, nullable=False, default=0, server_default=text('0'))
    created_at = mapped_column(Text, nullable=False)

    tenant = relationship('Tenant', back_populates='domains')

    @property
    def is_portal(self):
        return self.kind == DOMAIN_PORTAL


# The two kinds of platform admin. Both have full control over every school; only
# the super admin manages the platform's own team and reads its activity logs.
ROLE_SUPER = 'superadmin'
ROLE_ADMIN = 'admin'


class PlatformAdmin(PlatformBase):
    """A Brightstars Academics operator. Not a school account: a platform admin
    exists once, here, and may enter any school.

    Removing an admin only ever switches ``active`` off. The row stays, so the
    audit trail keeps pointing at a real account and the admin can be restored."""

    __tablename__ = 'platform_admins'
    __table_args__ = (UniqueConstraint('username'),)

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    username = mapped_column(Text, nullable=False)
    display_name = mapped_column(Text, nullable=False)
    password_hash = mapped_column(Text, nullable=False)
    email = mapped_column(Text)
    phone = mapped_column(Text)
    role = mapped_column(Text, nullable=False, default=ROLE_ADMIN, server_default=text("'admin'"))
    # Whether this admin may read /docs. The super admin always may; the super admin grants it to
    # anyone else, one admin at a time, from the Team page.
    docs_access = mapped_column(Integer, nullable=False, default=0, server_default=text('0'))
    # Whether this admin may open /platform/settings at all, and edit its Low/Medium-risk
    # environment variables and run its non-key terminal actions. The super admin always may.
    settings_access = mapped_column(Integer, nullable=False, default=0, server_default=text('0'))
    # Whether this admin (given settings_access) may also edit High/Critical-risk environment
    # variables and run the key-related actions (rotate-delivery-key, create-db-role --rotate,
    # set-db-url) — the platform's "highly trusted admin" tier. The super admin always may.
    settings_high_trust = mapped_column(Integer, nullable=False, default=0, server_default=text('0'))
    # Whether this admin may permanently delete a school (its database, its files, and its place
    # in the registry) - far more severe than anything else a platform admin can do to a school
    # (create, brand, suspend, enter: all either routine or reversible), so it needs its own,
    # narrower grant rather than riding on ordinary platform-admin status. The super admin always
    # may, and grants it to anyone else, one admin at a time, from the Team page.
    can_delete_schools = mapped_column(Integer, nullable=False, default=0, server_default=text('0'))
    active = mapped_column(Integer, nullable=False, default=1, server_default=text('1'))
    password_must_change = mapped_column(Integer, nullable=False, default=0, server_default=text('0'))
    created_at = mapped_column(Text, nullable=False)
    last_login_at = mapped_column(Text)
    removed_at = mapped_column(Text)
    removed_by = mapped_column(Text)


class PlatformEntryToken(PlatformBase):
    """A one-time ticket that lets a platform admin into a school.

    Each school is on its own domain, so the platform console cannot set a
    session cookie for it directly. Instead it mints a token here and sends the
    operator to the school's own domain to redeem it.

    Only the SHA-256 of the token is stored, so a copy of the registry does not
    let anyone enter a school. Tokens are single-use and short-lived.
    """

    __tablename__ = 'platform_entry_tokens'
    __table_args__ = (
        UniqueConstraint('token_hash'),
        Index('ix_platform_entry_tokens_tenant', 'tenant_id'),
    )

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash = mapped_column(Text, nullable=False)
    platform_admin_id = mapped_column(Integer, ForeignKey('platform_admins.id', ondelete='CASCADE'), nullable=False)
    tenant_id = mapped_column(Integer, ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False)
    created_at = mapped_column(Text, nullable=False)
    expires_at = mapped_column(Text, nullable=False)
    used_at = mapped_column(Text)
    created_ip = mapped_column(Text)


class PlatformAuditLog(PlatformBase):
    """Who did what across the platform, including every entry into a school."""

    __tablename__ = 'platform_audit_log'
    __table_args__ = (
        Index('ix_platform_audit_created', 'created_at'),
        Index('ix_platform_audit_tenant', 'tenant_id'),
    )

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform_admin_id = mapped_column(Integer, ForeignKey('platform_admins.id', ondelete='SET NULL'))
    # Snapshot, so the trail stays readable if the account is later removed.
    actor_username = mapped_column(Text)
    tenant_id = mapped_column(Integer, ForeignKey('tenants.id', ondelete='SET NULL'))
    action = mapped_column(Text, nullable=False)
    detail = mapped_column(Text)
    ip_address = mapped_column(Text)
    created_at = mapped_column(Text, nullable=False)


class RateLimitCounter(PlatformBase):
    """How many tries a key has made in its current window, shared by every worker.

    One row per limited action (a sign-in from one address, a password-recovery
    request, a school's test messages...). The key is stored only as its SHA-256,
    since keys hold IP addresses and usernames. ``expires_at`` is when the window
    ends, in seconds since 1970 on the database's own clock; a row past that time
    means nothing and is deleted in passing. See ``control_plane/ratelimit.py``.
    """

    __tablename__ = 'rate_limits'
    __table_args__ = (Index('ix_rate_limits_expires', 'expires_at'),)

    key_hash = mapped_column(Text, primary_key=True)
    hits = mapped_column(Integer, nullable=False, default=0, server_default=text('0'))
    expires_at = mapped_column(Float, nullable=False)


class PlatformState(PlatformBase):
    """A few named values the whole deployment shares, one row each.

    ``launch_id`` is the only one so far: a random value written once every time the server is
    started, the same for every worker of that start and different on the next. Sessions are
    stamped with it, which is how a restart signs people out (see ``control_plane/launch.py``).
    """

    __tablename__ = 'platform_state'

    key = mapped_column(Text, primary_key=True)
    value = mapped_column(Text, nullable=False)
    updated_at = mapped_column(Text, nullable=False)
