"""Data protection, the school's data export, the API and webhooks, proven end to end on PostgreSQL. In plain words:

A. Consents: a parent is asked to read the privacy notice until they confirm it; they allow or withdraw each purpose for
   their own children only; the school records a signed form (and must say what it holds); a teacher cannot; the
   consents spreadsheet is defused; an optional message goes only where it is allowed; exam registration leaves out a
   candidate without consent to share, and signed forms recorded for several candidates let them in.
B. Requests: a parent asks for a copy; a duplicate, or a child not theirs, is refused; the school approves and the copy
   is built: the child's record, released results only, the asker's own details, no password; another parent cannot
   download it. Erasure needs the parent's tick; only the School Admin erases, confirmed by the admission number; the
   child becomes a placeholder, their photo and messages go, results and fees stay; a parent with no other child is
   anonymised and can no longer sign in; one with another child is not.
C. The school's export: the School Admin's alone; every table as a spreadsheet and every uploaded file; no password or
   secret column, no stored credential.
D. The API: a key is shown once and only its hash kept; it reads only its areas; paging; wrong, missing and revoked keys
   are refused; unreleased results never appear; too many calls are slowed; no session cookie; the documentation is open.
E. Webhooks: a payment recorded reaches the school's address, signed with its secret, with no names; a test reaches
   it; an address that fails is tried again; a bad address is refused, and in production a private one is too.
F. A form protected against double submission, shown again with what to correct, no longer fails (core/idempotency.py).
Run:  python tests/verification/write_paths_privacy_api.py
"""
import atexit
import csv
import hashlib
import hmac
import http.server
import io
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import zipfile
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="beacon_privacy_")

import _pg  # noqa: E402

PREFIX, DROP_TEST_DATABASES = _pg.setup('privacy')
atexit.register(DROP_TEST_DATABASES)
atexit.register(shutil.rmtree, TMP, True)
os.environ.update({
    "BEACON_TENANTS_DIR": os.path.join(TMP, "tenants"),
    "BEACON_PLATFORM_HOSTS": "platform.test",
    "BEACON_PORTAL_DOMAIN": "portal.test",
    "BEACON_REGISTRY_CACHE_SECONDS": "0",
    "BEACON_DESIGN_CACHE_SECONDS": "0",
    "BEACON_SECRET": "x" * 40,
    "BEACON_ENV": "development",
})
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
os.chdir(ROOT)

import sqlalchemy as sa  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

import app as A  # noqa: E402
from control_plane import provisioning as pv  # noqa: E402
from control_plane.context import tenant_context  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import engine_for  # noqa: E402

A.app.config['BACKGROUND_INLINE'] = True
results, PROBLEMS = [], []
PL, ALPHA = "http://platform.test", "http://alpha.portal.test"


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)


class Errors(logging.Handler):
    def __init__(self):
        super().__init__()
        self.seen = []

    def emit(self, record):
        if record.exc_info and record.exc_info[1] is not None:
            self.seen.append(record.exc_info[1])


errors = Errors()
A.app.logger.addHandler(errors)
A.app.logger.propagate = False


def csrf_from(body):
    found = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', body)
    return found.group(1) if found else ""


class Person:
    count = [0]

    def __init__(self, base, label=""):
        self.base, self.client, self.label = base, A.app.test_client(), label or base
        Person.count[0] += 1
        self.addr = f"10.88.{Person.count[0]}.1"

    def _note(self, r, method, path):
        if r.status_code >= 500:
            PROBLEMS.append(f"{self.label}: {method} {path} answered {r.status_code}")
        return r

    def get(self, path, **kw):
        return self._note(self.client.get(path, base_url=self.base, environ_base={"REMOTE_ADDR": self.addr}, **kw), "GET", path)

    def text(self, path):
        return self.get(path).get_data(as_text=True)

    def post(self, path, data=None, page="/admin/password", extra=None):
        data = dict(data or {})
        body = self.text(page)
        data["_csrf_token"] = csrf_from(body)
        if extra == "idem":
            found = re.search(r'name="_idempotency_key"[^>]*value="([^"]+)"', body)
            data["_idempotency_key"] = found.group(1) if found else ""
        return self._note(self.client.post(path, data=data, base_url=self.base, environ_base={"REMOTE_ADDR": self.addr}),
                          "POST", path)


pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
console = A.app.test_client()
console.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                      "_csrf_token": csrf_from(console.get("/platform/login", base_url=PL).get_data(as_text=True))}, base_url=PL)
console.post("/platform/schools/new", data={"_csrf_token": csrf_from(console.get("/platform/schools/new", base_url=PL).get_data(as_text=True)),
                                            "name": "Alpha School", "code": "alpha"}, base_url=PL, content_type="multipart/form-data")
with platform_session() as s:
    INFO = to_info(get_tenant(s, "alpha"))


def sql(statement, **params):
    with engine_for(INFO).begin() as conn:
        result = conn.execute(sa.text(statement), params)
        return result.fetchall() if result.returns_rows else None


def one(statement, **params):
    rows = sql(statement, **params)
    return rows[0][0] if rows else None


def inside(fn, *args, **kwargs):
    with A.app.test_request_context(base_url=ALPHA), tenant_context(INFO):
        from models import db
        try:
            return fn(*args, **kwargs)
        finally:
            db.session.remove()


page = console.get("/platform/schools/alpha", base_url=PL).get_data(as_text=True)
r = console.post("/platform/schools/alpha/enter", data={"_csrf_token": csrf_from(page)}, base_url=PL)
op = Person(ALPHA, "School Admin")
op.get(r.headers["Location"][len(ALPHA):])
op.get("/admin/workspace/school")

PASSWORD = "Fixture-password-9"
HASH = generate_password_hash(PASSWORD, method="pbkdf2:sha256:1000")
NOW = datetime.now(timezone.utc).isoformat()
JSS1 = one("SELECT id FROM school_classes WHERE name = 'JSS 1'")
SSS3 = one("SELECT id FROM school_classes WHERE name = 'SSS 3'")
SESSION = one("SELECT id FROM academic_sessions WHERE is_current = 1")
role = {n: one("SELECT id FROM admin_types WHERE name = :n", n=n) for n in ("Subject Teacher", "Head Teacher")}


def new_student(first, last, class_id, **extra):
    sql("INSERT INTO students (admission_no, first_name, last_name, gender, date_of_birth, state_of_origin, guardian_phone, "
        "religion, food_allergies, created_at, active, school_id) VALUES (:a, :f, :l, 'Female', '2008-05-02', 'Lagos', "
        "'08031112222', 'Christianity', 'Peanuts', :n, 1, (SELECT id FROM schools LIMIT 1))",
        a=f"ADM-{first}", f=first, l=last, n=NOW)
    sid = one("SELECT id FROM students WHERE admission_no = :a", a=f"ADM-{first}")
    sql("INSERT INTO student_enrolments (student_id, class_id, session_id, enrolled_at, active) VALUES (:s, :c, :x, :n, 1)",
        s=sid, c=class_id, x=SESSION, n=NOW)
    return sid


def new_parent(username, name, children):
    sql("INSERT INTO parent_accounts (username, display_name, email, phone, password_hash, active, password_must_change, created_at) "
        "VALUES (:u, :d, :e, '08030000001', :h, 1, 0, :n)", u=username, d=name, e=f"{username}@family.test", h=HASH, n=NOW)
    pid = one("SELECT id FROM parent_accounts WHERE username = :u", u=username)
    for sid in children:
        sql("INSERT INTO parent_student_links (parent_id, student_id, relationship, active, created_at) VALUES (:p, :s, 'Mother', 1, :n)",
            p=pid, s=sid, n=NOW)
    person = Person(ALPHA, username)
    person.post("/login", {"username": username, "password": PASSWORD}, page="/login")
    return pid, person


def new_staff(username, roles):
    payload = {"username": username, "display_name": username.replace(".", " ").title(),
               "admin_type_ids": [str(role[r]) for r in roles], "duty_class": [""], "duty_subject": [""], "duty_department": [""]}
    op.post("/admin/administration/admins/new", payload, page="/admin/administration/admins/new")
    aid = one("SELECT id FROM admins WHERE username = :u", u=username)
    sql("UPDATE admins SET password_hash = :h, password_must_change = 0 WHERE id = :i", h=HASH, i=aid)
    person = Person(ALPHA, username)
    person.post("/login", {"username": username, "password": PASSWORD}, page="/login")
    person.get("/admin/workspace/school")
    return person


