"""Bank lock/unlock must be reachable and usable from the Controls page.

Locking is enforced in five places (editing a bank, adding, editing, deleting
and reordering its questions), so a lock that cannot be lifted through the UI
strands the bank permanently. The Phase 13 redesign of admin_controls.html
dropped the panel while leaving the routes and the enforcement in place; this
covers the whole cycle so that cannot regress silently.
"""
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DB = os.path.join(HERE, "banklocks.db")

SUPER_USER, SUPER_PW = "zz_lock_super", "LockPass!23456"
STAFF_USER, STAFF_PW = "zz_lock_staff", "StaffPass!2345"

shutil.copy(os.path.join(ROOT, "cbt.db"), DB)
sys.path.insert(0, ROOT)
from werkzeug.security import generate_password_hash  # noqa: E402

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
now = datetime.now(timezone.utc).isoformat()


def make_admin(username, password, system):
    tid = con.execute("SELECT id FROM admin_types WHERE is_system=? ORDER BY id LIMIT 1",
                      (system,)).fetchone()["id"]
    con.execute("DELETE FROM admins WHERE username=?", (username,))
    aid = con.execute(
        "INSERT INTO admins(username,display_name,password_hash,admin_type_id,active,"
        "created_at,password_must_change) VALUES(?,?,?,?,1,?,0)",
        (username, username, generate_password_hash(password), tid, now)).lastrowid
    con.execute("INSERT OR IGNORE INTO admin_role_assignments(admin_id,admin_type_id,assigned_at) "
                "VALUES(?,?,?)", (aid, tid, now))
    for p in con.execute("SELECT id FROM permissions"):
        con.execute("INSERT OR IGNORE INTO admin_permissions(admin_id,permission_id,granted_at) "
                    "VALUES(?,?,?)", (aid, p["id"], now))
    return aid


SUPER_ID = make_admin(SUPER_USER, SUPER_PW, 1)
STAFF_ID = make_admin(STAFF_USER, STAFF_PW, 0)
con.execute("DELETE FROM admin_resource_locks")
con.commit()
con.close()

os.environ["CRAINBOW_DB"] = DB
os.chdir(ROOT)
import app as A  # noqa: E402
import core.entrance as ENTRANCE  # noqa: E402

A.app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB}"
A.app.config["TESTING"] = True

BANK_ID = sorted(ENTRANCE.load_banks().keys())[0]
CSRF = re.compile(rb'name="_csrf_token"[^>]*value="([^"]+)"')
CONTROLS = "/admin/administration/controls"
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


def q(sql, *params):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        return c.execute(sql, params).fetchall()
    finally:
        c.close()


def locked():
    return bool(q("SELECT 1 FROM admin_resource_locks WHERE resource_type='bank' "
                  "AND resource_id=? AND unlocked_at IS NULL", BANK_ID))


with A.app.test_client() as c:
    c.post("/login", data={"username": SUPER_USER, "password": SUPER_PW})
    c.get("/admin/workspace/entrance")

    page = c.get(CONTROLS).get_data()
    check("controls page renders", len(page) > 1000, str(len(page)))
    check("lock panel present", b"Protected examination resources" in page)
    check("lock form present for Super Admin",
          b"/lock" in page and b'name="reason"' in page)
    m = CSRF.search(page)
    check("panel carries a CSRF token", m is not None)
    token = m.group(1).decode() if m else ""

    # ---- lock ----
    r = c.post(f"/admin/administration/resources/bank/{BANK_ID}/lock",
               data={"_csrf_token": token, "reason": "probe freeze"})
    check("lock accepted", r.status_code in (302, 303), str(r.status_code))
    check("bank is locked", locked())
    row = q("SELECT reason,locked_by FROM admin_resource_locks WHERE resource_id=? "
            "AND unlocked_at IS NULL", BANK_ID)
    check("lock reason stored", bool(row) and row[0]["reason"] == "probe freeze",
          str([dict(x) for x in row]))
    check("lock attributed to the Super Admin", bool(row) and row[0]["locked_by"] == SUPER_ID)
    check("a governance review item was raised",
          bool(q("SELECT 1 FROM admin_control_items WHERE target_type='bank' AND target_id=? "
                 "AND status='open' AND category='governance'", str(BANK_ID))))

    page = c.get(CONTROLS).get_data()
    check("locked state shown on the page", b"probe freeze" in page)
    check("unlock control offered", b"/unlock" in page)

    # ---- the lock actually blocks edits ----
    t = CSRF.search(c.get(f"/admin/banks/{BANK_ID}").get_data())
    if t:
        r = c.post(f"/admin/banks/{BANK_ID}/questions/new", data={
            "_csrf_token": t.group(1).decode(), "text": "should not be added",
            "option_a": "A", "option_b": "B", "option_c": "C", "option_d": "D",
            "answer": "0", "points": "1"})
        before = len(ENTRANCE.load_banks()[BANK_ID].get("questions", []))
        ENTRANCE.load_banks.cache_clear() if hasattr(ENTRANCE.load_banks, "cache_clear") else None
        after = len(ENTRANCE.load_banks()[BANK_ID].get("questions", []))
        check("locked bank rejects new questions", before == after, f"{before}->{after}")

    # ---- unlock ----
    page = c.get(CONTROLS).get_data()
    token = CSRF.search(page).group(1).decode()
    r = c.post(f"/admin/administration/resources/bank/{BANK_ID}/unlock",
               data={"_csrf_token": token})
    check("unlock accepted", r.status_code in (302, 303), str(r.status_code))
    check("bank is unlocked", not locked())
    check("governance item resolved on unlock",
          not q("SELECT 1 FROM admin_control_items WHERE target_type='bank' AND target_id=? "
                "AND status='open' AND category='governance'", str(BANK_ID)))

    # ---- CSRF is enforced ----
    r = c.post(f"/admin/administration/resources/bank/{BANK_ID}/lock",
               data={"reason": "no token"})
    check("lock without CSRF token refused", r.status_code == 403, str(r.status_code))
    check("no lock created without a token", not locked())

# ---- a non-super-admin may look but not act ----
with A.app.test_client() as c:
    c.post("/login", data={"username": STAFF_USER, "password": STAFF_PW})
    c.get("/admin/workspace/entrance")
    page = c.get(CONTROLS).get_data()
    check("staff admin sees the panel", b"Protected examination resources" in page)
    check("staff admin is offered no lock control", b'name="reason"' not in page)
    # The read-only panel renders no form, so take a session-valid token from
    # another page: the point is that the route refuses, not that the UI hides it.
    token = None
    for url in (f"/admin/banks/{BANK_ID}", "/admin/administration/messages",
                "/admin/password", CONTROLS):
        m = CSRF.search(c.get(url).get_data())
        if m:
            token = m.group(1).decode()
            break
    check("obtained a session CSRF token for the staff probe", token is not None)
    r = c.post(f"/admin/administration/resources/bank/{BANK_ID}/lock",
               data={"_csrf_token": token or "", "reason": "staff attempt"})
    check("staff admin cannot lock", not locked(), f"status={r.status_code}")

print()
failed = [r for r in results if not r[1]]
print(f"{len(results)-len(failed)}/{len(results)} bank-lock checks passed")
sys.exit(1 if failed else 0)
