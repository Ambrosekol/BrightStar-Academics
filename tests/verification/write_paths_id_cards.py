"""ID cards, proven end to end on PostgreSQL. In plain words:

A. Who: the School Admin and a Records Officer see ID cards; a Subject Teacher (without the permission) is refused
   the list, printing and the designer.
B. The studio: the standard design to start from; a saved design is rebuilt from known keys only (an unknown detail,
   a colour or a font or a clip carrying CSS, a gradient carrying CSS, another school's image, a script typed as text
   are all made harmless); too many items are refused. Each save is kept as a version that can be brought back.
   Templates. The school's own images: a real picture is kept, a fake one refused, a teacher refused; one a locked
   design uses cannot be removed. Locking stops saving and starting again until it is unlocked; each step is logged.
C. Printing: the ticked students' cards, front and back, each with its own QR code; the first print issues a card
   (random token, serial STU-000001), a second print reuses it. Never cached; logged. A student this account may not
   print for (one who has left) stops the whole print.
D. The check page (no sign-in): a valid card shows the school, name, class, photograph and "Valid"; an unknown code
   says so; nothing is cached or indexed. A replaced card's old code says it was cancelled, and its photograph is no
   longer served; a student who has left shows as not valid.
E. Staff cards: the staff list leaves out the platform's own accounts, and a staff card prints.
"""
import atexit
import os
import re
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_idcards_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('idcards')
atexit.register(DROP_TEST_DATABASES)
atexit.register(shutil.rmtree, TMP, True)
os.environ.update({
    "BRIGHTSTARS_TENANTS_DIR": os.path.join(TMP, "tenants"),
    "BRIGHTSTARS_PLATFORM_HOSTS": "platform.test",
    "BRIGHTSTARS_PORTAL_DOMAIN": "portal.test",
    "BRIGHTSTARS_REGISTRY_CACHE_SECONDS": "0",
    "BRIGHTSTARS_SECRET": "x" * 40,
    "BRIGHTSTARS_ENV": "development",
})
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
os.chdir(ROOT)

import json  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

import app as A  # noqa: E402
from control_plane import provisioning as pv  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import engine_for  # noqa: E402

results, PROBLEMS = [], []
PL = "http://platform.test"
UA = "http://alpha.portal.test"


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)


def token_in(body):
    found = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', body) or re.search(r'id="csrfToken" value="([^"]+)"', body)
    return found.group(1) if found else ""


class Person:
    count = [0]

    def __init__(self, label):
        self.c, self.label = A.app.test_client(), label
        Person.count[0] += 1
        self.addr = f"10.95.{Person.count[0]}.1"

    def get(self, path):
        r = self.c.get(path, base_url=UA, environ_base={"REMOTE_ADDR": self.addr})
        if r.status_code >= 500:
            PROBLEMS.append(f"{self.label}: GET {path} {r.status_code}")
        return r

    def text(self, path):
        return self.get(path).get_data(as_text=True)

    def post(self, path, data=None, page="/admin/password"):
        data = dict(data or {})
        data["_csrf_token"] = token_in(self.text(page))
        r = self.c.post(path, data=data, base_url=UA, environ_base={"REMOTE_ADDR": self.addr})
        if r.status_code >= 500:
            PROBLEMS.append(f"{self.label}: POST {path} {r.status_code}")
        return r

    def said(self):
        with self.c.session_transaction(base_url=UA) as sess:
            return " | ".join(t for _, t in sess.pop("_flashes", [])).lower()


pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
console = A.app.test_client()
console.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                      "_csrf_token": token_in(console.get("/platform/login", base_url=PL).get_data(as_text=True))}, base_url=PL)
console.post("/platform/schools/new", data={"_csrf_token": token_in(console.get("/platform/schools/new", base_url=PL).get_data(as_text=True)),
                                            "name": "Alpha School", "code": "alpha"}, base_url=PL, content_type="multipart/form-data")
with platform_session() as s:
    INFO = to_info(get_tenant(s, "alpha"))


def sql(statement, **params):
    with engine_for(INFO).begin() as conn:
        result = conn.execute(sa.text(statement), params)
        return result.fetchall() if result.returns_rows else None


