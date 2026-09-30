"""The platform's own team: who its administrators are, and what each has done.

Two kinds of platform admin exist, and both have full control over every school:

* the **super admin** — the overall admin who can always look over and protect
  the system. Only the super admin adds or removes platform admins, resets their
  passwords, and reads their activity logs. The console cannot create another
  one, and the super admin cannot be removed.
* **platform admins** — everyone else. They create, suspend, brand and enter
  schools, but cannot touch the team or read anyone else's log.

What an admin does inside a school is recorded in that school's own audit trail,
under their reserved account; the activity log reads it from there, so one log
shows an admin's whole footprint — on schools, within the platform, and inside
the schools they entered.

Removing an admin never deletes the account. It switches it off and revokes its
reserved account in every school, so a session already open inside a school stops
working at once; the row stays so that the audit trail keeps pointing at a real
person, and so the account can be restored.
"""

import math
import re
import secrets

import sqlalchemy as sa
from werkzeug.security import generate_password_hash

from .models import PlatformAdmin, PlatformAuditLog, PlatformEntryToken, ROLE_ADMIN, ROLE_SUPER, Tenant
from .provisioning import ProvisioningError, audit
from .registry import now_iso, platform_session

# No "@": a platform admin's reserved account inside a school is "platform@<username>",
# and that "@" namespace must stay unreachable from anything a person can type.
USERNAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{2,39}$')
PER_PAGE = 30

SIGNIN_ACTIONS = ('platform_admin.login', 'platform_admin.logout',
                  'platform_admin.login_failed', 'platform_admin.login_refused')

CATEGORIES = (('all', 'Everything'), ('schools', 'Schools'), ('inside', 'Inside schools'),
              ('account', 'Accounts & team'), ('signins', 'Sign-ins'))

ACTION_LABELS = {
    'tenant.create': 'Created a school',
    'tenant.active': 'Reactivated a school',
    'tenant.suspended': 'Suspended a school',
    'tenant.domain_add': "Added a school's own address",
    'tenant.domain_remove': "Removed a school's own address",
    'tenant.enter': 'Entered a school',
    'tenant.school_admin_create': "Created a school's administrator",
    'tenant.branding_update': "Changed a school's branding",
    'tenant.numbering_update': "Changed a school's numbering rules",
    'platform_admin.login': 'Signed in',
    'platform_admin.logout': 'Signed out',
    'platform_admin.login_failed': 'Failed sign-in',
    'platform_admin.login_refused': 'Sign-in refused',
    'platform_admin.password_change': 'Changed their own password',
    'platform_admin.create': 'Added a platform admin',
    'platform_admin.remove': 'Removed a platform admin',
    'platform_admin.restore': 'Restored a platform admin',
    'platform_admin.password_reset': "Reset an admin's password",
    'platform_admin.docs_access': "Changed an admin's access to the documentation",
    'platform_admin.settings_access': "Changed an admin's access to Settings",
    'platform_config.variable_changed': 'Changed an environment variable',
    'platform_config.action_run': 'Ran a terminal action from the portal',
    'platform_admin.school_access_revoked': "Revoked a removed admin's access inside schools",
    'platform_admin.promote': 'Became the super admin',
    'platform_admin.adopt': 'Adopted admins from an installation',
}
# Entries a super admin should notice at a glance.
ALERT_ACTIONS = {'tenant.suspended', 'platform_admin.login_failed', 'platform_admin.login_refused',
                 'platform_admin.remove', 'platform_admin.school_access_revoked',
                 'platform_config.variable_changed', 'platform_config.action_run'}


def category_of(action):
    if action.startswith('inside.'):
        return 'inside'
    if action in SIGNIN_ACTIONS:
        return 'signins'
    if action.startswith('tenant.'):
        return 'schools'
    return 'account'


def describe(action):
    if action.startswith('inside.'):
        return _humanise(action[len('inside.'):])
    return ACTION_LABELS.get(action, action)


# ---------------------------------------------------------------- the team

def _entries_of(admin):
    """The audit rows that belong to one admin: those linked to their account and
    older ones that recorded only their username."""
    return sa.or_(PlatformAuditLog.platform_admin_id == admin.id,
                  PlatformAuditLog.actor_username == admin.username)


