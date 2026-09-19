"""Guardians must be emailed and WhatsApp'd when their child gets a new
assignment or project.

Drives real assignment/project creation through the actual admin routes with
a student whose guardian_email is the school's own no-reply address (so this
never emails a real parent), and confirms: (1) creation succeeds and is never
blocked by a notification failure, (2) a real email is actually delivered via
the configured SMTP server, (3) WhatsApp being unconfigured in this
deployment degrades gracefully instead of raising, and (4) a student with no
guardian contact info at all does not break creation for the rest of the
class.
"""
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DB = os.path.join(HERE, "school_work_notify.db")
shutil.copy(os.path.join(ROOT, "cbt.db"), DB)
sys.path.insert(0, ROOT)
from werkzeug.security import generate_password_hash  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
now = datetime.now(timezone.utc).isoformat()

tid = con.execute("SELECT id FROM admin_types WHERE is_system=1 ORDER BY id LIMIT 1").fetchone()["id"]
admin_id = con.execute(
    "INSERT INTO admins(username,display_name,password_hash,admin_type_id,active,created_at,"
    "password_must_change) VALUES(?,?,?,?,1,?,0)",
    ("zz_notify_admin", "Notify Probe", generate_password_hash("NotifyPass!12"), tid, now)).lastrowid
con.execute("INSERT OR IGNORE INTO admin_role_assignments(admin_id,admin_type_id,assigned_at) VALUES(?,?,?)",
            (admin_id, tid, now))

school_id = con.execute("SELECT id FROM schools WHERE active=1 LIMIT 1").fetchone()["id"]
session_id = con.execute("SELECT id FROM academic_sessions WHERE is_current=1 AND active=1 LIMIT 1").fetchone()["id"]
class_id = con.execute("SELECT id FROM school_classes WHERE active=1 ORDER BY id LIMIT 1").fetchone()["id"]
subject_id = con.execute(
    "SELECT s.id FROM school_subjects s JOIN class_subjects cs ON cs.subject_id=s.id "
    "WHERE cs.class_id=? AND s.active=1 LIMIT 1", (class_id,)).fetchone()["id"]
sender_addr = os.environ.get("CRAINBOW_SMTP_FROM") or os.environ.get("CRAINBOW_SMTP_USER", "")

with_contact = con.execute(
    "INSERT INTO students(admission_no,first_name,last_name,guardian_email,guardian_phone,created_at,"
    "active,school_id,student_number,student_number_source) VALUES(?,?,?,?,?,?,1,?,?,?)",
    ("ZZ-NOTIFY-0001", "HasContact", "TestStudent", sender_addr, "08030000001", now, school_id,
     "ZZ-NOTIFY-0001", "existing")).lastrowid
no_contact = con.execute(
    "INSERT INTO students(admission_no,first_name,last_name,created_at,active,school_id,"
    "student_number,student_number_source) VALUES(?,?,?,?,1,?,?,?)",
    ("ZZ-NOTIFY-0002", "NoContact", "TestStudent", now, school_id, "ZZ-NOTIFY-0002", "existing")).lastrowid
for sid in (with_contact, no_contact):
    con.execute("INSERT INTO student_enrolments(student_id,class_id,session_id,active,enrolled_at) "
                "SELECT ?,?,id,1,? FROM academic_sessions WHERE is_current=1 AND active=1 LIMIT 1",
                (sid, class_id, now))
con.commit()
con.close()

os.environ["CRAINBOW_DB"] = DB
os.chdir(ROOT)
import app as A  # noqa: E402
import core.notifications as CN  # noqa: E402

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


# --- Unit-level check of the notification helpers themselves -------------
with A.app.app_context():
    check("SMTP is configured for this run", bool(os.environ.get("CRAINBOW_SMTP_HOST")))
    ok, detail = CN._notify_guardian_email(sender_addr, "Crainbow CBT test: new assignment alert",
                                           "This is an automated check for the assignment/project "
                                           "guardian-notification feature. Safe to ignore.")
    check("real email notification delivers via the configured SMTP server", ok, str(detail))

    ok2, detail2 = CN._notify_guardian_whatsapp("08030000001", "test")
    check("WhatsApp gracefully reports 'not configured' rather than raising",
          not ok2 and "not configured" in detail2, str(detail2))

    # Must never raise even when called with a mix of good/blank contacts.
    try:
        A._notify_guardians_of_school_work([with_contact, no_contact], "assignment", "Notify Test", "2026-01-01")
        check("bulk notify tolerates a student with no guardian contact info", True)
    except Exception as exc:
        check("bulk notify tolerates a student with no guardian contact info", False, str(exc))

# --- Full route-level check: creation must never be blocked ---------------
with A.app.test_client() as c:
    c.post("/login", data={"username": "zz_notify_admin", "password": "NotifyPass!12"})
    c.get("/admin/workspace/school")

    new_page = c.get("/admin/school/assignments/new").get_data(as_text=True)
    token = csrf_from(new_page)
    r = c.post("/admin/school/assignments/new", data={
        "_csrf_token": token, "title": "Route Notify Test", "instructions": "", "due_date": "2026-01-01",
        "assignment_type": "written", "timing_mode": "untimed", "class_id": str(class_id),
        "subject_id": str(subject_id), "session_id": str(session_id), "term": "First Term",
        "student_ids": [str(with_contact), str(no_contact)],
    })
    check("assignment creation succeeds despite live guardian notifications",
          r.status_code in (302, 303), f"{r.status_code} {r.get_data(as_text=True)[:200]}")

    exists = A.one_scalar(A.select(A.func.count()).select_from(A.SchoolAssignment)
                           .where(A.SchoolAssignment.title == "Route Notify Test"))
    check("the assignment row was actually created", exists == 1)

    new_proj_page = c.get("/admin/school/projects/new").get_data(as_text=True)
    proj_token = csrf_from(new_proj_page)
    rp = c.post("/admin/school/projects/new", data={
        "_csrf_token": proj_token, "title": "Route Notify Project Test", "instructions": "",
        "date_given": "2026-01-01", "due_date": "2026-01-15", "class_id": str(class_id),
        "subject_id": str(subject_id), "session_id": str(session_id), "term": "First Term",
        "student_ids": [str(with_contact), str(no_contact)],
    })
    check("project creation succeeds despite live guardian notifications",
          rp.status_code in (302, 303), f"{rp.status_code} {rp.get_data(as_text=True)[:200]}")
    proj_exists = A.one_scalar(A.select(A.func.count()).select_from(A.SchoolProject)
                                .where(A.SchoolProject.title == "Route Notify Project Test"))
    check("the project row was actually created", proj_exists == 1)

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
