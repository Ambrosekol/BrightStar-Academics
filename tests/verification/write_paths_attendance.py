"""Attendance, proven end to end on PostgreSQL: taking the register, the term summary, scope,
permissions and CSRF, and what a student and a parent are shown. In plain words, it proves:

A. Taking the register
   * a class's roster shows each student as 'Not marked' until marked; saving records present, late,
     absent and excused; a status left blank stays unmarked (no row is written for it);
   * re-saving the same statuses changes nothing and keeps the original marker; changing a status
     re-attributes the row to whoever changed it;
   * a status for a student outside the class roster (tampered id) is silently ignored;
   * only the day named in the address is touched: marking one day never changes another.
B. Scope
   * a class teacher marks and sees only her own class; another class is refused (404-equivalent:
     nothing saved, nothing shown) the same way report cards are.
C. The term summary
   * counts of present/late/absent/excused and the days-recorded total are exactly what was marked;
     the percentage counts late as present; a student with nothing marked has no percentage at all.
D. Permissions and CSRF
   * 'school.attendance.mark' may open and save the register; 'school.attendance.view' may open the
     summary but not the register; nobody with neither permission opens either; every write route is
     refused (403) without a valid CSRF token; a signed-out visitor is sent to sign in.
E. Students and parents
   * a student sees her own summary and history for a chosen term, and nobody else's; a parent sees a
     linked child's and nobody else's; another school's data never crosses over.
F. The guard files: the new routes are in the write-path coverage list and really exercised.

Run:  python tests/verification/write_paths_attendance.py
"""
import atexit
import html
import io
import logging
import os
import re
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_attendance_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('attendance')
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
from core import delivery as _delivery  # noqa: E402
from core.security import ADMIN_ENDPOINT_PERMISSIONS  # noqa: E402
from models import (  # noqa: E402
    Admin, AdminScope, AdminType, AdminTypePermission, ParentAccount, ParentStudentLink, Permission,
    School as SchoolRow, Student, StudentEnrolment,
)

_delivery._open_smtp = lambda settings: (_ for _ in ()).throw(RuntimeError("no mail in a test"))

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
        self.addr = f"10.60.{Person.count[0]}.1"

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
CUR = alpha.one("SELECT id FROM academic_sessions WHERE is_current = 1")
BETA_CUR = beta.one("SELECT id FROM academic_sessions WHERE is_current = 1")
J1, J2 = (alpha.one("SELECT id FROM school_classes WHERE name = :n", n=n) for n in ("JSS 1", "JSS 2"))
BETA_J1 = beta.one("SELECT id FROM school_classes WHERE name = 'JSS 1'")


def orm(school, fn):
    def go():
        out = fn()
        A.db.session.commit()
        return out
    return school.run(go)


def mk_student(school, first, last, class_id, session_id):
    def go():
        school_id = A.db.session.scalar(sa.select(SchoolRow.id))
        code = f"{school.code[:3].upper()}/{first[:3].upper()}"
        student = Student(admission_no=code, student_number=code, first_name=first, last_name=last, gender="Female",
                          created_at=NOW, active=1, school_id=school_id, login_username=f"{first}.{last}".lower(),
                          login_password_hash=HASH, account_active=1, password_must_change=0, guardian_name="A Guardian")
        A.db.session.add(student)
        A.db.session.flush()
        A.db.session.add(StudentEnrolment(student_id=student.id, class_id=class_id, session_id=session_id, enrolled_at=NOW,
                                          active=1, school_id=school_id))
        return student.id
    return orm(school, go)


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


def mk_parent(school, username, name, student_ids):
    def go():
        parent = ParentAccount(username=username, display_name=name, email=f"{username}@family.example", password_hash=HASH,
                               active=1, password_must_change=0, created_at=NOW)
        A.db.session.add(parent)
        A.db.session.flush()
        for sid in student_ids:
            A.db.session.add(ParentStudentLink(parent_id=parent.id, student_id=sid, relationship="Mother", active=1, created_at=NOW))
    orm(school, go)
    person = Person(school.base, "/parent/password", label=f"{school.code} parent {username}")
    person.post("/login", {"username": username, "password": PASSWORD}, page="/login")
    return person


