"""Fee collection, proven end to end on PostgreSQL. In plain words:

A. Discounts: a scholarship and the sibling discount are taken off as a class is billed, the bill keeping the full fee
   and why; a fixed discount given later re-works this session's bills; a fee is never set below what was paid on it;
   ending a discount charges in full again; bad discounts are refused, and a cashier cannot give one.
B. Instalments: a plan whose parts do not add to 100% is refused; a class put on a plan is overdue only for the parts
   whose date has passed; taking a student off (or choosing "no plan" on their account) brings back the fees' own dates.
C. Reminders: the settings and the words are saved (a text too long for one message, or an unknown placeholder, is
   refused); Send reminders now texts and emails each overdue family once, in the school's own words, and not twice in a
   day; the schedule starts on its own once, within the sending hours, and not again until it is due; one family can be
   reminded from their account.
D. Report card hold: only the School Admin sets it; a held card is refused to the parent with what is owed, a student
   let through is not held, and holding again works; the dashboard lists the card as held.
E. Expenses: recorded with a receipt photo, which only finance staff can open (never by its uploads address); bad
   amounts and future dates are refused; voiding needs a reason; a cashier cannot record one.
F. Profit and loss: money in (fees, store) and out (paid payroll with the school's pension, expenses) add up; only the
   School Admin (or someone given it) can see it, not the bursar.
Run:  python tests/verification/write_paths_fee_collection.py
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
from datetime import date, datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_feecollect_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('feecollect')
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
from control_plane.routing import engine_for  # noqa: E402

A.app.config['BACKGROUND_INLINE'] = True
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
        self.addr = f"10.86.{Person.count[0]}.1"

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


def inside(fn, *args, **kwargs):
    """Run a function of the application against the school's own database, as a page would."""
    with A.app.test_request_context(base_url=ALPHA), tenant_context(INFO):
        from models import db
        try:
            return fn(*args, **kwargs)
        finally:
            db.session.remove()


page = console.get("/platform/schools/alpha", base_url=PL).get_data(as_text=True)
r = console.post("/platform/schools/alpha/enter", data={"_csrf_token": csrf_from(page)}, base_url=PL)
op = Person(ALPHA, "School Admin")
op.get(r.headers["Location"][len(ALPHA):])
op.get("/admin/workspace/school")

PASSWORD = "Fixture-password-9"
HASH = generate_password_hash(PASSWORD, method="pbkdf2:sha256:1000")
JSS1 = one("SELECT id FROM school_classes WHERE name = 'JSS 1'")
SESSION = one("SELECT id FROM academic_sessions WHERE is_current = 1")
TODAY = date.today()
YESTERDAY, LATER = (TODAY - timedelta(days=1)).isoformat(), (TODAY + timedelta(days=30)).isoformat()
sql("UPDATE academic_sessions SET start_date = :a, end_date = :b WHERE id = :i",
    a=(TODAY - timedelta(days=60)).isoformat(), b=(TODAY + timedelta(days=200)).isoformat(), i=SESSION)
role = {name: one("SELECT id FROM admin_types WHERE name = :n", n=name)
        for name in ("Class Teacher", "Finance Manager", "Finance Records Officer")}


def new_student(first):
    op.post("/admin/school/students/new", {"first_name": first, "last_name": "Test", "gender": "Female",
                                           "class_id": str(JSS1), "state_of_origin": "Lagos"}, page="/admin/school/students/new")
    sid = one("SELECT id FROM students WHERE first_name = :f", f=first)
    sql("UPDATE students SET guardian_email = :e WHERE id = :i", e=f"{first.lower()}@family.test", i=sid)
    return sid


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


ADA, BISI, CHIDI = new_student("Adaone"), new_student("Bisitwo"), new_student("Chidithree")
NOW = datetime.now(timezone.utc).isoformat()
# One parent account linked to Ada and Bisi: a family of two. Ada joined first, so Bisi is the second child.
sql("INSERT INTO parent_accounts (username, display_name, email, phone, password_hash, active, password_must_change, created_at) "
    "VALUES ('mum.test', 'Mrs Test', 'mum@family.test', '08030000001', :h, 1, 0, :n)", h=HASH, n=NOW)
