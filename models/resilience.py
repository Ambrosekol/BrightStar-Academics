"""What survives a bad connection or a restart: a durable record of background work
(core/jobs.py) and of a form that must never be double-submitted (core/idempotency.py).

Both tables exist so that "the thread died before it finished" or "the browser resent the same
click" leave a row behind to recover from, instead of nothing at all.
"""

from sqlalchemy import Index, Integer, Text, UniqueConstraint, text

from .base import db


class BackgroundJob(db.Model):
    """One piece of work that must still happen even if the thread running it never finishes.

    ``status`` moves pending -> running -> done, or back to pending (to try again) or on to
    failed (attempts used up) if the handler raised. ``payload_json`` is the handler's keyword
    arguments, so a job can be re-run by any worker with no reference to the original request.
    """
    __tablename__ = 'background_jobs'
    __table_args__ = (Index('idx_background_jobs_status', 'status', 'created_at'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    kind = db.Column(Text, nullable=False)
    payload_json = db.Column(Text, nullable=False)
    status = db.Column(Text, nullable=False, default='pending', server_default=text("'pending'"))
    attempts = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    last_error = db.Column(Text)
    created_at = db.Column(Text, nullable=False)
    started_at = db.Column(Text)
    completed_at = db.Column(Text)


class IdempotencyKey(db.Model):
    """One form submission that must only ever be acted on once.

    ``key_hash`` is a hash of the scope (which form) and the token the form carried, planted in
    the page at render time (core/idempotency.py) the same way a CSRF token is. The first request
    to claim a key runs the write and records where it sent the person; a resubmission of the
    exact same click - a double-click, or a browser silently retrying a slow POST - finds the key
    already claimed and is sent to that same place again without the write running twice.
    """
    __tablename__ = 'idempotency_keys'
    __table_args__ = (UniqueConstraint('key_hash'), Index('idx_idempotency_created', 'created_at'))

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    key_hash = db.Column(Text, nullable=False)
    scope = db.Column(Text, nullable=False)
    status = db.Column(Text, nullable=False, default='pending', server_default=text("'pending'"))
    redirect_to = db.Column(Text)
    flash_message = db.Column(Text)
    flash_category = db.Column(Text)
    created_at = db.Column(Text, nullable=False)
    completed_at = db.Column(Text)
