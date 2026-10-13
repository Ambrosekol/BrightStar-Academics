"""Theory questions and marking, proven end to end on PostgreSQL. In plain words:

A. Setting the paper: an examination gets objective and theory questions (each theory question with its marks and a
   marking guide); a practice test refuses a theory question.
B. Taking it: a student sees a box to write in (never the marking guide), their answer is saved as they type, an
   answer over the limit is refused, and moving on with no option chosen no longer fails. On submitting, a paper with
   written answers has a result with no score; one whose theory was left blank is scored at once.
C. Holding the result: a result waiting for marking cannot be verified, sent on or typed over.
D. Who marks: To mark shows the paper only to its subject teacher, with how many scripts wait; another subject's
   teacher is refused the paper, the marks and the split.
E. Marking: answers are shown as plain text (a script typed into an answer is never run); a mark above the question's
   marks is refused; once every written answer is marked the score is filled in.
F. The split: the teacher moves marks to theory, and every score on the paper is recalculated; a split that leaves
   the objective part nothing, or more than the paper's total, is refused.
G. Locks: a mark changed after the result was checked sends it back to be checked; a released result's marks and the
   paper's split are locked. Every mark and split change is in the activity log.
"""
import atexit
import json
import logging
import os
import re
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_theory_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('theory')
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
A.app.config['BACKGROUND_INLINE'] = True
from control_plane import provisioning as pv  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import engine_for  # noqa: E402

results = []
PROBLEMS = []
PL = "http://platform.test"
ALPHA = "http://alpha.portal.test"


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


def csrf_from(body):
    found = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', body) or re.search(r'id="csrfToken" value="([^"]+)"', body)
    return found.group(1) if found else ""


class Person:
    count = [0]

    def __init__(self, label):
        self.client, self.label = A.app.test_client(), label
        Person.count[0] += 1
        self.addr = f"10.91.{Person.count[0]}.1"

    def _note(self, r, method, path):
        if r.status_code >= 500:
            PROBLEMS.append(f"{self.label}: {method} {path} answered {r.status_code}")
        return r

    def get(self, path, **kw):
        return self._note(self.client.get(path, base_url=ALPHA, environ_base={"REMOTE_ADDR": self.addr}, **kw), "GET", path)

    def text(self, path):
        return self.get(path).get_data(as_text=True)

    def post(self, path, data=None, page=None, token=None):
        data = dict(data or {})
        data["_csrf_token"] = token or csrf_from(self.text(page or "/admin/password"))
        return self._note(self.client.post(path, data=data, base_url=ALPHA, environ_base={"REMOTE_ADDR": self.addr}), "POST", path)

    def said(self):
        with self.client.session_transaction(base_url=ALPHA) as sess:
            found = list(sess.pop("_flashes", []))
        return " | ".join(text for _, text in found).lower()


pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
console = A.app.test_client()
console.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                      "_csrf_token": csrf_from(console.get("/platform/login", base_url=PL).get_data(as_text=True))}, base_url=PL)
console.post("/platform/schools/new", data={"_csrf_token": csrf_from(console.get("/platform/schools/new", base_url=PL).get_data(as_text=True)),
                                            "name": "Alpha School", "code": "alpha"},
             base_url=PL, content_type="multipart/form-data")
with platform_session() as s:
    INFO = to_info(get_tenant(s, "alpha"))


def sql(statement, **params):
    with engine_for(INFO).begin() as conn:
        result = conn.execute(sa.text(statement), params)
        return result.fetchall() if result.returns_rows else None


def one(statement, **params):
    rows = sql(statement, **params)
    return rows[0][0] if rows else None


page = console.get("/platform/schools/alpha", base_url=PL).get_data(as_text=True)
r = console.post("/platform/schools/alpha/enter", data={"_csrf_token": csrf_from(page)}, base_url=PL)
op = Person("School Admin")
op.get(r.headers["Location"][len(ALPHA):])
op.get("/admin/workspace/school")

PASSWORD = "Fixture-password-9"
HASH = generate_password_hash(PASSWORD, method="pbkdf2:sha256:1000")
J1 = one("SELECT id FROM school_classes WHERE name = 'JSS 1'")
SESSION = one("SELECT id FROM academic_sessions WHERE is_current = 1")
op.post("/admin/school/subjects/new", {"name": "Mathematics", "code": "MTH", "class_ids": [J1]}, page="/admin/school/subjects/new")
op.post("/admin/school/subjects/new", {"name": "English Language", "code": "ENG", "class_ids": [J1]}, page="/admin/school/subjects/new")
MATHS = one("SELECT id FROM school_subjects WHERE name = 'Mathematics'")
ENGLISH = one("SELECT id FROM school_subjects WHERE name = 'English Language'")
role = {name: one("SELECT id FROM admin_types WHERE name = :n", n=name) for name in ("Subject Teacher",)}
TERM = "First Term"


