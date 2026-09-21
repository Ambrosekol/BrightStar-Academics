"""Fee items, what a student owes, and what a parent sees, end to end on PostgreSQL.

Two throw-away schools ("alpha" and "beta"), each with its own administrators, students and parents.
Everything is created through the real forms and routes, with CSRF tokens read from the real pages,
and the checks read the database rows and the rendered pages. In plain words, it proves:

The fee catalogue
* a fee item's category is a fixed list and its billing rule is one of five, both checked on the
  server (a scripted POST cannot slip in a made-up value), and a row from before that rule existed is
  shown as "(legacy)" and is never rewritten by a refused save;
* an amount must be a real, finite number of naira, kept to the kobo, and a fee can only be charged to
  a class it is mapped to, only while it is active, only in a term that exists;
* changing a fee's amount never rewrites what students were already charged.

Charging students, paying, and paid / part paid / unpaid
* one live charge per fee, per student, per term: a repeat is refused, and a batch with one repeat writes nothing;
* a payment gets a receipt number carrying the school's own prefix, counted on its own in each school;
* "paid" always means money allocated to that specific fee, never a raw sum of payments: a payment that
  has not been allocated is reported separately and never hides an unrelated debt;
* an allocation can never exceed the fee's balance or the payment's balance, never touch another
  student's fee, never come from a voided payment, and a fee already paid in full cannot be picked or
  allocated again (the screen hides it and a hand-made POST is refused, writing nothing);
* voiding a payment gives the fee its balance back, and the fee can be paid again;
* kobo amounts add up exactly (three instalments of a fee are not "part paid" by a hair);
* Paid / Part Paid / Unpaid is shown the same on the Fee Structure page, the student's account, the
  fee-picker's JSON and the parent's page, and matches the database.

What a parent sees
* the dashboard and the fee page show the lifetime balance across every session, so a debt left in an
  older session is never hidden, with the older session called out; an unallocated payment is shown
  on the side and never netted off;
* assessing a fee or recording a payment tells the guardian by email and WhatsApp (through the school's
  own accounts, caught at the last step) and in the app; a failure of either channel never stops the
  charge or the payment being saved; a notification is "New" once and "Old" afterwards;
* a parent can download their own child's receipt and nobody else's, and never another family's account.

Who may do what, and school against school
* only people with the right finance permission reach the pages; a cashier sees only the payments they
  recorded; a person with no finance permission is refused;
* one school never sees another's fee items, students, payers, receipts, receipt prefix or parents.

Run:  python tests/verification/write_paths_finance.py
"""
import atexit
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # a failure may quote text from a page

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_finance_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('finance')
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

import app as A  # noqa: E402
import blueprints.finance.helpers as FIN  # noqa: E402
from control_plane import provisioning as pv  # noqa: E402
from control_plane.context import tenant_context  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import engine_for  # noqa: E402
from core import delivery as _delivery  # noqa: E402
from core.security import ADMIN_ENDPOINT_PERMISSIONS  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

# app.py has just read the developer's .env, which may hold real mail and WhatsApp credentials. A test
# must never be able to reach them: start from a platform with no shared account at all.
for name in ("BRIGHTSTARS_SMTP_HOST", "BRIGHTSTARS_SMTP_USER", "BRIGHTSTARS_SMTP_FROM", "BRIGHTSTARS_SMTP_PASSWORD",
             "BRIGHTSTARS_WHATSAPP_TOKEN", "BRIGHTSTARS_WHATSAPP_PHONE_NUMBER_ID", "BRIGHTSTARS_DELIVERY_KEY",
             "BRIGHTSTARS_SMTP_PORT", "BRIGHTSTARS_SMTP_SSL", "BRIGHTSTARS_SMTP_STARTTLS",
             "BRIGHTSTARS_WHATSAPP_GRAPH_VERSION"):
    os.environ.pop(name, None)

results = []
PL = "http://platform.test"
ALPHA, BETA = "http://alpha.portal.test", "http://beta.portal.test"
YEAR = datetime.now().year


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)


# ------------------------------------------------------------------ the outside world, stood in for
class Outbox:
    """Everything the application tried to send, caught at the very last step."""
    mail = []        # (the account it went through, the message)
    whatsapp = []    # (the token it was sent with, the JSON that was sent)
    outage = False   # when set, both channels fail, as if the providers were down


class FakeSMTP:
    def __init__(self, settings):
        self.settings = settings

    def send_message(self, msg):
        if Outbox.outage:
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
    if Outbox.outage:
        raise OSError("simulated WhatsApp outage")
    url = request.full_url
    if url.endswith("/media"):
        return FakeResponse({"id": "media-1"})
    Outbox.whatsapp.append((request.headers.get("Authorization"), json.loads(request.data.decode())))
    return FakeResponse({"messages": [{"id": "wamid.1"}]})


_delivery._open_smtp = lambda settings: FakeSMTP(settings)
_delivery.resolve_public = lambda host: ["93.184.216.34"]
urllib.request.urlopen = fake_urlopen


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
        self.addr = f"10.30.{Person.count[0]}.1"  # sign-in limits are per address

    def get(self, path, **kw):
        return self.client.get(path, base_url=self.base, environ_base={"REMOTE_ADDR": self.addr}, **kw)

    def text(self, path):
        return self.get(path).get_data(as_text=True)

    def post(self, path, data=None, page=None, files=None):
        """Submit a form the way a browser does: the token comes from a real page that carries a form."""
        data = dict(data or {})
        data["_csrf_token"] = csrf_from(self.text(page or self.form_page))
        for key, given in (files or {}).items():
            import io
            data[key] = (io.BytesIO(given[1]), given[0])
        return self.client.post(path, data=data, base_url=self.base, environ_base={"REMOTE_ADDR": self.addr},
                                content_type="multipart/form-data" if files else None)

    def flashes(self):
        with self.client.session_transaction(base_url=self.base) as sess:
            found = list(sess.pop("_flashes", []))
        return found

    def said(self, response=None):
        """The messages left for the next page, as one lowercase string."""
        return " | ".join(text for _, text in self.flashes()).lower()


def csrf(c, path, base):
    body = c.get(path, base_url=base).get_data(as_text=True)
    return csrf_from(body)


def info_for(slug):
    with platform_session() as s:
        return to_info(get_tenant(s, slug))


def in_school(info, fn):
    with A.app.app_context(), tenant_context(info):
        return fn()


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
        return in_school(self.info, fn)


pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
console = A.app.test_client()
console.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                      "_csrf_token": csrf(console, "/platform/login", PL)}, base_url=PL)
for code, name in (("alpha", "Alpha School"), ("beta", "Beta College")):
    console.post("/platform/schools/new", data={
        "_csrf_token": csrf(console, "/platform/schools/new", PL), "name": name, "code": code,
        "school_brand_primary": "#0d2b52", "school_brand_accent": "#1674b9"}, base_url=PL,
        content_type="multipart/form-data")
alpha, beta = School("alpha", ALPHA), School("beta", BETA)


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


op_alpha, op_beta = operator_in(alpha), operator_in(beta)

# Each school sets up its own mail and WhatsApp account, so every alert can be traced to the right sender.
for school, person in ((alpha, op_alpha), (beta, op_beta)):
    person.post("/admin/school/delivery/email/save", {
        "smtp_host": f"smtp.{school.code}.test", "smtp_port": "587", "smtp_security": "starttls",
        "smtp_user": f"mailer-{school.code}", "smtp_password": "mail-pass-123",
        "smtp_from": f"office@{school.code}.example"}, page="/admin/school/delivery")
    person.post("/admin/school/delivery/whatsapp/save", {
        "whatsapp_phone_number_id": "1045123456789" + ("1" if school is alpha else "2"), "whatsapp_graph_version": "v23.0",
        "whatsapp_token": f"token-{school.code}"}, page="/admin/school/delivery")
check("each school set up its own email and WhatsApp account",
      alpha.count("school_delivery_settings", "setting_key = 'smtp_host'") == 1
      and beta.count("school_delivery_settings", "setting_key = 'whatsapp_token'") == 1)

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
    temp = re.search(r"<code>(.+?)</code>", r.get_data(as_text=True), re.S)
    check(f"{name}'s account was created and its one-time password shown", temp is not None)
    parent = Person(school.base, "/parent/password")
    parent.post("/login", {"username": username, "password": temp.group(1)}, page="/login")
    parent.post("/parent/password", {"current_password": temp.group(1), "new_password": "a-parent-password-1",
                                     "confirm_password": "a-parent-password-1"}, page="/parent/password")
    return parent


