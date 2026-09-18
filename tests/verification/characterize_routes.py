"""Characterise every reachable GET route, for before/after diffing.

    python char13.py ref    # their pristine raw-SQL Phase13 app
    python char13.py new    # my converted SQLAlchemy app

Both runs get an identical, freshly seeded copy of the Phase13 database and
known credentials for each of the five identities (admin, student, candidate,
parent, anonymous), so the two JSON outputs are directly comparable.
"""
import hashlib
import importlib
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REF_ROOT = r"C:\Users\user\Downloads\Latest Crainbow_CBT_Phase13_Current_Project\Crainbow_CBT_Phase13_Current_Project"
NEW_ROOT = r"C:\Users\user\Downloads\Crainbow_CBT_Phase3H_Live_Messages_Fix\crainbow"
SEED_DB = os.path.join(REF_ROOT, "cbt.db")

MODE = sys.argv[1] if len(sys.argv) > 1 else "new"
ROOT = REF_ROOT if MODE == "ref" else NEW_ROOT
TEST_DB = os.path.join(HERE, f"c13_{MODE}.db")
OUT = os.path.join(HERE, f"c13_{MODE}.json")

ADMIN_USER, ADMIN_PW = "zz_c13_admin", "C13Pass!23456"
STUDENT_PW = "C13Stu!23456"
CANDIDATE_PW = "C13Cand!23456"
PARENT_USER, PARENT_PW = "zz_c13_parent", "C13Par!23456"


def seed():
    shutil.copy(SEED_DB, TEST_DB)
    sys.path.insert(0, ROOT)
    from werkzeug.security import generate_password_hash

    con = sqlite3.connect(TEST_DB)
    con.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).isoformat()
    ids = {}

    # --- a Super Admin holding every permission ---
    tid = con.execute("SELECT id FROM admin_types WHERE is_system=1 ORDER BY id LIMIT 1").fetchone()["id"]
    con.execute("DELETE FROM admins WHERE username=?", (ADMIN_USER,))
    aid = con.execute(
        "INSERT INTO admins(username,display_name,password_hash,admin_type_id,active,created_at,"
        "password_must_change) VALUES(?,?,?,?,1,?,0)",
        (ADMIN_USER, "C13 Probe", generate_password_hash(ADMIN_PW), tid, now)).lastrowid
    con.execute("INSERT OR IGNORE INTO admin_role_assignments(admin_id,admin_type_id,assigned_at) "
                "VALUES(?,?,?)", (aid, tid, now))
    for p in con.execute("SELECT id FROM permissions"):
        con.execute("INSERT OR IGNORE INTO admin_permissions(admin_id,permission_id,granted_at) "
                    "VALUES(?,?,?)", (aid, p["id"], now))
    ids["admin_id"] = aid

    stu = con.execute("SELECT id,admission_no FROM students ORDER BY id LIMIT 1").fetchone()
    if stu:
        con.execute("UPDATE students SET login_username=?,login_password_hash=?,account_active=1,"
                    "password_must_change=0,active=1 WHERE id=?",
                    (stu["admission_no"], generate_password_hash(STUDENT_PW), stu["id"]))
        ids["student_admission"] = stu["admission_no"]
        ids["student_id"] = stu["id"]

    cand = con.execute("SELECT id,candidate_code FROM candidates ORDER BY id LIMIT 1").fetchone()
    if cand:
        con.execute("UPDATE candidates SET password_hash=?,active=1 WHERE id=?",
                    (generate_password_hash(CANDIDATE_PW), cand["id"]))
        ids["candidate_code"] = cand["candidate_code"]

    # --- a parent linked to that student ---
    con.execute("DELETE FROM parent_accounts WHERE username=?", (PARENT_USER,))
    pid = con.execute(
        "INSERT INTO parent_accounts(username,display_name,password_hash,active,"
        "password_must_change,created_at) VALUES(?,?,?,1,0,?)",
        (PARENT_USER, "C13 Parent", generate_password_hash(PARENT_PW), now)).lastrowid
    if stu:
        con.execute("INSERT OR IGNORE INTO parent_student_links(parent_id,student_id,active,created_at) "
                    "VALUES(?,?,1,?)", (pid, stu["id"], now))
    ids["parent_id"] = pid
    con.commit()
    con.close()
    return ids


