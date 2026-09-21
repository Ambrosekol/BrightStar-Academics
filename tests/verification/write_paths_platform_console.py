"""The Brightstars Academics platform console, end to end.

Drives the real console through HTTP: signing in as a platform admin, creating
a school (with its branding and logo) through the form, managing its portal and
custom addresses, suspending it, and entering it on its own domain.

It also checks the shape the platform is meant to have: neither kind of address is
a website (the platform's own site is hosted elsewhere, so its console address
opens straight on the console), and every school's files sit in one folder of
their own.

The hostile cases matter as much as the happy path, so this also checks that
the console is invisible from a school's domain, that a platform session is not
a school session, that entry tickets are single-use, short-lived and bound to
one school, and that the reserved operator account cannot be signed into
through a school's own login form.

Run:  python tests/verification/write_paths_platform_console.py
"""
import io
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_console_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('console')
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
from werkzeug.datastructures import FileStorage  # noqa: E402

import app as A  # noqa: E402
from control_plane import provisioning as pv  # noqa: E402
from control_plane.context import tenant_context  # noqa: E402
from control_plane.entry import (  # noqa: E402
    PLATFORM_ADMIN_USERNAME_PREFIX, mint_entry_token, redeem_entry_token,
)
from control_plane.models import PlatformEntryToken  # noqa: E402
from control_plane.registry import get_tenant, platform_session, tenant_for_host, to_info  # noqa: E402
from control_plane.routing import dispose_engines  # noqa: E402
from models import Admin, School, SchoolPublicSetting  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


def client(host):
    return A.app.test_client(), f"http://{host}"


def csrf(c, url, path):
    """Read a page and pull its CSRF token out, the way a browser form would."""
    body = c.get(path, base_url=url).get_data(as_text=True)
    marker = 'name="_csrf_token" value="'
    start = body.index(marker) + len(marker)
    return body[start:body.index('"', start)]


def element(body, element_id):
    """The text of the element carrying this id."""
    marker = f'id="{element_id}">'
    start = body.index(marker) + len(marker)
    return body[start:body.index("<", start)].strip()


def info_for(slug):
    with platform_session() as s:
        tenant = get_tenant(s, slug)
        return to_info(tenant) if tenant else None


def png_bytes():
    """A tiny but structurally real PNG, so the upload validator accepts it."""
    signature = bytes([137, 80, 78, 71, 13, 10, 26, 10])
    ihdr = bytes([0, 0, 0, 13]) + b"IHDR" + bytes([0, 0, 0, 1, 0, 0, 0, 1, 8, 6, 0, 0, 0])
    return signature + ihdr + bytes(16)


pv.create_platform_admin("ops", "Ops Team", "a-long-platform-password")

c_pl, u_pl = client("platform.test")

# ---------------------------------------- the platform host opens on the console
r = c_pl.get("/", base_url=u_pl)
check("/ on the platform host goes straight to the console sign-in, not to a website",
      r.status_code == 302 and r.headers["Location"].endswith("/platform/login"),
      f'{r.status_code} {r.headers.get("Location")}')
check("a second platform host does the same",
      A.app.test_client().get("/", base_url="http://ops.test").headers.get("Location", "")
      .endswith("/platform/login"))
check("the console's sign-in page is served on the platform host",
      c_pl.get("/platform/login", base_url=u_pl).status_code == 200)
check("a second configured platform host works too",
      A.app.test_client().get("/platform/login", base_url="http://ops.test").status_code == 200)
# A browser keeps one host's cookies across every port, so a school sign-in left by an earlier
# run of the app can arrive on an address that belongs to no school, or on a platform address.
# It must be ignored, never looked up (no school is selected to look it up in), and the page
# must not start pinging "who is online", which only a school's own portal answers.
for kind in ("admin_id", "student_id", "parent_id", "candidate_id"):
    stale, _ = client("platform.test")
    with stale.session_transaction(base_url=u_pl) as sess:
        sess[kind] = 1
        sess["admin_logged_in"] = True
    r = stale.get("/", base_url=u_pl)
    check(f"a stale school sign-in ({kind}) does not break the platform address",
          r.status_code == 302 and r.headers["Location"].endswith("/platform/login"), str(r.status_code))
    with stale.session_transaction(base_url=u_pl) as sess:
        check(f"a stale school sign-in ({kind}) is dropped from the session", kind not in sess)
    page = stale.get("/platform/login", base_url=u_pl)
    check(f"a stale school sign-in ({kind}) does not break the console sign-in page",
          page.status_code == 200 and "presence/heartbeat" not in page.get_data(as_text=True))
