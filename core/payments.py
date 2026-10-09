"""A school's own Paystack account, so a parent can pay a fee online and have it land in the
school's finance records exactly like a payment recorded by hand.

The same shape as core/delivery.py's email/SMS settings, for the same reasons:

* **The secret key never touches a template or a log.** It is encrypted at rest, under a key
  derived from the application's secret and the school's code (its own domain, separate from
  core/delivery.py's, so a compromise of one derivation says nothing about the other), and it is
  never shown again once saved - only replaced.
* **A school with nothing set up simply cannot accept online payments**, and says so; there is no
  shared platform Paystack account to fall back to; a fee only ever reaches one school's own bank
  settlement, never a shared one.
* **Nothing is trusted from the browser alone.** A parent's browser is redirected to Paystack and
  back, but the payment is only ever confirmed by asking Paystack itself
  (``transaction/verify/<reference>``, with the school's own secret key) - never by trusting
  whatever the redirect or a webhook body claims on its own. A webhook is additionally checked
  against its signature (HMAC-SHA512 of the raw body, keyed by the secret key) before its claim is
  even read.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from flask import current_app
from sqlalchemy import select

from control_plane.context import current_tenant

API_BASE = 'https://api.paystack.co'
_ENCRYPTED = 'enc:v1:'


@dataclass(frozen=True)
class PaystackSettings:
    public_key: str
    secret_key: str


class PaystackError(Exception):
    """Paystack refused the request, or could not be reached. ``detail`` is safe to show."""

    def __init__(self, detail):
        self.detail = detail
        super().__init__(detail)


# ----------------------------------------------------------------- secrets at rest

def _cipher():
    material = os.environ.get('BRIGHTSTARS_DELIVERY_KEY', '').strip() or current_app.secret_key
    if isinstance(material, str):
        material = material.encode()
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=b'brightstars-payments-v1:' + current_tenant().slug.encode()).derive(material)
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(text):
    return _ENCRYPTED + _cipher().encrypt(text.encode()).decode()


def decrypt(stored):
    if not stored or not stored.startswith(_ENCRYPTED):
        return ''
    try:
        return _cipher().decrypt(stored[len(_ENCRYPTED):].encode()).decode()
    except (InvalidToken, ValueError):
        return ''


# ----------------------------------------------------------------- reading what is set up

def payment_settings():
    """This school's own Paystack settings, secret key decrypted, or None if not set up."""
    from models import SchoolPaymentSetting, db

    try:
        rows = dict(db.session.execute(
            select(SchoolPaymentSetting.setting_key, SchoolPaymentSetting.setting_value)).all())
    except Exception:  # a school mid-upgrade has no such table yet
        db.session.rollback()
        return None
    public_key = (rows.get('paystack_public_key') or '').strip()
    secret_key = decrypt(rows.get('paystack_secret_key'))
    if not public_key or not secret_key:
        return None
    return PaystackSettings(public_key=public_key, secret_key=secret_key)


def set_payment_settings(public_key, secret_key, admin_id):
    from datetime import datetime, timezone

    from models import SchoolPaymentSetting, db

    now = datetime.now(timezone.utc).isoformat()
    values = {'paystack_public_key': public_key.strip()}
    if secret_key:  # a blank secret key on the save form means "leave it as it is"
        values['paystack_secret_key'] = encrypt(secret_key.strip())
    for key, value in values.items():
        row = db.session.scalars(select(SchoolPaymentSetting).where(
            SchoolPaymentSetting.setting_key == key)).first()
        if row is None:
            db.session.add(SchoolPaymentSetting(setting_key=key, setting_value=value,
                                                 updated_at=now, updated_by=admin_id))
        else:
            row.setting_value = value
            row.updated_at = now
            row.updated_by = admin_id
    db.session.commit()


def clear_payment_settings():
    from models import SchoolPaymentSetting, db

    db.session.execute(SchoolPaymentSetting.__table__.delete())
    db.session.commit()


# ----------------------------------------------------------------- talking to Paystack

def _request(secret_key, method, path, payload=None, timeout=15):
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f'{API_BASE}{path}', data=body, method=method,
        headers={'Authorization': f'Bearer {secret_key}', 'Content-Type': 'application/json',
                 'Accept': 'application/json',
                 # Paystack sits behind Cloudflare, which answers urllib's default
                 # "Python-urllib/x.y" agent with an empty 403 before Paystack ever sees the key.
                 'User-Agent': 'BrightStarsAcademics/1.0 (+https://paystack.com)'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode())
        except (ValueError, UnicodeDecodeError):
            return exc.code, {}
    except Exception as exc:
        raise PaystackError(f'Paystack could not be reached: {str(exc)[:200]}') from exc