def new_staff(username, subject_id):
    payload = {"username": username, "display_name": username.replace(".", " ").title(),
               "admin_type_ids": [str(role["Subject Teacher"])], "duty_class": [str(J1)], "duty_subject": [str(subject_id)],
               "duty_department": [""]}
    op.post("/admin/administration/admins/new", payload, page="/admin/administration/admins/new")
    aid = one("SELECT id FROM admins WHERE username = :u", u=username)
    sql("UPDATE admins SET password_hash = :h, password_must_change = 0 WHERE id = :i", h=HASH, i=aid)
    person = Person(username)
    person.post("/login", {"username": username, "password": PASSWORD}, page="/login")
    person.get("/admin/workspace/school")
    return person, aid


maths, MATHS_T = new_staff("maths.teacher", MATHS)
english, ENGLISH_T = new_staff("english.teacher", ENGLISH)
check("two subject teachers are set up, one for Mathematics and one for English in JSS 1",
      MATHS and ENGLISH and one("SELECT COUNT(*) FROM teaching_duties WHERE admin_id IN (:a, :b)", a=MATHS_T, b=ENGLISH_T) == 2)

with A.app.app_context():
    from control_plane.context import tenant_context
    from datetime import datetime, timezone
    with tenant_context(INFO):
        from models import Student, StudentEnrolment, db
        now = datetime.now(timezone.utc).isoformat()
        school_id = one("SELECT id FROM schools LIMIT 1")
        STU = {}
        for code, first in (("TH-1", "Ada"), ("TH-2", "Bayo"), ("TH-3", "Chidi")):
            st = Student(admission_no=code, student_number=code, first_name=first, last_name="Test", created_at=now, active=1,
                         school_id=school_id, login_username=code.lower(), login_password_hash=HASH, account_active=1,
                         password_must_change=0, gender="Female")
            db.session.add(st)
            db.session.flush()
            db.session.add(StudentEnrolment(student_id=st.id, class_id=J1, session_id=SESSION, enrolled_at=now, active=1))
            STU[first] = st.id
        db.session.commit()
        db.session.remove()


def student(code):
    person = Person(code)
    person.post("/login", {"username": code.lower(), "password": PASSWORD}, page="/login")
    return person


# ================================================================ A. setting the paper
maths.post("/admin/school/examinations/new", {"title": "Maths exam", "instructions": "", "class_id": J1, "subject_id": MATHS,
                                              "duration_minutes": "60", "session_id": SESSION, "term": TERM},
           page="/admin/school/examinations/new")
EXAM = one("SELECT id FROM school_assessments WHERE title = 'Maths exam'")
detail = f"/admin/school/assessments/{EXAM}"
for n in range(2):
    maths.post(f"{detail}/questions/new", {"question_type": "objective", "question_text": f"Objective {n + 1}?", "instruction": "",
                                          "option_a": "A", "option_b": "B", "option_c": "C", "option_d": "D",
                                          "correct_option": 0, "points": 2}, page=detail)
GUIDE = "Award 1 mark for each correct step; the secret marking guide."
for n in range(2):
    maths.post(f"{detail}/questions/new", {"question_type": "theory", "question_text": f"Explain idea {n + 1}.", "instruction": "",
                                          "marking_guide": GUIDE, "points": 5}, page=detail)
kinds = sql("SELECT question_type, points, correct_option, marking_guide FROM school_questions WHERE assessment_id = :a ORDER BY sort_order", a=EXAM)
check("an examination gets two objective questions and two theory questions with their marks and guide",
      [k[0] for k in kinds] == ["objective", "objective", "theory", "theory"] and kinds[2][1] == 5 and kinds[2][2] == -1 and kinds[2][3] == GUIDE,
      str(kinds))
r = maths.post(f"{detail}/questions/new", {"question_type": "theory", "question_text": "Too big", "points": 500}, page=detail)
check("a theory question above the marks limit is refused", "between 1 and 100" in maths.said()
      and one("SELECT COUNT(*) FROM school_questions WHERE assessment_id = :a", a=EXAM) == 4)
