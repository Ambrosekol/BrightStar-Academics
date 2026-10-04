"""Billing a whole class, end to end on PostgreSQL.

One school with two classes, seven students and three fee items. A school administrator signs in and:
* the class roster lists exactly the students enrolled in that class for the session;
* billing the class in batches charges every student once per fee item, and a student's batch result
  is "billed";
* billing the same class again charges nobody twice: every student is "already", and no row is added;
* a fee item added later is charged to the whole class, and students who already have the other items
  are not charged those again (only the new item);
* a student enrolled in another class, an inactive student, and a fee item not assigned to the class
  are all refused, and nothing is written for the refused student;
* a batch larger than the limit, a missing billing period, and a signed-out request are refused;
* the roster reports how many students already have each fee for the billing period;
* the whole class's billing leaves one audit entry per batch.

Run:  python tests/verification/write_paths_class_billing.py
"""
import atexit
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_classbill_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('classbill')
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
for name in ("BRIGHTSTARS_SMTP_HOST", "BRIGHTSTARS_SMTP_USER", "BRIGHTSTARS_SMTP_FROM", "BRIGHTSTARS_SMTP_PASSWORD",
             "BRIGHTSTARS_WHATSAPP_TOKEN", "BRIGHTSTARS_WHATSAPP_PHONE_NUMBER_ID", "BRIGHTSTARS_DELIVERY_KEY"):
    os.environ.pop(name, None)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
os.chdir(ROOT)

import sqlalchemy as sa  # noqa: E402
from werkzeug.datastructures import MultiDict  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

import app as A  # noqa: E402
A.app.config['BACKGROUND_INLINE'] = True

from control_plane import provisioning as pv  # noqa: E402
from control_plane.context import tenant_context  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from models import (  # noqa: E402
    AcademicSession, Admin, AuditLog, FinanceFeeAssessment, FinanceFeeItem, FinanceFeeItemClass,
    SchoolClass, Student, StudentEnrolment, db,
)

results = []
BASE = "http://alpha.test"
YEAR = datetime.now().year
ADMIN_USER, ADMIN_PASS = "alpha_admin", "class-billing-pass-1"


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)


class Person:
    """A browser with its own cookies, at the school's address."""
    count = [0]

    def __init__(self):
        self.client = A.app.test_client()
        Person.count[0] += 1
        self.addr = f"10.40.{Person.count[0]}.1"

    def _kw(self):
        return {"base_url": BASE, "environ_base": {"REMOTE_ADDR": self.addr}}

    def get(self, path):
        return self.client.get(path, **self._kw())

    def csrf(self, page):
        body = self.get(page).get_data(as_text=True)
        marker = 'name="_csrf_token" value="'
        start = body.index(marker) + len(marker)
        return body[start:body.index('"', start)]

    def post_form(self, path, pairs, page):
        """Submit form fields as a browser does, repeated names included."""
        data = [("_csrf_token", self.csrf(page))] + list(pairs)
        return self.client.post(path, data=MultiDict(data), **self._kw())


def count_assessments(**filters):
    with A.app.app_context(), tenant_context(INFO):
        query = sa.select(sa.func.count()).select_from(FinanceFeeAssessment).where(FinanceFeeAssessment.active == 1)
        for key, value in filters.items():
            query = query.where(getattr(FinanceFeeAssessment, key) == value)
        return A.db.session.scalar(query)


# ---------------------------------------------------------------- build the school
INFO, _password = pv.create_tenant("alpha", "Alpha Academy", ["alpha.test"],
                                   admin_username=ADMIN_USER, admin_display_name="Alpha Admin",
                                   starter_banks=False)
now = datetime.now().isoformat()

