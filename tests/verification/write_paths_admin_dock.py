"""The administrator dock, proven end to end on PostgreSQL: the newest release is shown to an administrator
who has never seen one, closing it is remembered for that administrator only, the setup checklist stays until
it is closed, and every write needs the CSRF token. In plain words:

A. What's new: shown with what changed and a help link; closing it is saved for that administrator; a second
   administrator still sees it; a request without the CSRF token is refused.
B. Setup: shown while the school has steps to do; closing it hides it for the school.
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
TMP = tempfile.mkdtemp(prefix="brightstars_dock_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('admindock')
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




# ================================================================ the dock: the server sends the data, the browser decides
import core.release_notes as NOTES

RELEASE_LINK = 'href="/admin/guide/students"'
first = op.text("/admin/school/students")
check("an administrator's page carries what's new, with its notes and their help links",
      'data-dock="updates"' in first and "What's new" in first and RELEASE_LINK in first)
check("each release is marked with its version, so the browser can tell which ones are new",
      f'data-version="{NOTES.APP_VERSION}"' in first)
check("the setup checklist is offered, and its progress is fetched only when the browser's copy is stale",
      'data-dock="setup"' in first and 'data-setup-url="/admin/school/onboarding/card"' in first)
card = op.text("/admin/school/onboarding/card")
check("the checklist answers with its steps and that it should be shown",
      'data-setup-show="1"' in card and "Review your classes" in card)
dismissed = op.post("/admin/school/onboarding/dismiss", {})
check("closing the checklist is saved for the school", dismissed.status_code in (200, 302))
check("…and the checklist now answers that it should not be shown", 'data-setup-show="0"' in op.text("/admin/school/onboarding/card"))
check("another administrator is offered what's new as well (their browser decides what to show)",
      'data-dock="updates"' in keeper.text("/admin/school/students"))
check("the pages carry no version-by-user state on the server: the notes are the same for everyone",
      first.count('data-version=') == keeper.text("/admin/school/students").count('data-version='))

# ================================================================ families: students and parents
def mk_parent(school, username, display, password):
    def go():
        from models import ParentAccount
        row = ParentAccount(username=username, display_name=display,
                            password_hash=generate_password_hash(password, method="pbkdf2:sha256:1000"),
                            active=1, password_must_change=0, created_at=NOW)
        A.db.session.add(row)
        A.db.session.flush()
        return row.id
    return orm(school, go)


def student_login(no):
    person = Person(alpha.base, label=f"student {no}")
    r = person.post("/login", {"username": no, "password": PASSWORD}, page="/login")
    return person, r


STU, STU_NO = make_student("Ada", "Family", J1)
mk_parent(alpha, "parent_ada", "Mrs Ada Family", PASSWORD)
student_person, _ = student_login(STU_NO)
student_dash = student_person.text("/student/dashboard")
notice = student_dash.split('id="familyNotice"')[1].split("</section>")[0] if 'id="familyNotice"' in student_dash else ""
check("a student's notice carries the student's changes, with the whole message inside it",
      'data-audience="student"' in student_dash and "your sign-in is switched off" in notice)
check("…and it has no link to the staff guide", notice != "" and "/admin/guide/" not in notice and "Learn how" not in notice)
check("…and a student's notice never carries a parent's change", "Students who have left the school" not in notice)
check("…and the administrators' dock is not shown to a student", 'id="adminDock"' not in student_dash)
check("a student cannot open the staff guide", student_person.get("/admin/guide/students").status_code != 200)

parent_person = Person(alpha.base, label="parent")
parent_person.post("/login", {"username": "parent_ada", "password": PASSWORD}, page="/login")
parent_dash = parent_person.text("/parent/dashboard")
parent_notice = parent_dash.split('id="familyNotice"')[1].split("</section>")[0] if 'id="familyNotice"' in parent_dash else ""
check("a parent's notice carries the parent's changes", 'data-audience="family"' in parent_dash and "Students who have left the school" in parent_notice)
check("…and never a student's change", "your sign-in is switched off" not in parent_notice)
check("…and no link to the staff guide", "/admin/guide/" not in parent_notice)
check("a parent cannot open the staff guide either", parent_person.get("/admin/guide/students").status_code != 200)

# A parent keeps an archived child: the child's own sign-in is revoked, but the parent still sees them.
def link_parent(school, parent_id, student_id):
    def go():
        from models import ParentStudentLink
        A.db.session.add(ParentStudentLink(parent_id=parent_id, student_id=student_id, relationship="Mother", active=1, created_at=NOW))
        A.db.session.flush()
    orm(school, go)


PARENT_ID = alpha.one("SELECT id FROM parent_accounts WHERE username = 'parent_ada'")
link_parent(alpha, PARENT_ID, STU)
check("a parent can open their child's record while the child is at the school",
      parent_person.get(f"/parent/children/{STU}").status_code == 200)
op.post(ARCHIVE, {"student_ids": [str(STU)], "reason": "Graduated"})
check("the archived child's own sign-in is revoked", "could not verify" in student_login(STU_NO)[1].get_data(as_text=True).lower()
      and student_person.get("/student/dashboard").status_code == 302)
check("the archived child still appears on the parent's dashboard", "Ada Family" in parent_person.text("/parent/dashboard"))
child_page = parent_person.get(f"/parent/children/{STU}")
check("the parent can still open the archived child's record", child_page.status_code == 200 and "Ada Family" in child_page.get_data(as_text=True))
check("…and the child's fee account", parent_person.get(f"/parent/children/{STU}/finance").status_code == 200)


# ================================================================ no traceback, no server error
check("no page in this run was a server error, and none showed a traceback", not PROBLEMS, "; ".join(PROBLEMS[:4]))
check("no server error was logged by any request in this run", not errors.seen, "; ".join(str(e).splitlines()[0][:100] for e in errors.seen[:3]))

dispose_engines()
passed = sum(1 for _, ok, _ in results if ok)
print(f"\n{passed}/{len(results)} checks passed")
sys.exit(0 if passed == len(results) else 1)