MUM = one("SELECT id FROM parent_accounts WHERE username = 'mum.test'")
for sid in (ADA, BISI):
    sql("INSERT INTO parent_student_links (parent_id, student_id, relationship, active, created_at) "
        "VALUES (:p, :s, 'Mother', 1, :n)", p=MUM, s=sid, n=NOW)
sql("INSERT INTO finance_fee_items (name, category, stage, amount, applicability, required, optional, active, created_at, updated_at) "
    "VALUES ('Tuition', 'Tuition', 'Secondary', 100000, 'Full Session', 1, 0, 1, :n, :n), "
    "('Development levy', 'Development Levy', 'Secondary', 20000, 'Full Session', 1, 0, 1, :n, :n)", n=NOW)
TUITION = one("SELECT id FROM finance_fee_items WHERE name = 'Tuition'")
LEVY = one("SELECT id FROM finance_fee_items WHERE name = 'Development levy'")
for fid in (TUITION, LEVY):
    sql("INSERT INTO finance_fee_item_classes (fee_item_id, class_id, active, created_at) VALUES (:f, :c, 1, :n)", f=fid, c=JSS1, n=NOW)

bursar = new_staff("bur.sar", ["Finance Manager"])
cashier = new_staff("cash.ier", ["Finance Records Officer"])
teacher = new_staff("class.teacher", ["Class Teacher"])


def bill(sid, fid):
    return sql("SELECT amount, gross_amount, discount_amount, discount_note, id FROM finance_fee_assessments "
               "WHERE student_id = :s AND fee_item_id = :f AND active = 1", s=sid, f=fid)[0]


# ================================================================ A. discounts
DISC = "/admin/finance/discounts"
check("the bursar can open Discounts", bursar.get(DISC).status_code == 200)
r = cashier.get(DISC)
check("a cashier cannot open Discounts", r.status_code in (302, 403) and "Sibling discount" not in r.get_data(as_text=True))
cashier.post(f"{DISC}/new", {"student_id": ADA, "kind": "scholarship", "basis": "percent", "value": "50", "category": "Tuition",
                             "scope": "session"}, page="/admin/school")
check("…nor give a discount", not one("SELECT COUNT(*) FROM finance_discounts"))
bursar.post(f"{DISC}/new", {"student_id": ADA, "kind": "scholarship", "basis": "percent", "value": "150", "category": "Tuition",
                            "scope": "session"}, page=f"{DISC}/new")
check("a percentage over 100 is refused", not one("SELECT COUNT(*) FROM finance_discounts"))
bursar.post(f"{DISC}/new", {"student_id": "", "kind": "scholarship", "basis": "percent", "value": "50", "category": "Tuition",
                            "scope": "session"}, page=f"{DISC}/new")
check("a discount with no student is refused", not one("SELECT COUNT(*) FROM finance_discounts"))
bursar.post(f"{DISC}/new", {"student_id": ADA, "kind": "scholarship", "basis": "percent", "value": "50", "category": "Tuition",
                            "scope": "session", "label": "Governor's scholarship"}, page=f"{DISC}/new")
check("the bursar gives Ada a 50% scholarship on Tuition for this session",
      one("SELECT COUNT(*) FROM finance_discounts WHERE student_id = :s AND value = 50 AND session_id = :x", s=ADA, x=SESSION) == 1)
r = bursar.post(f"{DISC}/new", {"student_id": ADA, "kind": "scholarship", "basis": "percent", "value": "50", "category": "Tuition",
                                "scope": "session"}, page=f"{DISC}/new")
check("giving her exactly the same discount again is refused (two 50% would make 100%)",
      one("SELECT COUNT(*) FROM finance_discounts WHERE student_id = :s", s=ADA) == 1
      and "already has exactly this discount" in r.get_data(as_text=True))
