"""Exam mode and the redesigned paper pages, proven end to end on PostgreSQL. In plain words:

A. A school examination: it starts in exam mode with shuffling on. Starting it gives the browser a device tag (only
   its hash is stored). The whole paper comes on one page, in the student's own shuffled order, never with a correct
   answer or a marking guide; each shuffled question still marks the right option right. Answers save in the
   background. The page's reports are recorded (only known kinds, only with the form token, only for the student's
   own open paper). Opening the paper in another browser is recorded, never refused. Handing it in grades it.
B. Limits: with "end after 2 times leaving", the third time ends the paper on the server, and it is logged. With exam
   mode switched off, reports are refused. A limit outside 1-50 is refused. The paper's page shows the record.
C. The entrance examination: the same device tag, the whole paper on one page with the candidate's card, reports
   recorded, another browser recorded, and the admin's result page shows the candidate's record.
"""
import atexit
import os
import re
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="beacon_exammode_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('exammode')
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
from werkzeug.security import generate_password_hash  # noqa: E402

import app as A  # noqa: E402
A.app.config['BACKGROUND_INLINE'] = True
from control_plane import provisioning as pv  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import engine_for  # noqa: E402

results, PROBLEMS = [], []
PL = "http://platform.test"
UA = "http://alpha.portal.test"


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)


def token_in(body):
    found = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', body) or re.search(r'id="csrfToken" value="([^"]+)"', body)
    return found.group(1) if found else ""


class Person:
    count = [0]

    def __init__(self, label):
        self.c, self.label = A.app.test_client(), label
        Person.count[0] += 1
        self.addr = f"10.93.{Person.count[0]}.1"

    def get(self, path):
        r = self.c.get(path, base_url=UA, environ_base={"REMOTE_ADDR": self.addr})
        if r.status_code >= 500:
            PROBLEMS.append(f"{self.label}: GET {path} {r.status_code}")
        return r

    def text(self, path):
        return self.get(path).get_data(as_text=True)

    def post(self, path, data=None, page=None, token=None):
        data = dict(data or {})
        if token is not False:
            data["_csrf_token"] = token or token_in(self.text(page or "/admin/password"))
        r = self.c.post(path, data=data, base_url=UA, environ_base={"REMOTE_ADDR": self.addr})
        if r.status_code >= 500:
            PROBLEMS.append(f"{self.label}: POST {path} {r.status_code}")
        return r

    def said(self):
        with self.c.session_transaction(base_url=UA) as sess:
            return " | ".join(t for _, t in sess.pop("_flashes", [])).lower()


pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
console = A.app.test_client()
console.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                      "_csrf_token": token_in(console.get("/platform/login", base_url=PL).get_data(as_text=True))}, base_url=PL)
console.post("/platform/schools/new", data={"_csrf_token": token_in(console.get("/platform/schools/new", base_url=PL).get_data(as_text=True)),
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


def operator():
    page = console.get("/platform/schools/alpha", base_url=PL).get_data(as_text=True)
    r = console.post("/platform/schools/alpha/enter", data={"_csrf_token": token_in(page)}, base_url=PL)
    person = Person("operator")
    person.get(r.headers["Location"][len(UA):])
    return person


op = operator()
op.get("/admin/workspace/school")
PASSWORD = "Fixture-password-9"
HASH = generate_password_hash(PASSWORD, method="pbkdf2:sha256:1000")
J1 = one("SELECT id FROM school_classes WHERE name = 'JSS 1'")
SESSION = one("SELECT id FROM academic_sessions WHERE is_current = 1")
op.post("/admin/school/subjects/new", {"name": "Mathematics", "code": "MTH", "class_ids": [J1]}, page="/admin/school/subjects/new")
MATHS = one("SELECT id FROM school_subjects WHERE name = 'Mathematics'")

with A.app.app_context():
    from control_plane.context import tenant_context
    from datetime import datetime, timezone
    with tenant_context(INFO):
        from models import Student, StudentEnrolment, db
        now = datetime.now(timezone.utc).isoformat()
        school_id = one("SELECT id FROM schools LIMIT 1")
        STU = {}
        for code, first in (("EM-1", "Ada"), ("EM-2", "Bayo"), ("EM-3", "Chidi")):
            st = Student(admission_no=code, student_number=code, first_name=first, last_name="Test", created_at=now, active=1,
                         school_id=school_id, login_username=code.lower(), login_password_hash=HASH, account_active=1,
                         password_must_change=0, gender="Female")
            db.session.add(st)
            db.session.flush()
            db.session.add(StudentEnrolment(student_id=st.id, class_id=J1, session_id=SESSION, enrolled_at=now, active=1))
            STU[first] = st.id
        db.session.commit()
        db.session.remove()


def student(code):
    person = Person(code)
    person.post("/login", {"username": code.lower(), "password": PASSWORD}, page="/login")
    return person


def make_exam(title):
    op.post("/admin/school/examinations/new", {"title": title, "instructions": "", "class_id": J1, "subject_id": MATHS,
                                               "duration_minutes": "60", "session_id": SESSION, "term": "First Term",
                                               "delivery": "cbt"}, page="/admin/school/examinations/new")
    aid = one("SELECT id FROM school_assessments WHERE title = :t", t=title)
    detail = f"/admin/school/assessments/{aid}"
    for n, right in enumerate((0, 1, 2, 3)):
        op.post(f"{detail}/questions/new", {"question_type": "objective", "question_text": f"Q{n + 1}?", "instruction": "",
                                           "option_a": f"q{n + 1}-a", "option_b": f"q{n + 1}-b", "option_c": f"q{n + 1}-c",
                                           "option_d": f"q{n + 1}-d", "correct_option": right, "points": 1}, page=detail)
    op.post(f"{detail}/questions/new", {"question_type": "theory", "question_text": "Explain.", "marking_guide": "the guide text",
                                       "points": 5}, page=detail)
    op.post(f"{detail}/toggle", {}, page=detail)
    return aid, detail


# ================================================================ A. a school examination
EXAM, DETAIL = make_exam("Maths exam")
check("a new examination starts in exam mode, with shuffling on and no leave limit",
      sql("SELECT exam_mode, shuffle, exam_leave_limit FROM school_assessments WHERE id = :a", a=EXAM) == [(1, 1, None)])
TAKE = f"/student/assessments/{EXAM}"
ada = student("EM-1")
start_page = ada.text(TAKE)
check("the start page shows the student and exam mode's rules", "Ada Test" in start_page and "Exam mode" in start_page
      and "full screen" in start_page)
r = ada.post(f"{TAKE}/start", {}, token=token_in(start_page))
ADA_ATTEMPT = one("SELECT id FROM school_assessment_attempts WHERE student_id = :s", s=STU["Ada"])
cookie = r.headers.get("Set-Cookie", "")
stored = one("SELECT device_tag FROM school_assessment_attempts WHERE id = :i", i=ADA_ATTEMPT)
raw = re.search(rf"bs_exam_school_{ADA_ATTEMPT}=([^;]+)", cookie)
check("starting gives the browser a device tag; only its hash is stored",
      raw and stored and raw.group(1) != stored and len(stored) == 64 and "HttpOnly" in cookie)
page = ada.text(f"{TAKE}?q=1")
check("the whole paper comes on one page, in exam mode, with the student's card",
      page.count("data-pq ") == 5 and 'data-paper-mode="record"' in page and "data-paper-gate" in page and "EM-1" in page)
check("…never with a marking guide or a correct answer", "the guide text" not in page and "correct_option" not in page)
frozen = sql("SELECT q.question_id, q.option_a, q.option_b, q.option_c, q.option_d, q.correct_option FROM school_assessment_attempt_questions q "
             "WHERE q.attempt_id = :t AND q.question_type = 'objective'", t=ADA_ATTEMPT)
live = {qid: (opts, right) for qid, *opts, right in sql(
    "SELECT id, option_a, option_b, option_c, option_d, correct_option FROM school_questions WHERE assessment_id = :a AND question_type = 'objective'", a=EXAM)}
check("each shuffled question still points at the right option",
      len(frozen) == 4 and all(sorted(opts) == sorted(live[qid][0]) and opts[right] == live[qid][0][live[qid][1]]
                               for qid, *opts, right in frozen), str(frozen))
token = token_in(page)
for qid, *opts, right in frozen:
    r = ada.post(f"{TAKE}/answer", {"attempt_id": ADA_ATTEMPT, "question_id": qid, "option_index": right, "autosave": "1"}, token=token)
check("an option is saved in the background (JSON)", r.status_code == 200 and r.get_json().get("ok")
      and one("SELECT COUNT(*) FROM school_assessment_answers WHERE attempt_id = :t", t=ADA_ATTEMPT) == 4)
r = ada.post(f"{TAKE}/integrity", {"kind": "left_page", "question": "2"}, token=token)
check("leaving the page is recorded", r.status_code == 200 and r.get_json().get("left") == 1
      and sql("SELECT kind, question_number FROM exam_integrity_events WHERE attempt_id = :t", t=ADA_ATTEMPT) == [("left_page", 2)])
check("an unknown kind is refused", ada.post(f"{TAKE}/integrity", {"kind": "anything"}, token=token).status_code == 400)
check("…and so is 'another device' from the page (only the server decides that)",
      ada.post(f"{TAKE}/integrity", {"kind": "new_device"}, token=token).status_code == 400)
r = ada.post(f"{TAKE}/integrity", {"kind": "copy"}, token=False)
check("a report without the form token is refused", r.status_code in (400, 403)
      and one("SELECT COUNT(*) FROM exam_integrity_events WHERE attempt_id = :t", t=ADA_ATTEMPT) == 1)
other = student("EM-1")
other_page = other.get(f"{TAKE}?q=1")
check("opening the paper in another browser is recorded, never refused",
      other_page.status_code == 200 and one("SELECT COUNT(*) FROM exam_integrity_events WHERE attempt_id = :t AND kind = 'new_device'", t=ADA_ATTEMPT) == 1)
ada.text(f"{TAKE}?q=2")
check("…while the browser that started it is not", one("SELECT COUNT(*) FROM exam_integrity_events WHERE kind = 'new_device'") == 1)
chidi = student("EM-3")
r = chidi.post(f"{TAKE}/integrity", {"kind": "left_page"}, page="/student/password")
check("a student with no open paper cannot report", r.status_code == 409)
r = ada.post(f"{TAKE}/submit", {}, token=token)
check("handing it in grades it (all four objective right)", r.status_code == 302
      and sql("SELECT status, score FROM school_assessment_attempts WHERE id = :t", t=ADA_ATTEMPT) == [("submitted", 4)])
r = ada.post(f"{TAKE}/integrity", {"kind": "left_page"}, token=token)
check("…after which reports are refused", r.status_code == 409)

# ================================================================ B. limits, off, the record
op.post(f"{DETAIL}/edit", {"title": "Maths exam", "instructions": "", "class_id": J1, "subject_id": MATHS, "duration_minutes": "60",
                           "session_id": SESSION, "term": "First Term", "delivery": "cbt", "exam_settings": "1", "exam_mode": "1",
                           "shuffle": "1", "exam_leave_limit": "99"}, page=DETAIL)
check("a leave limit outside 1-50 is refused", "from 1 to 50" in op.said()
      and one("SELECT exam_leave_limit FROM school_assessments WHERE id = :a", a=EXAM) is None)
op.post(f"{DETAIL}/edit", {"title": "Maths exam", "instructions": "", "class_id": J1, "subject_id": MATHS, "duration_minutes": "60",
                           "session_id": SESSION, "term": "First Term", "delivery": "cbt", "exam_settings": "1", "exam_mode": "1",
                           "shuffle": "1", "exam_leave_limit": "2"}, page=DETAIL)
check("the teacher sets 'end after 2 times leaving'", one("SELECT exam_leave_limit FROM school_assessments WHERE id = :a", a=EXAM) == 2)
bayo = student("EM-2")
bayo_start = bayo.text(TAKE)
check("…and the start page tells the student", "After 2 times your paper is submitted for you" in bayo_start)
bayo.post(f"{TAKE}/start", {}, token=token_in(bayo_start))
bt = token_in(bayo.text(f"{TAKE}?q=1"))
answers = [bayo.post(f"{TAKE}/integrity", {"kind": "left_page"}, token=bt).get_json() for _ in range(3)]
BAYO_ATTEMPT = one("SELECT id FROM school_assessment_attempts WHERE student_id = :s", s=STU["Bayo"])
check("the third time ends the paper on the server", [a.get("ended") for a in answers] == [False, False, True]
      and one("SELECT status FROM school_assessment_attempts WHERE id = :t", t=BAYO_ATTEMPT) == "submitted")
check("…and it is in the activity log", one("SELECT COUNT(*) FROM audit_logs WHERE action = 'school_assessment_ended_by_exam_mode'") == 1)
record = op.text(DETAIL)
check("the paper's page shows the record: who left, how often, and who exam mode ended",
      "Exam mode record" in record and "Ended by exam mode" in record and "Test Bayo" in record)
EXAM2, DETAIL2 = make_exam("Maths test, no exam mode")
op.post(f"{DETAIL2}/edit", {"title": "Maths test, no exam mode", "instructions": "", "class_id": J1, "subject_id": MATHS,
                            "duration_minutes": "60", "session_id": SESSION, "term": "First Term", "delivery": "cbt",
                            "exam_settings": "1"}, page=DETAIL2)
check("exam mode and shuffling can be switched off", sql("SELECT exam_mode, shuffle FROM school_assessments WHERE id = :a", a=EXAM2) == [(0, 0)])
TAKE2 = f"/student/assessments/{EXAM2}"
start2 = chidi.text(TAKE2)
chidi.post(f"{TAKE2}/start", {}, token=token_in(start2))
page2 = chidi.text(f"{TAKE2}?q=1")
CHIDI2 = one("SELECT id FROM school_assessment_attempts WHERE student_id = :s AND assessment_id = :a", s=STU["Chidi"], a=EXAM2)
order = [q for (q,) in sql("SELECT question_id FROM school_assessment_attempt_questions WHERE attempt_id = :t ORDER BY question_order", t=CHIDI2)]
live_order = [q for (q,) in sql("SELECT id FROM school_questions WHERE assessment_id = :a ORDER BY sort_order", a=EXAM2)]
check("…then the paper keeps its own order and opens without exam mode",
      order == live_order and 'data-paper-mode="off"' in page2 and "data-paper-gate" not in page2)
check("…and reports are refused", chidi.post(f"{TAKE2}/integrity", {"kind": "left_page"}, token=token_in(page2)).status_code == 409)

# ================================================================ C. the entrance examination
ent = operator()
ent.get("/admin/workspace/entrance")
ent.post("/admin/entrance-config/standard", {}, page="/admin/entrance-config")
ent.post("/admin/entrance-config/save", {"bank_id": "starter_year7_mathematics", "entry_group": "year7", "subject": "mathematics",
                                         "session_id": str(SESSION), "questions_to_serve": "20"}, page="/admin/entrance-config")
r = ent.post("/admin/candidates/new", {"candidate_name": "Ngozi Eze", "target_class": "JSS 1", "school_attended": "Sunrise Primary",
                                      "parent_guardian_name": "Mrs Eze", "parent_guardian_relationship": "Mother",
                                      "primary_mobile": "08030000001"}, page="/admin/candidates/new")
code, password = re.findall(r'class="credential-code">([^<]+)<', r.get_data(as_text=True))[:2]
cand = Person("candidate")
cand.post("/login", {"username": code, "password": password}, page="/login")
dash = cand.text("/candidate/dashboard")
paper_id = re.findall(r"/candidate/papers/(\d+)/start", dash)[0]
r = cand.post(f"/candidate/papers/{paper_id}/start", {}, token=token_in(dash))
ATT = one("SELECT id FROM attempts ORDER BY id DESC LIMIT 1")
check("the entrance paper gives the browser a device tag too",
      f"bs_exam_entrance_{ATT}=" in r.headers.get("Set-Cookie", "") and len(one("SELECT device_tag FROM attempts WHERE id = :i", i=ATT) or "") == 64)
exam = cand.text("/exam?q=1")
check("the whole entrance paper is on one page, in exam mode, with the candidate's card",
      exam.count("data-pq ") == 20 and 'data-paper-mode="record"' in exam and code in exam and "Ngozi Eze" in exam
      and "Question 1 of 20" in exam)
et = token_in(exam)
first_q = re.search(r'data-qid="(\d+)"', exam).group(1)
r = cand.post("/answer", {"question_id": first_q, "option_index": 0}, token=et)
check("an entrance answer still saves in the background", r.status_code == 200 and r.get_json().get("ok"))
r = cand.post("/exam/integrity", {"kind": "fullscreen_exit", "question": "3"}, token=et)
check("leaving full screen in the entrance paper is recorded", r.status_code == 200
      and sql("SELECT kind FROM exam_integrity_events WHERE scope = 'entrance' AND attempt_id = :t", t=ATT) == [("fullscreen_exit",)])
check("an unknown kind is refused", cand.post("/exam/integrity", {"kind": "nope"}, token=et).status_code == 400)
again = Person("candidate elsewhere")
again.post("/login", {"username": code, "password": password}, page="/login")
again.get("/candidate/dashboard")
d2 = again.text("/candidate/dashboard")
again.post(f"/candidate/papers/{paper_id}/start", {}, token=token_in(d2))
again.get("/exam?q=1")
check("opening the entrance paper in another browser is recorded",
      one("SELECT COUNT(*) FROM exam_integrity_events WHERE scope = 'entrance' AND attempt_id = :t AND kind = 'new_device'", t=ATT) == 1)
detail = ent.text(f"/admin/results/{ATT}")
check("the admin's result page shows the candidate's record", "Exam mode record" in detail and "Left full screen: 1" in detail
      and "Opened on another device: 1" in detail)

check("no page answered with a server error", not PROBLEMS, "; ".join(PROBLEMS[:5]))
failed = [name for name, ok, _ in results if not ok]
print()
print(f"{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", "; ".join(failed))
    raise SystemExit(1)
