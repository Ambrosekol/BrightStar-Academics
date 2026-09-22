"""Sign-ins that must end, and sign-ins that must not, proven end to end on PostgreSQL.

* A restart signs everyone out (school staff, parents, students, candidates and the platform
  console), except people in the middle of an exam, who carry on exactly where they were: their
  answers, the heartbeat and the submit all still work after the restart. An exam that has run out
  of time protects nobody. An unreadable registry, or a server that never recorded a launch, signs
  nobody out.
* The check is cheap: nothing is asked of the registry for /health or shared static files, and a
  worker asks it only once in a few seconds.
* Changing or resetting a password ends the account's other sign-ins (staff, parent, student,
  candidate, platform admin) and never the sign-in that made the change.
* Every response names a Referrer-Policy; the password-reset pages are never cached and never
  send a referrer.
* BRIGHTSTARS_TRUSTED_PROXIES: off by default, so a forged X-Forwarded-* header changes nothing;
  on, only the client address and the scheme are believed (never the host); a bad value stops the
  application starting; the audit log records the connection's address, cut to 64 characters.

Run:  python tests/verification/write_paths_session_guard.py
"""
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_sessguard_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup("sessguard")
os.environ.update({
    "BRIGHTSTARS_TENANTS_DIR": os.path.join(TMP, "tenants"),
    "BRIGHTSTARS_PLATFORM_HOSTS": "platform.test",
    "BRIGHTSTARS_PORTAL_DOMAIN": "portal.test",
    "BRIGHTSTARS_REGISTRY_CACHE_SECONDS": "0",
    "BRIGHTSTARS_SECRET": "x" * 40,
    "BRIGHTSTARS_ENV": "development",
})
os.environ.pop("BRIGHTSTARS_TRUSTED_PROXIES", None)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
os.chdir(ROOT)

import sqlalchemy as sa  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

import app as A  # noqa: E402
from control_plane import config as cp_config  # noqa: E402
from control_plane import launch  # noqa: E402
from control_plane import provisioning as pv  # noqa: E402
from control_plane.context import tenant_context  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import dispose_engines, engine_for  # noqa: E402
from core import session_guard  # noqa: E402

results = []
PL = "http://platform.test"
ALPHA = "http://alpha.portal.test"
RESTARTED = "The system was restarted, please sign in again."
PASSWORD_CHANGED = "Your password was changed, please sign in again."


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


_addr = [0]


def next_addr():
    """Every sign-in "comes from" a new address: sign-in is limited per address, and this test
    signs people in a great many times."""
    _addr[0] += 1
    return f"10.77.{_addr[0] // 250}.{_addr[0] % 250 + 1}"


class Person:
    """A browser with its own cookies."""

    def __init__(self, base=ALPHA):
        self.base = base
        self.c = A.app.test_client()

    def get(self, path, **kw):
        return self.c.get(path, base_url=self.base, environ_base={"REMOTE_ADDR": "10.1.1.1"}, **kw)

    def post(self, path, data=None, **kw):
        return self.c.post(path, data=data or {}, base_url=self.base, environ_base={"REMOTE_ADDR": "10.1.1.1"}, **kw)

    def session(self):
        with self.c.session_transaction(base_url=self.base) as sess:
            return dict(sess)

    def token(self):
        with self.c.session_transaction(base_url=self.base) as sess:
            if not sess.get("_csrf_token"):
                sess["_csrf_token"] = "test-token-" + str(id(self))
            return sess["_csrf_token"]

    def form(self, path, data=None, **kw):
        return self.post(path, {**(data or {}), "_csrf_token": self.token()}, **kw)

    def sign_in(self, username, password):
        return self.c.post("/login", data={"username": username, "password": password}, base_url=self.base,
                           environ_base={"REMOTE_ADDR": next_addr()})

    def in_(self, path):
        """Whether a page opens for this person (200), rather than sending them to sign in."""
        return self.get(path).status_code == 200

    def sent_to_sign_in(self, path):
        r = self.get(path)
        return r.status_code == 302 and "/login" in r.headers.get("Location", "")


def info_for(slug):
    with platform_session() as s:
        tenant = get_tenant(s, slug)
        return to_info(tenant) if tenant else None


def in_school(fn):
    with A.app.app_context(), tenant_context(INFO):
        return fn()


def sql(statement, **params):
    with engine_for(INFO).begin() as conn:
        result = conn.execute(sa.text(statement), params)
        return result.fetchall() if result.returns_rows else None


def registry(statement, **params):
    with platform_session() as s:
        return s.execute(sa.text(statement), params).fetchall()


def restart():
    """What starting the server again does: a new launch id, before anything is served."""
    return launch.record_new_launch()


def page_text(response):
    return response.get_data(as_text=True)


NOW = datetime.now(timezone.utc)
iso = lambda dt: dt.isoformat()  # noqa: E731