def one(statement, **params):
    rows = sql(statement, **params)
    return rows[0][0] if rows else None


page = console.get("/platform/schools/alpha", base_url=PL).get_data(as_text=True)
r = console.post("/platform/schools/alpha/enter", data={"_csrf_token": token_in(page)}, base_url=PL)
op = Person("School Admin")
op.get(r.headers["Location"][len(UA):])
op.get("/admin/workspace/school")
PASSWORD = "Fixture-password-9"
HASH = generate_password_hash(PASSWORD, method="pbkdf2:sha256:1000")
J1 = one("SELECT id FROM school_classes WHERE name = 'JSS 1'")
SESSION = one("SELECT id FROM academic_sessions WHERE is_current = 1")
role = {name: one("SELECT id FROM admin_types WHERE name = :n", n=name) for name in ("School Records Officer", "Subject Teacher")}


def new_staff(username, role_name, extra=None):
    payload = {"username": username, "display_name": username.replace(".", " ").title(), "admin_type_ids": [str(role[role_name])],
               "duty_class": [""], "duty_subject": [""], "duty_department": [""], **(extra or {})}
    op.post("/admin/administration/admins/new", payload, page="/admin/administration/admins/new")
    aid = one("SELECT id FROM admins WHERE username = :u", u=username)
    sql("UPDATE admins SET password_hash = :h, password_must_change = 0 WHERE id = :i", h=HASH, i=aid)
    person = Person(username)
    person.post("/login", {"username": username, "password": PASSWORD}, page="/login")
    person.get("/admin/workspace/school")
    return person, aid


officer, OFFICER = new_staff("records.officer", "School Records Officer")


def override(aid, code):
    """The School Admin gives one member of staff a permission personally (an override on their staff record)."""
    sql("INSERT INTO admin_permissions (admin_id, permission_id, granted_at) SELECT :a, id, '2026-10-14' FROM permissions "
        "WHERE code = :c ON CONFLICT DO NOTHING", a=aid, c=code)
    from core.short_cache import clear
    clear()
op.post("/admin/school/subjects/new", {"name": "Mathematics", "code": "MTH", "class_ids": [J1]}, page="/admin/school/subjects/new")
MATHS = one("SELECT id FROM school_subjects WHERE name = 'Mathematics'")
teacher, TEACHER = new_staff("maths.teacher", "Subject Teacher", {"duty_class": [str(J1)], "duty_subject": [str(MATHS)]})

import io  # noqa: E402
from PIL import Image  # noqa: E402
buf = io.BytesIO()
Image.new("RGB", (60, 72), (40, 90, 160)).save(buf, "PNG")
with A.app.app_context():
    from control_plane.context import tenant_context
    from datetime import datetime, timezone
    with tenant_context(INFO):
        from core.storage import save_upload_bytes
        from models import Student, StudentEnrolment, db
        now = datetime.now(timezone.utc).isoformat()
        school_id = one("SELECT id FROM schools LIMIT 1")
        STU = {}
        for code, first in (("ID-1", "Ada"), ("ID-2", "Bayo"), ("ID-3", "Chidi")):
            st = Student(admission_no=code, student_number=code, first_name=first, last_name="Okafor", created_at=now, active=1,
                         school_id=school_id, gender="Female", account_active=1, password_must_change=0)
            db.session.add(st)
            db.session.flush()
            db.session.add(StudentEnrolment(student_id=st.id, class_id=J1, session_id=SESSION, enrolled_at=now, active=1))
            STU[first] = st.id
        db.session.get(Student, STU["Ada"]).photo_path = save_upload_bytes("students", "ada-id.png", buf.getvalue(), "image/png")
        db.session.commit()
        db.session.remove()

# ================================================================ A. who
check("the School Admin and the Records Officer have ID cards in the menu",
      "ID cards" in op.text("/admin/school") and "/admin/id-cards" in officer.text("/admin/school"))
check("a Subject Teacher does not", "/admin/id-cards" not in teacher.text("/admin/school"))
for path in ("/admin/id-cards", "/admin/id-cards/design/student"):
    r = teacher.get(path)
    check(f"…and is refused {path}", r.status_code in (302, 403) and "Ada Okafor" not in r.get_data(as_text=True))
