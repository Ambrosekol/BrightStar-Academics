"""Contract: a per-school engine's connection pool defaults small; the one shared registry engine
defaults larger.

One engine (and its own pool) is cached per school (control_plane/routing.py's engine_for), so
SQLAlchemy's own single-app defaults - a pool of 5, up to 10 more on top - would let a hundred
schools open a thousand-plus idle connections per worker process. The registry, by contrast, is one
engine asked on nearly every request regardless of which school it is for, so it needs the opposite
answer. See recommendations.html's Scale table.

Building an Engine object never opens a connection, so this needs no database.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from control_plane.routing import build_engine  # noqa: E402


def test_a_school_s_own_pool_defaults_small():
    for var in ("BRIGHTSTARS_DB_POOL_SIZE", "BRIGHTSTARS_DB_MAX_OVERFLOW"):
        os.environ.pop(var, None)
    engine = build_engine("postgresql://user:pass@localhost/does_not_need_to_exist")
    try:
        assert engine.pool.size() <= 2, engine.pool.size()
        assert engine.pool._max_overflow <= 3, engine.pool._max_overflow
    finally:
        engine.dispose()


def test_a_school_s_pool_is_tunable_by_environment_variable():
    os.environ["BRIGHTSTARS_DB_POOL_SIZE"] = "7"
    os.environ["BRIGHTSTARS_DB_MAX_OVERFLOW"] = "11"
    try:
        engine = build_engine("postgresql://user:pass@localhost/does_not_need_to_exist")
        try:
            assert engine.pool.size() == 7
            assert engine.pool._max_overflow == 11
        finally:
            engine.dispose()
    finally:
        os.environ.pop("BRIGHTSTARS_DB_POOL_SIZE", None)
        os.environ.pop("BRIGHTSTARS_DB_MAX_OVERFLOW", None)


def test_an_explicit_pool_size_overrides_the_environment_default():
    """control_plane/registry.py's platform_engine() uses this to give the one shared registry
    engine its own, much larger, numbers - regardless of what a school's own env vars say."""
    engine = build_engine("postgresql://user:pass@localhost/does_not_need_to_exist",
                          pool_size=10, max_overflow=20)
    try:
        assert engine.pool.size() == 10
        assert engine.pool._max_overflow == 20
    finally:
        engine.dispose()


def test_the_registry_engine_asks_for_the_larger_numbers():
    source = (ROOT / "control_plane" / "registry.py").read_text(encoding="utf-8")
    start = source.index("def platform_engine")
    end = source.index("\ndef ", start + 1)
    body = source[start:end]
    assert "BRIGHTSTARS_REGISTRY_POOL_SIZE" in body
    assert "BRIGHTSTARS_REGISTRY_MAX_OVERFLOW" in body


# ---------------------------------------------------------------- behind PgBouncer
URL = "postgresql+psycopg://user:pass@localhost/does_not_need_to_exist"


def _connect_params(engine):
    """What the engine would hand the driver when connecting, caught before any connection is made."""
    import sqlalchemy as sa
    seen = {}

    class Caught(Exception):
        pass

    @sa.event.listens_for(engine, "do_connect")
    def catch(dialect, record, cargs, cparams):
        seen.update(cparams)
        raise Caught()
    try:
        engine.connect()
    except Caught:
        pass
    return seen


def _refused(exc_type, phrase, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        assert phrase in str(exc), str(exc)
        return
    raise AssertionError(f"{fn.__name__} was not refused")


def _with_pooler(value, fn):
    old = os.environ.get("BRIGHTSTARS_DB_POOLER")
    os.environ["BRIGHTSTARS_DB_POOLER"] = value
    try:
        return fn()
    finally:
        if old is None:
            os.environ.pop("BRIGHTSTARS_DB_POOLER", None)
        else:
            os.environ["BRIGHTSTARS_DB_POOLER"] = old


def test_behind_pgbouncer_a_school_keeps_no_pool_and_no_prepared_statements():
    import sqlalchemy as sa

    def check():
        for engine in (build_engine(URL), build_engine(URL, pool_size=10, max_overflow=20, connect_timeout=5)):
            try:
                assert isinstance(engine.pool, sa.pool.NullPool), type(engine.pool)
                args = _connect_params(engine)
                assert "prepare_threshold" in args and args["prepare_threshold"] is None, args
            finally:
                engine.dispose()
        registry = build_engine(URL, connect_timeout=5)
        try:
            assert _connect_params(registry).get("connect_timeout") == 5
        finally:
            registry.dispose()
    _with_pooler("pgbouncer", check)


def test_behind_pgbouncer_a_school_kept_as_a_schema_is_refused():
    """Its search_path is a start-up option that transaction pooling would not carry over."""
    def check():
        _refused(ValueError, "database per school", build_engine, URL, db_schema="school_alpha")
    _with_pooler("pgbouncer", check)


def test_an_unknown_pooler_is_an_error_not_a_silent_default():
    from control_plane import config

    def check():
        _refused(RuntimeError, "BRIGHTSTARS_DB_POOLER", config.db_pooler)
    _with_pooler("pgbouncr", check)
    assert _with_pooler("", config.db_pooler) == ""


def test_without_a_pooler_a_school_keeps_its_small_pool():
    engine = _with_pooler("", lambda: build_engine(URL))
    try:
        assert engine.pool.size() <= 2
    finally:
        engine.dispose()


# ---------------------------------------------------------------- closing a school's connections
def test_idle_school_connections_close_after_a_minute_by_default():
    from control_plane import config
    os.environ.pop("BRIGHTSTARS_SCHOOL_ENGINE_IDLE_SECONDS", None)
    assert config.school_engine_idle_seconds() == 60


def test_release_engine_closes_one_school_and_the_next_use_builds_it_again():
    from types import SimpleNamespace

    from control_plane import routing
    school = SimpleNamespace(db_url=URL.replace("does_not_need_to_exist", "release_me"), db_schema=None)
    other = SimpleNamespace(db_url=URL.replace("does_not_need_to_exist", "keep_me"), db_schema=None)
    first, kept = routing.engine_for(school), routing.engine_for(other)
    try:
        routing.release_engine(school)
        assert (school.db_url, None) not in routing._engines
        assert routing._engines.get((other.db_url, None)) is kept
        again = routing.engine_for(school)
        assert again is not first
    finally:
        routing.release_engine(school)
        routing.release_engine(other)


def test_provisioning_and_upgrading_every_school_release_each_school_when_done():
    """A run through hundreds of schools (provisioning them, or the deploy-time upgrade) must not
    hold a connection to each of them until each goes quiet: that is what exhausted max_connections
    at about a hundred schools."""
    source = (ROOT / "control_plane" / "provisioning.py").read_text(encoding="utf-8")
    start = source.index("def upgrade_all_tenants")
    body = source[start:source.index("\ndef ", start + 1)]
    assert "release_engine(info)" in body and "finally:" in body
    start = source.index("def create_tenant")
    body = source[start:source.index("\ndef ", start + 1)]
    assert body.count("release_engine(info)") >= 2       # when it is done, and when it failed


def test_exporting_a_school_goes_to_postgresql_itself_not_through_pgbouncer():
    """pg_dump sets things for its whole session, which transaction pooling does not keep together."""
    source = (ROOT / "control_plane" / "provisioning.py").read_text(encoding="utf-8")
    start = source.index("def export_tenant_data")
    body = source[start:source.index("\ndef ", start + 1)]
    assert "config.db_direct_port() if config.db_pooler()" in body
    from control_plane import config
    os.environ.pop("BRIGHTSTARS_DB_DIRECT_PORT", None)
    assert config.db_direct_port() == 5432
