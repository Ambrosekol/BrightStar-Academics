"""Learning simulations (core/simulations.py), proven end to end on PostgreSQL. In plain words:

A. On by default: a Primary 2 pupil has Simulations in their menu and sees their class's simulations only; the
   page lets PhET in (its Content-Security-Policy) and embeds the simulation; one for another class is not found.
B. SSS departments: an Art student sees Art's and every department's, not Science's; a Science student sees
   Science's; a student whose department is not set sees them all; topic filter and search narrow the list.
C. Who may change it: a subject teacher cannot open or save the settings, and has no entry in Settings; a head
   teacher and the School Admin can.
D. Switches: a class switched off loses the menu entry and the pages, while other classes keep them; switched off
   for the school, nobody has them.
E. The list: a hidden simulation goes; one moved to other classes, or other SSS departments, follows; putting
   everything back as suggested undoes it all.
Run:  python tests/verification/write_paths_simulations.py
"""
import atexit
import json
import os
import re
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="beacon_simulations_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('simulations')
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
from control_plane import provisioning as pv  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import engine_for  # noqa: E402

results, PROBLEMS = [], []
PL = "http://platform.test"
UA = "http://alpha.portal.test"
PAGE = "/admin/school/simulations"


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


def token_in(body):
    found = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', body)
    return found.group(1) if found else ""


class Person:
    count = [0]

    def __init__(self, label):
        self.c, self.label = A.app.test_client(), label
        Person.count[0] += 1
        self.addr = f"10.97.{Person.count[0]}.1"

    def get(self, path):
        r = self.c.get(path, base_url=UA, environ_base={"REMOTE_ADDR": self.addr})
        if r.status_code >= 500:
            PROBLEMS.append(f"{self.label}: GET {path} {r.status_code}")
        return r

    def text(self, path):
        return self.get(path).get_data(as_text=True)

    def post(self, path, data=None, page="/admin/password"):
        data = dict(data or {})
        data["_csrf_token"] = token_in(self.text(page))
        r = self.c.post(path, data=data, base_url=UA, environ_base={"REMOTE_ADDR": self.addr})
        if r.status_code >= 500:
            PROBLEMS.append(f"{self.label}: POST {path} {r.status_code}")
        return r


pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
console = A.app.test_client()
console.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                      "_csrf_token": token_in(console.get("/platform/login", base_url=PL).get_data(as_text=True))}, base_url=PL)
console.post("/platform/schools/new", data={"_csrf_token": token_in(console.get("/platform/schools/new", base_url=PL).get_data(as_text=True)),
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


def stored():
    raw = one("SELECT setting_value FROM school_settings WHERE setting_key = 'learning_simulations'")
    return json.loads(raw) if raw else {}


page = console.get("/platform/schools/alpha", base_url=PL).get_data(as_text=True)
r = console.post("/platform/schools/alpha/enter", data={"_csrf_token": token_in(page)}, base_url=PL)
op = Person("School Admin")
op.get(r.headers["Location"][len(UA):])
op.get("/admin/workspace/school")
PASSWORD = "Fixture-password-9"
HASH = generate_password_hash(PASSWORD, method="pbkdf2:sha256:1000")
CLASS = {name: one("SELECT id FROM school_classes WHERE name = :n", n=name) for name in ("Primary 2", "JSS 1", "SSS 2")}
SESSION = one("SELECT id FROM academic_sessions WHERE is_current = 1")
NOW = "2026-10-17T08:00:00+00:00"
op.post("/admin/school/subjects/new", {"name": "Mathematics", "code": "MTH", "class_ids": [CLASS["JSS 1"]]},
        page="/admin/school/subjects/new")
MATHS = one("SELECT id FROM school_subjects WHERE name = 'Mathematics'")


def new_staff(username, role_name, extra=None):
    role = one("SELECT id FROM admin_types WHERE name = :n", n=role_name)
    payload = {"username": username, "display_name": username.replace(".", " ").title(), "admin_type_ids": [str(role)],
               "duty_class": [""], "duty_subject": [""], "duty_department": [""], **(extra or {})}
    op.post("/admin/administration/admins/new", payload, page="/admin/administration/admins/new")
    aid = one("SELECT id FROM admins WHERE username = :u", u=username)
    sql("UPDATE admins SET password_hash = :h, password_must_change = 0 WHERE id = :i", h=HASH, i=aid)
    person = Person(username)
    person.post("/login", {"username": username, "password": PASSWORD}, page="/login")
    person.get("/admin/workspace/school")
    return person


def new_student(username, class_name, department=None):
    school_id = one("SELECT id FROM schools LIMIT 1")
    sid = one("INSERT INTO students (admission_no, student_number, first_name, last_name, created_at, active, school_id, gender, "
              "account_active, password_must_change, login_username, login_password_hash, department) "
              "VALUES (:u, :u, :f, 'Test', :n, 1, :s, 'Female', 1, 0, :u, :h, :d) RETURNING id",
              u=username, f=username.split(".")[0].title(), n=NOW, s=school_id, h=HASH, d=department)
    sql("INSERT INTO student_enrolments (student_id, class_id, session_id, enrolled_at, active) VALUES (:a, :c, :s, :n, 1)",
        a=sid, c=CLASS[class_name], s=SESSION, n=NOW)
    person = Person(username)
    person.post("/login", {"username": username, "password": PASSWORD}, page="/login")
    return person


head = new_staff("head.teacher", "Head Teacher")
teacher = new_staff("subject.teacher", "Subject Teacher", {"duty_class": [str(CLASS["JSS 1"])], "duty_subject": [str(MATHS)]})
p2 = new_student("tolu.p2", "Primary 2")
jss1 = new_student("emeka.jss1", "JSS 1")
art = new_student("amaka.art", "SSS 2", "Art")
science = new_student("bayo.science", "SSS 2", "Science")
unset = new_student("chidi.unset", "SSS 2")


def listed(person, query=""):
    body = person.text("/student/simulations" + query)
    return set(re.findall(r'href="/student/simulations/([a-z0-9-]+)"', body)), body


def in_menu(person):
    return 'href="/student/simulations"' in person.text("/student/dashboard")


# ================================================================ A. on by default
mine, body = listed(p2)
r = p2.get("/student/simulations")
check("A. a Primary 2 pupil has Simulations in their menu", in_menu(p2))
check("A. …and sees their class's simulations only", "make-a-ten" in mine and "projectile-motion" not in mine
      and "calculus-grapher" not in mine, str(sorted(mine))[:200])
check("A. the list's policy lets PhET's pictures in", "img-src 'self' data: https://phet.colorado.edu" in r.headers.get("Content-Security-Policy", ""))
r = p2.get("/student/simulations/make-a-ten")
sim = r.get_data(as_text=True)
check("A. a simulation opens, embedded from PhET, with the credit", r.status_code == 200
      and 'src="https://phet.colorado.edu/sims/html/make-a-ten/latest/make-a-ten_en.html"' in sim and "CC BY 4.0" in sim)
policy = r.headers.get("Content-Security-Policy", "")
check("A. …its policy lets PhET's frame in and keeps the nonce", "frame-src https://phet.colorado.edu" in policy
      and "'nonce-" in policy and "unsafe-inline" not in policy.split("script-src", 1)[1].split(";", 1)[0], policy)
check("A. one for another class is not found", p2.get("/student/simulations/projectile-motion").status_code == 404)
check("A. nor is one that does not exist", p2.get("/student/simulations/not-a-sim").status_code == 404)
check("A. nothing is stored until someone saves", stored() == {})

# ================================================================ B. SSS departments
art_sims, _ = listed(art)
science_sims, _ = listed(science)
unset_sims, _ = listed(unset)
check("B. an SSS Art student sees Art's and every department's, not Science's",
      "neuron" in art_sims and "trig-tour" in art_sims and "molarity" not in art_sims)
check("B. a Science student sees Science's", "molarity" in science_sims and "neuron" in science_sims and "trig-tour" in science_sims)
check("B. a student whose department is not set sees them all", unset_sims == art_sims | science_sims)
check("B. an Art student cannot open a Science one", art.get("/student/simulations/molarity").status_code == 404)
maths_only, _ = listed(science, "?topic=maths")
check("B. the topic filter shows only that topic", maths_only and "molarity" not in maths_only and "trig-tour" in maths_only)
searched, _ = listed(science, "?q=Graphing")
check("B. search narrows the list", searched == {"graphing-quadratics", "graphing-lines", "graphing-slope-intercept"} & science_sims
      and searched, str(searched))

# ================================================================ C. who may change it
check("C. a subject teacher cannot open the settings", teacher.get(PAGE).status_code in (302, 403))
r = teacher.post(PAGE, {"action": "switches"})
check("C. …nor switch it off", r.status_code in (302, 403) and stored() == {})
check("C. …and has no Learning simulations in Settings", f'href="{PAGE}"' not in teacher.text("/admin/settings"))
check("C. a head teacher has it in Settings and can open it", f'href="{PAGE}"' in head.text("/admin/settings")
      and head.get(PAGE).status_code == 200)
check("C. the School Admin can open it", op.get(PAGE).status_code == 200)

# ================================================================ D. switches
everyone_but_p2 = [str(CLASS["JSS 1"]), str(CLASS["SSS 2"])] + [str(c) for (c,) in sql(
    "SELECT id FROM school_classes WHERE active = 1 AND level_order BETWEEN 1 AND 12 AND name NOT IN ('Primary 2', 'JSS 1', 'SSS 2')")]
head.post(PAGE, {"action": "switches", "enabled": "1", "class_on": everyone_but_p2})
check("D. the head teacher switches Primary 2 off", stored().get("classes_off") == [CLASS["Primary 2"]], str(stored()))
check("D. Primary 2 loses the menu entry and the pages", not in_menu(p2) and not listed(p2)[0]
      and "not available for your class" in p2.text("/student/simulations")
      and p2.get("/student/simulations/make-a-ten").status_code == 404)
check("D. JSS 1 keeps them", in_menu(jss1) and listed(jss1)[0])
op.post(PAGE, {"action": "switches", "class_on": everyone_but_p2})
check("D. switched off for the school, nobody has them", stored().get("enabled") is False and not in_menu(jss1)
      and not listed(jss1)[0] and not listed(science)[0])
op.post(PAGE, {"action": "switches", "enabled": "1", "class_on": everyone_but_p2 + [str(CLASS["Primary 2"])]})
check("D. switched back on, every class has them again", in_menu(p2) and in_menu(jss1) and stored().get("classes_off") == [])

# ================================================================ E. the list
head.post(PAGE + "?topic=maths", {"action": "catalogue", "slug": ["make-a-ten", "area-builder", "trig-tour"],
                                  "lo_make-a-ten": "1", "hi_make-a-ten": "2",
                                  "show_area-builder": "1", "lo_area-builder": "2", "hi_area-builder": "2",
                                  "show_trig-tour": "1", "lo_trig-tour": "11", "hi_trig-tour": "12",
                                  "has_dept_trig-tour": "1", "dept_trig-tour": ["Science"]})
mine, _ = listed(p2)
check("E. a hidden simulation goes", "make-a-ten" not in mine and stored().get("hidden") == ["make-a-ten"])
check("E. one moved to Primary 2 follows", "area-builder" in mine and stored().get("ranges") == {"area-builder": [2, 2]})
check("E. one moved to the Science department only leaves Art", "trig-tour" not in listed(art)[0]
      and "trig-tour" in listed(science)[0])
check("E. the settings page shows the changes", "3 changed" in head.text(PAGE + "?topic=maths"))
head.post(PAGE, {"action": "reset"})
mine, _ = listed(p2)
check("E. putting everything back as suggested undoes it all", "make-a-ten" in mine and "area-builder" not in mine
      and "trig-tour" in listed(art)[0] and not stored().get("hidden") and not stored().get("ranges")
      and not stored().get("departments"))
check("E. every save is in the activity log", (one("SELECT COUNT(*) FROM audit_logs WHERE action = 'learning_simulations_saved'") or 0) >= 4)

check("no page answered with a server error", not PROBLEMS, "; ".join(PROBLEMS[:5]))
failed = [name for name, ok, _ in results if not ok]
print()
print(f"{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", "; ".join(failed))
    raise SystemExit(1)