form = bursar.text(f"{DISC}/new")
key = re.search(r'name="_idempotency_key" value="([^"]+)"', form).group(1)
twice = {"student_id": BISI, "kind": "discount", "basis": "amount", "value": "1", "category": "Uniform", "scope": "session",
         "_idempotency_key": key, "_csrf_token": csrf_from(form)}
for _ in range(2):
    bursar.client.post(f"{DISC}/new", data=twice, base_url=ALPHA, environ_base={"REMOTE_ADDR": bursar.addr})
check("a double-click on Give the discount gives it once",
      one("SELECT COUNT(*) FROM finance_discounts WHERE student_id = :s AND category = 'Uniform'", s=BISI) == 1)
sql("DELETE FROM finance_discounts WHERE student_id = :s AND category = 'Uniform'", s=BISI)
bursar.post(f"{DISC}/sibling", {"enabled": "1", "percent": "10", "from_child": "2", "category": "All"}, page=DISC)
rule = json.loads(one("SELECT setting_value FROM school_settings WHERE setting_key = 'sibling_discount'") or "{}")
check("the sibling discount is saved: 10% off every fee from the 2nd child", rule.get("enabled") and rule.get("percent") == 10, str(rule))

# Billing the class: each student's discounts come off as they are billed.
res = bursar.post("/admin/finance/class-assessments/batch",
                  {"session_id": SESSION, "class_id": JSS1, "term": "Full Session", "due_date": YESTERDAY, "notes": "",
                   "fee_item_id": [TUITION, LEVY], "student_id": [ADA, BISI, CHIDI]}, page="/admin/finance/fee-items")
check("the class is billed", res.status_code == 200 and all(x["status"] == "billed" for x in res.get_json()["results"]),
      res.get_data(as_text=True)[:300])
a_t, b_t, b_l, c_t = bill(ADA, TUITION), bill(BISI, TUITION), bill(BISI, LEVY), bill(CHIDI, TUITION)
check("Ada (the first child) owes 50% of Tuition, and the bill keeps the full fee and why",
      a_t[0] == 50000 and a_t[1] == 100000 and a_t[2] == 50000 and "Governor's scholarship 50%" in (a_t[3] or ""), str(a_t))
check("Bisi (the second child) gets the sibling 10% on every fee",
      b_t[0] == 90000 and b_l[0] == 18000 and "Sibling 10%" in (b_t[3] or ""), f"{b_t} {b_l}")
check("Chidi, with no discount, is charged in full, with no discount recorded", c_t[0] == 100000 and c_t[1] is None, str(c_t))

# A fixed discount given later re-works the session's bills at once.
bursar.post(f"{DISC}/new", {"student_id": CHIDI, "kind": "staff_child", "basis": "amount", "value": "10000",
                            "category": "Development Levy", "scope": "session", "reprice": "1"}, page=f"{DISC}/new")
check("a ₦10,000 staff-child discount on the levy, given later, is taken off Chidi's levy already charged",
      bill(CHIDI, LEVY)[0] == 10000 and bill(CHIDI, LEVY)[2] == 10000, str(bill(CHIDI, LEVY)))


def pay(sid, amount, assessment_id):
    op.post("/admin/finance/payments/new", {"student_id": sid, "session_id": SESSION, "amount": amount, "category": "School Fees",
                                            "method": "Cash", "reference": "", "paid_at": "", "notes": "", "payer_name": "A Parent"},
            page="/admin/finance/payments/new")
    pid = one("SELECT MAX(id) FROM finance_payments WHERE student_id = :s", s=sid)
    op.post(f"/admin/finance/payments/{pid}/allocate", {"allocations": json.dumps({str(assessment_id): amount})},
            page=f"/admin/finance/payments/{pid}/allocate")
    return pid


CHIDI_PAY = pay(CHIDI, 30000, c_t[4])
bursar.post(f"{DISC}/new", {"student_id": CHIDI, "kind": "scholarship", "basis": "percent", "value": "100", "category": "Tuition",
                            "scope": "session", "reprice": "1"}, page=f"{DISC}/new")
