"""How a school's email and SMS messages are sent: receipts, password recovery, and the
alerts parents get about their child's account.

Email goes out through a mail server; SMS goes out through BulkSMS Nigeria. Who pays for the SMS is
decided by the platform, not by each school, with one setting (BRIGHTSTARS_SMS_PAYER):

* ``school``   - every school connects its own BulkSMS Nigeria account and pays for its own messages.
                 A school that has not connected one simply cannot send SMS, and says so. (The default.)
* ``platform`` - the platform's one account (BRIGHTSTARS_SMS_API_TOKEN / BRIGHTSTARS_SMS_SENDER_ID)
                 sends for every school, and the platform carries the cost. Schools are not asked for
                 SMS details at all.
* ``either``   - a school's own account is used when it has one, and the platform's otherwise.

Email works the way it always has: a school's own mail server, else the platform's shared one
(BRIGHTSTARS_SMTP_*).

Three things here are security-sensitive, and each has a reason:

* **Secrets at rest.** An SMTP password and a WhatsApp token belong to the school. They are kept
  encrypted in the school's own database, under a key derived from the application's secret and
  the school's code, so a copy of a school's database is not a copy of its mail credentials and one
  school's stored token means nothing in another. They are never shown again after being saved.
* **Where the server connects.** Letting a school choose the host that *the server* connects to
  is a way to probe the server's own network. So a school's mail host must be a public internet
  address (checked when it is saved and again just before every connection, and the connection is
  made to the very address that was checked), and only the standard mail ports are allowed. The
  platform's own configured server is trusted and not restricted.
* **A half-set-up school never borrows the platform's account.** If a school has entered a mail
  host, its settings are used exclusively, even if incomplete, rather than quietly sending its
  parents' mail through the platform's address.
"""

import base64
import ipaddress
import json
import os
import re
import smtplib
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from flask import current_app
from sqlalchemy import delete, select

from control_plane import config
from control_plane.context import current_tenant

EMAIL_KEYS = ('smtp_host', 'smtp_port', 'smtp_user', 'smtp_password', 'smtp_from', 'smtp_security')
SMS_KEYS = ('sms_api_token', 'sms_sender_id', 'sms_gateway')
SECRET_KEYS = frozenset({'smtp_password', 'sms_api_token'})

# The standard mail submission ports. A school cannot aim the server at an arbitrary one.
ALLOWED_SMTP_PORTS = (25, 465, 587, 2525)
SECURITY_CHOICES = (('starttls', 'STARTTLS (usual for port 587)'),
                    ('ssl', 'SSL/TLS from the start (usual for port 465)'),
                    ('none', 'None (only for a trusted internal server)'))

# BulkSMS Nigeria (https://www.bulksmsnigeria.com/api): one JSON POST per message, a bearer token.
SMS_API_URL = 'https://www.bulksmsnigeria.com/api/v2/sms'
SMS_BALANCE_URL = 'https://www.bulksmsnigeria.com/api/v2/balance'
SMS_GATEWAYS = (('direct-refund', 'Direct (refunded if not delivered)'),
                ('direct-corporate', 'Corporate (most reliable, for important notices)'),
                ('dual-backup', 'Dual route with backup'))
DEFAULT_SMS_GATEWAY = 'direct-refund'
SMS_PAYERS = ('school', 'platform', 'either')

_ENCRYPTED = 'enc:v1:'
_HOST_RE = re.compile(r'^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$')
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
_SENDER_RE = re.compile(r'^[A-Za-z0-9 ]{3,11}$')


@dataclass(frozen=True)
class EmailSettings:
    host: str
    port: int
    user: str
    password: str
    sender: str
    security: str   # 'starttls' | 'ssl' | 'none'
    source: str     # 'school' | 'platform'


@dataclass(frozen=True)
class SmsSettings:
    token: str
    sender: str     # the sender name parents see, at most 11 letters and digits
    gateway: str
    source: str     # 'school' | 'platform'


# ----------------------------------------------------------------- secrets at rest

