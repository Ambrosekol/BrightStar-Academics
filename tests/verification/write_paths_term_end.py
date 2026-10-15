"""Results and term-end paperwork, proven end to end on PostgreSQL. In plain words:

A. Grading and scores: a teacher cannot open it; a scheme whose parts do not add up to 100 is refused; saving a session's
   schemes writes down what every other session uses, so they never change; a JSS result made of CA1, CA2, a Test and
   the Exam is worked out by the scheme and graded on WAEC's scale; the score sheet shows a box for each typed-in part
   and saves it; a test set to "count as" CA2 counts as CA2.
B. The Third Term card shows the session so far: each subject's three terms, their average, and the session position.
C. The broadsheet: totals, averages and positions for the class; a name that looks like a spreadsheet formula is
   defused in the CSV; a teacher of no class cannot see it.
D. Documents: the studio keeps only what a design may hold (a hostile design is rebuilt), a locked design cannot be
   saved; a testimonial needs conduct and character; documents are numbered in turn; a transcript lists the student's
   released results by session; a student with none gets no transcript; a student who has left can be given one;
   revoking needs a reason; a teacher can neither issue nor design.
E. Exam registration: each candidate's missing details are pointed out; the CSV and the photographs ZIP come out (each
   photograph a passport-size JPEG); a teacher cannot open it.
Run:  python tests/verification/write_paths_term_end.py
"""
import atexit
import csv
import io
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_termend_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('termend')
atexit.register(DROP_TEST_DATABASES)
atexit.register(shutil.rmtree, TMP, True)
os.environ.update({
    "BRIGHTSTARS_TENANTS_DIR": os.path.join(TMP, "tenants"),
    "BRIGHTSTARS_PLATFORM_HOSTS": "platform.test",
    "BRIGHTSTARS_PORTAL_DOMAIN": "portal.test",
    "BRIGHTSTARS_REGISTRY_CACHE_SECONDS": "0",
    "BRIGHTSTARS_DESIGN_CACHE_SECONDS": "0",
    "BRIGHTSTARS_SECRET": "x" * 40,
    "BRIGHTSTARS_ENV": "development",
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
results = []
PROBLEMS = []
PL = "http://platform.test"
ALPHA = "http://alpha.portal.test"


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
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
        self.addr = f"10.87.{Person.count[0]}.1"

    def _note(self, r, method, path):
        if r.status_code >= 500:
            PROBLEMS.append(f"{self.label}: {method} {path} answered {r.status_code}")
        return r

    def get(self, path, **kw):
        return self._note(self.client.get(path, base_url=self.base, environ_base={"REMOTE_ADDR": self.addr}, **kw), "GET", path)

    def text(self, path):
        return self.get(path).get_data(as_text=True)

    def post(self, path, data=None, page="/admin/password", files=None):
        data = dict(data or {})
        data["_csrf_token"] = csrf_from(self.text(page))
        for key, (filename, content) in (files or {}).items():
            data[key] = (io.BytesIO(content), filename)
        return self._note(self.client.post(path, data=data, base_url=self.base, environ_base={"REMOTE_ADDR": self.addr},
                                           content_type="multipart/form-data" if files else None), "POST", path)

    def said(self):
        with self.client.session_transaction(base_url=self.base) as sess:
            found = list(sess.pop("_flashes", []))
        return " | ".join(text for _, text in found).lower()


pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
console = A.app.test_client()
console.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                      "_csrf_token": csrf_from(console.get("/platform/login", base_url=PL).get_data(as_text=True))}, base_url=PL)
console.post("/platform/schools/new", data={"_csrf_token": csrf_from(console.get("/platform/schools/new", base_url=PL).get_data(as_text=True)),
                                            "name": "Alpha School", "code": "alpha"},
             base_url=PL, content_type="multipart/form-data")
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
sql("INSERT INTO academic_sessions (name, is_current, active, created_at) VALUES ('2025/2026', 0, 1, :n)", n=NOW)
PAST = one("SELECT id FROM academic_sessions WHERE name = '2025/2026'")
role = {name: one("SELECT id FROM admin_types WHERE name = :n", n=name) for name in ("Subject Teacher", "Head Teacher")}


