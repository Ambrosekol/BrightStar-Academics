"""The fee receipt: the PDF, the email, the WhatsApp message and the authorised signature, end to end
on PostgreSQL.

Three throw-away schools: "alpha" (own logo, address and phone), "beta" (own, different logo) and "gamma"
(no logo, no contact details and no mail or WhatsApp account of its own). Everything is done through the
real admin, parent and finance routes with CSRF tokens read from real pages. The application's own PDF
builder and its own sending code run for real; only the very last step is caught (the mail connection
and ``urllib.request.urlopen``), so the messages can be read. The PDF is read without adding a library:
its streams are decompressed with zlib and its text is decoded through the fonts' own maps. In plain words,
it proves:

The receipt PDF
* it carries the school's own name, address, phone and email, the receipt number with the school's own
  prefix, who paid, the amount in figures and in words, what it was for and for which student and class,
  how it was paid, the date and the "Authorised Signature" line;
* the school's own logo is drawn; a school with no logo gets its name in that place; no school ever
  shows another school's logo, name, address, phone or numbers (a leftover from the school this
  platform grew out of used to be printed on every school's receipt);
* a voided payment's receipt says VOIDED wherever it is printed, downloaded, emailed or seen by a parent;
* the printed page, the on-screen page and the PDF say the same thing.

The authorised signature
* it can be drawn or uploaded, replaces (never piles up) the one before, and is deleted when removed;
  what is refused (not an image, a fake image, a made-up action, nothing chosen) changes nothing;
* the PDF and the pages show whichever signature is current, and none when there is none;
* only a finance manager can change it, and one school's signature never appears on another's receipt.

Email and WhatsApp
* the email goes to the guardian on the student's record, from the school's own account, saying who,
  how much and what for, with the very same PDF attached; the WhatsApp message carries the same PDF
  with a caption; each attempt is logged with its outcome and who sent it;
* a school with no account of its own falls back to the platform's account, and one with neither says so;
* no guardian address, no valid phone number, a provider that is down, a token the provider rejects, a
  reply with no media id, a voided payment: each is refused or reported clearly, logged, and nothing is sent;
* only people allowed to send receipts can, and a cashier only for the payments they recorded;
* what one school sends never carries another school's name, sender or token.

Run:  python tests/verification/write_paths_receipts.py
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
import urllib.error
import urllib.request
import zlib
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # a failure may quote text from a page

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_receipts_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('receipts')
atexit.register(DROP_TEST_DATABASES)  # even if this script stops half way, its databases go
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
from PIL import Image  # noqa: E402  (already installed with reportlab)

import app as A  # noqa: E402
A.app.config['BACKGROUND_INLINE'] = True  # messages to parents run at once, so they can be checked

import blueprints.finance.helpers as FIN  # noqa: E402
from control_plane import provisioning as pv  # noqa: E402
from control_plane.context import tenant_context  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import engine_for  # noqa: E402
from core import delivery as _delivery  # noqa: E402
from core.storage import uploads_dir  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

# app.py has just read the developer's .env, which may hold real mail and WhatsApp credentials. A test
# must never be able to reach them: start from a platform with no shared account at all.
PLATFORM_ENV = ("BRIGHTSTARS_SMTP_HOST", "BRIGHTSTARS_SMTP_USER", "BRIGHTSTARS_SMTP_FROM", "BRIGHTSTARS_SMTP_PASSWORD",
                "BRIGHTSTARS_WHATSAPP_TOKEN", "BRIGHTSTARS_WHATSAPP_PHONE_NUMBER_ID", "BRIGHTSTARS_DELIVERY_KEY",
                "BRIGHTSTARS_SMTP_PORT", "BRIGHTSTARS_SMTP_SSL", "BRIGHTSTARS_SMTP_STARTTLS",
                "BRIGHTSTARS_WHATSAPP_GRAPH_VERSION")
for name in PLATFORM_ENV:
    os.environ.pop(name, None)

results = []
PL = "http://platform.test"
ALPHA, BETA, GAMMA = "http://alpha.portal.test", "http://beta.portal.test", "http://gamma.portal.test"
YEAR = datetime.now().year


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)


# ------------------------------------------------------------------ the outside world, stood in for
class Outbox:
    """Everything the application tried to send, caught at the very last step."""
    mail = []        # (the account it went through, the message)
    whatsapp = []    # (the token, the address it was sent to, the JSON or the file)
    mode = None      # None, "outage", "http401" or "nomedia"


class FakeSMTP:
    def __init__(self, settings):
        self.settings = settings

    def send_message(self, msg):
        if Outbox.mode == "outage":
            raise OSError("simulated mail outage")
        Outbox.mail.append((self.settings, msg))

    def quit(self):
        pass

    def close(self):
        pass


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urlopen(request, timeout=0):
    url = request.full_url
    if Outbox.mode == "outage":
        raise OSError("simulated WhatsApp outage")
    if Outbox.mode == "http401":
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(b"the token was not accepted"))
    token = request.get_header("Authorization")
    if url.endswith("/media"):
        Outbox.whatsapp.append({"kind": "media", "url": url, "token": token, "body": request.data,
                                "content_type": request.get_header("Content-type")})
        return FakeResponse({} if Outbox.mode == "nomedia" else {"id": "media-1"})
    Outbox.whatsapp.append({"kind": "message", "url": url, "token": token, "json": json.loads(request.data.decode())})
    return FakeResponse({"messages": [{"id": "wamid.1"}]})


_delivery._open_smtp = lambda settings: FakeSMTP(settings)
_delivery.resolve_public = lambda host: ["93.184.216.34"]
urllib.request.urlopen = fake_urlopen


# ------------------------------------------------------------------ reading a PDF without a library
def _unescape(raw):
    """A PDF literal string (backslash escapes, octal codes) as bytes."""
    out, i = bytearray(), 0
    named = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}
    while i < len(raw):
        if raw[i] != 0x5C:
            out.append(raw[i])
            i += 1
        elif 0x30 <= raw[i + 1] <= 0x37:
            j = i + 1
            while j < len(raw) and j < i + 4 and 0x30 <= raw[j] <= 0x37:
                j += 1
            out.append(int(raw[i + 1:j], 8) & 255)
            i = j
        else:
            out.append(named.get(raw[i + 1], raw[i + 1]))
            i += 2
    return bytes(out)


def pdf_read(data):
    """``{"text": all the words drawn, "images": every picture's raw pixels}`` of a PDF made by reportlab.

    Streams are decompressed with zlib (after ASCII85 where used). The text is drawn with the school's
    embedded font, so each string is turned back into characters through that font's own ToUnicode map.
    """
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

    maps = {}
    for number, obj in objects.items():
        found = re.search(rb"/ToUnicode (\d+) 0 R", obj)
        if found and b"/Type /Font" in obj:
            _, cmap = decode(objects[int(found.group(1))])
            maps[number] = {int(code, 16): chr(int(char, 16)) for code, char in
                            re.findall(rb"<([0-9A-Fa-f]{2})>\s*<([0-9A-Fa-f]{4})>", cmap or b"")}
    fonts = {}
    for obj in objects.values():
        if b"stream" not in obj and b"/Type" not in obj:
            fonts.update({name.decode(): int(number) for name, number in re.findall(rb"/(F[\w+]+) (\d+) 0 R", obj)})
    words, images = [], []
    for obj in objects.values():
        head, body = decode(obj)
        if body is None:
            continue
        if b"/Subtype /Image" in head:
            images.append(body)
        elif b" Tf" in body:
            font = None
            for m in re.finditer(rb"/(F[\w+]+) [\d.]+ Tf|\(((?:\\.|[^\\)])*)\) Tj", body, re.S):
                if m.group(1):
                    font = maps.get(fonts.get(m.group(1).decode()))
                else:
                    raw = _unescape(m.group(2))
                    words.append("".join(font.get(b, "") for b in raw) if font else raw.decode("cp1252", "replace"))
    return {"text": "\n".join(words), "images": images}


def solid(rgb, size=(64, 24), kind="PNG"):
    """A one-colour picture, and the pixels a PDF holds for it."""
    picture = Image.new("RGBA", size, rgb + (255,))
    buf = io.BytesIO()
    picture.save(buf, kind)
    return buf.getvalue(), bytes(rgb) * (size[0] * size[1])


def draws(pdf, pixels):
    """Whether the PDF contains a picture with exactly these pixels."""
    return any(pixels in image for image in pdf["images"])


RED_LOGO, RED = solid((200, 30, 30), (90, 40))       # Alpha's logo
BLUE_LOGO, BLUE = solid((30, 60, 200), (90, 40))     # Beta's logo
GREEN_SIGN, GREEN = solid((20, 160, 60))              # a drawn signature
PURPLE_SIGN, PURPLE = solid((120, 30, 160))           # an uploaded one
# The platform ships no school's logo. This is the picture a receipt used to fall back to (a file
# that belonged to the first school): a stand-in for "a picture that is nobody's logo here".
_, OLD_SCHOOL = solid((250, 200, 20), (90, 40))
OLD_SCHOOL_WORDS = ("CREATIVE", "RAINBOW", "MONTESSORI", "Charles Okeke", "Alahun", "Maza-Maza", "0803 123 4567", "0810 987 6543")


# ------------------------------------------------------------------ small helpers
def csrf_from(html):
    found = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', html)
    return found.group(1) if found else ""


class Person:
    """One signed-in (or signed-out) person at one school: a browser with its own cookies."""

    count = [0]

    def __init__(self, base, form_page="/admin/password"):
        self.base, self.form_page, self.client = base, form_page, A.app.test_client()
        Person.count[0] += 1
        self.addr = f"10.40.{Person.count[0]}.1"  # sign-in limits are per address

    def get(self, path, **kw):
        return self.client.get(path, base_url=self.base, environ_base={"REMOTE_ADDR": self.addr}, **kw)

    def text(self, path):
        return self.get(path).get_data(as_text=True)

    def post(self, path, data=None, page=None, files=None):
        """Submit a form the way a browser does: the token comes from a real page that carries a form."""
        data = dict(data or {})
        data["_csrf_token"] = csrf_from(self.text(page or self.form_page))
        for key, (filename, content) in (files or {}).items():
            data[key] = (io.BytesIO(content), filename)
        return self.client.post(path, data=data, base_url=self.base, environ_base={"REMOTE_ADDR": self.addr},
                                content_type="multipart/form-data" if files else None)

    def said(self):
        """The messages left for the next page, as one lowercase string."""
        with self.client.session_transaction(base_url=self.base) as sess:
            found = list(sess.pop("_flashes", []))
        return " | ".join(text for _, text in found).lower()


def csrf(c, path, base):
    return csrf_from(c.get(path, base_url=base).get_data(as_text=True))


def info_for(slug):
    with platform_session() as s:
        return to_info(get_tenant(s, slug))


class School:
    """A school under test: its database, and helpers to read it."""

    def __init__(self, code, base):
        self.code, self.base, self.info = code, base, info_for(code)

    def sql(self, statement, **params):
        with engine_for(self.info).begin() as conn:
            result = conn.execute(sa.text(statement), params)
            return result.fetchall() if result.returns_rows else None

    def one(self, statement, **params):
        rows = self.sql(statement, **params)
        return rows[0][0] if rows else None

    def count(self, table, where="TRUE", **params):
        return self.one(f"SELECT count(*) FROM {table} WHERE {where}", **params)

    def run(self, fn):
        with A.app.app_context(), tenant_context(self.info):
            return fn()


pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
console = A.app.test_client()
console.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                      "_csrf_token": csrf(console, "/platform/login", PL)}, base_url=PL)
SCHOOLS = (
    # code, name, logo, address, phone, email
    ("alpha", "Alpha School", ("logo.png", RED_LOGO), "12 Palm Avenue, Ikeja, Lagos", "0803 111 2222", "info@alpha-school.example"),
    ("beta", "Beta College", ("logo.png", BLUE_LOGO), "4 River Road, Abuja", "0809 555 6666", "hello@beta-college.example"),
    ("gamma", "Gamma Academy", None, "", "", ""),
)
for code, name, logo, address, phone, email in SCHOOLS:
    form = {"_csrf_token": csrf(console, "/platform/schools/new", PL), "name": name, "code": code,
            "school_brand_primary": "#0d2b52", "school_brand_accent": "#1674b9", "school_address": address,
            "school_phone": phone, "school_email": email}
    if logo:
        form["logo"] = (io.BytesIO(logo[1]), logo[0])
    console.post("/platform/schools/new", data=form, base_url=PL, content_type="multipart/form-data")
alpha, beta, gamma = School("alpha", ALPHA), School("beta", BETA), School("gamma", GAMMA)


def operator_in(school):
    """The platform operator enters a school with a single-use ticket and is its administrator there."""
    r = console.post(f"/platform/schools/{school.code}/enter",
                     data={"_csrf_token": csrf(console, f"/platform/schools/{school.code}", PL)}, base_url=PL)
    person = Person(school.base)
    person.get(r.headers["Location"][len(school.base):])
    person.get("/admin/workspace/school")
    return person


def add_admin(school, username, role_name):
    """A staff member with one of the school's standard roles, who then signs in for real."""
    with A.app.app_context(), tenant_context(school.info):
        role = A.db.session.scalars(sa.select(A.AdminType).where(A.AdminType.name == role_name)).first()
        A.db.session.add(A.Admin(username=username, display_name=username.title(),
                                 password_hash=generate_password_hash("staff-password-123"),
                                 admin_type_id=role.id, active=1, password_must_change=0,
                                 created_at="2026-01-01T00:00:00+00:00"))
        A.db.session.commit()
    person = Person(school.base)
    person.post("/login", {"username": username, "password": "staff-password-123"}, page="/login")
    person.get("/admin/workspace/school")
    return person, school.one("SELECT id FROM admins WHERE username = :u", u=username)


op_alpha, op_beta, op_gamma = operator_in(alpha), operator_in(beta), operator_in(gamma)
check("three schools were created, two with their own logo and one without",
      alpha.one("SELECT setting_value FROM school_public_settings WHERE setting_key = 'school_logo'") is not None
      and beta.one("SELECT setting_value FROM school_public_settings WHERE setting_key = 'school_logo'") is not None
      and gamma.one("SELECT setting_value FROM school_public_settings WHERE setting_key = 'school_logo'") is None)

# Alpha and Beta each set up their own mail and WhatsApp account. Gamma sets up nothing.
for school, person, phone_id in ((alpha, op_alpha, "1045123456781"), (beta, op_beta, "1045123456782")):
    person.post("/admin/school/delivery/email/save", {
        "smtp_host": f"smtp.{school.code}.test", "smtp_port": "587", "smtp_security": "starttls",
        "smtp_user": f"mailer-{school.code}", "smtp_password": "mail-pass-123",
        "smtp_from": f"office@{school.code}.example"}, page="/admin/school/delivery")
    person.post("/admin/school/delivery/whatsapp/save", {
        "whatsapp_phone_number_id": phone_id, "whatsapp_graph_version": "v23.0",
        "whatsapp_token": f"token-{school.code}"}, page="/admin/school/delivery")

STATE_ROW = {"gender": "Female", "date_of_birth": "2014-05-01", "state_of_origin": "Lagos", "blood_group": "O+",
             "genotype": "AA"}


def register_student(person, school, first, last, guardian_email, guardian_phone, class_name="JSS 1"):
    """Register a student through the real form (the school numbers them itself)."""
    class_id = school.one("SELECT id FROM school_classes WHERE name = :n", n=class_name)
    person.post("/admin/school/students/new", {
        **STATE_ROW, "first_name": first, "middle_name": "", "last_name": last, "class_id": class_id,
        "guardian_name": f"{last} Family", "guardian_phone": guardian_phone, "guardian_email": guardian_email},
        page="/admin/school/students/new")
    return school.one("SELECT id FROM students WHERE first_name = :f AND last_name = :l", f=first, l=last)


def make_parent(person, school, name, username, email, student_ids):
    """A parent account made through the real form, who then signs in and sets their own password."""
    r = person.post("/admin/school/parents/new", {"display_name": name, "email": email, "phone": "08055550000",
                                                  "username": username, "student_ids": student_ids,
                                                  "relationship": "Mother"}, page="/admin/school/parents/new")
    temp = re.search(r"<code>(.+?)</code>", r.get_data(as_text=True), re.S).group(1)
    parent = Person(school.base, "/parent/password")
    parent.post("/login", {"username": username, "password": temp}, page="/login")
    parent.post("/parent/password", {"current_password": temp, "new_password": "a-parent-password-1",
                                     "confirm_password": "a-parent-password-1"}, page="/parent/password")
    return parent


FIN_URL = "/admin/finance"


def new_fee(person, school, name, amount, category="Tuition", rule="First Term"):
    class_id = school.one("SELECT id FROM school_classes WHERE name = 'JSS 1'")
    person.post(f"{FIN_URL}/fee-items/new", {"name": name, "category": category, "applicability": rule, "amount": amount,
                                             "required": "1", "notes": "", "class_ids": [class_id]},
                page=f"{FIN_URL}/fee-items/new")
    return school.one("SELECT id FROM finance_fee_items WHERE name = :n", n=name)


def pay(person, school, student_id, amount, payer="A Parent", category="Tuition", method="Cash", reference=""):
    """Record a payment through the real form; returns its id."""
    session_id = school.one("SELECT id FROM academic_sessions WHERE is_current = 1")
    r = person.post(f"{FIN_URL}/payments/new", {
        "student_id": student_id, "session_id": session_id, "amount": amount, "category": category, "method": method,
        "reference": reference, "paid_at": "", "notes": "", "payer_name": payer}, page=f"{FIN_URL}/payments/new")
    found = re.search(r"/receipts/(\d+)", r.headers.get("Location", ""))
    return int(found.group(1)) if found else None


def allocate(person, school, payment_id, student_id, item_id, amount, term="First Term"):
    session_id = school.one("SELECT id FROM academic_sessions WHERE is_current = 1")
    a = school.one("SELECT id FROM finance_fee_assessments WHERE student_id = :s AND fee_item_id = :f AND session_id = :c",
                   s=student_id, f=item_id, c=session_id)
    person.post(f"{FIN_URL}/payments/{payment_id}/allocate", {"allocations": json.dumps({str(a): amount})},
                page=f"{FIN_URL}/payments/{payment_id}/allocate")


def assess(person, school, student_id, item_id, term="First Term"):
    session_id = school.one("SELECT id FROM academic_sessions WHERE is_current = 1")
    person.post(f"{FIN_URL}/assessments/new", {"student_id": student_id, "session_id": session_id, "fee_item_id": [item_id],
                                               "term": term, "due_date": "", "notes": ""}, page=f"{FIN_URL}/fee-items")


def receipt_no(school, payment_id):
    return school.one("SELECT receipt_no FROM finance_payments WHERE id = :p", p=payment_id)


def logs(school, payment_id, channel):
    return [tuple(r) for r in school.sql(
        "SELECT status, recipient, provider_reference, error_message, sent_by FROM finance_delivery_logs "
        "WHERE payment_id = :p AND channel = :c ORDER BY id", p=payment_id, c=channel)]


def send(person, path, payment_id, page=None):
    """Press "Send by email" / "Send by WhatsApp" on the receipt page. Returns what the person is told."""
    r = person.post(f"{FIN_URL}/receipts/{payment_id}/{path}", {}, page=page or f"{FIN_URL}/receipts/{payment_id}")
    return r, person.said()


def pdf_of(person, payment_id):
    r = person.get(f"{FIN_URL}/receipts/{payment_id}/pdf")
    return r, (pdf_read(r.data) if r.status_code == 200 else None)


def body_of(msg):
    """The plain-text body of a message, whether or not it has attachments."""
    return msg.get_body(preferencelist=("plain",)).get_content()


def clear_outbox():
    Outbox.mail.clear()
    Outbox.whatsapp.clear()
    Outbox.mode = None


# ================================================================ 0. the people and the payments
ADA = register_student(op_alpha, alpha, "Ada", "Obi", "obi.family@alpha.example", "08031234567")
NADIA = register_student(op_alpha, alpha, "Nadia", "Nocontact", "", "")
CHIKE = register_student(op_beta, beta, "Chike", "Betaman", "chike.family@beta.example", "08035550009")
GINA = register_student(op_gamma, gamma, "Gina", "Gammason", "gina.family@gamma.example", "08035550010")
check("a student was registered in each school", None not in (ADA, NADIA, CHIKE, GINA))
mum_obi = make_parent(op_alpha, alpha, "Mrs Ada Obi", "mrs.obi", "mrs.obi@alpha.example", [ADA])
mr_beta = make_parent(op_beta, beta, "Mr Betaman", "mr.betaman", "mr.betaman@beta.example", [CHIKE])
cashier, CASHIER_ID = add_admin(alpha, "cashier", "Finance Records Officer")
manager, MANAGER_ID = add_admin(alpha, "manager", "Finance Manager")
outsider, OUTSIDER_ID = add_admin(alpha, "librarian", "Librarian")

TUITION = new_fee(op_alpha, alpha, "Tuition (JSS 1)", "40000")
assess(op_alpha, alpha, ADA, TUITION)
PA = pay(op_alpha, alpha, ADA, "12345.67", payer="Mrs Ada Obi", category="Tuition", method="Bank Transfer", reference="TRF-778899")
allocate(op_alpha, alpha, PA, ADA, TUITION, 12345.67)
PCASH = pay(op_alpha, alpha, ADA, "5000", payer="Mr Ada Obi", category="Tuition")
PNADIA = pay(op_alpha, alpha, NADIA, "1000", payer="Nadia's Aunt", category="Uniform")
BETA_FEE = new_fee(op_beta, beta, "Beta Lab Fee", "90000", "Other", "Full Session")
assess(op_beta, beta, CHIKE, BETA_FEE, "Full Session")
PB = pay(op_beta, beta, CHIKE, "777.50", payer="Beta Payer", category="Other", method="Cash")
GAMMA_FEE = new_fee(op_gamma, gamma, "Gamma Robotics", "15000", "Other", "Full Session")
PG = pay(op_gamma, gamma, GINA, "2500", payer="Gamma Payer", category="Other")
check("each school recorded its own payments, numbered with its own prefix, from one",
      all(p is not None for p in (PA, PCASH, PNADIA, PB, PG))
      and receipt_no(alpha, PA) == f"ALPHA-{YEAR}-00001" and receipt_no(alpha, PNADIA) == f"ALPHA-{YEAR}-00003"
      and receipt_no(beta, PB) == f"BETA-{YEAR}-00001" and receipt_no(gamma, PG) == f"GAMMA-{YEAR}-00001",
      str([receipt_no(alpha, PA), receipt_no(beta, PB), receipt_no(gamma, PG)]))
NO_A, NO_B, NO_G = receipt_no(alpha, PA), receipt_no(beta, PB), receipt_no(gamma, PG)
# Recording a payment already sent the guardian the receipt, by itself (email with the PDF, and WhatsApp). Check that, then
# start the rest of the run from a clean slate, so the checks below see only what they send by hand.
check("recording a payment sent the guardian the receipt automatically: the PDF by email, and by WhatsApp",
      len([m for m in Outbox.mail if NO_A in str(m[1]["Subject"])]) >= 1 and len(Outbox.whatsapp) >= 1
      and alpha.count("finance_delivery_logs", "payment_id = :p AND status = 'sent'", p=PA) == 2)
for school in (alpha, beta, gamma):
    school.sql("DELETE FROM finance_delivery_logs")
clear_outbox()

# ================================================================ 1. the amount in words
words = {0.05: "zero naira and five kobo only", 1: "one naira only", 21: "twenty-one naira only",
         100: "one hundred naira only", 12345.67: "twelve thousand three hundred and forty-five naira and sixty-seven kobo only",
         1234567.5: "one million two hundred and thirty-four thousand five hundred and sixty-seven naira and fifty kobo only"}
check("the amount in words is right for small, round and large amounts",
      all(FIN._amount_in_words(value) == said for value, said in words.items()),
      str({v: FIN._amount_in_words(v) for v in words if FIN._amount_in_words(v) != words[v]}))

# ================================================================ 2. what the PDF says
r, pdf = pdf_of(op_alpha, PA)
check("the receipt PDF downloads, named after the receipt number",
      r.status_code == 200 and r.content_type == "application/pdf" and r.data.startswith(b"%PDF")
      and NO_A in r.headers.get("Content-Disposition", ""))
paid_at = alpha.one("SELECT paid_at FROM finance_payments WHERE id = :p", p=PA)
date_text = datetime.strptime(paid_at[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
text = pdf["text"]
check("it says OFFICIAL RECEIPT, with the receipt number and the date", "OFFICIAL RECEIPT" in text and NO_A in text and date_text in text)
check("…who paid, and the amount in words and in figures",
      "Mrs Ada Obi" in text and all(w in text for w in ("twelve thousand", "forty-five naira", "sixty-seven"))
      and "₦12,345.67" in text)
check("…what it was for, for which student and class", "Tuition" in text and "Ada Obi" in text and "JSS 1" in text, text[:0])
check("…how it was paid: the method and the bank reference", "Bank Transfer" in text and "TRF-778899" in text)
check("…the school's own name, address, phone and email",
      "For: ALPHA SCHOOL" in text and "12 Palm Avenue, Ikeja, Lagos" in text and "Tel: 0803 111 2222" in text
      and "info@alpha-school.example" in text)
check("…and the Authorised Signature line", "Authorised Signature" in text)
check("…and is not marked VOIDED", "VOIDED" not in text)
check("the school's own logo is drawn, and nobody else's",
      draws(pdf, RED) and not draws(pdf, BLUE) and not draws(pdf, OLD_SCHOOL))
check("nothing of the school this platform grew out of is printed (name, address, phone numbers)",
      not any(w in text for w in OLD_SCHOOL_WORDS), str([w for w in OLD_SCHOOL_WORDS if w in text]))
r, pdf_cash = pdf_of(op_alpha, PCASH)
check("a cash payment with no reference says Cash, in figures and words",
      pdf_cash["text"].count("Cash") >= 1 and "five thousand naira only" in pdf_cash["text"] and "₦5,000.00" in pdf_cash["text"])
check("…and the next receipt number is the next one", receipt_no(alpha, PCASH) == f"ALPHA-{YEAR}-00002")

r, pdf_b = pdf_of(op_beta, PB)
check("Beta's receipt has Beta's logo, name, address, phone, prefix and payer, and none of Alpha's",
      draws(pdf_b, BLUE) and not draws(pdf_b, RED) and not draws(pdf_b, OLD_SCHOOL)
      and all(s in pdf_b["text"] for s in ("For: BETA COLLEGE", "4 River Road, Abuja", "Tel: 0809 555 6666", NO_B, "Beta Payer",
                                           "Chike Betaman", "₦777.50", "seven hundred and seventy-seven naira and fifty kobo only"))
      and not any(s in pdf_b["text"] for s in ("ALPHA", "Alpha", "Palm Avenue", "0803 111 2222", NO_A) + OLD_SCHOOL_WORDS))
r, pdf_g = pdf_of(op_gamma, PG)
check("a school with no logo gets its own name where the logo would be, and never another school's logo",
      pdf_g["images"] == [] and pdf_g["text"].count("Gamma Academy") >= 1 and "For: GAMMA ACADEMY" in pdf_g["text"]
      and not draws(pdf_g, OLD_SCHOOL) and not draws(pdf_g, RED) and not draws(pdf_g, BLUE), str(len(pdf_g["images"])))
check("…and, with no address or phone of its own, prints none (nobody else's)",
      "Tel:" not in pdf_g["text"] and "Lagos" not in pdf_g["text"] and NO_G in pdf_g["text"]
      and not any(w in pdf_g["text"] for w in OLD_SCHOOL_WORDS), str([w for w in OLD_SCHOOL_WORDS if w in pdf_g["text"]]))

# the printed page and the page on screen say the same thing as the PDF
for label, path in (("on-screen page", f"{FIN_URL}/receipts/{PA}"), ("print page", f"{FIN_URL}/receipts/{PA}/print")):
    html = op_alpha.text(path)
    check(f"the {label} shows the school's name, address, phone, receipt number, payer, amount and purpose",
          all(s in html for s in ("12 Palm Avenue, Ikeja, Lagos", "Tel: 0803 111 2222", NO_A, "Mrs Ada Obi", "₦12,345",
                                  "twelve thousand three hundred and forty-five naira and sixty-seven kobo only",
                                  "Tuition", "Ada Obi", "JSS 1", "For: ALPHA SCHOOL")), label)
    check(f"…with the school's own logo, and nothing of the school this platform grew out of",
          "uploads/branding/" in html and "images/school_logo.png" not in html
          and not any(w in html for w in OLD_SCHOOL_WORDS), label)
for label, op, pid, no in (("Beta", op_beta, PB, NO_B), ("Gamma", op_gamma, PG, NO_G)):
    html = op.text(f"{FIN_URL}/receipts/{pid}/print")
    check(f"{label}'s print page carries {label}'s own identity and none of the old school's",
          no in html and not any(w in html for w in OLD_SCHOOL_WORDS) and "images/school_logo.png" not in html
          and "Palm Avenue" not in html, label)
check("a school with no logo prints its name in the logo's place on the page too",
      'class="rc-logo">GA<' in op_gamma.text(f"{FIN_URL}/receipts/{PG}/print")
      and "uploads/branding" not in op_gamma.text(f"{FIN_URL}/receipts/{PG}/print"))
check("a receipt that does not exist is a 404, page or PDF", op_alpha.get(f"{FIN_URL}/receipts/999999/pdf").status_code == 404
      and op_alpha.get(f"{FIN_URL}/receipts/999999").status_code == 404)

# the parent's copy is the same receipt
r = mum_obi.get(f"/parent/children/{ADA}/receipts/{PA}/pdf")
check("a parent downloading their child's receipt gets the same PDF the school prints",
      r.status_code == 200 and pdf_read(r.data)["text"] == pdf["text"] and draws(pdf_read(r.data), RED))
check("…and neither can another family, at this school or at another (Beta's parent asks for a child and a payment that exist only at Alpha)",
      mum_obi.get(f"/parent/children/{ADA}/receipts/{PNADIA}/pdf").status_code == 404
      and mum_obi.get(f"/parent/children/{NADIA}/receipts/{PNADIA}/pdf").status_code == 404
      and mr_beta.get(f"/parent/children/{NADIA}/receipts/{PNADIA}/pdf").status_code == 404)
check("staff of one school cannot fetch another's receipt, page or PDF (the id does not exist there)",
      PCASH > 0 and PNADIA > PB and op_beta.get(f"{FIN_URL}/receipts/{PNADIA}").status_code == 404
      and op_beta.get(f"{FIN_URL}/receipts/{PNADIA}/pdf").status_code == 404
      and op_beta.get(f"{FIN_URL}/receipts/{PNADIA}/print").status_code == 404)

# ================================================================ 3. the authorised signature
SETTINGS = f"{FIN_URL}/receipt-settings"
KEY = "receipt_authorised_signature"


def signature_setting(school):
    return school.one("SELECT setting_value FROM school_settings WHERE setting_key = :k", k=KEY)


def signature_files(school):
    folder = school.run(lambda: os.path.join(uploads_dir(), "signatures"))
    return sorted(os.listdir(folder)) if os.path.isdir(folder) else []


page = op_alpha.text(SETTINGS)
check("the receipt settings page opens, and says no signature is configured yet",
      "Receipt Settings" in page and "No signature configured yet" in page)
check("…and the receipt says so too: a blank Authorised Signature line, no picture",
      not draws(pdf, GREEN) and not draws(pdf, PURPLE) and "<img src=\"/static/uploads/signatures" not in op_alpha.text(f"{FIN_URL}/receipts/{PA}"))

drawn = "data:image/png;base64," + base64.b64encode(GREEN_SIGN).decode()
r = op_alpha.post(SETTINGS, {"action": "draw", "signature_data_url": drawn}, page=SETTINGS)
check("a signature drawn on the pad (sent as a data: URL) is saved", r.status_code == 302 and op_alpha.said() == "signature saved."
      and (signature_setting(alpha) or "").startswith("uploads/signatures/"))
first_files = signature_files(alpha)
check("…as exactly one file in the school's own folder", len(first_files) == 1 and signature_setting(alpha).endswith(first_files[0]))
page = op_alpha.text(SETTINGS)
check("…and the settings page shows it, with a way to remove it",
      f"/static/uploads/signatures/{first_files[0]}" in page and "Remove signature" in page and "No signature configured" not in page)
r, pdf_signed = pdf_of(op_alpha, PA)
check("the PDF now carries the drawn signature above the line", draws(pdf_signed, GREEN) and "Authorised Signature" in pdf_signed["text"])
check("…and the receipt page and the print page show it",
      f"/static/uploads/signatures/{first_files[0]}" in op_alpha.text(f"{FIN_URL}/receipts/{PA}")
      and f"/static/uploads/signatures/{first_files[0]}" in op_alpha.text(f"{FIN_URL}/receipts/{PA}/print"))
check("…and every receipt of the school, old ones included, not just the next",
      draws(pdf_of(op_alpha, PCASH)[1], GREEN) and draws(pdf_of(op_alpha, PNADIA)[1], GREEN))
check("…but no other school's receipt", not draws(pdf_of(op_beta, PB)[1], GREEN) and not draws(pdf_of(op_gamma, PG)[1], GREEN)
      and signature_setting(beta) is None and signature_files(beta) == [])
check("the signature file is served to the school's administrators and to nobody signed out",
      op_alpha.get(f"/static/uploads/signatures/{first_files[0]}").status_code == 200
      and Person(ALPHA).get(f"/static/uploads/signatures/{first_files[0]}").status_code == 404)

r = op_alpha.post(SETTINGS, {"action": "upload"}, page=SETTINGS, files={"signature_file": ("signature.png", PURPLE_SIGN)})
second_files = signature_files(alpha)
check("uploading an image saves it", r.status_code == 302 and op_alpha.said() == "signature saved.")
check("…replacing the drawn one rather than stacking beside it: one file, a different one, the old one deleted",
      len(second_files) == 1 and second_files != first_files and signature_setting(alpha).endswith(second_files[0]))
r, pdf_signed = pdf_of(op_alpha, PA)
check("the PDF shows the uploaded signature and no longer the drawn one", draws(pdf_signed, PURPLE) and not draws(pdf_signed, GREEN))

# what is refused changes nothing
kept = (signature_setting(alpha), signature_files(alpha))
refused = {
    "a file that is not an image type": ("upload", {"signature_file": ("notes.txt", b"hello")}, "png, jpg"),
    "a text file with an image name": ("upload", {"signature_file": ("sig.png", b"this is not a picture at all")}, "valid image"),
    "no file at all": ("upload", None, "choose an image"),
    "a drawing that is not a PNG": ("draw", "data:image/png;base64," + base64.b64encode(b"not a png").decode(), "could not be read"),
    "a drawing that is not an image URL": ("draw", "javascript:alert(1)", "could not be read"),
    "an empty drawing": ("draw", "", "could not be read"),
    "an action nobody offers": ("shred", None, "unrecognised action"),
}
for label, (action, payload, message) in refused.items():
    if action == "upload":
        r = op_alpha.post(SETTINGS, {"action": "upload"}, page=SETTINGS, files=payload)
    else:
        r = op_alpha.post(SETTINGS, {"action": action, "signature_data_url": payload or ""}, page=SETTINGS)
    check(f"{label} is refused, with a message, and changes nothing",
          message in r.get_data(as_text=True).lower() and (signature_setting(alpha), signature_files(alpha)) == kept,
          f"HTTP {r.status_code}")

# only a finance manager may change it
before = (signature_setting(alpha), signature_files(alpha))
denied = [cashier.get(SETTINGS).status_code, outsider.get(SETTINGS).status_code]
posted = [who.post(SETTINGS, {"action": "remove"}).status_code for who in (cashier, outsider)]
check("a cashier and an administrator with no finance role cannot open or change the signature",
      denied == [403, 403] and posted == [403, 403] and (signature_setting(alpha), signature_files(alpha)) == before, str((denied, posted)))
check("a POST without a form token is refused and changes nothing",
      op_alpha.client.post(SETTINGS, data={"action": "remove"}, base_url=ALPHA).status_code == 403
      and signature_setting(alpha) == before[0])
check("a finance manager can change it", manager.post(SETTINGS, {"action": "draw", "signature_data_url": drawn}, page=SETTINGS).status_code == 302
      and len(signature_files(alpha)) == 1 and signature_setting(alpha) != before[0])

# Beta has its own, and neither shows on the other's receipts
op_beta.post(SETTINGS, {"action": "upload"}, page=SETTINGS, files={"signature_file": ("sig.png", PURPLE_SIGN)})
check("Beta keeps its own signature, in its own folder, and Alpha's receipts do not change",
      len(signature_files(beta)) == 1 and draws(pdf_of(op_beta, PB)[1], PURPLE) and not draws(pdf_of(op_beta, PB)[1], GREEN)
      and draws(pdf_of(op_alpha, PA)[1], GREEN) and not draws(pdf_of(op_alpha, PA)[1], PURPLE))
op_beta.post(SETTINGS, {"action": "remove"}, page=SETTINGS)
check("removing Beta's leaves Alpha's alone", signature_files(beta) == [] and len(signature_files(alpha)) == 1
      and draws(pdf_of(op_alpha, PA)[1], GREEN))

current_file = signature_files(alpha)[0]
r = op_alpha.post(SETTINGS, {"action": "remove"}, page=SETTINGS)
check("removing the signature clears the setting and deletes the file",
      r.status_code == 302 and op_alpha.said() == "authorised signature removed." and signature_setting(alpha) == ""
      and signature_files(alpha) == [] and not os.path.exists(os.path.join(
          alpha.run(lambda: uploads_dir()), "signatures", current_file)))
check("…the settings page says none is configured again, and the PDF has a blank line and still builds",
      "No signature configured yet" in op_alpha.text(SETTINGS) and not draws(pdf_of(op_alpha, PA)[1], GREEN)
      and "Authorised Signature" in pdf_of(op_alpha, PA)[1]["text"])
r = op_alpha.post(SETTINGS, {"action": "remove"}, page=SETTINGS)
check("removing when there is none is harmless", r.status_code == 302 and signature_files(alpha) == [])
check("every change to the signature was recorded in the school's audit log",
      alpha.count("audit_logs", "action = 'finance_receipt_signature_updated'") >= 5)

# ================================================================ 4. emailing a receipt
clear_outbox()
r, said = send(op_alpha, "email", PA)
check("the Send by email button on the receipt page sends, and says so",
      r.status_code == 302 and said == "receipt emailed successfully.", said)
sent = Outbox.mail
check("exactly one email left, to the guardian on the student's record",
      len(sent) == 1 and sent[0][1]["To"] == "obi.family@alpha.example")
settings, msg = sent[0] if sent else (None, None)
check("…through the school's own mail account, from the school's own address, not the platform's",
      settings.source == "school" and settings.host == "smtp.alpha.test" and msg["From"] == "office@alpha.example")
body = body_of(msg)
check("…with the school's name and the receipt number in the subject",
      msg["Subject"] == f"Alpha School Payment Receipt {NO_A}", msg["Subject"])
check("…and a body that says who, how much and what for, and is signed by the school",
      all(s in body for s in ("Dear Parent/Guardian", NO_A, "Ada Obi", "Amount paid: ₦12,345.67", "Paid for: Tuition", "Alpha School"))
      and "Beta" not in body and "Gamma" not in body, body)
attachments = list(msg.iter_attachments())
check("…with the receipt attached as a PDF named after the receipt number",
      len(attachments) == 1 and attachments[0].get_content_type() == "application/pdf"
      and attachments[0].get_filename() == f"{NO_A}.pdf")
attached = pdf_read(attachments[0].get_content())
current_pdf = pdf_of(op_alpha, PA)[1]
check("…which is the very same receipt the school prints (same words, same logo, same signature line)",
      attached["text"] == current_pdf["text"] and draws(attached, RED) and NO_A in attached["text"] and "Mrs Ada Obi" in attached["text"])
check("the attempt is logged as sent, to that address, by the signed-in person",
      len(logs(alpha, PA, "email")) == 1 and logs(alpha, PA, "email")[0][:3] == ("sent", "obi.family@alpha.example", "obi.family@alpha.example")
      and logs(alpha, PA, "email")[0][4] is not None, str(logs(alpha, PA, "email")))
check("…and in the audit log", alpha.count("audit_logs", "action = 'finance_receipt_email'") == 1)

# a signature drawn now is on the emailed PDF
op_alpha.post(SETTINGS, {"action": "draw", "signature_data_url": drawn}, page=SETTINGS)
clear_outbox()
send(op_alpha, "email", PCASH)
check("a receipt emailed after a signature was set carries it",
      len(Outbox.mail) == 1 and draws(pdf_read(list(Outbox.mail[0][1].iter_attachments())[0].get_content()), GREEN))
op_alpha.post(SETTINGS, {"action": "remove"}, page=SETTINGS)

# what goes wrong
clear_outbox()
r, said = send(op_alpha, "email", PNADIA)
check("a student with no guardian email address: refused with a clear message, nothing sent",
      "no parent/guardian email address" in said and not Outbox.mail, said)
check("…and the failed attempt is logged, with the reason",
      len(logs(alpha, PNADIA, "email")) == 1 and logs(alpha, PNADIA, "email")[0][0] == "failed"
      and "no parent/guardian email" in logs(alpha, PNADIA, "email")[0][3].lower())
Outbox.mode = "outage"
r, said = send(op_alpha, "email", PCASH)
check("a mail server that is down: the failure is reported, not hidden, and the receipt is unharmed",
      "email delivery failed" in said and r.status_code == 302 and not Outbox.mail
      and logs(alpha, PCASH, "email")[-1][0] == "failed" and "outage" in logs(alpha, PCASH, "email")[-1][3])
Outbox.mode = None
r, said = send(op_gamma, "email", PG)
check("a school with no mail account of its own and no platform account says email is not set up, and sends nothing",
      "not set up for this school" in said and not Outbox.mail and logs(gamma, PG, "email")[0][0] == "failed")
os.environ.update({"BRIGHTSTARS_SMTP_HOST": "mail.platform.test", "BRIGHTSTARS_SMTP_PORT": "587", "BRIGHTSTARS_SMTP_USER": "platform-user",
                   "BRIGHTSTARS_SMTP_PASSWORD": "platform-pw", "BRIGHTSTARS_SMTP_FROM": "noreply@platform.example"})
try:
    clear_outbox()
    r, said = send(op_gamma, "email", PG)
    check("…but with the platform's shared account it falls back to that, and the mail still names the school",
          said == "receipt emailed successfully." and len(Outbox.mail) == 1 and Outbox.mail[0][0].source == "platform"
          and Outbox.mail[0][1]["From"] == "noreply@platform.example"
          and Outbox.mail[0][1]["Subject"] == f"Gamma Academy Payment Receipt {NO_G}"
          and "Gamma Academy" in body_of(Outbox.mail[0][1]) and "Alpha" not in body_of(Outbox.mail[0][1]))
    clear_outbox()
    send(op_alpha, "email", PCASH)
    check("…while a school with its own account keeps using it",
          len(Outbox.mail) == 1 and Outbox.mail[0][0].source == "school" and Outbox.mail[0][1]["From"] == "office@alpha.example")
finally:
    for name in PLATFORM_ENV:
        os.environ.pop(name, None)
r, said = send(op_gamma, "email", PG)
check("…and once the platform account goes, so does the ability to send", "not set up" in said)

# who may send
clear_outbox()
PC = pay(cashier, alpha, ADA, "3000", payer="Mrs Ada Obi", category="Tuition")
clear_outbox()  # recording the payment told the guardian too
r, said = send(cashier, "email", PC, page=f"{FIN_URL}/receipts/{PC}")
check("a cashier can email the receipt of a payment they recorded", said == "receipt emailed successfully." and len(Outbox.mail) == 1, said)
clear_outbox()
r, said = send(cashier, "email", PA, page="/admin/password")
check("…but not someone else's: refused, nothing sent, nothing logged",
      r.status_code == 403 and not Outbox.mail and len(logs(alpha, PA, "email")) == 1)
r, said = send(cashier, "whatsapp", PA, page="/admin/password")
check("…nor send it by WhatsApp", r.status_code == 403 and not Outbox.whatsapp and logs(alpha, PA, "whatsapp") == [])
r, said = send(outsider, "email", PC, page="/admin/password")
check("an administrator with no finance permission cannot send any receipt",
      r.status_code == 403 and not Outbox.mail and len(logs(alpha, PC, "email")) == 2)  # the automatic one, and the earlier manual one
r, said = send(manager, "email", PA, page="/admin/password")
check("a finance manager can send anyone's", "receipt emailed successfully." in said and len(Outbox.mail) == 1, said)
clear_outbox()
r = op_alpha.client.post(f"{FIN_URL}/receipts/{PA}/email", data={}, base_url=ALPHA)
check("a request without a form token is refused, and sends nothing", r.status_code == 403 and not Outbox.mail)
check("sending is a POST: opening the address is not allowed",
      op_alpha.get(f"{FIN_URL}/receipts/{PA}/email").status_code == 405 and op_alpha.get(f"{FIN_URL}/receipts/{PA}/whatsapp").status_code == 405)
r, said = send(op_alpha, "email", 999999, page=f"{FIN_URL}/receipts/{PA}")
check("a receipt that does not exist is a 404, not an email", r.status_code == 404 and not Outbox.mail)

# Beta's mail is Beta's
clear_outbox()
r, said = send(op_beta, "email", PB)
settings, msg = Outbox.mail[0] if Outbox.mail else (None, None)
beta_pdf = pdf_read(list(msg.iter_attachments())[0].get_content()) if msg else {"text": "", "images": []}
check("Beta's receipt email goes out through Beta's own account to Beta's own guardian, in Beta's name",
      msg is not None and settings.host == "smtp.beta.test" and msg["From"] == "office@beta.example"
      and msg["To"] == "chike.family@beta.example" and msg["Subject"] == f"Beta College Payment Receipt {NO_B}")
check("…with nothing of Alpha's in the body or the attached PDF, and Beta's logo in it",
      msg is not None and "Alpha" not in body_of(msg) and "Alpha" not in beta_pdf["text"] and NO_A not in beta_pdf["text"]
      and draws(beta_pdf, BLUE) and not draws(beta_pdf, RED) and NO_B in beta_pdf["text"])
r, said = send(op_beta, "email", PNADIA, page=f"{FIN_URL}/receipts/{PB}")
check("Beta cannot email Alpha's receipt by its number (there is no such receipt at Beta)", r.status_code == 404)

# ================================================================ 5. sending a receipt by WhatsApp
clear_outbox()
r, said = send(op_alpha, "whatsapp", PA)
media = [c for c in Outbox.whatsapp if c["kind"] == "media"]
messages = [c for c in Outbox.whatsapp if c["kind"] == "message"]
check("the Send by WhatsApp button uploads the receipt and then sends it, and says so",
      r.status_code == 302 and said == "receipt sent through whatsapp business successfully." and len(media) == 1 and len(messages) == 1, said)
check("…using the school's own WhatsApp account and token, not another's",
      media and messages and all(c["token"] == "Bearer token-alpha" and "/v23.0/1045123456781/" in c["url"] for c in (media[0], messages[0])))
boundary = re.search(r"boundary=(\S+)", media[0]["content_type"]).group(1) if media else ""
part = media[0]["body"].split(b"Content-Type: application/pdf\r\n\r\n", 1)[1].rsplit(f"\r\n--{boundary}--".encode(), 1)[0] if media else b""
uploaded = pdf_read(part)
check("the file uploaded is the receipt PDF, named after the receipt number, and the very one the school prints",
      f'filename="{NO_A}.pdf"'.encode() in media[0]["body"] and part.startswith(b"%PDF")
      and uploaded["text"] == pdf_of(op_alpha, PA)[1]["text"] and draws(uploaded, RED))
sent_json = messages[0]["json"] if messages else {}
check("the message goes to the guardian's number in international form, as a document, with a caption naming the receipt and student",
      sent_json.get("to") == "+2348031234567" and sent_json.get("type") == "document"
      and sent_json["document"]["id"] == "media-1" and sent_json["document"]["filename"] == f"{NO_A}.pdf"
      and sent_json["document"]["caption"] == f"Official payment receipt {NO_A} — Ada Obi", str(sent_json))
check("the attempt is logged as sent, with WhatsApp's own message id",
      len(logs(alpha, PA, "whatsapp")) == 1 and logs(alpha, PA, "whatsapp")[0][:3] == ("sent", "08031234567", "wamid.1"))

clear_outbox()
r, said = send(op_alpha, "whatsapp", PNADIA)
check("a student with no valid WhatsApp number: refused with a clear message, nothing sent, logged",
      "no valid parent/guardian whatsapp number" in said and not Outbox.whatsapp
      and logs(alpha, PNADIA, "whatsapp")[0][0] == "failed")
r, said = send(op_gamma, "whatsapp", PG)
check("a school with no WhatsApp account says it is not set up, and sends nothing",
      "whatsapp is not set up" in said and not Outbox.whatsapp and logs(gamma, PG, "whatsapp")[0][0] == "failed")
Outbox.mode = "http401"
r, said = send(op_alpha, "whatsapp", PCASH)
check("a token WhatsApp rejects: the error is reported and logged, and the token is not echoed",
      "whatsapp api error 401" in said and "token-alpha" not in said
      and logs(alpha, PCASH, "whatsapp")[-1][0] == "failed" and "token-alpha" not in (logs(alpha, PCASH, "whatsapp")[-1][3] or ""))
Outbox.mode = "outage"
r, said = send(op_alpha, "whatsapp", PCASH)
check("WhatsApp unreachable: reported, logged, nothing sent",
      "whatsapp delivery failed" in said and not Outbox.whatsapp and len(logs(alpha, PCASH, "whatsapp")) == 2)
Outbox.mode = "nomedia"
r, said = send(op_alpha, "whatsapp", PCASH)
check("an upload WhatsApp does not give a media id for is not treated as sent",
      "no media id" in said and not [c for c in Outbox.whatsapp if c["kind"] == "message"]
      and logs(alpha, PCASH, "whatsapp")[-1][0] == "failed")
Outbox.mode = None
clear_outbox()
r, said = send(op_beta, "whatsapp", PB)
media, messages = ([c for c in Outbox.whatsapp if c["kind"] == k] for k in ("media", "message"))
beta_upload = pdf_read(media[0]["body"].split(b"Content-Type: application/pdf\r\n\r\n", 1)[1]) if media else {"text": "", "images": []}
check("Beta's WhatsApp receipt uses Beta's token and number, carries Beta's PDF, and names nobody at Alpha",
      len(media) == 1 and len(messages) == 1 and media[0]["token"] == "Bearer token-beta" and "/1045123456782/" in media[0]["url"]
      and messages[0]["json"]["to"] == "+2348035550009" and NO_B in messages[0]["json"]["document"]["caption"]
      and "Alpha" not in messages[0]["json"]["document"]["caption"]
      and NO_B in beta_upload["text"] and draws(beta_upload, BLUE) and not draws(beta_upload, RED) and "Alpha" not in beta_upload["text"])
os.environ.update({"BRIGHTSTARS_WHATSAPP_TOKEN": "platform-token", "BRIGHTSTARS_WHATSAPP_PHONE_NUMBER_ID": "111222333"})
try:
    clear_outbox()
    r, said = send(op_gamma, "whatsapp", PG)
    check("with the platform's shared account a school with none of its own can send, through the platform's token",
          said == "receipt sent through whatsapp business successfully." and Outbox.whatsapp
          and all(c["token"] == "Bearer platform-token" and "/111222333/" in c["url"] for c in Outbox.whatsapp))
finally:
    for name in PLATFORM_ENV:
        os.environ.pop(name, None)

# ================================================================ 6. a voided payment's receipt
PV = pay(op_alpha, alpha, ADA, "9999", payer="Wrong Payer", category="Tuition")
NO_V = receipt_no(alpha, PV)
op_alpha.post(f"{FIN_URL}/payments/{PV}/void", {"reason": "Entered against the wrong child"}, page=f"{FIN_URL}/receipts/{PV}")
r, pdf_v = pdf_of(op_alpha, PV)
check("a voided payment's PDF says VOIDED, and is otherwise the same receipt", "VOIDED" in pdf_v["text"] and NO_V in pdf_v["text"])
check("…and so does its page and its print page", "VOIDED" in op_alpha.text(f"{FIN_URL}/receipts/{PV}")
      and "VOIDED" in op_alpha.text(f"{FIN_URL}/receipts/{PV}/print"))
check("…and the parent's copy", "VOIDED" in pdf_read(mum_obi.get(f"/parent/children/{ADA}/receipts/{PV}/pdf").data)["text"])
clear_outbox()
r, said = send(op_alpha, "email", PV)
check("a voided receipt is not emailed: a clear message, nothing sent, the attempt logged as failed",
      "voided" in said and not Outbox.mail and logs(alpha, PV, "email")[-1][0] == "failed")
r, said = send(op_alpha, "whatsapp", PV)
check("…nor sent by WhatsApp", "voided" in said and not Outbox.whatsapp and logs(alpha, PV, "whatsapp")[-1][0] == "failed")
check("a receipt that is not voided never says VOIDED", "VOIDED" not in pdf_of(op_alpha, PA)[1]["text"])

# ================================================================ 7. the school's own receipt prefix
alpha.sql("INSERT INTO school_public_settings (setting_key, setting_value, updated_at) VALUES ('receipt_prefix', 'ALP', '2026-01-01')")
PN = pay(op_alpha, alpha, ADA, "1500", payer="Mrs Ada Obi", category="Tuition")
check("a school that sets its own receipt prefix numbers new receipts with it, from one, and old ones keep theirs",
      receipt_no(alpha, PN) == f"ALP-{YEAR}-00001" and receipt_no(alpha, PA) == NO_A)
clear_outbox()
send(op_alpha, "email", PN)
check("…and the email and PDF carry the new number",
      len(Outbox.mail) == 1 and Outbox.mail[0][1]["Subject"] == f"Alpha School Payment Receipt ALP-{YEAR}-00001"
      and f"ALP-{YEAR}-00001" in pdf_read(list(Outbox.mail[0][1].iter_attachments())[0].get_content())["text"])
check("…while another school's numbering is untouched",
      pay(op_beta, beta, CHIKE, "100", payer="Beta Payer", category="Other") is not None
      and beta.one("SELECT receipt_no FROM finance_payments ORDER BY id DESC LIMIT 1") == f"BETA-{YEAR}-00002")

# ================================================================ summary
print()
failed = [x for x in results if not x[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
