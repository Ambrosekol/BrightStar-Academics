"""A candidate's official result image and share page carry the right school, and only that school.

The result image used to be built from a logo file that belonged to the first school and from a
candidate photograph looked up in the project's own static folder, failed outright when either was
missing, and left the finished picture in a public folder shared by every school. This checks that:

  * the image is drawn with THIS school's logo and THIS candidate's photograph, and with nothing
    that belongs to another school or to the platform;
  * a school without a logo, and a candidate without a photograph, still get a result (the school's
    name stands where the logo would be);
  * a photograph path that tries to reach another school's files (or anywhere outside the school's
    own uploads folder) is ignored;
  * the finished picture is made in the school's own private folder, sent, and deleted: nothing is
    left behind, and nothing is ever put in a public folder;
  * one school cannot ask for another school's candidate;
  * a server with no Chrome says so with a plain error page, and never reveals paths.

Chrome itself is stood in for at the last step (the program that turns the page into a PNG), so the
test sees exactly which page it would draw, and needs no browser installed.

Run:  python tests/verification/write_paths_candidate_results.py
"""
import base64
import io
import os
import re
import shutil
import struct
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_results_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('results')
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
from PIL import Image  # noqa: E402

import app as A  # noqa: E402
import core.entrance as E  # noqa: E402
from control_plane import provisioning as pv  # noqa: E402
from control_plane.context import tenant_context  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import dispose_engines  # noqa: E402
from models import Attempt, Candidate, CandidatePaper, Examination  # noqa: E402

results = []
PL = "http://platform.test"
ALPHA, BETA, GAMMA = "http://alpha.portal.test", "http://beta.portal.test", "http://gamma.portal.test"
OLD_SCHOOL_WORDS = ("CREATIVE", "RAINBOW", "MONTESSORI")


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


def picture(rgb, size=(90, 40)):
    buf = io.BytesIO()
    Image.new("RGB", size, rgb).save(buf, "PNG")
    return buf.getvalue()


ALPHA_LOGO, BETA_LOGO = picture((200, 30, 30)), picture((30, 60, 200))
ALPHA_PHOTO, BETA_PHOTO = picture((20, 160, 60), (60, 70)), picture((120, 30, 160), (60, 70))


def data_uri(png):
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def csrf(c, path, base):
    body = c.get(path, base_url=base).get_data(as_text=True)
    marker = 'name="_csrf_token" value="'
    start = body.index(marker) + len(marker)
    return body[start:body.index('"', start)]


def info_for(slug):
    with platform_session() as s:
        tenant = get_tenant(s, slug)
        return to_info(tenant) if tenant else None


def in_school(slug, fn):
    with A.app.app_context(), tenant_context(info_for(slug)):
        return fn()


# ---------------------------------------------------------------- Chrome, stood in for at the last step
CAPTURED = []       # the pages Chrome was asked to draw
NEW_FILES = []      # every file made in the school's folder while it "drew"


def fake_png(path, size=640):
    """A real, valid PNG large enough to pass the size checks the code makes on Chrome's output."""
    raw = b"".join(b"\x00" + os.urandom(size * 3) for _ in range(size))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b""))


class _Done:
    returncode, stdout, stderr = 0, "", ""


def fake_run(command, **kwargs):
    screenshot = next(c.split("=", 1)[1] for c in command if c.startswith("--screenshot="))
    page = command[-1]
    html_file = page[len("file:///"):] if page.startswith("file:///") else page
    CAPTURED.append(open(html_file, encoding="utf-8").read())
    fake_png(screenshot)
    return _Done()


import subprocess  # noqa: E402

subprocess.run = fake_run
os.environ["BRIGHTSTARS_CHROME"] = sys.executable   # any file that exists: nothing real is launched

