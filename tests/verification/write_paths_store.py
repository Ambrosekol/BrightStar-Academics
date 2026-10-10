"""The school store, proven end to end on PostgreSQL: items with pictures, stock that can never be oversold, payments
taken at the office and online (Paystack faked), purchases handed over with a text to the parent, cancelling, who may
see what, and the parents' Store. Also the paged activity and notice logs, the setup checklist swapped in place, and
the static-file caching and compression. Run:  python tests/verification/write_paths_store.py
"""
import atexit
import io
import logging
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_store_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('store')
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
from werkzeug.security import generate_password_hash  # noqa: E402

import app as A  # noqa: E402
from control_plane import provisioning as pv  # noqa: E402
from control_plane.context import tenant_context  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import dispose_engines, engine_for  # noqa: E402
from models import Admin, AdminScope, AdminType, AdminTypePermission, Permission  # noqa: E402

results = []
PL = "http://platform.test"
ALPHA, BETA = "http://alpha.portal.test", "http://beta.portal.test"


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)


class Errors(logging.Handler):
    def __init__(self):
        super().__init__()
        self.seen = []

    def emit(self, record):
        if record.exc_info and record.exc_info[1] is not None:
            self.seen.append(record.exc_info[1])


errors = Errors()
A.app.logger.addHandler(errors)
A.app.logger.propagate = False
PROBLEMS = []


def csrf_from(body):
    found = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', body)
    return found.group(1) if found else ""


class Person:
    count = [0]

    def __init__(self, base, form_page="/admin/password", label=""):
        self.base, self.form_page, self.client, self.label = base, form_page, A.app.test_client(), label or base
        Person.count[0] += 1
        self.addr = f"10.91.{Person.count[0]}.1"

    def _note(self, r, method, path):
        if r.status_code >= 500:
            PROBLEMS.append(f"{self.label}: {method} {path} answered {r.status_code}")
        elif r.mimetype == "text/html" and "Traceback (most recent call last)" in r.get_data(as_text=True)[:200000]:
            PROBLEMS.append(f"{self.label}: {method} {path} showed a traceback")
        return r

    def get(self, path, **kw):
        return self._note(self.client.get(path, base_url=self.base, environ_base={"REMOTE_ADDR": self.addr}, **kw), "GET", path)

    def text(self, path):
        return self.get(path).get_data(as_text=True)

    def post(self, path, data=None, page=None, token=True):
        data = dict(data or {})
        if token is True:
            data["_csrf_token"] = csrf_from(self.text(page or self.form_page))
        elif token:
            data["_csrf_token"] = token
        r = self.client.post(path, data=data, base_url=self.base, environ_base={"REMOTE_ADDR": self.addr})
        return self._note(r, "POST", path)

    def said(self):
        with self.client.session_transaction(base_url=self.base) as sess:
            found = list(sess.pop("_flashes", []))
        return " | ".join(text for _, text in found).lower()


def info_for(slug):
    with platform_session() as s:
        return to_info(get_tenant(s, slug))


class School:
    def __init__(self, code, base):
        self.code, self.base, self.info = code, base, info_for(code)

    def sql(self, statement, **params):
        with engine_for(self.info).begin() as conn:
            result = conn.execute(sa.text(statement), params)
            return result.fetchall() if result.returns_rows else None

    def one(self, statement, **params):
        rows = self.sql(statement, **params)
        return rows[0][0] if rows else None

    def run(self, fn):
        with A.app.app_context(), tenant_context(self.info):
            return fn()


pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
console = A.app.test_client()
console.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                      "_csrf_token": csrf_from(console.get("/platform/login", base_url=PL).get_data(as_text=True))}, base_url=PL)
for code, name in (("alpha", "Alpha School"), ("beta", "Beta College")):
    form = {"_csrf_token": csrf_from(console.get("/platform/schools/new", base_url=PL).get_data(as_text=True)), "name": name, "code": code}
    console.post("/platform/schools/new", data=form, base_url=PL, content_type="multipart/form-data")
alpha, beta = School("alpha", ALPHA), School("beta", BETA)


def operator_in(school):
    page = console.get(f"/platform/schools/{school.code}", base_url=PL).get_data(as_text=True)
    r = console.post(f"/platform/schools/{school.code}/enter", data={"_csrf_token": csrf_from(page)}, base_url=PL)
    person = Person(school.base, label=f"{school.code} School Admin")
    person.get(r.headers["Location"][len(school.base):])
    person.get("/admin/workspace/school")
    return person


