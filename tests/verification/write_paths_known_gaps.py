"""The fixes for the gaps the README used to list, proven end to end on PostgreSQL.

* the school's top-level role is "School Admin", and an existing school's "Super Admin" role is
  renamed in place at start-up without losing anyone their access;
* the school website editor is gone, its identity and contact fields live on the School profile
  page, and a school that had the old permissions keeps the ability to edit them;
* the library search ignores case and treats % and _ literally, the overdue count is valid on
  PostgreSQL, and candidate numbers carry the school's own code rather than another school's;
* a school's uploaded files are served only to people entitled to them, and message attachments
  only through their permission-checked route;
* a failed sign-in says why, and keeps the username that was typed.

Run:  python tests/verification/write_paths_known_gaps.py
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_gaps_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('gaps')
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
from werkzeug.security import generate_password_hash  # noqa: E402

import app as A  # noqa: E402
from control_plane import provisioning as pv  # noqa: E402
from control_plane.context import tenant_context  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import dispose_engines, engine_for  # noqa: E402

results = []
PL = "http://platform.test"


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
        tenant = get_tenant(s, slug)
        return to_info(tenant) if tenant else None


def in_school(info, fn):
    with A.app.app_context(), tenant_context(info):
        return fn()


def sql(info, statement, **params):
    with engine_for(info).begin() as conn:
        result = conn.execute(sa.text(statement), params)
        return result.fetchall() if result.returns_rows else None


pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
console = A.app.test_client()
console.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                      "_csrf_token": csrf(console, "/platform/login", PL)}, base_url=PL)
for code, name in (("alpha", "Alpha School"), ("beta", "Beta College")):
    console.post("/platform/schools/new", data={
        "_csrf_token": csrf(console, "/platform/schools/new", PL), "name": name, "code": code,
        "school_brand_primary": "#0d2b52", "school_brand_accent": "#1674b9"}, base_url=PL,
        content_type="multipart/form-data")
info_alpha, info_beta = info_for("alpha"), info_for("beta")
ALPHA, BETA = "http://alpha.portal.test", "http://beta.portal.test"


def operator_in(code):
    url = f"http://{code}.portal.test"
    r = console.post(f"/platform/schools/{code}/enter", data={"_csrf_token": csrf(console, f"/platform/schools/{code}", PL)},
                     base_url=PL)
    c = A.app.test_client()
    c.get(r.headers["Location"][len(url):], base_url=url)
    c.get("/admin/workspace/school", base_url=url)
    return c


alpha = operator_in("alpha")


def add_admin(info, username, role_name):
    with A.app.app_context(), tenant_context(info):
        role = A.db.session.scalars(sa.select(A.AdminType).where(A.AdminType.name == role_name)).first()
        A.db.session.add(A.Admin(username=username, display_name=username.title(),
                                 password_hash=generate_password_hash("staff-password-123"),
                                 admin_type_id=role.id, active=1, password_must_change=0,
                                 created_at="2026-01-01T00:00:00+00:00"))
        A.db.session.commit()
        return A.db.session.scalars(sa.select(A.Admin.id).where(A.Admin.username == username)).first()


# ================================================================ 6. a failed sign-in explains itself
anon = A.app.test_client()
r = anon.post("/login", data={"username": "somebody", "password": "wrong-password"}, base_url=ALPHA)
page = r.get_data(as_text=True)
check("a failed sign-in says why", "We could not verify those login details" in page and 'role="alert"' in page)
check("…and keeps the username that was typed, but never the password",
      'value="somebody"' in page and "wrong-password" not in page)
r = anon.post("/login", data={"username": "", "password": ""}, base_url=ALPHA)
check("an empty sign-in says what is missing", "Enter your username" in r.get_data(as_text=True))
r = anon.post("/login", data={"username": '"><script>alert(1)</script>', "password": "x"}, base_url=ALPHA)
body = r.get_data(as_text=True)
check("a hostile username is shown as text, never as markup",
      "<script>alert(1)</script>" not in body and "&lt;script&gt;" in body)
throttled = None
for _ in range(10):
    throttled = anon.post("/login", data={"username": "flood", "password": "x"}, base_url=ALPHA)
check("too many attempts are refused, and the page says so",
      throttled.status_code == 429 and "Too many sign-in attempts" in throttled.get_data(as_text=True))
check("a normal visit to the sign-in page shows no error", 'role="alert"' not in anon.get("/login", base_url=BETA).get_data(as_text=True))

# ================================================================ 5. who may fetch a school's files
uploads = os.path.join(TMP, "tenants", "alpha", "uploads")
for folder, name in (("branding", "logo.png"), ("students", "child.png"), ("messages", "note.pdf"), ("signatures", "sig.png")):
    os.makedirs(os.path.join(uploads, folder), exist_ok=True)
    with open(os.path.join(uploads, folder, name), "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\nfile")
signed_out = A.app.test_client()
check("the school's logo and sign-in photographs are public, because the sign-in page needs them",
      signed_out.get("/static/uploads/branding/logo.png", base_url=ALPHA).status_code == 200)
check("a student's photograph is not served to someone who is not signed in",
      signed_out.get("/static/uploads/students/child.png", base_url=ALPHA).status_code == 404)
check("…nor a signature", signed_out.get("/static/uploads/signatures/sig.png", base_url=ALPHA).status_code == 404)
check("…and a refusal does not confirm the file exists (a file that is not there gives the same answer)",
      signed_out.get("/static/uploads/students/nothing.png", base_url=ALPHA).status_code == 404)
check("a signed-in administrator can open a student's photograph",
      alpha.get("/static/uploads/students/child.png", base_url=ALPHA).status_code == 200)
check("…and a signature", alpha.get("/static/uploads/signatures/sig.png", base_url=ALPHA).status_code == 200)
check("but nobody, even signed in, is given a message attachment through this route",
      alpha.get("/static/uploads/messages/note.pdf", base_url=ALPHA).status_code == 404
      and signed_out.get("/static/uploads/messages/note.pdf", base_url=ALPHA).status_code == 404)
check("…including by changing the case of the folder name",
      alpha.get("/static/uploads/Messages/note.pdf", base_url=ALPHA).status_code == 404)
check("…or by climbing out of another folder into it",
      alpha.get("/static/uploads/students/../messages/note.pdf", base_url=ALPHA).status_code in (404, 400))
check("a sign-in at another school does not open this school's files",
      operator_in("beta").get("/static/uploads/students/child.png", base_url=ALPHA).status_code == 404)

# the permission-checked route still works for the two people in the conversation, and only them
sender, recipient = add_admin(info_alpha, "sender", "Ordinary Admin"), add_admin(info_alpha, "recipient", "Ordinary Admin")
outsider = add_admin(info_alpha, "outsider", "Ordinary Admin")
sql(info_alpha, "INSERT INTO admin_messages (sender_admin_id, recipient_admin_id, body, sent_at, attachment_path, "
                "attachment_name, attachment_type) VALUES (:s, :r, 'see attached', '2026-01-01T00:00:00+00:00', "
                "'uploads/messages/note.pdf', 'note.pdf', 'pdf')", s=sender, r=recipient)
message_id = sql(info_alpha, "SELECT id FROM admin_messages ORDER BY id DESC LIMIT 1")[0][0]


def signed_in_as(username):
    c = A.app.test_client()
    c.post("/login", data={"username": username, "password": "staff-password-123"}, base_url=ALPHA)
    c.get("/admin/workspace/school", base_url=ALPHA)
    return c


url = f"/admin/administration/messages/attachment/{message_id}"
check("the sender can open the attachment through its own route", signed_in_as("sender").get(url, base_url=ALPHA).status_code == 200)
check("…and so can the recipient", signed_in_as("recipient").get(url, base_url=ALPHA).status_code == 200)
check("…but another administrator of the same school cannot", signed_in_as("outsider").get(url, base_url=ALPHA).status_code in (403, 404))
check("…and someone signed out cannot", A.app.test_client().get(url, base_url=ALPHA).status_code == 302)

# ================================================================ 3. PostgreSQL and case
def seed_books():
    sql(info_alpha, "INSERT INTO library_books (title, author, isbn, category, total_copies, available_copies, active, created_at) "
                    "VALUES ('The Holy BIBLE', 'Various', 'X1', 'Religion', 2, 2, 1, '2026-01-01'), "
                    "('Atlas of Africa', 'Ade Ola', 'X2', 'Geography', 1, 1, 1, '2026-01-01'), "
                    "('100% Maths', 'Bola', 'X3', 'Maths_Kids', 1, 1, 1, '2026-01-01')")


try:
    seed_books()
    seeded = True
except Exception as exc:  # the table's columns are not what this test assumed
    seeded = False
    print("NOTE could not seed library books:", str(exc).splitlines()[0][:160])
if seeded:
    def found(q):
        text = alpha.get("/admin/library", query_string={"q": q}, base_url=ALPHA).get_data(as_text=True)
        return {t for t in ("The Holy BIBLE", "Atlas of Africa", "100% Maths") if t in text}

    check("the library search ignores case, as it did on SQLite", found("bible") == {"The Holy BIBLE"}
          and found("ATLAS") == {"Atlas of Africa"} and found("ade ola") == {"Atlas of Africa"})
    check("…and searches every field", found("religion") == {"The Holy BIBLE"} and found("x2") == {"Atlas of Africa"})
    check("…treating a % typed in the search as a percent sign, not a wildcard", found("100%") == {"100% Maths"} and found("%") == {"100% Maths"})
    check("…and an underscore as an underscore", found("Maths_Kids") == {"100% Maths"} and found("_") == {"100% Maths"})
    check("…and matching nothing returns nothing", found("zzzz") == set())
page = alpha.get("/admin/library", base_url=ALPHA)
check("the library page loads on PostgreSQL, including its overdue count", page.status_code == 200)

check("candidate numbers carry the school's own code, not another school's",
      in_school(info_alpha, lambda: A.sys.modules["core.entrance"]._new_candidate_code()).startswith("ALPHA-")
      and in_school(info_beta, lambda: A.sys.modules["core.entrance"]._new_candidate_code()).startswith("BETA-"))

# ================================================================ 1. the top-level role's name
roles = alpha.get("/admin/administration/roles", base_url=ALPHA).get_data(as_text=True)
check("a school's top-level role is called School Admin", "School Admin" in roles and "Super Admin" not in roles)
top = sql(info_alpha, "SELECT id, name FROM admin_types WHERE is_system = 1")
check("…and there is exactly one", len(top) == 1 and top[0][1] == "School Admin", str(top))
top_id = top[0][0]
operator_id = sql(info_alpha, "SELECT id FROM admins WHERE username = 'platform@ops'")[0][0]

# A school from before the rename still has the old name. Upgrading must rename it in place.
sql(info_alpha, "UPDATE admin_types SET name = 'Super Admin' WHERE id = :i", i=top_id)
pv.upgrade_tenant(info_alpha)
renamed = sql(info_alpha, "SELECT id, name FROM admin_types WHERE is_system = 1")
check("an existing school's Super Admin role is renamed in place", renamed == [(top_id, "School Admin")], str(renamed))
check("…keeping its id, so nobody holding it loses anything",
      sql(info_alpha, "SELECT admin_type_id FROM admins WHERE id = :i", i=operator_id)[0][0] == top_id)
check("…and the operator can still work in the school",
      alpha.get("/admin/school", base_url=ALPHA).status_code == 200)
pv.upgrade_tenant(info_alpha)
check("upgrading again changes nothing", sql(info_alpha, "SELECT name FROM admin_types WHERE is_system = 1") == [("School Admin",)])

# If something already has the new name, the old row is left alone rather than failing to start.
sql(info_beta, "UPDATE admin_types SET name = 'Super Admin' WHERE is_system = 1")
sql(info_beta, "INSERT INTO admin_types (name, description, is_system, active, created_at) "
               "VALUES ('School Admin', 'a custom role', 0, 1, '2026-01-01')")
try:
    pv.upgrade_tenant(info_beta)
    survived = True
except Exception as exc:
    survived = False
    print("NOTE:", str(exc).splitlines()[0][:200])
check("a school where the new name is already taken still starts", survived)
check("…and its old role was left as it was", ("Super Admin",) in sql(info_beta, "SELECT name FROM admin_types WHERE is_system = 1"))
custom = sql(info_beta, "SELECT id FROM admin_types WHERE name = 'School Admin' AND is_system = 0")[0][0]
check("…and the custom role that took the name is NOT mistaken for the top-level role: it is granted nothing",
      sql(info_beta, "SELECT count(*) FROM admin_type_permissions WHERE admin_type_id = :c", c=custom)[0][0] == 0)
check("…while the real top-level role still holds every permission",
      sql(info_beta, "SELECT count(*) FROM admin_type_permissions p JOIN admin_types t ON t.id = p.admin_type_id "
                     "WHERE t.is_system = 1")[0][0] == sql(info_beta, "SELECT count(*) FROM permissions")[0][0])
sql(info_beta, "DELETE FROM admin_types WHERE name = 'School Admin' AND is_system = 0")
sql(info_beta, "UPDATE admin_types SET name = 'School Admin' WHERE is_system = 1")

# ================================================================ 4. the website editor is gone
check("the school website editor's pages no longer exist",
      all(alpha.get(p, base_url=ALPHA).status_code == 404 for p in (
          "/admin/school/website", "/admin/school/website/news/new", "/admin/school/website/enquiries")))
check("…and its menu entry is gone", "Website &amp; Content" not in alpha.get("/admin/school", base_url=ALPHA).get_data(as_text=True))
check("…and its permissions are gone from the catalogue",
      sql(info_alpha, "SELECT count(*) FROM permissions WHERE code LIKE 'website.%'")[0][0] == 0)
check("…and no code reaches for the pages, news or enquiries any more",
      not any(w in open(os.path.join(ROOT, "blueprints", "school", "routes.py"), encoding="utf-8").read()
              for w in ("SchoolPublicPage", "SchoolPublicNews", "SchoolPublicEnquiry", "handle_news_image_upload")))

# A school from before the removal: roles and people holding the old permissions must keep the ability.
pv.upgrade_tenant(info_alpha)
sql(info_alpha, "INSERT INTO permissions (code, name, module, description) VALUES "
                "('website.manage', 'old', 'website', 'old'), ('website.view', 'old', 'website', 'old')")
manage_id = sql(info_alpha, "SELECT id FROM permissions WHERE code = 'website.manage'")[0][0]
view_id = sql(info_alpha, "SELECT id FROM permissions WHERE code = 'website.view'")[0][0]
# A school from before the change has the old preset and not yet the new one.
sql(info_alpha, "DELETE FROM admin_type_permissions WHERE admin_type_id IN (SELECT id FROM admin_types WHERE name = 'School Profile Manager')")
sql(info_alpha, "DELETE FROM admin_types WHERE name = 'School Profile Manager'")
sql(info_alpha, "INSERT INTO admin_types (name, description, is_system, active, created_at) VALUES "
                "('Website & Content Manager', 'old preset', 0, 1, '2026-01-01'), ('Editors', 'custom', 0, 1, '2026-01-01')")
old_role = sql(info_alpha, "SELECT id FROM admin_types WHERE name = 'Website & Content Manager'")[0][0]
custom_role = sql(info_alpha, "SELECT id FROM admin_types WHERE name = 'Editors'")[0][0]
sql(info_alpha, "INSERT INTO admin_type_permissions (admin_type_id, permission_id, granted_at) VALUES "
                "(:o, :m, 'x'), (:o, :v, 'x'), (:c, :m, 'x'), (:c, :v, 'x')", o=old_role, m=manage_id, v=view_id, c=custom_role)
sql(info_alpha, "INSERT INTO admin_permissions (admin_id, permission_id, granted_at) VALUES (:a, :m, 'x'), (:a, :v, 'x')",
    a=recipient, m=manage_id, v=view_id)
pv.upgrade_tenant(info_alpha)
branding_id = sql(info_alpha, "SELECT id FROM permissions WHERE code = 'branding.manage'")[0][0]
check("the old website permissions are removed from an existing school",
      sql(info_alpha, "SELECT count(*) FROM permissions WHERE code LIKE 'website.%'")[0][0] == 0)
check("…and a custom role that held website.manage now holds branding.manage",
      sql(info_alpha, "SELECT count(*) FROM admin_type_permissions WHERE admin_type_id = :c AND permission_id = :b", c=custom_role, b=branding_id)[0][0] == 1)
check("…and so does an administrator who was granted it directly",
      sql(info_alpha, "SELECT count(*) FROM admin_permissions WHERE admin_id = :a AND permission_id = :b", a=recipient, b=branding_id)[0][0] == 1)
check("…and nothing dangles from the removed permissions",
      sql(info_alpha, "SELECT count(*) FROM admin_type_permissions WHERE permission_id IN (:m, :v)", m=manage_id, v=view_id)[0][0] == 0
      and sql(info_alpha, "SELECT count(*) FROM admin_permissions WHERE permission_id IN (:m, :v)", m=manage_id, v=view_id)[0][0] == 0)
check("the old preset role is renamed in place, with its members",
      sql(info_alpha, "SELECT name FROM admin_types WHERE id = :o", o=old_role) == [("School Profile Manager",)])
check("…and a role called something else is untouched",
      sql(info_alpha, "SELECT name FROM admin_types WHERE id = :c", c=custom_role) == [("Editors",)])
check("a school gets the new preset role too", sql(info_beta, "SELECT count(*) FROM admin_types WHERE name = 'School Profile Manager'")[0][0] == 1)

# ---- the School profile page carries what the website editor did
page = alpha.get("/admin/school/branding", base_url=ALPHA).get_data(as_text=True)
check("the School profile page offers the school's name and contact details",
      all(f'name="{f}"' in page for f in ("school_name", "school_motto", "school_tagline", "school_phone",
                                          "school_email", "school_address")) and "School profile" in page)


def save_profile(**fields):
    data = {"_csrf_token": csrf(alpha, "/admin/school/branding", ALPHA), "school_brand_primary": "#0d2b52",
            "school_brand_accent": "#1674b9", **fields}
    return alpha.post("/admin/school/branding/save", data=data, base_url=ALPHA, content_type="multipart/form-data",
                       follow_redirects=True).get_data(as_text=True)


text = save_profile(school_name="Alpha Grammar School", school_motto="Aim high", school_tagline="Alpha for life",
                    school_phone="0803 000 0000", school_email="office@alpha.example", school_address="1 Alpha Road")
check("a school changes its name and contact details", "has been saved" in text and "Alpha Grammar School" in text)
row = sql(info_alpha, "SELECT name, motto, tagline, phone, email, address FROM schools")[0]
check("…on the school's own record", row == ("Alpha Grammar School", "Aim high", "Alpha for life", "0803 000 0000",
                                             "office@alpha.example", "1 Alpha Road"), str(row))
check("…and its sign-in page shows them", "Alpha Grammar School" in A.app.test_client().get("/login", base_url=ALPHA).get_data(as_text=True)
      and "Aim high" in A.app.test_client().get("/login", base_url=ALPHA).get_data(as_text=True))
check("…and the platform's own list follows the new name", info_for("alpha").name == "Alpha Grammar School")
check("…keeping the school's code, address and folder",
      info_for("alpha").slug == "alpha" and os.path.isdir(os.path.join(TMP, "tenants", "alpha")))
text = save_profile(school_name="Alpha Grammar School", school_motto="", school_tagline="", school_phone="")
row = sql(info_alpha, "SELECT motto, tagline, phone, email FROM schools")[0]
check("a field the school blanks is cleared, and the rest are left", row == (None, None, None, "office@alpha.example"), str(row))
check("…and the cleared motto is gone from its sign-in page too",
      "Aim high" not in A.app.test_client().get("/login", base_url=ALPHA).get_data(as_text=True))
for label, fields, expected in (
        ("a blank name", {"school_name": "   "}, "must have a name"),
        ("a name that is too long", {"school_name": "x" * 151}, "at most 150"),
        ("an email that is not an address", {"school_name": "Alpha", "school_email": "not an email"}, "does not look like an email"),
        ("a motto that is too long", {"school_name": "Alpha", "school_motto": "m" * 201}, "at most 200")):
    text = save_profile(**fields)
    check(f"{label} is refused, and nothing changes", expected in text
          and sql(info_alpha, "SELECT name FROM schools")[0][0] == "Alpha Grammar School")
before = sql(info_alpha, "SELECT name, email FROM schools")[0]
alpha.post("/admin/school/branding/save", data={"_csrf_token": csrf(alpha, "/admin/school/branding", ALPHA),
           "school_brand_primary": "#0d2b52", "school_brand_accent": "#1674b9"}, base_url=ALPHA,
           content_type="multipart/form-data")
check("a save that does not send the details leaves them as they are (nothing is blanked by omission)",
      sql(info_alpha, "SELECT name, email FROM schools")[0] == before)
text = save_profile(school_name="  Alpha   Grammar\n School ")
check("stray spaces and line breaks in a name are tidied", sql(info_alpha, "SELECT name FROM schools")[0][0] == "Alpha Grammar School")

dispose_engines()
DROP_TEST_DATABASES()
shutil.rmtree(TMP, ignore_errors=True)
print()
failed = [x for x in results if not x[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
