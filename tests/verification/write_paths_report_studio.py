"""The report card studio, proven end to end on PostgreSQL. In plain words:

A. Who: the School Admin and a Report Card Officer (report_cards.manage) open the studio; a Subject Teacher is refused
   the studio, saving, the preview, switching it on, locking and the images. The officer reaches the school's images
   through the studio, not through the ID card routes it has no right to.
B. Saving: a hostile design is rebuilt from known keys only (an unknown item type, an unknown detail such as a password
   hash, a colour carrying CSS, an unknown table column, a position far off the page are all made harmless, and a
   script typed as text stays plain text); not a design, too large, or too many items is refused and nothing is saved;
   no CSRF token is refused. Each save is a version (an unchanged save is not), at most 20 are kept, and one can be
   brought back; an unknown version is 404. Every template is a design the rules keep as it is.
C. Locking: a locked design cannot be saved (409) until it is unlocked; an image a locked design uses cannot be
   removed until then. Each step is logged.
D. The preview: a PDF of the design on screen with a made-up student and the school's own name; never cached.
E. Switching it on: until then a student's PDF and page are the standard card; once on, the staff, parent and student
   PDFs and pages are drawn from the design, with the student's own attendance (days present, school opened), position
   in class, date of birth and the class teacher's results. Only the details the design shows are put in the page
   (the guardian's phone is not, nor any health detail). Switching back restores the standard card. Logged.
F. The new routes are in the guard files.
"""
import atexit
import base64
import io
import json
import os
import re
import shutil
import sys
import tempfile
import zlib

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="beacon_studio_")

import _pg  # noqa: E402

PREFIX, DROP_TEST_DATABASES = _pg.setup('rcstudio')
atexit.register(DROP_TEST_DATABASES)
atexit.register(shutil.rmtree, TMP, True)
os.environ.update({
    "BEACON_TENANTS_DIR": os.path.join(TMP, "tenants"),
    "BEACON_PLATFORM_HOSTS": "platform.test",
    "BEACON_PORTAL_DOMAIN": "portal.test",
    "BEACON_REGISTRY_CACHE_SECONDS": "0",
    "BEACON_SECRET": "x" * 40,
    "BEACON_ENV": "development",
})
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
os.chdir(ROOT)

import sqlalchemy as sa  # noqa: E402
from PIL import Image  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

import app as A  # noqa: E402
from control_plane import provisioning as pv  # noqa: E402
from control_plane.context import tenant_context  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import engine_for  # noqa: E402
from core import report_card_design as rcd  # noqa: E402
from core.security import ADMIN_ENDPOINT_PERMISSIONS  # noqa: E402
from models import (  # noqa: E402
    Admin, AdminType, AttendanceRecord, ParentAccount, ParentStudentLink, School as SchoolRow, SchoolStudentResult,
    SchoolSubject, Student, StudentEnrolment,
)

results, PROBLEMS = [], []
PL, UA = "http://platform.test", "http://alpha.portal.test"
TERM, SLUG = "First Term", "first-term"
NOW, REL = "2026-09-01T09:00:00+00:00", "2026-12-18T10:00:00+00:00"
PASSWORD = "Fixture-password-9"
HASH = generate_password_hash(PASSWORD, method="pbkdf2:sha256:1000")
STUDIO = "/admin/report-cards/studio"


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)


def csrf_from(body):
    found = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', body) or re.search(r'id="csrfToken" value="([^"]+)"', body)
    return found.group(1) if found else ""


class Person:
    count = [0]

    def __init__(self, form_page="/admin/password", label=""):
        self.c, self.form_page, self.label = A.app.test_client(), form_page, label
        Person.count[0] += 1
        self.addr = f"10.96.{Person.count[0]}.1"

    def get(self, path):
        r = self.c.get(path, base_url=UA, environ_base={"REMOTE_ADDR": self.addr})
        if r.status_code >= 500:
            PROBLEMS.append(f"{self.label}: GET {path} {r.status_code}")
        return r

    def text(self, path):
        return self.get(path).get_data(as_text=True)

    def post(self, path, data=None, page=None, files=None, token=True):
        data = dict(data or {})
        if token:
            data["_csrf_token"] = csrf_from(self.text(page or self.form_page))
        for key, (filename, content) in (files or {}).items():
            data[key] = (io.BytesIO(content), filename)
        r = self.c.post(path, data=data, base_url=UA, environ_base={"REMOTE_ADDR": self.addr},
                        content_type="multipart/form-data" if files else None)
        if r.status_code >= 500:
            PROBLEMS.append(f"{self.label}: POST {path} {r.status_code}")
        return r

    def said(self):
        with self.c.session_transaction(base_url=UA) as sess:
            return " | ".join(t for _, t in sess.pop("_flashes", [])).lower()


