"""Contract: the per-request and per-process caches that keep a growing number of schools cheap.

Each check uses only stand-ins and monkeypatching, so it needs no database server. Engines are
built but never connect (control_plane/routing.py's build_engine opens nothing).
"""
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask, session  # noqa: E402

import core.presence as presence  # noqa: E402
import core.public_settings as public_settings  # noqa: E402
import core.security as security  # noqa: E402
from control_plane import config, routing  # noqa: E402


def _app():
    app = Flask(__name__)
    app.secret_key = 'test-only'
    return app


def _tenant(slug):
    return SimpleNamespace(db_url=f'postgresql://u:p@localhost/{slug}', db_schema=None)


def test_a_quiet_school_engine_is_closed_and_rebuilt_on_next_use():
    os.environ['BRIGHTSTARS_SCHOOL_ENGINE_IDLE_SECONDS'] = '60'
    routing.dispose_engines()
    try:
        quiet, busy = _tenant('quiet'), _tenant('busy')
        now = time.monotonic()
        first = routing.engine_for(quiet)
        routing.engine_for(busy)
        # Pretend the quiet school was last used ten minutes ago, the busy one just now.
        routing._engine_last_used[(quiet.db_url, quiet.db_schema)] = now - 600
        routing._engine_last_used[(busy.db_url, busy.db_schema)] = now
        routing.evict_idle_engines(now)
        assert (quiet.db_url, quiet.db_schema) not in routing._engines
        assert (busy.db_url, busy.db_schema) in routing._engines
        rebuilt = routing.engine_for(quiet)
        assert rebuilt is not first
    finally:
        os.environ.pop('BRIGHTSTARS_SCHOOL_ENGINE_IDLE_SECONDS', None)
        routing.dispose_engines()


def test_idle_engine_eviction_can_be_turned_off():
    os.environ['BRIGHTSTARS_SCHOOL_ENGINE_IDLE_SECONDS'] = '0'
    routing.dispose_engines()
    try:
        tenant = _tenant('kept')
        engine = routing.engine_for(tenant)
        routing._engine_last_used[(tenant.db_url, tenant.db_schema)] = time.monotonic() - 10 ** 6
        routing.evict_idle_engines()
        assert routing._engines.get((tenant.db_url, tenant.db_schema)) is engine
    finally:
        os.environ.pop('BRIGHTSTARS_SCHOOL_ENGINE_IDLE_SECONDS', None)
        routing.dispose_engines()


def test_a_signed_in_request_writes_presence_once_per_interval():
    calls = []
    original = presence.touch_presence
    presence.touch_presence = lambda: calls.append(1)
    presence._last_request_touch.clear()
    try:
        app = _app()
        with app.test_request_context('/'):
            session['_presence_token'] = 'token-a'
            presence.touch_presence_if_due()
            presence.touch_presence_if_due()
            presence.touch_presence_if_due()
        assert len(calls) == 1, calls
        # Once the interval has passed, the next request writes again.
        presence._last_request_touch['token-a'] = time.monotonic() - presence.PRESENCE_REQUEST_INTERVAL_SECONDS - 1
        with app.test_request_context('/'):
            session['_presence_token'] = 'token-a'
            presence.touch_presence_if_due()
        assert len(calls) == 2, calls
    finally:
        presence.touch_presence = original
        presence._last_request_touch.clear()


def test_the_admin_row_is_loaded_once_per_request():
    loads = []
    original = security._load_active_admin
    security._load_active_admin = lambda admin_id: loads.append(admin_id) or {'id': admin_id}
    try:
        app = _app()
        with app.test_request_context('/'):
            session['admin_id'] = 7
            for _ in range(4):
                assert security.current_admin() == {'id': 7}
                assert security._active_admin(7) == {'id': 7}
        assert loads == [7], loads
    finally:
        security._load_active_admin = original


def test_public_settings_are_read_once_per_request_and_refreshed_after_a_write():
    reads = []
    original = public_settings.tuples
    public_settings.tuples = lambda query: reads.append(1) or [('school_name', 'Alpha')]
    try:
        app = _app()
        with app.test_request_context('/'):
            assert public_settings._public_settings() == {'school_name': 'Alpha'}
            public_settings._public_settings()
            assert len(reads) == 1, reads
            public_settings.forget_public_settings()
            public_settings._public_settings()
            assert len(reads) == 2, reads
    finally:
        public_settings.tuples = original
