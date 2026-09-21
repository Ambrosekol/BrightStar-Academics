"""Term-based grading: every test/exam/assignment/project belongs to a term
within a session, and a student's term result for a subject is
Exam(60) + CA(40) = 100, with tests/assignments/projects sharing CA_MAX_SCORE
under configurable weights (default 20/10/10).

Drives real assessment/assignment/project creation through the actual admin
forms (proving the new Term/Session fields are wired up end to end), grades
them, and checks the aggregation math in _term_subject_report() is exactly
right: each category's raw score is summed then scaled to its own cap, never
just added raw, and a practice test never contributes at all. Also covers the
CA-weighting settings page (Super-Admin-only, must sum to exactly 40) and
that the student profile's new "Term Results" table shows the right numbers.
"""
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DB = os.path.join(HERE, "term_grading.db")
shutil.copy(os.path.join(ROOT, "cbt.db"), DB)
sys.path.insert(0, ROOT)
from werkzeug.security import generate_password_hash  # noqa: E402

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
now = datetime.now(timezone.utc).isoformat()

super_tid = con.execute("SELECT id FROM admin_types WHERE is_system=1 ORDER BY id LIMIT 1").fetchone()["id"]
staff_tid = con.execute("SELECT id FROM admin_types WHERE is_system=0 AND active=1 ORDER BY id LIMIT 1").fetchone()["id"]

super_id = con.execute(
    "INSERT INTO admins(username,display_name,password_hash,admin_type_id,active,created_at,"
    "password_must_change) VALUES(?,?,?,?,1,?,0)",
    ("zz_term_super", "Term Super", generate_password_hash("TermSuper!123"), super_tid, now)).lastrowid
con.execute("INSERT OR IGNORE INTO admin_role_assignments(admin_id,admin_type_id,assigned_at) VALUES(?,?,?)",
            (super_id, super_tid, now))

staff_id = con.execute(
    "INSERT INTO admins(username,display_name,password_hash,admin_type_id,active,created_at,"
    "password_must_change) VALUES(?,?,?,?,1,?,0)",
    ("zz_term_staff", "Term Staff", generate_password_hash("TermStaff!123"), staff_tid, now)).lastrowid
con.execute("INSERT OR IGNORE INTO admin_role_assignments(admin_id,admin_type_id,assigned_at) VALUES(?,?,?)",
            (staff_id, staff_tid, now))
for p in con.execute("SELECT id FROM permissions"):
    con.execute("INSERT OR IGNORE INTO admin_permissions(admin_id,permission_id,granted_at) VALUES(?,?,?)",
                (staff_id, p["id"], now))

session_id = con.execute("SELECT id FROM academic_sessions WHERE is_current=1 AND active=1 LIMIT 1").fetchone()["id"]
school_id = con.execute("SELECT id FROM schools WHERE active=1 LIMIT 1").fetchone()["id"]
class_id = con.execute("SELECT id FROM school_classes WHERE active=1 ORDER BY id LIMIT 1").fetchone()["id"]
subject_id = con.execute(
    "SELECT s.id FROM school_subjects s JOIN class_subjects cs ON cs.subject_id=s.id "
    "WHERE cs.class_id=? AND s.active=1 LIMIT 1", (class_id,)).fetchone()["id"]

student_id = con.execute(
    "INSERT INTO students(admission_no,first_name,last_name,created_at,active,school_id,"
    "student_number,student_number_source,login_username,login_password_hash,account_active,"
    "password_must_change) VALUES(?,?,?,?,1,?,?,?,?,?,1,0)",
    ("ZZ-TERM-0001", "Term", "TestStudent", now, school_id, "ZZ-TERM-0001", "existing",
     "ZZ-TERM-0001", generate_password_hash("StudentTerm!1"))).lastrowid
con.execute("INSERT INTO student_enrolments(student_id,class_id,session_id,active,enrolled_at) VALUES(?,?,?,1,?)",
            (student_id, class_id, session_id, now))
con.commit()
con.close()

os.environ["CRAINBOW_DB"] = DB
os.chdir(ROOT)
import app as A  # noqa: E402
import blueprints.school.helpers as SCH  # noqa: E402

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


