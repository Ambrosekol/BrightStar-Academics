"""The platform's own team, and its activity logs, end to end.

There is one super admin, who can always look over and protect the system, and
ordinary platform admins, who have full control over every school but nothing to
do with the team itself. This drives the real console over HTTP and checks:

* who may add, remove, restore and reset admins — and that nobody else can, even
  with a valid form token;
* that removing an admin really takes their access away everywhere, including a
  session they already have open inside a school, while keeping their account and
  their log;
* that every action is written to the log against the admin who did it, and that
  the log arranged as "choose an admin, then read their log" shows exactly that
  admin's entries to the super admin and to nobody else;
* the designed page shown for an address that belongs to no school.

Run:  python tests/verification/write_paths_platform_team.py
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_team_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('team')
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
from sqlalchemy.engine import make_url  # noqa: E402

import app as A  # noqa: E402
from control_plane import config, provisioning as pv, team  # noqa: E402
from control_plane.models import (  # noqa: E402
    DOMAIN_PORTAL, PlatformAdmin, PlatformAuditLog, PlatformEntryToken, ROLE_ADMIN, ROLE_SUPER, Tenant,
    TenantDomain,
)
from control_plane.registry import (  # noqa: E402
    get_tenant, init_platform_db, platform_engine, platform_session, to_info,
)
from control_plane.routing import dispose_engines, engine_for  # noqa: E402

results = []
PL = "http://platform.test"
PASSWORD = "a-long-platform-password"


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


def client():
    return A.app.test_client()


def csrf(c, path, base=PL):
    body = c.get(path, base_url=base).get_data(as_text=True)
    marker = 'name="_csrf_token" value="'
    start = body.index(marker) + len(marker)
    return body[start:body.index('"', start)]


def sign_in(c, username, password):
    return c.post("/platform/login", data={"username": username, "password": password,
                                            "_csrf_token": csrf(c, "/platform/login")}, base_url=PL)


def post(c, path, page="/platform/team", **data):
    data["_csrf_token"] = csrf(c, page)
    return c.post(path, data=data, base_url=PL)


def admin_row(username):
    with platform_session() as s:
        a = s.scalars(sa.select(PlatformAdmin).where(PlatformAdmin.username == username)).first()
        return {"id": a.id, "role": a.role, "active": a.active, "must_change": a.password_must_change,
                "removed_at": a.removed_at, "removed_by": a.removed_by} if a else None


def info_for(slug):
    with platform_session() as s:
        tenant = get_tenant(s, slug)
        return to_info(tenant) if tenant else None


def all_password_hashes():
    with platform_session() as s:
        return [h for h in s.scalars(sa.select(PlatformAdmin.password_hash))]


def temp_password(response):
    body = response.get_data(as_text=True)
    marker = 'id="temp-password">'
    start = body.index(marker) + len(marker)
    return body[start:body.index("<", start)].strip()


def log_actions(username=None, category="all", per_page=1000):
    admin_id = admin_row(username)["id"] if username else None
    return [r["action"] for r in team.activity(admin_id, category, 1, per_page)["rows"]]


def make_school(c, code, name=None):
    return c.post("/platform/schools/new", data={
        "_csrf_token": csrf(c, "/platform/schools/new"), "name": name or code.title(), "code": code,
        "school_brand_primary": "#0d2b52", "school_brand_accent": "#1674b9"}, base_url=PL,
        content_type="multipart/form-data")


# ================================================================ roles and bootstrap
role_first = pv.create_platform_admin("ada", "Ada Root", PASSWORD)
role_second = pv.create_platform_admin("second", "Second Admin", PASSWORD)
role_third = pv.create_platform_admin("third", "Third Super", PASSWORD, superadmin=True)
check("the first admin on a platform becomes its super admin", role_first == ROLE_SUPER)
check("later admins are ordinary platform admins", role_second == ROLE_ADMIN)
check("another super admin can be added deliberately, from the command line", role_third == ROLE_SUPER)

# A registry created before roles existed has admins but no role column and no super admin.
with platform_engine().begin() as conn:
    conn.execute(sa.text("ALTER TABLE platform_admins DROP COLUMN role"))
    conn.execute(sa.text("ALTER TABLE platform_admins DROP COLUMN removed_at"))
    conn.execute(sa.text("ALTER TABLE platform_admins DROP COLUMN removed_by"))
init_platform_db()
check("an older registry gains the new columns in place",
      {"role", "removed_at", "removed_by"} <= {c["name"] for c in sa.inspect(platform_engine()).get_columns("platform_admins")})
check("…and its earliest admin becomes the super admin, everyone else an ordinary admin",
      admin_row("ada")["role"] == ROLE_SUPER and admin_row("second")["role"] == ROLE_ADMIN
      and admin_row("third")["role"] == ROLE_ADMIN)
check("…which is written to the log", "platform_admin.promote" in log_actions("ada"))
init_platform_db()
check("upgrading again changes nothing", admin_row("ada")["role"] == ROLE_SUPER
      and log_actions().count("platform_admin.promote") == 1)

with platform_session() as s:
    s.execute(sa.update(PlatformAdmin).where(PlatformAdmin.username == "third").values(active=0))
    s.commit()
# leave "third" removed so it plays no part below
sup = client()
sign_in(sup, "ada", PASSWORD)

# ================================================================ who may see the team
reg = client()
check("an ordinary admin signs in", sign_in(reg, "second", PASSWORD).status_code == 302)
nav_super = sup.get("/platform", base_url=PL).get_data(as_text=True)
nav_reg = reg.get("/platform", base_url=PL).get_data(as_text=True)
check("the super admin's menu offers Team; an ordinary admin's does not",
      "/platform/team" in nav_super and "/platform/team" not in nav_reg)
check("the console names each admin's role",
      "Super admin" in nav_super and "Platform admin" in nav_reg)
check("the super admin can open the Team page", sup.get("/platform/team", base_url=PL).status_code == 200)
check("an ordinary admin cannot", reg.get("/platform/team", base_url=PL).status_code == 403)
anon = client()
r = anon.get("/platform/team", base_url=PL)
check("nor can a signed-out visitor", r.status_code == 302 and "/platform/login" in r.headers["Location"])
check("the Team page does not exist on a school's address",
      client().get("/platform/team", base_url="http://anything.portal.test").status_code == 404)

# ================================================================ adding an admin
r = post(sup, "/platform/team/new", username="Amaka.Eze", display_name="Amaka Eze", email="amaka@example.test")
body = r.get_data(as_text=True)
temp = temp_password(r)
check("the super admin adds a platform admin and is shown a one-time password",
      r.status_code == 200 and "Platform admin added" in body and len(temp) >= 12)
amaka = admin_row("amaka.eze")
check("the new admin is an ordinary admin who must choose their own password",
      amaka and amaka["role"] == ROLE_ADMIN and amaka["must_change"] == 1 and amaka["active"] == 1, str(amaka))
check("the password is not kept anywhere in the clear", all(temp not in h for h in all_password_hashes()))

am = client()
r = sign_in(am, "amaka.eze", temp)
check("they can sign in with it, and are sent to choose a new one",
      r.status_code == 302 and r.headers["Location"].endswith("/platform"), r.headers.get("Location", ""))
r = am.get("/platform", base_url=PL)
check("nothing else in the console opens until they have",
      r.status_code == 302 and "/platform/password" in r.headers["Location"])
r = am.get("/platform/schools/new", base_url=PL)
check("…including the pages that change things",
      r.status_code == 302 and "/platform/password" in r.headers["Location"])
page = am.get("/platform/password", base_url=PL).get_data(as_text=True)
check("the password page explains why", "temporary password" in page.lower() and "Choose your own password" in page)
r = am.post("/platform/password", data={"_csrf_token": csrf(am, "/platform/password"), "current_password": temp,
                                        "new_password": "brand-new-password-1", "confirm_password": "brand-new-password-1"},
            base_url=PL)
check("choosing a password releases the console",
      r.status_code == 302 and am.get("/platform", base_url=PL).status_code == 200)
check("…and the temporary one stops working",
      sign_in(client(), "amaka.eze", temp).status_code == 401 and admin_row("amaka.eze")["must_change"] == 0)

for label, fields in (("a username with @ (the reserved namespace)", {"username": "a@b"}),
                      ("a username with a space", {"username": "two words"}),
                      ("a username that is too short", {"username": "ab"}),
                      ("a username with markup", {"username": "<script>"}),
                      ("a username that already exists", {"username": "amaka.eze"}),
                      ("an empty username", {"username": ""})):
    r = post(sup, "/platform/team/new", **{"display_name": "X", **fields})
    check(f"{label} is refused with a message, and nobody is added",
          r.status_code == 400 and 'class="errors"' in r.get_data(as_text=True))
with platform_session() as s:
    count = s.scalar(sa.select(sa.func.count(PlatformAdmin.id)))
check("…so only the intended admins exist", count == 4, str(count))

r = post(sup, "/platform/team/new", username="tunde", display_name='<img src=x onerror=alert(1)> Tunde')
page = sup.get("/platform/team", base_url=PL).get_data(as_text=True)
check("a display name with markup is shown as text, never as markup",
      "<img src=x" not in page and "&lt;img src=x" in page)
check("nobody is ever made a super admin from the console",
      admin_row("tunde")["role"] == ROLE_ADMIN and admin_row("amaka.eze")["role"] == ROLE_ADMIN)
check("adding an admin needs the form token",
      sup.post("/platform/team/new", data={"username": "nope", "display_name": "N"}, base_url=PL).status_code == 403
      and admin_row("nope") is None)

# ================================================================ ordinary admins keep full control of schools
r = make_school(am, "alpha", "Alpha Academy")
check("an ordinary admin can create a school", "Alpha Academy is ready" in r.get_data(as_text=True))
make_school(sup, "beta", "Beta College")
r = am.post("/platform/schools/beta/status", data={"_csrf_token": csrf(am, "/platform/schools/beta"),
            "status": "suspended", "reason": "test"}, base_url=PL)
check("…suspend one", r.status_code == 302 and info_for("beta").status == "suspended")
am.post("/platform/schools/beta/status", data={"_csrf_token": csrf(am, "/platform/schools/beta"),
        "status": "active"}, base_url=PL)
r = am.post("/platform/schools/alpha/enter", data={"_csrf_token": csrf(am, "/platform/schools/alpha")}, base_url=PL)
check("…and enter one", r.status_code == 302 and "/platform-entry/" in r.headers["Location"])
client().get(r.headers["Location"][len("http://alpha.portal.test"):], base_url="http://alpha.portal.test")
r = am.post("/platform/schools/alpha/branding", data={"_csrf_token": csrf(am, "/platform/schools/alpha"),
            "school_brand_primary": "#123456", "school_brand_accent": "#0b6e4f"}, base_url=PL,
            content_type="multipart/form-data")
check("…and change its branding", r.status_code == 302)

# ================================================================ but not the team
am_id, ada_id, tunde_id = admin_row("amaka.eze")["id"], admin_row("ada")["id"], admin_row("tunde")["id"]
denied = []
for path in ("/platform/team/new", f"/platform/team/{tunde_id}/remove", f"/platform/team/{tunde_id}/restore",
             f"/platform/team/{tunde_id}/reset-password", f"/platform/team/{ada_id}/remove"):
    r = am.post(path, data={"_csrf_token": csrf(am, "/platform"), "username": "sneaky", "display_name": "S"}, base_url=PL)
    denied.append(r.status_code)
check("an ordinary admin cannot add, remove, restore or reset — even with a valid form token",
      set(denied) == {403}, str(denied))
check("…and nothing changed", admin_row("tunde")["active"] == 1 and admin_row("ada")["active"] == 1
      and admin_row("sneaky") is None)

# ================================================================ the log: attribution
check("signing in is logged against the admin", "platform_admin.login" in log_actions("amaka.eze"))
check("a wrong password on a real account is logged against it",
      (sign_in(client(), "amaka.eze", "definitely-wrong"), "platform_admin.login_failed" in log_actions("amaka.eze"))[1])
before = len(log_actions())
sign_in(client(), "nobody-by-this-name", "whatever-password")
check("a username that matches nobody is not attributed to anyone",
      len(log_actions()) == before)
with platform_session() as s:
    ip = s.scalars(sa.select(PlatformAuditLog.ip_address).where(
        PlatformAuditLog.action == "platform_admin.login", PlatformAuditLog.actor_username == "amaka.eze")).first()
check("the address a sign-in came from is recorded", bool(ip), str(ip))
check("changing their own password is logged", "platform_admin.password_change" in log_actions("amaka.eze"))
mine = log_actions("amaka.eze")
check("what an admin did to schools is in their log: create, suspend, reactivate, enter, branding",
      {"tenant.create", "tenant.suspended", "tenant.active", "tenant.enter", "tenant.branding_update"} <= set(mine),
      str(sorted(set(mine))))
check("…and not in another admin's log",
      "tenant.create" not in log_actions("second") and "tenant.create" in log_actions("ada"))
check("the super admin's own actions are in theirs",
      {"platform_admin.create", "tenant.create"} <= set(log_actions("ada")))
with platform_session() as s:
    s.add(PlatformAuditLog(platform_admin_id=None, actor_username="second", action="tenant.create",
                           detail="written before entries carried an admin id", created_at="2020-01-01T00:00:00+00:00"))
    s.commit()
check("an older entry that names only the username still shows in that admin's log",
      any(r["detail"] and "before entries carried" in r["detail"] for r in team.activity(admin_row("second")["id"])["rows"]))

# categories
schools_only = log_actions("amaka.eze", "schools")
signins_only = log_actions("amaka.eze", "signins")
account_only = log_actions("ada", "account")
check("the Schools view holds only school actions",
      schools_only and all(a.startswith("tenant.") for a in schools_only))
check("the Sign-ins view holds only sign-ins and sign-outs",
      signins_only and all(a in team.SIGNIN_ACTIONS for a in signins_only))
check("the Accounts view holds team and password changes, and no sign-ins",
      account_only and all(a.startswith("platform_admin.") and a not in team.SIGNIN_ACTIONS for a in account_only))
check("an unknown category behaves as Everything", log_actions("amaka.eze", "nonsense") == log_actions("amaka.eze"))

# pagination
second_id = admin_row("second")["id"]
with platform_session() as s:
    for i in range(70):
        s.add(PlatformAuditLog(platform_admin_id=second_id, actor_username="second", action="tenant.enter",
                               detail=f"bulk {i}", created_at=f"2021-01-01T00:{i // 60:02d}:{i % 60:02d}+00:00"))
    s.commit()
first = team.activity(second_id, "all", 1)
last = team.activity(second_id, "all", 99)
check("a long log is paged, newest first",
      first["pages"] >= 3 and len(first["rows"]) == team.PER_PAGE and first["total"] >= 70, str(first["pages"]))
check("a page beyond the end is clamped to the last one", last["page"] == last["pages"] and last["rows"])
check("…and page 1 is the newest entry", first["rows"][0]["created_at"] >= first["rows"][-1]["created_at"])

# ================================================================ the log: arranged as admin -> logs
page = sup.get("/platform/activity", base_url=PL).get_data(as_text=True)
check("the super admin's activity page lists every admin to choose from",
      all(name in page for name in ("Ada Root", "Second Admin", "Amaka Eze", "Everyone")) and "Removed" in page)
r = sup.get(f"/platform/activity?admin={am_id}", base_url=PL)
page = r.get_data(as_text=True)
check("choosing an admin shows that admin's log",
      r.status_code == 200 and "amaka.eze" in page and team.describe("tenant.create") in page)
check("…and only theirs: another admin's entries are not on it",
      "second" not in page.split('<section aria-label="Log">')[1])
check("a category tab narrows it",
      team.describe("platform_admin.login") not in sup.get(f"/platform/activity?admin={am_id}&category=schools",
                                                           base_url=PL).get_data(as_text=True).split('<tbody>')[1])
check("Everyone shows all admins' entries together",
      "amaka.eze" in sup.get("/platform/activity?admin=all", base_url=PL).get_data(as_text=True))
check("an admin that does not exist is a 404", sup.get("/platform/activity?admin=99999", base_url=PL).status_code == 404)
check("a garbage admin id falls back to the viewer's own log, not an error",
      sup.get("/platform/activity?admin=abc", base_url=PL).status_code == 200)
check("a garbage page number does not break it", sup.get(f"/platform/activity?admin={am_id}&page=zzz", base_url=PL).status_code == 200)

page = am.get("/platform/activity", base_url=PL).get_data(as_text=True)
check("an ordinary admin sees only their own log, with no list of other admins",
      "Amaka Eze" in page and "Second Admin" not in page and "Everyone" not in page
      and "Everything you have done on the platform" in page)
for query in (f"?admin={ada_id}", "?admin=all", f"?admin={tunde_id}"):
    body = am.get("/platform/activity" + query, base_url=PL)
    txt = body.get_data(as_text=True)
    check(f"an ordinary admin asking for someone else's log ({query}) still gets only their own",
          body.status_code in (200, 404) and "ada</span>" not in txt and "Second Admin" not in txt
          and "Ada Root" not in txt.split('<section aria-label="Log">')[-1])
check("the activity log needs a sign-in", client().get("/platform/activity", base_url=PL).status_code == 302)
check("the old activity address redirects", sup.get("/platform/audit", base_url=PL).status_code == 302)
dash = sup.get("/platform", base_url=PL).get_data(as_text=True)
dash_reg = am.get("/platform", base_url=PL).get_data(as_text=True)
check("the dashboard's latest activity is everyone's for the super admin, one's own for the rest",
      "Latest activity" in dash and "Your latest activity" in dash_reg)

# ================================================================ removing an admin
r = sup.post(f"/platform/team/{ada_id}/remove", data={"_csrf_token": csrf(sup, "/platform/team")}, base_url=PL,
             follow_redirects=True)
check("the super admin cannot be removed, not even by themselves",
      "super admin cannot be removed" in r.get_data(as_text=True) and admin_row("ada")["active"] == 1)

# Amaka is inside a school when she is removed.
r = am.post("/platform/schools/alpha/enter", data={"_csrf_token": csrf(am, "/platform/schools/alpha")}, base_url=PL)
inside = client()
inside_url = "http://alpha.portal.test"
inside.get(r.headers["Location"][len("http://alpha.portal.test"):], base_url=inside_url)
check("an admin who has entered a school can work in it",
      inside.get("/admin/home", base_url=inside_url).status_code == 200)

# ---- what admins do INSIDE schools, read from each school's own audit trail
def work_inside(console, school_slug, colour):
    """Enter a school and change its branding from inside, as its operator would."""
    r = console.post(f"/platform/schools/{school_slug}/enter",
                     data={"_csrf_token": csrf(console, f"/platform/schools/{school_slug}")}, base_url=PL)
    url = f"http://{school_slug}.portal.test"
    school = client()
    school.get(r.headers["Location"][len(url):], base_url=url)
    school.get("/admin/workspace/school", base_url=url)
    school.post("/admin/school/branding/save", data={
        "_csrf_token": csrf(school, "/admin/school/branding", base=url),
        "school_brand_primary": colour, "school_brand_accent": "#1674b9"},
        base_url=url, content_type="multipart/form-data")
    return school


inside.get("/admin/workspace/school", base_url=inside_url)  # Amaka is already inside alpha
inside.post("/admin/school/branding/save", data={
    "_csrf_token": csrf(inside, "/admin/school/branding", base=inside_url),
    "school_brand_primary": "#223344", "school_brand_accent": "#1674b9"},
    base_url=inside_url, content_type="multipart/form-data")
work_inside(sup, "beta", "#334455")

am_inside = team.activity(am_id, "inside")
ada_inside = team.activity(ada_id, "inside")
every_inside = team.activity(None, "inside")
check("an admin's log shows what they did inside a school, read from that school's own audit trail",
      am_inside["total"] >= 2 and "inside.school_branding_updated" in [r["action"] for r in am_inside["rows"]],
      str([r["action"] for r in am_inside["rows"]]))
check("…every entry is theirs, in the school they entered, and marked as inside a school",
      all(r["actor"] == "amaka.eze" and r["slug"] == "alpha" and r["category"] == "inside" for r in am_inside["rows"]))
check("…and is worded for a person, not as a code",
      {"School branding updated", "Submitted a change"} <= {r["label"] for r in am_inside["rows"]})
check("another admin's in-school actions are in their own log, in their own school",
      ada_inside["total"] >= 2 and {r["slug"] for r in ada_inside["rows"]} == {"beta"}
      and {r["actor"] for r in ada_inside["rows"]} == {"ada"})
check("Everyone shows both, each attributed",
      {r["actor"] for r in every_inside["rows"]} == {"amaka.eze", "ada"}
      and {r["slug"] for r in every_inside["rows"]} == {"alpha", "beta"})
check("entering a school is not listed twice: it is in the platform log, not repeated inside",
      "inside.platform_admin_entered_school" not in [r["action"] for r in every_inside["rows"]]
      and "tenant.enter" in log_actions("amaka.eze"))

merged = team.activity(am_id, "all", 1, 1000)
platform_only = team.activity(am_id, "all", 1, 1000, inside=False)
moments = [team._when(r["created_at"]) for r in merged["rows"]]
check("Everything interleaves the platform log and the inside-school log, newest first",
      {"tenant.enter", "inside.school_branding_updated"} <= {r["action"] for r in merged["rows"]}
      and moments == sorted(moments, reverse=True))
check("…and its count is exactly the two added together",
      merged["total"] == platform_only["total"] + am_inside["total"])
check("the platform-only view touches no school and shows no inside entries",
      not any(r["category"] == "inside" for r in platform_only["rows"]))

# Rows that must not appear, and rows that must be handled safely.
info_alpha = info_for("alpha")


def school_audit(username, action="probe", details=None, when="2026-01-01T00:00:00+00:00", success=1):
    with engine_for(info_alpha).begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO audit_logs (username_snapshot, action, module, details, success, created_at) "
            "VALUES (:u, :a, 'test', :d, :s, :t)"), {"u": username, "a": action, "d": details, "s": success, "t": when})


school_audit("real_school_admin", "school_class_created")
school_audit("platform@amaka.eze", "hostile_probe", '<script>alert("x")</script>', "2026-01-02T00:00:00+00:00")
school_audit("platform@amaka.eze", "long_probe", "x" * 1000, "2026-01-03T00:00:00+00:00")
school_audit("platform@amaka.eze", "authorization_denied", None, "2026-01-04T00:00:00+00:00", success=0)
rows = team.activity(am_id, "inside", 1, 1000)["rows"]
acts = [r["action"] for r in rows]
check("a school's own staff never appear in an operator's log",
      "inside.school_class_created" not in acts and "real_school_admin" not in {r["actor"] for r in rows})
check("…and one admin's log holds only that admin's rows", {r["actor"] for r in rows} == {"amaka.eze"})
long_row = next(r for r in rows if r["action"] == "inside.long_probe")
check("a very long detail is cut short", len(long_row["detail"]) <= team.INSIDE_DETAIL_LIMIT + 1)
denied = next(r for r in rows if r["action"] == "inside.authorization_denied")
check("a refused or failed action inside a school is flagged", denied["alert"] is True)
page = sup.get(f"/platform/activity?admin={am_id}&category=inside", base_url=PL).get_data(as_text=True)
check("the Inside schools tab lists them, linked to the school",
      "Inside schools" in page and "hostile probe" in page.lower() and 'href="/platform/schools/alpha"' in page)
check("…and shows hostile text as text, never as markup",
      '<script>alert("x")</script>' not in page and "&lt;script&gt;" in page)
check("…and says only entered schools are shown", "Only schools they have entered" in page)
dash_page = sup.get("/platform", base_url=PL).get_data(as_text=True)
check("the dashboard's feed stays on the platform log and shows none of it",
      "hostile probe" not in dash_page.lower() and "School branding updated" not in dash_page)

# Paging across the merged log.
for i in range(45):
    school_audit("platform@amaka.eze", "bulk_probe", f"n{i}", f"2025-06-01T00:{i:02d}:00+00:00")
p1 = team.activity(am_id, "all", 1)
p2 = team.activity(am_id, "all", 2)
seen = [(r["created_at"], r["action"], r["detail"]) for r in p1["rows"] + p2["rows"]]
times = [team._when(r["created_at"]) for r in p1["rows"] + p2["rows"]]
check("the merged log pages cleanly: no entry twice, still newest first across pages",
      len(seen) == len(set(seen)) and times == sorted(times, reverse=True) and p1["pages"] >= 2
      and len(p1["rows"]) == team.PER_PAGE,
      f"rows={len(seen)} unique={len(set(seen))} sorted={times == sorted(times, reverse=True)} "
      f"pages={p1['pages']} page1={len(p1['rows'])} dupes={[x for x in seen if seen.count(x) > 1][:3]}")
check("…and its total counts both sources", p1["total"] == team.activity(am_id, "all", 1, 5, inside=False)["total"]
      + team.activity(am_id, "inside", 1, 5)["total"])
check("a page far beyond the end is clamped, not run away with",
      team.activity(am_id, "all", 10**9)["page"] == p1["pages"])

# Who may read it.
page_reg = reg.get(f"/platform/activity?admin={ada_id}&category=inside", base_url=PL).get_data(as_text=True)
check("an ordinary admin cannot read another admin's in-school log",
      "School branding updated" not in page_reg and "Ada Root" not in page_reg.split('<section aria-label="Log">')[-1])
check("…but can see their own tab", "Inside schools" in page_reg)
check("a signed-out visitor cannot", client().get("/platform/activity?category=inside", base_url=PL).status_code == 302)

with platform_session() as s:
    tokens_before = s.scalar(sa.select(sa.func.count(PlatformEntryToken.id)).where(PlatformEntryToken.platform_admin_id == am_id))
pending = am.post("/platform/schools/beta/enter", data={"_csrf_token": csrf(am, "/platform/schools/beta")}, base_url=PL)
pending_path = pending.headers["Location"][len("http://beta.portal.test"):]

r = sup.post(f"/platform/team/{am_id}/remove", data={"_csrf_token": csrf(sup, "/platform/team")}, base_url=PL,
             follow_redirects=True)
check("the super admin removes an admin", "Access removed" in r.get_data(as_text=True)
      and admin_row("amaka.eze")["active"] == 0)
check("the account is kept, with who removed it and when",
      admin_row("amaka.eze")["removed_by"] == "ada" and admin_row("amaka.eze")["removed_at"])
check("their console session ends at once",
      am.get("/platform", base_url=PL).status_code == 302 and "/platform/login" in am.get("/platform", base_url=PL).headers["Location"])
check("they cannot sign in again", sign_in(client(), "amaka.eze", "brand-new-password-1").status_code == 401)
check("…and the refused attempt is logged against them", "platform_admin.login_refused" in log_actions("amaka.eze"))
check("their session inside the school ends at the next click",
      inside.get("/admin/home", base_url=inside_url).status_code == 302
      and "/login" in inside.get("/admin/home", base_url=inside_url).headers["Location"])
with engine_for(info_for("alpha")).connect() as conn:
    row = conn.execute(sa.text("SELECT active FROM admins WHERE username = 'platform@amaka.eze'")).first()
check("…because their reserved account in the school was switched off", row is not None and row[0] == 0, str(row))
with platform_session() as s:
    tokens_after = s.scalar(sa.select(sa.func.count(PlatformEntryToken.id)).where(PlatformEntryToken.platform_admin_id == am_id))
check("a ticket into a school that they had not yet used is dead too",
      tokens_after == 0 and client().get(pending_path, base_url="http://beta.portal.test").status_code == 302
      and "/login" in client().get(pending_path, base_url="http://beta.portal.test").headers["Location"])
check("their log survives removal and can still be read",
      "tenant.create" in log_actions("amaka.eze") and "platform_admin.remove" in log_actions("ada"))
check("what a removed admin did inside schools survives their removal too",
      any(r["action"] == "inside.school_branding_updated" for r in team.activity(am_id, "inside", 1, 1000)["rows"]))
page = sup.get(f"/platform/activity?admin={am_id}", base_url=PL).get_data(as_text=True)
check("…listed under Removed on the activity page, marked as removed",
      "Removed" in page and 'class="removed"' in page)
check("removing someone twice is refused",
      "already been removed" in sup.post(f"/platform/team/{am_id}/remove", data={"_csrf_token": csrf(sup, "/platform/team")},
                                         base_url=PL, follow_redirects=True).get_data(as_text=True))
check("removing an admin who does not exist is refused, not a crash",
      sup.post("/platform/team/424242/remove", data={"_csrf_token": csrf(sup, "/platform/team")}, base_url=PL,
               follow_redirects=True).status_code == 200)
check("removing an admin needs the form token",
      sup.post(f"/platform/team/{tunde_id}/remove", data={}, base_url=PL).status_code == 403 and admin_row("tunde")["active"] == 1)

# ================================================================ restoring, and resetting a password
r = sup.post(f"/platform/team/{am_id}/restore", data={"_csrf_token": csrf(sup, "/platform/team")}, base_url=PL)
temp2 = temp_password(r)
check("a removed admin can be restored, with a new temporary password",
      r.status_code == 200 and admin_row("amaka.eze")["active"] == 1 and admin_row("amaka.eze")["must_change"] == 1)
check("their old password is discarded", sign_in(client(), "amaka.eze", "brand-new-password-1").status_code == 401)
am2 = client()
check("the new one works, and must be replaced", sign_in(am2, "amaka.eze", temp2).status_code == 302
      and "/platform/password" in am2.get("/platform", base_url=PL).headers["Location"])
check("restoring is logged", "platform_admin.restore" in log_actions("ada"))
check("restoring someone who is not removed is refused",
      "is not removed" in sup.post(f"/platform/team/{am_id}/restore", data={"_csrf_token": csrf(sup, "/platform/team")},
                                   base_url=PL, follow_redirects=True).get_data(as_text=True))

r = sup.post(f"/platform/team/{tunde_id}/reset-password", data={"_csrf_token": csrf(sup, "/platform/team")}, base_url=PL)
temp3 = temp_password(r)
check("the super admin can reset an admin's password",
      r.status_code == 200 and admin_row("tunde")["must_change"] == 1 and "platform_admin.password_reset" in log_actions("ada"))
check("…which locks out the old one", sign_in(client(), "tunde", "a-long-platform-password").status_code == 401
      and sign_in(client(), "tunde", temp3).status_code == 302)
check("the super admin's own password is not reset from the team page",
      "own Password page" in sup.post(f"/platform/team/{ada_id}/reset-password", data={"_csrf_token": csrf(sup, "/platform/team")},
                                      base_url=PL, follow_redirects=True).get_data(as_text=True))
sup.post(f"/platform/team/{am_id}/remove", data={"_csrf_token": csrf(sup, "/platform/team")}, base_url=PL)
r = sup.post(f"/platform/team/{am_id}/reset-password", data={"_csrf_token": csrf(sup, "/platform/team")},
             base_url=PL, follow_redirects=True)
check("a removed admin's password cannot be reset until they are restored",
      "restore them first" in r.get_data(as_text=True))

# the dashboard survives a school whose database cannot be reached
ghost_url = make_url(os.environ["BRIGHTSTARS_PLATFORM_DB"]).set(database="bs_test_no_such_database").render_as_string(hide_password=False)
with platform_session() as s:
    t = Tenant(slug="ghost", name="Ghost School", status="active", db_url=ghost_url, storage_key="ghost", created_at="2020-01-01T00:00:00+00:00")
    t.domains = [TenantDomain(hostname="ghost.portal.test", kind=DOMAIN_PORTAL, is_primary=1, created_at="2020-01-01T00:00:00+00:00")]
    s.add(t)
    s.commit()
page = sup.get("/platform", base_url=PL)
check("the dashboard still loads when one school's database is unreachable",
      page.status_code == 200 and "Ghost School" in page.get_data(as_text=True)
      and "could not be read" in page.get_data(as_text=True))
r = sup.post(f"/platform/team/{tunde_id}/remove", data={"_csrf_token": csrf(sup, "/platform/team")}, base_url=PL, follow_redirects=True)
with platform_session() as s_:
    ghost_id = s_.scalars(sa.select(Tenant.id).where(Tenant.slug == "ghost")).first()
    s_.add(PlatformAuditLog(platform_admin_id=am_id, actor_username="amaka.eze", tenant_id=ghost_id,
                            action="tenant.enter", detail="amaka.eze entered ghost",
                            created_at="2026-09-01T00:00:00+00:00"))
    s_.commit()
reachable = team.activity(am_id, "inside", 1, 1000)
check("a school whose audit trail cannot be read is reported, and does not hide the others",
      reachable["unreadable"] == ["ghost"] and any(r["slug"] == "alpha" for r in reachable["rows"]))
check("…and Everything reports it as well", team.activity(am_id, "all")["unreadable"] == ["ghost"])
check("…while the platform-only view never touches it", team.activity(am_id, "all", inside=False)["unreadable"] == [])
page = sup.get(f"/platform/activity?admin={am_id}&category=inside", base_url=PL)
check("the activity page says which school could not be read, and still loads",
      page.status_code == 200 and "could not be reached" in page.get_data(as_text=True)
      and "ghost" in page.get_data(as_text=True))
check("a page for an admin who has entered no school has nothing to report",
      team.activity(admin_row("second")["id"], "inside")["unreadable"] == []
      and team.activity(admin_row("second")["id"], "inside")["total"] == 0)
check("a category that reads no school report nothing unreadable",
      team.activity(am_id, "schools")["unreadable"] == [])

check("removing an admin still works when one school cannot be reached, and says which",
      admin_row("tunde")["active"] == 0 and "ghost" in r.get_data(as_text=True))

# ================================================================ the page for an address nobody owns
import html as _html  # noqa: E402

r = client().get("/login", base_url="http://localhost:5000")
body = r.get_data(as_text=True)
check("an unregistered address gets a designed page with the right status",
      r.status_code == 404 and r.mimetype == "text/html" and "We couldn't find that address" in _html.unescape(body))
check("…that says it is not registered, and names the address", "not registered" in body and "localhost" in body)
check("…and carries the Brightstars logo", "/static/brand/brightstars-logo.png" in body)
check("…and works with no stylesheet or script of its own to fetch", "<link rel=\"stylesheet\"" not in body and "<script" not in body)
r = client().get("/static/brand/brightstars-logo.png", base_url="http://localhost:5000")
check("the logo is served even on an address that belongs to nobody",
      r.status_code == 200 and r.mimetype == "image/png")
check("…but nothing else in static is", client().get("/static/admin.css", base_url="http://localhost:5000").status_code == 404
      and client().get("/static/uploads/x.png", base_url="http://localhost:5000").status_code == 404)
check("in development it points at the platform console and the school address form",
      "Development" in body and "platform.test" in body and "school-code" in body)
os.environ["BRIGHTSTARS_ENV"] = "production"
try:
    prod = client().get("/login", base_url="http://localhost:5000").get_data(as_text=True)
finally:
    os.environ["BRIGHTSTARS_ENV"] = "development"
check("in production that hint is never shown, so the console's address is not advertised",
      "Development" not in prod and "platform.test" not in prod)
from jinja2 import select_autoescape  # noqa: E402,F401
rendered = A.app.jinja_env.get_template("platform/notice.html").render(
    status=404, eyebrow="x", title="x", lead=None, host='"><script>alert(1)</script>', steps=[], dev=None)
check("a hostile hostname is shown as text", "<script>alert(1)</script>" not in rendered and "&lt;script&gt;" in rendered)
susp = client().get("/login", base_url="http://beta.portal.test")
sup.post("/platform/schools/beta/status", data={"_csrf_token": csrf(sup, "/platform/schools/beta"), "status": "suspended",
         "reason": "x"}, base_url=PL)
r = client().get("/login", base_url="http://beta.portal.test")
check("a suspended school gets a designed 503, not a blank line",
      r.status_code == 503 and "temporarily unavailable" in r.get_data(as_text=True).lower()
      and "brightstars-logo.png" in r.get_data(as_text=True))
r = client().get("/school/about", base_url="http://alpha.portal.test")
check("a page that does not exist gets the designed 404 too",
      r.status_code == 404 and "nothing at this address" in _html.unescape(r.get_data(as_text=True)).lower())
r = sup.get("/nope", base_url=PL)
check("…and so does an unknown path on the platform host", r.status_code == 404 and "brightstars-logo.png" in r.get_data(as_text=True))

# the sign-in page
page = client().get("/platform/login", base_url=PL).get_data(as_text=True)
check("the sign-in page shows the logo, and its form", "brightstars-logo.png" in page and 'name="username"' in page
      and 'name="password"' in page)
check("…and names no one who works there", "amaka" not in page.lower() and "tunde" not in page.lower())
r = sign_in(client(), "second", "wrong-again")
check("a failed sign-in explains itself without saying which half was wrong",
      r.status_code == 401 and "not recognised" in r.get_data(as_text=True))

dispose_engines()
DROP_TEST_DATABASES()
shutil.rmtree(TMP, ignore_errors=True)
print()
failed = [x for x in results if not x[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
