"""Who may open which of a school's uploaded files, proven for every folder and every kind of person.

The route /static/uploads/... used to open any file in a school's uploads folder for ANY signed-in
account of that school. Each folder now has its own rule (core/upload_access.py):

    branding     anyone                     messages     nobody (the download route only)
    admins       staff                      signatures   staff
    candidates   staff + that candidate     students     staff + that student + a linked parent
    questions    staff, students, candidates                (parents have no exam)
    assignments  staff, students            anything else  staff

A refusal is a plain 404 (the same answer as for a file that is not there). A sign-in that has
ended (a restart, a changed password), an account that has been switched off, and a cookie that
belongs to another school all open nothing. Ownership is settled by the database row that owns the
file (``students.photo_path`` and so on), never by the file's name.

Run:  python tests/verification/write_paths_upload_access.py
"""
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_uploadaccess_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup("upacc")
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
from control_plane import launch  # noqa: E402
from control_plane import provisioning as pv  # noqa: E402
from control_plane.context import tenant_context  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import dispose_engines, engine_for  # noqa: E402
from core import upload_access  # noqa: E402
from models import (  # noqa: E402
    AcademicSession, Admin, AdminType, Candidate, ParentAccount, ParentStudentLink, SchoolAssessment,
    SchoolAssessmentAttempt, SchoolClass, SchoolSubject, Student, StudentEnrolment,
)

results = []
PL = "http://platform.test"
ALPHA, BETA = "http://alpha.portal.test", "http://beta.portal.test"
PASSWORD = "a-good-password-1"
NOW = datetime.now(timezone.utc)
iso = lambda dt: dt.isoformat()  # noqa: E731


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


_addr = [0]


def next_addr():
    _addr[0] += 1
    return f"10.88.{_addr[0] // 250}.{_addr[0] % 250 + 1}"


class Person:
    def __init__(self, base=ALPHA):
        self.base, self.c = base, A.app.test_client()

    def get(self, path, base=None):
        return self.c.get(path, base_url=base or self.base, environ_base={"REMOTE_ADDR": "10.1.1.1"})

    def token(self):
        with self.c.session_transaction(base_url=self.base) as sess:
            if not sess.get("_csrf_token"):
                sess["_csrf_token"] = "test-token-" + str(id(self))
            return sess["_csrf_token"]

    def sign_in(self, username):
        # A real GET first lets the tenant boundary stamp this browser's session with this
        # school's own tenant_id; only then does a token planted straight into the session
        # (rather than scraped from a page) survive to the login POST instead of being wiped
        # as a foreign cookie.
        self.get("/login")
        return self.c.post("/login", data={"username": username, "password": PASSWORD, "_csrf_token": self.token()},
                           base_url=self.base, environ_base={"REMOTE_ADDR": next_addr()})

    def session(self):
        with self.c.session_transaction(base_url=self.base) as sess:
            return dict(sess)


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


# ================================================================ two schools, and every kind of person in alpha
pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
pv.create_tenant("alpha", "Alpha School", [], starter_banks=False)
pv.create_tenant("beta", "Beta College", [], starter_banks=False)