# ================================================================ set-up: a school with every kind of person
pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
pv.create_tenant("alpha", "Alpha School", [], starter_banks=True)
INFO = info_for("alpha")
from models import (  # noqa: E402
    Admin, AdminType, Attempt, AcademicSession, Candidate, CandidatePaper, Examination, ParentAccount,
    ParentStudentLink,
    PasswordResetToken, SchoolAssessment, SchoolAssessmentAttempt, SchoolAssessmentAttemptQuestion,
    SchoolAssignment, SchoolAssignmentAttempt, SchoolClass, SchoolQuestion, SchoolSubject, Student,
    StudentEnrolment,
)

PASSWORD = "a-good-password-1"


def make_school():
    db = A.db
    role = db.session.scalars(sa.select(AdminType).where(AdminType.is_system == 1)).first()
    for username in ("boss", "clerk"):
        db.session.add(Admin(username=username, display_name=username.title(), admin_type_id=role.id, active=1,
                             password_hash=generate_password_hash(PASSWORD), password_must_change=0,
                             created_at=iso(NOW)))
    session_id = db.session.scalars(sa.select(AcademicSession.id).where(AcademicSession.is_current == 1)).first()
    class_id = db.session.scalars(sa.select(SchoolClass.id).where(SchoolClass.name == "JSS 1")).first()
    subject = SchoolSubject(name="Mathematics", code="MTH", created_at=iso(NOW))
    db.session.add(subject)
    db.session.flush()
    boss_id = db.session.scalars(sa.select(Admin.id).where(Admin.username == "boss")).first()
    assessment = SchoolAssessment(assessment_type="test", title="Maths test", class_id=class_id, subject_id=subject.id,
                                  session_id=session_id, duration_minutes=30, question_count=2, active=1,
                                  created_by=boss_id, created_at=iso(NOW), term="First Term")
    db.session.add(assessment)
    db.session.flush()
    for n in range(2):
        db.session.add(SchoolQuestion(assessment_id=assessment.id, question_text=f"Question {n + 1}?",
                                      option_a="A", option_b="B", option_c="C", option_d="D",
                                      correct_option=n, points=5, sort_order=n + 1))
    quiz = SchoolAssignment(title="Quiz", class_id=class_id, subject_id=subject.id, created_by=boss_id,
                            created_at=iso(NOW), assignment_type="quiz", timing_mode="untimed", session_id=session_id)
    db.session.add(quiz)
    students = {}
    for n, name in enumerate(("Ada", "Bola", "Chi", "Dayo", "Efe", "Gina")):
        s = Student(admission_no=f"ADM{n}", first_name=name, last_name="Test", created_at=iso(NOW), active=1,
                    login_username=f"STU{n}", login_password_hash=generate_password_hash(PASSWORD),
                    account_active=1, password_must_change=0)
        db.session.add(s)
        db.session.flush()
        db.session.add(StudentEnrolment(student_id=s.id, class_id=class_id, session_id=session_id,
                                        enrolled_at=iso(NOW), active=1))
        students[name] = s.id
    parent = ParentAccount(username="parent1", display_name="Parent One", password_hash=generate_password_hash(PASSWORD),
                           active=1, password_must_change=0, created_at=iso(NOW))
    db.session.add(parent)
    db.session.flush()
    db.session.add(ParentStudentLink(parent_id=parent.id, student_id=students["Bola"], active=1, created_at=iso(NOW)))
    db.session.commit()
    return {"assessment": assessment.id, "quiz": quiz.id, "students": students, "parent": parent.id}


DATA = in_school(make_school)
ASSESSMENT, QUIZ, STUDENTS, PARENT = DATA["assessment"], DATA["quiz"], DATA["students"], DATA["parent"]

# ================================================================ a server that never recorded a launch signs nobody out
boss = Person()
r = boss.sign_in("boss", PASSWORD)
check("staff sign in", r.status_code == 302 and boss.in_("/admin/home"))
check("with no launch recorded yet, a session carries no launch stamp and nothing signs anyone out",
      launch.current_launch_id() is None and "launch" not in boss.session() and boss.in_("/admin/home"))
boss.get("/admin/workspace/entrance")
r = boss.form("/admin/entrance-config/standard")
check("(set-up) the standard entrance papers are set up", r.status_code == 302, str(r.status_code))


def register_candidate(name):
    r = boss.form("/admin/candidates/new", {"candidate_name": name, "target_class": "JSS 1", "school_attended": "Sunrise",
                                            "parent_guardian_name": "Mrs P", "parent_guardian_relationship": "Mother",
                                            "primary_mobile": "08030000001"}, content_type="multipart/form-data")
    text = page_text(r)
    code = re.search(r'Candidate ID</span><strong class="credential-code">(.+?)</strong>', text)
    password = re.search(r'Password</span><strong class="credential-code">(.+?)</strong>', text)
    assert code and password, f"could not register {name}: {r.status_code}"
    return code.group(1), password.group(1)