stale_platform, _ = client("platform.test")
with stale_platform.session_transaction(base_url=u_pl) as sess:
    sess["admin_id"] = 1
    sess["platform_admin_id"] = 7
stale_platform.get("/platform/login", base_url=u_pl)
with stale_platform.session_transaction(base_url=u_pl) as sess:
    check("dropping a stale school sign-in keeps a console sign-in beside it",
          sess.get("platform_admin_id") == 7 and "admin_id" not in sess)

u_nobody = "http://nobody.example"
stale_nobody = A.app.test_client()
with stale_nobody.session_transaction(base_url=u_nobody) as sess:
    sess["admin_id"] = 1
r = stale_nobody.get("/login", base_url=u_nobody)
check("an address no school owns still gets its designed page with a stale sign-in",
      r.status_code == 404 and "not registered" in r.get_data(as_text=True), str(r.status_code))
check("…and that page carries no heartbeat script (nothing there would answer it)",
      "presence/heartbeat" not in r.get_data(as_text=True))
check("…and asking for the heartbeat there is a clean not-found, not an error",
      stale_nobody.get("/presence/heartbeat", base_url=u_nobody).status_code == 404)

r = c_pl.get("/platform", base_url=u_pl)
check("the console requires sign-in", r.status_code == 302 and "/platform/login" in r.headers["Location"])

# ---------------------------------------------------------------------- signin
token = csrf(c_pl, u_pl, "/platform/login")
r = c_pl.post("/platform/login", data={"username": "ops", "password": "wrong", "_csrf_token": token},
              base_url=u_pl)
check("wrong platform credentials are refused", r.status_code == 401)
check("a refused sign-in leaves no session", c_pl.get("/platform", base_url=u_pl).status_code == 302)
r = c_pl.post("/platform/login",
              data={"username": "ops", "password": "a-long-platform-password", "_csrf_token": token},
              base_url=u_pl)
check("correct platform credentials sign in",
      r.status_code == 302 and r.headers["Location"].rstrip("/").endswith("/platform"),
      r.headers.get("Location", ""))
dash = c_pl.get("/platform", base_url=u_pl)
check("the dashboard loads and offers school creation",
      dash.status_code == 200 and "Create a school" in dash.get_data(as_text=True))
r = c_pl.get("/", base_url=u_pl)
check("once signed in, / goes to the dashboard instead of asking to sign in again",
      r.status_code == 302 and r.headers["Location"].rstrip("/").endswith("/platform"),
      r.headers.get("Location", ""))
check("a POST without a CSRF token is refused",
      c_pl.post("/platform/schools/new", data={"name": "X"}, base_url=u_pl).status_code == 403)

# -------------------------------------- creating a school, branding and all
token = csrf(c_pl, u_pl, "/platform/schools/new")
r = c_pl.post("/platform/schools/new", data={
    "name": "Alpha Academy", "code": "alpha",
    "domains": "portal.alphaacademy.test",
    "school_motto": "Learning that lasts", "school_tagline": "Alpha for life",
    "school_phone": "08030000000", "school_email": "head@alphaacademy.test",
    "school_address": "12 Alpha Road",
    "admin_username": "alpha_admin", "admin_display_name": "Alpha Admin",
    "db_url": "", "db_schema": "",
    "logo": FileStorage(io.BytesIO(png_bytes()), filename="logo.png", content_type="image/png"),
    "_csrf_token": token}, base_url=u_pl, content_type="multipart/form-data")
