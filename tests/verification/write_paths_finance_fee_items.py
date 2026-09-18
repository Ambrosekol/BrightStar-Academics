"""Fee item catalogue and the finance/fee-item disconnect.

Three things changed here. First, the Add/Edit Fee Item form's Category was
free text (an admin could type "Uniforms", "uniform", "UNIFORM..." and end up
with three different categories that mean the same thing) - it is now a fixed
dropdown. Second, Applicability/Billing Rule used to bundle terms into canned
combo strings ("First Term only", "Second and third term only") - each term
now stands on its own alongside Full Session and One-time. Both are validated
server-side, not just constrained in the <select>, and an older row carrying
a pre-existing value outside the new lists still renders and keeps that value
until an admin actively changes it (no silent data rewrite on save).

Third, and the actual complaint: fee items assessed to a student were an
island - nothing on the Fee Structure page or the Finance dashboard showed
whether an assessed item had been paid, and recording a payment never touched
the assessment it was meant to settle. The paid/allocate machinery already
existed (_finance_student_outstanding, /admin/finance/students/<id>/account,
/admin/finance/payments/<id>/allocate) but no page linked to it - it was
reachable only by typing the URL by hand. This checks the Recent Assessments
tab now shows a real Paid/Part Paid/Unpaid status per row with a link to that
student's account, the receipt page links to Allocate and to the account, the
dashboard's transaction ledger links to the account too, and the two
previously-orphaned routes are now gated by a real finance permission instead
of falling back to bare admin.access.
"""
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DB = os.path.join(HERE, "finance_fee_items.db")
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
    ("zz_fee_admin", "Fee Items Probe", generate_password_hash("FeePass!12345"), tid, now)).lastrowid
con.execute("INSERT OR IGNORE INTO admin_role_assignments(admin_id,admin_type_id,assigned_at) VALUES(?,?,?)",
            (admin_id, tid, now))

# A cashier who can record and view their own payments but was never granted
# finance.manage - exactly the profile the two newly-gated routes should
# admit, and the profile that would have slipped through before under the
# generic admin.access fallback.
cashier_tid = con.execute("SELECT id FROM admin_types WHERE is_system=0 AND active=1 ORDER BY id LIMIT 1").fetchone()["id"]
cashier_id = con.execute(
    "INSERT INTO admins(username,display_name,password_hash,admin_type_id,active,created_at,"
    "password_must_change) VALUES(?,?,?,?,1,?,0)",
    ("zz_fee_cashier", "Fee Items Cashier", generate_password_hash("CashPass!12345"), cashier_tid, now)).lastrowid
con.execute("INSERT OR IGNORE INTO admin_role_assignments(admin_id,admin_type_id,assigned_at) VALUES(?,?,?)",
            (cashier_id, cashier_tid, now))
for code in ("finance.record", "finance.view_own", "dashboard.view"):
    perm = con.execute("SELECT id FROM permissions WHERE code=?", (code,)).fetchone()
    if perm:
        con.execute("INSERT OR IGNORE INTO admin_permissions(admin_id,permission_id,granted_at) VALUES(?,?,?)",
                    (cashier_id, perm["id"], now))

# An admin with truly no finance permission at all, to prove the two newly
# gated routes now actually refuse someone outside finance.
outsider_tid = con.execute("SELECT id FROM admin_types WHERE is_system=0 AND active=1 ORDER BY id LIMIT 1").fetchone()["id"]
outsider_id = con.execute(
    "INSERT INTO admins(username,display_name,password_hash,admin_type_id,active,created_at,"
    "password_must_change) VALUES(?,?,?,?,1,?,0)",
    ("zz_fee_outsider", "Fee Items Outsider", generate_password_hash("OutPass!12345"), outsider_tid, now)).lastrowid
con.execute("INSERT OR IGNORE INTO admin_role_assignments(admin_id,admin_type_id,assigned_at) VALUES(?,?,?)",
            (outsider_id, outsider_tid, now))
perm = con.execute("SELECT id FROM permissions WHERE code=?", ("dashboard.view",)).fetchone()
if perm:
    con.execute("INSERT OR IGNORE INTO admin_permissions(admin_id,permission_id,granted_at) VALUES(?,?,?)",
                (outsider_id, perm["id"], now))

session_id = con.execute("SELECT id FROM academic_sessions WHERE is_current=1 AND active=1 LIMIT 1").fetchone()["id"]
class_id = con.execute("SELECT id FROM school_classes WHERE active=1 ORDER BY id LIMIT 1").fetchone()["id"]
student_row = con.execute(
    "SELECT s.id FROM students s JOIN student_enrolments se ON se.student_id=s.id "
    "WHERE se.class_id=? AND se.session_id=? AND se.active=1 AND s.active=1 LIMIT 1",
    (class_id, session_id)).fetchone()
