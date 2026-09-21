"""Tables left behind by the removed website editor.

A school database made before the editor was removed still holds ``school_public_pages``,
``school_public_news`` and ``school_public_enquiries``. This checks that an upgrade drops the
ones that are empty (nothing can be lost), leaves alone any that still hold rows, and that
``python -m control_plane drop-retired-tables`` shows what is left and only drops it with --yes.

Run:  python tests/verification/write_paths_retired_tables.py
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_retired_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('retired')
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
from control_plane import cli, provisioning as pv  # noqa: E402
from control_plane.models import PlatformAuditLog  # noqa: E402
from control_plane.registry import platform_session  # noqa: E402
from control_plane.routing import dispose_engines, engine_for  # noqa: E402
from core.retired_tables import RETIRED_TABLES  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


def sql(info, statement, **params):
    with engine_for(info).begin() as conn:
        result = conn.execute(sa.text(statement), params)
        return result.fetchall() if result.returns_rows else None


def present(info):
    return sorted(r[0] for r in sql(info, "SELECT table_name FROM information_schema.tables "
                                          "WHERE table_schema = current_schema() AND table_name = ANY(:names)",
                                    names=list(RETIRED_TABLES)))


def make_legacy_tables(info, rows_in=()):
    """The three tables as an old school database had them, with a row in the named ones."""
    for table in RETIRED_TABLES:
        sql(info, f'CREATE TABLE IF NOT EXISTS "{table}" (id SERIAL PRIMARY KEY, title TEXT)')
    for table in rows_in:
        sql(info, f'INSERT INTO "{table}" (title) VALUES (\'kept\')')


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue() + err.getvalue()


pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
alpha, _ = pv.create_tenant("alpha", "Alpha School")
beta, _ = pv.create_tenant("beta", "Beta College")
gamma, _ = pv.create_tenant("gamma", "Gamma Prep")

check("a school made today never has the retired tables", present(alpha) == [] and present(beta) == [])

# an old school: all three tables, empty; another: one of them still holds a post
make_legacy_tables(alpha)
make_legacy_tables(beta, rows_in=("school_public_news",))
check("(set-up) the old databases have the three tables", present(alpha) == sorted(RETIRED_TABLES) == present(beta))

before_tables = sql(alpha, "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = current_schema()")[0][0]
pv.upgrade_tenant(alpha)
pv.upgrade_tenant(beta)
check("an upgrade drops the retired tables that are empty", present(alpha) == [], str(present(alpha)))
check("…but never a table that still holds rows",
      present(beta) == ["school_public_news"] and sql(beta, "SELECT title FROM school_public_news") == [("kept",)],
      str(present(beta)))
after_tables = sql(alpha, "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = current_schema()")[0][0]
check("…and touches no other table", before_tables - after_tables == len(RETIRED_TABLES), f"{before_tables} -> {after_tables}")
pv.upgrade_tenant(alpha)
check("upgrading again is harmless", present(alpha) == [])

# ---------------------------------------------------------------- the command
code, text = run_cli("drop-retired-tables")
check("the command shows what each school still holds, and changes nothing without --yes",
      code == 0 and "school_public_news: 1 row(s)" in text and "Nothing was changed" in text
      and present(beta) == ["school_public_news"], text)
check("…and says plainly when a school has nothing left over",
      "alpha" in text and "nothing left over" in text and "gamma" in text, text)
check("…and does not name a school that has nothing", "school_public_pages" not in text, text)

code, text = run_cli("drop-retired-tables", "nosuchschool")
check("an unknown school is refused", code == 1 and "No school" in text, text)

code, text = run_cli("drop-retired-tables", "gamma", "--yes")
check("with --yes and a school that has nothing, nothing happens", code == 0 and present(beta) == ["school_public_news"], text)

code, text = run_cli("drop-retired-tables", "beta", "--yes")
check("with --yes the leftover table is dropped for good", code == 0 and present(beta) == [] and "dropped" in text, text)
with platform_session() as session:
    logged = session.scalars(sa.select(PlatformAuditLog.action)
                             .where(PlatformAuditLog.action == 'tenant.retired_tables_dropped')).all()
check("…and the platform's log records it", len(logged) == 1, str(logged))
check("…and the school still works afterwards",
      sql(beta, "SELECT COUNT(*) FROM schools")[0][0] == 1)

dispose_engines()
DROP_TEST_DATABASES()
shutil.rmtree(TMP, ignore_errors=True)
print()
failed = [x for x in results if not x[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