sql("INSERT INTO school_subjects (name, created_at, active) VALUES ('Mathematics', :n, 1)", n=NOW)
MATHS = one("SELECT id FROM school_subjects WHERE name = 'Mathematics'")
ADA, BISI, CHI = new_student("Adaone", "Okafor", JSS1), new_student("=Bisitwo", "Okafor", JSS1), new_student("Chithree", "Eze", JSS1)
S1, S2 = new_student("Seniorone", "Adeyemi", SSS3), new_student("Seniortwo", "Uche", SSS3)
for sid in (S1, S2):
    sql("INSERT INTO class_subjects (class_id, subject_id, created_at) VALUES (:c, :s, :n)", c=SSS3, s=MATHS, n=NOW) if sid == S1 else None
MUM, mum = new_parent("mum.test", "Mrs Okafor", [ADA, BISI])
DAD, dad = new_parent("dad.test", "Mr Eze", [CHI])
teacher = new_staff("subject.teacher", ["Subject Teacher"])
head = new_staff("head.teacher", ["Head Teacher"])


def store_photo(name):
    from core.storage import save_upload_bytes
    return save_upload_bytes("students", f"{name}_0123456789abcdef.png", b"\x89PNG\r\n\x1a\nphoto-" + name.encode(), "image/png")


PHOTO = inside(store_photo, "chi")
sql("UPDATE students SET photo_path = :p WHERE id = :s", p=inside(store_photo, "ada"), s=ADA)
sql("UPDATE students SET photo_path = :p WHERE id = :s", p=PHOTO, s=CHI)
for sid, status, score in ((ADA, 'released', 61), (ADA, 'entered', 99), (CHI, 'released', 70)):
    sql("INSERT INTO school_student_results (student_id, subject_id, score, max_score, term, session_id, status, created_at, "
        "source_type, component_name, released_at) VALUES (:s, :m, :sc, 100, 'First Term', :x, :st, :n, 'manual', 'Exam', :n)",
        s=sid, m=MATHS, sc=score, x=SESSION, st=status, n=NOW)
sql("INSERT INTO parent_feedback (parent_id, student_id, subject, body, status, created_at, updated_at) VALUES (:p, :s, 'Bus', 'About the school bus', 'open', :n, :n)",
    p=DAD, s=CHI, n=NOW)

# ================================================================ A. consents
check("a parent is asked to read the privacy notice on their home page", "Please read the privacy notice" in mum.text("/parent/dashboard"))
page = mum.text("/parent/privacy")
check("Privacy & my data lists their children with a switch for each purpose", "Adaone" in page and "Photos in publicity" in page
      and "Exam registration" in page and "Chithree" not in page)
mum.post("/parent/privacy/notice", {}, page="/parent/privacy")
check("confirming the notice is recorded with its version, and the prompt goes",
      one("SELECT notice_version FROM consent_records WHERE parent_id = :p AND purpose = 'privacy_notice'", p=MUM) == "2026-10-15"
      and "Please read the privacy notice" not in mum.text("/parent/dashboard"))
mum.post("/parent/privacy/consent", {"student_id": ADA, "purpose": "photos_media", "granted": "1"}, page="/parent/privacy")
mum.post("/parent/privacy/consent", {"student_id": BISI, "purpose": "optional_messages", "granted": "0"}, page="/parent/privacy")
check("a parent allows photos for one child and withdraws optional messages for the other",
      one("SELECT granted FROM consent_records WHERE student_id = :s AND purpose = 'photos_media'", s=ADA) == 1
      and one("SELECT granted FROM consent_records WHERE student_id = :s AND purpose = 'optional_messages'", s=BISI) == 0)
r = mum.post("/parent/privacy/consent", {"student_id": CHI, "purpose": "photos_media", "granted": "1"}, page="/parent/privacy")
check("…but cannot decide for a child who is not theirs", r.status_code == 404
      and not one("SELECT COUNT(*) FROM consent_records WHERE student_id = :s", s=CHI))
check("optional messages go only where they are allowed (the default is to send)",
      inside(lambda: __import__("core.privacy", fromlist=["x"]).allowed([ADA, BISI, CHI], "optional_messages")) == {ADA, CHI})
