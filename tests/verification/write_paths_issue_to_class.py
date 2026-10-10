"""Issuing assignments and projects to a whole class, proven end to end on PostgreSQL: a whole-class assignment
and project reach every student enrolled in the class, the form offers the choice, and choosing students with none
ticked is still refused. Run:  python tests/verification/write_paths_issue_to_class.py
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
TMP = tempfile.mkdtemp(prefix="brightstars_issuewhole_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('issuewhole')
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




# ================================================================ issuing to a whole class
def mk_subject(school, name):
    def go():
        from models import SchoolSubject
        row = SchoolSubject(name=name, created_at=NOW, active=1)
        A.db.session.add(row)
        A.db.session.flush()
        return row.id
    return orm(school, go)


MATH = mk_subject(alpha, "Mathematics")
CUR_SESSION = alpha.one("SELECT id FROM academic_sessions WHERE active = 1 ORDER BY id DESC LIMIT 1")
ASSIGN_NEW = "/admin/school/assignments/new"
PROJECT_NEW = "/admin/school/projects/new"

for name, last in (("Whole", "Classone"), ("Second", "Classtwo")):
    make_student(name, last, J1)
IN_CLASS = alpha.one("SELECT COUNT(*) FROM student_enrolments e JOIN students s ON s.id = e.student_id WHERE e.class_id = :c AND e.active = 1 AND s.active = 1 AND e.session_id = :s", c=J1, s=CUR_SESSION)

form = op.text(ASSIGN_NEW)
check("the assignment form offers a whole-class choice, with the class's students as the default",
      'name="issue_to"' in form and 'value="class"' in form and 'id="studentSection"' in form)

def base_fields(title, issue_to):
    return {"title": title, "class_id": str(J1), "subject_id": str(MATH), "session_id": str(CUR_SESSION),
            "term": "First Term", "assignment_type": "written", "timing_mode": "untimed",
            "issue_to": issue_to, "instructions": "Whole class work"}

before = alpha.one("SELECT COUNT(*) FROM assignment_students")
r = op.post(ASSIGN_NEW, base_fields("Whole class homework", "class"))
issued = alpha.one("SELECT COUNT(*) FROM assignment_students a JOIN school_assignments s ON s.id = a.assignment_id WHERE s.title = 'Whole class homework'")
check("an assignment issued to the whole class reaches every student enrolled in it", r.status_code == 302 and issued == IN_CLASS and IN_CLASS >= 2, f"{issued} of {IN_CLASS}")

r = op.post(ASSIGN_NEW, {**base_fields("Picked homework", "students"), "student_ids": []})
check("an assignment for selected students with none chosen is still refused", r.status_code == 200 and "Select at least one student" in r.get_data(as_text=True))

r = op.post(PROJECT_NEW, {"title": "Whole class project", "class_id": str(J1), "subject_id": str(MATH), "session_id": str(CUR_SESSION),
                          "term": "First Term", "date_given": "2026-10-01", "issue_to": "class"})
pissued = alpha.one("SELECT COUNT(*) FROM project_students p JOIN school_projects s ON s.id = p.project_id WHERE s.title = 'Whole class project'")
check("a project issued to the whole class reaches every student enrolled in it", r.status_code == 302 and pissued == IN_CLASS, f"{pissued} of {IN_CLASS}")

check("the assignments page lists the class's work", 'class="ac-row"' in op.text(f"/admin/school/assignments?class={alpha.one('SELECT name FROM school_classes WHERE id = :c', c=J1)}"))
check("the projects page lists the class's work", 'class="ac-row"' in op.text(f"/admin/school/projects?class={alpha.one('SELECT name FROM school_classes WHERE id = :c', c=J1)}"))

# ================================================================ no traceback, no server error
check("no page in this run was a server error, and none showed a traceback", not PROBLEMS, "; ".join(PROBLEMS[:4]))
check("no server error was logged by any request in this run", not errors.seen, "; ".join(str(e).splitlines()[0][:100] for e in errors.seen[:3]))

dispose_engines()
passed = sum(1 for _, ok, _ in results if ok)
print(f"\n{passed}/{len(results)} checks passed")
sys.exit(0 if passed == len(results) else 1)
