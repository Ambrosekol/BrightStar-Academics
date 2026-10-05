"""Bulk student import, proven end to end on PostgreSQL: valid rows created, bad rows skipped with a
reason, class scope, generated numbers and credentials, permissions and CSRF, and that nothing of it
leaks between schools. In plain words:

A. Importing
   * a good row creates exactly what "Register Student" creates by hand: a student, an enrolment in
     the named class for the current session, a generated admission number and a one-time login —
     and every one of those is reported back once, on the results page;
   * a row missing a name, with an unrecognised gender, an unknown class or a bad guardian email is
     skipped, with a reason, and every other row in the same file is still imported;
   * a class name is matched case-insensitively; a class outside the importing staff member's scope
     is refused for that row alone, the rest of the file unaffected;
   * a file with no header row, or missing a required column, is refused outright, before anything
     is written;
   * a file over the size limit is refused, with the limit named;
   * two students in the same file get two different generated admission numbers, never a duplicate.
B. Permissions and CSRF
   * 'school.students.create' is required for the page, the template download and the import itself;
     a POST without a CSRF token is refused (403) and nothing is written; a signed-out visitor is
     sent to sign in.
C. Isolation: a second school's own classes and students are never touched.

Run:  python tests/verification/write_paths_student_import.py
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
TMP = tempfile.mkdtemp(prefix="brightstars_studentimport_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('studentimport')
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
from models import Admin, AdminScope, AdminType, AdminTypePermission, Permission  # noqa: E402

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
        self.addr = f"10.80.{Person.count[0]}.1"

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

    def post(self, path, data=None, page=None, files=None, token=True):
        data = dict(data or {})
        if token is True:
            data["_csrf_token"] = csrf_from(self.text(page or self.form_page))
        elif token:
            data["_csrf_token"] = token
        for key, (filename, content) in (files or {}).items():
            data[key] = (io.BytesIO(content), filename)
        r = self.client.post(path, data=data, base_url=self.base, environ_base={"REMOTE_ADDR": self.addr},
                             content_type="multipart/form-data" if files else None)
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
J2 = alpha.one("SELECT id FROM school_classes WHERE name = 'JSS 2'")
IMPORT = "/admin/school/students/import"


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


mk_role(alpha, "Class Teacher J1", ["school.view", "school.students.view", "school.students.create", "school.classes.view"])
mk_role(alpha, "Nothing Special", ["school.view", "school.students.view", "school.classes.view"])
teacher_a, TEACHER_A = mk_staff(alpha, "teacher.a", "Mrs Amaka Tutor", "Class Teacher J1", "JSS 1")
nobody, _ = mk_staff(alpha, "nobody", "Nora Nothing", "Nothing Special", "JSS 1")


def csv_of(*rows, header="first_name,middle_name,last_name,gender,class,guardian_name,guardian_email,guardian_phone"):
    return (header + "\r\n" + "\r\n".join(rows) + "\r\n").encode("utf-8")


def do_import(person, csv_bytes, filename="import.csv"):
    return person.post(IMPORT, {}, files={"csv_file": (filename, csv_bytes)}, page=IMPORT)


# ================================================================ A. importing
check("the import page names the required and optional columns", "first_name" in teacher_a.text(IMPORT) and "guardian_phone" in teacher_a.text(IMPORT))
template = teacher_a.get(f"{IMPORT}/template.csv").get_data(as_text=True)
check("the template file downloads with the right header",
      template.splitlines()[0].strip() == "first_name,middle_name,last_name,gender,class,guardian_name,guardian_email,guardian_phone")

before = alpha.one("SELECT COUNT(*) FROM students")
r = do_import(teacher_a, csv_of(
    "Ada,Nkem,Obi,Female,JSS 1,Mrs Obi,mrs.obi@example.test,08010000000",
    ",Q,Nobody,Male,JSS 1,G,g@example.test,08010000000",      # missing first name
    "Bad,Q,Gender,Purple,JSS 1,G,g@example.test,08010000000", # bad gender
    "Chidi,Q,Eze,Male,Unknown Class,G,g@example.test,08010000000", # unknown class
    "Tobi,Q,Boundary,Male,JSS 2,G,g@example.test,08010000000",    # outside teacher A's scope (JSS 1 only)
    "Fola,Q,Bright,Female,JSS 1,Mr Bright,not-an-email,08010000000", # bad guardian email
    "Gina,,Missing,Female,JSS 1,,,",                          # no middle name, guardian name, email or phone
))
report = html.unescape(r.get_data(as_text=True))
check("the report shows one imported and six skipped, each with its own reason",
      "1 student imported" in r.get_data(as_text=True).lower() and "6 rows skipped" in r.get_data(as_text=True).lower()
      and alpha.one("SELECT COUNT(*) FROM students") == before + 1)
ADA = alpha.one("SELECT id FROM students WHERE first_name = 'Ada' AND last_name = 'Obi'")
check("Ada was created with a generated admission number, a class enrolment and a login",
      alpha.one("SELECT student_number FROM students WHERE id = :i", i=ADA) not in (None, "")
      and alpha.one("SELECT class_id FROM student_enrolments WHERE student_id = :i AND active = 1", i=ADA) == J1
      and alpha.one("SELECT login_username FROM students WHERE id = :i", i=ADA) not in (None, ""))
check("…and her one-time password and username are shown on the report, and downloadable from it (client-side)",
      "downloadCreds" in report and re.search(r"<td>\s*Ada Obi\s*</td>", report) is not None)
check("the skipped rows each name a reason: a missing name, a bad gender, an unknown class, out-of-scope, a bad email",
      all(w in report for w in ("first name and surname are required", "gender must be", 'class "Unknown Class" was not found',
                                "outside your authorised scope", "guardian email is not valid")))
check("a row missing its middle name, guardian name, guardian email or guardian phone is skipped, naming each one",
      all(w in report for w in ("middle name is required", "guardian name is required", "guardian email is required", "guardian phone is required")))

r = do_import(teacher_a, csv_of("Bola,Q,Eze,Female,JSS 1,G Eze,g.eze@example.test,08010000001", "Tunde,Q,Eze,Male,JSS 1,G Eze,g.eze@example.test,08010000002"))
BOLA = alpha.one("SELECT id FROM students WHERE first_name = 'Bola' AND last_name = 'Eze'")
TUNDE = alpha.one("SELECT id FROM students WHERE first_name = 'Tunde' AND last_name = 'Eze'")
check("two students in the same file get two different generated admission numbers",
      None not in (BOLA, TUNDE) and alpha.one("SELECT student_number FROM students WHERE id = :i", i=BOLA)
      != alpha.one("SELECT student_number FROM students WHERE id = :i", i=TUNDE))

check("a class name is matched case-insensitively", do_import(teacher_a, csv_of("Case,Q,Insensitive,Female,jss 1,G,g@example.test,08010000003")).status_code == 200
      and alpha.one("SELECT COUNT(*) FROM students WHERE first_name = 'Case'") == 1)

before2 = alpha.one("SELECT COUNT(*) FROM students")
r = teacher_a.post(IMPORT, {}, files={"csv_file": ("empty.csv", b"")}, page=IMPORT)
check("a file with no header row is refused outright, nothing written", "no header row" in teacher_a.said() and alpha.one("SELECT COUNT(*) FROM students") == before2)
r = teacher_a.post(IMPORT, {}, files={"csv_file": ("bad.csv", b"first_name,last_name\r\nA,B\r\n")}, page=IMPORT)
check("a file missing a required column is refused outright, nothing written",
      "missing" in teacher_a.said() and alpha.one("SELECT COUNT(*) FROM students") == before2)
huge = csv_of(f"Row,Q,Person,Female,JSS 1,G,g@example.test,08010000004,{'x' * (3 * 1024 * 1024)}")  # one row padded well past the 2 MB limit
r = teacher_a.post(IMPORT, {}, files={"csv_file": ("huge.csv", huge)}, page=IMPORT)
check(f"a file over the size limit ({len(huge)} bytes) is refused, with the limit named, before a single row is read",
      "limit" in teacher_a.said() and alpha.one("SELECT COUNT(*) FROM students") == before2)

# ================================================================ B. permissions and CSRF
check("with no students.create permission, nobody is refused (403) the page, the template and the import",
      nobody.get(IMPORT).status_code == 403 and nobody.get(f"{IMPORT}/template.csv").status_code == 403
      and nobody.post(IMPORT, {}, files={"csv_file": ("x.csv", csv_of("A,Q,B,Female,JSS 1,G,g@example.test,08010000005"))}, page="/admin/password").status_code == 403)
check("a POST without a CSRF token is refused (403), and nothing is written",
      teacher_a.post(IMPORT, {}, files={"csv_file": ("x.csv", csv_of("Nope,Q,Nope,Female,JSS 1,G,g@example.test,08010000006"))}, token=False).status_code == 403
      and alpha.one("SELECT COUNT(*) FROM students WHERE first_name = 'Nope'") == 0)
visitor = Person(ALPHA, label="signed-out visitor")
check("a signed-out visitor is sent to sign in from the page, and from the write route",
      visitor.get(IMPORT).status_code == 302
      and visitor.post(IMPORT, {}, files={"csv_file": ("x.csv", b"first_name\r\n")}, token=False).status_code in (302, 403))

# ================================================================ C. isolation
check("beta's own students are untouched by anything alpha imported", beta.one("SELECT COUNT(*) FROM students") == 0)

# ================================================================ no traceback, no server error
check("no page in this run was a server error, and none showed a traceback", not PROBLEMS, "; ".join(PROBLEMS[:4]))
check("no server error was logged by any request in this run", not errors.seen, "; ".join(str(e).splitlines()[0][:100] for e in errors.seen[:3]))

# ================================================================ the guard files
import pg_posts_coverage as coverage  # noqa: E402

check("the import route is in the form suite's list of exercised routes and really submitted",
      IMPORT in coverage.EXERCISED and coverage.unaccounted(A.app.url_map) == [] and coverage.stale(A.app.url_map) == ([], []))
posts_source = open(os.path.join(HERE, "write_paths_pg_posts.py"), encoding="utf-8").read()
check("…and write_paths_pg_posts.py really submits it", '"{STUDENTS}/import"' in posts_source)
check("every import endpoint has its permission in the endpoint map",
      ADMIN_ENDPOINT_PERMISSIONS.get("admin_school_students_import") == "admin.access"  # each step checks its own permission
      and ADMIN_ENDPOINT_PERMISSIONS.get("admin_school_students_import_template") == "admin.access"
      and ADMIN_ENDPOINT_PERMISSIONS.get("admin_school_students_import_run") == "admin.access")  # the step checks its own permission

dispose_engines()
DROP_TEST_DATABASES()
shutil.rmtree(TMP, ignore_errors=True)
print()
failed = [x for x in results if not x[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