op, op_beta = operator_in(alpha), operator_in(beta)
NOW = "2026-09-01T09:00:00+00:00"
PASSWORD = "Fixture-password-9"
HASH = generate_password_hash(PASSWORD, method="pbkdf2:sha256:1000")
J1 = alpha.one("SELECT id FROM school_classes WHERE name = 'JSS 1'")
S1 = alpha.one("SELECT id FROM school_classes WHERE name = 'SSS 1'")
REGISTER = "/admin/school/students?class=JSS+1"
ARCHIVE = "/admin/school/students/archive"
RESTORE = "/admin/school/students/archived/restore"
ARCHIVED_LIST = "/admin/school/students/archived"


def orm(school, fn):
    def go():
        out = fn()
        A.db.session.commit()
        return out
    return school.run(go)


def mk_staff(school, username, display, role_name, scope_class=None):
    def go():
        role = A.db.session.scalars(sa.select(AdminType).where(AdminType.name == role_name)).first()
        admin = Admin(username=username, display_name=display, password_hash=HASH, admin_type_id=role.id, active=1,
                      password_must_change=0, created_at=NOW)
        A.db.session.add(admin)
        A.db.session.flush()
        if scope_class:
            A.db.session.add(AdminScope(admin_id=admin.id, scope_type="class", scope_value=scope_class, created_at=NOW))
        return admin.id
    admin_id = orm(school, go)
    person = Person(school.base, label=f"{school.code} {username}")
    person.post("/login", {"username": username, "password": PASSWORD}, page="/login")
    person.get("/admin/workspace/school")
    return person, admin_id


def mk_role(school, name, codes):
    def go():
        role = AdminType(name=name, description="Only what is needed.", is_system=0, active=1, created_at=NOW)
        A.db.session.add(role)
        A.db.session.flush()
        for code in codes:
            permission = A.db.session.scalars(sa.select(Permission).where(Permission.code == code)).first()
            A.db.session.add(AdminTypePermission(admin_type_id=role.id, permission_id=permission.id, granted_at=NOW))
    orm(school, go)


mk_role(alpha, "Register Keeper", ["school.view", "school.students.view", "school.students.delete"])
mk_role(alpha, "Nothing Special", ["school.view", "school.students.view"])
keeper, _ = mk_staff(alpha, "keeper", "Kemi Keeper", "Register Keeper", scope_class="SSS 1")
nobody, _ = mk_staff(alpha, "nobody", "Nora Nothing", "Nothing Special")


def make_student(first, last, class_id):
    op.post("/admin/school/students/new", {
        "first_name": first, "last_name": last, "gender": "Female", "class_id": str(class_id),
        "state_of_origin": "Lagos", "blood_group": "", "genotype": "",
    })
    sid = alpha.one("SELECT id FROM students WHERE first_name = :f AND last_name = :l", f=first, l=last)
    no = alpha.one("SELECT admission_no FROM students WHERE id = :i", i=sid)
    # A known sign-in for the student, so the test can prove the login works before and not after.
    alpha.sql("UPDATE students SET login_username = :u, login_password_hash = :h, account_active = 1, "
              "password_must_change = 0 WHERE id = :i", u=no.upper(), h=HASH, i=sid)
    return sid, no





# ================================================================ set-up: students, a parent, the store's staff
from core import payments as PAY  # noqa: E402
import core.notifications as NOTIF  # noqa: E402

ADA, ADA_NO = make_student("Ada", "Store", J1)
BAYO, BAYO_NO = make_student("Bayo", "Store", J1)
CHI, CHI_NO = make_student("Chi", "Other", S1)

mk_role(alpha, "Store Looker", ["school.view", "store.view"])
keeper_s, KEEPER = mk_staff(alpha, "storekeeper", "Sam Storekeeper", "Store Keeper")
looker, _ = mk_staff(alpha, "looker", "Lola Looker", "Store Looker")


def mk_parent(username, children, phone="08031234567"):
    pid = alpha.one("INSERT INTO parent_accounts (username, display_name, password_hash, active, password_must_change, phone, created_at) "
                    "VALUES (:u, :d, :h, 1, 0, :p, :n) RETURNING id", u=username, d=username.title(), h=HASH, p=phone, n=NOW)
    for sid in children:
        alpha.sql("INSERT INTO parent_student_links (parent_id, student_id, relationship, active, created_at) VALUES (:p, :s, 'Mother', 1, :n)",
                  p=pid, s=sid, n=NOW)
    person = Person(ALPHA, label=username)
    person.post("/login", {"username": username, "password": PASSWORD}, page="/login")
    return person, pid


