"""A form that must never run twice: a double-click, two browser tabs racing, or - the case a
weak connection actually causes - the browser silently retrying a POST it never saw a reply to,
must never create the record a second time.

``idempotency_key()`` is a template global, the same idea as ``csrf_token()``: it plants a fresh
random value in a hidden field at render time. ``@idempotent_write('scope')`` decorates the view;
on POST, it claims that value in the database (``IdempotencyKey``, models/resilience.py) before
doing anything else. Exactly one request ever wins the claim and runs the real write; every other
request carrying the same key - the resubmission - is sent to wherever the winning request ended
up, without running the write again, once that first request has finished.

A form that never adds the hidden field is simply unprotected (the decorator falls through to the
view as if it were not there), so adding this to an existing route needs the one line in its
template as well as the decorator - a route search checks both are always done together.
"""

import hashlib
import secrets
from datetime import datetime, timezone
from functools import wraps

from flask import flash, redirect, request, session
from sqlalchemy import select

from models import IdempotencyKey, db
from core.db_helpers import insert_stmt

RESUBMITTED_WHILE_PENDING = (
    'This was already submitted a moment ago and is still being processed. '
    'Please wait a moment and refresh the page.'
)


def idempotency_key():
    """A fresh, single-use token for one form render. Needs no session bookkeeping: the claim
    itself (not a comparison against something stored first) is what makes it safe."""
    return secrets.token_urlsafe(24)


def _key_hash(scope, raw_key):
    return hashlib.sha256(f'{scope}:{raw_key}'.encode()).hexdigest()


def idempotent_write(scope):
    """Guard a view so resubmitting the same rendered form's click never runs its write twice.

    ``scope`` names this form (unique per route, so the same key value from two different pages
    never collides). Only POST/PUT/PATCH/DELETE are guarded; GET always renders normally.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if request.method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
                return fn(*args, **kwargs)
            raw_key = (request.form.get('_idempotency_key', '') or '').strip()
            if not raw_key:
                # No key on the form: this submission carries nothing to dedupe against, so it
                # is simply unprotected, exactly as if the decorator were not there.
                return fn(*args, **kwargs)

            key_hash = _key_hash(scope, raw_key)
            now = datetime.now(timezone.utc).isoformat()
            # A plain INSERT's rowcount is not reliably reported by every driver on ON CONFLICT DO
            # NOTHING (some report -1, "unknown", whether or not a row was inserted), so who won
            # the claim is read from RETURNING instead: a row comes back only when this exact
            # execution was the one that inserted it, never on a conflict.
            claim = insert_stmt(IdempotencyKey).values(
                scope=scope, key_hash=key_hash, status='pending', created_at=now
            ).on_conflict_do_nothing(index_elements=['key_hash']).returning(IdempotencyKey.id)
            claimed = db.session.execute(claim).scalar() is not None
            db.session.commit()

            if not claimed:
                existing = db.session.scalars(
                    select(IdempotencyKey).where(IdempotencyKey.key_hash == key_hash)).first()
                if existing and existing.status == 'done':
                    if existing.flash_message:
                        flash(existing.flash_message, existing.flash_category or 'success')
                    return redirect(existing.redirect_to or request.path)
                # Still pending: a genuine concurrent duplicate (two tabs, a very fast
                # double-click) racing the first request, which has not finished yet.
                flash(RESUBMITTED_WHILE_PENDING, 'error')
                return redirect(request.referrer or request.path)

            response = fn(*args, **kwargs)

            row = db.session.scalars(
                select(IdempotencyKey).where(IdempotencyKey.key_hash == key_hash)).first()
            if row is not None:
                row.status = 'done'
                row.completed_at = datetime.now(timezone.utc).isoformat()
                if 300 <= response.status_code < 400:
                    row.redirect_to = response.headers.get('Location')
                pending_flashes = session.get('_flashes') or []
                if pending_flashes:
                    row.flash_category, row.flash_message = pending_flashes[-1]
                db.session.commit()
            return response
        return wrapper
    return decorator