page = maths.text(detail)
check("the paper's page shows the theory tag, the split and a way to mark it",
      "Theory</span>" in page and "objective 4 · theory 10" in page and f"/admin/school/marking/{EXAM}" in page)
maths.post("/admin/school/practice-tests/new", {"title": "Maths practice", "instructions": "", "class_id": J1, "subject_id": MATHS,
                                               "duration_minutes": "10", "session_id": SESSION, "term": ""},
           page="/admin/school/practice-tests/new")
PRACTICE = one("SELECT id FROM school_assessments WHERE title = 'Maths practice'")
maths.post(f"/admin/school/assessments/{PRACTICE}/questions/new", {"question_type": "theory", "question_text": "Explain.", "points": 3},
           page=f"/admin/school/assessments/{PRACTICE}")
check("a practice test refuses a theory question", "objective questions only" in maths.said()
      and not one("SELECT COUNT(*) FROM school_questions WHERE assessment_id = :a", a=PRACTICE))
maths.post(f"{detail}/toggle", {}, page=detail)

# ================================================================ B. taking it
TAKE = f"/student/assessments/{EXAM}"


def open_q(person, q):
    body = person.text(f"{TAKE}?q={q}")
    found = re.search(r'name="attempt_id" value="(\d+)"', body)
    return body, (found.group(1) if found else None), int(re.search(r'name="question_id" value="(\d+)"', body).group(1)) if found else None


ada = student("TH-1")
ada.post(f"{TAKE}/start", {}, page=TAKE)
body, attempt, qid = open_q(ada, 1)
ada.post(f"{TAKE}/answer", {"attempt_id": attempt, "question_id": qid, "option_index": 0, "next_q": 2}, token=csrf_from(body))
body, attempt, qid = open_q(ada, 2)
r = ada.post(f"{TAKE}/answer", {"attempt_id": attempt, "question_id": qid, "next_q": 3}, token=csrf_from(body))
check("moving on with no option chosen saves nothing and goes to the next question (it used to fail)",
      r.status_code == 302 and "q=3" in r.headers.get("Location", ""), str(r.status_code))
body, attempt, theory1 = open_q(ada, 3)
check("a theory question shows a box to write in, and never its marking guide",
      'name="answer_text"' in body and "secret marking guide" not in body and 'type="radio"' not in body)
r = ada.post(f"{TAKE}/answer", {"attempt_id": attempt, "question_id": theory1, "answer_text": "Step one, then step two.", "autosave": "1"},
             token=csrf_from(body))
check("a written answer is saved as it is typed (autosave answers JSON)",
      r.status_code == 200 and r.get_json().get("ok") and one("SELECT answer_text FROM school_assessment_answers WHERE attempt_id = :t AND question_id = :q",
                                                               t=int(attempt), q=theory1) == "Step one, then step two.")
r = ada.post(f"{TAKE}/answer", {"attempt_id": attempt, "question_id": theory1, "answer_text": "x" * 5001, "autosave": "1"}, token=csrf_from(body))
check("an answer over the limit is refused and the saved one is kept", r.status_code == 413
      and one("SELECT length(answer_text) FROM school_assessment_answers WHERE attempt_id = :t AND question_id = :q", t=int(attempt), q=theory1) == 24)
body, attempt, theory2 = open_q(ada, 4)
ada.post(f"{TAKE}/answer", {"attempt_id": attempt, "question_id": theory2, "answer_text": "", "submit_assessment": "1", "next_q": 4},
         token=csrf_from(body))
ADA_ATTEMPT = int(attempt)
check("on submitting, the paper waits for marking and its result has no score yet",
      one("SELECT theory_status FROM school_assessment_attempts WHERE id = :i", i=ADA_ATTEMPT) == "pending"
      and sql("SELECT score, max_score, status FROM school_student_results WHERE assessment_id = :a AND student_id = :s",
              a=EXAM, s=STU["Ada"]) == [(None, 14.0, "entered")])
r = ada.post(f"{TAKE}/answer", {"attempt_id": attempt, "question_id": theory1, "answer_text": "too late", "autosave": "1"}, token=csrf_from(body))
check("after submitting, an autosave is refused", r.status_code == 409)
check("the student is told their written answers will be marked by their teacher",
      "marked by your subject teacher" in ada.text(f"{TAKE}/result"))