JSS1 = alpha.one("SELECT id FROM school_classes WHERE name = 'JSS 1'")
JSS2 = alpha.one("SELECT id FROM school_classes WHERE name = 'JSS 2'")
CURRENT = alpha.one("SELECT id FROM academic_sessions WHERE is_current = 1")
CURRENT_NAME = alpha.one("SELECT name FROM academic_sessions WHERE id = :i", i=CURRENT)
FIN_URL = "/admin/finance"
ITEMS = f"{FIN_URL}/fee-items"


def money(value):
    """The way a page writes an amount: 25,000.00 (no symbol)."""
    return f"{value:,.2f}"


def assess(person, student_id, session_id, item_ids, term="First Term", **extra):
    return person.post(f"{FIN_URL}/assessments/new", {
        "student_id": student_id, "session_id": session_id, "fee_item_id": item_ids, "term": term,
        "due_date": "", "notes": "", **extra}, page=ITEMS)


def pay(person, student_id, session_id, amount, payer="A Parent", category="School Fees", method="Cash",
        reference="", **extra):
    """Record a payment through the real form. Returns (response, the new payment's id or None)."""
    r = person.post(f"{FIN_URL}/payments/new", {
        "student_id": student_id, "session_id": session_id, "amount": amount, "category": category,
        "method": method, "reference": reference, "paid_at": "", "notes": "", "payer_name": payer, **extra},
        page=f"{FIN_URL}/payments/new")
    found = re.search(r"/receipts/(\d+)", r.headers.get("Location", ""))
    return r, int(found.group(1)) if found else None


def allocate(person, payment_id, amounts, raw=None):
    """Apply a payment to fees: ``amounts`` is {assessment id: naira}, or ``raw`` a hand-made body."""
    r = person.post(f"{FIN_URL}/payments/{payment_id}/allocate",
                    {"allocations": raw if raw is not None else json.dumps({str(k): v for k, v in amounts.items()})},
                    page=f"{FIN_URL}/payments/{payment_id}/allocate")
    return r, person.said()


def assessment_id(school, student_id, item_id, term="First Term", session_id=None):
    return school.one("SELECT id FROM finance_fee_assessments WHERE student_id = :s AND fee_item_id = :f "
                      "AND term = :t AND session_id = :c AND active = 1",
                      s=student_id, f=item_id, t=term, c=session_id or CURRENT)


def account(school, student_id, session_id=None):
    """The itemised account the application itself computes: {category: row}."""
    rows = school.run(lambda: FIN._finance_student_outstanding(student_id, session_id or CURRENT))
    return {r["category"]: r for r in rows}


def allocations(school, payment_id):
    return school.count("finance_payment_allocations", "payment_id = :p", p=payment_id)


def new_item(person, **fields):
    form = {"name": "Fee", "category": "School Fees", "applicability": "Full Session", "amount": "1000",
            "required": "1", "notes": "", "class_ids": [JSS1], **fields}
    return person.post(f"{ITEMS}/new", form, page=f"{ITEMS}/new")


def item_id(school, name):
    return school.one("SELECT id FROM finance_fee_items WHERE name = :n", n=name)


def history_rows(html, student_id):
    """The Fee Structure page's history dialog for one student: [(session, term, fee, status)]."""
    marker = f'id="assessmentHistoryModal-{student_id}"'
    if marker not in html:
        return []
    dialog = html.split(marker, 1)[1].split("</dialog>")[0]
    rows, session, term = [], None, None
    for m in re.finditer(r"<h3>(.*?)</h3>|<h4>(.*?)</h4>|<td>([^<]*)</td>\s*<td class=\"numeric\">.*?"
                         r"cr-finance-status \w+\">([^<]*)<", dialog, re.S):
        if m.group(1) is not None:
            session = m.group(1).strip()
        elif m.group(2) is not None:
            term = m.group(2).strip()
        else:
            rows.append((session, term, m.group(3).strip(), m.group(4).strip()))
    return rows


# ================================================================ 0. the people
IVY = register_student(op_alpha, alpha, "Ivy", "Bench", "ivy.bench@alpha.example", "08031110001")
JON = register_student(op_alpha, alpha, "Jon", "Nocontact", "", "")  # no guardian address or number at all
ADA = register_student(op_alpha, alpha, "Ada", "Obi", "obi.family@alpha.example", "08031234567")
BOLA = register_student(op_alpha, alpha, "Bola", "Eze", "eze.family@alpha.example", "08035550002")
KEMI = register_student(op_alpha, alpha, "Kemi", "Kobo", "kobo.family@alpha.example", "08035550003")
check("five students were registered at Alpha through the real form", None not in (IVY, JON, ADA, BOLA, KEMI))
mum_obi = make_parent(op_alpha, alpha, "Mrs Obi", "mrs.obi", "mrs.obi@alpha.example", [ADA])
mum_eze = make_parent(op_alpha, alpha, "Mrs Eze", "mrs.eze", "mrs.eze@alpha.example", [BOLA])
mum_kobo = make_parent(op_alpha, alpha, "Mrs Kobo", "mrs.kobo", "mrs.kobo@alpha.example", [KEMI])
check("three families at Alpha each have a parent who signed in",
      all(p.get("/parent/dashboard").status_code == 200 for p in (mum_obi, mum_eze, mum_kobo)))

# ================================================================ 1. the fee catalogue
check("the fee categories are the fixed list the school office chose from",
      {"School Fees", "Uniform", "Books & Stationery"} <= set(A.FINANCE_FEE_CATEGORIES))
check("each term stands on its own beside Full Session and One-time",
      A.FINANCE_FEE_APPLICABILITY == ["Full Session", "First Term", "Second Term", "Third Term", "One-time"])
check("the student account and payment allocation pages need real finance permissions, not bare admin access",
      ADMIN_ENDPOINT_PERMISSIONS.get("admin_finance_student_account") == "finance.view_own"
      and ADMIN_ENDPOINT_PERMISSIONS.get("admin_finance_payment_allocate") == "finance.record"
      and ADMIN_ENDPOINT_PERMISSIONS.get("admin_finance_fee_items") == "finance.manage")

form_page = op_alpha.text(f"{ITEMS}/new")
check("the new fee item form offers Category as a real drop-down", '<select name="category"' in form_page)
check("…and not as free text", re.search(r'<input[^>]*name="category"', form_page) is None)
check("…with every category in the list as an option",
      all(f">{c.replace('&', '&amp;')}<" in form_page for c in A.FINANCE_FEE_CATEGORIES))
check("every billing rule is offered as its own choice",
      all(f'value="{rule}"' in form_page and f"<span>{rule}</span>" in form_page for rule in A.FINANCE_FEE_APPLICABILITY))
check("the old bundled options (\"First Term only\") are gone",
      "Second and third term only" not in form_page and "First Term only" not in form_page)

# a scripted POST must not slip a made-up value into the catalogue
before = alpha.count("finance_fee_items")
bad = {
    "an unlisted category": ({"category": "Not A Real Category"}, "Select a valid fee category"),
    "an unlisted billing rule": ({"applicability": "Whenever"}, "Select a valid billing rule"),
    "a negative amount": ({"amount": "-5"}, "cannot be negative"),
    "an amount that is not a number": ({"amount": "abc"}, "fee amount"),
    "an amount of NaN": ({"amount": "nan"}, "fee amount"),
    "an infinite amount": ({"amount": "inf"}, "fee amount"),
    "an absurdly large amount": ({"amount": "1e30"}, "fee amount"),
    "no name": ({"name": ""}, "name is required"),
    "no class": ({"class_ids": []}, "at least one active class"),
}
for label, (fields, message) in bad.items():
    r = new_item(op_alpha, **{"name": "Bad Fee", **fields})
    check(f"a fee item with {label} is refused, with a message", message.lower() in r.get_data(as_text=True).lower(),
          f"HTTP {r.status_code}")
check("…and none of them was written", alpha.count("finance_fee_items") == before)

for name, category, rule, amount in (
        ("Tuition (JSS 1)", "Tuition", "First Term", "40000"), ("Uniform", "Uniform", "Full Session", "10000"),
        ("Books pack", "Books & Stationery", "Full Session", "8000"), ("Bus Transport", "Transport", "First Term", "30000"),
        ("Development levy", "Development Levy", "Full Session", "300.30"), ("Club", "Sports & Clubs", "Full Session", "2000")):
    new_item(op_alpha, name=name, category=category, applicability=rule, amount=amount)
TUITION, UNIFORM, BOOKS, BUS, LEVY, CLUB = (item_id(alpha, n) for n in (
    "Tuition (JSS 1)", "Uniform", "Books pack", "Bus Transport", "Development levy", "Club"))