def new_reference():
    """A reference unique enough to give Paystack, and to find our own row by afterwards.

    Paystack's own documentation only allows ``-``, ``.``, ``=`` and alphanumeric characters in a
    transaction reference - an underscore is not on that list, so a hyphen separates the prefix
    from the random part rather than the more usual underscore.
    """
    return 'bsa-' + secrets.token_hex(12)


def initialize_transaction(settings, email, amount_naira, reference, callback_url):
    """Start a transaction. Returns the URL to send the parent's browser to.

    Raises PaystackError, with a message safe to show, if Paystack refuses or cannot be reached.
    """
    status, body = _request(settings.secret_key, 'POST', '/transaction/initialize', {
        'email': email, 'amount': int(round(amount_naira * 100)),  # Paystack takes kobo, not naira
        'reference': reference, 'callback_url': callback_url,
    })
    if status != 200 or not body.get('status'):
        raise PaystackError(body.get('message') or 'Paystack refused to start this payment.')
    return body['data']['authorization_url']


def verify_transaction(settings, reference):
    """Ask Paystack itself what became of ``reference``. Returns Paystack's own data dict.

    Never trusts a callback's query string or a webhook's body on its own - this is the one
    source of truth for whether money actually moved.
    """
    status, body = _request(settings.secret_key, 'GET', f'/transaction/verify/{reference}')
    if status != 200 or not body.get('status'):
        raise PaystackError(body.get('message') or 'Paystack could not find this transaction.')
    return body['data']


def check_connection(settings):
    """Whether the secret key is one Paystack recognises, without creating a real transaction.

    Listing one transaction is a read-only call that needs a valid secret key and nothing else:
    200 for a key Paystack recognises, 401 for one it does not. ``(ok, detail)``, never raises.
    """
    try:
        status, body = _request(settings.secret_key, 'GET', '/transaction?perPage=1')
    except PaystackError as exc:
        return False, exc.detail
    if status == 401:
        return False, body.get('message') or 'Paystack did not accept this secret key.'
    if status == 200 and body.get('status'):
        mode = 'live' if settings.secret_key.startswith('sk_live_') else 'test'
        if settings.public_key.startswith('pk_live_') != settings.secret_key.startswith('sk_live_'):
            return False, 'Connected, but your public key and secret key are from different modes (one test, one live). Use a matching pair.'
        return True, f'Connected to Paystack ({mode} mode).'
    return False, body.get('message') or f'Paystack answered unexpectedly (status {status}).'


def request_refund(settings, transaction_reference, amount_naira=None, customer_note=None, merchant_note=None):
    """Ask Paystack to refund a transaction, in whole or in part. Returns Paystack's own data dict.

    Accepting this request only means Paystack has *started* the refund - its own ``status`` in
    the response is ordinarily 'pending', not 'processed'; the money has not moved yet. Only the
    ``refund.processed``/``refund.failed`` webhook (paystack_webhook) settles it, the same way a
    payment itself is only settled by verify_transaction or that same webhook, never by trusting
    what starting the request returned.
    """
    payload = {'transaction': transaction_reference}
    if amount_naira is not None:
        payload['amount'] = int(round(amount_naira * 100))  # kobo, not naira - same as everywhere else
    if customer_note:
        payload['customer_note'] = customer_note
    if merchant_note:
        payload['merchant_note'] = merchant_note
    status, body = _request(settings.secret_key, 'POST', '/refund', payload)
    if status not in (200, 201) or not body.get('status'):
        raise PaystackError(body.get('message') or 'Paystack refused to start this refund.')
    return body['data']


def fetch_refund(settings, paystack_refund_id):
    """Ask Paystack for a refund's own current status, by its id - a self-heal for a school whose
    webhook never arrives or was never set up, the same purpose reconcile_pending() serves for a
    payment itself.
    """
    status, body = _request(settings.secret_key, 'GET', f'/refund/{paystack_refund_id}')
    if status != 200 or not body.get('status'):
        raise PaystackError(body.get('message') or 'Paystack could not find this refund.')
    return body['data']


def verify_webhook_signature(secret_key, raw_body, signature_header):
    """Whether a webhook's signature matches its body, computed with the school's own secret key.

    Paystack signs with HMAC-SHA512 over the exact bytes of the request body; this must be
    checked before anything in the body is read as fact, since anyone can otherwise post a
    fabricated "payment succeeded" webhook to a guessed or leaked URL.
    """
    if not signature_header:
        return False
    expected = hmac.new(secret_key.encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature_header)