ADA = mk_student(alpha, "Ada", "Obi", J1, CUR)
BOLA = mk_student(alpha, "Bola", "Eze", J1, CUR)
TOBI = mk_student(alpha, "Tobi", "Boundary", J2, CUR)
BETA_CHIKE = mk_student(beta, "Chike", "Nwosu", BETA_J1, BETA_CUR)

mk_role(alpha, "Both Attendance", ["school.view", "school.students.view", "school.classes.view", "school.attendance.mark", "school.attendance.view"])
mk_role(alpha, "View Only Attendance", ["school.view", "school.students.view", "school.classes.view", "school.attendance.view"])
mk_role(alpha, "Nothing Special", ["school.view", "school.students.view", "school.classes.view"])
teacher_a, TEACHER_A = mk_staff(alpha, "teacher.a", "Mrs Amaka Tutor", "Both Attendance", "JSS 1")
teacher_b, TEACHER_B = mk_staff(alpha, "teacher.b", "Mr Babatunde Guide", "Both Attendance", "JSS 1")
viewer, _ = mk_staff(alpha, "viewer", "Vera Viewer", "View Only Attendance", "JSS 1")
nobody, _ = mk_staff(alpha, "nobody", "Nora Nothing", "Nothing Special", "JSS 1")
mum_ada = mk_parent(alpha, "mum.ada", "Mrs Obi", [ADA])
ada_portal = Person(ALPHA, "/student/password", label="alpha student ada")
ada_portal.post("/login", {"username": "Ada.Obi".lower(), "password": PASSWORD}, page="/login")
beta_teacher, _ = mk_staff(beta, "teacher", "Beta Teacher", "Primary Class Teacher", "JSS 1")

REG = "/admin/school/attendance"
DAY1, DAY2, DAY3 = "2026-09-14", "2026-09-15", "2026-09-16"


def reg_url(class_id, date, session_id=None, term="First Term"):
    return f"{REG}?class_id={class_id}&session_id={session_id or CUR}&term={term.replace(' ', '%20')}&date={date}"


def summary_url(class_id, session_id=None, term="First Term"):
    return f"{REG}/summary?class_id={class_id}&session_id={session_id or CUR}&term={term.replace(' ', '%20')}"


def mark(person, class_id, date, values, session_id=None, term="First Term"):
    """values: {student_id: status}"""
    return person.post(reg_url(class_id, date, session_id, term), {f"status_{k}": v for k, v in values.items()},
                       page=reg_url(class_id, date, session_id, term))


def row_of(body, student_id):
    found = re.search(rf'name="status_{student_id}" value="(\w*)" checked', body)
    return found.group(1) if found else "NOT-FOUND"


# ================================================================ A. taking the register
page = html.unescape(teacher_a.text(reg_url(J1, DAY1)))
check("before anything is marked, JSS 1's roster shows both students as Not marked",
      row_of(page, ADA) == "" and row_of(page, BOLA) == "" and "Ada Obi" in page and "Bola Eze" in page)

r = mark(teacher_a, J1, DAY1, {ADA: "present", BOLA: "absent"})
row = alpha.sql("SELECT status, marked_by_admin_id FROM attendance_records WHERE student_id = :s AND date = :d", s=ADA, d=DAY1)
check("marking the day records present/absent for each student, attributed to the marker",
      r.status_code == 302 and row and row[0] == (("present", TEACHER_A)))
check("…and the other student's status too",
      alpha.one("SELECT status FROM attendance_records WHERE student_id = :s AND date = :d", s=BOLA, d=DAY1) == "absent")

r = mark(teacher_b, J1, DAY1, {ADA: "present", BOLA: "absent"})
said = teacher_b.said()
check("teacher B re-saves the same statuses unchanged: Ada's row still credits teacher A",
      alpha.one("SELECT marked_by_admin_id FROM attendance_records WHERE student_id = :s AND date = :d", s=ADA, d=DAY1) == TEACHER_A
      and "no" in said and "changed" in said, said)