def _unescape(raw):
    """A PDF literal string (backslash escapes, octal codes) as bytes."""
    out, i = bytearray(), 0
    named = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}
    while i < len(raw):
        if raw[i] != 0x5C:
            out.append(raw[i]); i += 1
        elif i + 1 < len(raw) and 0x30 <= raw[i + 1] <= 0x37:
            j = i + 1
            while j < len(raw) and j < i + 4 and 0x30 <= raw[j] <= 0x37:
                j += 1
            out.append(int(raw[i + 1:j], 8) & 255); i = j
        else:
            out.append(named.get(raw[i + 1], raw[i + 1]) if i + 1 < len(raw) else 0x5C); i += 2
    return bytes(out)


def pdf_words(data):
    """Every piece of text a PDF draws, joined with single spaces (enough to find a phrase)."""
    objects = {int(m.group(1)): m.group(2) for m in re.finditer(rb"(\d+) 0 obj\s*(.*?)\s*endobj", data, re.S)}

    def decode(obj):
        head, keyword, rest = obj.partition(b"stream")
        if not keyword:
            return head, None
        body = rest.lstrip(b"\r\n").rsplit(b"endstream", 1)[0]
        try:
            for kind in re.findall(rb"/(ASCII85Decode|FlateDecode)", head):
                body = base64.a85decode(body.strip().removesuffix(b"~>")) if kind == b"ASCII85Decode" else zlib.decompress(body)
        except Exception:
            return head, None
        return head, body

    maps, fonts = {}, {}
    for number, obj in objects.items():
        found = re.search(rb"/ToUnicode (\d+) 0 R", obj)
        if found and b"/Type /Font" in obj:
            _, cmap = decode(objects[int(found.group(1))])
            maps[number] = {int(code, 16): chr(int(char, 16)) for code, char in
                            re.findall(rb"<([0-9A-Fa-f]{2})>\s*<([0-9A-Fa-f]{4})>", cmap or b"")}
    for obj in objects.values():
        if b"stream" not in obj and b"/Type" not in obj:
            fonts.update({name.decode(): int(number) for name, number in re.findall(rb"/(F[\w+]+) (\d+) 0 R", obj)})
    words = []
    for obj in objects.values():
        _, body = decode(obj)
        if body is None or b" Tf" not in body:
            continue
        font = None
        for m in re.finditer(rb"/(F[\w+]+) [\d.]+ Tf|\(((?:\\.|[^\\)])*)\) Tj", body, re.S):
            if m.group(1):
                font = maps.get(fonts.get(m.group(1).decode()))
            else:
                raw = _unescape(m.group(2))
                words.append("".join(font.get(b, "") for b in raw) if font else raw.decode("cp1252", "replace"))
    return " ".join(" ".join(words).split())


# ---------------------------------------------------------------- the school, its people and a term's results
pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
console = A.app.test_client()
console.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                      "_csrf_token": csrf_from(console.get("/platform/login", base_url=PL).get_data(as_text=True))}, base_url=PL)
console.post("/platform/schools/new", data={"_csrf_token": csrf_from(console.get("/platform/schools/new", base_url=PL).get_data(as_text=True)),
                                            "name": "Alpha School", "code": "alpha", "school_motto": "Excellence in Learning"},
             base_url=PL, content_type="multipart/form-data")
with platform_session() as s:
    INFO = to_info(get_tenant(s, "alpha"))


def sql(statement, **params):
    with engine_for(INFO).begin() as conn:
        result = conn.execute(sa.text(statement), params)
        return result.fetchall() if result.returns_rows else None


def one(statement, **params):
    rows = sql(statement, **params)
    return rows[0][0] if rows else None


def orm(fn):
    with A.app.app_context(), tenant_context(INFO):
        out = fn()
        A.db.session.commit()
        return out


page = console.get("/platform/schools/alpha", base_url=PL).get_data(as_text=True)
r = console.post("/platform/schools/alpha/enter", data={"_csrf_token": csrf_from(page)}, base_url=PL)
op = Person(label="School Admin")
op.get(r.headers["Location"][len(UA):])
op.get("/admin/workspace/school")
CUR = one("SELECT id FROM academic_sessions WHERE is_current = 1")
J1 = one("SELECT id FROM school_classes WHERE name = 'JSS 1'")


