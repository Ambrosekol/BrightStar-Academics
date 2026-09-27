"""A school's look — brand colours, logo and sign-in photographs — end to end.

Drives the real platform console over HTTP: creating a school with several
photographs and its own colours, seeing them on that school's sign-in page and
nowhere else, changing them afterwards, and the ways it must refuse bad input.

The hostile cases matter as much as the happy path. A colour is written into a
<style> block on every page of a school's portal, so anything that is not a plain
#rrggbb value must never reach a page even if it got into the database. Nothing
may be left half-created when an image is rejected. And a school's uploaded files
must stay unreachable from the platform host except through the console's own
signed-in preview.

It also covers a school's own administrators making the same changes from their admin
area — the permission that guards it, the audit trail, and that one school's change can
never reach another's. And that the Brightstars logo appears on the console's pages and
never on a school's portal.

Run:  python tests/verification/write_paths_portal_branding.py
"""
import io
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_branding_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('branding')
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
from werkzeug.datastructures import FileStorage  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

import app as A  # noqa: E402
from control_plane import provisioning as pv  # noqa: E402
from control_plane.context import tenant_context  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import dispose_engines  # noqa: E402
from core import theme  # noqa: E402
from models import AuditLog, SchoolPublicSetting  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


def client(host):
    return A.app.test_client(), f"http://{host}"


def csrf(c, url, path):
    body = c.get(path, base_url=url).get_data(as_text=True)
    marker = 'name="_csrf_token" value="'
    start = body.index(marker) + len(marker)
    return body[start:body.index('"', start)]


def info_for(slug):
    with platform_session() as s:
        tenant = get_tenant(s, slug)
        return to_info(tenant) if tenant else None


def image(kind="png", pad=0, marker=0):
    """A file that passes the image-signature check, optionally padded to a size.
    ``marker`` makes otherwise identical images distinguishable."""
    head = {"png": bytes([137, 80, 78, 71, 13, 10, 26, 10]) + bytes([0, 0, 0, 13]) + b"IHDR"
                   + bytes([0, 0, 0, 1, 0, 0, 0, 1, 8, 6, 0, 0, 0]) + bytes(16),
            "jpg": b"\xff\xd8\xff\xe0" + bytes(16)}[kind]
    return head + bytes([marker]) * 8 + bytes(pad)


def upload(name, data=None, kind="png", pad=0, marker=0):
    return FileStorage(io.BytesIO(data if data is not None else image(kind, pad, marker)),
                       filename=name, content_type="image/" + kind)


def branding_dir(slug):
    return os.path.join(TMP, "tenants", slug, "uploads", "branding")


def files_in(slug):
    folder = branding_dir(slug)
    return sorted(os.listdir(folder)) if os.path.isdir(folder) else []


def create(c, url, **fields):
    token = csrf(c, url, "/platform/schools/new")
    data = {"_csrf_token": token, "school_brand_primary": theme.DEFAULT_PRIMARY,
            "school_brand_accent": theme.DEFAULT_ACCENT}
    data.update(fields)
    return c.post("/platform/schools/new", data=data, base_url=url, content_type="multipart/form-data")


def setting(slug, key):
    with A.app.app_context(), tenant_context(info_for(slug)):
        row = A.db.session.scalars(sa.select(SchoolPublicSetting).where(
            SchoolPublicSetting.setting_key == key)).first()
        return row.setting_value if row else None


pv.create_platform_admin("ops", "Ops Team", "a-long-platform-password")
c_pl, u_pl = client("platform.test")

# ------------------------------------------- the Brightstars logo, platform pages
login = c_pl.get("/platform/login", base_url=u_pl).get_data(as_text=True)
check("the console sign-in page shows the Brightstars logo", "brand/brightstars-logo.png" in login)
check("…and the star as its browser-tab icon", "brand/favicon-32.png" in login)
r = c_pl.get("/static/brand/brightstars-logo.png", base_url=u_pl)
check("the logo file is served on the platform host as a PNG",
      r.status_code == 200 and r.mimetype == "image/png" and r.data[:8] == b"\x89PNG\r\n\x1a\n")

