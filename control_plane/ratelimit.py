"""Rate limits shared by every worker process.

Sign-in, password recovery and "send a test message" are all limited to a number
of tries per stretch of time. If each worker counted in its own memory, running
four workers would quietly allow four times the limit, so the counters live in
the registry database, which every worker already talks to.

How it counts
-------------
A **fixed window**. The first try for a key opens a window that lasts ``window``
seconds; tries inside it are counted; when it ends the count starts again. It is
one row per key and one statement per try, which is what makes it safe: the
``INSERT ... ON CONFLICT DO UPDATE`` below locks that row, so two workers trying
at the same instant are counted one after the other and cannot both slip in under
the limit. (The trade-off against the old in-memory "sliding" count is that
someone can use up the limit at the very end of one window and again at the very
start of the next. For a lock-out meant to stop guessing, that is fine.)

Time comes from PostgreSQL, not from the worker, so workers on different machines
with slightly different clocks still agree about when a window ends.

What is stored
--------------
Only a SHA-256 of the key, never the key itself, because keys hold IP addresses
and usernames. Expired rows mean nothing (the next try starts a fresh window) and
are deleted now and then, about once a minute per worker.

If the registry cannot be reached
---------------------------------
Nobody is locked out and the sign-in page does not crash. That call falls back
to a counter in this worker's own memory (the old behaviour) and a warning is
logged, at most once a minute.
"""

import hashlib
import logging
import threading
import time

import sqlalchemy as sa

from .registry import platform_session

log = logging.getLogger(__name__)

# One statement, one atomic step: start a window, or count inside the open one, or
# start a new window if the old one has ended. Returns how many tries this window has.
_COUNT = sa.text("""
    INSERT INTO rate_limits (key_hash, hits, expires_at)
    VALUES (:key_hash, 1,
            CAST(EXTRACT(EPOCH FROM statement_timestamp()) AS double precision) + :window)
    ON CONFLICT (key_hash) DO UPDATE SET
        hits = CASE WHEN rate_limits.expires_at <= EXTRACT(EPOCH FROM statement_timestamp())
                    THEN 1 ELSE rate_limits.hits + 1 END,
        expires_at = CASE WHEN rate_limits.expires_at <= EXTRACT(EPOCH FROM statement_timestamp())
                          THEN EXCLUDED.expires_at ELSE rate_limits.expires_at END
    RETURNING hits
""")
_PURGE = sa.text("DELETE FROM rate_limits WHERE expires_at <= EXTRACT(EPOCH FROM statement_timestamp())")

PURGE_EVERY_SECONDS = 60
WARN_EVERY_SECONDS = 60
# After a failure, stay off the registry for a moment. Otherwise a registry that is
# unreachable (and slow to say so) would delay every single sign-in attempt.
RETRY_AFTER_SECONDS = 5

_state_lock = threading.Lock()
_last_purge = float('-inf')
_last_warning = float('-inf')
_registry_down_until = 0.0

# The old, per-worker counter: only used while the registry is unreachable.
_LOCAL_BUCKETS = {}
_local_lock = threading.Lock()


def key_hash(key):
    return hashlib.sha256(str(key).encode('utf-8')).hexdigest()


def allow(key, limit, window):
    """True if this try is within ``limit`` tries per ``window`` seconds for ``key``."""
    if time.monotonic() >= _registry_down_until:
        try:
            return _allow_shared(key, limit, window)
        except Exception as exc:  # any failure here must never break a sign-in page
            _registry_failed(exc)
    return _allow_in_process(key, limit, window)


def _allow_shared(key, limit, window):
    global _last_purge
    # Its own short-lived registry session: never the school's session, and it is
    # rolled back and closed whatever happens, so the request's own session is untouched.
    with platform_session() as session:
        hits = session.execute(_COUNT, {'key_hash': key_hash(key), 'window': float(window)}).scalar_one()
        session.commit()
        now = time.monotonic()
        with _state_lock:
            purge = now - _last_purge >= PURGE_EVERY_SECONDS
            if purge:
                _last_purge = now
        if purge:
            try:
                session.execute(_PURGE)
                session.commit()
            except Exception:  # housekeeping only; the next minute tries again
                session.rollback()
    return hits <= limit


def _registry_failed(exc):
    global _last_warning, _registry_down_until
    now = time.monotonic()
    with _state_lock:
        _registry_down_until = now + RETRY_AFTER_SECONDS
        warn = now - _last_warning >= WARN_EVERY_SECONDS
        if warn:
            _last_warning = now
    if warn:
        reason = (str(exc).strip().splitlines() or [''])[0][:200]
        log.warning('Shared rate limits are unavailable (%s: %s); this worker is counting on its own '
                    'until the registry database answers again.', type(exc).__name__, reason)


def _allow_in_process(key, limit, window):
    now = time.monotonic()
    with _local_lock:
        bucket = [t for t in _LOCAL_BUCKETS.get(key, []) if now - t < window]
        if len(bucket) >= limit:
            _LOCAL_BUCKETS[key] = bucket
            return False
        bucket.append(now)
        _LOCAL_BUCKETS[key] = bucket
        return True
