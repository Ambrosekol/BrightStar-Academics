"""A small per-process cache for values that many requests ask for at once, such as the counts on the
admin pages. Each entry is kept for a few seconds and then read again.

Entries are scoped to the school being served (the tenant), so one school's count can never be shown
to another. The cache is bounded: when it is full it is emptied, rather than growing without limit as
the number of schools and administrators grows.
"""

import time

from control_plane.context import current_tenant

_MAX_ENTRIES = 4096
_entries = {}


def remember(name, seconds, compute, *key):
    """``compute(*key)``, kept for ``seconds`` for this school. ``seconds`` of zero computes every time."""
    if seconds <= 0:
        return compute(*key)
    tenant = current_tenant(required=False)
    scope = tenant.id if tenant is not None else None
    cache_key = (scope, name) + tuple(key)
    now = time.monotonic()
    hit = _entries.get(cache_key)
    if hit and hit[0] > now:
        return hit[1]
    value = compute(*key)
    if len(_entries) >= _MAX_ENTRIES:
        _entries.clear()
    _entries[cache_key] = (now + seconds, value)
    return value


def forget(name):
    """Drop every saved copy of ``name``, for every school: the next request computes it afresh."""
    for cache_key in [k for k in _entries if k[1] == name]:
        _entries.pop(cache_key, None)


def forget_here(*names):
    """Drop the saved copies of ``names`` for the school being served only: another school's counts and
    permissions are untouched, so one school's writes never slow another school's pages down."""
    tenant = current_tenant(required=False)
    scope = tenant.id if tenant is not None else None
    for cache_key in [k for k in _entries if k[0] == scope and k[1] in names]:
        _entries.pop(cache_key, None)


def clear():
    """Forget everything (tests, and anything that must see a change at once)."""
    _entries.clear()
