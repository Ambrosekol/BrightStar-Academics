"""Exercise the admissions registration and the promotion engine.

These are the two largest converted routes; admissions writes across six tables
in one transaction and promotion rewrites every enrolment in the school.
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
DB = os.path.join(HERE, "write13b.db")

ADMIN_USER, ADMIN_PW = "zz_w13b_admin", "W13bPass!2345"

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
    (ADMIN_USER, "W13b Probe", generate_password_hash(ADMIN_PW), tid, now)).lastrowid
con.execute("INSERT OR IGNORE INTO admin_role_assignments(admin_id,admin_type_id,assigned_at) "
            "VALUES(?,?,?)", (admin_id, tid, now))
for p in con.execute("SELECT id FROM permissions"):
    con.execute("INSERT OR IGNORE INTO admin_permissions(admin_id,permission_id,granted_at) "
                "VALUES(?,?,?)", (admin_id, p["id"], now))
CLASS_ID = con.execute("SELECT id FROM school_classes WHERE active=1 ORDER BY level_order LIMIT 1").fetchone()["id"]
NEXT_CLASS_ID = con.execute("SELECT id FROM school_classes WHERE active=1 ORDER BY level_order LIMIT 1 OFFSET 1").fetchone()["id"]
CUR_SESSION = con.execute("SELECT id FROM academic_sessions WHERE is_current=1 AND active=1 ORDER BY id DESC LIMIT 1").fetchone()
CUR_SESSION_ID = CUR_SESSION["id"] if CUR_SESSION else con.execute(
    "SELECT id FROM academic_sessions WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()["id"]
# A destination session for the promotion run.
NEXT_SESSION_ID = con.execute(
    "INSERT INTO academic_sessions(name,is_current,active,created_at) VALUES(?,0,1,?)",
    (f"ZZ W13b Next {os.getpid()}", now)).lastrowid
con.commit()
con.close()

os.environ["CRAINBOW_DB"] = DB
os.chdir(ROOT)
import app as A  # noqa: E402

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
    body = client.get(url).get_data()
    m = CSRF.search(body)
    return (m.group(1).decode() if m else None), len(body)


with A.app.test_client() as c:
    c.post("/login", data={"username": ADMIN_USER, "password": ADMIN_PW})
    c.get("/admin/workspace/school")

    # ------------------------------------------------------------- admissions
    t, page_len = token(c, "/admin/school/students/new")
    check("registration form renders", t is not None and page_len > 2000,
          f"csrf={bool(t)} len={page_len}")

    before = q("SELECT COUNT(*) n FROM students")[0]["n"]
    r = c.post("/admin/school/students/new", data={
        "_csrf_token": t,
        "first_name": "Zz", "middle_name": "W13b", "last_name": f"Probe{os.getpid()}",
        "gender": "Female", "date_of_birth": "2015-04-02",
        "state_of_origin": "Lagos",
        "blood_group": "O+", "genotype": "AA",
        "guardian_name": "W13b Guardian", "guardian_phone": "08033334444",
        "guardian_email": "w13b@example.com",
        "class_id": str(CLASS_ID),
        "previous_school": "Probe Primary", "reason_for_leaving": "Relocation",
        "religion": "Christianity", "denomination": "Anglican",
        "immunization": "Complete", "food_allergies": "None", "drug_allergies": "None",
        "other_health_challenges": "None", "disability": "No",
        "parent_signature": "W13b Guardian", "parent_signature_date": "2026-09-01",
        "father_guardian_name": "W13b Father", "father_guardian_address": "1 Probe Road",
        "father_guardian_office_phone": "012345678", "father_guardian_mobile": "08055556666",
        "father_guardian_email": "w13bfather@example.com",
        "mother_name": "W13b Mother", "mother_address": "1 Probe Road",
        "mother_office_phone": "012345679", "mother_email": "w13bmother@example.com",
        "mother_occupation": "Teacher",
    })
    after = q("SELECT COUNT(*) n FROM students")[0]["n"]
    check("student registered", after == before + 1, f"{before}->{after} status={r.status_code}")

    srow = q("SELECT id,student_number,admission_no,student_number_source,school_id "
             "FROM students ORDER BY id DESC LIMIT 1")
    if srow and after == before + 1:
        NEW_SID = srow[0]["id"]
        check("student number generated", bool(srow[0]["student_number"]), str(srow[0]["student_number"]))
        check("admission_no mirrors student number",
              srow[0]["admission_no"] == srow[0]["student_number"])
        check("source recorded as generated", srow[0]["student_number_source"] == "generated")
        check("student attached to school", srow[0]["school_id"] is not None)
        check("allocation ledger bound to student",
              bool(q("SELECT 1 FROM student_number_allocations WHERE student_id=? AND active=1", NEW_SID)))
        check("login account provisioned",
              q("SELECT login_username,login_password_hash FROM students WHERE id=?",
                NEW_SID)[0]["login_password_hash"] is not None)
        check("enrolled in chosen class",
              bool(q("SELECT 1 FROM student_enrolments WHERE student_id=? AND class_id=? AND active=1",
                     NEW_SID, CLASS_ID)))
        check("admission profile written",
              bool(q("SELECT 1 FROM student_admission_profiles WHERE student_id=?", NEW_SID)))
        contacts = q("SELECT role,parent_id FROM student_admission_contacts WHERE student_id=? ORDER BY role", NEW_SID)
        check("both admission contacts written", len(contacts) == 2, f"got {len(contacts)}")
        check("contacts linked to parent accounts",
              all(x["parent_id"] is not None for x in contacts))
        links = q("SELECT COUNT(*) n FROM parent_student_links WHERE student_id=? AND active=1", NEW_SID)[0]["n"]
        check("parent accounts linked to student", links == 2, f"got {links}")

        # Registering again must allocate the next number, not reuse it.
        t2, _ = token(c, "/admin/school/students/new")
        c.post("/admin/school/students/new", data={
            "_csrf_token": t2, "first_name": "Zz", "last_name": f"Probe2{os.getpid()}",
            "gender": "Male", "class_id": str(CLASS_ID), "state_of_origin": "Kano"})
        nums = [r["student_number"] for r in
                q("SELECT student_number FROM students WHERE student_number IS NOT NULL")]
        check("student numbers unique", len(nums) == len(set(nums)), f"{len(nums)} numbers")

        # The same parent email must be reused, not duplicated.
        parents_named = q("SELECT COUNT(*) n FROM parent_accounts WHERE lower(email)=?",
                          "w13bfather@example.com")[0]["n"]
        check("parent account reused by email", parents_named == 1, f"got {parents_named}")

    # -------------------------------------------------------------- promotion
    t, _ = token(c, "/admin/school/promotion/progressions")
    c.post("/admin/school/promotion/progressions", data={
        "_csrf_token": t, "action": "save",
        "from_class_id": str(CLASS_ID), "to_class_id": str(NEXT_CLASS_ID)})
    check("progression rule saved",
          bool(q("SELECT 1 FROM school_class_progressions WHERE from_class_id=? AND to_class_id=? AND active=1",
                 CLASS_ID, NEXT_CLASS_ID)))

    t, _ = token(c, "/admin/school/promotion")
    c.post("/admin/school/promotion", data={
        "_csrf_token": t, "action": "generate",
        "from_session_id": str(CUR_SESSION_ID), "to_session_id": str(NEXT_SESSION_ID)})
    run = q("SELECT id,status FROM academic_promotion_runs ORDER BY id DESC LIMIT 1")
    check("promotion draft created", bool(run) and run[0]["status"] == "draft",
          str([dict(r) for r in run]))
    if run:
        RUN_ID = run[0]["id"]
        items = q("SELECT COUNT(*) n FROM academic_promotion_items WHERE run_id=?", RUN_ID)[0]["n"]
        check("promotion items seeded", items > 0, f"got {items}")
        seeded = q("""SELECT COUNT(*) n FROM academic_promotion_items
                      WHERE run_id=? AND source_class_id=? AND proposed_class_id=?""",
                   RUN_ID, CLASS_ID, NEXT_CLASS_ID)[0]["n"]
        check("proposed class taken from progression rule", seeded > 0, f"got {seeded}")

        # Committing a draft must be refused; it has to be approved first.
        t, _ = token(c, f"/admin/school/promotion/{RUN_ID}")
        c.post(f"/admin/school/promotion/{RUN_ID}",
               data={"_csrf_token": t, "action": "commit"})
        check("commit before approval refused",
              q("SELECT status FROM academic_promotion_runs WHERE id=?", RUN_ID)[0]["status"] == "draft")

        t, _ = token(c, f"/admin/school/promotion/{RUN_ID}")
        c.post(f"/admin/school/promotion/{RUN_ID}",
               data={"_csrf_token": t, "action": "approve"})
        status = q("SELECT status FROM academic_promotion_runs WHERE id=?", RUN_ID)[0]["status"]
        check("promotion approved", status == "approved", f"got {status}")

        if status == "approved":
            before_enrol = q("SELECT COUNT(*) n FROM student_enrolments WHERE session_id=?",
                             NEXT_SESSION_ID)[0]["n"]
            t, _ = token(c, f"/admin/school/promotion/{RUN_ID}")
            c.post(f"/admin/school/promotion/{RUN_ID}",
                   data={"_csrf_token": t, "action": "commit"})
            final = q("SELECT status FROM academic_promotion_runs WHERE id=?", RUN_ID)[0]["status"]
            check("promotion committed", final == "committed", f"got {final}")
            after_enrol = q("SELECT COUNT(*) n FROM student_enrolments WHERE session_id=?",
                            NEXT_SESSION_ID)[0]["n"]
            check("new-session enrolments created", after_enrol > before_enrol,
                  f"{before_enrol}->{after_enrol}")
            check("promotion items record their enrolment",
                  q("""SELECT COUNT(*) n FROM academic_promotion_items
                       WHERE run_id=? AND committed_enrolment_id IS NOT NULL""",
                    RUN_ID)[0]["n"] > 0)
            check("enrollment history written",
                  q("SELECT COUNT(*) n FROM student_enrollment_history WHERE session_id=?",
                    NEXT_SESSION_ID)[0]["n"] > 0)
            # Committing twice must be refused.
            t, _ = token(c, f"/admin/school/promotion/{RUN_ID}")
            c.post(f"/admin/school/promotion/{RUN_ID}",
                   data={"_csrf_token": t, "action": "commit"})
            dupes = q("SELECT COUNT(*) n FROM student_enrolments WHERE session_id=?",
                      NEXT_SESSION_ID)[0]["n"]
            check("second commit creates no duplicate enrolments", dupes == after_enrol,
                  f"{after_enrol}->{dupes}")

print()
failed = [r for r in results if not r[1]]
print(f"{len(results)-len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