row = alpha.sql("SELECT category, applicability, amount FROM finance_fee_items WHERE id = :i", i=TUITION)[0]
check("a valid fee item is saved with the chosen category, billing rule and amount",
      tuple(row) == ("Tuition", "First Term", 40000))
check("…the amount is kept to the kobo", alpha.one("SELECT amount FROM finance_fee_items WHERE id = :i", i=LEVY) == 300.30)
check("…and it is mapped to the class it was made for",
      alpha.count("finance_fee_item_classes", "fee_item_id = :f AND class_id = :c AND active = 1", f=TUITION, c=JSS1) == 1)
new_item(op_alpha, name="JSS 2 trip", category="Other", applicability="One-time", amount="5000", class_ids=[JSS2])
TRIP = item_id(alpha, "JSS 2 trip")

# a row from before the catalogue was fixed keeps its value, and is flagged
admin_row = alpha.one("SELECT id FROM admins ORDER BY id LIMIT 1")
alpha.sql("INSERT INTO finance_fee_items (name, category, stage, amount, applicability, required, optional, active, "
          "created_at, updated_at, created_by, updated_by) VALUES ('Legacy Fee', 'Miscellaneous', 'All', 5000, "
          "'Second and third term only', 1, 0, 1, '2026-01-01', '2026-01-01', :a, :a)", a=admin_row)
LEGACY = item_id(alpha, "Legacy Fee")
alpha.sql("INSERT INTO finance_fee_item_classes (fee_item_id, class_id, active, created_at, created_by) "
          "VALUES (:f, :c, 1, '2026-01-01', :a)", f=LEGACY, c=JSS1, a=admin_row)
edit_page = op_alpha.text(f"{ITEMS}/{LEGACY}/edit")
check("editing an old row shows its original billing rule, selected", 'value="Second and third term only" checked' in edit_page)
check("…and flags it as legacy so an admin knows to update it", "(legacy)" in edit_page)
check("…and shows its original category too", "Miscellaneous (legacy)" in edit_page)
r = op_alpha.post(f"{ITEMS}/{LEGACY}/edit", {"name": "Legacy Fee", "category": "Miscellaneous", "amount": "5000",
                                             "applicability": "Second and third term only", "class_ids": [JSS1]},
                  page=f"{ITEMS}/{LEGACY}/edit")
check("saving it again with the old values is refused, and the row is not rewritten",
      "valid" in r.get_data(as_text=True).lower()
      and tuple(alpha.sql("SELECT category, applicability FROM finance_fee_items WHERE id = :i", i=LEGACY)[0])
      == ("Miscellaneous", "Second and third term only"))
op_alpha.post(f"{ITEMS}/{LEGACY}/edit", {"name": "Legacy Fee", "category": "Other", "amount": "5000",
                                         "applicability": "Second Term", "class_ids": [JSS1]}, page=f"{ITEMS}/{LEGACY}/edit")
check("choosing a valid category and rule is what changes it",
      tuple(alpha.sql("SELECT category, applicability FROM finance_fee_items WHERE id = :i", i=LEGACY)[0]) == ("Other", "Second Term"))
check("…and the legacy label is gone", "(legacy)" not in op_alpha.text(f"{ITEMS}/{LEGACY}/edit"))
check("an unknown fee item is a 404, not an error", op_alpha.get(f"{ITEMS}/999999/edit").status_code == 404)

# ================================================================ 2. charging students: who, what, when, how often
Outbox.mail.clear(), Outbox.whatsapp.clear()
r = assess(op_alpha, JON, CURRENT, [CLUB], term="First Term", amount="1")
check("a fee is charged to a student for a term", alpha.count("finance_fee_assessments", "student_id = :s", s=JON) == 1)
row = alpha.sql("SELECT amount, term, category, fee_item_id, active FROM finance_fee_assessments WHERE student_id = :s", s=JON)[0]
check("…at the amount in the catalogue, never one sent by the browser",
      tuple(row) == (2000, "First Term", "Sports & Clubs", CLUB, 1))
r = assess(op_alpha, JON, CURRENT, [CLUB], term="First Term")
check("charging the same fee again for the same student and term is refused, with a reason",
      "already exist" in op_alpha.said() and alpha.count("finance_fee_assessments", "student_id = :s", s=JON) == 1)
r = assess(op_alpha, JON, CURRENT, [CLUB, LEVY], term="First Term")
check("a batch with one repeat is refused as a whole: nothing is written, not even the new fee",
      alpha.count("finance_fee_assessments", "student_id = :s", s=JON) == 1
      and alpha.count("finance_fee_assessments", "student_id = :s AND fee_item_id = :f", s=JON, f=LEVY) == 0)
assess(op_alpha, JON, CURRENT, [CLUB], term="Second Term")
check("the same fee in another term is a separate charge",
      alpha.count("finance_fee_assessments", "student_id = :s AND fee_item_id = :f", s=JON, f=CLUB) == 2)
before = alpha.count("finance_fee_assessments")
assess(op_alpha, JON, CURRENT, [CLUB], term="Whenever")
check("a term that does not exist is refused", alpha.count("finance_fee_assessments") == before, op_alpha.said())
assess(op_alpha, JON, CURRENT, [TRIP], term="Full Session")
check("a fee can only be charged to a class it is mapped to", "blocked" in op_alpha.said()
      and alpha.count("finance_fee_assessments", "fee_item_id = :f", f=TRIP) == 0)
assess(op_alpha, JON, CURRENT, [], term="First Term")
check("a charge with no fee chosen is refused", "at least one fee item" in op_alpha.said())
assess(op_alpha, 999999, CURRENT, [CLUB], term="Third Term")
check("a student who does not exist cannot be charged", "could not be found" in op_alpha.said()
      and alpha.count("finance_fee_assessments", "student_id = 999999") == 0)
op_alpha.post(f"{ITEMS}/{CLUB}/toggle", {}, page=ITEMS)
check("a fee can be archived", alpha.one("SELECT active FROM finance_fee_items WHERE id = :i", i=CLUB) == 0)
assess(op_alpha, JON, CURRENT, [CLUB], term="Third Term")
check("an archived fee cannot be charged", "no longer available" in op_alpha.said()
      and alpha.count("finance_fee_assessments", "student_id = :s AND term = 'Third Term'", s=JON) == 0)
op_alpha.post(f"{ITEMS}/{CLUB}/toggle", {}, page=ITEMS)
check("…and can be brought back, with what was charged before untouched",
      alpha.one("SELECT active FROM finance_fee_items WHERE id = :i", i=CLUB) == 1
      and alpha.count("finance_fee_assessments", "student_id = :s AND active = 1", s=JON) == 2)

# changing a fee's amount never rewrites what students were already charged
op_alpha.post(f"{ITEMS}/{CLUB}/edit", {"name": "Club", "category": "Sports & Clubs", "applicability": "Full Session",
                                       "amount": "2500", "class_ids": [JSS1], "required": "1"}, page=f"{ITEMS}/{CLUB}/edit")
check("editing a fee changes the catalogue", alpha.one("SELECT amount FROM finance_fee_items WHERE id = :i", i=CLUB) == 2500)
check("…but not what Jon was already charged",
      sorted(r[0] for r in alpha.sql("SELECT amount FROM finance_fee_assessments WHERE student_id = :s", s=JON)) == [2000, 2000])
assess(op_alpha, JON, CURRENT, [CLUB], term="Third Term")
check("…and the next charge uses the new amount",
      alpha.one("SELECT amount FROM finance_fee_assessments WHERE student_id = :s AND term = 'Third Term'", s=JON) == 2500)
check("a student with no guardian address or number is still charged, and nothing was sent for them",
      alpha.count("finance_fee_assessments", "student_id = :s", s=JON) == 3 and not Outbox.mail and not Outbox.whatsapp)

# ================================================================ 3. paying, and what "paid" means
assess(op_alpha, IVY, CURRENT, [TUITION, UNIFORM], term="First Term")
assess(op_alpha, IVY, CURRENT, [LEVY], term="Full Session")
A_TUI, A_UNI = assessment_id(alpha, IVY, TUITION), assessment_id(alpha, IVY, UNIFORM)
A_LEVY = assessment_id(alpha, IVY, LEVY, "Full Session")
check("Ivy was charged tuition, uniform and the levy", None not in (A_TUI, A_UNI, A_LEVY))