def list_admins():
    """Every platform admin — active ones first, the super admin at the top — with
    how much each has done."""
    with platform_session() as session:
        admins = session.scalars(sa.select(PlatformAdmin).order_by(
            PlatformAdmin.active.desc(), (PlatformAdmin.role == ROLE_SUPER).desc(),
            PlatformAdmin.id)).all()
        stats = {a_id: (n, last) for a_id, n, last in session.execute(
            sa.select(PlatformAdmin.id, sa.func.count(PlatformAuditLog.id),
                      sa.func.max(PlatformAuditLog.created_at))
            .join(PlatformAuditLog, sa.or_(PlatformAuditLog.platform_admin_id == PlatformAdmin.id,
                                           PlatformAuditLog.actor_username == PlatformAdmin.username))
            .group_by(PlatformAdmin.id))}
        return [{
            'id': a.id, 'username': a.username, 'display_name': a.display_name, 'email': a.email,
            'role': a.role, 'is_super': a.role == ROLE_SUPER, 'active': bool(a.active),
            'docs_access': a.role == ROLE_SUPER or bool(a.docs_access),
            'settings_access': a.role == ROLE_SUPER or bool(a.settings_access),
            'settings_high_trust': a.role == ROLE_SUPER or bool(a.settings_high_trust),
            'can_delete_schools': a.role == ROLE_SUPER or bool(a.can_delete_schools),
            'created_at': a.created_at, 'last_login_at': a.last_login_at,
            'removed_at': a.removed_at, 'removed_by': a.removed_by,
            'must_change': bool(a.password_must_change),
            'entries': stats.get(a.id, (0, None))[0], 'last_entry_at': stats.get(a.id, (0, None))[1],
        } for a in admins]


def _new_temporary_password():
    return secrets.token_urlsafe(12)


def create_admin(username, display_name, email, actor, actor_id):
    """Add an ordinary platform admin. Returns the one-time temporary password.

    The new admin must replace it the first time they sign in, so the person who
    created the account never knows the password they end up using.
    """
    username = (username or '').strip().lower()
    if not USERNAME_RE.match(username):
        raise ProvisioningError('A username is 3 to 40 characters: lower-case letters, digits, dots, '
                                'hyphens and underscores, starting with a letter or digit.')
    display_name = (display_name or '').strip()[:120] or username
    email = (email or '').strip()[:200] or None
    password = _new_temporary_password()
    with platform_session() as session:
        if session.scalars(sa.select(PlatformAdmin).where(PlatformAdmin.username == username)).first():
            raise ProvisioningError(f'{username} already exists.')
        session.add(PlatformAdmin(username=username, display_name=display_name, email=email,
                                  password_hash=generate_password_hash(password), role=ROLE_ADMIN,
                                  active=1, password_must_change=1, created_at=now_iso()))
        audit(session, 'platform_admin.create', f'{username} (platform admin)', None, actor, actor_id)
        session.commit()
    return password


def _target(session, admin_id):
    admin = session.get(PlatformAdmin, admin_id)
    if admin is None:
        raise ProvisioningError('There is no such platform admin.')
    return admin


def remove_admin(admin_id, actor, actor_id):
    """Take a platform admin's access away everywhere, keeping the account and its logs.

    Returns ``{'schools': n, 'failed': [codes]}``: in how many schools their
    reserved account was switched off, and which schools could not be reached
    (those need another attempt, and the result says so).
    """
    with platform_session() as session:
        admin = _target(session, admin_id)
        if admin.role == ROLE_SUPER:
            raise ProvisioningError('The super admin cannot be removed.')
        if not admin.active:
            raise ProvisioningError(f'{admin.username} has already been removed.')
        username = admin.username
        admin.active = 0
        admin.removed_at = now_iso()
        admin.removed_by = actor
        session.execute(sa.delete(PlatformEntryToken).where(PlatformEntryToken.platform_admin_id == admin.id))
        audit(session, 'platform_admin.remove', username, None, actor, actor_id)
        session.commit()
    result = revoke_school_access(username)
    with platform_session() as session:
        note = f'{username}: switched off in {result["schools"]} school(s)'
        if result['failed']:
            note += '; could not be reached: ' + ', '.join(result['failed'])
        audit(session, 'platform_admin.school_access_revoked', note, None, actor, actor_id)
        session.commit()
    return result