bayo = student("TH-2")
bayo.post(f"{TAKE}/start", {}, page=TAKE)
for q in range(1, 5):
    body, attempt, qid = open_q(bayo, q)
    data = {"attempt_id": attempt, "question_id": qid, "next_q": q + 1}
    if q <= 2:
        data["option_index"] = 0
    if q == 4:
        data["submit_assessment"] = "1"
    bayo.post(f"{TAKE}/answer", data, token=csrf_from(body))
check("a paper whose written answers were left blank is scored at once (4 of 4 objective, 0 theory: 4 of 14)",
      sql("SELECT score, max_score FROM school_student_results WHERE assessment_id = :a AND student_id = :s", a=EXAM, s=STU["Bayo"]) == [(4.0, 14.0)]
      and one("SELECT theory_status FROM school_assessment_attempts WHERE student_id = :s", s=STU["Bayo"]) == "marked")

chidi = student("TH-3")
chidi.post(f"{TAKE}/start", {}, page=TAKE)
EVIL = '<script>alert("x")</script> & my answer'
for q in range(1, 5):
    body, attempt, qid = open_q(chidi, q)
    data = {"attempt_id": attempt, "question_id": qid, "next_q": q + 1}
    if q <= 2:
        data["option_index"] = 1
    else:
        data["answer_text"] = EVIL
    if q == 4:
        data["submit_assessment"] = "1"
    chidi.post(f"{TAKE}/answer", data, token=csrf_from(body))

# ================================================================ C. holding the result
ADA_RESULT = one("SELECT id FROM school_student_results WHERE assessment_id = :a AND student_id = :s", a=EXAM, s=STU["Ada"])
op.post(f"/admin/school/results/{ADA_RESULT}/workflow", {"action": "verify"}, page="/admin/school/results")
check("a result waiting for marking cannot be verified", "waiting for its theory answers" in op.said()
      and one("SELECT status FROM school_student_results WHERE id = :i", i=ADA_RESULT) == "entered")
r = op.get(f"/admin/school/results/{ADA_RESULT}/edit")
check("…nor typed over by hand: it is sent to marking instead", r.status_code == 302 and f"/admin/school/marking/{EXAM}" in r.headers.get("Location", ""))

# ================================================================ D. who marks
listing = maths.text("/admin/school/marking")
check("To mark shows the Mathematics teacher the paper, with two scripts waiting",
      "Maths exam" in listing and re.search(r'ac-tag--attn">2<', listing) is not None)
home = maths.text("/admin/school")
check("the menu has To mark (not an Academics tab)", "To mark" in home and "admin/school/marking" in home
      and "To mark" not in maths.text("/admin/school/examinations").split("ac-tabs")[1].split("</nav>")[0])
check("the English teacher does not see the Mathematics paper", "Maths exam" not in english.text("/admin/school/marking"))
r = english.get(f"/admin/school/marking/{EXAM}")
check("…and is refused it by its address", r.status_code in (302, 403) and "Explain idea" not in r.get_data(as_text=True))
r = english.post(f"/admin/school/marking/{EXAM}/students/{STU['Ada']}", {f"mark_{theory1}": "5"}, page="/admin/password")
check("…and cannot mark it (refused by its own check, past the form token)", r.status_code in (302, 403) and one("SELECT awarded FROM school_assessment_answers WHERE attempt_id = :t AND question_id = :q",
                                 t=ADA_ATTEMPT, q=theory1) is None)
english.post(f"/admin/school/marking/{EXAM}/split", {"theory_marks": "12"}, page="/admin/password")
check("…or change its split", one("SELECT theory_marks FROM school_assessments WHERE id = :a", a=EXAM) is None)
check("each of the three refusals is recorded", one("SELECT COUNT(*) FROM audit_logs WHERE action = 'authorization_denied' AND target_type = 'marking'") == 3,
      str(one("SELECT COUNT(*) FROM audit_logs WHERE action = 'authorization_denied' AND target_type = 'marking'")))

# ================================================================ E. marking
paper = maths.text(f"/admin/school/marking/{EXAM}?student={STU['Chidi']}")
check("a written answer is shown as plain text: a script typed into it is never run",
      "&lt;script&gt;alert(" in paper and '<script>alert("x")' not in paper and "secret marking guide" in paper)
PAPER = f"/admin/school/marking/{EXAM}"
maths.post(f"{PAPER}/students/{STU['Ada']}", {f"mark_{theory1}": "6"}, page=PAPER)
check("a mark above the question's marks is refused", "give a mark from 0 to 5" in maths.said()
      and one("SELECT awarded FROM school_assessment_answers WHERE attempt_id = :t AND question_id = :q", t=ADA_ATTEMPT, q=theory1) is None)