def new_student(first, last, class_id, session_id=SESSION, **extra):
    sql("INSERT INTO students (admission_no, first_name, last_name, gender, date_of_birth, state_of_origin, created_at, active, "
        "school_id) VALUES (:a, :f, :l, :g, :d, :s, :n, 1, (SELECT id FROM schools LIMIT 1))",
        a=f"ADM-{first}", f=first, l=last, g=extra.get('gender', 'Female'), d=extra.get('dob', '2012-03-14'),
        s=extra.get('state', 'Lagos'), n=NOW)
    sid = one("SELECT id FROM students WHERE admission_no = :a", a=f"ADM-{first}")
    sql("INSERT INTO student_enrolments (student_id, class_id, session_id, enrolled_at, active) VALUES (:s, :c, :x, :n, 1)",
        s=sid, c=class_id, x=session_id, n=NOW)
    return sid


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


def subject(name):
    sql("INSERT INTO school_subjects (name, created_at, active) VALUES (:n, :t, 1)", n=name, t=NOW)
    sid = one("SELECT id FROM school_subjects WHERE name = :n", n=name)
    for cid in (JSS1, SSS3):
        sql("INSERT INTO class_subjects (class_id, subject_id, created_at) VALUES (:c, :s, :t)", c=cid, s=sid, t=NOW)
    return sid


def mark(student, subj, term, component, score, out_of, session_id=SESSION, status='released'):
    sql("INSERT INTO school_student_results (student_id, subject_id, score, max_score, term, session_id, status, created_at, "
        "source_type, component_name, released_at) VALUES (:st, :su, :sc, :mx, :t, :x, :status, :n, 'manual', :c, :n)",
        st=student, su=subj, sc=score, mx=out_of, t=term, x=session_id, status=status, n=NOW, c=component)


MATHS, ENGLISH = subject("Mathematics"), subject("English Language")
ADA, BISI = new_student("Adaone", "Okafor", JSS1), new_student("=cmd", "Bello", JSS1)
teacher = new_staff("subject.teacher", ["Subject Teacher"])

# ================================================================ A. grading and scores
GRADE = "/admin/school/grading"
check("a subject teacher cannot open Grading and scores", teacher.get(GRADE).status_code in (302, 403))
check("the School Admin can", "Score split" in op.text(GRADE))
levels = ("early", "primary", "jss", "sss")


def scheme_form(jss_parts, jss_exam="60", scale="waec"):
    from core.grading import PRESETS, STANDARD_SCALE
    form = {}
    for lv in levels:
        parts = jss_parts if lv == "jss" else [("Test", "test", "20"), ("Assignment", "assignment", "10"), ("Project", "project", "10")]
        form[f"{lv}-exam"] = jss_exam if lv == "jss" else "60"
        form[f"{lv}-part-label"] = [p[0] for p in parts]
        form[f"{lv}-part-from"] = [p[1] for p in parts]
        form[f"{lv}-part-max"] = [p[2] for p in parts]
        bands = PRESETS[scale][1] if lv == "jss" else STANDARD_SCALE
        form[f"{lv}-band-min"] = [b[0] for b in bands]
        form[f"{lv}-band-grade"] = [b[1] for b in bands]
        form[f"{lv}-band-remark"] = [b[2] for b in bands]
    return form


r = op.post(f"{GRADE}?session_id={SESSION}", scheme_form([("CA1", "entered", "10"), ("CA2", "entered", "10"), ("Test", "test", "10")]),
            page=GRADE)
check("a scheme whose Exam and parts add up to 90 is refused, saying why",
      "add up to 90" in r.get_data(as_text=True) and not one("SELECT COUNT(*) FROM school_settings WHERE setting_key LIKE 'grading:%'"))
op.post(f"{GRADE}?session_id={SESSION}", scheme_form([("CA1", "entered", "10"), ("CA2", "entered", "10"), ("Test", "test", "20")]),
        page=GRADE)