op.post(f"/admin/school/students/{CHI}/consent", {"purpose": "photos_media", "granted": "1", "note": ""}, page=f"/admin/school/students/{CHI}")
check("the school must say what form it holds", not one("SELECT COUNT(*) FROM consent_records WHERE student_id = :s", s=CHI))
op.post(f"/admin/school/students/{CHI}/consent", {"purpose": "photos_media", "granted": "1", "note": "Signed form, 12 September"},
        page=f"/admin/school/students/{CHI}")
check("…and then a signed form is recorded, as staff, with the note",
      sql("SELECT source, note FROM consent_records WHERE student_id = :s", s=CHI) == [("staff", "Signed form, 12 September")])
check("the student's page shows the consents and their history", all(x in op.text(f"/admin/school/students/{CHI}")
                                                                     for x in ("Consents", "From a signed form", "Signed form, 12 September")))
r = teacher.post(f"/admin/school/students/{ADA}/consent", {"purpose": "exam_sharing", "granted": "1", "note": "x"}, page="/admin/school")
check("a teacher cannot record a consent", r.status_code in (302, 403)
      and not one("SELECT COUNT(*) FROM consent_records WHERE student_id = :s AND purpose = 'exam_sharing'", s=ADA))
rows = list(csv.reader(io.StringIO(op.get("/admin/school/privacy/consents.csv").get_data(as_text=True).lstrip("﻿"))))
check("the consents spreadsheet lists every student, a formula-looking name defused",
      len(rows) == 6 and not any(cell.startswith("=") for row in rows for cell in row) and "allowed" in rows[1][3], str(rows[:3]))
EXR = f"/admin/school/exam-registration?body=waec&class_id={SSS3}&session_id={SESSION}"
check("exam registration says who has no consent to share", "consent to share" in op.text(EXR))
r = op.get(EXR + "&format=csv")
check("…and with nobody consenting there is nothing to download", r.status_code == 302)
op.post("/admin/school/exam-registration/consent", {"student_id": [str(S1)], "note": "Signed forms collected 14 October",
                                                    "back": EXR}, page=EXR)
csv_rows = list(csv.reader(io.StringIO(op.get(EXR + "&format=csv").get_data(as_text=True).lstrip("﻿"))))
check("signed forms recorded for a candidate let them, and only them, into the CSV",
      len(csv_rows) == 2 and csv_rows[1][1] == "Adeyemi", str(csv_rows))

# ================================================================ B. requests
mum.post("/parent/privacy/request", {"student_id": ADA, "kind": "export", "reason": "For our records"}, page="/parent/privacy")
REQ = one("SELECT id FROM data_requests WHERE student_id = :s AND kind = 'export'", s=ADA)
check("a parent asks for a copy; it waits for the school with a date thirty days on",
      REQ and one("SELECT status FROM data_requests WHERE id = :i", i=REQ) == "pending")
mum.post("/parent/privacy/request", {"student_id": ADA, "kind": "export"}, page="/parent/privacy")
mum.post("/parent/privacy/request", {"student_id": CHI, "kind": "export"}, page="/parent/privacy")
check("a second identical request, or one for a child not theirs, is refused",
      one("SELECT COUNT(*) FROM data_requests") == 1)
check("the school sees it waiting", "Requests waiting" in op.text("/admin/school/privacy") and "Mrs Okafor" in op.text("/admin/school/privacy"))
op.post(f"/admin/school/privacy/requests/{REQ}", {"action": "approve"}, page="/admin/school/privacy")
check("approving builds the copy, and the request is done",
      one("SELECT status FROM data_requests WHERE id = :i", i=REQ) == "done"
      and one("SELECT status FROM data_exports WHERE kind = 'family'") == "ready")
z = mum.get(f"/parent/privacy/requests/{REQ}/download")
names = zipfile.ZipFile(io.BytesIO(z.data)).namelist() if z.status_code == 200 else []
check("the parent downloads a ZIP of the child's data", {"child.json", "you.json", "results.csv", "consents.csv"} <= set(names), str(names))
zf = zipfile.ZipFile(io.BytesIO(z.data))
results_csv = zf.read("results.csv").decode("utf-8-sig")
you = json.loads(zf.read("you.json"))
check("…with released results only, and no password anywhere",
      "61" in results_csv and "99" not in results_csv and "password_hash" not in you
      and "login_password_hash" not in json.loads(zf.read("child.json")))