def revoke_school_access(username):
    """Switch off ``username``'s reserved account in every school.

    An admin who is inside a school when they are removed is holding a school
    session, which the platform cannot end directly. The school checks its own
    account on every request, so switching that account off ends the session at
    the next click. The account is switched back on if they are restored and
    enter again.
    """
    from .entry import PLATFORM_ADMIN_USERNAME_PREFIX
    from .registry import to_info
    from .routing import engine_for

    with platform_session() as session:
        infos = [to_info(t) for t in session.scalars(sa.select(Tenant).order_by(Tenant.id))]
    switched, failed = 0, []
    for info in infos:
        try:
            with engine_for(info).begin() as conn:
                done = conn.execute(sa.text('UPDATE admins SET active = 0 WHERE username = :u'),
                                    {'u': f'{PLATFORM_ADMIN_USERNAME_PREFIX}{username}'})
            switched += 1 if done.rowcount else 0
        except Exception:
            failed.append(info.slug)
    return {'schools': switched, 'failed': failed}


def restore_admin(admin_id, actor, actor_id):
    """Give a removed admin their access back, with a new temporary password.

    The old password is discarded: an admin may have been removed because their
    credentials were exposed, so coming back means choosing a new one.
    """
    password = _new_temporary_password()
    with platform_session() as session:
        admin = _target(session, admin_id)
        if admin.active:
            raise ProvisioningError(f'{admin.username} is not removed.')
        admin.active = 1
        admin.removed_at = None
        admin.removed_by = None
        admin.password_hash = generate_password_hash(password)
        admin.password_must_change = 1
        audit(session, 'platform_admin.restore', admin.username, None, actor, actor_id)
        username = admin.username
        session.commit()
    return username, password


def set_docs_access(admin_id, allowed, actor, actor_id):
    """Let a platform admin read the documentation (``/docs``), or take that away again.

    The super admin can always read it, so there is nothing to grant or revoke for them. Returns the
    admin's username.
    """
    with platform_session() as session:
        admin = _target(session, admin_id)
        if admin.role == ROLE_SUPER:
            raise ProvisioningError('The super admin can always read the documentation.')
        if not admin.active:
            raise ProvisioningError(f'{admin.username} has been removed; restore them first.')
        admin.docs_access = 1 if allowed else 0
        audit(session, 'platform_admin.docs_access', f'{admin.username}: {"granted" if allowed else "withdrawn"}',
              None, actor, actor_id)
        username = admin.username
        session.commit()
    return username


def set_delete_access(admin_id, allowed, actor, actor_id):
    """Let a platform admin permanently delete a school, or take that away again.

    The super admin can always delete a school, so there is nothing to grant or revoke for them.
    Returns the admin's username.
    """
    with platform_session() as session:
        admin = _target(session, admin_id)
        if admin.role == ROLE_SUPER:
            raise ProvisioningError('The super admin can always delete a school.')
        if not admin.active:
            raise ProvisioningError(f'{admin.username} has been removed; restore them first.')
        admin.can_delete_schools = 1 if allowed else 0
        audit(session, 'platform_admin.delete_access',
              f'{admin.username}: {"granted" if allowed else "withdrawn"}', None, actor, actor_id)
        username = admin.username
        session.commit()
    return username


def set_settings_access(admin_id, access=None, high_trust=None, actor='cli', actor_id=None):
    """Grant or withdraw one admin's access to Settings (``/platform/settings``), and/or their
    "highly trusted" tier within it. Either argument left ``None`` leaves that flag as it is.

    The super admin always has both, so there is nothing to grant or revoke for them. Withdrawing
    plain access also withdraws high trust (there is no page left to be trusted with); granting
    high trust also grants plain access (being trusted with more implies being let in at all).
    Returns the admin's username.
    """
    with platform_session() as session:
        admin = _target(session, admin_id)
        if admin.role == ROLE_SUPER:
            raise ProvisioningError('The super admin always has full access to Settings.')
        if not admin.active:
            raise ProvisioningError(f'{admin.username} has been removed; restore them first.')
        if access is not None:
            admin.settings_access = 1 if access else 0
            if not access:
                admin.settings_high_trust = 0
        if high_trust is not None:
            admin.settings_high_trust = 1 if high_trust else 0
            if high_trust:
                admin.settings_access = 1
        now_state = 'highly trusted' if admin.settings_high_trust else (
            'granted' if admin.settings_access else 'withdrawn')
        audit(session, 'platform_admin.settings_access', f'{admin.username}: {now_state}', None, actor, actor_id)
        username = admin.username
        session.commit()
    return username


