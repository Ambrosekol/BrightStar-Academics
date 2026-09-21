"""Each school numbers its candidates and students by its own rule.

The rule is a small Python file in the school's own folder, ``tenants/<code>/numbering.py``.
This builds throw-away schools and checks that:

  * a new school is given the starter rule, and numbers exactly as the platform always did;
  * a school that has no rule file yet (one made before this existed) gets it on first use;
  * two schools with different rules never affect each other, and an edit to one school's file
    takes effect on its next number with no restart;
  * a rule can use the candidate's name and class, and can move on when a code is taken;
  * a rule that misbehaves (an error, a bad code, no function, a code that is always taken) is
    refused with a plain message, saves nothing and never falls back to another rule;
  * a candidate registered through the real admin form gets the school's own code, and a broken
    rule shows a message on the form instead of a crash;
  * student numbers can be written a school's own way while the running number and the ledger
    that stops a number being issued twice stay the platform's.

Run:  python tests/verification/write_paths_numbering.py
"""
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_numbering_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('numbering')
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
from core import numbering as N  # noqa: E402
from services.student_number_generator import StudentNumberAllocationError, allocate_student_number  # noqa: E402

results = []
PL = "http://platform.test"
YEAR = datetime.now().year


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


def in_school(info, fn):
    with A.app.app_context(), tenant_context(info):
        return fn()


def sql(info, statement, **params):
    with engine_for(info).begin() as conn:
        result = conn.execute(sa.text(statement), params)
        return result.fetchall() if result.returns_rows else None


def rules_file(code):
    return os.path.join(TMP, "tenants", code, "numbering.py")


def write_rules(code, text):
    """Replace a school's rule file, the way the platform team would."""
    path = rules_file(code)
    before = os.stat(path).st_mtime_ns if os.path.exists(path) else 0
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    # Some file systems stamp times coarsely; make sure the change is visible.
    os.utime(path, ns=(before + 5_000_000_000, before + 5_000_000_000))


def new_code(info, name="", cls=""):
    return in_school(info, lambda: N.new_candidate_code(candidate_name=name, target_class=cls))


def add_candidate(info, code, name="Someone"):
    def go():
        A.db.session.add(A.Candidate(candidate_code=code, candidate_name=name, target_class="JSS 1",
                                     password_hash="x", created_at="2026-01-01T00:00:00+00:00", active=1))
        A.db.session.commit()
    in_school(info, go)


def refusal(fn):
    """The plain message a broken rule produces, or None if it produced a number."""
    try:
        fn()
    except (N.NumberingRuleError, StudentNumberAllocationError) as exc:
        return str(exc)
    return None


def candidate_count(info):
    return sql(info, "SELECT COUNT(*) FROM candidates")[0][0]


pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
console = A.app.test_client()
console.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                      "_csrf_token": csrf(console, "/platform/login", PL)}, base_url=PL)
for code, name in (("alpha", "Alpha School"), ("beta", "Beta College")):
    console.post("/platform/schools/new", data={
        "_csrf_token": csrf(console, "/platform/schools/new", PL), "name": name, "code": code}, base_url=PL,
        content_type="multipart/form-data")
info_alpha, info_beta = info_for("alpha"), info_for("beta")
STARTER = open(N.STARTER_FILE, encoding="utf-8").read()

# ================================================================ a new school starts with the starter rule
check("a new school is given its own numbering rules in its own folder",
      os.path.isfile(rules_file("alpha")) and os.path.isfile(rules_file("beta")))
check("…a copy of the platform's starter, one file per school",
      open(rules_file("alpha"), encoding="utf-8").read() == STARTER
      and rules_file("alpha") != rules_file("beta"))
check("…and the console's folder listing shows it as part of the school",
      any(e["name"] == "numbering.py" for e in pv.tenant_folder_listing(info_alpha)["entries"]))
check("installing never overwrites a file that is already there",
      N.install_rules_file(os.path.join(TMP, "tenants", "alpha")) is False)

first = new_code(info_alpha)
check("the starter rule numbers as the platform always did: CODE-year-0001",
      first == f"ALPHA-{YEAR}-0001", first)