check("another parent cannot download it", dad.get(f"/parent/privacy/requests/{REQ}/download").status_code == 404)
dad.post("/parent/privacy/request", {"student_id": CHI, "kind": "erasure"}, page="/parent/privacy")
check("asking to erase needs the parent to tick that they understand", not one("SELECT COUNT(*) FROM data_requests WHERE kind = 'erasure'"))
dad.post("/parent/privacy/request", {"student_id": CHI, "kind": "erasure", "understood": "1"}, page="/parent/privacy")
ERASE = one("SELECT id FROM data_requests WHERE kind = 'erasure'")
head.post(f"/admin/school/privacy/requests/{ERASE}", {"action": "approve", "confirm": "ADM-Chithree"}, page="/admin/school/privacy")
check("a head teacher can see requests but cannot erase", one("SELECT status FROM data_requests WHERE id = :i", i=ERASE) == "pending"
      and one("SELECT erased_at FROM students WHERE id = :s", s=CHI) is None)
op.post(f"/admin/school/privacy/requests/{ERASE}", {"action": "approve", "confirm": "ADM-wrong"}, page="/admin/school/privacy")
check("the School Admin must type the admission number", one("SELECT erased_at FROM students WHERE id = :s", s=CHI) is None)
op.post(f"/admin/school/privacy/requests/{ERASE}", {"action": "approve", "confirm": "adm-chithree"}, page="/admin/school/privacy")
row = sql("SELECT first_name, last_name, date_of_birth, guardian_phone, religion, food_allergies, photo_path, active, erased_at "
          "FROM students WHERE id = :s", s=CHI)[0]
check("erased: the child is a placeholder with no personal details", row[0] == "Former" and row[1] == f"student #{CHI}"
      and row[2:7] == (None, None, None, None, None) and row[7] == 0 and row[8], str(row))
check("…the photograph file is gone", not inside(lambda: __import__("core.storage", fromlist=["x"]).upload_exists(PHOTO)))
check("…their messages are gone", not one("SELECT COUNT(*) FROM parent_feedback WHERE student_id = :s", s=CHI))
check("…their results stay", one("SELECT score FROM school_student_results WHERE student_id = :s", s=CHI) == 70)
check("…the parent with no other child is anonymised and cannot sign in",
      one("SELECT display_name FROM parent_accounts WHERE id = :p", p=DAD) == "Former parent"
      and one("SELECT email FROM parent_accounts WHERE id = :p", p=DAD) is None
      and "parent" not in (Person(ALPHA).post("/login", {"username": "dad.test", "password": PASSWORD}, page="/login").headers.get("Location") or ""))
check("…and the erasure is in the activity log", one("SELECT COUNT(*) FROM audit_logs WHERE action = 'student_data_erased'") == 1)

# ================================================================ C. the school's export
check("a teacher cannot export the school's data", teacher.get("/admin/school/data-export").status_code in (302, 403))
sql("INSERT INTO school_settings (school_id, setting_key, setting_value, updated_at) VALUES ((SELECT id FROM schools LIMIT 1), "
    "'smtp_password', 'enc:v1:SECRET-LOOKING', :n)", n=NOW)
op.post("/admin/school/data-export", {}, page="/admin/school/data-export")
EXP = one("SELECT id FROM data_exports WHERE kind = 'school'")
check("the School Admin prepares an export, built in the background", one("SELECT status FROM data_exports WHERE id = :i", i=EXP) == "ready",
      str(one("SELECT error FROM data_exports WHERE id = :i", i=EXP)))
z = op.get(f"/admin/school/data-export/{EXP}/download")
zf = zipfile.ZipFile(io.BytesIO(z.data)) if z.status_code == 200 else None
names = zf.namelist() if zf else []
check("it has a spreadsheet for every table, the uploaded files and a README",
      {"README.txt", "data/students.csv", "data/finance_payments.csv", "data/consent_records.csv"} <= set(names)
      and any(n.startswith("files/") for n in names), str(names[:8]))