token = csrf(c_pl, u_pl, "/platform/login")
c_pl.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                   "_csrf_token": token}, base_url=u_pl)
dash = c_pl.get("/platform", base_url=u_pl).get_data(as_text=True)
check("the signed-in console header shows the Brightstars logo", "brand/brightstars-logo.png" in dash)

# ------------------------------------- creating a school with photographs and colours
photos = [upload("front.jpg", kind="jpg", marker=1), upload("hall.png", marker=2),
          upload("field.jpg", kind="jpg", marker=3)]
r = create(c_pl, u_pl, name="Maroon Academy", code="maroon", school_motto="Ever upward",
           school_brand_primary="#7A1F3D", school_brand_accent="#0b6e4f",
           logo=upload("logo.png", marker=9), gallery=photos)
check("a school is created with a logo, three photographs and its own colours",
      r.status_code == 200 and "Maroon Academy is ready" in r.get_data(as_text=True)
      and info_for("maroon") is not None, str(r.status_code))
check("its colours are stored normalised",
      setting("maroon", theme.PRIMARY_KEY) == "#7a1f3d" and setting("maroon", theme.ACCENT_KEY) == "#0b6e4f",
      f"{setting('maroon', theme.PRIMARY_KEY)} {setting('maroon', theme.ACCENT_KEY)}")
stored = theme.parse_gallery(setting("maroon", theme.GALLERY_KEY))
check("all three photographs are recorded", len(stored) == 3, str(stored))
check("…and stored inside the school's own folder, alongside its logo",
      all(os.path.isfile(os.path.join(TMP, "tenants", "maroon", *p.split("/"))) for p in stored)
      and len(files_in("maroon")) == 4, str(files_in("maroon")))

c_m, u_m = client("maroon.portal.test")
page = c_m.get("/login", base_url=u_m).get_data(as_text=True)
check("the school's sign-in page shows every photograph",
      len(re.findall(r'class="login-slide"', page)) == 3 and all(p.split("/", 1)[1] in page for p in stored))
check("…and is coloured with the school's own colours",
      "--brand: #7a1f3d" in page and "--brand-accent: #0b6e4f" in page)
check("every page carries the school's theme so menus and buttons follow it",
      '<style id="school-theme">' in page and "--navy:#7a1f3d" in page and "--blue:#0b6e4f" in page)
check("the Brightstars logo never appears on a school's portal", "brightstars-logo" not in page)
# The colours must reach the pages behind the sign-in too, where the menus and buttons are.
token = csrf(c_pl, u_pl, "/platform/schools/maroon")
r = c_pl.post("/platform/schools/maroon/enter", data={"_csrf_token": token}, base_url=u_pl)
c_m.get(r.headers["Location"][len("http://maroon.portal.test"):], base_url=u_m)
admin_page = c_m.get("/admin/workspace/school", base_url=u_m, follow_redirects=True)
admin_html = admin_page.get_data(as_text=True)
check("the school's admin area carries its colours as well",
      admin_page.status_code == 200 and '<style id="school-theme">' in admin_html
      and "--navy:#7a1f3d" in admin_html and admin_html.index("school-theme") < admin_html.index("<body"),
      str(admin_page.status_code))
check("…and shows the school's own logo, not the platform's",
      "maroon_logo_" in admin_html and "brightstars-logo" not in admin_html,
      f"school logo: {'maroon_logo_' in admin_html}, platform logo: {'brightstars-logo' in admin_html}; img: {re.findall(r'<img[^>]*>', admin_html)[:3]}")
c_m.get("/logout", base_url=u_m)
served = [c_m.get("/static/" + p, base_url=u_m) for p in stored]
check("the photographs are served to visitors of that school's portal",
      all(r.status_code == 200 and r.mimetype.startswith("image/") for r in served),
      str([r.status_code for r in served]))
for r in served:
    r.close()  # a served file stays open until its response is closed, and Windows will not delete an open file
check("but a school's uploads are not served from the platform host",
      c_pl.get("/static/" + stored[0], base_url=u_pl).status_code == 404)

# A request carrying a logo and several photographs is larger than the
# application-wide limit, which is sized for one photo.
big = create(c_pl, u_pl, name="Big Photos", code="bigphotos",
             logo=upload("logo.png", pad=1_000_000, marker=1),
             gallery=[upload(f"p{i}.jpg", kind="jpg", pad=2_900_000, marker=i) for i in range(3)])