def _cipher():
    """The encryption key for the current school's secrets.

    Derived from the application's secret (or BRIGHTSTARS_DELIVERY_KEY, so rotating the session
    secret need not strand stored credentials) and the school's code.
    """
    material = os.environ.get('BRIGHTSTARS_DELIVERY_KEY', '').strip() or current_app.secret_key
    if isinstance(material, str):
        material = material.encode()
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=b'brightstars-delivery-v1:' + current_tenant().slug.encode()).derive(material)
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(text):
    return _ENCRYPTED + _cipher().encrypt(text.encode()).decode()


def decrypt(stored):
    """The secret, or '' if it is unreadable — a changed key, or a value that was never
    encrypted. A secret is never accepted in the clear, so it is simply not used."""
    if not stored or not stored.startswith(_ENCRYPTED):
        return ''
    try:
        return _cipher().decrypt(stored[len(_ENCRYPTED):].encode()).decode()
    except (InvalidToken, ValueError):
        return ''


# ----------------------------------------------------------------- reading what is set up

def _school_values():
    """The current school's own delivery settings, secrets decrypted. Read once per request; a save drops it."""
    from core.speed import request_memo
    return dict(request_memo('delivery-settings', _read_school_values))


def _read_school_values():
    from models import SchoolDeliverySetting, db

    try:
        rows = db.session.execute(select(SchoolDeliverySetting.setting_key,
                                         SchoolDeliverySetting.setting_value)).all()
    except Exception:  # a school mid-upgrade has no such table yet
        db.session.rollback()
        return {}
    return {key: (decrypt(value) if key in SECRET_KEYS else (value or '')) for key, value in rows}


def _int(value, default):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _platform_security(port):
    """The platform's shared server: implicit TLS on 465, else STARTTLS, unless overridden."""
    override = os.environ.get('BRIGHTSTARS_SMTP_SSL', '').strip()
    if override:
        return 'ssl' if override != '0' else ('none' if os.environ.get('BRIGHTSTARS_SMTP_STARTTLS', '1') == '0' else 'starttls')
    if port == 465:
        return 'ssl'
    return 'none' if os.environ.get('BRIGHTSTARS_SMTP_STARTTLS', '1') == '0' else 'starttls'


def email_settings():
    """The email account to send this school's mail from, or None when there is none."""
    own = _school_values()
    host = own.get('smtp_host', '').strip()
    if host:
        user = own.get('smtp_user', '').strip()
        sender = own.get('smtp_from', '').strip() or user
        if not sender:
            return None  # half set up: do not fall back to the platform's account
        return EmailSettings(host, _int(own.get('smtp_port'), 587), user, own.get('smtp_password', ''), sender,
                             own.get('smtp_security') or 'starttls', 'school')
    host = os.environ.get('BRIGHTSTARS_SMTP_HOST', '').strip()
    user = os.environ.get('BRIGHTSTARS_SMTP_USER', '').strip()
    sender = os.environ.get('BRIGHTSTARS_SMTP_FROM', user).strip()
    if not host or not sender:
        return None
    port = _int(os.environ.get('BRIGHTSTARS_SMTP_PORT', '587') or 587, 587)
    return EmailSettings(host, port, user, os.environ.get('BRIGHTSTARS_SMTP_PASSWORD', ''), sender,
                         _platform_security(port), 'platform')


def sms_payer():
    """Who pays for SMS: ``school``, ``platform`` or ``either`` (see the module docstring)."""
    value = os.environ.get('BRIGHTSTARS_SMS_PAYER', 'school').strip().lower()
    return value if value in SMS_PAYERS else 'school'


def _platform_gateway():
    value = os.environ.get('BRIGHTSTARS_SMS_GATEWAY', '').strip()
    return value if value in dict(SMS_GATEWAYS) else DEFAULT_SMS_GATEWAY


def _platform_sms():
    token = os.environ.get('BRIGHTSTARS_SMS_API_TOKEN', '').strip()
    sender = os.environ.get('BRIGHTSTARS_SMS_SENDER_ID', '').strip()
    if not token or not sender:
        return None
    return SmsSettings(token, sender, _platform_gateway(), 'platform')