def reset_password(admin_id, actor, actor_id):
    """Replace an admin's password with a new temporary one. Returns ``(username, password)``."""
    password = _new_temporary_password()
    with platform_session() as session:
        admin = _target(session, admin_id)
        if admin.role == ROLE_SUPER:
            raise ProvisioningError("The super admin's password is changed from their own Password page.")
        if not admin.active:
            raise ProvisioningError(f'{admin.username} has been removed; restore them first.')
        admin.password_hash = generate_password_hash(password)
        admin.password_must_change = 1
        audit(session, 'platform_admin.password_reset', admin.username, None, actor, actor_id)
        username = admin.username
        session.commit()
    return username, password


# ---------------------------------------------------------------- activity

# What a platform admin does inside a school is recorded in that school's own
# audit trail, under their reserved account "platform@<username>". These rows are
# read from there, so the platform log can show the whole picture of an admin.
INSIDE_PREFIX = 'inside.'
# Already in the platform log as "Entered a school"; showing it twice would be noise.
_HIDDEN_INSIDE = ('platform_admin_entered_school',)
# Things inside a school a super admin should notice at a glance.
INSIDE_ALERTS = {'authorization_denied', 'scope_denied', 'admin_created', 'admin_status_changed',
                 'admin_password_reset', 'role_created', 'role_updated'}
INSIDE_DETAIL_LIMIT = 300


def _when(value):
    """A sortable moment for an ISO-8601 timestamp; unreadable ones sort last."""
    from datetime import datetime, timezone

    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _humanise(action):
    return (action or '').replace('_', ' ').strip().capitalize() or 'Unknown action'


class _InsideSchools:
    """One admin's (or everyone's) actions inside the schools they entered.

    Only schools that were actually entered are read: an admin can act inside a
    school only after redeeming an entry ticket, and every redemption is in the
    platform log. That keeps this to a few queries however many schools exist. A
    school that cannot be read is reported, never allowed to hide the rest.
    """

    def __init__(self, admin_id=None):
        from .entry import PLATFORM_ADMIN_USERNAME_PREFIX
        from .registry import to_info
        from .routing import engine_for

        self._engine_for = engine_for
        self.prefix = PLATFORM_ADMIN_USERNAME_PREFIX
        with platform_session() as session:
            admin = session.get(PlatformAdmin, admin_id) if admin_id is not None else None
            if admin_id is not None and admin is None:
                self.infos, self.username = [], None
            else:
                entered = sa.select(PlatformAuditLog.tenant_id).where(
                    PlatformAuditLog.action == 'tenant.enter', PlatformAuditLog.tenant_id.is_not(None))
                if admin is not None:
                    entered = entered.where(_entries_of(admin))
                ids = set(session.scalars(entered))
                self.infos = [to_info(t) for t in session.scalars(
                    sa.select(Tenant).where(Tenant.id.in_(ids)).order_by(Tenant.id))] if ids else []
                self.username = admin.username if admin is not None else None
        self.counts, self.unreadable = {}, []
        for info in self.infos:
            try:
                self.counts[info.slug] = self._count(info)
            except Exception:
                self.unreadable.append(info.slug)

    def _who(self):
        """The SQL that selects the reserved account(s), with its bound value."""
        if self.username is not None:
            return 'username_snapshot = :who', f'{self.prefix}{self.username}'
        return 'username_snapshot LIKE :who', f'{self.prefix}%'

    def _count(self, info):
        clause, value = self._who()
        with self._engine_for(info).connect() as conn:
            return conn.execute(sa.text(
                f'SELECT COUNT(*) FROM audit_logs WHERE {clause} AND action <> :hidden'),
                {'who': value, 'hidden': _HIDDEN_INSIDE[0]}).scalar() or 0

    @property
    def total(self):
        return sum(self.counts.values())

    def rows(self, limit):
        """The newest ``limit`` entries across every school read, newest first."""
        clause, value = self._who()
        out = []
        for info in self.infos:
            if not self.counts.get(info.slug):
                continue
            try:
                with self._engine_for(info).connect() as conn:
                    found = conn.execute(sa.text(
                        'SELECT created_at, username_snapshot, action, module, target_type, target_id, '
                        'details, ip_address, success FROM audit_logs '
                        f'WHERE {clause} AND action <> :hidden ORDER BY created_at DESC, id DESC LIMIT :n'),
                        {'who': value, 'hidden': _HIDDEN_INSIDE[0], 'n': limit}).mappings().all()
            except Exception:
                if info.slug not in self.unreadable:
                    self.unreadable.append(info.slug)
                continue
            for r in found:
                out.append(self._row(info, r))
        out.sort(key=lambda r: _when(r['created_at']), reverse=True)
        return out[:limit]

    def _row(self, info, r):
        action = r['action']
        who = (r['username_snapshot'] or '')
        detail = ' · '.join(p for p in (
            (r['target_type'] or '') + (f' {r["target_id"]}' if r['target_id'] else ''),
            r['details'] or '') if p.strip())
        return {
            'created_at': r['created_at'], 'actor': who[len(self.prefix):] if who.startswith(self.prefix) else who,
            'action': INSIDE_PREFIX + action,
            'label': 'Submitted a change' if action == 'state_change_request' else _humanise(action),
            'category': 'inside',
            'alert': action in INSIDE_ALERTS or not r['success'],
            'detail': (detail[:INSIDE_DETAIL_LIMIT] + '…') if len(detail) > INSIDE_DETAIL_LIMIT else detail,
            'ip': r['ip_address'], 'slug': info.slug, 'school': info.name,
        }