r = teacher.post("/admin/id-cards/print", {"kind": "student", "person": [STU["Ada"]]})
check("…and cannot print", r.status_code in (302, 403) and not one("SELECT COUNT(*) FROM id_cards"))
listing = officer.text("/admin/id-cards?kind=student")
check("the list shows each student and that no card is issued yet", "Okafor Ada" in listing and listing.count("Not issued") == 3)

# ================================================================ B. the designer
DES = "/admin/id-cards/design/student"
check("the Records Officer is refused the designer on the role alone (studios: the School Admin, a head teacher, or a "
      "personal override)", officer.get(DES).status_code in (302, 403) and "/admin/design-studios" not in officer.text("/admin/school"))
override(OFFICER, "id_cards.manage")
check("…and opens it, with Design studios in the menu, once given the permission personally",
      officer.get(DES).status_code == 200 and "/admin/design-studios" in officer.text("/admin/school"))
page = officer.text(DES)
check("the designer opens on the standard design", "iddData" in page and "STUDENT IDENTITY CARD" in page)
hostile = {"orientation": "landscape", "background": "red;background:url(evil)", "sides": {"front": [
    {"type": "field", "field": "password", "x": 1, "y": 1, "w": 20, "h": 5},
    {"type": "text", "text": "<script>alert(1)</script>", "x": -40, "y": 500, "w": 900, "h": 6, "color": "#123456;x:y", "size": 99},
    {"type": "field", "field": "admission_no", "label": "Adm", "x": 5, "y": 5, "w": 30, "h": 5, "color": "accent"},
    {"type": "unknown", "x": 1}], "back": []}}
r = officer.post(DES, {"layout": json.dumps(hostile)}, page=DES)
saved = json.loads(one("SELECT layout_json FROM id_card_designs WHERE kind = 'student'") or "{}")
front = saved.get("sides", {}).get("front", [])
check("a saved design is rebuilt from known keys only",
      r.status_code == 200 and saved.get("background", {}).get("color") == "white" and len(front) == 2
      and front[0]["text"] == "<script>alert(1)</script>" and front[0]["color"] == "ink" and front[0]["size"] == 72
      and front[0]["y"] == 54.0 and front[0]["w"] == 171.2 and "password" not in json.dumps(saved)
      and front[1]["field"] == "admission_no" and front[1]["color"] == "accent", str(front))
v2 = {"orientation": "portrait", "background": {"color": "#ABCDEF", "gradient": {"type": "linear", "from": "red;x", "to": "#000000", "angle": 9999},
                                                "image": 999},
      "sides": {"front": [
          {"type": "text", "text": "Hi\x00\x07 there\n\n\n\n\n\n\n\n\nlast", "font": "evil, url(x)", "weight": 550, "letter": 999,
           "rotate": 720, "opacity": -5, "shadow": "neon", "x": 1, "y": 1, "w": 10, "h": 5},
          {"type": "shape", "shape": "url(#x)", "fill": "expression(1)", "stroke": "#112233", "stroke_w": 99, "dash": "x",
           "gradient": {"type": "conic", "from": "#000000"}, "x": 1, "y": 1, "w": 5, "h": 5},
          {"type": "photo", "clip": "url(evil)", "fit": "scale-down", "border": "#fff;x", "x": 1, "y": 1, "w": 5, "h": 5},
          {"type": "image", "asset": 999, "x": 1, "y": 1, "w": 5, "h": 5},
          {"type": "barcode", "field": "password", "fg": "#ff0000", "x": 1, "y": 1, "w": 20, "h": 8},
          {"type": "signature", "who": "someone", "x": 1, "y": 1, "w": 10, "h": 4}], "back": []}}