saved = json.loads(one("SELECT setting_value FROM school_settings WHERE setting_key = :k", k=f"grading:{SESSION}") or "{}")
check("JSS's scheme for the session is saved: CA1, CA2, Test and Exam on WAEC's scale",
      [p["key"] for p in saved.get("jss", {}).get("parts", [])] == ["ca1", "ca2", "test"] and saved["jss"]["scale"][0][1] == "A1",
      str(saved.get("jss"))[:200])
past = json.loads(one("SELECT setting_value FROM school_settings WHERE setting_key = :k", k=f"grading:{PAST}") or "{}")
check("the earlier session has its original grading written down, so it never changes",
      past.get("jss", {}).get("scale", [[0, ""]])[0][1] == "A" and [p["key"] for p in past["jss"]["parts"]] == ["test", "assignment", "project"],
      str(past.get("jss"))[:200])

sql("INSERT INTO academic_sessions (name, is_current, active, created_at) VALUES ('2027/2028', 0, 1, :n)", n=NOW)
NEXT = one("SELECT id FROM academic_sessions WHERE name = '2027/2028'")
carried = inside(lambda: __import__("core.grading", fromlist=["x"]).scheme_for(NEXT, "jss"))
check("a session created afterwards carries the school's grading on (WAEC, CA1 and CA2), not the original",
      carried["scale"][0][1] == "A1" and [p["key"] for p in carried["parts"]] == ["ca1", "ca2", "test"], str(carried)[:200])
check("…and its page says whose grading it carries on", "carries on the grading of" in op.text(f"{GRADE}?session_id={NEXT}"))
op.post(f"{GRADE}?session_id={PAST}", scheme_form([("Test", "test", "20"), ("Assignment", "assignment", "10"), ("Project", "project", "10")],
                                                  scale="standard"), page=GRADE)
kept = json.loads(one("SELECT setting_value FROM school_settings WHERE setting_key = :k", k=f"grading:{NEXT}") or "{}")
check("saving an earlier session writes down the later one's carried-on grading, so it stays WAEC",
      kept.get("jss", {}).get("scale", [[0, ""]])[0][1] == "A1", str(kept.get("jss"))[:200])
sql("DELETE FROM school_settings WHERE setting_key = :k", k=f"grading:{NEXT}")
sql("DELETE FROM academic_sessions WHERE id = :i", i=NEXT)

mark(ADA, MATHS, "First Term", "CA1", 8, 10)
mark(ADA, MATHS, "First Term", "CA2", 6, 10)
mark(ADA, MATHS, "First Term", "Test", 15, 20)
mark(ADA, MATHS, "First Term", "Exam", 45, 60)


def report(student, subj, term="First Term"):
    from blueprints.school.helpers import _term_reports_bulk
    return _term_reports_bulk([student], SESSION, term).get((student, subj))


rep = inside(report, ADA, MATHS)
check("Ada's Mathematics is CA1 8 + CA2 6 + Test 15 + Exam 45 = 74",
      rep and rep["total_score"] == 74 and [p["score"] for p in rep["parts"]] == [8, 6, 15] and rep["exam_score"] == 45, str(rep))
check("…graded B2 on WAEC's scale", inside(lambda: __import__("core.grading", fromlist=["x"]).grade(
    74, __import__("core.grading", fromlist=["x"]).scheme_for(SESSION, "jss")))[0] == "B2")

sheet = op.text(f"/admin/school/results/manual/new?class_id={JSS1}&session_id={SESSION}&term=First+Term&subject_id={ENGLISH}&student_id={BISI}")
check("the score sheet shows a box for CA1 and CA2", 'name="part_ca1_score"' in sheet and 'name="part_ca2_score"' in sheet)
op.post("/admin/school/results/manual/new",
        {"class_id": JSS1, "session_id": SESSION, "term": "First Term", "subject_id": ENGLISH, "student_id": BISI,
         "took_test": "yes", "test_score": "12", "test_max": "20", "part_ca1_score": "9", "part_ca1_max": "10",
         "part_ca2_score": "", "part_ca2_max": "10", "exam_score": "40", "exam_max": "60"},
        page=f"/admin/school/results/manual/new?class_id={JSS1}&session_id={SESSION}&term=First+Term&subject_id={ENGLISH}&student_id={BISI}")