mum, MUM = mk_parent("mum.store", [ADA, BAYO])
other_mum, _ = mk_parent("other.mum", [CHI], phone="08039999999")

# ================================================================ who sees the store
check("a Store Keeper opens Inventory, Purchase claims and Record a payment",
      all(keeper_s.get(u).status_code == 200 for u in ("/admin/store", "/admin/store/claims", "/admin/store/sell")))
menu = keeper_s.text("/admin/school")
check("…and the menu has a Store group with all three", all(t in menu for t in (">Store<", "Inventory", "Purchase claims", "Record a payment")))
check("someone with View the store opens Inventory and Purchase claims, but not Record a payment",
      looker.get("/admin/store").status_code == 200 and looker.get("/admin/store/claims").status_code == 200
      and looker.get("/admin/store/sell").status_code == 403)
# (What's new mentions the pages by name, so the menu entry itself is what is looked for.)
check("…and their menu does not offer Record a payment", "<span>Record a payment" not in looker.text("/admin/store")
      and "<span>Purchase claims" in looker.text("/admin/store"))
check("someone without either sees no Store and is refused it", nobody.get("/admin/store").status_code == 403
      and "<span>Purchase claims" not in nobody.text("/admin/settings"))
check("a parent cannot open the staff store pages", mum.get("/admin/store").status_code in (302, 403))
check("staff are sent away from the parents' Store", op.get("/parent/store").status_code == 302)


# ================================================================ items with pictures
def png():
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (60, 40), (20, 80, 140)).save(b, "PNG")
    b.seek(0)
    return b


def new_item(person, name, price, stock, pictures=0, category="Uniform"):
    data = {"name": name, "category": category, "price": str(price), "stock": str(stock), "description": "Size 8",
            "_csrf_token": csrf_from(person.text("/admin/store/items/new"))}
    for n in range(1, pictures + 1):
        data[f"photo_{n}"] = (png(), f"p{n}.png")
    return person.client.post("/admin/store/items/new", data=data, base_url=ALPHA, environ_base={"REMOTE_ADDR": person.addr},
                              content_type="multipart/form-data")


r = new_item(keeper_s, "School sweater", 8500, 5, pictures=2)
SWEATER = alpha.one("SELECT id FROM store_items WHERE name = 'School sweater'")
row = alpha.sql("SELECT price, stock, photo_1, photo_2, photo_3 FROM store_items WHERE id = :i", i=SWEATER)[0]
check("an item is added with its price, stock and two pictures", r.status_code == 302 and row[0] == 8500 and row[1] == 5
      and row[2] and row[3] and not row[4], str(row))
check("its pictures are stored in the store folder", row[2].startswith("uploads/store/") and row[3].startswith("uploads/store/"))
r = new_item(keeper_s, "Bad price", "nothing", 3)
check("an item with no price is refused, with a reason, and nothing is saved",
      r.status_code == 200 and "Enter a price" in r.get_data(as_text=True) and not alpha.one("SELECT id FROM store_items WHERE name = 'Bad price'"))
check("Record a payment is refused to someone who may only look", new_item(looker, "Looker item", 100, 1).status_code == 403)
inv = keeper_s.text("/admin/store")
check("Inventory shows the item, its price and its stock", "School sweater" in inv and "8,500.00" in inv and "5 left" in inv)
pic = "/static/" + row[2]
check("a parent may open a store picture", mum.get(pic).status_code == 200)
check("…and so may staff", op.get(pic).status_code == 200)
anon = Person(ALPHA, label="anon")
check("…but nobody signed out", anon.get(pic).status_code == 404)

# editing: remove the first picture, the second moves up
token = csrf_from(keeper_s.text(f"/admin/store/items/{SWEATER}/edit"))
second = row[3]
r = keeper_s.client.post(f"/admin/store/items/{SWEATER}/edit", data={"name": "School sweater", "category": "Uniform", "price": "9000",
                         "description": "Size 8", "remove_1": "1", "_csrf_token": token},
                         base_url=ALPHA, environ_base={"REMOTE_ADDR": keeper_s.addr}, content_type="multipart/form-data")
row = alpha.sql("SELECT price, stock, photo_1, photo_2 FROM store_items WHERE id = :i", i=SWEATER)[0]
check("editing changes the price, keeps the stock, and a removed first picture lets the second move up",
      r.status_code == 302 and row[0] == 9000 and row[1] == 5 and row[2] == second and row[3] is None, str(row))

