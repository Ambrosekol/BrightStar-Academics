"""Paying an approved payroll through the school's own Paystack, proven end to end on PostgreSQL against a stand-in
Paystack. In plain words:

A. Who: only the school's own School Admin (the proprietress) reviews and sends; the Payroll Officer, the bursar and a
   platform operator visiting the school are refused. A draft payroll is never paid.
B. The review: each person with the name the bank holds for their account; a name that does not match is offered
   unticked; someone whose bank details changed after approval is not offered at all; the balance is shown.
C. Sending needs the proprietress's own password; a balance that does not cover the total sends nothing. It sends the
   exact net pay of each ticked person, to their own account, once: a second click sends nobody again.
D. While a transfer is being paid, the payroll cannot be reopened or marked paid. A webhook with a bad signature is
   refused; a signed one is only believed once Paystack itself confirms. When everyone has been paid, the payroll is
   marked paid on its own.
E. Paystack refusing (the transfer OTP still on) marks them not paid, with the way to fix it, and they can be sent
   again; Paystack not answering leaves them pending, and Check transfers settles them by asking Paystack.
F. No account number is shown on a page or written to the log.
"""
import atexit
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="beacon_paypaystack_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup("paypaystack")
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
from werkzeug.security import generate_password_hash  # noqa: E402

import app as A  # noqa: E402
from control_plane import provisioning as pv  # noqa: E402
from control_plane.context import tenant_context  # noqa: E402
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
        self.addr = f"10.97.{Person.count[0]}.1"

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
op = Person("platform operator")
op.get(r.headers["Location"][len(UA):])
op.get("/admin/workspace/school")
PASSWORD = "Fixture-password-9"
HASH = generate_password_hash(PASSWORD, method="pbkdf2:sha256:1000")
role = {name: one("SELECT id FROM admin_types WHERE name = :n", n=name)
        for name in ("Payroll Officer", "Finance Manager", "Subject Teacher", "HR Officer")}


def sign_in(username):
    person = Person(username)
    person.post("/login", {"username": username, "password": PASSWORD}, page="/login")
    person.get("/admin/workspace/school")
    return person


def new_staff(username, display, role_name):
    payload = {"username": username, "display_name": display, "admin_type_ids": [str(role[role_name])],
               "duty_class": [""], "duty_subject": [""], "duty_department": [""]}
    op.post("/admin/administration/admins/new", payload, page="/admin/administration/admins/new")
    aid = one("SELECT id FROM admins WHERE username = :u", u=username)
    sql("UPDATE admins SET password_hash = :h, password_must_change = 0 WHERE id = :i", h=HASH, i=aid)
    return sign_in(username), aid


officer, OFFICER = new_staff("pay.officer", "Mr Tunde Pay", "Payroll Officer")
bursar, BURSAR = new_staff("bursar", "Mrs Ngozi Bursar", "Finance Manager")
teacher, TEACHER = new_staff("mrs.bello", "Mrs Funmi Bello", "Subject Teacher")
hro, HRO = new_staff("hr.officer", "Mr Hr Officer", "HR Officer")
hro.post("/admin/hr/from-accounts", {}, page="/admin/hr")
# The school's own proprietress: a School Admin account of the school's, not the platform's.
SYSTEM_ROLE = one("SELECT id FROM admin_types WHERE is_system = 1 ORDER BY id LIMIT 1")
PROP = one("INSERT INTO admins (username, display_name, password_hash, admin_type_id, active, password_must_change, created_at) "
           "VALUES ('proprietress', 'Mrs Adaeze Owner', :h, :t, 1, 0, '2026-10-14T08:00:00+00:00') RETURNING id", h=HASH, t=SYSTEM_ROLE)
prop = sign_in("proprietress")
BELLO = one("SELECT id FROM hr_staff WHERE admin_id = :a", a=TEACHER)
TUNDE = one("SELECT id FROM hr_staff WHERE admin_id = :a", a=OFFICER)
NGOZI = one("SELECT id FROM hr_staff WHERE admin_id = :a", a=BURSAR)
PAY = "/admin/payroll"