EMEKA = register_candidate("Emeka Exam")
FEMI = register_candidate("Femi Idle")
GOZIE = register_candidate("Gozie Expired")
check("(set-up) three candidates are registered", all(EMEKA) and all(FEMI) and all(GOZIE))

# ================================================================ the first launch, and everybody signs in
first_launch = restart()
check("a launch id is written to the registry, once, at start",
      registry("SELECT value FROM platform_state WHERE key = 'launch_id'")[0][0] == first_launch
      and re.fullmatch(r"[0-9a-f]{32}", first_launch) is not None)

staff, parent, student_ada, student_bola = Person(), Person(), Person(), Person()
student_chi, student_dayo, student_efe, student_gina = Person(), Person(), Person(), Person()
cand_emeka, cand_femi, cand_gozie = Person(), Person(), Person()
console = Person(PL)

r = console.c.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                            "_csrf_token": console.token()}, base_url=PL)
check("the platform admin signs in to the console", r.status_code == 302 and console.in_("/platform"))
console_before = console.session()
for who, username, password in ((staff, "clerk", PASSWORD), (parent, "parent1", PASSWORD),
                                (student_ada, "STU0", PASSWORD), (student_bola, "STU1", PASSWORD),
                                (student_chi, "STU2", PASSWORD), (student_dayo, "STU3", PASSWORD),
                                (student_efe, "STU4", PASSWORD), (student_gina, "STU5", PASSWORD),
                                (cand_emeka, *EMEKA), (cand_femi, *FEMI), (cand_gozie, *GOZIE)):
    who.sign_in(username, password)
check("everyone signs in, and each session is stamped with the current launch and a password marker",
      all(p.session().get("launch") == first_launch and p.session().get("pwv")
          for p in (staff, parent, student_ada, student_bola, cand_emeka, cand_femi))
      and console_before.get("launch") == first_launch and console_before.get("pwv"))
check("nobody is signed out while the launch has not changed",
      all(p.in_(path) for p, path in ((staff, "/admin/home"), (parent, "/parent/dashboard"),
                                      (student_ada, "/student/dashboard"), (cand_emeka, "/candidate/dashboard"),
                                      (console, "/platform"))))

# ---- who is sitting an exam when the server restarts
r = student_ada.form(f"/student/assessments/{ASSESSMENT}/start")
check("(set-up) a student starts a test", r.status_code == 302, str(r.status_code))
ATT_ADA = in_school(lambda: A.db.session.scalars(sa.select(SchoolAssessmentAttempt.id).where(
    SchoolAssessmentAttempt.student_id == STUDENTS["Ada"])).first())
paper = in_school(lambda: A.db.session.scalars(sa.select(CandidatePaper.id).where(
    CandidatePaper.candidate_id == A.db.session.scalars(sa.select(Candidate.id).where(
        Candidate.candidate_code == EMEKA[0])).first()).order_by(CandidatePaper.slot)).first())
r = cand_emeka.form(f"/candidate/papers/{paper}/start")
check("(set-up) a candidate starts an entrance paper", r.status_code == 302 and "/exam" in r.headers["Location"], str(r.status_code))
ATT_EMEKA = in_school(lambda: A.db.session.scalars(sa.select(Attempt.id).order_by(Attempt.id.desc())).first())


def put_attempts():
    db = A.db
    q = db.session.scalars(sa.select(SchoolQuestion).where(SchoolQuestion.assessment_id == ASSESSMENT)).all()
    chi = SchoolAssessmentAttempt(student_id=STUDENTS["Chi"], assessment_id=ASSESSMENT, started_at=iso(NOW - timedelta(hours=2)),
                                  expires_at=iso(NOW - timedelta(minutes=30)), status="active")
    db.session.add(chi)
    for student, hours in (("Dayo", 0), ("Efe", 5)):
        db.session.add(SchoolAssignmentAttempt(assignment_id=QUIZ, student_id=STUDENTS[student],
                                               started_at=iso(NOW - timedelta(hours=hours, minutes=1)),
                                               expires_at=None, status="active"))
    gozie = db.session.scalars(sa.select(Candidate.id).where(Candidate.candidate_code == GOZIE[0])).first()
    exam = db.session.scalars(sa.select(Examination.id)).first()
    bank = db.session.scalars(sa.select(CandidatePaper.bank_id).where(CandidatePaper.candidate_id == gozie)).first()
    db.session.add(Attempt(candidate="Gozie Expired", exam_id=exam, bank_id=bank, candidate_id=gozie,
                           started_at=iso(NOW - timedelta(hours=3)), expires_at=iso(NOW - timedelta(hours=2)),
                           status="active"))
    db.session.commit()


in_school(put_attempts)

# ================================================================ the restart
second_launch = restart()
check("a restart gives a new launch id", second_launch != first_launch and re.fullmatch(r"[0-9a-f]{32}", second_launch) is not None)