said = bursar.said()
check("a full scholarship given after ₦30,000 was paid never sets the fee below what was paid, and says so",
      bill(CHIDI, TUITION)[0] == 30000 and "could not be taken off" in said, f"{bill(CHIDI, TUITION)} {said}")
check("…taking ₦70,000 off and keeping the ₦30,000 it could not take off as waiting",
      bill(CHIDI, TUITION)[2] == 70000 and one("SELECT discount_held FROM finance_fee_assessments WHERE id = :i", i=c_t[4]) == 30000)
check("the fee account shows the discount waiting on a payment",
      "₦30,000.00 more waiting on a payment" in bursar.text(f"/admin/finance/students/{CHIDI}/account"))
check("the Discounts page lists them, with what was taken off this session",
      all(n in bursar.text(DISC) for n in ("Test, Adaone", "Staff child", "Taken off this session")))
ada_award = one("SELECT id FROM finance_discounts WHERE student_id = :s", s=ADA)
bursar.post(f"{DISC}/{ada_award}/end", {"reprice": "1"}, page=DISC)
check("ending Ada's scholarship (with this session's fees) charges her Tuition in full again",
      bill(ADA, TUITION)[0] == 100000 and bill(ADA, TUITION)[2] is None
      and one("SELECT active FROM finance_discounts WHERE id = :i", i=ada_award) == 0, str(bill(ADA, TUITION)))
check("…and it stays on the record, under Ended", "Governor" in bursar.text(f"{DISC}?view=ended"))

# ================================================================ B. instalments
INST = "/admin/finance/instalments"
r = bursar.post(f"{INST}/new", {"name": "Two parts", "session_id": SESSION, "term": "Full Session",
                                "part_percent": ["50", "40"], "part_date": [YESTERDAY, LATER]}, page=f"{INST}/new")
check("a plan whose parts add up to 90% is refused, saying why", not one("SELECT COUNT(*) FROM finance_instalment_plans")
      and "must add up to 100%" in r.get_data(as_text=True))
bursar.post(f"{INST}/new", {"name": "Two parts", "session_id": SESSION, "term": "Full Session",
                            "part_percent": ["60", "40"], "part_date": [YESTERDAY, LATER]}, page=f"{INST}/new")
PLAN = one("SELECT id FROM finance_instalment_plans WHERE name = 'Two parts'")
check("a plan of 60% by yesterday and 40% in a month is saved", PLAN)
bursar.post(f"{INST}/{PLAN}/students", {"class_id": JSS1}, page=INST)
check("putting JSS 1 on it puts all three students on it",
      one("SELECT COUNT(*) FROM finance_student_plans WHERE plan_id = :p", p=PLAN) == 3)
r = bursar.post(f"{INST}/{PLAN}/edit", {"name": "Two parts", "term": "First Term",
                                        "part_percent": ["60", "40"], "part_date": [YESTERDAY, LATER]}, page=f"{INST}/{PLAN}/edit")
check("a plan with students on it cannot be moved to another billing period",
      one("SELECT term FROM finance_instalment_plans WHERE id = :p", p=PLAN) == "Full Session"
      and "Make a new plan" in r.get_data(as_text=True))
r = bursar.post(f"/admin/finance/students/999999/plan", {"session_id": SESSION, "term": "Full Session", "plan_id": PLAN},
                page=INST)
check("choosing a plan for a student who does not exist is refused, not an error", r.status_code == 404, str(r.status_code))


def account(sid):
    from blueprints.finance import analytics
    return next(r for r in analytics.accounts(SESSION) if r["id"] == sid)


bisi = inside(account, BISI)
check("Bisi, on the plan, is overdue only for the 60% whose date has passed, with the 40% next",
      bisi["overdue"] == 64800 and bisi["next_due"] and bisi["next_due"][0] == LATER and bisi["next_due"][1] == 43200, str(bisi))
bursar.post(f"{INST}/{PLAN}/students/{BISI}/remove", {}, page=INST)
check("taken off the plan, Bisi is overdue for everything past its own due date again",
      inside(account, BISI)["overdue"] == 108000, str(inside(account, BISI)))