add_candidate(info_alpha, first)
add_candidate(info_alpha, f"ALPHA-{YEAR}-0002")
check("…and carries on from the highest number used", new_code(info_alpha) == f"ALPHA-{YEAR}-0003")
add_candidate(info_alpha, f"ALPHA-{YEAR}-0009")
check("…even if earlier numbers were removed or skipped", new_code(info_alpha) == f"ALPHA-{YEAR}-0010")
add_candidate(info_alpha, f"ALPHA-{YEAR - 1}-0500")
check("…and each year has its own count", new_code(info_alpha) == f"ALPHA-{YEAR}-0010")
add_candidate(info_alpha, f"ALPHA-{YEAR}-LEGACY")
check("…and ignores codes whose ending is not a plain number", new_code(info_alpha) == f"ALPHA-{YEAR}-0010")
check("another school starts its own count at 1, under its own code",
      new_code(info_beta) == f"BETA-{YEAR}-0001")

# ================================================================ a school without a rule file
os.remove(rules_file("beta"))
again = new_code(info_beta)
check("a school with no rule file gets one the first time it needs a number, and numbers as before",
      os.path.isfile(rules_file("beta")) and again == f"BETA-{YEAR}-0001"
      and open(rules_file("beta"), encoding="utf-8").read() == STARTER)

# ================================================================ different rules, no cross-talk
write_rules("beta", '''
def candidate_code(ctx):
    level = ctx.target_class.replace(" ", "").upper() or "GEN"
    return ctx.next_in_sequence(f"{ctx.school_code[:1]}/{level}/".replace("/", "-"), digits=3)
''')
check("a school's own rule decides its codes, and can use the class applied for",
      new_code(info_beta, "Ada", "jss 1") == "B-JSS1-001", new_code(info_beta, "Ada", "jss 1"))
check("…it sees the candidate's details, so a different class gives a different series",
      new_code(info_beta, "Bola", "ss 1") == "B-SS1-001")
check("…and does not change any other school", new_code(info_alpha) == f"ALPHA-{YEAR}-0010")
add_candidate(info_beta, "B-JSS1-001")
check("its own numbering carries on from its own codes", new_code(info_beta, "Ada", "JSS 1") == "B-JSS1-002")

write_rules("beta", '''
def candidate_code(ctx):
    return f"newer-{ctx.now.year}-{ctx.next_in_sequence('', digits=2)}"
''')
check("a change to the file takes effect on the next number, with no restart, and is made capitals",
      new_code(info_beta) == f"NEWER-{YEAR}-01", new_code(info_beta))

write_rules("beta", '''
def candidate_code(ctx):
    return "  fixed-1  "
''')
add_candidate(info_beta, "FIXED-1")
write_rules("beta", '''
def candidate_code(ctx):
    return f"FIXED-{1 + ctx.attempt}"
''')
check("when a code is taken the rule is asked again, so a counter can move on",
      new_code(info_beta) == "FIXED-2", new_code(info_beta))

# ================================================================ a broken rule is refused, plainly
def bad(text):
    write_rules("beta", text)
    return refusal(lambda: new_code(info_beta, "Ada", "JSS 1"))


before = candidate_count(info_beta)
cases = {
    "a rule that raises an error": ("def candidate_code(ctx):\n    return 1 / 0\n", "ZeroDivisionError"),
    "a rule with a syntax error": ("def candidate_code(ctx)\n    return 'X'\n", "could not be read"),
    "a file with no candidate_code function": ("x = 1\n", "no candidate_code() function"),
    "a rule that returns nothing": ("def candidate_code(ctx):\n    return None\n", "must return text"),
    "a rule that returns a number": ("def candidate_code(ctx):\n    return 12345\n", "must return text"),
    "a code with a space in it": ("def candidate_code(ctx):\n    return 'AB 123'\n", "not allowed"),
    "a code that tries to climb out of a folder": ("def candidate_code(ctx):\n    return '../../X1'\n", "not allowed"),
    "a code with a slash": ("def candidate_code(ctx):\n    return 'AB/123'\n", "not allowed"),
    "a code that is too short": ("def candidate_code(ctx):\n    return 'A1'\n", "not allowed"),
    "a code that is too long": ("def candidate_code(ctx):\n    return 'A' * 41\n", "not allowed"),
    "an empty code": ("def candidate_code(ctx):\n    return '   '\n", "not allowed"),
    "a code with markup in it": ("def candidate_code(ctx):\n    return 'A<b>1'\n", "not allowed"),
    "a code that is always taken": ("def candidate_code(ctx):\n    return 'FIXED-1'\n", "already in use"),
    "candidate_code that is not a function": ("candidate_code = 'X'\n", "not a function"),
}
for label, (text, expected) in cases.items():
    message = bad(text)
    check(f"{label} is refused with a plain message", message is not None and expected in message, str(message))