# ================================================================ selling at the office
r = keeper_s.post("/admin/store/sell", {"item_id": str(SWEATER), "quantity": "2", "student_id": str(ADA), "method": "Cash"}, page="/admin/store/sell")
P1 = alpha.one("SELECT max(id) FROM store_purchases")
p1 = alpha.sql("SELECT quantity, amount, status, method, unit_price, item_name FROM store_purchases WHERE id = :i", i=P1)[0]
check("a cash sale is recorded for the student, at the item's price", r.status_code == 302 and p1[0] == 2 and p1[1] == 18000 and p1[2] == "paid"
      and p1[3] == "Cash" and p1[4] == 9000 and p1[5] == "School sweater", str(p1))
check("…and takes the stock from 5 to 3", alpha.one("SELECT stock FROM store_items WHERE id = :i", i=SWEATER) == 3)
check("…and the purchase opens next, with its STR receipt number", f"/admin/store/purchases/{P1}" in r.headers.get("Location", "")
      and f"STR-{P1:06d}" in keeper_s.text(f"/admin/store/purchases/{P1}"))
check("…and the parent is told in the portal", alpha.one("SELECT count(*) FROM school_notifications WHERE recipient_type = 'parent' AND recipient_id = :p AND category = 'store'", p=MUM) == 1)
r = keeper_s.post("/admin/store/sell", {"item_id": str(SWEATER), "quantity": "4", "student_id": str(BAYO), "method": "Cash"}, page="/admin/store/sell")
check("selling more than is left is refused, and the stock is untouched", r.status_code == 200 and "Only 3" in r.get_data(as_text=True)
      and alpha.one("SELECT stock FROM store_items WHERE id = :i", i=SWEATER) == 3)
r = keeper_s.post("/admin/store/sell", {"item_id": str(SWEATER), "quantity": "1", "student_id": str(BAYO), "method": "POS"}, page="/admin/store/sell")
check("a POS payment needs its reference", r.status_code == 200 and "reference" in r.get_data(as_text=True).lower()
      and alpha.one("SELECT stock FROM store_items WHERE id = :i", i=SWEATER) == 3)
r = keeper_s.post("/admin/store/sell", {"item_id": str(SWEATER), "quantity": "1", "student_id": str(BAYO), "method": "POS", "reference": "POS-7781"}, page="/admin/store/sell")
P2 = alpha.one("SELECT max(id) FROM store_purchases")
check("…and with it, it is recorded", r.status_code == 302 and alpha.one("SELECT reference FROM store_purchases WHERE id = :i", i=P2) == "POS-7781")
# the last two, raced: two sales of 2 when 2 are left: only one may succeed
before = alpha.one("SELECT stock FROM store_items WHERE id = :i", i=SWEATER)
for who in (ADA, BAYO):
    keeper_s.post("/admin/store/sell", {"item_id": str(SWEATER), "quantity": str(before), "student_id": str(who), "method": "Cash"}, page="/admin/store/sell")
check("the last ones cannot be sold twice", alpha.one("SELECT stock FROM store_items WHERE id = :i", i=SWEATER) == 0
      and alpha.one("SELECT count(*) FROM store_purchases WHERE item_id = :i AND quantity = :q AND id > :p", i=SWEATER, q=before, p=P2) == 1)
check("a sold-out item shows as sold out", "Sold out" in keeper_s.text("/admin/store"))
r = keeper_s.post(f"/admin/store/items/{SWEATER}/restock", {"change": "10"}, page="/admin/store")
check("adding stock puts it back on the shelf", r.status_code == 302 and alpha.one("SELECT stock FROM store_items WHERE id = :i", i=SWEATER) == 10)
keeper_s.post(f"/admin/store/items/{SWEATER}/restock", {"change": "-50"}, page="/admin/store")
check("a correction never takes the stock below nought", alpha.one("SELECT stock FROM store_items WHERE id = :i", i=SWEATER) == 0)
keeper_s.post(f"/admin/store/items/{SWEATER}/restock", {"change": "6"}, page="/admin/store")

