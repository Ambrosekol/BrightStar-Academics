"""Drive the mutating routes of the merged app and assert on database state.

Route parity only covers GETs. This exercises the writes that matter across the
new Phase 13 subsystems as well as the core assessment cycle, and checks the
resulting rows rather than just the HTTP status.
"""
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = r"C:\Users\user\Downloads\Crainbow_CBT_Phase3H_Live_Messages_Fix\crainbow"
SEED = r"C:\Users\user\Downloads\Latest Crainbow_CBT_Phase13_Current_Project\Crainbow_CBT_Phase13_Current_Project\cbt.db"
DB = os.path.join(HERE, "write13.db")

ADMIN_USER, ADMIN_PW = "zz_w13_admin", "W13Pass!23456"
STUDENT_PW = "W13Stu!23456"

shutil.copy(SEED, DB)
sys.path.insert(0, ROOT)
from werkzeug.security import generate_password_hash  # noqa: E402

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
now = datetime.now(timezone.utc).isoformat()
tid = con.execute("SELECT id FROM admin_types WHERE is_system=1 ORDER BY id LIMIT 1").fetchone()["id"]
con.execute("DELETE FROM admins WHERE username=?", (ADMIN_USER,))
admin_id = con.execute(
    "INSERT INTO admins(username,display_name,password_hash,admin_type_id,active,created_at,"
    "password_must_change) VALUES(?,?,?,?,1,?,0)",
    (ADMIN_USER, "W13 Probe", generate_password_hash(ADMIN_PW), tid, now)).lastrowid
con.execute("INSERT OR IGNORE INTO admin_role_assignments(admin_id,admin_type_id,assigned_at) "
            "VALUES(?,?,?)", (admin_id, tid, now))
for p in con.execute("SELECT id FROM permissions"):
    con.execute("INSERT OR IGNORE INTO admin_permissions(admin_id,permission_id,granted_at) "
                "VALUES(?,?,?)", (admin_id, p["id"], now))
other_admin = con.execute("SELECT id FROM admins WHERE id<>? AND active=1 ORDER BY id LIMIT 1",
                          (admin_id,)).fetchone()["id"]
# A student with a known password, in a class we can target.
stu = con.execute("""SELECT s.id,s.admission_no,se.class_id,se.session_id
                     FROM students s JOIN student_enrolments se
                       ON se.student_id=s.id AND se.active=1
                     WHERE s.active=1 ORDER BY s.id LIMIT 1""").fetchone()
con.execute("UPDATE students SET login_username=?,login_password_hash=?,account_active=1,"
            "password_must_change=0,active=1 WHERE id=?",
            (stu["admission_no"], generate_password_hash(STUDENT_PW), stu["id"]))
con.execute("DELETE FROM school_assessment_answers")
con.execute("DELETE FROM school_assessment_attempt_questions")
con.execute("DELETE FROM school_assessment_attempts")
con.commit()
STUDENT_ID, CLASS_ID, SESSION_ID = stu["id"], stu["class_id"], stu["session_id"]
STUDENT_ADMISSION = stu["admission_no"]
con.close()

os.environ["CRAINBOW_DB"] = DB
os.chdir(ROOT)
import app as A  # noqa: E402
import blueprints.student_portal.helpers as STUDENT  # noqa: E402

A.app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB}"
A.app.config["TESTING"] = True
with A.app.app_context():
    A.init_db()

CSRF = re.compile(rb'name="_csrf_token"[^>]*value="([^"]+)"')
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


def token(client, url):
    m = CSRF.search(client.get(url).get_data())
    return m.group(1).decode() if m else None