r = officer.post(DES, {"layout": json.dumps(v2)}, page=DES)
saved = json.loads(one("SELECT layout_json FROM id_card_designs WHERE kind = 'student'") or "{}")
f = saved.get("sides", {}).get("front", [])
text, shape, photo = (f + [{}, {}, {}])[:3]
check("…and so are every style, shape, clip and picture choice",
      r.status_code == 200 and saved["orientation"] == "portrait" and saved["background"]["color"] == "#abcdef"
      and saved["background"]["gradient"] == {"type": "linear", "from": "brand", "to": "#000000", "angle": 360}
      and saved["background"]["image"] is None and len(f) == 5
      and text.get("font") == "sans" and text.get("weight") == 400 and text.get("letter") == 100 and text.get("rotate") == 180
      and text.get("opacity") == 0 and text.get("shadow") == "none" and "\x00" not in text.get("text", "") and text.get("text", "").count("\n") <= 7
      and shape.get("shape") == "rect" and shape.get("fill") == "brand" and shape.get("stroke_w") == 5 and shape.get("dash") == "solid"
      and shape.get("gradient") is None and photo.get("clip") == "none" and photo.get("fit") == "cover" and photo.get("border") == "none"
      and [e["type"] for e in f] == ["text", "shape", "photo", "barcode", "signature"]
      and f[3]["field"] == "serial" and f[4]["who"] == "head", json.dumps(f)[:600])
r = officer.post(DES, {"layout": json.dumps({"sides": {"front": [{"type": "shape"}] * 61}})}, page=DES)
check("a side with too many items is refused", r.status_code == 400 and "at most 60" in r.get_json().get("error", ""))
versions = sql("SELECT id FROM id_card_design_versions WHERE kind = 'student' ORDER BY id")
r = officer.get(f"{DES}/versions/{versions[0][0]}") if versions else None
check("each save is kept as a version, and an earlier one can be read back to bring it back",
      len(versions) == 2 and r is not None and r.status_code == 200 and r.get_json()["layout"]["orientation"] == "landscape")
check("a teacher cannot read a version", teacher.get(f"{DES}/versions/{versions[0][0]}").status_code in (302, 403))
officer.post(f"{DES}/reset", {"template": "modern"}, page=DES)
check("starting from the Modern template gives an upright card with a barcode",
      json.loads(one("SELECT layout_json FROM id_card_designs WHERE kind = 'student'"))["orientation"] == "portrait"
      and '"barcode"' in one("SELECT layout_json FROM id_card_designs WHERE kind = 'student'"))

# the school's own images
def upload(person, name, data):
    token = token_in(person.text(DES) if person is officer else person.text("/admin/password"))
    return person.c.post("/admin/id-cards/assets", data={"_csrf_token": token, "image": (io.BytesIO(data), name)},
                         base_url=UA, environ_base={"REMOTE_ADDR": person.addr}, content_type="multipart/form-data")


badge = io.BytesIO()
Image.new("RGBA", (40, 40), (200, 30, 30, 255)).save(badge, "PNG")
r = upload(officer, "School badge.png", badge.getvalue())
ASSET = (r.get_json() or {}).get("asset", {}).get("id")
check("a real picture is kept as a card image, under its own name", r.status_code == 200 and ASSET
      and one("SELECT name FROM id_card_assets WHERE id = :i", i=ASSET) == "School badge"
      and (one("SELECT file_path FROM id_card_assets WHERE id = :i", i=ASSET) or "").startswith("uploads/idcards/"))
r = upload(officer, "evil.png", b"<svg onload=alert(1)>")
check("a file pretending to be a picture is refused", r.status_code == 400 and one("SELECT COUNT(*) FROM id_card_assets") == 1)
r = upload(teacher, "x.png", badge.getvalue())
check("a teacher cannot upload card images", r.status_code in (302, 403) and one("SELECT COUNT(*) FROM id_card_assets") == 1)
with_image = {"orientation": "landscape", "background": {"color": "white", "image": ASSET},
              "sides": {"front": [{"type": "image", "asset": ASSET, "x": 2, "y": 2, "w": 10, "h": 10, "clip": "hexagon"},
                                  {"type": "barcode", "field": "admission_no", "x": 40, "y": 40, "w": 30, "h": 10},
                                  {"type": "text", "text": "</script><script>alert(1)</script>", "x": 2, "y": 30, "w": 40, "h": 6}],
                        "back": [{"type": "text", "text": "Back", "x": 2, "y": 2, "w": 20, "h": 5}]}}