with A.app.app_context(), tenant_context(INFO):
    session_row = A.db.session.scalars(sa.select(AcademicSession).where(AcademicSession.name == f"{YEAR}/{YEAR + 1}")).first()
    if session_row is None:
        session_row = AcademicSession(name=f"{YEAR}/{YEAR + 1}", is_current=1, active=1, created_at=now)
        A.db.session.add(session_row)
        A.db.session.flush()
    session_id = session_row.id

    def class_named(name, level):
        # A new school already comes with the standard classes; use them when they are there.
        found = A.db.session.scalars(sa.select(SchoolClass).where(SchoolClass.name == name)).first()
        if found is None:
            found = SchoolClass(name=name, stage="Primary", level_order=level, active=1, created_at=now)
            A.db.session.add(found)
            A.db.session.flush()
        found.active = 1
        return found

    primary3 = class_named("Primary 3", 3)
    primary4 = class_named("Primary 4", 4)
    A.db.session.flush()

    students = []
    for index in range(7):
        in_p3 = index < 5
        student = Student(admission_no=f"ALP-{index + 1:03d}", first_name=f"Pupil{index + 1}",
                          last_name="Test", created_at=now, active=1)
        A.db.session.add(student)
        A.db.session.flush()
        A.db.session.add(StudentEnrolment(student_id=student.id,
                                          class_id=(primary3.id if in_p3 else primary4.id),
                                          session_id=session_id, enrolled_at=now, active=1))
        students.append(student)
    inactive = Student(admission_no="ALP-900", first_name="Gone", last_name="Test", created_at=now, active=0)
    A.db.session.add(inactive)
    A.db.session.flush()
    A.db.session.add(StudentEnrolment(student_id=inactive.id, class_id=primary3.id,
                                      session_id=session_id, enrolled_at=now, active=1))

    def fee(name, amount, classes):
        item = FinanceFeeItem(name=name, category="School Fees", stage="Primary", amount=amount,
                              applicability="Full Session", required=1, optional=0, active=1,
                              created_at=now, updated_at=now)
        A.db.session.add(item)
        A.db.session.flush()
        for klass in classes:
            A.db.session.add(FinanceFeeItemClass(fee_item_id=item.id, class_id=klass.id, active=1, created_at=now))
        return item

    tuition = fee("Tuition", 85000.0, [primary3, primary4])
    levy = fee("Development levy", 12500.0, [primary3])
    p4_only = fee("Primary 4 trip", 4000.0, [primary4])
    A.db.session.commit()
    ids = {"session": session_id, "p3": primary3.id, "p4": primary4.id,
           "student": [s.id for s in students], "inactive": inactive.id,
           "tuition": tuition.id, "levy": levy.id, "p4_only": p4_only.id}

    admin = A.db.session.scalars(sa.select(Admin).where(Admin.username == ADMIN_USER)).first()
    admin.password_hash = generate_password_hash(ADMIN_PASS)
    admin.password_must_change = 0
    A.db.session.commit()

# ---------------------------------------------------------------- sign in as the school's administrator
admin_person = Person()
signed_in = admin_person.post_form("/login", [("username", ADMIN_USER), ("password", ADMIN_PASS)], "/login")
check("the school's administrator signs in", signed_in.status_code in (302, 303), str(signed_in.status_code))
# The school workspace is chosen once per sign-in, as a person does on the workspace home page.
admin_person.get("/admin/workspace/school")

ROSTER = f"/admin/finance/class-roster.json?session_id={ids['session']}&class_id={ids['p3']}&term=Full%20Session"
roster_response = admin_person.get(ROSTER)
roster = roster_response.get_json() or {}
p3_students = [s["id"] for s in roster.get("students", [])]
check("the roster lists exactly the students enrolled in the class", roster_response.status_code == 200
      and p3_students == ids["student"][:5], f"{roster_response.status_code} {p3_students}")
check("the inactive student is not on the roster", ids["inactive"] not in p3_students)
check("the roster counts nobody as already charged before billing", roster.get("already") == {}, str(roster.get("already")))

BATCH = "/admin/finance/class-assessments/batch"
PAGE = "/admin/finance/fee-items"


def batch(student_ids, fee_ids, term="Full Session", class_id=None):
    pairs = [("session_id", ids["session"]), ("class_id", class_id or ids["p3"]), ("term", term)]
    pairs += [("fee_item_id", fid) for fid in fee_ids]
    pairs += [("student_id", sid) for sid in student_ids]
    return admin_person.post_form(BATCH, pairs, PAGE)


first = batch(p3_students[:3], [ids["tuition"], ids["levy"]])
first_json = first.get_json() or {}
check("the first batch is accepted", first.status_code == 200, f"{first.status_code} {first_json.get('error', '')}")
check("every student in the first batch is reported billed",
      [r["status"] for r in first_json.get("results", [])] == ["billed"] * 3,
      str([r.get("status") for r in first_json.get("results", [])]))
second = batch(p3_students[3:], [ids["tuition"], ids["levy"]])
second_json = second.get_json() or {}
check("the second batch bills the rest of the class",
      [r["status"] for r in second_json.get("results", [])] == ["billed"] * 2)