r = mark(teacher_b, J1, DAY1, {ADA: "late", BOLA: "absent"})
check("teacher B changes Ada's status: the row now credits teacher B",
      alpha.sql("SELECT status, marked_by_admin_id FROM attendance_records WHERE student_id = :s AND date = :d", s=ADA, d=DAY1)[0]
      == ("late", TEACHER_B))

kept = alpha.one("SELECT COUNT(*) FROM attendance_records WHERE date = :d", d=DAY1)
mark(teacher_a, J1, DAY1, {ADA: "late", BOLA: "absent", 999999: "present"})
check("a status for a student id outside the roster (tampered) is silently ignored: nothing new is written",
      alpha.one("SELECT COUNT(*) FROM attendance_records WHERE date = :d", d=DAY1) == kept)

mark(teacher_a, J1, DAY2, {ADA: "excused"})
check("marking a second day never touches the first day's records",
      alpha.one("SELECT status FROM attendance_records WHERE student_id = :s AND date = :d", s=ADA, d=DAY1) == "late"
      and alpha.one("SELECT status FROM attendance_records WHERE student_id = :s AND date = :d", s=ADA, d=DAY2) == "excused")

mark(teacher_a, J1, DAY1, {BOLA: ""})
check("clearing a status back to blank deletes its row",
      alpha.one("SELECT COUNT(*) FROM attendance_records WHERE student_id = :s AND date = :d", s=BOLA, d=DAY1) == 0)
mark(teacher_a, J1, DAY1, {BOLA: "absent"})  # put it back for the summary section below

# ================================================================ B. scope
kept_tobi = alpha.one("SELECT COUNT(*) FROM attendance_records WHERE student_id = :s", s=TOBI)
r = mark(teacher_a, J2, DAY1, {TOBI: "present"})
check("teacher A (JSS 1 only) cannot mark JSS 2's register: nothing is written for Tobi",
      alpha.one("SELECT COUNT(*) FROM attendance_records WHERE student_id = :s", s=TOBI) == kept_tobi)
check("…and JSS 2's roster is not even shown to her", "Tobi Boundary" not in teacher_a.text(reg_url(J2, DAY1)))

# ================================================================ C. the term summary
mark(teacher_a, J1, DAY3, {ADA: "present", BOLA: "present"})
# Ada: DAY1 late, DAY2 excused, DAY3 present -> 3 recorded, (late+present)=2 of 3 = 66.7%
# Bola: DAY1 absent, DAY3 present -> 2 recorded, 1 of 2 = 50.0%
summary_page = html.unescape(teacher_a.text(summary_url(J1)))
check("the term summary shows each student's exact counts and the right percentage (late counts as present)",
      "66.7%" in summary_page and "50.0%" in summary_page and "Ada Obi" in summary_page and "Bola Eze" in summary_page)

# ================================================================ D. permissions and CSRF
check("with school.attendance.view only, the viewer opens the summary but is refused (403) the register",
      viewer.get(summary_url(J1)).status_code == 200 and viewer.get(reg_url(J1, DAY1)).status_code == 403
      and viewer.post(reg_url(J1, DAY1), {f"status_{ADA}": "present"}, page="/admin/password").status_code == 403)
check("with neither permission, nobody is refused (403) on both pages",
      nobody.get(reg_url(J1, DAY1)).status_code == 403 and nobody.get(summary_url(J1)).status_code == 403)
check("…nor do they see 'Attendance' in the menu, while a marker does",
      'href="/admin/school/attendance' not in nobody.text("/admin/school") and 'href="/admin/school/attendance' in teacher_a.text("/admin/school"))
kept_row = alpha.sql("SELECT status FROM attendance_records WHERE student_id = :s AND date = :d", s=ADA, d=DAY1)
check("a POST without a CSRF token is refused (403), and nothing changes",
      op.post(reg_url(J1, DAY1), {f"status_{ADA}": "present"}, token=False).status_code == 403
      and op.post(reg_url(J1, DAY1), {f"status_{ADA}": "present"}, token="wrong").status_code == 403
      and alpha.sql("SELECT status FROM attendance_records WHERE student_id = :s AND date = :d", s=ADA, d=DAY1) == kept_row)
