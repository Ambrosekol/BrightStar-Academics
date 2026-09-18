"""A student who starts a CBT assignment but doesn't finish it must be able
to click "Continue assignment" and pick up where they left off.

student_assignment_detail's POST action=start handler only had branches for
"never started" and "already submitted" attempts. A student with an 'active'
(in-progress) attempt fell through both checks and the page just re-rendered
itself with no redirect - "Continue" silently did nothing. This drives a real
start, answers one question, abandons the session (simulating closing the
browser), then clicks "Continue" again and checks it resumes at the next
unanswered question rather than reloading the detail page or restarting.
"""
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DB = os.path.join(HERE, "assignment_resume.db")
shutil.copy(os.path.join(ROOT, "cbt.db"), DB)
sys.path.insert(0, ROOT)
from werkzeug.security import generate_password_hash  # noqa: E402

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
now = datetime.now(timezone.utc).isoformat()

tid = con.execute("SELECT id FROM admin_types WHERE is_system=1 ORDER BY id LIMIT 1").fetchone()["id"]
admin_id = con.execute(
    "INSERT INTO admins(username,display_name,password_hash,admin_type_id,active,created_at,"
    "password_must_change) VALUES(?,?,?,?,1,?,0)",
    ("zz_resume_admin", "Resume Probe", generate_password_hash("ResumePass!12"), tid, now)).lastrowid

school_id = con.execute("SELECT id FROM schools WHERE active=1 LIMIT 1").fetchone()["id"]
class_id = con.execute("SELECT id FROM school_classes WHERE active=1 ORDER BY id LIMIT 1").fetchone()["id"]
subject_id = con.execute("SELECT id FROM school_subjects WHERE active=1 ORDER BY id LIMIT 1").fetchone()["id"]

student_id = con.execute(
    "INSERT INTO students(admission_no,first_name,last_name,created_at,active,school_id,"
    "student_number,student_number_source,login_username,login_password_hash,account_active,"
    "password_must_change) VALUES(?,?,?,?,1,?,?,?,?,?,1,0)",
    ("ZZ-RESUME-0001", "Resume", "TestStudent", now, school_id, "ZZ-RESUME-0001", "existing",
     "ZZ-RESUME-0001", generate_password_hash("StudentPass!1"))).lastrowid
con.execute("INSERT INTO student_enrolments(student_id,class_id,session_id,active,enrolled_at) "
            "SELECT ?,?,id,1,? FROM academic_sessions WHERE is_current=1 AND active=1 LIMIT 1",
            (student_id, class_id, now))

assignment_id = con.execute(
    "INSERT INTO school_assignments(title,instructions,class_id,subject_id,created_by,created_at,"
    "active,assignment_type,timing_mode) VALUES(?,?,?,?,?,?,1,'quiz','untimed')",
    ("Resume Test Quiz", "", class_id, subject_id, admin_id, now)).lastrowid
con.execute("INSERT INTO assignment_students(assignment_id,student_id,status) VALUES(?,?,'undone')",
            (assignment_id, student_id))
for i in range(1, 4):
    con.execute(
        "INSERT INTO assignment_questions(assignment_id,question_text,option_a,option_b,option_c,"
        "option_d,correct_option,points,sort_order) VALUES(?,?,?,?,?,?,?,1,?)",
        (assignment_id, f"Question {i}?", "A", "B", "C", "D", 0, i))
con.commit()
con.close()

os.environ["CRAINBOW_DB"] = DB
os.chdir(ROOT)
import app as A  # noqa: E402

A.app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB}"
A.app.config["TESTING"] = True
with A.app.app_context():
    A.init_db()

CSRF = re.compile(r'name="_csrf_token"[^>]*value="([^"]+)"')
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


def csrf_from(html):
    m = CSRF.search(html)
    return m.group(1) if m else ""


def q(sql, *params):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        return c.execute(sql, params).fetchall()
    finally:
        c.close()


with A.app.test_client() as c:
    c.post("/login", data={"username": "ZZ-RESUME-0001", "password": "StudentPass!1"})

    detail_page = c.get(f"/student/assignments/{assignment_id}").get_data(as_text=True)
    token = csrf_from(detail_page)

    r1 = c.post(f"/student/assignments/{assignment_id}", data={"action": "start", "_csrf_token": token})
    check("starting redirects into the quiz", r1.status_code in (302, 303) and "take" in r1.headers.get("Location", ""),
          f"{r1.status_code} -> {r1.headers.get('Location')}")

    take_page = c.get(f"/student/assignments/{assignment_id}/take").get_data(as_text=True)
    check("first question shown", "Question 1?" in take_page)
    take_token = csrf_from(take_page)
    qid1 = q("SELECT question_id FROM school_assignment_attempt_questions WHERE question_order=1 "
             "AND attempt_id=(SELECT id FROM school_assignment_attempts WHERE assignment_id=? "
             "AND student_id=?)", assignment_id, student_id)[0]["question_id"]
    c.post(f"/student/assignments/{assignment_id}/take",
           data={"question_id": qid1, "option_index": "1", "_csrf_token": take_token})

    # Simulate the student closing the browser: drop the Flask session
    # entirely (assignment_attempt_id / question_index are gone), but the DB
    # attempt row is still 'active'. This is exactly the state that made
    # "Continue assignment" a no-op before the fix.
    with c.session_transaction() as sess:
        sess.pop("assignment_attempt_id", None)
        sess.pop("assignment_question_index", None)
        sess.pop("assignment_question_started_at", None)

    detail_page_2 = c.get(f"/student/assignments/{assignment_id}").get_data(as_text=True)
    check("shows Continue, not Start, once in progress", "ontinue" in detail_page_2)
    token2 = csrf_from(detail_page_2)

    r2 = c.post(f"/student/assignments/{assignment_id}", data={"action": "start", "_csrf_token": token2})
    check("clicking Continue redirects into the quiz (not a no-op reload)",
          r2.status_code in (302, 303) and "take" in r2.headers.get("Location", ""),
          f"{r2.status_code} -> {r2.headers.get('Location')}")

    resumed_page = c.get(f"/student/assignments/{assignment_id}/take").get_data(as_text=True)
    check("resumes at question 2, not question 1 (real progress restored)", "Question 2?" in resumed_page)
    check("does not create a second attempt row", len(q(
        "SELECT id FROM school_assignment_attempts WHERE assignment_id=? AND student_id=?",
        assignment_id, student_id)) == 1)

with A.app.app_context():
    A.db.session.remove()
    A.db.engine.dispose()
try:
    os.remove(DB)
except OSError:
    pass

print()
failed = [x for x in results if not x[1]]
print(f"{len(results)-len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
