"""Which launch of the server this is.

Every time the server is started it writes a new random **launch id** into the platform registry
(table ``platform_state``). Every session is stamped with the id that was current when it was
opened, so after a restart the stamps no longer match and people have to sign in again, except
those in the middle of an exam (``core/session_guard.py`` decides that).

Why the registry and not a value made when the process starts: a deployment may run several
worker processes, and they must all agree. The id is written once by whatever starts the server
(``python app.py``, ``python -m control_plane upgrade`` or ``python -m control_plane new-launch``)
before any worker serves a request; every worker only reads it.

Reading is cached for a few seconds per worker, so the check on a request costs a comparison, not
a query. If the registry cannot be read, the worker keeps using the last id it saw, and if it has
never seen one it reports "unknown". Unknown means nobody is signed out: an unreachable registry
must never be the reason a room full of candidates loses its exam.
"""

import logging
import secrets
import threading
import time

import sqlalchemy as sa

from .models import PlatformState
from .registry import now_iso, platform_session

log = logging.getLogger(__name__)

KEY = 'launch_id'
# How long a worker trusts what it last read. Small enough that a worker which somehow outlives a
# restart notices within moments; large enough that the registry is not asked on every request.
CACHE_SECONDS = 5.0
# After a failed read, stay off the registry for a moment so a slow, unreachable database does not
# delay every request.
RETRY_AFTER_SECONDS = 5.0
WARN_EVERY_SECONDS = 60.0

_lock = threading.Lock()
_value = None          # the last launch id read (or written) by this process
_fresh_until = 0.0     # until when _value may be used without asking again
_last_warning = float('-inf')


def reset_cache():
    """Forget what this process knows, so the next request reads the registry."""
    global _value, _fresh_until
    with _lock:
        _value = None
        _fresh_until = 0.0


def record_new_launch():
    """Write a new launch id and return it. Everybody signed in before now must sign in again,
    unless they are sitting an exam. Called once per server start, never per request."""
    global _value, _fresh_until
    launch_id = secrets.token_hex(16)
    with platform_session() as session:
        row = session.get(PlatformState, KEY)
        if row is None:
            session.add(PlatformState(key=KEY, value=launch_id, updated_at=now_iso()))
        else:
            row.value = launch_id
            row.updated_at = now_iso()
        session.commit()
    with _lock:
        _value = launch_id
        _fresh_until = time.monotonic() + CACHE_SECONDS
    return launch_id


def current_launch_id():
    """The launch id in force now, or None when it cannot be told (nothing written yet, or the
    registry is unreachable and this process has never read it)."""
    global _value, _fresh_until, _last_warning
    now = time.monotonic()
    if now < _fresh_until:
        return _value
    try:
        with platform_session() as session:
            found = session.scalars(sa.select(PlatformState.value).where(PlatformState.key == KEY)).first()
    except Exception as exc:  # any failure here must never break a request
        with _lock:
            _fresh_until = now + RETRY_AFTER_SECONDS
            warn = now - _last_warning >= WARN_EVERY_SECONDS
            if warn:
                _last_warning = now
        if warn:
            reason = (str(exc).strip().splitlines() or [''])[0][:200]
            log.warning('The server launch id could not be read (%s: %s); nobody is signed out for '
                        'a restart until the registry answers again.', type(exc).__name__, reason)
            from core.alerting import alert
            alert('registry_unreachable', 'The platform registry could not be read.',
                  error_type=type(exc).__name__, reason=reason)
        return _value
    with _lock:
        _value = found
        _fresh_until = now + CACHE_SECONDS
    return found
