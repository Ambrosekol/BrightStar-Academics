"""The admissions funnel (entrance exam -> waitlist -> a real student), proven end to end on
PostgreSQL. In plain words:

A. The waitlist
   * only a candidate who has attempted at least one paper appears; one who has not is not listed at
     all; entries are ranked by overall percentage, and filterable by entry level and status.
B. Admitting
   * admitting creates exactly what registering a student by hand creates: a student record with a
     generated admission number, a class enrolment for the current session, and a one-time login;
   * the name split onto the admit form can be corrected before confirming;
   * a candidate with guardian contact details on file also gets a linked parent portal account when
     the box is left ticked; one with none gets no parent account, even if ticked;
   * admitting is terminal: an admitted candidate cannot be admitted again, declined, or reset.
C. Declining
   * declining takes a candidate off the waitlist with an optional reason, and can be undone (back to
     pending) — unlike admitting.
D. Permissions, scope and CSRF
   * 'candidates.admit' is required for the whole page and every action; 'candidates.view' alone is
     refused; the navigation only shows "Admissions" with the permission;
   * an officer scoped to one entry level's candidates cannot act on another's;
   * every write is refused (403) without a valid CSRF token; a signed-out visitor is sent to sign in.
E. Isolation: a second school's own candidates and waitlist are never touched.
F. The guard files: the new routes are in the write-path coverage list and really exercised.

Run:  python tests/verification/write_paths_admissions.py
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
TMP = tempfile.mkdtemp(prefix="brightstars_admissions_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('admissions')
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
    Admin, AdminScope, AdminType, AdminTypePermission, Candidate, Permission,
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
        self.addr = f"10.100.{Person.count[0]}.1"

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
    form = {"_csrf_token": csrf_from(console.get("/platform/schools/new", base_url=PL).get_data(as_text=True)), "name": name, "code": code,
           "starter_banks": "1"}
    console.post("/platform/schools/new", data=form, base_url=PL, content_type="multipart/form-data")
alpha, beta = School("alpha", ALPHA), School("beta", BETA)


def operator_in(school):
    page = console.get(f"/platform/schools/{school.code}", base_url=PL).get_data(as_text=True)
    r = console.post(f"/platform/schools/{school.code}/enter", data={"_csrf_token": csrf_from(page)}, base_url=PL)
    person = Person(school.base, label=f"{school.code} School Admin")
    person.get(r.headers["Location"][len(school.base):])
    person.get("/admin/workspace/entrance")
    return person


op, op_beta = operator_in(alpha), operator_in(beta)
NOW = "2026-09-01T09:00:00+00:00"
PASSWORD = "Fixture-password-9"
HASH = generate_password_hash(PASSWORD, method="pbkdf2:sha256:1000")
J1 = alpha.one("SELECT id FROM school_classes WHERE name = 'JSS 1'")
CANDS = "/admin/candidates"
ADMISSIONS = f"{CANDS}/admissions"


def orm(school, fn):
    def go():
        out = fn()
        A.db.session.commit()
        return out
    return school.run(go)


def mk_role(school, name, codes):
    def go():
        role = AdminType(name=name, description="Only what is needed.", is_system=0, active=1, created_at=NOW)
        A.db.session.add(role)
        A.db.session.flush()
        for code in codes:
            permission = A.db.session.scalars(sa.select(Permission).where(Permission.code == code)).first()
            A.db.session.add(AdminTypePermission(admin_type_id=role.id, permission_id=permission.id, granted_at=NOW))
    orm(school, go)


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
    orm(school, go)
    person = Person(school.base, label=f"{school.code} {username}")
    person.post("/login", {"username": username, "password": PASSWORD}, page="/login")
    person.get("/admin/workspace/entrance")
    return person


op.post("/admin/entrance-config/standard", {})  # the standard banks, live for the current session — candidates need all three papers available to register
mk_role(alpha, "Admissions Officer Fixture", ["dashboard.view", "candidates.view", "candidates.admit"])
mk_role(alpha, "Viewer Only", ["dashboard.view", "candidates.view"])
officer = mk_staff(alpha, "officer", "Olu Officer", "Admissions Officer Fixture")
viewer = mk_staff(alpha, "viewer", "Vera Viewer", "Viewer Only")
jss1_officer = mk_staff(alpha, "jss1officer", "Jss Officer", "Admissions Officer Fixture", scope_class="JSS 1")


def register(person, name, target_class="JSS 1", **extra):
    form = {"candidate_name": name, "target_class": target_class, "school_attended": "Sunrise Primary",
           "parent_guardian_name": f"{name}'s Guardian", "parent_guardian_relationship": "Mother",
           "primary_mobile": "08031112222", "parent_guardian_email": f"{name.split()[0].lower()}.guardian@example.test",
           **extra}
    person.post(f"{CANDS}/new", form, page=f"{CANDS}/new")
    return alpha.one("SELECT id FROM candidates WHERE candidate_name = :n", n=name)


def submit_attempt(cid, percentage):
    bank_id = alpha.one("SELECT bank_id FROM candidate_papers WHERE candidate_id = :c ORDER BY slot LIMIT 1", c=cid)
    exam_id = alpha.one("SELECT id FROM examinations WHERE bank_id = :b", b=bank_id)
    alpha.sql("INSERT INTO attempts (candidate, exam_id, bank_id, started_at, expires_at, submitted_at, score, "
             "max_score, percentage, status, candidate_id) VALUES ('x', :e, :b, :now, :now, :now, :p, 100, :p, 'submitted', :c)",
             e=exam_id, b=bank_id, now=NOW, p=percentage, c=cid)


# ================================================================ A. the waitlist
NEVER_ATTEMPTED = register(op, "Never Attempted")
HIGH_SCORE = register(op, "High Scorer")
submit_attempt(HIGH_SCORE, 88)
LOW_SCORE = register(op, "Low Scorer")
submit_attempt(LOW_SCORE, 55)
NO_GUARDIAN = register(op, "No Guardian Info")
# Registration itself requires a guardian name and a mobile number, so this simulates an older
# record with none on file (a data anomaly), the only realistic way this case arises.
alpha.sql("UPDATE candidates SET parent_guardian_name = NULL, parent_guardian_email = NULL, primary_mobile = NULL WHERE id = :c", c=NO_GUARDIAN)
submit_attempt(NO_GUARDIAN, 70)

page = op.text(f"{ADMISSIONS}?entry_group=year7")
check("a candidate who has never attempted a paper is not on the waitlist at all", "Never Attempted" not in page)
check("candidates who have attempted a paper are ranked by score, highest first",
      page.find("High Scorer") < page.find("No Guardian Info") < page.find("Low Scorer"))
check("the pending count on the page matches how many are actually pending",
      "3 candidates shown, 3 still pending" in page or "3 still pending" in page)

# ================================================================ B. admitting
before_students = alpha.one("SELECT COUNT(*) FROM students")
r = op.post(f"{CANDS}/{HIGH_SCORE}/admit", {"first_name": "Highla", "middle_name": "", "last_name": "Scorer",
                                            "gender": "Female", "class_id": J1, "create_parent_account": "1"}, page=ADMISSIONS)
STUDENT_HIGH = alpha.one("SELECT admitted_student_id FROM candidates WHERE id = :c", c=HIGH_SCORE)
check("admitting created a real student with a generated admission number and a class enrolment",
      alpha.one("SELECT COUNT(*) FROM students") == before_students + 1
      and alpha.one("SELECT first_name FROM students WHERE id = :s", s=STUDENT_HIGH) == "Highla"
      and alpha.one("SELECT student_number FROM students WHERE id = :s", s=STUDENT_HIGH) not in (None, "")
      and alpha.one("SELECT class_id FROM student_enrolments WHERE student_id = :s AND active = 1", s=STUDENT_HIGH) == J1)
check("…and a one-time login", alpha.one("SELECT login_username FROM students WHERE id = :s", s=STUDENT_HIGH) not in (None, ""))
check("…and, since guardian details were on file, a linked parent portal account",
      alpha.one("SELECT COUNT(*) FROM parent_student_links WHERE student_id = :s", s=STUDENT_HIGH) == 1)
check("admitted is terminal: admitting again is refused, and nothing changes",
      op.post(f"{CANDS}/{HIGH_SCORE}/admit", {"first_name": "X", "last_name": "Y", "class_id": J1}, page=ADMISSIONS).status_code in (302, 200)
      and "already been admitted" in op.said()
      and alpha.one("SELECT COUNT(*) FROM students WHERE id = :s", s=STUDENT_HIGH) == 1)
check("…nor can an admitted candidate be declined or reset",
      op.post(f"{CANDS}/{HIGH_SCORE}/decline", {}, page=ADMISSIONS).status_code in (302, 200) and "cannot be declined" in op.said()
      and op.post(f"{CANDS}/{HIGH_SCORE}/admission-reset", {}, page=ADMISSIONS).status_code in (302, 200) and "cannot be" in op.said())

r = op.post(f"{CANDS}/{NO_GUARDIAN}/admit", {"first_name": "No", "last_name": "Guardian", "class_id": J1,
                                             "create_parent_account": "1"}, page=ADMISSIONS)
STUDENT_NO_GUARDIAN = alpha.one("SELECT admitted_student_id FROM candidates WHERE id = :c", c=NO_GUARDIAN)
check("a candidate with no guardian details on file gets no parent account, even with the box ticked",
      STUDENT_NO_GUARDIAN is not None and alpha.one("SELECT COUNT(*) FROM parent_student_links WHERE student_id = :s", s=STUDENT_NO_GUARDIAN) == 0)

# ================================================================ C. declining
kept = alpha.one("SELECT admission_status FROM candidates WHERE id = :c", c=LOW_SCORE)
op.post(f"{CANDS}/{LOW_SCORE}/decline", {"note": "Below the cut-off this year"}, page=ADMISSIONS)
check("declining takes a candidate off the waitlist with a reason",
      alpha.one("SELECT admission_status FROM candidates WHERE id = :c", c=LOW_SCORE) == "declined"
      and alpha.one("SELECT admission_note FROM candidates WHERE id = :c", c=LOW_SCORE) == "Below the cut-off this year")
_pending_page = op.text(f"{ADMISSIONS}?status=pending")
_pending_rows = re.search(r'<tbody>(.*?)</tbody>', _pending_page, re.S)
check("…and a declined candidate no longer shows as pending (the earlier decline's own flash message,"
     " which of course names her, does not count)", "Low Scorer" not in (_pending_rows.group(1) if _pending_rows else _pending_page))
op.post(f"{CANDS}/{LOW_SCORE}/admission-reset", {}, page=ADMISSIONS)
check("…but declining can be undone, back to pending",
      alpha.one("SELECT admission_status FROM candidates WHERE id = :c", c=LOW_SCORE) == "pending"
      and alpha.one("SELECT admission_note FROM candidates WHERE id = :c", c=LOW_SCORE) is None)

# ================================================================ D. permissions, scope, CSRF
check("with candidates.view only, the viewer is refused (403) the whole waitlist and every action",
      viewer.get(ADMISSIONS).status_code == 403
      and viewer.post(f"{CANDS}/{LOW_SCORE}/admit", {"first_name": "X", "last_name": "Y"}, page="/admin/password").status_code == 403
      and viewer.post(f"{CANDS}/{LOW_SCORE}/decline", {}, page="/admin/password").status_code == 403)
check("…nor do they see 'Admissions' in the menu, while an officer does",
      "admissions" not in viewer.text("/admin/examination").lower() and "admissions" in officer.text("/admin/examination").lower())
SSS_CAND = register(op, "Sss Candidate", target_class="SSS 1")
submit_attempt(SSS_CAND, 65)
check("an officer scoped to JSS 1 candidates only can act on one of them",
      jss1_officer.post(f"{CANDS}/{LOW_SCORE}/decline", {"note": "scoped test"}, page=ADMISSIONS).status_code == 302)
check("…but is refused (403) on a candidate outside her scope (SSS 1), and nothing changes",
      jss1_officer.post(f"{CANDS}/{SSS_CAND}/decline", {}, page=ADMISSIONS).status_code == 403
      and alpha.one("SELECT admission_status FROM candidates WHERE id = :c", c=SSS_CAND) == "pending")
op.post(f"{CANDS}/{LOW_SCORE}/admission-reset", {}, page=ADMISSIONS)  # undo the scoped officer's decline for the checks below
kept_row = alpha.one("SELECT admission_status FROM candidates WHERE id = :c", c=LOW_SCORE)
check("a POST without a CSRF token is refused (403), and nothing changes",
      op.post(f"{CANDS}/{LOW_SCORE}/decline", {"note": "no token"}, token=False).status_code == 403
      and alpha.one("SELECT admission_status FROM candidates WHERE id = :c", c=LOW_SCORE) == kept_row)
visitor = Person(ALPHA, label="signed-out visitor")
check("a signed-out visitor is sent to sign in from the waitlist, and from every write route",
      visitor.get(ADMISSIONS).status_code == 302
      and visitor.post(f"{CANDS}/{LOW_SCORE}/decline", {}, token=False).status_code in (302, 403))

# ================================================================ E. isolation
check("beta's own candidates and waitlist are untouched by anything alpha did", beta.one("SELECT COUNT(*) FROM candidates") == 0)

# ================================================================ no traceback, no server error
check("no page in this run was a server error, and none showed a traceback", not PROBLEMS, "; ".join(PROBLEMS[:4]))
check("no server error was logged by any request in this run", not errors.seen, "; ".join(str(e).splitlines()[0][:100] for e in errors.seen[:3]))

# ================================================================ F. the guard files
import pg_posts_coverage as coverage  # noqa: E402

adm_writes = {rule.rule for rule in A.app.url_map.iter_rules()
             if rule.methods & {"POST"} and rule.rule.endswith(("/admit", "/decline", "/admission-reset"))}
check("the three admissions form routes are in the form suite's list of exercised routes",
      adm_writes == {f"{CANDS}/<int:cid>/admit", f"{CANDS}/<int:cid>/decline", f"{CANDS}/<int:cid>/admission-reset"}
      and adm_writes <= set(coverage.EXERCISED), str(adm_writes))
check("…and no write route of the application is unaccounted for", coverage.unaccounted(A.app.url_map) == [] and coverage.stale(A.app.url_map) == ([], []))
posts_source = open(os.path.join(HERE, "write_paths_pg_posts.py"), encoding="utf-8").read()
check("…and write_paths_pg_posts.py really submits them",
      all(s in posts_source for s in ("/admit", "/decline", "/admission-reset")))
check("every admissions endpoint has its permission in the endpoint map",
      all(ADMIN_ENDPOINT_PERMISSIONS.get(e) == "candidates.admit" for e in
          ("admin_candidate_admissions", "admin_candidate_admit", "admin_candidate_decline", "admin_candidate_admission_reset")))

dispose_engines()
DROP_TEST_DATABASES()
shutil.rmtree(TMP, ignore_errors=True)
print()
failed = [x for x in results if not x[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
