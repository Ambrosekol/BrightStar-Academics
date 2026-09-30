"""Decides, on every request, whether the person's sign-in is still good.

A session is a signed cookie: it says who someone is, and nothing in it expires by itself. Two
things must end a sign-in that the cookie alone cannot end, and both are decided here, in one
place, before any route runs:

1. **A restart signs everyone out, except people sitting an exam.** Each session is stamped with
   the *launch id* current when it was opened (``control_plane/launch.py``). A request whose stamp
   is not the current one belongs to a sign-in from before the last restart. If that person has an
   exam running right now (an entrance candidate with an unfinished paper, a student with an
   unfinished test, examination, practice or quiz) their session is kept and stamped again, so
   they carry on exactly where they were. Anyone else is signed out and meets the sign-in page.

2. **Changing or resetting a password ends the account's other sign-ins.** Each session is also
   stamped with a short fingerprint of the password hash the account had when it signed in. A
   changed password is a different hash, so every session opened with the old one stops working.
   This is judged from the stored hash itself, not from a flag some route has to remember to set,
   so it holds however the password was changed (the person, an administrator, a reset link, a
   command). The person changing their own password is stamped again on the spot and stays in.

Failing safe: if the launch id or the school's database cannot be read, nobody is signed out for
it. Nothing here runs for ``/health`` or for shared static files.
"""

import hashlib
import time
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from flask import flash, g, request, session

from control_plane import config
from control_plane.launch import current_launch_id
from models import (
    Admin, Attempt, Candidate, ParentAccount, SchoolAssessmentAttempt,
    SchoolAssignmentAttempt, Student, db,
)

LAUNCH_KEY = 'launch'
PASSWORD_KEY = 'pwv'
# Must equal control_plane.console.SESSION_KEY (a contract test checks it).
PLATFORM_KEY = 'platform_admin_id'
# The reserved account a platform operator uses inside a school gets a fresh random password on
# every entry (control_plane/entry.py), so its hash means nothing; only "active" governs it.
OPERATOR_PREFIX = 'platform@'
# A quiz with no deadline of its own (untimed, or timed per question) still counts as "in
# progress" for this long after it was started, so an abandoned one cannot keep a sign-in alive
# through every restart for ever.
UNTIMED_QUIZ_WINDOW = timedelta(hours=3)

# The requests that are themselves part of signing in, or need no sign-in: they go on with whatever
# session is left (an empty one) instead of being sent to the sign-in page.
SIGN_IN_ENDPOINTS = frozenset({'login', 'admin_login', 'platform_login', 'forgot_password', 'password_reset'})

RESTARTED = 'The system was restarted, please sign in again.'
PASSWORD_CHANGED = 'Your password was changed, please sign in again.'


# ---------------------------------------------------------------- what the session says

def password_fingerprint(password_hash):
    """A short, one-way marker of a stored password hash. Salted hashes differ every time a
    password is set, so any change (even to the same words) gives a different marker."""
    return hashlib.sha256((password_hash or '').encode('utf-8')).hexdigest()[:20]


def _identity():
    """``(kind, account_id)`` of whoever this session is signed in as, or ``(None, None)``."""
    for kind, key in (('admin', 'admin_id'), ('student', 'student_id'),
                      ('parent', 'parent_id'), ('candidate', 'candidate_id'),
                      ('platform', PLATFORM_KEY)):
        value = session.get(key)
        if value:
            try:
                return kind, int(value)
            except (TypeError, ValueError):
                return None, None
    return None, None


def hash_of(kind, account):
    """The stored password hash of an account object, whatever the kind."""
    if kind == 'student':
        return account['login_password_hash']
    return account['password_hash']


def _read_fingerprint(kind, account_id):
    """The fingerprint of the account's password now, read straight from the database, or None
    when the account cannot be found (which is then left to the ordinary checks, that already
    treat a missing account as signed out)."""
    if kind == 'platform':
        from control_plane.models import PlatformAdmin
        from control_plane.registry import platform_session

        with platform_session() as reg:
            stored = reg.scalars(sa.select(PlatformAdmin.password_hash).where(
                PlatformAdmin.id == account_id, PlatformAdmin.active == 1)).first()
        return None if stored is None else password_fingerprint(stored)
    if kind == 'admin':
        row = db.session.execute(sa.select(Admin.password_hash, Admin.username)
                                 .where(Admin.id == account_id)).first()
        if row is None:
            return None
        if (row.username or '').startswith(OPERATOR_PREFIX):
            return 'operator'
        return password_fingerprint(row.password_hash)
    column = {'student': Student.login_password_hash, 'parent': ParentAccount.password_hash,
              'candidate': Candidate.password_hash}[kind]
    owner = {'student': Student, 'parent': ParentAccount, 'candidate': Candidate}[kind]
    stored = db.session.execute(sa.select(column).where(owner.id == account_id)).first()
    return None if stored is None else password_fingerprint(stored[0])


