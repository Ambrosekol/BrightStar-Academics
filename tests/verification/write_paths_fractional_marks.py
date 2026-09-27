"""Entrance marks can be fractions: a 40-question paper is 100 marks, 2.5 a question.

The mark columns used to be whole-number columns, so 2.5 was stored as 2 and a "100-mark" paper of
40 questions added up to 80 (the percentage still looked right, which hid it). This checks that:

  * an older school database, whose mark columns are whole-number columns, is upgraded in place
    without changing a single stored mark, and that upgrading again does nothing more;
  * a real 40-question paper, sat by a candidate through the real pages, is graded out of exactly
    100 with 2.5 marks for each right answer, and a paper with a fractional total is exact too;
  * whole marks still read as whole marks on every page and in the exports ("100", not "100.0"),
    while a fractional mark shows its decimals ("62.5");
  * the counts the paper set-up accepts are exactly the ones that make each question worth a
    whole number of hundredths of a mark, so no paper can add up to something other than 100.

Run:  python tests/verification/write_paths_fractional_marks.py
"""
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_marks_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('marks')
os.environ.update({
    "BRIGHTSTARS_TENANTS_DIR": os.path.join(TMP, "tenants"),
    "BRIGHTSTARS_PLATFORM_HOSTS": "platform.test",
    "BRIGHTSTARS_PORTAL_DOMAIN": "portal.test",
    "BRIGHTSTARS_REGISTRY_CACHE_SECONDS": "0",
    "BRIGHTSTARS_SECRET": "x" * 40,
})
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
os.chdir(ROOT)

import sqlalchemy as sa  # noqa: E402

import app as A  # noqa: E402
from control_plane import provisioning as pv  # noqa: E402
from control_plane.context import tenant_context  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import dispose_engines, engine_for  # noqa: E402
from core import marks  # noqa: E402
from core.entrance import _valid_question_configuration  # noqa: E402
from models import Attempt, AttemptQuestion  # noqa: E402

results = []
PL = "http://platform.test"


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


def csrf(c, path, base):
    body = c.get(path, base_url=base).get_data(as_text=True)
    marker = 'name="_csrf_token" value="'
    start = body.index(marker) + len(marker)
    return body[start:body.index('"', start)]


def info_for(slug):
    with platform_session() as s:
        tenant = get_tenant(s, slug)
        return to_info(tenant) if tenant else None


def in_school(slug, fn):
    with A.app.app_context(), tenant_context(info_for(slug)):
        return fn()


def sql(slug, statement, **params):
    with engine_for(info_for(slug)).begin() as conn:
        result = conn.execute(sa.text(statement), params)
        return result.fetchall() if result.returns_rows else None


def column_types(slug):
    rows = sql(slug, "SELECT table_name, column_name, data_type FROM information_schema.columns "
                     "WHERE table_schema = current_schema() AND ((table_name = 'attempt_questions' AND column_name = 'points') "
                     "OR (table_name = 'attempts' AND column_name IN ('score', 'max_score')))")
    return {(t, c): d for t, c, d in rows}


# ================================================================ the helpers themselves
check("a whole mark reads as a whole number", marks.tidy(100.0) == 100 and isinstance(marks.tidy(100.0), int))
check("a fractional mark keeps its decimals", marks.tidy(62.5) == 62.5)
check("float noise from adding marks is taken off", marks.total([2.5] * 40) == 100 and marks.total([0.1] * 3) == 0.3)
check("nothing and None count as no marks", marks.total([]) == 0 and marks.total([None, 2.5]) == 2.5 and marks.tidy(None) is None)

# The paper set-up accepts a count only if the marks per question come to exactly 100.
accepted = [n for n in range(1, 201) if _valid_question_configuration(n)[0]]
check("only counts that split 100 marks exactly are accepted (2.5 a question is fine, 3.33 is not)",
      accepted and all(abs(round(100.0 / n, 2) * n - 100.0) < 1e-9 for n in accepted), str(accepted))
check("…including 40 and 80, which need decimals, and not 30 or 32",
      {40, 80} <= set(accepted) and not ({30, 32, 3, 6, 7} & set(accepted)), str(accepted))

# ================================================================ a school, and a candidate at a 40-question paper
pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
console = A.app.test_client()
console.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                      "_csrf_token": csrf(console, "/platform/login", PL)}, base_url=PL)
console.post("/platform/schools/new", data={
    "_csrf_token": csrf(console, "/platform/schools/new", PL), "name": "Alpha School", "code": "alpha",
    "starter_banks": "1"}, base_url=PL, content_type="multipart/form-data")
UA = "http://alpha.portal.test"


