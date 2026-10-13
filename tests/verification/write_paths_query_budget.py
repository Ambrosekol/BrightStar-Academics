"""Query budgets: how many database queries each staff page of the newer areas may make, with a school of 70 staff,
proven on PostgreSQL. With tens of thousands of schools on one database cluster, a page that reads people one by one
(a query per person) is what overloads it; each page here must stay within its budget however many staff there are.

A. Warm pages (the second view): staff records, leave, attendance, payroll, ID cards, Clock in, My leave, My payslips.
B. Working out a payroll again. C. A page right after a write elsewhere in the school.
D. The same pages with twice as many staff take no more queries (nothing grows with the number of people).
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
TMP = tempfile.mkdtemp(prefix="brightstars_qbudget_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup("qbudget")
atexit.register(DROP_TEST_DATABASES)
atexit.register(shutil.rmtree, TMP, True)
os.environ.update({
    "BRIGHTSTARS_TENANTS_DIR": os.path.join(TMP, "tenants"),
    "BRIGHTSTARS_PLATFORM_HOSTS": "platform.test",
    "BRIGHTSTARS_PORTAL_DOMAIN": "portal.test",
    "BRIGHTSTARS_REGISTRY_CACHE_SECONDS": "0",
    "BRIGHTSTARS_SECRET": "x" * 40,
    "BRIGHTSTARS_ENV": "development",
    # Counts and permissions are kept for their usual few seconds in production; here for the whole run, so a slow
    # machine never sees one run out between the warm-up and the measured view and count it as a page's own query.
    "BRIGHTSTARS_BADGE_CACHE_SECONDS": "3600",
    "BRIGHTSTARS_ADMIN_CACHE_SECONDS": "3600",
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
import io
from datetime import date, timedelta  # noqa: E402
T = date.today()
role = {name: one("SELECT id FROM admin_types WHERE name = :n", n=name) for name in ("HR Officer", "Subject Teacher", "Head Teacher")}


def new_staff(username, display, role_name, extra=None):
    payload = {"username": username, "display_name": display, "admin_type_ids": [str(role[role_name])],
               "duty_class": [""], "duty_subject": [""], "duty_department": [""], "phone": "0803 555 0101", **(extra or {})}
    op.post("/admin/administration/admins/new", payload, page="/admin/administration/admins/new")
    aid = one("SELECT id FROM admins WHERE username = :u", u=username)
    sql("UPDATE admins SET password_hash = :h, password_must_change = 0 WHERE id = :i", h=HASH, i=aid)
    person = Person(username)
    person.post("/login", {"username": username, "password": PASSWORD}, page="/login")
    person.get("/admin/workspace/school")
    return person, aid


officer, OFFICER = new_staff("hr.officer", "Mrs Funmi Bello", "HR Officer")
op.post("/admin/school/subjects/new", {"name": "Mathematics", "code": "MTH", "class_ids": [J1]}, page="/admin/school/subjects/new")
MATHS = one("SELECT id FROM school_subjects WHERE name = 'Mathematics'")
teacher, TEACHER = new_staff("mr.okon", "Mr Emeka Okon", "Subject Teacher", {"duty_class": [str(J1)], "duty_subject": [str(MATHS)]})
head, HEAD = new_staff("head.teacher", "Dr Ngozi Adeyemi", "Head Teacher")
new_staff("mrs.audu", "Mrs Halima Audu", "Subject Teacher")
officer.post("/admin/hr/from-accounts", {}, page="/admin/hr")
from datetime import date, timedelta
T = date.today()
MON = T + timedelta(days=7 - T.weekday())
D = lambda n: (MON + timedelta(days=n)).isoformat()
OKON = one("SELECT id FROM hr_staff WHERE admin_id = :a", a=TEACHER)
officer.post(f"/admin/hr/{OKON}/edit", {"title": "Mr", "first_name": "Emeka", "middle_name": "Chukwudi", "last_name": "Okon", "gender": "Male",
    "date_of_birth": "1988-04-12", "marital_status": "Married", "state_of_origin": "Anambra", "phone": "0803 555 0101", "email": "emeka.okon@example.com",
    "address": "14 Allen Avenue, Ikeja, Lagos", "staff_no": "BSA/T/014", "job_title": "Mathematics teacher", "department": "Sciences",
    "category": "teaching", "employment_type": "full_time", "employment_date": "2019-09-09", "confirmation_date": "2020-09-01", "status": "active",
    "admin_id": str(TEACHER), "nok_name": "Mrs Ada Okon", "nok_relationship": "Spouse", "nok_phone": "0805 222 3344",
    "nok_address": "14 Allen Avenue, Ikeja, Lagos", "notes": "Strong results in WAEC 2025. Asked about a study leave for an M.Ed."}, page=f"/admin/hr/{OKON}/edit")
officer.post("/admin/hr/new", {"first_name": "Musa", "last_name": "Ibrahim", "title": "Mr", "category": "non_teaching", "employment_type": "full_time",
    "status": "active", "job_title": "Driver", "department": "Transport", "phone": "0803 111 2222", "employment_date": "2021-01-11", "staff_no": "BSA/N/003"}, page="/admin/hr/new")
officer.post("/admin/hr/new", {"first_name": "Grace", "last_name": "Eze", "title": "Mrs", "category": "non_teaching", "employment_type": "part_time",
    "status": "left", "exit_date": "2026-07-31", "exit_reason": "resigned", "job_title": "Cleaner", "employment_date": "2018-02-05"}, page="/admin/hr/new")
for q, i, y in (("B.Sc. (Ed.) Mathematics", "University of Nigeria, Nsukka", "2011"), ("TRCN registration", "Teachers Registration Council", "2013")):
    officer.post(f"/admin/hr/{OKON}/qualifications", {"qualification": q, "institution": i, "year": y}, page=f"/admin/hr/{OKON}?tab=files")
token = token_in(officer.text(f"/admin/hr/{OKON}?tab=files"))
officer.c.post(f"/admin/hr/{OKON}/documents", data={"_csrf_token": token, "label": "Appointment letter", "category": "appointment",
    "document": (io.BytesIO(b"%PDF-1.4" + bytes([10]) + b"%%EOF" + bytes([10]) + b" " * 40000), "appointment.pdf")}, base_url=UA, content_type="multipart/form-data")
for kind, when, summary, extra in (("appointment", "2019-09-09", "Appointed Mathematics teacher", {}),
                                   ("confirmation", "2020-09-01", "Confirmed after probation", {}),
                                   ("query", "2024-03-14", "Queried for lateness", {"detail": "Late three times in one week."}),
                                   ("promotion", "2025-09-01", "", {"to_value": "Senior Mathematics teacher"}),
                                   ("commendation", "2025-11-20", "Commended for WAEC results", {})):
    officer.post(f"/admin/hr/{OKON}/career", {"kind": kind, "effective_date": when, "summary": summary, **extra}, page=f"/admin/hr/{OKON}?tab=career")
q = one("SELECT id FROM hr_career_events WHERE kind = 'query'")
officer.post(f"/admin/hr/career/{q}/strike", {"reason": "Recorded against the wrong teacher"}, page=f"/admin/hr/{OKON}?tab=career")
ANNUAL = one("SELECT id FROM hr_leave_types WHERE name = 'Annual leave'")
CASUAL = one("SELECT id FROM hr_leave_types WHERE name = 'Casual leave'")
SICK = one("SELECT id FROM hr_leave_types WHERE name = 'Sick leave'")
teacher.post("/admin/my-leave", {"leave_type_id": CASUAL, "start_date": (T - timedelta(days=20)).isoformat(), "end_date": (T - timedelta(days=19)).isoformat(), "reason": "Family wedding"}, page="/admin/my-leave")
officer.post(f"/admin/hr/leave/{one('SELECT max(id) FROM hr_leave_requests')}/decide", {"decision": "approve"}, page="/admin/hr/leave")
teacher.post("/admin/my-leave", {"leave_type_id": ANNUAL, "start_date": D(7), "end_date": D(11), "cover": "Mrs Audu", "reason": "Travelling home"}, page="/admin/my-leave")
MUSA = one("SELECT id FROM hr_staff WHERE first_name = 'Musa'")
officer.post(f"/admin/hr/{MUSA}/leave", {"leave_type_id": SICK, "start_date": (T - timedelta(days=1)).isoformat(), "end_date": D(1), "reason": "Malaria"}, page=f"/admin/hr/{MUSA}?tab=leave")
officer.post("/admin/my-leave", {"leave_type_id": CASUAL, "start_date": D(2), "end_date": D(2), "reason": "Bank appointment"}, page="/admin/my-leave")

for rn in ("Payroll Officer", "Finance Manager"):
    role[rn] = one("SELECT id FROM admin_types WHERE name = :n", n=rn)
pay_off, PAYOFF = new_staff("pay.officer", "Mr Tunde Adebayo", "Payroll Officer")
bursar, BURSAR = new_staff("bursar", "Mrs Ngozi Eze", "Finance Manager")
officer.post("/admin/hr/from-accounts", {}, page="/admin/hr")
ITEM = {name: i for i, name in sql("SELECT id, name FROM payroll_items")}
sql("UPDATE payroll_settings SET checked_by = 'Ade & Co, Chartered Accountants', checked_on = :d", d=T.isoformat()) if one("SELECT COUNT(*) FROM payroll_settings") else None
pay_off.get("/admin/payroll")
sql("UPDATE payroll_settings SET checked_by = 'Ade & Co, Chartered Accountants', checked_on = :d", d=T.isoformat())
banks = ["GTBank", "Access Bank", "Zenith Bank", "UBA", "First Bank", "Moniepoint", "OPay"]
for n, (sid,) in enumerate(sql("SELECT id FROM hr_staff WHERE status <> 'left' ORDER BY id")):
    data = {"on_payroll": "1", "basic": str(120000 + 35000 * n), "bank_name": banks[n % len(banks)], "account_number": f"01234{n:05d}",
            "account_name": "", "pfa": "Stanbic IBTC Pension" if n % 2 else "ARM Pension", "rsa_pin": f"PEN1002003{n:05d}",
            f"item_{ITEM['Housing allowance']}": "1", f"item_amount_{ITEM['Housing allowance']}": str(30000 + 5000 * n),
            f"item_{ITEM['Transport allowance']}": "1", f"item_amount_{ITEM['Transport allowance']}": "15000"}
    if n % 3 == 0:
        data.update({f"item_{ITEM['Cooperative']}": "1", f"item_amount_{ITEM['Cooperative']}": "5000"})
    if n == 4:
        data.pop("account_number")
    op.post(f"/admin/payroll/staff/{sid}", data, page=f"/admin/payroll/staff/{sid}")
prev = (T.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
pay_off.post("/admin/payroll/runs", {"period": prev}, page="/admin/payroll")
R1 = one("SELECT id FROM payroll_runs WHERE period = :p", p=prev)
bursar.post(f"/admin/payroll/runs/{R1}/approve", {}, page=f"/admin/payroll/runs/{R1}")
pay_off.post(f"/admin/payroll/runs/{R1}/paid", {}, page=f"/admin/payroll/runs/{R1}")
pay_off.post("/admin/payroll/runs", {"period": T.strftime("%Y-%m")}, page="/admin/payroll")
R2 = one("SELECT id FROM payroll_runs WHERE period = :p", p=T.strftime("%Y-%m"))
pay_off.post(f"/admin/payroll/runs/{R2}/adjust", {"staff_id": OKON, "kind": "earning", "label": "Arrears (September)", "amount": "12500", "taxable": "1"}, page=f"/admin/payroll/runs/{R2}")
pay_off.post(f"/admin/payroll/runs/{R2}/adjust", {"staff_id": MUSA, "kind": "deduction", "label": "Salary advance", "amount": "20000"}, page=f"/admin/payroll/runs/{R2}")

# ---------------------------------------------------------------- more people, so per-person queries show
from core import staff_attendance as _sat
from werkzeug.security import generate_password_hash as _gph
EXTRA = int(os.environ.get('MEASURE_EXTRA', '60'))
now_iso = datetime.now(timezone.utc).isoformat() if 'datetime' in dir() else T.isoformat()
role_sub = one("SELECT id FROM admin_types WHERE name = 'Subject Teacher'")
for n in range(EXTRA):
    aid = one("INSERT INTO admins (username, display_name, password_hash, admin_type_id, active, created_at, password_must_change) "
              "VALUES (:u, :d, :h, :t, 1, :c, 0) RETURNING id", u=f'extra{n}', d=f'Extra Teacher {n}', h=HASH, t=role_sub, c=T.isoformat())
    sid = one("INSERT INTO hr_staff (admin_id, first_name, last_name, category, employment_type, status, created_at) "
              "VALUES (:a, :f, :l, 'teaching', 'full_time', 'active', :c) RETURNING id", a=aid, f=f'Extra{n}', l=f'Teacher{n}', c=T.isoformat())
    sql("INSERT INTO payroll_staff (staff_id, on_payroll, basic, annual_rent, paye_exempt, pension_exempt, nhf_exempt) VALUES (:s, 1, :b, 0, 0, 0, 0)",
        s=sid, b=10_000_000 + n * 100_000)
    sql("INSERT INTO payroll_staff_items (staff_id, item_id, amount) VALUES (:s, :i, 2000000)", s=sid, i=ITEM['Housing allowance'])
    sql("INSERT INTO hr_leave_requests (staff_id, leave_type_id, start_date, end_date, days, status, requested_by, requested_at) "
        "VALUES (:s, (SELECT id FROM hr_leave_types WHERE name='Casual leave'), :d, :d, 1, 'pending', :a, :c)",
        s=sid, d=(T + timedelta(days=20 + n % 5)).isoformat(), a=aid, c=T.isoformat())
    for k in range(1, 20):
        d = T - timedelta(days=k)
        if d.isoweekday() <= 5:
            sql("INSERT INTO staff_attendance_records (staff_id, day, clock_in, late, in_method, created_at) VALUES (:s, :d, '07:50', 0, 'phone', :c)",
                s=sid, d=d.isoformat(), c=T.isoformat())
officer.post("/admin/settings/staff-attendance", {"enabled": "1", "method": "qr_gps", "latitude": "6.5244", "longitude": "3.3792", "radius_m": "150",
    "max_accuracy_m": "100", "late_after": "08:00", **{f"day_{d}": "1" for d in "12345"}}, page="/admin/settings/staff-attendance")
sql("UPDATE staff_attendance_settings SET started_on = :d", d=(T - timedelta(days=25)).isoformat())
pay_off.post(f"/admin/payroll/runs/{R2}/refresh", {}, page=f"/admin/payroll/runs/{R2}")


# ---------------------------------------------------------------- count
seen = []


@sa.event.listens_for(sa.engine.Engine, "before_cursor_execute")
def _log(conn, cursor, statement, params, context, executemany):
    seen.append(statement)


EXTRA_SLIP = one("SELECT id FROM payroll_payslips WHERE run_id = :r LIMIT 1", r=R2)
PAGES = [
    ("teacher", teacher, "/admin/my-leave", 14), ("teacher", teacher, "/admin/clock", 12), ("teacher", teacher, "/admin/my-payslips", 6),
    ("hr", officer, "/admin/hr", 10), ("hr", officer, f"/admin/hr/{OKON}", 10), ("hr", officer, f"/admin/hr/{OKON}?tab=leave", 15),
    ("hr", officer, f"/admin/hr/{OKON}?tab=career", 10), ("hr", officer, f"/admin/hr/{OKON}?tab=files", 10),
    ("hr", officer, "/admin/hr/leave", 12), ("hr", officer, "/admin/staff-attendance", 11),
    ("hr", officer, "/admin/staff-attendance/month", 11), ("hr", officer, "/admin/settings/staff-attendance", 9),
    ("pay", pay_off, "/admin/payroll", 12), ("pay", pay_off, f"/admin/payroll/runs/{R2}", 12), ("pay", pay_off, "/admin/payroll/staff", 8),
    ("pay", pay_off, f"/admin/payroll/staff/{OKON}", 11), ("pay", pay_off, f"/admin/payroll/payslips/{EXTRA_SLIP}", 8),
    ("op", op, "/admin/id-cards?kind=staff", 7), ("op", op, "/admin/id-cards/design/student", 10),
]


def measure():
    out = {}
    for who, person, path, budget in PAGES:
        for _ in range(2):
            person.get(path)
        seen.clear()
        r = person.get(path)
        out[path] = (len(seen), r.status_code)
    return out


first = measure()
for who, person, path, budget in PAGES:
    count, status = first[path]
    check(f"{path} ({who}) makes at most {budget} queries", status == 200 and count <= budget, f"{count} queries, status {status}")
seen.clear()
pay_off.post(f"/admin/payroll/runs/{R2}/refresh", {}, page=f"/admin/payroll/runs/{R2}")
check("working out a payroll of 70 people again stays within 30 queries", len(seen) <= 30, f"{len(seen)} queries")
seen.clear()
teacher.get("/admin/my-payslips")
check("a page right after another member of staff's write stays within 30 queries", len(seen) <= 30, f"{len(seen)} queries")

# D. twice as many people: no page may grow
for n in range(EXTRA, EXTRA * 2):
    aid = one("INSERT INTO admins (username, display_name, password_hash, admin_type_id, active, created_at, password_must_change) "
              "VALUES (:u, :d, :h, :t, 1, :c, 0) RETURNING id", u=f"extra{n}", d=f"Extra Teacher {n}", h=HASH, t=role_sub, c=T.isoformat())
    sid = one("INSERT INTO hr_staff (admin_id, first_name, last_name, category, employment_type, status, created_at) "
              "VALUES (:a, :f, :l, 'teaching', 'full_time', 'active', :c) RETURNING id", a=aid, f=f"Extra{n}", l=f"Teacher{n}", c=T.isoformat())
    sql("INSERT INTO payroll_staff (staff_id, on_payroll, basic, annual_rent, paye_exempt, pension_exempt, nhf_exempt) VALUES (:s, 1, 10000000, 0, 0, 0, 0)", s=sid)
    sql("INSERT INTO hr_leave_requests (staff_id, leave_type_id, start_date, end_date, days, status, requested_by, requested_at) "
        "VALUES (:s, (SELECT id FROM hr_leave_types WHERE name='Casual leave'), :d, :d, 1, 'pending', :a, :c)",
        s=sid, d=(T + timedelta(days=21)).isoformat(), a=aid, c=T.isoformat())
pay_off.post(f"/admin/payroll/runs/{R2}/refresh", {}, page=f"/admin/payroll/runs/{R2}")
second = measure()
grew = [f"{p}: {first[p][0]} -> {second[p][0]}" for p in first if second[p][0] > first[p][0]]
check("with twice as many staff, no page makes more queries", not grew, "; ".join(grew))

check("no page answered with a server error", not PROBLEMS, "; ".join(PROBLEMS[:5]))
failed = [name for name, ok, _ in results if not ok]
print()
print(f"{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", "; ".join(failed))
    raise SystemExit(1)
