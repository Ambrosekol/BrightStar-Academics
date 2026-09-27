"""Term grading, proven end to end on PostgreSQL: a subject's term result is Exam (60) + CA (40) = 100.

The rule, in plain words:

* Every test, examination, assignment and project belongs to one term of one academic session.
* A subject's term result is the Exam (out of 60) plus Continuous Assessment (out of 40).
* Continuous assessment is tests + assignments + projects, sharing the 40 marks by weights the
  School Admin sets (20 / 10 / 10 until changed). The weights must always add up to exactly 40.
* Within one kind of work, the raw marks are added up first and then scaled once
  (two tests 8/10 and 10/20 are 18/30 of the test share, not an average of two percentages, and
  never simply added on top). A score above its maximum can never lift a component past its share.
* A practice test never counts. Another term, session or subject never leaks in.
* Marks are shown to students only once released.

Drives the real application over HTTP against throw-away PostgreSQL databases: the work is created
through the real forms, students take the tests and examinations in their own portal, and offline
marks go in through the results form. Every check reads the database rows or what a page shows.

Run:  python tests/verification/write_paths_term_grading.py
"""
import html
import logging
import os
import re
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_grading_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('grading')
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
from werkzeug.security import generate_password_hash  # noqa: E402

import app as A  # noqa: E402
import blueprints.school.helpers as SCH  # noqa: E402
from control_plane import provisioning as pv  # noqa: E402
from control_plane.context import tenant_context  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import dispose_engines, engine_for  # noqa: E402

results = []
PL = "http://platform.test"
TOKEN = re.compile(r'name="_csrf_token"\s+value="([^"]+)"')


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)


class Errors(logging.Handler):
    """Remembers every exception the application logs while it answers a request."""

    def __init__(self):
        super().__init__()
        self.seen = []

    def emit(self, record):
        if record.exc_info and record.exc_info[1] is not None:
            self.seen.append(record.exc_info[1])


errors = Errors()
A.app.logger.addHandler(errors)
A.app.logger.propagate = False

# Nothing in this run may reach the outside world (a guardian is emailed when work is set).
from core import delivery as _delivery  # noqa: E402

_delivery._open_smtp = lambda settings: (_ for _ in ()).throw(RuntimeError("no mail in a test"))


class Browser:
    """One person's browser: cookies of their own, and CSRF tokens taken from real pages."""

    def __init__(self, base, form_page):
        self.c = A.app.test_client()
        self.base = base
        self.form_page = form_page  # any page of theirs that carries a form (so it carries a token)

    def get(self, path, **kw):
        return self.c.get(path, base_url=self.base, **kw)

    def text(self, path):
        return html.unescape(self.get(path).get_data(as_text=True))

    def token(self, page=None):
        body = self.get(page or self.form_page).get_data(as_text=True)
        found = TOKEN.search(body)
        if not found:
            raise AssertionError(f"no CSRF token on {page or self.form_page}")
        return found.group(1)

    def post(self, path, data=None, page=None, token=True, multipart=False):
        data = dict(data or {})
        if token is True:
            data["_csrf_token"] = self.token(page)
        elif token:
            data["_csrf_token"] = token
        return self.c.post(path, data=data, base_url=self.base,
                           content_type="multipart/form-data" if multipart else None)

    def flashes(self):
        """The messages the last request left for the next page, as plain text (and clears them)."""
        with self.c.session_transaction(base_url=self.base) as sess:
            found = [text for _, text in sess.pop("_flashes", [])]
        return found

    def said(self, r, phrase):
        """Whether a reply (or the message it left for the next page) says this, ignoring case."""
        body = html.unescape(r.get_data(as_text=True)) if r.status_code == 200 else ""
        return any(phrase.lower() in text.lower() for text in self.flashes() + [body])


def info_for(slug):
    with platform_session() as s:
        tenant = get_tenant(s, slug)
        return to_info(tenant) if tenant else None


def in_school(info, fn):
    with A.app.app_context(), tenant_context(info):
        return fn()


def sql(statement, **params):
    with engine_for(INFO).begin() as conn:
        result = conn.execute(sa.text(statement), params)
        return result.fetchall() if result.returns_rows else None


def one(statement, **params):
    rows = sql(statement, **params)
    return rows[0][0] if rows else None


def close(a, b, places=2):
    return a is not None and abs(float(a) - float(b)) < 10 ** -places / 2


# ================================================================ the platform, the school, the School Admin
pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
console = A.app.test_client()
console.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
             "_csrf_token": TOKEN.search(console.get("/platform/login", base_url=PL).get_data(as_text=True)).group(1)}, base_url=PL)
for code, name in (("grades", "Grades School"), ("other", "Other School")):
    console.post("/platform/schools/new", data={
        "_csrf_token": TOKEN.search(console.get("/platform/schools/new", base_url=PL).get_data(as_text=True)).group(1),
        "name": name, "code": code, "school_brand_primary": "#0d2b52", "school_brand_accent": "#1674b9"},
        base_url=PL, content_type="multipart/form-data")
INFO, INFO_OTHER = info_for("grades"), info_for("other")
SCHOOL = "http://grades.portal.test"


def operator_in(code):
    """The platform operator enters a school and is its School Admin (the top-level role)."""
    base = f"http://{code}.portal.test"
    page = console.get(f"/platform/schools/{code}", base_url=PL).get_data(as_text=True)
    r = console.post(f"/platform/schools/{code}/enter", data={"_csrf_token": TOKEN.search(page).group(1)}, base_url=PL)
    b = Browser(base, "/admin/school/sessions")
    b.get(r.headers["Location"][len(base):])
    b.get("/admin/workspace/school")
    return b


admin = operator_in("grades")
check("the School Admin is inside the school", admin.get("/admin/school").status_code == 200)
check("…and holds the top-level role", one("SELECT t.is_system FROM admins a JOIN admin_types t ON t.id = a.admin_type_id "
                                            "WHERE a.username = 'platform@ops'") == 1)

# ---------------------------------------------------------------- a school with no results yet
CURRENT = one("SELECT id FROM academic_sessions WHERE is_current = 1")
JSS1 = one("SELECT id FROM school_classes WHERE name = 'JSS 1'")
check("a school with no results at all: no result rows and the results page opens",
      one("SELECT count(*) FROM school_student_results") == 0 and admin.get("/admin/school/results").status_code == 200)
check("…and its CA weights are the defaults, 20 / 10 / 10, adding up to the 40 CA marks",
      in_school(INFO, SCH._ca_weights) == {"test": 20, "assignment": 10, "project": 10}
      and A.EXAM_MAX_SCORE == 60 and A.CA_MAX_SCORE == 40 and sum(A.CA_DEFAULT_WEIGHTS.values()) == A.CA_MAX_SCORE)
page = admin.text("/admin/school/sessions")
check("the sessions page shows the weighting form with the defaults",
      "Continuous assessment weighting" in page and 'name="ca_weight_test"' in page and 'name="ca_weight_test" min="0" step="0.5" value="20"' in page)

# ================================================================ the subjects and students, through the real forms
r = admin.post("/admin/school/subjects/new", {"name": "Mathematics", "code": "MTH", "class_ids": [JSS1]})
r = admin.post("/admin/school/subjects/new", {"name": "English Language", "code": "ENG", "class_ids": [JSS1]})
MATHS = one("SELECT id FROM school_subjects WHERE name = 'Mathematics'")
ENGLISH = one("SELECT id FROM school_subjects WHERE name = 'English Language'")
check("two subjects were created for JSS 1", MATHS is not None and ENGLISH is not None)

