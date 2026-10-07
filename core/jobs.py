"""Durable background work: telling parents or students something has happened (an email, a
WhatsApp message, an in-app alert) without making the person who triggered it wait for a slow mail
server. A row is written to the database *before* the thread that does the sending starts, so a
thread that never finishes - a restart, a crash, a connection dropped mid-send - leaves something
to retry instead of the work simply vanishing, which is what a bare ``threading.Thread`` (this
module's predecessor) would do.

A handler is a plain function of keyword arguments that are JSON-serialisable (the arguments a
request already has: ids, not ORM objects), registered once with ``@job_handler('name')``.
``enqueue('name', **kwargs)`` records the work and starts it. Setting ``BACKGROUND_INLINE`` in the
application's config runs it in the calling thread instead, which is what makes it testable: the
existing tests that need a notification to have happened before the response comes back set this
and keep working unchanged.

There is deliberately no separate worker process to run or deploy: every call to ``enqueue``
first gives any of this *same kind's* earlier stuck jobs (a 'running' row whose thread died, or a
'pending'/'failed' row with attempts left) another try. A school with no traffic for a while has
nothing stuck to retry; a school with any traffic at all self-heals the next time it does
something of the same kind - no cron, no extra service, nothing new to keep running.
"""

import json
import threading
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from flask import current_app, has_request_context, request

from control_plane.context import current_tenant, tenant_context
from models import BackgroundJob, db

MAX_ATTEMPTS = 5
STUCK_RUNNING_AFTER_SECONDS = 300  # a thread that started this long ago and never finished died
RETRY_FAILED_AFTER_SECONDS = 120   # do not hammer a mail server that just rejected a message

_HANDLERS = {}


def job_handler(kind):
    """Register a function as the handler for jobs enqueued under this ``kind``."""
    def register(fn):
        _HANDLERS[kind] = fn
        return fn
    return register


def enqueue(kind, **payload):
    """Durably record ``kind(**payload)`` and start it. Returns the job's id.

    Call this only after whatever the job reports has already been committed: it may run in a
    thread that opens its own database session and would not otherwise see the change.
    """
    if kind not in _HANDLERS:
        raise ValueError(f'no handler registered for job kind {kind!r}')
    now = datetime.now(timezone.utc).isoformat()
    job = BackgroundJob(kind=kind, payload_json=json.dumps(payload), status='pending',
                        attempts=0, created_at=now)
    db.session.add(job)
    db.session.commit()
    _retry_stuck(kind)
    _spawn(job.id)
    return job.id


def _spawn(job_id):
    """Run one job's handler, in a thread of its own unless BACKGROUND_INLINE is set."""
    app = current_app._get_current_object()

    if app.config.get('BACKGROUND_INLINE'):
        # Same thread, same session, same tenant already selected: nothing to switch, and the
        # caller's own objects (still attached to this session) stay usable afterwards. This is
        # what makes the tests deterministic without disturbing the request they are asserting on.
        try:
            _execute(job_id)
        except Exception:
            app.logger.exception('Job %s could not even be started', job_id)
        return

    tenant = current_tenant(required=False)
    base_url = request.host_url if has_request_context() else None

    def run():
        try:
            with app.test_request_context(base_url=base_url or 'http://localhost/'):
                if tenant is not None:
                    with tenant_context(tenant):
                        _execute(job_id)
                else:
                    _execute(job_id)
        except Exception:
            app.logger.exception('Job %s could not even be started', job_id)

    threading.Thread(target=run, name='brightstars-job', daemon=True).start()


