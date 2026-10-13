"""Staff attendance, proven end to end on PostgreSQL. In plain words:

A. Setting up: only those allowed see it; it cannot be turned on with location checks before the school's location is
   set; impossible settings are refused; the clock page may ask for the location and nothing else.
B. The screen: its link is shown once and only its hash kept; it opens with no sign-in, never cached or indexed, and
   shows the code the server makes; a wrong link shows nothing.
C. Clocking in: a wrong code, an old code, a phone 8 km away, a vague or nonsensical position are refused, and none
   registers a phone; the right code at the school clocks in, registers the phone by a hash, and keeps no position;
   twice is refused; another phone cannot clock for them; clocking out; lateness; too many tries are stopped; the
   code-only and location-only ways work.
D. The register: who is in; marking by hand needs a note, never overwrites a phone's time, and is refused to a teacher.
E. Corrections: asked for, managers told, decided only by them, recorded as corrected, the person told.
F. The month: leave shows as leave, a closed day as closed, a past work day with nothing as absent; the CSV defuses
   a formula-looking name, is never cached and is logged.
G. A phone reset lets a new phone become theirs; a new screen link ends the old one and its codes.
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
TMP = tempfile.mkdtemp(prefix="brightstars_staffatt_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup("staffatt")
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


def one_platform(statement, **params):
    with platform_session() as session:
        row = session.execute(sa.text(statement), params).first()
        return row[0] if row else None


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
role = {name: one("SELECT id FROM admin_types WHERE name = :n", n=name) for name in ("HR Officer", "Subject Teacher", "Head Teacher")}


def new_staff(username, role_name, extra=None):
    payload = {"username": username, "display_name": username.replace(".", " ").title(), "admin_type_ids": [str(role[role_name])],
               "duty_class": [""], "duty_subject": [""], "duty_department": [""], **(extra or {})}
    op.post("/admin/administration/admins/new", payload, page="/admin/administration/admins/new")
    aid = one("SELECT id FROM admins WHERE username = :u", u=username)
    sql("UPDATE admins SET password_hash = :h, password_must_change = 0 WHERE id = :i", h=HASH, i=aid)
    person = Person(username)
    person.post("/login", {"username": username, "password": PASSWORD}, page="/login")
    person.get("/admin/workspace/school")
    return person, aid


def login_again(username):
    person = Person(username + " (another phone)")
    person.post("/login", {"username": username, "password": PASSWORD}, page="/login")
    person.get("/admin/workspace/school")
    return person


officer, OFFICER = new_staff("hr.officer", "HR Officer")
teacher, TEACHER = new_staff("mrs.bello", "Subject Teacher")
other, OTHER = new_staff("mr.okon", "Subject Teacher")
head, HEAD = new_staff("head.teacher", "Head Teacher")
officer.post("/admin/hr/from-accounts", {}, page="/admin/hr")
officer.post("/admin/hr/new", {"first_name": "Musa", "last_name": "=HYPERLINK(\"http://x\")", "category": "non_teaching",
                               "employment_type": "full_time", "status": "active", "job_title": "Driver"}, page="/admin/hr/new")
BELLO = one("SELECT id FROM hr_staff WHERE admin_id = :a", a=TEACHER)
MUSA = one("SELECT id FROM hr_staff WHERE first_name = 'Musa'")

import json  # noqa: E402,F811
from datetime import date, datetime, timedelta, timezone  # noqa: E402
with A.app.app_context():
    from core import staff_attendance as sat
    TODAY = sat.school_now().date()
SCHOOL = (6.5244, 3.3792)
NEAR = {"lat": "6.5250", "lng": "3.3795", "acc": "25"}          # about 70 m away
FAR = {"lat": "6.6000", "lng": "3.3800", "acc": "20"}           # about 8 km away
VAGUE = {"lat": "6.5250", "lng": "3.3795", "acc": "450"}
SET = "/admin/staff-attendance/settings"
CLOCK = "/admin/clock"


def settings_form(**over):
    form = {"method": "qr_gps", "latitude": "", "longitude": "", "radius_m": "150", "max_accuracy_m": "100", "late_after": "23:59",
            **{f"day_{d}": "1" for d in "12345"}}
    form.update(over)
    return form


def digits(window_shift=0):
    secret = one("SELECT code_secret FROM staff_attendance_settings")
    return sat.code_for(secret, sat.window_of() + window_shift)


def punch(person, action, code=None, where=NEAR):
    data = {"action": action, **(where or {})}
    if code is not None:
        data["code"] = code
    r = person.post(CLOCK, data, page=CLOCK)
    try:
        return r.status_code, json.loads(r.get_data(as_text=True))
    except ValueError:
        return r.status_code, {}


# ================================================================ A. who, and setting up
check("the HR Officer has Staff attendance; a teacher has neither it nor Clock in while it is off",
      "/admin/staff-attendance" in officer.text("/admin/school") and "/admin/staff-attendance" not in teacher.text("/admin/school")
      and "/admin/clock" not in teacher.text("/admin/school"))
for path in (SET, "/admin/staff-attendance", "/admin/staff-attendance/month"):
    check(f"a teacher is refused {path}", teacher.get(path).status_code in (302, 403))
check("the head teacher may see the register, not the settings",
      head.get("/admin/staff-attendance").status_code == 200 and head.get(SET).status_code in (302, 403))
officer.post(SET, settings_form(enabled="1"), page=SET)
check("attendance cannot be turned on with location checks before the school's location is set",
      not one("SELECT enabled FROM staff_attendance_settings") and "location" in officer.said())
officer.post(SET, settings_form(enabled="1", latitude="6.5244", longitude="3.3792"), page=SET)
check("with the location set it is on, and absences count from today",
      one("SELECT enabled FROM staff_attendance_settings") == 1 and one("SELECT started_on FROM staff_attendance_settings") == TODAY.isoformat())
officer.post(SET, settings_form(enabled="1", latitude="91", longitude="3.3792", radius_m="5"), page=SET)
check("an impossible latitude or a tiny radius is refused", one("SELECT radius_m FROM staff_attendance_settings") == 150
      and one("SELECT latitude FROM staff_attendance_settings") == 6.5244)
check("staff with a record now have Clock in", "/admin/clock" in teacher.text("/admin/school"))
check("the clock page lets the page ask for the location, and nothing else",
      "geolocation=(self)" in teacher.get(CLOCK).headers.get("Permissions-Policy", "")
      and "camera=()" in teacher.get(CLOCK).headers.get("Permissions-Policy", ""))

# ================================================================ B. the screen
officer.post("/admin/staff-attendance/display", {}, page=SET)
page = officer.text(SET)
found = re.search(r'/staff-attendance/display/([A-Za-z0-9_\-]+)', page)
TOKEN = found.group(1) if found else ""
check("a link for the screen is made and shown once", TOKEN and TOKEN not in officer.text(SET))
check("…and only its hash is kept", one("SELECT display_token_hash FROM staff_attendance_settings") not in ("", None, TOKEN))
screen = Person("the screen")
r = screen.get(f"/staff-attendance/display/{TOKEN}")
check("the screen opens with no sign-in, never cached or indexed",
      r.status_code == 200 and "Staff clock-in" in r.get_data(as_text=True)
      and "no-store" in r.headers.get("Cache-Control", "") and "noindex" in r.headers.get("X-Robots-Tag", ""))
code = screen.get(f"/staff-attendance/display/{TOKEN}/code").get_json() or {}
check("it shows the code the server makes, and the QR code", code.get("digits") == digits()[1] and code.get("path", "").startswith("M"))
check("a wrong link shows nothing", screen.get("/staff-attendance/display/not-the-token").status_code == 404
      and screen.get("/staff-attendance/display/not-the-token/code").status_code == 404)

# ================================================================ C. clocking in
status, j = punch(teacher, "in", "123456")
check("a wrong code is refused", status == 400 and "not the one on the school" in j.get("error", ""))
status, j = punch(teacher, "in", digits(-(sat.GRACE + 2))[1])
check("an old code (a photo sent home) is refused", status == 400 and not one("SELECT COUNT(*) FROM staff_attendance_records"))
status, j = punch(teacher, "in", digits()[1], FAR)
check("the right code from 8 km away is refused", status == 400 and "m from the school" in j.get("error", ""))
status, j = punch(teacher, "in", digits()[1], VAGUE)
check("a phone unsure of where it is is refused, not guessed at", status == 400 and "only sure" in j.get("error", ""))
status, j = punch(teacher, "in", digits()[1], {"lat": "nan", "lng": "3.3", "acc": "5"})
check("a position that is not a number is refused", status == 400)
check("no refusal registered a phone", not one("SELECT COUNT(*) FROM staff_attendance_devices"))
status, j = punch(teacher, "in", digits()[0])
row = sql("SELECT clock_in, late, in_method, in_distance_m, in_accuracy_m FROM staff_attendance_records WHERE staff_id = :s", s=BELLO)
check("the QR code from the screen, at the school, clocks in", status == 200 and j.get("ok") and row and row[0][2] == "phone"
      and 40 <= row[0][3] <= 100 and row[0][4] == 25 and row[0][1] == 0)
check("…registers this phone, by a hash only", one("SELECT COUNT(*) FROM staff_attendance_devices WHERE admin_id = :a AND active = 1", a=TEACHER) == 1
      and "This phone is now" in j.get("message", ""))
check("…and keeps no position, in the record or the log",
      not [c for c in sql("SELECT column_name FROM information_schema.columns WHERE table_name = 'staff_attendance_records'")
           if c[0] in ("latitude", "longitude", "in_lat", "in_lng")]
      and not one("SELECT COUNT(*) FROM audit_logs WHERE details LIKE '%6.525%'"))
status, j = punch(teacher, "in", digits()[1])
check("clocking in twice is refused", status == 400 and "already" in j.get("error", ""))
phone2 = login_again("mrs.bello")
status, j = punch(phone2, "out", digits()[1])
check("another phone cannot clock for them", status == 403 and "not the phone you clock in with" in j.get("error", ""))
status, j = punch(teacher, "out", digits()[1])
check("they clock out from their own phone", status == 200 and one("SELECT clock_out FROM staff_attendance_records WHERE staff_id = :s", s=BELLO))
status, j = punch(teacher, "out", digits()[1])
check("…once", status == 400)

# lateness, and the rate limit
sql("UPDATE staff_attendance_settings SET late_after = '00:00'")
status, j = punch(other, "in", digits()[1])
check("someone clocking in after the late time is marked late", status == 200 and "late" in j.get("message", "")
      and one("SELECT late FROM staff_attendance_records r JOIN hr_staff s ON s.id = r.staff_id WHERE s.admin_id = :a", a=OTHER) == 1)
statuses = [punch(head, "in", "000000")[0] for _ in range(14)]
check("too many tries are stopped for a while", statuses[-1] == 429 and 429 not in statuses[:11])

# the code only, or the location only
OKON = one("SELECT id FROM hr_staff WHERE admin_id = :a", a=OTHER)
sql("UPDATE staff_attendance_settings SET method = 'gps'")
sql("DELETE FROM staff_attendance_records WHERE staff_id = :s", s=OKON)
status, j = punch(other, "in", None)
check("with the location only, no code is needed", status == 200)
sql("UPDATE staff_attendance_settings SET method = 'qr'")
sql("DELETE FROM staff_attendance_records WHERE staff_id = :s", s=OKON)
status, j = punch(other, "in", digits()[1], FAR)
check("with the code only, the location is not checked", status == 200)
sql("UPDATE staff_attendance_settings SET method = 'qr_gps', late_after = '23:59'")

# ================================================================ D. the register
REG = "/admin/staff-attendance"
reg = officer.text(REG)
check("the register shows who is in, by phone, with the distance", "Bello" in reg and "phone" in reg)
officer.post(f"{REG}/mark", {"staff_id": MUSA, "day": TODAY.isoformat(), "kind": "in", "time": "07:40", "note": ""}, page=REG)
check("marking by hand needs a note", not one("SELECT COUNT(*) FROM staff_attendance_records WHERE staff_id = :s", s=MUSA))
officer.post(f"{REG}/mark", {"staff_id": MUSA, "day": TODAY.isoformat(), "kind": "in", "time": "07:40", "note": "No phone: drives the bus"}, page=REG)
check("the driver, who has no account, is marked in by hand",
      sql("SELECT clock_in, in_method FROM staff_attendance_records WHERE staff_id = :s", s=MUSA) == [("07:40", "register")])
officer.post(f"{REG}/mark", {"staff_id": BELLO, "day": TODAY.isoformat(), "kind": "in", "time": "06:00", "note": "x"}, page=REG)
check("a time made on a phone is never overwritten by hand", one("SELECT in_method FROM staff_attendance_records WHERE staff_id = :s", s=BELLO) == "phone")
officer.post(f"{REG}/mark", {"staff_id": MUSA, "day": TODAY.isoformat(), "kind": "out", "time": "07:00", "note": "x"}, page=REG)
check("leaving before arriving is refused", one("SELECT clock_out FROM staff_attendance_records WHERE staff_id = :s", s=MUSA) is None)
r = teacher.post(f"{REG}/mark", {"staff_id": MUSA, "day": TODAY.isoformat(), "kind": "out", "time": "16:00", "note": "x"})
check("a teacher cannot mark anyone", one("SELECT clock_out FROM staff_attendance_records WHERE staff_id = :s", s=MUSA) is None)

# ================================================================ E. corrections
YESTERDAY = (TODAY - timedelta(days=1)).isoformat()
teacher.post("/admin/clock/correction", {"day": YESTERDAY, "kind": "out", "time": "15:00", "reason": "Phone died"}, page=CLOCK)
check("leaving cannot be corrected for a day with no arrival", not one("SELECT COUNT(*) FROM staff_attendance_corrections"))
teacher.post("/admin/clock/correction", {"day": YESTERDAY, "kind": "in", "time": "07:30", "reason": "Phone died"}, page=CLOCK)
CORR = one("SELECT id FROM staff_attendance_corrections")
check("a correction is asked for, and those who manage attendance are told",
      CORR and one("SELECT COUNT(*) FROM admin_notifications WHERE admin_id = :a AND title = 'Attendance correction to decide'", a=OFFICER) == 1)
teacher.post("/admin/clock/correction", {"day": (TODAY + timedelta(days=1)).isoformat(), "kind": "in", "time": "07:30", "reason": "x"}, page=CLOCK)
check("a day in the future cannot be corrected", one("SELECT COUNT(*) FROM staff_attendance_corrections") == 1)
teacher.post(f"{REG}/corrections/{CORR}/decide", {"decision": "approve"})
check("the teacher cannot decide it", one("SELECT status FROM staff_attendance_corrections WHERE id = :i", i=CORR) == "pending")
officer.post(f"{REG}/corrections/{CORR}/decide", {"decision": "approve"}, page=REG)
check("the HR Officer approves it: the day is recorded as corrected, and the teacher is told",
      sql("SELECT clock_in, in_method FROM staff_attendance_records WHERE staff_id = :s AND day = :d", s=BELLO, d=YESTERDAY) == [("07:30", "correction")]
      and one("SELECT COUNT(*) FROM admin_notifications WHERE admin_id = :a AND title = 'Attendance correction approved'", a=TEACHER) == 1)

# ================================================================ F. the month, leave and closed days
start = TODAY - timedelta(days=20)
sql("UPDATE staff_attendance_settings SET started_on = :d", d=start.isoformat())
work = [start + timedelta(days=i) for i in range(20) if (start + timedelta(days=i)).isoweekday() <= 5]
LEAVE_DAY, CLOSED_DAY = work[0], work[1]
officer.post(f"/admin/hr/{MUSA}/leave", {"leave_type_id": one("SELECT id FROM hr_leave_types WHERE name = 'Casual leave'"),
                                         "start_date": LEAVE_DAY.isoformat(), "end_date": LEAVE_DAY.isoformat()}, page=f"/admin/hr/{MUSA}?tab=leave")
officer.post(f"{REG.replace('/admin/staff-attendance', '/admin/staff-attendance')}/closed", {"day": CLOSED_DAY.isoformat(), "label": "Public holiday"}, page=SET)
check("a closed day is added", one("SELECT label FROM staff_attendance_closed_days") == "Public holiday")
day_page = officer.text(f"{REG}?day={LEAVE_DAY.isoformat()}")
check("on the register, a day of approved leave shows as leave, not absence", "On leave" in day_page)
check("…a closed day as closed", "School closed" in officer.text(f"{REG}?day={CLOSED_DAY.isoformat()}"))
check("…and a past work day with nothing as absent", "Absent" in officer.text(f"{REG}?day={work[2].isoformat()}"))
month = LEAVE_DAY.strftime("%Y-%m")
sql("UPDATE hr_staff SET job_title = '=1+1' WHERE id = :i", i=MUSA)
r = officer.get(f"{REG}/month?month={month}&format=csv")
body = r.get_data(as_text=True)
check("the month downloads as a CSV, never cached", r.status_code == 200 and r.mimetype == "text/csv" and "no-store" in r.headers.get("Cache-Control", ""))
import csv as _csv, io as _io  # noqa: E402
cells = [c for row in _csv.reader(_io.StringIO(body)) for c in row]
check("a formula-looking cell is defused", "'=1+1" in cells and not any(c[:1] in "=+@" for c in cells if c))
check("…and is logged", one("SELECT COUNT(*) FROM audit_logs WHERE action = 'staff_attendance_exported'") == 1)
check("a teacher cannot download it", teacher.get(f"{REG}/month?month={month}&format=csv").status_code in (302, 403))

# ================================================================ G. phones and the screen's link
DEV = one("SELECT id FROM staff_attendance_devices WHERE admin_id = :a AND active = 1", a=TEACHER)
officer.post(f"/admin/staff-attendance/devices/{DEV}/reset", {}, page=SET)
check("the manager resets a phone, and the teacher is told",
      one("SELECT active FROM staff_attendance_devices WHERE id = :i", i=DEV) == 0
      and one("SELECT COUNT(*) FROM admin_notifications WHERE admin_id = :a AND title = 'Clock-in phone reset'", a=TEACHER) == 1)
sql("DELETE FROM staff_attendance_records WHERE staff_id = :s AND day = :d", s=BELLO, d=TODAY.isoformat())
status, j = punch(phone2, "in", digits()[1])
check("…and their new phone becomes theirs", status == 200 and one("SELECT COUNT(*) FROM staff_attendance_devices WHERE admin_id = :a AND active = 1", a=TEACHER) == 1)
old_code = digits()[1]
officer.post("/admin/staff-attendance/display", {}, page=SET)
check("a new screen link ends the old one, and its codes",
      screen.get(f"/staff-attendance/display/{TOKEN}").status_code == 404 and old_code != digits()[1])
r = teacher.post("/admin/staff-attendance/display", {})
check("a teacher cannot make a screen link", r.status_code in (302, 403))

# ================================================================ H. each school's own time zone
check("a new school keeps its day in Nigeria's time zone", one_platform("SELECT timezone FROM tenants WHERE slug = 'alpha'") == "Africa/Lagos")
school_page = console.get("/platform/schools/alpha", base_url=PL).get_data(as_text=True)
console.post("/platform/schools/alpha/timezone", data={"_csrf_token": token_in(school_page), "timezone": "Mars/Olympus"}, base_url=PL)
check("a time zone not on the list is refused", one_platform("SELECT timezone FROM tenants WHERE slug = 'alpha'") == "Africa/Lagos")
console.post("/platform/schools/alpha/timezone", data={"_csrf_token": token_in(school_page), "timezone": "Africa/Nairobi"}, base_url=PL)
check("a platform admin changes it, and it is in the platform's audit trail",
      one_platform("SELECT timezone FROM tenants WHERE slug = 'alpha'") == "Africa/Nairobi"
      and one_platform("SELECT COUNT(*) FROM platform_audit_log WHERE action = 'tenant.timezone'") == 1)
r = teacher.post("/platform/schools/alpha/timezone", {"timezone": "UTC"})
check("a school's own staff cannot change it", one_platform("SELECT timezone FROM tenants WHERE slug = 'alpha'") == "Africa/Nairobi")
from zoneinfo import ZoneInfo  # noqa: E402
sql("DELETE FROM staff_attendance_records WHERE staff_id = :s AND day >= :d", s=BELLO, d=(TODAY - timedelta(days=1)).isoformat())
expected = {(datetime.now(timezone.utc) + timedelta(seconds=s)).astimezone(ZoneInfo("Africa/Nairobi")).strftime("%Y-%m-%d %H:%M") for s in (0, 30)}
status, j = punch(phone2, "in", digits()[1])
got = sql("SELECT day, clock_in FROM staff_attendance_records WHERE staff_id = :s ORDER BY day DESC LIMIT 1", s=BELLO)
check("clocking in now uses the school's own time zone", status == 200 and got and f"{got[0][0]} {got[0][1]}" in expected,
      f"{got} not in {expected}")

OFFICER_REC = one("SELECT id FROM hr_staff WHERE admin_id = :a", a=OFFICER)
officer.post("/admin/staff-attendance/mark", {"staff_id": OFFICER_REC, "day": TODAY.isoformat(), "kind": "in", "time": "07:00",
                                              "note": "I was here early"}, page="/admin/staff-attendance")
check("a manager cannot mark their own attendance by hand", not one("SELECT COUNT(*) FROM staff_attendance_records WHERE staff_id = :s", s=OFFICER_REC)
      and "nobody marks their own" in officer.said())

check("no page answered with a server error", not PROBLEMS, "; ".join(PROBLEMS[:5]))
failed = [name for name, ok, _ in results if not ok]
print()
print(f"{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", "; ".join(failed))
    raise SystemExit(1)