r = officer.post(DES, {"layout": json.dumps(with_image)}, page=DES)
saved = json.loads(one("SELECT layout_json FROM id_card_designs WHERE kind = 'student'"))
check("the school's own image is kept on the design, and as its background", saved["sides"]["front"][0].get("asset") == ASSET
      and saved["background"]["image"] == ASSET and saved["sides"]["front"][0]["clip"] == "hexagon")
officer.post(f"{DES}/lock", {"lock": "1"}, page=DES)
r = officer.post(f"/admin/id-cards/assets/{ASSET}/delete", {}, page=DES)
check("an image a locked design uses cannot be removed", r.status_code == 409 and one("SELECT COUNT(*) FROM id_card_assets") == 1)
officer.post(f"{DES}/lock", {"lock": "0"}, page=DES)
officer.post(f"{DES}/reset", {}, page=DES)
check("starting again restores the standard design", "STUDENT IDENTITY CARD" in (one("SELECT layout_json FROM id_card_designs WHERE kind = 'student'") or ""))
officer.post(f"{DES}/lock", {"lock": "1"}, page=DES)
r = officer.post(DES, {"layout": json.dumps(hostile)}, page=DES)
check("a locked design cannot be saved", r.status_code == 409 and "STUDENT IDENTITY CARD" in one("SELECT layout_json FROM id_card_designs WHERE kind = 'student'"))
officer.post(f"{DES}/reset", {}, page=DES)
check("…nor started again", "locked" in officer.said())
officer.post(f"{DES}/lock", {"lock": "0"}, page=DES)
check("unlocking it allows changes again", one("SELECT locked FROM id_card_designs WHERE kind = 'student'") == 0)
check("each step is in the activity log", {a for (a,) in sql("SELECT DISTINCT action FROM audit_logs WHERE action LIKE 'id_card_design%%'")}
      >= {"id_card_design_saved", "id_card_design_reset", "id_card_design_locked", "id_card_design_unlocked"})

# ================================================================ C. printing
r = officer.post("/admin/id-cards/print", {"kind": "student", "person": [STU["Ada"], STU["Bayo"]], "backs": "1"}, page="/admin/id-cards")
sheet = r.get_data(as_text=True)
cards = sql("SELECT person_id, token, serial, status, printed_count FROM id_cards ORDER BY id")
check("printing issues each ticked student a card with a random token and a serial",
      r.status_code == 200 and len(cards) == 2 and {c[2] for c in cards} == {"STU-000001", "STU-000002"}
      and all(len(c[1]) >= 20 for c in cards) and cards[0][1] != cards[1][1], str(cards))
data = json.loads(re.search(r'<script type="application/json" id="idcSheet"[^>]*>(.*?)</script>', sheet, re.S).group(1))
check("the sheet has each card's front and back, its QR code, barcode and the student's details",
      sheet.count('data-side="front"') == 2 and sheet.count('data-side="back"') == 2 and len(data["cards"]) == 2
      and all(c["qr"][0].startswith("M") for c in data["cards"]) and data["cards"][0]["data"]["name"] == "Ada Okafor"
      and data["cards"][1]["data"]["admission_no"] == "ID-2" and data["cards"][0]["data"]["serial"] == "STU-000001"
      and str(ASSET) in data["common"]["assets"] and "id-card-render.js" in sheet)
check("…with text that tries to end the data block kept as data", "</script><script>alert(1)" not in sheet)
check("…and is never cached", "no-store" in r.headers.get("Cache-Control", ""))
officer.post("/admin/id-cards/print", {"kind": "student", "person": [STU["Ada"]]}, page="/admin/id-cards")
officer.post(DES, {"layout": json.dumps(with_image)}, page=DES)
r = officer.post("/admin/id-cards/print", {"kind": "student", "person": [STU["Ada"]]}, page="/admin/id-cards")
data2 = json.loads(re.search(r'<script type="application/json" id="idcSheet"[^>]*>(.*?)</script>', r.get_data(as_text=True), re.S).group(1))
check("a design with a barcode prints each card's own barcode", data2["cards"][0]["barcodes"].get("admission_no", [""])[0].startswith("M"))
officer.post(f"{DES}/reset", {}, page=DES)
check("a second print reuses the card", one("SELECT COUNT(*) FROM id_cards") == 2
      and one("SELECT printed_count FROM id_cards WHERE person_id = :p", p=STU["Ada"]) == 3)