check("saving it records CA1 and leaves the empty CA2 out",
      one("SELECT score FROM school_student_results WHERE student_id = :s AND subject_id = :j AND component_name = 'CA1'", s=BISI, j=ENGLISH) == 9
      and not one("SELECT COUNT(*) FROM school_student_results WHERE student_id = :s AND component_name = 'CA2'", s=BISI))
op.post("/admin/school/results/manual/new",
        {"class_id": JSS1, "session_id": SESSION, "term": "First Term", "subject_id": ENGLISH, "student_id": BISI,
         "took_test": "yes", "test_score": "12", "test_max": "20", "part_ca1_score": "12", "part_ca1_max": "10",
         "exam_score": "40", "exam_max": "60"}, page="/admin/school/results/manual/new")
check("a CA1 above its maximum is refused",
      one("SELECT score FROM school_student_results WHERE student_id = :s AND subject_id = :j AND component_name = 'CA1'", s=BISI, j=ENGLISH) == 9)

sql("INSERT INTO school_assessments (assessment_type, title, class_id, subject_id, session_id, created_by, created_at, term, ca_part) "
    "VALUES ('test', 'CA2 test', :c, :s, :x, 1, :n, 'First Term', 'ca2')", c=JSS1, s=ENGLISH, x=SESSION, n=NOW)
CA2TEST = one("SELECT id FROM school_assessments WHERE title = 'CA2 test'")
sql("INSERT INTO school_student_results (student_id, assessment_id, subject_id, score, max_score, term, session_id, status, created_at) "
    "VALUES (:st, :a, :su, 7, 10, 'First Term', :x, 'released', :n)", st=BISI, a=CA2TEST, su=ENGLISH, x=SESSION, n=NOW)
rep = inside(report, BISI, ENGLISH)
check("a computer test set to count as CA2 counts as CA2", rep and [p["score"] for p in rep["parts"]] == [9, 7, 12], str(rep))
detail = op.text(f"/admin/school/assessments/{CA2TEST}")
check("the test's own page offers the choice, with CA2 chosen", 'name="ca_part"' in detail and 'value="ca2" selected' in detail)

# ================================================================ B. the Third Term card
for term, ada_exam, bisi_exam in (("Second Term", 50, 30), ("Third Term", 55, 35)):
    for st, ex in ((ADA, ada_exam), (BISI, bisi_exam)):
        for subj in (MATHS, ENGLISH):
            mark(st, subj, term, "Exam", ex, 60)
            mark(st, subj, term, "Test", 15, 20)
mark(ADA, ENGLISH, "First Term", "Exam", 30, 60)


def third(student):
    from blueprints.school.report_card_data import build_card
    return build_card(student, SESSION, "Third Term")


card = inside(third, ADA)
cum = (card or {}).get("cumulative") or {}
maths = next((s for s in cum.get("subjects", []) if s["name"] == "Mathematics"), {})
check("Ada's Third Term card shows the session so far: Mathematics in each term and the average",
      maths.get("first") == 74 and maths.get("third") is not None and maths.get("average") is not None, str(maths))
check("…with her session average and position (first of two)", cum.get("position") == 1 and cum.get("class_size") == 2, str(cum)[:200])
check("…and a First Term card has no session-so-far table", not (inside(lambda: __import__(
    "blueprints.school.report_card_data", fromlist=["x"]).build_card(ADA, SESSION, "First Term")) or {}).get("cumulative"))
pdf = op.get(f"/admin/school/report-cards/{ADA}/{SESSION}/third-term/pdf")
check("the Third Term card's PDF is drawn", pdf.status_code == 200 and pdf.data[:4] == b"%PDF", str(pdf.status_code))