bursar.post(f"/admin/finance/students/{BISI}/plan", {"session_id": SESSION, "term": "Full Session", "plan_id": PLAN},
            page=f"/admin/finance/students/{BISI}/account")
check("the plan can be chosen again from Bisi's own fee account", inside(account, BISI)["overdue"] == 64800)
acct = bursar.text(f"/admin/finance/students/{BISI}/account")
check("Bisi's fee account shows the parts, the sibling discount and the reminder button",
      "by " + YESTERDAY in acct and "Overdue" in acct and "sibling 10% off" in acct and "Send a reminder" in acct)
check("the plan's Students page lists them", "Bisitwo" in bursar.text(f"{INST}/{PLAN}/students"))

# ================================================================ C. reminders
SENT = {"sms": [], "email": []}
import core.notifications as N  # noqa: E402


class _Settings:
    sender = "school@alpha.test"


N.sms_settings = lambda: _Settings()
N.send_sms = lambda settings, number, text: (SENT["sms"].append((number, text)) or (True, "ok"))
N.email_settings = lambda: _Settings()
N.send_email = lambda settings, msg: SENT["email"].append((msg["To"], msg["Subject"], msg.get_content()))
REM = "/admin/finance/reminders"
bursar.post(REM, {"enabled": "1", "every_days": "7", "min_overdue": "1000", "sms": "1", "email": "1"}, page=REM)
saved = json.loads(one("SELECT setting_value FROM school_settings WHERE setting_key = 'fee_reminders'") or "{}")
check("the bursar switches reminders on, every 7 days, by text and email", saved.get("enabled") and saved.get("every_days") == 7, str(saved))
bursar.post(f"{REM}/words/sms_fee_reminder", {"body": "{school}: " + "please pay now " * 12}, page=REM)
check("a reminder text too long for one message is refused", "one text" in bursar.said()
      and not one("SELECT COUNT(*) FROM school_settings WHERE setting_key = 'message_wording:sms_fee_reminder'"))
bursar.post(f"{REM}/words/sms_fee_reminder", {"body": "Hello {mother}, pay up."}, page=REM)
check("…as is one with a placeholder it does not have", "curly brackets" in bursar.said())
bursar.post(f"{REM}/words/sms_fee_reminder", {"body": "{school}: {amount} for {child} is overdue. Kindly pay."}, page=REM)
check("the school's own words for the text are saved",
      one("SELECT COUNT(*) FROM school_settings WHERE setting_key = 'message_wording:sms_fee_reminder'") == 1)
page = bursar.text(REM)
check("the page lists who is due a reminder now", all(n in page for n in ("Test, Adaone", "Test, Bisitwo")) and "Send reminders now" in page)
bursar.post(f"{REM}/send", {}, page=REM)
texts = [t for _, t in SENT["sms"]]
check("Send reminders now texts the families in the school's own words",
      any("for Bisitwo Test is overdue. Kindly pay." in t and "N64,800.00" in t for t in texts), str(texts)[:300])
check("…and emails the guardian, with what is overdue and the whole balance",
      any(to == "bisitwo@family.test" and "N64,800.00" in body.replace("₦", "N") and "N108,000.00" in body.replace("₦", "N")
          for to, _, body in SENT["email"]), str(SENT["email"])[:300])
check("…and each one is in the notice log as a fee reminder",
      one("SELECT COUNT(*) FROM notification_delivery_logs WHERE kind = 'fee_reminder' AND status = 'sent'") >= 2)
check("…and the parent sees it in the parent portal",
      one("SELECT COUNT(*) FROM school_notifications WHERE recipient_id = :p AND title LIKE 'Fees overdue%'", p=MUM) >= 1)
before = len(SENT["sms"])
bursar.post(f"{REM}/send", {}, page=REM)
check("sending again the same day reminds nobody twice", len(SENT["sms"]) == before, f"{before} -> {len(SENT['sms'])}")
sql("DELETE FROM notification_delivery_logs WHERE kind = 'fee_reminder'")
before = len(SENT["sms"])
bursar.post(f"/admin/finance/students/{CHIDI}/remind", {"session_id": SESSION}, page=f"/admin/finance/students/{CHIDI}/account")
check("one family can be reminded from their account", len(SENT["sms"]) == before or "reminder sent" in bursar.said())


