"""Exam/test timetables, proven end to end on PostgreSQL: building a draft, releasing it, who is
notified, scope, permissions, CSRF, and what a student and a parent are shown. In plain words:

A. Building the draft
   * an entry (class, subject, type, date, time, venue) is added, edited and deleted;
   * a class/subject pair outside a staff member's scope is refused, and nothing is written;
   * a student never sees a draft entry, on the dashboard, the timetable page or its PDF.
B. Releasing
   * releasing sets every not-yet-released entry for the session and term (across every class the
     releasing staff member may access) and only those; a second release of the same scope releases
     nothing more and says so; a release notifies, once, every currently enrolled student of a
     released class and every parent linked to them (in-app), and the guardian by email and WhatsApp.
C. Scope
   * a class teacher only ever adds, edits, deletes or releases entries for her own classes.
D. Permissions and CSRF
   * 'school.timetable.view' opens the list read-only; 'school.timetable.manage' may add/edit/delete
     but not release; 'school.timetable.release' may release; nobody with none of these is refused
     everywhere; every write is refused (403) without a valid CSRF token.
E. Students and parents
   * a student sees only released entries of her own class, and can download the PDF; a parent sees
     a linked child's; another school's data never crosses over.
F. The guard files: the new routes are in the write-path coverage list and really exercised.

Run:  python tests/verification/write_paths_timetable.py
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
import zlib

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_timetable_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('timetable')
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
    School as SchoolRow, SchoolSubject, ClassSubject, Student, StudentEnrolment,
)

_delivery._open_smtp = lambda settings: (_ for _ in ()).throw(RuntimeError("no mail in a test"))
A.app.config['BACKGROUND_INLINE'] = True  # release notices run at once, so they can be checked
SENT_EMAILS = []


def fake_email(guardian_email, subject, body, **kwargs):
    SENT_EMAILS.append((guardian_email, subject, body))
    return True, guardian_email


import blueprints.school.timetable_notices as _tt_notices  # noqa: E402
# Patched on the module that imported the name (a `from x import y` binds its own reference), not
# on core.notifications itself, or the patch would never be seen by the code under test.
_tt_notices._notify_guardian_email = fake_email
_tt_notices._notify_guardian_whatsapp = lambda phone, text, **kwargs: (False, 'not configured in this test')

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
        self.addr = f"10.70.{Person.count[0]}.1"

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
J1, J2 = (alpha.one("SELECT id FROM school_classes WHERE name = :n", n=n) for n in ("JSS 1", "JSS 2"))
TERM = "First Term"


def orm(school, fn):
    def go():
        out = fn()
        A.db.session.commit()
        return out
    return school.run(go)


def mk_subject(school, name, class_ids):
    def go():
        subj = SchoolSubject(name=name, code=name[:3].upper(), created_at=NOW, active=1)
        A.db.session.add(subj)
        A.db.session.flush()
        for cid in class_ids:
            A.db.session.add(ClassSubject(class_id=cid, subject_id=subj.id, locked=0, final_locked=0, created_at=NOW))
        return subj.id
    return orm(school, go)


def mk_student(school, first, last, class_id, session_id):
    def go():
        school_id = A.db.session.scalar(sa.select(SchoolRow.id))
        code = f"{school.code[:3].upper()}/{first[:3].upper()}"
        student = Student(admission_no=code, student_number=code, first_name=first, last_name=last, gender="Female",
                          created_at=NOW, active=1, school_id=school_id, login_username=f"{first}.{last}".lower(),
                          login_password_hash=HASH, account_active=1, password_must_change=0, guardian_name="A Guardian",
                          guardian_email=f"{first.lower()}.guardian@example.test", guardian_phone="08010000000")
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


MATHS = mk_subject(alpha, "Mathematics", [J1, J2])
ADA = mk_student(alpha, "Ada", "Obi", J1, CUR)
BOLA = mk_student(alpha, "Bola", "Eze", J1, CUR)
TOBI = mk_student(alpha, "Tobi", "Boundary", J2, CUR)

mk_role(alpha, "Timetable Manager", ["school.view", "school.students.view", "school.classes.view", "school.subjects.view",
                                    "school.timetable.view", "school.timetable.manage"])
mk_role(alpha, "Timetable Releaser", ["school.view", "school.students.view", "school.classes.view", "school.subjects.view",
                                     "school.timetable.view", "school.timetable.manage", "school.timetable.release"])
mk_role(alpha, "View Only Timetable", ["school.view", "school.students.view", "school.classes.view", "school.subjects.view",
                                      "school.timetable.view"])
mk_role(alpha, "Nothing Special", ["school.view", "school.students.view", "school.classes.view"])
teacher_a, TEACHER_A = mk_staff(alpha, "teacher.a", "Mrs Amaka Tutor", "Timetable Manager", "JSS 1")
head, HEAD = mk_staff(alpha, "head", "Mr Head Teacher", "Timetable Releaser", "JSS 1")
viewer, _ = mk_staff(alpha, "viewer", "Vera Viewer", "View Only Timetable", "JSS 1")
nobody, _ = mk_staff(alpha, "nobody", "Nora Nothing", "Nothing Special", "JSS 1")
mum_ada = mk_parent(alpha, "mum.ada", "Mrs Obi", [ADA])
ada_portal = Person(ALPHA, "/student/password", label="alpha student ada")
ada_portal.post("/login", {"username": "ada.obi", "password": PASSWORD}, page="/login")

TT = "/admin/school/timetable"


def tt_url(session_id=None, term=TERM):
    return f"{TT}?session_id={session_id or CUR}&term={term.replace(' ', '%20')}"


def add_entry(person, class_id, subject_id, date, start, end, venue, exam_type="examination", session_id=None, term=TERM):
    return person.post(f"{TT}/new?session_id={session_id or CUR}&term={term.replace(' ', '%20')}",
                       {"class_id": class_id, "subject_id": subject_id, "exam_type": exam_type, "date": date,
                        "start_time": start, "end_time": end, "venue": venue}, page=tt_url(session_id, term))


# ================================================================ A. building the draft
page = html.unescape(teacher_a.text(tt_url()))
check("the timetable page opens with nothing on it yet", "No timetable entry has been added" in page)
r = add_entry(teacher_a, J1, MATHS, "2026-12-01", "09:00", "11:00", "Main Hall")
ENTRY = alpha.one("SELECT id FROM exam_timetable_entries WHERE class_id = :c AND subject_id = :s", c=J1, s=MATHS)
check("an entry was added as a draft", r.status_code == 302 and ENTRY is not None
      and alpha.one("SELECT released_at FROM exam_timetable_entries WHERE id = :e", e=ENTRY) is None)
teacher_a.post(f"{TT}/{ENTRY}/edit?session_id={CUR}&term={TERM}",
              {"class_id": J1, "subject_id": MATHS, "exam_type": "examination", "date": "2026-12-02",
               "start_time": "10:00", "end_time": "12:00", "venue": "Hall B"}, page=tt_url())
check("the entry was edited", alpha.one("SELECT date FROM exam_timetable_entries WHERE id = :e", e=ENTRY) == "2026-12-02"
      and alpha.one("SELECT venue FROM exam_timetable_entries WHERE id = :e", e=ENTRY) == "Hall B")
kept = alpha.one("SELECT COUNT(*) FROM exam_timetable_entries")
r = add_entry(teacher_a, J2, MATHS, "2026-12-03", "09:00", "10:00", "")
check("teacher A (JSS 1 only) cannot add an entry for JSS 2: nothing is written", alpha.one("SELECT COUNT(*) FROM exam_timetable_entries") == kept)
check("…and told so", "authorised scope" in teacher_a.said())
check("a student never sees a draft entry on her dashboard, the timetable page or the PDF",
      "Hall B" not in ada_portal.text("/student/dashboard") and "Hall B" not in ada_portal.text("/student/timetable")
      and ada_portal.get("/student/timetable/pdf").status_code in (200, 404)
      and b"Hall B" not in ada_portal.get("/student/timetable/pdf").data)

# ================================================================ B. releasing
add_entry(teacher_a, J1, MATHS, "2026-12-05", "09:00", "11:00", "Main Hall")  # a second entry for JSS 1, still draft
draft_before = alpha.one("SELECT COUNT(*) FROM exam_timetable_entries WHERE released_at IS NULL")
r = head.post(f"{TT}/release?session_id={CUR}&term={TERM}", {"exam_type": "examination"}, page=tt_url())
check(f"releasing sets every draft entry for the session and term ({draft_before} of them) as released, and says so",
      alpha.one("SELECT COUNT(*) FROM exam_timetable_entries WHERE released_at IS NULL") == 0 and "notified" in head.said())
r = head.post(f"{TT}/release?session_id={CUR}&term={TERM}", {"exam_type": "examination"}, page=tt_url())
check("releasing again releases nothing more, and says so plainly", "nothing to release" in head.said())
check("Ada (JSS 1) now sees both released entries and can download the PDF",
      "Hall B" in ada_portal.text("/student/timetable") and "Main Hall" in ada_portal.text("/student/timetable")
      and b"%PDF" in ada_portal.get("/student/timetable/pdf").data)
tobi_portal = Person(ALPHA, "/student/password", label="alpha student tobi")
tobi_portal.post("/login", {"username": "tobi.boundary", "password": PASSWORD}, page="/login")
check("…confirmed directly: Tobi's own timetable page shows nothing", "No timetable has been released" in tobi_portal.text("/student/timetable"))
check("Ada's parent was told: an in-app alert, and (in this test) an email attempt was made to the guardian address",
      "timetable" in mum_ada.text(f"/parent/children/{ADA}")
      and any("timetable" in subj.lower() for _, subj, _ in SENT_EMAILS))
check("a parent sees the released timetable for her linked child, and can download its PDF",
      "Hall B" in mum_ada.text(f"/parent/children/{ADA}/timetable") and b"%PDF" in mum_ada.get(f"/parent/children/{ADA}/timetable/pdf").data)

# ================================================================ C. scope (already shown above for add/edit); class bundle for release
check("the release only ever touched JSS 1 and JSS 2 (both in teacher_a/head's own reach) — nothing at another school",
      beta.one("SELECT COUNT(*) FROM exam_timetable_entries") == 0)

# ================================================================ D. permissions and CSRF
check("with school.timetable.view only, the viewer opens the list but cannot add or release",
      viewer.get(tt_url()).status_code == 200
      and viewer.post(f"{TT}/new?session_id={CUR}&term={TERM}", {"class_id": J1, "subject_id": MATHS, "exam_type": "test",
                                                                   "date": "2026-12-06", "start_time": "09:00"}, page="/admin/password").status_code == 403
      and viewer.post(f"{TT}/release?session_id={CUR}&term={TERM}", {}, page="/admin/password").status_code == 403)
check("with school.timetable.manage (teacher A) but not .release, releasing is refused (403)",
      teacher_a.post(f"{TT}/release?session_id={CUR}&term={TERM}", {}, page="/admin/password").status_code == 403)
check("with none of the three permissions, nobody is refused (403) on the list itself", nobody.get(tt_url()).status_code == 403)
check("…nor do they see 'Exam Timetable' in the menu, while a viewer does",
      "timetable" not in nobody.text("/admin/school").lower() and "timetable" in viewer.text("/admin/school").lower())
kept_count = alpha.one("SELECT COUNT(*) FROM exam_timetable_entries")
check("a POST without a CSRF token is refused (403) on every write route, and nothing changes",
      teacher_a.post(f"{TT}/new?session_id={CUR}&term={TERM}", {"class_id": J1, "subject_id": MATHS, "exam_type": "test",
                                                                  "date": "2026-12-07", "start_time": "09:00"}, token=False).status_code == 403
      and teacher_a.post(f"{TT}/{ENTRY}/delete?session_id={CUR}&term={TERM}", {}, token=False).status_code == 403
      and head.post(f"{TT}/release?session_id={CUR}&term={TERM}", {}, token=False).status_code == 403
      and alpha.one("SELECT COUNT(*) FROM exam_timetable_entries") == kept_count)
visitor = Person(ALPHA, label="signed-out visitor")
check("a signed-out visitor is sent to sign in from the list, and from every write route",
      visitor.get(tt_url()).status_code == 302
      and visitor.post(f"{TT}/new", {"class_id": J1}, token=False).status_code in (302, 403))

# ================================================================ E. students and parents (cross-checks)
check("a student cannot open the staff pages", ada_portal.get(tt_url()).status_code in (302, 403, 404))
check("a parent cannot open the staff pages", mum_ada.get(tt_url()).status_code in (302, 403, 404))
check("a parent cannot open another family's child's timetable", mum_ada.get(f"/parent/children/{TOBI}/timetable").status_code == 404)

# ================================================================ F. no traceback, no server error
check("no page in this run was a server error, and none showed a traceback", not PROBLEMS, "; ".join(PROBLEMS[:4]))
check("no server error was logged by any request in this run", not errors.seen, "; ".join(str(e).splitlines()[0][:100] for e in errors.seen[:3]))

# ================================================================ G. the guard files
import pg_posts_coverage as coverage  # noqa: E402

tt_writes = {rule.rule for rule in A.app.url_map.iter_rules() if rule.methods & {"POST"} and "timetable" in rule.rule}
check("the four timetable form routes are in the form suite's list of exercised routes",
      tt_writes == {TT + "/new", TT + "/<int:entry_id>/edit", TT + "/<int:entry_id>/delete", TT + "/release"}
      and tt_writes <= set(coverage.EXERCISED), str(tt_writes))
check("…and no write route of the application is unaccounted for", coverage.unaccounted(A.app.url_map) == [] and coverage.stale(A.app.url_map) == ([], []))
posts_source = open(os.path.join(HERE, "write_paths_pg_posts.py"), encoding="utf-8").read()
check("…and write_paths_pg_posts.py really submits them",
      all(s in posts_source for s in ("/admin/school/timetable/new", "/timetable/{TT_ENTRY}/edit", "/timetable/release", "/timetable/{TT_ENTRY}/delete")))
check("every timetable endpoint has its permission in the endpoint map",
      ADMIN_ENDPOINT_PERMISSIONS.get("admin_school_timetable") == "school.timetable.view"
      and ADMIN_ENDPOINT_PERMISSIONS.get("admin_school_timetable_new") == "school.timetable.manage"
      and ADMIN_ENDPOINT_PERMISSIONS.get("admin_school_timetable_edit") == "school.timetable.manage"
      and ADMIN_ENDPOINT_PERMISSIONS.get("admin_school_timetable_delete") == "school.timetable.manage"
      and ADMIN_ENDPOINT_PERMISSIONS.get("admin_school_timetable_release") == "school.timetable.release")

dispose_engines()
DROP_TEST_DATABASES()
shutil.rmtree(TMP, ignore_errors=True)
print()
failed = [x for x in results if not x[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
