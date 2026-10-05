"""Past results, imported in bulk and proven end to end on PostgreSQL: one row per student, subject, term and
session; every valid row is released and appears on the student's record; a result that is already there is
never overwritten; an archived graduate's past results are imported too; and only a staff member who may
release results can run the import. In plain words:

A. Importing
   * a row with an exam (and optionally a test) becomes released results for that subject, term and session,
     with the importing administrator recorded and a workflow entry saying where they came from;
   * a row with no test records only the exam;
   * a row with a bad admission number, session, subject or term, a score above its maximum, a test with no
     maximum, a score that is not a number, or a result that already exists is skipped with its reason, and
     every other row in the same file is still imported.
B. Graduates: an archived student's past results are imported, and their record shows them.
C. Permissions and CSRF: only a staff member who may release results can run the import; a POST without a
   CSRF token is refused (403) and nothing is written.
D. Isolation: a second school's results are never touched.

Run:  python tests/verification/write_paths_results_import.py
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
TMP = tempfile.mkdtemp(prefix="brightstars_results_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('resultsimport')
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

    def post(self, path, data=None, page=None, token=True, files=None):
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
mk_role(alpha, "Results Releaser", ["admin.access", "school.view", "school.students.view", "school.results.release"])
releaser, _ = mk_staff(alpha, "releaser", "Rita Release", "Results Releaser")


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




# ================================================================ fixtures for results
IMPORT = "/admin/school/students/import"


def mk_subject(school, name):
    def go():
        from models import SchoolSubject
        row = SchoolSubject(name=name, created_at=NOW, active=1)
        A.db.session.add(row)
        A.db.session.flush()
        return row.id
    return orm(school, go)


def mk_session(school, name):
    def go():
        from models import AcademicSession
        row = AcademicSession(name=name, is_current=0, active=0, created_at=NOW)
        A.db.session.add(row)
        A.db.session.flush()
        return row.id
    return orm(school, go)


MATH = mk_subject(alpha, "Mathematics")
ENG = mk_subject(alpha, "English Language")
PAST = mk_session(alpha, "2023/2024")
RESULTS = "/admin/school/students/import-results"
RESULTS_TEMPLATE = f"{RESULTS}/template.csv"
RESULTS_HEADER = "admission_no,session,term,subject,test_score,test_max,exam_score,exam_max,notes"


def results_csv(*rows):
    return (RESULTS_HEADER + "\r\n" + "\r\n".join(rows) + "\r\n").encode("utf-8")


def do_results(person, data, token=True):
    return person.post(RESULTS, {}, files={"csv_file": ("results.csv", data)}, token=token)


ADA, ADA_NO = make_student("Ada", "Results", J1)
BOLA, BOLA_NO = make_student("Bola", "Graduate", J1)

template = op.get(RESULTS_TEMPLATE).get_data(as_text=True)
check("the results template downloads with the right header", template.splitlines()[0].strip() == RESULTS_HEADER)

# ================================================================ A. importing
r = do_results(op, results_csv(
    f"{ADA_NO},2023/2024,First Term,Mathematics,18,20,45,60,",
    f"{ADA_NO},2023/2024,First Term,English Language,,,52,60,Test not recorded",
    f"{ADA_NO},2023/2024,Second Term,Mathematics,16,20,48,60,",
    f"{ADA_NO},2023/2024,First Term,Mathematics,18,20,45,60,",       # already recorded
    f"{ADA_NO},2023/2024,First Term,Mathematics,20,20,70,60,",       # exam above its maximum
    f"{ADA_NO},2023/2024,Third Term,Mathematics,12,,40,60,",         # test with no maximum
    f"{ADA_NO},2023/2024,Third Term,Physics,10,20,30,60,",           # no such subject
    f"{ADA_NO},2019/2020,First Term,Mathematics,10,20,30,60,",       # no such session
    f"NOT-REAL,2023/2024,First Term,Mathematics,1,2,3,4,",           # no such student
    f"{ADA_NO},2023/2024,Full Session,Mathematics,1,2,3,4,",         # not a term
    f"{ADA_NO},2023/2024,First Term,Mathematics,abc,20,45,60,",      # not a number
))
body_text = r.get_data(as_text=True)
check("the report shows three imported and eight skipped", "3 rows imported" in body_text and "8 rows skipped" in body_text)
check("…each skipped row names its reason",
      all(w in body_text for w in ("already has a result", "must be between 0 and its maximum", "both be given",
                                   "no subject named", "no session named", "no student with admission number",
                                   "term must be", "must be numbers")))

math_first = alpha.sql("""SELECT component_name, score, max_score, status, source_type, released_at, session_id
                          FROM school_student_results
                          WHERE student_id = :s AND subject_id = :m AND term = 'First Term'
                          ORDER BY component_name""", s=ADA, m=MATH)
check("Ada's first-term mathematics is recorded as a test and an exam",
      [row[0] for row in math_first] == ["Exam", "Test"])
check("…both components are released, from a migration, and carry their scores and maximums",
      all(row[3] == "released" and row[4] == "migration" and row[5] for row in math_first)
      and {row[0]: (row[1], row[2]) for row in math_first} == {"Exam": (45.0, 60.0), "Test": (18.0, 20.0)})
check("…against the session they belong to", all(row[6] == PAST for row in math_first))
english = alpha.sql("SELECT component_name FROM school_student_results WHERE student_id = :s AND subject_id = :e", s=ADA, e=ENG)
check("a row with no test records only the exam", [row[0] for row in english] == ["Exam"])
events = alpha.one("""SELECT COUNT(*) FROM result_workflow_events e JOIN school_student_results r ON r.id = e.result_id
                      WHERE r.student_id = :s AND e.to_status = 'released'""", s=ADA)
check("each migrated component has a workflow entry that says it was migrated", events == 5)
check("the record already there was left as it was",
      alpha.one("""SELECT score FROM school_student_results
                   WHERE student_id = :s AND subject_id = :m AND term = 'First Term' AND component_name = 'Exam'""",
                s=ADA, m=MATH) == 45.0)

# ================================================================ B. graduates
op.post(ARCHIVE, {"student_ids": [str(BOLA)], "reason": "Graduated"})
r = do_results(op, results_csv(f"{BOLA_NO},2023/2024,First Term,Mathematics,15,20,40,60,"))
check("an archived graduate's past result is imported", "1 row imported" in r.get_data(as_text=True)
      and alpha.one("SELECT COUNT(*) FROM school_student_results WHERE student_id = :s", s=BOLA) == 2)
record = op.get(f"/admin/school/students/archived/{BOLA}/record").get_data(as_text=True)
check("…and the graduate's record shows it", "Mathematics" in record and "First Term" in record)

# ================================================================ C. permissions and CSRF
check("the import step is on the bulk import page for someone who may release results",
      "Import past results" in op.text(IMPORT))
check("a staff member who may only release results can open the page, and sees only that step",
      "Import past results" in releaser.text(IMPORT) and "do not have permission to import student history" in releaser.text(IMPORT))
check("someone with no bulk-import permission is refused the page", keeper.get(IMPORT).status_code == 403)
r = do_results(releaser, results_csv(f"{BOLA_NO},2023/2024,Second Term,Mathematics,11,20,35,60,"))
check("…and a releaser can run the import", "1 row imported" in r.get_data(as_text=True))
check("someone who may not release results is refused the template", keeper.get(RESULTS_TEMPLATE).status_code == 403)
keeper.post(RESULTS, {}, files={"csv_file": ("r.csv", results_csv(f"{ADA_NO},2023/2024,Third Term,Mathematics,10,20,30,60,"))})
check("…and the import itself writes nothing for them",
      alpha.one("SELECT COUNT(*) FROM school_student_results WHERE student_id = :s AND term = 'Third Term'", s=ADA) == 0)
r = nobody.post(RESULTS, {}, files={"csv_file": ("r.csv", results_csv(f"{ADA_NO},2023/2024,Third Term,Mathematics,10,20,30,60,"))})
check("…and a staff member with no results permission is refused (403)", r.status_code == 403)
r = do_results(op, results_csv(f"{ADA_NO},2023/2024,Third Term,Mathematics,10,20,30,60,"), token=False)
check("a POST without a CSRF token is refused (403), and nothing is written",
      r.status_code == 403 and alpha.one("SELECT COUNT(*) FROM school_student_results WHERE student_id = :s AND term = 'Third Term'", s=ADA) == 0)

# ================================================================ C2. the bulk-import permission
mk_role(alpha, "Bulk Importer", ["admin.access", "school.view", "school.bulk_import"])
bulk, bulk_id = mk_staff(alpha, "bulk", "Bayo Bulk", "Bulk Importer")
check("a staff member with only the bulk-import permission sees all three import steps",
      all(w in bulk.text(IMPORT) for w in ("Import students", "Import enrolment history", "Import past results")))
r = do_results(bulk, results_csv(f"{ADA_NO},2023/2024,Third Term,Mathematics,10,20,30,60,"))
check("…and can run the past-results import with no results permission of their own", "1 row imported" in r.get_data(as_text=True))
check("…and the import is recorded in the audit log with who ran it and the file name",
      alpha.one("SELECT COUNT(*) FROM audit_logs WHERE action = 'academic_results_bulk_imported' AND admin_id = :a AND details LIKE '%results.csv%'",
                a=bulk_id) == 1)

# ================================================================ D. isolation
check("a second school's results are never touched", beta.one("SELECT COUNT(*) FROM school_student_results") == 0)

# ================================================================ no traceback, no server error
check("no page in this run was a server error, and none showed a traceback", not PROBLEMS, "; ".join(PROBLEMS[:4]))
check("no server error was logged by any request in this run", not errors.seen, "; ".join(str(e).splitlines()[0][:100] for e in errors.seen[:3]))

dispose_engines()
passed = sum(1 for _, ok, _ in results if ok)
print(f"\n{passed}/{len(results)} checks passed")
sys.exit(0 if passed == len(results) else 1)