def staff(username, role_name):
    def go():
        role = A.db.session.scalars(sa.select(AdminType).where(AdminType.name == role_name)).first()
        admin = Admin(username=username, display_name=username.title(), password_hash=HASH, admin_type_id=role.id, active=1,
                      password_must_change=0, created_at=NOW)
        A.db.session.add(admin)
        A.db.session.flush()
        return admin.id
    aid = orm(go)
    person = Person(label=username)
    person.post("/login", {"username": username, "password": PASSWORD}, page="/login")
    person.get("/admin/workspace/school")
    return person, aid


officer, OFFICER = staff("rc.officer", "Report Card Officer")


def override(aid, code):
    """The School Admin gives one member of staff a permission personally (an override on their staff record)."""
    sql("INSERT INTO admin_permissions (admin_id, permission_id, granted_at) SELECT :a, id, '2026-10-14' FROM permissions "
        "WHERE code = :c ON CONFLICT DO NOTHING", a=aid, c=code)
    from core.short_cache import clear
    clear()
teacher, TEACHER = staff("subject.teacher", "Subject Teacher")


def setup():
    school_id = A.db.session.scalar(sa.select(SchoolRow.id))
    maths = SchoolSubject(name="Mathematics", code="MTH", created_at=NOW, active=1)
    A.db.session.add(maths)
    A.db.session.flush()
    ids = {}
    for code, first, last, test, exam, extra in (
            ("ALP/0001", "Ada", "Okafor", 18, 54, {"date_of_birth": "2014-03-12", "guardian_phone": "0803 777 1234",
                                                   "drug_allergies": "PENICILLIN-SECRET", "genotype": "AS"}),
            ("ALP/0002", "Bayo", "Adeyemi", 10, 30, {})):
        st = Student(admission_no=code, student_number=code, first_name=first, last_name=last, gender="Female", created_at=NOW,
                     active=1, school_id=school_id, login_username=first.lower(), login_password_hash=HASH, account_active=1,
                     password_must_change=0, guardian_name="Mr Okafor", **extra)
        A.db.session.add(st)
        A.db.session.flush()
        A.db.session.add(StudentEnrolment(student_id=st.id, class_id=J1, session_id=CUR, enrolled_at=NOW, active=1, school_id=school_id))
        for component, score, out_of in (("Test", test, 20), ("Exam", exam, 60)):
            A.db.session.add(SchoolStudentResult(student_id=st.id, subject_id=maths.id, score=float(score), max_score=float(out_of),
                                                 term=TERM, session_id=CUR, status="released", created_at=NOW, source_type="manual",
                                                 component_name=component, released_at=REL))
        ids[first] = st.id
    # Ada's register this term: 5 present, 1 late, 2 absent -> 6 days present of 8, 75%.
    for day, status in enumerate(["present"] * 5 + ["late"] + ["absent"] * 2, 1):
        A.db.session.add(AttendanceRecord(student_id=ids["Ada"], class_id=J1, session_id=CUR, term=TERM, date=f"2026-10-{day:02d}",
                                          status=status, created_at=NOW))
    parent = ParentAccount(username="mum", display_name="Mrs Okafor", email="mum@family.example", password_hash=HASH, active=1,
                           password_must_change=0, created_at=NOW)
    A.db.session.add(parent)
    A.db.session.flush()
    A.db.session.add(ParentStudentLink(parent_id=parent.id, student_id=ids["Ada"], relationship="Mother", active=1, created_at=NOW))
    return ids


IDS = orm(setup)
ADA = IDS["Ada"]
parent = Person("/parent/password", "parent")
parent.post("/login", {"username": "mum", "password": PASSWORD}, page="/login")
student = Person("/student/password", "student")
student.post("/login", {"username": "ada", "password": PASSWORD}, page="/login")
STAFF_PAGE = f"/admin/school/report-cards/{ADA}/{CUR}/{SLUG}"
PARENT_PAGE = f"/parent/children/{ADA}/report-cards/{CUR}/{SLUG}"
STUDENT_PAGE = f"/student/report-cards/{CUR}/{SLUG}"


def logged(action):
    return one("SELECT COUNT(*) FROM audit_logs WHERE action = :a", a=action) or 0