def sample_ids():
    con = sqlite3.connect(TEST_DB)

    def first(sql, default=1):
        try:
            r = con.execute(sql).fetchone()
        except sqlite3.Error:
            return default
        return r[0] if r else default

    p = {
        "aid": first("SELECT id FROM attempts ORDER BY id LIMIT 1"),
        "cid": first("SELECT id FROM candidates ORDER BY id LIMIT 1"),
        "sid": first("SELECT id FROM students ORDER BY id LIMIT 1"),
        "student_id": first("SELECT id FROM students ORDER BY id LIMIT 1"),
        "assignment_id": first("SELECT id FROM school_assignments ORDER BY id LIMIT 1"),
        "assessment_id": first("SELECT id FROM school_assessments ORDER BY id LIMIT 1"),
        "question_id": first("SELECT id FROM school_questions ORDER BY id LIMIT 1"),
        "subject_id": first("SELECT id FROM school_subjects ORDER BY id LIMIT 1"),
        "class_id": first("SELECT id FROM school_classes ORDER BY id LIMIT 1"),
        "rid": first("SELECT id FROM admin_types WHERE is_system=0 ORDER BY id LIMIT 1"),
        "nid": first("SELECT id FROM admin_notifications ORDER BY id LIMIT 1"),
        "paper_id": first("SELECT id FROM candidate_papers ORDER BY id LIMIT 1"),
        "item_id": first("SELECT id FROM finance_fee_items ORDER BY id LIMIT 1"),
        "payment_id": first("SELECT id FROM finance_payments ORDER BY id LIMIT 1"),
        "book_id": first("SELECT id FROM library_books ORDER BY id LIMIT 1"),
        "pid": first("SELECT id FROM parent_accounts ORDER BY id LIMIT 1"),
        "feedback_id": first("SELECT id FROM parent_feedback ORDER BY id LIMIT 1"),
        "project_id": first("SELECT id FROM school_projects ORDER BY id LIMIT 1"),
        "run_id": first("SELECT id FROM academic_promotion_runs ORDER BY id LIMIT 1"),
        "result_id": first("SELECT id FROM school_student_results ORDER BY id LIMIT 1"),
        "history_id": first("SELECT id FROM student_enrollment_history ORDER BY id LIMIT 1"),
        "news_id": first("SELECT id FROM school_public_news ORDER BY id LIMIT 1"),
        "eid": first("SELECT id FROM school_public_enquiries ORDER BY id LIMIT 1"),
        "config_id": first("SELECT id FROM entrance_bank_configs ORDER BY id LIMIT 1"),
        "bid": first("SELECT bank_id FROM examinations ORDER BY id LIMIT 1", "x"),
        "slug": first("SELECT slug FROM school_public_news ORDER BY id LIMIT 1", "none"),
        "qid": 1, "token": "invalid-token",
    }
    con.close()
    return p


TOKEN_RE = re.compile(rb'(_csrf_token"[^>]*value=")[^"]*"')
ISO_RE = re.compile(rb"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(\+00:00|Z)?")
TIME_RE = re.compile(rb"\b\d{1,2}:\d{2}(:\d{2})?\s*(AM|PM|am|pm)?\b")
CSRF_META = re.compile(rb'(csrf[_-]?token[^"]{0,20}"\s*content=")[^"]*"', re.I)


# The token is also embedded in inline JavaScript, e.g.
# fd.append('_csrf_token','<token>'), which the attribute patterns above miss.
CSRF_JS = re.compile(rb"(_csrf_token'\s*,\s*')[^']*'")


def normalize(body):
    body = TOKEN_RE.sub(rb'\1REDACTED"', body)
    body = CSRF_META.sub(rb"\1REDACTED\"", body)
    body = CSRF_JS.sub(rb"\1REDACTED'", body)
    body = ISO_RE.sub(b"<TS>", body)
    body = TIME_RE.sub(b"<TIME>", body)
    return body