signed_out = {
    "staff": (staff, "/admin/home"), "a parent": (parent, "/parent/dashboard"),
    "a student not in an exam": (student_bola, "/student/dashboard"),
    "a student whose test has run out of time": (student_chi, "/student/dashboard"),
    "a student whose quiz was started five hours ago": (student_efe, "/student/dashboard"),
    "a student with no attempt": (student_gina, "/student/dashboard"),
    "a candidate not in an exam": (cand_femi, "/candidate/dashboard"),
    "a candidate whose paper has run out of time": (cand_gozie, "/candidate/dashboard"),
    "the platform admin": (console, "/platform"),
}
for label, (who, path) in signed_out.items():
    target = "/platform/login" if who is console else "/login"
    r = who.get(path)
    check(f"after a restart, {label} meets the sign-in page",
          r.status_code == 302 and target in r.headers.get("Location", ""), f"{r.status_code} {r.headers.get('Location')}")
sign_in_page = page_text(staff.get("/login"))
check("…and the page says the system was restarted", RESTARTED in sign_in_page, "message not shown")
check("…and that message is shown once, not on every later page", RESTARTED not in page_text(staff.get("/login")))
check("the platform sign-in page says it too", RESTARTED in page_text(console.get("/platform/login")))
check("a signed-out session keeps nothing about the person", all(k not in staff.session() for k in ("admin_id", "pwv")))
check("…but keeps which school it belongs to", staff.session().get("tenant_id") == INFO.id)
r = staff.sign_in("clerk", PASSWORD)
check("someone signed out by a restart can sign in again at once, with no message left over to confuse them",
      r.status_code == 302 and staff.in_("/admin/home") and RESTARTED not in page_text(staff.get("/admin/home")))
check("…and is stamped with the new launch", staff.session().get("launch") == second_launch)

# ---- the people in an exam carry on
r = student_ada.get("/student/dashboard")
check("a student in the middle of a test is not signed out", r.status_code == 200)
check("…nor is a student whose quiz (no deadline of its own) was started a minute ago",
      student_dayo.in_("/student/dashboard") and student_dayo.session().get("launch") == second_launch)
check("…and the session is stamped with the new launch, keeping the attempt's page working",
      student_ada.session().get("launch") == second_launch and student_ada.session().get("student_id"))
r = student_ada.get(f"/student/assessments/{ASSESSMENT}?q=1")
check("…the test page opens exactly where it was", r.status_code == 200 and "Question 1?" in page_text(r))
q1 = in_school(lambda: A.db.session.scalars(sa.select(SchoolQuestion.id).where(
    SchoolQuestion.assessment_id == ASSESSMENT).order_by(SchoolQuestion.sort_order)).first())
r = student_ada.form(f"/student/assessments/{ASSESSMENT}/answer",
                     {"attempt_id": ATT_ADA, "question_id": q1, "option_index": 0, "next_q": 2})
check("…an answer is saved after the restart",
      r.status_code == 302 and sql("SELECT count(*) FROM school_assessment_answers WHERE attempt_id = :a", a=ATT_ADA)[0][0] == 1)
r = student_ada.get("/presence/heartbeat")
check("…the heartbeat still answers", r.status_code == 200 and r.get_json() == {"ok": True})

r = cand_emeka.get("/candidate/dashboard")
check("a candidate in the middle of a paper is not signed out", r.status_code == 200
      and cand_emeka.session().get("launch") == second_launch)
r = cand_emeka.get("/exam?q=1")
check("…the exam page opens exactly where it was", r.status_code == 200 and "timer" in page_text(r))
qid = sql("SELECT question_id FROM attempt_questions WHERE attempt_id = :a ORDER BY question_order LIMIT 1", a=ATT_EMEKA)[0][0]
r = cand_emeka.form("/answer", {"question_id": qid, "option_index": 1})
check("…an answer is saved after the restart (autosave)", r.status_code == 200 and r.get_json() == {"ok": True}
      and sql("SELECT count(*) FROM answers WHERE attempt_id = :a", a=ATT_EMEKA)[0][0] == 1)
check("…the heartbeat still answers", cand_emeka.get("/presence/heartbeat").get_json() == {"ok": True})

# ---- a second restart while they are still in the exam: still kept; then the exam ends
third_launch = restart()
check("a second restart during the same exam keeps them again",
      student_ada.in_("/student/dashboard") and cand_emeka.in_("/candidate/dashboard")
      and student_ada.session().get("launch") == third_launch and cand_emeka.session().get("launch") == third_launch)
r = student_ada.form(f"/student/assessments/{ASSESSMENT}/answer",
                     {"attempt_id": ATT_ADA, "question_id": q1, "option_index": 0, "submit_assessment": "1"})
check("the student submits the test after the restarts", r.status_code == 302
      and sql("SELECT status FROM school_assessment_attempts WHERE id = :a", a=ATT_ADA)[0][0] == "submitted")
