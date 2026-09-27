"""Every form submission (POST) in the application, made for real against PostgreSQL.

write_paths_pg_smoke.py opens every page. This is its other half: it submits every
state-changing route, because that is where a query PostgreSQL rejects is easiest to
miss (an INSERT, an UPDATE ... WHERE on a text column, a DELETE with a subquery, an
aggregate behind a redirect). SQLite forgave a lot of that; PostgreSQL does not.

What it does, in plain terms:

1. Builds a throw-away school and gets around in it as each kind of person: the platform
   operator, the school's administrator, a student, a parent and an entrance candidate.
2. Lists every route that accepts a POST (or PUT, PATCH, DELETE) straight from
   ``app.url_map`` and submits a real, valid form to each one, as the right person, with a
   real CSRF token and real ids taken from the data it has built up (it registers the
   students, creates the classes' subjects, takes the exams, records the payments...).
   Where it is cheap it checks the row really was written, changed or removed.
3. Sends every route an empty form, a form full of hostile values and a form with no CSRF
   token, and checks each one is refused cleanly (a message and a redirect, or a 4xx) and never
   with a 500 or a database error.
4. Anything that would reach the outside world (email, WhatsApp) is caught by a stand-in at
   the very last step, so the real sending code still runs.
5. THE GUARD: the run fails if any route that accepts a POST is neither driven here nor listed in
   ``EXEMPT`` (in pg_posts_coverage.py) with a reason. A new route cannot ship untested on
   PostgreSQL: whoever adds it has to add a submission here, or say in one line why not.

A "database error" is any SQLAlchemy exception the application logged or raised, a 500, or one of
PostgreSQL's own messages ("current transaction is aborted", GroupingError ...) showing up in a page.

Run:  python tests/verification/write_paths_pg_posts.py
"""
import base64
import io
import json
import logging
import os
import re
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # a failure may quote hostile text from a page

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_posts_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('posts')
import atexit  # noqa: E402

atexit.register(DROP_TEST_DATABASES)  # even if this script stops half way, its databases go
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
from werkzeug.exceptions import HTTPException  # noqa: E402

import app as A  # noqa: E402
import pg_posts_coverage as coverage  # noqa: E402
from control_plane import provisioning as pv  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import dispose_engines, engine_for  # noqa: E402

PL = "http://platform.test"
SCHOOL = "http://posts.portal.test"
SCHOOL_HOST = "posts.portal.test"
PLATFORM_PASSWORD = "a-long-platform-password"
NEW_PASSWORD = "a-brand-new-password-1"

results = []
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)


# ------------------------------------------------------------------ what counts as a database error

# Words PostgreSQL / SQLAlchemy / psycopg use for "this query is not valid". If any of them is in a page
# the application sent back, the page is showing a database failure to a person.
DB_WORDS = ("ProgrammingError", "DataError", "IntegrityError", "InvalidTextRepresentation", "GroupingError",
            "InFailedSqlTransaction", "current transaction is aborted", "UndefinedFunction",
            "UndefinedColumn", "StringDataRightTruncation", "NumericValueOutOfRange", "psycopg.errors",
            "sqlalchemy.exc", "operator does not exist", "invalid input syntax for")


class Errors(logging.Handler):
    """Remembers every exception the application logs while it answers a request."""

    def __init__(self):
        super().__init__()
        self.seen = []

    def emit(self, record):
        if record.exc_info and record.exc_info[1] is not None:
            self.seen.append((record.getMessage(), record.exc_info[1]))


errors = Errors()
A.app.logger.addHandler(errors)
for handler in list(A.app.logger.handlers):
    if handler is not errors:  # keep the run's output to the checks; the handler above keeps the detail
        A.app.logger.removeHandler(handler)
A.app.logger.propagate = False
A.app.config['BACKGROUND_INLINE'] = True


def is_database_error(exc):
    while exc is not None:
        if isinstance(exc, sa.exc.SQLAlchemyError):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def first_line(exc):
    return str(exc).strip().splitlines()[0][:220] if str(exc).strip() else type(exc).__name__


# ------------------------------------------------------------------ the outside world, stood in for

class FakeSMTP:
    """Stands in for the mail server's connection, so email is built and 'sent' for real but goes nowhere."""
    sent = []

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)

    def quit(self):
        pass

    def close(self):
        pass


class FakeHTTPResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


WHATSAPP_CALLS = []


def fake_urlopen(request, timeout=None):
    """Stands in for the WhatsApp Cloud API and Paystack: answers each request the way the real
    one would."""
    url = request.full_url if hasattr(request, "full_url") else str(request)
    if "api.paystack.co" in url:
        if "connection-check-does-not-exist" in url:
            return FakeHTTPResponse({"status": False, "message": "Transaction not found"}, status=404)
        if url.endswith("/transaction/initialize"):
            return FakeHTTPResponse({"status": True, "data": {
                "authorization_url": "https://checkout.paystack.test/fake", "reference": "posts-test-ref"}})
        return FakeHTTPResponse({"status": True, "data": {"status": "success", "amount": 100000, "id": 1}})
    WHATSAPP_CALLS.append(url)
    if url.endswith("/media"):
        return FakeHTTPResponse({"id": "media-1"})
    if url.endswith("/messages"):
        return FakeHTTPResponse({"messages": [{"id": "wamid.1"}]})
    return FakeHTTPResponse({"verified_name": "Posts School", "display_phone_number": "+234 800 000 0000"})


from core import delivery as _delivery  # noqa: E402
import urllib.request  # noqa: E402

_delivery._open_smtp = lambda settings: FakeSMTP()
_delivery.resolve_public = lambda host: ["93.184.216.34"]
urllib.request.urlopen = fake_urlopen

# ------------------------------------------------------------------ people and requests

ADAPTER = A.app.url_map.bind(SCHOOL_HOST)
PLATFORM_ADAPTER = A.app.url_map.bind("platform.test")
HITS = {}  # rule -> the first valid submission made to it: (actor name, concrete path, form field names)
JUNK_PLAN = {}  # rule -> (actor, path, fields) to sweep with junk at the end
PROBLEMS = []  # database errors found anywhere, with where
POSTS = [0]


def rule_of(path, adapter):
    from werkzeug.exceptions import MethodNotAllowed, NotFound
    from werkzeug.routing import RequestRedirect
    try:
        rule, _ = adapter.match(path.split("?")[0], method="POST", return_rule=True)
        return rule.rule
    except (NotFound, MethodNotAllowed, RequestRedirect):
        return None


def sql(statement, **params):
    with engine_for(INFO).begin() as conn:
        result = conn.execute(sa.text(statement), params)
        return result.fetchall() if result.returns_rows else None


def one(statement, **params):
    rows = sql(statement, **params)
    return rows[0][0] if rows else None


def count(table, where="TRUE", **params):
    return one(f"SELECT count(*) FROM {table} WHERE {where}", **params)


class Actor:
    """One signed-in (or anonymous) person: a browser with its own cookies."""

    made = [0]

    def __init__(self, name, base=SCHOOL, adapter=ADAPTER, admin=False):
        self.name, self.base, self.adapter, self.admin = name, base, adapter, admin
        self.client = A.app.test_client()
        # Each person "connects" from an address of their own, because sign-in, password recovery and
        # the platform console all limit tries per address (and the limit is shared through the registry).
        Actor.made[0] += 1
        self.addr = f"10.20.{Actor.made[0]}.1"

    def ensure_workspace(self, path):
        """An administrator must pick the entrance or the school workspace before opening its pages."""
        wanted = A._admin_workspace_for_path(path.split("?")[0]) if path.startswith("/admin") else None
        if wanted and self.session().get("admin_workspace") != wanted:
            self.get(f"/admin/workspace/{wanted}")

    # --- reading
    def get(self, path, **kw):
        errors.seen.clear()
        return self.client.get(path, base_url=self.base, **kw)

    def text(self, path):
        return self.get(path).get_data(as_text=True)

    # --- the session
    def session(self):
        with self.client.session_transaction(base_url=self.base) as sess:
            return dict(sess)

    def token(self):
        """The CSRF token the application put in this person's session, the way a rendered form has it."""
        if not self.session().get("_csrf_token"):
            self.get("/platform/login" if self.base == PL else "/login")
        with self.client.session_transaction(base_url=self.base) as sess:
            if not sess.get("_csrf_token"):
                sess["_csrf_token"] = "test-token-" + self.name
            return sess["_csrf_token"]

    def flashes(self, clear=True):
        """The messages the last request left for the next page: [(category, text)]."""
        with self.client.session_transaction(base_url=self.base) as sess:
            found = list(sess.get("_flashes", []))
            if clear:
                sess.pop("_flashes", None)
        return found

    # --- writing
    def post(self, path, data=None, files=None, label=None, valid=True, token=True, expect=(200, 302, 303),
             allow_error_flash=False, multipart=False, headers=None, addr=None):
        """Submit a form. ``valid`` submissions are the real thing and are counted as the route's coverage.

        Returns a Result. A valid submission fails the run if it was refused (an error message, or a
        status outside ``expect``); every submission fails it if PostgreSQL rejected anything.
        """
        data = dict(data or {})
        if token:
            data["_csrf_token"] = self.token()
        if files:
            multipart = True
            for key, given in files.items():
                # one (filename, bytes) pair, or a list of them for a field that takes several files
                pairs = given if isinstance(given, list) else [given]
                made = [(io.BytesIO(content), name) for name, content in pairs]
                data[key] = made if isinstance(given, list) else made[0]
        if self.admin:
            self.ensure_workspace(path)
        errors.seen.clear()
        POSTS[0] += 1
        try:
            resp = self.client.post(path, data=data, base_url=self.base, headers=headers or {},
                                    environ_base={"REMOTE_ADDR": addr or self.addr},
                                    content_type="multipart/form-data" if multipart else None)
            escaped = None
        except Exception as exc:  # something the application's own error handler did not catch
            resp, escaped = None, exc
            errors.seen.append((str(exc), exc))
        flashes = self.flashes()
        result = Result(self, path, resp, flashes, list(errors.seen), escaped)
        rule = rule_of(path, self.adapter)
        label = label or f"{self.name} POST {rule or path}"
        db_problem = result.database_problem()
        if db_problem:
            PROBLEMS.append((label, db_problem))
            check(f"{label}: no database error", False, db_problem)
        if valid:
            if rule is None:
                check(f"{label}: is a real route", False, path)
            else:
                HITS.setdefault(rule, (self, path, [k for k in data if k != "_csrf_token"], dict(data)))
            problem = result.refused(expect, allow_error_flash)
            check(f"{label}: accepted", problem is None, problem or "")
        return result


class Result:
    def __init__(self, actor, path, resp, flashes, logged, escaped):
        self.actor, self.path, self.resp, self.flashes, self.logged, self.escaped = actor, path, resp, flashes, logged, escaped
        self.status = resp.status_code if resp is not None else 599
        self.body = resp.get_data(as_text=True) if (resp is not None and resp.mimetype in ("text/html", "application/json")) else ""
        self.location = resp.headers.get("Location", "") if resp is not None else ""

    def database_problem(self):
        for message, exc in self.logged:
            if is_database_error(exc):
                return f"{first_line(exc)}"
        if self.escaped is not None and is_database_error(self.escaped):
            return first_line(self.escaped)
        if self.status >= 500:
            other = self.logged[-1][1] if self.logged else self.escaped
            return f"HTTP {self.status}" + (f" ({type(other).__name__}: {first_line(other)})" if other is not None else "")
        for word in DB_WORDS:
            if word in self.body:
                return f"the page mentions {word!r}"
        return None

    def refused(self, expect, allow_error_flash):
        if self.status not in expect:
            return f"HTTP {self.status}, wanted one of {expect} ({self.location or self.body[:80]!r})"
        errs = [t for c, t in self.flashes if c == "error"]
        if errs and not allow_error_flash:
            return "refused with: " + errs[0][:140]
        return None

    @property
    def json(self):
        return json.loads(self.body) if self.body else {}

    def said(self, text):
        return any(text.lower() in t.lower() for _, t in self.flashes) or text.lower() in self.body.lower()


def grab(pattern, text, what):
    found = re.search(pattern, text, re.S)
    check(f"found {what}", found is not None, text[:160].replace("\n", " ") if not found else "")
    return found.group(1).strip() if found else ""


INFO = None  # the school under test, once it exists