student_id = student_row["id"]

# A pre-existing row carrying a legacy combo value from before the catalogue
# was fixed, to prove editing it doesn't silently rewrite its data.
legacy_item_id = con.execute(
    "INSERT INTO finance_fee_items(name,category,stage,amount,applicability,required,optional,active,"
    "created_at,updated_at,created_by,updated_by) VALUES(?,?,?,?,?,?,?,1,?,?,?,?)",
    ("Legacy Fee", "Miscellaneous", "All", 5000, "Second and third term only", 1, 0, now, now, admin_id, admin_id)).lastrowid
con.execute("INSERT INTO finance_fee_item_classes(fee_item_id,class_id,active,created_at,created_by) VALUES(?,?,1,?,?)",
            (legacy_item_id, class_id, now, admin_id))

item_id = con.execute(
    "INSERT INTO finance_fee_items(name,category,stage,amount,applicability,required,optional,active,"
    "created_at,updated_at,created_by,updated_by) VALUES(?,?,?,?,?,?,?,1,?,?,?,?)",
    ("Probe Tuition", "Tuition", "All", 40000, "First Term", 1, 0, now, now, admin_id, admin_id)).lastrowid
con.execute("INSERT INTO finance_fee_item_classes(fee_item_id,class_id,active,created_at,created_by) VALUES(?,?,1,?,?)",
            (item_id, class_id, now, admin_id))

assess_id = con.execute(
    "INSERT INTO finance_fee_assessments(student_id,session_id,category,amount,due_date,notes,created_by,"
    "created_at,active,fee_item_id,term) VALUES(?,?,?,?,?,?,?,?,1,?,?)",
    (student_id, session_id, "Tuition", 40000, None, "", admin_id, now, item_id, "First Term")).lastrowid

payment_id = con.execute(
    "INSERT INTO finance_payments(receipt_no,student_id,session_id,amount,category,method,reference,paid_at,"
    "recorded_by,status,notes,created_at,payer_name) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
    ("ZZFEE-0001", student_id, session_id, 15000, "Tuition", "Cash", "", now[:16], admin_id, "posted", "", now, "Probe Parent")).lastrowid
con.execute("INSERT INTO finance_payment_allocations(payment_id,assessment_id,amount,created_by,created_at) "
            "VALUES(?,?,?,?,?)", (payment_id, assess_id, 15000, admin_id, now))

# A second payment recorded by the cashier themselves, since the allocate
# route's own body-level check only ever admits the recorder of a payment
# (or someone with finance.view_all) - finance.record alone does not open
# every payment's allocation screen, only the ones that admin recorded.
cashier_payment_id = con.execute(
    "INSERT INTO finance_payments(receipt_no,student_id,session_id,amount,category,method,reference,paid_at,"
    "recorded_by,status,notes,created_at,payer_name) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
    ("ZZFEE-0002", student_id, session_id, 5000, "Tuition", "Cash", "", now[:16], cashier_id, "posted", "", now, "Probe Parent")).lastrowid
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


check("FINANCE_FEE_CATEGORIES includes the categories the admin asked for",
      {'School Fees', 'Uniform', 'Books & Stationery'} <= set(A.FINANCE_FEE_CATEGORIES))
check("FINANCE_FEE_APPLICABILITY lists each term independently",
      A.FINANCE_FEE_APPLICABILITY == ['Full Session', 'First Term', 'Second Term', 'Third Term', 'One-time'])
check("the two previously-orphaned finance routes are no longer bare admin.access",
      A.ADMIN_ENDPOINT_PERMISSIONS.get('admin_finance_student_account') == 'finance.view_own'
      and A.ADMIN_ENDPOINT_PERMISSIONS.get('admin_finance_payment_allocate') == 'finance.record')