r = cand_emeka.form("/submit")
check("the candidate submits the paper after the restarts", r.status_code == 302
      and sql("SELECT status FROM attempts WHERE id = :a", a=ATT_EMEKA)[0][0] in ("submitted", "expired"))
fourth_launch = restart()
check("once the exam is finished, the next restart signs them out like everyone else",
      student_ada.sent_to_sign_in("/student/dashboard") and cand_emeka.sent_to_sign_in("/candidate/dashboard"))

# a stale sign-in that goes straight to the sign-in form (no page first) is not held against them
late = Person()
late.sign_in("STU5", PASSWORD)
restart()
r = late.sign_in("STU5", PASSWORD)
check("someone whose sign-in went stale at a restart can sign in again straight from the form",
      r.status_code == 302 and late.in_("/student/dashboard"))
check("…and is not shown the restart message afterwards (it is only for pages that were refused)",
      RESTARTED not in page_text(late.get("/student/dashboard")))

# a form sent from a page that was left open across a restart
stale = Person()
stale.sign_in("STU5", PASSWORD)
restart()
r = stale.form("/logout")
check("a form sent from a page left open across a restart goes to the sign-in page, not to a security-token error",
      r.status_code == 302 and "/login" in r.headers.get("Location", ""), f"{r.status_code} {r.headers.get('Location')}")
check("…and the sign-in page says why", RESTARTED in page_text(stale.get("/login")))

# ================================================================ failing safe
staff.sign_in("clerk", PASSWORD)
stamped_with = staff.session().get("launch")
real_platform_session = launch.platform_session


attempts = []


def broken_platform_session():
    attempts.append(1)
    raise RuntimeError("the registry is down")


launch.platform_session = broken_platform_session
try:
    launch.reset_cache()
    fifth = None
    # the registry has moved on (a restart happened) but this worker cannot read it
    with real_platform_session() as s:
        s.execute(sa.text("UPDATE platform_state SET value = 'ffffffffffffffffffffffffffffffff' WHERE key = 'launch_id'"))
        s.commit()
    r = staff.get("/admin/home")
    check("when the registry cannot be read, nobody is signed out (fail safe)", r.status_code == 200, str(r.status_code))
    for _ in range(5):
        staff.get("/admin/home")
    check("…and the worker does not ask again on every request while it is down", len(attempts) == 1, str(len(attempts)))
finally:
    launch.platform_session = real_platform_session
launch.reset_cache()
r = staff.get("/admin/home")
check("once the registry can be read again, the restart takes effect", r.status_code == 302 and "/login" in r.headers["Location"])

# a worker that has already read the id keeps using it while the registry is down
fifth = restart()
staff.sign_in("clerk", PASSWORD)
launch._fresh_until = 0
launch.platform_session = broken_platform_session
try:
    r = staff.get("/admin/home")
    check("a worker that has read the id keeps using the last one it saw while the registry is down",
          r.status_code == 200 and launch.current_launch_id() == fifth)
finally:
    launch.platform_session = real_platform_session

# another process (another worker) wrote the id: this one notices within the cache window
with platform_session() as s:
    s.execute(sa.text("UPDATE platform_state SET value = 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee' WHERE key = 'launch_id'"))
    s.commit()
before = launch.current_launch_id()
launch._fresh_until = 0   # the few seconds have passed
check("a worker re-reads the registry once its few seconds are up, so it follows another worker's launch",
      before == fifth and launch.current_launch_id() == "e" * 32)
restart()

# ================================================================ cheap, and not for static files or /health
reads = []
_orig = launch.platform_session


def counting_platform_session():
    reads.append(1)
    return _orig()


launch.platform_session = counting_platform_session
try:
    staff.sign_in("clerk", PASSWORD)
    launch.reset_cache()
    reads.clear()
    staff.get("/health")
    staff.get("/static/app.css")
    check("/health and static files never ask the registry which launch it is", not reads, str(len(reads)))
    for _ in range(12):
        staff.get("/admin/home")
    check("twelve page loads cost the registry one read, not twelve", len(reads) == 1, str(len(reads)))
finally:
    launch.platform_session = _orig
r = Person().get("/health")
check("/health works with no session at all", r.status_code == 200)

# ================================================================ the command line
from control_plane import cli  # noqa: E402

before_cli = launch.current_launch_id()
code = cli.main(["new-launch"])
check("the command line can record a new launch (for a deployment that does not run app.py)",
      code == 0 and launch.current_launch_id() != before_cli)
src_app = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
check("python app.py records a launch after upgrading the schools and before it serves",
      src_app.index("upgrade_all_tenants()") < src_app.index("record_new_launch()") < src_app.index("app.run("))

# ================================================================ changing a password ends the other sign-ins
def two_sessions(username, password):
    a, b = Person(), Person()
    a.sign_in(username, password)
    b.sign_in(username, password)
    return a, b