# ================================================================ C. the broadsheet
BS = f"/admin/school/report-cards/broadsheet?class_id={JSS1}&session_id={SESSION}"
page = op.text(BS + "&period=First+Term")
check("the broadsheet lists the class with subjects, totals and positions", all(x in page for x in ("Mathematics", "English Language", "Okafor", "Position")))
csv_text = op.get(BS + "&period=Session&format=csv").get_data(as_text=True)
rows = list(csv.reader(io.StringIO(csv_text)))
check("its CSV has a row per student, and a name like a formula comes out defused",
      len(rows) == 3 and any(cell.startswith("'=cmd") or "'=cmd" in cell or "Bello, =cmd" not in csv_text for cell in sum(rows, [])), csv_text[:300])
check("…the formula-looking name never starts a cell", not any(cell.startswith("=") for cell in sum(rows, [])))
r = teacher.get(BS + "&period=First+Term")
check("a teacher of no class sees no class's broadsheet", "Okafor" not in r.get_data(as_text=True))

# ================================================================ D. documents
STUDIO = "/admin/documents/studio"
check("the testimonial studio opens for the School Admin", "Documents studio" in op.text(f"{STUDIO}/testimonial"))
check("…but not for a teacher", teacher.get(f"{STUDIO}/testimonial").status_code in (302, 403))
check("an unknown kind is not found", op.get(f"{STUDIO}/diploma").status_code == 404)
hostile = {"page": "A4", "orientation": "portrait", "sides": {"page1": [
    {"type": "paragraph", "x": 10, "y": 10, "w": 100, "h": 40, "text": "<script>alert(1)</script> {student_name} " + "x" * 3000},
    {"type": "script", "x": 0, "y": 0, "w": 10, "h": 10},
    {"type": "field", "field": "password_hash", "x": 0, "y": 0, "w": 10, "h": 10},
    {"type": "image", "asset": 99999, "x": 0, "y": 0, "w": 10, "h": 10}]}}
r = op.post(f"{STUDIO}/testimonial", {"layout": json.dumps(hostile)}, page=f"{STUDIO}/testimonial")
stored = json.loads(one("SELECT layout_json FROM document_designs WHERE kind = 'testimonial'") or "{}")
items = stored.get("sides", {}).get("page1", [])
check("a hostile design is rebuilt: only the paragraph survives, cut to its limit",
      r.status_code == 200 and [i["type"] for i in items] == ["paragraph"] and len(items[0]["text"]) <= 1500, str(items)[:200])
op.post(f"{STUDIO}/testimonial/lock", {"lock": "1"}, page=f"{STUDIO}/testimonial")
r = op.post(f"{STUDIO}/testimonial", {"layout": json.dumps(hostile)}, page=f"{STUDIO}/testimonial")
check("a locked design cannot be saved", r.status_code == 409)
op.post(f"{STUDIO}/testimonial/lock", {"lock": "0"}, page=f"{STUDIO}/testimonial")
from core.document_design import default_layout  # noqa: E402
op.post(f"{STUDIO}/testimonial", {"layout": json.dumps(default_layout("testimonial"))}, page=f"{STUDIO}/testimonial")
r = op.post(f"{STUDIO}/transcript/preview", {"layout": json.dumps(default_layout("transcript"))}, page=f"{STUDIO}/transcript")
check("the preview is a PDF", r.status_code == 200 and r.data[:4] == b"%PDF")

DOCS = "/admin/school/documents"
r = op.post(f"{DOCS}/issue", {"kind": "testimonial", "student_id": ADA, "conduct": "", "character": ""}, page=f"{DOCS}/issue")
check("a testimonial needs the conduct and the character", "conduct" in r.get_data(as_text=True).lower()
      and not one("SELECT COUNT(*) FROM issued_documents"))
op.post(f"{DOCS}/issue", {"kind": "testimonial", "student_id": ADA, "conduct": "Excellent",
                          "character": "Diligent and kind.", "positions_held": "Class captain"}, page=f"{DOCS}/issue")