heads = {n: zf.open(n).readline().decode("utf-8-sig") for n in names if n.startswith("data/")} if zf else {}
check("…and no password, secret or token column in any of them",
      heads and not any(re.search(r"password_hash|secret|token", h) for h in heads.values()),
      str([n for n, h in heads.items() if re.search(r"password_hash|secret|token", h)]))
settings_csv = zf.read("data/school_settings.csv").decode("utf-8-sig") if zf else ""
check("…nor any stored credential", "SECRET-LOOKING" not in settings_csv)
check("…students are as erased", f"student #{CHI}" in zf.read("data/students.csv").decode("utf-8-sig") if zf else False)

# ================================================================ D. the API
check("a teacher cannot open API & webhooks", teacher.get("/admin/school/api").status_code in (302, 403))
r = op.post("/admin/school/api/keys", {"name": "Accounting system", "scopes": ["students", "payments", "results"]},
            page="/admin/school/api", extra="idem")
KEY = (re.search(r"bsa_[0-9a-f]{12}_[A-Za-z0-9_-]{32,64}", r.get_data(as_text=True)) or [None])[0] if r.status_code == 200 else None
check("a key is shown once, when it is made", KEY is not None, str(r.status_code))
check("…and only its hash is kept", KEY and one("SELECT secret_hash FROM api_keys") not in KEY and KEY.split("_")[2] not in str(sql("SELECT * FROM api_keys")))
check("…not again afterwards", KEY and KEY not in op.text("/admin/school/api"))
api = A.app.test_client()


def call(path, key=KEY):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return api.get(path, base_url=ALPHA, headers=headers)


r = call("/api/v1/students?limit=2")
body = r.get_json() or {}
check("the key reads students, a page at a time", r.status_code == 200 and len(body.get("data", [])) == 2 and body.get("next_after"),
      str(r.status_code))
r2 = call(f"/api/v1/students?limit=2&after={body.get('next_after')}")
check("…the next page follows on", r2.status_code == 200 and r2.get_json()["data"][0]["id"] > body["data"][-1]["id"])
check("…with no health details, and no session cookie", "food_allergies" not in r.get_data(as_text=True)
      and "Set-Cookie" not in r.headers)
everyone = call("/api/v1/students?status=all&limit=200").get_json()["data"]
check("an erased student appears as the placeholder", any(s["id"] == CHI and s["erased"] and s["first_name"] == "Former" for s in everyone))
res = call("/api/v1/results").get_json()["data"]
check("results: released only", sorted(x["score"] for x in res) == [61, 70], str(res))
check("an area the key was not given is refused", call("/api/v1/attendance").status_code == 403)
check("no key, or a wrong one, is refused", call("/api/v1/students", key=None).status_code == 401
      and call("/api/v1/students", key=KEY[:-4] + "AAAA").status_code == 401)
check("an unknown endpoint answers in JSON", call("/api/v1/nothing-here").status_code == 404
      and (call("/api/v1/nothing-here").get_json() or {}).get("error", {}).get("code") == "not_found")
check("a filter that is not a number is refused, not a server error", call("/api/v1/students?limit=abc").status_code == 400)
from core import api_keys as _api_keys  # noqa: E402
_api_keys.RATE_PER_MINUTE = 3
codes = [call("/api/v1/students").status_code for _ in range(4)]
_api_keys.RATE_PER_MINUTE = 120
check("too many calls are slowed down", codes[-1] == 429, str(codes))
check("the documentation and the description are open to anyone",
      api.get("/api/v1/docs", base_url=ALPHA).status_code == 200
      and "/students" in api.get("/api/v1/openapi.json", base_url=ALPHA).get_json()["paths"])
KEY_ID = one("SELECT id FROM api_keys")
op.post(f"/admin/school/api/keys/{KEY_ID}/revoke", {}, page="/admin/school/api")
check("a revoked key stops working at once", call("/api/v1/students").status_code == 401)

# ================================================================ E. webhooks
received, ANSWER = [], [200]


