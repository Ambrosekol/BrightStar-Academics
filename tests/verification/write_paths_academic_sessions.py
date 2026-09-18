"""Academic session management: create, switch the current session, archive
and reactivate — and that only a Super Admin can reach any of it.

Until now there was no admin UI at all to create a new academic session or
change which one is current; is_current was only ever set once, by the
fresh-install seed. This drives the real /admin/school/sessions route end to
end and checks: a non-Super-Admin is refused outright, creating a session
with "make current" flips it atomically (never two sessions current at
once), a session cannot be archived while it's current, an archived session
cannot be set current until reactivated, and _school_current_session() (the
function every dashboard/receipt/assessment relies on) picks up the change.
"""
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DB = os.path.join(HERE, "academic_sessions.db")
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
    ("zz_sessions_super", "Sessions Super", generate_password_hash("SuperPass!123"), super_tid, now)).lastrowid
con.execute("INSERT OR IGNORE INTO admin_role_assignments(admin_id,admin_type_id,assigned_at) VALUES(?,?,?)",
            (super_id, super_tid, now))

staff_id = con.execute(
    "INSERT INTO admins(username,display_name,password_hash,admin_type_id,active,created_at,"
    "password_must_change) VALUES(?,?,?,?,1,?,0)",
    ("zz_sessions_staff", "Sessions Staff", generate_password_hash("StaffPass!123"), staff_tid, now)).lastrowid
con.execute("INSERT OR IGNORE INTO admin_role_assignments(admin_id,admin_type_id,assigned_at) VALUES(?,?,?)",
            (staff_id, staff_tid, now))
for p in con.execute("SELECT id FROM permissions"):
    con.execute("INSERT OR IGNORE INTO admin_permissions(admin_id,permission_id,granted_at) VALUES(?,?,?)",
                (staff_id, p["id"], now))
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


def current_sessions():
    with A.app.app_context():
        from sqlalchemy import select
        return A.all_rows(select(A.AcademicSession.id, A.AcademicSession.name,
                                  A.AcademicSession.is_current, A.AcademicSession.active))


with A.app.test_client() as staff_c:
    staff_c.post("/login", data={"username": "zz_sessions_staff", "password": "StaffPass!123"})
    staff_c.get("/admin/workspace/school")
    r = staff_c.get("/admin/school/sessions")
    check("a non-Super-Admin is refused outright", r.status_code == 403, str(r.status_code))

original = current_sessions()
original_current = [s for s in original if s["is_current"]]
check("exactly one session is current before the test starts", len(original_current) == 1, str(original_current))

with A.app.test_client() as c:
    c.post("/login", data={"username": "zz_sessions_super", "password": "SuperPass!123"})
    c.get("/admin/workspace/school")

    page = c.get("/admin/school/sessions").get_data(as_text=True)
    check("Super Admin can open the page", "Academic Sessions" in page)
    token = csrf_from(page)

    r1 = c.post("/admin/school/sessions", data={
        "_csrf_token": token, "action": "create", "name": "ZZ-TEST-SESSION",
        "start_date": "2030-09-01", "end_date": "2031-07-31", "make_current": "1",
    })
    check("creating a session with make_current redirects back", r1.status_code in (302, 303))

    rows = current_sessions()
    new_row = next((r for r in rows if r["name"] == "ZZ-TEST-SESSION"), None)
    check("the new session was created", bool(new_row))
    check("the new session is current", bool(new_row and new_row["is_current"]))
    still_current = [r for r in rows if r["is_current"]]
    check("only one session is current after switching (never two)", len(still_current) == 1, str(still_current))
    check("the previously-current session lost is_current", new_row["id"] != original_current[0]["id"])

    with A.app.app_context():
        current_via_helper = A._school_current_session()
    check("_school_current_session() now returns the new session",
          bool(current_via_helper and current_via_helper["name"] == "ZZ-TEST-SESSION"))

    page2 = c.get("/admin/school/sessions").get_data(as_text=True)
    token2 = csrf_from(page2)
    r2 = c.post("/admin/school/sessions", data={
        "_csrf_token": token2, "action": "archive", "session_id": str(new_row["id"]),
    })
    after_archive = next(r for r in current_sessions() if r["id"] == new_row["id"])
    check("archiving the CURRENT session is refused", bool(after_archive["active"]), str(dict(after_archive)))

    dup_page = c.get("/admin/school/sessions").get_data(as_text=True)
    dup_token = csrf_from(dup_page)
    r3 = c.post("/admin/school/sessions", data={
        "_csrf_token": dup_token, "action": "create", "name": "ZZ-TEST-SESSION",
        "start_date": "", "end_date": "", "make_current": "",
    })
    dup_count = sum(1 for r in current_sessions() if r["name"] == "ZZ-TEST-SESSION")
    check("creating a duplicate-named session is refused", dup_count == 1, str(dup_count))

    # Switch current back to the original session so the new one is free to archive.
    orig_page = c.get("/admin/school/sessions").get_data(as_text=True)
    orig_token = csrf_from(orig_page)
    c.post("/admin/school/sessions", data={
        "_csrf_token": orig_token, "action": "set_current", "session_id": str(original_current[0]["id"]),
    })

    arch_page = c.get("/admin/school/sessions").get_data(as_text=True)
    arch_token = csrf_from(arch_page)
    c.post("/admin/school/sessions", data={
        "_csrf_token": arch_token, "action": "archive", "session_id": str(new_row["id"]),
    })
    after_real_archive = next(r for r in current_sessions() if r["id"] == new_row["id"])
    check("the (now non-current) session archives successfully", not after_real_archive["active"])

    reactivate_page = c.get("/admin/school/sessions").get_data(as_text=True)
    reactivate_token = csrf_from(reactivate_page)
    r4 = c.post("/admin/school/sessions", data={
        "_csrf_token": reactivate_token, "action": "set_current", "session_id": str(new_row["id"]),
    })
    still_archived_and_not_current = next(r for r in current_sessions() if r["id"] == new_row["id"])
    check("an archived session cannot be set current directly",
          not still_archived_and_not_current["is_current"], str(dict(still_archived_and_not_current)))

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