# ---------------------------------------------------------------- three schools
pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
console = A.app.test_client()
console.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                      "_csrf_token": csrf(console, "/platform/login", PL)}, base_url=PL)
for code, name, logo in (("alpha", "Alpha School", ALPHA_LOGO), ("beta", "Beta College", BETA_LOGO), ("gamma", "Gamma Academy", None)):
    form = {"_csrf_token": csrf(console, "/platform/schools/new", PL), "name": name, "code": code,
            "school_motto": f"Motto of {name}", "starter_banks": "1"}
    if logo:
        form["logo"] = (io.BytesIO(logo), "logo.png")
    console.post("/platform/schools/new", data=form, base_url=PL, content_type="multipart/form-data")


def operator(code, base):
    r = console.post(f"/platform/schools/{code}/enter",
                     data={"_csrf_token": csrf(console, f"/platform/schools/{code}", PL)}, base_url=PL)
    c = A.app.test_client()
    c.get(r.headers["Location"][len(base):], base_url=base)
    c.get("/admin/workspace/entrance", base_url=base)
    return c


admins = {"alpha": operator("alpha", ALPHA), "beta": operator("beta", BETA), "gamma": operator("gamma", GAMMA)}
BASES = {"alpha": ALPHA, "beta": BETA, "gamma": GAMMA}

for code in admins:
    # (the token belongs to the session, so any form page will do)
    r = admins[code].post("/admin/entrance-config/standard",
                          data={"_csrf_token": csrf(admins[code], "/admin/candidates/new", BASES[code])}, base_url=BASES[code])
    check(f"(set-up) {code} sets up the standard entrance papers", r.status_code == 302, str(r.status_code))


def register(code, name, photo=None):
    """Register a candidate through the real admin form, with a photograph if one is given."""
    c, base = admins[code], BASES[code]
    form = {"_csrf_token": csrf(c, "/admin/candidates/new", base), "candidate_name": name, "target_class": "JSS 1",
            "school_attended": "Sunrise Primary", "parent_guardian_name": "Mrs Parent",
            "parent_guardian_relationship": "Mother", "primary_mobile": "08030000001"}
    if photo:
        form["photo"] = (io.BytesIO(photo), "photo.png")
    r = c.post("/admin/candidates/new", data=form, base_url=base, content_type="multipart/form-data")
    text = r.get_data(as_text=True)
    if not (r.status_code == 200 and "Registered Successfully" in text):
        flat = re.sub(r"<[^>]+>", " ", text)
        print("   registration refused:", r.status_code, re.sub(r"\s+", " ", " ".join(re.findall(r"[^.]*(?:required|valid|not available|must)[^.]*\.", flat)))[:400])
        return False
    return True


def candidate_id(code, name):
    return in_school(code, lambda: A.db.session.scalar(sa.select(Candidate.id).where(Candidate.candidate_name == name)))


def Candidate_exists(cid):
    return A.db.session.scalar(sa.select(Candidate.id).where(Candidate.id == cid)) is not None


def complete_all_papers(code, cid):
    """Three submitted papers for the candidate, so the result is 'completed'."""
    def go():
        name = A.db.session.scalar(sa.select(Candidate.candidate_name).where(Candidate.id == cid))
        for paper in A.db.session.scalars(sa.select(CandidatePaper).where(CandidatePaper.candidate_id == cid)).all():
            exam = A.db.session.scalar(sa.select(Examination.id).where(Examination.bank_id == paper.bank_id))
            A.db.session.add(Attempt(candidate=name, exam_id=exam, bank_id=paper.bank_id, candidate_id=cid,
                                       started_at="2026-01-01T09:00:00+00:00", expires_at="2026-01-01T10:00:00+00:00",
                                       submitted_at="2026-01-01T09:40:00+00:00", score=80.0, max_score=100.0,
                                       percentage=80.0, status="submitted"))
        A.db.session.commit()
    in_school(code, go)


def set_photo_path(code, cid, path):
    def go():
        A.db.session.execute(sa.update(Candidate).where(Candidate.id == cid).values(photo_path=path))
        A.db.session.commit()
    in_school(code, go)


def result_image(code, cid):
    return admins[code].get(f"/admin/candidates/{cid}/result/image", base_url=BASES[code])