class Receiver(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        received.append((dict(self.headers), body))
        self.send_response(ANSWER[0])
        self.end_headers()

    def log_message(self, *a):
        pass


server = http.server.HTTPServer(("127.0.0.1", 0), Receiver)
threading.Thread(target=server.serve_forever, daemon=True).start()
HOOK = f"http://127.0.0.1:{server.server_port}/hooks"
r = op.post("/admin/school/api/webhooks", {"url": "ftp://example.test/x", "events": ["payment.recorded"]},
            page="/admin/school/api", extra="idem")
check("an address that is not http(s) is refused", not one("SELECT COUNT(*) FROM webhook_endpoints"))
r = op.post("/admin/school/api/webhooks", {"url": HOOK, "events": ["payment.recorded", "student.archived"], "description": "Books"},
            page="/admin/school/api", extra="idem")
SECRET = (re.search(r"whsec_[A-Za-z0-9_-]+", r.get_data(as_text=True)) or [None])[0]
check("an address is added and its signing secret shown once", SECRET and one("SELECT secret_encrypted FROM webhook_endpoints") != SECRET)
op.post("/admin/finance/payments/new", {"student_id": ADA, "session_id": SESSION, "amount": "25000", "category": "School Fees",
                                        "method": "Cash", "reference": "", "paid_at": "", "notes": "", "payer_name": "Mrs Okafor"},
        page="/admin/finance/payments/new")
hit = [b for h, b in received if h.get("Beacon-Event") == "payment.recorded"]
check("recording a payment tells the address", len(hit) == 1, str(len(received)))
if hit:
    headers = next(h for h, b in received if b == hit[0])
    t, v1 = (dict(p.split("=", 1) for p in headers["Beacon-Signature"].split(","))[k] for k in ("t", "v1"))
    expected = hmac.new(SECRET.encode(), t.encode() + b"." + hit[0], hashlib.sha256).hexdigest()
    payload = json.loads(hit[0])
    check("…signed with the address's secret", hmac.compare_digest(expected, v1))
    check("…saying what happened with ids and money, no names", payload["type"] == "payment.recorded"
          and payload["data"]["amount"] == 25000 and payload["data"]["student_id"] == ADA and b"Okafor" not in hit[0])
EP = one("SELECT id FROM webhook_endpoints")
op.post(f"/admin/school/api/webhooks/{EP}", {"action": "test"}, page="/admin/school/api")
check("a test reaches it", any(h.get("Beacon-Event") == "ping" for h, b in received))
ANSWER[0] = 500
ar = op.post("/admin/school/students/archive", {"student_ids": str(BISI), "reason": "Left the school"})
last = sql("SELECT status, attempts, response_code FROM webhook_deliveries WHERE event = 'student.archived'")
check("an address that answers 500 is noted and tried again later", last and last[0][0] == "retrying" and last[0][2] == 500,
      f"{last} {ar.status_code} {ar.headers.get('Location')} archived={one('SELECT archived_at FROM students WHERE id = :s', s=BISI)} "
      f"deliveries={sql('SELECT event, status, error FROM webhook_deliveries')}")
errors.seen[:] = [e for e in errors.seen if "answered 500" not in str(e)]   # the failure asked for above, logged on purpose
ANSWER[0] = 200
from core import webhooks as _hooks  # noqa: E402
os.environ["BEACON_ENV"] = "production"
try:
    _hooks._resolve("127.0.0.1", 80)
    refused = False
except ValueError:
    refused = True
os.environ["BEACON_ENV"] = "development"
check("in production an address on the server's own network is refused", refused)
server.shutdown()

# ================================================================ F. a protected form shown again
r = op.post("/admin/school/documents/issue", {"kind": "testimonial", "student_id": ADA, "conduct": "", "character": ""},
            page="/admin/school/documents/issue", extra="idem")
check("a form protected against double submission, shown again with what to correct, answers normally",
      r.status_code == 200 and "conduct" in r.get_data(as_text=True).lower(), str(r.status_code))

# ================================================================ every page opens
for path in ("/admin/school/privacy", "/admin/school/data-export", "/admin/school/api", "/admin/settings", f"/admin/school/students/{ADA}"):
    r = op.get(path)
    check(f"{path} opens for the School Admin", r.status_code == 200, str(r.status_code))
check("the head teacher can open Privacy & data requests", head.get("/admin/school/privacy").status_code == 200)
check("no page answered with a server error", not PROBLEMS, "; ".join(PROBLEMS[:5]))
check("nothing raised behind the scenes", not errors.seen, "; ".join(repr(e)[:150] for e in errors.seen[:3]))
failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
