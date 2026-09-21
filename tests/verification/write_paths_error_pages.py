"""The 404, 403 and 500 pages, on a real multi-school platform.

Every error used to fall through to Werkzeug's plain default page ("Not Found", "Forbidden",
"Internal Server Error" on a blank background). Two branded pages (error_4xx.html, error_5xx.html)
now cover every ``abort()`` and every unhandled exception, and this checks the promises they make:

  * a school's error page carries THAT school's name and never another school's;
  * the specific reason for a 403 (a missing CSRF token) still comes through;
  * the 500 page never leaks the exception message, its type or a traceback, and the database
    session is left usable afterwards;
  * a deliberate "permission denied" page keeps its own template (it is a normal response,
    not a raised error, so the generic handler must never intercept it);
  * a signed-in person (staff, parent, student, candidate) is never sent to a stranger's front door:
    they get "Go Back", no "Sign In", and a fallback that leads to their OWN dashboard;
  * the platform host (no school selected) and an address no school owns also answer with a
    proper page, not a crash, and a fault there does not leak either.

Run:  python tests/verification/write_paths_error_pages.py
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_errors_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('errors')
os.environ.update({
    "BRIGHTSTARS_TENANTS_DIR": os.path.join(TMP, "tenants"),
    "BRIGHTSTARS_PLATFORM_HOSTS": "platform.test",
    "BRIGHTSTARS_PORTAL_DOMAIN": "portal.test",
    "BRIGHTSTARS_REGISTRY_CACHE_SECONDS": "0",
    "BRIGHTSTARS_SECRET": "x" * 40,
})
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
os.chdir(ROOT)

import sqlalchemy as sa  # noqa: E402
from flask import url_for  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

import app as A  # noqa: E402
from control_plane import provisioning as pv  # noqa: E402
from control_plane.context import tenant_context  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import dispose_engines  # noqa: E402

import logging  # noqa: E402


class _Captured(logging.Handler):
    """Keeps what the application logs, so the deliberate faults below do not flood the output
    and so we can check the operators DO get the details that the visitor must not."""

    def __init__(self):
        super().__init__()
        self.text = []

    def emit(self, record):
        self.text.append(record.getMessage() + (self.format(record) if record.exc_info else ""))


captured = _Captured()
A.app.logger.handlers.clear()
A.app.logger.addHandler(captured)
A.app.logger.propagate = False

results = []
PL = "http://platform.test"
ALPHA, BETA = "http://alpha.portal.test", "http://beta.portal.test"
SECRET = "deliberate test explosion - must never reach the response body"


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


# Throwaway routes that always raise, to exercise the 500 handler through a full dispatch, the same
# way a genuine bug would. Flask only allows adding routes before its first request.
@A.app.route("/__test_boom__")
def _boom():
    raise RuntimeError(SECRET)


@A.app.route("/platform/__test_boom__")
def _platform_boom():
    raise RuntimeError(SECRET)


@A.app.route("/__test_db_boom__")
def _db_boom():
    # A failed statement leaves the database session needing a rollback; the error page must cope.
    A.db.session.execute(sa.text("SELECT * FROM a_table_that_does_not_exist"))
    return "unreachable"


def csrf(c, path, base):
    body = c.get(path, base_url=base).get_data(as_text=True)
    marker = 'name="_csrf_token" value="'
    start = body.index(marker) + len(marker)
    return body[start:body.index('"', start)]


def info_for(slug):
    with platform_session() as s:
        tenant = get_tenant(s, slug)
        return to_info(tenant) if tenant else None


def in_school(slug, fn):
    with A.app.app_context(), tenant_context(info_for(slug)):
        return fn()


pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
console = A.app.test_client()
console.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                      "_csrf_token": csrf(console, "/platform/login", PL)}, base_url=PL)
for code, name in (("alpha", "Alpha Academy"), ("beta", "Beta College")):
    console.post("/platform/schools/new", data={
        "_csrf_token": csrf(console, "/platform/schools/new", PL), "name": name, "code": code}, base_url=PL,
        content_type="multipart/form-data")

# ---------------------------------------------------------------- people who can sign in
NOW = "2026-01-01T00:00:00+00:00"
PASSWORD = "Correct-horse-9"


def seed_people():
    role = A.db.session.scalars(sa.select(A.AdminType).where(A.AdminType.is_system == 0, A.AdminType.active == 1)
                                .order_by(A.AdminType.id)).first()
    staff = A.Admin(username="zz_errpage_staff", display_name="Errpage Staff",
                    password_hash=generate_password_hash(PASSWORD), admin_type_id=role.id, active=1,
                    password_must_change=0, created_at=NOW)
    A.db.session.add(staff)
    A.db.session.flush()
    A.db.session.add(A.AdminRoleAssignment(admin_id=staff.id, admin_type_id=role.id, assigned_at=NOW))
    school_id = A.db.session.scalar(sa.select(A.School.id))
    student = A.Student(admission_no="ERR/0001", student_number="ERR/0001", first_name="Err", last_name="Student",
                        active=1, created_at=NOW, school_id=school_id, login_username="err.student",
                        login_password_hash=generate_password_hash(PASSWORD), account_active=1)
    A.db.session.add(student)
    A.db.session.add(A.ParentAccount(username="err.parent", display_name="Err Parent",
                                     password_hash=generate_password_hash(PASSWORD), active=1, created_at=NOW))
    A.db.session.add(A.Candidate(candidate_code="ERR-2026-0001", candidate_name="Err Candidate", target_class="JSS 1",
                                 password_hash=generate_password_hash(PASSWORD), created_at=NOW, active=1))
    A.db.session.commit()


in_school("alpha", seed_people)


def signed_in(username, workspace=None):
    c = A.app.test_client()
    c.post("/login", data={"username": username, "password": PASSWORD}, base_url=ALPHA)
    if workspace:
        c.get(workspace, base_url=ALPHA)
    return c


def dashboards():
    """Where each kind of signed-in person belongs, as the app itself spells it."""
    with A.app.test_request_context(base_url=ALPHA):
        return {kind: url_for(endpoint) for kind, endpoint in (
            ("admin", "admin_workspace_home"), ("parent", "parent_dashboard"),
            ("student", "student_dashboard"), ("candidate", "candidate_dashboard"))}


HOME = dashboards()

# ================================================================ an anonymous visitor on a school's address
anon = A.app.test_client()
r = anon.get("/this-route-definitely-does-not-exist", base_url=ALPHA)
body = r.get_data(as_text=True)
check("an unknown address on a school's portal returns 404", r.status_code == 404)
check("the 404 page is the branded page, not Werkzeug's default", "Page Not Found" in body and "err-code" in body)
check("…and never uses Werkzeug's wording", "was not found on the server" not in body)
check("…and offers a way back to the front door", "Back to Homepage" in body or 'href="/"' in body)
check("the 404 page carries this school's name", "Alpha Academy" in body and "Beta College" not in body)
beta_body = A.app.test_client().get("/this-route-definitely-does-not-exist", base_url=BETA).get_data(as_text=True)
check("…and another school's 404 carries that school's own name, never the first school's",
      "Beta College" in beta_body and "Alpha Academy" not in beta_body)
check("an anonymous visitor is offered the way in, not a dashboard", "Sign In" in body or "Back to Homepage" in body)

r = anon.post("/logout", data={}, base_url=ALPHA)
body = r.get_data(as_text=True)
check("a missing CSRF token returns 403", r.status_code == 403)
check("the 403 page is the branded page", "Access Denied" in body)
check("…and still says the real, specific reason rather than a generic message",
      "Invalid or missing CSRF token" in body)

r = anon.post("/health", base_url=ALPHA)
check("a disallowed method still renders the branded page (405)", r.status_code == 405 and "err-code" in r.get_data(as_text=True))

r = anon.get("/__test_boom__", base_url=ALPHA)
body = r.get_data(as_text=True)
check("an unhandled exception returns 500", r.status_code == 500)
check("the 500 page is the branded page, not Werkzeug's default", "Something Went Wrong" in body)
check("the 500 page never leaks the exception message", "deliberate test explosion" not in body and "must never reach" not in body)
check("…nor the exception type or a traceback", "RuntimeError" not in body and "Traceback" not in body and "File \"" not in body)
check("…and still offers a way back", "Back to Homepage" in body)
check("…and still carries the school's own name", "Alpha Academy" in body)
check("the details of the fault ARE logged for the operators, server side only",
      any("deliberate test explosion" in line and "RuntimeError" in line for line in captured.text))

r = anon.get("/__test_db_boom__", base_url=ALPHA)
body = r.get_data(as_text=True)
check("a database error is a plain 500 too, with no SQL or table name in it",
      r.status_code == 500 and "Something Went Wrong" in body and "a_table_that_does_not_exist" not in body
      and "ProgrammingError" not in body and "psycopg" not in body.lower())
check("…and the next request on that connection works (the failed transaction was rolled back)",
      anon.get("/login", base_url=ALPHA).status_code == 200)

# a wrong method on a route that needs a school, with no school signed in, is still a page
r = anon.get("/admin/school/sessions", base_url=ALPHA)
check("a protected page for someone not signed in sends them to sign in, not to an error page",
      r.status_code in (302, 401) and "err-code" not in r.get_data(as_text=True))

# ================================================================ a deliberate "permission denied" keeps its own page
staff = signed_in("zz_errpage_staff", "/admin/workspace/school")
r = staff.get("/admin/school/delivery", base_url=ALPHA)
body = r.get_data(as_text=True)
check("a permission-denied page keeps its own dedicated template",
      r.status_code == 403 and "not available to you" in body, f"{r.status_code}")
check("…and is NOT the generic error page (that handler must never intercept it)", "err-code" not in body)

# ================================================================ signed-in people are never sent to a stranger's front door
people = (("staff member", "admin", "zz_errpage_staff", "/admin/workspace/school"),
          ("parent", "parent", "err.parent", None),
          ("student", "student", "err.student", None),
          ("candidate", "candidate", "ERR-2026-0001", None))
for label, kind, username, workspace in people:
    c = signed_in(username, workspace)
    check(f"({label}) really is signed in", any(k in dict(c.get("/", base_url=ALPHA).headers).get("Location", "")
                                              for k in ("admin", "parent", "student", "candidate")),
          dict(c.get("/", base_url=ALPHA).headers).get("Location", ""))
    r404 = c.get("/this-route-definitely-does-not-exist", base_url=ALPHA)
    b404 = r404.get_data(as_text=True)
    check(f"({label}) a 404 offers 'Go Back', not 'Back to Homepage'", r404.status_code == 404 and "Go Back" in b404 and "Back to Homepage" not in b404)
    check(f"({label}) …and no 'Sign In' link, since they are signed in", "Sign In" not in b404)
    check(f"({label}) …and the fallback leads to their own dashboard ({HOME[kind]}), not the public front door",
          HOME[kind] in b404, HOME[kind])
    others = [url for k, url in HOME.items() if k != kind]
    check(f"({label}) …and not to anyone else's dashboard", not any(f'"{u}"' in b404 for u in others))
    r500 = c.get("/__test_boom__", base_url=ALPHA)
    b500 = r500.get_data(as_text=True)
    check(f"({label}) a 500 offers 'Go Back' too, and leaks nothing",
          r500.status_code == 500 and "Go Back" in b500 and "Back to Homepage" not in b500
          and "deliberate test explosion" not in b500 and "RuntimeError" not in b500)

# ================================================================ the platform host and an unowned address
r = console.get("/this-page-does-not-exist", base_url=PL)
body = r.get_data(as_text=True)
check("an unknown page on the platform host is a designed page (404), not Werkzeug's default",
      r.status_code == 404 and "was not found on the server" not in body and "<html" in body.lower())
r = console.get("/platform/__test_boom__", base_url=PL)
body = r.get_data(as_text=True)
check("a fault inside the console is a plain 500 page, not a crash", r.status_code == 500 and "<html" in body.lower(), f"{r.status_code}")
check("…and leaks nothing", "deliberate test explosion" not in body and "RuntimeError" not in body and "Traceback" not in body)
check("…and the console still works afterwards", console.get("/platform", base_url=PL).status_code == 200)
r = A.app.test_client().get("/", base_url="http://nobody.example")
body = r.get_data(as_text=True)
check("an address no school owns gets its own designed page", r.status_code == 404 and "not registered" in body)
check("the health check answers without any school", A.app.test_client().get("/health", base_url="http://nobody.example").status_code == 200)

dispose_engines()
DROP_TEST_DATABASES()
shutil.rmtree(TMP, ignore_errors=True)
print()
failed = [x for x in results if not x[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