def make_alpha():
    db = A.db
    role = db.session.scalars(sa.select(AdminType).where(AdminType.is_system == 1)).first()
    plain = db.session.scalars(sa.select(AdminType).where(AdminType.name == "Ordinary Admin")).first()
    for username, role_id in (("boss", role.id), ("plain", plain.id)):
        db.session.add(Admin(username=username, display_name=username.title(), admin_type_id=role_id, active=1,
                             password_hash=generate_password_hash(PASSWORD), password_must_change=0,
                             created_at=iso(NOW)))
    session_id = db.session.scalars(sa.select(AcademicSession.id).where(AcademicSession.is_current == 1)).first()
    class_id = db.session.scalars(sa.select(SchoolClass.id).where(SchoolClass.name == "JSS 1")).first()
    subject = SchoolSubject(name="Mathematics", code="MTH", created_at=iso(NOW))
    db.session.add(subject)
    db.session.flush()
    boss_id = db.session.scalars(sa.select(Admin.id).where(Admin.username == "boss")).first()
    assessment = SchoolAssessment(assessment_type="test", title="Maths test", class_id=class_id, subject_id=subject.id,
                                  session_id=session_id, duration_minutes=30, question_count=1, active=1,
                                  created_by=boss_id, created_at=iso(NOW), term="First Term")
    db.session.add(assessment)
    db.session.flush()
    ids = {"assessment": assessment.id}
    for n, name in enumerate(("ada", "bola")):
        s = Student(admission_no=f"ADM{n}", first_name=name.title(), last_name="Test", created_at=iso(NOW), active=1,
                    login_username=f"stu_{name}", login_password_hash=generate_password_hash(PASSWORD),
                    account_active=1, password_must_change=0, photo_path=f"uploads/students/{name}.png")
        db.session.add(s)
        db.session.flush()
        db.session.add(StudentEnrolment(student_id=s.id, class_id=class_id, session_id=session_id,
                                        enrolled_at=iso(NOW), active=1))
        ids[name] = s.id
    for name, child in (("mum_of_ada", "ada"), ("mum_of_bola", "bola")):
        p = ParentAccount(username=name, display_name=name, password_hash=generate_password_hash(PASSWORD),
                          active=1, password_must_change=0, created_at=iso(NOW))
        db.session.add(p)
        db.session.flush()
        db.session.add(ParentStudentLink(parent_id=p.id, student_id=ids[child], active=1, created_at=iso(NOW)))
        ids[name] = p.id
    for n, name in enumerate(("emeka", "femi")):
        c = Candidate(candidate_code=f"CAND-{n}", candidate_name=name.title(), target_class="JSS 1", created_at=iso(NOW),
                      password_hash=generate_password_hash(PASSWORD), active=1, photo_path=f"uploads/candidates/{name}.png")
        db.session.add(c)
        db.session.flush()
        ids[name] = c.id
    db.session.commit()
    return ids


def make_beta():
    db = A.db
    role = db.session.scalars(sa.select(AdminType).where(AdminType.is_system == 1)).first()
    db.session.add(Admin(username="boss", display_name="Beta Boss", admin_type_id=role.id, active=1,
                         password_hash=generate_password_hash(PASSWORD), password_must_change=0, created_at=iso(NOW)))
    db.session.commit()


IDS = in_school("alpha", make_alpha)
in_school("beta", make_beta)

