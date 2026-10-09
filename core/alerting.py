"""A single, minimal hook for the handful of log lines that mean something is degraded enough for
a person to want to know right away, not just a line to notice later while reading the log.

Set BRIGHTSTARS_ALERT_WEBHOOK to any URL that accepts a JSON POST (Slack's own "Incoming Webhook"
address works with no changes, since the payload is ``{"text": "..."}``) and every alert() call
also posts there - fire-and-forget, in a thread of its own, so a slow or unreachable webhook never
adds a moment to the request that triggered it, and never turns a real problem into a second one.
With nothing configured, alert() only logs - exactly what every one of these call sites already did
before this file existed, so there is nothing new to break for a deployment that wants nothing more
than that.

This is deliberately not a delivery channel of its own (no retry, no queue, no history): it is a
notice to whoever already has a phone or a Slack channel wired to that one webhook, for the four
things recommendations.html's Hardening section names as worth paging on:

* the platform registry could not be read at all (control_plane/launch.py)
* a job has used up its retries and is left failed (core/jobs.py)
* a burst of delivery failures for one school (core/delivery.py)
* a sign-in rate limit was hit - a streak of refusals, not one honest mistake (blueprints/auth/routes.py)
"""

import json
import logging
import os
import threading
import urllib.request

_log = logging.getLogger('brightstars.alert')


def alert(category, message, **details):
    """Log, and (if configured) notify a webhook. Never raises; never blocks the caller."""
    try:
        _log.warning('[%s] %s %s', category, message, details or '')
    except Exception:
        pass
    webhook = os.environ.get('BRIGHTSTARS_ALERT_WEBHOOK', '').strip()
    if not webhook:
        return
    text = f'[{category}] {message}' + (f' — {details}' if details else '')
    payload = json.dumps({'text': text}).encode()

    def _send():
        try:
            request = urllib.request.Request(
                webhook, data=payload, method='POST',
                headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(request, timeout=5).read()
        except Exception:
            pass  # the alert already reached the log; the webhook is a bonus, not a guarantee

    threading.Thread(target=_send, name='brightstars-alert', daemon=True).start()


def note_delivery_failure(channel, detail=''):
    """Count one email/SMS delivery failure for the current school, and alert once a burst
    of them - not a single honest bounce, which is not worth anyone's attention - has piled up
    within a short window. The counter itself is the same shared, registry-backed one every
    sign-in and password-reset rate limit already uses (control_plane/ratelimit.py), so it is
    shared across every worker process without a table of its own.
    """
    from control_plane.context import current_tenant
    from control_plane.ratelimit import allow

    try:
        tenant = current_tenant(required=False)
        scope = tenant.slug if tenant else 'unknown'
    except Exception:
        scope = 'unknown'
    if not allow(f'delivery-failure-burst:{scope}:{channel}', limit=5, window=600):
        alert('delivery_failure_burst', f'A burst of {channel} delivery failures for one school.',
              school=scope, channel=channel, detail=str(detail)[:200])
