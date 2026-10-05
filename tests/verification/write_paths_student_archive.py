"""Archiving students, proven end to end on PostgreSQL: an archived student leaves the register, the
student count and sign-in at once; the archived list and each archived student's academic record
are for school administrators only; restoring brings everything back; and nothing crosses schools.
In plain words:

A. Archiving
   * one student, and several at once, can be archived from the register; each goes off the register,
     their sign-in stops working (even for a session already open), and the archive date, reason and
     archiving administrator are recorded;
   * an already archived student is left as it is, and a student outside a class-scoped teacher's
     classes is left alone;
   * the existing deactivate switch refuses an archived student, so they cannot be re-enabled by accident.
B. Lists and records
   * the archived list shows archived students only, and each one's record shows their class placement;
   * a student who was never archived has no archived record and gets no page for one.
C. Permissions and CSRF
   * only a school administrator sees the archived list and records, and only one can restore a student;
     another staff member with school.students.view is refused both;
   * a POST without a CSRF token is refused (403) and nothing is archived.
D. Restoring
   * a restored student is back on the register, their sign-in works again and the archive details are cleared.
E. Isolation
   * a second school's students are never touched.

Run:  python tests/verification/write_paths_student_archive.py
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
TMP = tempfile.mkdtemp(prefix="brightstars_archive_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('studentarchive')
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
        self.addr = f"10.82.{Person.count[0]}.1"

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


ADA, ADA_NO = make_student("Ada", "Archive", J1)
TUNDE, TUNDE_NO = make_student("Tunde", "Stays", J1)
BOLA, BOLA_NO = make_student("Bola", "Bulk", J1)
CHIKA, CHIKA_NO = make_student("Chika", "Scoped", S1)
DEE, DEE_NO = make_student("Dee", "Outside", J1)
for sid in (ADA, TUNDE, BOLA, CHIKA, DEE):
    check(f"the student {sid} was registered with a sign-in", alpha.one("SELECT login_username FROM students WHERE id = :i", i=sid) not in (None, ""))

ACTIVE_BEFORE = alpha.one("SELECT COUNT(*) FROM students WHERE active = 1")


def student_login(no):
    person = Person(alpha.base, label=f"student {no}")
    r = person.post("/login", {"username": no, "password": PASSWORD}, page="/login")
    return person, r


# Ada signs in before the archive, so the later refusal is a real change.
ada_person, r = student_login(ADA_NO)
check("before the archive, the student's sign-in works", r.status_code in (301, 302, 303) and "/student" in r.headers.get("Location", ""))
check("…and their dashboard opens", ada_person.get("/student/dashboard").status_code == 200)

# ================================================================ A. archiving
r = op.post(ARCHIVE, {"student_ids": [str(ADA)], "reason": "Left the school", "class": "JSS 1"})
check("a school administrator can archive a student from the register", r.status_code == 302, str(r.status_code))
row = alpha.sql("SELECT active, archived_at, archive_reason, archived_by_admin_id FROM students WHERE id = :i", i=ADA)[0]
check("the student is switched off, dated, given the reason and the administrator who archived them",
      row[0] == 0 and row[1] and row[2] == "Left the school" and row[3] is not None, str(row))
check("the register's flash reports the archive", "archived" in op.said())
check("the student count drops by exactly one", alpha.one("SELECT COUNT(*) FROM students WHERE active = 1") == ACTIVE_BEFORE - 1)
register_page = op.text(REGISTER)
check("the archived student has gone from the class register", ADA_NO not in register_page)
check("…and the student who stayed is still on it", TUNDE_NO in register_page)

check("an open session of the archived student is turned away", ada_person.get("/student/dashboard").status_code == 302)
_, r = student_login(ADA_NO)
check("the archived student cannot sign in again", "could not verify" in r.get_data(as_text=True).lower())

before_date = alpha.one("SELECT archived_at FROM students WHERE id = :i", i=ADA)
op.post(ARCHIVE, {"student_ids": [str(ADA)], "reason": "Second attempt", "class": "JSS 1"})
check("archiving an already archived student changes nothing",
      alpha.one("SELECT archived_at FROM students WHERE id = :i", i=ADA) == before_date
      and alpha.one("SELECT archive_reason FROM students WHERE id = :i", i=ADA) == "Left the school")

op.post(ARCHIVE, {"student_ids": [str(BOLA), str(TUNDE)], "reason": "Transferred out"})
check("several students can be archived in one go",
      alpha.one("SELECT COUNT(*) FROM students WHERE id IN (:a,:b) AND active = 0 AND archived_at IS NOT NULL", a=BOLA, b=TUNDE) == 2)
op.post(RESTORE, {"student_ids": [str(TUNDE)]})
check("the restored one of those two comes back to the register", alpha.one("SELECT active FROM students WHERE id = :i", i=TUNDE) == 1)
op.post(ARCHIVE, {"student_ids": [str(TUNDE)], "reason": "Transferred out"})
check("…and can be archived again", alpha.one("SELECT active FROM students WHERE id = :i", i=TUNDE) == 0)

r = op.post(ARCHIVE, {"student_ids": [str(DEE)], "reason": "No token"}, token=False)
check("an archive POST without a CSRF token is refused (403)", r.status_code == 403 and alpha.one("SELECT active FROM students WHERE id = :i", i=DEE) == 1, str(r.status_code))

op.post(f"/admin/school/students/{ADA}/toggle", {})
check("the deactivate switch refuses an archived student",
      alpha.one("SELECT active FROM students WHERE id = :i", i=ADA) == 0
      and alpha.one("SELECT archived_at FROM students WHERE id = :i", i=ADA) is not None
      and "restore" in op.said())

keeper.post(ARCHIVE, {"student_ids": [str(DEE)], "reason": "Outside my class"})
check("a class teacher cannot archive a student outside their class",
      alpha.one("SELECT active FROM students WHERE id = :i", i=DEE) == 1)
keeper.post(ARCHIVE, {"student_ids": [str(CHIKA)], "reason": "Moved on"})
check("…but can archive a student in their own class",
      alpha.one("SELECT active FROM students WHERE id = :i", i=CHIKA) == 0)
keeper.post(RESTORE, {"student_ids": [str(CHIKA)]})
check("…and cannot restore a student, even one in their class",
      alpha.one("SELECT active FROM students WHERE id = :i", i=CHIKA) == 0)

# ================================================================ B. lists and records
archived_page = op.text(ARCHIVED_LIST)
check("the archived list shows the archived student", ADA_NO in archived_page)
check("…and not a student still on the register", DEE_NO not in archived_page and CHIKA_NO in archived_page)
check("…the archived list is reached from the Archived students tab", "Archived students" in archived_page)

record = op.get(f"/admin/school/students/archived/{ADA}/record")
body = record.get_data(as_text=True)
check("each archived student's academic record opens", record.status_code == 200, str(record.status_code))
check("…and shows their class placement", "JSS 1" in body and "Class placements" in body)
check("…and says why they were archived", "Left the school" in body)
check("a student who was never archived has no archived record", op.get(f"/admin/school/students/archived/{DEE}/record").status_code == 404)

# ================================================================ C. permissions
check("a staff member without the school-admin role is refused the archived list", ADA_NO not in nobody.text(ARCHIVED_LIST))
check("…and refused an archived student's record",
      nobody.get(f"/admin/school/students/archived/{ADA}/record").status_code != 200)
nobody.post(RESTORE, {"student_ids": [str(ADA)]})
check("…and cannot restore anyone", alpha.one("SELECT active FROM students WHERE id = :i", i=ADA) == 0)

# ================================================================ D. restoring
r = op.post(RESTORE, {"student_ids": [str(ADA)]})
row = alpha.sql("SELECT active, archived_at, archive_reason, archived_by_admin_id FROM students WHERE id = :i", i=ADA)[0]
check("a school administrator can restore an archived student", row[0] == 1 and row[1] is None and row[2] is None and row[3] is None, str(row))
check("the restored student is back on the register", ADA_NO in op.text(REGISTER))
check("…and their sign-in works again", student_login(ADA_NO)[1].status_code in (301, 302, 303))

# ================================================================ E. isolation
check("a second school's students are never touched", beta.one("SELECT COUNT(*) FROM students WHERE archived_at IS NOT NULL") == 0)

# ================================================================ report
check("no page in this run was a server error, and none showed a traceback", not PROBLEMS, "; ".join(PROBLEMS[:4]))
check("no server error was logged by any request in this run", not errors.seen, "; ".join(str(e).splitlines()[0][:100] for e in errors.seen[:3]))

dispose_engines()
passed = sum(1 for _, ok, _ in results if ok)
print(f"\n{passed}/{len(results)} checks passed")
sys.exit(0 if passed == len(results) else 1)