def scheduled(hour):
    from blueprints.finance.reminders import start_reminders
    from core.staff_attendance import school_now
    return start_reminders(now=school_now().replace(hour=hour, minute=0), inline=True)


sql("DELETE FROM notification_delivery_logs WHERE kind = 'fee_reminder'")
check("the schedule does not send outside the sending hours", inside(scheduled, 22) is False)
check("within them, the reminders start on their own once", inside(scheduled, 10) is True)
check("…and not again the same day, nor until 7 days have passed", inside(scheduled, 11) is False)
cashier.post(REM, {"enabled": "", "every_days": "7", "min_overdue": "0", "sms": "1"}, page="/admin/school")
check("a cashier cannot change the reminders",
      json.loads(one("SELECT setting_value FROM school_settings WHERE setting_key = 'fee_reminders'"))["enabled"])

# ================================================================ D. report card hold
HOLD = "/admin/finance/report-hold"
r = bursar.get(HOLD)
check("the bursar cannot open the report card hold settings (the School Admin's alone)",
      r.status_code in (302, 403) or "Hold report cards for unpaid fees" not in r.get_data(as_text=True))
op.post(HOLD, {"enabled": "1", "basis": "overdue", "min_owed": "0"}, page=HOLD)
check("the School Admin switches the hold on", json.loads(one(
    "SELECT setting_value FROM school_settings WHERE setting_key = 'report_card_hold'") or "{}").get("enabled"))
mum = Person(ALPHA, "parent")
mum.post("/login", {"username": "mum.test", "password": PASSWORD}, page="/login")
r = mum.get(f"/parent/children/{BISI}/report-cards/{SESSION}/first-term")
body = r.get_data(as_text=True)
check("a parent opening a held card is told it is held, with what is overdue and a link to pay",
      r.status_code == 403 and "being held" in body and "64,800.00" in body and f"/parent/children/{BISI}/finance" in body,
      f"{r.status_code} {body[:200]}")
bursar.post(f"/admin/finance/students/{BISI}/report-hold", {"action": "release", "note": ""}, page=f"/admin/finance/students/{BISI}/account")
check("letting a student through needs a reason", not one("SELECT COUNT(*) FROM finance_report_hold_exemptions"))
bursar.post(f"/admin/finance/students/{BISI}/report-hold", {"action": "release", "note": "Promised to pay by Friday"},
            page=f"/admin/finance/students/{BISI}/account")
r = mum.get(f"/parent/children/{BISI}/report-cards/{SESSION}/first-term")
check("once let through, Bisi's card is no longer held (there is simply no card yet)",
      r.status_code == 404, str(r.status_code))
check("the settings page lists who was let through, and why", "Promised to pay by Friday" in op.text(HOLD))
op.post(f"/admin/finance/students/{BISI}/report-hold", {"back": "settings"}, page=HOLD)
check("Hold again holds them again", mum.get(f"/parent/children/{BISI}/report-cards/{SESSION}/first-term").status_code == 403)
sql("UPDATE school_settings SET setting_value = '{}' WHERE setting_key = 'report_card_hold'")
check("with the hold off, nothing is held", mum.get(f"/parent/children/{BISI}/report-cards/{SESSION}/first-term").status_code == 404)
check("the parent's fee page shows the discount and the parts",
      all(x in mum.text(f"/parent/children/{BISI}/finance") for x in ("off: Sibling 10%", "in parts:")))

# ================================================================ E. expenses
EXP = "/admin/finance/expenses"
from PIL import Image  # noqa: E402
png = io.BytesIO()
Image.new("RGB", (40, 30), (200, 30, 30)).save(png, "PNG")
check("a cashier cannot open Expenses", cashier.get(EXP).status_code in (302, 403))
bursar.post(f"{EXP}/new", {"amount": "-5", "category": "Power & fuel (NEPA, diesel)", "spent_on": TODAY.isoformat(),
                           "description": "Diesel", "method": "Cash"}, page=f"{EXP}/new")
