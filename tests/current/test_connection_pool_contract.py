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
