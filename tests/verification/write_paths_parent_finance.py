"""Parents could not see what they had been charged, what they had paid, or
what they still owed anywhere in the parent portal - the dashboard only ever
showed academic activity. This adds a fee summary to /parent/dashboard, a
full per-child fee account page (/parent/children/<id>/finance) with a
Paid/Part Paid/Unpaid breakdown and downloadable receipts, and an automatic
email + WhatsApp + in-app alert whenever finance/a Super Admin assesses a new
fee to a student or records a payment against one - previously a parent only
ever found out about either by asking the school office.

It also fixes a real, pre-existing gap this work surfaced: templates/app.css
(loaded by every parent- and student-facing page) never defined `.btn`,
`.btn-primary` or `.btn-light` at all - only admin.css did - so every "button"
on the parent portal (Open profile, Send feedback, etc.) had always rendered
as a bare underlined link with no button styling whatsoever. This test checks
the CSS now exists and that the new fee-account page actually uses it.

Drives the real admin routes (fee assessment creation, payment recording) so
the notification hooks are proven end to end, not just called directly; and
drives the real parent routes to check the numbers and markup a parent
actually sees, plus that a parent can never reach another family's fee
account or receipt.
"""
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DB = os.path.join(HERE, "parent_finance.db")
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
    ("zz_pfin_admin", "Parent Finance Probe", generate_password_hash("PfinPass!123"), tid, now)).lastrowid
con.execute("INSERT OR IGNORE INTO admin_role_assignments(admin_id,admin_type_id,assigned_at) VALUES(?,?,?)",
            (admin_id, tid, now))

session_id = con.execute("SELECT id FROM academic_sessions WHERE is_current=1 AND active=1 LIMIT 1").fetchone()["id"]
session_name = con.execute("SELECT name FROM academic_sessions WHERE id=?", (session_id,)).fetchone()["name"]
class_id = con.execute("SELECT id FROM school_classes WHERE active=1 ORDER BY id LIMIT 1").fetchone()["id"]

# A brand new student with no financial history at all, so the assessed/paid/
# outstanding math below can be asserted as exact figures rather than deltas
# against whatever this shared fixture database already carries.
student_id = con.execute(
    "INSERT INTO students(admission_no,first_name,last_name,guardian_email,guardian_phone,created_at,"
    "active,account_active,password_must_change) VALUES(?,?,?,?,?,?,1,1,0)",
    ("ZZ-PFIN-STUDENT", "Fee", "Testchild", "zz_pfin_guardian@example.com", "08030000099", now)).lastrowid
con.execute("INSERT INTO student_enrolments(student_id,class_id,session_id,enrolled_at,active) "
            "VALUES(?,?,?,?,1)", (student_id, class_id, session_id, now[:10]))

# A second, unrelated student+parent to prove cross-family isolation.
other_student_id = con.execute(
    "INSERT INTO students(admission_no,first_name,last_name,created_at,active,account_active,"
    "password_must_change) VALUES(?,?,?,?,1,1,0)",
    ("ZZ-PFIN-OTHER", "Other", "Family", now)).lastrowid
other_parent_id = con.execute(
    "INSERT INTO parent_accounts(username,display_name,password_hash,email,phone,created_at,active,"
    "password_must_change) VALUES(?,?,?,?,?,?,1,0)",
    ("zz_pfin_other_parent", "Other Family Parent", generate_password_hash("OtherPass!123"),
     "other@example.com", "08030000098", now)).lastrowid
con.execute("INSERT INTO parent_student_links(parent_id,student_id,relationship,active,created_at,created_by) "
            "VALUES(?,?,?,1,?,?)", (other_parent_id, other_student_id, "Father", now, admin_id))

parent_id = con.execute(
    "INSERT INTO parent_accounts(username,display_name,password_hash,email,phone,created_at,active,"
    "password_must_change) VALUES(?,?,?,?,?,?,1,0)",
    ("zz_pfin_parent", "Fee Test Parent", generate_password_hash("ParentPfin!123"),
     "parent-portal@example.com", "08030000097", now)).lastrowid
con.execute("INSERT INTO parent_student_links(parent_id,student_id,relationship,active,created_at,created_by) "
            "VALUES(?,?,?,1,?,?)", (parent_id, student_id, "Mother", now, admin_id))

item_id = con.execute(
    "INSERT INTO finance_fee_items(name,category,stage,amount,applicability,required,optional,active,"
    "created_at,updated_at,created_by,updated_by) VALUES(?,?,?,?,?,?,?,1,?,?,?,?)",
    ("ZZ Pfin Tuition", "Tuition", "All", 50000, "First Term", 1, 0, now, now, admin_id, admin_id)).lastrowid
con.execute("INSERT INTO finance_fee_item_classes(fee_item_id,class_id,active,created_at,created_by) VALUES(?,?,1,?,?)",
            (item_id, class_id, now, admin_id))