# Every request from a signed-in person used to read this straight from the database (see
# _read_fingerprint above); on a remote database that is one extra round-trip on nearly every
# request in the app, most of which did not otherwise need one. Cached per (school, kind,
# account) for BRIGHTSTARS_PASSWORD_CHECK_CACHE_SECONDS, the same way core/branding.py caches a
# school's branding. The tradeoff this buys: a password change (by the person, an administrator,
# or a reset link) can take up to that long to sign out a *different* session; the person's own
# session is exempted by _invalidate_fingerprint_cache below, called from refresh_password_stamp,
# so changing your own password still ends your own old sign-in immediately, as documented at the
# top of this file.
_fingerprint_cache = {}
_FINGERPRINT_CACHE_MAX = 5000


def _fingerprint_cache_key(kind, account_id):
    tenant = g.get('tenant')
    return (tenant.id if (kind != 'platform' and tenant is not None) else None, kind, account_id)


def _invalidate_fingerprint_cache(kind, account_id):
    _fingerprint_cache.pop(_fingerprint_cache_key(kind, account_id), None)


def _current_fingerprint(kind, account_id):
    """``_read_fingerprint``, cached briefly per process (see above). Zero-second caching (the
    default's floor) reads the database every time, exactly like before this cache existed."""
    ttl = config.password_check_cache_seconds()
    if ttl <= 0:
        return _read_fingerprint(kind, account_id)
    key = _fingerprint_cache_key(kind, account_id)
    hit = _fingerprint_cache.get(key)
    now = time.monotonic()
    if hit and hit[0] > now:
        return hit[1]
    value = _read_fingerprint(kind, account_id)
    if len(_fingerprint_cache) >= _FINGERPRINT_CACHE_MAX:
        _fingerprint_cache.clear()
    _fingerprint_cache[key] = (now + ttl, value)
    return value


# ---------------------------------------------------------------- who is sitting an exam

def sitting_exam(kind, account_id):
    """Whether this person has an exam running right now.

    An entrance candidate: a paper they started whose time has not run out. A student: a test,
    examination or practice paper they started whose time has not run out, or a quiz they started
    (with its own deadline if it has one). When the answer cannot be found out, the answer is yes:
    a fault must not throw someone out of an exam.
    """
    if g.get('tenant') is None or kind not in ('candidate', 'student'):
        return False
    now = datetime.now(timezone.utc)
    stamp = now.isoformat()
    try:
        if kind == 'candidate':
            found = db.session.execute(sa.select(Attempt.id).where(
                Attempt.candidate_id == account_id, Attempt.status == 'active',
                Attempt.expires_at > stamp).limit(1)).first()
            return found is not None
        found = db.session.execute(sa.select(SchoolAssessmentAttempt.id).where(
            SchoolAssessmentAttempt.student_id == account_id,
            SchoolAssessmentAttempt.status == 'active',
            SchoolAssessmentAttempt.expires_at > stamp).limit(1)).first()
        if found is not None:
            return True
        quiz = SchoolAssignmentAttempt
        found = db.session.execute(sa.select(quiz.id).where(
            quiz.student_id == account_id, quiz.status == 'active',
            sa.or_(quiz.expires_at > stamp,
                   sa.and_(quiz.expires_at.is_(None),
                           quiz.started_at > (now - UNTIMED_QUIZ_WINDOW).isoformat()))).limit(1)).first()
        return found is not None
    except Exception:
        db.session.rollback()
        return True


# ---------------------------------------------------------------- the verdict on a session