body = r.get_data(as_text=True)
check("the create form reports success", r.status_code == 200 and "Alpha Academy is ready" in body)

portal_host = element(body, "portal-address")
temp_password = element(body, "temp-password")
check("a portal address is issued automatically from the school's code",
      portal_host == "alpha.portal.test", portal_host)
check("a one-time password for the school's first admin is shown once", len(temp_password) > 8)
check("the CNAME record the school must add is spelled out",
      "portal.alphaacademy.test" in body and portal_host in body and "CNAME" in body)

alpha = info_for("alpha")
check("the school is in the registry", alpha is not None and alpha.name == "Alpha Academy")
check("the portal address resolves to it", tenant_for_host(portal_host).slug == "alpha")
check("the school's own address resolves to it too",
      tenant_for_host("portal.alphaacademy.test").slug == "alpha")

# ------------------------------------------------- branding captured up front
with A.app.app_context(), tenant_context(alpha):
    school = A.db.session.scalars(sa.select(School)).first()
    settings = dict(A.db.session.execute(sa.select(
        SchoolPublicSetting.setting_key, SchoolPublicSetting.setting_value)).all())
check("the school's own name and motto are stored in its database",
      school.name == "Alpha Academy" and school.motto == "Learning that lasts",
      f"{school.name} / {school.motto}")
check("its contact details are stored too",
      school.phone == "08030000000" and school.email == "head@alphaacademy.test"
      and school.address == "12 Alpha Road")
check("the portal reads the same branding back",
      settings.get("school_name") == "Alpha Academy"
      and settings.get("school_tagline") == "Alpha for life")
logo_rel = settings.get("school_logo", "")
check("the logo is recorded and stored in the school's own folder",
      logo_rel.startswith("uploads/branding/")
      and os.path.isfile(os.path.join(TMP, "tenants", "alpha", *logo_rel.split("/"))),
      logo_rel)

c_school, u_school = client(portal_host)
login_page = c_school.get("/login", base_url=u_school).get_data(as_text=True)
check("the school's own name is what its sign-in page shows",
      "Alpha Academy" in login_page and "Creative Rainbow" not in login_page)

# ------------------------------------------------- one folder holds everything
root = os.path.join(TMP, "tenants", "alpha")
check("every file belonging to the school is in tenants/<code>/",
      os.path.isdir(root)
      and os.path.isdir(os.path.join(root, "data")) and os.path.isdir(os.path.join(root, "uploads")),
      str(sorted(os.listdir(root)) if os.path.isdir(root) else "missing"))
check("the school's data lives in a PostgreSQL database of its own",
      make_url(alpha.db_url).get_backend_name() == "postgresql"
      and make_url(alpha.db_url).database != make_url(
          os.environ["BRIGHTSTARS_PLATFORM_DB"]).database,
      make_url(alpha.db_url).database)
check("the created page shows the operator that folder", root in body)

# ------------------------------------- a school gets a portal, not a website
r = c_school.get("/", base_url=u_school)
check("/ on a school is its sign-in page, not a marketing site",
      r.status_code == 302 and r.headers["Location"].endswith("/login"),
      f'{r.status_code} {r.headers.get("Location")}')
for path in ("/school", "/school/about", "/school/academics", "/school/school-life",
             "/school/admissions", "/school/news", "/school/contact"):
    check(f"a school has no public website at {path}",
          c_school.get(path, base_url=u_school).status_code == 404)
check("the school's portal itself still works",
      c_school.get("/login", base_url=u_school).status_code == 200)

r = c_school.post("/login", data={"username": "alpha_admin", "password": temp_password},
                  base_url=u_school)
check("the school's first admin can sign in with the one-time password",
      r.status_code == 302 and "/admin/password" in r.headers["Location"],
      f'{r.status_code} {r.headers.get("Location")}')
c_own, u_own = client("portal.alphaacademy.test")
check("the school's own address serves the same portal",
      c_own.get("/login", base_url=u_own).status_code == 200)

