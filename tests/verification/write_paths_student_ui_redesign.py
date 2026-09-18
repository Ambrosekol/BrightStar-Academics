"""The Students list and student profile pages were redesigned: the class
register now opens in a modal dialog instead of an inline section requiring
a scroll, and the profile page's long stack of sections (Term Results,
Academic Record, Assignments, Projects, Enrolment History) became a compact
grid of tiles that each open their content in a modal.

This checks the actual markup rather than just "the page renders": that the
class register lives inside a <dialog> that auto-opens via a showModal()
call (so a class selection is visible without scrolling), that all five
profile sections are now <dialog> elements rather than always-rendered
stacked <section> blocks, and that the redirect used after correcting an
enrolment-history entry (which targets #enrolment-history) still has a
matching element to open, since that anchor previously scrolled to a visible
section and now must open a hidden dialog instead.
"""
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DB = os.path.join(HERE, "student_ui_redesign.db")
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
    ("zz_ui_admin", "UI Redesign Probe", generate_password_hash("UiPass!12345"), tid, now)).lastrowid
con.execute("INSERT OR IGNORE INTO admin_role_assignments(admin_id,admin_type_id,assigned_at) VALUES(?,?,?)",
            (admin_id, tid, now))

school_id = con.execute("SELECT id FROM schools WHERE active=1 LIMIT 1").fetchone()["id"]
session_id = con.execute("SELECT id FROM academic_sessions WHERE is_current=1 AND active=1 LIMIT 1").fetchone()["id"]
class_id = con.execute("SELECT id FROM school_classes WHERE active=1 ORDER BY id LIMIT 1").fetchone()["id"]
class_name = con.execute("SELECT name FROM school_classes WHERE id=?", (class_id,)).fetchone()["name"]

student_id = con.execute(
    "INSERT INTO students(admission_no,first_name,last_name,created_at,active,school_id,"
    "student_number,student_number_source) VALUES(?,?,?,?,1,?,?,?)",
    ("ZZ-UIREDESIGN-0001", "UiRedesign", "TestStudent", now, school_id,
     "ZZ-UIREDESIGN-0001", "existing")).lastrowid
con.execute("INSERT INTO student_enrolments(student_id,class_id,session_id,active,enrolled_at) VALUES(?,?,?,1,?)",
            (student_id, class_id, session_id, now))
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
    c.post("/login", data={"username": "zz_ui_admin", "password": "UiPass!12345"})
    c.get("/admin/workspace/school")

    no_class_page = c.get("/admin/school/students").get_data(as_text=True)
    check("with no class selected, no register dialog is rendered", 'id="studentsModal"' not in no_class_page)

    class_page = c.get(f"/admin/school/students?class={class_name}").get_data(as_text=True)
    check("selecting a class renders the register inside a <dialog>",
          '<dialog class="cr-modal" id="studentsModal">' in class_page)
    check("the register dialog auto-opens (no scrolling needed)",
          "document.getElementById('studentsModal')?.showModal();" in class_page)
    check("the registered test student appears inside the dialog markup",
          "UiRedesign" in class_page.split('id="studentsModal"')[1].split('</dialog>')[0])

    profile = c.get(f"/admin/school/students/{student_id}").get_data(as_text=True)
    check("profile page renders", "STUDENT PROFILE" in profile)
    for modal_id, label in [
        ("modal-term-results", "Term Results"),
        ("modal-academic-record", "Academic Record"),
        ("modal-assignments", "Assignments"),
        ("modal-projects", "Projects"),
        ("modal-enrolment-history", "Enrolment History"),
    ]:
        check(f"{label} is a <dialog> (not an always-visible stacked section)",
              f'<dialog class="cr-modal" id="{modal_id}">' in profile)
        check(f"a quick-access tile opens the {label} dialog",
              f"document.getElementById('{modal_id}').showModal()" in profile)

    check("the identity cards (personal/guardian/account) use a 3-column grid, not modals",
          '<div class="profile-grid-3">' in profile and "STUDENT INFORMATION" in profile
          and "PARENT / GUARDIAN" in profile and "STUDENT ACCOUNT" in profile)

    check("the #enrolment-history redirect target still resolves to a real dialog to auto-open",
          "'#enrolment-history':'modal-enrolment-history'" in profile)

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
