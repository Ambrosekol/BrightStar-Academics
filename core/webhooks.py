"""Webhooks: telling the school's own system that something happened, the moment it does.

The School Admin adds an address (Settings > API & webhooks) and chooses the events it wants. When one happens the
application calls ``emit(event, data)`` after its own change is committed: a delivery row is written for each address
that wants it and sent in the background, signed, and tried again on failure (core/jobs.py, five tries). An address
that keeps failing (``DISABLE_AFTER`` deliveries in a row) is switched off, and the School Admin sees why.

What is sent is small on purpose: what happened and the ids involved, never a child's name or contact details. The
receiving system reads anything more through the API, with a key that limits what it may read.

Every notice is POSTed as JSON with two headers the receiver checks before trusting it:

    BrightStars-Event: payment.recorded
    BrightStars-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256 of "<t>.<body>" with the endpoint's secret>

Sending to an address the school typed is a way into the server's own network if left open, so each delivery: needs
https (http only outside production), resolves the host once and refuses any address that is not on the public internet
(in production), then connects to exactly that address (so a DNS answer that changes between the check and the
connection cannot redirect it), with a short timeout, no redirects followed, and only the status code read back.
"""

import hashlib
import hmac
import http.client
import ipaddress
import json
import secrets
import socket
import ssl
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import sqlalchemy as sa

from control_plane import config
from core.jobs import enqueue, job_handler
from models import WebhookDelivery, WebhookEndpoint, db

EVENTS = {
    'student.created': 'A student is registered or admitted',
    'student.archived': 'A student is archived (left, graduated, erased)',
    'payment.recorded': 'A fee payment is recorded (at the office or online)',
    'payment.voided': 'A payment is voided or refunded',
    'report_card.ready': "A student's report card for a term is ready",
}
TIMEOUT_SECONDS = 8
DISABLE_AFTER = 15
KEEP_DAYS = 30
MAX_PER_SCHOOL = 10
USER_AGENT = 'BrightstarsAcademics-Webhooks/1'


def _now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- the address
def check_url(url):
    """The cleaned URL, or ValueError saying what is wrong with it. Only the shape is checked here; where it points is
    checked at every delivery (``_resolve``), since a name can be repointed later."""
    url = (url or '').strip()
    if len(url) > 500:
        raise ValueError('That address is too long.')
    parts = urlsplit(url)
    allowed = ('https',) if config.is_production() else ('https', 'http')
    if parts.scheme not in allowed:
        raise ValueError('The address must start with https://' if config.is_production()
                         else 'The address must start with https:// or http://')
    if not parts.hostname:
        raise ValueError('The address has no server name.')
    if parts.username or parts.password:
        raise ValueError('Leave any name or password out of the address.')
    if parts.port is not None and not 1 <= parts.port <= 65535:
        raise ValueError('That port is not valid.')
    if '#' in url:
        raise ValueError('Leave the # part out of the address.')
    return url


