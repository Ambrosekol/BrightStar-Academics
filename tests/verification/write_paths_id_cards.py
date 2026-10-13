"""ID cards, proven end to end on PostgreSQL. In plain words:

A. Who: the School Admin and a Records Officer see ID cards; a Subject Teacher (without the permission) is refused
   the list, printing and the designer.
B. The designer: the standard design to start from; a saved design is rebuilt from known keys only (an unknown
   detail, a colour carrying CSS, an item off the card, a script typed as text are all made harmless); too many items
   are refused. Locking stops saving and starting again until it is unlocked; each step is logged.
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
      r.status_code == 200 and saved.get("background") == "white" and len(front) == 2
      and front[0] == {"type": "text", "x": 0, "y": 53.0, "w": 85.6, "h": 1.0, "size": 28, "bold": False, "upper": False,
                       "align": "left", "color": "ink", "text": "<script>alert(1)</script>"}
      and front[1]["field"] == "admission_no" and front[1]["color"] == "accent", str(front))
r = officer.post(DES, {"layout": json.dumps({"sides": {"front": [{"type": "rect"}] * 31}})}, page=DES)
check("a side with too many items is refused", r.status_code == 400 and "at most 30" in r.get_json().get("error", ""))
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
check("the sheet has each card's front and back, its QR code and the student's details",
      sheet.count('<div class="idc"') == 4 and sheet.count('<path d="M') == 2 and "Ada Okafor" in sheet and "ID-2" in sheet
      and "STU-000001" in sheet)
check("…and is never cached", "no-store" in r.headers.get("Cache-Control", ""))
officer.post("/admin/id-cards/print", {"kind": "student", "person": [STU["Ada"]]}, page="/admin/id-cards")
check("a second print reuses the card", one("SELECT COUNT(*) FROM id_cards") == 2
      and one("SELECT printed_count FROM id_cards WHERE person_id = :p", p=STU["Ada"]) == 2)
check("printing is in the activity log", one("SELECT COUNT(*) FROM audit_logs WHERE action = 'id_cards_printed'") == 2)
sql("UPDATE students SET archived_at = '2026-10-13' WHERE id = :s", s=STU["Chidi"])
r = officer.post("/admin/id-cards/print", {"kind": "student", "person": [STU["Bayo"], STU["Chidi"]]}, page="/admin/id-cards")
check("a student this account may not print for (one who has left) stops the whole print",
      r.status_code == 302 and "nothing was printed" in officer.said() and one("SELECT COUNT(*) FROM id_cards") == 2)

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

check("no page answered with a server error", not PROBLEMS, "; ".join(PROBLEMS[:5]))
failed = [name for name, ok, _ in results if not ok]
print()
print(f"{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", "; ".join(failed))
    raise SystemExit(1)