def versions():
    return one("SELECT COUNT(*) FROM report_card_design_versions") or 0


def save(person, layout, token=True):
    return person.post(STUDIO, {"layout": layout if isinstance(layout, str) else json.dumps(layout)}, page=STUDIO, token=token)


# ================================================================ A. who
r = op.get(STUDIO)
body = r.get_data(as_text=True)
check("A1 the School Admin opens the studio, with the templates, the blocks and the student details",
      r.status_code == 200 and 'id="iddData"' in body and "Results table" in body and "Attendance" in body
      and "Date of birth" in body and "report-card-render.js" in body)
cfg = json.loads(re.search(r'<script type="application/json" id="iddData"[^>]*>(.*?)</script>', body, re.S).group(1))
check("A2 the studio starts from the Classic template, with four templates to pick from",
      cfg["mode"] == "report" and len(cfg["templates"]) == 4 and cfg["layout"]["page"] == "A4"
      and cfg["layout"] == rcd.default_layout("classic"))
check("A3 a Report Card Officer is refused on the role alone: only the School Admin, a head teacher or a personal "
      "override opens a studio", officer.get(STUDIO).status_code in (302, 403))
override(OFFICER, "report_cards.manage")
check("A3 …and opens it once the School Admin gives the officer the permission personally", officer.get(STUDIO).status_code == 200)
denied = [teacher.get(STUDIO).status_code,
          save(teacher, rcd.default_layout()).status_code,
          teacher.post(STUDIO + "/preview", {"layout": json.dumps(rcd.default_layout())}).status_code,
          teacher.post(STUDIO + "/use", {"use": "1"}).status_code,
          teacher.post(STUDIO + "/lock", {"lock": "1"}).status_code,
          teacher.get(STUDIO + "/versions/1").status_code]
check("A4 a Subject Teacher is refused the studio, saving, the preview, switching it on, locking and versions",
      all(code in (302, 403) for code in denied) and one("SELECT COUNT(*) FROM report_card_designs") == 0, str(denied))
check("A5 the studio is mapped to report_cards.manage",
      all(ADMIN_ENDPOINT_PERMISSIONS.get(e) == "report_cards.manage" for e in (
          "admin_report_card_studio", "admin_report_card_studio_save", "admin_report_card_studio_lock", "admin_report_card_studio_use",
          "admin_report_card_studio_version", "admin_report_card_studio_preview", "admin_report_card_asset_upload",
          "admin_report_card_asset_delete")))

buf = io.BytesIO()
Image.new("RGB", (40, 40), (200, 30, 30)).save(buf, "PNG")
PNG = buf.getvalue()
r = officer.post(STUDIO + "/assets", page=STUDIO, files={"image": ("stamp.png", PNG)})
asset = (r.get_json() or {}).get("asset") or {}
check("A6 the officer uploads an image from the studio", r.status_code == 200 and asset.get("id"))
r2 = officer.post("/admin/id-cards/assets", page=STUDIO, files={"image": ("other.png", PNG)})
check("A7 ...but not through the ID card routes it has no right to", r2.status_code in (302, 403)
      and one("SELECT COUNT(*) FROM id_card_assets") == 1)
r3 = teacher.post(STUDIO + "/assets", files={"image": ("x.png", PNG)})
check("A8 a Subject Teacher cannot upload images", r3.status_code in (302, 403) and one("SELECT COUNT(*) FROM id_card_assets") == 1)

# ================================================================ B. saving
hostile = rcd.default_layout("modern")
page1 = hostile["sides"]["page1"]
page1 += [
    {"type": "script", "x": 1, "y": 1, "w": 5, "h": 5},
    {"type": "field", "field": "password_hash", "x": 1, "y": 1, "w": 5, "h": 5},
    {"type": "text", "text": "<img src=x onerror=alert(1)>", "x": 99999, "y": -99999, "w": 40, "h": 6,
     "color": "red;background:url(javascript:alert(1))", "font": "comic"},
    {"type": "results", "columns": ["subject", "evil", "total"], "x": 10, "y": 10, "w": 100, "h": 50, "head_fill": "expression(1)",
     "size": 999},
    {"type": "image", "asset": 999999, "x": 1, "y": 1, "w": 5, "h": 5},
]
before = versions()
r = save(op, hostile)
kept = (r.get_json() or {}).get("layout") or {}
p1 = kept.get("sides", {}).get("page1", [])
types = [e["type"] for e in p1]
text_el = next((e for e in p1 if e.get("type") == "text" and "onerror" in e.get("text", "")), {})
res_el = [e for e in p1 if e.get("type") == "results"][-1] if "results" in types else {}
check("B1 a hostile design is saved only as what the rules keep", r.status_code == 200 and "script" not in types
      and not any(e.get("field") == "password_hash" for e in p1) and not any(e.get("asset") == 999999 for e in p1))