def activity(admin_id=None, category='all', page=1, per_page=PER_PAGE, inside=True):
    """A page of the platform's record of its admins, newest first.

    ``admin_id`` narrows it to one admin's own entries; ``None`` is everyone's.
    ``category`` is one of :data:`CATEGORIES`. Besides the platform's own log this
    includes what the admin did *inside* schools, read from each school's audit
    trail (the "Inside schools" view, and "Everything"); pass ``inside=False`` for
    the platform log alone, which is cheaper and touches no school database.

    The result also carries ``unreadable``: schools whose audit trail could not be
    read, so a missing entry is never silently mistaken for no activity.
    """
    category = category if category in dict(CATEGORIES) else 'all'
    reader = _InsideSchools(admin_id) if inside and category in ('all', 'inside') else None
    with platform_session() as session:
        admin = None
        if admin_id is not None:
            admin = session.get(PlatformAdmin, admin_id)
            if admin is None:
                return {'rows': [], 'total': 0, 'page': 1, 'pages': 1, 'per_page': per_page, 'unreadable': []}
        clauses = [_entries_of(admin)] if admin is not None else []
        if category == 'schools':
            clauses.append(PlatformAuditLog.action.startswith('tenant.', autoescape=True))
        elif category == 'signins':
            clauses.append(PlatformAuditLog.action.in_(SIGNIN_ACTIONS))
        elif category == 'account':
            clauses.append(sa.and_(PlatformAuditLog.action.startswith('platform_admin.', autoescape=True),
                                   PlatformAuditLog.action.not_in(SIGNIN_ACTIONS)))

        platform_total = 0 if category == 'inside' else (
            session.scalar(sa.select(sa.func.count(PlatformAuditLog.id)).where(*clauses)) or 0)
        total = platform_total + (reader.total if reader else 0)
        pages = max(1, math.ceil(total / per_page))
        page = min(max(1, page), pages)
        # Both sources are cut by the same key the union is sorted on (time, newest first), so the
        # newest page*per_page of each always contain this page of the union.
        want = page * per_page
        platform_rows = []
        if category != 'inside':
            stmt = (sa.select(PlatformAuditLog, Tenant.slug, Tenant.name)
                    .outerjoin(Tenant, Tenant.id == PlatformAuditLog.tenant_id)
                    .where(*clauses).order_by(PlatformAuditLog.created_at.desc(), PlatformAuditLog.id.desc())
                    .limit(want))
            platform_rows = [{
                'created_at': row.created_at, 'actor': row.actor_username or 'command line',
                'action': row.action, 'label': describe(row.action), 'category': category_of(row.action),
                'alert': row.action in ALERT_ACTIONS, 'detail': row.detail, 'ip': row.ip_address,
                'slug': slug, 'school': name,
            } for row, slug, name in session.execute(stmt)]
    rows = platform_rows
    if reader is not None:
        rows = sorted(platform_rows + reader.rows(want), key=lambda r: _when(r['created_at']), reverse=True)
    start = (page - 1) * per_page
    return {'rows': rows[start:start + per_page], 'total': total, 'page': page, 'pages': pages,
            'per_page': per_page, 'unreadable': list(reader.unreadable) if reader else []}