# ---------------------------------------------------------------- the stand-in Paystack
SECRET = "sk_test_" + "a1b2c3d4" * 4
ACCOUNTS = {("058", "0123456789"): "FUNMI BELLO", ("057", "1111111111"): "JOHN DOE", ("033", "2222222222"): "NGOZI BURSAR"}
BANKS = [{"name": "Guaranty Trust Bank", "code": "058"}, {"name": "Zenith Bank", "code": "057"},
         {"name": "United Bank For Africa", "code": "033"}, {"name": "Access Bank", "code": "044"},
         {"name": "Access Bank (Diamond)", "code": "063"}]
STATE = {"balance": 500_000_000, "bulk": [], "verify": {}, "bulk_mode": "ok", "calls": []}


class Reply:
    def __init__(self, payload, status=200):
        self.status, self.payload = status, json.dumps(payload).encode()

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fail(request, code, payload):
    raise urllib.error.HTTPError(request.full_url, code, "error", {}, io.BytesIO(json.dumps(payload).encode()))


def fake_urlopen(request, timeout=0):
    url = urllib.parse.urlparse(request.full_url)
    query = dict(urllib.parse.parse_qsl(url.query))
    body = json.loads(request.data) if request.data else None
    STATE["calls"].append((request.get_method(), url.path, body))
    assert request.headers.get("Authorization") == f"Bearer {SECRET}"
    if url.path == "/bank":
        return Reply({"status": True, "data": BANKS})
    if url.path == "/bank/resolve":
        name = ACCOUNTS.get((query.get("bank_code"), query.get("account_number")))
        if not name:
            fail(request, 422, {"status": False, "message": "Could not resolve account name. Check parameters or try again."})
        return Reply({"status": True, "data": {"account_number": query["account_number"], "account_name": name}})
    if url.path == "/balance":
        return Reply({"status": True, "data": [{"currency": "NGN", "balance": STATE["balance"]}]})
    if url.path == "/transferrecipient":
        return Reply({"status": True, "data": {"recipient_code": "RCP_" + body["account_number"][-4:] + body["bank_code"]}}, 201)
    if url.path == "/transfer/bulk":
        if STATE["bulk_mode"] == "otp":
            fail(request, 400, {"status": False, "message": "You need to disable the Transfers OTP to use this feature"})
        if STATE["bulk_mode"] == "silent":
            raise TimeoutError("timed out")
        STATE["bulk"].append(body)
        return Reply({"status": True, "data": [{"reference": t["reference"], "recipient": t["recipient"], "amount": t["amount"],
                                                "transfer_code": "TRF_" + t["reference"][-6:], "status": "received"}
                                               for t in body["transfers"]]})
    if url.path.startswith("/transfer/verify/"):
        ref = url.path.rsplit("/", 1)[-1]
        if ref not in STATE["verify"]:
            fail(request, 404, {"status": False, "message": "Transfer not found"})
        return Reply({"status": True, "data": {"reference": ref, "status": STATE["verify"][ref], "reason": "Account closed"}})
    raise AssertionError(f"unexpected Paystack call {url.path}")


real_urlopen = urllib.request.urlopen
urllib.request.urlopen = fake_urlopen

with A.app.app_context(), tenant_context(INFO):
    from core import payments
    payments.set_payment_settings("pk_test_" + "z9" * 16, SECRET, PROP)
    A.db.session.commit()
    A.db.session.remove()
from core.short_cache import clear  # noqa: E402
clear()


# ---------------------------------------------------------------- an approved payroll of three
def set_pay(sid, bank, account, name):
    prop.post(f"{PAY}/staff/{sid}", {"on_payroll": "1", "basic": "300000", "bank_name": bank, "account_number": account,
                                       "account_name": name}, page=f"{PAY}/staff/{sid}")