# a payment is refused unless it is a real, finite, sensible amount for a real student and session
paid_before = alpha.count("finance_payments")
bad_payments = {
    "no amount": (IVY, CURRENT, "", "amount"),
    "a zero amount": (IVY, CURRENT, "0", "greater than zero"),
    "a negative amount": (IVY, CURRENT, "-500", "greater than zero"),
    "an amount that is not a number": (IVY, CURRENT, "abc", "amount"),
    "an amount of NaN": (IVY, CURRENT, "nan", "amount"),
    "an infinite amount": (IVY, CURRENT, "inf", "amount"),
    "an absurdly large amount": (IVY, CURRENT, "1e30", "amount"),
    "no student": ("", CURRENT, "1500", "select a student"),
    "a student who does not exist": (999999, CURRENT, "1500", "student not found"),
    "no academic session": (IVY, "", "1500", "select an academic session"),
}
for label, (student, session, amount, message) in bad_payments.items():
    r, new_id = pay(op_alpha, student, session, amount)
    check(f"a payment with {label} is refused, with a message", new_id is None and r.status_code == 200
          and message in r.get_data(as_text=True).lower(), f"HTTP {r.status_code} {r.headers.get('Location', '')}")
check("…and none of them was recorded", alpha.count("finance_payments") == paid_before)

r, P1 = pay(op_alpha, IVY, CURRENT, "15000", payer="Ivy's Mum", category="Tuition", reference="CASH-001")
row = alpha.sql("SELECT receipt_no, amount, status, payer_name, recorded_by FROM finance_payments WHERE id = :p", p=P1)[0]
SCHOOL_CODE = alpha.one("SELECT code FROM schools ORDER BY id LIMIT 1")
check("a payment is recorded and gets a receipt number with the school's own prefix, counted from one",
      row[0] == f"{SCHOOL_CODE}-{YEAR}-00001" and SCHOOL_CODE.upper() == "ALPHA", str(tuple(row)))
check("…as posted, for the amount and payer entered", (row[1], row[2], row[3]) == (15000, "posted", "Ivy's Mum"))
check("…and the receipt page opens for it", f"{SCHOOL_CODE}-{YEAR}-00001" in op_alpha.text(f"{FIN_URL}/receipts/{P1}"))
mine = account(alpha, IVY)
check("a payment that has not been allocated to a fee pays nothing: every fee is still Unpaid",
      all(mine[c]["status"] == "Unpaid" and mine[c]["paid"] == 0 for c in ("Tuition", "Uniform", "Development Levy")),
      str({c: r["status"] for c, r in mine.items()}))
totals = alpha.run(lambda: FIN._finance_student_lifetime_totals(IVY, CURRENT))
check("…it is reported on the side as unallocated, and the balance is the full charge",
      (totals["assessed"], totals["paid"], totals["outstanding"], totals["unallocated"]) == (50300.30, 0, 50300.30, 15000),
      str(totals))

# allocating: what is refused, and that nothing is written when it is
page = op_alpha.text(f"{FIN_URL}/payments/{P1}/allocate")
check("the allocate page lists the fees still owing and how much of the payment is free",
      all(f'data-assessment-id="{a}"' in page for a in (A_TUI, A_UNI, A_LEVY)) and "₦15000.00" in page)
JON_ASSESSMENT = alpha.one("SELECT id FROM finance_fee_assessments WHERE student_id = :s LIMIT 1", s=JON)
refused = {
    "a body that is not JSON": (None, "not json", "invalid"),
    "a body that is not an object": (None, "[1, 2]", "invalid"),
    "more than the fee still owes": ({A_TUI: 50000}, None, "exceeds its outstanding balance"),
    "more than the payment holds": ({A_TUI: 12000, A_UNI: 6000}, None, "exceeds the payment balance"),
    "another student's fee": ({JON_ASSESSMENT: 100}, None, "same student"),
    "a fee that does not exist": ({999999: 100}, None, "could not be found"),
    "only zero and negative amounts": ({A_TUI: 0, A_UNI: -5}, None, "at least one"),
    "an amount of NaN": (None, json.dumps({str(A_TUI): "nan"}), "invalid"),
    "an amount that is not a number": (None, json.dumps({str(A_TUI): "lots"}), "invalid"),
}
for label, (amounts, raw, message) in refused.items():
    r, said = allocate(op_alpha, P1, amounts, raw)
    check(f"an allocation with {label} is refused, and nothing is written",
          message in said and allocations(alpha, P1) == 0, said)

r, said = allocate(op_alpha, P1, {A_TUI: 15000})
check("a valid allocation is saved, and says the payment is fully allocated",
      allocations(alpha, P1) == 1 and "fully allocated" in said, said)
mine = account(alpha, IVY)
check("the fee it was applied to is Part Paid: 15,000 of 40,000, 25,000 still owed",
      (mine["Tuition"]["status"], mine["Tuition"]["paid"], mine["Tuition"]["outstanding"]) == ("Part Paid", 15000, 25000))
check("…and the other fees are untouched", mine["Uniform"]["status"] == "Unpaid" and mine["Development Levy"]["status"] == "Unpaid")

fee_page = op_alpha.text(ITEMS)
check("the Fee Structure page shows Part Paid for that charge, with a link to the student's account",
      (CURRENT_NAME, "First Term", "Tuition (JSS 1)", "Part Paid") in history_rows(fee_page, IVY)
      and f"/admin/finance/students/{IVY}/account?session_id={CURRENT}" in fee_page)
account_page = op_alpha.text(f"{FIN_URL}/students/{IVY}/account?session_id={CURRENT}")
check("the student's account page shows the outstanding balance, the status, and the payment with an Allocate link",
      f"₦{money(25000)}" in account_page and 'cr-finance-status partial">Part Paid' in account_page
      and f"{SCHOOL_CODE}-{YEAR}-00001" in account_page and f"/admin/finance/payments/{P1}/allocate" in account_page)
receipt_page = op_alpha.text(f"{FIN_URL}/receipts/{P1}")
check("the receipt page links to Allocate to Fees and to the student's fee account",
      f"/admin/finance/payments/{P1}/allocate" in receipt_page and f"/admin/finance/students/{IVY}/account" in receipt_page)
check("the finance dashboard's recent payments link to the student's account too",
      f"/admin/finance/students/{IVY}/account" in op_alpha.text(FIN_URL))
picked = op_alpha.get(f"{FIN_URL}/students/{IVY}/assessed-items.json?session_id={CURRENT}").get_json()
check("the fee picker's JSON lists what Ivy is charged for each term, none of it paid yet",
      picked.get("First Term", {}).get(str(TUITION), {}).get("paid") is False
      and picked.get("First Term", {}).get(str(UNIFORM), {}).get("paid") is False
      and picked.get("Full Session", {}).get(str(LEVY), {}).get("paid") is False, str(picked))

# paying a fee in full: it disappears from the allocate screen and can never be allocated again
r, P2 = pay(op_alpha, IVY, CURRENT, "25000", payer="Ivy's Mum", category="Tuition")
allocate(op_alpha, P2, {A_TUI: 25000})
mine = account(alpha, IVY)
check("the second payment settles the fee: Paid, nothing owed",
      (mine["Tuition"]["status"], mine["Tuition"]["paid"], mine["Tuition"]["outstanding"]) == ("Paid", 40000, 0))
check("…the Fee Structure page and the fee picker's JSON both say Paid",
      (CURRENT_NAME, "First Term", "Tuition (JSS 1)", "Paid") in history_rows(op_alpha.text(ITEMS), IVY)
      and op_alpha.get(f"{FIN_URL}/students/{IVY}/assessed-items.json?session_id={CURRENT}").get_json()
      ["First Term"][str(TUITION)]["paid"] is True)
r, P3 = pay(op_alpha, IVY, CURRENT, "5000", payer="Ivy's Mum", category="Uniform")
page = op_alpha.text(f"{FIN_URL}/payments/{P3}/allocate")
check("the allocate screen no longer offers the fee that is paid in full, and still offers the ones that are not",
      f'data-assessment-id="{A_TUI}"' not in page and f'data-assessment-id="{A_UNI}"' in page)
r, said = allocate(op_alpha, P3, {A_TUI: 100})
check("a hand-made request to allocate to a fee already paid in full is refused, and writes nothing",
      "already been fully paid" in said and allocations(alpha, P3) == 0, said)
r, said = allocate(op_alpha, P3, {A_UNI: 4000})
check("part of a payment can be applied, and the rest is reported as unallocated",
      "₦1,000.00 remains unallocated" in said and allocations(alpha, P3) == 1, said)
check("…and the allocate page says how much is still free", "₦1000.00" in op_alpha.text(f"{FIN_URL}/payments/{P3}/allocate"))
allocate(op_alpha, P3, {A_UNI: 1000})
r, said = allocate(op_alpha, P3, {A_UNI: 1})
check("once a payment is fully applied, no more can be taken from it",
      "exceeds the payment balance" in said and allocations(alpha, P3) == 2, said)
