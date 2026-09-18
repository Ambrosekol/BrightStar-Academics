"""The authorised-signature setting (draw, upload, remove) and its effect on
the generated receipt PDF.

Written alongside the receipt redesign that added a real signature image to
every receipt (previously just a blank line). Covers: the settings page
renders, drawing a signature (posted as a canvas data: URL) saves and shows a
preview, uploading an image file does the same and replaces the drawn one
without leaving the old file behind, removing it clears the setting, and the
PDF generator embeds whichever signature is currently configured without
raising even when none is set.
"""
import base64
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DB = os.path.join(HERE, "receipt_settings.db")
shutil.copy(os.path.join(ROOT, "cbt.db"), DB)
sys.path.insert(0, ROOT)
from werkzeug.security import generate_password_hash  # noqa: E402

# A well-known minimal valid 1x1 transparent PNG.
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
now = datetime.now(timezone.utc).isoformat()
tid = con.execute("SELECT id FROM admin_types WHERE is_system=1 ORDER BY id LIMIT 1").fetchone()["id"]
admin_id = con.execute(
    "INSERT INTO admins(username,display_name,password_hash,admin_type_id,active,created_at,"
    "password_must_change) VALUES(?,?,?,?,1,?,0)",
    ("zz_sig_admin", "Signature Probe", generate_password_hash("SigPass!2345"), tid, now)).lastrowid
con.execute("INSERT OR IGNORE INTO admin_role_assignments(admin_id,admin_type_id,assigned_at) VALUES(?,?,?)",
            (admin_id, tid, now))
for p in con.execute("SELECT id FROM permissions"):
    con.execute("INSERT OR IGNORE INTO admin_permissions(admin_id,permission_id,granted_at) VALUES(?,?,?)",
                (admin_id, p["id"], now))

school_id = con.execute("SELECT id FROM schools WHERE active=1 LIMIT 1").fetchone()["id"]
session_id = con.execute("SELECT id FROM academic_sessions WHERE is_current=1 AND active=1 LIMIT 1").fetchone()["id"]
student_id = con.execute(
    "INSERT INTO students(admission_no,first_name,last_name,created_at,active,school_id,"
    "student_number,student_number_source) VALUES(?,?,?,?,1,?,?,?)",
    ("ZZ-SIG-0001", "Signature", "TestStudent", now, school_id, "ZZ-SIG-0001", "existing")).lastrowid
payment_id = con.execute(
    "INSERT INTO finance_payments(receipt_no,student_id,session_id,amount,category,method,"
    "paid_at,recorded_by,status,created_at,payer_name) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
    ("ZZ-SIG-TEST-0001", student_id, session_id, 5000, "School Fees", "Cash",
     now, admin_id, "posted", now, "Signature Test Payer")).lastrowid
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


saved_files = []

with A.app.test_client() as c:
    c.post("/login", data={"username": "zz_sig_admin", "password": "SigPass!2345"})
    c.get("/admin/workspace/school")

    page = c.get("/admin/finance/receipt-settings").get_data(as_text=True)
    check("settings page renders", "Receipt Settings" in page)
    check("shows the no-signature message initially", "No signature configured yet" in page)

    with A.app.app_context():
        pdf_bytes, _ = A._receipt_pdf(payment_id)
    check("PDF renders with no signature configured", len(pdf_bytes) > 1000)

    token = csrf_from(page)
    data_url = "data:image/png;base64," + base64.b64encode(TINY_PNG).decode()
    r = c.post("/admin/finance/receipt-settings", data={
        "_csrf_token": token, "action": "draw", "signature_data_url": data_url,
    })
    check("drawn signature redirects back", r.status_code in (302, 303), str(r.status_code))

    with A.app.app_context():
        drawn_path = A._receipt_signature_abspath()
    check("drawn signature file was saved", bool(drawn_path and os.path.exists(drawn_path)))
    if drawn_path:
        saved_files.append(drawn_path)

    page2 = c.get("/admin/finance/receipt-settings").get_data(as_text=True)
    check("settings page now shows a signature preview", "Authorised signature" in page2 and "img src=" in page2)

    with A.app.app_context():
        pdf_bytes2, _ = A._receipt_pdf(payment_id)
    check("PDF renders with a drawn signature configured", len(pdf_bytes2) > 1000)

    token2 = csrf_from(page2)
    upload_data = {
        "_csrf_token": token2, "action": "upload",
        "signature_file": (__import__("io").BytesIO(TINY_PNG), "signature.png"),
    }
    r2 = c.post("/admin/finance/receipt-settings", data=upload_data, content_type="multipart/form-data")
    check("uploaded signature redirects back", r2.status_code in (302, 303), str(r2.status_code))

    with A.app.app_context():
        uploaded_path = A._receipt_signature_abspath()
    check("uploaded signature file was saved", bool(uploaded_path and os.path.exists(uploaded_path)))
    check("uploading replaced the drawn file, not stacked it", uploaded_path != drawn_path)
    check("the old drawn file was deleted after being replaced", drawn_path and not os.path.exists(drawn_path))
    if uploaded_path:
        saved_files.append(uploaded_path)

    page3 = c.get("/admin/finance/receipt-settings").get_data(as_text=True)
    token3 = csrf_from(page3)
    r3 = c.post("/admin/finance/receipt-settings", data={"_csrf_token": token3, "action": "remove"})
    check("remove redirects back", r3.status_code in (302, 303), str(r3.status_code))

    with A.app.app_context():
        after_remove = A._receipt_signature_abspath()
    check("signature cleared after removal", after_remove is None)
    check("the uploaded file was deleted on removal", uploaded_path and not os.path.exists(uploaded_path))

    page4 = c.get("/admin/finance/receipt-settings").get_data(as_text=True)
    check("settings page shows no-signature message again", "No signature configured yet" in page4)

with A.app.app_context():
    A.db.session.remove()
    A.db.engine.dispose()
for f in saved_files:
    if f and os.path.exists(f):
        try: os.remove(f)
        except OSError: pass
try:
    os.remove(DB)
except OSError:
    pass

print()
failed = [x for x in results if not x[1]]
print(f"{len(results)-len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
