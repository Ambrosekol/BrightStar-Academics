"""Payroll, proven end to end on PostgreSQL. In plain words:

A. Who: the Payroll Officer and the School Admin have Payroll; the bursar, a teacher and the HR Officer do not, and are
   refused its pages. Only the School Admin approves.
B. Settings: bands out of order and percentages over 100 are refused; who checked them is kept; a teacher cannot change them.
C. Pay: a bad account number is refused; bank numbers, pension PINs and tax IDs are encrypted and never shown or logged;
   nobody sets their own pay.
D. A month: a payslip worked out exactly (checked by hand); one payroll a month; one-off lines; never one's own; the
   bank schedule waits for approval; staff do not see a draft; the preparer cannot approve; once approved it is locked.
E. Payslips and exports: a teacher sees her own, never a colleague's; the bank schedule and returns carry the decrypted
   numbers, defuse formula-looking cells, are never cached and are logged; a teacher cannot download them.
F. Reopening hides payslips and needs a different approver; unpaid leave is taken off; paid is final.
G. Rotating the key re-encrypts the payroll secrets, and they still read.
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
TMP = tempfile.mkdtemp(prefix="beacon_payroll_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup("payroll")
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
role = {name: one("SELECT id FROM admin_types WHERE name = :n", n=name)
        for name in ("Payroll Officer", "Finance Manager", "Subject Teacher", "HR Officer")}


def new_staff(username, display, role_name):
    payload = {"username": username, "display_name": display, "admin_type_ids": [str(role[role_name])],
               "duty_class": [""], "duty_subject": [""], "duty_department": [""]}
    op.post("/admin/administration/admins/new", payload, page="/admin/administration/admins/new")
    aid = one("SELECT id FROM admins WHERE username = :u", u=username)
    sql("UPDATE admins SET password_hash = :h, password_must_change = 0 WHERE id = :i", h=HASH, i=aid)
    person = Person(username)
    person.post("/login", {"username": username, "password": PASSWORD}, page="/login")
    person.get("/admin/workspace/school")
    return person, aid


officer, OFFICER = new_staff("pay.officer", "Mr Tunde Pay", "Payroll Officer")
bursar, BURSAR = new_staff("bursar", "Mrs Ngozi Bursar", "Finance Manager")
teacher, TEACHER = new_staff("mrs.bello", "Mrs Funmi Bello", "Subject Teacher")
hro, HRO = new_staff("hr.officer", "Mr Hr Officer", "HR Officer")
hro.post("/admin/hr/from-accounts", {}, page="/admin/hr")
sql("UPDATE hr_staff SET first_name = '=cmd|calc' WHERE admin_id = :a", a=HRO)    # a formula-looking name, for the CSVs
BELLO = one("SELECT id FROM hr_staff WHERE admin_id = :a", a=TEACHER)
TUNDE = one("SELECT id FROM hr_staff WHERE admin_id = :a", a=OFFICER)
NGOZI = one("SELECT id FROM hr_staff WHERE admin_id = :a", a=BURSAR)
ITEM = {name: i for i, name in sql("SELECT id, name FROM payroll_items")}

from datetime import date, timedelta  # noqa: E402
TODAY = date.today()
PERIOD = TODAY.strftime("%Y-%m")
PAY = "/admin/payroll"

# ================================================================ A. who
check("the Payroll Officer has Payroll in the menu; the bursar (Finance Manager) does not",
      "/admin/payroll" in officer.text("/admin/school") and "/admin/payroll" not in bursar.text("/admin/school"))
check("the bursar is refused the payroll and the staff pay", bursar.get(PAY).status_code in (302, 403)
      and bursar.get(f"{PAY}/staff/{BELLO}").status_code in (302, 403))
check("a teacher and the HR Officer do not: salaries are seen nowhere else",
      "/admin/payroll" not in teacher.text("/admin/school") and "/admin/payroll" not in hro.text("/admin/school"))
for path in (PAY, f"{PAY}/staff", f"{PAY}/staff/{BELLO}", f"{PAY}/settings"):
    check(f"a teacher is refused {path}", teacher.get(path).status_code in (302, 403))
check("the HR Officer is refused the staff pay", hro.get(f"{PAY}/staff/{BELLO}").status_code in (302, 403))
check("the starting pay items and the accountant warning are there",
      {"Housing allowance", "Transport allowance", "Cooperative"} <= set(ITEM) and "not been checked by the school's accountant" in officer.text(PAY))

# ================================================================ B. settings
SET = f"{PAY}/settings"
form = {"paye_enabled": "1", "pension_enabled": "1", "pension_employee_bp": "8", "pension_employer_bp": "10", "nhf_bp": "2.5",
        "rent_relief_bp": "20", "rent_relief_cap": "500000", "checked_by": "Ade & Co, Chartered Accountants"}
bands = [("800000", "0"), ("3000000", "15"), ("12000000", "18"), ("25000000", "21"), ("50000000", "23"), ("", "25")]
for i, (upper, rate) in enumerate(bands):
    form[f"band_upper_{i}"], form[f"band_rate_{i}"] = upper, rate
officer.post(SET, dict(form, band_upper_1="500000"), page=SET)
check("PAYE bands out of order are refused", "above the one before" in officer.said())
officer.post(SET, dict(form, pension_employee_bp="120"), page=SET)
check("a percentage over 100 is refused", one("SELECT checked_by FROM payroll_settings") is None)
officer.post(SET, form, page=SET)
check("the settings are saved, with who checked them", one("SELECT checked_by FROM payroll_settings") == "Ade & Co, Chartered Accountants"
      and "not been checked" not in officer.text(PAY))
check("a teacher cannot change them", teacher.post(SET, dict(form, pension_employee_bp="0")).status_code in (302, 403)
      and one("SELECT pension_employee_bp FROM payroll_settings") == 800)


# ================================================================ C. people's pay
def set_pay(person, sid, **over):
    data = {"on_payroll": "1", "basic": "300000", "bank_name": "GTBank", "account_number": "0123456789", "account_name": "FUNMI BELLO",
            "pfa": "Stanbic IBTC Pension", "rsa_pin": "PEN100200300400", "tax_id": "TIN-55-66",
            f"item_{ITEM['Housing allowance']}": "1", f"item_amount_{ITEM['Housing allowance']}": "50,000",
            f"item_{ITEM['Transport allowance']}": "1", f"item_amount_{ITEM['Transport allowance']}": "20000",
            f"item_{ITEM['Cooperative']}": "1", f"item_amount_{ITEM['Cooperative']}": "5000"}
    data.update(over)
    return person.post(f"{PAY}/staff/{sid}", data, page=f"{PAY}/staff/{sid}")


set_pay(officer, BELLO, account_number="12345")
check("an account number that is not ten digits is refused", not one("SELECT COUNT(*) FROM payroll_staff"))
set_pay(officer, BELLO)
row = sql("SELECT basic, account_number_enc, account_last4, rsa_pin_enc, tax_id_enc FROM payroll_staff WHERE staff_id = :s", s=BELLO)[0]
check("the Payroll Officer sets Mrs Bello's pay", row[0] == 30_000_000 and row[2] == "6789")
check("…the account number, pension PIN and tax ID are encrypted, never stored plain",
      row[1].startswith("enc:v1:") and "0123456789" not in row[1] and row[3].startswith("enc:v1:") and "PEN100" not in row[3]
      and row[4].startswith("enc:v1:"))
check("…and the log says what changed, never the amounts or the numbers",
      not one("SELECT COUNT(*) FROM audit_logs WHERE action <> 'payroll_settings_saved' AND (details LIKE '%30000000%' OR details LIKE '%0123456789%' OR details LIKE '%PEN100%')"))
page = officer.text(f"{PAY}/staff/{BELLO}")
check("the pay page shows only the last four digits, never the number or the PIN",
      "6789" in page and "0123456789" not in page and "PEN100200300400" not in page)
set_pay(officer, TUNDE, basic="999999")
check("nobody sets their own pay", not one("SELECT COUNT(*) FROM payroll_staff WHERE staff_id = :s", s=TUNDE)
      and "someone else sets your own pay" in officer.said())
op.post(f"{PAY}/staff/{TUNDE}", {"on_payroll": "1", "basic": "200000", "bank_name": "UBA", "account_number": "2222222222"}, page=f"{PAY}/staff/{TUNDE}")
op.post(f"{PAY}/staff/{NGOZI}", {"on_payroll": "1", "basic": "250000", "bank_name": "Zenith Bank", "account_number": "3333333333"}, page=f"{PAY}/staff/{NGOZI}")
HRO_REC = one("SELECT id FROM hr_staff WHERE admin_id = :a", a=HRO)
op.post(f"{PAY}/staff/{HRO_REC}", {"on_payroll": "1", "basic": "100000", "bank_name": "UBA", "account_number": "4444444444"}, page=f"{PAY}/staff/{HRO_REC}")
check("the School Admin sets the Payroll Officer's pay", one("SELECT COUNT(*) FROM payroll_staff WHERE on_payroll = 1") == 4)

# ================================================================ D. a month's payroll
officer.post(f"{PAY}/runs", {"period": "2026-1' OR 1=1"}, page=PAY)
check("a month that is not YYYY-MM is refused", not one("SELECT COUNT(*) FROM payroll_runs"))
officer.post(f"{PAY}/runs", {"period": PERIOD}, page=PAY)
RUN = one("SELECT id FROM payroll_runs WHERE period = :p", p=PERIOD)
slip = sql("SELECT basic, gross, paye, pension_employee, pension_employer, deductions, net FROM payroll_payslips WHERE run_id = :r AND staff_id = :s",
           r=RUN, s=BELLO)
# Worked by hand: gross 370,000; pension 8% of 370,000 = 29,600 (school 37,000); taxable a year 4,440,000 - 355,200 = 4,084,800;
# tax 0 + 2,200,000 x 15% + 1,084,800 x 18% = 525,264 a year = 43,772 a month; deductions 43,772 + 29,600 + 5,000 = 78,372.
check("Mrs Bello's payslip is worked out exactly", slip == [(30_000_000, 37_000_000, 4_377_200, 2_960_000, 3_700_000, 7_837_200, 29_162_800)], str(slip))
check("a draft payroll of everyone on it is prepared", one("SELECT headcount FROM payroll_runs WHERE id = :r", r=RUN) == 4)
officer.post(f"{PAY}/runs", {"period": PERIOD}, page=PAY)
check("a second payroll for the same month is refused", one("SELECT COUNT(*) FROM payroll_runs") == 1)
officer.post(f"{PAY}/runs/{RUN}/adjust", {"staff_id": BELLO, "kind": "earning", "label": "Arrears", "amount": "10000", "taxable": "1"}, page=f"{PAY}/runs/{RUN}")
check("a one-off line is added and the payslip worked out again",
      one("SELECT gross FROM payroll_payslips WHERE run_id = :r AND staff_id = :s", r=RUN, s=BELLO) == 38_000_000)
officer.post(f"{PAY}/runs/{RUN}/adjust", {"staff_id": TUNDE, "kind": "earning", "label": "Bonus", "amount": "500000"}, page=f"{PAY}/runs/{RUN}")
check("nobody adds a line to their own payslip", not one("SELECT COUNT(*) FROM payroll_adjustments WHERE staff_id = :s", s=TUNDE))
r = officer.get(f"{PAY}/runs/{RUN}/bank.csv")
check("the bank schedule is not ready before approval", r.status_code == 302)
check("the teacher cannot see her payslip while it is a draft", "No payslips yet" in teacher.text("/admin/my-payslips"))
officer.post(f"{PAY}/runs/{RUN}/approve", {}, page=f"{PAY}/runs/{RUN}")
check("the Payroll Officer cannot approve (no right, and it is theirs)", one("SELECT status FROM payroll_runs WHERE id = :r", r=RUN) == "draft")
bursar.post(f"{PAY}/runs/{RUN}/approve", {}, page="/admin/school")
check("the bursar cannot approve it", one("SELECT status FROM payroll_runs WHERE id = :r", r=RUN) == "draft")
op.post(f"{PAY}/runs/{RUN}/approve", {}, page=f"{PAY}/runs/{RUN}")
check("the School Admin approves it", one("SELECT status FROM payroll_runs WHERE id = :r", r=RUN) == "approved")
officer.post(f"{PAY}/runs/{RUN}/adjust", {"staff_id": BELLO, "kind": "deduction", "label": "Late", "amount": "100"}, page=f"{PAY}/runs/{RUN}")
officer.post(f"{PAY}/runs/{RUN}/refresh", {}, page=f"{PAY}/runs/{RUN}")
check("an approved payroll is locked", one("SELECT gross FROM payroll_payslips WHERE run_id = :r AND staff_id = :s", r=RUN, s=BELLO) == 38_000_000
      and one("SELECT COUNT(*) FROM payroll_adjustments") == 1)

# ================================================================ E. payslips and exports
mine = teacher.text("/admin/my-payslips")
check("the teacher now has My payslips, with her month", "/admin/my-payslips" in teacher.text("/admin/school") and "₦" in mine)
PID = one("SELECT id FROM payroll_payslips WHERE run_id = :r AND staff_id = :s", r=RUN, s=BELLO)
OTHER = one("SELECT id FROM payroll_payslips WHERE run_id = :r AND staff_id = :s", r=RUN, s=TUNDE)
r = teacher.get(f"{PAY}/payslips/{PID}")
check("she opens her own payslip, never cached", r.status_code == 200 and "Net pay" in r.get_data(as_text=True)
      and "no-store" in r.headers.get("Cache-Control", "") and "····6789" in r.get_data(as_text=True)
      and "0123456789" not in r.get_data(as_text=True))
check("she cannot open a colleague's", teacher.get(f"{PAY}/payslips/{OTHER}").status_code in (302, 403))
check("the bursar cannot download the bank schedule", bursar.get(f"{PAY}/runs/{RUN}/bank.csv").status_code in (302, 403))
r = officer.get(f"{PAY}/runs/{RUN}/bank.csv")
body = r.get_data(as_text=True)
check("the Payroll Officer downloads the bank schedule, with the full account numbers, never cached",
      r.status_code == 200 and r.mimetype == "text/csv" and "0123456789" in body and "no-store" in r.headers.get("Cache-Control", ""))
import csv as _csv, io as _io  # noqa: E402
cells = [c for line in _csv.reader(_io.StringIO(body)) for c in line]
check("…with a formula-looking name defused", any(c.startswith("'=cmd") for c in cells)
      and not any(c[:1] in "=+@" for c in cells if c))
returns = op.get(f"{PAY}/runs/{RUN}/returns.csv").get_data(as_text=True)
check("the returns carry PAYE, the pension PIN and the tax ID", "PEN100200300400" in returns and "TIN-55-66" in returns and "45572.00" in returns.replace(",", ""))  # with the taxed arrears: 546,864 a year
check("each download is logged", one("SELECT COUNT(*) FROM audit_logs WHERE action IN ('payroll_bank_schedule_downloaded', 'payroll_returns_downloaded')") == 2)
check("a teacher cannot download them", teacher.get(f"{PAY}/runs/{RUN}/bank.csv").status_code in (302, 403))

# ================================================================ F. reopen, approve again, pay
op.post(f"{PAY}/runs/{RUN}/reopen", {}, page=f"{PAY}/runs/{RUN}")
check("the School Admin reopens it as a draft", one("SELECT status FROM payroll_runs WHERE id = :r", r=RUN) == "draft")
check("…and staff stop seeing it", teacher.get(f"{PAY}/payslips/{PID}").status_code in (302, 403))
op.post(f"{PAY}/runs/{RUN}/approve", {}, page=f"{PAY}/runs/{RUN}")
check("whoever reopened it cannot approve it again", one("SELECT status FROM payroll_runs WHERE id = :r", r=RUN) == "draft")

# unpaid leave, taken off pay when the school says so
sql("UPDATE payroll_settings SET deduct_unpaid_leave = 1")
first = TODAY.replace(day=1)
while first.isoweekday() > 5:
    first += timedelta(days=1)
UNPAID = one("SELECT id FROM hr_leave_types WHERE name = 'Unpaid leave'")
hro.post(f"/admin/hr/{BELLO}/leave", {"leave_type_id": UNPAID, "start_date": first.isoformat(), "end_date": first.isoformat()}, page=f"/admin/hr/{BELLO}?tab=leave")
officer.post(f"{PAY}/runs/{RUN}/refresh", {}, page=f"{PAY}/runs/{RUN}")
lines = json.loads(one("SELECT lines_json FROM payroll_payslips WHERE run_id = :r AND staff_id = :s", r=RUN, s=BELLO))
check("a day of approved unpaid leave is taken off", any(l["label"] == "Unpaid leave (1 day)" and l["amount"] < 0 for l in lines))

op.post(f"{PAY}/runs/{RUN}/approve", {}, page=f"{PAY}/runs/{RUN}")
check("the School Admin approves it", one("SELECT status FROM payroll_runs WHERE id = :r", r=RUN) == "approved")
officer.post(f"{PAY}/runs/{RUN}/paid", {}, page=f"{PAY}/runs/{RUN}")
check("the Payroll Officer marks it paid", one("SELECT status FROM payroll_runs WHERE id = :r", r=RUN) == "paid")
op.post(f"{PAY}/runs/{RUN}/reopen", {}, page=f"{PAY}/runs/{RUN}")
officer.post(f"{PAY}/runs/{RUN}/cancel", {}, page=f"{PAY}/runs/{RUN}")
check("a paid payroll is final: it cannot be reopened or cancelled", one("SELECT status FROM payroll_runs WHERE id = :r", r=RUN) == "paid")

# ================================================================ G. the key can be rotated
with A.app.app_context():
    from control_plane.context import tenant_context
    with tenant_context(INFO):
        from core import payroll as P
        from core.secrets_rotation import rotate_one_tenant
        from models import StaffPay, db
        old = os.environ.get("BEACON_DELIVERY_KEY", "").strip() or (A.app.secret_key if isinstance(A.app.secret_key, str) else A.app.secret_key.decode())
        rotated, skipped = rotate_one_tenant(old, "n" * 48)
        os.environ["BEACON_DELIVERY_KEY"] = "n" * 48
        acct = P.decrypt(db.session.scalars(sa.select(StaffPay).where(StaffPay.staff_id == BELLO)).first().account_number_enc)
        db.session.remove()
check("rotating the key re-encrypts the bank numbers, PINs and tax IDs, and they still read", rotated >= 6 and not skipped and acct == "0123456789",
      f"{rotated} {skipped} {acct}")

# ================================================================ H. the approver sees what changed since last month
NEXT = (TODAY.replace(day=28) + timedelta(days=5)).strftime("%Y-%m")
op.post(f"{PAY}/staff/{BELLO}", {"on_payroll": "1", "basic": "400000", "bank_name": "GTBank"}, page=f"{PAY}/staff/{BELLO}")
officer.post(f"{PAY}/runs", {"period": NEXT}, page=PAY)
RUN2 = one("SELECT id FROM payroll_runs WHERE period = :p", p=NEXT)
page = op.text(f"{PAY}/runs/{RUN2}")
check("the next month shows each net pay against the last paid month", "Change from" in page and "+₦" in page)

check("no page answered with a server error", not PROBLEMS, "; ".join(PROBLEMS[:5]))
failed = [name for name, ok, _ in results if not ok]
print()
print(f"{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", "; ".join(failed))
    raise SystemExit(1)
