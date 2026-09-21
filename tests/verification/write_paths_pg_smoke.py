"""Every admin, student, parent and candidate page, opened against real PostgreSQL.

The application was written for SQLite and moved to PostgreSQL later, and PostgreSQL
is stricter: it type-checks a query even when the table is empty, wants every selected
column in a GROUP BY to be grouped or aggregated, is case-sensitive where SQLite was
not, and has no ``date('now')``. A page that builds a query SQLite accepted and
PostgreSQL rejects fails with a 500 the first time anyone opens it.

So this signs in to a fresh school as its operator and opens every GET page under
/admin, in the workspace that page belongs to: first the argument-free ones over empty
tables, then, with one row put in every table, those pages again and every page that
takes an id, and finally the student, parent and candidate portals as each kind of account.
It fails on any *database* error. It cannot prove a page is correct, only
that its queries are valid PostgreSQL, which is exactly the class of bug the move
introduced. (A page that errors for another reason on the placeholder rows, such as a
template formatting a number that the placeholder left empty, is reported but is not a
database problem, so it is not failed here.)

Run:  python tests/verification/write_paths_pg_smoke.py
"""
import logging
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_smoke_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('smoke')
os.environ.update({
    "BRIGHTSTARS_TENANTS_DIR": os.path.join(TMP, "tenants"),
    "BRIGHTSTARS_PLATFORM_HOSTS": "platform.test",
    "BRIGHTSTARS_PORTAL_DOMAIN": "portal.test",
    "BRIGHTSTARS_REGISTRY_CACHE_SECONDS": "0",
    "BRIGHTSTARS_SECRET": "x" * 40,
})
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
os.chdir(ROOT)

import sqlalchemy as sa  # noqa: E402

import app as A  # noqa: E402
from control_plane import provisioning as pv  # noqa: E402
from control_plane.context import tenant_context  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import dispose_engines  # noqa: E402

results = []
PL = "http://platform.test"
SCHOOL = "http://smoke.portal.test"

# Pages that end the session or act on a plain GET, so they are not "just a page".
SKIP = ("logout", "login", "enter", "download", "delete", "export", "password", "presence", "heartbeat",
        "notification_open", "notification_read", "workspace")


class Errors(logging.Handler):
    """Remembers the exceptions the application logs while it answers a request."""

    def __init__(self):
        super().__init__()
        self.seen = []

    def emit(self, record):
        if record.exc_info and record.exc_info[1] is not None:
            self.seen.append(record.exc_info[1])


errors = Errors()
A.app.logger.addHandler(errors)


def is_database_error(exc):
    while exc is not None:
        if isinstance(exc, sa.exc.SQLAlchemyError):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


def csrf(c, path, base):
    body = c.get(path, base_url=base).get_data(as_text=True)
    marker = 'name="_csrf_token" value="'
    start = body.index(marker) + len(marker)
    return body[start:body.index('"', start)]


pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
console = A.app.test_client()
console.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                      "_csrf_token": csrf(console, "/platform/login", PL)}, base_url=PL)
console.post("/platform/schools/new", data={
    "_csrf_token": csrf(console, "/platform/schools/new", PL), "name": "Smoke School", "code": "smoke",
    "school_brand_primary": "#0d2b52", "school_brand_accent": "#1674b9"}, base_url=PL,
    content_type="multipart/form-data")
r = console.post("/platform/schools/smoke/enter", data={"_csrf_token": csrf(console, "/platform/schools/smoke", PL)},
                 base_url=PL)
school = A.app.test_client()
school.get(r.headers["Location"][len(SCHOOL):], base_url=SCHOOL)
check("the operator is signed in to a fresh school", school.get("/admin/home", base_url=SCHOOL).status_code == 200)


def routes(with_arguments, area="/admin"):
    """The GET paths worth opening in one area, with any argument filled in. ``area`` "portal"
    means everything a student, parent or candidate can reach: not admin, platform or static."""
    found = set()
    for rule in A.app.url_map.iter_rules():
        if "GET" not in rule.methods or bool(rule.arguments) != with_arguments:
            continue
        if area == "portal":
            if rule.rule.startswith(("/admin", "/platform", "/static")) or rule.rule in ("/", "/health"):
                continue
        elif not rule.rule.startswith(area):
            continue
        if any(word in rule.endpoint or word in rule.rule for word in SKIP):
            continue
        path = re.sub(r"<(?:int|float):[^>]+>", "1", rule.rule)
        found.add(re.sub(r"<(?:path:|string:)?[^>]+>", "x", path))
    return sorted(found)