def _execute(job_id):
    # Claim the job in one conditional UPDATE. Several threads or containers may try for the same
    # job at once; only the one whose UPDATE changes the row (1 row) runs it, the rest back off.
    claimed = db.session.execute(sa.text(
        "UPDATE background_jobs SET status = 'running', started_at = :started "
        "WHERE id = :i AND status IN ('pending', 'failed')"
    ), {'started': datetime.now(timezone.utc).isoformat(), 'i': job_id}).rowcount
    db.session.commit()
    if claimed != 1:
        return  # already done, or another thread or container has claimed it
    job = db.session.get(BackgroundJob, job_id)
    handler = _HANDLERS.get(job.kind)
    try:
        if handler is None:
            raise LookupError(f'no handler registered for job kind {job.kind!r}')
        handler(**json.loads(job.payload_json or '{}'))
        job.status = 'done'
        job.completed_at = datetime.now(timezone.utc).isoformat()
        job.last_error = None
    except Exception as exc:
        job.attempts = (job.attempts or 0) + 1
        job.last_error = str(exc)[:2000]
        job.status = 'failed' if job.attempts >= MAX_ATTEMPTS else 'pending'
        current_app.logger.exception('Job %s (%s) failed (attempt %s)', job_id, job.kind, job.attempts)
        if job.status == 'failed':
            from core.alerting import alert
            alert('job_exhausted_retries', f'A {job.kind!r} background job used up its retries.',
                  job_id=job_id, attempts=job.attempts, last_error=job.last_error)
    db.session.commit()


def _retry_stuck(kind):
    """Give this kind's earlier stuck jobs another try. One indexed query; nothing to do most of
    the time, so it is cheap to call on every enqueue rather than only from a separate schedule."""
    now = datetime.now(timezone.utc)
    running_cutoff = (now - timedelta(seconds=STUCK_RUNNING_AFTER_SECONDS)).isoformat()
    failed_cutoff = (now - timedelta(seconds=RETRY_FAILED_AFTER_SECONDS)).isoformat()
    stuck = db.session.execute(sa.text(
        "SELECT id, status FROM background_jobs WHERE kind = :kind AND ("
        "(status = 'running' AND started_at < :running_cutoff) OR "
        "(status IN ('pending', 'failed') AND attempts < :max_attempts AND created_at < :failed_cutoff)"
        ") ORDER BY id LIMIT 5"
    ), {'kind': kind, 'running_cutoff': running_cutoff, 'failed_cutoff': failed_cutoff,
        'max_attempts': MAX_ATTEMPTS}).fetchall()
    for job_id, status in stuck:
        # A 'running' row whose thread died is not really running any more.
        db.session.execute(sa.text(
            "UPDATE background_jobs SET status = 'pending' WHERE id = :i AND status = 'running'"
        ), {'i': job_id})
        db.session.commit()
        _spawn(job_id)


def sweep_all_kinds(limit=50):
    """Give every stuck job in this tenant's database - of any kind - another try.

    Ordinary traffic already does this opportunistically (``_retry_stuck``, above) for whichever
    kind was just enqueued again; a school quiet enough to have nothing of that kind happen for a
    while would otherwise leave a stuck job stuck. This is the dedicated poller recommended in
    recommendations.html's Hardening section - one indexed query, callable from
    ``python -m control_plane sweep-jobs`` on a cron/systemd timer across every school, without a
    new service of its own to deploy or keep running. Returns how many jobs it restarted.
    """
    now = datetime.now(timezone.utc)
    running_cutoff = (now - timedelta(seconds=STUCK_RUNNING_AFTER_SECONDS)).isoformat()
    failed_cutoff = (now - timedelta(seconds=RETRY_FAILED_AFTER_SECONDS)).isoformat()
    stuck = db.session.execute(sa.text(
        "SELECT id FROM background_jobs WHERE "
        "(status = 'running' AND started_at < :running_cutoff) OR "
        "(status IN ('pending', 'failed') AND attempts < :max_attempts AND created_at < :failed_cutoff) "
        "ORDER BY id LIMIT :limit"
    ), {'running_cutoff': running_cutoff, 'failed_cutoff': failed_cutoff,
        'max_attempts': MAX_ATTEMPTS, 'limit': limit}).fetchall()
    for (job_id,) in stuck:
        db.session.execute(sa.text(
            "UPDATE background_jobs SET status = 'pending' WHERE id = :i AND status = 'running'"
        ), {'i': job_id})
        db.session.commit()
        # Run and wait, not _spawn's own thread: a short-lived CLI process that spawned a
        # daemon thread and then exited would kill it before it ever finished, leaving the very
        # row this was meant to unstick just as stuck. A poller is expected to block until done.
        _execute(job_id)
    return len(stuck)