def png(name="photo.png"):
    return (name, PNG)


# ================================================================ 0. the platform: sign in, make a school
pv.create_platform_admin("ops", "Ops", PLATFORM_PASSWORD)
console = Actor("platform operator", PL, PLATFORM_ADAPTER)
r = console.post("/platform/login", {"username": "ops", "password": PLATFORM_PASSWORD})
check("the platform operator is signed in", console.get("/platform").status_code == 200)

r = console.post("/platform/schools/new", {
    "name": "Posts School", "code": "posts", "school_brand_primary": "#0d2b52", "school_brand_accent": "#1674b9",
    "school_motto": "Trust the process", "school_phone": "0803 111 2222", "school_email": "office@posts.test",
    "admin_username": "schooladmin", "admin_display_name": "School Admin", "starter_banks": "1"},
    files={"logo": png("logo.png"), "gallery": [png("a.png"), png("b.png")]}, label="create a school")
SCHOOLADMIN_TEMP = grab(r'id="temp-password">(.+?)</span>', r.body, "the school administrator's one-time password")
with platform_session() as s_:
    INFO = to_info(get_tenant(s_, "posts"))
check("the school exists with its own database", INFO is not None and count("academic_sessions") >= 1)

# the operator enters the school (a single-use ticket), which is how the school is driven from here on
r = console.post("/platform/schools/posts/enter", {}, label="enter the school")
op = Actor("operator", admin=True)
op.get(r.location[len(SCHOOL):])
check("the operator is inside the school", op.get("/admin/home").status_code == 200)

# the new-school setup checklist: classes are pre-seeded (so step one is already done), nothing else is yet
op.get("/admin/workspace/school")
page = op.text("/admin/school")
check("the setup checklist shows on a brand-new school, one of four steps already done",
      "Finish setting up your school" in page and "1/4" in page and "Add your subjects" in page)
op.post("/admin/school/onboarding/dismiss", {})
page = op.text("/admin/school")
check("dismissing the checklist hides it, but leaves a way to bring it back",
      "Finish setting up your school" not in page and "Show setup checklist" in page)
op.post("/admin/school/onboarding/show", {})
check("…and showing it again brings it back", "Finish setting up your school" in op.text("/admin/school"))

# ================================================================ 1. sessions, classes, subjects
def cls(name):
    return one("SELECT id FROM school_classes WHERE name = :n", n=name)


CURRENT = one("SELECT id FROM academic_sessions WHERE is_current = 1")
JSS1, JSS2, JSS3, PRIM6, PRIM5 = cls("JSS 1"), cls("JSS 2"), cls("JSS 3"), cls("Primary 6"), cls("Primary 5")

SESSIONS = "/admin/school/sessions"
op.post(SESSIONS, {"action": "create", "name": "2025/2026", "start_date": "2025-09-01", "end_date": "2026-07-31"})
PAST = one("SELECT id FROM academic_sessions WHERE name = '2025/2026'")
check("a past academic session was created", PAST is not None and one("SELECT is_current FROM academic_sessions WHERE id = :i", i=PAST) == 0)
op.post(SESSIONS, {"action": "create", "name": "2027/2028", "start_date": "2027-09-01", "end_date": "2028-07-31"})
NEXT = one("SELECT id FROM academic_sessions WHERE name = '2027/2028'")
check("the next academic session was created", NEXT is not None)
op.post(SESSIONS, {"action": "create", "name": "2024/2025"})
OLD = one("SELECT id FROM academic_sessions WHERE name = '2024/2025'")
op.post(SESSIONS, {"action": "archive", "session_id": OLD})
check("a session can be archived", one("SELECT active FROM academic_sessions WHERE id = :i", i=OLD) == 0)
op.post(SESSIONS, {"action": "reactivate", "session_id": OLD})
check("…and reactivated", one("SELECT active FROM academic_sessions WHERE id = :i", i=OLD) == 1)
op.post(SESSIONS, {"action": "set_current", "session_id": CURRENT})
check("a session can be made the current one", one("SELECT is_current FROM academic_sessions WHERE id = :i", i=CURRENT) == 1)
r = op.post(SESSIONS, {"action": "set_ca_weights", "ca_weight_test": "20", "ca_weight_assignment": "10", "ca_weight_project": "10"})
check("continuous-assessment weights were saved", r.said("weighting updated"))

op.post(f"/admin/school/classes/{PRIM5}/toggle", {})
check("a class can be switched on", one("SELECT active FROM school_classes WHERE id = :i", i=PRIM5) == 1)
op.post(f"/admin/school/classes/{PRIM5}/edit", {"name": "Primary 5", "stage": "Primary", "level_order": "5", "optional": "1"})
check("a class was edited", one("SELECT optional FROM school_classes WHERE id = :i", i=PRIM5) == 1)

SUBJ = "/admin/school/subjects"
op.post(f"{SUBJ}/new", {"name": "Mathematics", "code": "MTH", "class_ids": [JSS1, JSS2]})
MATHS = one("SELECT id FROM school_subjects WHERE name = 'Mathematics'")
check("a subject was created and attached to two classes", count("class_subjects", "subject_id = :s", s=MATHS) == 2)
r = op.post(f"{SUBJ}/quick-create", {"name": "English Language", "code": "ENG", "class_id": JSS1})
ENGLISH = one("SELECT id FROM school_subjects WHERE name = 'English Language'")
check("a subject was quick-created (JSON)", r.json.get("ok") is True and ENGLISH == r.json.get("subject_id"))
op.post(f"{SUBJ}/new", {"name": "Basic Science", "code": "BSC", "class_ids": [JSS1]})
SCIENCE = one("SELECT id FROM school_subjects WHERE name = 'Basic Science'")
op.post(f"{SUBJ}/{SCIENCE}/edit", {"name": "Basic Science and Technology", "code": "BST", "class_ids": [JSS1, JSS2]})
check("a subject was edited and given a second class",
      one("SELECT name FROM school_subjects WHERE id = :i", i=SCIENCE) == "Basic Science and Technology"
      and count("class_subjects", "subject_id = :s", s=SCIENCE) == 2)
op.post(f"{SUBJ}/{SCIENCE}/lock/{JSS2}", {})
check("a class-subject link can be locked", one("SELECT locked FROM class_subjects WHERE subject_id = :s AND class_id = :c", s=SCIENCE, c=JSS2) == 1)
op.post(f"{SUBJ}/{SCIENCE}/lock/{JSS2}", {})
check("…and unlocked", one("SELECT locked FROM class_subjects WHERE subject_id = :s AND class_id = :c", s=SCIENCE, c=JSS2) == 0)
op.post(f"{SUBJ}/new", {"name": "Civic Education", "code": "CVE", "class_ids": [JSS3]})
CIVIC = one("SELECT id FROM school_subjects WHERE name = 'Civic Education'")
op.post(f"{SUBJ}/{CIVIC}/final-lock/{JSS3}", {})
check("a class-subject link can be permanently locked", one("SELECT final_locked FROM class_subjects WHERE subject_id = :s", s=CIVIC) == 1)
op.post(f"{SUBJ}/new", {"name": "Scratch Subject", "code": "SCR", "class_ids": [JSS1]})
SCRATCH = one("SELECT id FROM school_subjects WHERE name = 'Scratch Subject'")
op.post(f"{SUBJ}/{SCRATCH}/delete", {})
check("a subject can be removed", one("SELECT active FROM school_subjects WHERE id = :i", i=SCRATCH) == 0)

# ================================================================ 2. students, and the parents registered with them
STATE_ROW = {"gender": "Female", "date_of_birth": "2014-05-01", "state_of_origin": "Lagos", "blood_group": "O+",
             "genotype": "AA", "guardian_name": "Chidi Obi", "guardian_phone": "08031234567",
             "guardian_email": "chidi.obi@example.test", "class_id": JSS1,
             "previous_school": "Sunrise Nursery", "religion": "Christianity", "denomination": "Catholic",
             "father_guardian_name": "Chidi Obi", "father_guardian_address": "12 Palm Street",
             "father_guardian_office_phone": "01 234 5678", "father_guardian_mobile": "08031234567",
             "father_guardian_email": "chidi.obi@example.test", "mother_name": "Ngozi Obi",
             "mother_address": "12 Palm Street", "mother_office_phone": "01 234 9999",
             "mother_email": "ngozi.obi@example.test", "mother_occupation": "Nurse"}
STUDENTS = "/admin/school/students"


def register_student(first, last, **extra):
    form = {**STATE_ROW, "first_name": first, "middle_name": "", "last_name": last, **extra}
    r = op.post(f"{STUDENTS}/new", form, files={"photo": png(f"{first.lower()}.png")})
    login = grab(r'Student ID / Username</span><strong>(.+?)</strong>', r.body, f"{first}'s student login ID")
    password = grab(r'credential-password">(.+?)</strong>', r.body, f"{first}'s temporary password")
    sid = one("SELECT id FROM students WHERE login_username = :u", u=login)
    return sid, login, password


ADA, ADA_LOGIN, ADA_TEMP = register_student("Ada", "Obi")
TUNDE, TUNDE_LOGIN, TUNDE_TEMP = register_student("Tunde", "Obi", gender="Male")  # a brother: shares the parents
check("two students were registered, each enrolled in JSS 1 with an admission profile",
      count("student_enrolments", "class_id = :c AND session_id = :s AND active = 1", c=JSS1, s=CURRENT) == 2
      and count("student_admission_profiles") == 2)
check("…and the brother was linked to the same parent accounts, not new ones",
      count("parent_accounts") == 2 and count("parent_student_links", "student_id = :s", s=TUNDE) == 2)
check("…each has a generated student number", one("SELECT student_number FROM students WHERE id = :i", i=ADA) not in (None, ""))

op.post(f"{STUDENTS}/{ADA}/edit", {"admission_no": one("SELECT admission_no FROM students WHERE id = :i", i=ADA),
                                   "first_name": "Adaeze", "middle_name": "", "last_name": "Obi", "gender": "Female",
                                   "date_of_birth": "2014-05-01", "class_id": JSS1, "guardian_name": "Chidi Obi",
                                   "guardian_phone": "08031234567", "guardian_email": "chidi.obi@example.test"},
        files={"photo": png("ada2.png")})
check("a student was edited", one("SELECT first_name FROM students WHERE id = :i", i=ADA) == "Adaeze")
r = op.post(f"{STUDENTS}/{TUNDE}/account", {})
TUNDE_TEMP = grab(r'credential-password">(.+?)</strong>', r.body, "a reset student password")
op.post(f"{STUDENTS}/{TUNDE}/account/toggle", {})
check("a student's login can be switched off", one("SELECT account_active FROM students WHERE id = :i", i=TUNDE) == 0)
op.post(f"{STUDENTS}/{TUNDE}/account/toggle", {})
check("…and on again", one("SELECT account_active FROM students WHERE id = :i", i=TUNDE) == 1)
op.post(f"{STUDENTS}/{TUNDE}/toggle", {})
check("a student can be deactivated", one("SELECT active FROM students WHERE id = :i", i=TUNDE) == 0)
op.post(f"{STUDENTS}/{TUNDE}/toggle", {})
check("…and reactivated", one("SELECT active FROM students WHERE id = :i", i=TUNDE) == 1)
op.post(f"{STUDENTS}/{ADA}/history", {"level_name": "Primary 6", "session_id": PAST, "enrolled_at": "2025-09-01",
                                      "completed_at": "2026-07-20", "notes": "Joined from Sunrise Nursery"})
HISTORY = one("SELECT id FROM student_enrollment_history WHERE student_id = :s ORDER BY id DESC LIMIT 1", s=ADA)
check("enrolment history was recorded", HISTORY is not None)
op.post(f"{STUDENTS}/{ADA}/history/{HISTORY}/edit", {"level_name": "Primary 6", "session_id": PAST,
                                                     "enrolled_at": "2025-09-02", "completed_at": "2026-07-21",
                                                     "correction_reason": "The register had the wrong date"})
check("enrolment history was corrected with a reason",
      one("SELECT correction_reason FROM student_enrollment_history WHERE id = :i", i=HISTORY) == "The register had the wrong date")