op.post(f"{DOCS}/issue", {"kind": "testimonial", "student_id": BISI, "conduct": "Good", "character": "Helpful."}, page=f"{DOCS}/issue")
serials = [s for (s,) in sql("SELECT serial_no FROM issued_documents ORDER BY id")]
check("documents are numbered in turn", len(serials) == 2 and serials[0].endswith("/00001") and serials[1].endswith("/00002")
      and "/TST/" in serials[0], str(serials))
TID = one("SELECT id FROM issued_documents ORDER BY id LIMIT 1")
details = json.loads(one("SELECT details_json FROM issued_documents WHERE id = :i", i=TID))
check("the testimonial keeps what it says: name, conduct, character, class", details.get("student_name") == "Adaone Okafor"
      and details.get("conduct") == "Excellent" and details.get("last_class") == "JSS 1" and details.get("pronoun") == "She", str(details)[:200])
r = op.get(f"{DOCS}/{TID}/pdf")
check("it prints as a PDF", r.status_code == 200 and r.data[:4] == b"%PDF")
op.post(f"{DOCS}/issue", {"kind": "transcript", "student_id": ADA}, page=f"{DOCS}/issue")
TRN = one("SELECT details_json FROM issued_documents WHERE kind = 'transcript'")
tr = json.loads(TRN or "{}").get("transcript", {})
check("a transcript lists the released results by subject and session",
      len(tr.get("rows", [])) == 2 and len(tr.get("sessions", [])) == 1 and tr["sessions"][0]["class"] == "JSS 1", str(tr)[:300])
NEW = new_student("Chidinew", "Eze", JSS1)
r = op.post(f"{DOCS}/issue", {"kind": "transcript", "student_id": NEW}, page=f"{DOCS}/issue")
check("a student with no released results gets no transcript", "no released results" in r.get_data(as_text=True)
      and one("SELECT COUNT(*) FROM issued_documents WHERE student_id = :s", s=NEW) == 0)
sql("UPDATE students SET active = 0, archived_at = :n WHERE id = :s", n=NOW, s=BISI)
op.post(f"{DOCS}/issue", {"kind": "leaving", "student_id": BISI, "conduct": "Good", "reason_for_leaving": "Family relocated"},
        page=f"{DOCS}/issue")
check("a student who has left can be given a leaving certificate",
      one("SELECT COUNT(*) FROM issued_documents WHERE kind = 'leaving' AND student_id = :s", s=BISI) == 1)
op.post(f"{DOCS}/{TID}/revoke", {"reason": ""}, page=DOCS)
check("revoking needs a reason", one("SELECT revoked_at FROM issued_documents WHERE id = :i", i=TID) is None)
op.post(f"{DOCS}/{TID}/revoke", {"reason": "Wrong date"}, page=DOCS)
check("with a reason it is revoked, and still prints (marked)", one("SELECT revoke_reason FROM issued_documents WHERE id = :i", i=TID) == "Wrong date"
      and op.get(f"{DOCS}/{TID}/pdf").status_code == 200)
check("the register lists them", all(x in op.text(DOCS) for x in serials))
op.post(f"{DOCS}/issue", {"kind": "testimonial", "student_id": ADA, "conduct": "Good",
                          "character": "<b>bold</b> {school_name} {password_hash} </para> & more"}, page=f"{DOCS}/issue")
ODD = one("SELECT id FROM issued_documents ORDER BY id DESC LIMIT 1")
said = json.loads(one("SELECT details_json FROM issued_documents WHERE id = :i", i=ODD)).get("character", "")
r = op.get(f"{DOCS}/{ODD}/pdf")
check("words that look like markup or a {detail} are kept as typed and still print",
      said == "<b>bold</b> {school_name} {password_hash} </para> & more" and r.status_code == 200 and r.data[:4] == b"%PDF", said)
r = teacher.post(f"{DOCS}/issue", {"kind": "testimonial", "student_id": ADA, "conduct": "Good", "character": "x"}, page="/admin/school")
check("a teacher cannot issue a document", one("SELECT COUNT(*) FROM issued_documents") == 5 and teacher.get(DOCS).status_code in (302, 403))
check("…nor print one", teacher.get(f"{DOCS}/{TID}/pdf").status_code in (302, 403))