# ================================================================ handing over, with a text
SENT = []
NOTIF.sms_settings = lambda: type("S", (), {"source": "school"})()
NOTIF.send_sms = lambda settings, number, text: (SENT.append((number, text)) or (True, None))
claims = keeper_s.text("/admin/store/claims")
check("Purchase claims lists what is to be collected, with Hand over", f"STR-{P1:06d}" in claims and "Hand over" in claims)
r = keeper_s.post(f"/admin/store/purchases/{P1}/claim", {"claimed_by_type": "parent", "claimed_by_name": "Mrs Store"}, page=f"/admin/store/purchases/{P1}")
p1 = alpha.sql("SELECT status, claimed_by_type, claimed_by_name, claimed_marked_by, claimed_at FROM store_purchases WHERE id = :i", i=P1)[0]
check("marking it collected by a parent records who, when and who handed it over",
      r.status_code == 302 and p1[0] == "claimed" and p1[1] == "parent" and p1[2] == "Mrs Store" and p1[3] == KEEPER and p1[4], str(p1))
check("…and texts the parent, saying what was collected and by whom",
      any("School sweater" in t and "Mrs Store" in t for _, t in SENT), str(SENT))
check("…and the notice log records the text", alpha.one("SELECT count(*) FROM notification_delivery_logs WHERE kind = 'store_claimed' AND status = 'sent'") >= 1)
r = keeper_s.post(f"/admin/store/purchases/{P1}/claim", {"claimed_by_type": "student"}, page=f"/admin/store/purchases/{P1}")
check("a purchase cannot be handed over twice", alpha.one("SELECT claimed_by_name FROM store_purchases WHERE id = :i", i=P1) == "Mrs Store")
SENT.clear()
keeper_s.post(f"/admin/store/purchases/{P2}/claim", {"claimed_by_type": "student"}, page=f"/admin/store/purchases/{P2}")
check("collected by the student, the student's own name is recorded", alpha.one("SELECT claimed_by_name FROM store_purchases WHERE id = :i", i=P2) == "Bayo Store"
      and any("the student" in t for _, t in SENT))
r = keeper_s.post(f"/admin/store/purchases/{P2}/claim", {"claimed_by_type": "parent", "claimed_by_name": ""}, page=f"/admin/store/purchases/{P2}")
check("someone who may only look cannot hand anything over", looker.post(f"/admin/store/purchases/{P1}/claim", {"claimed_by_type": "student"},
                                                                       page=f"/admin/store/purchases/{P1}").status_code == 403)

# cancelling
P3 = alpha.one("SELECT max(id) FROM store_purchases WHERE status = 'paid'")
q3 = alpha.one("SELECT quantity FROM store_purchases WHERE id = :i", i=P3)
stock_before = alpha.one("SELECT stock FROM store_items WHERE id = :i", i=SWEATER)
keeper_s.post(f"/admin/store/purchases/{P3}/cancel", {"reason": ""}, page=f"/admin/store/purchases/{P3}")
check("cancelling needs a reason", alpha.one("SELECT status FROM store_purchases WHERE id = :i", i=P3) == "paid")
keeper_s.post(f"/admin/store/purchases/{P3}/cancel", {"reason": "recorded twice"}, page=f"/admin/store/purchases/{P3}")
check("a cancelled purchase puts what it took back in stock", alpha.one("SELECT status FROM store_purchases WHERE id = :i", i=P3) == "cancelled"
      and alpha.one("SELECT stock FROM store_items WHERE id = :i", i=SWEATER) == stock_before + q3)
check("a collected purchase cannot be cancelled", keeper_s.post(f"/admin/store/purchases/{P1}/cancel", {"reason": "x"}, page=f"/admin/store/purchases/{P1}")
      and alpha.one("SELECT status FROM store_purchases WHERE id = :i", i=P1) == "claimed")

# hiding an item takes it out of the parents' Store, without losing it
new_item(keeper_s, "Old badge", 500, 4)
BADGE = alpha.one("SELECT id FROM store_items WHERE name = 'Old badge'")
check("a new item shows in the parents' Store", "Old badge" in mum.text("/parent/store"))
keeper_s.post(f"/admin/store/items/{BADGE}/toggle", {}, page="/admin/store")
check("hidden, it leaves the parents' Store but stays in the inventory under Hidden",
      "Old badge" not in mum.text("/parent/store") and "Old badge" in keeper_s.text("/admin/store?show=hidden"))
keeper_s.post(f"/admin/store/items/{BADGE}/toggle", {}, page="/admin/store?show=hidden")
check("…and Show again brings it back", "Old badge" in mum.text("/parent/store"))
check("someone who may only look cannot hide an item", looker.post(f"/admin/store/items/{BADGE}/toggle", {}, page="/admin/store").status_code == 403)