restart()
boss2 = Person()
boss2.sign_in("boss", PASSWORD)
clerk_a, clerk_b = two_sessions("clerk", PASSWORD)
check("(set-up) the same staff account is signed in in two browsers", clerk_a.in_("/admin/home") and clerk_b.in_("/admin/home"))
r = clerk_a.form("/admin/password", {"current_password": PASSWORD, "new_password": "brand-new-pass-1",
                                     "confirm_password": "brand-new-pass-1"})
check("a staff member changes their own password", r.status_code == 302, str(r.status_code))
check("…they stay signed in where they changed it", clerk_a.in_("/admin/home"))
r = clerk_b.get("/admin/home")
check("…and the other browser is signed out", r.status_code == 302 and "/login" in r.headers["Location"])
check("…with a reason that says so", PASSWORD_CHANGED in page_text(clerk_b.get("/login")))
check("…and the new password works", clerk_b.sign_in("clerk", "brand-new-pass-1").status_code == 302 and clerk_b.in_("/admin/home"))
check("…while the old one does not", "We could not verify" in page_text(Person().sign_in("clerk", PASSWORD)))
CLERK_PASSWORD = "brand-new-pass-1"

# an administrator resets another administrator's login
victim_a, victim_b = two_sessions("clerk", CLERK_PASSWORD)
clerk_id = sql("SELECT id FROM admins WHERE username = 'clerk'")[0][0]
boss2.get("/admin/workspace/school")
r = boss2.form(f"/admin/administration/admins/{clerk_id}/credentials/reset")
check("an administrator resets another administrator's login", r.status_code in (200, 302), str(r.status_code))
check("…and every browser that was signed in as that account is signed out",
      victim_a.sent_to_sign_in("/admin/home") and victim_b.sent_to_sign_in("/admin/home"))
check("…while the administrator who did it stays in", boss2.in_("/admin/home"))

# a parent changes their own password
pa, pb = two_sessions("parent1", PASSWORD)
r = pa.form("/parent/password", {"current_password": PASSWORD, "new_password": "parent-new-pass-1",
                                 "confirm_password": "parent-new-pass-1"})
check("a parent changes their own password", r.status_code == 302, str(r.status_code))
check("…stays in where they changed it, and is signed out everywhere else",
      pa.in_("/parent/dashboard") and pb.sent_to_sign_in("/parent/dashboard"))

# a student changes their own password
sa_, sb_ = two_sessions("STU3", PASSWORD)
r = sa_.form("/student/password", {"current_password": PASSWORD, "new_password": "student-new-pass-1",
                                   "confirm_password": "student-new-pass-1"})
check("a student changes their own password", r.status_code == 302, str(r.status_code))
check("…stays in where they changed it, and is signed out everywhere else",
      sa_.in_("/student/dashboard") and sb_.sent_to_sign_in("/student/dashboard"))

# a reset link ends every sign-in of the account it was for
ra, rb = two_sessions("STU4", PASSWORD)
raw = "reset-token-for-efe-0123456789"
sql("INSERT INTO password_reset_tokens (account_type, account_id, token_hash, expires_at, created_at) "
    "VALUES ('student', :i, :h, :e, :c)", i=STUDENTS["Efe"], h=hashlib.sha256(raw.encode()).hexdigest(),
    e=iso(NOW + timedelta(minutes=30)), c=iso(NOW))
r = Person().post(f"/reset-password/{raw}", {"new_password": "efe-reset-pass-1", "confirm_password": "efe-reset-pass-1"})
check("a student resets their password with an emailed link", r.status_code == 302, str(r.status_code))
check("…and both browsers that were signed in with the old password are signed out",
      ra.sent_to_sign_in("/student/dashboard") and rb.sent_to_sign_in("/student/dashboard"))

# an administrator resets a candidate's credentials
cand_id = sql("SELECT id FROM candidates WHERE candidate_code = :c", c=FEMI[0])[0][0]
fa, fb = two_sessions(*FEMI)
boss2.get("/admin/workspace/entrance")
r = boss2.form(f"/admin/candidates/{cand_id}/credentials/reset")
check("an administrator resets a candidate's credentials", r.status_code in (200, 302), str(r.status_code))
check("…and the candidate's old sign-ins end", fa.sent_to_sign_in("/candidate/dashboard") and fb.sent_to_sign_in("/candidate/dashboard"))

# the platform console
c1, c2 = Person(PL), Person(PL)
for c in (c1, c2):
    c.c.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                      "_csrf_token": c.token()}, base_url=PL)
check("(set-up) one platform admin is signed in in two browsers", c1.in_("/platform") and c2.in_("/platform"))
r = c1.form("/platform/password", {"current_password": "a-long-platform-password",
                                   "new_password": "another-long-platform-pass", "confirm_password": "another-long-platform-pass"})