def crawl():
    ids = seed()
    params = sample_ids()

    os.environ["CRAINBOW_DB"] = TEST_DB
    sys.path.insert(0, ROOT)
    os.chdir(ROOT)
    mod = importlib.import_module("app")
    mod.DB = TEST_DB
    if MODE == "new":
        mod.app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{TEST_DB}"
    flask_app = mod.app
    flask_app.config["TESTING"] = True
    try:
        if MODE == "new":
            with flask_app.app_context():
                mod.init_db()
        else:
            mod.init_db()
    except Exception as exc:
        print(f"init_db warning: {type(exc).__name__}: {exc}")

    results = {}

    def record(client, label, url):
        try:
            r = client.get(url, follow_redirects=False)
            body = normalize(r.get_data())
            results[label] = {"status": r.status_code,
                              "location": r.headers.get("Location"),
                              "hash": hashlib.sha256(body).hexdigest()[:16],
                              "len": len(body)}
        except Exception as exc:
            results[label] = {"error": f"{type(exc).__name__}: {exc}"}

    def fill(url):
        out = url
        for k, v in params.items():
            out = re.sub(r"<[^:>]*:?" + k + r">", str(v), out)
        return out

    gets = [str(r) for r in flask_app.url_map.iter_rules() if "GET" in r.methods]

    # --- anonymous: public site and login surfaces ---
    with flask_app.test_client() as c:
        for url in sorted(gets):
            if url.startswith(("/admin", "/student", "/candidate", "/parent", "/static")):
                continue
            if "logout" in url:
                continue
            f = fill(url)
            if "<" in f:
                continue
            record(c, f"anon {url}", f)

    # --- admin ---
    with flask_app.test_client() as c:
        r = c.post("/login", data={"username": ADMIN_USER, "password": ADMIN_PW})
        results["admin login"] = {"status": r.status_code, "location": r.headers.get("Location")}
        for url in sorted(u for u in gets if u.startswith("/admin")):
            # /admin/home deliberately clears the workspace selection, and the
            # workspace switchers are asserted explicitly before each request,
            # so sweeping them here would desynchronise everything after.
            if "logout" in url or url == "/admin/home" or url.startswith("/admin/workspace"):
                continue
            f = fill(url)
            if "<" in f:
                continue
            c.get("/admin/workspace/school" if url.startswith("/admin/school")
                  else "/admin/workspace/entrance")
            record(c, f"admin {url}", f)

    # --- student ---
    if ids.get("student_admission"):
        with flask_app.test_client() as c:
            r = c.post("/login", data={"username": ids["student_admission"], "password": STUDENT_PW})
            results["student login"] = {"status": r.status_code, "location": r.headers.get("Location")}
            for url in sorted(u for u in gets if u.startswith("/student")):
                if "logout" in url:
                    continue
                f = fill(url)
                if "<" in f:
                    continue
                record(c, f"student {url}", f)

    # --- candidate ---
    if ids.get("candidate_code"):
        with flask_app.test_client() as c:
            r = c.post("/login", data={"username": ids["candidate_code"], "password": CANDIDATE_PW})
            results["candidate login"] = {"status": r.status_code, "location": r.headers.get("Location")}
            for url in sorted(u for u in gets if u.startswith("/candidate")) + ["/exam", "/result", "/review"]:
                if "logout" in url:
                    continue
                f = fill(url)
                if "<" in f:
                    continue
                record(c, f"candidate {url}", f)

    # --- parent ---
    with flask_app.test_client() as c:
        r = c.post("/login", data={"username": PARENT_USER, "password": PARENT_PW})
        results["parent login"] = {"status": r.status_code, "location": r.headers.get("Location")}
        for url in sorted(u for u in gets if u.startswith("/parent")):
            if "logout" in url:
                continue
            f = fill(url)
            if "<" in f:
                continue
            record(c, f"parent {url}", f)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=1, sort_keys=True)
    err = sum(1 for v in results.values() if "error" in v)
    print(f"{MODE}: {len(results)} routes recorded ({len(results)-err} ok, {err} errored) -> {OUT}")


if __name__ == "__main__":
    crawl()