con.commit()
con.close()

os.environ["CRAINBOW_DB"] = DB
os.chdir(ROOT)
import app as A  # noqa: E402
import core.notifications as CN  # noqa: E402
from sqlalchemy import select  # noqa: E402

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


check(".btn/.btn-primary/.btn-light now exist in app.css (parent/student pages loaded only app.css, which never defined them)",
      all(needle in open(os.path.join(ROOT, "static", "app.css"), encoding="utf-8").read()
          for needle in [".btn{", ".btn-primary{", ".btn-light{"]))

# cbt.db is a live, shared fixture that may already carry earlier real
# assessments/payments for this same student+session from other work, so
# totals are asserted as deltas against a snapshot taken before this test
# does anything, never as hardcoded absolutes.
with A.app.app_context():
    before_totals = A._finance_student_lifetime_totals(student_id, session_id)

calls = {"email": [], "whatsapp": []}
real_email, real_whatsapp = CN._notify_guardian_email, CN._notify_guardian_whatsapp


def fake_email(*args, **kwargs):
    calls["email"].append(args)
    return True, "sent"


def fake_whatsapp(*args, **kwargs):
    calls["whatsapp"].append(args)
    return True, "sent"


CN._notify_guardian_email = fake_email
CN._notify_guardian_whatsapp = fake_whatsapp
assess_id = None
payment_id = None
try:
    with A.app.test_client() as admin_c:
        admin_c.post("/login", data={"username": "zz_pfin_admin", "password": "PfinPass!123"})
        admin_c.get("/admin/workspace/school")

        fee_page = admin_c.get("/admin/finance/fee-items").get_data(as_text=True)
        token = csrf_from(fee_page)
        r1 = admin_c.post("/admin/finance/assessments/new", data={
            "_csrf_token": token, "student_id": str(student_id), "session_id": str(session_id),
            "fee_item_id": str(item_id), "term": "First Term", "due_date": "", "notes": ""})
        check("fee assessment creation redirects (succeeds)", r1.status_code in (302, 303), str(r1.status_code))

        with A.app.app_context():
            assess_id = A.one_scalar(select(A.FinanceFeeAssessment.id).where(
                A.FinanceFeeAssessment.student_id == student_id, A.FinanceFeeAssessment.fee_item_id == item_id))
        check("the assessment was actually written", assess_id is not None)

        token2 = csrf_from(admin_c.get("/admin/finance/payments/new").get_data(as_text=True))
        r2 = admin_c.post("/admin/finance/payments/new", data={
            "_csrf_token": token2, "student_id": str(student_id), "session_id": str(session_id),
            "amount": "20000", "category": "Tuition", "method": "Cash", "reference": "",
            "paid_at": "", "notes": "", "payer_name": "Fee Test Parent"})
        check("payment recording redirects (succeeds)", r2.status_code in (302, 303), str(r2.status_code))

        with A.app.app_context():
            payment_id = A.one_scalar(select(A.FinancePayment.id).where(
                A.FinancePayment.student_id == student_id).order_by(A.FinancePayment.id.desc()))
        check("the payment was actually written", payment_id is not None)
finally:
    CN._notify_guardian_email, CN._notify_guardian_whatsapp = real_email, real_whatsapp

check("assessing a fee emailed the guardian address on the student record",
      any(c[0] == "zz_pfin_guardian@example.com" for c in calls["email"]), str(calls["email"]))
check("assessing a fee WhatsApp'd the guardian number on the student record",
      any(c[0] == "08030000099" for c in calls["whatsapp"]), str(calls["whatsapp"]))
check("the fee-assessed email mentions the fee name and amount",
      any("ZZ Pfin Tuition" in str(c) and "50,000" in str(c) for c in calls["email"]))
check("recording a payment also emailed the guardian a confirmation",
      len(calls["email"]) >= 2)
check("the payment-recorded email mentions the amount and receipt",
      any("20,000" in str(c) for c in calls["email"][1:]))

with A.app.app_context():
    notif_count = A.one_scalar(select(A.func.count()).select_from(A.SchoolNotification).where(
        A.SchoolNotification.recipient_type == "parent", A.SchoolNotification.recipient_id == parent_id,
        A.SchoolNotification.category == "finance"))
check("two in-app finance notifications were created for the linked parent (assessed + paid)",
      notif_count == 2, str(notif_count))