check("printing is in the activity log", one("SELECT COUNT(*) FROM audit_logs WHERE action = 'id_cards_printed'") == 3)
sql("UPDATE students SET archived_at = '2026-10-13' WHERE id = :s", s=STU["Chidi"])
r = officer.post("/admin/id-cards/print", {"kind": "student", "person": [STU["Bayo"], STU["Chidi"]]}, page="/admin/id-cards")
check("a student this account may not print for (one who has left) stops the whole print",
      r.status_code == 302 and "nothing was printed" in officer.said() and one("SELECT COUNT(*) FROM id_cards") == 2)

# The student's details a design can show, and only those it shows reach the printed sheet.
sql("UPDATE students SET genotype = 'AS', guardian_name = 'Mr Emeka Okafor', date_of_birth = '2014-03-12', "
    "drug_allergies = 'PENICILLIN-SECRET' WHERE id = :s", s=STU["Ada"])
plain = officer.post("/admin/id-cards/print", {"kind": "student", "person": [STU["Ada"]]}, page="/admin/id-cards").get_data(as_text=True)
studio = officer.text(DES)
check("the studio's Details tab offers the student's details (date of birth, genotype, guardian and more)",
      all(f'data-field="{f}"' in studio for f in ("date_of_birth", "age", "genotype", "guardian_name", "state_of_origin", "religion")))
check("a design that does not show a detail never puts it in the printed sheet",
      "Mr Emeka Okafor" not in plain and '"genotype"' not in plain and "PENICILLIN" not in plain)
detailed = {"orientation": "landscape", "background": {"color": "white"}, "sides": {"front": [
    {"type": "field", "field": f, "x": 4, "y": 4 + 6 * i, "w": 60, "h": 5, "label": f}
    for i, f in enumerate(("name", "date_of_birth", "genotype", "guardian_name"))], "back": []}}
officer.post(DES, {"layout": json.dumps(detailed)}, page=DES)
r = officer.post("/admin/id-cards/print", {"kind": "student", "person": [STU["Ada"]]}, page="/admin/id-cards")
shown = json.loads(re.search(r'<script type="application/json" id="idcSheet"[^>]*>(.*?)</script>', r.get_data(as_text=True), re.S).group(1))["cards"][0]["data"]
check("…and one that shows them prints the student's own, and nothing else of the record",
      shown == {"name": "Ada Okafor", "date_of_birth": "12 March 2014", "genotype": "AS", "guardian_name": "Mr Emeka Okafor"}
      and "PENICILLIN" not in r.get_data(as_text=True), str(shown))
officer.post(f"{DES}/reset", {}, page=DES)

# ================================================================ D. the check page
ADA_TOKEN = one("SELECT token FROM id_cards WHERE person_id = :p", p=STU["Ada"])
visitor = Person("visitor")
r = visitor.get(f"/id/{ADA_TOKEN}")
body = r.get_data(as_text=True)
check("a valid card's code opens the check page with no sign-in: school, name, class, photo, Valid",
      r.status_code == 200 and "Valid card" in body and "Ada Okafor" in body and "JSS 1" in body and "Alpha School" in body
      and f"/id/{ADA_TOKEN}/photo" in body and "ID-1" not in body)
check("…never cached or indexed", "no-store" in r.headers.get("Cache-Control", "") and "noindex" in r.headers.get("X-Robots-Tag", ""))
photo = visitor.get(f"/id/{ADA_TOKEN}/photo")
check("…and the photograph is served for a valid card", photo.status_code == 200 and photo.headers.get("Content-Type") == "image/png"
      and photo.data[:4] == b"\x89PNG")
