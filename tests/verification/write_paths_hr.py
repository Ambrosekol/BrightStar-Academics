"""HR, proven end to end on PostgreSQL. In plain words:

A. Who: the HR Officer has Staff records and Staff leave; a Subject Teacher has neither and is refused every page.
B. Records: one for each staff account, at once (never twice, never the platform's own); a driver with no account; a
   wrong email or an impossible date is refused and explained; a name is shown as text; a record is never cached, and
   opening it is logged without its contents; a staff number or an account cannot be used twice; a change is logged
   by the fields changed, never their values; a teacher cannot change a record.
C. Documents: a real PDF is kept under a random name in a folder no address opens; a file pretending to be a PDF, and
   a web page, are refused; HR opens it (right type, not sniffed, not cached, logged); a teacher cannot, and nobody
   can by the file's address.
D. Career: a promotion changes the job title; an entry is never deleted, only struck out with a reason.
E. Leave: asked for on My leave (which shows the balance and the person's record without HR's private notes),
   counted in working days; approvers are told; overlapping leave, too many days, backwards dates and a weekend are
   refused; nobody approves their own; approval re-checks the balance against the person's own allowance; the person
   is told; they cancel leave not yet started; HR records leave for someone without an account; the kinds of leave
   change; someone who has left cannot ask.
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
TMP = tempfile.mkdtemp(prefix="beacon_hr_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup("hr")
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
role = {name: one("SELECT id FROM admin_types WHERE name = :n", n=name) for name in ("HR Officer", "Subject Teacher", "Head Teacher")}
check("a new school has the HR Officer role, with the three HR permissions",
      sorted(r[0] for r in sql("SELECT p.code FROM admin_type_permissions tp JOIN permissions p ON p.id = tp.permission_id "
                                "WHERE tp.admin_type_id = :r AND p.code LIKE 'hr.%'", r=role["HR Officer"])) == ["hr.leave.approve", "hr.manage", "hr.view"])
check("…and the standard kinds of leave", one("SELECT COUNT(*) FROM hr_leave_types WHERE active = 1") == 7
      and one("SELECT days_per_year FROM hr_leave_types WHERE name = 'Annual leave'") == 10)


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


officer, OFFICER = new_staff("hr.officer", "HR Officer")
op.post("/admin/school/subjects/new", {"name": "Mathematics", "code": "MTH", "class_ids": [J1]}, page="/admin/school/subjects/new")
MATHS = one("SELECT id FROM school_subjects WHERE name = 'Mathematics'")
teacher, TEACHER = new_staff("mrs.bello", "Subject Teacher", {"duty_class": [str(J1)], "duty_subject": [str(MATHS)]})
head, HEAD = new_staff("head.teacher", "Head Teacher")

from datetime import date, timedelta  # noqa: E402
TODAY = date.today()
MONDAY = TODAY + timedelta(days=7 - TODAY.weekday() + 7)        # a Monday at least a week away
D = lambda n: (MONDAY + timedelta(days=n)).isoformat()  # noqa: E731

# ================================================================ A. who
home = officer.text("/admin/school")
check("the HR Officer has Staff records and Staff leave in the menu", "/admin/hr" in home and "Staff leave" in home)
check("a Subject Teacher has neither", "/admin/hr" not in teacher.text("/admin/school"))
for path in ("/admin/hr", "/admin/hr/new", "/admin/hr/leave", "/admin/hr/leave/types"):
    r = teacher.get(path)
    check(f"…and is refused {path}", r.status_code in (302, 403))

# ================================================================ B. records
r = officer.post("/admin/hr/from-accounts", {}, page="/admin/hr")
linked = dict(sql("SELECT admin_id, last_name FROM hr_staff WHERE admin_id IS NOT NULL"))
check("records are made for the staff accounts, from what they hold", TEACHER in linked and OFFICER in linked and HEAD in linked
      and linked[TEACHER] == "Bello" and one("SELECT title FROM hr_staff WHERE admin_id = :a", a=TEACHER) == "Mrs")
check("…not for the platform's own accounts", not sql("SELECT 1 FROM hr_staff s JOIN admins a ON a.id = s.admin_id WHERE a.username LIKE 'platform@%'"))
again = len(sql("SELECT id FROM hr_staff"))
officer.post("/admin/hr/from-accounts", {}, page="/admin/hr")
check("…and doing it again adds nobody twice", len(sql("SELECT id FROM hr_staff")) == again)

DRIVER = {"first_name": "Musa", "last_name": "Ibrahim<script>x</script>", "category": "non_teaching", "employment_type": "full_time",
          "status": "active", "job_title": "Driver", "phone": "0803 111 2222", "email": "not-an-email", "date_of_birth": "1990-02-30",
          "notes": "Private: owes the school a uniform deposit.", "staff_no": "BS/001", "role": "School Admin"}
r = officer.post("/admin/hr/new", DRIVER, page="/admin/hr/new")
body = r.get_data(as_text=True)
check("a record with a wrong email and an impossible date is refused, and says why",
      r.status_code == 400 and "email address does not look right" in body and "Date of birth is not a date" in body)
DRIVER.update({"email": "musa@example.com", "date_of_birth": "1990-02-03"})
r = officer.post("/admin/hr/new", DRIVER, page="/admin/hr/new")
MUSA = one("SELECT id FROM hr_staff WHERE staff_no = 'BS/001'")
check("a driver with no portal account gets a record", r.status_code == 302 and MUSA
      and one("SELECT admin_id FROM hr_staff WHERE id = :i", i=MUSA) is None)
page = officer.text(f"/admin/hr/{MUSA}")
check("…whose name is shown as text, never as markup", "Ibrahim&lt;script&gt;" in page and "<script>x</script>" not in page)
check("…and the page is never cached", officer.get(f"/admin/hr/{MUSA}").headers.get("Cache-Control", "").startswith("no-store"))
check("opening a record is in the activity log, without its contents",
      one("SELECT COUNT(*) FROM audit_logs WHERE action = 'hr_record_viewed' AND target_id = :t", t=str(MUSA)) >= 1
      and not one("SELECT COUNT(*) FROM audit_logs WHERE details LIKE '%uniform deposit%' OR details LIKE '%0803 111%'"))
r = officer.post("/admin/hr/new", dict(DRIVER, first_name="Another", staff_no="bs/001"), page="/admin/hr/new")
check("a staff number already in use is refused", r.status_code == 400 and "already someone else" in r.get_data(as_text=True))
r = officer.post("/admin/hr/new", dict(DRIVER, first_name="Linker", staff_no="", admin_id=str(TEACHER)), page="/admin/hr/new")
check("an account that already has a record cannot be linked to a second one", r.status_code == 400
      and one("SELECT COUNT(*) FROM hr_staff WHERE admin_id = :a", a=TEACHER) == 1)

r = officer.post(f"/admin/hr/{MUSA}/edit", dict(DRIVER, status="left"), page=f"/admin/hr/{MUSA}/edit")
check("marking someone as left needs the date and the reason", r.status_code == 400 and "Enter the date they left" in r.get_data(as_text=True))
r = officer.post(f"/admin/hr/{MUSA}/edit", dict(DRIVER, job_title="Senior driver", phone="0803 999 0000"), page=f"/admin/hr/{MUSA}/edit")
logged = one("SELECT details FROM audit_logs WHERE action = 'hr_record_updated' ORDER BY id DESC LIMIT 1") or ""
check("a change is saved, and the log names the fields changed, never their values",
      one("SELECT job_title FROM hr_staff WHERE id = :i", i=MUSA) == "Senior driver" and "job_title" in logged and "phone" in logged
      and "0803" not in logged and "Senior driver" not in logged)
r = teacher.post(f"/admin/hr/{MUSA}/edit", dict(DRIVER, job_title="Hacked"))
check("a teacher cannot change a record", r.status_code in (302, 403) and one("SELECT job_title FROM hr_staff WHERE id = :i", i=MUSA) == "Senior driver")

# ================================================================ C. documents
import io  # noqa: E402
PNG_BYTES = bytes([0x89]) + b"PNG" + bytes([13, 10, 26, 10]) + b"0" * 64
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
DOCS = f"/admin/hr/{MUSA}?tab=files"


def upload(person, name, data, label="Driving licence"):
    token = token_in(person.text(DOCS) if person is officer else person.text("/admin/password"))
    return person.c.post(f"/admin/hr/{MUSA}/documents", data={"_csrf_token": token, "label": label, "category": "identity",
                                                              "document": (io.BytesIO(data), name)},
                         base_url=UA, environ_base={"REMOTE_ADDR": person.addr}, content_type="multipart/form-data")


upload(officer, "licence.pdf", PDF)
DOC = one("SELECT id FROM hr_documents WHERE staff_id = :s", s=MUSA)
STORED = one("SELECT file_path FROM hr_documents WHERE id = :i", i=DOC) or ""
check("a PDF is uploaded, under a random name in the HR folder", DOC and STORED.startswith("uploads/hr/hr_") and STORED.endswith(".pdf"))
upload(officer, "evil.pdf", b"MZ\x90\x00 this is a program, not a PDF")
check("a file that only pretends to be a PDF is refused", one("SELECT COUNT(*) FROM hr_documents") == 1
      and "contents do not match" in officer.said())
upload(officer, "page.html", b"<html><script>alert(1)</script></html>")
check("a web page is refused", one("SELECT COUNT(*) FROM hr_documents") == 1)
r = officer.get(f"/admin/hr/documents/{DOC}")
check("HR opens the document: the right type, not sniffed, not cached",
      r.status_code == 200 and r.data == PDF and r.headers["Content-Type"] == "application/pdf"
      and r.headers.get("X-Content-Type-Options") == "nosniff" and "no-store" in r.headers.get("Cache-Control", ""))
check("…and opening it is logged", one("SELECT COUNT(*) FROM audit_logs WHERE action = 'hr_document_opened'") == 1)
r = teacher.get(f"/admin/hr/documents/{DOC}")
check("a teacher cannot open it", r.status_code in (302, 403) and r.data != PDF)
for who, person in (("the School Admin", op), ("a teacher", teacher)):
    r = person.get("/" + STORED) if STORED else None
    check(f"…nor can {who} by its file address", r is not None and r.status_code == 404)
r = teacher.post(f"/admin/hr/documents/{DOC}/remove", {})
check("a teacher cannot remove it", one("SELECT COUNT(*) FROM hr_documents") == 1)

officer.post(f"/admin/hr/{MUSA}/qualifications", {"qualification": "Class E driving licence", "institution": "FRSC", "year": "2015"}, page=DOCS)
check("a qualification is added", one("SELECT qualification FROM hr_qualifications WHERE staff_id = :s", s=MUSA) == "Class E driving licence")
officer.post(f"/admin/hr/{MUSA}/qualifications", {"qualification": "Typed by mistake"}, page=DOCS)
officer.post(f"/admin/hr/qualifications/{one("SELECT id FROM hr_qualifications WHERE qualification = 'Typed by mistake'")}/remove", {}, page=DOCS)
check("…and one added by mistake is removed", one("SELECT COUNT(*) FROM hr_qualifications") == 1)
upload(officer, "scan.png", PNG_BYTES, label="Wrong scan")
SCAN = one("SELECT id FROM hr_documents WHERE label = 'Wrong scan'")
SCAN_PATH = one("SELECT file_path FROM hr_documents WHERE id = :i", i=SCAN)
officer.post(f"/admin/hr/documents/{SCAN}/remove", {}, page=DOCS)
with A.app.app_context():
    from control_plane.context import tenant_context
    with tenant_context(INFO):
        from core.storage import read_upload_bytes
        gone = read_upload_bytes(SCAN_PATH) is None
check("a document removed by HR is gone, file and all", SCAN and not one("SELECT COUNT(*) FROM hr_documents WHERE id = :i", i=SCAN) and gone)

# ================================================================ D. career
CAREER = f"/admin/hr/{MUSA}?tab=career"
officer.post(f"/admin/hr/{MUSA}/career", {"kind": "promotion", "effective_date": TODAY.isoformat(), "to_value": "Transport officer"}, page=CAREER)
check("a promotion is recorded, and changes the job title",
      one("SELECT job_title FROM hr_staff WHERE id = :i", i=MUSA) == "Transport officer"
      and sql("SELECT from_value, to_value FROM hr_career_events WHERE kind = 'promotion'") == [("Senior driver", "Transport officer")])
officer.post(f"/admin/hr/{MUSA}/career", {"kind": "query", "effective_date": TODAY.isoformat(), "summary": "Queried for lateness",
                                          "detail": "Late three times in a week."}, page=CAREER)
QUERY = one("SELECT id FROM hr_career_events WHERE kind = 'query'")
officer.post(f"/admin/hr/career/{QUERY}/strike", {"reason": ""}, page=CAREER)
check("striking an entry out needs a reason", one("SELECT struck_at FROM hr_career_events WHERE id = :i", i=QUERY) is None)
officer.post(f"/admin/hr/career/{QUERY}/strike", {"reason": "Recorded against the wrong person"}, page=CAREER)
page = officer.text(CAREER)
check("a struck-out entry stays on the record, crossed through, with the reason",
      one("SELECT COUNT(*) FROM hr_career_events") == 2 and "is-struck" in page and "Recorded against the wrong person" in page)
check("there is no way to delete or edit a career entry",
      not any(rule.rule.startswith("/admin/hr/career/") and not rule.rule.endswith("/strike") for rule in A.app.url_map.iter_rules()))

# ================================================================ E. leave
ME = "/admin/my-leave"
BELLO = one("SELECT id FROM hr_staff WHERE admin_id = :a", a=TEACHER)
ANNUAL = one("SELECT id FROM hr_leave_types WHERE name = 'Annual leave'")
CASUAL = one("SELECT id FROM hr_leave_types WHERE name = 'Casual leave'")
sql("UPDATE hr_staff SET notes = 'Private: under review' WHERE id = :i", i=BELLO)
mine = teacher.text(ME)
check("a teacher with a record has My leave, with their balance", "/admin/my-leave" in teacher.text("/admin/school")
      and "Annual leave" in mine and "of 10 left" in mine)
check("…and their own record, without HR's private notes", "Bello" in mine and "under review" not in mine)
check("a member of staff with no record is told so", "no staff record yet" in op.text(ME))

teacher.post(ME, {"leave_type_id": ANNUAL, "start_date": D(0), "end_date": D(6)}, page=ME)
LEAVE1 = one("SELECT id FROM hr_leave_requests WHERE staff_id = :s", s=BELLO)
check("leave is asked for, counted in working days (Monday to the next Sunday is 5)",
      sql("SELECT days, status FROM hr_leave_requests WHERE id = :i", i=LEAVE1) == [(5, "pending")])
check("…and those who may approve leave are told; the teacher is not",
      one("SELECT COUNT(*) FROM admin_notifications WHERE admin_id = :a AND title = 'Leave to approve'", a=OFFICER) == 1
      and one("SELECT COUNT(*) FROM admin_notifications WHERE admin_id = :a AND title = 'Leave to approve'", a=HEAD) == 1
      and not one("SELECT COUNT(*) FROM admin_notifications WHERE admin_id = :a", a=TEACHER))
teacher.post(ME, {"leave_type_id": ANNUAL, "start_date": D(4), "end_date": D(8)}, page=ME)
check("leave that overlaps leave already asked for is refused", one("SELECT COUNT(*) FROM hr_leave_requests") == 1
      and "overlap" in teacher.said())
teacher.post(ME, {"leave_type_id": ANNUAL, "start_date": D(14), "end_date": D(25)}, page=ME)
check("more days than are left (counting those waiting) are refused", one("SELECT COUNT(*) FROM hr_leave_requests") == 1
      and "only 5 are left" in teacher.said())
teacher.post(ME, {"leave_type_id": ANNUAL, "start_date": D(6), "end_date": D(5)}, page=ME)
check("a last day before the first is refused", "before the first" in teacher.said())
teacher.post(ME, {"leave_type_id": ANNUAL, "start_date": D(5), "end_date": D(6)}, page=ME)
check("a weekend only is refused: there are no working days", "weekend" in teacher.said())
r = teacher.post(f"/admin/hr/leave/{LEAVE1}/decide", {"decision": "approve"})
check("the teacher cannot approve their own leave", one("SELECT status FROM hr_leave_requests WHERE id = :i", i=LEAVE1) == "pending")

waiting = officer.text("/admin/hr/leave")
check("the leave page shows the request with the balance after it", "Bello" in waiting and "5 of 10" in waiting)
officer.post(f"/admin/hr/leave/{LEAVE1}/decide", {"decision": "approve", "note": "Enjoy"}, page="/admin/hr/leave")
check("HR approves it, and the teacher is told",
      one("SELECT status FROM hr_leave_requests WHERE id = :i", i=LEAVE1) == "approved"
      and one("SELECT COUNT(*) FROM admin_notifications WHERE admin_id = :a AND title = 'Leave approved'", a=TEACHER) == 1)
officer.post(f"/admin/hr/leave/{LEAVE1}/decide", {"decision": "decline"}, page="/admin/hr/leave")
check("a decided request cannot be decided again", one("SELECT status FROM hr_leave_requests WHERE id = :i", i=LEAVE1) == "approved")
check("My leave now shows 5 of 10 left", "of 10 left" in teacher.text(ME) and ">5<" in teacher.text(ME))

# the HR Officer's own leave: never approved by themselves
OWN = one("SELECT id FROM hr_staff WHERE admin_id = :a", a=OFFICER)
officer.post(ME, {"leave_type_id": CASUAL, "start_date": D(1), "end_date": D(1)}, page=ME)
LEAVE2 = one("SELECT id FROM hr_leave_requests WHERE staff_id = :s", s=OWN)
officer.post(f"/admin/hr/leave/{LEAVE2}/decide", {"decision": "approve"}, page="/admin/hr/leave")
check("the HR Officer cannot approve their own leave", one("SELECT status FROM hr_leave_requests WHERE id = :i", i=LEAVE2) == "pending")
head.post(f"/admin/hr/leave/{LEAVE2}/decide", {"decision": "decline", "note": "Exams that day"}, page="/admin/hr/leave")
check("the head teacher may decide it", one("SELECT status FROM hr_leave_requests WHERE id = :i", i=LEAVE2) == "declined")
check("…and the head teacher, who may only approve leave, cannot open staff records", head.get(f"/admin/hr/{MUSA}").status_code in (302, 403))

# allowances and the re-check on approval
YEAR = int(D(0)[:4])
teacher.post(ME, {"leave_type_id": ANNUAL, "start_date": D(14), "end_date": D(18)}, page=ME)
LEAVE3 = one("SELECT id FROM hr_leave_requests WHERE staff_id = :s AND start_date = :d", s=BELLO, d=D(14))
officer.post(f"/admin/hr/{BELLO}/allowance", {"leave_type_id": ANNUAL, "year": YEAR, "days": "7", "note": "Part-time this year"},
             page=f"/admin/hr/{BELLO}?tab=leave")
officer.post(f"/admin/hr/leave/{LEAVE3}/decide", {"decision": "approve"}, page="/admin/hr/leave")
check("approval checks the balance again: 5 more days do not fit a 7-day allowance with 5 taken",
      one("SELECT status FROM hr_leave_requests WHERE id = :i", i=LEAVE3) == "pending" and "only 2 day(s)" in officer.said())
officer.post(f"/admin/hr/{BELLO}/allowance", {"leave_type_id": ANNUAL, "year": YEAR, "days": "15", "note": "Carried over"},
             page=f"/admin/hr/{BELLO}?tab=leave")
officer.post(f"/admin/hr/leave/{LEAVE3}/decide", {"decision": "approve"}, page="/admin/hr/leave")
check("with a larger allowance of their own, it is approved", one("SELECT status FROM hr_leave_requests WHERE id = :i", i=LEAVE3) == "approved")
teacher.post(f"/admin/hr/leave/{LEAVE3}/cancel", {}, page=ME)
check("the teacher cancels approved leave that has not started, and approvers are told",
      one("SELECT status FROM hr_leave_requests WHERE id = :i", i=LEAVE3) == "cancelled"
      and one("SELECT COUNT(*) FROM admin_notifications WHERE admin_id = :a AND title = 'Leave cancelled'", a=OFFICER) == 1)
r = teacher.post(f"/admin/hr/leave/{LEAVE2}/cancel", {}, page=ME)
check("a teacher cannot cancel someone else's leave", one("SELECT status FROM hr_leave_requests WHERE id = :i", i=LEAVE2) == "declined")

# leave recorded by HR for the driver, who has no account
officer.post(f"/admin/hr/{MUSA}/leave", {"leave_type_id": CASUAL, "start_date": D(2), "end_date": D(3), "reason": "Family"},
             page=f"/admin/hr/{MUSA}?tab=leave")
check("HR records leave for someone without an account, approved at once",
      sql("SELECT days, status FROM hr_leave_requests WHERE staff_id = :s", s=MUSA) == [(2, "approved")])
check("the leave page lists it as coming up", "Ibrahim" in officer.text("/admin/hr/leave"))

# kinds of leave
TYPES = "/admin/hr/leave/types"
form = {f"days_{i}": str(d) for i, d in sql("SELECT id, days_per_year FROM hr_leave_types")}
form.update({f"paid_{i}": "1" for (i,) in sql("SELECT id FROM hr_leave_types WHERE paid = 1")})
form.update({f"active_{i}": "1" for (i,) in sql("SELECT id FROM hr_leave_types WHERE active = 1")})
form.update({f"days_{ANNUAL}": "15", "new_name": "Compassionate leave", "new_days": "3", "new_paid": "1"})
officer.post(TYPES, form, page=TYPES)
check("HR changes a kind's days and adds a kind of leave",
      one("SELECT days_per_year FROM hr_leave_types WHERE id = :i", i=ANNUAL) == 15
      and one("SELECT days_per_year FROM hr_leave_types WHERE name = 'Compassionate leave'") == 3)
r = head.post(TYPES, dict(form, **{f"days_{ANNUAL}": "99"}))
check("the head teacher cannot change them", one("SELECT days_per_year FROM hr_leave_types WHERE id = :i", i=ANNUAL) == 15)

# leaving
officer.post(f"/admin/hr/{BELLO}/edit", {"first_name": "Funmi", "last_name": "Bello", "category": "teaching",
                                         "employment_type": "full_time", "status": "left", "exit_date": TODAY.isoformat(), "admin_id": str(TEACHER),
                                         "exit_reason": "resigned"}, page=f"/admin/hr/{BELLO}/edit")
check("marking a linked member of staff as left warns that their account is still on", "account is still on" in officer.said())
teacher.post(ME, {"leave_type_id": CASUAL, "start_date": D(30), "end_date": D(30)}, page=ME)
check("someone who has left cannot ask for leave", not one("SELECT COUNT(*) FROM hr_leave_requests WHERE start_date = :d", d=D(30)))

# ================================================================ F. nobody changes their own record
officer.post(f"/admin/hr/{OWN}/allowance", {"leave_type_id": ANNUAL, "year": YEAR, "days": "366"}, page=f"/admin/hr/{OWN}?tab=leave")
check("the HR Officer cannot raise their own leave allowance", not one("SELECT COUNT(*) FROM hr_leave_allowances WHERE staff_id = :s", s=OWN)
      and "your own staff record" in officer.said())
officer.post(f"/admin/hr/{OWN}/edit", {"first_name": "Hr", "last_name": "Officer", "job_title": "Head of HR", "category": "teaching",
                                       "employment_type": "full_time", "status": "active", "admin_id": str(OFFICER)}, page=f"/admin/hr/{OWN}/edit")
check("…nor edit their own record", one("SELECT job_title FROM hr_staff WHERE id = :s", s=OWN) != "Head of HR")
officer.post(f"/admin/hr/{OWN}/career", {"kind": "commendation", "effective_date": TODAY.isoformat(), "summary": "Self-praise"}, page=f"/admin/hr/{OWN}?tab=career")
officer.post(f"/admin/hr/{OWN}/leave", {"leave_type_id": CASUAL, "start_date": D(40), "end_date": D(40)}, page=f"/admin/hr/{OWN}?tab=leave")
check("…nor record their own career or leave", not one("SELECT COUNT(*) FROM hr_career_events WHERE staff_id = :s", s=OWN)
      and not one("SELECT COUNT(*) FROM hr_leave_requests WHERE staff_id = :s AND start_date = :d", s=OWN, d=D(40)))
check("each refusal is in the activity log", one("SELECT COUNT(*) FROM audit_logs WHERE action = 'authorization_denied' AND details LIKE '%own_record%'") >= 4)

check("no page answered with a server error", not PROBLEMS, "; ".join(PROBLEMS[:5]))
failed = [name for name, ok, _ in results if not ok]
print()
print(f"{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", "; ".join(failed))
    raise SystemExit(1)