with A.app.test_client() as c:
    c.post("/login", data={"username": "zz_fee_admin", "password": "FeePass!12345"})
    c.get("/admin/workspace/school")

    new_form = c.get("/admin/finance/fee-items/new").get_data(as_text=True)
    check("the new-item form renders Category as a real <select>", '<select name="category"' in new_form)
    for cat in A.FINANCE_FEE_CATEGORIES:
        needle = f'>{cat}<'.replace('&', '&amp;')
        check(f"Category dropdown offers '{cat}'", needle in new_form, "missing option")
    check("Category is no longer a free-text input", 'name="category" placeholder' not in new_form)
    for rule in A.FINANCE_FEE_APPLICABILITY:
        check(f"Billing Rule offers '{rule}' as its own option",
              f'value="{rule}"' in new_form and f'>{rule}<' in new_form)
    check("the old bundled combo option is gone",
          "Second and third term only" not in new_form and "First Term only" not in new_form)

    csrf = new_form.split('name="_csrf_token" value="')[1].split('"')[0]

    # Invalid category/applicability must be refused server-side, not just
    # hidden by the <select> - a scripted or replayed POST must not slip a
    # made-up category into the catalogue.
    bad = c.post("/admin/finance/fee-items/new", data={
        "_csrf_token": csrf, "name": "Bad Fee", "category": "Not A Real Category",
        "applicability": "Whenever", "amount": "100", "class_ids": [str(class_id)]})
    check("an invalid category is rejected server-side", "Select a valid fee category" in bad.get_data(as_text=True))
    exists = con2 = sqlite3.connect(DB)
    row = con2.execute("SELECT 1 FROM finance_fee_items WHERE name='Bad Fee'").fetchone()
    con2.close()
    check("the rejected fee item was never written to the database", row is None)

    # A legitimate create with a real category and an independent term.
    good = c.post("/admin/finance/fee-items/new", data={
        "_csrf_token": csrf, "name": "ZZ New Uniform", "category": "Uniform",
        "applicability": "Second Term", "amount": "7500", "class_ids": [str(class_id)]}, follow_redirects=True)
    con2 = sqlite3.connect(DB)
    con2.row_factory = sqlite3.Row
    created = con2.execute("SELECT category,applicability,amount FROM finance_fee_items WHERE name='ZZ New Uniform'").fetchone()
    con2.close()
    check("a valid submission creates the fee item with the chosen category/term",
          created is not None and created["category"] == "Uniform" and created["applicability"] == "Second Term")

    # Editing a legacy row must show its out-of-catalogue value rather than
    # silently swapping it for the first canonical option.
    legacy_edit = c.get(f"/admin/finance/fee-items/{legacy_item_id}/edit").get_data(as_text=True)
    check("editing a legacy row still shows its original (non-canonical) applicability value",
          'value="Second and third term only" checked' in legacy_edit)
    check("the legacy value is visibly flagged so an admin knows to update it",
          "(legacy)" in legacy_edit)

    # The Fee Structure page's Recent Assessments tab.
    fee_items_page = c.get("/admin/finance/fee-items").get_data(as_text=True)
    check("the assessed Tuition item shows Part Paid (N15,000 of N40,000 allocated)",
          'cr-finance-status partial">Part Paid' in fee_items_page)
    check("the assessment row links straight to that student's fee account",
          f'/admin/finance/students/{student_id}/account?session_id={session_id}' in fee_items_page)

    # The student account page itself.
    account_page = c.get(f"/admin/finance/students/{student_id}/account?session_id={session_id}").get_data(as_text=True)
    check("the student account page shows the correct outstanding balance", "₦25,000.00" in account_page)
    check("the student account page shows the Part Paid status", 'cr-finance-status partial">Part Paid' in account_page)
    check("the student account page lists the payment with an Allocate action",
          "ZZFEE-0001" in account_page and f'/admin/finance/payments/{payment_id}/allocate' in account_page)

    # The receipt page now bridges to allocation and to the account.
    receipt_page = c.get(f"/admin/finance/receipts/{payment_id}").get_data(as_text=True)
    check("the receipt page links to Allocate to Fees", f'/admin/finance/payments/{payment_id}/allocate' in receipt_page)
    check("the receipt page links to the Student Fee Account",
          f'/admin/finance/students/{student_id}/account' in receipt_page)

    # The dashboard's transaction ledger now links to the account too.
    dashboard_page = c.get("/admin/finance").get_data(as_text=True)
    check("the dashboard's recent transactions link to the student's fee account",
          f'/admin/finance/students/{student_id}/account' in dashboard_page)

with A.app.test_client() as c:
    c.post("/login", data={"username": "zz_fee_cashier", "password": "CashPass!12345"})
    c.get("/admin/workspace/school")
    r1 = c.get(f"/admin/finance/students/{student_id}/account?session_id={session_id}")
    check("a cashier with finance.view_own can open the student account page", r1.status_code == 200)
    r2 = c.get(f"/admin/finance/payments/{cashier_payment_id}/allocate")
    check("a cashier with finance.record can open the allocate page for a payment they recorded",
          r2.status_code == 200)

with A.app.test_client() as c:
    c.post("/login", data={"username": "zz_fee_outsider", "password": "OutPass!12345"})
    c.get("/admin/workspace/school")
    r3 = c.get(f"/admin/finance/students/{student_id}/account?session_id={session_id}")
    check("an admin with no finance permission is refused the student account page", r3.status_code == 403)
    r4 = c.get(f"/admin/finance/payments/{payment_id}/allocate")
    check("an admin with no finance permission is refused the allocate page", r4.status_code == 403)

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