# --------------------------------------------------- refusing bad creations
token = csrf(c_pl, u_pl, "/platform/schools/new")
r = c_pl.post("/platform/schools/new", data={
    "name": "Clash", "code": "alpha", "_csrf_token": token}, base_url=u_pl)
check("a duplicate school code is refused with a readable message",
      "already exists" in r.get_data(as_text=True))
r = c_pl.post("/platform/schools/new", data={
    "name": "Bad", "code": "bad", "domains": "portal.alphaacademy.test", "_csrf_token": token},
    base_url=u_pl)
check("taking another school's address is refused",
      "already belongs to another school" in r.get_data(as_text=True) and info_for("bad") is None)
r = c_pl.post("/platform/schools/new", data={
    "name": "Reserved", "code": "reserved", "domains": "platform.test", "_csrf_token": token},
    base_url=u_pl)
check("a platform hostname cannot be given to a school",
      "reserved for the platform console" in r.get_data(as_text=True) and info_for("reserved") is None)
r = c_pl.post("/platform/schools/new", data={"name": "", "code": "", "_csrf_token": token}, base_url=u_pl)
check("a school with no name is refused", "name is required" in r.get_data(as_text=True))

# A file that is not really an image must not leave a half-created school behind,
# and the operator must be able to simply fix it and submit again.
r = c_pl.post("/platform/schools/new", data={
    "name": "Logo Test", "code": "logotest", "_csrf_token": token,
    "logo": FileStorage(io.BytesIO(b"not an image at all"), filename="evil.png",
                        content_type="image/png")}, base_url=u_pl,
    content_type="multipart/form-data")
check("a file that is not really an image is refused",
      "does not appear to be a valid image" in r.get_data(as_text=True))
check("…and the half-created school is rolled back out of the registry",
      info_for("logotest") is None and tenant_for_host("logotest.portal.test") is None)
token = csrf(c_pl, u_pl, "/platform/schools/new")
r = c_pl.post("/platform/schools/new", data={
    "name": "Logo Test", "code": "logotest", "_csrf_token": token,
    "logo": FileStorage(io.BytesIO(png_bytes()), filename="logo.png", content_type="image/png")},
    base_url=u_pl, content_type="multipart/form-data")
check("…so the same code can be used again once the logo is fixed",
      info_for("logotest") is not None and "Logo Test is ready" in r.get_data(as_text=True))
r = c_pl.post("/platform/schools/new", data={
    "name": "Beta College", "code": "", "_csrf_token": token}, base_url=u_pl)
check("a blank code is derived from the school's name, and no domain is needed",
      info_for("beta-college") is not None)
check("that school is reachable on its issued portal address immediately",
      tenant_for_host("beta-college.portal.test").slug == "beta-college")

# ----------------------------------------------------- domains and admins
beta = info_for("beta-college")
token = csrf(c_pl, u_pl, f"/platform/schools/{beta.slug}")
c_pl.post(f"/platform/schools/{beta.slug}/domains",
          data={"action": "add", "hostname": "portal.beta.test", "_csrf_token": token}, base_url=u_pl)
check("an address added in the console reaches the school",
      tenant_for_host("portal.beta.test").slug == beta.slug)
c_pl.post(f"/platform/schools/{beta.slug}/domains",
          data={"action": "remove", "hostname": "portal.beta.test", "_csrf_token": token}, base_url=u_pl)
check("an address removed in the console stops resolving", tenant_for_host("portal.beta.test") is None)
r = c_pl.post(f"/platform/schools/{beta.slug}/domains",
              data={"action": "remove", "hostname": "beta-college.portal.test", "_csrf_token": token},
              base_url=u_pl)
check("the issued portal address cannot be removed",
      tenant_for_host("beta-college.portal.test") is not None)
r = c_pl.post(f"/platform/schools/{beta.slug}/domains",
              data={"action": "add", "hostname": "http://x.test/p", "_csrf_token": token}, base_url=u_pl)
check("a malformed address is rejected with a message, not a crash", r.status_code == 302)

r = c_pl.post(f"/platform/schools/{beta.slug}/admins",
              data={"username": "beta_head", "display_name": "Beta Head", "_csrf_token": token}, base_url=u_pl)