check("a logo plus photographs totalling more than the ordinary request limit is accepted",
      info_for("bigphotos") is not None and len(files_in("bigphotos")) == 4,
      f"{big.status_code} {files_in('bigphotos')}")
r = c_pl.post("/platform/login", data={"username": "x" * 9_000_000}, base_url=u_pl)
check("…while every other request keeps the ordinary limit", r.status_code == 413, str(r.status_code))

# ------------------------------------ a school that chose nothing looks as before
r = create(c_pl, u_pl, name="Plain School", code="plain")
plain = client("plain.portal.test")
page = plain[0].get("/login", base_url=plain[1]).get_data(as_text=True)
check("a school that picks no colours and no photographs gets neither",
      info_for("plain") is not None and "school-theme" not in page and 'class="login-slide"' not in page
      and "--brand: #0d2b52" in page and setting("plain", theme.PRIMARY_KEY) is None,
      f'{setting("plain", theme.PRIMARY_KEY)}')

# ------------------------------------------------------- refusing bad choices
def refused(name, expect, code, **fields):
    r = create(c_pl, u_pl, name=name, code=code, **fields)
    text = r.get_data(as_text=True)
    check(f"{name}: refused with a readable message", expect in text, text[text.find("errors"):][:200])
    check(f"{name}: …and nothing was created",
          info_for(code) is None and not os.path.isdir(os.path.join(TMP, "tenants", code)),
          str(info_for(code)))


refused("Too Light", "too light", "toolight", school_brand_primary="#ffee88")
refused("Light Accent", "accent colour is too light", "lightaccent", school_brand_accent="#ccffcc")
refused("Bad Colour", "is not a colour", "badcolour", school_brand_primary="red;}body{display:none")
refused("Fake Photo", "notes.png", "fakephoto",
        gallery=[upload("ok.png", marker=1), upload("notes.png", data=b"this is not an image")])
refused("Wrong Type", "valid image", "wrongtype", gallery=[upload("script.png", data=b"<script>alert(1)</script>")])
refused("Too Many", "at most 8", "toomany",
        gallery=[upload(f"p{i}.png", marker=i) for i in range(9)])

# ------------------------------------------- a bad colour in the database is never printed
with A.app.app_context(), tenant_context(info_for("plain")):
    A.db.session.add(SchoolPublicSetting(setting_key=theme.PRIMARY_KEY,
                                         setting_value="red;}body{display:none", updated_at="now"))
    A.db.session.commit()
page = plain[0].get("/login", base_url=plain[1]).get_data(as_text=True)
check("a malformed stored colour never reaches a page",
      "display:none" not in page and "school-theme" not in page and "--brand: #0d2b52" in page)
with A.app.app_context(), tenant_context(info_for("plain")):
    A.db.session.execute(sa.delete(SchoolPublicSetting).where(SchoolPublicSetting.setting_key == theme.PRIMARY_KEY))
    A.db.session.commit()

# ----------------------------------------------- changing a school's look later
page = c_pl.get("/platform/schools/maroon", base_url=u_pl).get_data(as_text=True)
check("the school's page in the console lists its photographs to manage",
      page.count('name="remove_photo"') == 3 and 'value="#7a1f3d"' in page and "Save branding" in page)
first = stored[0].rsplit("/", 1)[-1]
r = c_pl.get(f"/platform/schools/maroon/branding/{first}", base_url=u_pl)
check("a platform admin can preview an uploaded photograph",
      r.status_code == 200 and r.mimetype.startswith("image/"))
r.close()

token = csrf(c_pl, u_pl, "/platform/schools/maroon")
r = c_pl.post("/platform/schools/maroon/branding", data={
    "_csrf_token": token, "school_brand_primary": "#123456", "school_brand_accent": "#0b6e4f",
    "remove_photo": first,
    "gallery": [upload("new1.png", marker=11), upload("new2.jpg", kind="jpg", marker=12)]},
    base_url=u_pl, content_type="multipart/form-data", follow_redirects=True)
now = theme.parse_gallery(setting("maroon", theme.GALLERY_KEY))
check("removing one photograph and adding two leaves four",
      len(now) == 4 and stored[0] not in now and all(p in now for p in stored[1:]), str(now))