with A.app.test_client() as staff:
    staff.post("/login", data={"username": "zz_term_staff", "password": "TermStaff!123"})
    staff.get("/admin/workspace/school")

    # --- Create the exam and test assessments through the real form -------
    exam_page = staff.get("/admin/school/examinations/new").get_data(as_text=True)
    check("examination form offers a Term field", 'name="term"' in exam_page)
    token = csrf_from(exam_page)
    r = staff.post("/admin/school/examinations/new", data={
        "_csrf_token": token, "title": "Term Grading Exam", "class_id": str(class_id),
        "subject_id": str(subject_id), "session_id": str(session_id), "term": "First Term",
        "duration_minutes": "30", "instructions": "",
    })
    check("exam creation with a term succeeds", r.status_code in (302, 303), str(r.status_code))

    test_page = staff.get("/admin/school/tests/new").get_data(as_text=True)
    token = csrf_from(test_page)
    r = staff.post("/admin/school/tests/new", data={
        "_csrf_token": token, "title": "Term Grading Test", "class_id": str(class_id),
        "subject_id": str(subject_id), "session_id": str(session_id), "term": "First Term",
        "duration_minutes": "20", "instructions": "",
    })
    check("test creation with a term succeeds", r.status_code in (302, 303), str(r.status_code))

    # A test/exam without a term must be refused.
    r_missing = staff.post("/admin/school/tests/new", data={
        "_csrf_token": csrf_from(staff.get("/admin/school/tests/new").get_data(as_text=True)),
        "title": "No Term Test", "class_id": str(class_id), "subject_id": str(subject_id),
        "session_id": str(session_id), "term": "", "duration_minutes": "20", "instructions": "",
    })
    check("a test without a term is refused", "Select a valid term" in r_missing.get_data(as_text=True))

    practice_page = staff.get("/admin/school/practice-tests/new").get_data(as_text=True)
    token = csrf_from(practice_page)
    r = staff.post("/admin/school/practice-tests/new", data={
        "_csrf_token": token, "title": "Term Grading Practice", "class_id": str(class_id),
        "subject_id": str(subject_id), "session_id": str(session_id), "term": "",
        "duration_minutes": "10", "instructions": "",
    })
    check("a practice test without a term still succeeds (term optional)", r.status_code in (302, 303),
          str(r.status_code))

    # --- Create the assignment and project through the real forms ---------
    assignment_page = staff.get("/admin/school/assignments/new").get_data(as_text=True)
    check("assignment form offers Session and Term fields",
          'name="session_id"' in assignment_page and 'name="term"' in assignment_page)
    token = csrf_from(assignment_page)
    r = staff.post("/admin/school/assignments/new", data={
        "_csrf_token": token, "title": "Term Grading Assignment", "instructions": "",
        "due_date": "2026-01-01", "assignment_type": "written", "timing_mode": "untimed",
        "class_id": str(class_id), "subject_id": str(subject_id), "session_id": str(session_id),
        "term": "First Term", "student_ids": [str(student_id)], "max_score": "10",
    })
    check("assignment creation with session+term succeeds", r.status_code in (302, 303), str(r.status_code))
    assignment_id = q("SELECT id FROM school_assignments WHERE title='Term Grading Assignment'")[0]["id"]

    project_page = staff.get("/admin/school/projects/new").get_data(as_text=True)
    check("project form offers Session and Term fields",
          'name="session_id"' in project_page and 'name="term"' in project_page)
    token = csrf_from(project_page)
    r = staff.post("/admin/school/projects/new", data={
        "_csrf_token": token, "title": "Term Grading Project", "instructions": "",
        "date_given": "2026-01-01", "due_date": "2026-01-15", "class_id": str(class_id),
        "subject_id": str(subject_id), "session_id": str(session_id), "term": "First Term",
        "student_ids": [str(student_id)], "max_score": "10",
    })
    check("project creation with session+term succeeds", r.status_code in (302, 303), str(r.status_code))
    project_id = q("SELECT id FROM school_projects WHERE title='Term Grading Project'")[0]["id"]

    # --- Grade the assignment (9/10) and project (7/10) --------------------
    assignment_detail = staff.get(f"/admin/school/assignments/{assignment_id}").get_data(as_text=True)
    token = csrf_from(assignment_detail)
    staff.post(f"/admin/school/assignments/{assignment_id}/students/{student_id}", data={
        "_csrf_token": token, "status": "done", "score": "9",
    })
    project_detail = staff.get(f"/admin/school/projects/{project_id}").get_data(as_text=True)
    token = csrf_from(project_detail)
    staff.post(f"/admin/school/projects/{project_id}/students/{student_id}", data={
        "_csrf_token": token, "status": "done", "score": "7",
    })