mine = account(alpha, IVY)
check("Uniform is Part Paid: 5,000 of 10,000", (mine["Uniform"]["status"], mine["Uniform"]["paid"]) == ("Part Paid", 5000))

# voiding a payment gives the fee its balance back, and the fee can be paid again
r = op_alpha.post(f"{FIN_URL}/payments/{P2}/void", {"reason": ""}, page=f"{FIN_URL}/receipts/{P2}")
check("a payment cannot be voided without a reason", alpha.one("SELECT status FROM finance_payments WHERE id = :p", p=P2) == "posted"
      and "reason is required" in op_alpha.said())
op_alpha.post(f"{FIN_URL}/payments/{P2}/void", {"reason": "Entered against the wrong child"}, page=f"{FIN_URL}/receipts/{P2}")
row = alpha.sql("SELECT status, void_reason, voided_by FROM finance_payments WHERE id = :p", p=P2)[0]
check("a payment is voided with its reason and who voided it, and stays on record",
      (row[0], row[1]) == ("voided", "Entered against the wrong child") and row[2] is not None)
check("…the receipt page says VOIDED", "VOIDED" in op_alpha.text(f"{FIN_URL}/receipts/{P2}"))
mine = account(alpha, IVY)
check("…and the money no longer counts: the fee is Part Paid again, 25,000 owed",
      (mine["Tuition"]["status"], mine["Tuition"]["paid"], mine["Tuition"]["outstanding"]) == ("Part Paid", 15000, 25000))
check("…the fee picker sees it as unpaid again",
      op_alpha.get(f"{FIN_URL}/students/{IVY}/assessed-items.json?session_id={CURRENT}").get_json()
      ["First Term"][str(TUITION)]["paid"] is False)
totals = alpha.run(lambda: FIN._finance_student_lifetime_totals(IVY, CURRENT))
check("…and it is not counted as unallocated money either", totals["unallocated"] == 0, str(totals))
r, said = allocate(op_alpha, P2, {A_TUI: 100})
check("a voided payment cannot be allocated", "only a posted payment" in said and allocations(alpha, P2) == 1, said)
op_alpha.post(f"{FIN_URL}/payments/{P2}/void", {"reason": "Again"}, page=f"{FIN_URL}/receipts/{P2}")
check("…nor voided twice", "only a posted payment" in op_alpha.said()
      and alpha.one("SELECT void_reason FROM finance_payments WHERE id = :p", p=P2) == "Entered against the wrong child")
r, P4 = pay(op_alpha, IVY, CURRENT, "25000", payer="Ivy's Mum", category="Tuition")
r, said = allocate(op_alpha, P4, {A_TUI: 25000})
check("the balance the void gave back can be paid again: a new payment settles the fee",
      allocations(alpha, P4) == 1 and account(alpha, IVY)["Tuition"]["status"] == "Paid", said)
totals = alpha.run(lambda: FIN._finance_student_lifetime_totals(IVY, CURRENT))
check("Ivy's totals add up: 50,300.30 charged, 45,000 paid to fees, 5,300.30 owed, nothing unallocated",
      (totals["assessed"], totals["paid"], totals["outstanding"], totals["unallocated"]) == (50300.30, 45000, 5300.30, 0),
      str(totals))
paid_rows = alpha.sql("SELECT sum(amount) FROM finance_payments WHERE student_id = :s AND status = 'posted'", s=IVY)[0][0]
check("…which matches the money actually posted for her (15,000 + 5,000 + 25,000)", paid_rows == 45000)

# a payment is kept to the kobo
r, PJ = pay(op_alpha, JON, CURRENT, "100.999", payer="Jon's Dad", category="Uniform")
check("an amount with more than two decimals is kept to the kobo, never stored as a fraction of one",
      alpha.one("SELECT amount FROM finance_payments WHERE id = :p", p=PJ) == 101.0)
page = op_alpha.text(f"{FIN_URL}/receipts/{PJ}")
check("…and the receipt reads ₦101.00, in words too, not ₦100.100",
      "₦101.00" in page and "one hundred and one naira only" in page and "100.100" not in page)

# kobo amounts add up exactly
assess(op_alpha, KEMI, CURRENT, [LEVY], term="Full Session")
A_KEMI = assessment_id(alpha, KEMI, LEVY, "Full Session")
r, PK1 = pay(op_alpha, KEMI, CURRENT, "100.10", payer="Mrs Kobo", category="Development Levy")
allocate(op_alpha, PK1, {A_KEMI: 100.10})
check("a fee of ₦300.30 paid ₦100.10 is Part Paid, ₦200.20 owed",
      (account(alpha, KEMI)["Development Levy"]["status"], account(alpha, KEMI)["Development Levy"]["outstanding"]) == ("Part Paid", 200.20))
r, PK2 = pay(op_alpha, KEMI, CURRENT, "200.20", payer="Mrs Kobo", category="Development Levy")
allocate(op_alpha, PK2, {A_KEMI: 200.20})
kobo = account(alpha, KEMI)["Development Levy"]
check("…and once ₦200.20 more is applied it is Paid, with exactly nothing owed (not a rounding crumb)",
      (kobo["status"], kobo["outstanding"]) == ("Paid", 0), str(kobo))
check("…the fee picker and the Fee Structure page agree",
      op_alpha.get(f"{FIN_URL}/students/{KEMI}/assessed-items.json?session_id={CURRENT}").get_json()
      ["Full Session"][str(LEVY)]["paid"] is True
      and (CURRENT_NAME, "Full Session", "Development levy", "Paid") in history_rows(op_alpha.text(ITEMS), KEMI))
dash = mum_kobo.text("/parent/dashboard")
check("…and the parent's dashboard says there is no outstanding fee, instead of an outstanding balance of ₦0.00",
      "No outstanding fees" in dash and "You have an outstanding balance" not in dash)
check("…and the parent's fee page shows the levy as Paid",
      'pcf-badge paid">Paid' in mum_kobo.text(f"/parent/children/{KEMI}/finance"))

# ================================================================ 4. no session's balance is ever hidden
op_alpha.post("/admin/school/sessions", {"action": "create", "name": "2025/2026", "start_date": "2025-09-01",
                                         "end_date": "2026-07-31"}, page="/admin/school/sessions")
PAST = alpha.one("SELECT id FROM academic_sessions WHERE name = '2025/2026'")
PAST_NAME = "2025/2026"
check("a past academic session exists that is not the current one", PAST is not None and PAST != CURRENT and CURRENT_NAME != PAST_NAME)
# (There is no one-click way to put a student in an old session, so this one row is written directly.)
alpha.sql("INSERT INTO student_enrolments (student_id, class_id, session_id, enrolled_at, active) "
          "VALUES (:s, :c, :p, '2025-09-01', 1)", s=ADA, c=JSS1, p=PAST)

# last session: 40,000 tuition, 15,000 paid to it: 25,000 owed, and the school has moved on
assess(op_alpha, ADA, PAST, [TUITION], term="First Term")
A_PAST = assessment_id(alpha, ADA, TUITION, "First Term", PAST)
r, PA1 = pay(op_alpha, ADA, PAST, "15000", payer="Mrs Obi", category="Tuition")
allocate(op_alpha, PA1, {A_PAST: 15000})
# this session: 10,000 uniform paid in full, 8,000 books not paid, and 5,000 paid but not yet applied to anything
assess(op_alpha, ADA, CURRENT, [UNIFORM, BOOKS], term="Full Session")
A_UNI_ADA, A_BOOKS_ADA = assessment_id(alpha, ADA, UNIFORM, "Full Session"), assessment_id(alpha, ADA, BOOKS, "Full Session")
r, PA2 = pay(op_alpha, ADA, CURRENT, "10000", payer="Mrs Obi", category="Uniform")
allocate(op_alpha, PA2, {A_UNI_ADA: 10000})
r, PA3 = pay(op_alpha, ADA, CURRENT, "5000", payer="Mrs Obi", category="Books & Stationery")

lifetime = alpha.run(lambda: FIN._finance_student_lifetime_totals(ADA))
check("across every session Ada was charged 58,000, has 25,000 applied to her fees and owes 33,000, "
      "with 5,000 received but not yet applied (never netted off the debt)",
      (lifetime["assessed"], lifetime["paid"], lifetime["outstanding"], lifetime["unallocated"]) == (58000, 25000, 33000, 5000),
      str(lifetime))
check("last session's fee is Part Paid with 25,000 owed", account(alpha, ADA, PAST)["Tuition"]["outstanding"] == 25000
      and account(alpha, ADA, PAST)["Tuition"]["status"] == "Part Paid")