check("B2 ...text stays plain text, a CSS colour and an unknown font are replaced, an off-page position is pulled in",
      text_el.get("text") == "<img src=x onerror=alert(1)>" and text_el.get("color") == "ink" and text_el.get("font") == "dejavu"
      and text_el.get("x") <= 210 and text_el.get("y") >= -297, str(text_el))
check("B3 ...a table keeps only real columns, a real heading colour and a size in range",
      res_el.get("columns") == ["subject", "total"] and res_el.get("head_fill") == "brand" and res_el.get("size") == 16, str(res_el))
check("B4 the saved design is the rebuilt one, kept as a version and logged",
      json.loads(one("SELECT layout_json FROM report_card_designs")) == kept and versions() == before + 1
      and logged("report_card_design_saved") == 1)
r = save(op, kept)
check("B5 saving it again unchanged keeps no new version", r.status_code == 200 and versions() == before + 1, f"{r.status_code} {versions()} {before} {r.get_data(as_text=True)[:200]}")
too_many = rcd.default_layout()
too_many["sides"]["page2"] = [{"type": "text", "text": f"t{i}", "x": 1, "y": 1, "w": 5, "h": 5} for i in range(rcd.MAX_ELEMENTS + 1)]
bad = [save(op, "not json").status_code, save(op, "[1, 2]").status_code, save(op, too_many).status_code,
       save(op, json.dumps({"x": "y" * 700_000})).status_code]
check("B6 not a design, too many items or too large is refused and nothing changes", bad == [400, 400, 400, 400]
      and json.loads(one("SELECT layout_json FROM report_card_designs")) == kept, str(bad))
r = save(op, rcd.default_layout("bold"), token=False)
check("B7 no CSRF token, no save", r.status_code in (400, 403) and json.loads(one("SELECT layout_json FROM report_card_designs")) == kept)
for i in range(22):
    lay = rcd.default_layout("elegant")
    lay["sides"]["page2"] = [{"type": "text", "text": f"Version {i}", "x": 10, "y": 10, "w": 50, "h": 8}]
    save(op, lay)
check("B8 at most 20 versions are kept", versions() == 20)
vid = one("SELECT MIN(id) FROM report_card_design_versions")
r = op.get(f"{STUDIO}/versions/{vid}")
check("B9 an earlier version can be brought back", r.status_code == 200 and r.get_json()["layout"]["sides"]["page2"][0]["text"].startswith("Version"))
check("B10 an unknown version is not found", op.get(f"{STUDIO}/versions/999999").status_code == 404)
check("B11 every template is a design the rules keep as it is",
      all(rcd.clean_layout(rcd.default_layout(t)) == rcd.default_layout(t) for t in rcd.TEMPLATES))

# ================================================================ C. locking
design = rcd.default_layout("classic")
design["sides"]["page1"] += [
    {"type": "text", "text": "DESIGNEDMARK7731", "font": "helvetica", "x": 150, "y": 280, "w": 50, "h": 6, "size": 9},
    {"type": "field", "field": "days_present", "label": "Days present", "font": "helvetica", "x": 10, "y": 270, "w": 60, "h": 5, "size": 8},
    {"type": "field", "field": "days_opened", "label": "School opened", "font": "helvetica", "x": 10, "y": 276, "w": 60, "h": 5, "size": 8},
    {"type": "field", "field": "position_of", "label": "Position", "font": "helvetica", "x": 80, "y": 270, "w": 60, "h": 5, "size": 8},
    {"type": "field", "field": "date_of_birth", "label": "Born", "font": "helvetica", "x": 80, "y": 276, "w": 60, "h": 5, "size": 8},
    {"type": "field", "field": "attendance_percentage", "label": "Attendance", "font": "helvetica", "x": 150, "y": 270, "w": 50, "h": 5, "size": 8},
    {"type": "image", "asset": asset["id"], "x": 180, "y": 5, "w": 15, "h": 15},
]
r = save(op, design)
check("C1 the design with the school's image is saved", r.status_code == 200 and any(e.get("type") == "image" for e in r.get_json()["layout"]["sides"]["page1"]))
op.post(STUDIO + "/lock", {"lock": "1"}, page=STUDIO)
check("C2 locking is kept and logged", one("SELECT locked FROM report_card_designs") == 1 and logged("report_card_design_locked") == 1)
r = save(op, rcd.default_layout("bold"))
check("C3 a locked design cannot be saved", r.status_code == 409 and "DESIGNEDMARK7731" in one("SELECT layout_json FROM report_card_designs"))
r = op.post(f"{STUDIO}/assets/{asset['id']}/delete", page=STUDIO)
check("C4 an image a locked report design uses cannot be removed", r.status_code == 409 and one("SELECT COUNT(*) FROM id_card_assets") == 1)
op.post(STUDIO + "/lock", {"lock": "0"}, page=STUDIO)
check("C5 unlocking is kept and logged", one("SELECT locked FROM report_card_designs") == 0 and logged("report_card_design_unlocked") == 1)

