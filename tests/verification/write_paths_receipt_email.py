"""Send a payment receipt by email through the real admin route.

Verifies the whole path — receipt PDF generation, attachment, and the actual
SMTP delivery against the real configured mail server (.env) — not just the
isolated _smtp_send() helper. The email address used is the school's own
no-reply address, so this never contacts a real parent/guardian.

_smtp_send() itself (the SSL-vs-STARTTLS fix) was separately proven against
the live server in test_smtp_live.py: the old SMTP()+starttls() code hangs
until it times out on port 465 (an implicit-TLS-only port); the fixed code
connects via SMTP_SSL and sends in ~2-3 seconds.
"""
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DB = os.path.join(HERE, "receipt_email.db")
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
    ("zz_receipt_email", "Receipt Email Probe", generate_password_hash("ReceiptEmail!23"), tid, now)).lastrowid
con.execute("INSERT OR IGNORE INTO admin_role_assignments(admin_id,admin_type_id,assigned_at) VALUES(?,?,?)",
            (admin_id, tid, now))
for p in con.execute("SELECT id FROM permissions"):
    con.execute("INSERT OR IGNORE INTO admin_permissions(admin_id,permission_id,granted_at) VALUES(?,?,?)",
                (admin_id, p["id"], now))

school_id = con.execute("SELECT id FROM schools WHERE active=1 LIMIT 1").fetchone()["id"]
session_id = con.execute("SELECT id FROM academic_sessions WHERE is_current=1 AND active=1 LIMIT 1").fetchone()["id"]
sender_addr = os.environ.get("CRAINBOW_SMTP_FROM") or os.environ.get("CRAINBOW_SMTP_USER", "")
student_id = con.execute(
    "INSERT INTO students(admission_no,first_name,last_name,guardian_email,created_at,active,"
    "school_id,student_number,student_number_source) VALUES(?,?,?,?,?,1,?,?,?)",
    ("ZZ-RECEIPT-0001", "Receipt", "TestStudent", sender_addr, now, school_id,
     "ZZ-RECEIPT-0001", "existing")).lastrowid
payment_id = con.execute(
    "INSERT INTO finance_payments(receipt_no,student_id,session_id,amount,category,method,"
    "paid_at,recorded_by,status,created_at,payer_name) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
    ("ZZ-RECEIPT-TEST-0001", student_id, session_id, 15000, "School Fees", "Cash",
     now, admin_id, "posted", now, "Receipt Test Payer")).lastrowid
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


def q(sql, *params):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        return c.execute(sql, params).fetchall()
    finally:
        c.close()


check("SMTP is configured for this run", bool(os.environ.get("CRAINBOW_SMTP_HOST")),
      "no CRAINBOW_SMTP_HOST set - this run cannot exercise real delivery")

with A.app.test_client() as c:
    c.post("/login", data={"username": "zz_receipt_email", "password": "ReceiptEmail!23"})
    c.get("/admin/workspace/school")

    page = c.get(f"/admin/finance/receipts/{payment_id}").get_data(as_text=True)
    check("receipt page renders", len(page) > 500, f"len={len(page)}")

    t0 = __import__("time").time()
    r = c.post(f"/admin/finance/receipts/{payment_id}/email", data={
        "_csrf_token": re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', page).group(1)
    })
    elapsed = __import__("time").time() - t0
    check("email send request completed quickly (not a timeout)", elapsed < 15, f"{elapsed:.1f}s")
    check("email send redirected back to the receipt", r.status_code in (302, 303), str(r.status_code))

    log = q("SELECT status,error_message FROM finance_delivery_logs WHERE payment_id=? AND channel='email' "
            "ORDER BY id DESC LIMIT 1", payment_id)
    check("a delivery log row was written", bool(log))
    if log:
        check("delivery logged as sent", log[0]["status"] == "sent", str(dict(log[0])))

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
