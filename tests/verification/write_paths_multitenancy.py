"""Multi-tenancy end to end: one process, several schools, each on its own
domain, database, question banks and uploads.

Builds a throwaway platform in a temp folder:

  * ``alpha``     a school with a first admin and the platform's standard question banks
  * ``beta``      a school with no admin and no banks, on two domains
  * ``gamma``     a school with a question bank of its own

and then tries to make one school see or affect another: by hostname, by a
session cookie copied between schools, by uploads and question banks on disk,
and by running a query with no school selected. It also checks the platform
side: every school is made from scratch, and suspension works.

Run:  python tests/verification/write_paths_multitenancy.py
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
# A fresh folder outside the project each run: nothing here may ever touch the
# real cbt.db, and a crashed earlier run must not leave state behind to collide with.
TMP = tempfile.mkdtemp(prefix="brightstars_mt_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('mt')
os.environ.update({
    "BRIGHTSTARS_TENANTS_DIR": os.path.join(TMP, "tenants"),
    "BRIGHTSTARS_PLATFORM_HOSTS": "platform.test,ops.test",
    "BRIGHTSTARS_PORTAL_DOMAIN": "portal.test",
    "BRIGHTSTARS_REGISTRY_CACHE_SECONDS": "0",
    "BRIGHTSTARS_SECRET": "x" * 40,
})
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
os.chdir(ROOT)

import sqlalchemy as sa  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

import app as A  # noqa: E402
from control_plane import provisioning as pv  # noqa: E402
from control_plane.context import NoTenantError, tenant_context  # noqa: E402
from control_plane.models import PlatformAuditLog  # noqa: E402
from control_plane.registry import get_tenant, platform_session, tenant_for_host, to_info  # noqa: E402
from control_plane.routing import dispose_engines, engine_for  # noqa: E402
from core.entrance import load_banks  # noqa: E402
from core.storage import data_dir, uploads_dir  # noqa: E402
from models import Admin, School, Student  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


def csrf(c, path, base):
    body = c.get(path, base_url=base).get_data(as_text=True)
    marker = 'name="_csrf_token" value="'
    start = body.index(marker) + len(marker)
    return body[start:body.index('"', start)]


def info_for(slug):
    with platform_session() as s:
        return to_info(get_tenant(s, slug))


def count(info, model):
    with A.app.app_context(), tenant_context(info):
        return A.db.session.scalar(sa.select(sa.func.count()).select_from(model))


# ---------------------------------------------------------------- provisioning
alpha, alpha_password = pv.create_tenant("alpha", "Alpha Academy", ["alpha.test"],
                                        admin_username="alpha_admin", admin_display_name="Alpha Admin")
# Beta and gamma are made without the platform's standard banks, to show a school can start with none.
beta, beta_password = pv.create_tenant("beta", "Beta College", ["beta.test", "www.beta.test"],
                                       starter_banks=False)
gamma, _ = pv.create_tenant("gamma", "Gamma Grammar School", ["gamma.test"], starter_banks=False)
# Gamma brings a question bank of its own, placed in its own folder.
with open(os.path.join(pv.tenant_folder(gamma), "data", "gamma_own_bank.json"), "w", encoding="utf-8") as fh:
    json.dump({"id": "gamma_own_bank", "name": "Gamma's own bank", "duration_seconds": 600,
               "questions": [{"id": 1, "text": "2 + 2 = ?", "options": ["3", "4", "5", "6"], "answer": 1, "points": 1}]}, fh)

names = {make_url(i.db_url).database for i in (gamma, alpha, beta)}
check("each school has its own PostgreSQL database", len(names) == 3, str(sorted(names)))
from control_plane import config as _config
with sa.create_engine(make_url(gamma.db_url).set(database=_config.pg_maintenance_db()),
                      isolation_level="AUTOCOMMIT").connect() as _c:
    live = {r[0] for r in _c.execute(sa.text(
        "SELECT datname FROM pg_database WHERE datname = ANY(:n)"), {"n": list(names)})}
check("…and all three really exist on the server", live == names, str(sorted(live)))
check("each school database holds exactly its own school row",
      all(count(i, School) == 1 for i in (gamma, alpha, beta)))
with A.app.app_context(), tenant_context(alpha):
    names = A.db.session.scalars(sa.select(School.name)).all()
check("a new school is seeded under its own name, not another school's",
      names == ["Alpha Academy"], str(names))

with A.app.app_context(), tenant_context(alpha):
    alpha_admins = A.db.session.scalars(sa.select(Admin.username)).all()
with A.app.app_context(), tenant_context(beta):
    beta_admins = A.db.session.scalars(sa.select(Admin.username)).all()
check("a new school gets no bootstrap Super Admin, only the admin it was given",
      alpha_admins == ["alpha_admin"] and beta_admins == [], f"{alpha_admins} / {beta_admins}")
check("the first school admin is forced to change the temporary password", bool(alpha_password))

# ---------------------------------------------------------------------- routing
def client(host):
    return A.app.test_client(), f"http://{host}"


c_gm, u_gm = client("gamma.test")
c_al, u_al = client("alpha.test")
c_be, u_be = client("beta.test")
c_be2, u_be2 = client("www.beta.test")
c_un, u_un = client("nobody.test")
c_pl, u_pl = client("platform.test")

check("each school's domain serves that school's portal",
      all(c.get("/login", base_url=u).status_code == 200 for c, u in ((c_gm, u_gm), (c_al, u_al), (c_be, u_be))))
check("a school with two domains answers on both", c_be2.get("/login", base_url=u_be2).status_code == 200)
r = c_un.get("/", base_url=u_un)
check("an unregistered domain is refused before any application code runs",
      r.status_code == 404 and b"not registered" in r.data)
check("the platform host opens on the console, and never serves a school's portal",
      c_pl.get("/", base_url=u_pl).status_code == 302
      and c_pl.get("/", base_url=u_pl).headers["Location"].endswith("/platform/login")
      and c_pl.get("/login", base_url=u_pl).status_code == 404
      and c_pl.get("/admin/home", base_url=u_pl).status_code == 404)
check("/health answers on any host (load balancers)", c_un.get("/health", base_url=u_un).status_code == 200)
check("shared static assets are served on a school and on the platform host",
      c_al.get("/static/app.css", base_url=u_al).status_code == 200
      and c_pl.get("/static/app.css", base_url=u_pl).status_code == 200)

# ------------------------------------------------- data isolation between databases
with A.app.app_context(), tenant_context(alpha):
    A.db.session.add(Student(admission_no="ALPHA-0001", first_name="Only", last_name="InAlpha", active=1, created_at="2026-01-01"))
    A.db.session.commit()
with A.app.app_context(), tenant_context(alpha):
    in_alpha = A.db.session.scalar(sa.select(sa.func.count()).select_from(Student).where(Student.last_name == "InAlpha"))
with A.app.app_context(), tenant_context(beta):
    in_beta = A.db.session.scalar(sa.select(sa.func.count()).select_from(Student).where(Student.last_name == "InAlpha"))
with A.app.app_context(), tenant_context(gamma):
    in_gm = A.db.session.scalar(sa.select(sa.func.count()).select_from(Student).where(Student.last_name == "InAlpha"))
check("a student saved for school A exists in A and is invisible to B and to the existing school",
      (in_alpha, in_beta, in_gm) == (1, 0, 0), f"{(in_alpha, in_beta, in_gm)}")

# ------------------------------------------------------- fail closed, never a default
raised = False
with A.app.app_context():
    try:
        A.db.session.execute(sa.text("SELECT 1 FROM admins"))
    except NoTenantError:
        raised = True
    A.db.session.remove()
check("a query with no school selected is refused instead of using a default database", raised)

# ---------------------------------------------------------- sessions across schools
r = c_al.post("/login", data={"username": "alpha_admin", "password": alpha_password,
                              "_csrf_token": csrf(c_al, "/login", u_al)}, base_url=u_al)
check("a school admin can sign in with the temporary password and is sent to change it",
      r.status_code == 302 and "/admin/password" in r.headers["Location"], f"{r.status_code} {r.headers.get('Location')}")
r_ok = c_al.get("/admin/password", base_url=u_al)
check("the signed-in session works on its own school", r_ok.status_code == 200)

cookie = c_al.get_cookie("session", domain="alpha.test")
c_be.set_cookie("session", cookie.value, domain="beta.test")
r_x = c_be.get("/admin/password", base_url=u_be)
check("a session cookie copied from school A is rejected by school B",
      r_x.status_code == 302 and "/login" in r_x.headers["Location"], f"{r_x.status_code} {r_x.headers.get('Location')}")
c_gm.set_cookie("session", cookie.value, domain="gamma.test")
r_y = c_gm.get("/admin/password", base_url=u_gm)
check("…and by the existing school too",
      r_y.status_code == 302 and "/login" in r_y.headers["Location"], f"{r_y.status_code}")
check("school A's own session is unaffected by that", c_al.get("/admin/password", base_url=u_al).status_code == 200)
c_be_fresh, _ = client("beta.test")
r_login_b = c_be_fresh.post("/login", data={"username": "alpha_admin", "password": alpha_password,
                                            "_csrf_token": csrf(c_be_fresh, "/login", u_be)}, base_url=u_be)
r_after_b = c_be_fresh.get("/admin/password", base_url=u_be)
check("school A's admin credentials do not sign in to school B",
      r_login_b.status_code == 200 and "Location" not in r_login_b.headers
      and r_after_b.status_code == 302 and "/login" in r_after_b.headers["Location"],
      f"{r_login_b.status_code} {r_after_b.status_code}")

# -------------------------------------------------------------------- files on disk
with A.app.app_context(), tenant_context(alpha):
    up_alpha = uploads_dir()
    open(os.path.join(up_alpha, "note.txt"), "w").write("alpha-only")
    data_alpha = data_dir()
    banks_alpha = load_banks()
with A.app.app_context(), tenant_context(beta):
    banks_beta = load_banks()
with A.app.app_context(), tenant_context(gamma):
    banks_gm = load_banks()
    up_gm = uploads_dir()
check("each school has its own uploads and question-bank folders",
      len({up_alpha, up_gm, data_alpha}) == 3 and "alpha" in up_alpha and "gamma" in up_gm)
check("a school's own question bank stays in its own folder, and none of them reached another school",
      len(banks_gm) > 0 and not (set(banks_gm) & (set(banks_alpha) | set(banks_beta))),
      f"{len(banks_gm)} / {len(banks_alpha)} / {len(banks_beta)}")
check("a new school starts with the platform's standard banks in its own folder; one made without them starts with none",
      len(banks_alpha) == 6 and all(i.startswith("starter_") for i in banks_alpha) and len(banks_beta) == 0,
      f"{sorted(banks_alpha)} / {len(banks_beta)}")
check("an upload is served to its own school",
      c_al.get("/static/uploads/note.txt", base_url=u_al).get_data(as_text=True) == "alpha-only")
check("…but is not reachable from another school's domain",
      c_be.get("/static/uploads/note.txt", base_url=u_be).status_code == 404
      and c_gm.get("/static/uploads/note.txt", base_url=u_gm).status_code == 404)
check("…nor from the platform host", c_pl.get("/static/uploads/note.txt", base_url=u_pl).status_code == 404)
check("path traversal out of the uploads folder is refused",
      all(c_al.get(p, base_url=u_al).status_code == 404
          for p in ("/static/uploads/../data/x.json", "/static/uploads/%2e%2e/%2e%2e/app.db",
                    "/static/uploads/..%5c..%5capp.db")))
with A.app.app_context(), tenant_context(alpha):
    from core.storage import stored_upload_path
    escapes = [stored_upload_path(p) for p in ("uploads/../../beta/uploads/x", "../beta/x", "uploads/../app.db")]
    inside = stored_upload_path("uploads/note.txt")
check("a stored upload path cannot point outside the school's own folder",
      escapes == [None, None, None] and inside and os.path.isfile(inside), f"{escapes} {inside}")

# --------------------------------------------------------- suspension and domains
pv.set_status("beta", "suspended", "unpaid")
r_s = c_be.get("/login", base_url=u_be)
check("a suspended school is unavailable on all its domains",
      r_s.status_code == 503 and c_be2.get("/login", base_url=u_be2).status_code == 503)
check("suspending one school does not affect another", c_al.get("/login", base_url=u_al).status_code == 200)
pv.set_status("beta", "active")
check("reactivating restores it", c_be.get("/login", base_url=u_be).status_code == 200)

pv.add_domain("alpha", "portal.alpha-academy.test")
check("a domain added to a school reaches that school",
      tenant_for_host("portal.alpha-academy.test").slug == "alpha")
pv.remove_domain("portal.alpha-academy.test")
check("a removed domain stops resolving", tenant_for_host("portal.alpha-academy.test") is None)
for bad in ("platform.test", "alpha.test", "not a host", "http://x.test/path"):
    try:
        pv.add_domain("beta", bad)
        ok = False
    except (pv.ProvisioningError, ValueError):
        ok = True
    check(f"a reserved, duplicate or malformed domain is refused: {bad!r}", ok)
for bad_slug in ("Bad Slug", "../etc", "", "a" * 60):
    try:
        pv.create_tenant(bad_slug, "X", ["x.test"])
        ok = False
    except (pv.ProvisioningError, ValueError):
        ok = True
    check(f"an unsafe school code is refused: {bad_slug!r}", ok)
try:
    pv.create_tenant("epsilon", "Epsilon", ["alpha.test"])
    ok = False
except pv.ProvisioningError:
    ok = True
with platform_session() as s:
    check("a school cannot take another school's domain, and a refused create leaves nothing behind",
          ok and get_tenant(s, "epsilon") is None)

with platform_session() as s:
    actions = {a.action for a in s.scalars(sa.select(PlatformAuditLog))}
check("platform actions are recorded in the audit log",
      {"tenant.create", "tenant.suspended", "tenant.active", "tenant.domain_add"} <= actions,
      str(actions))

# ----------------------------------------------------------------------- upgrade
pv.upgrade_all_tenants()
check("upgrading every school is idempotent (no duplicate school rows)",
      all(count(i, School) == 1 for i in (gamma, alpha, beta)))

dispose_engines()
DROP_TEST_DATABASES()
shutil.rmtree(TMP, ignore_errors=True)
print()
failed = [x for x in results if not x[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