# ================================================================ the parents' Store
page = mum.text("/parent/store")
check("a parent's Store shows the items with their price", "School sweater" in page and "9,000.00" in page)
check("…and their own purchases, with collected and who", "Collected" in page and "Mrs Store" in page)
check("…but not another family's purchases", "for Chi " not in page and "for Ada " in page)
check("…and the parent portal's menu has Store", 'href="/parent/store"' in page)
check("without online payment, the Store says to pay at the office, and has no Pay button",
      "Online payment is not available" in page and "data-buy data-price" not in page)
r = mum.post("/parent/store/buy", {"item_id": str(SWEATER), f"qty_{ADA}": "1"}, page="/parent/store")
check("…and a purchase cannot be started", alpha.one("SELECT count(*) FROM finance_online_payments") == 0)

# ---- online, Paystack faked
STARTED, VERIFY = [], {}
PAY.payment_settings = lambda: PAY.PaystackSettings(public_key="pk_test_x", secret_key="sk_test_x")
PAY.initialize_transaction = lambda settings, email, amount, reference, callback: (STARTED.append((amount, reference, callback)) or "https://checkout.paystack.test/abc")
PAY.verify_transaction = lambda settings, reference: VERIFY.get(reference, {"status": "failed", "gateway_response": "Declined", "amount": 0})
page = mum.text("/parent/store")
check("with online payment, the Store offers to pay for each child", "data-buy data-price" in page and f"qty_{ADA}" in page and f"qty_{BAYO}" in page)
r = mum.post("/parent/store/buy", {"item_id": str(SWEATER), f"qty_{ADA}": "1", f"qty_{BAYO}": "2"}, page="/parent/store")
check("buying for two children starts one Paystack checkout for the whole amount",
      r.status_code == 302 and r.headers["Location"].startswith("https://checkout.paystack.test") and STARTED and STARTED[-1][0] == 27000)
REF = STARTED[-1][1]
check("…and nothing is taken from the stock until Paystack says it was paid", alpha.one("SELECT stock FROM store_items WHERE id = :i", i=SWEATER) == stock_before + q3)
r = mum.post("/parent/store/buy", {"item_id": str(SWEATER), f"qty_{CHI}": "1"}, page="/parent/store")
check("a parent cannot buy for a child who is not theirs", r.status_code == 400)
r = mum.post("/parent/store/buy", {"item_id": str(SWEATER), f"qty_{ADA}": "500"}, page="/parent/store")
check("…nor more than is in stock", alpha.one("SELECT count(*) FROM finance_online_payments") == 1)
VERIFY[REF] = {"status": "success", "amount": 2700000, "id": 99}
stock0 = alpha.one("SELECT stock FROM store_items WHERE id = :i", i=SWEATER)
r = mum.get(f"/parent/store/pay/callback?reference={REF}")
made = alpha.sql("SELECT student_id, quantity, method, status, parent_id FROM store_purchases WHERE reference = :r ORDER BY student_id", r=REF)
check("once Paystack confirms, each child gets their purchase, paid online, waiting to be collected",
      r.status_code == 302 and sorted((m[0], m[1]) for m in made) == sorted([(ADA, 1), (BAYO, 2)])
      and all(m[2] == "Paystack" and m[3] == "paid" and m[4] == MUM for m in made), str(made))
check("…and the stock goes down by three", alpha.one("SELECT stock FROM store_items WHERE id = :i", i=SWEATER) == stock0 - 3)
check("…and no fee payment is made from it", alpha.one("SELECT count(*) FROM finance_payments WHERE reference = :r", r=REF) == 0)
mum.get(f"/parent/store/pay/callback?reference={REF}")
check("a second callback for the same payment changes nothing", alpha.one("SELECT count(*) FROM store_purchases WHERE reference = :r", r=REF) == 2)
check("the online purchases show under Purchase claims", "Online" in keeper_s.text("/admin/store/claims"))
# sold out between checkout and payment: still recorded, marked short
alpha.sql("UPDATE store_items SET stock = 1 WHERE id = :i", i=SWEATER)
mum.post("/parent/store/buy", {"item_id": str(SWEATER), f"qty_{ADA}": "1"}, page="/parent/store")
REF2 = STARTED[-1][1]
alpha.sql("UPDATE store_items SET stock = 0 WHERE id = :i", i=SWEATER)
VERIFY[REF2] = {"status": "success", "amount": 900000, "id": 100}
mum.get(f"/parent/store/pay/callback?reference={REF2}")
check("a payment that arrives after the last one has gone is still recorded, marked out of stock",
      alpha.one("SELECT short FROM store_purchases WHERE reference = :r", r=REF2) == 1 and alpha.one("SELECT stock FROM store_items WHERE id = :i", i=SWEATER) == 0)