def _resolve(host, port):
    """The one address to connect to: refused if, in production, it is not on the public internet."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ValueError(f'The server "{host}" could not be found.') from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split('%')[0])
        if getattr(ip, 'ipv4_mapped', None):
            ip = ip.ipv4_mapped
        if config.is_production() and not ip.is_global:
            raise ValueError('That address is not on the public internet, so nothing is sent to it.')
        return str(ip), info[0]
    raise ValueError(f'The server "{host}" could not be found.')


class _PinnedHTTPS(http.client.HTTPSConnection):
    """HTTPS to an address already checked, still verifying the certificate against the name."""

    def __init__(self, host, ip, family, port, **kw):
        super().__init__(host, port, timeout=TIMEOUT_SECONDS, context=ssl.create_default_context(), **kw)
        self._ip, self._family = ip, family

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, ip, family, port, **kw):
        super().__init__(host, port, timeout=TIMEOUT_SECONDS, **kw)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


def _post(url, body, headers):
    """POST ``body`` and return the status code. Raises on anything that is not a delivery."""
    parts = urlsplit(check_url(url))
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    ip, family = _resolve(parts.hostname, port)
    conn_cls = _PinnedHTTPS if parts.scheme == 'https' else _PinnedHTTP
    conn = conn_cls(parts.hostname, ip, family, port)
    path = (parts.path or '/') + (f'?{parts.query}' if parts.query else '')
    try:
        conn.request('POST', path, body=body, headers=headers)
        response = conn.getresponse()
        response.read(2048)            # never more than a little of whatever comes back
        return response.status
    finally:
        conn.close()


# ---------------------------------------------------------------- secrets and signatures
def new_secret():
    return 'whsec_' + secrets.token_urlsafe(32)


def sign(secret, timestamp, body):
    return hmac.new(secret.encode(), f'{timestamp}.'.encode() + body, hashlib.sha256).hexdigest()


def _secret(endpoint):
    from core.delivery import decrypt
    return decrypt(endpoint.secret_encrypted)


# ---------------------------------------------------------------- sending
def emit(event, data):
    """Queue ``event`` for every active address that wants it. Never raises into the caller (its own work is done)."""
    try:
        if event not in EVENTS and event != 'ping':
            raise ValueError(f'Unknown event {event!r}')
        endpoints = [e for e in db.session.scalars(sa.select(WebhookEndpoint).where(WebhookEndpoint.active == 1)).all()
                     if event == 'ping' or event in json.loads(e.events or '[]')]
        if not endpoints:
            return []
        return _queue(endpoints, event, data)
    except Exception:
        from flask import current_app
        current_app.logger.exception('Webhook %s could not be queued', event)
        db.session.rollback()
        return []


def _queue(endpoints, event, data):
    from control_plane.context import current_tenant
    tenant = current_tenant(required=False)
    event_id = 'evt_' + uuid.uuid4().hex
    now = _now().isoformat()
    payload = json.dumps({'id': event_id, 'type': event, 'created_at': now,
                          'school': tenant.slug if tenant else None, 'data': data}, separators=(',', ':'), default=str)
    rows = [WebhookDelivery(endpoint_id=e.id, event_id=event_id, event=event, payload_json=payload, status='pending',
                            attempts=0, created_at=now) for e in endpoints]
    db.session.add_all(rows)
    db.session.commit()
    for row in rows:
        enqueue('webhook_deliver', delivery_id=row.id)
    _prune()
    return rows


def payment_data(payment):
    """What a payment notice says: ids and money, no names."""
    return {'payment_id': payment.id, 'receipt_no': payment.receipt_no, 'student_id': payment.student_id,
            'session_id': payment.session_id, 'amount': payment.amount, 'currency': 'NGN', 'category': payment.category,
            'method': payment.method, 'paid_at': payment.paid_at, 'status': payment.status,
            'voided_at': payment.voided_at}


def send_test(endpoint):
    """A 'ping' to one address, sent now. Returns the delivery row."""
    return _queue([endpoint], 'ping', {'message': 'A test from Brightstars Academics.'})[0]


@job_handler('webhook_deliver')
def deliver(delivery_id):
    row = db.session.get(WebhookDelivery, delivery_id)
    if row is None or row.status == 'delivered':
        return
    endpoint = db.session.get(WebhookEndpoint, row.endpoint_id)
    if endpoint is None or (not endpoint.active and row.event != 'ping'):
        row.status, row.error = 'skipped', 'The address was switched off.'
        db.session.commit()
        return
    secret = _secret(endpoint)
    if not secret:
        # The stored secret cannot be read (the server's key changed): sending unsigned would let anyone forge one.
        row.status, row.error = 'failed', 'The signing secret cannot be read. Remove this address and add it again.'
        db.session.commit()
        return
    body = row.payload_json.encode()
    timestamp = str(int(time.time()))
    headers = {'Content-Type': 'application/json', 'User-Agent': USER_AGENT, 'BrightStars-Event': row.event,
               'BrightStars-Delivery': str(row.id),
               'BrightStars-Signature': f't={timestamp},v1={sign(secret, timestamp, body)}'}
    row.attempts = (row.attempts or 0) + 1
    try:
        code = _post(endpoint.url, body, headers)
        row.response_code = code
        if not 200 <= code < 300:
            raise RuntimeError(f'The address answered {code}.')
    except Exception as exc:
        row.error = str(exc)[:300]
        last = row.attempts >= 5 or isinstance(exc, ValueError)       # a refused address will not get better
        row.status = 'failed' if last else 'retrying'
        if last:
            endpoint.failures_in_a_row = (endpoint.failures_in_a_row or 0) + 1
            if endpoint.failures_in_a_row >= DISABLE_AFTER and endpoint.active:
                endpoint.active = 0
                endpoint.disabled_reason = (f'Switched off after {DISABLE_AFTER} deliveries in a row failed. '
                                            f'The last: {row.error}')
        db.session.commit()
        if not last:
            raise                       # core/jobs tries again later
        return
    row.status, row.error, row.delivered_at = 'delivered', None, _now().isoformat()
    endpoint.failures_in_a_row = 0
    db.session.commit()


def _prune():
    cutoff = (_now() - timedelta(days=KEEP_DAYS)).isoformat()
    db.session.execute(sa.delete(WebhookDelivery).where(WebhookDelivery.created_at < cutoff))
    db.session.commit()


def resend(delivery):
    """Send an earlier delivery again (same event id, so the receiver can tell it is a repeat)."""
    delivery.status, delivery.attempts, delivery.error = 'pending', 0, None
    db.session.commit()
    enqueue('webhook_deliver', delivery_id=delivery.id)