r = visitor.get("/id/not-a-real-token-at-all")
check("an unknown code says so", r.status_code == 404 and "No such card" in r.get_data(as_text=True))
ADA_CARD = one("SELECT id FROM id_cards WHERE person_id = :p AND status = 'active'", p=STU["Ada"])
officer.post(f"/admin/id-cards/cards/{ADA_CARD}/replace", {"reason": "Lost"}, page="/admin/id-cards")
check("replacing a lost card cancels it and issues a new one",
      sql("SELECT status FROM id_cards WHERE person_id = :p ORDER BY id", p=STU["Ada"]) == [("cancelled",), ("active",)])
body = visitor.text(f"/id/{ADA_TOKEN}")
check("…the old code now says it was cancelled", "Not valid: this card was cancelled" in body and "Valid card" not in body)
check("…and no longer serves the photograph", visitor.get(f"/id/{ADA_TOKEN}/photo").status_code == 404)
BAYO_TOKEN = one("SELECT token FROM id_cards WHERE person_id = :p", p=STU["Bayo"])
sql("UPDATE students SET archived_at = '2026-10-13' WHERE id = :s", s=STU["Bayo"])
check("a student who has left shows as not valid", "no longer at the school" in visitor.text(f"/id/{BAYO_TOKEN}"))
teacher_cancel = teacher.post(f"/admin/id-cards/cards/{ADA_CARD + 2}/cancel", {"reason": "x"})
check("a teacher without the permission cannot cancel a card", teacher_cancel.status_code in (302, 403)
      and one("SELECT status FROM id_cards WHERE id = :i", i=ADA_CARD + 2) == "active")

# ================================================================ E. staff cards
staff = officer.text("/admin/id-cards?kind=staff")
check("the staff list leaves out the platform's own accounts", "Records Officer" in staff and "platform@" not in staff and "Ops" not in staff)
r = officer.post("/admin/id-cards/print", {"kind": "staff", "person": [TEACHER]}, page="/admin/id-cards")
check("a staff card prints, with its own serial", r.status_code == 200 and "STF-" in r.get_data(as_text=True)
      and "Maths Teacher" in r.get_data(as_text=True))
staff_studio = officer.text("/admin/id-cards/design/staff")
check("the staff studio's Details tab offers the staff member's details (title, employed since, emergency contact)",
      all(f'data-field="{f}"' in staff_studio for f in ("title", "employed_since", "nok_name", "nok_phone", "category", "staff_no"))
      and 'data-field="genotype"' not in staff_studio)

# ================================================================ F. teaching staff: no studios; a class teacher prints for their own class
# A school that went further and gave its class teachers every studio permission: still no studio for them.
CT_ROLE = one("SELECT id FROM admin_types WHERE name = 'Class Teacher'")
for code in ("id_cards.manage", "report_cards.manage", "finance.manage"):
    sql("INSERT INTO admin_type_permissions (admin_type_id, permission_id, granted_at) "
        "SELECT :r, id, '2026-10-14' FROM permissions WHERE code = :c ON CONFLICT DO NOTHING", r=CT_ROLE, c=code)
check("the Class Teacher role can print ID cards from the start",
      one("SELECT COUNT(*) FROM admin_type_permissions tp JOIN permissions p ON p.id = tp.permission_id "
          "WHERE tp.admin_type_id = :r AND p.code = 'id_cards.print'", r=CT_ROLE) == 1)
J2 = one("SELECT id FROM school_classes WHERE name = 'JSS 2'")
with A.app.app_context():
    with tenant_context(INFO):
        from models import Student, StudentEnrolment, db
        dayo = Student(admission_no="ID-4", student_number="ID-4", first_name="Dayo", last_name="Bello", created_at=now, active=1,
                       school_id=school_id, gender="Male", account_active=1, password_must_change=0)
        db.session.add(dayo)
        db.session.flush()
        db.session.add(StudentEnrolment(student_id=dayo.id, class_id=J2, session_id=SESSION, enrolled_at=now, active=1))
        DAYO = dayo.id
        db.session.commit()
        db.session.remove()
role["Class Teacher"] = CT_ROLE
form_teacher, FORM_TEACHER = new_staff("form.teacher", "Class Teacher", {"duty_class_teacher": [str(J1)]})
menu = form_teacher.text("/admin/school")
check("a class teacher has ID cards in the menu, but no Design studios",
      "/admin/id-cards" in menu and "/admin/design-studios" not in menu)
