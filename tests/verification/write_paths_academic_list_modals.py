"""Assignments, Projects, Tests, Practice Tests and Examinations all follow
the same "choose a class, then see a long list below" pattern the Students
page used to have - and had the exact same problem: the list rendered inline
below the whole class-card grid, requiring a scroll to reach it. This applies
the same fix already verified for Students: the list now opens in a modal
that auto-shows on page load.

Checks the real markup for all three list-owning routes (the assessment list
template is shared by tests/practice-tests/examinations, so exercising one
covers all three), and confirms the Projects page's search form - which
previously had no hidden `class` field, silently losing the class filter on
every search - now preserves it.
"""
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DB = os.path.join(HERE, "academic_list_modals.db")
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
    ("zz_lm_admin", "List Modal Probe", generate_password_hash("LmPass!12345"), tid, now)).lastrowid
con.execute("INSERT OR IGNORE INTO admin_role_assignments(admin_id,admin_type_id,assigned_at) VALUES(?,?,?)",
            (admin_id, tid, now))

session_id = con.execute("SELECT id FROM academic_sessions WHERE is_current=1 AND active=1 LIMIT 1").fetchone()["id"]
class_id = con.execute("SELECT id FROM school_classes WHERE active=1 ORDER BY id LIMIT 1").fetchone()["id"]
class_name = con.execute("SELECT name FROM school_classes WHERE id=?", (class_id,)).fetchone()["name"]
subject_id = con.execute(
    "SELECT s.id FROM school_subjects s JOIN class_subjects cs ON cs.subject_id=s.id "
    "WHERE cs.class_id=? AND s.active=1 LIMIT 1", (class_id,)).fetchone()["id"]

con.execute("INSERT INTO school_assignments(title,instructions,class_id,subject_id,created_by,created_at,"
            "active,assignment_type,timing_mode,session_id,term) VALUES(?,?,?,?,?,?,1,'written','untimed',?,?)",
            ("ZZ Modal Assignment", "", class_id, subject_id, admin_id, now, session_id, "First Term"))
con.execute("INSERT INTO school_projects(title,instructions,class_id,subject_id,date_given,created_by,created_at,"
            "active,session_id,term) VALUES(?,?,?,?,?,?,?,1,?,?)",
            ("ZZ Modal Project", "", class_id, subject_id, now[:10], admin_id, now, session_id, "First Term"))
con.execute("INSERT INTO school_assessments(assessment_type,title,instructions,class_id,subject_id,session_id,"
            "duration_minutes,question_count,active,created_by,created_at,term) VALUES('test',?,?,?,?,?,?,0,1,?,?,?)",
            ("ZZ Modal Test", "", class_id, subject_id, session_id, 30, admin_id, now, "First Term"))
con.commit()
con.close()

os.environ["CRAINBOW_DB"] = DB
os.chdir(ROOT)
import app as A  # noqa: E402

A.app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB}"
A.app.config["TESTING"] = True
with A.app.app_context():
    A.init_db()

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


with A.app.test_client() as c:
    c.post("/login", data={"username": "zz_lm_admin", "password": "LmPass!12345"})
    c.get("/admin/workspace/school")

    for label, url, modal_id, needle in [
        ("Assignments", f"/admin/school/assignments?class={class_name}", "assignmentsModal", "ZZ Modal Assignment"),
        ("Projects", f"/admin/school/projects?class={class_name}", "projectsModal", "ZZ Modal Project"),
        ("Tests", f"/admin/school/tests?class={class_name}", "assessmentListModal", "ZZ Modal Test"),
        ("Practice Tests", f"/admin/school/practice-tests?class={class_name}", "assessmentListModal", None),
        ("Examinations", f"/admin/school/examinations?class={class_name}", "assessmentListModal", None),
    ]:
        no_class_page = c.get(url.split('?')[0]).get_data(as_text=True)
        check(f"{label}: with no class selected, no list dialog is rendered",
              f'id="{modal_id}"' not in no_class_page)

        page = c.get(url).get_data(as_text=True)
        check(f"{label}: selecting a class renders the list inside a <dialog>",
              f'<dialog class="cr-modal" id="{modal_id}">' in page)
        check(f"{label}: the dialog auto-opens (no scrolling needed)",
              f"document.getElementById('{modal_id}')?.showModal();" in page)
        if needle:
            check(f"{label}: the seeded row appears inside the dialog markup",
                  needle in page.split(f'id="{modal_id}"')[1].split('</dialog>')[0])

    # Projects search must preserve the class filter - previously it had no
    # hidden `class` field, so submitting Search silently dropped back to
    # "no class selected" and lost the whole filtered view.
    projects_page = c.get(f"/admin/school/projects?class={class_name}").get_data(as_text=True)
    check("Projects search form preserves the selected class via a hidden field",
          f'<input type="hidden" name="class" value="{class_name}">' in projects_page)

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
