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


def test_the_admin_row_is_loaded_once_per_request_and_kept_between_requests():
    loads = []
    original_load, original_snapshot = security._load_active_admin, security._snapshot
    security._load_active_admin = lambda admin_id: loads.append(admin_id) or {'id': admin_id}
    security._snapshot = lambda admin: admin  # the stand-in row is already a plain dictionary
    security._ADMIN_CACHE.clear()
    try:
        app = _app()
        for _ in range(3):
            with app.test_request_context('/'):
                session['admin_id'] = 7
                for _ in range(4):
                    assert security.current_admin() == {'id': 7}
                    assert security._active_admin(7) == {'id': 7}
        assert loads == [7], loads
    finally:
        security._load_active_admin, security._snapshot = original_load, original_snapshot
        security._ADMIN_CACHE.clear()


def test_the_admin_row_is_kept_separately_for_each_school():
    from control_plane.context import reset_current_tenant, set_current_tenant
    loads = []
    original_load, original_snapshot = security._load_active_admin, security._snapshot
    security._load_active_admin = lambda admin_id: loads.append(admin_id) or {'id': admin_id}
    security._snapshot = lambda admin: admin
    security._ADMIN_CACHE.clear()
    try:
        for school in (SimpleNamespace(id=1), SimpleNamespace(id=2)):
            token = set_current_tenant(school)
            try:
                assert security._cached_active_admin(7) == {'id': 7}
                assert security._cached_active_admin(7) == {'id': 7}
            finally:
                reset_current_tenant(token)
        assert loads == [7, 7], loads
    finally:
        security._load_active_admin, security._snapshot = original_load, original_snapshot
        security._ADMIN_CACHE.clear()


def test_an_admin_row_is_not_kept_when_there_is_no_such_account():
    original_load = security._load_active_admin
    calls = []
    security._load_active_admin = lambda admin_id: calls.append(admin_id)
    security._ADMIN_CACHE.clear()
    try:
        assert security._cached_active_admin(9) is None
        assert security._cached_active_admin(9) is None
        assert calls == [9, 9], calls
    finally:
        security._load_active_admin = original_load
        security._ADMIN_CACHE.clear()


def test_a_badge_count_is_kept_for_its_lifetime_and_computed_every_time_at_zero():
    from core import short_cache
    counts = []
    compute = lambda who: counts.append(who) or len(counts)
    short_cache.clear()
    assert short_cache.remember('badge-test', 30, compute, 1) == short_cache.remember('badge-test', 30, compute, 1)
    assert counts == [1], counts
    short_cache.remember('badge-test', 0, compute, 1)
    short_cache.remember('badge-test', 0, compute, 1)
    assert counts == [1, 1, 1], counts
    short_cache.clear()


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


def test_an_administrators_permissions_are_read_once_and_read_again_after_a_school_write():
    from core import short_cache
    reads = []
    original_codes, original_active = security.admin_permission_codes, security._active_admin
    security.admin_permission_codes = lambda admin_id: reads.append(admin_id) or {'students.view'}
    security._active_admin = lambda admin_id: security._AdminSnapshot(
        {'id': 7, 'admin_type_id': 1, 'admin_type_system': 0})
    short_cache.clear()
    try:
        for _ in range(3):
            assert security.admin_has_permission(7, 'students.view') is True
            assert security.admin_has_permission(7, 'finance.manage') is False
        assert reads == [7], reads
        security.clear_school_admin_caches()
        assert security.admin_has_permission(7, 'students.view') is True
        assert reads == [7, 7], reads
    finally:
        security.admin_permission_codes, security._active_admin = original_codes, original_active
        short_cache.clear()