def tenant_files(code, sub=""):
    root = os.path.join(TMP, "tenants", code, sub)
    return sorted(os.path.relpath(os.path.join(b, n), root) for b, _, names in os.walk(root) for n in names) if os.path.isdir(root) else []


check("(set-up) alpha registers a candidate with a photograph", register("alpha", "Ada Photo", ALPHA_PHOTO))
check("(set-up) beta registers one with a different photograph", register("beta", "Bola Photo", BETA_PHOTO))
check("(set-up) alpha registers one with no photograph", register("alpha", "Ada Nophoto"))
check("(set-up) gamma, which has no logo, registers one", register("gamma", "Gina Nologo"))
ids = {name: candidate_id(code, name) for code, name in (("alpha", "Ada Photo"), ("beta", "Bola Photo"), ("alpha", "Ada Nophoto"), ("gamma", "Gina Nologo"))}
for (code, name) in (("alpha", "Ada Photo"), ("beta", "Bola Photo"), ("alpha", "Ada Nophoto"), ("gamma", "Gina Nologo")):
    complete_all_papers(code, ids[name])

check("the platform ships no logo file that a school's result could fall back to",
      not os.path.exists(os.path.join(ROOT, "static", "images", "school_logo.png")))
check("…and no public folder for generated images exists at all", not os.path.exists(os.path.join(ROOT, "static", "generated")))

# ================================================================ alpha: its own logo and its candidate's photograph
before = tenant_files("alpha")
r = result_image("alpha", ids["Ada Photo"])
check("the result image is produced", r.status_code == 200 and r.mimetype == "image/png" and r.data[:8] == b"\x89PNG\r\n\x1a\n", str(r.status_code))
page = CAPTURED[-1]
check("it is drawn with alpha's own logo", data_uri(ALPHA_LOGO) in page)
check("…and this candidate's own photograph", data_uri(ALPHA_PHOTO) in page)
check("…and never another school's logo or photograph", data_uri(BETA_LOGO) not in page and data_uri(BETA_PHOTO) not in page)
check("…and carries alpha's name and motto", "Alpha School" in page.title() or "ALPHA SCHOOL" in page or "Alpha School" in page)
check("…and nothing belonging to the school the platform grew from",
      not any(w in page.upper() for w in OLD_SCHOOL_WORDS) and "Beta College" not in page and "BETA COLLEGE" not in page)
check("the finished picture is sent and then deleted: nothing is left in the school's folder",
      tenant_files("alpha") == before, str(set(tenant_files("alpha")) ^ set(before)))
check("…and nothing was ever put in a public folder", not os.path.exists(os.path.join(ROOT, "static", "generated")))

# ================================================================ beta gets beta's, alpha's candidate is not reachable from beta
r = result_image("beta", ids["Bola Photo"])
page = CAPTURED[-1]
check("beta's result uses beta's logo and photograph, not alpha's",
      r.status_code == 200 and data_uri(BETA_LOGO) in page and data_uri(BETA_PHOTO) in page
      and data_uri(ALPHA_LOGO) not in page and data_uri(ALPHA_PHOTO) not in page)
# (each school numbers its candidates from 1, so use an id that exists only at alpha)
alpha_only = ids["Ada Nophoto"]
check("(set-up) that id really is alpha's alone: beta has no candidate with it",
      in_school("beta", lambda: Candidate_exists(alpha_only)) is False)
check("one school cannot ask for another school's candidate (404, nothing drawn)",
      (lambda n: result_image("beta", alpha_only).status_code == 404 and len(CAPTURED) == n)(len(CAPTURED)))
anon = A.app.test_client().get(f"/admin/candidates/{ids['Ada Photo']}/result/image", base_url=ALPHA)
check("someone not signed in cannot get a result image", anon.status_code in (302, 401, 403) and anon.mimetype != "image/png")