check("the removed photograph is deleted from the school's folder, the new ones are saved",
      first not in files_in("maroon") and len(files_in("maroon")) == 5, str(files_in("maroon")))
check("the new main colour is what the portal now shows",
      "--brand: #123456" in c_m.get("/login", base_url=u_m).get_data(as_text=True))
check("the school's other details were left alone by the change",
      setting("maroon", "school_motto") == "Ever upward" and setting("maroon", "school_name") == "Maroon Academy")

# A photograph that is still being downloaded cannot be deleted on Windows. The change is
# already saved by then, so that must never turn a successful save into an error page.
busy_name = now[1].rsplit("/", 1)[-1]
busy = c_pl.get(f"/platform/schools/maroon/branding/{busy_name}", base_url=u_pl)
token = csrf(c_pl, u_pl, "/platform/schools/maroon")
r = c_pl.post("/platform/schools/maroon/branding", data={
    "_csrf_token": token, "school_brand_primary": "#123456", "school_brand_accent": "#0b6e4f",
    "remove_photo": busy_name}, base_url=u_pl, follow_redirects=True)
check("removing a photograph that is mid-download still saves, rather than failing",
      r.status_code == 200 and "has been updated" in r.get_data(as_text=True)
      and now[1] not in theme.parse_gallery(setting("maroon", theme.GALLERY_KEY)),
      str(r.status_code))
busy.close()
now = theme.parse_gallery(setting("maroon", theme.GALLERY_KEY))

token = csrf(c_pl, u_pl, "/platform/schools/maroon")
c_pl.post("/platform/schools/maroon/branding", data={
    "_csrf_token": token, "school_brand_primary": "#123456", "school_brand_accent": "#0b6e4f",
    "gallery": [upload("replacement.png", marker=30)]}, base_url=u_pl, content_type="multipart/form-data")
before = files_in("maroon")
token = csrf(c_pl, u_pl, "/platform/schools/maroon")
r = c_pl.post("/platform/schools/maroon/branding", data={
    "_csrf_token": token, "school_brand_primary": "#123456", "school_brand_accent": "#0b6e4f",
    "gallery": [upload(f"x{i}.png", marker=20 + i) for i in range(5)]},
    base_url=u_pl, content_type="multipart/form-data", follow_redirects=True)
check("adding more than the gallery holds is refused with a message",
      "at most 8" in r.get_data(as_text=True))
check("…and nothing was saved or left behind",
      files_in("maroon") == before and len(theme.parse_gallery(setting("maroon", theme.GALLERY_KEY))) == 4)

token = csrf(c_pl, u_pl, "/platform/schools/maroon")
r = c_pl.post("/platform/schools/maroon/branding", data={
    "_csrf_token": token, "school_brand_primary": "#f5f5f5", "school_brand_accent": "#0b6e4f"},
    base_url=u_pl, follow_redirects=True)
check("a colour that is too light is refused when editing too",
      "too light" in r.get_data(as_text=True)
      and "--brand: #123456" in c_m.get("/login", base_url=u_m).get_data(as_text=True))

token = csrf(c_pl, u_pl, "/platform/schools/maroon")
c_pl.post("/platform/schools/maroon/branding", data={
    "_csrf_token": token, "school_brand_primary": theme.DEFAULT_PRIMARY,
    "school_brand_accent": theme.DEFAULT_ACCENT}, base_url=u_pl)
page = c_m.get("/login", base_url=u_m).get_data(as_text=True)
check("choosing the portal's own colours removes the school's choice",
      setting("maroon", theme.PRIMARY_KEY) is None and setting("maroon", theme.ACCENT_KEY) is None
      and "school-theme" not in page and "--brand: #0d2b52" in page)
check("…and its photographs are untouched by that",
      len(re.findall(r'class="login-slide"', page)) == 4)

# ------------------------------------------------------------------ access control
check("changing branding needs a CSRF token",
      c_pl.post("/platform/schools/maroon/branding", data={"school_brand_primary": "#123456"},
                base_url=u_pl).status_code == 403)
