"""The school's chain of authority, proven end to end on PostgreSQL: teaching duties given on the staff
form, a subject teacher kept to their class, subject and SSS department, the subject teacher -> class
teacher hand-off of results, the class teacher alone releasing their class, the Teachers page a head
teacher uses, SSS departments on students and subjects, and Crèche/Nursery pupils who never sign in
while their parents still see fees and report cards. Run:  python tests/verification/write_paths_staff_hierarchy.py
"""
import atexit
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
TMP = tempfile.mkdtemp(prefix="brightstars_hierarchy_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('hierarchy')
atexit.register(DROP_TEST_DATABASES)
atexit.register(shutil.rmtree, TMP, True)
os.environ.update({
    "BRIGHTSTARS_TENANTS_DIR": os.path.join(TMP, "tenants"),
    "BRIGHTSTARS_PLATFORM_HOSTS": "platform.test",
    "BRIGHTSTARS_PORTAL_DOMAIN": "portal.test",
    "BRIGHTSTARS_REGISTRY_CACHE_SECONDS": "0",
    "BRIGHTSTARS_ADMIN_CACHE_SECONDS": "0",
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
from control_plane.routing import dispose_engines, engine_for  # noqa: E402

results = []
PL = "http://platform.test"
ALPHA = "http://alpha.portal.test"


def refused(r):
    """A 403 from the access rules (the 'Access needed' page), never one from a missing CSRF token."""
    return r.status_code == 403 and "Access needed" in r.get_data(as_text=True)


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
PROBLEMS = []


def csrf_from(body):
    found = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', body)
    return found.group(1) if found else ""


class Person:
    count = [0]

    def __init__(self, base, form_page="/admin/password", label=""):
        self.base, self.form_page, self.client, self.label = base, form_page, A.app.test_client(), label or base
        Person.count[0] += 1
        self.addr = f"10.83.{Person.count[0]}.1"

    def _note(self, r, method, path):
        if r.status_code >= 500:
            PROBLEMS.append(f"{self.label}: {method} {path} answered {r.status_code}")
        elif r.mimetype == "text/html" and "Traceback (most recent call last)" in r.get_data(as_text=True)[:200000]:
            PROBLEMS.append(f"{self.label}: {method} {path} showed a traceback")
        return r

    def get(self, path, **kw):
        return self._note(self.client.get(path, base_url=self.base, environ_base={"REMOTE_ADDR": self.addr}, **kw), "GET", path)

    def text(self, path):
        return self.get(path).get_data(as_text=True)

    def post(self, path, data=None, page=None, token=True):
        data = dict(data or {})
        if token is True:
            data["_csrf_token"] = csrf_from(self.text(page or self.form_page))
        elif token:
            data["_csrf_token"] = token
        r = self.client.post(path, data=data, base_url=self.base, environ_base={"REMOTE_ADDR": self.addr})
        return self._note(r, "POST", path)

    def said(self):
        with self.client.session_transaction(base_url=self.base) as sess:
            found = list(sess.pop("_flashes", []))
        return " | ".join(text for _, text in found).lower()


class School:
    def __init__(self, code, base):
        with platform_session() as s:
            self.info = to_info(get_tenant(s, code))
        self.code, self.base = code, base

    def sql(self, statement, **params):
        with engine_for(self.info).begin() as conn:
            result = conn.execute(sa.text(statement), params)
            return result.fetchall() if result.returns_rows else None

    def one(self, statement, **params):
        rows = self.sql(statement, **params)
        return rows[0][0] if rows else None


pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
console = A.app.test_client()
console.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                      "_csrf_token": csrf_from(console.get("/platform/login", base_url=PL).get_data(as_text=True))}, base_url=PL)
form = {"_csrf_token": csrf_from(console.get("/platform/schools/new", base_url=PL).get_data(as_text=True)), "name": "Alpha School", "code": "alpha"}
console.post("/platform/schools/new", data=form, base_url=PL, content_type="multipart/form-data")
alpha = School("alpha", ALPHA)
page = console.get("/platform/schools/alpha", base_url=PL).get_data(as_text=True)
r = console.post("/platform/schools/alpha/enter", data={"_csrf_token": csrf_from(page)}, base_url=PL)
op = Person(ALPHA, label="School Admin")
op.get(r.headers["Location"][len(ALPHA):])
op.get("/admin/workspace/school")

PASSWORD = "Fixture-password-9"
HASH = generate_password_hash(PASSWORD, method="pbkdf2:sha256:1000")
cls = {name: alpha.one("SELECT id FROM school_classes WHERE name = :n", n=name) for name in ("JSS 1", "JSS 2", "SSS 1", "Nursery 1", "Crèche")}
SESSION = alpha.one("SELECT id FROM academic_sessions WHERE is_current = 1")
role = {name: alpha.one("SELECT id FROM admin_types WHERE name = :n", n=name)
        for name in ("Head Teacher", "Class Teacher", "Subject Teacher", "Finance Manager", "Librarian")}

# ================================================================ the seeded structure
check("Crèche and Nursery 1-3 are seeded, switched off until the school runs them",
      cls["Crèche"] and cls["Nursery 1"] and alpha.one("SELECT COUNT(*) FROM school_classes WHERE stage IN ('Crèche','Nursery') AND active = 0") == 4)
check("the teaching roles sit in the chain: Head Teacher, Class Teacher, Subject Teacher",
      [alpha.one("SELECT level FROM admin_types WHERE id = :i", i=role[n]) for n in ("Head Teacher", "Class Teacher", "Subject Teacher")]
      == ["head_teacher", "class_teacher", "subject_teacher"])
check("only the head teacher's role can assign teaching duties, and only the teachers can send results on",
      alpha.one("SELECT COUNT(*) FROM admin_type_permissions tp JOIN permissions p ON p.id = tp.permission_id "
                "WHERE p.code = 'school.staff.assign' AND tp.admin_type_id IN (:c, :s)", c=role["Class Teacher"], s=role["Subject Teacher"]) == 0
      and alpha.one("SELECT COUNT(*) FROM admin_type_permissions tp JOIN permissions p ON p.id = tp.permission_id "
                    "WHERE p.code = 'school.results.release' AND tp.admin_type_id = :s", s=role["Subject Teacher"]) == 0)

classes_page = op.text("/admin/school/classes")
check("the Classes page explains early years and SSS departments", "paper only" in classes_page and "Departments apply" in classes_page)
op.post(f"/admin/school/classes/{cls['Nursery 1']}/toggle", page="/admin/school/classes")
check("a school switches Nursery 1 on", alpha.one("SELECT active FROM school_classes WHERE id = :i", i=cls["Nursery 1"]) == 1)

# ================================================================ subjects with SSS departments
def new_subject(name, class_names, departments=()):
    op.post("/admin/school/subjects/new", {"name": name, "code": "", "class_ids": [str(cls[c]) for c in class_names],
                                           "departments": list(departments)}, page="/admin/school/subjects/new")
    return alpha.one("SELECT id FROM school_subjects WHERE name = :n", n=name)


MATH = new_subject("Mathematics", ["JSS 1", "JSS 2", "SSS 1", "Nursery 1"])
PHYS = new_subject("Physics", ["SSS 1"], ["Science"])
LIT = new_subject("Literature", ["SSS 1"], ["Art", "Commercial"])
BASIC = new_subject("Basic Science", ["JSS 1", "JSS 2"], ["Science", "Art", "Commercial"])
check("a subject records the SSS departments that take it (none ticked, or all, means every department)",
      alpha.one("SELECT departments FROM school_subjects WHERE id = :i", i=PHYS) == "Science"
      and alpha.one("SELECT departments FROM school_subjects WHERE id = :i", i=LIT) == "Art,Commercial"
      and alpha.one("SELECT departments FROM school_subjects WHERE id = :i", i=MATH) is None
      and alpha.one("SELECT departments FROM school_subjects WHERE id = :i", i=BASIC) is None)
check("the subjects list shows a department-only subject", "SSS: Science only" in op.text("/admin/school/subjects"))

# ================================================================ students, with an SSS department
def new_student(first, class_name, department=""):
    op.post("/admin/school/students/new", {"first_name": first, "last_name": "Test", "gender": "Female",
                                           "class_id": str(cls[class_name]), "state_of_origin": "Lagos",
                                           "department": department}, page="/admin/school/students/new")
    sid = alpha.one("SELECT id FROM students WHERE first_name = :f", f=first)
    no = alpha.one("SELECT admission_no FROM students WHERE id = :i", i=sid)
    alpha.sql("UPDATE students SET login_username = :u, login_password_hash = :h, account_active = 1, "
              "password_must_change = 0 WHERE id = :i", u=no.upper(), h=HASH, i=sid)
    return sid, no.upper()


J1A, _ = new_student("Juniorone", "JSS 1", department="Science")
J2A, _ = new_student("Juniortwo", "JSS 2")
SCI, SCI_LOGIN = new_student("Sciencekid", "SSS 1", "Science")
ART, ART_LOGIN = new_student("Artkid", "SSS 1", "Art")
NODEPT, _ = new_student("Undecided", "SSS 1")
NUR, NUR_LOGIN = new_student("Tinytot", "Nursery 1")
check("an SSS student keeps their department; a junior student never gets one",
      alpha.one("SELECT department FROM students WHERE id = :i", i=SCI) == "Science"
      and alpha.one("SELECT department FROM students WHERE id = :i", i=J1A) is None)
r = op.post("/admin/school/students/new", {"first_name": "Bad", "last_name": "Dept", "gender": "Male", "class_id": str(cls["SSS 1"]),
                                           "state_of_origin": "Lagos", "department": "Music"}, page="/admin/school/students/new")
check("an unknown department is refused with a plain message", r.status_code == 200 and "Science, Art or Commercial" in r.get_data(as_text=True))

# ================================================================ registering staff with teaching duties
def new_staff(username, roles, class_teacher=(), subjects=()):
    data = {"username": username, "display_name": username.replace(".", " ").title(), "admin_type_ids": [str(role[r]) for r in roles],
            "duty_class_teacher": [str(cls[c]) for c in class_teacher],
            "duty_class": [str(cls[c]) for c, _s, _d in subjects] + [""],
            "duty_subject": [str(s) for _c, s, _d in subjects] + [""],
            "duty_department": [d for _c, _s, d in subjects] + [""]}
    r = op.post("/admin/administration/admins/new", data, page="/admin/administration/admins/new")
    aid = alpha.one("SELECT id FROM admins WHERE username = :u", u=username)
    alpha.sql("UPDATE admins SET password_hash = :h, password_must_change = 0 WHERE id = :i", h=HASH, i=aid)
    person = Person(ALPHA, label=username)
    person.post("/login", {"username": username, "password": PASSWORD}, page="/login")
    person.get("/admin/workspace/school")
    return person, aid, r


staff_form = op.text("/admin/administration/admins/new")
check("the staff form lists roles from the most senior down and offers teaching duties",
      staff_form.index("Head teacher") < staff_form.index("Class teacher") < staff_form.index("Subject teacher")
      and 'name="duty_class_teacher"' in staff_form and 'name="duty_department"' in staff_form)

head, HEAD, _ = new_staff("head", ["Head Teacher"])
bursar, BURSAR, _ = new_staff("bursar", ["Finance Manager"])
ct_j1, CT_J1, _ = new_staff("ct.jss1", ["Class Teacher"], class_teacher=["JSS 1"])
ct_s1, CT_S1, _ = new_staff("ct.sss1", ["Class Teacher"], class_teacher=["SSS 1"])
ct_nur, CT_NUR, _ = new_staff("ct.nursery", ["Class Teacher"], class_teacher=["Nursery 1"])
st, ST, r = new_staff("st.maths", ["Subject Teacher"],
                      subjects=[("JSS 1", MATH, "Science"), ("SSS 1", MATH, "Science"), ("SSS 1", PHYS, "")])
duties = alpha.sql("SELECT c.name, s.name, d.department FROM teaching_duties d JOIN school_classes c ON c.id = d.class_id "
                   "LEFT JOIN school_subjects s ON s.id = d.subject_id WHERE d.admin_id = :a ORDER BY d.id", a=ST)
check("the staff form saves subject duties, keeping a department only in SSS",
      [tuple(x) for x in duties] == [("JSS 1", "Mathematics", None), ("SSS 1", "Mathematics", "Science"), ("SSS 1", "Physics", None)], duties)
check("a class teacher's duty is the class with no subject",
      alpha.one("SELECT COUNT(*) FROM teaching_duties WHERE admin_id = :a AND class_id = :c AND subject_id IS NULL", a=CT_J1, c=cls["JSS 1"]) == 1)
check("the duties are written to the audit log in plain words",
      "Mathematics — SSS 1 (Science)" in (alpha.one("SELECT details FROM audit_logs WHERE action = 'admin_created' AND target_id = :t", t=str(ST)) or ""))

check("the staff directory says a teacher is limited to their duties", "<strong>Teacher</strong> · 3 class or subject duties" in op.text("/admin/administration/admins"))
check("a student's record shows their SSS department", "Department" in (d := op.text(f"/admin/school/students/{SCI}")) and "Science" in d)

# ================================================================ a subject teacher sees only their own
def chips(page):
    """The class chips on an Academics page."""
    return set(re.findall(r'class="ac-chip[^"]*"[^>]*>([^<]+?) <small>', page))
cards = st.text("/admin/school/results")
check("a subject teacher's Results page lists only the classes they teach", chips(cards) == {"JSS 1", "SSS 1"}, chips(cards))
check("a subject teacher cannot open a class they do not teach", "Juniortwo" not in st.text("/admin/school/results?class=JSS+2"))
sss1_list = st.text("/admin/school/results?class=SSS+1")
check("in SSS a subject teacher still sees every student of the departments they teach (Physics is all of SSS 1)",
      "Sciencekid" in sss1_list and "Artkid" in sss1_list)

MANUAL = "/admin/school/results/manual/new"


def enter(person, student, subject, class_name, score=12):
    return person.post(MANUAL, {"class_id": str(cls[class_name]), "session_id": str(SESSION), "student_id": str(student),
                                "subject_id": str(subject), "term": "First Term", "took_test": "yes", "test_score": str(score),
                                "test_max": "40", "exam_score": "40", "exam_max": "60"}, page=MANUAL)


def marks(student, subject):
    return [tuple(x) for x in alpha.sql("SELECT component_name, status FROM school_student_results WHERE student_id = :s "
                                        "AND subject_id = :j ORDER BY component_name", s=student, j=subject)]


r = enter(st, J1A, MATH, "JSS 1")
check("a subject teacher records their own subject for their class", r.status_code == 302 and marks(J1A, MATH) == [("Exam", "entered"), ("Test", "entered")])
r = enter(st, J1A, BASIC, "JSS 1")
check("a subject they do not teach is refused", r.status_code == 200 and not marks(J1A, BASIC))
r = enter(st, ART, MATH, "SSS 1")
check("an SSS student outside the department they teach the subject to is refused",
      r.status_code == 200 and "do not teach this subject" in r.get_data(as_text=True) and not marks(ART, MATH))
r = enter(st, SCI, MATH, "SSS 1")
check("a student of their department is accepted", r.status_code == 302 and len(marks(SCI, MATH)) == 2)
r = enter(st, NODEPT, MATH, "SSS 1")
check("a student whose department is not set yet is not hidden from them", r.status_code == 302 and len(marks(NODEPT, MATH)) == 2)
r = enter(st, J2A, MATH, "JSS 2")
check("a class they do not teach is refused", r.status_code == 200 and not marks(J2A, MATH))
picker = st.text(f"{MANUAL}?class_id={cls['SSS 1']}&subject_id={MATH}&session_id={SESSION}")
check("the score form offers a department-limited teacher only the students they teach", "Sciencekid" in picker and "Artkid" not in picker)

# ================================================================ the hand-off: subject teacher -> class teacher
r = st.post("/admin/school/results/release-term", {"student_id": str(J1A), "session_id": str(SESSION), "term": "First Term"})
check("a subject teacher cannot release results", refused(r) and marks(J1A, MATH) == [("Exam", "entered"), ("Test", "entered")])
term_page = f"/admin/school/results?class=JSS+1&student={J1A}&session={SESSION}&term=First+Term"
check("the subject teacher's term view offers Send all to class teacher, not Release",
      "Send all to class teacher" in (t := st.text(term_page)) and "Release all results for this term" not in t)
r = st.post("/admin/school/results/submit-term", {"student_id": str(J1A), "session_id": str(SESSION), "term": "First Term"}, page=term_page)
check("Send all to class teacher makes the subject teacher's marks ready to release",
      r.status_code == 302 and marks(J1A, MATH) == [("Exam", "approved"), ("Test", "approved")])
r = ct_s1.post("/admin/school/results/release-term", {"student_id": str(J1A), "session_id": str(SESSION), "term": "First Term"})
check("another class's class teacher cannot release them", refused(r) and marks(J1A, MATH)[0][1] == "approved")
check("the class teacher sees Release for their own class", "Release all results for this term" in ct_j1.text(term_page))
r = ct_j1.post("/admin/school/results/release-term", {"student_id": str(J1A), "session_id": str(SESSION), "term": "First Term"}, page=term_page)
check("the class teacher releases them to the student and parents", r.status_code == 302 and marks(J1A, MATH) == [("Exam", "released"), ("Test", "released")])

# class-wide send, then a class teacher sends one back
r = st.post("/admin/school/results/submit-class", {"class": "SSS 1", "session": str(SESSION), "term": "First Term"},
            page=f"/admin/school/results/submit-class?class=SSS+1&session={SESSION}&term=First+Term")
check("a subject teacher sends a whole class's marks to the class teacher at once",
      r.status_code == 302 and {s for _c, s in marks(SCI, MATH) + marks(NODEPT, MATH)} == {"approved"})
one_mark = alpha.one("SELECT id FROM school_student_results WHERE student_id = :s AND subject_id = :j AND component_name = 'Test'", s=SCI, j=MATH)
sci_term = f"/admin/school/results?class=SSS+1&student={SCI}&session={SESSION}&term=First+Term"
r = st.post(f"/admin/school/results/{one_mark}/workflow", {"action": "return"})
check("a subject teacher cannot send a mark back", refused(r) and "Send back" not in st.text(sci_term)
      and alpha.one("SELECT status FROM school_student_results WHERE id = :i", i=one_mark) == "approved",
      f"{r.status_code} refused={refused(r)} sendback={'Send back' in st.text(sci_term)} status={alpha.one('SELECT status FROM school_student_results WHERE id = :i', i=one_mark)}")
check("the class teacher's term view offers Send back on a mark that is ready", "Send back" in ct_s1.text(sci_term))
r = ct_s1.post(f"/admin/school/results/{one_mark}/workflow", {"action": "return"}, page=sci_term)
check("the class teacher sends one back to be corrected", r.status_code == 302 and alpha.one("SELECT status FROM school_student_results WHERE id = :i", i=one_mark) == "entered")
check("the subject teacher sees the returned mark with Send to class teacher", "Send to class teacher" in st.text(sci_term))
r = st.post(f"/admin/school/results/{one_mark}/workflow", {"action": "submit"}, page=sci_term)
check("the subject teacher sends the corrected mark again", r.status_code == 302 and alpha.one("SELECT status FROM school_student_results WHERE id = :i", i=one_mark) == "approved")
r = ct_j1.post("/admin/school/results/release-class", {"class": "SSS 1", "session": str(SESSION), "term": "First Term"})
check("a class teacher cannot release another class at once either", refused(r))
r = ct_s1.post("/admin/school/results/release-class", {"class": "SSS 1", "session": str(SESSION), "term": "First Term"},
               page=f"/admin/school/results/release-class?class=SSS+1&session={SESSION}&term=First+Term")
check("the SSS 1 class teacher releases the class's ready marks", {s for _c, s in marks(SCI, MATH)} == {"released"})
senior_card = op.text(f"/admin/school/report-cards/{SCI}/{SESSION}/first-term")
check("an SSS student's report card shows their department", "<dt>Department</dt><dd>Science</dd>" in senior_card)
check("a JSS student's report card has no department line", "<dt>Department</dt>" not in op.text(f"/admin/school/report-cards/{J1A}/{SESSION}/first-term"))

# ================================================================ the whole-school release date
r = ct_j1.post("/admin/school/results/release-schedule", {"result_release_at": "2027-01-01T09:00"})
check("a class teacher cannot set the whole school's release date", refused(r) and "release-schedule" not in ct_j1.text("/admin/school/results"))
r = head.post("/admin/school/results/release-schedule", {"result_release_at": "2027-01-01T09:00"})
check("a head teacher can", r.status_code == 302 and alpha.one("SELECT result_release_at FROM academic_sessions WHERE id = :i", i=SESSION))

# ================================================================ the Teachers page
check("a subject teacher signing in goes straight to the school portal: there is no workspace to choose",
      st.get("/admin/home").status_code == 302 and st.get("/admin/home").headers["Location"].endswith("/admin/school"))
r = st.get("/admin/workspace/entrance")
check("…and the Entrance examination workspace is refused to them (they were not given it)",
      r.status_code == 302 and "/admin/home" in r.headers["Location"] and st.get("/admin/candidates").status_code == 302)
# (What's new in the dock mentions Switch workspace by name, so the menu entry itself is what is looked for.)
check("…nor does their menu offer Switch workspace", "<span>Switch workspace</span>" not in st.text("/admin/school")
      and "<span>Switch workspace</span>" in op.text("/admin/school"))
check("the School Admin, who has both, still chooses on Workspace Home", op.get("/admin/home").status_code == 200)
op.get("/admin/workspace/school")   # Workspace Home clears the choice; carry on in the school portal
check("a class teacher has no Teachers page", refused(ct_j1.get("/admin/school/teaching")))
board = head.text("/admin/school/teaching")
check("the head teacher's Teachers page shows each class's class teacher and subject teachers",
      "Ct Jss1" in board and "St Maths" in board and "Not assigned" in board)
check("it lists the teachers below them, not the bursar", f"/admin/school/teaching/{ST}" in board and f"/admin/school/teaching/{BURSAR}" not in board)
r = head.post(f"/admin/school/teaching/{ST}", {"duty_class": [str(cls["JSS 2"])], "duty_subject": [str(MATH)], "duty_department": [""]},
              page=f"/admin/school/teaching/{ST}")
check("the head teacher reassigns a subject teacher", r.status_code == 302
      and [tuple(x) for x in alpha.sql("SELECT class_id, subject_id FROM teaching_duties WHERE admin_id = :a", a=ST)] == [(cls["JSS 2"], MATH)])
check("the change takes effect at once: the teacher now sees JSS 2, not JSS 1",
      chips(st.text("/admin/school/results")) == {"JSS 2"})
check("a head teacher cannot change the bursar's duties", refused(head.get(f"/admin/school/teaching/{BURSAR}")))
r = head.post(f"/admin/school/teaching/{HEAD}", {"duty_class_teacher": [str(cls["JSS 1"])]})
check("nor their own", refused(r) and not alpha.one("SELECT COUNT(*) FROM teaching_duties WHERE admin_id = :a", a=HEAD))

# ================================================================ SSS departments on the student side
alpha.sql("INSERT INTO school_assessments (assessment_type, title, class_id, subject_id, session_id, duration_minutes, question_count, "
          "active, created_by, created_at, term) VALUES ('test', 'Physics Test One', :c, :s, :x, 20, 1, 1, :a, '2026-09-01', 'First Term')",
          c=cls["SSS 1"], s=PHYS, x=SESSION, a=CT_S1)
sci_student = Person(ALPHA, label="science student")
sci_student.post("/login", {"username": SCI_LOGIN, "password": PASSWORD}, page="/login")
art_student = Person(ALPHA, label="art student")
art_student.post("/login", {"username": ART_LOGIN, "password": PASSWORD}, page="/login")
check("a Science student sees the Physics test; an Art student does not",
      "Physics Test One" in sci_student.text("/student/tests") and "Physics Test One" not in art_student.text("/student/tests"))
r = op.post("/admin/school/assignments/new", {"title": "Physics homework", "class_id": str(cls["SSS 1"]), "subject_id": str(PHYS),
                                              "session_id": str(SESSION), "term": "First Term", "assignment_type": "written",
                                              "timing_mode": "untimed", "issue_to": "class"}, page="/admin/school/assignments/new")
given = {row[0] for row in alpha.sql("SELECT a.student_id FROM assignment_students a JOIN school_assignments s ON s.id = a.assignment_id "
                                     "WHERE s.title = 'Physics homework'")}
check("work for a Science subject issued to the whole class goes to Science students (and the undecided), not Art",
      given == {SCI, NODEPT}, given)

# ================================================================ Crèche and Nursery
nursery_pupil = Person(ALPHA, label="nursery pupil")
r = nursery_pupil.post("/login", {"username": NUR_LOGIN, "password": PASSWORD}, page="/login")
check("a Nursery pupil cannot sign in, and is told the parent portal is the way in",
      r.status_code == 200 and "do not sign in" in r.get_data(as_text=True) and nursery_pupil.get("/student/dashboard").status_code == 302)
r = op.post("/admin/school/assignments/new", {"title": "Nursery homework", "class_id": str(cls["Nursery 1"]), "subject_id": str(MATH),
                                              "session_id": str(SESSION), "term": "First Term", "assignment_type": "written",
                                              "timing_mode": "untimed", "issue_to": "class"}, page="/admin/school/assignments/new")
check("no online assignment can be set for a Nursery class", r.status_code == 200 and not alpha.one("SELECT COUNT(*) FROM school_assignments WHERE title = 'Nursery homework'"))
check("Nursery classes are not offered for tests", f'value="{cls["Nursery 1"]}"' not in op.text("/admin/school/tests/new"))
r = enter(ct_nur, NUR, MATH, "Nursery 1")
check("the Nursery class teacher records a pupil's scores", r.status_code == 302 and len(marks(NUR, MATH)) == 2)
alpha.sql("INSERT INTO parent_accounts (username, display_name, password_hash, active, password_must_change, created_at) "
          "VALUES ('mum.tot', 'Mum Tot', :h, 1, 0, '2026-09-01')", h=HASH)
PARENT = alpha.one("SELECT id FROM parent_accounts WHERE username = 'mum.tot'")
alpha.sql("INSERT INTO parent_student_links (parent_id, student_id, active, created_at) VALUES (:p, :s, 1, '2026-09-01')", p=PARENT, s=NUR)
parent = Person(ALPHA, label="nursery parent")
parent.post("/login", {"username": "mum.tot", "password": PASSWORD}, page="/login")
child = parent.text(f"/parent/children/{NUR}")
check("the Nursery parent sees fees, results and report cards, but no Work tab, attendance or timetable",
      "Fee account" in child and "Report cards" in child and 'id="tab-work"' not in child
      and 'id="attendance"' not in child and "Assignment average" not in child)

# ================================================================ an Overview made for each role
librarian, _LIB, _ = new_staff("librarian", ["Librarian"])
views = {name: person.text("/admin/school?term=First+Term") for name, person in
         (("proprietor", op), ("head", head), ("bursar", bursar), ("librarian", librarian),
          ("class teacher", ct_s1), ("subject teacher", st))}
# Markers that only appear in a rendered panel (class names alone also appear in the page's own CSS).
ACADEMIC, MONEY, MINE, BOOKS = 'id="ovClasses"', 'id="ovDaily"', 'id="ovTeach"', 'id="ovLibrary"'
RANKING = ("<th>Top student</th>", "Best this term")
def ranks(page): return any(m in page for m in RANKING)
check("the proprietor's Overview has the setup checklist, the whole school and the rankings",
      "Setting up the school" in views["proprietor"] and ACADEMIC in views["proprietor"] and ranks(views["proprietor"]))
check("the head teacher sees the whole school and the rankings, but not the setup checklist or the fees",
      ACADEMIC in views["head"] and ranks(views["head"]) and "Setting up the school" not in views["head"] and MONEY not in views["head"])
check("the bursar sees fees and collections, and nothing about results or the best students",
      MONEY in views["bursar"] and not ranks(views["bursar"])
      and ACADEMIC not in views["bursar"] and MINE not in views["bursar"] and "Sciencekid" not in views["bursar"])
check("the librarian sees the library, and neither fees nor results",
      BOOKS in views["librarian"] and MONEY not in views["librarian"] and not ranks(views["librarian"]) and ACADEMIC not in views["librarian"])
check("a class teacher sees their own class (register, marks to release, comments) and its best student, not the school",
      "SSS 1" in views["class teacher"] and "Today&#39;s register" in views["class teacher"] and ranks(views["class teacher"])
      and "Sciencekid" in views["class teacher"] and ACADEMIC not in views["class teacher"] and MONEY not in views["class teacher"])
check("a subject teacher sees the subjects they teach, and no rankings, fees or whole-school figures",
      MINE in views["subject teacher"] and "JSS 2" in views["subject teacher"] and not ranks(views["subject teacher"])
      and MONEY not in views["subject teacher"] and ACADEMIC not in views["subject teacher"])
def go_to(page):
    """The labels of the Overview's "Go to" links (not the rest of the page, whose notices can name anything)."""
    nav = re.search(r'<nav class="ov-p ov-links" aria-label="Your pages">(.*?)</nav>', page, re.S)
    return set(re.findall(r'<a href="[^"]*">([^<]+)</a>', nav.group(1))) if nav else set()
check("the shortcuts are only pages the person can open",
      "Record a payment" in go_to(views["bursar"]) and "Enter offline scores" not in go_to(views["bursar"])
      and "Enter offline scores" in go_to(views["subject teacher"]) and "Record a payment" not in go_to(views["subject teacher"]),
      str((go_to(views["bursar"]), go_to(views["subject teacher"]))))

# ================================================================ class teacher of one class, subject teacher in another
mixed, MIXED, _ = new_staff("mixed.teacher", ["Class Teacher"], class_teacher=["JSS 2"], subjects=[("SSS 1", PHYS, "")])
register = mixed.text(f"/admin/school/attendance?class_id={cls['SSS 1']}&session_id={SESSION}")
check("the register offers only the class they are class teacher of, not a class they teach a subject in",
      re.findall(r'class="ac-chip[^"]*" href="[^"]*class_id=(\d+)', register) == [str(cls["JSS 2"])] and "Sciencekid" not in register)
today = alpha.one("SELECT CURRENT_DATE::text")
mixed.post("/admin/school/attendance", {"class_id": str(cls["SSS 1"]), "session_id": str(SESSION), "term": "First Term",
                                        "date": today, f"status_{SCI}": "absent"})
check("a register posted for the subject class is not saved",
      not alpha.one("SELECT COUNT(*) FROM attendance_records WHERE class_id = :c AND marked_by_admin_id = :a", c=cls["SSS 1"], a=MIXED))
mixed.post("/admin/school/attendance", {"class_id": str(cls["JSS 2"]), "session_id": str(SESSION), "term": "First Term",
                                        "date": today, f"status_{J2A}": "present"})
check("the register for their own class is saved",
      alpha.one("SELECT status FROM attendance_records WHERE student_id = :s AND date = :d", s=J2A, d=today) == "present")
check("the term attendance summary (read only) still covers the class they teach a subject in",
      "Sciencekid" in mixed.text(f"/admin/school/attendance/summary?class_id={cls['SSS 1']}&session_id={SESSION}"))
cards = mixed.text("/admin/school/report-cards")
check("report cards offer only the class they are class teacher of",
      f'value="{cls["JSS 2"]}"' in cards and f'value="{cls["SSS 1"]}"' not in cards)
r = mixed.post("/admin/school/report-cards/comments", {"student_id": str(SCI), "session_id": str(SESSION), "term": "First Term",
                                                     "comment": "Not my class to comment on."})
check("a report card comment for a student in their subject class is refused",
      r.status_code == 400 and not alpha.one("SELECT COUNT(*) FROM report_card_comments WHERE author_admin_id = :a", a=MIXED))
r = mixed.post("/admin/school/report-cards/comments", {"student_id": str(J2A), "session_id": str(SESSION), "term": "First Term",
                                                     "comment": "A steady term."})
check("a comment for a student in their own class is saved",
      r.status_code == 200 and alpha.one("SELECT comment FROM report_card_comments WHERE student_id = :s", s=J2A) == "A steady term.")
r = mixed.get(f"/admin/school/report-cards/{SCI}/{SESSION}/first-term")
check("they cannot open the report card of a student in their subject class", r.status_code in (403, 404))
r = enter(mixed, SCI, PHYS, "SSS 1", score=20)
check("they still record Physics for the SSS 1 class they teach", r.status_code == 302 and len(marks(SCI, PHYS)) == 2)
r = mixed.post("/admin/school/results/release-term", {"student_id": str(SCI), "session_id": str(SESSION), "term": "First Term"})
check("but they cannot release SSS 1's results", refused(r))

# ================================================================ a subject teacher sets their own work
maker, MAKER, _ = new_staff("maker.teacher", ["Subject Teacher"], subjects=[("JSS 1", MATH, ""), ("SSS 1", PHYS, "")])
for page in ("/admin/school/tests/new", "/admin/school/practice-tests/new", "/admin/school/examinations/new",
             "/admin/school/assignments/new", "/admin/school/projects/new"):
    check(f"a subject teacher can open {page}", maker.get(page).status_code == 200)
form = maker.text("/admin/school/tests/new")
reach = json.loads(re.search(r'<script type="application/json" id="subjectReach"[^>]*>(.*?)</script>', form).group(1))
check("the new-work form narrows the subject list to what they teach in each class",
      reach == {"by": {str(MATH): [cls["JSS 1"]], str(PHYS): [cls["SSS 1"]]}, "every": []}, reach)
check("the head teacher's form is not narrowed", 'id="subjectReach"' not in head.text("/admin/school/tests/new"))


def new_assessment(kind, title, class_name, subject):
    path = {"test": "/admin/school/tests/new", "practice": "/admin/school/practice-tests/new", "examination": "/admin/school/examinations/new"}[kind]
    r = maker.post(path, {"title": title, "class_id": str(cls[class_name]), "subject_id": str(subject), "session_id": str(SESSION),
                          "term": "First Term", "duration_minutes": "30", "instructions": ""}, page=path)
    return r, alpha.one("SELECT id FROM school_assessments WHERE title = :t AND created_by = :a", t=title, a=MAKER)


r, own_test = new_assessment("test", "Maker JSS1 maths test", "JSS 1", MATH)
check("a subject teacher creates a test for their own subject and class", r.status_code == 302 and own_test)
r, own_exam = new_assessment("examination", "Maker JSS1 maths exam", "JSS 1", MATH)
check("…an examination", r.status_code == 302 and own_exam)
r, own_practice = new_assessment("practice", "Maker SSS1 physics practice", "SSS 1", PHYS)
check("…and a practice test, in their other class and subject", r.status_code == 302 and own_practice)
r, wrong = new_assessment("test", "Maker JSS1 physics test", "JSS 1", PHYS)
check("a subject they do not teach in that class is refused", r.status_code == 200 and not wrong)
r, wrong = new_assessment("examination", "Maker JSS2 maths exam", "JSS 2", MATH)
check("…and so is a class they do not teach", r.status_code == 200 and not wrong)
question = {"question_text": "What is 2 + 2?", "instruction": "", "option_a": "3", "option_b": "4", "option_c": "5", "option_d": "6",
            "correct_option": "1", "points": "1"}
maker.post(f"/admin/school/assessments/{own_test}/questions/new", question, page=f"/admin/school/assessments/{own_test}")
check("they add questions to their own test", alpha.one("SELECT COUNT(*) FROM school_questions WHERE assessment_id = :a", a=own_test) == 1)
op.post("/admin/school/tests/new", {"title": "Head office science test", "class_id": str(cls["JSS 1"]), "subject_id": str(BASIC),
                                    "session_id": str(SESSION), "term": "First Term", "duration_minutes": "30"}, page="/admin/school/tests/new")
others = alpha.one("SELECT id FROM school_assessments WHERE title = 'Head office science test'")
r = maker.post(f"/admin/school/assessments/{others}/questions/new", question)
check("but not to a test in a subject they do not teach", refused(r)
      and not alpha.one("SELECT COUNT(*) FROM school_questions WHERE assessment_id = :a", a=others))
check("nor open it", refused(maker.get(f"/admin/school/assessments/{others}")))
r = maker.post("/admin/school/assignments/new", {"title": "Maker fractions quiz", "class_id": str(cls["JSS 1"]), "subject_id": str(MATH),
                                                 "session_id": str(SESSION), "term": "First Term", "assignment_type": "quiz",
                                                 "timing_mode": "untimed", "issue_to": "class"}, page="/admin/school/assignments/new")
quiz = alpha.one("SELECT id FROM school_assignments WHERE title = 'Maker fractions quiz' AND created_by = :a", a=MAKER)
check("they create an assignment for their class", r.status_code == 302 and quiz
      and alpha.one("SELECT COUNT(*) FROM assignment_students WHERE assignment_id = :a", a=quiz) >= 1)
maker.post(f"/admin/school/assignments/{quiz}/questions", question, page=f"/admin/school/assignments/{quiz}")
check("…and add a question to it", alpha.one("SELECT COUNT(*) FROM assignment_questions WHERE assignment_id = :a", a=quiz) == 1)
r = maker.post("/admin/school/projects/new", {"title": "Maker pendulum project", "class_id": str(cls["SSS 1"]), "subject_id": str(PHYS),
                                              "session_id": str(SESSION), "term": "First Term", "date_given": "2026-10-01",
                                              "issue_to": "class"}, page="/admin/school/projects/new")
proj = alpha.one("SELECT id FROM school_projects WHERE title = 'Maker pendulum project' AND created_by = :a", a=MAKER)
given = {row[0] for row in alpha.sql("SELECT student_id FROM project_students WHERE project_id = :p", p=proj or 0)}
check("they create a project for their SSS class, which goes only to students who take the subject",
      r.status_code == 302 and given == {SCI, NODEPT}, given)
r = maker.post("/admin/school/assignments/new", {"title": "Maker sneaky essay", "class_id": str(cls["JSS 2"]), "subject_id": str(MATH),
                                                 "session_id": str(SESSION), "term": "First Term", "assignment_type": "written",
                                                 "timing_mode": "untimed", "issue_to": "class"}, page="/admin/school/assignments/new")
check("an assignment for a class they do not teach is refused",
      r.status_code == 200 and not alpha.one("SELECT COUNT(*) FROM school_assignments WHERE title = 'Maker sneaky essay'"))
lists = maker.text("/admin/school/tests?all=1")
check("their Tests list shows their own class's tests, not other subjects'",
      "Maker JSS1 maths test" in lists and "Head office science test" not in lists)

# ================================================================ Academics: one menu item, pages that load in place
page = op.text("/admin/school")
menu = page[page.index('class="rail-nav"'):page.index("</nav>", page.index('class="rail-nav"'))]
check("the menu has one Academics item in place of Results, Assignments, Projects, Tests, Practice tests and Examinations",
      ">Academics<" in menu and ">Assignments<" not in menu and ">Examinations<" not in menu and "Results &amp; Records</span>" not in menu)
tabs = op.text("/admin/school/assignments")
check("each Academics page carries the section tabs", all(t in tabs for t in (">Results<", ">Assignments<", ">Projects<", ">Tests<", ">Practice tests<", ">Examinations<")))
frag = op.client.get("/admin/school/tests", base_url=ALPHA, headers={"X-Fragment": "1"}, environ_base={"REMOTE_ADDR": op.addr})
body = frag.get_data(as_text=True)
check("a page asked for in place answers with its content only, not the menu and header around it",
      frag.status_code == 200 and "<html" not in body and "rail-nav" not in body and 'class="ac-tabs"' in body and "data-fragment-title" in body)
check("the in-place loader is on every page, scoped to Students, Academics, Finance and the timetable",
      "inplace.js" in page and all(p in page for p in ("/admin/school/students,", "/admin/school/results,", "=/admin/finance,", "/admin/school/timetable\"")))


# ================================================================ entering scores: one student after another
ENTRY = "/admin/school/results/manual/new"
entry = op.text(f"{ENTRY}?class_id={cls['SSS 1']}&session_id={SESSION}&term=Second+Term&subject_id={LIT}")
check("score entry lists only the students who take the subject, with how many are done",
      "Artkid" in entry and "Undecided" in entry and "Sciencekid" not in entry and "0 of 2 scored" in entry)
check("…and offers to start with the first student without a score", "Start with the first student without a score" in entry)
r = op.post(ENTRY, {"class_id": str(cls["SSS 1"]), "session_id": str(SESSION), "term": "Second Term", "subject_id": str(LIT),
                    "student_id": str(ART), "took_test": "yes", "test_score": "15", "test_max": "20", "exam_score": "45", "exam_max": "60"}, page=ENTRY)
where = r.headers.get("Location", "")
check("saving moves straight on to the next student without a score, keeping the same maxima",
      r.status_code == 302 and f"student_id={NODEPT}" in where and "tmax=20" in where and "emax=60" in where, where)
nxt = op.text(where)
check("…where the Test maximum is already filled in, and the count moved on",
      'name="test_max" type="number" min="0.01" step="0.01" inputmode="decimal" value="20"' in nxt and "1 of 2 students have a score in Literature" in nxt)
students_step = op.text(f"{ENTRY}?class_id={cls['SSS 1']}&session_id={SESSION}&term=Second+Term&subject_id={LIT}")
check("…and the student list shows the saved score beside the student", ">45/60<" in students_step and "1 of 2 scored" in students_step)
r = op.post(ENTRY, {"class_id": str(cls["SSS 1"]), "session_id": str(SESSION), "term": "Second Term", "subject_id": str(LIT),
                    "student_id": str(NODEPT), "took_test": "no", "absence_reason": "Ill on the day", "exam_score": "50", "exam_max": "60"}, page=ENTRY)
check("the last student in the class stays on screen, with a note that the class is done",
      f"student_id={NODEPT}" in r.headers.get("Location", "") and "every student in the class now has a score" in op.said())
console.post("/platform/schools/alpha/branding", data={
    "_csrf_token": csrf_from(console.get("/platform/schools/alpha", base_url=PL).get_data(as_text=True)),
    "school_brand_primary": "#5a1e3c", "school_brand_accent": "#8a4a12"}, base_url=PL, content_type="multipart/form-data")
themed = op.text(ENTRY)
check("the page takes the school's own brand colours", '<style id="school-theme">' in themed and "--navy:#5a1e3c" in themed and "--blue:#8a4a12" in themed,
      (re.findall(r'<style id="school-theme">[^<]*', themed) or ['no theme'])[0][:160] + ' | ' + str(re.findall(r'class="flash [^"]*"[^>]*>([^<]*)', op.text("/admin/school/branding"))))

# ================================================================ one step at a time, and editing in a pop-up
check("the button reads Enter offline scores", ">+ Enter offline scores<" in op.text("/admin/school/results"))
listing = op.text(f"/admin/school/assignments?class=JSS+1")
check("an assignment's Edit opens in a pop-up", f'/admin/school/assignments/{quiz}/edit" data-modal' in listing)
popup = op.text(f"/admin/school/assignments/{quiz}/edit?modal=1&return_to=/admin/school/assignments%3Fclass%3DJSS%2B1")
check("…which leaves out the page frame and shows the guided sections",
      'class="ac-modal-head"' in popup and 'class="ac-tabs"' not in popup and popup.count("data-step-panel data-title") == 4 and 'data-guided="free"' in popup)
form = {"title": "Maker fractions quiz (revised)", "class_id": str(cls["JSS 1"]), "subject_id": str(MATH), "session_id": str(SESSION),
        "term": "First Term", "assignment_type": "quiz", "timing_mode": "untimed", "issue_to": "class", "modal": "1",
        "return_to": "/admin/school/assignments?class=JSS+1"}
r = op.post(f"/admin/school/assignments/{quiz}/edit", form, page=f"/admin/school/assignments/{quiz}/edit")
check("saving it goes back to the list it was opened from",
      r.status_code == 302 and r.headers.get("Location", "").endswith("/admin/school/assignments?class=JSS+1")
      and alpha.one("SELECT title FROM school_assignments WHERE id = :i", i=quiz) == "Maker fractions quiz (revised)")
r = op.post(f"/admin/school/assignments/{quiz}/edit", {**form, "return_to": "https://evil.example/"}, page=f"/admin/school/assignments/{quiz}/edit")
check("…and never anywhere else", r.status_code == 302 and "evil" not in r.headers.get("Location", ""))
new_form = op.text("/admin/school/assignments/new")
check("the new-assignment form is guided, section by section", new_form.count("data-step-panel data-title") == 4 and 'data-guided="steps"' in new_form
      and "data-guided-next" in new_form)
check("the new-test form is guided too", op.text("/admin/school/tests/new").count("data-step-panel data-title") == 2)

# ================================================================ no traceback, no server error
check("no page in this run was a server error, and none showed a traceback", not PROBLEMS, "; ".join(PROBLEMS[:4]))
check("no server error was logged by any request in this run", not errors.seen, "; ".join(str(e).splitlines()[0][:100] for e in errors.seen[:3]))

dispose_engines()
passed = sum(1 for _, ok, _ in results if ok)
print(f"\n{passed}/{len(results)} checks passed")
sys.exit(0 if passed == len(results) else 1)
