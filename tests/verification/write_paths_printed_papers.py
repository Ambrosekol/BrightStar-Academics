"""Papers sat on paper (printed), proven end to end on PostgreSQL. In plain words:

A. Setting it: an examination is made "on paper" with objective and theory questions. It can never be opened to
   students online, and students never see it in the portal or can start it.
B. Printing: its subject teacher prints the question paper (never cached, and logged) and one answer booklet per
   student with their name and admission number; another subject's teacher is refused both.
C. Entering marks: To mark lists it with every student still to enter; a sheet with one wrong mark saves nothing; a
   right sheet scores the student fully marked, records the absent one as absent and leaves the half-entered one
   waiting. An absent result can move on, a waiting one cannot. Another subject's teacher is refused.
D. The split re-scores the sheet; a released result's row is locked; once marks exist it can no longer be switched
   back to the computer; and a hand edit of its result is sent to the marks sheet. Every save is logged.
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
TMP = tempfile.mkdtemp(prefix="brightstars_printed_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('printed')
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


# ================================================================ A. setting it
maths.post("/admin/school/examinations/new", {"title": "Maths paper exam", "instructions": "Answer all questions.", "class_id": J1,
                                              "subject_id": MATHS, "duration_minutes": "90", "session_id": SESSION, "term": TERM,
                                              "delivery": "printed"}, page="/admin/school/examinations/new")
EXAM = one("SELECT id FROM school_assessments WHERE title = 'Maths paper exam'")
check("an examination is made to be sat on paper", one("SELECT delivery FROM school_assessments WHERE id = :a", a=EXAM) == "printed")
detail = f"/admin/school/assessments/{EXAM}"
for n in range(2):
    maths.post(f"{detail}/questions/new", {"question_type": "objective", "question_text": f"Objective {n + 1}?", "instruction": "",
                                          "option_a": "Alpha", "option_b": "Beta", "option_c": "Gamma", "option_d": "Delta",
                                          "correct_option": 0, "points": 2}, page=detail)
for n in range(2):
    maths.post(f"{detail}/questions/new", {"question_type": "theory", "question_text": f"Explain idea {n + 1}.", "instruction": "",
                                          "marking_guide": "secret guide", "points": 5}, page=detail)
page = maths.text(detail)
check("its page offers the question paper, the answer booklets and the marks, not 'Open to students'",
      "Sat on paper" in page and "Print question paper" in page and "Print answer booklets" in page and "Open to students</button>" not in page)
maths.post(f"{detail}/toggle", {}, page=detail)
check("it can never be opened to students online", "never opened to students online" in maths.said()
      and one("SELECT active FROM school_assessments WHERE id = :a", a=EXAM) == 0)
ada = student("TH-1")
check("students do not see it in the portal", "Maths paper exam" not in ada.text("/student/assessments/examinations"))
token = csrf_from(ada.text("/student/password")) or csrf_from(ada.text("/student/dashboard"))
r = ada.post(f"/student/assessments/{EXAM}/start", {}, token=token)
check("…and cannot start it by its address (refused past the form token)", bool(token) and r.status_code == 404
      and not one("SELECT COUNT(*) FROM school_assessment_attempts"), f"token={bool(token)} status={r.status_code}")

# ================================================================ B. printing
r = maths.get(f"/admin/school/marking/{EXAM}/print/paper")
body = r.get_data(as_text=True)
check("the subject teacher prints the question paper: both sections, the options, the time and the marks",
      r.status_code == 200 and "Section A · Objective" in body and "Section B · Theory" in body and "Gamma" in body
      and "Explain idea 2." in body and "90 minutes" in body and "secret guide" not in body)
check("…which is never cached", "no-store" in r.headers.get("Cache-Control", ""))
r = maths.get(f"/admin/school/marking/{EXAM}/print/booklets")
body = r.get_data(as_text=True)
check("…and an answer booklet for each student, with their name and admission number",
      r.status_code == 200 and body.count('class="booklet"') == 3 and "Ada Test" in body and "TH-2" in body and "Chidi Test" in body
      and body.count('class="bub"') == 3 * 2 * 4 and "no-store" in r.headers.get("Cache-Control", ""))
r = maths.get(f"/admin/school/marking/{EXAM}/print/booklets?student={STU['Bayo']}")
check("…or for one student", r.get_data(as_text=True).count('class="booklet"') == 1)
check("each print is in the activity log",
      one("SELECT COUNT(*) FROM audit_logs WHERE action = 'school_paper_printed' AND success = 1") == 3)
for what in ("paper", "booklets"):
    r = english.get(f"/admin/school/marking/{EXAM}/print/{what}")
    check(f"another subject's teacher is refused the {what}", r.status_code in (302, 403) and "Explain idea" not in r.get_data(as_text=True)
          and "Ada Test" not in r.get_data(as_text=True))

# ================================================================ C. entering marks
listing = maths.text("/admin/school/marking")
check("To mark lists the paper, on paper, with three students to enter",
      "Maths paper exam" in listing and "on paper" in listing and re.search(r'ac-tag--attn">3<', listing) is not None)
sheet = f"/admin/school/marking/{EXAM}"
T1, T2 = [i for (i,) in sql("SELECT id FROM school_questions WHERE assessment_id = :a AND question_type = 'theory' ORDER BY sort_order", a=EXAM)]
good = {"student": [STU["Ada"], STU["Bayo"], STU["Chidi"]],
        f"obj_{STU['Ada']}": "3", f"m_{STU['Ada']}_{T1}": "4", f"m_{STU['Ada']}_{T2}": "5",
        f"absent_{STU['Bayo']}": "1",
        f"obj_{STU['Chidi']}": "2"}
bad = dict(good)
bad[f"m_{STU['Ada']}_{T2}"] = "6"
r = maths.post(f"{sheet}/printed", bad, page="/admin/password")
check("a sheet with one mark above its question's marks saves nothing", r.status_code == 400
      and "Theory question 2: give a mark from 0 to 5" in r.get_data(as_text=True) and not one("SELECT COUNT(*) FROM school_printed_scripts"))
maths.said()
maths.post(f"{sheet}/printed", good, page="/admin/password")


def result(name):
    return sql("SELECT score, max_score, status, component_name FROM school_student_results WHERE assessment_id = :a AND student_id = :s",
               a=EXAM, s=STU[name])


check("a fully entered script is scored (3 of 4 objective + 9 of 10 theory = 12 of 14)", result("Ada") == [(12.0, 14.0, "entered", None)],
      str(result("Ada")))
check("an absent student is recorded as absent, with no score", result("Bayo") == [(None, 14.0, "entered", "Absent")], str(result("Bayo")))
check("a half-entered script waits, with no score", result("Chidi") == [(None, 14.0, "entered", None)], str(result("Chidi")))
BAYO_R = one("SELECT id FROM school_student_results WHERE assessment_id = :a AND student_id = :s", a=EXAM, s=STU["Bayo"])
CHIDI_R = one("SELECT id FROM school_student_results WHERE assessment_id = :a AND student_id = :s", a=EXAM, s=STU["Chidi"])
op.post(f"/admin/school/results/{BAYO_R}/workflow", {"action": "verify"}, page="/admin/school/results")
check("the absent result can move on", one("SELECT status FROM school_student_results WHERE id = :i", i=BAYO_R) == "verified")
op.post(f"/admin/school/results/{CHIDI_R}/workflow", {"action": "verify"}, page="/admin/school/results")
check("…the waiting one cannot", "cannot move on" in op.said() and one("SELECT status FROM school_student_results WHERE id = :i", i=CHIDI_R) == "entered")
r = english.post(f"{sheet}/printed", {"student": [STU["Chidi"]], f"m_{STU['Chidi']}_{T1}": "5"}, page="/admin/password")
check("another subject's teacher cannot enter marks", r.status_code in (302, 403)
      and one("SELECT COUNT(*) FROM school_printed_marks m JOIN school_printed_scripts s ON s.id = m.script_id WHERE s.student_id = :s",
              s=STU["Chidi"]) == 0)
check("the marks are in the activity log", one("SELECT COUNT(*) FROM audit_logs WHERE action = 'school_printed_marks_entered'") == 1)

# ================================================================ D. the split, locks
maths.post(f"{sheet}/split", {"theory_marks": "7"}, page="/admin/password")
check("moving marks to theory re-scores the sheet (3/4 of 7 + 9/10 of 7 = 11.55)",
      one("SELECT score FROM school_student_results WHERE assessment_id = :a AND student_id = :s", a=EXAM, s=STU["Ada"]) == 11.55)
ADA_R = one("SELECT id FROM school_student_results WHERE assessment_id = :a AND student_id = :s", a=EXAM, s=STU["Ada"])
sql("UPDATE school_student_results SET status = 'released' WHERE id = :i", i=ADA_R)
maths.post(f"{sheet}/printed", {"student": [STU["Ada"]], f"obj_{STU['Ada']}": "0", f"m_{STU['Ada']}_{T1}": "0", f"m_{STU['Ada']}_{T2}": "0"},
           page="/admin/password")
check("a released result's row is locked", one("SELECT score FROM school_student_results WHERE id = :i", i=ADA_R) == 11.55
      and one("SELECT objective_score FROM school_printed_scripts WHERE student_id = :s", s=STU["Ada"]) == 3.0)
maths.post(f"{detail}/edit", {"title": "Maths paper exam", "instructions": "", "class_id": J1, "subject_id": MATHS, "duration_minutes": "90",
                              "session_id": SESSION, "term": TERM, "delivery": "cbt"}, page=detail)
check("once marks exist it can no longer be switched to the computer", "can no longer change" in maths.said()
      and one("SELECT delivery FROM school_assessments WHERE id = :a", a=EXAM) == "printed")
r = op.get(f"/admin/school/results/{CHIDI_R}/edit")
check("a hand edit of its result is sent to the marks sheet", r.status_code == 302 and f"/admin/school/marking/{EXAM}" in r.headers.get("Location", ""))
check("the sheet shows each student's state", all(w in maths.text(sheet) for w in ("Absent", "To enter", "Released")))

check("no page answered with a server error", not PROBLEMS, "; ".join(PROBLEMS[:5]))
check("nothing raised behind the scenes", not errors.seen, "; ".join(repr(e) for e in errors.seen[:3]))

failed = [name for name, ok, _ in results if not ok]
print()
print(f"{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", "; ".join(failed))
    raise SystemExit(1)