set_pay(BELLO, "GTBank", "0123456789", "FUNMI BELLO")
set_pay(TUNDE, "Zenith Bank", "1111111111", "TUNDE PAY")
set_pay(NGOZI, "UBA", "2222222222", "NGOZI BURSAR")
from datetime import date  # noqa: E402
officer.post(f"{PAY}/runs", {"period": date.today().strftime("%Y-%m")}, page=PAY)
RUN = one("SELECT id FROM payroll_runs ORDER BY id DESC LIMIT 1")
REVIEW = f"{PAY}/runs/{RUN}/paystack"
SEND = f"{REVIEW}/send"
NET = {sid: one("SELECT net FROM payroll_payslips WHERE run_id = :r AND staff_id = :s", r=RUN, s=sid) for sid in (BELLO, TUNDE, NGOZI)}
SLIP = {sid: one("SELECT id FROM payroll_payslips WHERE run_id = :r AND staff_id = :s", r=RUN, s=sid) for sid in (BELLO, TUNDE, NGOZI)}

# ================================================================ A. who, and only once approved
check("A. a draft payroll is never paid", prop.get(REVIEW).status_code == 302 and not STATE["bulk"])
prop.post(f"{PAY}/runs/{RUN}/approve", {}, page=f"{PAY}/runs/{RUN}")
check("A. the proprietress approves it", one("SELECT status FROM payroll_runs WHERE id = :r", r=RUN) == "approved")
# After approval, Mrs Bursar's bank details change: her salary must not follow them.
sql("UPDATE payroll_staff SET bank_name = 'Access Bank' WHERE staff_id = :s", s=NGOZI)
run_page = prop.text(f"{PAY}/runs/{RUN}")
check("A. the payroll's page offers the proprietress Review and pay", "Review and pay" in run_page)
check("A. the Payroll Officer is not offered it, and is refused the review and sending",
      "Review and pay" not in officer.text(f"{PAY}/runs/{RUN}") and officer.get(REVIEW).status_code in (302, 403)
      and officer.post(SEND, {"password": PASSWORD, "slip": [SLIP[BELLO]]}, page=f"{PAY}/runs/{RUN}").status_code in (302, 403))
check("A. the bursar is refused", bursar.get(REVIEW).status_code in (302, 403))
check("A. a platform operator visiting the school as its School Admin is refused (never a school's money)",
      op.get(REVIEW).status_code in (302, 403) and "Review and pay" not in op.text(f"{PAY}/runs/{RUN}"))
check("A. nothing was sent by any of them", not STATE["bulk"])

# ================================================================ B. the review
review = prop.text(REVIEW)
check("B. the review shows the name each bank holds for the account", "FUNMI BELLO" in review and "JOHN DOE" in review)
check("B. a matching name is ticked; one that does not match is offered unticked",
      re.search(rf'value="{SLIP[BELLO]}"[^>]*checked', review) and re.search(rf'value="{SLIP[TUNDE]}"', review)
      and not re.search(rf'value="{SLIP[TUNDE]}"[^>]*checked', review))
check("B. someone whose bank details changed after approval is not offered at all",
      f'value="{SLIP[NGOZI]}"' not in review and "changed after approval" in review)
check("B. the school's Paystack balance is shown", "5,000,000.00" in review)

# ================================================================ C. sending
prop.post(SEND, {"password": "not-my-password", "slip": [SLIP[BELLO]]}, page=REVIEW)
check("C. a wrong password sends nothing", not STATE["bulk"] and not one("SELECT COUNT(*) FROM payroll_payslips WHERE transfer_status IS NOT NULL"))
STATE["balance"] = 1000
prop.post(SEND, {"password": PASSWORD, "slip": [SLIP[BELLO]]}, page=REVIEW)
check("C. a balance that does not cover the total sends nothing", not STATE["bulk"] and "does not cover" in prop.said())
STATE["balance"] = 500_000_000
prop.post(SEND, {"password": PASSWORD, "slip": [SLIP[BELLO], SLIP[NGOZI]]}, page=REVIEW)
sent = [t for b in STATE["bulk"] for t in b["transfers"]]
check("C. it sends Mrs Bello's exact net pay to her own account, and not the one whose details changed",
      len(sent) == 1 and sent[0]["amount"] == NET[BELLO] and sent[0]["recipient"] == "RCP_6789058"
      and sent[0]["reason"].startswith("Salary"), str(sent))
