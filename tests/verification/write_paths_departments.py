"""Senior secondary departments in the entrance examination, proven end to end on PostgreSQL. In plain words:

A. Registering: a candidate entering SSS 1 must be given a department choice (Science, Art or Commercial), and it is
   kept; a candidate entering JSS 1 has none, even if one is sent.
B. Sitting the papers: the SSS General Knowledge paper (whose standard bank has questions tagged by department)
   gives the candidate an equal share from each of the three departments, and each served question keeps its tag;
   the untagged Mathematics and English papers are drawn as before.
C. The recommendation: a candidate who chose Science but did best on the Art questions (and in English) is shown on
   the admissions waitlist, and on their own page, with Art suggested, and why; a junior candidate is shown none.
D. Admitting: the department chosen on the admit form (here not the recommended one) is the student's department;
   admitting into a junior class keeps no department; a department that does not exist is refused.
E. Tagging questions: the question form saves and clears a question's department; a bank file may tag questions
   (an "Arts" spelling is read as Art) and an unknown department is refused.
"""
import atexit
import html
import io
import json
import logging
import os
import re
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="beacon_departments_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('departments')
atexit.register(DROP_TEST_DATABASES)
atexit.register(shutil.rmtree, TMP, True)
os.environ.update({
    "BEACON_TENANTS_DIR": os.path.join(TMP, "tenants"),
    "BEACON_PLATFORM_HOSTS": "platform.test",
    "BEACON_PORTAL_DOMAIN": "portal.test",
    "BEACON_REGISTRY_CACHE_SECONDS": "0",
    "BEACON_SECRET": "x" * 40,
    "BEACON_ENV": "development",
})
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
os.chdir(ROOT)

import sqlalchemy as sa  # noqa: E402

import app as A  # noqa: E402
from control_plane import provisioning as pv  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import engine_for  # noqa: E402

results = []
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
    found = (re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', body) or re.search(r'id="csrfToken" value="([^"]+)"', body)
             or re.search(r'name="csrf-token" content="([^"]+)"', body))
    return found.group(1) if found else ""


class Person:
    def __init__(self, base):
        self.base, self.client = base, A.app.test_client()

    def get(self, path, **kw):
        return self.client.get(path, base_url=self.base, **kw)

    def text(self, path):
        return html.unescape(self.get(path).get_data(as_text=True))

    def post(self, path, data=None, page=None, **kw):
        data = dict(data or {})
        data["_csrf_token"] = csrf_from(self.get(page or path).get_data(as_text=True))
        return self.client.post(path, data=data, base_url=self.base, **kw)

    def said(self):
        with self.client.session_transaction(base_url=self.base) as sess:
            return " | ".join(text for _, text in sess.pop("_flashes", [])).lower()


with platform_session() as s:
    pass
pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
console = A.app.test_client()
console.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                      "_csrf_token": csrf_from(console.get("/platform/login", base_url=PL).get_data(as_text=True))}, base_url=PL)
console.post("/platform/schools/new", data={"_csrf_token": csrf_from(console.get("/platform/schools/new", base_url=PL).get_data(as_text=True)),
                                            "name": "Alpha School", "code": "alpha", "starter_banks": "1"},
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


page = console.get("/platform/schools/alpha", base_url=PL).get_data(as_text=True)
r = console.post("/platform/schools/alpha/enter", data={"_csrf_token": csrf_from(page)}, base_url=PL)
op = Person(ALPHA)
op.get(r.headers["Location"][len(ALPHA):])
op.get("/admin/workspace/entrance")
r = op.post("/admin/entrance-config/standard", page="/admin/candidates/new")
check("(set-up) the standard entrance papers are set up", r.status_code == 302, str(r.status_code))

# ================================================================ A. registering
FORM = {"school_attended": "Hilltop College", "parent_guardian_name": "Mrs Eze", "parent_guardian_relationship": "Mother",
        "primary_mobile": "08030000001"}


def register(name, target, department=None):
    data = {**FORM, "candidate_name": name, "target_class": target}
    if department is not None:
        data["department_choice"] = department
    r = op.post("/admin/candidates/new", data, page="/admin/candidates/new", content_type="multipart/form-data")
    body = html.unescape(r.get_data(as_text=True))
    code = re.search(r'Candidate ID</span><strong class="credential-code">(.+?)</strong>', body)
    password = re.search(r'Password</span><strong class="credential-code">(.+?)</strong>', body)
    return body, (code.group(1) if code else None), (password.group(1) if password else None)


form_page = op.text("/admin/candidates/new")
check("the registration form offers a department choice for SSS, with the three departments",
      'name="department_choice"' in form_page and all(f'value="{d}"' in form_page for d in ("Science", "Art", "Commercial")))
body, code, _ = register("No Department", "SS 1")
check("an SSS candidate with no department choice is refused, saying why",
      code is None and "Choose the department the candidate wants" in body and one("SELECT COUNT(*) FROM candidates") == 0)
body, code, _ = register("Bad Department", "SS 1", "Physics")
check("…and so is a department that does not exist", code is None and one("SELECT COUNT(*) FROM candidates") == 0)
body, SCI_CODE, SCI_PASS = register("Ngozi Science", "SS 1", "Science")
check("an SSS candidate with a department is registered, and the slip shows it",
      SCI_CODE is not None and "SS 1 · Science" in body
      and one("SELECT department_choice FROM candidates WHERE candidate_code = :c", c=SCI_CODE) == "Science")
_, ART_CODE, _ = register("Tunde Art", "SS 1", "art")
check("a department typed in another case is kept in its proper form",
      one("SELECT department_choice FROM candidates WHERE candidate_code = :c", c=ART_CODE) == "Art")
_, JSS_CODE, JSS_PASS = register("Ada Junior", "JSS 1", "Science")
check("a JSS 1 candidate keeps no department, even if one is sent",
      JSS_CODE is not None and one("SELECT department_choice FROM candidates WHERE candidate_code = :c", c=JSS_CODE) is None)
SCI = one("SELECT id FROM candidates WHERE candidate_code = :c", c=SCI_CODE)
ART = one("SELECT id FROM candidates WHERE candidate_code = :c", c=ART_CODE)
JSS = one("SELECT id FROM candidates WHERE candidate_code = :c", c=JSS_CODE)

# ================================================================ B. sitting the papers
candidate = Person(ALPHA)
candidate.post("/login", {"username": SCI_CODE, "password": SCI_PASS}, page="/login")
papers = sql("SELECT cp.id, cp.bank_id FROM candidate_papers cp WHERE cp.candidate_id = :c ORDER BY cp.slot", c=SCI)
check("the SSS candidate has three papers", len(papers) == 3, str(papers))
attempts = {}
for paper_id, bank_id in papers:
    candidate.post(f"/candidate/papers/{paper_id}/start", page="/candidate/dashboard")
    attempt = one("SELECT id FROM attempts WHERE candidate_id = :c AND bank_id = :b", c=SCI, b=bank_id)
    attempts[bank_id] = attempt
    exam_page = candidate.get("/exam").get_data(as_text=True)
    token = csrf_from(exam_page)
    served = sql("SELECT question_id, correct_option, department FROM attempt_questions WHERE attempt_id = :a", a=attempt)
    if "general_knowledge" in bank_id:
        shares = {d: sum(1 for q in served if q[2] == d) for d in ("Science", "Art", "Commercial")}
        check("the General Knowledge paper serves an equal share from each department (6 of 25 each)",
              len(served) == 25 and shares == {"Science": 6, "Art": 6, "Commercial": 6}, str(shares))
        check("…and every served question keeps a valid tag or none",
              all(q[2] in (None, "Science", "Art", "Commercial") for q in served))
    else:
        check(f"the {bank_id.split('_')[-1]} paper has no department questions", all(q[2] is None for q in served))
    # Right on the Art questions and in English; wrong on Science and Commercial; half right in Mathematics.
    for i, (qid, right, department) in enumerate(served):
        if department == "Art" or "english" in bank_id:
            choice = right
        elif department in ("Science", "Commercial"):
            choice = (right + 1) % 4
        elif "mathematics" in bank_id:
            choice = right if i % 2 == 0 else (right + 1) % 4
        else:
            choice = right
        candidate.client.post("/answer", data={"question_id": qid, "option_index": choice, "_csrf_token": token}, base_url=ALPHA)
    candidate.client.post("/submit", data={"_csrf_token": token}, base_url=ALPHA)
check("all three papers were submitted and marked",
      all(one("SELECT status FROM attempts WHERE id = :a", a=a) == "submitted" for a in attempts.values()))
# The junior candidate sits one paper, so they are on the waitlist too.
junior = Person(ALPHA)
junior.post("/login", {"username": JSS_CODE, "password": JSS_PASS}, page="/login")
jpaper = one("SELECT id FROM candidate_papers WHERE candidate_id = :c ORDER BY slot LIMIT 1", c=JSS)
junior.post(f"/candidate/papers/{jpaper}/start", page="/candidate/dashboard")
junior.client.post("/submit", data={"_csrf_token": csrf_from(junior.get("/exam").get_data(as_text=True))}, base_url=ALPHA)

# ================================================================ C. the recommendation
waitlist = op.text("/admin/candidates/admissions")
# (the row holds the admit form, whose department table has rows of its own: read up to the row's Decline form)
row = waitlist.split("Ngozi Science")[1].split("/decline")[0] if "Ngozi Science" in waitlist else ""
check("the waitlist shows the SSS candidate's choice and suggests the department they did best in",
      "Chose <b>Science</b>" in row and "Suggest Art" in row, row[:300])
check("…with the reason, the three departments and the department picker starting at the suggestion",
      "Stronger in Art" in row and "than in their choice, Science" in row and 'name="department"' in row
      and re.search(r'<option value="Art" selected>Art \(recommended\)', row) is not None
      and "Science (their choice)" in row)
jrow = waitlist.split("Ada Junior")[1].split("</tr>")[0] if "Ada Junior" in waitlist else ""
check("a JSS candidate's row has no department at all", jrow != "" and "Chose" not in jrow and 'name="department"' not in jrow)
detail = op.text(f"/admin/candidates/{SCI}")
check("the candidate's own page shows the department panel, their choice and the recommendation",
      'id="department"' in detail and "Recommended: Art." in detail and "Department choice" in detail)

# ================================================================ D. admitting
SSS1 = one("SELECT id FROM school_classes WHERE name = 'SSS 1'")
JSS1 = one("SELECT id FROM school_classes WHERE name = 'JSS 1'")
admit = {"first_name": "Ngozi", "last_name": "Science", "class_id": str(SSS1)}
r = op.post(f"/admin/candidates/{SCI}/admit", {**admit, "department": "Chemistry"}, page="/admin/candidates/admissions")
check("a department that does not exist is refused when admitting, and nobody is admitted",
      r.status_code == 302 and "science, art or commercial" in op.said()
      and one("SELECT admission_status FROM candidates WHERE id = :c", c=SCI) == "pending")
r = op.post(f"/admin/candidates/{SCI}/admit", {**admit, "department": "Commercial"}, page="/admin/candidates/admissions")
student = one("SELECT admitted_student_id FROM candidates WHERE id = :c", c=SCI)
check("the officer may choose a department other than the one suggested, and it is the student's department",
      r.status_code == 200 and student is not None and one("SELECT department FROM students WHERE id = :s", s=student) == "Commercial")
check("…and the admitted page says so", "Commercial department" in html.unescape(r.get_data(as_text=True)))
r = op.post(f"/admin/candidates/{ART}/admit", {"first_name": "Tunde", "last_name": "Art", "class_id": str(JSS1), "department": "Art"},
            page="/admin/candidates/admissions")
# Tunde sat no paper, so he is not on the waitlist; admitting still works by address, and shows the rule.
student2 = one("SELECT admitted_student_id FROM candidates WHERE id = :c", c=ART)
check("admitting into a junior class keeps no department", student2 is not None and one("SELECT department FROM students WHERE id = :s", s=student2) is None)

# ================================================================ E. tagging questions
BANK = "starter_sss1_mathematics"
r = op.post(f"/admin/banks/{BANK}/questions/new", {"text": "What is 7 x 8?", "option0": "54", "option1": "56", "option2": "58",
                                                   "option3": "64", "answer": "1", "points": "1", "department": "Science"},
            page=f"/admin/banks/{BANK}/questions/new", content_type="multipart/form-data")
bank_page = op.text(f"/admin/banks/{BANK}")
check("the question form saves a question's department, and the bank shows it", r.status_code == 302 and 'title="Tests the Science department"' in bank_page)
qid = max(int(x) for x in re.findall(rf'/admin/banks/{BANK}/questions/(\d+)/edit', bank_page))
op.post(f"/admin/banks/{BANK}/questions/{qid}/edit", {"text": "What is 7 x 8?", "option0": "54", "option1": "56", "option2": "58",
                                                      "option3": "64", "answer": "1", "points": "1", "department": ""},
        page=f"/admin/banks/{BANK}/questions/{qid}/edit", content_type="multipart/form-data")
check("…and clears it when set back to general", 'title="Tests the Science department"' not in op.text(f"/admin/banks/{BANK}"))


def import_bank(questions):
    payload = {"id": "dept_import_test", "name": "Departments import", "questions": questions}
    data = {"bank_file": (io.BytesIO(json.dumps(payload).encode()), "bank.json")}
    return op.post("/admin/banks/import", data, page="/admin/banks/import", content_type="multipart/form-data")


q = lambda i, department: {"id": i, "text": f"Question {i}", "options": ["a", "b", "c", "d"], "answer": 0, "department": department}  # noqa: E731
r = import_bank([q(1, "Physics")])
check("a bank file with an unknown department is refused, saying which values are allowed",
      r.status_code == 400 and "must be Science, Art or Commercial" in html.unescape(r.get_data(as_text=True)))
r = import_bank([q(1, "Arts"), q(2, "commerce"), q(3, None)])
imported = op.text("/admin/banks/dept_import_test")
check("…while one using Arts and commerce is imported, read as Art and Commercial",
      r.status_code in (200, 302) and 'title="Tests the Art department"' in imported and 'title="Tests the Commercial department"' in imported)

check("no server error was logged by any request in this run", not errors.seen, repr(errors.seen[:2]))
passed = sum(1 for _, ok, _ in results if ok)
print(f"\n{passed}/{len(results)} checks passed")
sys.exit(0 if passed == len(results) else 1)