# ================================================================ D. the preview
r = op.post(STUDIO + "/preview", {"layout": json.dumps(design)}, page=STUDIO)
words = pdf_words(r.get_data()) if r.status_code == 200 else ""
check("D1 the preview is a PDF of the design with the made-up student and the school's name, never cached",
      r.status_code == 200 and r.mimetype == "application/pdf" and r.get_data()[:4] == b"%PDF" and "no-store" in r.headers.get("Cache-Control", "")
      and "DESIGNEDMARK7731" in words and "Adaeze" in words and "ALPHA SCHOOL" in words.upper(), words[:300])
check("D2 a preview of something that is not a design is refused", op.post(STUDIO + "/preview", {"layout": "nope"}, page=STUDIO).status_code == 400)

# ================================================================ E. switching it on
r = op.get(STAFF_PAGE + "/pdf")
standard = pdf_words(r.get_data())
check("E1 until it is switched on, the student's card is the standard one", r.status_code == 200 and "DESIGNEDMARK7731" not in standard
      and 'id="rcdData"' not in op.text(STAFF_PAGE))
op.post(STUDIO + "/use", {"use": "1"}, page=STUDIO)
check("E2 switching it on is kept and logged", one("SELECT in_use FROM report_card_designs") == 1 and logged("report_card_design_used") == 1)
for who, person, path in (("staff", op, STAFF_PAGE), ("parent", parent, PARENT_PAGE), ("student", student, STUDENT_PAGE)):
    r = person.get(path + "/pdf")
    words = pdf_words(r.get_data()) if r.status_code == 200 else ""
    check(f"E3 the {who} PDF is drawn from the design, with Ada's attendance, position and birthday",
          r.status_code == 200 and "DESIGNEDMARK7731" in words and "Days present: 6" in words and "School opened: 8" in words
          and "Position: 1st of 2" in words and "Born: 12 March 2014" in words and "Attendance: 75%" in words
          and "Mathematics" in words and "PENICILLIN" not in words, words[:400])
    body = person.text(path)
    found = re.search(r'<script type="application/json" id="rcdData"[^>]*>(.*?)</script>', body, re.S)
    data = json.loads(found.group(1)) if found else {}
    view = data.get("view", {})
    check(f"E4 the {who} page is drawn from the design with the same details, and only those the design shows",
          bool(found) and view.get("data", {}).get("days_present") == "6" and view.get("data", {}).get("position_of") == "1st of 2"
          and "guardian_phone" not in view.get("data", {}) and "0803 777 1234" not in body and "PENICILLIN" not in body
          and "genotype" not in view.get("data", {}) and '<main class="rc-sheet">' not in body
          and view.get("report", {}).get("subjects", [{}])[0].get("subject") == "Mathematics"
          and all(str(v) == f"/report-card-design/images/{k}" for k, v in view.get("assets", {}).items()) and view.get("assets"))
    image = person.get(f"/report-card-design/images/{asset['id']}")
    check(f"E4b the {who} gets the design's image from its own address, kept by the browser for a week",
          image.status_code == 200 and image.mimetype.startswith("image/") and "private" in image.headers.get("Cache-Control", "")
          and "max-age=604800" in image.headers.get("Cache-Control", ""))