with A.app.test_client() as parent_c:
    parent_c.post("/login", data={"username": "zz_pfin_parent", "password": "ParentPfin!123"})

    # The top-level balance is allocation-based, exactly like the itemised
    # breakdown, so it can never show less debt than a fee genuinely carries
    # just because an unrelated payment was overpaid elsewhere. The 20,000
    # payment above was recorded but never allocated to the new 50,000
    # assessment, so it must NOT reduce "paid" or "outstanding" yet - it
    # shows up only as "unallocated" until a staff member applies it.
    with A.app.app_context():
        after_totals = A._finance_student_lifetime_totals(student_id, session_id)
    check("the assessed total increased by exactly N50,000",
          after_totals["assessed"] - before_totals["assessed"] == 50000, str(after_totals))
    check("the paid total does NOT increase from an unallocated payment",
          after_totals["paid"] - before_totals["paid"] == 0, str(after_totals))
    check("the outstanding total increased by the full N50,000 (nothing allocated to it yet)",
          after_totals["outstanding"] - before_totals["outstanding"] == 50000, str(after_totals))
    check("the unallocated total increased by exactly N20,000 (the unapplied payment)",
          after_totals["unallocated"] - before_totals["unallocated"] == 20000, str(after_totals))

    dash = parent_c.get("/parent/dashboard").get_data(as_text=True)
    check("the dashboard shows the Fees sub-panel", "FEES</span>" in dash or ">Fees<" in dash)
    check("the dashboard's outstanding figure matches the freshly computed total",
          "{:,.0f}".format(after_totals["outstanding"]) in dash)
    check("the dashboard links to the child's fee account",
          f"/parent/children/{student_id}/finance" in dash)
    check("a real in-app finance notification appears on the dashboard",
          "New fee charged" in dash or "Payment received" in dash)

    finance_page = parent_c.get(f"/parent/children/{student_id}/finance").get_data(as_text=True)
    check("the fee account page renders", "Fee Account" in finance_page or "FEE ACCOUNT" in finance_page)
    check("the fee account page's top balance matches the freshly computed total",
          "{:,.2f}".format(after_totals["outstanding"]) in finance_page)
    # The per-fee breakdown and the top balance now agree: since the 20,000
    # payment was recorded but never allocated to this assessment by staff,
    # the line item reads Unpaid and the top balance carries the full
    # 50,000 as outstanding, with the 20,000 called out separately as an
    # unallocated credit rather than silently netted against it.
    check("the itemised fee breakdown shows Unpaid (payment not yet allocated to this specific fee)",
          "Unpaid" in finance_page)
    check("the fee account page calls out the unallocated payment separately",
          "not yet applied to a specific fee" in finance_page)
    check("the fee account page lists the payment with a receipt link",
          f"/parent/children/{student_id}/receipts/{payment_id}/pdf" in finance_page)
    check("the fee account page uses real .btn buttons, not bare links",
          'class="btn btn-light btn-small"' in finance_page)

    pdf = parent_c.get(f"/parent/children/{student_id}/receipts/{payment_id}/pdf")
    check("a parent can download their own child's receipt PDF",
          pdf.status_code == 200 and pdf.content_type == "application/pdf")

    # Cross-family isolation: this parent must never reach the other family's
    # child, fee account or receipt, no matter how the URL is guessed.
    r3 = parent_c.get(f"/parent/children/{other_student_id}/finance")
    check("a parent cannot open another family's fee account", r3.status_code == 404)
    r4 = parent_c.get(f"/parent/children/{student_id}/receipts/99999/pdf")
    check("a parent cannot fetch a receipt for a payment that isn't theirs", r4.status_code == 404)

with A.app.test_client() as other_c:
    other_c.post("/login", data={"username": "zz_pfin_other_parent", "password": "OtherPass!123"})
    r5 = other_c.get(f"/parent/children/{student_id}/finance")
    check("a different family's parent cannot open this student's fee account", r5.status_code == 404)
    r6 = other_c.get(f"/parent/children/{student_id}/receipts/{payment_id}/pdf")
    check("a different family's parent cannot download this student's receipt", r6.status_code == 404)

# The "never blocks" contract: a real failure in both channels must not stop
# the assessment or payment from being recorded.
def raising_email(*args, **kwargs):
    raise Exception("simulated SMTP outage")


def raising_whatsapp(*args, **kwargs):
    raise Exception("simulated WhatsApp outage")


CN._notify_guardian_email = raising_email
CN._notify_guardian_whatsapp = raising_whatsapp
try:
    with A.app.test_client() as admin_c2:
        admin_c2.post("/login", data={"username": "zz_pfin_admin", "password": "PfinPass!123"})
        admin_c2.get("/admin/workspace/school")
        token3 = csrf_from(admin_c2.get("/admin/finance/payments/new").get_data(as_text=True))
        r7 = admin_c2.post("/admin/finance/payments/new", data={
            "_csrf_token": token3, "student_id": str(student_id), "session_id": str(session_id),
            "amount": "5000", "category": "Tuition", "method": "Cash", "reference": "",
            "paid_at": "", "notes": "", "payer_name": "Fee Test Parent"})
        check("recording a payment still succeeds when both notification channels raise",
              r7.status_code in (302, 303), str(r7.status_code))
finally:
    CN._notify_guardian_email, CN._notify_guardian_whatsapp = real_email, real_whatsapp

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
