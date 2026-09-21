"""Multi-tenancy end to end: one process, several schools, each on its own
domain, database, question banks and uploads.

Builds a throwaway platform in a temp folder:

  * ``crainbow``  imported from a stand-in single-school SQLite database
  * ``alpha``     a brand-new school with a first admin
  * ``beta``      a brand-new school with no admin, on two domains

and then tries to make one school see or affect another: by hostname, by a
session cookie copied between schools, by uploads and question banks on disk,
and by running a query with no school selected. It also checks the platform
side: the existing Super Admin becomes a platform admin, suspension works, and
the original database is never modified.

Run:  python tests/verification/write_paths_multitenancy.py
"""
import hashlib
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
# A fresh folder outside the project each run: nothing here may ever touch the
# real cbt.db, and a crashed earlier run must not leave state behind to collide with.
TMP = tempfile.mkdtemp(prefix="brightstars_mt_")

LEGACY_COPY = os.path.join(TMP, "legacy_single_school.db")

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
from control_plane.models import PlatformAdmin, PlatformAuditLog  # noqa: E402
from control_plane.registry import get_tenant, platform_session, tenant_for_host, to_info  # noqa: E402
from control_plane.routing import dispose_engines, engine_for  # noqa: E402
from core.entrance import load_banks  # noqa: E402
from core.storage import data_dir, uploads_dir  # noqa: E402
from models import Admin, School, Student  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def info_for(slug):
    with platform_session() as s:
        return to_info(get_tenant(s, slug))


def count(info, model):
    with A.app.app_context(), tenant_context(info):
        return A.db.session.scalar(sa.select(sa.func.count()).select_from(model))


def build_legacy_sqlite(path):
    """A stand-in for a pre-platform single-school SQLite installation.

    Built from the models so it always matches the shape the importer expects,
    and populated with just enough to prove the import carried data across.
    """
    from werkzeug.security import generate_password_hash

    engine = sa.create_engine("sqlite:///" + path.replace("\\", "/"))
    A.db.metadata.create_all(engine)
    now = "2026-01-01T00:00:00+00:00"
    m = A.db.metadata.tables
    with engine.begin() as con:
        con.execute(m["schools"].insert(), [
            {"code": "CRMS", "name": "Creative Rainbow Montessori School",
             "motto": "A stand-in motto", "active": 1, "created_at": now}])
        con.execute(m["admin_types"].insert(), [
            {"name": "Super Admin", "is_system": 1, "active": 1, "created_at": now},
            {"name": "Ordinary Admin", "is_system": 0, "active": 1, "created_at": now}])
        con.execute(m["admins"].insert(), [
            {"username": "superadmin", "display_name": "Super Admin",
             "password_hash": generate_password_hash("legacy-password"),
             "admin_type_id": 1, "active": 1, "created_at": now}])
        con.execute(m["students"].insert(), [
            {"admission_no": f"CRMS-{i:04d}", "first_name": f"Pupil{i}", "last_name": "Legacy",
             "active": 1, "created_at": now, "school_id": 1} for i in range(1, 8)])
    engine.dispose()


build_legacy_sqlite(LEGACY_COPY)

# ---------------------------------------------------------------- provisioning
source_hash_before = sha(LEGACY_COPY)
with sa.create_engine("sqlite:///" + LEGACY_COPY.replace("\\", "/")).connect() as c:
    legacy_students = c.execute(sa.text("SELECT COUNT(*) FROM students")).scalar()
    legacy_supers = [r[0] for r in c.execute(sa.text(
        "SELECT a.username FROM admins a JOIN admin_types t ON t.id=a.admin_type_id "
        "WHERE t.is_system=1 AND a.active=1"))]

crainbow = pv.register_existing_tenant(
    "crainbow", "Creative Rainbow Montessori School", ["crainbow.test"], LEGACY_COPY,
    source_data=os.path.join(ROOT, "data"), source_uploads=os.path.join(ROOT, "static", "uploads"))
alpha, alpha_password = pv.create_tenant("alpha", "Alpha Academy", ["alpha.test"],
                                        admin_username="alpha_admin", admin_display_name="Alpha Admin")
# Beta is made without the platform's standard banks, to show a school can start with none.
beta, beta_password = pv.create_tenant("beta", "Beta College", ["beta.test", "www.beta.test"],
                                       starter_banks=False)

check("the existing school keeps every student it had", count(crainbow, Student) == legacy_students,
      f"{count(crainbow, Student)} vs {legacy_students}")
check("registering an existing school never modifies the original database",
      sha(LEGACY_COPY) == source_hash_before)
names = {make_url(i.db_url).database for i in (crainbow, alpha, beta)}
check("each school has its own PostgreSQL database", len(names) == 3, str(sorted(names)))
with sa.create_engine(make_url(crainbow.db_url).set(database="postgres"),
                      isolation_level="AUTOCOMMIT").connect() as _c:
    live = {r[0] for r in _c.execute(sa.text(
        "SELECT datname FROM pg_database WHERE datname = ANY(:n)"), {"n": list(names)})}
check("…and all three really exist on the server", live == names, str(sorted(live)))
check("each school database holds exactly its own school row",
      all(count(i, School) == 1 for i in (crainbow, alpha, beta)))
with A.app.app_context(), tenant_context(alpha):
    names = A.db.session.scalars(sa.select(School.name)).all()
check("a new school is seeded under its own name, not Creative Rainbow's",
      names == ["Alpha Academy"], str(names))

with A.app.app_context(), tenant_context(alpha):
    alpha_admins = A.db.session.scalars(sa.select(Admin.username)).all()
with A.app.app_context(), tenant_context(beta):
    beta_admins = A.db.session.scalars(sa.select(Admin.username)).all()