visitor = Person("/admin/password", "visitor")
other = officer.post(STUDIO + "/assets", page=STUDIO, files={"image": ("unused.png", PNG)}).get_json()["asset"]
check("E4c a design image is not served to someone signed out, nor an image the design does not use",
      visitor.get(f"/report-card-design/images/{asset['id']}").status_code == 404
      and parent.get(f"/report-card-design/images/{other['id']}").status_code == 404
      and parent.get("/report-card-design/images/999999").status_code == 404)
statements = []


def _note(conn, cursor, statement, *rest):
    statements.append(statement)


parent.get(PARENT_PAGE)                                 # the first view after a change reads the design once
sa.event.listen(sa.engine.Engine, "before_cursor_execute", _note)
for _ in range(3):
    parent.get(PARENT_PAGE); student.get(STUDENT_PAGE + "/pdf")
sa.event.remove(sa.engine.Engine, "before_cursor_execute", _note)
design_reads = [s for s in statements if "report_card_designs" in s or "id_card_assets" in s]
check("E4d the design is read once and kept, not read again for every card shown or downloaded", design_reads == [], str(design_reads[:2]))
op.post(STUDIO + "/use", {"use": "0"}, page=STUDIO)
r = op.get(STAFF_PAGE + "/pdf")
check("E5 switching back restores the standard card, and is logged", "DESIGNEDMARK7731" not in pdf_words(r.get_data())
      and 'id="rcdData"' not in op.text(STAFF_PAGE) and logged("report_card_design_standard") == 1)
op.post(STUDIO + "/use", {"use": "1"}, page=STUDIO)
sql("UPDATE report_card_designs SET layout_json = 'broken{'")
with A.app.app_context(), tenant_context(INFO):         # a change made behind the app's back: drop the kept copy
    from blueprints.school.report_card_data import forget_design
    forget_design()
r = op.get(STAFF_PAGE + "/pdf")
check("E6 a design that cannot be read falls back to the standard card instead of failing",
      r.status_code == 200 and r.get_data()[:4] == b"%PDF" and op.get(STAFF_PAGE).status_code == 200)

# ================================================================ the Design studios menu entry
body_op, r_off, r_t = op.text("/admin/design-studios"), officer.get("/admin/design-studios"), teacher.get("/admin/design-studios")
body_off = r_off.get_data(as_text=True)
check("E7 Design studios offers the School Admin both studios, the officer only the report card studio, a teacher neither",
      "/admin/id-cards/design/student" in body_op and "/admin/report-cards/studio" in body_op and r_off.status_code == 200
      and "/admin/report-cards/studio" in body_off and "/admin/id-cards/design/student" not in body_off and r_t.status_code in (302, 403))
check("E8 the menu shows Design studios to those who may use one, and not to a teacher",
      "/admin/design-studios" in body_op and "/admin/design-studios" in body_off and "/admin/design-studios" not in teacher.text("/admin/school"))

IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
          "Version/17.5 Mobile/15E148 Safari/604.1")
ANDROID = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36"
DESKTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


def as_device(path, agent):
    r = op.c.get(path, base_url=UA, headers={"User-Agent": agent}, environ_base={"REMOTE_ADDR": op.addr})
    return r.status_code, r.get_data(as_text=True)


phone_pages = [as_device(p, a) for p in (STUDIO, "/admin/id-cards/design/student", "/admin/design-studios") for a in (IPHONE, ANDROID)]
check("E9 on a phone, both studios and the Design studios page ask for a computer instead",
      all(code == 200 and "Open this on a computer" in body and 'id="iddData"' not in body and "/admin/id-cards/design/staff" not in body
          for code, body in phone_pages), str([c for c, _ in phone_pages]))
code, body = as_device(STUDIO, DESKTOP)
check("E10 on a computer the studio opens, carrying the small-screen message only for a narrow window",
      code == 200 and 'id="iddData"' in body and 'class="ac studio-small"' in body and "studio-small--always" not in body)

# ================================================================ F. guard files
cover = open(os.path.join(HERE, "pg_posts_coverage.py"), encoding="utf-8").read()
check("F1 the new write routes are listed for the write-route coverage check",
      all(f'"{STUDIO}{e}"' in cover for e in ("", "/lock", "/use", "/preview", "/assets", "/assets/<int:asset_id>/delete")))
check("F2 nothing answered with a server error", not PROBLEMS, "; ".join(PROBLEMS[:5]))

passed = sum(1 for _, ok, _ in results if ok)
print(f"\n{passed}/{len(results)} checks passed")
sys.exit(0 if passed == len(results) else 1)