check("C. her payslip is now being paid, with its own reference", one(
      "SELECT transfer_status FROM payroll_payslips WHERE id = :p", p=SLIP[BELLO]) == "pending"
      and one("SELECT transfer_reference FROM payroll_payslips WHERE id = :p", p=SLIP[BELLO]).startswith(f"bsp-{RUN}-"))
prop.post(SEND, {"password": PASSWORD, "slip": [SLIP[BELLO]]}, page=REVIEW)
check("C. a second click pays nobody twice", len([t for b in STATE["bulk"] for t in b["transfers"]]) == 1)

# ================================================================ D. while it is being paid, and settling it
prop.post(f"{PAY}/runs/{RUN}/reopen", {}, page=f"{PAY}/runs/{RUN}")
check("D. a payroll with salaries sent cannot be reopened", one("SELECT status FROM payroll_runs WHERE id = :r", r=RUN) == "approved")
officer.post(f"{PAY}/runs/{RUN}/paid", {}, page=f"{PAY}/runs/{RUN}")
check("D. nor marked paid while a transfer is still being paid", one("SELECT status FROM payroll_runs WHERE id = :r", r=RUN) == "approved")
REF = one("SELECT transfer_reference FROM payroll_payslips WHERE id = :p", p=SLIP[BELLO])
event = json.dumps({"event": "transfer.success", "data": {"reference": REF}}).encode()
bad = A.app.test_client().post("/paystack/webhook", data=event, base_url=UA, headers={"x-paystack-signature": "0" * 128})
check("D. a webhook with a bad signature is refused, and changes nothing", bad.status_code == 401
      and one("SELECT transfer_status FROM payroll_payslips WHERE id = :p", p=SLIP[BELLO]) == "pending")
signature = hmac.new(SECRET.encode(), event, hashlib.sha512).hexdigest()
STATE["verify"][REF] = "pending"
A.app.test_client().post("/paystack/webhook", data=event, base_url=UA, headers={"x-paystack-signature": signature})
check("D. a signed webhook is not believed until Paystack itself confirms", one(
      "SELECT transfer_status FROM payroll_payslips WHERE id = :p", p=SLIP[BELLO]) == "pending")
STATE["verify"][REF] = "success"
ok = A.app.test_client().post("/paystack/webhook", data=event, base_url=UA, headers={"x-paystack-signature": signature})
check("D. once Paystack confirms, her payslip is paid", ok.status_code == 200
      and one("SELECT transfer_status FROM payroll_payslips WHERE id = :p", p=SLIP[BELLO]) == "success")
check("D. the payroll is not marked paid while others are still to be paid", one("SELECT status FROM payroll_runs WHERE id = :r", r=RUN) == "approved")

# ================================================================ E. Paystack refusing, and not answering
STATE["bulk_mode"] = "otp"
prop.post(SEND, {"password": PASSWORD, "slip": [SLIP[TUNDE]]}, page=REVIEW)
check("E. Paystack refusing (the transfer OTP still on) marks him not paid, with the way to fix it",
      one("SELECT transfer_status FROM payroll_payslips WHERE id = :p", p=SLIP[TUNDE]) == "failed"
      and "Preferences" in (one("SELECT transfer_error FROM payroll_payslips WHERE id = :p", p=SLIP[TUNDE]) or ""))
STATE["bulk_mode"] = "silent"
prop.post(SEND, {"password": PASSWORD, "slip": [SLIP[TUNDE]]}, page=REVIEW)
check("E. a failed one can be sent again; when Paystack does not answer, it stays pending (never sent twice)",
      one("SELECT transfer_status FROM payroll_payslips WHERE id = :p", p=SLIP[TUNDE]) == "pending")