anon, u_anon = client("platform.test")
r = anon.post("/platform/schools/maroon/branding", data={"school_brand_primary": "#123456"}, base_url=u_anon)
check("changing branding needs a platform sign-in", r.status_code == 302 and "/platform/login" in r.headers["Location"])
r = anon.get(f"/platform/schools/maroon/branding/{first}", base_url=u_anon)
check("previewing a school's photographs needs a platform sign-in",
      r.status_code == 302 and "/platform/login" in r.headers["Location"])
check("neither exists on a school's own address",
      c_m.get(f"/platform/schools/maroon/branding/{now[0].rsplit('/', 1)[-1]}", base_url=u_m).status_code == 404
      and c_m.post("/platform/schools/maroon/branding", data={}, base_url=u_m).status_code == 404)
name = now[0].rsplit("/", 1)[-1]
for label, path in (("a path that climbs out of the folder", "..%2f..%2fdata%2fx.png"),
                    ("a file that is not an image type", "notes.txt"),
                    ("an SVG, which can carry script", "logo.svg"),
                    ("a name that was never uploaded", "nothing_here.png")):
    check(f"the preview refuses {label}",
          c_pl.get(f"/platform/schools/maroon/branding/{path}", base_url=u_pl).status_code == 404)
check("the preview of an unknown school is a 404",
      c_pl.get(f"/platform/schools/ghost/branding/{name}", base_url=u_pl).status_code == 404)
check("one school cannot be shown another's photograph through its own name",
      c_pl.get(f"/platform/schools/plain/branding/{name}", base_url=u_pl).status_code == 404)

# ======================================= a school changing its own look (school admin area)
create(c_pl, u_pl, name="Self Serve", code="selfserve")
token = csrf(c_pl, u_pl, "/platform/schools/selfserve")
r = c_pl.post("/platform/schools/selfserve/enter", data={"_csrf_token": token}, base_url=u_pl)
c_s, u_s = client("selfserve.portal.test")
c_s.get(r.headers["Location"][len("http://selfserve.portal.test"):], base_url=u_s)
c_s.get("/admin/workspace/school", base_url=u_s)

home = c_s.get("/admin/school", base_url=u_s).get_data(as_text=True)
check("the school's admin area offers a Branding page in its menu",
      "/admin/school/branding" in home and "<span>School profile</span>" in home)
page = c_s.get("/admin/school/branding", base_url=u_s)
html = page.get_data(as_text=True)
check("the Branding page loads with colours, logo and photographs to manage",
      page.status_code == 200 and "Save changes" in html and 'name="school_brand_primary"' in html
      and 'name="gallery"' in html and 'name="logo"' in html, str(page.status_code))


def save_branding(client_, url, **fields):
    data = {"_csrf_token": csrf(client_, url, "/admin/school/branding"),
            "school_brand_primary": theme.DEFAULT_PRIMARY, "school_brand_accent": theme.DEFAULT_ACCENT}
    data.update(fields)
    return client_.post("/admin/school/branding/save", data=data, base_url=url,
                        content_type="multipart/form-data", follow_redirects=True)


# Together larger than the ordinary request limit: this only works if the limit is raised
# before any request hook reads the form.
r = save_branding(c_s, u_s, school_brand_primary="#7A1F3D", school_brand_accent="#0b6e4f",
                  logo=upload("logo.png", pad=1_000_000, marker=5),
                  gallery=[upload(f"s{i}.jpg", kind="jpg", pad=2_900_000, marker=40 + i) for i in range(3)])
check("a school admin can save colours, a logo and photographs together, even above the ordinary limit",
      r.status_code == 200 and "Your school branding has been saved" in r.get_data(as_text=True),
      str(r.status_code))
mine = theme.parse_gallery(setting("selfserve", theme.GALLERY_KEY))
check("…they are stored in that school's own folder",
      len(mine) == 3 and len(files_in("selfserve")) == 4 and setting("selfserve", theme.PRIMARY_KEY) == "#7a1f3d",
      str(files_in("selfserve")))
page = client("selfserve.portal.test")
login_html = page[0].get("/login", base_url=page[1]).get_data(as_text=True)
check("…and its sign-in page immediately shows them",
      len(re.findall(r'class="login-slide"', login_html)) == 3 and "--brand: #7a1f3d" in login_html)