with A.app.test_client() as c:
    r = c.post("/login", data={"username": ADMIN_USER, "password": ADMIN_PW})
    check("admin login redirects", r.status_code == 302, str(r.status_code))
    check("last_login_at written",
          q("SELECT last_login_at FROM admins WHERE id=?", admin_id)[0]["last_login_at"] is not None)

    c.get("/admin/workspace/school")

    # ---------------------------------------------------------------- subjects
    t = token(c, "/admin/school/subjects/new")
    name = f"ZZ W13 Subject {os.getpid()}"
    c.post("/admin/school/subjects/new",
           data={"_csrf_token": t, "name": name, "code": "ZZW", "class_ids": str(CLASS_ID)})
    row = q("SELECT id FROM school_subjects WHERE name=?", name)
    check("subject created", bool(row))
    SUBJECT_ID = row[0]["id"] if row else None
    if SUBJECT_ID:
        check("class_subjects link created",
              bool(q("SELECT 1 FROM class_subjects WHERE subject_id=? AND class_id=?",
                     SUBJECT_ID, CLASS_ID)))

    # ------------------------------------------------------------- assessment
    AID = None
    if SUBJECT_ID:
        t = token(c, "/admin/school/tests/new")
        r = c.post("/admin/school/tests/new", data={
            "_csrf_token": t, "title": "ZZ W13 Test", "instructions": "probe",
            "class_id": str(CLASS_ID), "subject_id": str(SUBJECT_ID),
            "duration_minutes": "30", "session_id": str(SESSION_ID), "term": "First Term"})
        arow = q("SELECT id FROM school_assessments WHERE title='ZZ W13 Test' ORDER BY id DESC LIMIT 1")
        check("assessment created", bool(arow), f"status={r.status_code}")
        AID = arow[0]["id"] if arow else None

    if AID:
        detail = f"/admin/school/assessments/{AID}"
        for n in range(2):
            t = token(c, detail)
            c.post(f"{detail}/questions/new", data={
                "_csrf_token": t, "question_text": f"W13 question {n}?", "instruction": "",
                "option_a": "A", "option_b": "B", "option_c": "C", "option_d": "D",
                "correct_option": "1", "points": "1"})
        qs = q("SELECT id,correct_option FROM school_questions WHERE assessment_id=? ORDER BY sort_order", AID)
        check("two questions added", len(qs) == 2, f"got {len(qs)}")
        cnt = q("SELECT question_count FROM school_assessments WHERE id=?", AID)[0]["question_count"]
        check("question_count resynced", cnt == 2, f"got {cnt}")
        t = token(c, detail)
        c.post(f"{detail}/toggle", data={"_csrf_token": t})
        check("assessment activated",
              q("SELECT active FROM school_assessments WHERE id=?", AID)[0]["active"] == 1)

    # --------------------------------------------------------------- messaging
    t = token(c, f"/admin/administration/messages?with={other_admin}")
    before = q("SELECT COUNT(*) n FROM admin_messages")[0]["n"]
    r = c.post("/admin/administration/messages/send",
               data={"_csrf_token": t, "recipient_id": str(other_admin), "body": "w13 probe"})
    after = q("SELECT COUNT(*) n FROM admin_messages")[0]["n"]
    check("message sent", after == before + 1, f"{before}->{after} status={r.status_code}")

    # ----------------------------------------------------------------- library
    t = token(c, "/admin/library")
    c.post("/admin/library/books/new", data={
        "_csrf_token": t, "title": "ZZ W13 Book", "author": "Probe", "isbn": "",
        "publisher": "", "category": "Probe", "shelf": "A1",
        "publication_year": "2020", "copies": "3"})
    book = q("SELECT id,total_copies,available_copies FROM library_books WHERE title='ZZ W13 Book'")
    check("library book created", bool(book))
    if book:
        BOOK_ID = book[0]["id"]
        check("copies initialised", book[0]["total_copies"] == 3 and book[0]["available_copies"] == 3)
        t = token(c, "/admin/library")
        c.post("/admin/library/issue", data={
            "_csrf_token": t, "book_id": str(BOOK_ID), "member_type": "student",
            "member_id": str(STUDENT_ID), "days": "14"})
        loan = q("SELECT id,status FROM library_loans WHERE book_id=? AND status='borrowed'", BOOK_ID)
        check("book issued", bool(loan))
        avail = q("SELECT available_copies FROM library_books WHERE id=?", BOOK_ID)[0]["available_copies"]
        check("availability decremented", avail == 2, f"got {avail}")
        if loan:
            t = token(c, "/admin/library")
            c.post(f"/admin/library/loans/{loan[0]['id']}/return", data={"_csrf_token": t})
            avail = q("SELECT available_copies FROM library_books WHERE id=?", BOOK_ID)[0]["available_copies"]
            check("availability restored on return", avail == 3, f"got {avail}")
            check("loan marked returned",
                  q("SELECT status FROM library_loans WHERE id=?", loan[0]["id"])[0]["status"] == "returned")

    # ------------------------------------------------------------------ parent
    t = token(c, "/admin/school/parents/new")
    r = c.post("/admin/school/parents/new", data={
        "_csrf_token": t, "display_name": "ZZ W13 Parent", "email": "w13parent@example.com",
        "phone": "08011112222", "username": f"zz.w13.parent.{os.getpid()}",
        "student_ids": str(STUDENT_ID), "relationship": "Guardian"})
    prow = q("SELECT id FROM parent_accounts WHERE display_name='ZZ W13 Parent' ORDER BY id DESC LIMIT 1")
    check("parent account created", bool(prow), f"status={r.status_code}")
    if prow:
        check("parent linked to student",
              bool(q("SELECT 1 FROM parent_student_links WHERE parent_id=? AND student_id=? AND active=1",
                     prow[0]["id"], STUDENT_ID)))
        check("parent must change password",
              q("SELECT password_must_change FROM parent_accounts WHERE id=?",
                prow[0]["id"])[0]["password_must_change"] == 1)

    # ----------------------------------------------------------------- finance
    t = token(c, "/admin/finance/fee-items/new")
    r = c.post("/admin/finance/fee-items/new", data={
        "_csrf_token": t, "name": "ZZ W13 Fee", "category": "School Fees",
        "applicability": "Full Session", "required": "1", "amount": "50000",
        "class_ids": str(CLASS_ID), "notes": ""})
    fee = q("SELECT id,amount FROM finance_fee_items WHERE name='ZZ W13 Fee'")
    check("fee item created", bool(fee), f"status={r.status_code}")
    if fee:
        FEE_ID = fee[0]["id"]
        check("fee mapped to class",
              bool(q("SELECT 1 FROM finance_fee_item_classes WHERE fee_item_id=? AND class_id=? AND active=1",
                     FEE_ID, CLASS_ID)))
        t = token(c, "/admin/finance/fee-items")
        c.post("/admin/finance/assessments/new", data={
            "_csrf_token": t, "student_id": str(STUDENT_ID), "session_id": str(SESSION_ID),
            "fee_item_id": str(FEE_ID), "term": "Full Session", "due_date": "", "notes": ""})
        ass = q("SELECT id,amount FROM finance_fee_assessments WHERE student_id=? AND fee_item_id=? AND active=1",
                STUDENT_ID, FEE_ID)
        check("fee assessed to student", bool(ass))
        # A second identical assessment must be refused.
        t = token(c, "/admin/finance/fee-items")
        c.post("/admin/finance/assessments/new", data={
            "_csrf_token": t, "student_id": str(STUDENT_ID), "session_id": str(SESSION_ID),
            "fee_item_id": str(FEE_ID), "term": "Full Session", "due_date": "", "notes": ""})
        again = q("SELECT COUNT(*) n FROM finance_fee_assessments WHERE student_id=? AND fee_item_id=? AND active=1",
                  STUDENT_ID, FEE_ID)[0]["n"]
        check("duplicate fee assessment refused", again == 1, f"got {again}")

        t = token(c, "/admin/finance/payments/new")
        r = c.post("/admin/finance/payments/new", data={
            "_csrf_token": t, "student_id": str(STUDENT_ID), "session_id": str(SESSION_ID),
            "amount": "20000", "category": "School Fees", "method": "Cash",
            "reference": "", "paid_at": "", "notes": "", "payer_name": "W13 Payer"})
        pay = q("SELECT id,receipt_no,status FROM finance_payments WHERE payer_name='W13 Payer' ORDER BY id DESC LIMIT 1")
        check("payment recorded", bool(pay), f"status={r.status_code}")
        if pay and ass:
            PAY_ID = pay[0]["id"]
            check("receipt number allocated", bool(pay[0]["receipt_no"]), pay[0]["receipt_no"])
            check("payment posted", pay[0]["status"] == "posted", pay[0]["status"])
            t = token(c, f"/admin/finance/payments/{PAY_ID}/allocate")
            import json as _json
            c.post(f"/admin/finance/payments/{PAY_ID}/allocate", data={
                "_csrf_token": t, "allocations": _json.dumps({str(ass[0]["id"]): 20000})})
            alloc = q("SELECT amount FROM finance_payment_allocations WHERE payment_id=?", PAY_ID)
            check("payment allocated", len(alloc) == 1 and float(alloc[0]["amount"]) == 20000.0,
                  str([dict(a) for a in alloc]))
            # Over-allocating beyond the payment balance must be refused.
            t = token(c, f"/admin/finance/payments/{PAY_ID}/allocate")
            c.post(f"/admin/finance/payments/{PAY_ID}/allocate", data={
                "_csrf_token": t, "allocations": _json.dumps({str(ass[0]["id"]): 999999})})
            n_alloc = q("SELECT COUNT(*) n FROM finance_payment_allocations WHERE payment_id=?", PAY_ID)[0]["n"]
            check("over-allocation refused", n_alloc == 1, f"got {n_alloc}")

            t = token(c, f"/admin/finance/receipts/{PAY_ID}")
            c.post(f"/admin/finance/payments/{PAY_ID}/void",
                   data={"_csrf_token": t, "reason": "probe void"})
            check("payment voided",
                  q("SELECT status FROM finance_payments WHERE id=?", PAY_ID)[0]["status"] == "voided")
            t = token(c, f"/admin/finance/receipts/{PAY_ID}")
            c.post(f"/admin/finance/payments/{PAY_ID}/void",
                   data={"_csrf_token": t, "reason": "again"})
            check("second void refused",
                  q("SELECT COUNT(*) n FROM finance_payments WHERE id=? AND status='voided'",
                    PAY_ID)[0]["n"] == 1)

    # ------------------------------------------------------- website + enquiry
    t = token(c, "/admin/school/website/news/new")
    c.post("/admin/school/website/news/new", data={
        "_csrf_token": t, "title": "ZZ W13 News", "excerpt": "probe",
        "body": "probe body", "published": "1"})
    news = q("SELECT id,slug,published,published_at FROM school_public_news WHERE title='ZZ W13 News'")
    check("news article created", bool(news))
    if news:
        check("slug generated", bool(news[0]["slug"]), str(news[0]["slug"]))
        check("published_at stamped on publish", news[0]["published_at"] is not None)

    # ------------------------------------------------------ result workflow
    if AID and SUBJECT_ID:
        t = token(c, "/admin/school/results/manual/new")
        c.post("/admin/school/results/manual/new", data={
            "_csrf_token": t, "class_id": str(CLASS_ID), "session_id": str(SESSION_ID),
            "student_id": str(STUDENT_ID), "subject_id": str(SUBJECT_ID),
            "term": "Full Session", "took_test": "yes",
            "test_score": "15", "test_max": "20",
            "exam_score": "60", "exam_max": "80"})
        entered = q("""SELECT id,status,component_name FROM school_student_results
                       WHERE student_id=? AND subject_id=? AND source_type='manual'
                       ORDER BY id""", STUDENT_ID, SUBJECT_ID)
        check("manual results entered", len(entered) == 2, f"got {len(entered)}")
        if entered:
            RID = entered[0]["id"]
            check("entered status", entered[0]["status"] == "entered", entered[0]["status"])
            # Approving before verifying must be refused.
            t = token(c, "/admin/school/results")
            c.post(f"/admin/school/results/{RID}/workflow",
                   data={"_csrf_token": t, "action": "approve"})
            check("approve before verify refused",
                  q("SELECT status FROM school_student_results WHERE id=?", RID)[0]["status"] == "entered")
            for action, expect in (("verify", "verified"), ("approve", "approved"), ("release", "released")):
                t = token(c, "/admin/school/results")
                c.post(f"/admin/school/results/{RID}/workflow",
                       data={"_csrf_token": t, "action": action, "reason": "probe"})
                got = q("SELECT status FROM school_student_results WHERE id=?", RID)[0]["status"]
                check(f"result {action} -> {expect}", got == expect, f"got {got}")
            events = q("SELECT COUNT(*) n FROM result_workflow_events WHERE result_id=?", RID)[0]["n"]
            check("workflow events recorded", events >= 4, f"got {events}")