# ---- the files: one in every folder, plus two the rules do not name
FILES = {  # folder -> file name
    "branding": "logo.png", "messages": "note.pdf", "admins": "staff.png", "signatures": "sig.png",
    "candidates": "emeka.png", "students": "ada.png", "questions": "q.png", "assignments": "a.png",
    "misc": "x.png",
}
EXTRA_CANDIDATE, EXTRA_STUDENT = ("candidates", "femi.png"), ("students", "bola.png")
uploads = os.path.join(TMP, "tenants", "alpha", "uploads")
for folder, name in list(FILES.items()) + [EXTRA_CANDIDATE, EXTRA_STUDENT]:
    os.makedirs(os.path.join(uploads, folder), exist_ok=True)
    with open(os.path.join(uploads, folder, name), "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + f"{folder}/{name}".encode())
with open(os.path.join(uploads, "root.txt"), "wb") as f:
    f.write(b"a file directly in the uploads folder")
beta_uploads = os.path.join(TMP, "tenants", "beta", "uploads", "students")
os.makedirs(beta_uploads, exist_ok=True)
with open(os.path.join(beta_uploads, "ada.png"), "wb") as f:
    f.write(b"\x89PNG\r\n\x1a\nbeta")

# ---- the people
people = {}
for label, base, username in (("staff (School Admin)", ALPHA, "boss"), ("staff (ordinary role)", ALPHA, "plain"),
                              ("the student it belongs to", ALPHA, "stu_ada"), ("another student", ALPHA, "stu_bola"),
                              ("the parent of that student", ALPHA, "mum_of_ada"),
                              ("an unrelated parent", ALPHA, "mum_of_bola"),
                              ("the candidate it belongs to", ALPHA, "CAND-0"), ("another candidate", ALPHA, "CAND-1")):
    p = Person(base)
    p.sign_in(username)
    people[label] = p
people["someone not signed in"] = Person(ALPHA)
beta_staff = Person(BETA)
beta_staff.sign_in("boss")
check("(set-up) everyone signs in",
      "admin_id" in people["staff (School Admin)"].session() and "admin_id" in people["staff (ordinary role)"].session()
      and "student_id" in people["the student it belongs to"].session()
      and "student_id" in people["another student"].session()
      and "parent_id" in people["an unrelated parent"].session()
      and "candidate_id" in people["another candidate"].session()
      and "parent_id" in people["the parent of that student"].session()
      and "candidate_id" in people["the candidate it belongs to"].session()
      and "admin_id" in beta_staff.session())

# ================================================================ the matrix, folder by folder
STAFF = ("staff (School Admin)", "staff (ordinary role)")
OWNER_STUDENT, OTHER_STUDENT = "the student it belongs to", "another student"
PARENT, STRANGER_PARENT = "the parent of that student", "an unrelated parent"
OWNER_CAND, OTHER_CAND = "the candidate it belongs to", "another candidate"
NOBODY = "someone not signed in"
EVERYONE = tuple(people)


def opens(person, folder, name):
    return people[person].get(f"/static/uploads/{folder}/{name}").status_code == 200


def matrix(folder, name, allowed, describe):
    """Every kind of person against one file: exactly the allowed ones get it, and the rest get 404."""
    wrong = []
    for person in EVERYONE:
        r = people[person].get(f"/static/uploads/{folder}/{name}")
        want = person in allowed
        got = r.status_code == 200
        if got != want or (not want and r.status_code != 404):
            wrong.append(f"{person}: {r.status_code}")
    check(f"{folder}/ {describe}", not wrong, "; ".join(wrong))


matrix("branding", "logo.png", EVERYONE, "is public: the sign-in page needs the logo and photographs")
matrix("messages", "note.pdf", (), "is served to nobody here, not even staff (the permission-checked download route only)")
matrix("admins", "staff.png", STAFF, "is for staff")
matrix("signatures", "sig.png", STAFF, "is for staff (report cards and receipts embed signatures themselves)")
matrix("candidates", "emeka.png", STAFF + (OWNER_CAND,), "is for staff and the candidate the photograph belongs to")
matrix("candidates", "femi.png", STAFF + (OTHER_CAND,), "…and another candidate's photograph is that candidate's, not everyone's")
matrix("students", "ada.png", STAFF + (OWNER_STUDENT, PARENT),
       "is for staff, the student it belongs to, and a parent linked to that student")
matrix("students", "bola.png", STAFF + (OTHER_STUDENT, STRANGER_PARENT),
       "…and another student's photograph is opened by that student and their own parent, not by the first student or their parent")
matrix("questions", "q.png", STAFF + (OWNER_STUDENT, OTHER_STUDENT, OWNER_CAND, OTHER_CAND),
       "is for staff, students and candidates (they sit the exams); parents have none")
matrix("assignments", "a.png", STAFF + (OWNER_STUDENT, OTHER_STUDENT), "is for staff and students")
matrix("misc", "x.png", STAFF, "a folder no rule names is for staff only")
check("a file directly in the uploads folder is for staff only",
      all(people[p].get("/static/uploads/root.txt").status_code == (200 if p in STAFF else 404) for p in EVERYONE))
check("a staff member is given the real bytes, not just a status",
      people[STAFF[0]].get("/static/uploads/students/ada.png").data.endswith(b"students/ada.png"))

# ================================================================ a refusal is a plain 404, and says nothing
r_refused = people[OTHER_STUDENT].get("/static/uploads/students/ada.png")
r_missing = people[OTHER_STUDENT].get("/static/uploads/students/no-such-file.png")
check("a refusal looks exactly like a file that is not there",
      r_refused.status_code == r_missing.status_code == 404
      and re.sub(rb'nonce="[^"]*"', b"", r_refused.get_data()) == re.sub(rb'nonce="[^"]*"', b"", r_missing.get_data()),
      f"{r_refused.status_code} {r_missing.status_code} {r_refused.get_data()[:300]!r} // {r_missing.get_data()[:300]!r}")
check("…for someone not signed in too",
      people[NOBODY].get("/static/uploads/students/ada.png").status_code == 404
      == people[NOBODY].get("/static/uploads/students/none.png").status_code)

# ================================================================ decided on the path that will be served
check("climbing out of a folder you may open into one you may not is judged by where it lands",
      people[OWNER_STUDENT].get("/static/uploads/students/../signatures/sig.png").status_code in (404, 400)
      and people[OWNER_STUDENT].get("/static/uploads/students/../messages/note.pdf").status_code in (404, 400)
      and people[OWNER_STUDENT].get("/static/uploads/students/../admins/staff.png").status_code in (404, 400))
check("…and the other way: a path that lands in the student's own folder is judged as the student's",
      people[OWNER_STUDENT].get("/static/uploads/signatures/../students/ada.png").status_code == 200
      and people[OTHER_STUDENT].get("/static/uploads/signatures/../students/ada.png").status_code == 404)
check("a backslash cannot be used to slip past the folder check",
      people[OWNER_STUDENT].get("/static/uploads/students%5C..%5Csignatures%5Csig.png").status_code in (404, 400))
check("changing the case of a folder name does not change who may open it",
      people[OWNER_STUDENT].get("/static/uploads/Signatures/sig.png").status_code == 404
      and people[OWNER_STUDENT].get("/static/uploads/Admins/staff.png").status_code == 404
      and people[OWNER_STUDENT].get("/static/uploads/Messages/note.pdf").status_code == 404)
check("a photograph is matched by the exact file name the database holds, so a look-alike name opens nothing",
      people[OWNER_STUDENT].get("/static/uploads/students/ada.png.png").status_code == 404
      and people[OWNER_STUDENT].get("/static/uploads/students/nested/../bola.png").status_code == 404)

# ================================================================ ownership comes from the database row
sql("alpha", "UPDATE students SET photo_path = 'uploads/students/bola.png' WHERE id = :i", i=IDS["ada"])
check("the file follows the database row, not its name: when Ada's row holds bola.png, Ada opens that one and no longer ada.png",
      opens(OWNER_STUDENT, "students", "bola.png") and not opens(OWNER_STUDENT, "students", "ada.png"))
sql("alpha", "UPDATE students SET photo_path = 'uploads/students/ada.png' WHERE id = :i", i=IDS["ada"])
check("…and back again", opens(OWNER_STUDENT, "students", "ada.png") and not opens(OTHER_STUDENT, "students", "ada.png"))

sql("alpha", "UPDATE parent_student_links SET active = 0 WHERE parent_id = :p", p=IDS["mum_of_ada"])
check("a parent whose link to the student has been ended no longer gets their photograph",
      not opens(PARENT, "students", "ada.png"))
sql("alpha", "UPDATE parent_student_links SET active = 1 WHERE parent_id = :p", p=IDS["mum_of_ada"])
check("…and gets it again when the link is restored", opens(PARENT, "students", "ada.png"))
sql("alpha", "INSERT INTO parent_student_links (parent_id, student_id, active, created_at) VALUES (:p, :s, 1, :c)",
    p=IDS["mum_of_bola"], s=IDS["ada"], c=iso(NOW))
check("a parent linked to two children gets both children's photographs, and only theirs",
      opens(STRANGER_PARENT, "students", "ada.png") and opens(STRANGER_PARENT, "students", "bola.png"))
sql("alpha", "DELETE FROM parent_student_links WHERE parent_id = :p AND student_id = :s", p=IDS["mum_of_bola"], s=IDS["ada"])
check("…until that link goes", not opens(STRANGER_PARENT, "students", "ada.png") and opens(STRANGER_PARENT, "students", "bola.png"))

# ================================================================ accounts that have been switched off
sql("alpha", "UPDATE students SET account_active = 0 WHERE id = :i", i=IDS["ada"])
check("a student whose account is switched off opens nothing, not even their own photograph",
      not opens(OWNER_STUDENT, "students", "ada.png") and not opens(OWNER_STUDENT, "questions", "q.png"))
sql("alpha", "UPDATE students SET account_active = 1 WHERE id = :i", i=IDS["ada"])
sql("alpha", "UPDATE parent_accounts SET active = 0 WHERE id = :i", i=IDS["mum_of_ada"])
check("a parent whose account is switched off opens nothing", not opens(PARENT, "students", "ada.png"))
sql("alpha", "UPDATE parent_accounts SET active = 1 WHERE id = :i", i=IDS["mum_of_ada"])
sql("alpha", "UPDATE candidates SET active = 0 WHERE id = :i", i=IDS["emeka"])
check("a candidate whose account is switched off opens nothing", not opens(OWNER_CAND, "candidates", "emeka.png")
      and not opens(OWNER_CAND, "questions", "q.png"))
sql("alpha", "UPDATE candidates SET active = 1 WHERE id = :i", i=IDS["emeka"])
sql("alpha", "UPDATE admins SET active = 0 WHERE username = 'plain'")
check("a member of staff who has been switched off opens nothing", not opens("staff (ordinary role)", "admins", "staff.png"))
sql("alpha", "UPDATE admins SET active = 1 WHERE username = 'plain'")
check("(the accounts are all back)", opens(OWNER_STUDENT, "students", "ada.png") and opens(PARENT, "students", "ada.png")
      and opens(OWNER_CAND, "candidates", "emeka.png") and opens("staff (ordinary role)", "admins", "staff.png"))

# ================================================================ another school
check("another school's staff, on their own address, cannot ask for this school's file names",
      beta_staff.get("/static/uploads/students/ada.png", base=BETA).get_data() == b"\x89PNG\r\n\x1a\nbeta")
check("…they get their own school's file of that name, never alpha's",
      beta_staff.get("/static/uploads/admins/staff.png", base=BETA).status_code == 404)
cookie = beta_staff.c.get_cookie("session", domain="beta.portal.test")
thief = Person(ALPHA)
thief.c.set_cookie("session", cookie.value, domain="alpha.portal.test")
check("a sign-in cookie copied from another school opens nothing here",
      thief.get("/static/uploads/students/ada.png").status_code == 404
      and thief.get("/static/uploads/signatures/sig.png").status_code == 404
      and thief.get("/static/uploads/branding/logo.png").status_code == 200)
check("nothing is served on the platform host",
      Person(PL).get("/static/uploads/branding/logo.png").status_code == 404)

# ================================================================ a sign-in that has ended opens nothing
launch.record_new_launch()
first = people[OWNER_STUDENT].session()
r = people[OWNER_STUDENT].get("/static/uploads/students/ada.png")
check("after a restart, a student who is not in an exam can no longer open their photograph", r.status_code == 404)
check("…and asking for a picture does not touch their cookie (a page loading beside it would lose its own)",
      "Set-Cookie" not in r.headers and people[OWNER_STUDENT].session() == first)
r = people[PARENT].get("/static/uploads/students/ada.png")
check("…nor can a parent", r.status_code == 404)
check("…but the public sign-in pictures still load for everyone",
      people[OWNER_STUDENT].get("/static/uploads/branding/logo.png").status_code == 200)

# a student in the middle of an exam keeps their access across the restart
sql("alpha", "INSERT INTO school_assessment_attempts (student_id, assessment_id, started_at, expires_at, status) "
             "VALUES (:s, :a, :st, :ex, 'active')", s=IDS["bola"], a=IDS["assessment"], st=iso(NOW),
    ex=iso(NOW + timedelta(minutes=30)))
check("a student in the middle of an exam still gets their question pictures after the restart",
      opens(OTHER_STUDENT, "questions", "q.png") and opens(OTHER_STUDENT, "students", "bola.png"))
sql("alpha", "UPDATE school_assessment_attempts SET expires_at = :e WHERE student_id = :s",
    e=iso(NOW - timedelta(minutes=1)), s=IDS["bola"])
launch.record_new_launch()
check("…until the exam has run out of time and the next restart comes", not opens(OTHER_STUDENT, "questions", "q.png"))

# a changed password ends file access too
p2 = Person(ALPHA)
p2.sign_in("stu_ada")
check("(set-up) a fresh sign-in opens the student's photograph", p2.get("/static/uploads/students/ada.png").status_code == 200)
sql("alpha", "UPDATE students SET login_password_hash = :h WHERE id = :i", h=generate_password_hash("another-one-1"), i=IDS["ada"])
from core import session_guard as _guard  # noqa: E402
_guard._fingerprint_cache.clear()   # a change made behind the application's back is seen once the short-lived copy is dropped
check("once the student's password has been changed (elsewhere), that sign-in opens nothing",
      p2.get("/static/uploads/students/ada.png").status_code == 404, str(p2.get("/static/uploads/students/ada.png").status_code))

# ================================================================ signed-out behaviour that must not change
sql("alpha", "INSERT INTO school_public_settings (setting_key, setting_value, updated_at) "
             "VALUES ('school_logo', 'uploads/legacy_logo.png', :n) "
             "ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value", n=iso(NOW))
with open(os.path.join(uploads, "legacy_logo.png"), "wb") as f:
    f.write(b"\x89PNG\r\n\x1a\nlegacy")
check("a school whose logo predates the branding folder still shows it on its sign-in page",
      people[NOBODY].get("/static/uploads/legacy_logo.png").status_code == 200
      and people[NOBODY].get("/static/uploads/root.txt").status_code == 404,
      f"{people[NOBODY].get('/static/uploads/legacy_logo.png').status_code} {people[NOBODY].get('/static/uploads/root.txt').status_code}")

# ================================================================ the rule table itself
check("every folder the application writes to has a rule (nothing falls into 'staff only' by accident)",
      set(upload_access.FOLDER_RULES) == {"branding", "messages", "admins", "signatures", "candidates", "students",
                                          "questions", "assignments"})
check("an unnamed folder is for staff only", upload_access.rule_for("anything") == frozenset({"admin"}))

# ================================================================ tidy up
dispose_engines()
DROP_TEST_DATABASES()
shutil.rmtree(TMP, ignore_errors=True)
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
