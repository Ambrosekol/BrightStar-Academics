"""Class subdivisions and class transfers, proven end to end on PostgreSQL. In plain words:

A. Settings: the School Admin opens Class subdivisions (with its examples) from Settings; a JSS class is split into
   JSS 1A and JSS 1B, and the students already in it are spread across them in turn. One subdivision alone, two with
   the same name, and a class that is not JSS or SSS are refused.
B. Placing: new students (from the staff portal, and an admission made the way the entrance admit does it) go to the
   subdivision with the fewest students; in SSS a student goes to one of their department's (or one with none).
C. Locking: once locked nothing can be changed until it is unlocked.
D. Class teachers per subdivision: the class teacher of JSS 1A sees only JSS 1A's students (the student list, a
   student's page, the register and report cards); a JSS 1B student's page is refused to them.
E. Transfers: the class teacher of JSS 1A asks to move a student to JSS 1B; nothing moves until the class teacher of
   JSS 1B approves (the asker cannot approve their own); then the student moves across. Declining and cancelling
   leave the student where they are; one waiting transfer per student. An SSS department change into a subdivision
   with no class teacher waits for the School Admin. A teacher with no class is refused the page.
F. Removing a subdivision moves its students into the others, ends its class teacher's duty and cancels transfers
   into it; removing them all undoes the split.
"""
import atexit
import logging
import os
import re
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_subdivisions_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('subdivisions')
atexit.register(DROP_TEST_DATABASES)
atexit.register(shutil.rmtree, TMP, True)
os.environ.update({
    "BRIGHTSTARS_TENANTS_DIR": os.path.join(TMP, "tenants"),
    "BRIGHTSTARS_PLATFORM_HOSTS": "platform.test",
    "BRIGHTSTARS_PORTAL_DOMAIN": "portal.test",
    "BRIGHTSTARS_REGISTRY_CACHE_SECONDS": "0",
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
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import engine_for  # noqa: E402

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
        self.addr = f"10.84.{Person.count[0]}.1"

    def _note(self, r, method, path):
        if r.status_code >= 500:
            PROBLEMS.append(f"{self.label}: {method} {path} answered {r.status_code}")
        return r

    def get(self, path, **kw):
        return self._note(self.client.get(path, base_url=self.base, environ_base={"REMOTE_ADDR": self.addr}, **kw), "GET", path)

    def text(self, path):
        return self.get(path).get_data(as_text=True)

    def post(self, path, data=None, page="/admin/password"):
        data = dict(data or {})
        data["_csrf_token"] = csrf_from(self.text(page))
        return self._note(self.client.post(path, data=data, base_url=self.base, environ_base={"REMOTE_ADDR": self.addr}), "POST", path)

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


page = console.get("/platform/schools/alpha", base_url=PL).get_data(as_text=True)
r = console.post("/platform/schools/alpha/enter", data={"_csrf_token": csrf_from(page)}, base_url=PL)
op = Person(ALPHA, "School Admin")
op.get(r.headers["Location"][len(ALPHA):])
op.get("/admin/workspace/school")

PASSWORD = "Fixture-password-9"
HASH = generate_password_hash(PASSWORD, method="pbkdf2:sha256:1000")
cls = {name: one("SELECT id FROM school_classes WHERE name = :n", n=name) for name in ("JSS 1", "SSS 1")}
OTHER = one("SELECT id FROM school_classes WHERE active = 1 AND name NOT LIKE 'JSS%' AND name NOT LIKE 'SS%' ORDER BY level_order LIMIT 1")
SESSION = one("SELECT id FROM academic_sessions WHERE is_current = 1")
role = {name: one("SELECT id FROM admin_types WHERE name = :n", n=name) for name in ("Class Teacher", "Subject Teacher")}


def new_student(first, class_name, department=""):
    op.post("/admin/school/students/new", {"first_name": first, "last_name": "Test", "gender": "Female",
                                           "class_id": str(cls[class_name]), "state_of_origin": "Lagos",
                                           "department": department}, page="/admin/school/students/new")
    return one("SELECT id FROM students WHERE first_name = :f", f=first)


def sub_of(student_id):
    return one("SELECT subdivision_id FROM student_enrolments WHERE student_id = :s AND session_id = :x AND active = 1",
               s=student_id, x=SESSION)


def subs(class_name):
    return {name: sid for sid, name in sql("SELECT id, name FROM class_subdivisions WHERE class_id = :c AND active = 1",
                                           c=cls[class_name])}


def count_in(sid):
    return one("SELECT COUNT(*) FROM student_enrolments WHERE subdivision_id = :s AND session_id = :x AND active = 1",
               s=sid, x=SESSION)


def save(class_name, data):
    return op.post(f"/admin/school/subdivisions/{cls[class_name]}", data, page="/admin/school/subdivisions")


# ================================================================ A. settings
before = [new_student(f"Early{n}", "JSS 1") for n in "abcde"]
page = op.text("/admin/school/subdivisions")
check("the Class subdivisions page opens with in-page examples and the JSS and SSS classes",
      "JSS 2 Gold" in page and "SS 1C (Art)" in page and "JSS 1" in page and "SSS 1" in page)
check("Settings lists Class subdivisions", "Class subdivisions" in op.text("/admin/settings"))
r = save("JSS 1", {"new_name": ["JSS 1A", ""]})
check("one subdivision alone is refused", r.status_code == 400 and "at least two" in r.get_data(as_text=True))
r = save("JSS 1", {"new_name": ["JSS 1A", "jss 1a"]})
check("two subdivisions with the same name are refused", r.status_code == 400 and "share a name" in r.get_data(as_text=True))
if OTHER:
    r = op.post(f"/admin/school/subdivisions/{OTHER}", {"new_name": ["X", "Y"]}, page="/admin/school/subdivisions")
    check("a class that is not JSS or SSS cannot be split",
          "only an active jss or sss class" in op.said() and not one("SELECT COUNT(*) FROM class_subdivisions WHERE class_id = :c", c=OTHER))
r = save("JSS 1", {"new_name": ["JSS 1A", "JSS 1B"]})
J = subs("JSS 1")
check("JSS 1 is split into JSS 1A and JSS 1B", r.status_code == 302 and set(J) == {"JSS 1A", "JSS 1B"})
check("the five students already in JSS 1 are spread in turn (3 and 2)",
      all(sub_of(s) for s in before) and sorted([count_in(J["JSS 1A"]), count_in(J["JSS 1B"])]) == [2, 3], "5 students placed" in op.said())

# ================================================================ B. placing
later = [new_student(f"Later{n}", "JSS 1") for n in "abc"]
check("new students from the staff portal fill the smaller subdivision first (4 and 4)",
      count_in(J["JSS 1A"]) == 4 and count_in(J["JSS 1B"]) == 4 and all(sub_of(s) for s in later))
with A.app.app_context():
    from control_plane.context import tenant_context
    from datetime import datetime, timezone
    with tenant_context(INFO):
        from models import Student, StudentEnrolment, db
        now = datetime.now(timezone.utc).isoformat()
        st = Student(admission_no="ADM-SUB-1", first_name="Admitted", last_name="Test", created_at=now, active=1,
                     school_id=one("SELECT id FROM schools LIMIT 1"))
        db.session.add(st)
        db.session.flush()
        db.session.add(StudentEnrolment(student_id=st.id, class_id=cls["JSS 1"], session_id=SESSION, enrolled_at=now, active=1))
        db.session.commit()
        ADMITTED = st.id
        db.session.remove()
check("a student admitted (enrolled the way the entrance admit does) is placed too", sub_of(ADMITTED) in J.values())

r = save("SSS 1", {"new_name": ["SS 1A", "SS 1B", "SS 1C"], "new_department": ["Science", "Science", "Art"]})
S = subs("SSS 1")
check("SSS 1 is split with departments", r.status_code == 302 and one(
    "SELECT department FROM class_subdivisions WHERE id = :i", i=S["SS 1C"]) == "Art")
sci = [new_student(f"Sci{n}", "SSS 1", "Science") for n in "ab"]
art = new_student("Artone", "SSS 1", "Art")
check("an SSS Science student goes to a Science subdivision, in turn; an Art student to the Art one",
      {sub_of(s) for s in sci} == {S["SS 1A"], S["SS 1B"]} and sub_of(art) == S["SS 1C"])

# ================================================================ C. locking
op.post("/admin/school/subdivisions/lock", {"lock": "1"}, page="/admin/school/subdivisions")
check("the School Admin locks the set-up", "locked" in op.said() and "Unlock to make changes" in op.text("/admin/school/subdivisions"))
save("JSS 1", {"sub_id": [str(J["JSS 1A"]), str(J["JSS 1B"])], "sub_name": ["Renamed", "JSS 1B"]})
check("a locked set-up cannot be changed", "locked" in op.said() and set(subs("JSS 1")) == {"JSS 1A", "JSS 1B"})


# ================================================================ D. class teachers per subdivision
def new_staff(username, roles, data):
    payload = {"username": username, "display_name": username.replace(".", " ").title(),
               "admin_type_ids": [str(role[r]) for r in roles], "duty_class": [""], "duty_subject": [""], "duty_department": [""], **data}
    op.post("/admin/administration/admins/new", payload, page="/admin/administration/admins/new")
    aid = one("SELECT id FROM admins WHERE username = :u", u=username)
    sql("UPDATE admins SET password_hash = :h, password_must_change = 0 WHERE id = :i", h=HASH, i=aid)
    person = Person(ALPHA, username)
    person.post("/login", {"username": username, "password": PASSWORD}, page="/login")
    person.get("/admin/workspace/school")
    return person, aid


cta, CTA = new_staff("teacher.a", ["Class Teacher"], {"duty_class_teacher_subdivision": [str(J["JSS 1A"])]})
ctb, CTB = new_staff("teacher.b", ["Class Teacher"], {"duty_class_teacher_subdivision": [str(J["JSS 1B"])]})
cts, CTS = new_staff("teacher.s", ["Class Teacher"], {"duty_class_teacher_subdivision": [str(S["SS 1A"])]})
check("a class teacher is given a subdivision of a class",
      one("SELECT subdivision_id FROM teaching_duties WHERE admin_id = :a AND subject_id IS NULL", a=CTA) == J["JSS 1A"])
in_a = [sid for (sid,) in sql("SELECT student_id FROM student_enrolments WHERE subdivision_id = :s", s=J["JSS 1A"])]
in_b = [sid for (sid,) in sql("SELECT student_id FROM student_enrolments WHERE subdivision_id = :s", s=J["JSS 1B"])]
name = {sid: one("SELECT first_name FROM students WHERE id = :i", i=sid) for sid in in_a + in_b}
listing = cta.text("/admin/school/students?class=JSS+1")
check("the student list shows the JSS 1A teacher only JSS 1A's students",
      all(name[s] in listing for s in in_a) and not any(name[s] in listing for s in in_b))
check("a JSS 1B student's page is refused to the JSS 1A teacher, their own opens",
      name[in_b[0]] not in cta.text(f"/admin/school/students/{in_b[0]}") and name[in_a[0]] in cta.text(f"/admin/school/students/{in_a[0]}"))
register = cta.text(f"/admin/school/attendance?class_id={cls['JSS 1']}&session_id={SESSION}")
check("the register lists the subdivision's students only",
      all(name[s] in register for s in in_a) and not any(name[s] in register for s in in_b))
cards = cta.get(f"/admin/school/report-cards/students?class_id={cls['JSS 1']}&session_id={SESSION}&term=First+Term").get_json() or {}
check("report cards list the subdivision's students only", cards.get("total") == len(in_a), str(cards.get("total")))

# ================================================================ E. transfers
check("a subdivision's class teacher has Class transfers in the menu", "Class transfers" in cta.text("/admin/school/transfers"))
MOVER = in_a[0]
cta.post("/admin/school/transfers/request", {"student_id": str(MOVER), "to_subdivision_id": str(J["JSS 1B"]), "reason": "Parents asked"},
         page="/admin/school/transfers")
T1 = one("SELECT id FROM class_transfers WHERE student_id = :s AND status = 'pending'", s=MOVER)
check("the JSS 1A teacher asks to move a student to JSS 1B, and nothing moves yet",
      T1 and sub_of(MOVER) == J["JSS 1A"] and name[MOVER] in cta.text("/admin/school/students?class=JSS+1"))
check("the JSS 1B teacher is told", one("SELECT COUNT(*) FROM admin_notifications WHERE admin_id = :a AND title LIKE 'Class transfer%'", a=CTB) == 1)
r = cta.post("/admin/school/transfers/request", {"student_id": str(MOVER), "to_subdivision_id": str(J["JSS 1B"])}, page="/admin/school/transfers")
check("a second transfer for the same student is refused", r.status_code == 400 and "already has a transfer waiting" in r.get_data(as_text=True))
cta.post(f"/admin/school/transfers/{T1}/approve", page="/admin/school/transfers")
check("the asker cannot approve their own transfer", one("SELECT status FROM class_transfers WHERE id = :t", t=T1) == "pending")
check("the JSS 1B teacher sees it waiting for them", name[MOVER] in ctb.text("/admin/school/transfers"))
ctb.post(f"/admin/school/transfers/{T1}/approve", page="/admin/school/transfers")
check("once the JSS 1B teacher approves, the student moves across",
      one("SELECT status FROM class_transfers WHERE id = :t", t=T1) == "approved" and sub_of(MOVER) == J["JSS 1B"]
      and name[MOVER] not in cta.text("/admin/school/students?class=JSS+1") and name[MOVER] in ctb.text("/admin/school/students?class=JSS+1"))
STAY = in_a[1]
cta.post("/admin/school/transfers/request", {"student_id": str(STAY), "to_subdivision_id": str(J["JSS 1B"])}, page="/admin/school/transfers")
T2 = one("SELECT id FROM class_transfers WHERE student_id = :s AND status = 'pending'", s=STAY)
ctb.post(f"/admin/school/transfers/{T2}/decline", {"note": "Full"}, page="/admin/school/transfers")
check("a declined transfer leaves the student where they are",
      one("SELECT status FROM class_transfers WHERE id = :t", t=T2) == "declined" and sub_of(STAY) == J["JSS 1A"])
cta.post("/admin/school/transfers/request", {"student_id": str(STAY), "to_subdivision_id": str(J["JSS 1B"])}, page="/admin/school/transfers")
T3 = one("SELECT id FROM class_transfers WHERE student_id = :s AND status = 'pending'", s=STAY)
ctb.post(f"/admin/school/transfers/{T3}/cancel", page="/admin/school/transfers")
still = one("SELECT status FROM class_transfers WHERE id = :t", t=T3) == "pending"
cta.post(f"/admin/school/transfers/{T3}/cancel", page="/admin/school/transfers")
check("only the asker cancels a transfer, and the student stays",
      still and one("SELECT status FROM class_transfers WHERE id = :t", t=T3) == "cancelled" and sub_of(STAY) == J["JSS 1A"])
SCI1 = next(s for s in sci if sub_of(s) == S["SS 1A"])
r = cts.post("/admin/school/transfers/request", {"student_id": str(SCI1), "to_subdivision_id": str(S["SS 1C"]), "to_department": "Science"},
             page="/admin/school/transfers")
check("a Science student cannot be moved into the Art subdivision", r.status_code == 400 and "is for Art students" in r.get_data(as_text=True))
cts.post("/admin/school/transfers/request", {"student_id": str(SCI1), "to_subdivision_id": str(S["SS 1C"]), "to_department": "Art"},
         page="/admin/school/transfers")
T4 = one("SELECT id FROM class_transfers WHERE student_id = :s AND status = 'pending'", s=SCI1)
check("a department change into a subdivision with no class teacher waits for the School Admin",
      T4 and name.get(SCI1) is None and "Sci" in op.text("/admin/school/transfers"))
op.post(f"/admin/school/transfers/{T4}/approve", page="/admin/school/transfers")
check("the School Admin approves: the student is now Art, in SS 1C",
      one("SELECT department FROM students WHERE id = :i", i=SCI1) == "Art" and sub_of(SCI1) == S["SS 1C"])
subj, _ = new_staff("teacher.subject", ["Subject Teacher"], {})
r = subj.get("/admin/school/transfers")
check("a member of staff with no JSS or SSS class is refused Class transfers",
      r.status_code in (302, 403) or "Class transfers are for" in r.get_data(as_text=True))

# ================================================================ F. removing subdivisions
op.post("/admin/school/subdivisions/lock", {"lock": "0"}, page="/admin/school/subdivisions")
cta.post("/admin/school/transfers/request", {"student_id": str(STAY), "to_subdivision_id": str(J["JSS 1B"])}, page="/admin/school/transfers")
T5 = one("SELECT id FROM class_transfers WHERE student_id = :s AND status = 'pending'", s=STAY)
b_students = [sid for (sid,) in sql("SELECT student_id FROM student_enrolments WHERE subdivision_id = :s", s=J["JSS 1B"])]
save("JSS 1", {"sub_id": [str(J["JSS 1A"]), str(J["JSS 1B"])], "sub_name": ["JSS 1A", "JSS 1B"], "sub_remove": [str(J["JSS 1B"])],
               "new_name": ["JSS 1C"]})
J2 = subs("JSS 1")
check("removing JSS 1B moves its students into the others", set(J2) == {"JSS 1A", "JSS 1C"}
      and all(sub_of(s) in J2.values() for s in b_students))
check("…ends its class teacher's duty and cancels the transfer into it",
      not one("SELECT COUNT(*) FROM teaching_duties WHERE subdivision_id = :s", s=J["JSS 1B"])
      and one("SELECT status FROM class_transfers WHERE id = :t", t=T5) == "cancelled")
save("JSS 1", {"sub_id": [str(J2["JSS 1A"]), str(J2["JSS 1C"])], "sub_name": ["JSS 1A", "JSS 1C"],
               "sub_remove": [str(J2["JSS 1A"]), str(J2["JSS 1C"])]})
check("removing them all undoes the split", not subs("JSS 1") and not one(
    "SELECT COUNT(*) FROM student_enrolments WHERE class_id = :c AND subdivision_id IS NOT NULL", c=cls["JSS 1"]))

check("no page answered with a server error", not PROBLEMS, "; ".join(PROBLEMS[:5]))
check("nothing raised behind the scenes", not errors.seen, "; ".join(repr(e) for e in errors.seen[:3]))
failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