maths.post(f"{PAPER}/students/{STU['Ada']}", {f"mark_{theory1}": "nan"}, page=PAPER)
check("…and so is a mark that is not a number", "give a mark from 0 to 5" in maths.said())
maths.post(f"{PAPER}/students/{STU['Ada']}", {f"mark_{theory1}": "4", f"comment_{theory1}": "Good steps"}, page=PAPER)
check("once every written answer is marked the score is filled in (2 of 4 objective + 4 of 10 theory = 6 of 14)",
      sql("SELECT score, max_score FROM school_student_results WHERE id = :i", i=ADA_RESULT) == [(6.0, 14.0)]
      and one("SELECT theory_status FROM school_assessment_attempts WHERE id = :i", i=ADA_ATTEMPT) == "marked")
chidi_attempt = one("SELECT id FROM school_assessment_attempts WHERE student_id = :s", s=STU["Chidi"])
maths.post(f"{PAPER}/students/{STU['Chidi']}", {f"mark_{theory1}": "3"}, page=PAPER)
check("a script with one answer still unmarked keeps waiting, with no score",
      one("SELECT score FROM school_student_results WHERE assessment_id = :a AND student_id = :s", a=EXAM, s=STU["Chidi"]) is None
      and "1 answer still to mark" in maths.said())

# ================================================================ F. the split
maths.post(f"{PAPER}/split", {"theory_marks": "14"}, page=PAPER)
check("a split that leaves the objective part nothing is refused", "leave some marks" in maths.said())
maths.post(f"{PAPER}/split", {"theory_marks": "20"}, page=PAPER)
check("…and so is one above the paper's total", "between 0 and 14" in maths.said()
      and one("SELECT theory_marks FROM school_assessments WHERE id = :a", a=EXAM) is None)
maths.post(f"{PAPER}/split", {"theory_marks": "7"}, page=PAPER)
check("the teacher moves marks to theory (objective 7, theory 7) and every score is recalculated",
      sql("SELECT objective_marks, theory_marks FROM school_assessments WHERE id = :a", a=EXAM) == [(7.0, 7.0)]
      and one("SELECT score FROM school_student_results WHERE id = :i", i=ADA_RESULT) == 6.3
      and one("SELECT score FROM school_student_results WHERE assessment_id = :a AND student_id = :s", a=EXAM, s=STU["Bayo"]) == 7.0)

# ================================================================ G. locks
sql("UPDATE school_student_results SET status = 'verified' WHERE id = :i", i=ADA_RESULT)
maths.post(f"{PAPER}/students/{STU['Ada']}", {f"mark_{theory1}": "5"}, page=PAPER)
check("a mark changed after the result was checked sends it back to be checked again",
      sql("SELECT status, score FROM school_student_results WHERE id = :i", i=ADA_RESULT) == [("entered", 7.0)]
      and one("SELECT COUNT(*) FROM result_workflow_events WHERE result_id = :i AND to_status = 'entered'", i=ADA_RESULT) == 1
      and "goes back to be checked again" in maths.said())
sql("UPDATE school_student_results SET status = 'released' WHERE id = :i", i=ADA_RESULT)
maths.post(f"{PAPER}/students/{STU['Ada']}", {f"mark_{theory1}": "1"}, page=PAPER)
check("a released result's marks are locked", "locked" in maths.said()
      and one("SELECT awarded FROM school_assessment_answers WHERE attempt_id = :t AND question_id = :q", t=ADA_ATTEMPT, q=theory1) == 5.0)
maths.post(f"{PAPER}/split", {"theory_marks": "10"}, page=PAPER)
check("…and so is the paper's split", "can no longer change" in maths.said()
      and one("SELECT theory_marks FROM school_assessments WHERE id = :a", a=EXAM) == 7.0)
check("every mark and split change is in the activity log",
      one("SELECT COUNT(*) FROM audit_logs WHERE action = 'school_theory_marked'") >= 3
      and one("SELECT COUNT(*) FROM audit_logs WHERE action = 'school_theory_split_changed'") == 1)

check("no page answered with a server error", not PROBLEMS, "; ".join(PROBLEMS[:5]))
check("nothing raised behind the scenes", not errors.seen, "; ".join(repr(e) for e in errors.seen[:3]))

failed = [name for name, ok, _ in results if not ok]
print()
print(f"{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", "; ".join(failed))
    raise SystemExit(1)
