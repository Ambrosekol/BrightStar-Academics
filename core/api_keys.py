"""The school's API keys: read-only access for the school's own developers, or a partner it hires.

A key is ``bsa_<prefix>_<secret>``. The prefix finds the key's row; only a SHA-256 hash of the secret is kept (a key is
long and random, so a slow password hash buys nothing and would slow every call), compared in constant time. The key is
shown once, when the School Admin creates it, and can be revoked at any time. Each key may read only the areas
(``SCOPES``) it was given, never writes, and is limited to ``RATE_PER_MINUTE`` calls a minute.
"""

import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from models import ApiKey, db

SCOPES = {
    'students': 'Students: names, admission numbers, class, gender, date of birth',
    'parents': "Parents: names and contact details, and which children they are linked to",
    'classes': 'Classes and subjects',
    'attendance': 'Attendance: each day each student was present, late or absent',
    'results': 'Released results',
    'fees': 'Fees charged to each student',
    'payments': 'Payments and receipts',
}
RATE_PER_MINUTE = 120
MAX_KEYS = 20
KEY_RE = re.compile(r'^bsa_([0-9a-f]{12})_([A-Za-z0-9_-]{32,64})$')


def _now():
    return datetime.now(timezone.utc)


def _hash(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def create(name, scopes, admin_id):
    """A new key (not committed). Returns ``(row, the full key)``: the key is never available again."""
    name = ' '.join(str(name or '').split())[:80]
    if not name:
        raise ValueError('Name the key after who will use it, for example "Accounting system".')
    scopes = sorted({s for s in scopes if s in SCOPES})
    if not scopes:
        raise ValueError('Choose at least one area the key may read.')
    if db.session.scalar(sa.select(sa.func.count()).select_from(ApiKey).where(ApiKey.revoked_at.is_(None))) >= MAX_KEYS:
        raise ValueError(f'A school can have at most {MAX_KEYS} keys in use. Revoke one you no longer need.')
    prefix, secret = secrets.token_hex(6), secrets.token_urlsafe(32)
    row = ApiKey(name=name, prefix=prefix, secret_hash=_hash(secret), scopes=json.dumps(scopes), created_by=admin_id,
                 created_at=_now().isoformat())
    db.session.add(row)
    return row, f'bsa_{prefix}_{secret}'


def authenticate(header):
    """``(ApiKey, None)`` for a valid ``Authorization: Bearer <key>``, or ``(None, why)``."""
    header = (header or '').strip()
    if not header.lower().startswith('bearer '):
        return None, 'Send the key as: Authorization: Bearer <key>'
    found = KEY_RE.match(header[7:].strip())
    if not found:
        return None, 'That is not a valid key.'
    prefix, secret = found.groups()
    row = db.session.scalars(sa.select(ApiKey).where(ApiKey.prefix == prefix)).first()
    # Compared even when there is no such key, so a wrong prefix takes as long as a wrong secret.
    expected = row.secret_hash if row else _hash('no such key ' + prefix)
    if not hmac.compare_digest(expected, _hash(secret)) or row is None:
        return None, 'That is not a valid key.'
    if row.revoked_at:
        return None, 'This key has been revoked.'
    return row, None


def scopes_of(row):
    try:
        return set(json.loads(row.scopes or '[]'))
    except ValueError:
        return set()


def touch(row, ip):
    """Note when a key was last used, at most once a minute (not on every call)."""
    now = _now()
    if not row.last_used_at or row.last_used_at < (now - timedelta(minutes=1)).isoformat():
        row.last_used_at, row.last_used_ip = now.isoformat(), (ip or '')[:64]
        db.session.commit()