before_students = count("students")
# Primary 5, deliberately: JSS 1 and JSS 2 have exact-count checks later (the promotion section),
# which an extra imported student would throw off.
csv_bytes = ("first_name,last_name,gender,class,guardian_name,guardian_email\r\n"
            "Chika,Nwosu,Female,Primary 5,Mrs Nwosu,mrs.nwosu@example.test\r\n"
            "Bad,Row,Not-A-Gender,Primary 5,,\r\n").encode("utf-8")
r = op.post(f"{STUDENTS}/import", {}, files={"csv_file": ("import.csv", csv_bytes)})
check("a bulk student import created the good row and skipped the bad one",
      count("students") == before_students + 1 and "1 student imported" in r.body.lower() and "1 row skipped" in r.body.lower())
check("…the imported student was enrolled in the named class with a generated admission number",
      one("SELECT student_number FROM students WHERE first_name = 'Chika' AND last_name = 'Nwosu'") not in (None, ""))
CHIKA = one("SELECT id FROM students WHERE first_name = 'Chika' AND last_name = 'Nwosu'")
op.post(f"{STUDENTS}/{CHIKA}/toggle", {})  # deactivated: later exact-count sections (promotion) assume only Ada and Tunde
check("…deactivating her afterwards keeps her out of everyone else's counts below",
      one("SELECT active FROM students WHERE id = :i", i=CHIKA) == 0)

# ================================================================ 3. the school's own email and WhatsApp (set up now, so everything below can send)
DELIVERY = "/admin/school/delivery"
op.post(f"{DELIVERY}/email/save", {"smtp_host": "smtp.posts.test", "smtp_port": "587", "smtp_security": "starttls",
                                   "smtp_user": "office@posts.test", "smtp_password": "mail-pass-123",
                                   "smtp_from": "office@posts.test"})
check("the school's mail server was saved, with its password encrypted",
      one("SELECT setting_value FROM school_delivery_settings WHERE setting_key = 'smtp_password'").startswith("enc:"))
r = op.post(f"{DELIVERY}/email/test", {"to": "principal@example.test"})
check("a test email was 'sent' through the mail stand-in", len(FakeSMTP.sent) == 1 and r.said("test message was sent"))
op.post(f"{DELIVERY}/whatsapp/save", {"whatsapp_phone_number_id": "104512345678901", "whatsapp_graph_version": "v23.0",
                                      "whatsapp_token": "EAAG-test-token"})
check("the school's WhatsApp account was saved", count("school_delivery_settings", "setting_key = 'whatsapp_phone_number_id'") == 1)
r = op.post(f"{DELIVERY}/whatsapp/check", {})
check("the WhatsApp connection check ran against the stand-in", r.said("Connected to WhatsApp"))

# ================================================================ 4. assignments and projects
ASSIGN = "/admin/school/assignments"
ASSIGN_FORM = {"title": "Fractions homework", "instructions": "Do all questions.", "class_id": JSS1, "subject_id": MATHS,
               "session_id": CURRENT, "term": "First Term", "assignment_type": "quiz", "timing_mode": "overall",
               "time_limit_minutes": "30", "due_date": "2026-12-01", "date_given": "2026-10-01",
               "student_ids": [ADA, TUNDE]}
mail_before = len(FakeSMTP.sent)
op.post(f"{ASSIGN}/new", ASSIGN_FORM)
QUIZ = one("SELECT id FROM school_assignments WHERE title = 'Fractions homework'")
check("an assignment was created and given to two students", count("assignment_students", "assignment_id = :a", a=QUIZ) == 2)
check("…and the guardians were told by email", len(FakeSMTP.sent) > mail_before)
for text, correct, points in (("What is 1/2 + 1/4?", 2, 1), ("What is 3/4 of 8?", 1, 2)):
    op.post(f"{ASSIGN}/{QUIZ}/questions", {"question_text": text, "instruction": "Pick one", "option_a": "1/4", "option_b": "6",
                                            "option_c": "3/4", "option_d": "2", "correct_option": correct, "points": points},
            files={"image": png("q.png")})
check("two questions were added and the assignment's maximum score follows them",
      count("assignment_questions", "assignment_id = :a", a=QUIZ) == 2 and one("SELECT max_score FROM school_assignments WHERE id = :a", a=QUIZ) == 3)
op.post(f"{ASSIGN}/{QUIZ}/students/{TUNDE}", {"status": "done", "score": "2", "remark": "Well done"})
check("an assignment mark was recorded", one("SELECT score FROM assignment_students WHERE assignment_id = :a AND student_id = :s", a=QUIZ, s=TUNDE) == 2)
op.post(f"{ASSIGN}/{QUIZ}/edit", {**ASSIGN_FORM, "title": "Fractions homework (revised)"})
check("an assignment was edited", one("SELECT title FROM school_assignments WHERE id = :a", a=QUIZ) == "Fractions homework (revised)")
op.post(f"{ASSIGN}/new", {**ASSIGN_FORM, "title": "Written exercise", "assignment_type": "written", "timing_mode": "untimed",
                          "max_score": "10"})
WRITTEN = one("SELECT id FROM school_assignments WHERE title = 'Written exercise'")
op.post(f"{ASSIGN}/{WRITTEN}/delete", {})
check("an assignment can be removed", one("SELECT active FROM school_assignments WHERE id = :a", a=WRITTEN) == 0)

PROJ = "/admin/school/projects"
PROJ_FORM = {"title": "Build a model", "instructions": "Use recycled material", "class_id": JSS1, "subject_id": SCIENCE,
             "session_id": CURRENT, "term": "First Term", "date_given": "2026-10-01", "due_date": "2026-11-30",
             "max_score": "10", "student_ids": [ADA, TUNDE]}
op.post(f"{PROJ}/new", PROJ_FORM)
PROJECT = one("SELECT id FROM school_projects WHERE title = 'Build a model'")
check("a project was created and given to two students", count("project_students", "project_id = :p", p=PROJECT) == 2)
op.post(f"{PROJ}/{PROJECT}/students/{ADA}", {"status": "done", "score": "8", "remark": "Creative"})
check("a project mark was recorded", one("SELECT score FROM project_students WHERE project_id = :p AND student_id = :s", p=PROJECT, s=ADA) == 8)
op.post(f"{PROJ}/{PROJECT}/edit", {**PROJ_FORM, "title": "Build a working model"})
check("a project was edited", one("SELECT title FROM school_projects WHERE id = :p", p=PROJECT) == "Build a working model")
op.post(f"{PROJ}/new", {**PROJ_FORM, "title": "Scratch project"})
SCRATCH_PROJECT = one("SELECT id FROM school_projects WHERE title = 'Scratch project'")
op.post(f"{PROJ}/{SCRATCH_PROJECT}/delete", {})
check("a project can be removed", one("SELECT active FROM school_projects WHERE id = :p", p=SCRATCH_PROJECT) == 0)

# ================================================================ 5. tests, practice tests and examinations
def new_assessment(kind, path, title, session_id, term="First Term", questions=3, **extra):
    """Create one (tests/practice-tests/examinations), give it questions and return its id."""
    op.post(f"/admin/school/{path}/new", {"title": title, "instructions": "Answer all.", "class_id": JSS1,
                                          "subject_id": MATHS, "session_id": session_id, "term": term,
                                          "duration_minutes": "20", **extra})
    aid = one("SELECT id FROM school_assessments WHERE title = :t", t=title)
    check(f"a {kind} was created", aid is not None and one("SELECT assessment_type FROM school_assessments WHERE id = :i", i=aid) == kind)
    for n in range(questions):
        op.post(f"/admin/school/assessments/{aid}/questions/new",
                {"question_text": f"{title}: question {n + 1}?", "instruction": "", "option_a": "A", "option_b": "B",
                 "option_c": "C", "option_d": "D", "correct_option": n % 4, "points": "5"},
                files={"image": png("q.png")} if n == 0 else None)
    check(f"…with {questions} questions and the count kept in step",
          count("school_questions", "assessment_id = :a", a=aid) == questions
          and one("SELECT question_count FROM school_assessments WHERE id = :a", a=aid) == questions)
    return aid


TEST = new_assessment("test", "tests", "Maths test one", CURRENT)
EXAM = new_assessment("examination", "examinations", "Maths exam one", CURRENT, term="Third Term")
PRACTICE = new_assessment("practice", "practice-tests", "Old practice paper", PAST, term="")
for aid in (TEST, EXAM, PRACTICE):
    op.post(f"/admin/school/assessments/{aid}/toggle", {})
check("assessments can be opened to students", count("school_assessments", "active = 1 AND id IN (:a, :b, :c)", a=TEST, b=EXAM, c=PRACTICE) == 3)
op.post(f"/admin/school/assessments/{TEST}/edit", {"title": "Maths test one (revised)", "instructions": "Answer all.", "class_id": JSS1,
                                                    "subject_id": MATHS, "session_id": CURRENT, "term": "First Term",
                                                    "duration_minutes": "25"})
check("an assessment was edited", one("SELECT duration_minutes FROM school_assessments WHERE id = :a", a=TEST) == 25)
first_q, second_q, third_q = [r[0] for r in sql("SELECT id FROM school_questions WHERE assessment_id = :a ORDER BY id", a=TEST)]
op.post(f"/admin/school/assessments/{TEST}/questions/{second_q}/edit",
        {"question_text": "Rewritten question?", "instruction": "Read carefully", "option_a": "W", "option_b": "X",
         "option_c": "Y", "option_d": "Z", "correct_option": "3", "points": "5", "remove_image": "1"})
check("a question was edited", one("SELECT question_text FROM school_questions WHERE id = :q", q=second_q) == "Rewritten question?")
r = op.post(f"/admin/school/assessments/{TEST}/questions/reorder", {"order": f"{third_q},{first_q},{second_q}"})
check("questions were reordered (JSON)", r.json.get("ok") is True and one("SELECT sort_order FROM school_questions WHERE id = :q", q=third_q) == 1)
op.post(f"/admin/school/assessments/{PRACTICE}/questions/{one('SELECT max(id) FROM school_questions WHERE assessment_id = :a', a=PRACTICE)}/delete", {})
check("a question was deleted and the count follows", one("SELECT question_count FROM school_assessments WHERE id = :a", a=PRACTICE) == 2)
for kind, path in (("test", "tests"), ("practice", "practice-tests"), ("examination", "examinations")):
    scratch = new_assessment(kind, path, f"Scratch {kind}", CURRENT, term="First Term" if kind != "practice" else "", questions=1)
    op.post(f"/admin/school/{path}/{scratch}/delete", {}, label=f"operator POST /admin/school/{path}/<int:assessment_id>/delete")
    check(f"a {kind} can be deleted (with its questions)", count("school_assessments", "id = :a", a=scratch) == 0)

# ================================================================ 6. offline results and their workflow
RESULTS = "/admin/school/results"
op.post(f"{RESULTS}/manual/new", {"class_id": JSS1, "session_id": CURRENT, "student_id": ADA, "subject_id": MATHS,
                                  "term": "First Term", "took_test": "yes", "test_score": "15", "test_max": "20",
                                  "exam_score": "40", "exam_max": "60"})
check("a Test and an Exam result were entered together",
      count("school_student_results", "student_id = :s AND status = 'entered'", s=ADA) == 2)
op.post(f"{RESULTS}/manual/new", {"class_id": JSS1, "session_id": CURRENT, "student_id": TUNDE, "subject_id": MATHS,
                                  "term": "First Term", "took_test": "no", "absence_reason": "Ill on the day",
                                  "exam_score": "30", "exam_max": "60"})
check("…and one for a student who missed the test", count("school_student_results", "student_id = :s AND component_name LIKE 'Test%Absent'", s=TUNDE) == 1)
TEST_RESULT = one("SELECT id FROM school_student_results WHERE student_id = :s AND component_name = 'Test'", s=ADA)
EXAM_RESULT = one("SELECT id FROM school_student_results WHERE student_id = :s AND component_name = 'Exam'", s=ADA)
op.post(f"{RESULTS}/{TEST_RESULT}/edit", {"score": "16", "max_score": "20", "component_name": "Test", "term": "First Term", "reason": ""})
check("a result was corrected", one("SELECT score FROM school_student_results WHERE id = :r", r=TEST_RESULT) == 16)
for result_id in (TEST_RESULT, EXAM_RESULT):
    for action in ("verify", "approve"):
        op.post(f"{RESULTS}/{result_id}/workflow", {"action": action, "reason": "Checked against the script"})