check("a platform admin changes their password", r.status_code == 302, str(r.status_code))
check("…stays in where they changed it, and is signed out in the other browser",
      c1.in_("/platform") and c2.get("/platform").status_code == 302 and "/platform/login" in c2.get("/platform").headers["Location"])

# an operator entering a school twice does not end the first entry
def enter():
    op = Person(PL)
    op.c.post("/platform/login", data={"username": "ops", "password": "another-long-platform-pass",
                                       "_csrf_token": op.token()}, base_url=PL)
    r = op.form("/platform/schools/alpha/enter")
    school = Person()
    school.c.get(r.headers["Location"][len(ALPHA):], base_url=ALPHA)
    return school


e1 = enter()
e2 = enter()
check("a platform operator who enters a school twice keeps the first entry (the reserved account's password changes every time, and is not what keeps them in)",
      e1.in_("/admin/home") and e2.in_("/admin/home"))
restart()
check("…but a restart ends both", e1.sent_to_sign_in("/admin/home") and e2.sent_to_sign_in("/admin/home"))

# a login POST arriving with a stale identity/password stamp already in the cookie (an open tab from
# before someone else changed this very account's password) must not be turned away by a wiped CSRF
# token: the sign-in itself must still work, using whatever token that open tab already had.
# Its own address: a login attempt is rate-limited per (address, username), and this block signs
# "ops" in twice more on top of every other test in this file that does, all from the test client's
# shared default address — on the same address that would eventually trip the very limit this file
# tests elsewhere.
stale_tab = Person(PL)
stale_env = {"REMOTE_ADDR": "10.44.1.1"}
stale_tab.c.post("/platform/login", data={"username": "ops", "password": "another-long-platform-pass",
                                          "_csrf_token": stale_tab.token()}, base_url=PL, environ_base=stale_env)
check("(set-up) a platform admin has an open tab, signed in", stale_tab.in_("/platform"))
kept_token = stale_tab.session().get("_csrf_token")
with platform_session() as s:
    s.execute(sa.text("UPDATE platform_admins SET password_hash = :h WHERE username = 'ops'"),
              {"h": generate_password_hash("changed-elsewhere-pass-1")})
    s.commit()
check("…and the account's password is changed by someone else while that tab sits open",
      kept_token and stale_tab.session().get("_csrf_token") == kept_token)
r = stale_tab.c.post("/platform/login", data={"username": "ops", "password": "changed-elsewhere-pass-1",
                                               "_csrf_token": kept_token}, base_url=PL, environ_base=stale_env)
check("signing in from that stale tab, with its old CSRF token, still works and is not refused as a bad token",
      r.status_code == 302 and stale_tab.in_("/platform"), f"{r.status_code}")
with platform_session() as s:
    s.execute(sa.text("UPDATE platform_admins SET password_hash = :h WHERE username = 'ops'"),
              {"h": generate_password_hash("another-long-platform-pass")})
    s.commit()

# ================================================================ referrers and caching
anon = Person()
login_page = anon.get("/login")
check("every response names a Referrer-Policy: same-origin, so nothing is told to another site",
      login_page.headers.get("Referrer-Policy") == "same-origin"
      and anon.get("/nowhere-at-all").headers.get("Referrer-Policy") == "same-origin"
      and anon.get("/static/app.css").headers.get("Referrer-Policy") == "same-origin"
      and anon.get("/health").headers.get("Referrer-Policy") == "same-origin"
      and console.get("/platform/login").headers.get("Referrer-Policy") == "same-origin")
token = "a-valid-looking-reset-token"
sql("INSERT INTO password_reset_tokens (account_type, account_id, token_hash, expires_at, created_at) "
    "VALUES ('student', :i, :h, :e, :c)", i=STUDENTS["Gina"], h=hashlib.sha256(token.encode()).hexdigest(),
    e=iso(NOW + timedelta(minutes=30)), c=iso(NOW))
for label, path in (("a valid link", f"/reset-password/{token}"), ("an invalid one", "/reset-password/not-a-real-token"),
                    ("the request page", "/forgot-password")):
    r = anon.get(path)
    check(f"the password-reset page ({label}) is never stored and never sends a referrer",
          "no-store" in r.headers.get("Cache-Control", "") and r.headers.get("Referrer-Policy") == "no-referrer")
r = anon.post(f"/reset-password/{token}", {"new_password": "x", "confirm_password": "y"})
check("…nor is the answer to submitting it", "no-store" in r.headers.get("Cache-Control", ""))
check("an ordinary page is not marked no-store by that rule", "no-store" not in login_page.headers.get("Cache-Control", ""))
sources = ""
for folder in ("blueprints", "core", "control_plane", "templates", "static"):
    for base, _, names in os.walk(os.path.join(ROOT, folder)):
        for name in names:
            if name.endswith((".py", ".html", ".js")):
                sources += open(os.path.join(base, name), encoding="utf-8", errors="ignore").read()