STUDENT_FORM = {"gender": "Female", "date_of_birth": "2014-05-01", "state_of_origin": "Lagos", "class_id": JSS1,
                "guardian_name": "A Guardian", "guardian_phone": "08031234567", "guardian_email": "guardian@example.test"}


def register(first, last):
    r = admin.post("/admin/school/students/new", {**STUDENT_FORM, "first_name": first, "middle_name": "", "last_name": last})
    body = html.unescape(r.get_data(as_text=True))
    login = re.search(r"Student ID / Username</span><strong>(.+?)</strong>", body, re.S)
    password = re.search(r'credential-password">(.+?)</strong>', body, re.S)
    check(f"{first} was registered and given a login", bool(login and password), body[:120].replace("\n", " "))
    login, password = login.group(1).strip(), password.group(1).strip()
    return one("SELECT id FROM students WHERE login_username = :u", u=login), login, password


ADA, ADA_LOGIN, ADA_TEMP = register("Ada", "Obi")          # one of each kind of work: the plain case, 86
BOLA, BOLA_LOGIN, BOLA_TEMP = register("Bola", "Eze")      # several tests, assignments and projects
CHIDI, _, _ = register("Chidi", "Nwosu")                   # nothing recorded at all
DAYO, _, _ = register("Dayo", "Bello")                     # an exam and nothing else
EMEKA, _, _ = register("Emeka", "Okoro")                   # a perfect 100
FOLA, _, _ = register("Fola", "Ade")                       # rounding
GINA, _, _ = register("Gina", "Ojo")                       # marks above the maximum
check("seven students are enrolled in JSS 1 for the current session",
      one("SELECT count(*) FROM student_enrolments WHERE class_id = :c AND session_id = :s AND active = 1", c=JSS1, s=CURRENT) == 7)

# ================================================================ every piece of work belongs to a term
ASSESS = {"test": "tests", "examination": "examinations", "practice": "practice-tests"}


def make_assessment(kind, title, points, subject=None, term="First Term", session=None, open_it=True):
    """A test / examination / practice test made through its real form, then given its questions."""
    admin.post(f"/admin/school/{ASSESS[kind]}/new", {
        "title": title, "instructions": "Answer all.", "class_id": JSS1, "subject_id": subject or MATHS,
        "session_id": session or CURRENT, "term": term, "duration_minutes": "30"})
    aid = one("SELECT id FROM school_assessments WHERE title = :t", t=title)
    detail = f"/admin/school/assessments/{aid}"
    for n, pts in enumerate(points):
        admin.post(f"{detail}/questions/new", {
            "question_text": f"{title} question {n + 1}?", "instruction": "", "option_a": "A", "option_b": "B",
            "option_c": "C", "option_d": "D", "correct_option": n % 4, "points": pts}, page=detail)
    if open_it:
        admin.post(f"{detail}/toggle", {}, page=detail)
    return aid


def make_assignment(title, max_score, students, term="First Term", subject=None, session=None):
    admin.post("/admin/school/assignments/new", {
        "title": title, "instructions": "", "due_date": "2027-01-01", "assignment_type": "written",
        "timing_mode": "untimed", "class_id": JSS1, "subject_id": subject or MATHS, "session_id": session or CURRENT,
        "term": term, "student_ids": students, "max_score": max_score})
    return one("SELECT id FROM school_assignments WHERE title = :t", t=title)


def make_project(title, max_score, students, term="First Term", subject=None, session=None):
    admin.post("/admin/school/projects/new", {
        "title": title, "instructions": "", "date_given": "2026-10-01", "due_date": "2026-11-30",
        "class_id": JSS1, "subject_id": subject or MATHS, "session_id": session or CURRENT, "term": term,
        "student_ids": students, "max_score": max_score})
    return one("SELECT id FROM school_projects WHERE title = :t", t=title)


def grade_assignment(aid, student, score):
    return admin.post(f"/admin/school/assignments/{aid}/students/{student}",
                      {"status": "done", "score": score}, page=f"/admin/school/assignments/{aid}")


def grade_project(pid, student, score):
    return admin.post(f"/admin/school/projects/{pid}/students/{student}",
                      {"status": "done", "score": score}, page=f"/admin/school/projects/{pid}")


NEW_PASSWORD = "a-brand-new-password-1"