check("…and Purchase claims counts it", "Out of stock" in keeper_s.text("/admin/store/claims"))
check("other parents see the item, but not this family's purchases", "School sweater" in other_mum.text("/parent/store")
      and "Mrs Store" not in other_mum.text("/parent/store"))

# ================================================================ the logs, paged; the checklist in place
for i in range(60):
    alpha.sql("INSERT INTO audit_logs (username_snapshot, action, module, success, created_at) VALUES ('robot', :a, 'store', 1, :t)",
              a=f"probe_{i}", t=f"2026-09-02T10:{i:02d}:00")
logs = op.text("/admin/administration/audit-logs")
check("the activity log shows fifty at a time, with the notices tab beside it", logs.count('class="lg-row') == 50 and "Notices to parents" in logs and "Older" in logs)
check("…and its second page holds the rest", 0 < op.text("/admin/administration/audit-logs?page=2").count('class="lg-row') <= 50)
check("…and searching runs over the whole log", op.text("/admin/administration/audit-logs?q=probe_3").count('class="lg-row') == 11)
notices = op.text("/admin/administration/audit-logs/notices")
check("the notices tab lists the texts sent to parents", "Store purchase collected" in notices and "Notices to parents" in notices)
pop = op.client.get(f"/admin/school/notice-log?student_id={ADA}&modal=1", base_url=ALPHA, headers={"X-Fragment": "1", "X-Modal": "1"},
                    environ_base={"REMOTE_ADDR": op.addr}).get_data(as_text=True)
check("a student's notice log answers as a pop-up, with that student's texts", "Ada Store" in pop and "Store purchase collected" in pop and "<html" not in pop)
box = op.client.post("/admin/school/onboarding/dismiss", data={"_csrf_token": csrf_from(op.text("/admin/school"))}, base_url=ALPHA,
                     headers={"X-Setup-Box": "1"}, environ_base={"REMOTE_ADDR": op.addr})
check("hiding the setup checklist from the Overview answers with just the checklist's box", box.status_code == 200
      and "Show setup checklist" in box.get_data(as_text=True) and "<html" not in box.get_data(as_text=True))
box = op.client.post("/admin/school/onboarding/show", data={"_csrf_token": csrf_from(op.text("/admin/school"))}, base_url=ALPHA,
                     headers={"X-Setup-Box": "1"}, environ_base={"REMOTE_ADDR": op.addr})
check("…and showing it again answers with the open checklist", box.status_code == 200 and "Hide this checklist" in box.get_data(as_text=True)
      and " open>" in box.get_data(as_text=True))
check("without that header, hiding it still reloads the Overview as before", op.post("/admin/school/onboarding/dismiss", {}, page="/admin/school").status_code == 302)

# ================================================================ faster pages
home = op.text("/admin/school")
css_url = re.search(r'href="(/static/school-ui\.css\?[^"]+)"', home).group(1)
check("the portal's own files are addressed by version", "m=" in css_url)
r = op.get(css_url.replace("&amp;", "&"))
check("…and such an address is kept by the browser for a year", "immutable" in r.headers.get("Cache-Control", "") and "max-age=31536000" in r.headers.get("Cache-Control", ""))
r = op.get("/static/school-ui.css")
check("…while an address without its version is kept only briefly", r.headers.get("Cache-Control") == "public, max-age=300")
r = op.client.get("/admin/school", base_url=ALPHA, headers={"Accept-Encoding": "gzip"}, environ_base={"REMOTE_ADDR": op.addr})
import gzip as _gzip  # noqa: E402
check("a page is sent compressed to a browser that accepts it, and decompresses to the same page",
      r.headers.get("Content-Encoding") == "gzip" and "Overview" in _gzip.decompress(r.get_data()).decode())
r = op.client.get(css_url.replace("&amp;", "&"), base_url=ALPHA, headers={"Accept-Encoding": "gzip"}, environ_base={"REMOTE_ADDR": op.addr})
check("…and so is a stylesheet", r.headers.get("Content-Encoding") == "gzip" and b"--brand" in _gzip.decompress(r.get_data()))
check("…but never to a client that did not ask", op.get("/admin/school").headers.get("Content-Encoding") is None)

# ================================================================ an installable app, in the school's own logo
import json as _json  # noqa: E402
from PIL import Image as _Image  # noqa: E402
r = anon.get("/manifest.webmanifest")
m = _json.loads(r.get_data(as_text=True))
check("the school's app manifest is open to anyone, in the school's own name", r.status_code == 200
      and r.mimetype == "application/manifest+json" and m["name"] == "Alpha School" and m["display"] == "standalone" and m["start_url"].startswith("/"))
