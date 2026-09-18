"""Follow-up fixes to the finance/parent-portal work: a fully paid fee item
must disappear from the payment allocation screen and can never receive a
further allocation server-side; the fee-item picker on the Student Billing
tab must grey out (with a clear label) any fee item already assessed for the
selected student+session+term, distinguishing "already paid" from "already
charged but unpaid"; the Recent Assessments tab is now grouped Student ->
Session -> Term inside a modal instead of one long flat table; a parent's
outstanding balance must never hide a debt left over from a previous academic
session (previously the dashboard and fee-account page only ever looked at
the current session); and a child's profile page now labels notifications
New the first time they're seen and Old on every visit after that.
"""
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DB = os.path.join(HERE, "finance_followups.db")
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
    ("zz_ff_admin", "Followups Probe", generate_password_hash("FfPass!12345"), tid, now)).lastrowid
con.execute("INSERT OR IGNORE INTO admin_role_assignments(admin_id,admin_type_id,assigned_at) VALUES(?,?,?)",
            (admin_id, tid, now))

current_session_id = con.execute("SELECT id FROM academic_sessions WHERE is_current=1 AND active=1 LIMIT 1").fetchone()["id"]
class_id = con.execute("SELECT id FROM school_classes WHERE active=1 ORDER BY id LIMIT 1").fetchone()["id"]

# A past, non-current academic session, to prove a balance left over from it
# is never hidden just because the school has moved on.
past_session_id = con.execute(
    "INSERT INTO academic_sessions(name,is_current,active,created_at) VALUES(?,0,1,?)",
    ("2024/2025 (Past)", now)).lastrowid

student_id = con.execute(
    "INSERT INTO students(admission_no,first_name,last_name,guardian_email,guardian_phone,created_at,"
    "active,account_active,password_must_change) VALUES(?,?,?,?,?,?,1,1,0)",
    ("ZZ-FF-STUDENT", "Followup", "Testchild", "zz_ff_guardian@example.com", "08030000199", now)).lastrowid
con.execute("INSERT INTO student_enrolments(student_id,class_id,session_id,enrolled_at,active) "
            "VALUES(?,?,?,?,1)", (student_id, class_id, past_session_id, now[:10]))
con.execute("INSERT INTO student_enrolments(student_id,class_id,session_id,enrolled_at,active) "
            "VALUES(?,?,?,?,1)", (student_id, class_id, current_session_id, now[:10]))

parent_id = con.execute(
    "INSERT INTO parent_accounts(username,display_name,password_hash,email,phone,created_at,active,"
    "password_must_change) VALUES(?,?,?,?,?,?,1,0)",
    ("zz_ff_parent", "Followup Parent", generate_password_hash("ParentFf!123"),
     "ff-parent@example.com", "08030000198", now)).lastrowid
con.execute("INSERT INTO parent_student_links(parent_id,student_id,relationship,active,created_at,created_by) "
            "VALUES(?,?,?,1,?,?)", (parent_id, student_id, "Mother", now, admin_id))

# Fee catalogue: one item, mapped to the class, usable in either session.
item_id = con.execute(
    "INSERT INTO finance_fee_items(name,category,stage,amount,applicability,required,optional,active,"
    "created_at,updated_at,created_by,updated_by) VALUES(?,?,?,?,?,?,?,1,?,?,?,?)",
    ("ZZ FF Tuition", "Tuition", "All", 40000, "First Term", 1, 0, now, now, admin_id, admin_id)).lastrowid
con.execute("INSERT INTO finance_fee_item_classes(fee_item_id,class_id,active,created_at,created_by) VALUES(?,?,1,?,?)",
            (item_id, class_id, now, admin_id))

item2_id = con.execute(
    "INSERT INTO finance_fee_items(name,category,stage,amount,applicability,required,optional,active,"
    "created_at,updated_at,created_by,updated_by) VALUES(?,?,?,?,?,?,?,1,?,?,?,?)",
    ("ZZ FF Uniform", "Uniform", "All", 10000, "Full Session", 1, 0, now, now, admin_id, admin_id)).lastrowid
con.execute("INSERT INTO finance_fee_item_classes(fee_item_id,class_id,active,created_at,created_by) VALUES(?,?,1,?,?)",
            (item2_id, class_id, now, admin_id))

# In the PAST session: assessed 40,000 (Tuition), paid only 15,000 - a real,
# unresolved balance of 25,000 that the school has since moved on from.
past_assess_id = con.execute(
    "INSERT INTO finance_fee_assessments(student_id,session_id,category,amount,due_date,notes,created_by,"
    "created_at,active,fee_item_id,term) VALUES(?,?,?,?,?,?,?,?,1,?,?)",
    (student_id, past_session_id, "Tuition", 40000, None, "", admin_id, now, item_id, "First Term")).lastrowid
