"""Platform settings that every server shares.

The Settings page used to write the server's own ``.env`` file, so with the application running on
several servers an edit reached only the one that handled the request. Two kinds of setting are
therefore kept apart:

* **Shared** settings (mail and WhatsApp accounts, cache lengths, upload limits, addresses...) are
  stored once, in the platform database every server already reads. Each server copies them into its
  own environment at the start of a request, at most every ``SYNC_SECONDS``, so one edit reaches every
  server within seconds and a restarted or newly added server picks them up with no file to copy.
  A secret (an SMTP password, a token) is encrypted at rest.
* **Per-server** settings stay in that server's environment, because they describe the server itself
  or have to be known before the database can be reached: the database URL, the signing secret, the
  delivery key, file paths, ports, the proxy layout, pool sizes. Anything that only takes effect on
  a restart is per-server too. The page says so and shows which server it is talking to.

A value set in the shared store beats ``.env``; it replaces what the server started with while it
exists, and removing it puts back what the server started with.
"""

import base64
import os
import socket
import threading
import time

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from flask import current_app
import sqlalchemy as sa

from . import settings_registry
from .models import PlatformState
from .registry import now_iso, platform_session

PREFIX = 'env:'
SYNC_SECONDS = 10
_ENCRYPTED = 'enc:v1:'

# Describe this server, or are needed before the registry can be reached: never shared.
SERVER_ONLY = frozenset({
    'BRIGHTSTARS_PLATFORM_DB', 'BRIGHTSTARS_SECRET', 'BRIGHTSTARS_DELIVERY_KEY', 'BRIGHTSTARS_ENV',
    'BRIGHTSTARS_TENANTS_DIR', 'BRIGHTSTARS_TRUSTED_PROXIES', 'PORT', 'BRIGHTSTARS_CHROME',
    'BRIGHTSTARS_PG_DUMP', 'BRIGHTSTARS_DB_POOL_SIZE', 'BRIGHTSTARS_DB_MAX_OVERFLOW',
    'BRIGHTSTARS_REGISTRY_POOL_SIZE', 'BRIGHTSTARS_REGISTRY_MAX_OVERFLOW',
})

_lock = threading.Lock()
_next_sync = 0.0
_originals = {}   # name -> what this server started with (None = not set), for every name we replaced


def is_shared(variable):
    return variable.name not in SERVER_ONLY and variable.effect == settings_registry.EFFECT_IMMEDIATE


def server_name():
    return os.environ.get('BRIGHTSTARS_SERVER_NAME', '').strip() or socket.gethostname()


# ---------------------------------------------------------------- secrets at rest

def _cipher():
    material = os.environ.get('BRIGHTSTARS_DELIVERY_KEY', '').strip() or current_app.secret_key
    if isinstance(material, str):
        material = material.encode()
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=b'brightstars-platform-settings-v1').derive(material)
    return Fernet(base64.urlsafe_b64encode(key))


def _seal(variable, value):
    return _ENCRYPTED + _cipher().encrypt(value.encode()).decode() if variable.secret else value


def _open(variable, stored):
    if not variable.secret:
        return stored
    if not stored.startswith(_ENCRYPTED):
        return ''
    try:
        return _cipher().decrypt(stored[len(_ENCRYPTED):].encode()).decode()
    except (InvalidToken, ValueError):
        return ''


# ---------------------------------------------------------------- reading and writing

def _stored():
    with platform_session() as session:
        rows = session.execute(sa.select(PlatformState.key, PlatformState.value)
                               .where(PlatformState.key.like(PREFIX + '%'))).all()
    return {key[len(PREFIX):]: value for key, value in rows}


def shared_values():
    """``{name: value}`` of every shared setting that has been saved, secrets decrypted."""
    out = {}
    for name, stored in _stored().items():
        variable = settings_registry.get(name)
        if variable is not None and is_shared(variable):
            out[name] = _open(variable, stored)
    return out


def save(variable, value):
    """Store ``value`` for every server (a blank value clears the setting) and apply it here now."""
    if not is_shared(variable):
        raise ValueError(f'{variable.name} is a per-server setting.')
    key = PREFIX + variable.name
    with platform_session() as session:
        row = session.get(PlatformState, key)
        if not value:
            if row is not None:
                session.delete(row)
        elif row is None:
            session.add(PlatformState(key=key, value=_seal(variable, value), updated_at=now_iso()))
        else:
            row.value = _seal(variable, value)
            row.updated_at = now_iso()
        session.commit()
    sync(force=True)


def sync(force=False):
    """Copy the shared settings into this server's environment. Cheap to call on every request: it
    reads the database at most once every ``SYNC_SECONDS`` and never raises."""
    global _next_sync
    now = time.monotonic()
    if not force and now < _next_sync:
        return
    with _lock:
        if not force and time.monotonic() < _next_sync:
            return
        _next_sync = time.monotonic() + SYNC_SECONDS
        try:
            wanted = shared_values()
        except Exception:
            current_app.logger.warning('Could not read the shared platform settings; keeping the last ones.',
                                       exc_info=True)
            return
        for name, value in wanted.items():
            if name not in _originals:
                _originals[name] = os.environ.get(name)
            os.environ[name] = value
        for name in [n for n in _originals if n not in wanted]:
            original = _originals.pop(name)
            if original is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = original


def install(app):
    """Run ``sync`` at the start of requests. Registered after the other hooks so the platform
    registry is already available."""
    @app.before_request
    def _sync_shared_settings():
        sync()