def crawl(paths, who=None):
    """Open each page; return (database failures, other failures) as (path, detail) lists."""
    database, other = [], []
    for path in paths:
        if who is None:
            school.get(f"/admin/workspace/{A._admin_workspace_for_path(path) or 'school'}", base_url=SCHOOL)
        errors.seen.clear()
        try:
            status = (who or school).get(path, base_url=SCHOOL).status_code
        except Exception as exc:  # an exception that escaped the application's own error handler
            errors.seen.append(exc)
            status = 500
        if status >= 500:
            problem = next((e for e in errors.seen if is_database_error(e)), None)
            if problem is not None:
                database.append((path, str(problem).splitlines()[0][:160]))
            else:
                other.append((path, type(errors.seen[-1]).__name__ if errors.seen else "500"))
    return database, other


def report(label, paths, database, other):
    check(f"{len(paths)} {label} were opened", len(paths) > 30, str(len(paths)))
    check(f"…and none of them fails on a database error", not database,
          "; ".join(f"{p}: {m}" for p, m in database))
    if other:
        print(f"NOTE {len(other)} page(s) failed for a non-database reason on the placeholder rows: "
              + ", ".join(f"{p} ({t})" for p, t in other))


plain = routes(False)
report("argument-free admin pages over empty tables", plain, *crawl(plain))

# ------------------------------------------------ the same, with one row in every table
# Empty tables hide a lot: most failures need a row to join. So put one plausible row in every
# table (id 1, pointing at the other tables' id 1).
with platform_session() as s_:
    info = to_info(get_tenant(s_, "smoke"))


def placeholder(column):
    kind = column.type.__class__.__name__.lower()
    if "int" in kind:
        return 1
    if kind in ("float", "numeric"):
        return 1.0
    if "bool" in kind:
        return 1
    return "2026-01-01" if "date" in column.name or column.name.endswith("_at") else "x"


seeded, unseeded = 0, []
with A.app.app_context(), tenant_context(info):
    engine = A.db.session.get_bind()
    for table in A.db.metadata.sorted_tables:
        row = {}
        for column in table.columns:
            if column.foreign_keys or column.primary_key:
                row[column.name] = 1
            elif not column.nullable and column.default is None and column.server_default is None:
                row[column.name] = placeholder(column)
        try:
            with engine.begin() as conn:
                if conn.execute(sa.select(sa.func.count()).select_from(table)).scalar():
                    continue
                conn.execute(table.insert().values(**row))
            seeded += 1
        except Exception:
            unseeded.append(table.name)
    with engine.begin() as conn:  # keep each sequence ahead of the ids just inserted
        for table in A.db.metadata.sorted_tables:
            for column in table.primary_key.columns:
                if "int" not in column.type.__class__.__name__.lower():
                    continue
                sequence = conn.execute(sa.text("SELECT pg_get_serial_sequence(:t, :c)"),
                                        {"t": table.name, "c": column.name}).scalar()
                if sequence:
                    conn.execute(sa.text(f'SELECT setval(:s, (SELECT COALESCE(MAX("{column.name}"), 1) '
                                         f'FROM "{table.name}"))'), {"s": sequence})
check(f"a row was put in {seeded} tables to open pages against", seeded > 40, f"{seeded}; skipped: {unseeded}")

report("argument-free admin pages again, now with data", plain, *crawl(plain))
with_ids = routes(True)
report("admin pages that take an id", with_ids, *crawl(with_ids))

# ------------------------------------------------ the student, parent and candidate portals
def signed_in_as(kind):
    """A client holding a session for account id 1 of this kind, bound to the school."""
    c = A.app.test_client()
    with c.session_transaction(base_url=SCHOOL) as sess:
        sess["tenant_id"] = info.id
        sess[f"{kind}_id"] = 1
    return c


portal_paths = routes(False, "portal") + routes(True, "portal")
database, other = [], []
for kind in ("student", "parent", "candidate"):
    d, o = crawl(portal_paths, signed_in_as(kind))
    database += [(f"{kind}: {p}", m) for p, m in d]
    other += [(f"{kind}: {p}", t) for p, t in o]
check(f"{len(portal_paths)} student, parent and candidate pages were opened as each kind of account",
      len(portal_paths) > 15, str(len(portal_paths)))
check("…and none of them fails on a database error", not database, "; ".join(f"{p}: {m}" for p, m in database))
if other:
    print(f"NOTE {len(other)} portal page(s) failed for a non-database reason on the placeholder rows: "
          + ", ".join(f"{p} ({t})" for p, t in other))

dispose_engines()
DROP_TEST_DATABASES()
shutil.rmtree(TMP, ignore_errors=True)
print()
bad = [x for x in results if not x[1]]
print(f"{len(results) - len(bad)}/{len(results)} checks passed")
sys.exit(1 if bad else 0)