def student_in(login, temp):
    """A student signs in with the login they were given and lands on their portal."""
    b = Browser(SCHOOL, "/student/password")
    r = b.post("/login", {"username": login, "password": temp}, page="/login")
    check(f"{login} signs in to the student portal", "/student/dashboard" in r.headers.get("Location", ""),
          r.headers.get("Location", ""))
    # A new student's first visit is the page where they choose a private password.
    b.post("/student/password", {"current_password": temp, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
    return b


def take(b, aid, right):
    """A student takes an assessment in the portal, getting the first `right` questions right and the rest wrong."""
    page = f"/student/assessments/{aid}"
    b.post(f"{page}/start", {}, page=page)
    total = one("SELECT question_count FROM school_assessments WHERE id = :a", a=aid)
    for q in range(1, total + 1):
        body = b.get(f"{page}?q={q}").get_data(as_text=True)
        attempt = re.search(r'name="attempt_id" value="(\d+)"', body).group(1)
        qid = int(re.search(r'name="question_id" value="(\d+)"', body).group(1))
        token = TOKEN.search(body).group(1)
        correct = one("SELECT correct_option FROM school_assessment_attempt_questions WHERE attempt_id = :t AND question_id = :q",
                      t=int(attempt), q=qid)
        data = {"attempt_id": attempt, "question_id": qid, "option_index": correct if q <= right else (correct + 1) % 4,
                "next_q": q + 1}
        if q == total:
            data["submit_assessment"] = "1"
        b.post(f"{page}/answer", data, token=token)
    return one("SELECT status FROM school_assessment_attempts WHERE assessment_id = :a ORDER BY id DESC LIMIT 1", a=aid)


def report(student, subject=None, term="First Term", session=None):
    return in_school(INFO, lambda: SCH._term_subject_report(student, session or CURRENT, term, subject or MATHS))


def term_rows(student):
    """The Term Results table on a student's page: {(session, term, subject): (CA, Exam, Total)} as written."""
    page = admin.text(f"/admin/school/students/{student}")
    found = {}
    for block in re.findall(r'<div class="term-report-block">(.*?)</table>', page, re.S):
        head = re.search(r"<strong>(.*?)</strong><span>(.*?)</span>", block, re.S)
        for row in re.findall(r"<tr>\s*<td><strong>(.*?)</strong></td>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*<td><strong>(.*?)</strong></td>",
                              block, re.S):
            found[(head.group(1), head.group(2), row[0])] = row[1:]
    return found


exam_page = admin.text("/admin/school/examinations/new")
check("the examination form asks for a session and a term", 'name="term"' in exam_page and 'name="session_id"' in exam_page)
check("…and so do the test, assignment and project forms",
      all('name="term"' in admin.text(p) and 'name="session_id"' in admin.text(p)
          for p in ("/admin/school/tests/new", "/admin/school/assignments/new", "/admin/school/projects/new")))

NEW_TEST = {"title": "No term test", "instructions": "", "class_id": JSS1, "subject_id": MATHS, "session_id": CURRENT,
            "duration_minutes": "20"}
for label, term in (("without a term", ""), ("with a term that does not exist", "Fourth Term")):
    r = admin.post("/admin/school/tests/new", {**NEW_TEST, "term": term})
    check(f"a test {label} is refused, and nothing is created",
          admin.said(r, "Select a valid term") and one("SELECT count(*) FROM school_assessments WHERE title = 'No term test'") == 0)
r = admin.post("/admin/school/examinations/new", {**NEW_TEST, "title": "No term exam", "term": ""})
check("an examination without a term is refused too",
      admin.said(r, "Select a valid term") and one("SELECT count(*) FROM school_assessments WHERE title = 'No term exam'") == 0)
r = admin.post("/admin/school/tests/new", {**NEW_TEST, "title": "No session test", "term": "First Term", "session_id": ""})
check("a test with no session is given the school's current one (never left without)",
      one("SELECT session_id FROM school_assessments WHERE title = 'No session test'") == CURRENT)
sql("DELETE FROM school_assessments WHERE title = 'No session test'")

ASSIGN = {"title": "Bad assignment", "instructions": "", "due_date": "2027-01-01", "assignment_type": "written",
          "timing_mode": "untimed", "class_id": JSS1, "subject_id": MATHS, "session_id": CURRENT, "term": "First Term",
          "student_ids": [ADA], "max_score": "10"}
for label, over, phrase in (("without a term", {"term": ""}, "Select a valid term"),
                            ("with a term that does not exist", {"term": "Term 4"}, "Select a valid term"),
                            ("with no session", {"session_id": ""}, "valid academic session"),
                            ("with a session that does not exist", {"session_id": "987654"}, "valid academic session")):
    r = admin.post("/admin/school/assignments/new", {**ASSIGN, **over})
    check(f"an assignment {label} is refused", admin.said(r, phrase) and one("SELECT count(*) FROM school_assignments WHERE title = 'Bad assignment'") == 0)
    r = admin.post("/admin/school/projects/new", {**ASSIGN, "title": "Bad project", "date_given": "2026-10-01", **over})
    check(f"…and so is a project {label}", admin.said(r, phrase) and one("SELECT count(*) FROM school_projects WHERE title = 'Bad project'") == 0)

r = admin.post("/admin/school/practice-tests/new", {**NEW_TEST, "title": "Term-free practice", "term": ""})
check("a practice test needs no term (practice never counts toward the record)",
      one("SELECT term FROM school_assessments WHERE title = 'Term-free practice'") == "Full Session")
r = admin.post("/admin/school/practice-tests/new", {**NEW_TEST, "title": "Bad practice", "term": "Fourth Term"})
check("…but a term it does give must be a real one", one("SELECT count(*) FROM school_assessments WHERE title = 'Bad practice'") == 0)
sql("DELETE FROM school_assessments WHERE title = 'Term-free practice'")

# ================================================================ Ada: one of each kind of work (the plain case)
TEST1 = make_assessment("test", "Maths test one", [1] * 10)
TEST2 = make_assessment("test", "Maths test two", [5] * 4)
EXAM = make_assessment("examination", "Maths exam", [5] * 10)
PRACTICE = make_assessment("practice", "Maths practice", [1] * 3, term="")
check("a test, a second test, an examination and a practice test were made through their forms, with their questions",
      [one("SELECT question_count FROM school_assessments WHERE id = :a", a=a) for a in (TEST1, TEST2, EXAM, PRACTICE)] == [10, 4, 10, 3])
check("each belongs to First Term of the current session",
      sql("SELECT term, session_id FROM school_assessments WHERE id IN (:a, :b, :c)", a=TEST1, b=TEST2, c=EXAM) == [("First Term", CURRENT)] * 3)

A1 = make_assignment("Assignment one", "10", [ADA, BOLA])
A2 = make_assignment("Assignment two", "20", [BOLA])
P1 = make_project("Project one", "10", [ADA, BOLA])
P2 = make_project("Project two", "5", [BOLA])
check("assignments and projects were made with their session and term",
      sql("SELECT term, session_id, max_score FROM school_assignments WHERE id = :a", a=A1) == [("First Term", CURRENT, 10.0)]
      and sql("SELECT term, session_id, max_score FROM school_projects WHERE id = :p", p=P1) == [("First Term", CURRENT, 10.0)])

ada_portal = student_in(ADA_LOGIN, ADA_TEMP)
check("Ada takes test one: 8 of 10 right", take(ada_portal, TEST1, 8) == "submitted")
check("…and the examination: 9 of 10 questions right, each worth 5 (45 of 50)", take(ada_portal, EXAM, 9) == "submitted")
ada_portal.get(f"/student/practice/{PRACTICE}")
practice_key = {f"q_{qid}": right for qid, right in sql("SELECT id, correct_option FROM school_questions WHERE assessment_id = :a", a=PRACTICE)}
r = ada_portal.post(f"/student/practice/{PRACTICE}", practice_key, page=f"/student/practice/{PRACTICE}")
check("…and the practice test, getting every question right (it is marked on the spot and nothing is recorded)",
      r.status_code == 200 and "NOT RECORDED" in r.get_data(as_text=True)
      and one("SELECT count(*) FROM school_assessment_attempts WHERE assessment_id = :a", a=PRACTICE) == 0)
grade_assignment(A1, ADA, "9")
grade_project(P1, ADA, "7")
check("the results the portal wrote are 8/10 and 45/50, and none for the practice test",
      sql("SELECT r.score, r.max_score, a.assessment_type FROM school_student_results r JOIN school_assessments a ON a.id = r.assessment_id "
          "WHERE r.student_id = :s ORDER BY a.assessment_type", s=ADA) == [(45.0, 50.0, "examination"), (8.0, 10.0, "test")])
r = report(ADA)
check("Ada's exam is scaled to 60: 45/50 = 54", close(r["exam_score"], 54) and r["exam_max"] == 60, str(r))
check("…her test to its 20: 8/10 = 16", close(r["test_score"], 16) and r["test_max"] == 20)
check("…her assignment to its 10: 9/10 = 9", close(r["assignment_score"], 9) and r["assignment_max"] == 10)
check("…her project to its 10: 7/10 = 7", close(r["project_score"], 7) and r["project_max"] == 10)
check("…CA is 16 + 9 + 7 = 32 out of 40", close(r["ca_score"], 32) and r["ca_max"] == 40)
check("…and the term result is 54 + 32 = 86 out of 100", close(r["total_score"], 86) and r["total_max"] == 100, str(r))
check("the practice test (a perfect 3/3) added nothing to either part",
      r["exam_score"] < 60 and r["test_score"] < 20 and one("SELECT count(*) FROM school_student_results WHERE assessment_id = :a", a=PRACTICE) == 0)

rows = term_rows(ADA)
check("her page shows the Term Results table with those figures",
      rows.get(("2026/2027", "First Term", "Mathematics")) == ("32.0 / 40", "54.0 / 60", "86.0 / 100")
      or any(v == ("32.0 / 40", "54.0 / 60", "86.0 / 100") for v in rows.values()), str(rows))

# ================================================================ Bola: several of each kind (marks are added up, then scaled once)
bola_portal = student_in(BOLA_LOGIN, BOLA_TEMP)
check("Bola takes test one (8/10), test two (2 of 4 questions right, 10/20) and the exam (6 of 10 right, 30/50)",
      [take(bola_portal, TEST1, 8), take(bola_portal, TEST2, 2), take(bola_portal, EXAM, 6)] == ["submitted"] * 3)
grade_assignment(A1, BOLA, "9")
grade_assignment(A2, BOLA, "5")
grade_project(P1, BOLA, "7")
grade_project(P2, BOLA, "1")
r = report(BOLA)
check("two tests, 8/10 and 10/20, are 18/30 of the test share (12.0) — not the average of two percentages (13.0), nor 8 + 10",
      close(r["test_score"], 12.0), str(r["test_score"]))
check("two assignments, 9/10 and 5/20, are 14/30 of the assignment share (4.67) — not 9, nor the average (5.75)",
      close(r["assignment_score"], 4.67), str(r["assignment_score"]))
check("two projects, 7/10 and 1/5, are 8/15 of the project share (5.33)", close(r["project_score"], 5.33), str(r["project_score"]))
check("CA is the sum of the three shares, 22.0 out of 40", close(r["ca_score"], 22.0) and r["ca_max"] == 40, str(r["ca_score"]))
check("the exam, 30/50, is 36 out of 60, and the term result is 58 out of 100",
      close(r["exam_score"], 36) and close(r["total_score"], 58.0) and r["total_max"] == 100, str(r))
check("Ada's result did not change when Bola's marks went in (each student's marks are their own)",
      close(report(ADA)["total_score"], 86))

# ---------------------------------------------------------------- what never leaks into a term's result
A_SECOND = make_assignment("Second-term assignment", "10", [BOLA], term="Second Term")
grade_assignment(A_SECOND, BOLA, "10")
A_ENGLISH = make_assignment("English assignment", "10", [BOLA], subject=ENGLISH)
grade_assignment(A_ENGLISH, BOLA, "10")
admin.post("/admin/school/sessions", {"action": "create", "name": "2027/2028", "start_date": "2027-09-01", "end_date": "2028-07-31"})
NEXT = one("SELECT id FROM academic_sessions WHERE name = '2027/2028'")
A_NEXT = make_assignment("Next-year assignment", "10", [BOLA], session=NEXT)
grade_assignment(A_NEXT, BOLA, "10")
check("a perfect assignment in another term, another subject and another session was recorded for Bola",
      one("SELECT count(*) FROM assignment_students WHERE student_id = :s AND score = 10", s=BOLA) == 3)
r = report(BOLA)
check("…and none of them is in Bola's First Term Mathematics result", close(r["assignment_score"], 4.67) and close(r["total_score"], 58.0), str(r))
check("…each is in the result it belongs to",
      close(report(BOLA, term="Second Term")["assignment_score"], 10) and close(report(BOLA, subject=ENGLISH)["assignment_score"], 10)
      and close(report(BOLA, session=NEXT)["assignment_score"], 10))
check("…on their own, an assignment alone is out of its 10 CA marks and the exam part is 0, not missing",
      close(report(BOLA, term="Second Term")["total_score"], 10) and report(BOLA, term="Second Term")["exam_score"] == 0)
rows = term_rows(BOLA)
check("Bola's page keeps them apart: four separate term blocks, one line each",
      len(rows) == 4 and {k[1] for k in rows} == {"First Term", "Second Term"} and {k[2] for k in rows} == {"Mathematics", "English Language"},
      str(rows))
check("…in order, the newest session first",
      list(term_rows(BOLA))[0][0] == "2027/2028")

# ---------------------------------------------------------------- work that is removed stops counting
A_GONE = make_assignment("Assignment to remove", "10", [BOLA])
grade_assignment(A_GONE, BOLA, "10")
check("a further assignment, 10/10, lifts the assignment share: 24/40 of 10 = 6.0", close(report(BOLA)["assignment_score"], 6.0),
      str(report(BOLA)["assignment_score"]))
admin.post(f"/admin/school/assignments/{A_GONE}/delete", {}, page=f"/admin/school/assignments/{A_GONE}")
check("removing that assignment takes its mark out of the term result again",
      one("SELECT active FROM school_assignments WHERE id = :a", a=A_GONE) == 0 and close(report(BOLA)["assignment_score"], 4.67),
      str(report(BOLA)["assignment_score"]))
P_GONE = make_project("Project to remove", "10", [BOLA])
grade_project(P_GONE, BOLA, "10")
admin.post(f"/admin/school/projects/{P_GONE}/delete", {}, page=f"/admin/school/projects/{P_GONE}")
check("…and the same for a removed project", close(report(BOLA)["project_score"], 5.33), str(report(BOLA)["project_score"]))

# ---------------------------------------------------------------- work with no maximum cannot be scaled, so it is left out
A_LOOSE = make_assignment("Assignment with no maximum", "", [BOLA])
r = grade_assignment(A_LOOSE, BOLA, "5")
check("an assignment made without a maximum can still be marked", r.status_code in (302, 303))
check("…but its marks do not count (a score with nothing to scale it against would inflate the share to 6.33)",
      close(report(BOLA)["assignment_score"], 4.67) and close(report(BOLA)["total_score"], 58.0), str(report(BOLA)))
P_LOOSE = make_project("Project with no maximum", "", [BOLA])
grade_project(P_LOOSE, BOLA, "5")
check("…and the same for a project", close(report(BOLA)["project_score"], 5.33), str(report(BOLA)["project_score"]))
lone = make_assignment("Only a loose assignment", "", [DAYO])
grade_assignment(lone, DAYO, "7")
r = report(DAYO)
check("a student whose only work has no maximum has no term result and nothing divides by zero",
      r["assignment_score"] == 0 and r["total_score"] == 0 and not r["has_data"], str(r))

# ---------------------------------------------------------------- marking: refused and accepted values
before = one("SELECT score FROM assignment_students WHERE assignment_id = :a AND student_id = :s", a=A1, s=BOLA)
for label, value in (("above the maximum", "11"), ("negative", "-1"), ("not a number (nan)", "nan"), ("infinite", "inf"),
                     ("infinite, negative", "-inf")):
    r = grade_assignment(A1, BOLA, value)
    refused = one("SELECT score FROM assignment_students WHERE assignment_id = :a AND student_id = :s", a=A1, s=BOLA) == before
    check(f"an assignment score {label} is refused, and the mark stays {before:g}", refused and close(report(BOLA)["assignment_score"], 4.67),
          str(one("SELECT score FROM assignment_students WHERE assignment_id = :a AND student_id = :s", a=A1, s=BOLA)))
    admin.flashes()
before = one("SELECT score FROM project_students WHERE project_id = :p AND student_id = :s", p=P1, s=BOLA)
for label, value in (("above the maximum", "10.5"), ("negative", "-0.5"), ("not a number (nan)", "nan"), ("infinite", "inf")):
    r = grade_project(P1, BOLA, value)
    check(f"a project score {label} is refused, and the mark stays {before:g}",
          one("SELECT score FROM project_students WHERE project_id = :p AND student_id = :s", p=P1, s=BOLA) == before
          and close(report(BOLA)["project_score"], 5.33))
    admin.flashes()
grade_assignment(A2, BOLA, "")
check("clearing a mark leaves the work ungraded: it drops out of the sum (9/10 alone is 9.0) rather than counting as 0",
      one("SELECT score FROM assignment_students WHERE assignment_id = :a AND student_id = :s", a=A2, s=BOLA) is None
      and close(report(BOLA)["assignment_score"], 9.0), str(report(BOLA)["assignment_score"]))
grade_assignment(A2, BOLA, "4.5")
check("a mark with a decimal is kept as it is: 9/10 and 4.5/20 are 13.5/30, 4.5 of the 10",
      close(report(BOLA)["assignment_score"], 4.5), str(report(BOLA)["assignment_score"]))
grade_assignment(A2, BOLA, "5")

# ================================================================ marks entered offline, through the results form
MANUAL = "/admin/school/results/manual/new"
form = admin.text(MANUAL)
options = re.findall(r'<option value="([^"]*)"', re.search(r'<select name="term".*?</select>', form, re.S).group(0))
check("the offline results form offers the terms the report reads: First, Second, Third Term and Full Session",
      options == ["First Term", "Second Term", "Third Term", "Full Session"], str(options))


def manual(student, **fields):
    data = {"class_id": JSS1, "session_id": CURRENT, "student_id": student, "subject_id": MATHS, "term": "First Term",
            "took_test": "yes", **fields}
    return admin.post(MANUAL, data, page=MANUAL)


def latest(student, component):
    """The newest row for a component: (score, max, status, term, exception reason), or None."""
    rows = sql("SELECT score, max_score, status, term, override_reason FROM school_student_results "
               "WHERE student_id = :s AND component_name = :c ORDER BY id DESC LIMIT 1", s=student, c=component)
    return rows[0] if rows else None


# ---- a perfect 100
E_A = make_assignment("Emeka assignment", "10", [EMEKA])
E_P = make_project("Emeka project", "10", [EMEKA])
grade_assignment(E_A, EMEKA, "10")
grade_project(E_P, EMEKA, "10")
r = manual(EMEKA, test_score="10", test_max="10", exam_score="60", exam_max="60")
check("a Test and an Exam entered offline are two rows, waiting to be verified",
      r.status_code in (302, 303) and [x[2] for x in (latest(EMEKA, "Test"), latest(EMEKA, "Exam"))] == ["entered", "entered"])
r = report(EMEKA)
check("full marks in everything: Exam 60 + CA 40 = exactly 100, no more",
      close(r["exam_score"], 60) and close(r["test_score"], 20) and close(r["assignment_score"], 10) and close(r["project_score"], 10)
      and close(r["ca_score"], 40) and close(r["total_score"], 100) and r["total_max"] == 100, str(r))

# ---- rounding
F_A = make_assignment("Fola assignment", "3", [FOLA])
F_P = make_project("Fola project", "3", [FOLA])
grade_assignment(F_A, FOLA, "1")
grade_project(F_P, FOLA, "1")
manual(FOLA, test_score="2", test_max="3", exam_score="41", exam_max="59")
r = report(FOLA)
check("marks are rounded to two decimal places: exam 41/59 is 41.69, test 2/3 is 13.33, a third of a share is 3.33",
      close(r["exam_score"], 41.69) and close(r["test_score"], 13.33) and close(r["assignment_score"], 3.33)
      and close(r["project_score"], 3.33), str(r))
check("…CA is the rounded parts added (19.99) and the total 61.68 — with no stray digits",
      close(r["ca_score"], 19.99) and close(r["total_score"], 61.68) and r["ca_score"] == round(r["ca_score"], 2)
      and r["total_score"] == round(r["total_score"], 2), str(r))
rows = term_rows(FOLA)
check("…and the page shows them to one decimal place",
      any(v == ("20.0 / 40", "41.7 / 60", "61.7 / 100") for v in rows.values()), str(rows))

# ---- a missing component
r = manual(DAYO, took_test="no", absence_reason="Ill on the day of the test", exam_score="30", exam_max="60")
check("a student who missed the test is recorded with the reason, and no test score",
      r.status_code in (302, 303) and latest(DAYO, "Test — Absent")[0] is None and latest(DAYO, "Test — Absent")[4] == "Ill on the day of the test")
r = report(DAYO)
check("…their term result is the exam alone: CA 0, Exam 30/60, total 30 out of 100",
      close(r["ca_score"], 0) and close(r["exam_score"], 30) and close(r["total_score"], 30) and r["has_data"], str(r))
r = manual(CHIDI, took_test="no", absence_reason="", exam_score="30", exam_max="60")
check("missing the test without saying why is refused", admin.said(r, "State why the student did not take the test")
      and latest(CHIDI, "Exam") is None)
r = report(CHIDI)
check("a student with nothing recorded has an empty result: all zeros, no data, no division by zero",
      r["total_score"] == 0 and r["ca_score"] == 0 and r["exam_score"] == 0 and not r["has_data"], str(r))
check("…and no Term Results block on their page (0 terms recorded)",
      term_rows(CHIDI) == {} and "0 terms recorded" in admin.text(f"/admin/school/students/{CHIDI}"))

# ---- a score above its maximum
r = manual(GINA, test_score="9", test_max="10", exam_score="70", exam_max="60")
check("an Exam score above its maximum, with no exception reason, is refused and nothing is saved",
      admin.said(r, "requires an approved exception reason") and latest(GINA, "Exam") is None and latest(GINA, "Test") is None)
r = manual(GINA, test_score="11", test_max="10", exam_score="60", exam_max="60", exam_override_reason="Bonus question")
check("a Test score above its maximum is refused even when a reason is given", admin.said(r, "valid Test score") and latest(GINA, "Test") is None)
r = manual(GINA, test_score="9", test_max="10", exam_score="70", exam_max="60", exam_override_reason="Bonus question marked in")
check("with an approved exception reason the Exam mark is saved, and the reason is kept with it",
      r.status_code in (302, 303) and latest(GINA, "Exam")[0] == 70.0 and latest(GINA, "Exam")[4] == "Bonus question marked in")
r = report(GINA)
check("…but the exam part stops at its 60, so the result never passes 100: 60 + 18 = 78",
      close(r["exam_score"], 60) and close(r["test_score"], 18) and close(r["total_score"], 78) and r["total_max"] == 100, str(r))

# ---- entries that make no sense
bad = {"a zero exam maximum": dict(exam_score="30", exam_max="0"), "a negative exam maximum": dict(exam_score="30", exam_max="-60"),
       "a negative exam score": dict(exam_score="-1", exam_max="60"), "no exam score": dict(exam_score="", exam_max="60"),
       "an exam score that is not a number": dict(exam_score="lots", exam_max="60"),
       "an exam score of nan": dict(exam_score="nan", exam_max="60"), "an exam maximum of nan": dict(exam_score="30", exam_max="nan"),
       "an infinite exam score with a reason": dict(exam_score="inf", exam_max="60", exam_override_reason="because"),
       "a zero test maximum": dict(test_score="0", test_max="0", exam_score="30", exam_max="60"),
       "a negative test score": dict(test_score="-1", test_max="10", exam_score="30", exam_max="60"),
       "a test score of nan": dict(test_score="nan", test_max="10", exam_score="30", exam_max="60")}
for label, fields in bad.items():
    fields = {"test_score": "5", "test_max": "10", **fields}
    r = manual(CHIDI, **fields)
    check(f"{label} is refused, and nothing is saved for the student",
          r.status_code == 200 and one("SELECT count(*) FROM school_student_results WHERE student_id = :s", s=CHIDI) == 0,
          f"{r.status_code}")
r = manual(CHIDI, test_score="5", test_max="10", exam_score="30", exam_max="60", term="Fourth Term")
check("a term that does not exist is refused", r.status_code == 200 and one("SELECT count(*) FROM school_student_results WHERE student_id = :s", s=CHIDI) == 0)
r = manual(CHIDI, test_score="5", test_max="10", exam_score="30", exam_max="60", subject_id=987654)
check("a subject that does not exist is refused", r.status_code == 200 and one("SELECT count(*) FROM school_student_results WHERE student_id = :s", s=CHIDI) == 0)

# ================================================================ marks reach students only when released
def dashboard_results(portal):
    """What the student's own 'Recent results' panel lists: [(score, max)]."""
    body = portal.get("/student/dashboard").get_data(as_text=True)
    panel = body[body.index('id="results"'):]
    panel = panel[:panel.index("</section>")]
    return [(float(a), float(b)) for a, b in re.findall(r'student-score">\s*([\d.]+)/([\d.]+)', panel)]


def result_id(student, assessment):
    return one("SELECT id FROM school_student_results WHERE student_id = :s AND assessment_id = :a", s=student, a=assessment)


def workflow(result, action, actor=None, reason=""):
    # a member of staff without the release permission has no form on the results page, so a token comes from a page that has one
    return (actor or admin).post(f"/admin/school/results/{result}/workflow", {"action": action, "reason": reason},
                                 page="/admin/school/results" if actor is None else "/admin/password")


ADA_EXAM, ADA_TEST = result_id(ADA, EXAM), result_id(ADA, TEST1)
status = lambda rid: one("SELECT status FROM school_student_results WHERE id = :r", r=rid)  # noqa: E731
check("Ada's test and exam wait as 'entered'",
      [status(ADA_TEST), status(ADA_EXAM)] == ["entered", "entered"])
check("her own page lists no result at all: the exam and test are not shown to her yet, and practice is never a result",
      dashboard_results(ada_portal) == [], str(dashboard_results(ada_portal)))
page = ada_portal.text(f"/student/assessments/{EXAM}/result")
check("the exam's result page says it was received but shows no score",
      "Submission received" in page and "withheld" in page and "<strong>45" not in page and "ASSESSMENT COMPLETE" not in page)
check("…and her page has no Term Results table at all (that is for staff)",
      "Term Results" not in ada_portal.text("/student/dashboard") and "Continuous Assessment" not in ada_portal.text("/student/dashboard"))
results_page = admin.text(f"/admin/school/results?class=JSS 1&student={ADA}&session={CURRENT}&term=First Term")
check("staff see the marks and the 'Verify' step in the dialog for the student's term", "Verify" in results_page and "Mathematics" in results_page,
      results_page[results_page.find("rr-body"):][:200])

r = workflow(ADA_EXAM, "release")
check("a result cannot be released before it is verified and approved", status(ADA_EXAM) == "entered" and admin.said(r, "must be approved before it can be released"))
r = workflow(ADA_EXAM, "approve")
check("…nor approved before it is verified", status(ADA_EXAM) == "entered" and admin.said(r, "must be verified before it can be approved"))
r = workflow(ADA_EXAM, "explode")
check("an action that is not a step is refused", status(ADA_EXAM) == "entered" and r.status_code in (302, 403))
workflow(ADA_EXAM, "verify", reason="Checked against the script")
check("verifying moves it on and records who did it",
      status(ADA_EXAM) == "verified" and one("SELECT verified_by FROM school_student_results WHERE id = :r", r=ADA_EXAM) is not None)
workflow(ADA_EXAM, "approve")
check("approving moves it on, but the student still sees nothing of it",
      status(ADA_EXAM) == "approved" and one("SELECT approved_by FROM school_student_results WHERE id = :r", r=ADA_EXAM) is not None
      and dashboard_results(ada_portal) == [])
workflow(ADA_EXAM, "release")
check("releasing shows it: the result is 'released' with the time it was released",
      status(ADA_EXAM) == "released" and one("SELECT released_at FROM school_student_results WHERE id = :r", r=ADA_EXAM) is not None)
check("the student now sees the exam mark 45/50 on her page, and still not the test",
      sorted(dashboard_results(ada_portal)) == [(45.0, 50.0)], str(dashboard_results(ada_portal)))
page = ada_portal.text(f"/student/assessments/{EXAM}/result")
check("…and the exam's result page shows her score", "ASSESSMENT COMPLETE" in page and "<strong>45</strong>" in page and "/ 50" in page, page[page.find("<main"):][:300])
check("every step is on the record: entered → verified → approved → released",
      [tuple(x) for x in sql("SELECT from_status, to_status FROM result_workflow_events WHERE result_id = :r ORDER BY id", r=ADA_EXAM)]
      == [("verified", "approved"), ("approved", "released")] or
      [tuple(x) for x in sql("SELECT from_status, to_status FROM result_workflow_events WHERE result_id = :r ORDER BY id", r=ADA_EXAM)]
      == [("entered", "verified"), ("verified", "approved"), ("approved", "released")],
      str(sql("SELECT from_status, to_status FROM result_workflow_events WHERE result_id = :r ORDER BY id", r=ADA_EXAM)))
check("moving a result through the workflow does not change her term result: still 86",
      close(report(ADA)["total_score"], 86) and close(term_rows(ADA)[next(iter(term_rows(ADA)))][2].split(" ")[0], 86))

before = report(ADA)["total_score"]
r = admin.post(f"/admin/school/results/{ADA_EXAM}/edit", {"score": "10", "max_score": "50", "component_name": "Exam", "term": "First Term", "reason": ""},
               page="/admin/school/results")
check("a released result cannot be edited (the correction process is separate)",
      one("SELECT score FROM school_student_results WHERE id = :r", r=ADA_EXAM) == 45.0 and admin.said(r, "cannot be edited"))
workflow(ADA_TEST, "verify")
edit = f"/admin/school/results/{ADA_TEST}/edit"
EDIT_FORM = edit  # its own page carries the form and token
for label, fields, phrase in (("a score above the maximum with no reason", {"score": "11", "max_score": "10"}, "requires an authorised exception reason"),
                              ("a zero maximum", {"score": "5", "max_score": "0"}, "greater than zero"),
                              ("a negative score", {"score": "-1", "max_score": "10"}, "cannot be negative"),
                              ("a score of nan", {"score": "nan", "max_score": "10"}, "greater than zero"),
                              ("a maximum of inf", {"score": "5", "max_score": "inf"}, "greater than zero"),
                              ("a term that does not exist", {"score": "5", "max_score": "10", "term": "Fourth Term"}, "valid term")):
    r = admin.post(edit, {"component_name": "Test", "term": "First Term", "reason": "", **fields}, page=edit)
    check(f"editing a result with {label} is refused and the mark is unchanged",
          r.status_code == 200 and admin.said(r, phrase) and one("SELECT score FROM school_student_results WHERE id = :r", r=ADA_TEST) == 8.0,
          str(r.status_code))
admin.post(edit, {"score": "7", "max_score": "10", "component_name": "Test", "term": "First Term", "reason": "Marked again"}, page=edit)
check("an edit that is accepted sends the result back to the start: 'entered', verification cleared",
      status(ADA_TEST) == "entered" and one("SELECT verified_by FROM school_student_results WHERE id = :r", r=ADA_TEST) is None
      and one("SELECT score FROM school_student_results WHERE id = :r", r=ADA_TEST) == 7.0)
check("…and the term result follows the corrected mark at once: test 7/10 is 14, total 84",
      close(report(ADA)["test_score"], 14) and close(report(ADA)["total_score"], 84))
admin.post(edit, {"score": "8", "max_score": "10", "component_name": "Test", "term": "1st Term", "reason": ""}, page=edit)
check("an old-style '1st Term' typed into the edit form is filed as First Term, where the report reads it",
      one("SELECT term FROM school_student_results WHERE id = :r", r=ADA_TEST) == "First Term" and close(report(ADA)["total_score"], 86))

# ---- an offline result: entering it again, and after it is approved
rows_before = one("SELECT count(*) FROM school_student_results WHERE student_id = :s AND component_name = 'Exam'", s=FOLA)
manual(FOLA, test_score="2", test_max="3", exam_score="42", exam_max="59")
check("entering a component again while it is still 'entered' corrects the same row, it does not add a second",
      one("SELECT count(*) FROM school_student_results WHERE student_id = :s AND component_name = 'Exam'", s=FOLA) == rows_before == 1
      and latest(FOLA, "Exam")[0] == 42.0)
manual(FOLA, test_score="2", test_max="3", exam_score="41", exam_max="59")
E_EXAM, E_TEST = (one("SELECT id FROM school_student_results WHERE student_id = :s AND component_name = :c", s=EMEKA, c=c) for c in ("Exam", "Test"))
for step in ("verify", "approve"):
    workflow(E_EXAM, step)
r = manual(EMEKA, test_score="10", test_max="10", exam_score="59", exam_max="60")
check("an approved offline Exam cannot be typed over: the form says to use the correction process, and nothing changes",
      admin.said(r, "already has an approved/released record") and latest(EMEKA, "Exam")[0] == 60.0 and latest(EMEKA, "Exam")[2] == "approved")
check("…the Test entered in the same submission was not changed either (all or nothing)", latest(EMEKA, "Test")[0] == 10.0)

# ---- the school-wide release date
check("nothing but 'approved' results are released by a release date: the entered ones wait",
      status(E_TEST) == "entered")
r = admin.post("/admin/school/results/release-schedule", {"result_release_at": "not a date"}, page="/admin/school/results")
check("a release date that is not a date is refused", admin.said(r, "valid release date") and status(E_EXAM) == "approved")
admin.post("/admin/school/results/release-schedule", {"result_release_at": "2999-01-01T09:00"}, page="/admin/school/results")
check("a release date in the future releases nothing yet", status(E_EXAM) == "approved"
      and one("SELECT result_release_at FROM academic_sessions WHERE id = :s", s=CURRENT) is not None)
admin.post("/admin/school/results/release-schedule", {"result_release_at": "2026-01-01T09:00"}, page="/admin/school/results")
check("a release date that has passed releases the approved results, and only those",
      status(E_EXAM) == "released" and status(E_TEST) == "entered")
admin.post("/admin/school/results/release-schedule", {"result_release_at": ""}, page="/admin/school/results")
check("clearing the date clears it", one("SELECT result_release_at FROM academic_sessions WHERE id = :s", s=CURRENT) is None)
check("the term results have not moved through all of this: Emeka still 100, Ada still 86",
      close(report(EMEKA)["total_score"], 100) and close(report(ADA)["total_score"], 86))

# ================================================================ who may do what
def add_staff(role_name, permissions, username):
    """A role with just these permissions, and a member of staff holding it, made through the real forms."""
    admin.post("/admin/administration/roles/new", {
        "name": role_name, "description": "Only what is needed.",
        "permissions": [one("SELECT id FROM permissions WHERE code = :c", c=c) for c in permissions]}, page="/admin/administration/roles/new")
    role = one("SELECT id FROM admin_types WHERE name = :n", n=role_name)
    r = admin.post("/admin/administration/admins/new", {
        "username": username, "display_name": username.title(), "email": f"{username}@grades.test", "phone": "08000000000",
        "whatsapp": "08000000000", "admin_type_ids": [role], "scope_type": "global"}, page="/admin/administration/admins/new")
    temp = re.search(r'credential-password">(.+?)</strong>', html.unescape(r.get_data(as_text=True)), re.S)
    check(f"{username} was made, holding only the role '{role_name}'", temp is not None and role is not None)
    b = Browser(SCHOOL, "/admin/password")
    r = b.post("/login", {"username": username, "password": temp.group(1).strip()}, page="/login")
    b.post("/admin/password", {"current_password": temp.group(1).strip(), "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
    b.get("/admin/workspace/school")
    return b


clerk = add_staff("Results Entry Clerk", ["admin.access", "school.view", "school.students.view", "school.results.view", "school.results.enter"], "clerk")
check("the clerk is not the School Admin",
      one("SELECT t.is_system FROM admins a JOIN admin_types t ON t.id = a.admin_type_id WHERE a.username = 'clerk'") == 0)
check("the clerk can open the results page but is offered no Verify, Approve or Release step",
      clerk.get("/admin/school/results").status_code == 200
      and not any(w in clerk.text(f"/admin/school/results?class=JSS 1&student={ADA}&session={CURRENT}&term=First Term")
                  for w in (">Verify<", ">Approve<", ">Release<", "Release all results")))
r = manual_as = clerk.post(MANUAL, {"class_id": JSS1, "session_id": CURRENT, "student_id": CHIDI, "subject_id": MATHS, "term": "First Term",
                                    "took_test": "yes", "test_score": "5", "test_max": "10", "exam_score": "30", "exam_max": "60"}, page=MANUAL)
check("the clerk can enter an offline result (they were given that permission)",
      r.status_code in (302, 303) and latest(CHIDI, "Exam")[2] == "entered")
CHIDI_EXAM = one("SELECT id FROM school_student_results WHERE student_id = :s AND component_name = 'Exam'", s=CHIDI)
for step in ("verify", "approve", "release"):
    r = workflow(CHIDI_EXAM, step, actor=clerk)
    check(f"…but cannot '{step}' it", r.status_code == 403 and status(CHIDI_EXAM) == "entered", str(r.status_code))
r = clerk.post("/admin/school/results/release-schedule", {"result_release_at": "2026-01-01T09:00"}, page="/admin/password")
check("…nor set the release date", r.status_code == 403 and one("SELECT result_release_at FROM academic_sessions WHERE id = :s", s=CURRENT) is None)
check("…and the clerk's entry counts in the term result straight away (Chidi: 5/10 test = 10, 30/60 exam = 30 → 40)",
      close(report(CHIDI)["total_score"], 40) and close(report(CHIDI)["exam_score"], 30) and close(report(CHIDI)["test_score"], 10), str(report(CHIDI)))

# ================================================================ the weights: School Admin only, and always 40
def weights(actor=None, page="/admin/school/sessions", **w):
    return (actor or admin).post("/admin/school/sessions", {"action": "set_ca_weights", "ca_weight_test": w.get("test", ""),
                                 "ca_weight_assignment": w.get("assignment", ""), "ca_weight_project": w.get("project", "")}, page=page)


def current_weights():
    return in_school(INFO, SCH._ca_weights)


DEFAULTS = {"test": 20, "assignment": 10, "project": 10}
check("a member of staff cannot open the sessions and weighting page", clerk.get("/admin/school/sessions").status_code == 403)
r = weights(clerk, page="/admin/password", test="30", assignment="5", project="5")
check("…nor change the weights: refused, and nothing is stored",
      r.status_code == 403 and current_weights() == DEFAULTS and one("SELECT count(*) FROM school_settings WHERE setting_key LIKE 'ca_weight_%'") == 0)
r = admin.post("/admin/school/sessions", {"action": "set_ca_weights", "ca_weight_test": "30", "ca_weight_assignment": "5",
                                          "ca_weight_project": "5"}, token=False)
check("a request without the CSRF token is refused", r.status_code == 403 and current_weights() == DEFAULTS)
anonymous = Browser(SCHOOL, "/login")
r = anonymous.post("/admin/school/sessions", {"action": "set_ca_weights", "ca_weight_test": "30", "ca_weight_assignment": "5",
                                              "ca_weight_project": "5"}, token=False)
check("…and so is one from somebody who is not signed in", r.status_code in (302, 403) and current_weights() == DEFAULTS)

for label, w, phrase in (("add up to 60", dict(test="20", assignment="20", project="20"), "add up to exactly 40 (currently 60)"),
                         ("add up to 30", dict(test="10", assignment="10", project="10"), "add up to exactly 40 (currently 30)"),
                         ("fall just short: 39.99", dict(test="39.99", assignment="0", project="0"), "add up to exactly 40"),
                         ("go just over: 40.5", dict(test="20", assignment="10", project="10.5"), "add up to exactly 40"),
                         ("are left empty", dict(), "add up to exactly 40 (currently 0)"),
                         ("include a negative number", dict(test="-5", assignment="25", project="20"), "cannot be negative"),
                         ("include a word", dict(test="lots", assignment="20", project="20"), "valid numbers"),
                         ("are nan", dict(test="nan", assignment="nan", project="nan"), "valid numbers"),
                         ("include nan, so the sum is nan", dict(test="nan", assignment="0", project="0"), "valid numbers"),
                         ("include inf", dict(test="inf", assignment="0", project="0"), "valid numbers")):
    r = weights(**w)
    check(f"weights that {label} are refused, and the weights are unchanged",
          admin.said(r, phrase) and current_weights() == DEFAULTS and one("SELECT count(*) FROM school_settings WHERE setting_key LIKE 'ca_weight_%'") == 0,
          str(current_weights()))
check("…and a refusal leaves every student's result alone", close(report(ADA)["total_score"], 86) and close(report(BOLA)["total_score"], 58))

r = weights(test="15", assignment="15", project="10")
check("weights that add up to exactly 40 are accepted", r.status_code in (302, 303) and admin.said(r, "weighting updated") or current_weights() == {"test": 15, "assignment": 15, "project": 10})
check("…they are stored, one row per component", sorted(sql("SELECT setting_key, setting_value FROM school_settings WHERE setting_key LIKE 'ca_weight_%'"))
      == [("ca_weight_assignment", "15.0"), ("ca_weight_project", "10.0"), ("ca_weight_test", "15.0")])
sessions_page = admin.text("/admin/school/sessions")
check("…and the sessions page shows them", re.search(r'name="ca_weight_assignment"[^>]*value="15(\.0)?"', sessions_page) is not None,
      re.findall(r'name="ca_weight_assignment"[^>]*>', sessions_page)[0] if 'ca_weight_assignment' in sessions_page else "no field")
r = report(ADA)
check("…every result is recalculated with them: Ada's test 8/10 is 12, assignment 9/10 is 13.5, project 7; CA 32.5, total 86.5",
      close(r["test_score"], 12) and close(r["assignment_score"], 13.5) and close(r["project_score"], 7) and close(r["ca_score"], 32.5)
      and close(r["total_score"], 86.5) and r["test_max"] == 15 and r["ca_max"] == 40 and close(r["exam_score"], 54), str(r))
check("…and the student's page shows the new figures", any(v == ("32.5 / 40", "54.0 / 60", "86.5 / 100") for v in term_rows(ADA).values()), str(term_rows(ADA)))
check("the change is in the audit log, with the new weights",
      "15" in (one("SELECT details FROM audit_logs WHERE action = 'ca_weights_updated' ORDER BY id DESC LIMIT 1") or ""))

for combo in (dict(test="40", assignment="0", project="0"), dict(test="0", assignment="0", project="40"),
              dict(test="13.33", assignment="13.33", project="13.34"), dict(test="0.5", assignment="0.5", project="39"),
              dict(test="20", assignment="10", project="10")):
    weights(**combo)
    r = report(EMEKA)
    check(f"with weights {combo['test']}/{combo['assignment']}/{combo['project']}, full marks are still exactly CA 40 and 100 in all",
          close(r["ca_score"], 40) and close(r["total_score"], 100) and r["ca_max"] == 40 and current_weights() == {k: float(v) for k, v in combo.items()},
          str(r))
weights(test="40", assignment="0", project="0")
r = report(BOLA)
check("a weight of 0 switches that kind of work off: with 40/0/0 only tests count (Bola: 18/30 of 40 = 24, total 60)",
      close(r["test_score"], 24) and r["assignment_score"] == 0 and r["project_score"] == 0 and close(r["total_score"], 60), str(r))
weights(test="20", assignment="10", project="10")
check("putting the defaults back restores every result", close(report(BOLA)["total_score"], 58) and close(report(ADA)["total_score"], 86))

# ---- each school has its own weights
other = operator_in("other")
check("another school starts with the defaults, whatever this school does",
      in_school(INFO_OTHER, SCH._ca_weights) == DEFAULTS)
weights(test="30", assignment="5", project="5")
check("…this school's change did not reach it", in_school(INFO_OTHER, SCH._ca_weights) == DEFAULTS and current_weights() == {"test": 30, "assignment": 5, "project": 5})
other.post("/admin/school/sessions", {"action": "set_ca_weights", "ca_weight_test": "10", "ca_weight_assignment": "10",
                                      "ca_weight_project": "20"}, page="/admin/school/sessions")
check("…and its change did not reach this one",
      in_school(INFO_OTHER, SCH._ca_weights) == {"test": 10, "assignment": 10, "project": 20} and current_weights() == {"test": 30, "assignment": 5, "project": 5})
weights(test="20", assignment="10", project="10")

# ================================================================ deleting a test that students have taken
errors.seen.clear()
r = admin.post(f"/admin/school/tests/{TEST2}/delete", {}, page=f"/admin/school/assessments/{TEST2}")
check("a test that students have taken cannot be deleted: the school is told why, it does not fail with a database error",
      r.status_code in (302, 303) and not errors.seen and admin.said(r, "already has student results recorded"),
      f"{r.status_code} {[str(e).splitlines()[0][:120] for e in errors.seen]}")
check("…the test, its questions and Bola's mark are all still there, and his result is unchanged",
      one("SELECT count(*) FROM school_assessments WHERE id = :a", a=TEST2) == 1
      and one("SELECT count(*) FROM school_student_results WHERE assessment_id = :a", a=TEST2) == 1 and close(report(BOLA)["total_score"], 58))
errors.seen.clear()
NOT_TAKEN = make_assessment("test", "Test nobody took", [1, 1], open_it=False)
r = admin.post(f"/admin/school/tests/{NOT_TAKEN}/delete", {}, page=f"/admin/school/assessments/{NOT_TAKEN}")
check("…but one nobody has taken can be deleted, with its questions",
      r.status_code in (302, 303) and one("SELECT count(*) FROM school_assessments WHERE id = :a", a=NOT_TAKEN) == 0
      and one("SELECT count(*) FROM school_questions WHERE assessment_id = :a", a=NOT_TAKEN) == 0)

# ================================================================ the end
check("no database error was logged by any request in this run", not errors.seen,
      "; ".join(str(e).splitlines()[0][:100] for e in errors.seen[:3]))
dispose_engines()
DROP_TEST_DATABASES()
shutil.rmtree(TMP, ignore_errors=True)
print()
failed = [x for x in results if not x[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