listing = form_teacher.text("/admin/id-cards")
check("…sees only their own class's students, with no Staff tab and no design link",
      "Okafor Ada" in listing and "Bello Dayo" not in listing and "kind=staff" not in listing
      and "/admin/id-cards/design/" not in listing)
check("…and asking for staff cards still shows their class's students", "Bello Dayo" not in form_teacher.text("/admin/id-cards?kind=staff")
      and "Okafor Ada" in form_teacher.text("/admin/id-cards?kind=staff"))
cards_before = one("SELECT COUNT(*) FROM id_cards")
r = form_teacher.post("/admin/id-cards/print", {"kind": "student", "person": [STU["Ada"], DAYO]}, page="/admin/id-cards")
check("…cannot print a card for a student of another class", r.status_code in (302, 403)
      and one("SELECT COUNT(*) FROM id_cards") == cards_before)
r = form_teacher.post("/admin/id-cards/print", {"kind": "staff", "person": [OFFICER]}, page="/admin/id-cards")
check("…nor a staff card", r.status_code in (302, 403) and one("SELECT COUNT(*) FROM id_cards") == cards_before)
r = form_teacher.post("/admin/id-cards/print", {"kind": "student", "person": [STU["Ada"]]}, page="/admin/id-cards")
check("…and prints their own class's cards", r.status_code == 200 and "Ada" in r.get_data(as_text=True), f"{r.status_code} {r.headers.get('Location')} {form_teacher.said()}")
STAFF_CARD = one("SELECT id FROM id_cards WHERE kind = 'staff' AND status = 'active' LIMIT 1")
form_teacher.post(f"/admin/id-cards/cards/{STAFF_CARD}/cancel", {"reason": "x"}, page="/admin/id-cards")
check("…cannot cancel a card outside their class (here, a staff card), even with the permission to cancel",
      one("SELECT status FROM id_cards WHERE id = :i", i=STAFF_CARD) == "active")
for path in ("/admin/design-studios", "/admin/id-cards/design/student", "/admin/report-cards/studio", "/admin/finance/receipts/studio"):
    r = form_teacher.get(path)
    check(f"a class teacher is refused {path}, even holding the studio's permission", r.status_code in (302, 403)
          and "iddData" not in r.get_data(as_text=True))
before = one("SELECT COUNT(*) FROM id_card_design_versions")
r = form_teacher.post("/admin/id-cards/design/student", {"layout": "{}"}, page="/admin/id-cards")
check("…and cannot save a design", r.status_code in (302, 403) and one("SELECT COUNT(*) FROM id_card_design_versions") == before)
check("the refusal is in the activity log", one("SELECT COUNT(*) FROM audit_logs WHERE action = 'authorization_denied' "
                                                "AND details LIKE '%design studios%'") >= 4)
check("a subject teacher has neither ID cards nor Design studios",
      "/admin/id-cards" not in teacher.text("/admin/school") and "/admin/design-studios" not in teacher.text("/admin/school"))
role["Head Teacher"] = one("SELECT id FROM admin_types WHERE name = 'Head Teacher'")
head, HEAD = new_staff("the.head", "Head Teacher")
check("the head teacher opens every design studio on their role", all(head.get(path).status_code == 200 for path in (
      "/admin/design-studios", "/admin/id-cards/design/student", "/admin/report-cards/studio", "/admin/finance/receipts/studio"))
      and "/admin/design-studios" in head.text("/admin/school"))
override(FORM_TEACHER, "id_cards.manage")
check("a class teacher given the ID card permission personally opens the ID card studio (only that one)",
      "iddData" in form_teacher.text("/admin/id-cards/design/student")
      and form_teacher.get("/admin/report-cards/studio").status_code in (302, 403)
      and "/admin/design-studios" in form_teacher.text("/admin/school"))
check("a class teacher still writes report card comments: their report cards page opens as before",
      form_teacher.get("/admin/school/report-cards").status_code == 200)

check("no page answered with a server error", not PROBLEMS, "; ".join(PROBLEMS[:5]))
failed = [name for name, ok, _ in results if not ok]
print()
print(f"{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", "; ".join(failed))
    raise SystemExit(1)