check("results were verified and approved", count("school_student_results", "id IN (:a, :b) AND status = 'approved'", a=TEST_RESULT, b=EXAM_RESULT) == 2)
TUNDE_RESULTS = [r[0] for r in sql("SELECT id FROM school_student_results WHERE student_id = :s ORDER BY id", s=TUNDE)]
for result_id in TUNDE_RESULTS:
    for action in ("verify", "approve"):
        op.post(f"{RESULTS}/{result_id}/workflow", {"action": action, "reason": "Checked against the script"})
page = op.text(f"{RESULTS}?class=JSS%201")
check("results: choosing a class opens a dialog listing its students", "Students in JSS 1" in page and "Search by name or admission number" in page)
page = op.text(f"{RESULTS}?class=JSS%201&student={TUNDE}")
check("results: choosing a student asks for a session and term first", "Choose the session and term" in page and "Release all results" not in page)
page = op.text(f"{RESULTS}?class=JSS%201&student={TUNDE}&session={CURRENT}&term=First%20Term")
check("results: the term shows its subjects, with one release button at the top",
      "Release all results for this term" in page and page.index("Release all results") < page.index("<table"))
before = len(FakeSMTP.sent)
op.post(f"{RESULTS}/release-term", {"student_id": TUNDE, "session_id": CURRENT, "term": "First Term",
                                    "return_to": f"{RESULTS}?class=JSS%201&student={TUNDE}"})
check("results: one press released every approved result of the term",
      count("school_student_results", "id = ANY(:ids) AND status = 'released'", ids=TUNDE_RESULTS) == len(TUNDE_RESULTS))
check("…and the parents were told the report card is ready (email and in-app)",
      any("report card ready" in str(m["Subject"]) for m in FakeSMTP.sent[before:])
      and count("school_notifications", "student_id = :s AND category = 'results'", s=TUNDE) >= 0)
r = op.post(f"{RESULTS}/release-term", {"student_id": TUNDE, "session_id": CURRENT, "term": "First Term"}, valid=False)
check("results: releasing a term with nothing approved is refused", r.said("no approved results"))
op.post(f"{RESULTS}/{TEST_RESULT}/workflow", {"action": "release"})
check("a result was released", one("SELECT status FROM school_student_results WHERE id = :r", r=TEST_RESULT) == "released")
op.post(f"{RESULTS}/release-schedule", {"result_release_at": "2026-01-01T09:00"})
check("a release date in the past released the rest",
      one("SELECT status FROM school_student_results WHERE id = :r", r=EXAM_RESULT) == "released"
      and one("SELECT result_release_at FROM academic_sessions WHERE id = :s", s=CURRENT) is not None)
op.post(f"{RESULTS}/release-schedule", {"result_release_at": ""})

# ================================================================ 7. finance
FIN = "/admin/finance"
FEE = {"name": "Tuition (JSS 1)", "category": "School Fees", "applicability": "First Term", "amount": "50000",
       "required": "1", "notes": "Termly tuition", "class_ids": [JSS1]}
op.post(f"{FIN}/fee-items/new", FEE)
TUITION = one("SELECT id FROM finance_fee_items WHERE name = 'Tuition (JSS 1)'")
op.post(f"{FIN}/fee-items/new", {**FEE, "name": "Uniform", "category": "Uniform", "amount": "12000", "optional": "1", "required": ""})
UNIFORM = one("SELECT id FROM finance_fee_items WHERE name = 'Uniform'")
check("two fee items were created and mapped to their class", count("finance_fee_item_classes", "class_id = :c AND active = 1", c=JSS1) == 2)
check("…and the setup checklist now considers itself complete and stays out of the way",
      "Finish setting up your school" not in op.text("/admin/school"))
op.post(f"{FIN}/fee-items/{UNIFORM}/edit", {**FEE, "name": "School uniform", "category": "Uniform", "amount": "13500", "optional": "1"})
check("a fee item was edited", one("SELECT amount FROM finance_fee_items WHERE id = :i", i=UNIFORM) == 13500)
op.post(f"{FIN}/fee-items/{UNIFORM}/toggle", {})
op.post(f"{FIN}/fee-items/{UNIFORM}/toggle", {})
check("a fee item can be archived and brought back", one("SELECT active FROM finance_fee_items WHERE id = :i", i=UNIFORM) == 1)
op.post(f"{FIN}/assessments/new", {"student_id": ADA, "session_id": CURRENT, "fee_item_id": [TUITION, UNIFORM],
                                   "term": "First Term", "due_date": "2026-11-15", "notes": "First term bill"})
check("two fee assessments were raised for a student", count("finance_fee_assessments", "student_id = :s AND active = 1", s=ADA) == 2)
check("…and the parents were told (in-app notification)", count("school_notifications", "category IS NOT NULL AND recipient_type = 'parent'") >= 1)

PAY = {"student_id": ADA, "session_id": CURRENT, "amount": "30000", "category": "School Fees", "method": "Cash",
       "reference": "CASH-001", "paid_at": "2026-10-05 10:30", "payer_name": "Chidi Obi", "notes": "Part payment"}
mail_auto = len(FakeSMTP.sent)
r = op.post(f"{FIN}/payments/new", PAY)
PAYMENT = int(grab(r"/receipts/(\d+)", r.location, "the new receipt's id"))
check("a payment was recorded with a receipt number", one("SELECT receipt_no FROM finance_payments WHERE id = :p", p=PAYMENT) is not None)
assessment_id = one("SELECT id FROM finance_fee_assessments WHERE student_id = :s AND fee_item_id = :f", s=ADA, f=TUITION)
op.post(f"{FIN}/payments/{PAYMENT}/allocate", {"allocations": json.dumps({str(assessment_id): 30000})})
check("a payment was allocated to a fee", one("SELECT sum(amount) FROM finance_payment_allocations WHERE payment_id = :p", p=PAYMENT) == 30000)
check("a recorded payment emailed its receipt to the guardian automatically",
      len(FakeSMTP.sent) == mail_auto + 1 and any(p.get_content_type() == "application/pdf" for p in FakeSMTP.sent[-1].iter_attachments())
      and "Part payment" in FakeSMTP.sent[-1].get_body().get_content())
check("…and sent it by WhatsApp too, both attempts logged",
      count("finance_delivery_logs", "payment_id = :p AND status = 'sent'", p=PAYMENT) == 2)
receipt_pdf = op.get(f"{FIN}/receipts/{PAYMENT}/pdf")
check("the receipt PDF is drawn from the school's own identity", receipt_pdf.status_code == 200 and receipt_pdf.data.startswith(b"%PDF"))
receipt_page = op.text(f"{FIN}/receipts/{PAYMENT}")
check("the receipt page carries the school's brand colours and the bursar's note",
      "--rc-primary:#" in receipt_page and "Part payment" in receipt_page and "Trust the process" in receipt_page)
mail_before = len(FakeSMTP.sent)
op.post(f"{FIN}/receipts/{PAYMENT}/email", {})
check("a receipt PDF was emailed to the guardian", len(FakeSMTP.sent) == mail_before + 1
      and any(part.get_content_type() == "application/pdf" for part in FakeSMTP.sent[-1].iter_attachments()))
op.post(f"{FIN}/receipts/{PAYMENT}/whatsapp", {})
check("a receipt PDF was sent by WhatsApp", any(u.endswith("/media") for u in WHATSAPP_CALLS)
      and count("finance_delivery_logs", "payment_id = :p AND status = 'sent'", p=PAYMENT) == 4)
r = op.post(f"{FIN}/payments/new", {**PAY, "amount": "5000", "reference": "CASH-002"})
VOIDED = int(grab(r"/receipts/(\d+)", r.location, "the second receipt's id"))
op.post(f"{FIN}/payments/{VOIDED}/void", {"reason": "Entered against the wrong child"})
check("a payment can be voided, with a reason", one("SELECT status FROM finance_payments WHERE id = :p", p=VOIDED) == "voided")
SIGN = f"{FIN}/receipt-settings"
op.post(SIGN, {"action": "draw", "signature_data_url": "data:image/png;base64," + base64.b64encode(PNG).decode()})
check("a drawn signature was stored", (one("SELECT setting_value FROM school_settings WHERE setting_key = 'receipt_authorised_signature'") or "").startswith("uploads/signatures/"))
op.post(SIGN, {"action": "upload"}, files={"signature_file": png("sig.png")})
op.post(SIGN, {"action": "remove"})
check("a signature can be uploaded and removed", one("SELECT setting_value FROM school_settings WHERE setting_key = 'receipt_authorised_signature'") == "")

# ================================================================ 6b. online payments (Paystack settings)
PSK = f"{FIN}/paystack"
op.post(f"{PSK}/save", {"public_key": "pk_test_postskey", "secret_key": "sk_test_postssecret"})
check("Paystack settings were saved", one("SELECT setting_value FROM school_payment_settings WHERE setting_key = 'paystack_public_key'") == "pk_test_postskey")
check("the secret key is encrypted at rest", (one("SELECT setting_value FROM school_payment_settings WHERE setting_key = 'paystack_secret_key'") or "").startswith("enc:v1:"))
op.post(f"{PSK}/test", {})
op.post(f"{PSK}/clear", {})
check("Paystack settings can be tested and removed", one("SELECT COUNT(*) FROM school_payment_settings") == 0)

# ================================================================ 7b. report cards: the teacher's comment, signatures and the head's details
RC = "/admin/school/report-cards"
DRAWN = "data:image/png;base64," + base64.b64encode(PNG).decode()
op.post(f"{RC}/my-signature", {"action": "draw", "signature_data_url": DRAWN})
check("a staff member's own signature was stored", one("SELECT COUNT(*) FROM admins WHERE signature_path LIKE 'uploads/signatures/%'") == 1)
op.post(f"{RC}/my-signature", {"action": "upload"}, files={"signature_file": png("teacher.png")})
op.post(f"{RC}/my-signature", {"action": "remove"})
check("a staff signature can be replaced and removed", one("SELECT COUNT(*) FROM admins WHERE signature_path IS NOT NULL AND signature_path <> ''") == 0)
ADA_CLASS = one("SELECT class_id FROM student_enrolments WHERE student_id = :s AND session_id = :c", s=ADA, c=CURRENT)
op.post(f"{RC}/comments?class_id={ADA_CLASS}&session_id={CURRENT}&term=First Term", {f"comment_{ADA}": "A hardworking and polite pupil."})
check("the class teacher's comment on a student's report card was saved",
      one("SELECT comment FROM report_card_comments WHERE student_id = :s AND term = 'First Term'", s=ADA) == "A hardworking and polite pupil.")
op.post(f"{RC}/comments?class_id={ADA_CLASS}&session_id={CURRENT}&term=First Term", {f"comment_{ADA}": ""})
check("clearing the box removes the comment", one("SELECT COUNT(*) FROM report_card_comments WHERE student_id = :s", s=ADA) == 0)
op.post(f"{RC}/comments?class_id={ADA_CLASS}&session_id={CURRENT}&term=First Term", {f"comment_{ADA}": "Shows great promise."})
op.post(f"{RC}/traits?class_id={ADA_CLASS}&session_id={CURRENT}&term=First Term",
        {f"trait_{ADA}_punctuality": "5", f"trait_{ADA}_neatness": "3"})
check("the class teacher's affective/psychomotor trait ratings on a student's report card were saved",
      one("SELECT ratings FROM report_card_traits WHERE student_id = :s AND term = 'First Term'", s=ADA)
      == '{"neatness": 3, "punctuality": 5}')
op.post(f"{RC}/traits?class_id={ADA_CLASS}&session_id={CURRENT}&term=First Term", {})
check("clearing every trait removes the row", one("SELECT COUNT(*) FROM report_card_traits WHERE student_id = :s", s=ADA) == 0)

# ================================================================ 7c. attendance
ATT_DATE = "2026-09-15"
op.post(f"/admin/school/attendance?class_id={ADA_CLASS}&session_id={CURRENT}&term=First Term&date={ATT_DATE}",
        {f"status_{ADA}": "present"})
check("a student's attendance for a day was recorded",
      one("SELECT status FROM attendance_records WHERE student_id = :s AND date = :d", s=ADA, d=ATT_DATE) == "present")

# ================================================================ 7d. exam timetable
r = op.post("/admin/school/timetable/new?session_id=%s&term=First Term" % CURRENT,
            {"class_id": ADA_CLASS, "subject_id": MATHS, "exam_type": "examination", "date": "2026-12-01",
             "start_time": "09:00", "end_time": "11:00", "venue": "Main Hall"})