def sms_settings():
    """The BulkSMS Nigeria account to send this school's SMS from, or None when there is none."""
    payer = sms_payer()
    if payer == 'platform':
        return _platform_sms()
    own = _school_values()
    token, sender = own.get('sms_api_token', '').strip(), own.get('sms_sender_id', '').strip()
    if token or sender:
        if not token or not sender:
            return None  # half set up: do not fall back to the platform's account
        gateway = own.get('sms_gateway') or DEFAULT_SMS_GATEWAY
        return SmsSettings(token, sender, gateway if gateway in dict(SMS_GATEWAYS) else DEFAULT_SMS_GATEWAY, 'school')
    return _platform_sms() if payer == 'either' else None


def status():
    """What the settings page shows: which account each channel uses.

    Non-secret facts only. A stored password or token is reported as "saved", never returned,
    and the platform's shared server is described by its sender address alone (that is what a
    parent sees), not by its host or user name.
    """
    own = _school_values()
    email, sms = email_settings(), sms_settings()
    payer = sms_payer()
    email_own = bool(own.get('smtp_host', '').strip())
    sms_own = bool(own.get('sms_api_token') or own.get('sms_sender_id', '').strip())
    return {
        'email': {
            'mode': email.source if email else ('incomplete' if email_own else 'none'),
            'own_set': email_own,
            'sender': email.sender if email else '',
            'host': own.get('smtp_host', ''), 'port': _int(own.get('smtp_port'), 587),
            'user': own.get('smtp_user', ''), 'from': own.get('smtp_from', ''),
            'security': own.get('smtp_security') or 'starttls',
            'has_password': bool(own.get('smtp_password')),
        },
        'sms': {
            'mode': sms.source if sms else ('incomplete' if sms_own and payer != 'platform' else 'none'),
            'payer': payer,
            # With the platform paying, a school has nothing to enter: its own settings are not used.
            'can_configure': payer != 'platform',
            'own_set': sms_own and payer != 'platform',
            'sender_id': own.get('sms_sender_id', ''),
            'sender_in_use': sms.sender if sms else '',
            'gateway': own.get('sms_gateway') or DEFAULT_SMS_GATEWAY,
            'has_token': bool(own.get('sms_api_token')),
        },
    }


# ----------------------------------------------------------------- where the server may connect