# --- Seed the exam (45/50) and test (8/10) results as CBT grading would ---
exam_id = q("SELECT id FROM school_assessments WHERE title='Term Grading Exam'")[0]["id"]
test_id = q("SELECT id FROM school_assessments WHERE title='Term Grading Test'")[0]["id"]
practice_id = q("SELECT id FROM school_assessments WHERE title='Term Grading Practice'")[0]["id"]
con = sqlite3.connect(DB)
con.execute("INSERT INTO school_student_results(student_id,assessment_id,subject_id,score,max_score,term,"
            "session_id,status,created_at,source_type) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (student_id, exam_id, subject_id, 45, 50, "First Term", session_id, "released", now, "cbt"))
con.execute("INSERT INTO school_student_results(student_id,assessment_id,subject_id,score,max_score,term,"
            "session_id,status,created_at,source_type) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (student_id, test_id, subject_id, 8, 10, "First Term", session_id, "released", now, "cbt"))
# A practice-test result must never contribute, even if one somehow exists.
con.execute("INSERT INTO school_student_results(student_id,assessment_id,subject_id,score,max_score,term,"
            "session_id,status,created_at,source_type) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (student_id, practice_id, subject_id, 10, 10, "First Term", session_id, "released", now, "cbt"))
con.commit()
con.close()

with A.app.app_context():
    report = SCH._term_subject_report(student_id, session_id, "First Term", subject_id)
    # Exam: 45/50 -> scaled to 60 = 54
    check("exam scaled correctly (45/50 -> /60)", abs(report["exam_score"] - 54.0) < 0.01, str(report["exam_score"]))
    # Test: 8/10 -> scaled to default weight 20 = 16
    check("test scaled correctly (8/10 -> /20)", abs(report["test_score"] - 16.0) < 0.01, str(report["test_score"]))
    # Assignment: 9/10 -> scaled to default weight 10 = 9
    check("assignment scaled correctly (9/10 -> /10)", abs(report["assignment_score"] - 9.0) < 0.01,
          str(report["assignment_score"]))
    # Project: 7/10 -> scaled to default weight 10 = 7
    check("project scaled correctly (7/10 -> /10)", abs(report["project_score"] - 7.0) < 0.01,
          str(report["project_score"]))
    check("CA = test+assignment+project = 32, under the 40 cap",
          abs(report["ca_score"] - 32.0) < 0.01, str(report["ca_score"]))
    check("total = exam(54) + ca(32) = 86 out of 100",
          abs(report["total_score"] - 86.0) < 0.01 and report["total_max"] == 100, str(report))
    check("practice test never contributes to the report",
          report["exam_score"] < 60 and report["test_score"] < 20)  # would be maxed out if practice leaked in

# --- Student profile page shows the term summary table --------------------
with A.app.test_client() as staff2:
    staff2.post("/login", data={"username": "zz_term_staff", "password": "TermStaff!123"})
    staff2.get("/admin/workspace/school")
    profile = staff2.get(f"/admin/school/students/{student_id}").get_data(as_text=True)
    check("student profile shows the Term Results section", "TERM RESULTS" in profile)
    check("student profile shows the correct CA figure", "32.0 / 40" in profile, profile[:0])
    check("student profile shows the correct Exam figure", "54.0 / 60" in profile)
    check("student profile shows the correct Total figure", "86.0 / 100" in profile)

# --- CA weighting settings page: Super-Admin-only, must sum to 40 ---------
with A.app.test_client() as staff3:
    staff3.post("/login", data={"username": "zz_term_staff", "password": "TermStaff!123"})
    staff3.get("/admin/workspace/school")
    r = staff3.get("/admin/school/sessions")
    check("a non-Super-Admin cannot reach the sessions/CA-weights page", r.status_code == 403, str(r.status_code))

with A.app.test_client() as super_c:
    super_c.post("/login", data={"username": "zz_term_super", "password": "TermSuper!123"})
    super_c.get("/admin/workspace/school")
    page = super_c.get("/admin/school/sessions").get_data(as_text=True)
    check("School Admin sees the CA weighting form", "Continuous assessment weighting" in page)
    token = csrf_from(page)

    bad = super_c.post("/admin/school/sessions", data={
        "_csrf_token": token, "action": "set_ca_weights",
        "ca_weight_test": "20", "ca_weight_assignment": "20", "ca_weight_project": "20",
    })
    with A.app.app_context():
        check("weights that don't sum to 40 are refused", SCH._ca_weights() == A.CA_DEFAULT_WEIGHTS,
              str(SCH._ca_weights()))

    good = super_c.post("/admin/school/sessions", data={
        "_csrf_token": token, "action": "set_ca_weights",
        "ca_weight_test": "15", "ca_weight_assignment": "15", "ca_weight_project": "10",
    })
    check("weights summing to exactly 40 are accepted", good.status_code in (302, 303))
    with A.app.app_context():
        new_weights = SCH._ca_weights()
    check("the new weighting is actually saved",
          new_weights == {"test": 15.0, "assignment": 15.0, "project": 10.0}, str(new_weights))

    with A.app.app_context():
        rescaled = SCH._term_subject_report(student_id, session_id, "First Term", subject_id)
    # Test 8/10 -> /15 = 12; assignment 9/10 -> /15 = 13.5; project 7/10 -> /10 = 7
    check("changing the weighting immediately changes future report calculations",
          abs(rescaled["test_score"] - 12.0) < 0.01 and abs(rescaled["assignment_score"] - 13.5) < 0.01,
          str(rescaled))

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