REF2 = one("SELECT transfer_reference FROM payroll_payslips WHERE id = :p", p=SLIP[TUNDE])
STATE["bulk_mode"] = "ok"
officer.post(f"{PAY}/runs/{RUN}/paystack/check", {}, page=f"{PAY}/runs/{RUN}")
check("E. Check transfers leaves a just-sent one Paystack has no record of yet as pending",
      one("SELECT transfer_status FROM payroll_payslips WHERE id = :p", p=SLIP[TUNDE]) == "pending")
STATE["verify"][REF2] = "success"
officer.post(f"{PAY}/runs/{RUN}/paystack/check", {}, page=f"{PAY}/runs/{RUN}")
check("E. …and settles it once Paystack confirms (the Payroll Officer may check)",
      one("SELECT transfer_status FROM payroll_payslips WHERE id = :p", p=SLIP[TUNDE]) == "success")
check("E. the payroll is still approved: Mrs Bursar was not paid from here", one("SELECT status FROM payroll_runs WHERE id = :r", r=RUN) == "approved")
officer.post(f"{PAY}/runs/{RUN}/paid", {}, page=f"{PAY}/runs/{RUN}")
check("E. with nothing being paid, the Payroll Officer marks it paid (Mrs Bursar paid by the bank)",
      one("SELECT status FROM payroll_runs WHERE id = :r", r=RUN) == "paid")

# A second month, everyone paid from here: it is marked paid on its own.
NEXT = one("SELECT to_char((date_trunc('month', CURRENT_DATE) + interval '1 month')::date, 'YYYY-MM')")
sql("UPDATE payroll_staff SET bank_name = 'UBA' WHERE staff_id = :s", s=NGOZI)
officer.post(f"{PAY}/runs", {"period": NEXT}, page=PAY)
RUN2 = one("SELECT id FROM payroll_runs WHERE period = :p", p=NEXT)
prop.post(f"{PAY}/runs/{RUN2}/approve", {}, page=f"{PAY}/runs/{RUN2}")
slips2 = [i for (i,) in sql("SELECT id FROM payroll_payslips WHERE run_id = :r AND net > 0", r=RUN2)]
prop.post(f"{PAY}/runs/{RUN2}/paystack/send", {"password": PASSWORD, "slip": slips2}, page=f"{PAY}/runs/{RUN2}/paystack")
for (ref,) in sql("SELECT transfer_reference FROM payroll_payslips WHERE run_id = :r", r=RUN2):
    STATE["verify"][ref] = "success"
prop.post(f"{PAY}/runs/{RUN2}/paystack/check", {}, page=f"{PAY}/runs/{RUN2}")
check("D. when everyone has been paid through Paystack, the payroll is marked paid on its own",
      one("SELECT status FROM payroll_runs WHERE id = :r", r=RUN2) == "paid" and len(slips2) == 3)

# ================================================================ F. no account numbers anywhere
pages = prop.text(REVIEW.replace(str(RUN), str(RUN2))) + prop.text(f"{PAY}/runs/{RUN}") + officer.text(f"{PAY}/runs/{RUN}")
logged = " ".join(str(d) for (d,) in sql("SELECT details FROM audit_logs WHERE details IS NOT NULL"))
check("F. no account number on a page or in the log", not any(n in pages + logged for n in ("0123456789", "1111111111", "2222222222")))
check("F. every step is in the activity log", one("SELECT COUNT(*) FROM audit_logs WHERE action = 'payroll_paystack_sent'") >= 3
      and one("SELECT COUNT(*) FROM audit_logs WHERE action = 'payroll_paystack_password_refused'") == 1)

urllib.request.urlopen = real_urlopen
check("no page answered with a server error", not PROBLEMS, "; ".join(PROBLEMS[:5]))
failed = [name for name, ok, _ in results if not ok]
print()
print(f"{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", "; ".join(failed))
    raise SystemExit(1)
