"""Sign-ins that have been ended by signing out, shared by every worker (the registry's ``ended_sessions``).

Signing out clears the cookie in that browser, but a session is a signed cookie: a copy taken earlier would
otherwise go on working. Every sign-in carries a random id (core/session_guard.py); signing out records it
here, and the guard refuses a session whose id is recorded.

Asked on every request from someone signed in, so a "still fine" answer is remembered in this worker for a
few seconds (``LIVE_CACHE_SECONDS``): a copy used straight after signing out may get through on a worker that
had just checked it, for at most that long. Signing out in this worker forgets its own memory at once.

Failing safe, as the rate limits do: if the registry cannot be reached, a session is treated as not ended
(nobody is thrown out of an exam by a database fault) and the ended id is kept in this worker's memory too.
"""

import hashlib
import logging
import threading
import time

import sqlalchemy as sa

from .registry import platform_session

log = logging.getLogger(__name__)

LIVE_CACHE_SECONDS = 15
PURGE_EVERY_SECONDS = 300

_lock = threading.Lock()
_live = {}            # sid hash -> monotonic time until which "not ended" is believed
_ended_here = {}      # sid hash -> epoch until (kept when the registry could not be written)
_last_purge = float('-inf')

_END = sa.text("""
    INSERT INTO ended_sessions (sid_hash, until) VALUES (:h, :until)
    ON CONFLICT (sid_hash) DO UPDATE SET until = GREATEST(ended_sessions.until, EXCLUDED.until)
""")
_IS_ENDED = sa.text("SELECT 1 FROM ended_sessions WHERE sid_hash = :h AND until > EXTRACT(EPOCH FROM statement_timestamp())")
_PURGE = sa.text("DELETE FROM ended_sessions WHERE until <= EXTRACT(EPOCH FROM statement_timestamp())")


def _hash(sid):
    return hashlib.sha256(str(sid).encode('utf-8')).hexdigest()


def end(sid, seconds):
    """Record that the sign-in ``sid`` has ended, for ``seconds`` (as long as it could otherwise have lasted)."""
    if not sid:
        return
    h = _hash(sid)
    until = time.time() + max(60.0, float(seconds))
    with _lock:
        _live.pop(h, None)
        _ended_here[h] = until
    try:
        with platform_session() as session:
            session.execute(_END, {'h': h, 'until': until})
            session.commit()
    except Exception as exc:
        log.warning('Could not record an ended sign-in in the registry (%s); this worker remembers it.',
                    type(exc).__name__)


def is_ended(sid):
    """Whether the sign-in ``sid`` was ended by signing out."""
    if not sid:
        return False
    h = _hash(sid)
    now_mono, now = time.monotonic(), time.time()
    with _lock:
        local = _ended_here.get(h)
        if local is not None:
            if local > now:
                return True
            _ended_here.pop(h, None)
        if _live.get(h, 0) > now_mono:
            return False
    try:
        with platform_session() as session:
            found = session.execute(_IS_ENDED, {'h': h}).first() is not None
            _purge_now_and_then(session)
    except Exception:
        return False
    if not found:
        with _lock:
            if len(_live) > 20000:
                _live.clear()
            _live[h] = now_mono + LIVE_CACHE_SECONDS
    return found


def _purge_now_and_then(session):
    global _last_purge
    now = time.monotonic()
    with _lock:
        if now - _last_purge < PURGE_EVERY_SECONDS:
            return
        _last_purge = now
    try:
        session.execute(_PURGE)
        session.commit()
    except Exception:
        session.rollback()