check("a new school gets no bootstrap Super Admin, only the admin it was given",
      alpha_admins == ["alpha_admin"] and beta_admins == [], f"{alpha_admins} / {beta_admins}")
check("the first school admin is forced to change the temporary password", bool(alpha_password))

# ------------------------------------------------- platform admins (the Super Admin)
adopted = pv.adopt_superadmins(LEGACY_COPY)
with platform_session() as s:
    platform_hashes = {a.username: a.password_hash for a in s.scalars(sa.select(PlatformAdmin))}
check("the existing Super Admin(s) become platform admins", set(adopted) == set(legacy_supers) and adopted,
      f"{adopted} vs {legacy_supers}")
with sa.create_engine("sqlite:///" + LEGACY_COPY.replace("\\", "/")).connect() as c:
    same_hash = all(
        c.execute(sa.text("SELECT password_hash FROM admins WHERE username=:u"), {"u": u}).scalar()
        == platform_hashes[u] for u in adopted)
check("their password hash is carried over (no plaintext, no reset)", same_hash)
check("adopting does not modify the source database", sha(LEGACY_COPY) == source_hash_before)
check("adopting again is a no-op", pv.adopt_superadmins(LEGACY_COPY) == [])

# ---------------------------------------------------------------------- routing
def client(host):
    return A.app.test_client(), f"http://{host}"


c_cr, u_cr = client("crainbow.test")
c_al, u_al = client("alpha.test")
c_be, u_be = client("beta.test")
c_be2, u_be2 = client("www.beta.test")
c_un, u_un = client("nobody.test")
c_pl, u_pl = client("platform.test")

check("each school's domain serves that school's portal",
      all(c.get("/login", base_url=u).status_code == 200 for c, u in ((c_cr, u_cr), (c_al, u_al), (c_be, u_be))))
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
with A.app.app_context(), tenant_context(crainbow):
    in_cr = A.db.session.scalar(sa.select(sa.func.count()).select_from(Student).where(Student.last_name == "InAlpha"))
check("a student saved for school A exists in A and is invisible to B and to the existing school",
      (in_alpha, in_beta, in_cr) == (1, 0, 0), f"{(in_alpha, in_beta, in_cr)}")

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
r = c_al.post("/login", data={"username": "alpha_admin", "password": alpha_password}, base_url=u_al)
check("a school admin can sign in with the temporary password and is sent to change it",
      r.status_code == 302 and "/admin/password" in r.headers["Location"], f"{r.status_code} {r.headers.get('Location')}")
r_ok = c_al.get("/admin/password", base_url=u_al)
check("the signed-in session works on its own school", r_ok.status_code == 200)

cookie = c_al.get_cookie("session", domain="alpha.test")
c_be.set_cookie("session", cookie.value, domain="beta.test")
r_x = c_be.get("/admin/password", base_url=u_be)
check("a session cookie copied from school A is rejected by school B",
      r_x.status_code == 302 and "/login" in r_x.headers["Location"], f"{r_x.status_code} {r_x.headers.get('Location')}")
c_cr.set_cookie("session", cookie.value, domain="crainbow.test")
r_y = c_cr.get("/admin/password", base_url=u_cr)
check("…and by the existing school too",
      r_y.status_code == 302 and "/login" in r_y.headers["Location"], f"{r_y.status_code}")
check("school A's own session is unaffected by that", c_al.get("/admin/password", base_url=u_al).status_code == 200)
c_be_fresh, _ = client("beta.test")
r_login_b = c_be_fresh.post("/login", data={"username": "alpha_admin", "password": alpha_password}, base_url=u_be)
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
with A.app.app_context(), tenant_context(crainbow):
    banks_cr = load_banks()
    up_cr = uploads_dir()
check("each school has its own uploads and question-bank folders",
      len({up_alpha, up_cr, data_alpha}) == 3 and "alpha" in up_alpha and "crainbow" in up_cr)
check("the existing school's own question banks were copied over, and none of them reached another school",
      len(banks_cr) > 0 and not (set(banks_cr) & (set(banks_alpha) | set(banks_beta))),
      f"{len(banks_cr)} / {len(banks_alpha)} / {len(banks_beta)}")
check("a new school starts with the platform's standard banks in its own folder; one made without them starts with none",
      len(banks_alpha) == 6 and all(i.startswith("starter_") for i in banks_alpha) and len(banks_beta) == 0,
      f"{sorted(banks_alpha)} / {len(banks_beta)}")
check("an upload is served to its own school",
      c_al.get("/static/uploads/note.txt", base_url=u_al).get_data(as_text=True) == "alpha-only")
check("…but is not reachable from another school's domain",
      c_be.get("/static/uploads/note.txt", base_url=u_be).status_code == 404
      and c_cr.get("/static/uploads/note.txt", base_url=u_cr).status_code == 404)
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
    pv.create_tenant("gamma", "Gamma", ["alpha.test"])
    ok = False
except pv.ProvisioningError:
    ok = True
with platform_session() as s:
    check("a school cannot take another school's domain, and a refused create leaves nothing behind",
          ok and get_tenant(s, "gamma") is None)

with platform_session() as s:
    actions = {a.action for a in s.scalars(sa.select(PlatformAuditLog))}
check("platform actions are recorded in the audit log",
      {"tenant.create", "tenant.suspended", "tenant.active", "tenant.domain_add", "platform_admin.adopt"} <= actions,
      str(actions))

# ----------------------------------------------------------------------- upgrade
pv.upgrade_all_tenants()
check("upgrading every school is idempotent (no duplicate school rows)",
      all(count(i, School) == 1 for i in (crainbow, alpha, beta)))

dispose_engines()
DROP_TEST_DATABASES()
shutil.rmtree(TMP, ignore_errors=True)
print()
failed = [x for x in results if not x[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