check("each student has exactly the two fee rows, so 10 in all for the class",
      count_assessments(fee_item_id=ids["tuition"]) == 5 and count_assessments(fee_item_id=ids["levy"]) == 5,
      f"tuition={count_assessments(fee_item_id=ids['tuition'])} levy={count_assessments(fee_item_id=ids['levy'])}")

again = batch(p3_students, [ids["tuition"], ids["levy"]])
again_json = again.get_json() or {}
check("billing the class again reports every student already billed",
      [r["status"] for r in again_json.get("results", [])] == ["already"] * 5,
      str([r.get("status") for r in again_json.get("results", [])]))
check("billing the class again writes no new rows",
      count_assessments(fee_item_id=ids["tuition"]) == 5 and count_assessments(fee_item_id=ids["levy"]) == 5)

roster_after = (admin_person.get(ROSTER).get_json() or {})
check("the roster now shows how many students already have each fee for the period",
      roster_after.get("already", {}).get(str(ids["tuition"])) == 5 and roster_after.get("already", {}).get(str(ids["levy"])) == 5,
      str(roster_after.get("already")))

# A new fee added for the same class: the students who have the old fees get only the new one.
with A.app.app_context(), tenant_context(INFO):
    sports = FinanceFeeItem(name="Sports kit", category="Uniforms", stage="Primary", amount=9500.5,
                            applicability="Annual", required=0, optional=1, active=1,
                            created_at=now, updated_at=now)
    A.db.session.add(sports)
    A.db.session.flush()
    A.db.session.add(FinanceFeeItemClass(fee_item_id=sports.id, class_id=ids["p3"], active=1, created_at=now))
    A.db.session.commit()
    ids["sports"] = sports.id

mixed = batch(p3_students, [ids["tuition"], ids["sports"]])
mixed_json = mixed.get_json() or {}
check("a new fee is charged to the whole class, and the old fee is not charged again",
      all(r.get("status") == "billed" and r.get("items") == 1 and r.get("skipped_items") == 1
          for r in mixed_json.get("results", [])),
      str(mixed_json.get("results", [])[:1]))
check("the new fee now exists for all five students",
      count_assessments(fee_item_id=ids["sports"]) == 5)

# Refusals: a student from another class, a fee not assigned to this class, a missing period, too many.
other_class = batch([ids["student"][5]], [ids["tuition"]])
other_json = other_class.get_json() or {}
check("a student in another class is refused and nothing is written for them",
      other_json.get("results", [{}])[0].get("status") == "failed"
      and count_assessments(student_id=ids["student"][5]) == 0,
      str(other_json))

wrong_fee = batch(p3_students[:1], [ids["p4_only"]])
check("a fee not assigned to the class is refused as a whole batch",
      wrong_fee.status_code == 400 and "not assigned" in (wrong_fee.get_json() or {}).get("error", ""),
      f"{wrong_fee.status_code}")

bad_term = admin_person.post_form(BATCH, [("session_id", ids["session"]), ("class_id", ids["p3"]),
                                          ("term", "Semester 9"), ("fee_item_id", ids["tuition"]),
                                          ("student_id", p3_students[0])], PAGE)
check("an unknown billing period is refused", bad_term.status_code == 400, str(bad_term.status_code))

too_many = admin_person.post_form(BATCH, [("session_id", ids["session"]), ("class_id", ids["p3"]),
                                          ("term", "Full Session"), ("fee_item_id", ids["tuition"])]
                                  + [("student_id", n) for n in range(1, 102)], PAGE)
check("a batch over the limit is refused", too_many.status_code == 400, str(too_many.status_code))

anonymous = Person()
signed_out = anonymous.client.get(ROSTER, **anonymous._kw())
check("a signed-out request is sent to sign in, not given the roster",
      signed_out.status_code in (302, 303, 401) and b"students" not in signed_out.data,
      str(signed_out.status_code))

with A.app.app_context(), tenant_context(INFO):
    audit_rows = A.db.session.scalar(sa.select(sa.func.count()).select_from(AuditLog)
                                     .where(AuditLog.action == 'finance_class_assessment_batch'))
check("each batch that billed anyone leaves one audit entry", audit_rows == 3, str(audit_rows))

# ---------------------------------------------------------------- report
failed = [name for name, ok, _ in results if not ok]
print()
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
if failed:
    print("FAILED:", "; ".join(failed))
    raise SystemExit(1)