check("this session's fees are Paid and Unpaid",
      (account(alpha, ADA)["Uniform"]["status"], account(alpha, ADA)["Books & Stationery"]["status"]) == ("Paid", "Unpaid"))

fee_page = op_alpha.text(ITEMS)
check("the Fee Structure page has a searchable list of students, and a dialog for each",
      'id="financeHistorySearch"' in fee_page and f'id="assessmentHistoryModal-{ADA}"' in fee_page)
check("…whose dialog for Ada groups her charges by session, then term, each with its own Paid / Part Paid / Unpaid",
      sorted(history_rows(fee_page, ADA)) == sorted([
          (PAST_NAME, "First Term", "Tuition (JSS 1)", "Part Paid"),
          (CURRENT_NAME, "Full Session", "Uniform", "Paid"),
          (CURRENT_NAME, "Full Session", "Books pack", "Unpaid")]), str(history_rows(fee_page, ADA)))
check("the fee picker carries each fee's id, and a place for an 'already charged' label",
      f'data-fee-item-id="{UNIFORM}"' in fee_page and "data-fee-already" in fee_page)
now_json = op_alpha.get(f"{FIN_URL}/students/{ADA}/assessed-items.json?session_id={CURRENT}").get_json()
past_json = op_alpha.get(f"{FIN_URL}/students/{ADA}/assessed-items.json?session_id={PAST}").get_json()
check("the picker's JSON marks the paid fee as paid and the unpaid one as not, for this session",
      now_json["Full Session"][str(UNIFORM)]["paid"] is True and now_json["Full Session"][str(BOOKS)]["paid"] is False, str(now_json))
check("…does not mention a fee never charged for that term or a fee charged in another session",
      "First Term" not in now_json and str(TUITION) not in now_json.get("Full Session", {}), str(now_json))
check("…and reports last session's fee as unpaid", past_json["First Term"][str(TUITION)]["paid"] is False)
check("…an unusable request gets an empty answer, not an error",
      op_alpha.get(f"{FIN_URL}/students/{ADA}/assessed-items.json").get_json() == {}
      and op_alpha.get(f"{FIN_URL}/students/{ADA}/assessed-items.json?session_id=abc").get_json() == {})

page = op_alpha.text(f"{FIN_URL}/payments/{PA3}/allocate")
check("the allocate screen for the 5,000 offers the unpaid books, not the paid uniform nor last session's tuition",
      f'data-assessment-id="{A_BOOKS_ADA}"' in page and f'data-assessment-id="{A_UNI_ADA}"' not in page
      and f'data-assessment-id="{A_PAST}"' not in page)
r, said = allocate(op_alpha, PA3, {A_UNI_ADA: 100})
check("a hand-made request for the paid uniform is refused, and writes nothing",
      "already been fully paid" in said and allocations(alpha, PA3) == 0, said)

dash = mum_obi.text("/parent/dashboard")
check("the parent's dashboard has a Fees panel", "<span>Fees</span>" in dash)
check("…showing the lifetime balance, 33,000, not zero, though this session's debt is only 8,000",
      "₦33,000.00" in dash and "₦33,000</strong>" in dash and "You have an outstanding balance" in dash,
      re.findall(r"pd-outstanding-amount\">([^<]*)<", dash)[0] if "pd-outstanding-amount" in dash else "no banner")
check("…and lists the child once, and links to their fee account, though Ada is enrolled in two sessions "
      "(listed twice, her balance was added up twice)", f"/parent/children/{ADA}/finance" in dash and dash.count(f'href="/parent/children/{ADA}"') == 1
      and alpha.count("student_enrolments", "student_id = :s AND active = 1", s=ADA) == 2)
check("…and shows nothing about another family's child", "Bola" not in dash and "Kemi" not in dash)
fee_account = mum_obi.text(f"/parent/children/{ADA}/finance")
check("the parent's fee page opens with the same lifetime figures: 58,000 charged, 25,000 paid, 33,000 owed",
      all(f"₦{money(v)}" in fee_account for v in (58000, 25000, 33000)))
check("…calls out that a balance is also outstanding in last session", "Balance also outstanding in:" in fee_account
      and f"{PAST_NAME} · ₦25,000" in fee_account)
check("…reports the 5,000 received but not applied to a fee, separately",
      "₦5,000.00 recently paid, not yet applied to a specific fee" in fee_account)
check("…lists this session's fees with Paid and Unpaid",
      'pcf-badge paid">Paid' in fee_account and 'pcf-badge unpaid">Unpaid' in fee_account)
past_view = mum_obi.text(f"/parent/children/{ADA}/finance?session_id={PAST}")
check("…and last session's fee, when chosen, as Part Paid with 25,000 owed",
      'pcf-badge partial">Part Paid' in past_view and f"₦{money(25000)}" in past_view and "Tuition" in past_view)
check("the parent's payment list has a receipt link for each payment and uses real buttons",
      f"/parent/children/{ADA}/receipts/{PA1}/pdf" in fee_account and 'class="btn btn-light btn-small"' in fee_account)
css = open(os.path.join(ROOT, "static", "app.css"), encoding="utf-8").read()
check("the parent pages' stylesheet defines the button styles they use",
      all(needle in css for needle in (".btn{", ".btn-primary{", ".btn-light{")))

# a notification is New the first time it is seen, Old after
first = mum_obi.text(f"/parent/children/{ADA}")
second = mum_obi.text(f"/parent/children/{ADA}")
check("the alerts about a child are labelled New the first time the profile is opened",
      'class="pcd-notif-tag is-new"' in first and "New fee charged for Ada Obi" in first)
check("…and Old on every visit after that", 'class="pcd-notif-tag is-new"' not in second
      and 'class="pcd-notif-tag is-old"' in second)

# ================================================================ 5. what a parent is told, and never being blocked by it
Outbox.mail.clear(), Outbox.whatsapp.clear()


def bola_alerts():
    return alpha.count("school_notifications", "category = 'finance' AND student_id = :s", s=BOLA)


n_before = bola_alerts()
new_item(op_alpha, name="Bus Transport (JSS 1)", category="Transport", applicability="First Term", amount="30000")
BUS2 = item_id(alpha, "Bus Transport (JSS 1)")
assess(op_alpha, BOLA, CURRENT, [BUS2], term="First Term")
sent = [(s, m) for s, m in Outbox.mail if m["To"] == "eze.family@alpha.example"]
check("charging a fee emailed the guardian on the student's record, once", len(sent) == 1, str(len(Outbox.mail)))
settings, msg = sent[0] if sent else (None, None)
check("…through the school's own mail account, from the school's own address",
      settings is not None and settings.source == "school" and settings.host == "smtp.alpha.test"
      and msg["From"] == "office@alpha.example")
body = msg.get_content() if msg else ""
check("…saying what was charged, how much, and for which term and session",
      msg is not None and "Bola Eze" in msg["Subject"] and "Bus Transport (JSS 1)" in body and "₦30,000.00" in body
      and "First Term" in body and CURRENT_NAME in body, body[:200])
check("…signed with the school's name, not another's", "Alpha School" in body and "Beta" not in body)
texts = [(token, payload) for token, payload in Outbox.whatsapp if payload["to"] == "+2348035550002"]
check("…and sent the guardian's WhatsApp number a message with the school's own token",
      len(texts) == 1 and texts[0][0] == "Bearer token-alpha", str(Outbox.whatsapp))
check("…mentioning the school, the child, the fee and the amount",
      texts and all(s in texts[0][1]["text"]["body"] for s in ("Alpha School", "Bola Eze", "Bus Transport (JSS 1)", "₦30,000.00")))
check("…and left one in-app alert for that child's parent, and none for another family's",
      bola_alerts() == n_before + 1
      and "New fee charged for Bola Eze" in mum_eze.text("/parent/dashboard")
      and "New fee charged for Bola Eze" not in mum_obi.text("/parent/dashboard"))

Outbox.mail.clear(), Outbox.whatsapp.clear()
r, PB1 = pay(op_alpha, BOLA, CURRENT, "20000", payer="Mrs Eze", category="Transport")
receipt_b1 = alpha.one("SELECT receipt_no FROM finance_payments WHERE id = :p", p=PB1)
sent = [(s, m) for s, m in Outbox.mail if m["To"] == "eze.family@alpha.example"]
body = sent[0][1].get_content() if sent else ""
check("recording a payment emailed the guardian a confirmation, with the amount and the receipt number",
      len(sent) == 1 and "Payment received" in sent[0][1]["Subject"] and "₦20,000.00" in body and receipt_b1 in body, body[:200])