TT_ENTRY = one("SELECT id FROM exam_timetable_entries WHERE class_id = :c AND subject_id = :s", c=ADA_CLASS, s=MATHS)
check("a timetable entry was added", TT_ENTRY is not None and one("SELECT venue FROM exam_timetable_entries WHERE id = :e", e=TT_ENTRY) == "Main Hall")
op.post(f"/admin/school/timetable/{TT_ENTRY}/edit?session_id={CURRENT}&term=First Term",
        {"class_id": ADA_CLASS, "subject_id": MATHS, "exam_type": "examination", "date": "2026-12-02",
         "start_time": "10:00", "end_time": "12:00", "venue": "Hall B"})
check("the entry was edited", one("SELECT date FROM exam_timetable_entries WHERE id = :e", e=TT_ENTRY) == "2026-12-02"
      and one("SELECT venue FROM exam_timetable_entries WHERE id = :e", e=TT_ENTRY) == "Hall B")
op.post(f"/admin/school/timetable/release?session_id={CURRENT}&term=First Term", {"exam_type": "examination"})
check("the timetable was released", one("SELECT released_at FROM exam_timetable_entries WHERE id = :e", e=TT_ENTRY) is not None)
op.post(f"/admin/school/timetable/{TT_ENTRY}/delete?session_id={CURRENT}&term=First Term", {})
check("the entry was deleted", one("SELECT COUNT(*) FROM exam_timetable_entries WHERE id = :e", e=TT_ENTRY) == 0)

r = op.post(f"{RC}/settings", {"head_title": "Proprietress", "head_name": "Mrs A. B. Okoye", "next_term_begins": "Monday, 4 January"})
check("the head's title, name and next-term date were saved", one("SELECT setting_value FROM school_settings WHERE setting_key = 'report_head_name'") == "Mrs A. B. Okoye"
      and one("SELECT setting_value FROM school_settings WHERE setting_key = 'report_head_title'") == "Proprietress")
op.post(f"{RC}/settings", {"action": "draw", "signature_data_url": DRAWN})
check("the head's signature was stored", (one("SELECT setting_value FROM school_settings WHERE setting_key = 'report_head_signature'") or "").startswith("uploads/signatures/"))
op.post(f"{RC}/settings", {"action": "upload"}, files={"signature_file": png("head.png")})
op.post(f"{RC}/settings", {"action": "remove"})
check("the head's signature can be replaced and removed", not one("SELECT setting_value FROM school_settings WHERE setting_key = 'report_head_signature'"))

# ================================================================ 8. the library
LIB = "/admin/library"
op.post(f"{LIB}/books/new", {"title": "Things Fall Apart", "author": "Chinua Achebe", "isbn": "9780385474542",
                             "publisher": "Anchor", "publication_year": "1994", "category": "Fiction", "shelf": "F2", "copies": "3"})
BOOK = one("SELECT id FROM library_books WHERE title = 'Things Fall Apart'")
check("a book was added with three copies", one("SELECT available_copies FROM library_books WHERE id = :b", b=BOOK) == 3)
op.post(f"{LIB}/books/{BOOK}/edit", {"title": "Things Fall Apart", "author": "Chinua Achebe", "isbn": "9780385474542",
                                     "publisher": "Anchor Books", "publication_year": "1994", "category": "Fiction",
                                     "shelf": "F3", "total_copies": "4"})
check("a book was edited (a copy added)", one("SELECT available_copies FROM library_books WHERE id = :b", b=BOOK) == 4)
op.post(f"{LIB}/issue", {"book_id": BOOK, "member_type": "student", "member_id": ADA, "days": "14"})
STAFF_ID = one("SELECT id FROM admins ORDER BY id LIMIT 1")
op.post(f"{LIB}/issue", {"book_id": BOOK, "member_type": "staff", "member_id": STAFF_ID, "days": "7"})
check("a book was lent to a student and to a member of staff", count("library_loans", "status = 'borrowed'") == 2
      and one("SELECT available_copies FROM library_books WHERE id = :b", b=BOOK) == 2)
LOAN = one("SELECT id FROM library_loans WHERE member_type = 'student' ORDER BY id LIMIT 1")
op.post(f"{LIB}/loans/{LOAN}/return", {})
check("a loan was returned", one("SELECT status FROM library_loans WHERE id = :l", l=LOAN) == "returned"
      and one("SELECT available_copies FROM library_books WHERE id = :b", b=BOOK) == 3)
op.post(f"{LIB}/books/{BOOK}/toggle", {}, allow_error_flash=True)
check("a book cannot be archived while copies are out", one("SELECT active FROM library_books WHERE id = :b", b=BOOK) == 1, "toggle was allowed")
op.post(f"{LIB}/books/new", {"title": "Spare copy", "copies": "1"})
SPARE = one("SELECT id FROM library_books WHERE title = 'Spare copy'")
op.post(f"{LIB}/books/{SPARE}/toggle", {})
check("a book with every copy in can be archived", one("SELECT active FROM library_books WHERE id = :b", b=SPARE) == 0)

# ================================================================ 9. parents: the school's side, then the parent's own portal
PARENTS = "/admin/school/parents"
r = op.post(f"{PARENTS}/new", {"display_name": "Ifeoma Eze", "email": "ifeoma@example.test", "phone": "08055550000",
                               "username": "ifeoma.eze", "student_ids": [ADA], "relationship": "aunt"})
AUNT = one("SELECT id FROM parent_accounts WHERE username = 'ifeoma.eze'")
check("a parent account was created and linked to a student", count("parent_student_links", "parent_id = :p AND active = 1", p=AUNT) == 1)
op.post(f"{PARENTS}/{AUNT}/edit", {"display_name": "Ifeoma Eze-Obi", "email": "ifeoma@example.test", "phone": "08055550000",
                                   "student_ids": [ADA, TUNDE], "relationship": "aunt"})
check("a parent account was edited and given a second child", count("parent_student_links", "parent_id = :p AND active = 1", p=AUNT) == 2)
FATHER = one("SELECT id FROM parent_accounts WHERE email = 'chidi.obi@example.test'")
FATHER_USER = one("SELECT username FROM parent_accounts WHERE id = :p", p=FATHER)
r = op.post(f"{PARENTS}/{FATHER}/credentials/reset", {})
FATHER_TEMP = grab(r"<code>(.+?)</code>", r.body, "the parent's temporary password")
AUNT_USER = one("SELECT username FROM parent_accounts WHERE id = :p", p=AUNT)
r = op.post(f"{PARENTS}/{AUNT}/credentials/reset", {})
AUNT_TEMP = grab(r"<code>(.+?)</code>", r.body, "the aunt's temporary password")