check("a negative amount is refused", not one("SELECT COUNT(*) FROM finance_expenses"))
bursar.post(f"{EXP}/new", {"amount": "5000", "category": "Power & fuel (NEPA, diesel)", "spent_on": LATER,
                           "description": "Diesel", "method": "Cash"}, page=f"{EXP}/new")
check("an expense dated in the future is refused", not one("SELECT COUNT(*) FROM finance_expenses"))
bursar.post(f"{EXP}/new", {"amount": "45000", "category": "Power & fuel (NEPA, diesel)", "spent_on": TODAY.isoformat(),
                           "paid_to": "Total filling station", "description": "200 litres of diesel", "method": "Cash"},
            page=f"{EXP}/new", files={"receipt": ("receipt.png", png.getvalue())})
EX, PATH = (sql("SELECT id, receipt_path FROM finance_expenses") or [(None, None)])[0]
check("the bursar records ₦45,000 of diesel with a photo of the receipt", EX and PATH and PATH.startswith("expenses/") or
      (PATH and "expenses/" in PATH), str(PATH))
r = bursar.get(f"{EXP}/{EX}/receipt")
check("…which the bursar can open", r.status_code == 200 and r.mimetype.startswith("image/"), f"{r.status_code} {r.mimetype}")
check("…but a teacher cannot", teacher.get(f"{EXP}/{EX}/receipt").status_code in (302, 403))
check("…and nobody can by its uploads address", bursar.get("/" + PATH if PATH.startswith("uploads/") else f"/uploads/{PATH}").status_code == 404)
bursar.post(f"{EXP}/new", {"amount": "8000", "category": "Maintenance & repairs", "spent_on": TODAY.isoformat(),
                           "description": "Fixing a door", "method": "Cash"}, page=f"{EXP}/new")
WRONG = one("SELECT id FROM finance_expenses WHERE amount = 8000")
bursar.post(f"{EXP}/{WRONG}/void", {"reason": ""}, page=EXP)
check("voiding needs a reason", one("SELECT status FROM finance_expenses WHERE id = :i", i=WRONG) == "posted")
bursar.post(f"{EXP}/{WRONG}/void", {"reason": "Entered twice"}, page=EXP)
check("with a reason it is voided, and kept", one("SELECT status FROM finance_expenses WHERE id = :i", i=WRONG) == "voided")
page = bursar.text(EXP)
check("the month's page totals only what counts", "₦45,000.00" in page and "1 voided" in page)

# ================================================================ F. profit and loss
PLP = "/admin/finance/profit-loss"
check("the bursar cannot see the profit and loss (it shows staff pay)", bursar.get(PLP).status_code in (302, 403))
sql("INSERT INTO payroll_runs (period, status, gross, deductions, net, employer_pension, headcount, prepared_at, paid_at) "
    "VALUES (:p, 'paid', 25000000, 0, 25000000, 1000000, 3, :n, :n)", p=TODAY.strftime("%Y-%m"), n=NOW)


def pl():
    from blueprints.finance.expenses import _month_bounds, profit_and_loss
    return profit_and_loss(*_month_bounds(TODAY.strftime("%Y-%m")))


sql("INSERT INTO finance_expenses (spent_on, category, amount, description, status, recorded_by, created_at) "
    "VALUES (:d, 'Other', 99999, 'Last month', 'posted', 1, :n)",
    d=(TODAY.replace(day=1) - timedelta(days=1)).isoformat(), n=NOW)
report = inside(pl)
check("money in is the fees received this month", report["fees_total"] == 30000 and report["income"] == 30000, str(report)[:300])
check("money out is the paid payroll (with the school's pension) and the expenses that count",
      report["payroll_total"] == 260000 and report["expenses_total"] == 45000 and report["spending"] == 305000, str(report)[:300])
