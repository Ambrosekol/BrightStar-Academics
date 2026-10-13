"""A small per-process cache for values that many requests ask for at once, such as the counts on the
admin pages. Each entry is kept for a few seconds and then read again.

Entries are scoped to the school being served (the tenant), so one school's count can never be shown
to another, and they are held school by school: dropping one school's counts (forget_here, on every
write there) looks only at that school's entries, never at every school's.

The cache is bounded, for a platform serving tens of thousands of schools from one process. When it is
full, entries that have run out go first; if that is not enough, the schools served least recently are
dropped whole. It is never emptied all at once, which would make every school's next page count
everything again at the same moment.
"""

import threading
import time
from collections import OrderedDict

from control_plane.context import current_tenant

_MAX_ENTRIES = 50_000
_scopes = OrderedDict()      # scope (the school's id, or None) -> {(name, *key): (expires, value)}, least recently used first
_size = [0]
_lock = threading.Lock()


def _scope():
    tenant = current_tenant(required=False)
    return tenant.id if tenant is not None else None


def remember(name, seconds, compute, *key):
    """``compute(*key)``, kept for ``seconds`` for this school. ``seconds`` of zero computes every time."""
    if seconds <= 0:
        return compute(*key)
    scope, entry_key, now = _scope(), (name,) + tuple(key), time.monotonic()
    with _lock:
        entries = _scopes.get(scope)
        hit = entries.get(entry_key) if entries is not None else None
        if hit and hit[0] > now:
            _scopes.move_to_end(scope)
            return hit[1]
    value = compute(*key)        # outside the lock: it reads the database
    with _lock:
        entries = _scopes.setdefault(scope, {})
        _scopes.move_to_end(scope)
        if entry_key not in entries:
            _size[0] += 1
        entries[entry_key] = (now + seconds, value)
        if _size[0] > _MAX_ENTRIES:
            _make_room(now)
    return value


def _make_room(now):
    """Drop what has run out; then, while still too full, the least recently served schools."""
    for scope in list(_scopes):
        entries = _scopes[scope]
        for entry_key in [k for k, (expires, _) in entries.items() if expires <= now]:
            del entries[entry_key]
            _size[0] -= 1
        if not entries:
            del _scopes[scope]
    while _size[0] > _MAX_ENTRIES * 3 // 4 and _scopes:
        _, entries = _scopes.popitem(last=False)
        _size[0] -= len(entries)


def forget(name):
    """Drop every saved copy of ``name``, for every school: the next request computes it afresh."""
    with _lock:
        for entries in _scopes.values():
            for entry_key in [k for k in entries if k[0] == name]:
                del entries[entry_key]
                _size[0] -= 1


def forget_here(*names):
    """Drop the saved copies of ``names`` for the school being served only: another school's counts and
    permissions are untouched, so one school's writes never slow another school's pages down."""
    scope = _scope()
    with _lock:
        entries = _scopes.get(scope)
        if not entries:
            return
        for entry_key in [k for k in entries if k[0] in names]:
            del entries[entry_key]
            _size[0] -= 1


def clear():
    """Forget everything (tests, and anything that must see a change at once)."""
    with _lock:
        _scopes.clear()
        _size[0] = 0