# -------------------------------------------------------------- student side
if AID:
    with A.app.test_client() as c:
        r = c.post("/login", data={"username": STUDENT_ADMISSION, "password": STUDENT_PW})
        check("student login redirects", r.status_code == 302, str(r.status_code))
        t = token(c, f"/student/assessments/{AID}")
        r = c.post(f"/student/assessments/{AID}/start", data={"_csrf_token": t})
        att = q("SELECT id,status FROM school_assessment_attempts WHERE student_id=? AND assessment_id=?",
                STUDENT_ID, AID)
        check("attempt created", bool(att), f"status={r.status_code}")
        if att:
            attempt_id = att[0]["id"]
            frozen = q("SELECT COUNT(*) n FROM school_assessment_attempt_questions WHERE attempt_id=?",
                       attempt_id)[0]["n"]
            check("question snapshot frozen", frozen == 2, f"got {frozen}")
            qs = q("""SELECT question_id,correct_option FROM school_assessment_attempt_questions
                      WHERE attempt_id=? ORDER BY question_order""", attempt_id)
            page = f"/student/assessments/{AID}"
            t = token(c, page)
            c.post(f"{page}/answer", data={
                "_csrf_token": t, "attempt_id": str(attempt_id),
                "question_id": str(qs[0]["question_id"]),
                "option_index": str(qs[0]["correct_option"]), "next_q": "2"})
            wrong = 0 if qs[1]["correct_option"] != 0 else 1
            t = token(c, page)
            c.post(f"{page}/answer", data={
                "_csrf_token": t, "attempt_id": str(attempt_id),
                "question_id": str(qs[1]["question_id"]),
                "option_index": str(wrong), "next_q": "2", "submit_assessment": "1"})
            saved = q("SELECT COUNT(*) n FROM school_assessment_answers WHERE attempt_id=?",
                      attempt_id)[0]["n"]
            check("answers recorded", saved == 2, f"got {saved}")
            done = q("SELECT status,score,max_score,percentage FROM school_assessment_attempts WHERE id=?",
                     attempt_id)[0]
            check("attempt submitted", done["status"] == "submitted", f"got {done['status']}")
            check("graded 1 of 2", done["score"] == 1 and done["max_score"] == 2,
                  f"score={done['score']} max={done['max_score']}")
            check("percentage computed", abs((done["percentage"] or 0) - 50.0) < 0.01,
                  f"got {done['percentage']}")
            res = q("""SELECT COUNT(*) n FROM school_student_results
                       WHERE student_id=? AND assessment_id=?""", STUDENT_ID, AID)[0]["n"]
            check("result row written once", res == 1, f"got {res}")
            with A.app.app_context():
                STUDENT.student_assessment_grade(attempt_id, auto=False)
            res2 = q("""SELECT COUNT(*) n FROM school_student_results
                        WHERE student_id=? AND assessment_id=?""", STUDENT_ID, AID)[0]["n"]
            check("re-grade is idempotent", res2 == 1, f"got {res2}")

print()
failed = [r for r in results if not r[1]]
print(f"{len(results)-len(failed)}/{len(results)} write-path checks passed")
sys.exit(1 if failed else 0)