past_payment_id = con.execute(
    "INSERT INTO finance_payments(receipt_no,student_id,session_id,amount,category,method,reference,paid_at,"
    "recorded_by,status,notes,created_at,payer_name) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
    ("ZZFF-PAST-0001", student_id, past_session_id, 15000, "Tuition", "Cash", "", now[:16], admin_id,
     "posted", "", now, "Followup Parent")).lastrowid
con.execute("INSERT INTO finance_payment_allocations(payment_id,assessment_id,amount,created_by,created_at) "
            "VALUES(?,?,?,?,?)", (past_payment_id, past_assess_id, 15000, admin_id, now))

# In the CURRENT session: a fully paid Uniform fee (should vanish from the
# allocate screen), and an as-yet-unassessed Tuition fee item (still pickable).
current_assess_id = con.execute(
    "INSERT INTO finance_fee_assessments(student_id,session_id,category,amount,due_date,notes,created_by,"
    "created_at,active,fee_item_id,term) VALUES(?,?,?,?,?,?,?,?,1,?,?)",
    (student_id, current_session_id, "Uniform", 10000, None, "", admin_id, now, item2_id, "Full Session")).lastrowid
current_payment_id = con.execute(
    "INSERT INTO finance_payments(receipt_no,student_id,session_id,amount,category,method,reference,paid_at,"
    "recorded_by,status,notes,created_at,payer_name) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
    ("ZZFF-CUR-0001", student_id, current_session_id, 10000, "Uniform", "Cash", "", now[:16], admin_id,
     "posted", "", now, "Followup Parent")).lastrowid
con.execute("INSERT INTO finance_payment_allocations(payment_id,assessment_id,amount,created_by,created_at) "
            "VALUES(?,?,?,?,?)", (current_payment_id, current_assess_id, 10000, admin_id, now))

# A still-unpaid fee in the current session too, purely so the allocate
# screen's form still has something legitimate to render - otherwise, with
# every current-session fee already fully paid, the page would show its
# empty state instead of a form at all.
item3_id = con.execute(
    "INSERT INTO finance_fee_items(name,category,stage,amount,applicability,required,optional,active,"
    "created_at,updated_at,created_by,updated_by) VALUES(?,?,?,?,?,?,?,1,?,?,?,?)",
    ("ZZ FF Books", "Books & Stationery", "All", 8000, "Full Session", 1, 0, now, now, admin_id, admin_id)).lastrowid
con.execute("INSERT INTO finance_fee_item_classes(fee_item_id,class_id,active,created_at,created_by) VALUES(?,?,1,?,?)",
            (item3_id, class_id, now, admin_id))
current_assess2_id = con.execute(
    "INSERT INTO finance_fee_assessments(student_id,session_id,category,amount,due_date,notes,created_by,"
    "created_at,active,fee_item_id,term) VALUES(?,?,?,?,?,?,?,?,1,?,?)",
    (student_id, current_session_id, "Books & Stationery", 8000, None, "", admin_id, now, item3_id, "Full Session")).lastrowid

# A second, fresh payment in the current session, unallocated - this is what
# the allocate screen will be exercised against.
fresh_payment_id = con.execute(
    "INSERT INTO finance_payments(receipt_no,student_id,session_id,amount,category,method,reference,paid_at,"
    "recorded_by,status,notes,created_at,payer_name) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
    ("ZZFF-CUR-0002", student_id, current_session_id, 5000, "Tuition", "Cash", "", now[:16], admin_id,
     "posted", "", now, "Followup Parent")).lastrowid

con.commit()
con.close()

os.environ["CRAINBOW_DB"] = DB
os.chdir(ROOT)
import app as A  # noqa: E402
from sqlalchemy import select  # noqa: E402

A.app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB}"
A.app.config["TESTING"] = True
with A.app.app_context():
    A.init_db()

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


