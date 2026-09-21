"""How a school's email and WhatsApp messages are sent: receipts, password recovery, and the
alerts parents get about school work.

Each school can set up its own mail server and its own WhatsApp Business account, so what a
parent receives comes from the school and not from the platform. A school that has set up
nothing falls back to the platform's shared account (the BRIGHTSTARS_SMTP_* and
BRIGHTSTARS_WHATSAPP_* environment settings), and a school that has set up nothing *and* has no
shared account simply cannot send, and says so.

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
WHATSAPP_KEYS = ('whatsapp_token', 'whatsapp_phone_number_id', 'whatsapp_graph_version')
SECRET_KEYS = frozenset({'smtp_password', 'whatsapp_token'})

# The standard mail submission ports. A school cannot aim the server at an arbitrary one.
ALLOWED_SMTP_PORTS = (25, 465, 587, 2525)
SECURITY_CHOICES = (('starttls', 'STARTTLS (usual for port 587)'),
                    ('ssl', 'SSL/TLS from the start (usual for port 465)'),
                    ('none', 'None (only for a trusted internal server)'))
DEFAULT_GRAPH_VERSION = 'v23.0'
GRAPH_URL = 'https://graph.facebook.com'

_ENCRYPTED = 'enc:v1:'
_HOST_RE = re.compile(r'^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$')
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
_VERSION_RE = re.compile(r'^v\d{1,2}\.\d{1,2}$')


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
class WhatsAppSettings:
    token: str
    phone_id: str
    version: str
    source: str


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
    """The current school's own delivery settings, secrets decrypted."""
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


def whatsapp_settings():
    """The WhatsApp Business account to send this school's messages from, or None."""
    own = _school_values()
    if own.get('whatsapp_phone_number_id', '').strip() or own.get('whatsapp_token'):
        token, phone_id = own.get('whatsapp_token', '').strip(), own.get('whatsapp_phone_number_id', '').strip()
        if not token or not phone_id:
            return None  # half set up: do not fall back to the platform's account
        return WhatsAppSettings(token, phone_id, own.get('whatsapp_graph_version') or DEFAULT_GRAPH_VERSION, 'school')
    token = os.environ.get('BRIGHTSTARS_WHATSAPP_TOKEN', '').strip()
    phone_id = os.environ.get('BRIGHTSTARS_WHATSAPP_PHONE_NUMBER_ID', '').strip()
    if not token or not phone_id:
        return None
    return WhatsAppSettings(token, phone_id,
                            os.environ.get('BRIGHTSTARS_WHATSAPP_GRAPH_VERSION', DEFAULT_GRAPH_VERSION).strip(),
                            'platform')


def status():
    """What the settings page shows: which account each channel uses.

    Non-secret facts only. A stored password or token is reported as "saved", never returned,
    and the platform's shared server is described by its sender address alone (that is what a
    parent sees), not by its host or user name.
    """
    own = _school_values()
    email, whatsapp = email_settings(), whatsapp_settings()
    email_own = bool(own.get('smtp_host', '').strip())
    whatsapp_own = bool(own.get('whatsapp_phone_number_id', '').strip() or own.get('whatsapp_token'))
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
        'whatsapp': {
            'mode': whatsapp.source if whatsapp else ('incomplete' if whatsapp_own else 'none'),
            'own_set': whatsapp_own,
            'phone_id': own.get('whatsapp_phone_number_id', ''),
            'version': own.get('whatsapp_graph_version') or DEFAULT_GRAPH_VERSION,
            'has_token': bool(own.get('whatsapp_token')),
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


def save_whatsapp(form, admin_id):
    """Validate and store a school's own WhatsApp Business account. A blank token keeps the stored one."""
    from models import SchoolDeliverySetting, db

    phone_id = (form.get('whatsapp_phone_number_id') or '').strip()
    if not re.fullmatch(r'\d{5,25}', phone_id):
        raise ValueError('The phone number ID is the long number WhatsApp gives your business phone, digits only.')
    version = (form.get('whatsapp_graph_version') or DEFAULT_GRAPH_VERSION).strip()
    if not _VERSION_RE.match(version):
        raise ValueError('The API version looks like v23.0.')
    token = (form.get('whatsapp_token') or '').strip()
    if len(token) > 1000:
        raise ValueError('The access token is too long.')
    if not token and not _school_values().get('whatsapp_token'):
        raise ValueError('Enter the access token from your WhatsApp Business account.')

    values = {'whatsapp_phone_number_id': phone_id, 'whatsapp_graph_version': version}
    if token:
        values['whatsapp_token'] = encrypt(token)
    for key, value in values.items():
        _upsert(db.session, SchoolDeliverySetting, key, value, admin_id)
    db.session.commit()
    return sorted(k for k in values if k != 'whatsapp_token') + (['whatsapp_token'] if token else [])


def clear_channel(channel):
    """Forget the school's own settings for ``email`` or ``whatsapp``, so it uses the platform's again."""
    from models import SchoolDeliverySetting, db

    keys = EMAIL_KEYS if channel == 'email' else WHATSAPP_KEYS
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


def check_whatsapp(settings):
    """Ask WhatsApp who the credentials belong to, without sending anything. ``(ok, detail)``."""
    request = urllib.request.Request(
        f'{GRAPH_URL}/{settings.version}/{settings.phone_id}?fields=display_phone_number,verified_name',
        headers={'Authorization': f'Bearer {settings.token}'})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            found = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return False, f'WhatsApp did not accept these details (error {exc.code}).'
    except Exception as exc:
        return False, f'WhatsApp could not be reached: {str(exc)[:200]}'
    who = ' — '.join(p for p in (found.get('verified_name'), found.get('display_phone_number')) if p)
    return True, f'Connected to WhatsApp{": " + who if who else ""}.'