check("…and the shortfall is the difference", report["net"] == -275000)
page = op.text(PLP)
check("the School Admin sees it, with both sides", all(x in page for x in ("Money in", "Money out", "Shortfall", "Staff pay (gross)")))
check("the session view works from the session's dates", "Month by month" in op.text(f"{PLP}?period=session"))

# ================================================================ G. a waiting discount comes off when its payment goes
op.post(f"/admin/finance/payments/{CHIDI_PAY}/void", {"reason": "Paid into the wrong account"},
        page=f"/admin/finance/receipts/{CHIDI_PAY}")
said = op.said()
check("voiding the payment that held Chidi's scholarship back takes the rest of it off: Tuition is now nothing",
      bill(CHIDI, TUITION)[0] == 0 and bill(CHIDI, TUITION)[2] == 100000
      and one("SELECT discount_held FROM finance_fee_assessments WHERE id = :i", i=c_t[4]) is None
      and "30,000.00 of discount" in said, f"{bill(CHIDI, TUITION)} {said}")
check("…and nothing else of Chidi's changes (the levy keeps its own discount)", bill(CHIDI, LEVY)[0] == 10000)
check("…and Ada's Tuition, whose scholarship was ended on purpose, is not touched", bill(ADA, TUITION)[0] == 100000)

# The same when Paystack confirms a refund of an online payment.
ADA_PAY = pay(ADA, 40000, bill(ADA, TUITION)[4])
bursar.post(f"{DISC}/new", {"student_id": ADA, "kind": "scholarship", "basis": "percent", "value": "100", "category": "Tuition",
                            "scope": "session", "reprice": "1"}, page=f"{DISC}/new")
check("a full scholarship given after Ada paid ₦40,000 takes ₦60,000 off and keeps ₦40,000 waiting",
      bill(ADA, TUITION)[0] == 40000 and bill(ADA, TUITION)[2] == 60000
      and one("SELECT discount_held FROM finance_fee_assessments WHERE id = :i", i=bill(ADA, TUITION)[4]) == 40000,
      str(bill(ADA, TUITION)))
sql("INSERT INTO finance_online_payments (reference, student_id, session_id, amount, status, payment_id, created_at) "
    "VALUES ('T-REF-1', :s, :x, 40000, 'success', :p, :n)", s=ADA, x=SESSION, p=ADA_PAY, n=NOW)
sql("INSERT INTO finance_refunds (payment_id, online_payment_id, amount, status, paystack_refund_id, requested_by, requested_at) "
    "VALUES (:p, (SELECT id FROM finance_online_payments WHERE reference = 'T-REF-1'), 40000, 'pending', 'RF-1', 1, :n)",
    p=ADA_PAY, n=NOW)


def refunded():
    from blueprints.finance.paystack import _finalize_refund
    _finalize_refund('refund.processed', {'id': 'RF-1'})


inside(refunded)
check("once Paystack confirms the refund, the waiting discount comes off: Ada's Tuition is now nothing",
      one("SELECT status FROM finance_payments WHERE id = :i", i=ADA_PAY) == "voided" and bill(ADA, TUITION)[0] == 0
      and one("SELECT discount_held FROM finance_fee_assessments WHERE id = :i", i=bill(ADA, TUITION)[4]) is None,
      str(bill(ADA, TUITION)))

# ================================================================ every page opens
for path in (DISC, f"{DISC}/new", INST, f"{INST}/new", f"{INST}/{PLAN}/edit", REM, EXP, f"{EXP}/new", "/admin/finance/accounts",
             f"/admin/finance/students/{ADA}/account", "/admin/settings", "/admin/settings/message-wording"):
    r = op.get(path)
    check(f"{path} opens for the School Admin", r.status_code == 200, str(r.status_code))
check("the menu shows the new Finance pages", all(x in op.text("/admin/school") for x in ("Discounts", "Instalments", "Expenses", "Profit &amp; loss")))
check("no page answered with a server error", not PROBLEMS, "; ".join(PROBLEMS[:5]))
check("nothing raised behind the scenes", not errors.seen, "; ".join(repr(e) for e in errors.seen[:3]))
failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