check("…with its icons at 192 and 512, and a maskable one", {i["sizes"] for i in m["icons"]} == {"192x192", "512x512"}
      and any(i.get("purpose") == "maskable" for i in m["icons"]))
icon = anon.get(m["icons"][0]["src"])
check("the app icon is a 192 by 192 picture", icon.status_code == 200 and icon.mimetype == "image/png"
      and _Image.open(io.BytesIO(icon.get_data())).size == (192, 192))
check("…and the browser's own /favicon.ico is the school's icon too", anon.get("/favicon.ico").mimetype == "image/png")
check("an icon size nobody uses is not made", anon.get("/pwa/icon-999.png").status_code == 404)
sw = anon.get("/sw.js")
body = sw.get_data(as_text=True)
check("the service worker is served for the whole site", sw.status_code == 200 and sw.headers.get("Service-Worker-Allowed") == "/"
      and "javascript" in sw.mimetype)
check("…and keeps only the portal's own files: never an upload, and pages always from the network",
      "/static/uploads/" in body and "request.mode === 'navigate'" in body and "fetch(request).catch" in body)
off = anon.get("/offline")
check("the offline page needs no sign-in and carries the school's name", off.status_code == 200 and "Alpha School" in off.get_data(as_text=True))
for who, url in ((op, "/admin/school"), (mum, "/parent/store"), (anon, "/login")):
    page = who.text(url)
    check(f"{url} names the manifest, the school's tab icon and its home-screen icon, and registers the app",
          'rel="manifest"' in page and 'rel="icon"' in page and 'rel="apple-touch-icon"' in page and "serviceWorker" in page
          and "pwa-install.js" in page and 'data-name="Alpha School"' in page)
before = re.search(r'rel="icon" type="image/png" sizes="32x32" href="([^"]+)"', op.text("/admin/school")).group(1)
tok = csrf_from(op.text("/admin/school/branding"))
logo = io.BytesIO(); _Image.new("RGB", (300, 120), (200, 30, 30)).save(logo, "PNG"); logo.seek(0)
op.client.post("/admin/school/branding/save", data={"_csrf_token": tok, "logo": (logo, "logo.png")}, base_url=ALPHA,
               environ_base={"REMOTE_ADDR": op.addr}, content_type="multipart/form-data")
after = re.search(r'rel="icon" type="image/png" sizes="32x32" href="([^"]+)"', op.text("/admin/school")).group(1)
check("a new logo gives the icons a new address, so browsers show it at once", before != after)
red = _Image.open(io.BytesIO(anon.get(after).get_data())).convert("RGB").getpixel((16, 16))
check("…and the icon is the school's own logo", red[0] > 150 and red[1] < 90, str(red))

# ================================================================ the store on the Overview
home = op.text("/admin/school")
check("the School Admin's Overview has a Store panel with this month's sales and what waits to be collected",
      'id="ovStore"' in home and "Sales this month" in home and "To be collected" in home and "Best sellers" in home)
check("…and so does a Store Keeper's", 'id="ovStore"' in keeper_s.text("/admin/school"))
check("…but not the Overview of someone without store access", 'id="ovStore"' not in nobody.text("/admin/school"))
op.post("/admin/school/onboarding/show", {}, page="/admin/school")   # an earlier check left it hidden
check("the setup checklist has the store step, done now that the store has items",
      re.search(r'data-step="Open the school store" data-done="1"', op.text("/admin/school")) is not None)

# ================================================================ dashboards are where Back leads, never where it starts
check("the Overview has no Back button", 'class="ui-back"' not in op.text("/admin/school"))
check("…but another page has one", 'class="ui-back"' in op.text("/admin/store"))
check("…and the workspace chooser has none either", 'class="ui-back"' not in op.text("/admin/home"))
op.get("/admin/workspace/school")

# ================================================================ no traceback, no server error
check("no page in this run was a server error, and none showed a traceback", not PROBLEMS, "; ".join(PROBLEMS[:4]))
check("no server error was logged by any request in this run", not errors.seen, "; ".join(str(e).splitlines()[0][:100] for e in errors.seen[:3]))

dispose_engines()
passed = sum(1 for _, ok, _ in results if ok)
print(f"\n{passed}/{len(results)} checks passed")
sys.exit(0 if passed == len(results) else 1)