check("the console can add an administrator to a school",
      r.status_code == 200 and "beta_head" in r.get_data(as_text=True))

# ------------------------------------------------------ suspend / activate
c_pl.post(f"/platform/schools/{beta.slug}/status",
          data={"status": "suspended", "reason": "invoice overdue", "_csrf_token": token}, base_url=u_pl)
c_beta, u_beta = client("beta-college.portal.test")
check("suspending in the console takes the school offline",
      c_beta.get("/login", base_url=u_beta).status_code == 503)
check("the school page shows why it is suspended",
      "invoice overdue" in c_pl.get(f"/platform/schools/{beta.slug}", base_url=u_pl).get_data(as_text=True))
r = c_pl.post(f"/platform/schools/{beta.slug}/enter", data={"_csrf_token": token}, base_url=u_pl)
check("a suspended school cannot be entered",
      r.status_code == 302 and "/platform-entry/" not in r.headers["Location"])
c_pl.post(f"/platform/schools/{beta.slug}/status",
          data={"status": "active", "_csrf_token": token}, base_url=u_pl)
check("reactivating in the console restores it",
      c_beta.get("/login", base_url=u_beta).status_code == 200)
r = c_pl.post(f"/platform/schools/{beta.slug}/status",
              data={"status": "deleted", "_csrf_token": token}, base_url=u_pl)
check("an unknown status is rejected", r.status_code == 400)

# ------------------------------------------------------------ enter school
token = csrf(c_pl, u_pl, "/platform/schools/alpha")
r = c_pl.post("/platform/schools/alpha/enter", data={"_csrf_token": token}, base_url=u_pl)
check("entering a school goes to that school's own portal address",
      r.status_code == 302 and r.headers["Location"].startswith(f"http://{portal_host}/platform-entry/"),
      r.headers.get("Location", ""))
entry_path = r.headers["Location"][len(f"http://{portal_host}"):]

c_enter, u_enter = client(portal_host)
r = c_enter.get(entry_path, base_url=u_enter)
check("redeeming the ticket signs the operator in on the school",
      r.status_code == 302 and "/admin/home" in r.headers["Location"],
      f'{r.status_code} {r.headers.get("Location")}')
check("the operator has full authority inside the school",
      c_enter.get("/admin/home", base_url=u_enter).status_code == 200)
check("a signed-in page on a school's portal carries the heartbeat script",
      "/presence/heartbeat" in c_enter.get("/admin/home", base_url=u_enter).get_data(as_text=True))
check("…and on a school's portal the heartbeat is answered",
      c_enter.get("/presence/heartbeat", base_url=u_enter).status_code == 200)

with A.app.app_context(), tenant_context(alpha):
    operator = A.db.session.scalars(sa.select(Admin).where(
        Admin.username == PLATFORM_ADMIN_USERNAME_PREFIX + "ops")).first()
    operator_hash = operator.password_hash if operator else None
    is_system = bool(operator.admin_type.is_system) if operator else False
check("the operator acts through a reserved account in the school's own database",
      operator is not None and is_system)
check("that account's username cannot be created by the school itself",
      "@" in PLATFORM_ADMIN_USERNAME_PREFIX)

r = c_enter.get(entry_path, base_url=u_enter)
check("an entry ticket cannot be used twice",
      r.status_code == 302 and "/login" in r.headers["Location"])
check("an unknown ticket is refused",
      A.app.test_client().get("/platform-entry/not-a-real-token", base_url=u_enter).status_code == 302)

other = mint_entry_token(1, alpha.id, "127.0.0.1")
check("a ticket for one school is refused by another",
      redeem_entry_token(other, info_for("beta-college")) is None)
check("…and is still usable on the school it was minted for",
      redeem_entry_token(other, alpha) is not None)

expired = mint_entry_token(1, alpha.id, "127.0.0.1")
with platform_session() as s:
    row = s.scalars(sa.select(PlatformEntryToken).order_by(PlatformEntryToken.id.desc())).first()
    row.expires_at = "2000-01-01T00:00:00+00:00"
    s.commit()