def assess():
    """``(verdict, launch, fingerprint)`` for this request's session. Changes nothing.

    ``verdict`` is ``'ok'`` (nothing to do), ``'stamp'`` (fine, but write the current stamps into
    the session) or ``'restart'`` / ``'password'`` (sign the person out, and why).
    """
    launch = current_launch_id()
    kind, account_id = _identity()
    stale = launch is not None and session.get(LAUNCH_KEY) != launch
    if kind is None:
        # Nobody is signed in, so there is nothing to lose: just keep the stamp current.
        return ('stamp' if stale else 'ok'), launch, None
    if stale and not sitting_exam(kind, account_id):
        return 'restart', launch, None
    try:
        current = _current_fingerprint(kind, account_id)
    except Exception:
        if kind != 'platform':
            db.session.rollback()
        current = None  # cannot tell: leave the person as they are
    stamped = session.get(PASSWORD_KEY)
    if current is not None and stamped is not None and stamped != current:
        return 'password', launch, None
    adopt = current is not None and stamped is None
    return ('stamp' if (stale or adopt) else 'ok'), launch, current


def usable_for_files():
    """Whether this request's sign-in may still open private files. Read-only: a request for a
    picture must never rewrite the cookie a page loading at the same moment is also writing."""
    verdict, _, _ = assess()
    return verdict in ('ok', 'stamp')


# ---------------------------------------------------------------- acting on it

def stamp_session(launch=None, fingerprint=None):
    launch = launch if launch is not None else current_launch_id()
    if launch is not None:
        session[LAUNCH_KEY] = launch
    if fingerprint is not None:
        session[PASSWORD_KEY] = fingerprint


def stamp_sign_in(kind, account):
    """Mark a session that has just been opened: this launch, and this account's password."""
    fingerprint = 'operator' if (kind == 'admin' and str(account['username'] or '').startswith(OPERATOR_PREFIX)) \
        else password_fingerprint(hash_of(kind, account))
    stamp_session(fingerprint=fingerprint)


def refresh_password_stamp():
    """After the signed-in person has changed their own password: stamp the session again so
    that this sign-in stays, while every other one made with the old password ends."""
    kind, account_id = _identity()
    if kind is None:
        return
    # The request that got here already ran guard_session with the *old* password, which may
    # have just (re)populated the fingerprint cache with it. Drop that entry so the read below
    # goes to the database and picks up the change just made, instead of serving the stale value
    # back and stamping this very session with the password it no longer has.
    _invalidate_fingerprint_cache(kind, account_id)
    fingerprint = _current_fingerprint(kind, account_id)
    if fingerprint is not None:
        session[PASSWORD_KEY] = fingerprint


def sign_out(reason):
    """End this sign-in and say why. The school the cookie belongs to is kept, so the sign-in page
    still knows where it is; everything about the person goes.

    A page or a form submission that was refused is sent to the sign-in page with the reason,
    instead of failing further on with a security-token error nobody can make sense of. Returns
    that redirect (or None when the request is itself a step in signing in).

    A POST to a sign-in endpoint itself (submitting the login form, requesting or using a password
    reset) is left completely alone: it does not depend on whatever identity this session already
    carried, and clearing the session here would wipe the CSRF token that very request is checked
    against, turning a correct sign-in into a confusing "invalid token" refusal. The view replaces
    the whole session on success regardless. A GET to one of those endpoints (someone opening the
    sign-in page itself with a stale cookie) still clears and shows the message below, since that
    request carries no form to protect.
    """
    from flask import redirect, url_for

    if request.endpoint in SIGN_IN_ENDPOINTS and request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        return None
    keep = {key: session[key] for key in ('tenant_id',) if key in session}
    session.clear()
    session.update(keep)
    stamp_session()
    flash(RESTARTED if reason == 'restart' else PASSWORD_CHANGED, 'error')
    if request.endpoint in SIGN_IN_ENDPOINTS or request.method in ('GET', 'HEAD'):
        return None    # already meeting the sign-in page, or about to be sent there by the route itself
    return redirect(url_for('platform_login' if g.get('on_platform_host') else 'login'))


def take_sign_out_reason():
    """The message left by ``sign_out``, taken out of the queue (and so not shown twice), or None.
    For a sign-in page that has no place of its own for queued messages."""
    from flask import get_flashed_messages

    messages = [m for _, m in get_flashed_messages(with_categories=True) if m in (RESTARTED, PASSWORD_CHANGED)]
    return messages[0] if messages else None


def guard_session():
    """The before-request hook."""
    path = request.path
    if path == '/health' or path.startswith('/static/'):
        return None
    verdict, launch, fingerprint = assess()
    if verdict == 'stamp':
        stamp_session(launch, fingerprint)
    elif verdict in ('restart', 'password'):
        return sign_out(verdict)
    return None


def install(app):
    """Must be installed straight after the school resolver and before any other hook, because the
    hooks after it act on who is signed in."""
    app.before_request(guard_session)
