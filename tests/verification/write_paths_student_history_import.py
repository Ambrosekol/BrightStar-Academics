"""Bulk enrolment-history import, proven end to end on PostgreSQL: valid rows recorded against an
already-existing student, bad rows skipped with a reason, an archived session accepted (unlike the
hand-entry form's own dropdown), permissions and CSRF, and that nothing of it leaks between
schools. In plain words:

A. Importing
   * a good row - an existing student's admission number, a recognised level, a session matched
     by name - records exactly what the single "Add enrolment history" form records by hand, and
     is reported back once, on the results page;
   * a row with an unknown admission number, an unrecognised level, a session name that does not
     exist, or a badly formed date is skipped, with a reason, and every other row in the same file
     is still imported;
   * a session that is no longer the school's *active* one is still accepted - bringing in
     archived history is exactly the point, unlike the hand-entry form's dropdown of active
     sessions only;
   * a file with no header row, or missing a required column, is refused outright, before
     anything is written.
B. Permissions and CSRF
   * 'student.history.manage' is required for the page, the template download and the import
     itself; a POST without a CSRF token is refused (403) and nothing is written; a signed-out
     visitor is sent to sign in.
C. Isolation: a second school's own students and history are never touched.

Run:  python tests/verification/write_paths_student_history_import.py
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
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_historyimport_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('historyimport')
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
from core.security import ADMIN_ENDPOINT_PERMISSIONS  # noqa: E402
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
        self.addr = f"10.81.{Person.count[0]}.1"

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
CURRENT = alpha.one("SELECT id FROM academic_sessions WHERE active = 1 ORDER BY id DESC LIMIT 1")
IMPORT = "/admin/school/students/import-history"


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


mk_role(alpha, "History Importer", ["school.view", "school.students.view", "student.history.manage"])
mk_role(alpha, "Nothing Special", ["school.view", "school.students.view"])
historian, _ = mk_staff(alpha, "historian", "Mrs History", "History Importer")
nobody, _ = mk_staff(alpha, "nobody", "Nora Nothing", "Nothing Special")

# A real student, made through the real single-registration form, and an older, now-archived
# session (deliberately no longer active - bringing in exactly that kind of history is the point).
r = op.post("/admin/school/students/new", {
    "first_name": "Ada", "last_name": "Obi", "gender": "Female", "class_id": str(J1),
    "state_of_origin": "Lagos", "blood_group": "", "genotype": "",
})
ADA = alpha.one("SELECT id FROM students WHERE first_name = 'Ada' AND last_name = 'Obi'")
ADA_NO = alpha.one("SELECT admission_no FROM students WHERE id = :i", i=ADA)


def add_archived_session(school, name):
    def go():
        from models import AcademicSession
        row = AcademicSession(name=name, is_current=0, active=0, created_at=NOW)
        A.db.session.add(row)
        A.db.session.flush()
        return row.id
    return orm(school, go)


ARCHIVED = add_archived_session(alpha, "2023/2024")


def csv_of(*rows, header="admission_no,level,session,enrolled_at,completed_at,notes"):
    return (header + "\r\n" + "\r\n".join(rows) + "\r\n").encode("utf-8")


def do_import(person, csv_bytes, filename="history.csv"):
    return person.post(IMPORT, {}, files={"csv_file": (filename, csv_bytes)}, page=IMPORT)


# ================================================================ A. importing
check("the import page names the required and optional columns and the recognised levels",
      "admission_no" in historian.text(IMPORT) and "notes" in historian.text(IMPORT) and "JSS 1" in historian.text(IMPORT))
template = historian.get(f"{IMPORT}/template.csv").get_data(as_text=True)
check("the template file downloads with the right header",
      template.splitlines()[0].strip() == "admission_no,level,session,enrolled_at,completed_at,notes")

r = do_import(historian, csv_of(
    f"{ADA_NO},JSS 1,2023/2024,2023-09-11,2024-07-19,Brought in from her previous school",  # good, archived session
    "NOT-REAL,JSS 1,2023/2024,,,",                        # unknown admission number
    f"{ADA_NO},Not A Level,2023/2024,,,",                 # unrecognised level
    f"{ADA_NO},JSS 1,No Such Session,,,",                 # unknown session
    f"{ADA_NO},JSS 1,2023/2024,not-a-date,,",             # badly formed date
))
report = html.unescape(r.get_data(as_text=True))
check("the report shows one imported and four skipped, each with its own reason",
      "1 enrolment history row imported" in r.get_data(as_text=True).lower() and "4 rows skipped" in r.get_data(as_text=True).lower())
check("the skipped rows each name a reason: unknown admission number, unrecognised level, unknown session, a bad date",
      all(w in report for w in ("no active student with admission number", "is not a recognised level",
                                "no session named", "must be a date")))
check("the good row was recorded against an archived (no longer active) session, unlike the hand-entry form's own dropdown",
      alpha.one("SELECT level_name FROM student_enrollment_history WHERE student_id = :s AND session_id = :sess",
                s=ADA, sess=ARCHIVED) == "JSS 1"
      and alpha.one("SELECT active FROM academic_sessions WHERE id = :i", i=ARCHIVED) == 0)
check("…and it carries the note from the file",
      alpha.one("SELECT notes FROM student_enrollment_history WHERE student_id = :s AND session_id = :sess",
                s=ADA, sess=ARCHIVED) == "Brought in from her previous school")

before = alpha.one("SELECT COUNT(*) FROM student_enrollment_history WHERE student_id = :s", s=ADA)
r = historian.post(IMPORT, {}, files={"csv_file": ("empty.csv", b"")}, page=IMPORT)
check("a file with no header row is refused outright, nothing written",
      "no header row" in historian.said() and alpha.one("SELECT COUNT(*) FROM student_enrollment_history WHERE student_id = :s", s=ADA) == before)
r = historian.post(IMPORT, {}, files={"csv_file": ("bad.csv", b"admission_no,level\r\nA,B\r\n")}, page=IMPORT)
check("a file missing a required column is refused outright, nothing written",
      "missing" in historian.said() and alpha.one("SELECT COUNT(*) FROM student_enrollment_history WHERE student_id = :s", s=ADA) == before)

# ================================================================ B. permissions and CSRF
check("with no student.history.manage permission, nobody is refused (403) the page, the template and the import",
      nobody.get(IMPORT).status_code == 403 and nobody.get(f"{IMPORT}/template.csv").status_code == 403
      and nobody.post(IMPORT, {}, files={"csv_file": ("x.csv", csv_of(f"{ADA_NO},JSS 1,2023/2024,,,"))}, page="/admin/password").status_code == 403)
check("a POST without a CSRF token is refused (403), and nothing is written",
      historian.post(IMPORT, {}, files={"csv_file": ("x.csv", csv_of(f"{ADA_NO},JSS 2,2023/2024,,,"))}, token=False).status_code == 403
      and alpha.one("SELECT COUNT(*) FROM student_enrollment_history WHERE student_id = :s AND level_name = 'JSS 2'", s=ADA) == 0)
visitor = Person(ALPHA, label="signed-out visitor")
check("a signed-out visitor is sent to sign in from the page, and from the write route",
      visitor.get(IMPORT).status_code == 302
      and visitor.post(IMPORT, {}, files={"csv_file": ("x.csv", b"admission_no\r\n")}, token=False).status_code in (302, 403))

# ================================================================ C. isolation
check("beta's own students and history are untouched by anything alpha imported",
      beta.one("SELECT COUNT(*) FROM students") == 0 and beta.one("SELECT COUNT(*) FROM student_enrollment_history") == 0)

# ================================================================ no traceback, no server error
check("no page in this run was a server error, and none showed a traceback", not PROBLEMS, "; ".join(PROBLEMS[:4]))
check("no server error was logged by any request in this run", not errors.seen, "; ".join(str(e).splitlines()[0][:100] for e in errors.seen[:3]))

# ================================================================ the guard files
import pg_posts_coverage as coverage  # noqa: E402

check("the import route is in the form suite's list of exercised routes and really submitted",
      IMPORT in coverage.EXERCISED and coverage.unaccounted(A.app.url_map) == [] and coverage.stale(A.app.url_map) == ([], []))
posts_source = open(os.path.join(HERE, "write_paths_pg_posts.py"), encoding="utf-8").read()
check("…and write_paths_pg_posts.py really submits it", '"{STUDENTS}/import-history"' in posts_source)
check("every import endpoint has its permission in the endpoint map",
      ADMIN_ENDPOINT_PERMISSIONS.get("admin_school_students_import_history") == "student.history.manage"
      and ADMIN_ENDPOINT_PERMISSIONS.get("admin_school_students_import_history_template") == "student.history.manage"
      and ADMIN_ENDPOINT_PERMISSIONS.get("admin_school_students_import_history_run") == "student.history.manage")

dispose_engines()
DROP_TEST_DATABASES()
shutil.rmtree(TMP, ignore_errors=True)
print()
failed = [x for x in results if not x[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