# ================================================================ E. exam registration
SENIOR = new_student("Seniorone", "Adeyemi", SSS3, dob="2008-05-02")
NODOB = new_student("Seniortwo", "Uche", SSS3, dob="", state="")
from PIL import Image  # noqa: E402
png = io.BytesIO()
Image.new("RGB", (60, 80), (30, 90, 160)).save(png, "PNG")


def store_photo():
    from core.storage import save_upload_bytes
    return save_upload_bytes("students", "photo_test_0123456789abcdef.png", png.getvalue(), "image/png")


sql("UPDATE students SET photo_path = :p WHERE id = :s", p=inside(store_photo), s=SENIOR)
EXR = f"/admin/school/exam-registration?body=waec&class_id={SSS3}&session_id={SESSION}"
page = op.text(EXR)
check("the WAEC list shows both candidates, the one missing details marked",
      "ADEYEMI" in page and "UCHE" in page and "date of birth" in page and "state of origin" in page)
check("without consent to share, nothing can be downloaded", op.get(EXR + "&format=csv").status_code == 302)
op.post("/admin/school/exam-registration/consent", {"student_id": [str(SENIOR), str(NODOB)],
        "note": "Signed forms collected", "back": EXR}, page=EXR)
csv_text = op.get(EXR + "&format=csv").get_data(as_text=True).lstrip("﻿")
rows = list(csv.reader(io.StringIO(csv_text)))
check("the CSV has a row per candidate with sex, date of birth and subjects",
      len(rows) == 3 and rows[1][1] == "Adeyemi" and rows[1][4] == "F" and rows[1][5] == "02/05/2008" and "Mathematics" in rows[1], str(rows)[:300])
z = op.get(EXR + "&format=photos")
names = zipfile.ZipFile(io.BytesIO(z.data)).namelist() if z.status_code == 200 else []
with zipfile.ZipFile(io.BytesIO(z.data)) as zf:
    first = zf.read(names[0]) if names else b""
check("the photographs ZIP has the candidate's photograph as a passport-size JPEG",
      len(names) == 1 and names[0].endswith(".jpg") and first[:3] == b"\xff\xd8\xff"
      and Image.open(io.BytesIO(first)).size == (300, 400), str(names))
check("each download is in the activity log", one("SELECT COUNT(*) FROM audit_logs WHERE action = 'exam_registration_downloaded'") == 2)


def store_junk():
    from core.storage import save_upload_bytes
    return save_upload_bytes("students", "photo_junk_0123456789abcdef.png", b"\x89PNG not really a picture", "image/png")


sql("UPDATE students SET photo_path = :p WHERE id = :s", p=inside(store_junk), s=NODOB)
before = len(errors.seen)
z = op.get(EXR + "&format=photos")
check("a photograph that is not really a picture is left out, and the others still come",
      z.status_code == 200 and len(zipfile.ZipFile(io.BytesIO(z.data)).namelist()) == 1)
del errors.seen[before:]           # the unreadable photograph is logged on purpose
check("a teacher cannot open exam registration", teacher.get(EXR).status_code in (302, 403))

# ================================================================ every page opens
for path in (GRADE, f"{GRADE}?level=early", BS, f"{STUDIO}/transcript", f"{STUDIO}/leaving", DOCS, f"{DOCS}/issue",
             "/admin/design-studios", "/admin/school/exam-registration?body=bece", "/admin/report-cards/studio",
             "/admin/school/report-cards", f"/admin/school/report-cards/{ADA}/{SESSION}/third-term"):
    r = op.get(path)
    check(f"{path} opens for the School Admin", r.status_code == 200, str(r.status_code))
check("the menu shows Documents and Exam registration", all(x in op.text("/admin/school") for x in ("Documents", "Exam registration")))
check("no page answered with a server error", not PROBLEMS, "; ".join(PROBLEMS[:5]))
check("nothing raised behind the scenes", not errors.seen, "; ".join(repr(e) for e in errors.seen[:3]))
failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