check("…without touching any other school",
      setting("maroon", theme.PRIMARY_KEY) is None and setting("plain", theme.GALLERY_KEY) is None
      and files_in("plain") == [])

r = save_branding(c_s, u_s, school_brand_primary="#f0f0f0")
check("a colour that is too light is refused with a message",
      "too light" in r.get_data(as_text=True) and setting("selfserve", theme.PRIMARY_KEY) == "#7a1f3d")
r = save_branding(c_s, u_s, school_brand_primary="red;}body{display:none")
check("a value that is not a colour is refused", "is not a colour" in r.get_data(as_text=True)
      and setting("selfserve", theme.PRIMARY_KEY) == "#7a1f3d")
before = files_in("selfserve")
r = save_branding(c_s, u_s, school_brand_primary="#7a1f3d",
                  gallery=[upload("real.png", marker=60), upload("fake.png", data=b"<html>not an image")])
check("a file that is not an image is refused, naming the file, and nothing is saved",
      "fake.png" in r.get_data(as_text=True) and files_in("selfserve") == before)
r = save_branding(c_s, u_s, school_brand_primary="#7a1f3d",
                  gallery=[upload(f"z{i}.png", marker=70 + i) for i in range(6)])
check("more photographs than the gallery holds are refused, and nothing is saved",
      "at most 8" in r.get_data(as_text=True) and files_in("selfserve") == before
      and len(theme.parse_gallery(setting("selfserve", theme.GALLERY_KEY))) == 3)

gone = mine[0].rsplit("/", 1)[-1]
r = save_branding(c_s, u_s, school_brand_primary="#7a1f3d", school_brand_accent="#0b6e4f", remove_photo=gone)
check("a school admin can remove a photograph, which is deleted from its folder",
      gone not in files_in("selfserve") and len(theme.parse_gallery(setting("selfserve", theme.GALLERY_KEY))) == 2)
r = save_branding(c_s, u_s, remove_photo="../../../plain/uploads/branding/anything.png")
check("a removal can only name one of the school's own photographs",
      len(theme.parse_gallery(setting("selfserve", theme.GALLERY_KEY))) == 2)
r = save_branding(c_s, u_s)
check("choosing the portal's own colours clears the school's choice, keeping its photographs",
      setting("selfserve", theme.PRIMARY_KEY) is None
      and len(theme.parse_gallery(setting("selfserve", theme.GALLERY_KEY))) == 2)
check("no CSRF token, no change",
      c_s.post("/admin/school/branding/save", data={"school_brand_primary": "#7a1f3d"},
               base_url=u_s).status_code == 403 and setting("selfserve", theme.PRIMARY_KEY) is None)

with A.app.app_context(), tenant_context(info_for("selfserve")):
    recorded = A.db.session.scalars(sa.select(AuditLog).where(
        AuditLog.action == "school_branding_updated")).all()
check("every branding change is recorded in the school's audit log", len(recorded) >= 3, str(len(recorded)))

# --- who may do it: the branding.manage permission, not just being an administrator
with A.app.app_context(), tenant_context(info_for("selfserve")):
    ordinary = A.db.session.scalars(sa.select(A.AdminType).where(A.AdminType.name == "Ordinary Admin")).first()
    A.db.session.add(A.Admin(username="clerk", display_name="Clerk",
                             password_hash=generate_password_hash("clerk-password-123"),
                             admin_type_id=ordinary.id, active=1, password_must_change=0,
                             created_at="2026-01-01T00:00:00+00:00"))
    permission = A.db.session.scalars(sa.select(A.Permission).where(A.Permission.code == "branding.manage")).first()
    permission_id, ordinary_id = permission.id if permission else None, ordinary.id
    A.db.session.commit()
check("the branding.manage permission exists in every school", permission_id is not None)

c_clerk, u_clerk = client("selfserve.portal.test")
# A real GET first lets the tenant boundary stamp this brand-new client's session with this
# school's own tenant_id; only then does a token planted straight into the session (rather than
# scraped from a page) survive to the next request instead of being wiped as a foreign cookie.
c_clerk.get("/login", base_url=u_clerk)
with c_clerk.session_transaction(base_url=u_clerk) as sess:
    sess["_csrf_token"] = "t" * 32