# ================================================================ no photograph, no logo: still a result
n = len(CAPTURED)
r = result_image("alpha", ids["Ada Nophoto"])
page = CAPTURED[-1]
check("a candidate with no photograph still gets a result, with alpha's logo and no photograph",
      r.status_code == 200 and len(CAPTURED) == n + 1 and data_uri(ALPHA_LOGO) in page and data_uri(ALPHA_PHOTO) not in page)
r = result_image("gamma", ids["Gina Nologo"])
page = CAPTURED[-1]
check("a school with no logo still gets a result, with its own name where the logo would be",
      r.status_code == 200 and "GAMMA ACADEMY" in page.upper() and data_uri(ALPHA_LOGO) not in page and data_uri(BETA_LOGO) not in page
      and 'class="logo"' not in page and not any(w in page.upper() for w in OLD_SCHOOL_WORDS))

# ================================================================ a photograph path cannot reach another school's files
beta_photo_file = next(f for f in tenant_files("beta", "uploads") if f.startswith("candidates"))
attempts = {
    "a path climbing into another school's folder": "uploads/../../beta/uploads/" + beta_photo_file.replace(os.sep, "/"),
    "another school's file named outright": "/" + os.path.join(TMP, "tenants", "beta", "uploads", beta_photo_file).replace(os.sep, "/"),
    "a file outside every upload folder": "../../../app.py",
    "a Windows path": "C:\\Windows\\win.ini",
    "a static path": "static/images/school_placeholder_logo.svg",
}
for label, path in attempts.items():
    set_photo_path("alpha", ids["Ada Nophoto"], path)
    r = result_image("alpha", ids["Ada Nophoto"])
    page = CAPTURED[-1]
    check(f"{label} is ignored: the result is made without it",
          r.status_code == 200 and data_uri(BETA_PHOTO) not in page and "win.ini" not in page and "[fonts]" not in page,
          str(r.status_code))
set_photo_path("alpha", ids["Ada Nophoto"], None)

# ================================================================ the share page
share = admins["alpha"].get(f"/admin/candidates/{ids['Ada Photo']}/result/share", base_url=ALPHA)
body = share.get_data(as_text=True)
check("the share page carries alpha's own logo", share.status_code == 200 and data_uri(ALPHA_LOGO) in body)
check("…and nothing from another school or the platform", data_uri(BETA_LOGO) not in body and not any(w in body.upper() for w in OLD_SCHOOL_WORDS))
share_g = admins["gamma"].get(f"/admin/candidates/{ids['Gina Nologo']}/result/share", base_url=GAMMA)
check("a school with no logo still gets a share page, without a logo picture",
      share_g.status_code == 200 and "data:image" not in share_g.get_data(as_text=True).split("<body")[-1][:4000])

# ================================================================ a server with no Chrome
os.environ["BRIGHTSTARS_CHROME"] = os.path.join(TMP, "no-such-chrome.exe")
check("a named Chrome that does not exist is treated as no Chrome", E._find_chrome() is None)
n = len(CAPTURED)
r = result_image("alpha", ids["Ada Photo"])
body = r.get_data(as_text=True)
check("with no Chrome the admin gets a plain error page, not a crash", r.status_code == 500 and "Something Went Wrong" in body)
check("…that reveals no path, no exception and no program name",
      TMP not in body and "no-such-chrome" not in body and "Traceback" not in body and "RuntimeError" not in body)
check("…and leaves nothing behind in the school's folder", tenant_files("alpha") == before and len(CAPTURED) == n)
os.environ["BRIGHTSTARS_CHROME"] = sys.executable
check("BRIGHTSTARS_CHROME names the program to use", str(E._find_chrome()) == sys.executable)
os.environ.pop("BRIGHTSTARS_CHROME")
found = E._find_chrome()
check("with nothing set, Chrome is looked for in the usual places (Windows, Linux, macOS names)",
      found is None or os.path.isfile(str(found)))

dispose_engines()
DROP_TEST_DATABASES()
shutil.rmtree(TMP, ignore_errors=True)
print()
failed = [x for x in results if not x[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
