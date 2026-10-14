"""The receipt studio, proven end to end on PostgreSQL. In plain words:

A. Who: the School Admin and the bursar (finance.manage) open the receipt studio; a cashier who only records payments
   is refused the studio, saving, the preview, switching it on, locking and versions.
B. Saving: a hostile design is rebuilt from known keys only (an unknown page size, item type or detail, a report card's
   block, a colour carrying CSS, a stamp's word too long, an unknown stamp shape or summary item, another signature);
   text typed into it stays plain text; not a design, too many items or too large is refused; no CSRF token is refused.
   An unchanged save keeps no new version. Every template, on every page size, is a design the rules keep.
C. Locking stops saving, and an image a locked receipt design uses cannot be removed. The preview is a PDF of the
   design with a made-up payment, never cached.
D. Switching it on: the staff and parent receipt PDFs, the payment page and the printout are drawn from the design with
   the payment's own details; a voided receipt is always marked VOID and never shows the PAID stamp. The design is read
   once and kept, not for every receipt. Switching back restores the standard receipt. Every step is logged.
E. The Design studios page offers the bursar only the receipt studio; a phone is asked to use a computer; the new routes
   are in the guard files.
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
TMP = tempfile.mkdtemp(prefix="brightstars_studio_")

import _pg  # noqa: E402

PREFIX, DROP_TEST_DATABASES = _pg.setup('rctstudio')
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

import sqlalchemy as sa  # noqa: E402
from PIL import Image  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

import app as A  # noqa: E402
from control_plane import provisioning as pv  # noqa: E402
from control_plane.context import tenant_context  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import engine_for  # noqa: E402
from core import receipt_design as recd  # noqa: E402
from core.security import ADMIN_ENDPOINT_PERMISSIONS  # noqa: E402
from models import (  # noqa: E402
    Admin, AdminType, FinancePayment, ParentAccount, ParentStudentLink, School as SchoolRow, Student, StudentEnrolment,
)

results, PROBLEMS = [], []
PL, UA = "http://platform.test", "http://alpha.portal.test"
TERM, SLUG = "First Term", "first-term"
NOW, REL = "2026-09-01T09:00:00+00:00", "2026-12-18T10:00:00+00:00"
PASSWORD = "Fixture-password-9"
HASH = generate_password_hash(PASSWORD, method="pbkdf2:sha256:1000")
STUDIO = "/admin/finance/receipts/studio"


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




# ---------------------------------------------------------------- the school, its people and two payments
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
OP_ID = one("SELECT id FROM admins ORDER BY id LIMIT 1")


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


bursar, BURSAR = staff("the.bursar", "Finance Manager")


def override(aid, code):
    """The School Admin gives one member of staff a permission personally (an override on their staff record)."""
    sql("INSERT INTO admin_permissions (admin_id, permission_id, granted_at) SELECT :a, id, '2026-10-14' FROM permissions "
        "WHERE code = :c ON CONFLICT DO NOTHING", a=aid, c=code)
    from core.short_cache import clear
    clear()
cashier, CASHIER = staff("the.cashier", "Finance Records Officer")


def setup():
    school_id = A.db.session.scalar(sa.select(SchoolRow.id))
    st = Student(admission_no="ALP/0001", student_number="ALP/0001", first_name="Ada", last_name="Okafor", gender="Female",
                 created_at=NOW, active=1, school_id=school_id, account_active=1, password_must_change=0, guardian_name="Mrs Okafor")
    A.db.session.add(st)
    A.db.session.flush()
    A.db.session.add(StudentEnrolment(student_id=st.id, class_id=J1, session_id=CUR, enrolled_at=NOW, active=1, school_id=school_id))
    paid = FinancePayment(receipt_no="RCT/TEST/0001", student_id=st.id, session_id=CUR, amount=85000.0, category="School Fees",
                          method="Bank transfer", reference="TRF-1234", paid_at=NOW, recorded_by=OP_ID, status="posted",
                          created_at=NOW, payer_name="Mrs Ngozi Okafor")
    void = FinancePayment(receipt_no="RCT/TEST/0002", student_id=st.id, session_id=CUR, amount=5000.0, category="School Fees",
                          method="Cash", paid_at=NOW, recorded_by=OP_ID, status="voided", created_at=NOW, voided_at=NOW,
                          voided_by=OP_ID, void_reason="Entered twice", payer_name="Mrs Ngozi Okafor")
    A.db.session.add_all([paid, void])
    parent = ParentAccount(username="mum", display_name="Mrs Okafor", email="mum@family.example", password_hash=HASH, active=1,
                           password_must_change=0, created_at=NOW)
    A.db.session.add(parent)
    A.db.session.flush()
    A.db.session.add(ParentStudentLink(parent_id=parent.id, student_id=st.id, relationship="Mother", active=1, created_at=NOW))
    return st.id, paid.id, void.id


ADA, PAID, VOID = orm(setup)
parent = Person("/parent/password", "parent")
parent.post("/login", {"username": "mum", "password": PASSWORD}, page="/login")
PDF = f"/admin/finance/receipts/{PAID}/pdf"
VOID_PDF = f"/admin/finance/receipts/{VOID}/pdf"
PAGE = f"/admin/finance/receipts/{PAID}"
PRINT = f"/admin/finance/receipts/{PAID}/print"
PARENT_PDF = f"/parent/children/{ADA}/receipts/{PAID}/pdf"


def logged(action):
    return one("SELECT COUNT(*) FROM audit_logs WHERE action = :a", a=action) or 0


def versions():
    return one("SELECT COUNT(*) FROM receipt_design_versions") or 0


def save(person, layout, token=True):
    return person.post(STUDIO, {"layout": layout if isinstance(layout, str) else json.dumps(layout)}, page=STUDIO, token=token)


# ================================================================ A. who
r = op.get(STUDIO)
body = r.get_data(as_text=True)
check("A1 the School Admin opens the receipt studio, with its blocks and the payment's details",
      r.status_code == 200 and 'id="iddData"' in body and "Amount paid" in body and "Paid stamp" in body
      and 'data-field="amount_words"' in body and 'data-field="balance"' in body and "receipt-render.js" in body)
cfg = json.loads(re.search(r'<script type="application/json" id="iddData"[^>]*>(.*?)</script>', body, re.S).group(1))
check("A2 the studio starts from the Classic template on A5 landscape, with four templates and four page sizes",
      cfg["mode"] == "receipt" and len(cfg["templates"]) == 4 and cfg["layout"] == recd.default_layout("classic")
      and (cfg["layout"]["page"], cfg["layout"]["orientation"]) == ("A5", "landscape") and len(cfg["page_choices"]) == 4)
check("A3 the bursar (Finance Manager) is refused on the role alone: only the School Admin, a head teacher or a "
      "personal override opens a studio", bursar.get(STUDIO).status_code in (302, 403))
override(BURSAR, "finance.manage")
check("A3 …and opens it once the School Admin gives the bursar the permission personally", bursar.get(STUDIO).status_code == 200)
denied = [cashier.get(STUDIO).status_code, save(cashier, recd.default_layout()).status_code,
          cashier.post(STUDIO + "/preview", {"layout": json.dumps(recd.default_layout())}).status_code,
          cashier.post(STUDIO + "/use", {"use": "1"}).status_code, cashier.post(STUDIO + "/lock", {"lock": "1"}).status_code,
          cashier.get(STUDIO + "/versions/1").status_code]
check("A4 a cashier who only records payments is refused the studio, saving, the preview, switching it on, locking and versions",
      all(code in (302, 403) for code in denied) and one("SELECT COUNT(*) FROM receipt_designs") == 0, str(denied))
check("A5 every receipt studio route needs finance.manage",
      all(ADMIN_ENDPOINT_PERMISSIONS.get(e) == "finance.manage" for e in (
          "admin_receipt_studio", "admin_receipt_studio_save", "admin_receipt_studio_lock", "admin_receipt_studio_use",
          "admin_receipt_studio_version", "admin_receipt_studio_preview", "admin_receipt_asset_upload", "admin_receipt_asset_delete")))
buf = io.BytesIO()
Image.new("RGB", (40, 40), (200, 30, 30)).save(buf, "PNG")
PNG = buf.getvalue()
asset = bursar.post(STUDIO + "/assets", page=STUDIO, files={"image": ("stamp.png", PNG)}).get_json()["asset"]
check("A6 the bursar uploads one of the school's images from the receipt studio", bool(asset.get("id")))

# ================================================================ B. saving
hostile = recd.default_layout("modern")
hostile["page"] = "A3"
hostile["sides"]["page1"] += [
    {"type": "script", "x": 1, "y": 1, "w": 5, "h": 5},
    {"type": "results", "x": 1, "y": 1, "w": 5, "h": 5},
    {"type": "field", "field": "password_hash", "x": 1, "y": 1, "w": 5, "h": 5},
    {"type": "text", "text": "<img src=x onerror=alert(1)>", "x": 99999, "y": 5, "w": 40, "h": 6, "color": "red;background:url(x)"},
    {"type": "summary", "items": ["balance", "evil"], "x": 10, "y": 10, "w": 100, "h": 15},
    {"type": "stamp", "text": "PAID" * 10, "shape": "star", "x": 10, "y": 10, "w": 30, "h": 30},
    {"type": "signature", "who": "head", "x": 10, "y": 10, "w": 30, "h": 10},
]
before = versions()
r = save(op, hostile)
kept = (r.get_json() or {}).get("layout") or {}
p1 = kept.get("sides", {}).get("page1", [])
types = [e["type"] for e in p1]
text_el = next((e for e in p1 if "onerror" in e.get("text", "")), {})
stamp_el = [e for e in p1 if e["type"] == "stamp"][-1] if "stamp" in types else {}
summary_el = [e for e in p1 if e["type"] == "summary"][-1] if "summary" in types else {}
sign_el = [e for e in p1 if e["type"] == "signature"][-1] if "signature" in types else {}
check("B1 a hostile receipt design is saved only as what the rules keep",
      r.status_code == 200 and kept.get("page") == "A5" and "script" not in types and "results" not in types
      and not any(e.get("field") == "password_hash" for e in p1))
check("B2 ...text stays plain, a CSS colour is replaced, a stamp's word is cut short, an unknown stamp shape and summary item go, a signature is the authorised one",
      text_el.get("text") == "<img src=x onerror=alert(1)>" and text_el.get("color") == "ink" and text_el.get("x") <= 210
      and len(stamp_el.get("text", "")) <= 16 and stamp_el.get("shape") == "round" and summary_el.get("items") == ["balance"]
      and sign_el.get("who") == "authorised", str([text_el.get("x"), stamp_el, summary_el.get("items")]))
check("B3 kept as a version and logged", versions() == before + 1 and logged("receipt_design_saved") == 1)
save(op, kept)
check("B4 saving it again unchanged keeps no new version", versions() == before + 1)
too_many = recd.default_layout()
too_many["sides"]["page1"] = [{"type": "text", "text": f"t{i}", "x": 1, "y": 1, "w": 5, "h": 5} for i in range(recd.MAX_ELEMENTS + 1)]
bad = [save(op, "not json").status_code, save(op, too_many).status_code, save(op, json.dumps({"x": "y" * 500_000})).status_code]
check("B5 not a design, too many items or too large is refused and nothing changes", bad == [400, 400, 400]
      and json.loads(one("SELECT layout_json FROM receipt_designs")) == kept, str(bad))
check("B6 no CSRF token, no save", save(op, recd.default_layout("bold"), token=False).status_code in (400, 403)
      and json.loads(one("SELECT layout_json FROM receipt_designs")) == kept)
check("B7 every template is a design the rules keep as it is, on every page size",
      all(recd.clean_layout(recd.default_layout(t)) == recd.default_layout(t) for t in recd.TEMPLATES)
      and all(recd.clean_layout(dict(recd.default_layout(), page=p, orientation=o))["page"] == p for p, o, _ in recd.PAGE_CHOICES))

# ================================================================ C. locking, the preview
design = recd.default_layout("classic")
design["sides"]["page1"] += [
    {"type": "text", "text": "RECEIPTMARK4417", "font": "helvetica", "x": 60, "y": 140, "w": 60, "h": 5, "size": 8},
    {"type": "image", "asset": asset["id"], "x": 190, "y": 130, "w": 12, "h": 12},
]
save(op, design)
op.post(STUDIO + "/lock", {"lock": "1"}, page=STUDIO)
check("C1 a locked receipt design cannot be saved, and is logged", save(op, recd.default_layout("bold")).status_code == 409
      and logged("receipt_design_locked") == 1)
r = bursar.post(f"{STUDIO}/assets/{asset['id']}/delete", page=STUDIO)
check("C2 an image a locked receipt design uses cannot be removed", r.status_code == 409 and "receipt design" in r.get_data(as_text=True))
op.post(STUDIO + "/lock", {"lock": "0"}, page=STUDIO)
r = op.post(STUDIO + "/preview", {"layout": json.dumps(design)}, page=STUDIO)
words = pdf_words(r.get_data()) if r.status_code == 200 else ""
check("C3 the preview is a PDF of the design with a made-up payment and the school's name, never cached",
      r.status_code == 200 and r.get_data()[:4] == b"%PDF" and "no-store" in r.headers.get("Cache-Control", "")
      and "RECEIPTMARK4417" in words and "RCT/2026/00042" in words and "ALPHA SCHOOL" in words.upper(), words[:200])

# ================================================================ D. switching it on
r = op.get(PDF)
check("D1 until it is switched on, receipts are the standard ones", r.status_code == 200 and "RECEIPTMARK4417" not in pdf_words(r.get_data())
      and 'id="rcdReceipt"' not in op.text(PAGE))
op.post(STUDIO + "/use", {"use": "1"}, page=STUDIO)
check("D2 switching it on is kept and logged", one("SELECT in_use FROM receipt_designs") == 1 and logged("receipt_design_used") == 1)
for who, person, path in (("staff", op, PDF), ("parent", parent, PARENT_PDF)):
    r = person.get(path)
    words = pdf_words(r.get_data()) if r.status_code == 200 else ""
    check(f"D3 the {who}'s receipt PDF is drawn from the design, with this payment's own details",
          r.status_code == 200 and "RECEIPTMARK4417" in words and "RCT/TEST/0001" in words and "Mrs Ngozi Okafor" in words
          and "₦85,000.00" in words and "eighty-five thousand" in words.lower() and "PAID" in words and "VOID" not in words, words[:300])
for label, path in (("payment page", PAGE), ("printout", PRINT)):
    body = op.text(path)
    found = re.search(r'<script type="application/json" id="rcdReceipt"[^>]*>(.*?)</script>', body, re.S)
    data = json.loads(found.group(1)) if found else {}
    check(f"D4 the {label} draws the design with the payment's details",
          bool(found) and data["view"]["data"]["receipt_no"] == "RCT/TEST/0001" and data["view"]["report"]["amount"] == "₦85,000.00"
          and data["voided"] is False and "onerror=alert" not in body and "receipt-render.js" in body)
r = op.get(VOID_PDF)
words = pdf_words(r.get_data())
check("D5 a voided payment's receipt is marked VOID across the page, and its PAID stamp is left off",
      r.status_code == 200 and "VOID" in words and "This receipt was cancelled" in words and "RECEIPTMARK4417" in words
      and " PAID " not in f" {words} ", words[-200:])
check("D6 ...and its page says so too", json.loads(re.search(r'id="rcdReceipt"[^>]*>(.*?)</script>', op.text(f"/admin/finance/receipts/{VOID}"), re.S).group(1))["voided"] is True)
statements = []


def _note(conn, cursor, statement, *rest):
    statements.append(statement)


sa.event.listen(sa.engine.Engine, "before_cursor_execute", _note)
for _ in range(3):
    op.get(PDF); parent.get(PARENT_PDF)
sa.event.remove(sa.engine.Engine, "before_cursor_execute", _note)
check("D7 the design is read once and kept, not for every receipt", not [s for s in statements if "receipt_designs" in s])
op.post(STUDIO + "/use", {"use": "0"}, page=STUDIO)
check("D8 switching back restores the standard receipt, and is logged", "RECEIPTMARK4417" not in pdf_words(op.get(PDF).get_data())
      and 'id="rcdReceipt"' not in op.text(PAGE) and logged("receipt_design_standard") == 1)

# ================================================================ E. the Design studios page, phones, guard files
body = bursar.text("/admin/design-studios")
check("E1 Design studios offers the bursar the receipt studio, and not the others",
      "/admin/finance/receipts/studio" in body and "/admin/report-cards/studio" not in body and "/admin/id-cards/design/student" not in body)
IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
          "Mobile/15E148 Safari/604.1")
r = op.c.get(STUDIO, base_url=UA, headers={"User-Agent": IPHONE}, environ_base={"REMOTE_ADDR": op.addr})
check("E2 on a phone the receipt studio asks for a computer", "Open this on a computer" in r.get_data(as_text=True)
      and 'id="iddData"' not in r.get_data(as_text=True))
cover = open(os.path.join(HERE, "pg_posts_coverage.py"), encoding="utf-8").read()
check("E3 the new write routes are listed for the write-route coverage check",
      all(f'"{STUDIO}{e}"' in cover for e in ("", "/lock", "/use", "/preview", "/assets", "/assets/<int:asset_id>/delete")))
check("E4 nothing answered with a server error", not PROBLEMS, "; ".join(PROBLEMS[:5]))

passed = sum(1 for _, ok, _ in results if ok)
print(f"\n{passed}/{len(results)} checks passed")
sys.exit(0 if passed == len(results) else 1)