def resolve_public(host):
    """The public IP addresses ``host`` points at, or ValueError.

    Outside production a private address is allowed, so a developer can use a local mail catcher;
    in production every address a name resolves to must be a public internet address.
    """
    try:
        found = {info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
    except OSError:
        raise ValueError(f'The mail server address "{host}" could not be found.') from None
    addresses = []
    for text in sorted(found):
        ip = ipaddress.ip_address(text.split('%')[0])
        if getattr(ip, 'ipv4_mapped', None):
            ip = ip.ipv4_mapped
        if config.is_production() and not ip.is_global:
            raise ValueError('That mail server address is not allowed: it is not a public internet address.')
        addresses.append(str(ip))
    if not addresses:
        raise ValueError(f'The mail server address "{host}" could not be found.')
    return addresses


# ----------------------------------------------------------------- saving

def _upsert(session, model, key, value, admin_id):
    from core.speed import request_forget
    request_forget('delivery-settings')
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    row = session.scalars(select(model).where(model.setting_key == key)).first()
    if row is None:
        session.add(model(setting_key=key, setting_value=value, updated_at=now, updated_by=admin_id))
    else:
        row.setting_value, row.updated_at, row.updated_by = value, now, admin_id


def save_email(form, admin_id):
    """Validate and store a school's own mail server. Returns the names of the fields set.

    A blank password keeps the stored one; there is no way to read it back.
    """
    from models import SchoolDeliverySetting, db

    host = (form.get('smtp_host') or '').strip().lower()
    if not host or not _HOST_RE.match(host):
        raise ValueError('Enter the mail server as a host name, such as smtp.example.com.')
    port = _int(form.get('smtp_port'), 0)
    if port not in ALLOWED_SMTP_PORTS:
        raise ValueError('The mail server port must be one of ' + ', '.join(map(str, ALLOWED_SMTP_PORTS)) + '.')
    security = (form.get('smtp_security') or 'starttls').strip()
    if security not in dict(SECURITY_CHOICES):
        raise ValueError('Choose how the connection to the mail server is secured.')
    user = (form.get('smtp_user') or '').strip()
    sender = (form.get('smtp_from') or '').strip() or user
    if not _EMAIL_RE.match(sender) or len(sender) > 200:
        raise ValueError('Enter the address the emails are sent from, such as office@yourschool.org.')
    if len(user) > 200:
        raise ValueError('The mail server username is too long.')
    password = form.get('smtp_password') or ''
    if len(password) > 300:
        raise ValueError('The mail server password is too long.')
    resolve_public(host)  # refuse a private address now, not only when the first message is sent

    values = {'smtp_host': host, 'smtp_port': str(port), 'smtp_user': user, 'smtp_from': sender,
              'smtp_security': security}
    if password:
        values['smtp_password'] = encrypt(password)
    for key, value in values.items():
        _upsert(db.session, SchoolDeliverySetting, key, value, admin_id)
    db.session.commit()
    return sorted(k for k in values if k != 'smtp_password') + (['smtp_password'] if password else [])


def save_sms(form, admin_id):
    """Validate and store a school's own BulkSMS Nigeria account. A blank token keeps the stored one."""
    from models import SchoolDeliverySetting, db

    if sms_payer() == 'platform':
        raise ValueError('SMS is provided by the platform for every school, so there is nothing to set up here.')
    sender = (form.get('sms_sender_id') or '').strip()
    if not _SENDER_RE.match(sender):
        raise ValueError('The sender name is what parents see on the message: 3 to 11 letters and digits, '
                         'registered and approved on your BulkSMS Nigeria account.')
    gateway = (form.get('sms_gateway') or DEFAULT_SMS_GATEWAY).strip()
    if gateway not in dict(SMS_GATEWAYS):
        raise ValueError('Choose how the messages are routed.')
    token = (form.get('sms_api_token') or '').strip()
    if len(token) > 1000:
        raise ValueError('The API token is too long.')
    if not token and not _school_values().get('sms_api_token'):
        raise ValueError('Enter the API token from your BulkSMS Nigeria account.')

    values = {'sms_sender_id': sender, 'sms_gateway': gateway}
    if token:
        values['sms_api_token'] = encrypt(token)
    for key, value in values.items():
        _upsert(db.session, SchoolDeliverySetting, key, value, admin_id)
    db.session.commit()
    return sorted(k for k in values if k != 'sms_api_token') + (['sms_api_token'] if token else [])


def clear_channel(channel):
    """Forget the school's own settings for ``email`` or ``sms``, so it uses the platform's again."""
    from models import SchoolDeliverySetting, db

    keys = EMAIL_KEYS if channel == 'email' else SMS_KEYS
    from core.speed import request_forget
    request_forget('delivery-settings')
    db.session.execute(delete(SchoolDeliverySetting).where(SchoolDeliverySetting.setting_key.in_(keys)))
    db.session.commit()


# ----------------------------------------------------------------- sending

def _open_smtp(settings):
    """A connected SMTP client. Raises on any failure.

    A school's own host is checked just before connecting and the connection is made to the
    address that was checked, so the name cannot be pointed somewhere else in between. The
    certificate is still verified against the host *name*.
    """
    context = ssl.create_default_context()
    if settings.source == 'school':
        target = resolve_public(settings.host)[0]
    else:
        target = settings.host
    client = (smtplib.SMTP_SSL(timeout=20, context=context) if settings.security == 'ssl'
              else smtplib.SMTP(timeout=20))
    client._host = settings.host  # what TLS checks the certificate against
    try:
        client.connect(target, settings.port)
        if settings.security == 'starttls':
            client.starttls(context=context)
        if settings.user:
            client.login(settings.user, settings.password)
    except Exception:
        try:
            client.close()
        except Exception:
            pass
        raise
    return client


def send_email(settings, msg):
    """Send a prepared message with ``settings``. Raises on failure; the caller decides what to say."""
    if not msg['From']:
        msg['From'] = settings.sender
    client = _open_smtp(settings)
    try:
        client.send_message(msg)
    finally:
        try:
            client.quit()
        except Exception:
            client.close()


def check_email(settings, to_address, school):
    """Send a short test message. Returns ``(ok, detail)``; never raises."""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg['Subject'] = f'{school} — test of your email delivery'
    msg['To'] = to_address
    msg.set_content(f'This is a test message from {school}. If you can read it, your school can send email.')
    try:
        send_email(settings, msg)
        return True, f'A test message was sent to {to_address}.'
    except Exception as exc:
        return False, f'The test message could not be sent: {str(exc)[:200]}'


def _sms_request(url, settings, payload=None):
    """One call to BulkSMS Nigeria. Returns ``(http status, parsed JSON or {})``; raises only on a network failure."""
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method='POST' if payload is not None else 'GET',
        headers={'Authorization': f'Bearer {settings.token}', 'Accept': 'application/json',
                 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status, text = getattr(response, 'status', 200), response.read().decode(errors='replace')
    except urllib.error.HTTPError as exc:
        status, text = exc.code, exc.read().decode(errors='replace')
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}


def _sms_problem(status, found):
    """The provider's own words for what went wrong, trimmed, or a plain description of the status."""
    error = found.get('error') if isinstance(found.get('error'), dict) else {}
    text = found.get('message') or error.get('message') or ''
    code = found.get('code') or error.get('code') or ''
    if text:
        return f'{text} ({code})' if code else str(text)[:200]
    return {401: 'The API token was not accepted.', 402: 'The BulkSMS Nigeria wallet has no balance.',
            403: 'The sender name is not registered or approved.'}.get(status, f'BulkSMS Nigeria answered with error {status}.')


def send_sms(settings, to, body):
    """Send one text message. Returns ``(ok, detail)`` - the provider's message id, or why it failed. Never raises."""
    try:
        status, found = _sms_request(SMS_API_URL, settings,
                                     {'from': settings.sender, 'to': to, 'body': body, 'gateway': settings.gateway})
    except Exception as exc:
        return False, f'SMS delivery failed: {str(exc)[:200]}'
    failed = status >= 400 or found.get('status') == 'error' or 'error' in found
    if failed:
        return False, _sms_problem(status, found)
    data = found.get('data') if isinstance(found.get('data'), dict) else {}
    return True, str(data.get('id') or to)


def _wallets(found):
    """The money figures in a balance reply, as ``[(label, amount)]``. The reply has several wallets (universal,
    SMS, bonus credit); each numeric value is shown under its own name, so nothing is guessed at."""
    data = found.get('data') if isinstance(found.get('data'), dict) else found
    out = []

    def walk(node, prefix):
        for key, value in node.items():
            name = f'{prefix} {key}'.strip() if prefix else str(key)
            if isinstance(value, dict):
                walk(value, name)
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                out.append((name, float(value)))
            elif isinstance(value, str) and re.fullmatch(r'-?\d+(\.\d+)?', value.strip()):
                out.append((name, float(value)))
    if isinstance(data, dict):
        walk(data, '')
    skip = ('id', 'code', 'status', 'user')
    return [(re.sub(r'[_\-]+', ' ', n).strip().capitalize().replace('Sms', 'SMS').replace(' sms', ' SMS'), v)
            for n, v in out if n.lower() not in skip]


def sms_balance(settings):
    """Ask BulkSMS Nigeria what is left in the account. Returns ``(ok, detail, wallets)``; never raises."""
    try:
        status, found = _sms_request(SMS_BALANCE_URL, settings)
    except Exception as exc:
        return False, f'BulkSMS Nigeria could not be reached: {str(exc)[:200]}', []
    if status >= 400 or found.get('status') == 'error':
        return False, _sms_problem(status, found), []
    wallets = _wallets(found)
    if not wallets:
        return True, 'Connected to BulkSMS Nigeria, but it did not report a balance in a form that can be shown here. Check your BulkSMS Nigeria dashboard.', []
    def show(label, amount):
        if 'credit' in label.lower():          # bonus credits are a count of messages, not naira
            return f'{label} {amount:,.0f}'
        return f'₦{amount:,.2f}' if label.lower() == 'balance' else f'{label} ₦{amount:,.2f}'
    parts = [show(label, amount) for label, amount in wallets]
    return True, 'Balance: ' + ' · '.join(parts), wallets


def check_sms(settings):
    """Ask BulkSMS Nigeria whether the token works, without sending anything. ``(ok, detail)``."""
    ok, detail, _ = sms_balance(settings)
    if not ok:
        return False, detail
    return True, f'Connected to BulkSMS Nigeria. Messages will be sent as "{settings.sender}".'