check("…and sent it by WhatsApp too, with the receipt number",
      any(p["to"] == "+2348035550002" and receipt_b1 in p["text"]["body"] and "₦20,000.00" in p["text"]["body"]
          for _, p in Outbox.whatsapp))
check("…and there are now two in-app alerts for Bola's parent: the charge and the payment", bola_alerts() == n_before + 2)

# the 20,000 is recorded but not applied to any fee yet: it is not "paid", and does not hide the debt
totals = alpha.run(lambda: FIN._finance_student_lifetime_totals(BOLA))
check("Bola owes the whole 30,000 for now, and the 20,000 is shown as received but unallocated",
      (totals["assessed"], totals["paid"], totals["outstanding"], totals["unallocated"]) == (30000, 0, 30000, 20000), str(totals))
dash = mum_eze.text("/parent/dashboard")
check("the dashboard says so: 30,000 outstanding", "₦30,000</strong>" in dash and "You have an outstanding balance" in dash)
page = mum_eze.text(f"/parent/children/{BOLA}/finance")
check("the fee page shows the fee as Unpaid, and the 20,000 as received but not yet applied",
      'pcf-badge unpaid">Unpaid' in page and "₦20,000.00 recently paid, not yet applied to a specific fee" in page)
check("…with the payment listed and its receipt one click away",
      receipt_b1 in page and f"/parent/children/{BOLA}/receipts/{PB1}/pdf" in page)
pdf = mum_eze.get(f"/parent/children/{BOLA}/receipts/{PB1}/pdf")
check("the parent can download the receipt PDF of their own child's payment",
      pdf.status_code == 200 and pdf.content_type == "application/pdf" and pdf.data.startswith(b"%PDF")
      and receipt_b1 in pdf.headers.get("Content-Disposition", ""))

A_BUS = assessment_id(alpha, BOLA, BUS2, "First Term")
allocate(op_alpha, PB1, {A_BUS: 20000})
totals = alpha.run(lambda: FIN._finance_student_lifetime_totals(BOLA))
check("once the office applies it, Bola's fee is Part Paid: 20,000 paid, 10,000 owed, nothing unallocated",
      (totals["paid"], totals["outstanding"], totals["unallocated"]) == (20000, 10000, 0)
      and 'pcf-badge partial">Part Paid' in mum_eze.text(f"/parent/children/{BOLA}/finance"), str(totals))
r, PB2 = pay(op_alpha, BOLA, CURRENT, "10000", payer="Mrs Eze", category="Transport")
allocate(op_alpha, PB2, {A_BUS: 10000})
dash = mum_eze.text("/parent/dashboard")
check("paid in full, the parent's dashboard says there is nothing outstanding",
      "No outstanding fees" in dash and "You have an outstanding balance" not in dash)

# a failed email or WhatsApp never stops a charge or a payment
Outbox.mail.clear(), Outbox.whatsapp.clear()
Outbox.outage = True
try:
    n_before = bola_alerts()
    assess(op_alpha, BOLA, CURRENT, [BOOKS], term="Full Session")
    r, PB3 = pay(op_alpha, BOLA, CURRENT, "1000", payer="Mrs Eze", category="Books & Stationery")
finally:
    Outbox.outage = False
check("with both email and WhatsApp down, the charge is still saved", alpha.count(
    "finance_fee_assessments", "student_id = :s AND fee_item_id = :f", s=BOLA, f=BOOKS) == 1)
check("…and the payment is still saved", PB3 is not None and alpha.one(
    "SELECT status FROM finance_payments WHERE id = :p", p=PB3) == "posted")
check("…and the in-app alerts were still left for the parent", bola_alerts() == n_before + 2)
check("…and nothing was sent", not Outbox.mail and not Outbox.whatsapp)

# a parent sees their own children, and only theirs
cross = {
    "another family's fee account": mum_obi.get(f"/parent/children/{BOLA}/finance"),
    "another family's child": mum_obi.get(f"/parent/children/{BOLA}"),
    "a receipt of another family's child": mum_obi.get(f"/parent/children/{BOLA}/receipts/{PB1}/pdf"),
    "another family's receipt under their own child's address": mum_obi.get(f"/parent/children/{ADA}/receipts/{PB1}/pdf"),
    "a payment that does not exist": mum_obi.get(f"/parent/children/{ADA}/receipts/999999/pdf"),
}
for label, response in cross.items():
    check(f"a parent cannot open {label}", response.status_code == 404, str(response.status_code))
anon = Person(ALPHA, "/login")
check("someone who is signed out is sent to sign in, not shown a fee account",
      anon.get(f"/parent/children/{ADA}/finance").status_code == 302
      and anon.get(f"/parent/children/{ADA}/receipts/{PA1}/pdf").status_code == 302)
check("a parent cannot reach the school's finance pages",
      all(mum_obi.get(f"{FIN_URL}{p}").status_code in (302, 403, 404) for p in
          ("", "/fee-items", f"/receipts/{PA1}", f"/students/{ADA}/account", f"/payments/{PA1}/allocate")))
parent_post = mum_obi.post(f"{FIN_URL}/payments/{PA3}/allocate", {"allocations": json.dumps({str(A_BOOKS_ADA): 5000})},
                           page="/parent/password")
check("…nor apply a payment to a fee", parent_post.status_code in (302, 403) and allocations(alpha, PA3) == 0)

# ================================================================ 6. who may do what
cashier, CASHIER_ID = add_admin(alpha, "cashier", "Finance Records Officer")
manager, MANAGER_ID = add_admin(alpha, "manager", "Finance Manager")
outsider, OUTSIDER_ID = add_admin(alpha, "librarian", "Librarian")
r, PC = pay(cashier, IVY, CURRENT, "3000", payer="Ivy's Mum", category="Uniform")
check("a cashier (finance.record) can record a payment, which is theirs",
      PC is not None and alpha.one("SELECT recorded_by FROM finance_payments WHERE id = :p", p=PC) == CASHIER_ID)
receipt_p1 = alpha.one("SELECT receipt_no FROM finance_payments WHERE id = :p", p=P1)
receipt_pc = alpha.one("SELECT receipt_no FROM finance_payments WHERE id = :p", p=PC)
check("…and can open the student's account (finance.view_own)",
      cashier.get(f"{FIN_URL}/students/{IVY}/account?session_id={CURRENT}").status_code == 200)
check("…and the allocate page of a payment they recorded", cashier.get(f"{FIN_URL}/payments/{PC}/allocate").status_code == 200)
check("…and their own receipt, as a page and as a PDF",
      cashier.get(f"{FIN_URL}/receipts/{PC}").status_code == 200 and cashier.get(f"{FIN_URL}/receipts/{PC}/pdf").status_code == 200)
check("…but not the allocate page, receipt or PDF of a payment someone else recorded",
      cashier.get(f"{FIN_URL}/payments/{P1}/allocate").status_code == 403
      and cashier.get(f"{FIN_URL}/receipts/{P1}").status_code == 403
      and cashier.get(f"{FIN_URL}/receipts/{P1}/pdf").status_code == 403)
rows_before = allocations(alpha, P1)
r = cashier.post(f"{FIN_URL}/payments/{P1}/allocate", {"allocations": json.dumps({str(A_LEVY): 100})})
check("…nor apply someone else's payment to a fee", r.status_code == 403 and allocations(alpha, P1) == rows_before)
check("…and cannot manage the fee catalogue or the receipt settings",
      all(cashier.get(f"{FIN_URL}{p}").status_code == 403 for p in ("/fee-items", "/fee-items/new", "/receipt-settings")))
charges = alpha.count("finance_fee_assessments")
r = cashier.post(f"{FIN_URL}/assessments/new", {"student_id": IVY, "session_id": CURRENT, "fee_item_id": [BOOKS], "term": "First Term"})
check("…nor charge a student a fee", r.status_code == 403 and alpha.count("finance_fee_assessments") == charges)
r = cashier.post(f"{FIN_URL}/payments/{PC}/void", {"reason": "I changed my mind"})
check("…nor void a payment", r.status_code == 403 and alpha.one("SELECT status FROM finance_payments WHERE id = :p", p=PC) == "posted")
dash = cashier.text(FIN_URL)
check("a cashier's dashboard lists only the payments they recorded, and has no school-wide balance",
      receipt_pc in dash and receipt_p1 not in dash and "Outstanding Assessed Fees" not in dash)

assessed = alpha.one("SELECT coalesce(sum(amount), 0) FROM finance_fee_assessments WHERE active = 1")
allocated = alpha.one("SELECT coalesce(sum(a.amount), 0) FROM finance_payment_allocations a "
                      "JOIN finance_payments p ON p.id = a.payment_id JOIN finance_fee_assessments f ON f.id = a.assessment_id "
                      "WHERE p.status = 'posted' AND f.active = 1")