def operator():
    r = console.post("/platform/schools/alpha/enter",
                     data={"_csrf_token": csrf(console, "/platform/schools/alpha", PL)}, base_url=PL)
    c = A.app.test_client()
    c.get(r.headers["Location"][len(UA):], base_url=UA)
    c.get("/admin/workspace/entrance", base_url=UA)
    return c


admin = operator()
admin.post("/admin/entrance-config/standard", data={"_csrf_token": csrf(admin, "/admin/entrance-config", UA)}, base_url=UA)

# Year 7 Mathematics has 44 questions: serve 40 of them, so each is worth 2.5.
session_id = in_school("alpha", lambda: A.db.session.scalar(sa.text(
    "SELECT id FROM academic_sessions WHERE is_current = 1")))
r = admin.post("/admin/entrance-config/save", data={
    "bank_id": "starter_year7_mathematics", "entry_group": "year7", "subject": "mathematics",
    "session_id": str(session_id), "questions_to_serve": "40", "_csrf_token": csrf(admin, "/admin/entrance-config", UA)},
    base_url=UA)
served = sql("alpha", "SELECT questions_to_serve, marks_per_question FROM entrance_bank_configs "
                      "WHERE bank_id = 'starter_year7_mathematics' ORDER BY id DESC LIMIT 1")
check("a school can set a paper to 40 questions, which is 2.5 marks each",
      r.status_code == 302 and served == [(40, 2.5)], f"{r.status_code} {served}")

r = admin.post("/admin/candidates/new", data={
    "candidate_name": "Ada Obi", "target_class": "JSS 1", "school_attended": "Sunrise Primary",
    "parent_guardian_name": "Mrs Obi", "parent_guardian_relationship": "Mother", "primary_mobile": "08030000001",
    "_csrf_token": csrf(admin, "/admin/candidates/new", UA)}, base_url=UA)
codes = re.findall(r'class="credential-code">([^<]+)<', r.get_data(as_text=True))
check("a candidate is registered for the papers", len(codes) == 2, r.get_data(as_text=True)[:160] if len(codes) != 2 else "")
code, password = codes

cand = A.app.test_client()
cand.post("/login", data={"username": code, "password": password, "_csrf_token": csrf(cand, "/login", UA)}, base_url=UA)
dash = cand.get("/candidate/dashboard", base_url=UA).get_data(as_text=True)
paper_ids = re.findall(r"/candidate/papers/(\d+)/start", dash)
token = re.search(r'name="_csrf_token" value="([^"]+)"', dash).group(1)
r = cand.post(f"/candidate/papers/{paper_ids[0]}/start", data={"_csrf_token": token}, base_url=UA)
check("the candidate starts the 40-question paper", r.status_code == 302 and "/exam" in r.headers["Location"])
exam = cand.get("/exam?q=1", base_url=UA).get_data(as_text=True)
check("…which serves 40 questions", "Question 1 of 40" in exam)
exam_token = re.search(r'id="csrfToken" value="([^"]+)"', exam).group(1)


def frozen():
    def go():
        attempt = A.db.session.scalars(sa.select(Attempt).order_by(Attempt.id.desc())).first()
        rows = A.db.session.execute(sa.select(AttemptQuestion.question_id, AttemptQuestion.correct_option,
                                              AttemptQuestion.points).where(AttemptQuestion.attempt_id == attempt.id)).all()
        return attempt.id, rows
    return in_school("alpha", go)


attempt_id, rows = frozen()
check("each frozen question is worth exactly 2.5 marks, not 2",
      len(rows) == 40 and {float(p) for _, _, p in rows} == {2.5}, str({float(p) for _, _, p in rows}))

# Answer 30 of the 40 correctly and 5 wrongly, leave 5 blank: 30 x 2.5 = 75 out of 100.
right = 30
for n, (question_id, correct, _) in enumerate(rows):
    if n < right:
        choice = correct
    elif n < right + 5:
        choice = (correct + 1) % 4
    else:
        continue
    cand.post("/answer", data={"question_id": question_id, "option_index": choice, "_csrf_token": exam_token}, base_url=UA)
r = cand.post("/submit", data={"_csrf_token": exam_token}, base_url=UA)
check("the candidate submits", r.status_code == 302, str(r.status_code))
attempt = sql("alpha", "SELECT status, score, max_score, percentage FROM attempts WHERE id = :i", i=attempt_id)[0]
check("the paper is graded out of exactly 100, not 80", attempt[2] == 100.0, str(attempt))
check("…30 right answers are worth exactly 75 marks", attempt[1] == 75.0, str(attempt))
check("…and the percentage agrees", abs(attempt[3] - 75.0) < 1e-9, str(attempt))