father = Actor("parent")
r = father.post("/login", {"username": FATHER_USER, "password": FATHER_TEMP})
check("a parent signs in with the temporary password and is sent to change it", "/parent/password" in r.location)
father.post("/parent/password", {"current_password": FATHER_TEMP, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
check("a parent's password was changed", one("SELECT password_must_change FROM parent_accounts WHERE id = :p", p=FATHER) == 0)
father.post("/parent/feedback", {"student_id": ADA, "subject": "Homework load", "body": "Is the homework too much this term?"},
            files={"attachment": ("permission_slip.txt", b"Please allow Ada to leave early on Friday.")})
FEEDBACK = one("SELECT id FROM parent_feedback WHERE subject = 'Homework load'")
check("a parent's message was sent to the school and staff were notified",
      FEEDBACK is not None and count("admin_notifications", "title = 'New parent message'") >= 1)
check("the attachment the parent picked was actually saved, not silently dropped",
      (one("SELECT attachment_path FROM parent_feedback WHERE id = :f", f=FEEDBACK) or "").startswith("uploads/messages/")
      and one("SELECT attachment_name FROM parent_feedback WHERE id = :f", f=FEEDBACK) == "permission_slip.txt"
      and one("SELECT file_type FROM parent_feedback WHERE id = :f", f=FEEDBACK) == "file")
r = father.get(f"/parent/feedback/{FEEDBACK}/attachment")
check("the parent can fetch back what they attached, under its own name",
      r.status_code == 200 and r.get_data(as_text=True) == "Please allow Ada to leave early on Friday."
      and "permission_slip.txt" in (r.headers.get("Content-Disposition") or ""))
aunt = Actor("parent")
aunt.post("/login", {"username": AUNT_USER, "password": AUNT_TEMP})
aunt.post("/parent/password", {"current_password": AUNT_TEMP, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
check("a different parent cannot fetch it", aunt.get(f"/parent/feedback/{FEEDBACK}/attachment").status_code == 404)
check("a signed-out visitor cannot fetch it", Actor("stranger").get(f"/parent/feedback/{FEEDBACK}/attachment").status_code in (302, 401, 403, 404))
FB = "/admin/school/parent-feedback"
check("staff with permission can fetch the same attachment from the school side",
      op.get(f"{FB}/{FEEDBACK}/attachment").status_code == 200)
r2 = father.post("/parent/feedback", {"subject": "A form", "body": "Attaching the wrong kind of file."},
                 files={"attachment": ("form.exe", b"not really a program, just the wrong extension")}, valid=False)
check("a disallowed file type is refused, and nothing was saved for it",
      r2.status in (200, 302) and one("SELECT COUNT(*) FROM parent_feedback WHERE subject = 'A form'") == 0)
mail_before = len(FakeSMTP.sent)
wa_before = len(WHATSAPP_CALLS)
op.post(f"{FB}/{FEEDBACK}/reply", {"body": "Thank you, we will review it."})
check("the school's reply reached the parent (in the app, by email and by WhatsApp)",
      one("SELECT status FROM parent_feedback WHERE id = :f", f=FEEDBACK) == "in_progress"
      and len(FakeSMTP.sent) > mail_before and len(WHATSAPP_CALLS) > wa_before)
father.post(f"/parent/feedback/{FEEDBACK}/reply", {"body": "Thank you."})
check("the parent replied in the same thread", count("parent_feedback_replies", "feedback_id = :f", f=FEEDBACK) == 2)
op.post(f"{FB}/{FEEDBACK}/status", {"status": "resolved"})
check("a conversation can be marked resolved", one("SELECT status FROM parent_feedback WHERE id = :f", f=FEEDBACK) == "resolved")

# ================================================================ 10. the student's own portal
student = Actor("student")
r = student.post("/login", {"username": ADA_LOGIN, "password": ADA_TEMP})
check("a student signs in with the ID and password they were given", "/student/dashboard" in r.location, f"{r.status} {r.location}")
before = one("SELECT login_password_hash FROM students WHERE id = :s", s=ADA)
student.post("/student/password", {"current_password": ADA_TEMP, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
check("a student's password was changed", one("SELECT login_password_hash FROM students WHERE id = :s", s=ADA) != before)

# the quiz assignment: start it, answer every question, and see it marked
student.post(f"/student/assignments/{QUIZ}", {"action": "start"})
ATTEMPT = one("SELECT id FROM school_assignment_attempts WHERE assignment_id = :a AND student_id = :s", a=QUIZ, s=ADA)
check("a student started a quiz assignment (its questions were frozen)",
      ATTEMPT is not None and count("school_assignment_attempt_questions", "attempt_id = :t", t=ATTEMPT) == 2)
for qid, right in sql("SELECT question_id, correct_option FROM school_assignment_attempt_questions WHERE attempt_id = :t ORDER BY question_order", t=ATTEMPT):
    student.post(f"/student/assignments/{QUIZ}/take", {"question_id": qid, "option_index": right})
check("the quiz was answered and marked from the frozen copy",
      one("SELECT status FROM school_assignment_attempts WHERE id = :t", t=ATTEMPT) == "submitted"
      and one("SELECT score FROM assignment_students WHERE assignment_id = :a AND student_id = :s", a=QUIZ, s=ADA) == 3)

# the class test: start, answer each question (one deliberately wrong), submit on the last
student.post(f"/student/assessments/{TEST}/start", {})
TRY = one("SELECT id FROM school_assessment_attempts WHERE assessment_id = :a AND student_id = :s", a=TEST, s=ADA)
check("a student started a test (its questions were frozen)", TRY is not None and count("school_assessment_attempt_questions", "attempt_id = :t", t=TRY) == 3)
frozen = sql("SELECT question_id, correct_option FROM school_assessment_attempt_questions WHERE attempt_id = :t ORDER BY question_order", t=TRY)
for position, (qid, right) in enumerate(frozen, 1):
    last = position == len(frozen)
    student.post(f"/student/assessments/{TEST}/answer",
                 {"attempt_id": TRY, "question_id": qid, "option_index": right if position != 2 else (right + 1) % 4,
                  "next_q": position + 1, **({"submit_assessment": "1"} if last else {})})
check("the test was submitted and marked", one("SELECT status FROM school_assessment_attempts WHERE id = :t", t=TRY) == "submitted"
      and one("SELECT score FROM school_assessment_attempts WHERE id = :t", t=TRY) == 10)
check("…and a result row was written for the school to verify",
      count("school_student_results", "student_id = :s AND assessment_id = :a", s=ADA, a=TEST) == 1)

# practice is a self-study bank: any session, any number of times, and never recorded
practice_page = student.text("/student/practice")
check("student practice: the practice tests of the student's class are always listed", "Old practice paper" in practice_page)
attempts_before = count("school_assessment_attempts")
results_before = count("school_student_results")
for run in range(2):
    student.get(f"/student/practice/{PRACTICE}")
    served = student.session().get("student_practice", {}).get("ids", [])
    check(f"student practice: run {run + 1} served the practice paper's questions", sorted(served) == sorted(r[0] for r in sql("SELECT id FROM school_questions WHERE assessment_id = :a", a=PRACTICE)))
    r = student.post(f"/student/practice/{PRACTICE}", {f"q_{qid}": right for qid, right in sql("SELECT id, correct_option FROM school_questions WHERE assessment_id = :a", a=PRACTICE)})
    check(f"student practice: run {run + 1} was marked, and it can be taken again", r.status == 200 and "NOT RECORDED" in r.body and "Retake this practice" in r.body)
check("student practice leaves no attempt and no result behind",
      count("school_assessment_attempts") == attempts_before and count("school_student_results") == results_before)
r = student.post(f"/student/assessments/{PRACTICE}/start", {}, valid=False)
check("student practice: the old graded start route now leads to the practice page", f"/student/practice/{PRACTICE}" in r.location and count("school_assessment_attempts") == attempts_before)

# ================================================================ 11. the public practice area (past sessions only)
visitor = Actor("visitor")
r = visitor.post("/practice", {"class_id": JSS1, "subject_id": MATHS})
check("practice: choosing a class and subject leads to a past paper", f"/practice/{PRACTICE}" in r.location)
visitor.get(f"/practice/{PRACTICE}")
answers = {f"q_{qid}": right for qid, right in sql("SELECT id, correct_option FROM school_questions WHERE assessment_id = :a", a=PRACTICE)}
r = visitor.post(f"/practice/{PRACTICE}", answers)
check("practice: answers were marked", r.status == 200 and "100" in r.body)

# ================================================================ 12. the entrance examination: banks, papers, candidates, an exam taken
# A new school starts with the standard banks (Mathematics, English, General Knowledge for Year 7 and SSS 1).
from core.entrance import load_banks  # noqa: E402
from core.storage import data_dir  # noqa: E402


def banks_here():
    with A.app.app_context():
        from control_plane.context import tenant_context
        with tenant_context(INFO):
            return load_banks()


check("the school started with its standard question banks", len(banks_here()) >= 6)
BANKS = "/admin/banks"

# a bank of the school's own: create, edit, add / edit / reorder / delete questions
op.post(f"{BANKS}/new", {"name": "Posts Mathematics Drill", "level": "JSS 1", "duration_minutes": "30", "version": "1.0"})
MY_BANK = "posts_mathematics_drill_jss_1"
check("a question bank was created (as a file in the school's own folder)", MY_BANK in banks_here())
op.post(f"{BANKS}/{MY_BANK}/edit", {"name": "Posts Mathematics Drill", "level": "JSS 1", "duration_minutes": "45", "version": "1.1"})
check("a bank's settings were edited", banks_here()[MY_BANK]["duration_seconds"] == 2700)
for n in range(3):
    op.post(f"{BANKS}/{MY_BANK}/questions/new", {"text": f"What is {n + 1} + {n + 1}?", "instruction": "", "option0": str(n),
                                                 "option1": str((n + 1) * 2), "option2": "9", "option3": "10", "answer": "1", "points": "1"},
            files={"image": png("q.png")} if n == 0 else None)
questions = [q["id"] for q in banks_here()[MY_BANK]["questions"]]
check("three questions were added to the bank", len(questions) == 3)
op.post(f"{BANKS}/{MY_BANK}/questions/{questions[1]}/edit", {"text": "What is 10 - 3?", "instruction": "Subtract", "option0": "7",
                                                                "option1": "6", "option2": "5", "option3": "4", "answer": "0", "points": "2"})
check("a bank question was edited", next(q for q in banks_here()[MY_BANK]["questions"] if q["id"] == questions[1])["text"] == "What is 10 - 3?")
r = op.post(f"{BANKS}/{MY_BANK}/questions/reorder", {"order": ",".join(str(q) for q in reversed(questions))})
check("bank questions were reordered (JSON)", r.json.get("ok") is True and [q["id"] for q in banks_here()[MY_BANK]["questions"]] == list(reversed(questions)))
op.post(f"{BANKS}/{MY_BANK}/questions/{questions[0]}/delete", {})
check("a bank question was deleted", len(banks_here()[MY_BANK]["questions"]) == 2)

# importing a bank from a file, then replacing it
IMPORT_BANK = {"id": "posts_english_import", "name": "Imported English", "level": "JSS 1", "duration_seconds": 1800, "version": "1.0",
               "subject": "English", "entry_group": "year7",
               "questions": [{"id": n, "text": f"Choose the noun {n}", "options": ["run", "table", "quickly", "blue"], "answer": 1, "points": 1}
                             for n in range(1, 6)]}
op.post(f"{BANKS}/import", {}, files={"bank_file": ("bank.json", json.dumps(IMPORT_BANK).encode())})
check("a bank was imported from a JSON file", "posts_english_import" in banks_here())
op.post(f"{BANKS}/import", {"replace": "1"}, files={"bank_file": ("bank.json", json.dumps({**IMPORT_BANK, "name": "Imported English v2"}).encode())})
check("…and replaced by a second file", banks_here()["posts_english_import"]["name"] == "Imported English v2")

# a bank can be locked by the School Admin (and then refuses changes) and unlocked again
ADMIN_ROOT = "/admin/administration"
op.post(f"{ADMIN_ROOT}/resources/bank/{MY_BANK}/lock", {"reason": "Under review before the exam"})
check("a bank was locked", count("admin_resource_locks", "resource_id = :b AND unlocked_at IS NULL", b=MY_BANK) == 1)
r = op.post(f"{BANKS}/{MY_BANK}/questions/new", {"text": "x?", "option0": "a", "option1": "b", "option2": "c", "option3": "d", "answer": "0", "points": "1"},
            valid=False)
check("…and a locked bank refuses new questions", len(banks_here()[MY_BANK]["questions"]) == 2 and r.said("locked"))
op.post(f"{ADMIN_ROOT}/resources/bank/{MY_BANK}/unlock", {})
check("a bank was unlocked", count("admin_resource_locks", "resource_id = :b AND unlocked_at IS NULL", b=MY_BANK) == 0)
CONTROL = one("SELECT id FROM admin_control_items WHERE status = 'open' ORDER BY id LIMIT 1")
op.post(f"{ADMIN_ROOT}/controls/{CONTROL}/resolve", {})
check("a review item was marked resolved", one("SELECT status FROM admin_control_items WHERE id = :c", c=CONTROL) == "resolved")
OP_ID = op.session()["admin_id"]
NOTE = one("SELECT id FROM admin_notifications WHERE admin_id = :a ORDER BY id LIMIT 1", a=OP_ID)
op.post(f"{ADMIN_ROOT}/notifications/{NOTE}/read", {})
check("a notification was marked read", one("SELECT read_at FROM admin_notifications WHERE id = :n", n=NOTE) is not None)
op.post(f"/admin/examinations/{MY_BANK}/toggle", {}, allow_error_flash=True)  # activation now lives in Exam Configuration

# the standard papers for this session, one click; then a configuration of our own for a past session (practice)
CONFIG = "/admin/entrance-config"
op.post(f"{CONFIG}/standard", {})
check("the standard entrance papers were set up and are live for the current session",
      count("entrance_bank_configs", "session_id = :s AND active = 1", s=CURRENT) == 6)
op.post(f"{CONFIG}/save", {"bank_id": "starter_year7_english", "entry_group": "year7", "subject": "english", "session_id": PAST,
                           "term": "Full Session", "questions_to_serve": "20", "reason": "Last year's paper, for practice"})
PAST_CONFIG = one("SELECT id FROM entrance_bank_configs WHERE session_id = :s", s=PAST)
check("an entrance configuration was created for a past session", PAST_CONFIG is not None and one("SELECT active FROM entrance_bank_configs WHERE id = :c", c=PAST_CONFIG) == 0)
op.post(f"{CONFIG}/save", {"config_id": PAST_CONFIG, "bank_id": "starter_year7_english", "entry_group": "year7", "subject": "english",
                           "session_id": PAST, "term": "Full Session", "questions_to_serve": "25", "reason": "Use 25 questions"})
check("…and edited", one("SELECT questions_to_serve FROM entrance_bank_configs WHERE id = :c", c=PAST_CONFIG) == 25)
op.post(f"{CONFIG}/{PAST_CONFIG}/activate", {"reason": "Check it works"})
check("a configuration was activated", one("SELECT active FROM entrance_bank_configs WHERE id = :c", c=PAST_CONFIG) == 1)
# public entrance practice: nobody registers; a subject serves last session's paper or a bank set up for practice
op.post(f"{CONFIG}/save", {"bank_id": MY_BANK, "entry_group": "year7", "subject": "mathematics", "session_id": PAST, "term": "Full Session",
                           "questions_to_serve": "2", "reason": "Last year's Mathematics paper"})
attempts_before = count("attempts")
listing = visitor.text("/entrance-practice?group=year7")
check("entrance practice: anyone can pick a target class and see its subjects", "Year 7 / JSS 1" in listing and "Mathematics" in listing)
visitor.get("/entrance-practice/year7/mathematics")
picked = visitor.session().get("entrance_practice", {}).get("ids", [])
r = visitor.post("/entrance-practice/year7/mathematics", {f"q_{qid}": "1" for qid in picked})
check("entrance practice: last session's paper was served, taken without signing in, and marked",
      len(picked) == 2 and r.status == 200 and "NOT RECORDED" in r.body)
check("entrance practice: the current session's own bank is never offered", visitor.get("/entrance-practice/year7/english").status_code == 404)
op.text("/admin/practice-tests")
page = op.text("/admin/practice-tests")
check("entrance practice: the administrator sees where each subject's questions come from", "Where each subject" in page)
r = op.post("/admin/practice-tests/save", {"entry_group": "year7", "subject": "english", "source": "custom",
                                            "bank_id": "starter_year7_english", "questions_to_serve": "5"}, valid=False)
check("entrance practice: a bank used by the current examination cannot be offered", r.said("cannot also be offered"))
op.post("/admin/practice-tests/save", {"entry_group": "year7", "subject": "english", "source": "custom",
                                       "bank_id": "posts_english_import", "questions_to_serve": "3"})
check("entrance practice: a custom bank was chosen for a subject",
      count("entrance_practice_settings", "entry_group = 'year7' AND subject = 'english' AND source = 'custom'") == 1)
visitor.get("/entrance-practice/year7/english")
check("entrance practice: the custom questions were served, three at random", len(visitor.session().get("entrance_practice", {}).get("ids", [])) == 3)
check("entrance practice leaves nothing recorded", count("attempts") == attempts_before)

# a candidate: register, sign in, sit a paper, be marked; then the administrator's controls over the result
CANDS = "/admin/candidates"
ADMISSIONS = "/admin/candidates/admissions"
CAND_FORM = {"candidate_name": "Emeka Nwosu", "target_class": "JSS 1", "school_attended": "Sunrise Primary",
             "parent_guardian_name": "Ada Nwosu", "parent_guardian_relationship": "Mother", "primary_mobile": "08031112222",
             "alternative_mobile": "08033334444", "parent_guardian_email": "ada.nwosu@example.test"}
r = op.post(f"{CANDS}/new", CAND_FORM, files={"photo": png("emeka.png")})
CAND_CODE = grab(r'Candidate ID</span><strong class="credential-code">(.+?)</strong>', r.body, "the candidate's ID")
CAND_PASS = grab(r'Password</span><strong class="credential-code">(.+?)</strong>', r.body, "the candidate's password")
CAND = one("SELECT id FROM candidates WHERE candidate_code = :c", c=CAND_CODE)
check("a candidate was registered with three papers", CAND is not None and count("candidate_papers", "candidate_id = :c", c=CAND) == 3)
r = op.post(f"{CANDS}/{CAND}/credentials/reset", {})
CAND_PASS = grab(r'New password</span><strong class="credential-code">(.+?)</strong>', r.body, "the candidate's new password")
r = op.post(f"{CANDS}/new", {**CAND_FORM, "candidate_name": "Second Candidate"})
SPARE_CAND = one("SELECT id FROM candidates WHERE candidate_name = 'Second Candidate'")

candidate = Actor("candidate")
r = candidate.post("/login", {"username": CAND_CODE, "password": CAND_PASS})
check("a candidate signed in", "/candidate/dashboard" in r.location, f"{r.status} {r.location}")
PAPER = one("SELECT id FROM candidate_papers WHERE candidate_id = :c ORDER BY slot LIMIT 1", c=CAND)
candidate.post(f"/candidate/papers/{PAPER}/start", {})
ATT = one("SELECT id FROM attempts WHERE candidate_id = :c", c=CAND)
check("a candidate started a paper (its questions were frozen)", ATT is not None and count("attempt_questions", "attempt_id = :a", a=ATT) >= 25)
served = sql("SELECT question_id, correct_option FROM attempt_questions WHERE attempt_id = :a ORDER BY question_order LIMIT 4", a=ATT)
for qid, right in served:
    r = candidate.post("/answer", {"question_id": qid, "option_index": right})
    check("an exam answer was saved (JSON)", r.json.get("ok") is True)
check("…and re-answering replaces it (upsert)", (candidate.post("/answer", {"question_id": served[0][0], "option_index": 0}).json.get("ok") is True)
      and count("answers", "attempt_id = :a", a=ATT) == 4)
candidate.post("/submit", {})
check("the exam was submitted and marked", one("SELECT status FROM attempts WHERE id = :a", a=ATT) == "submitted"
      and one("SELECT score FROM attempts WHERE id = :a", a=ATT) is not None)
op.post(f"/admin/results/{ATT}/regrade", {})
op.post(f"/admin/results/{ATT}/grant-retake", {})
check("a retake was granted after the result", count("retake_grants", "candidate_id = :c AND used_at IS NULL", c=CAND) == 1)
candidate.post(f"/candidate/papers/{PAPER}/start", {})
check("…and the candidate used it to start the paper again", count("attempts", "candidate_id = :c", c=CAND) == 2)
op.post(f"{CANDS}/{SPARE_CAND}/delete", {})
check("a candidate can be deleted", count("candidates", "id = :c", c=SPARE_CAND) == 0)

# ================================================================ 12b. the admissions waitlist
op.post(f"{CANDS}/new", {**CAND_FORM, "candidate_name": "Third Candidate"})
THIRD_CAND = one("SELECT id FROM candidates WHERE candidate_name = 'Third Candidate'")
paper_bank = one("SELECT bank_id FROM candidate_papers WHERE candidate_id = :c ORDER BY slot LIMIT 1", c=THIRD_CAND)
exam_id = one("SELECT id FROM examinations WHERE bank_id = :b", b=paper_bank)
sql("INSERT INTO attempts (candidate, exam_id, bank_id, started_at, expires_at, submitted_at, score, max_score, percentage, status, candidate_id) "
    "VALUES ('Third Candidate', :e, :b, :now, :now, :now, 62, 100, 62, 'submitted', :c)",
    e=exam_id, b=paper_bank, now="2026-09-01T09:00:00+00:00", c=THIRD_CAND)
# Emeka (CAND) does not appear here: his only attempt was superseded by the retake he started
# above, which is still in progress, so he is correctly not "completed" for this waitlist.
waitlist = op.text(f"{ADMISSIONS}?entry_group=year7")
check("a candidate with a submitted paper appears on the admissions waitlist",
      "Third Candidate" in waitlist and "Pending" in waitlist)
op.post(f"{CANDS}/{THIRD_CAND}/decline", {"note": "Did not meet the cut-off"})
check("a candidate was declined off the waitlist, with a reason",
      one("SELECT admission_status FROM candidates WHERE id = :c", c=THIRD_CAND) == "declined")
op.post(f"{CANDS}/{THIRD_CAND}/admission-reset", {})
check("…and put back on the waitlist", one("SELECT admission_status FROM candidates WHERE id = :c", c=THIRD_CAND) == "pending")
r = op.post(f"{CANDS}/{THIRD_CAND}/admit", {"first_name": "Third", "last_name": "Candidate", "gender": "Female",
                                            "class_id": JSS1, "create_parent_account": "1"})
NEW_STUDENT = one("SELECT admitted_student_id FROM candidates WHERE id = :c", c=THIRD_CAND)
check("a candidate was admitted: a real, enrolled student now exists with a generated number and a login",
      one("SELECT admission_status FROM candidates WHERE id = :c", c=THIRD_CAND) == "admitted" and NEW_STUDENT is not None
      and one("SELECT class_id FROM student_enrolments WHERE student_id = :s AND active = 1", s=NEW_STUDENT) == JSS1
      and one("SELECT login_username FROM students WHERE id = :s", s=NEW_STUDENT) not in (None, ""))
check("…and, since the candidate had guardian contact details, a linked parent portal account was created too",
      count("parent_student_links", "student_id = :s", s=NEW_STUDENT) == 1)
op.post(f"{STUDENTS}/{NEW_STUDENT}/toggle", {})  # deactivated: later exact-count sections (promotion) assume only Ada and Tunde in JSS 1
check("…deactivating the newly admitted student afterwards keeps them out of everyone else's counts below",
      one("SELECT active FROM students WHERE id = :i", i=NEW_STUDENT) == 0)

# ================================================================ 13. staff: roles, accounts, messages
def permission_ids(*codes):
    return [r[0] for r in sql("SELECT id FROM permissions WHERE code = ANY(:c)", c=list(codes))]


op.post(f"{ADMIN_ROOT}/roles/new", {"name": "Records Clerk", "description": "Keeps student records",
                                    "permissions": permission_ids("admin.access", "school.view", "school.students.view", "school.students.edit")})
ROLE = one("SELECT id FROM admin_types WHERE name = 'Records Clerk'")
check("a staff role was created with its permissions", count("admin_type_permissions", "admin_type_id = :r", r=ROLE) == 4)
op.post(f"{ADMIN_ROOT}/roles/{ROLE}/edit", {"name": "Records Clerk", "description": "Keeps and prints student records",
                                            "permissions": permission_ids("admin.access", "school.view", "school.students.view", "school.students.edit", "school.classes.view")})
check("…and edited", count("admin_type_permissions", "admin_type_id = :r", r=ROLE) == 5)

r = op.post(f"{ADMIN_ROOT}/admins/new", {"username": "clerk1", "display_name": "Clerk One", "email": "clerk1@posts.test",
                                        "phone": "08099990000", "whatsapp": "08099990000", "admin_type_ids": [ROLE],
                                        "scope_type": "global"})
CLERK_TEMP = grab(r'Temporary Password</span><strong class="credential-password">(.+?)</strong>', r.body, "the new staff member's password")
CLERK = one("SELECT id FROM admins WHERE username = 'clerk1'")
check("a staff account was created", CLERK is not None and count("admin_role_assignments", "admin_id = :a", a=CLERK) == 1)
op.post(f"{ADMIN_ROOT}/admins/{CLERK}/edit", {"display_name": "Clerk One (JSS)", "email": "clerk1@posts.test", "phone": "08099990000",
                                             "admin_type_ids": [ROLE], "scope_types": ["class"], "scope_values_class": [ "JSS 1"],
                                             "permissions": permission_ids("library.view")})
check("a staff account was edited and limited to one class",
      count("admin_scopes", "admin_id = :a AND scope_type = 'class' AND scope_value = 'JSS 1'", a=CLERK) == 1
      and count("admin_permissions", "admin_id = :a", a=CLERK) == 1)
r = op.post(f"{ADMIN_ROOT}/admins/{CLERK}/credentials/reset", {})
CLERK_TEMP = grab(r'Temporary Password</span><strong class="credential-password">(.+?)</strong>', r.body, "the staff member's new password")
op.post(f"{ADMIN_ROOT}/admins/{CLERK}/toggle", {})
check("a staff account can be suspended", one("SELECT active FROM admins WHERE id = :a", a=CLERK) == 0)
op.post(f"{ADMIN_ROOT}/admins/{CLERK}/toggle", {})
check("…and brought back", one("SELECT active FROM admins WHERE id = :a", a=CLERK) == 1)

clerk = Actor("staff member", admin=True)
r = clerk.post("/login", {"username": "clerk1", "password": CLERK_TEMP})
check("a staff member signs in and is sent to choose a private password", "/admin/password" in r.location, f"{r.status} {r.location}")
clerk.post("/admin/password", {"current_password": CLERK_TEMP, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
check("a staff member's password was changed", one("SELECT password_must_change FROM admins WHERE id = :a", a=CLERK) == 0)
school_admin = Actor("school administrator", admin=True)
r = school_admin.post("/admin/login", {"username": "schooladmin", "password": SCHOOLADMIN_TEMP})
check("the school's own administrator signs in through the older sign-in address too", "/admin/password" in r.location, f"{r.status} {r.location}")

r = op.post(f"{ADMIN_ROOT}/messages/send", {"recipient_id": CLERK, "body": "Please update the JSS 1 register."},
            files={"attachment": ("register.txt", b"JSS 1 register")})
check("a message with an attachment was sent to a colleague", count("admin_messages", "recipient_admin_id = :c AND attachment_path IS NOT NULL", c=CLERK) == 1)

# ================================================================ 14. forgotten passwords, by email
FakeSMTP.sent.clear()
stranger = Actor("someone who forgot their password")
stranger.post("/forgot-password", {"identifier": "clerk1"})
mails = [m for m in FakeSMTP.sent if "Password reset" in (m["Subject"] or "")]
check("a password-recovery email was sent (and a recovery token stored, hashed)", len(mails) == 1 and count("password_reset_tokens", "used_at IS NULL") == 1)
token = grab(r"/reset-password/([A-Za-z0-9_\-]+)", mails[0].get_content() if mails else "", "the recovery link")
stranger.post(f"/reset-password/{token}", {"new_password": "recovered-password-9", "confirm_password": "recovered-password-9"})
check("the password was reset through the emailed link", count("password_reset_tokens", "used_at IS NULL") == 0
      and Actor("signing in again").post("/login", {"username": "clerk1", "password": "recovered-password-9"}).location.endswith("/admin/home"))

# ================================================================ 14b. the school's own profile and look; taking its email and WhatsApp away again
op.post("/admin/school/branding/save", {"school_name": "Posts Academy", "school_motto": "Steady wins", "school_tagline": "Learn well",
                                        "school_phone": "0803 000 1111", "school_email": "hello@posts.test", "school_address": "1 Test Road",
                                        "school_brand_primary": "#0d2b52", "school_brand_accent": "#1674b9"},
        files={"logo": png("logo2.png"), "gallery": [png("d.png"), png("e.png")]})
check("the school changed its own name, contact details and look",
      one("SELECT name FROM schools ORDER BY id LIMIT 1") == "Posts Academy"
      and one("SELECT setting_value FROM school_public_settings WHERE setting_key = 'school_motto'") == "Steady wins")
op.post(f"{DELIVERY}/email/clear", {})
check("the school's own mail settings were removed", count("school_delivery_settings", "setting_key = 'smtp_host'") == 0)
op.post(f"{DELIVERY}/whatsapp/clear", {})
check("…and its WhatsApp settings", count("school_delivery_settings", "setting_key = 'whatsapp_phone_number_id'") == 0)

# ================================================================ 15. promotion to the next session (last, because it moves the students)
PROMO = "/admin/school/promotion"
op.post(f"{PROMO}/progressions", {"action": "save", "from_class_id": JSS1, "to_class_id": JSS2})
check("a class progression rule was saved", count("school_class_progressions", "from_class_id = :f AND to_class_id = :t AND active = 1", f=JSS1, t=JSS2) == 1)
op.post(PROMO, {"action": "generate", "from_session_id": OLD, "to_session_id": PAST})
EMPTY_RUN = one("SELECT id FROM academic_promotion_runs WHERE from_session_id = :f", f=OLD)
op.post(f"{PROMO}/{EMPTY_RUN}", {"action": "cancel"})
check("a promotion draft can be cancelled", one("SELECT status FROM academic_promotion_runs WHERE id = :r", r=EMPTY_RUN) == "cancelled")
op.post(PROMO, {"action": "generate", "from_session_id": CURRENT, "to_session_id": NEXT})
RUN = one("SELECT id FROM academic_promotion_runs WHERE from_session_id = :f AND to_session_id = :t", f=CURRENT, t=NEXT)
items = [r[0] for r in sql("SELECT id FROM academic_promotion_items WHERE run_id = :r ORDER BY id", r=RUN)]
check("a promotion draft was generated for the enrolled students", len(items) == 2)
form = {"action": "save", "item_id": items}
for item in items:
    form[f"action_{item}"] = "promote"
    form[f"reason_{item}"] = "Passed the year"
op.post(f"{PROMO}/{RUN}", form)
op.post(f"{PROMO}/{RUN}", {"action": "approve"})
check("a promotion draft was approved", one("SELECT status FROM academic_promotion_runs WHERE id = :r", r=RUN) == "approved")
op.post(f"{PROMO}/{RUN}", {"action": "commit"})
check("a promotion was committed: both students are now in JSS 2 for the next session",
      count("student_enrolments", "session_id = :s AND class_id = :c", s=NEXT, c=JSS2) == 2)

# ================================================================ 16. the platform console (its own registry database)
def registry(statement, **params):
    with platform_session() as s:
        return s.execute(sa.text(statement), params).scalar()


r = console.post("/platform/team/new", {"username": "ops2", "display_name": "Ops Two", "email": "ops2@example.test"})
OPS2_TEMP = grab(r'id="temp-password">(.+?)</span>', r.body, "the new platform admin's one-time password")
OPS2 = registry("SELECT id FROM platform_admins WHERE username = 'ops2'")
check("a platform admin was added", OPS2 is not None)
ops2 = Actor("second platform admin", PL, PLATFORM_ADAPTER)
ops2.post("/platform/login", {"username": "ops2", "password": OPS2_TEMP})
ops2.post("/platform/password", {"current_password": OPS2_TEMP, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
check("…who chose their own password on first sign-in", registry("SELECT password_must_change FROM platform_admins WHERE id = :i", i=OPS2) in (0, False))
console.post(f"/platform/team/{OPS2}/remove", {})
check("a platform admin's access was removed (the account is kept)", registry("SELECT count(*) FROM platform_admins WHERE id = :i", i=OPS2) == 1
      and registry("SELECT active FROM platform_admins WHERE id = :i", i=OPS2) in (0, False))
r = console.post(f"/platform/team/{OPS2}/restore", {})
grab(r'id="temp-password">(.+?)</span>', r.body, "a restored platform admin's one-time password")
r = console.post(f"/platform/team/{OPS2}/reset-password", {})
OPS2_RESET_TEMP = grab(r'id="temp-password">(.+?)</span>', r.body, "a reset platform admin's one-time password")
console.post(f"/platform/team/{OPS2}/docs-access", {"allow": "1"})
check("the super admin granted a platform admin access to the documentation",
      registry("SELECT docs_access FROM platform_admins WHERE id = :i", i=OPS2) in (1, True))
check("…and a visitor can read the public marketing page but not the documentation",
      Actor("visitor", PL, PLATFORM_ADAPTER).get("/marketing").status_code == 200
      and Actor("stranger", PL, PLATFORM_ADAPTER).get("/docs").status_code in (302, 404))

console.post("/platform/schools/posts/branding", {"school_brand_primary": "#123456", "school_brand_accent": "#1674b9"},
             files={"logo": png("newlogo.png"), "gallery": [png("c.png")]})
check("a school's colours were changed from the console", one("SELECT setting_value FROM school_public_settings WHERE setting_key = 'school_brand_primary'") == "#123456")
console.post("/platform/schools/posts/domains", {"action": "add", "hostname": "portal.postsschool.example"})
check("an address was added for the school", registry("SELECT count(*) FROM tenant_domains WHERE hostname = 'portal.postsschool.example'") == 1)
console.post("/platform/schools/posts/domains", {"action": "remove", "hostname": "portal.postsschool.example"})
check("…and removed", registry("SELECT count(*) FROM tenant_domains WHERE hostname = 'portal.postsschool.example'") == 0)
r = console.post("/platform/numbering/preview", {"candidate_pattern": "{school}-{yy}-{seq:3}", "student_pattern": "", "first_number": "1",
                                                   "slug": "posts", "name": "", "code": ""})
check("the numbering preview shows the school's next codes", r.json.get("candidate", {}).get("ok") is True and r.json["candidate"]["samples"])
console.post("/platform/schools/posts/numbering", {"candidate_pattern": "{school}-{yy}-{seq:3}", "student_pattern": "", "first_number": "1"})
check("a school's numbering rules were changed from the console",
      '{school}-{yy}-{seq:3}' in open(os.path.join(TMP, "tenants", "posts", "numbering.json"), encoding="utf-8").read())
r = console.post("/platform/schools/posts/admins", {"username": "secondadmin", "display_name": "Second Admin"})
grab(r'id="temp-password">(.+?)</span>', r.body, "a new school administrator's one-time password")
check("a school administrator was created from the console", count("admins", "username = 'secondadmin'") == 1)
console.post("/platform/schools/posts/status", {"status": "suspended", "reason": "Testing suspension"})
check("a school was suspended (its portal is refused)", Actor("caller during suspension").get("/login").status_code == 503)
console.post("/platform/schools/posts/status", {"status": "active"})
check("…and reactivated", Actor("caller after reactivation").get("/login").status_code == 200)
r = console.post("/platform/schools/new", {"name": "Second School", "code": "second"}, label="create a second school")
check("a second school was created on request", registry("SELECT count(*) FROM tenants WHERE slug = 'second'") == 1)
console.post("/platform/password", {"current_password": PLATFORM_PASSWORD, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
check("the platform operator changed their own password", console.get("/platform").status_code == 200)

# sign-out (each with its own throw-away sign-in so the sweep below still has people signed in)
leaver = Actor("student signing out")
leaver.post("/login", {"username": ADA_LOGIN, "password": NEW_PASSWORD})
r = leaver.post("/logout", {})
check("a student signed out", "student_id" not in leaver.session())
op_leaver = Actor("platform admin signing out", PL, PLATFORM_ADAPTER)
op_leaver.post("/platform/login", {"username": "ops", "password": NEW_PASSWORD})
op_leaver.post("/platform/logout", {})
check("a platform admin signed out", "platform_admin_id" not in op_leaver.session())

# Changing or resetting a password ends the account's other sign-ins (core/session_guard.py). Two people the
# sweep below sends junk as had their passwords changed by other steps above (the staff member through the
# emailed recovery link, the second platform admin through the super admin's reset), so they sign in again with
# the password they have now, to be signed-in people once more.
clerk.post("/login", {"username": "clerk1", "password": "recovered-password-9"})
ops2.post("/platform/login", {"username": "ops2", "password": OPS2_RESET_TEMP})

# ================================================================ 17. JUNK: every route, refused cleanly
# For each route: an empty form, two forms of hostile values, no CSRF token, and (for routes with an id in the
# address) an id that does not exist. Anything under 500, with no database error, is a clean refusal.
CLEAN = {200, 201, 204, 301, 302, 303, 307, 308, 400, 401, 403, 404, 405, 409, 410, 413, 415, 422, 429, 503}
HOSTILE = "<script>alert(1)</script> ' \" ; -- é中%s{{7*7}}"
counter = [0]


def fresh_addr():
    counter[0] += 1
    return f"10.{100 + counter[0] // 60000}.{(counter[0] // 250) % 250}.{counter[0] % 250 + 1}"


def bogus_path(rule):
    path = re.sub(r"<int:[^>]+>", "987654321", rule)
    path = re.sub(r"<path:[^>]+>", "nope/x", path)
    return re.sub(r"<[^>]+>", "nope", path)


def junk_variants(fields):
    yield "an empty form", {}, True
    yield "hostile text in every field", {k: HOSTILE for k in fields}, True
    yield "huge and negative numbers in every field", {k: ("-99999999999999999999" if i % 2 else "99999999999999999999") for i, k in enumerate(fields)}, True
    yield "a NUL byte in every field", {k: "before\x00after" for k in fields}, True
    yield "no CSRF token", {k: "1" for k in fields}, False


junk_total = 0
bounced = []  # routes whose junk was all turned away at the sign-in page: it never reached the route's own code
ANONYMOUS = {"visitor", "student signing out", "platform admin signing out"}
# A password reset ends every sign-in of the account it resets (core/session_guard.py), and an empty
# form is a valid request to a reset route, so junk sent to one really does end the sessions of the
# person it is aimed at. Those routes go last, so the junk for every other route is still sent by
# people who are signed in.
def _ends_sign_ins(rule):
    return any(word in rule for word in ("reset", "credentials", "/account"))


for rule in sorted(coverage.write_rules(A.app.url_map), key=lambda r: (_ends_sign_ins(r), r)):
    if rule in coverage.EXEMPT or rule not in HITS:
        continue
    who, path, fields, _ = HITS[rule]
    attempts = [(name, path, form, token) for name, form, token in junk_variants(fields)]
    attempts.append(("an id that does not exist", bogus_path(rule), {}, True))
    turned_away = 0
    for name, target, form, token in attempts:
        junk_total += 1
        r = who.post(target, form, valid=False, token=token, addr=fresh_addr(), label=f"junk ({name}) {who.name} POST {rule}")
        if r.status not in CLEAN:
            check(f"junk ({name}) {rule}: refused cleanly", False, f"HTTP {r.status}")
        if r.status == 302 and re.search(r"/(platform/)?login(\?|$)", r.location) and token:
            turned_away += 1
    if turned_away == len(attempts) - 1 and who.name not in ANONYMOUS and not rule.endswith(("/login", "/logout")):
        bounced.append(rule)
check("the junk reached each route as a signed-in person (none was simply sent back to sign in)", not bounced, "; ".join(bounced))

# ================================================================ 18. the guard, and the numbers
exercised = set(HITS)
missing = coverage.unaccounted(A.app.url_map)
check(f"GUARD: every route that accepts a POST is exercised here or exempt with a reason ({len(missing)} are not)", not missing,
      "; ".join(missing))
gone, both = coverage.stale(A.app.url_map)
check("GUARD: pg_posts_coverage.py names no route that no longer exists, and none twice", not gone and not both, f"{gone} {both}")
declared = set(coverage.EXERCISED)
check("GUARD: what this run really submitted is exactly what pg_posts_coverage.EXERCISED claims",
      exercised == declared,
      f"submitted but not declared: {sorted(exercised - declared)}; declared but not submitted: {sorted(declared - exercised)}")
check("no submission anywhere caused a database error", not PROBLEMS, "; ".join(f"{a}: {b}" for a, b in PROBLEMS[:8]))
total = len(coverage.write_rules(A.app.url_map))
print()
print(f"POST coverage: {total} routes accept a write; {len(exercised)} exercised on PostgreSQL, {len(coverage.EXEMPT)} exempt.")
for rule, why in sorted(coverage.EXEMPT.items()):
    print(f"  EXEMPT {rule}: {why}")
print(f"{POSTS[0]} form submissions were made ({junk_total} of them junk).")
if exercised != declared:
    print("EXERCISED = frozenset({")
    for rule in sorted(exercised):
        print(f'    "{rule}",')
    print("})")
# @@ END @@
dispose_engines()
DROP_TEST_DATABASES()
shutil.rmtree(TMP, ignore_errors=True)
print()
bad = [x for x in results if not x[1]]
print(f"{len(results) - len(bad)}/{len(results)} checks passed")
sys.exit(1 if bad else 0)