check("an expired ticket is refused", redeem_entry_token(expired, alpha) is None)
check("a ticket cannot be redeemed on the platform host",
      A.app.test_client().get(entry_path, base_url=u_pl).status_code == 404)

# ------------------------------------ the reserved account is not a login
c_try, u_try = client(portal_host)
r = c_try.post("/login", data={"username": PLATFORM_ADMIN_USERNAME_PREFIX + "ops",
                               "password": "a-long-platform-password"}, base_url=u_try)
check("the operator account cannot be signed into with the platform password",
      r.status_code == 200 and "Location" not in r.headers)
check("its stored password is not the platform password",
      operator_hash and "a-long-platform-password" not in operator_hash)

# --------------------------------------- the console is invisible to schools
for path in ("/platform", "/platform/login", "/platform/schools/new", "/platform/audit",
             "/platform/activity", "/platform/team", "/platform/schools/alpha"):
    check(f"a school's address does not expose {path}",
          c_school.get(path, base_url=u_school).status_code == 404)
check("a platform session is not a school session",
      c_pl.get("/admin/home", base_url=u_pl).status_code == 404)

# ------------------------------------------------------------- platform audit
from control_plane import team  # noqa: E402

recorded = {row["action"] for row in team.activity(None, "all", 1, per_page=1000)["rows"]}
for action in ("tenant.create", "tenant.enter", "tenant.suspended", "tenant.domain_add"):
    check(f"the activity log records {action}", action in recorded)
check("…and the activity page shows them in words",
      team.describe("tenant.create") in c_pl.get("/platform/activity?admin=all&category=schools",
                                                 base_url=u_pl).get_data(as_text=True))
r = c_pl.get("/platform/audit", base_url=u_pl)
check("the old activity address still leads to the activity log",
      r.status_code == 302 and r.headers["Location"].endswith("/platform/activity"))

# ------------------------------------------------------- platform password
token = csrf(c_pl, u_pl, "/platform/password")
r = c_pl.post("/platform/password", data={"current_password": "wrong", "new_password": "another-long-one",
                                          "confirm_password": "another-long-one", "_csrf_token": token},
              base_url=u_pl)
check("changing the platform password needs the current one", "incorrect" in r.get_data(as_text=True))
r = c_pl.post("/platform/password", data={"current_password": "a-long-platform-password",
                                          "new_password": "short", "confirm_password": "short",
                                          "_csrf_token": token}, base_url=u_pl)
check("a short platform password is refused", "at least 10" in r.get_data(as_text=True))
r = c_pl.post("/platform/password", data={"current_password": "a-long-platform-password",
                                          "new_password": "a-brand-new-long-password",
                                          "confirm_password": "a-brand-new-long-password",
                                          "_csrf_token": token}, base_url=u_pl)
check("the platform password can be changed", r.status_code == 302)
c_fresh, _ = client("platform.test")
t2 = csrf(c_fresh, u_pl, "/platform/login")
check("the new platform password works",
      c_fresh.post("/platform/login", data={"username": "ops", "password": "a-brand-new-long-password",
                                            "_csrf_token": t2}, base_url=u_pl).status_code == 302)

# ------------------------------------------------- errors on the platform host
check("an unknown platform path renders an error page rather than crashing",
      c_pl.get("/platform/nope", base_url=u_pl).status_code == 404)
check("a school path on the platform host is refused",
      c_pl.get("/admin/school", base_url=u_pl).status_code == 404)
check("an unregistered address is refused outright",
      A.app.test_client().get("/login", base_url="http://nobody.test").status_code == 404)
check("signing out clears the platform session",
      c_pl.post("/platform/logout", data={"_csrf_token": csrf(c_pl, u_pl, "/platform/password")},
                base_url=u_pl).status_code == 302
      and c_pl.get("/platform", base_url=u_pl).status_code == 302)

dispose_engines()
DROP_TEST_DATABASES()
shutil.rmtree(TMP, ignore_errors=True)
print()
failed = [x for x in results if not x[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