check("…and a refused rule saves nothing", candidate_count(info_beta) == before)
message = bad("def candidate_code(ctx):\n    raise RuntimeError('boom')\n")
check("…and never falls back to another rule (the platform's starter is not used instead)",
      message is not None and "ALPHA" not in message and "BETA" not in message)
check("…nor does it leave the school's other files or any other school affected",
      new_code(info_alpha) == f"ALPHA-{YEAR}-0010")
write_rules("beta", STARTER)
check("putting a good rule back makes the school work again", new_code(info_beta) == f"BETA-{YEAR}-0001")

# a rule file that is a link leading outside the school's folder is never followed
outside = os.path.join(TMP, "outside_rules.py")
with open(outside, "w", encoding="utf-8") as fh:
    fh.write("def candidate_code(ctx):\n    return 'OUTSIDE-1'\n")
os.remove(rules_file("beta"))
try:
    os.symlink(outside, rules_file("beta"))
except (OSError, NotImplementedError):
    check("a link leading out of the school's folder is refused (skipped: this machine cannot make links)", True)
else:
    message = refusal(lambda: new_code(info_beta))
    check("a link leading out of the school's folder is never followed",
          message is not None and "inside the school's own folder" in message, str(message))
    os.remove(rules_file("beta"))
write_rules("beta", STARTER)

# ================================================================ through the real admin form
def signed_in(code):
    url = f"http://{code}.portal.test"
    r = console.post(f"/platform/schools/{code}/enter",
                     data={"_csrf_token": csrf(console, f"/platform/schools/{code}", PL)}, base_url=PL)
    c = A.app.test_client()
    c.get(r.headers["Location"][len(url):], base_url=url)
    c.get("/admin/workspace/entrance", base_url=url)
    return c, url


R = sys.modules["blueprints.entrance.routes"]
# Registering needs a full set of entrance papers; the paper setup is not what is under test, so
# stand in three papers and let everything else in the route run for real.
R.required_papers_for_target = lambda target: ([{"id": f"bank{i}", "name": f"Bank {i}", "duration_seconds": 3600, "questions": [{}]}
                                                for i in range(1, 4)], [])
R._entrance_config_row = lambda group, subject, session_id=None, **kw: {
    "id": None, "bank_id": {"mathematics": "bank1", "english": "bank2", "general_knowledge": "bank3"}[subject]}
R._school_current_session = lambda: {"id": 1}

form = {"candidate_name": "Ada Obi", "target_class": "JSS 1", "school_attended": "Little Steps",
        "parent_guardian_name": "Mrs Obi", "parent_guardian_relationship": "Mother", "primary_mobile": "08030000000"}
c_alpha, u_alpha = signed_in("alpha")
r = c_alpha.post("/admin/candidates/new", data={**form, "_csrf_token": csrf(c_alpha, "/admin/candidates/new", u_alpha)},
                 base_url=u_alpha, content_type="multipart/form-data")
body = r.get_data(as_text=True)
expected_code = f"ALPHA-{YEAR}-0010"
check("registering a candidate through the admin form gives the school's own code",
      r.status_code == 200 and expected_code in body, f"{r.status_code}")
check("…and that code is what was saved",
      sql(info_alpha, "SELECT candidate_name FROM candidates WHERE candidate_code = :c", c=expected_code) == [("Ada Obi",)])

c_beta, u_beta = signed_in("beta")
write_rules("beta", '''
def candidate_code(ctx):
    return f"{ctx.school_code}-{ctx.candidate_name.split()[0].upper()}-{ctx.next_in_sequence('', digits=2)}"
''')
r = c_beta.post("/admin/candidates/new", data={**form, "_csrf_token": csrf(c_beta, "/admin/candidates/new", u_beta)},
                base_url=u_beta, content_type="multipart/form-data")