c_clerk.post("/login", data={"username": "clerk", "password": "clerk-password-123", "_csrf_token": "t" * 32}, base_url=u_clerk)
c_clerk.get("/admin/workspace/school", base_url=u_clerk)
with c_clerk.session_transaction(base_url=u_clerk) as sess:
    sess["_csrf_token"] = "t" * 32
r = c_clerk.get("/admin/school/branding", base_url=u_clerk)
check("an administrator without the permission cannot open the page",
      r.status_code != 200 and "Save changes" not in r.get_data(as_text=True), str(r.status_code))
r = c_clerk.post("/admin/school/branding/save", data={"_csrf_token": "t" * 32,
                 "school_brand_primary": "#123456"}, base_url=u_clerk, content_type="multipart/form-data")
check("…nor change the branding, even with a valid form token",
      r.status_code != 302 and setting("selfserve", theme.PRIMARY_KEY) is None, str(r.status_code))
check("…and the menu does not offer it to them",
      "<span>School profile</span>" not in c_clerk.get("/admin/school", base_url=u_clerk).get_data(as_text=True))

with A.app.app_context(), tenant_context(info_for("selfserve")):
    A.db.session.add(A.AdminTypePermission(admin_type_id=ordinary_id, permission_id=permission_id,
                                           granted_at="2026-01-01T00:00:00+00:00"))
    A.db.session.commit()
r = c_clerk.get("/admin/school/branding", base_url=u_clerk)
check("once a role is granted branding.manage, its holders can use the page",
      r.status_code == 200 and "Save changes" in r.get_data(as_text=True), str(r.status_code))
r = c_clerk.post("/admin/school/branding/save", data={"_csrf_token": "t" * 32,
                 "school_brand_primary": "#123456", "school_brand_accent": theme.DEFAULT_ACCENT},
                 base_url=u_clerk, content_type="multipart/form-data")
check("…and change the branding", r.status_code == 302 and setting("selfserve", theme.PRIMARY_KEY) == "#123456")

# A school that existed before this permission was introduced picks it up when the application
# next starts (the same upgrade runs for every school), and its top-level admin holds it.
with A.app.app_context(), tenant_context(info_for("selfserve")):
    A.db.session.execute(sa.delete(A.AdminTypePermission).where(A.AdminTypePermission.permission_id == permission_id))
    A.db.session.execute(sa.delete(A.Permission).where(A.Permission.id == permission_id))
    A.db.session.commit()
pv.upgrade_tenant(info_for("selfserve"))
with A.app.app_context(), tenant_context(info_for("selfserve")):
    restored = A.db.session.scalars(sa.select(A.Permission).where(A.Permission.code == "branding.manage")).first()
    top = A.db.session.scalars(sa.select(A.AdminType.id).where(A.AdminType.is_system == 1)).first()
    held = restored is not None and A.db.session.scalars(sa.select(A.AdminTypePermission).where(
        A.AdminTypePermission.admin_type_id == top,
        A.AdminTypePermission.permission_id == restored.id)).first() is not None
    # Nobody else is silently given it: only the top-level role, and the preset role that exists
    # for exactly this (which starts with no members).
    spread = restored is not None and sorted(A.db.session.scalars(sa.select(A.AdminType.name).where(
        A.AdminType.id.in_(sa.select(A.AdminTypePermission.admin_type_id).where(
            A.AdminTypePermission.permission_id == restored.id)))).all())
check("an existing school gains the permission on upgrade, held by its top-level administrator",
      held)
check("…and the upgrade grants it to no other role than the profile-manager preset",
      spread == sorted([A.SCHOOL_ADMIN_ROLE, "School Profile Manager"]), str(spread))

anon, u_anon = client("selfserve.portal.test")
check("signed-out visitors cannot reach the page or save",
      anon.get("/admin/school/branding", base_url=u_anon).status_code == 302
      and anon.post("/admin/school/branding/save", data={"school_brand_primary": "#123456"},
                    base_url=u_anon).status_code in (302, 403))
check("the page does not exist on the platform host",
      c_pl.get("/admin/school/branding", base_url=u_pl).status_code == 404)

dispose_engines()
DROP_TEST_DATABASES()
shutil.rmtree(TMP, ignore_errors=True)
print()
failed = [x for x in results if not x[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