check("(why same-origin and not no-referrer) the application really does read the Referer of its own pages",
      "request.referrer" in sources and "'Referer'" in sources)

# ================================================================ the trusted proxy setting
def audit_ip_for(person, **environ):
    """Sign in the clerk and read the address the audit log kept for it."""
    person.c.post("/login", data={"username": "boss", "password": PASSWORD}, base_url=ALPHA, **environ)
    return sql("SELECT ip_address FROM audit_logs WHERE action = 'admin_login' ORDER BY id DESC LIMIT 1")[0][0]


forged = {"X-Forwarded-For": "6.6.6.6", "X-Forwarded-Proto": "https", "X-Forwarded-Host": "beta.portal.test"}
ip = audit_ip_for(Person(), headers=forged, environ_base={"REMOTE_ADDR": "10.9.8.7"})
check("off by default: a forged X-Forwarded-For is not written into the audit log", ip == "10.9.8.7", ip)
long_ip = audit_ip_for(Person(), environ_base={"REMOTE_ADDR": "9" * 100})
check("…and the address is cut to 64 characters", len(long_ip) == 64, str(len(long_ip)))
check("off by default: the audit log code no longer reads the header at all",
      "X-Forwarded" not in open(os.path.join(ROOT, "core", "security.py"), encoding="utf-8").read())
op = Person(PL)
op.c.post("/platform/login", data={"username": "ops", "password": "another-long-platform-pass",
                                   "_csrf_token": op.token()}, base_url=PL)
r = op.c.post("/platform/schools/alpha/enter", data={"_csrf_token": op.token()}, base_url=PL,
              headers={"X-Forwarded-Proto": "https"})
check("off by default: a forged X-Forwarded-Proto does not change the address the console sends an operator to",
      r.headers["Location"].startswith("http://alpha.portal.test"), r.headers["Location"])
check("off by default: the application is not wrapped in ProxyFix", type(A.app.wsgi_app).__name__ != "ProxyFix")

original_wsgi = A.app.wsgi_app
A.apply_trusted_proxies(A.app, 1)
try:
    check("with 1 proxy the application is wrapped", type(A.app.wsgi_app).__name__ == "ProxyFix")
    ip = audit_ip_for(Person(), headers={"X-Forwarded-For": "198.51.100.7, 203.0.113.9"},
                      environ_base={"REMOTE_ADDR": "10.0.0.1"})
    check("with 1 proxy the address is the last one the proxy reports, and what the visitor forged in front is ignored",
          ip == "203.0.113.9", ip)
    who = Person()
    r = who.get("/login", headers={"X-Forwarded-Host": "beta.portal.test", "X-Forwarded-Prefix": "/elsewhere"})
    check("…the host still chooses the school (X-Forwarded-Host and -Prefix are never believed)",
          r.status_code == 200 and "Alpha School" in page_text(r), str(r.status_code))
    op = Person(PL)
    op.c.post("/platform/login", data={"username": "ops", "password": "another-long-platform-pass",
                                       "_csrf_token": op.token()}, base_url=PL)
    r = op.c.post("/platform/schools/alpha/enter", data={"_csrf_token": op.token()}, base_url=PL,
                  headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "1.2.3.4"})
    check("…and the scheme comes from the proxy, so the console sends the operator to https",
          r.headers["Location"].startswith("https://alpha.portal.test"), r.headers["Location"])
finally:
    A.app.wsgi_app = original_wsgi

for raw, expected in (("", 0), ("0", 0), (" 2 ", 2), ("5", 5)):
    os.environ["BRIGHTSTARS_TRUSTED_PROXIES"] = raw
    check(f'BRIGHTSTARS_TRUSTED_PROXIES="{raw}" is {expected}', cp_config.trusted_proxies() == expected)
for raw in ("abc", "-1", "1.5", "6", "yes", "99999999999999999999"):
    os.environ["BRIGHTSTARS_TRUSTED_PROXIES"] = raw
    try:
        cp_config.trusted_proxies()
        check(f'BRIGHTSTARS_TRUSTED_PROXIES="{raw}" is refused', False)
    except RuntimeError as exc:
        check(f'BRIGHTSTARS_TRUSTED_PROXIES="{raw}" is refused with a plain message',
              "BRIGHTSTARS_TRUSTED_PROXIES" in str(exc) and "reverse prox" in str(exc))
env = {**os.environ, "BRIGHTSTARS_TRUSTED_PROXIES": "lots"}
done = subprocess.run([sys.executable, "-c", "import app"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
check("the application refuses to start on a bad value, saying why",
      done.returncode != 0 and "BRIGHTSTARS_TRUSTED_PROXIES" in done.stderr)
os.environ.pop("BRIGHTSTARS_TRUSTED_PROXIES", None)

# ================================================================ tidy up
dispose_engines()
DROP_TEST_DATABASES()
shutil.rmtree(TMP, ignore_errors=True)
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