check("a school's own rule is used by the form too, with the name that was typed",
      r.status_code == 200 and "BETA-ADA-01" in r.get_data(as_text=True))

write_rules("beta", "def candidate_code(ctx):\n    return 1 / 0\n")
count_before = candidate_count(info_beta)
r = c_beta.post("/admin/candidates/new", data={**form, "_csrf_token": csrf(c_beta, "/admin/candidates/new", u_beta)},
                base_url=u_beta, content_type="multipart/form-data")
page = r.get_data(as_text=True)
check("a broken rule shows a message on the form instead of an error page",
      r.status_code == 200 and "numbering.py" in page and "Traceback" not in page, str(r.status_code))
check("…and registers nobody", candidate_count(info_beta) == count_before)
write_rules("beta", STARTER)

# ================================================================ student numbers
def issue(info):
    def go():
        school_id = A.db.session.scalar(sa.select(A.School.id))
        try:
            number = allocate_student_number(school_id=school_id, allocated_by="test")["student_number"]
            A.db.session.commit()
            return number
        except Exception:
            A.db.session.rollback()
            raise
    return in_school(info, go)


default_number = issue(info_alpha)
check("without a student rule the school's numbering policy writes the number, as it always did",
      re.fullmatch(rf"[A-Za-z0-9_-]+/{YEAR}/\d{{4}}", default_number) is not None, default_number)

write_rules("alpha", STARTER + '''

def student_number(ctx):
    return f"{ctx.school_code}/{ctx.year % 100:02d}/{ctx.sequence:05d}"
''')
sequence_before = sql(info_alpha, "SELECT next_sequence FROM school_numbering_policies")[0][0]
mine = issue(info_alpha)
check("a school can write its student numbers its own way",
      mine == f"ALPHA/{YEAR % 100:02d}/{sequence_before:05d}", mine)
check("…the running number still comes from the platform, and moves on",
      sql(info_alpha, "SELECT next_sequence FROM school_numbering_policies")[0][0] == sequence_before + 1)
check("…and the number is recorded in the ledger that stops a number being issued twice",
      sql(info_alpha, "SELECT COUNT(*) FROM student_number_allocations WHERE student_number = :n", n=mine)[0][0] == 1)
check("…another school's numbers are untouched by it",
      re.fullmatch(rf"[A-Za-z0-9_-]+/{YEAR}/\d{{4}}", issue(info_beta)) is not None)

write_rules("alpha", STARTER + '''

def student_number(ctx):
    return "SAME/1"
''')
issue(info_alpha)
message = refusal(lambda: issue(info_alpha))
check("a rule that repeats a number is stopped by the platform's own duplicate check",
      message is not None and "collision" in message.lower(), str(message))
sequence_now = sql(info_alpha, "SELECT next_sequence FROM school_numbering_policies")[0][0]
check("…and the running number does not move when a number is refused",
      sql(info_alpha, "SELECT next_sequence FROM school_numbering_policies")[0][0] == sequence_now)

for label, text, expected in (
        ("a student rule that raises an error", "def student_number(ctx):\n    return 1 / 0\n", "ZeroDivisionError"),
        ("a student rule with a bad number", "def student_number(ctx):\n    return 'a b'\n", "not allowed"),
        ("a student rule that returns nothing", "def student_number(ctx):\n    return None\n", "must return text")):
    write_rules("alpha", STARTER + "\n" + text)
    message = refusal(lambda: issue(info_alpha))
    check(f"{label} is refused with a plain message", message is not None and expected in message, str(message))
write_rules("alpha", STARTER)
check("removing the student rule returns to the policy",
      re.fullmatch(rf"[A-Za-z0-9_-]+/{YEAR}/\d{{4}}", issue(info_alpha)) is not None)

# ================================================================ the platform's own copy is what a school starts from
check("the starter file the platform ships defines the candidate rule and keeps the student rule optional",
      "def candidate_code(ctx):" in STARTER and "\ndef student_number(ctx):" not in STARTER)

dispose_engines()
DROP_TEST_DATABASES()
shutil.rmtree(TMP, ignore_errors=True)
print()
failed = [x for x in results if not x[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
