"""Database connections with many schools, proven on PostgreSQL. In plain words:

A. Creating a school leaves none of its connections open, and neither does upgrading every school (the
   deploy step): going through hundreds of schools holds one school's connection at a time, so it never
   runs into PostgreSQL's max_connections the way it did at about a hundred schools.
B. A school that is used opens its connections again on its own.
C. move-db-address (for putting PgBouncer in front of a server that already has schools): without
   --apply nothing changes; with it, every school on the old address moves and keeps its own database
   name and credentials; a school whose address is an env: reference, or on another server, is left alone.
Run:  python tests/verification/write_paths_db_connections.py
"""
import atexit
import os
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="beacon_dbconn_")

import _pg  # noqa: E402

PREFIX, DROP_TEST_DATABASES = _pg.setup('dbconn')
atexit.register(DROP_TEST_DATABASES)
atexit.register(shutil.rmtree, TMP, True)
os.environ.update({
    "BEACON_TENANTS_DIR": os.path.join(TMP, "tenants"),
    "BEACON_PLATFORM_HOSTS": "platform.test",
    "BEACON_PORTAL_DOMAIN": "portal.test",
    "BEACON_REGISTRY_CACHE_SECONDS": "0",
    "BEACON_SECRET": "x" * 40,
})
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import sqlalchemy as sa  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

import app as A  # noqa: E402,F401
from control_plane import provisioning as pv  # noqa: E402
from control_plane import routing  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)


ADMIN = _pg._admin_engine(make_url(os.environ["BEACON_PLATFORM_DB"]))


def open_to_schools():
    """Connections open right now to this run's school databases (not the registry)."""
    with ADMIN.connect() as conn:
        return conn.execute(sa.text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname LIKE :p AND datname NOT LIKE :r"),
            {"p": PREFIX + "%", "r": PREFIX + "_platform"}).scalar()


def cached():
    return [key for key in routing._engines if PREFIX in (key[0] or "")]


# ================================================================ A. nothing left open
SLUGS = [f"conn{i}" for i in range(6)]
infos = [pv.create_tenant(slug, f"School {slug}", starter_banks=False, actor="test")[0] for slug in SLUGS]
check("six schools were created", len(infos) == 6)
check("creating them left none of their connections open", open_to_schools() == 0 and not cached(),
      f"{open_to_schools()} open, {len(cached())} cached")
pv.upgrade_all_tenants()
check("upgrading every school (the deploy step) left none open either", open_to_schools() == 0 and not cached(),
      f"{open_to_schools()} open, {len(cached())} cached")

# ================================================================ B. used again, it reconnects
with routing.engine_for(infos[0]).connect() as conn:
    name = conn.execute(sa.text("SELECT name FROM schools ORDER BY id LIMIT 1")).scalar()
check("a school used afterwards connects again on its own", name == "School conn0", str(name))
routing.release_engine(infos[0])
check("…and releasing it closes that connection", open_to_schools() == 0, str(open_to_schools()))

# ================================================================ C. move-db-address
url0 = make_url(infos[0].db_url)
HOST, PORT = url0.host or "localhost", url0.port or 5432
pv.set_db_url("conn4", "env:SOME_SCHOOL_DB")
pv.set_db_url("conn5", url0.set(host="db2.example.test", database=make_url(infos[5].db_url).database)
              .render_as_string(hide_password=False))


def stored(slug):
    with platform_session() as s:
        return get_tenant(s, slug).db_url


before = {slug: stored(slug) for slug in SLUGS}
moved, skipped = pv.move_db_address(HOST, PORT, HOST, 6432)
check("without --apply it only says what would move", sorted(s for s, _ in moved) == SLUGS[:4]
      and {slug: stored(slug) for slug in SLUGS} == before, str(moved))
check("…and lists the env: school and the one on another server as left alone",
      {s for s, _ in skipped} == {"conn4", "conn5"}, str(skipped))
pv.move_db_address(HOST, PORT, HOST, 6432, apply=True)
after = {slug: make_url(stored(slug)) for slug in SLUGS[:4]}
check("with --apply each school on the old address now uses the new port",
      all((u.host, u.port) == (HOST, 6432) for u in after.values()), str({s: (u.host, u.port) for s, u in after.items()}))
check("…keeping its own database name, user and password",
      all(after[s].database == make_url(before[s]).database and after[s].username == make_url(before[s]).username
          and after[s].password == make_url(before[s]).password for s in after))
check("…and leaving the other two exactly as they were", stored("conn4") == before["conn4"] and stored("conn5") == before["conn5"])
with platform_session() as s:
    logged = s.execute(sa.text("SELECT count(*) FROM platform_audit_log WHERE action = 'tenant.db_url_changed' "
                               "AND detail LIKE 'moved to %'")).scalar()
check("each move is in the platform's audit log", logged == 4, str(logged))
pv.move_db_address(HOST, 6432, HOST, PORT, apply=True)          # back, so the teardown reaches them
check("moving them back works the same way", all(make_url(stored(s)).port == PORT for s in SLUGS[:4]))

ADMIN.dispose()
failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