dash = manager.text(FIN_URL)
check("a finance manager's dashboard shows everyone's payments and the school-wide outstanding balance, "
      "which is the charges less the money applied to them",
      receipt_pc in dash and receipt_p1 in dash and "Outstanding Assessed Fees" in dash
      and f"₦{max(0, assessed - allocated):.2f}" in dash, f"{assessed} - {allocated}")
check("…and can open the fee catalogue and anyone's receipt",
      manager.get(ITEMS).status_code == 200 and manager.get(f"{FIN_URL}/receipts/{P1}").status_code == 200)

forbidden = ("", "/payments/new", "/fee-items", f"/receipts/{P1}", f"/receipts/{P1}/pdf", f"/students/{IVY}/account",
             f"/payments/{P1}/allocate", f"/students/{IVY}/assessed-items.json?session_id={CURRENT}", "/receipt-settings")
check("an administrator with no finance permission is refused every finance page",
      all(outsider.get(f"{FIN_URL}{p}").status_code == 403 for p in forbidden),
      str([p for p in forbidden if outsider.get(f"{FIN_URL}{p}").status_code != 403]))
before = (alpha.count("finance_payments"), alpha.count("finance_payment_allocations"), alpha.count("finance_fee_assessments"))
outsider.post(f"{FIN_URL}/payments/new", {"student_id": IVY, "session_id": CURRENT, "amount": "100", "category": "Tuition",
                                          "method": "Cash"})
outsider.post(f"{FIN_URL}/payments/{PC}/allocate", {"allocations": json.dumps({str(A_UNI): 100})})
outsider.post(f"{FIN_URL}/assessments/new", {"student_id": IVY, "session_id": CURRENT, "fee_item_id": [BOOKS], "term": "First Term"})
check("…and cannot record a payment, apply one or charge a fee even with a valid form token",
      (alpha.count("finance_payments"), alpha.count("finance_payment_allocations"), alpha.count("finance_fee_assessments")) == before)
check("a POST without a form token is refused, and writes nothing",
      op_alpha.client.post(f"{FIN_URL}/payments/new", data={"student_id": IVY, "session_id": CURRENT, "amount": "100",
                                                            "category": "Tuition", "method": "Cash"},
                           base_url=ALPHA).status_code == 403 and alpha.count("finance_payments") == before[0])

# ================================================================ 7. school against school
JSS1_B = beta.one("SELECT id FROM school_classes WHERE name = 'JSS 1'")
CURRENT_B = beta.one("SELECT id FROM academic_sessions WHERE is_current = 1")
new_item(op_beta, name="Beta Lab Fee", category="Other", applicability="Full Session", amount="90000", class_ids=[JSS1_B])
LAB = item_id(beta, "Beta Lab Fee")
CHIKE = register_student(op_beta, beta, "Chike", "Betaman", "chike.family@beta.example", "08035550009")
mr_beta = make_parent(op_beta, beta, "Mr Betaman", "mr.betaman", "mr.betaman@beta.example", [CHIKE])
Outbox.mail.clear(), Outbox.whatsapp.clear()
assess(op_beta, CHIKE, CURRENT_B, [LAB], term="Full Session")
r, PBETA = pay(op_beta, CHIKE, CURRENT_B, "45000", payer="Beta Payer", category="Other")
A_LAB = assessment_id(beta, CHIKE, LAB, "Full Session", CURRENT_B)
allocate(op_beta, PBETA, {A_LAB: 45000})
BETA_CODE = beta.one("SELECT code FROM schools ORDER BY id LIMIT 1")
check("Beta charged, took a payment and applied it on its own, with its own receipt numbering from one",
      beta.one("SELECT receipt_no FROM finance_payments WHERE id = :p", p=PBETA) == f"{BETA_CODE}-{YEAR}-00001"
      and account(beta, CHIKE, CURRENT_B)["Other"]["status"] == "Part Paid")
check("Beta's alerts went out through Beta's own mail account and WhatsApp token, to Beta's own guardian",
      Outbox.mail and all(s.source == "school" and s.host == "smtp.beta.test" and m["From"] == "office@beta.example"
                          and m["To"] == "chike.family@beta.example" for s, m in Outbox.mail)
      and Outbox.whatsapp and all(t == "Bearer token-beta" and p["to"] == "+2348035550009" for t, p in Outbox.whatsapp)
      and all("Alpha" not in m.get_content() for _, m in Outbox.mail)
      and all("Alpha" not in p["text"]["body"] for _, p in Outbox.whatsapp))
check("Alpha's receipt numbers are its own, unbroken from one, and never Beta's",
      [r[0] for r in alpha.sql("SELECT receipt_no FROM finance_payments ORDER BY id")]
      == [f"{SCHOOL_CODE}-{YEAR}-{n:05d}" for n in range(1, alpha.count("finance_payments") + 1)] and SCHOOL_CODE != BETA_CODE)

alpha_secrets = ["Tuition (JSS 1)", "Books pack", "Development levy", "Bus Transport", "Ivy's Mum", "Mrs Obi", "Mrs Eze",
                 "Mrs Kobo", "Jon's Dad", "Bench", "Nocontact", "Alpha School", "office@alpha.example",
                 f"{SCHOOL_CODE}-{YEAR}-"]
beta_secrets = ["Beta Lab Fee", "Beta Payer", "Betaman", "Beta College", "office@beta.example", f"{BETA_CODE}-{YEAR}-"]
beta_pages = {"the dashboard": op_beta.text(FIN_URL), "the fee catalogue": op_beta.text(ITEMS),
              "the payment form": op_beta.text(f"{FIN_URL}/payments/new"),
              "a receipt": op_beta.text(f"{FIN_URL}/receipts/{PBETA}"),
              "a student account": op_beta.text(f"{FIN_URL}/students/{CHIKE}/account?session_id={CURRENT_B}"),
              "the allocate page": op_beta.text(f"{FIN_URL}/payments/{PBETA}/allocate"),
              "the parent's dashboard": mr_beta.text("/parent/dashboard"),
              "the parent's fee page": mr_beta.text(f"/parent/children/{CHIKE}/finance")}
leaks = [(label, s) for label, html in beta_pages.items() for s in alpha_secrets if s in html]
check("none of Alpha's fees, students, payers, parents, receipts, prefix or address ever appears in Beta's finance pages",
      not leaks, str(leaks[:3]))
alpha_pages = {"the dashboard": manager.text(FIN_URL), "the fee catalogue": op_alpha.text(ITEMS),
               "the payment form": op_alpha.text(f"{FIN_URL}/payments/new"),
               "a student account": op_alpha.text(f"{FIN_URL}/students/{IVY}/account?session_id={CURRENT}"),
               "the parent's dashboard": mum_obi.text("/parent/dashboard"),
               "the parent's fee page": mum_obi.text(f"/parent/children/{ADA}/finance")}
leaks = [(label, s) for label, html in alpha_pages.items() for s in beta_secrets if s in html]
check("…and none of Beta's ever appears in Alpha's", not leaks, str(leaks[:3]))
check("Beta's fee item is not offered to Alpha's students, nor Alpha's to Beta's",
      f'data-fee-item-id="{LAB}"' in op_beta.text(ITEMS) and "Beta Lab Fee" not in op_alpha.text(ITEMS)
      and "Tuition (JSS 1)" not in op_beta.text(ITEMS))
beta_last_payment = beta.one("SELECT max(id) FROM finance_payments")
beta_last_student = beta.one("SELECT max(id) FROM students")
check("an id from Alpha's data that does not exist in Beta is a 404 at Beta, for staff and for parents",
      PA1 > beta_last_payment and KEMI > beta_last_student
      and op_beta.get(f"{FIN_URL}/receipts/{PA1}").status_code == 404
      and op_beta.get(f"{FIN_URL}/receipts/{PA1}/pdf").status_code == 404
      and op_beta.get(f"{FIN_URL}/students/{KEMI}/account").status_code == 404
      and mr_beta.get(f"/parent/children/{KEMI}/finance").status_code == 404
      and mr_beta.get(f"/parent/children/{CHIKE}/receipts/{PA1}/pdf").status_code == 404)
alpha_family = Person(BETA, "/login")
r = alpha_family.post("/login", {"username": "mrs.obi", "password": "a-parent-password-1"})
check("Alpha's parent cannot sign in at Beta with Alpha's credentials",
      "/parent" not in r.headers.get("Location", "") and alpha_family.get("/parent/dashboard").status_code == 302)

# ================================================================ summary
print()
failed = [x for x in results if not x[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
