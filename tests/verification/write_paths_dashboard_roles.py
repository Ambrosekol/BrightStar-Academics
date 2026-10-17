"""The staff Overview for every kind of staff role, proven end to end on PostgreSQL. In plain words:

A. Every starter role opens the Overview without an error; an entrance-only role has no School Portal and opens the
   entrance Overview instead, which shows the admissions waitlist to whoever decides admissions, registrations this
   week and papers in progress.
B. Staff today (HR officer, head teacher): who is in, who is not in yet, who is on leave, and leave waiting for a decision;
   a subject teacher sees none of it.
C. Payroll: the payroll officer sees this month's payroll and the payslips to check; the School Admin also sees what
   waits for their approval; nobody else sees payroll, the bursar (Finance Manager) included.
D. Report cards: the report card officer sees each class's cards ready and comments written, and whether the head's
   signature is set; a class teacher sees their own class instead, with its birthdays this week.
E. A school whose Finance Manager role had payroll permissions loses them on its next start; a payroll permission given
   to one person personally is kept.
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
TMP = tempfile.mkdtemp(prefix="beacon_dashroles_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('dashroles')
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
from core.security import ADMIN_ROLE_PRESETS  # noqa: E402

results, PROBLEMS = [], []
PL = "http://platform.test"
UA = "http://alpha.portal.test"


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


def token_in(body):
    found = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', body) or re.search(r'id="csrfToken" value="([^"]+)"', body)
    return found.group(1) if found else ""


class Person:
    count = [0]

    def __init__(self, label):
        self.c, self.label = A.app.test_client(), label
        Person.count[0] += 1
        self.addr = f"10.96.{Person.count[0]}.1"

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
op.post("/admin/school/subjects/new", {"name": "Mathematics", "code": "MTH", "class_ids": [J1]}, page="/admin/school/subjects/new")
MATHS = one("SELECT id FROM school_subjects WHERE name = 'Mathematics'")


def new_staff(username, role_name, extra=None):
    role = one("SELECT id FROM admin_types WHERE name = :n", n=role_name)
    payload = {"username": username, "display_name": username.replace(".", " ").title(), "admin_type_ids": [str(role)],
               "duty_class": [""], "duty_subject": [""], "duty_department": [""], **(extra or {})}
    op.post("/admin/administration/admins/new", payload, page="/admin/administration/admins/new")
    aid = one("SELECT id FROM admins WHERE username = :u", u=username)
    sql("UPDATE admins SET password_hash = :h, password_must_change = 0 WHERE id = :i", h=HASH, i=aid)
    person = Person(username)
    person.post("/login", {"username": username, "password": PASSWORD}, page="/login")
    person.get("/admin/workspace/school")
    return person, aid


staff = {}
for n, role_name in enumerate(ADMIN_ROLE_PRESETS):
    extra = {}
    if role_name == "Class Teacher":
        extra = {"duty_class_teacher": [str(J1)]}
    elif role_name == "Subject Teacher":
        extra = {"duty_class": [str(J1)], "duty_subject": [str(MATHS)]}
    staff[role_name] = new_staff(f"role{n}.person", role_name, extra)

# The school's day: a student whose birthday is today, staff attendance switched on, a member of staff in, one on leave,
# one asking for leave, and this month's payroll prepared with a payslip that needs a look.
with A.app.app_context(), tenant_context(INFO):
    from core.staff_attendance import school_now
    TODAY = school_now().date()
NOW = TODAY.isoformat() + "T08:00:00+00:00"
school_id = one("SELECT id FROM schools LIMIT 1")
ADA = one("INSERT INTO students (admission_no, student_number, first_name, last_name, created_at, active, school_id, gender, "
          "date_of_birth, account_active, password_must_change) VALUES ('DR-1', 'DR-1', 'Ada', 'Okafor', :n, 1, :s, 'Female', :d, 1, 0) "
          "RETURNING id", n=NOW, s=school_id, d=f"{TODAY.day:02d}/{TODAY.month:02d}/2013")
sql("INSERT INTO student_enrolments (student_id, class_id, session_id, enrolled_at, active) VALUES (:a, :c, :s, :n, 1)",
    a=ADA, c=J1, s=SESSION, n=NOW)
HR = {role: one("INSERT INTO hr_staff (admin_id, first_name, last_name, category, employment_type, status, created_at) "
                "VALUES (:a, :f, 'Staff', :c, 'full_time', 'active', :n) RETURNING id", a=aid, f=role.split()[0],
                c='teaching' if 'Teacher' in role else 'non_teaching', n=NOW)
      for role, (_p, aid) in staff.items()}
sql("INSERT INTO staff_attendance_settings (enabled, method, radius_m, max_accuracy_m, late_after, work_days, started_on, updated_at) "
    "VALUES (1, 'qr', 150, 100, '23:59', '1234567', :d, :n)", d=TODAY.isoformat(), n=NOW)
sql("INSERT INTO staff_attendance_records (staff_id, day, clock_in, late, in_method, created_at) VALUES (:s, :d, '07:40', 0, 'register', :n)",
    s=HR["Librarian"], d=TODAY.isoformat(), n=NOW)
LEAVE_TYPE = one("SELECT id FROM hr_leave_types ORDER BY id LIMIT 1") or one(
    "INSERT INTO hr_leave_types (name, days_per_year, paid, active, sort_order, created_at) VALUES ('Annual leave', 20, 1, 1, 0, :n) RETURNING id", n=NOW)
sql("INSERT INTO hr_leave_requests (staff_id, leave_type_id, start_date, end_date, days, status, requested_at) "
    "VALUES (:s, :t, :d, :d, 1, 'approved', :n)", s=HR["Store Keeper"], t=LEAVE_TYPE, d=TODAY.isoformat(), n=NOW)
sql("INSERT INTO hr_leave_requests (staff_id, leave_type_id, start_date, end_date, days, status, requested_at, reason) "
    "VALUES (:s, :t, :d, :d, 1, 'pending', :n, 'A family wedding')", s=HR["Subject Teacher"], t=LEAVE_TYPE, d=TODAY.isoformat(), n=NOW)
RUN = one("INSERT INTO payroll_runs (period, status, gross, deductions, net, employer_pension, headcount, prepared_at) "
          "VALUES (:p, 'draft', 60000000, 10000000, 50000000, 0, 2, :n) RETURNING id", p=TODAY.strftime('%Y-%m'), n=NOW)
sql("INSERT INTO payroll_payslips (run_id, staff_id, name, lines_json, basic, gross, deductions, net, warnings) "
    "VALUES (:r, :s, 'Store Keeper', '[]', 0, 0, 0, 0, 'No bank details')", r=RUN, s=HR["Store Keeper"])
from core.short_cache import clear  # noqa: E402
clear()


def overview(role):
    person = staff[role][0]
    r = person.get("/admin/school")
    return r, r.get_data(as_text=True)


# ================================================================ A. every role
ENTRANCE_ONLY = [role for role, spec in ADMIN_ROLE_PRESETS.items() if "school.view" not in spec["permissions"]]
for role in ADMIN_ROLE_PRESETS:
    if role in ENTRANCE_ONLY:
        staff[role][0].get("/admin/workspace/entrance")
        r = staff[role][0].get("/admin/examination")
        check(f"A. {role}: has no School Portal, and its entrance Overview opens", r.status_code == 200
              and staff[role][0].get("/admin/school").status_code in (302, 403), str(r.status_code))
    else:
        r, body = overview(role)
        check(f"A. {role}: the Overview opens without an error", r.status_code == 200, str(r.status_code))
entrance = staff["Admissions Officer"][0].text("/admin/examination")
check("A. the admissions officer's entrance Overview has the admissions waitlist, registrations this week and papers in progress",
      "enAdm" in entrance and "Waiting for a decision" in entrance and "Registered this week" in entrance and "Sitting a paper now" in entrance)
check("A. an entrance role without the right to admit has no admissions panel",
      "enAdm" not in staff["Question Bank Manager"][0].text("/admin/examination"))
r, body = op.get("/admin/school"), op.text("/admin/school")
check("A. the School Admin's Overview opens, with staff today and payroll on it",
      r.status_code == 200 and "Staff today" in body and "Payroll" in body)

# ================================================================ B. staff today
_, hro = overview("HR Officer")
check("B. the HR officer sees staff today: who is in, who is not in yet, who is on leave",
      "Staff today" in hro and "In today" in hro and "Not in yet" in hro and "On leave today" in hro)
check("…the staff count and the leave waiting for a decision, with its details",
      "Leave to decide" in hro and "Leave waiting for a decision" in hro and "Annual leave" in hro)
_, head = overview("Head Teacher")
check("B. the head teacher sees staff today and the leave to decide", "Staff today" in head and "Leave to decide" in head)
_, subj = overview("Subject Teacher")
check("B. a subject teacher sees none of it, nor payroll", "ovStaff" not in subj and "ovPayroll" not in subj)

# ================================================================ C. payroll
_, pay = overview("Payroll Officer")
check("C. the payroll officer sees this month's payroll, waiting for approval, and the payslip to check",
      "ovPayroll" in pay and "Prepared, waiting for approval" in pay and "Payslips to check" in pay
      and "Waiting for your approval" not in pay)
_, bursar = overview("Finance Manager")
check("C. the bursar has no payroll (money, not staff pay)", "ovPayroll" not in bursar and "Collections" in bursar)
check("C. the School Admin sees the payroll waiting for their approval", "Waiting for your approval" in op.text("/admin/school"))
_, hro_again = overview("HR Officer")
check("C. nobody else sees payroll (the HR officer, the librarian)", "ovPayroll" not in hro_again and "ovPayroll" not in overview("Librarian")[1])

# ================================================================ D. report cards and the class teacher
_, rco = overview("Report Card Officer")
check("D. the report card officer sees each class's cards and comments, and the head's signature",
      "ovCards" in rco and "Comments written" in rco and "JSS 1" in rco and "Head&#39;s signature" in rco)
_, ct = overview("Class Teacher")
check("D. a class teacher sees their class with its birthdays this week, not the report card officer's panel",
      "Birthdays this week" in ct and "Ada Okafor" in ct and "Today" in ct and "ovCards" not in ct)

# ================================================================ E. a school that had given its bursar payroll loses it on its next start
FM = one("SELECT id FROM admin_types WHERE name = 'Finance Manager'")
for code in ("payroll.view", "payroll.approve"):
    sql("INSERT INTO admin_type_permissions (admin_type_id, permission_id, granted_at) SELECT :r, id, :n FROM permissions "
        "WHERE code = :c ON CONFLICT DO NOTHING", r=FM, c=code, n=NOW)
sql("INSERT INTO admin_permissions (admin_id, permission_id, granted_at) SELECT :a, id, :n FROM permissions "
    "WHERE code = 'payroll.view' ON CONFLICT DO NOTHING", a=staff["Librarian"][1], n=NOW)
with A.app.app_context(), tenant_context(INFO):
    A.init_admin_security()
    A.db.session.commit()
    A.db.session.remove()
held = {c for (c,) in sql("SELECT p.code FROM admin_type_permissions tp JOIN permissions p ON p.id = tp.permission_id "
                          "WHERE tp.admin_type_id = :r", r=FM)}
check("E. on the next start, the Finance Manager role carries no payroll permission, whatever the school had given it",
      not {c for c in held if c.startswith("payroll.")} and "finance.manage" in held, str(sorted(held)))
check("E. …while a payroll permission the School Admin gave one person personally is kept",
      one("SELECT COUNT(*) FROM admin_permissions ap JOIN permissions p ON p.id = ap.permission_id WHERE ap.admin_id = :a "
          "AND p.code = 'payroll.view'", a=staff["Librarian"][1]) == 1)

check("no page answered with a server error", not PROBLEMS, "; ".join(PROBLEMS[:5]))
failed = [name for name, ok, _ in results if not ok]
print()
print(f"{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", "; ".join(failed))
    raise SystemExit(1)