# A fractional total: change one question's snapshot to 0.5 marks and grade again.
sql("alpha", "UPDATE attempt_questions SET points = 0.5 WHERE attempt_id = :i AND question_order = 1", i=attempt_id)
in_school("alpha", lambda: __import__("core.entrance", fromlist=["grade"]).grade(attempt_id, force=True))
attempt = sql("alpha", "SELECT score, max_score FROM attempts WHERE id = :i", i=attempt_id)[0]
check("a total that is not a whole number is stored exactly (73.0 of 98.0 after one question is halved)",
      attempt == (73.0, 98.0), str(attempt))
sql("alpha", "UPDATE attempt_questions SET points = 2.5 WHERE attempt_id = :i", i=attempt_id)
in_school("alpha", lambda: __import__("core.entrance", fromlist=["grade"]).grade(attempt_id, force=True))

# ================================================================ how it reads on the pages
for label, path in (("the attempts list", "/admin/attempts"), ("the results summary", "/admin/results/summary"),
                    ("the ranking", "/admin/rankings")):
    page = admin.get(path, base_url=UA)
    body = page.get_data(as_text=True)
    check(f"{label} loads", page.status_code == 200 and "Traceback" not in body,
          f"{page.status_code} {page.headers.get('Location', '')}")
    # (A percentage such as "75.0%" is meant to have its decimal; it is the marks that must not.)
    check(f"{label} shows whole marks as whole numbers",
          not re.search(r"75\.0\s*/\s*100|/\s*100\.0|>\s*75\.0\s*<|>\s*100\.0\s*<", body), path)
    # (The ranking lists only candidates who have finished all three papers, so it has no marks yet.)
    if path != "/admin/rankings":
        check(f"{label} does show the marks", re.search(r"75\s*/\s*100|>\s*75\s*<", body) is not None, path)
detail = admin.get(f"/admin/results/{attempt_id}", base_url=UA)
text = detail.get_data(as_text=True)
check("a result's detail page reads '75 / 100'", detail.status_code == 200 and "75 / 100" in text, str(detail.status_code))

csv_body = admin.get("/admin/export/rankings.csv", base_url=UA).get_data(as_text=True)
check("the rankings export writes the marks as 75 and 100, not 75.0 and 100.0",
      ",75,100,75.0," in csv_body, csv_body[:200])
payload = json.loads(admin.get("/admin/export/results.json", base_url=UA).get_data(as_text=True))
check("the JSON export carries whole marks as whole numbers",
      payload and payload[0]["score"] == 75 and payload[0]["max_score"] == 100
      and isinstance(payload[0]["score"], int), str(payload[:1]))

# ================================================================ an older database is upgraded in place
check("a school made today already has decimal mark columns",
      set(column_types("alpha").values()) == {"double precision"}, str(column_types("alpha")))

# Make it look like a school from before: whole-number columns holding an 80-out-of-80 paper.
for table, column in (("attempt_questions", "points"), ("attempts", "score"), ("attempts", "max_score")):
    sql("alpha", f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE integer USING round("{column}")')
old = sql("alpha", "SELECT id, score, max_score FROM attempts ORDER BY id")
check("(set-up) the columns are whole-number columns again, with the old marks in them",
      set(column_types("alpha").values()) == {"integer"} and old == [(attempt_id, 75, 100)], str((column_types("alpha"), old)))

pv.upgrade_tenant(info_for("alpha"))
check("upgrading turns all three columns into decimals", set(column_types("alpha").values()) == {"double precision"},
      str(column_types("alpha")))
check("…without changing a stored mark", sql("alpha", "SELECT id, score, max_score FROM attempts ORDER BY id") == [(attempt_id, 75.0, 100.0)])
check("…and the default of 1 mark a question survives",
      sql("alpha", "SELECT column_default FROM information_schema.columns WHERE table_name = 'attempt_questions' "
                   "AND column_name = 'points' AND table_schema = current_schema()")[0][0].startswith("1"))
pv.upgrade_tenant(info_for("alpha"))
check("upgrading again does nothing more", set(column_types("alpha").values()) == {"double precision"})
sql("alpha", "UPDATE attempt_questions SET points = 2.5 WHERE attempt_id = :i AND question_order = 2", i=attempt_id)
check("…and a decimal mark can now be stored in an upgraded school",
      sql("alpha", "SELECT points FROM attempt_questions WHERE attempt_id = :i AND question_order = 2", i=attempt_id) == [(2.5,)])

dispose_engines()
DROP_TEST_DATABASES()
shutil.rmtree(TMP, ignore_errors=True)
print()
failed = [x for x in results if not x[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