with A.app.test_client() as admin_c:
    admin_c.post("/login", data={"username": "zz_ff_admin", "password": "FfPass!12345"})
    admin_c.get("/admin/workspace/school")

    # --- Allocate screen must exclude the fully paid Uniform assessment ---
    allocate_page = admin_c.get(f"/admin/finance/payments/{fresh_payment_id}/allocate").get_data(as_text=True)
    check("the allocate screen does not list the fully paid Uniform fee",
          "Uniform" not in allocate_page)
    check("the allocate screen still lists the genuinely unpaid Books & Stationery fee",
          "Books &amp; Stationery" in allocate_page)

    # A direct POST trying to allocate to the already-fully-paid assessment
    # must be refused server-side, not just hidden from the UI.
    csrf_m = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', allocate_page)
    csrf = csrf_m.group(1) if csrf_m else ""
    import json as _json
    r = admin_c.post(f"/admin/finance/payments/{fresh_payment_id}/allocate", data={
        "_csrf_token": csrf, "allocations": _json.dumps({str(current_assess_id): 100})},
        follow_redirects=True)
    body = r.get_data(as_text=True)
    check("allocating to an already fully paid assessment is refused server-side",
          "already been fully paid" in body)
    with A.app.app_context():
        alloc_count = A.one_scalar(select(A.func.count()).select_from(A.FinancePaymentAllocation).where(
            A.FinancePaymentAllocation.payment_id == fresh_payment_id))
    check("no allocation row was written for the refused request", alloc_count == 0)

    # --- assessed-items.json endpoint (backs the Student Billing picker) ---
    assessed_json = admin_c.get(
        f"/admin/finance/students/{student_id}/assessed-items.json?session_id={current_session_id}").get_json()
    check("the assessed-items endpoint reports the Uniform item as paid for Full Session",
          assessed_json.get("Full Session", {}).get(str(item2_id), {}).get("paid") is True, str(assessed_json))
    check("the assessed-items endpoint does not report the never-assessed Tuition item",
          str(item_id) not in assessed_json.get("First Term", {}))

    billing_page = admin_c.get("/admin/finance/fee-items").get_data(as_text=True)
    check("the fee item picker card carries a data-fee-item-id for JS matching",
          f'data-fee-item-id="{item2_id}"' in billing_page)
    check("the fee item picker has a hidden already-assessed label slot per card",
          "data-fee-already" in billing_page)

    # --- Recent Assessments grouped Student -> Session -> Term in a modal ---
    check("the Recent Assessments tab groups by student in a dialog",
          f'id="assessmentHistoryModal-{student_id}"' in billing_page)
    modal_html = billing_page.split(f'id="assessmentHistoryModal-{student_id}"')[1].split("</dialog>")[0]
    check("the student's modal shows the past session as its own heading",
          "2024/2025 (Past)" in modal_html)
    check("the student's modal shows the current session as its own heading",
          "2026/2027" in modal_html)
    check("the student's modal shows First Term as its own sub-heading",
          "First Term" in modal_html)
    check("the student's modal shows Full Session as its own sub-heading",
          "Full Session" in modal_html)
    check("the history list has a search box to filter by student/admission number",
          'id="financeHistorySearch"' in billing_page)

# --- A balance left over from a previous session must never be hidden ---
# Past: assessed 40,000, paid (allocated) 15,000 -> 25,000 still owed there.
# Current: Uniform 10,000/10,000 (fully allocated, 0 owed) + Books 8,000/0
# (8,000 owed) = 18,000 assessed, 10,000 allocated. The 5,000 fresh payment
# is deliberately left unallocated. Lifetime: assessed 58,000, paid
# (allocated) 25,000, outstanding 33,000 (25,000 + 8,000 - never netted
# against the unrelated, unallocated 5,000), unallocated 5,000.
with A.app.app_context():
    lifetime = A._finance_student_lifetime_totals(student_id)
check("the student's lifetime outstanding balance includes the unresolved past-session debt",
      lifetime["outstanding"] == 33000.0, str(lifetime))
check("the unallocated fresh payment is reported separately, not netted into outstanding",
      lifetime["unallocated"] == 5000.0, str(lifetime))

with A.app.test_client() as parent_c:
    parent_c.post("/login", data={"username": "zz_ff_parent", "password": "ParentFf!123"})

    dash = parent_c.get("/parent/dashboard").get_data(as_text=True)
    check("the parent dashboard reflects the carried-over balance, not zero",
          "33,000" in dash)

    finance_page = parent_c.get(f"/parent/children/{student_id}/finance").get_data(as_text=True)
    check("the fee account page's lifetime balance also reflects the carried-over debt",
          "33,000.00" in finance_page)
    check("the fee account page flags that a balance also exists in the other (non-selected) session",
          "2024/2025 (Past)" in finance_page or "Balance also outstanding in" in finance_page)
    check("the fee account page calls out the unallocated fresh payment separately",
          "5,000.00 recently paid" in finance_page)

    # --- Notifications on the child profile: New the first time, Old after ---
    now2 = datetime.now(timezone.utc).isoformat()
    with A.app.app_context():
        A.db.session.add(A.SchoolNotification(
            recipient_type="parent", recipient_id=parent_id, student_id=student_id,
            category="finance", title="ZZ FF test notification", message="test message",
            action_url=None, created_at=now2, created_by=admin_id))
        A.db.session.commit()

    first_view = parent_c.get(f"/parent/children/{student_id}").get_data(as_text=True)
    check("a brand-new notification is labelled New on first view",
          "ZZ FF test notification" in first_view and "is-new" in first_view)

    second_view = parent_c.get(f"/parent/children/{student_id}").get_data(as_text=True)
    marker = "ZZ FF test notification"
    row_start = second_view.index(marker) - 400
    row_slice = second_view[max(0, row_start):second_view.index(marker) + 200]
    check("the same notification is labelled Old on the very next view",
          "is-old" in row_slice and "is-new" not in row_slice, row_slice)

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