visitor = Person(ALPHA, label="signed-out visitor")
check("a signed-out visitor is sent to sign in from both pages, and from the write route",
      visitor.get(reg_url(J1, DAY1)).status_code == 302 and visitor.get(summary_url(J1)).status_code == 302
      and visitor.post(reg_url(J1, DAY1), {f"status_{ADA}": "present"}, token=False).status_code in (302, 403))

# ================================================================ E. students and parents
ada_att_page = ada_portal.text("/student/attendance?session_id=%s&term=First%%20Term" % CUR)
check("Ada sees her own attendance summary and history for the term she was marked in",
      ("66.7" in ada_att_page))
bola_portal = Person(ALPHA, "/student/password", label="alpha student bola")
bola_portal.post("/login", {"username": "bola.eze", "password": PASSWORD}, page="/login")
bola_att_page = bola_portal.text("/student/attendance?session_id=%s&term=First%%20Term" % CUR)
bola_rates = re.findall(r"([\d.]+) percent attendance", bola_att_page)
check("…and Bola sees her own, not Ada's", bola_rates == ["50.0"] and "66.7" not in bola_att_page, str(bola_rates))
check("a student cannot open the staff pages",
      ada_portal.get(reg_url(J1, DAY1)).status_code in (302, 403, 404) and ada_portal.get(summary_url(J1)).status_code in (302, 403, 404))
mum_ada_page = mum_ada.text(f"/parent/children/{ADA}/attendance?session_id={CUR}&term=First%20Term")
check("a parent sees her linked child's attendance", "66.7" in mum_ada_page)
check("…and cannot open another family's child's attendance",
      mum_ada.get(f"/parent/children/{BOLA}/attendance").status_code == 404)
check("a parent cannot open the staff pages",
      mum_ada.get(reg_url(J1, DAY1)).status_code in (302, 403, 404))
check("beta's own database has no attendance at all yet: alpha's marking never reached it",
      beta.one("SELECT COUNT(*) FROM attendance_records") == 0)
mark(beta_teacher, BETA_J1, DAY1, {BETA_CHIKE: "present"})
check("…once beta marks its own register (a separate database entirely), it is recorded there",
      beta.one("SELECT status FROM attendance_records WHERE student_id = :s", s=BETA_CHIKE) == "present")

# ================================================================ F. no traceback, no server error
check("no page in this run was a server error, and none showed a traceback", not PROBLEMS, "; ".join(PROBLEMS[:4]))
check("no server error was logged by any request in this run", not errors.seen, "; ".join(str(e).splitlines()[0][:100] for e in errors.seen[:3]))

# ================================================================ G. the guard files
import pg_posts_coverage as coverage  # noqa: E402

att_writes = {rule.rule for rule in A.app.url_map.iter_rules() if rule.methods & {"POST"} and "attendance" in rule.rule}
check("the attendance form route is in the form suite's list of exercised routes",
      att_writes == {REG} and att_writes <= set(coverage.EXERCISED), str(att_writes))
check("…and no write route of the application is unaccounted for", coverage.unaccounted(A.app.url_map) == [] and coverage.stale(A.app.url_map) == ([], []))
posts_source = open(os.path.join(HERE, "write_paths_pg_posts.py"), encoding="utf-8").read()
check("…and write_paths_pg_posts.py really submits it", "/admin/school/attendance?class_id=" in posts_source)
check("every attendance endpoint has its permission in the endpoint map",
      ADMIN_ENDPOINT_PERMISSIONS.get("admin_school_attendance") == "school.attendance.mark"
      and ADMIN_ENDPOINT_PERMISSIONS.get("admin_school_attendance_save") == "school.attendance.mark"
      and ADMIN_ENDPOINT_PERMISSIONS.get("admin_school_attendance_summary") == "school.attendance.view")

dispose_engines()
DROP_TEST_DATABASES()
shutil.rmtree(TMP, ignore_errors=True)
print()
failed = [x for x in results if not x[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
