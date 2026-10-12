"""Finance > New students to bill, proven end to end on PostgreSQL. In plain words:

A. Joining: a student registered by hand, one imported in bulk and one admitted from the entrance exam each land on
   the list, marked with how they came in, as "Not billed yet"; the menu and the Overview count them. A student
   created already archived does not, and students already in the school are not on it.
B. Billing: Bill opens Fee structure with the student chosen; once billed they move to "Waiting for payment" with
   what they owe, and the count goes down.
C. Paying: part of the bill applied keeps them waiting with less owed; money received but not applied is shown as
   such and does not count as paid; once the bill is paid in full they leave the list for Done, on their own.
D. By hand: taking students off needs a reason and finance.manage (a cashier cannot); they can be put back, and one
   who paid cannot be put back. Someone with no finance permission cannot open the list.
Run:  python tests/verification/write_paths_new_students.py
"""
import atexit
import io
import json
import logging
import os
import re
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_newstudents_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('newstudents')
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
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import engine_for  # noqa: E402

results = []
PROBLEMS = []
PL = "http://platform.test"
ALPHA = "http://alpha.portal.test"


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


def csrf_from(body):
    found = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', body)
    return found.group(1) if found else ""


class Person:
    count = [0]

    def __init__(self, base, label=""):
        self.base, self.client, self.label = base, A.app.test_client(), label or base
        Person.count[0] += 1
        self.addr = f"10.85.{Person.count[0]}.1"

    def _note(self, r, method, path):
        if r.status_code >= 500:
            PROBLEMS.append(f"{self.label}: {method} {path} answered {r.status_code}")
        return r

    def get(self, path, **kw):
        return self._note(self.client.get(path, base_url=self.base, environ_base={"REMOTE_ADDR": self.addr}, **kw), "GET", path)

    def text(self, path):
        return self.get(path).get_data(as_text=True)

    def post(self, path, data=None, page="/admin/password", files=None):
        data = dict(data or {})
        data["_csrf_token"] = csrf_from(self.text(page))
        for key, (filename, content) in (files or {}).items():
            data[key] = (io.BytesIO(content), filename)
        return self._note(self.client.post(path, data=data, base_url=self.base, environ_base={"REMOTE_ADDR": self.addr},
                                           content_type="multipart/form-data" if files else None), "POST", path)

    def said(self):
        with self.client.session_transaction(base_url=self.base) as sess:
            found = list(sess.pop("_flashes", []))
        return " | ".join(text for _, text in found).lower()


pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
console = A.app.test_client()
console.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                      "_csrf_token": csrf_from(console.get("/platform/login", base_url=PL).get_data(as_text=True))}, base_url=PL)
console.post("/platform/schools/new", data={"_csrf_token": csrf_from(console.get("/platform/schools/new", base_url=PL).get_data(as_text=True)),
                                            "name": "Alpha School", "code": "alpha"},
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


page = console.get("/platform/schools/alpha", base_url=PL).get_data(as_text=True)
r = console.post("/platform/schools/alpha/enter", data={"_csrf_token": csrf_from(page)}, base_url=PL)
op = Person(ALPHA, "School Admin")
op.get(r.headers["Location"][len(ALPHA):])
op.get("/admin/workspace/school")

PASSWORD = "Fixture-password-9"
HASH = generate_password_hash(PASSWORD, method="pbkdf2:sha256:1000")
cls = {name: one("SELECT id FROM school_classes WHERE name = :n", n=name) for name in ("JSS 1", "SSS 1")}
OTHER = one("SELECT id FROM school_classes WHERE active = 1 AND name NOT LIKE 'JSS%' AND name NOT LIKE 'SS%' ORDER BY level_order LIMIT 1")
SESSION = one("SELECT id FROM academic_sessions WHERE is_current = 1")
role = {name: one("SELECT id FROM admin_types WHERE name = :n", n=name) for name in ("Class Teacher", "Finance Manager", "Finance Records Officer")}


def new_student(first, class_name, department=""):
    op.post("/admin/school/students/new", {"first_name": first, "last_name": "Test", "gender": "Female",
                                           "class_id": str(cls[class_name]), "state_of_origin": "Lagos",
                                           "department": department}, page="/admin/school/students/new")
    return one("SELECT id FROM students WHERE first_name = :f", f=first)



PAGE = "/admin/finance/new-students"


def new_student(first, class_name="JSS 1"):
    op.post("/admin/school/students/new", {"first_name": first, "last_name": "Test", "gender": "Female",
                                           "class_id": str(cls[class_name]), "state_of_origin": "Lagos"},
            page="/admin/school/students/new")
    return one("SELECT id FROM students WHERE first_name = :f", f=first)


def listed(sid):
    """(source, cleared_at, cleared_reason) of the student's row, or None."""
    rows = sql("SELECT source, cleared_at, cleared_reason FROM finance_new_students WHERE student_id = :s", s=sid)
    return tuple(rows[0]) if rows else None


def view(person, which):
    return person.text(f"{PAGE}?view={which}")


def new_staff(username, roles):
    payload = {"username": username, "display_name": username.replace(".", " ").title(),
               "admin_type_ids": [str(role[r]) for r in roles], "duty_class": [""], "duty_subject": [""], "duty_department": [""]}
    op.post("/admin/administration/admins/new", payload, page="/admin/administration/admins/new")
    aid = one("SELECT id FROM admins WHERE username = :u", u=username)
    sql("UPDATE admins SET password_hash = :h, password_must_change = 0 WHERE id = :i", h=HASH, i=aid)
    person = Person(ALPHA, username)
    person.post("/login", {"username": username, "password": PASSWORD}, page="/login")
    person.get("/admin/workspace/school")
    return person


# ================================================================ A. joining
check("students already in the school are not on the list", not one("SELECT COUNT(*) FROM finance_new_students"))
ADA = new_student("Adaone")
check("a student registered by hand is on the list, as registered and not billed",
      listed(ADA) and listed(ADA)[0] == "registered" and listed(ADA)[1] is None)
CSV = ("first_name,middle_name,last_name,gender,class,guardian_name,guardian_email,guardian_phone\r\n"
       "Importa,Q,Test,Male,JSS 1,Mr Test,guardian@example.test,08010000001\r\n").encode("utf-8")
op.post("/admin/school/students/import", {}, page="/admin/school/students/import", files={"csv_file": ("new.csv", CSV)})
IMP = one("SELECT id FROM students WHERE first_name = 'Importa'")
check("a student imported in bulk is on the list, as imported", IMP and listed(IMP) and listed(IMP)[0] == "imported")

from control_plane.context import tenant_context  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
NOW = datetime.now(timezone.utc).isoformat()
with A.app.app_context(), tenant_context(INFO):
    from models import FinanceFeeItem, FinanceFeeItemClass, Student, StudentEnrolment, db
    school_id = one("SELECT id FROM schools LIMIT 1")
    st = Student(admission_no="ADM-NB-1", first_name="Admitted", last_name="Test", created_at=NOW, active=1, school_id=school_id)
    st._joined_by = "admitted"   # what the entrance admit sets
    gone = Student(admission_no="ADM-NB-2", first_name="Leaver", last_name="Test", created_at=NOW, active=1,
                   school_id=school_id, archived_at=NOW)
    db.session.add_all([st, gone])
    db.session.flush()
    db.session.add(StudentEnrolment(student_id=st.id, class_id=cls["JSS 1"], session_id=SESSION, enrolled_at=NOW, active=1))
    item = FinanceFeeItem(name="Tuition", category="School Fees", stage="Secondary", amount=50000.0, applicability="Full Session",
                          required=1, optional=0, active=1, created_at=NOW, updated_at=NOW)
    db.session.add(item)
    db.session.flush()
    db.session.add(FinanceFeeItemClass(fee_item_id=item.id, class_id=cls["JSS 1"], active=1, created_at=NOW))
    db.session.commit()
    ADM, GONE, FEE = st.id, gone.id, item.id
    db.session.remove()
check("a student admitted from the entrance exam is on the list, as admitted", listed(ADM) and listed(ADM)[0] == "admitted")
check("a student created already archived is not", not listed(GONE))
page = view(op, "unbilled")
check("the list shows all three as not billed, each with how they came in",
      all(n in page for n in ("Adaone", "Importa", "Admitted")) and "Imported" in page
      and "Admitted from the entrance exam" in page and "3 new students not billed yet" in page, page[:0])
home = op.text("/admin/school")
check("the menu and the Overview count them", "New students to bill" in home and "New, not billed" in home,
      f'menu={"New students to bill" in home} overview={"New, not billed" in home}')
check("Bill opens Fee structure with the student already chosen",
      f'<option value="{ADA}" selected>' in op.text(f"/admin/finance/fee-items?student_id={ADA}"))

# ================================================================ B. billing
op.post("/admin/finance/assessments/new", {"student_id": ADA, "session_id": SESSION, "fee_item_id": [FEE], "term": "Full Session",
                                           "due_date": "", "notes": ""}, page="/admin/finance/fee-items")
BILL = one("SELECT id FROM finance_fee_assessments WHERE student_id = :s", s=ADA)
op.said()   # the "billed" message names the student; read it so the list below is the list alone
check("once billed, the student moves to Waiting for payment with what they owe",
      BILL and "Adaone" not in view(op, "unbilled") and "Adaone" in view(op, "awaiting") and "₦50,000.00" in view(op, "awaiting"),
      f'bill={BILL} unbilled={"Adaone" in view(op, "unbilled")} awaiting={"Adaone" in view(op, "awaiting")} amount={"₦50,000.00" in view(op, "awaiting")} {op.said()}')
check("…and the count goes down", "2 new students not billed yet" in view(op, "unbilled"))


# ================================================================ C. paying
def pay(amount):
    op.post("/admin/finance/payments/new", {"student_id": ADA, "session_id": SESSION, "amount": amount, "category": "School Fees",
                                            "method": "Cash", "reference": "", "paid_at": "", "notes": "", "payer_name": "A Parent"},
            page="/admin/finance/payments/new")
    return one("SELECT MAX(id) FROM finance_payments WHERE student_id = :s", s=ADA)


def apply(payment_id, amount):
    op.post(f"/admin/finance/payments/{payment_id}/allocate", {"allocations": json.dumps({str(BILL): amount})},
            page=f"/admin/finance/payments/{payment_id}/allocate")


P1 = pay("20000")
apply(P1, 20000)
page = view(op, "awaiting")
check("part of the bill paid: still waiting, with less owed", "Adaone" in page and "₦30,000.00" in page and listed(ADA)[1] is None)
P2 = pay("30000")
page = view(op, "awaiting")
check("money received but not applied shows as such, and does not count as paid",
      "Adaone" in page and "received, not yet applied" in page and listed(ADA)[1] is None)
apply(P2, 30000)
page = view(op, "awaiting")
check("paid in full: the student leaves the list on their own", "Adaone" not in page and listed(ADA)[2] == "paid")
done = view(op, "cleared")
check("…and is kept under Done as paid in full", "Adaone" in done and "Paid in full" in done)

# ================================================================ D. by hand, and who may
cashier = new_staff("cash.ier", ["Finance Records Officer"])
check("a cashier (finance.record) can see the list", "Importa" in view(cashier, "unbilled"))
r = cashier.get(f"/admin/finance/students/{ADA}/account")
check("…but cannot open a student's whole fee account by its address (security audit M1)",
      r.status_code in (302, 403) and "₦50,000.00" not in r.get_data(as_text=True), str(r.status_code))
check("…while the bursar can", op.get(f"/admin/finance/students/{ADA}/account").status_code == 200)
cashier.post(f"{PAGE}/remove", {"student_id": [IMP], "note": "Scholarship"}, page=PAGE)
check("…but cannot take anyone off it", listed(IMP)[1] is None)
op.post(f"{PAGE}/remove", {"student_id": [IMP], "note": ""}, page=PAGE)
check("taking a student off needs a reason", listed(IMP)[1] is None and "say why" in op.said())
op.post(f"{PAGE}/remove", {"student_id": [IMP], "note": "Full scholarship"}, page=PAGE)
check("with a reason, the bursar takes them off", listed(IMP)[2] == "removed" and "Importa" not in view(op, "unbilled")
      and "Full scholarship" in view(op, "cleared"))
op.post(f"{PAGE}/{IMP}/restore", page=f"{PAGE}?view=cleared")
check("…and can put them back", listed(IMP)[1] is None and "Importa" in view(op, "unbilled"))
op.post(f"{PAGE}/{ADA}/restore", page=f"{PAGE}?view=cleared")
check("a student who paid cannot be put back", listed(ADA)[2] == "paid")
teacher = new_staff("class.teacher", ["Class Teacher"])
r = teacher.get(PAGE)
check("someone with no finance permission cannot open the list",
      r.status_code in (302, 403) and 'href="/admin/finance/new-students"' not in teacher.text("/admin/school"))

check("no page answered with a server error", not PROBLEMS, "; ".join(PROBLEMS[:5]))
check("nothing raised behind the scenes", not errors.seen, "; ".join(repr(e) for e in errors.seen[:3]))
failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
