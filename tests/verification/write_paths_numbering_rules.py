"""Each school writes its candidate codes and student numbers by its own PATTERN.

A pattern such as ``{school}-{year}-{seq:4}`` is set by the platform team on the platform console
(when the school is created, and later on the school's page), kept in the school's own folder as
``tenants/<code>/numbering.json``, and READ (never run) whenever a number is made. This builds
throw-away schools on PostgreSQL and checks that:

  * every placeholder does what the reference says, padding and growth work, and a number that
    cannot fit is refused with a clear message;
  * the running number restarts by itself when a part that changes ({year}, {month}, {class}) does,
    honours the first-number setting, and is checked against the school's real candidates;
  * bad patterns (unknown placeholders, none of {seq}/{random}, two {seq}, unbalanced braces,
    forbidden characters, too long) are refused with the position of the problem;
  * the create-school form saves the default or custom rules, the school's page edits them, and each
    change is recorded with the old and the new pattern; only signed-in platform admins can;
  * the live preview reads the school's real next numbers, needs the CSRF token, and writes nothing;
  * two schools with different rules stay independent, and a candidate registered through the real
    admin form and a student created through the real form get the school's own numbers;
  * a numbering.json that is wrong is refused with a plain message and never silently replaced.

Run:  python tests/verification/write_paths_numbering_rules.py
"""
import glob
import html
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_numrules_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('numrules')
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
from control_plane import team  # noqa: E402
from control_plane.context import tenant_context  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import dispose_engines, engine_for  # noqa: E402
from core import numbering as N  # noqa: E402
from core import numbering_pattern as NP  # noqa: E402
from services.student_number_generator import StudentNumberAllocationError, allocate_student_number  # noqa: E402

results = []
PL = "http://platform.test"
NOW = datetime.now()
YEAR = NOW.year
YY = f"{YEAR % 100:02d}"
MONTH = f"{NOW.month:02d}"
MON = NP.MONTHS[NOW.month - 1]


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


def registry(statement, **params):
    with platform_session() as s:
        return s.execute(sa.text(statement), params).all()


def rules_dir(code):
    return os.path.join(TMP, "tenants", code)


def rules_file(code):
    return os.path.join(rules_dir(code), "numbering.json")


def read_json(code):
    with open(rules_file(code), encoding="utf-8") as fh:
        return json.load(fh)


def raw(code):
    with open(rules_file(code), "rb") as fh:
        return fh.read()


def new_code(info, name="", cls=""):
    return in_school(info, lambda: N.new_candidate_code(candidate_name=name, target_class=cls))


def add_candidate(info, code, name="Someone", cls="JSS 1"):
    def go():
        A.db.session.add(A.Candidate(candidate_code=code, candidate_name=name, target_class=cls,
                                     password_hash="x", created_at="2026-01-01T00:00:00+00:00", active=1))
        A.db.session.commit()
    in_school(info, go)


def refusal(fn):
    """The plain message a refused rule produces, or None if it produced a number."""
    try:
        fn()
    except (N.NumberingRuleError, StudentNumberAllocationError) as exc:
        return str(exc)
    return None


def audit_rows(action="tenant.numbering_update"):
    return registry("SELECT a.detail, a.actor_username, t.slug FROM platform_audit_log a "
                    "LEFT JOIN tenants t ON t.id = a.tenant_id WHERE a.action = :a ORDER BY a.id", a=action)


# ================================================================ 1. the language, on its own (no school needed)
F = NP.Facts("ABC", "Bright Future Academy", datetime(2026, 9, 5, 10, 30), target_class="JSS 1", prefix="ABC")


def make(pattern, kind=NP.CANDIDATE, seq=None, facts=F, pick=None):
    return NP.check_pattern(pattern, kind, facts).render(facts, seq, pick)


def why(pattern, kind=NP.CANDIDATE, facts=None, first=1):
    try:
        NP.check_pattern(pattern, kind, facts, first)
    except NP.PatternError as exc:
        return exc
    return None


for pattern, expected in (
        ("{school}-{seq}", "ABC-7"), ("{initials}-{seq}", "BFA-7"), ("X{year}-{seq}", "X2026-7"),
        ("X{yy}-{seq}", "X26-7"), ("X{month}-{seq}", "X09-7"), ("X{mon}-{seq}", "XSEP-7"),
        ("X{day}-{seq}", "X05-7"), ("X{class}-{seq}", "XJSS1-7"), ("ABC-{seq:4}", "ABC-0007"),
        ("ABC-{seq:4-5}", "ABC-0007"), ("ab-{seq:2}", "AB-07"), ("{school}-{yy}{month}{day}-{seq:3}", "ABC-260905-007")):
    check(f"placeholder pattern {pattern} makes {expected}", make(pattern, seq=7) == expected, make(pattern, seq=7))
check("{initials} skips little connecting words",
      NP.initials_of("Bright Stars of the Future Academy") == "BSFA" and NP.initials_of("St. Mary's College") == "SMC")
check("{class} is compacted and capitalised, and empty when unknown",
      NP.compact_class("jss 1") == "JSS1" and NP.compact_class("SS-2 Science") == "SS2SCIENCE" and NP.compact_class("") == "")
check("{class} unknown leaves the text either side of it",
      make("ABC-{class}-{seq:3}", seq=4, facts=NP.Facts("ABC", "x", F.now, target_class="")) == "ABC--004")
check("a code is always capitals, however the pattern is typed", make("abc.{school}_x-{seq:2}", seq=3) == "ABC.ABC_X-03")

digits = make("ABC-{random:6}")
check("{random:N} makes N random digits", re.fullmatch(r"ABC-\d{6}", digits) is not None, digits)
letters = "".join(make("ABC-{letters:5}")[4:] for _ in range(300))
check("{letters:N} makes capital letters and never I or O", re.fullmatch(r"[A-Z]+", letters) and not set("IO") & set(letters), letters[:40])
alnum = "".join(make("ABC-{alnum:5}")[4:] for _ in range(300))
check("{alnum:N} makes capital letters and digits and never I or O",
      re.fullmatch(r"[A-Z0-9]+", alnum) and not set("IO") & set(alnum) and any(c.isdigit() for c in alnum), alnum[:40])
check("random parts differ from code to code", len({make("ABC-{random:8}") for _ in range(20)}) > 15)
check("a scripted 'random' choice is honoured (so collisions can be tested)",
      make("ABC-{letters:2}", pick=lambda alphabet: "K") == "ABC-KK")

check("{prefix} is available in student numbers, and {seq} is the running number",
      make("{prefix}/{yy}/{seq:5}", NP.STUDENT, 12) == "ABC/26/00012")
check("|lower works in student numbers", make("{school|lower}/{mon|lower}/{seq:3}", NP.STUDENT, 4) == "abc/sep/004")
check("|upper is accepted", make("{school|upper}-{seq}", NP.STUDENT, 4) == "ABC-4")
check("student numbers may use a slash", make("{school}/{year}/{seq:4}", NP.STUDENT, 4) == "ABC/2026/0004")
check("a pattern's {year} is the year given for the student, not always today's",
      make("{year}-{seq}", NP.STUDENT, 4, facts=NP.Facts("ABC", "x", F.now, year=2024, prefix="ABC")) == "2024-4")

# padding and growth
grow = NP.check_pattern("ABC-{seq:4-5}", NP.CANDIDATE, F)
check("{seq:4-5} pads to 4 and grows to 5 digits", grow.render(F, 9) == "ABC-0009" and grow.render(F, 9999) == "ABC-9999"
      and grow.render(F, 10000) == "ABC-10000" and grow.render(F, 99999) == "ABC-99999")
message = refusal(lambda: grow.render(F, 100000))
check("…and beyond 5 digits is refused with a clear message that says how to widen it",
      message is not None and "{seq:4-5}" in message and "{seq:4-6}" in message and "platform team" in message, str(message))
fixed = NP.check_pattern("ABC-{seq:4}", NP.CANDIDATE, F)
message = refusal(lambda: fixed.render(F, 10000))
check("{seq:4} is exactly 4 digits: the 10,000th number is refused, not silently longer", message is not None and "9999" in message, str(message))
check("{seq} alone is not padded and has no upper limit", make("ABC-{seq}", seq=123456789) == "ABC-123456789")
stu = NP.check_pattern("{prefix}-{seq:2}", NP.STUDENT, F)
message = refusal(lambda: stu.render(F, 100))
check("a student's running number that does not fit is refused, saying so", message is not None and "100" in message and "{seq:2}" in message, str(message))

# the counter
CODES = ["ABC-2026-0007", "ABC-2025-0900", "abc-2026-0010", "ABC-2026-LEGACY", "XABC-2026-0500", "ABC-2026-0011-X", "ABC-2026-"]
yearly = NP.check_pattern("{school}-{year}-{seq:4}", NP.CANDIDATE, F)
check("the counter goes on from the biggest number with the same text in every other part",
      yearly.next_number(F, 1, CODES) == 11)
check("…and starts again when the year changes (a code of another year is not counted)",
      yearly.next_number(NP.Facts("ABC", "x", datetime(2027, 1, 2)), 1, CODES) == 1)
monthly = NP.check_pattern("{school}-{year}{month}-{seq:3}", NP.CANDIDATE, F)
check("{month} restarts the count each month",
      monthly.next_number(F, 1, ["ABC-202609-004", "ABC-202608-090"]) == 5
      and monthly.next_number(NP.Facts("ABC", "x", datetime(2026, 10, 1)), 1, ["ABC-202609-004"]) == 1)
by_class = NP.check_pattern("{school}-{class}-{seq:3}", NP.CANDIDATE, F)
check("{class} counts each class on its own",
      by_class.next_number(F, 1, ["ABC-JSS1-005", "ABC-SS1-020"]) == 6
      and by_class.next_number(NP.Facts("ABC", "x", F.now, target_class="SS 1"), 1, ["ABC-JSS1-005", "ABC-SS1-020"]) == 21
      and by_class.next_number(NP.Facts("ABC", "x", F.now, target_class="JSS 2"), 1, ["ABC-JSS1-005", "ABC-SS1-020"]) == 1)
never = NP.check_pattern("{school}-{seq:4}", NP.CANDIDATE, F)
check("with nothing that changes in the pattern the count never restarts",
      never.next_number(F, 1, ["ABC-0007"]) == 8 and never.next_number(NP.Facts("ABC", "x", datetime(2031, 3, 3)), 1, ["ABC-0007"]) == 8)
check("the first number is where the count starts when nothing matches, and never sends it backwards",
      never.next_number(F, 500, []) == 500 and never.next_number(F, 500, ["ABC-0007"]) == 500
      and never.next_number(F, 500, ["ABC-0600"]) == 601)
check("a first number of 0 is allowed", never.next_number(F, 0, []) == 0 and make("ABC-{seq:3}", seq=0) == "ABC-000")
mixed = NP.check_pattern("{school}-{seq:3}-{letters:2}", NP.CANDIDATE, F)
check("random parts are wildcards when counting", mixed.next_number(F, 1, ["ABC-004-KA", "ABC-009-XZ", "ABC-020-K1"]) == 10)
check("what comes before the first varying part is the fixed text to look for",
      yearly.leading_text(F) == "ABC-2026-" and NP.check_pattern("{seq:3}-{school}", NP.CANDIDATE, F).leading_text(F) == "")
check("text in a pattern is matched literally, never as an expression",
      NP.check_pattern("A.B-{seq:2}", NP.CANDIDATE, F).next_number(F, 1, ["AXB-05", "A.B-03"]) == 4)

# refusing bad patterns
BAD = (
    ("an unknown placeholder", "{schol}-{seq}", NP.CANDIDATE, "Unknown placeholder {schol} at position 1", 1),
    ("…with a suggestion", "{schol}-{seq}", NP.CANDIDATE, "Did you mean {school}?", 1),
    ("a placeholder in capitals", "{SCHOOL}-{seq}", NP.CANDIDATE, "lower case", 1),
    ("no {seq} or random part", "ABC-{year}", NP.CANDIDATE, "every code would be the same", None),
    ("plain text only", "ABC-XYZ", NP.CANDIDATE, "every code would be the same", None),
    ("two {seq}", "{seq}-{year}-{seq:3}", NP.CANDIDATE, "more than once", 14),
    ("a { that is never closed", "ABC-{seq", NP.CANDIDATE, "never closed", 5),
    ("a { closed by another {", "ABC-{school-{seq}", NP.CANDIDATE, "Unbalanced braces", 5),
    ("a } with no {", "ABC}-{seq}", NP.CANDIDATE, "no '{'", 4),
    ("empty braces", "ABC-{}-{seq}", NP.CANDIDATE, "Empty braces", 5),
    ("a space", "AB C-{seq}", NP.CANDIDATE, "A space at position 3", 3),
    ("a slash in a candidate code", "AB/C-{seq}", NP.CANDIDATE, "only allowed in student numbers", 3),
    ("an at sign", "AB@C-{seq}", NP.CANDIDATE, "position 3 is not allowed", 3),
    ("markup", "<b>-{seq}", NP.CANDIDATE, "not allowed", 1),
    ("an accented letter", "ÉCOLE-{seq}", NP.CANDIDATE, "not allowed", 1),
    ("a line break", "ABC\n-{seq}", NP.CANDIDATE, "not allowed", 4),
    ("a path climbing out of a folder", "../../{seq}", NP.STUDENT, "must start with a letter or a digit", None),
    ("a pattern that is too long", "A" * 76 + "{seq}", NP.CANDIDATE, "characters long", 81),
    ("{class} in a student number", "{class}/{seq}", NP.STUDENT, "only available in candidate codes", 1),
    ("{prefix} in a candidate code", "{prefix}-{seq}", NP.CANDIDATE, "only available in student numbers", 1),
    ("{seq:0}", "ABC-{seq:0}", NP.CANDIDATE, "from 1 to 12", 5),
    ("a backwards range", "ABC-{seq:6-4}", NP.CANDIDATE, "second number", 5),
    ("more than 12 digits", "ABC-{seq:13}", NP.CANDIDATE, "from 1 to 12", 5),
    ("{seq:x}", "ABC-{seq:x}", NP.CANDIDATE, "number of digits", 5),
    ("{random} with no count", "ABC-{random}", NP.CANDIDATE, "how many to make", 5),
    ("{random:0}", "ABC-{random:0}", NP.CANDIDATE, "from 1 to 12", 5),
    ("options on {year}", "ABC-{year:4}-{seq}", NP.CANDIDATE, "takes no options", 5),
    ("|lower in a candidate code", "{school|lower}-{seq}", NP.CANDIDATE, "always made capitals", 1),
    ("a case filter that does not exist", "{school|title}-{seq}", NP.STUDENT, "use |upper or |lower", 1),
    ("a case filter on a number", "{year|lower}-{seq}", NP.STUDENT, "is a number", 1),
    ("two case filters", "{school|lower|upper}-{seq}", NP.STUDENT, "more than one case filter", 1),
    ("{seq} next to random digits", "ABC-{seq}{random:2}", NP.CANDIDATE, "Put a letter", 10),
    ("a code that would start with a dash", "-{seq:3}", NP.CANDIDATE, "must start with a letter or a digit", None),
    ("a code that is too short", "{seq}", NP.CANDIDATE, "shorter than 3 characters", None),
    ("a pattern that fails when the class is not known", "{class}-{seq:3}", NP.CANDIDATE, "class is not known", None),
)
for label, pattern, kind, fragment, position in BAD:
    error = why(pattern, kind)
    if fragment is None:
        check(f"({label}) is a legal pattern", error is None, str(error))
        continue
    check(f"{label} is refused, saying '{fragment}'" + (f" at position {position}" if position else ""),
          error is not None and fragment in str(error) and error.position == position, f"{error} / {getattr(error, 'position', '-')}")
check("a pattern whose longest code passes 40 characters is refused for the real school",
      "up to" in str(why("{initials}{initials}{initials}{initials}{initials}-{seq:12}", NP.CANDIDATE, NP.Facts("LONGCODE", "Big Old Wide Academy Of Lower Learning Centre Inc"))))
check("a pattern the wrong type is refused", why(None) is not None and why(12) is not None)

for value, good in (("1", 1), (" 5 ", 5), ("0", 0), (7, 7)):
    check(f"first number {ascii(value)} is accepted", N.read_first_number(value) == good)
for value in ("", "abc", "-1", "1.5", "1e3", "٣", str(N.MAX_FIRST_NUMBER + 1), True, None, 2.5):
    try:
        N.read_first_number(value)
        check(f"first number {ascii(value)} is refused", False)
    except N.PatternError:
        check(f"first number {ascii(value)} is refused", True)
check("a first number too big for the {seq} is refused", "more digits" in str(why("ABC-{seq:4}", NP.CANDIDATE, None, 12345)))
check("the default candidate pattern reproduces the platform's own ABC-2026-0001",
      make(N.DEFAULT_CANDIDATE_PATTERN, seq=1) == f"ABC-2026-0001")

# the palette is the reference: everything on it is a real, accepted placeholder
for token, meaning, example, kinds in NP.PALETTE:
    kind = NP.CANDIDATE if NP.CANDIDATE in kinds else NP.STUDENT
    pattern = ("ABC-" + token) if token.startswith("{seq") else ("ABC-{seq:3}-" + token)
    ok = NP.check_pattern(pattern, kind, F) is not None
    check(f"palette entry {token} is accepted by the checker and has a meaning and an example", ok and bool(meaning) and bool(example))

# ================================================================ 2. rules kept in a folder (no school needed)
FOLDER = os.path.join(TMP, "scratch")
os.makedirs(FOLDER)
check("no file means the default rules", N.read_rules(FOLDER) == N.DEFAULT_RULES)
written = N.write_rules(FOLDER, N.Rules("{school}-{yy}-{seq:3}", "{prefix}/{seq:5}", 40))
check("rules are written to numbering.json and read back exactly", N.read_rules(FOLDER) == written
      and json.load(open(os.path.join(FOLDER, "numbering.json"), encoding="utf-8")) == {
          "version": 1, "candidate_pattern": "{school}-{yy}-{seq:3}", "student_pattern": "{prefix}/{seq:5}", "first_number": 40})
check("writing leaves no temporary file behind", not glob.glob(os.path.join(FOLDER, ".numbering-*")))
before = open(os.path.join(FOLDER, "numbering.json"), "rb").read()
with mock.patch.object(N.os, "replace", side_effect=OSError("disk full")):
    message = refusal(lambda: N.write_rules(FOLDER, N.Rules("{school}-{seq:6}", "", 1)))
check("a failed write is refused and leaves the old file whole and no debris",
      message is not None and open(os.path.join(FOLDER, "numbering.json"), "rb").read() == before
      and not glob.glob(os.path.join(FOLDER, ".numbering-*")), str(message))
message = refusal(lambda: N.write_rules(FOLDER, N.Rules("{nonsense}", "", 1)))
check("nothing that fails the checks can reach the file, not even through write_rules",
      message is not None and open(os.path.join(FOLDER, "numbering.json"), "rb").read() == before, str(message))

GOOD = {"version": 1, "candidate_pattern": "{school}-{seq:4}", "student_pattern": "", "first_number": 1}
CORRUPT = {
    "not JSON at all": "this is { not json",
    "an empty file": "",
    "JSON that is not an object": "[1, 2, 3]",
    "an entry missing": json.dumps({k: v for k, v in GOOD.items() if k != "first_number"}),
    "an entry too many": json.dumps({**GOOD, "run": "import os"}),
    "the wrong version": json.dumps({**GOOD, "version": 2}),
    "a version that is a boolean": json.dumps({**GOOD, "version": True}),
    "a pattern that is not text": json.dumps({**GOOD, "candidate_pattern": 5}),
    "an empty candidate pattern": json.dumps({**GOOD, "candidate_pattern": ""}),
    "a pattern with an unknown placeholder": json.dumps({**GOOD, "candidate_pattern": "{eval}-{seq}"}),
    "a pattern with two {seq}": json.dumps({**GOOD, "candidate_pattern": "{seq}-{seq}"}),
    "a student pattern with {class}": json.dumps({**GOOD, "student_pattern": "{class}-{seq}"}),
    "a first number that is text": json.dumps({**GOOD, "first_number": "one"}),
    "a first number that is negative": json.dumps({**GOOD, "first_number": -3}),
    "a first number that is a fraction": json.dumps({**GOOD, "first_number": 1.5}),
    "a file far too large": json.dumps({**GOOD, "candidate_pattern": "{school}-{seq:4}" + " " * 9000}),
    "bytes that are not text": None,
}
for label, content in CORRUPT.items():
    with open(os.path.join(FOLDER, "numbering.json"), "wb") as fh:
        fh.write(b"\xff\xfe\x00bad" if content is None else content.encode("utf-8"))
    message = refusal(lambda: N.read_rules(FOLDER))
    check(f"a numbering.json with {label} is refused with a plain message", message is not None and "numbering.json" in message
          and "Nothing has been changed" in message and "Traceback" not in message, str(message))
with open(os.path.join(FOLDER, "numbering.json"), "w", encoding="utf-8") as fh:
    json.dump(GOOD, fh)
check("…and a good one is read", N.read_rules(FOLDER) == N.Rules("{school}-{seq:4}", "", 1))
os.remove(os.path.join(FOLDER, "numbering.json"))
os.mkdir(os.path.join(FOLDER, "numbering.json"))
check("a folder where the file should be is refused", refusal(lambda: N.read_rules(FOLDER)) is not None)
os.rmdir(os.path.join(FOLDER, "numbering.json"))

# ================================================================ 3. the console: creating schools
pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
console = A.app.test_client()
console.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                      "_csrf_token": csrf(console, "/platform/login", PL)}, base_url=PL)


def create(code, name, **extra):
    return console.post("/platform/schools/new", data={
        "_csrf_token": csrf(console, "/platform/schools/new", PL), "name": name, "code": code, **extra},
        base_url=PL, content_type="multipart/form-data")


page = console.get("/platform/schools/new", base_url=PL).get_data(as_text=True)
check("the create-school form has a Numbering section, filled with the default",
      ">Numbering<" in page and 'id="numbering-candidate"' in page and 'value="{school}-{year}-{seq:4}"' in page
      and 'name="student_pattern"' in page and 'name="first_number"' in page and 'value="1"' in page)
check("…in a monospace, editor-like input with a live preview area, a reset button and the reference",
      'class="np-input"' in page and 'id="numbering-candidate-result"' in page and 'id="numbering-reset"' in page
      and "Reference: every placeholder" in page and "platform-numbering.js" in page)
missing_chips = [t for t, *_ in NP.PALETTE if f'data-token="{t}"' not in page]
check("…a clickable chip for every placeholder, each with a tooltip saying what it means and an example",
      not missing_chips and page.count('class="np-chip"') == len(NP.PALETTE) and page.count("Example: ") >= len(NP.PALETTE), str(missing_chips))
check("…the reference table lists every placeholder", all(f"<code>{t}</code>" in page for t, *_ in NP.PALETTE))
check("…the preview address and a CSRF token are on the page, and the page warns of nothing that is not yet true",
      'data-preview-url="/platform/numbering/preview"' in page and 'data-csrf="' in page and "already issued" not in page)
for asset in ("platform-numbering.js", "platform-numbering.css"):
    check(f"the editor's {asset} is served", console.get(f"/static/{asset}", base_url=PL).status_code == 200)

create("alpha", "Alpha School")
info_alpha = info_for("alpha")
check("a school created without touching the numbering section gets the default rules, written to its folder",
      os.path.isfile(rules_file("alpha")) and read_json("alpha") == {
          "version": 1, "candidate_pattern": "{school}-{year}-{seq:4}", "student_pattern": "", "first_number": 1})
check("…and that file is shown in the console's folder listing, as a file (no program)",
      any(e["name"] == "numbering.json" for e in pv.tenant_folder_listing(info_alpha)["entries"])
      and not os.path.exists(os.path.join(rules_dir("alpha"), "numbering.py")))
check("…and creating it with the default is not an audited change", audit_rows() == [])

create("beta", "Beta College", candidate_pattern="{initials}-{yy}{mon}-{class}-{seq:3}",
       student_pattern="{prefix}/{yy}/{seq:5}", first_number="100")
info_beta = info_for("beta")
check("a school created with custom rules has them in its numbering.json",
      read_json("beta") == {"version": 1, "candidate_pattern": "{initials}-{yy}{mon}-{class}-{seq:3}",
                            "student_pattern": "{prefix}/{yy}/{seq:5}", "first_number": 100})
rows = audit_rows()
check("…and the platform audit trail records them, against the school and the operator",
      len(rows) == 1 and rows[0][2] == "beta" and rows[0][1] == "ops" and "{initials}-{yy}{mon}-{class}-{seq:3}" in rows[0][0]
      and "first number: 100" in rows[0][0], str(rows))

before_tenants = registry("SELECT count(*) FROM tenants")[0][0]
before_dirs = sorted(os.listdir(os.path.join(TMP, "tenants")))
for label, fields, fragment in (
        ("an unknown placeholder", {"candidate_pattern": "{schol}-{seq}"}, "Unknown placeholder {schol} at position 1"),
        ("no running number", {"candidate_pattern": "{school}-{year}"}, "every code would be the same"),
        ("a bad student pattern", {"student_pattern": "{class}/{seq}"}, "Student number pattern"),
        ("an empty candidate pattern", {"candidate_pattern": ""}, "cannot be empty"),
        ("a first number that is not a number", {"first_number": "many"}, "whole number"),
        ("a pattern that is too long", {"candidate_pattern": "A" * 90 + "{seq}"}, "characters long")):
    r = create("gamma", "Gamma Academy", **fields)
    body = html.unescape(r.get_data(as_text=True))
    check(f"creating a school with {label} is refused and says why", "The school was not created" in body and fragment in body, body[body.find("errors"):][:200])
check("…and a refused school leaves nothing behind: no registry row, no folder, no database",
      registry("SELECT count(*) FROM tenants")[0][0] == before_tenants and sorted(os.listdir(os.path.join(TMP, "tenants"))) == before_dirs
      and not registry("SELECT 1 FROM pg_database WHERE datname = :n", n=f"{PREFIX}_gamma"))
r = create("gamma", "Gamma Academy", candidate_pattern="{schol}-{seq}", student_pattern="{prefix}-{seq:6}", first_number="7")
body = html.unescape(r.get_data(as_text=True))
check("the form remembers what was typed after a refusal", 'value="{schol}-{seq}"' in body and 'value="{prefix}-{seq:6}"' in body and 'value="7"' in body)
r = create("gamma", "Gamma Academy", candidate_pattern="<script>alert(1)</script>-{seq}")
body = r.get_data(as_text=True)
check("…and shows hostile text escaped, never as markup", "<script>alert(1)" not in body and "&lt;script&gt;alert(1)" in body)
check("a name and a code that are fine still create (default rules) after all that", create("gamma", "Gamma Academy").status_code == 200
      and info_for("gamma") is not None and read_json("gamma")["candidate_pattern"] == "{school}-{year}-{seq:4}")

# ================================================================ 4. the school's page
page = console.get("/platform/schools/beta", base_url=PL).get_data(as_text=True)
check("a school's page has the Numbering section showing the school's own saved rules",
      'id="numbering-candidate"' in page and 'value="{initials}-{yy}{mon}-{class}-{seq:3}"' in page
      and 'value="{prefix}/{yy}/{seq:5}"' in page and 'name="first_number"' in page and 'value="100"' in page)
check("…with the warning that codes already issued do not change", "Codes already issued do not change" in page)
check("…a Save button posting to the school's numbering address, with a CSRF token",
      'action="/platform/schools/beta/numbering"' in page and "Save numbering" in page and 'data-slug="beta"' in page)

token = csrf(console, "/platform/schools/beta", PL)
before_file = raw("beta")
r = console.post("/platform/schools/beta/numbering", data={
    "_csrf_token": token, "candidate_pattern": "{school}-{class}-{seq:4-5}", "student_pattern": "", "first_number": "1"}, base_url=PL)
check("saving valid rules on the school's page succeeds and returns to the page",
      r.status_code == 302 and r.headers["Location"].startswith("/platform/schools/beta"))
check("…the file now holds the new rules", read_json("beta") == {"version": 1, "candidate_pattern": "{school}-{class}-{seq:4-5}",
                                                                    "student_pattern": "", "first_number": 1})
rows = audit_rows()
check("…the audit trail records the OLD and the NEW pattern",
      len(rows) == 2 and "{initials}-{yy}{mon}-{class}-{seq:3} -> {school}-{class}-{seq:4-5}" in rows[-1][0]
      and "{prefix}/{yy}/{seq:5} -> (the numbering policy)" in rows[-1][0] and "100 -> 1" in rows[-1][0]
      and rows[-1][1] == "ops" and rows[-1][2] == "beta", str(rows[-1]))
page = console.get("/platform/schools/beta", base_url=PL).get_data(as_text=True)  # the flash is shown once, here
check("…and the activity log describes it in words", "Changed a school's numbering rules" in
      html.unescape(console.get("/platform/activity?admin=all", base_url=PL).get_data(as_text=True)))
check("…and the page shows the saved rules and the flash", 'value="{school}-{class}-{seq:4-5}"' in page and "numbering rules have been saved" in page)

good_file = raw("beta")
audits_before = len(audit_rows())
for label, fields, fragment in (
        ("an unknown placeholder", {"candidate_pattern": "{schol}-{seq}"}, "Unknown placeholder {schol}"),
        ("no {seq} or random", {"candidate_pattern": "{school}"}, "every code would be the same"),
        ("two {seq}", {"candidate_pattern": "{seq}-{seq}"}, "more than once"),
        ("unbalanced braces", {"candidate_pattern": "{school-{seq}"}, "Unbalanced braces"),
        ("a forbidden character", {"candidate_pattern": "AB C-{seq}"}, "A space at position 3"),
        ("a pattern that is too long", {"candidate_pattern": "A" * 81 + "{seq}"}, "characters long"),
        ("an empty candidate pattern", {"candidate_pattern": ""}, "cannot be empty"),
        ("a bad student pattern", {"student_pattern": "{class}-{seq}"}, "only available in candidate codes"),
        ("a bad first number", {"first_number": "-4"}, "whole number")):
    data = {"_csrf_token": token, "candidate_pattern": "{school}-{seq:4}", "student_pattern": "", "first_number": "1", **fields}
    r = console.post("/platform/schools/beta/numbering", data=data, base_url=PL)
    body = html.unescape(r.get_data(as_text=True))
    check(f"saving {label} is refused (400) with the reason on the page", r.status_code == 400 and fragment in body
          and "The numbering rules were not saved" in body, f"{r.status_code}")
check("…and none of the refused saves changed the file or wrote an audit entry", raw("beta") == good_file and len(audit_rows()) == audits_before)
r = console.post("/platform/schools/beta/numbering", data={"_csrf_token": token, "candidate_pattern": "<script>alert(1)</script>{seq}",
                                                           "student_pattern": "", "first_number": "1"}, base_url=PL)
body = r.get_data(as_text=True)
check("hostile text typed into the editor comes back escaped", r.status_code == 400 and "<script>alert(1)" not in body and "&lt;script&gt;alert(1)" in body)
r = console.post("/platform/schools/beta/numbering", data={"_csrf_token": token, "candidate_pattern": "{school}-{seq:4}"}, base_url=PL)
check("a form with fields missing is refused, changing nothing", r.status_code == 400 and raw("beta") == good_file)
r = console.post("/platform/schools/beta/numbering", data={"_csrf_token": token, "candidate_pattern": "x" * 5000,
                                                           "student_pattern": "y" * 5000, "first_number": "9" * 5000}, base_url=PL)
check("a huge submission is refused cleanly", r.status_code == 400 and raw("beta") == good_file)
r = console.post("/platform/schools/beta/numbering", data={"_csrf_token": token, "candidate_pattern": "a\x00b-{seq}",
                                                           "student_pattern": "", "first_number": "1"}, base_url=PL)
check("a NUL byte is refused cleanly", r.status_code == 400 and raw("beta") == good_file)
r = console.post("/platform/schools/nosuchschool/numbering", data={"_csrf_token": token, "candidate_pattern": "{school}-{seq:4}",
                                                                   "student_pattern": "", "first_number": "1"}, base_url=PL)
check("a school that does not exist is a 404", r.status_code == 404)

# putting back the same rules is still a save, and is recorded
console.post("/platform/schools/beta/numbering", data={"_csrf_token": token, "candidate_pattern": "{school}-{class}-{seq:4-5}",
                                                      "student_pattern": "", "first_number": "1"}, base_url=PL)
check("every save is recorded, even one that changes nothing", len(audit_rows()) == audits_before + 1)

# ================================================================ 5. who may
audits_before = len(audit_rows())
good_form = {"candidate_pattern": "{school}-{seq:6}", "student_pattern": "", "first_number": "1"}
anonymous = A.app.test_client()
anon_token = csrf(anonymous, "/platform/login", PL)
r = anonymous.post("/platform/schools/beta/numbering", data={**good_form, "_csrf_token": anon_token}, base_url=PL)
check("someone who is not signed in to the console is sent to sign in, and nothing changes",
      r.status_code == 302 and "/platform/login" in r.headers["Location"] and raw("beta") == good_file)
r = anonymous.post("/platform/numbering/preview", data={**good_form, "_csrf_token": anon_token}, base_url=PL)
check("…and cannot use the preview either", r.status_code == 302 and "/platform/login" in r.headers["Location"])
school_url = "http://beta.portal.test"
r = console.post("/platform/schools/beta/numbering", data={**good_form, "_csrf_token": token}, base_url=school_url)
check("the console's numbering address does not exist on a school's own address", r.status_code == 404 and raw("beta") == good_file)
r = console.post("/platform/numbering/preview", data={**good_form, "_csrf_token": token}, base_url=school_url)
check("…nor does the preview", r.status_code == 404)
r = console.post("/platform/schools/beta/numbering", data=good_form, base_url=PL)
check("a save with no CSRF token is refused", r.status_code == 403 and raw("beta") == good_file)
r = console.post("/platform/schools/beta/numbering", data={**good_form, "_csrf_token": "not-the-token"}, base_url=PL)
check("…or the wrong one", r.status_code == 403 and raw("beta") == good_file)
r = console.get("/platform/schools/beta/numbering", base_url=PL)
check("the numbering address does not answer a GET", r.status_code == 405)

pv.create_platform_admin("ops2", "Ops Two", "another-long-password")   # an ordinary platform admin
ordinary = A.app.test_client()
ordinary.post("/platform/login", data={"username": "ops2", "password": "another-long-password",
                                       "_csrf_token": csrf(ordinary, "/platform/login", PL)}, base_url=PL)
r = ordinary.post("/platform/schools/beta/numbering", data={**good_form, "_csrf_token": csrf(ordinary, "/platform/schools/beta", PL)}, base_url=PL)
rows = audit_rows()
check("an ordinary platform admin may set a school's numbering, like the school's other settings, and it is attributed to them",
      r.status_code == 302 and read_json("beta")["candidate_pattern"] == "{school}-{seq:6}" and rows[-1][1] == "ops2", str(rows[-1]))

temporary = team.create_admin("ops3", "Ops Three", "", "ops", 1)
newcomer = A.app.test_client()
newcomer.post("/platform/login", data={"username": "ops3", "password": temporary,
                                       "_csrf_token": csrf(newcomer, "/platform/login", PL)}, base_url=PL)
r = newcomer.post("/platform/schools/beta/numbering", data={**good_form, "candidate_pattern": "{school}-{seq:7}",
                                                            "_csrf_token": csrf(newcomer, "/platform/password", PL)}, base_url=PL)
check("an admin who has not yet chosen their own password is sent to do that first, changing nothing",
      r.status_code == 302 and "/platform/password" in r.headers["Location"] and read_json("beta")["candidate_pattern"] == "{school}-{seq:6}")
ops3 = registry("SELECT id FROM platform_admins WHERE username = 'ops3'")[0][0]
team.remove_admin(ops3, "ops", 1)
r = newcomer.post("/platform/schools/beta/numbering", data={**good_form, "candidate_pattern": "{school}-{seq:7}", "_csrf_token": "x"}, base_url=PL)
check("an admin whose access was removed cannot", r.status_code in (302, 403) and read_json("beta")["candidate_pattern"] == "{school}-{seq:6}")
# put beta back to what the rest of the run expects
console.post("/platform/schools/beta/numbering", data={"_csrf_token": token, "candidate_pattern": "{school}-{class}-{seq:3}",
                                                      "student_pattern": "", "first_number": "1"}, base_url=PL)

# ================================================================ 6. the preview
def preview(client=console, **fields):
    data = {"candidate_pattern": "{school}-{year}-{seq:4}", "student_pattern": "", "first_number": "1", **fields}
    return client.post("/platform/numbering/preview", data=data, base_url=PL,
                       headers={"X-CSRF-Token": csrf(client, "/platform/schools/new", PL)})


snapshot = lambda: (registry("SELECT count(*) FROM platform_audit_log")[0][0], raw("alpha"), raw("beta"),  # noqa: E731
                    sql(info_alpha, "SELECT count(*) FROM candidates")[0][0], registry("SELECT count(*) FROM tenants")[0][0],
                    sorted(os.listdir(rules_dir("alpha"))))
before = snapshot()
r = preview(code="delta", name="Delta Academy of Learning")
data = r.get_json()
check("the preview answers with JSON, marked not to be cached", r.status_code == 200 and r.mimetype == "application/json"
      and r.headers.get("Cache-Control") == "no-store")
check("while creating a school it shows three example codes made from the code and name typed on the form",
      data["candidate"]["ok"] and data["candidate"]["samples"] == [f"DELTA-{YEAR}-0001", f"DELTA-{YEAR}-0002", f"DELTA-{YEAR}-0003"], str(data["candidate"]))
data = preview(code="", name="St. Mary's College", candidate_pattern="{initials}-{class}-{seq:3}").get_json()
check("…with the code taken from the name when the code is blank, and the sample class JSS 1",
      data["candidate"]["samples"][0] == "SMC-JSS1-001", str(data["candidate"]))
data = preview(candidate_pattern="{school}-{seq:3}", first_number="40").get_json()
check("…the first number is where the examples start", data["candidate"]["samples"] == ["CODE-040", "CODE-041", "CODE-042"], str(data))
data = preview(candidate_pattern="{school}-{random:5}").get_json()
check("…random patterns show three different-looking examples", len(data["candidate"]["samples"]) == 3
      and all(re.fullmatch(r"CODE-\d{5}", s) for s in data["candidate"]["samples"]))
data = preview(student_pattern="{prefix}/{yy}/{seq:5}", code="delta").get_json()
check("…student numbers are shown too", data["student"]["ok"] and data["student"]["samples"][0] == f"DELTA/{YY}/00001", str(data["student"]))
data = preview(student_pattern="").get_json()
check("…an empty student pattern says the numbering policy decides, and shows how it would write them",
      data["student"]["ok"] and data["student"].get("empty") and data["student"]["samples"][0] == f"CODE/{YEAR}/0001", str(data["student"]))

for pattern, fragment, position in (("{schol}-{seq}", "Unknown placeholder", 1), ("ABC-{year}", "every code would be the same", None),
                                    ("{seq}-{seq}", "more than once", 7), ("{school-{seq}", "Unbalanced braces", 1),
                                    ("AB C-{seq}", "A space at position 3", 3), ("A" * 81, "characters long", 81)):
    data = preview(candidate_pattern=pattern).get_json()
    result = data["candidate"]
    check(f"the preview reports {fragment!r} inline, with its position ({position})",
          result["ok"] is False and fragment in result["error"] and result["position"] == position, str(result))
data = preview(student_pattern="{class}-{seq}").get_json()
check("…a bad student pattern is reported on its own, leaving the candidate pattern's preview", data["student"]["ok"] is False and data["candidate"]["ok"])
data = preview(first_number="lots").get_json()
check("…a bad first number is reported on its own", data["first_number"]["ok"] is False and "whole number" in data["first_number"]["error"])
data = preview(candidate_pattern="\x00{seq}", student_pattern="\x00", first_number="\x00", code="\x00", name="\x00").get_json()
check("…a NUL byte in every field is refused inline, not with a crash", data["candidate"]["ok"] is False and data["student"]["ok"] is False)
data = preview(candidate_pattern="x" * 5000).get_json()
check("…input is capped, so a huge pattern is 'too long' rather than a burden", data["candidate"]["ok"] is False and "characters long" in data["candidate"]["error"])
data = preview(candidate_pattern="<img src=x onerror=alert(1)>-{seq}").get_json()
check("…hostile text is refused inline and comes back only as data in JSON (the page draws it with textContent)",
      data["candidate"]["ok"] is False and "not allowed" in data["candidate"]["error"])
check("the preview wrote nothing anywhere: no audit entry, no file, no candidate, no school",
      snapshot() == before, "something changed")

r = console.post("/platform/numbering/preview", data={"candidate_pattern": "{school}-{seq:4}", "student_pattern": "", "first_number": "1"}, base_url=PL)
check("the preview needs the CSRF token", r.status_code == 403)
r = console.post("/platform/numbering/preview", data={"candidate_pattern": "{school}-{seq:4}", "student_pattern": "", "first_number": "1",
                                                      "_csrf_token": csrf(console, "/platform/schools/new", PL)}, base_url=PL)
check("…in the form or in a header", r.status_code == 200 and r.get_json()["candidate"]["ok"])
check("it answers only to POST", console.get("/platform/numbering/preview", base_url=PL).status_code == 405)

# on a school that exists, the preview is that school's real next numbers
add_candidate(info_alpha, f"ALPHA-{YEAR}-0007")
add_candidate(info_alpha, f"ALPHA-{YEAR - 1}-0900")
data = preview(slug="alpha", candidate_pattern="{school}-{year}-{seq:4}").get_json()
check("for an existing school the preview shows its real next numbers, going on from its own candidates",
      data["candidate"]["samples"] == [f"ALPHA-{YEAR}-0008", f"ALPHA-{YEAR}-0009", f"ALPHA-{YEAR}-0010"], str(data["candidate"]))
data = preview(slug="alpha", candidate_pattern="{school}-{seq:4}").get_json()
check("…and follows the pattern typed, not the saved one (a pattern with no year counts every code that fits)",
      data["candidate"]["samples"][0] == "ALPHA-0001", str(data["candidate"]))
data = preview(slug="alpha", student_pattern="").get_json()
check("…and shows the school's own policy for student numbers", data["student"]["samples"][0].startswith(f"ALPHA/{YEAR}/"), str(data["student"]))
data = preview(slug="alpha", student_pattern="{prefix}-{seq:6}").get_json()
check("…and the platform's real next student running number", re.fullmatch(r"ALPHA-\d{6}", data["student"]["samples"][0]) is not None)
check("a school code that does not exist, or is not a school code at all, is a 404, never a database call",
      preview(slug="nosuchschool").status_code == 404 and preview(slug="../etc").status_code == 404 and preview(slug="a\x00b").status_code == 404)

with mock.patch.object(pv, "_read_candidate_codes", side_effect=RuntimeError("database is down")):
    r = preview(slug="alpha")
data = r.get_json()
check("a school that cannot be read still gets a preview (examples), and the page says so",
      r.status_code == 200 and data["candidate"]["ok"] and "could not be read" in data.get("unreadable", ""), str(data))
with mock.patch.object(pv, "_read_school", side_effect=lambda info: {"code": "ALPHA", "name": "Alpha School"}), \
        mock.patch.object(pv, "engine_for", side_effect=RuntimeError("no such database")):
    r = preview(slug="alpha")
check("…even when nothing about the school can be read", r.status_code == 200 and r.get_json()["candidate"]["ok"])
with mock.patch.object(pv, "engine_for", side_effect=RuntimeError("no such database")):
    r = console.get("/platform/schools/alpha", base_url=PL)
check("…and the school's page still opens", r.status_code == 200 and 'id="numbering-candidate"' in r.get_data(as_text=True))

# the read-only guarantee is real, not just a promise in the source
try:
    with engine_for(info_alpha).connect() as conn:
        conn.execution_options(postgresql_readonly=True)
        conn.execute(sa.text("UPDATE candidates SET candidate_name = 'changed'"))
    check("the connection the preview uses cannot write (the database itself refuses)", False)
except Exception as exc:
    check("the connection the preview uses cannot write (the database itself refuses)", "read-only" in str(exc).lower(), str(exc)[:120])
check("…and the candidates are as they were", sql(info_alpha, "SELECT count(*) FROM candidates WHERE candidate_name = 'changed'")[0][0] == 0)

# ================================================================ 7. what a school's own rule makes (real database)
os.remove(rules_file("alpha"))
check("a school with no numbering.json numbers by the default, and nothing is written for it",
      new_code(info_alpha) == f"ALPHA-{YEAR}-0008" and not os.path.exists(rules_file("alpha")))
console.post("/platform/schools/alpha/numbering", data={"_csrf_token": token, "candidate_pattern": "{school}-{year}-{seq:4}",
                                                        "student_pattern": "", "first_number": "1"}, base_url=PL)
check("…and saving on the school's page creates it (the old rules were the default)",
      os.path.isfile(rules_file("alpha")) and "the old file could not be read" not in audit_rows()[-1][0]
      and "-> {school}-{year}-{seq:4}" in audit_rows()[-1][0])
add_candidate(info_alpha, f"ALPHA-{YEAR}-0009")
check("the default counts up from the highest number this year", new_code(info_alpha) == f"ALPHA-{YEAR}-0010")
add_candidate(info_alpha, f"ALPHA-{YEAR}-LEGACY")
check("…ignoring codes that do not end in a number", new_code(info_alpha) == f"ALPHA-{YEAR}-0010")
add_candidate(info_alpha, f"ALPHA-{YEAR - 1}-5000")
check("…and each year has its own count (another year's codes are not counted)", new_code(info_alpha) == f"ALPHA-{YEAR}-0010")

console.post("/platform/schools/alpha/numbering", data={"_csrf_token": token, "candidate_pattern": "{school}-{class}-{seq:3}",
                                                        "student_pattern": "", "first_number": "1"}, base_url=PL)
add_candidate(info_alpha, "ALPHA-JSS1-005", cls="JSS 1")
add_candidate(info_alpha, "ALPHA-SS1-020", cls="SS 1")
check("a change made on the school's page is used by the very next number, with no restart, and counts each class on its own",
      new_code(info_alpha, "Ada", "JSS 1") == "ALPHA-JSS1-006" and new_code(info_alpha, "Bola", "ss 1") == "ALPHA-SS1-021"
      and new_code(info_alpha, "Chi", "JSS 2") == "ALPHA-JSS2-001")
check("…and 'no class known' is its own count", new_code(info_alpha, "Dee", "") == "ALPHA--001")
check("…while another school is untouched by all of it", new_code(info_beta, "Ada", "JSS 1") == "BETA-JSS1-001")

# {year}/{month} restart, on real data
other_month = "01" if MONTH != "01" else "02"
console.post("/platform/schools/beta/numbering", data={"_csrf_token": token, "candidate_pattern": "{school}-{year}{month}-{seq:3}",
                                                      "student_pattern": "", "first_number": "1"}, base_url=PL)
add_candidate(info_beta, f"BETA-{YEAR}{other_month}-050")
check("{month} restarts the count in a new month (a code of another month is not counted)", new_code(info_beta) == f"BETA-{YEAR}{MONTH}-001")
add_candidate(info_beta, f"BETA-{YEAR}{MONTH}-003")
check("…and goes on within the month", new_code(info_beta) == f"BETA-{YEAR}{MONTH}-004")

# the first number
console.post("/platform/schools/beta/numbering", data={"_csrf_token": token, "candidate_pattern": "{school}-B{seq:4}",
                                                      "student_pattern": "", "first_number": "500"}, base_url=PL)
check("the first number sets where the count starts", new_code(info_beta) == "BETA-B0500")
add_candidate(info_beta, "BETA-B0007")
check("…and older, smaller codes do not pull it back", new_code(info_beta) == "BETA-B0500")
add_candidate(info_beta, "BETA-B0600")
check("…while a bigger code moves it on", new_code(info_beta) == "BETA-B0601")

# padding, growth and refusal, on real data
console.post("/platform/schools/beta/numbering", data={"_csrf_token": token, "candidate_pattern": "{school}-G{seq:4-5}",
                                                      "student_pattern": "", "first_number": "1"}, base_url=PL)
add_candidate(info_beta, "BETA-G9999")
check("{seq:4-5} grows from 4 to 5 digits when the 4-digit numbers are used up", new_code(info_beta) == "BETA-G10000")
add_candidate(info_beta, "BETA-G99999")
message = refusal(lambda: new_code(info_beta))
count_before = sql(info_beta, "SELECT count(*) FROM candidates")[0][0]
check("…and beyond 5 digits registration is refused with a message that names the pattern and says who can widen it",
      message is not None and "{seq:4-5}" in message and "platform team" in message, str(message))
console.post("/platform/schools/beta/numbering", data={"_csrf_token": token, "candidate_pattern": "{school}-H{seq:4}",
                                                      "student_pattern": "", "first_number": "1"}, base_url=PL)
add_candidate(info_beta, "BETA-H9999")
message = refusal(lambda: new_code(info_beta))
check("…{seq:4} is refused after 9999", message is not None and "9999" in message, str(message))
check("…and a refusal registers nobody", sql(info_beta, "SELECT count(*) FROM candidates")[0][0] == count_before + 1)

# random patterns retry on a collision
console.post("/platform/schools/beta/numbering", data={"_csrf_token": token, "candidate_pattern": "{school}-R{random:1}",
                                                      "student_pattern": "", "first_number": "1"}, base_url=PL)
add_candidate(info_beta, "BETA-R5")
picks = iter("5566")
with mock.patch.object(NP.secrets, "choice", side_effect=lambda alphabet: next(picks)):
    got = new_code(info_beta)
check("a random pattern that hits a code already in use tries again", got == "BETA-R6", got)
with mock.patch.object(NP.secrets, "choice", side_effect=lambda alphabet: "5"):
    message = refusal(lambda: new_code(info_beta))
check("…and gives up with a plain message after the attempts allowed", message is not None and "already in use" in message
      and "platform team" in message, str(message))
check("random codes really are made by the pattern", re.fullmatch(r"BETA-R\d", new_code(info_beta)) is not None)

# two schools, two rules, side by side
console.post("/platform/schools/alpha/numbering", data={"_csrf_token": token, "candidate_pattern": "{initials}{yy}-{seq:3}",
                                                       "student_pattern": "", "first_number": "1"}, base_url=PL)
console.post("/platform/schools/beta/numbering", data={"_csrf_token": token, "candidate_pattern": "{school}.{seq}",
                                                      "student_pattern": "", "first_number": "1"}, base_url=PL)
a1, b1 = new_code(info_alpha), new_code(info_beta)
check("two schools with different rules each number by their own", a1 == f"AS{YY}-001" and b1 == "BETA.1", f"{a1} {b1}")
add_candidate(info_alpha, a1)
check("…and each keeps its own count", new_code(info_alpha) == f"AS{YY}-002" and new_code(info_beta) == "BETA.1")
check("…and each school's file is its own", read_json("alpha")["candidate_pattern"] == "{initials}{yy}-{seq:3}"
      and read_json("beta")["candidate_pattern"] == "{school}.{seq}")

# ================================================================ 8. through the real forms
def signed_in(code):
    url = f"http://{code}.portal.test"
    r = console.post(f"/platform/schools/{code}/enter",
                     data={"_csrf_token": csrf(console, f"/platform/schools/{code}", PL)}, base_url=PL)
    c = A.app.test_client()
    c.get(r.headers["Location"][len(url):], base_url=url)
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


def register(client, url, **extra):
    client.get("/admin/workspace/entrance", base_url=url)
    return client.post("/admin/candidates/new", data={**form, **extra, "_csrf_token": csrf(client, "/admin/candidates/new", url)},
                       base_url=url, content_type="multipart/form-data")


console.post("/platform/schools/alpha/numbering", data={"_csrf_token": token, "candidate_pattern": "{school}-{class}-{yy}{mon}-{seq:4}",
                                                       "student_pattern": "{prefix}/{yy}/{seq:5}", "first_number": "1"}, base_url=PL)
c_alpha, u_alpha = signed_in("alpha")
r = register(c_alpha, u_alpha)
body = r.get_data(as_text=True)
expected = f"ALPHA-JSS1-{YY}{MON}-0001"
check("registering a candidate through the real admin form gives the school's own code (with the class typed on the form)",
      r.status_code == 200 and expected in body, f"{r.status_code}")
check("…and that code is what was saved", sql(info_alpha, "SELECT candidate_name FROM candidates WHERE candidate_code = :c", c=expected) == [("Ada Obi",)])
r = register(c_alpha, u_alpha, target_class="SS 1", candidate_name="Bola Eze")
check("…the next candidate of another class starts that class's own count", f"ALPHA-SS1-{YY}{MON}-0001" in r.get_data(as_text=True))
r = register(c_alpha, u_alpha)
check("…and the same class goes on", f"ALPHA-JSS1-{YY}{MON}-0002" in r.get_data(as_text=True))

c_beta, u_beta = signed_in("beta")
console.post("/platform/schools/beta/numbering", data={"_csrf_token": token, "candidate_pattern": "{initials}-{seq:2}",
                                                      "student_pattern": "", "first_number": "1"}, base_url=PL)
r = register(c_beta, u_beta)
check("another school's form gives that school's own code", r.status_code == 200 and "BC-01" in r.get_data(as_text=True))

# students, through the real form
JSS1 = sql(info_alpha, "SELECT id FROM school_classes WHERE name = 'JSS 1'")[0][0]
STUDENT = {"gender": "Female", "date_of_birth": "2014-05-01", "state_of_origin": "Lagos", "class_id": JSS1,
           "guardian_name": "A Guardian", "guardian_phone": "08031234567", "guardian_email": "guardian@example.test"}


def new_student(client, url, first, last, expect_ok=True):
    client.get("/admin/workspace/school", base_url=url)
    r = client.post("/admin/school/students/new", data={**STUDENT, "first_name": first, "middle_name": "", "last_name": last,
                                                       "_csrf_token": csrf(client, "/admin/school/students/new", url)},
                    base_url=url, content_type="multipart/form-data")
    return r


policy_sequence = sql(info_alpha, "SELECT next_sequence FROM school_numbering_policies")[0][0]
r = new_student(c_alpha, u_alpha, "Ada", "Obi")
number = sql(info_alpha, "SELECT student_number FROM students WHERE first_name = 'Ada'")
check("a student created through the real form gets a number written by the school's student pattern",
      number == [(f"ALPHA/{YY}/{policy_sequence:05d}",)], f"{r.status_code} {number}")
check("…and the platform's own running number moved on by one, with the number recorded in the ledger",
      sql(info_alpha, "SELECT next_sequence FROM school_numbering_policies")[0][0] == policy_sequence + 1
      and sql(info_alpha, "SELECT count(*) FROM student_number_allocations WHERE student_number = :n", n=number[0][0])[0][0] == 1)

r = new_student(c_beta, u_beta, "Bea", "Ade")
number = sql(info_beta, "SELECT student_number FROM students WHERE first_name = 'Bea'")
check("a school with no student pattern still numbers students by its policy, as it always did",
      number and re.fullmatch(rf"BETA/{YEAR}/\d{{4}}", number[0][0]) is not None, f"{r.status_code} {number}")

# a student pattern that cannot be written is refused, and the running number does not move
sql(info_alpha, "UPDATE school_numbering_policies SET next_sequence = 99999")
console.post("/platform/schools/alpha/numbering", data={"_csrf_token": token, "candidate_pattern": "{school}-{seq:4}",
                                                       "student_pattern": "{prefix}-{seq:4-5}", "first_number": "1"}, base_url=PL)
students_before = sql(info_alpha, "SELECT count(*) FROM students")[0][0]
r = new_student(c_alpha, u_alpha, "Chi", "Nwosu")
check("{seq:4-5} carries a student's running number to 99999", sql(info_alpha, "SELECT student_number FROM students WHERE first_name = 'Chi'") == [("ALPHA-99999",)])
r = new_student(c_alpha, u_alpha, "Dee", "Okoro")
page = html.unescape(r.get_data(as_text=True))
check("…and beyond 5 digits the registration is refused with the pattern's message, and registers nobody",
      "{seq:4-5}" in page and "platform team" in page and sql(info_alpha, "SELECT count(*) FROM students")[0][0] == students_before + 1, page[:200])
check("…the running number did not move on a refusal", sql(info_alpha, "SELECT next_sequence FROM school_numbering_policies")[0][0] == 100000)

# the student function on its own
console.post("/platform/schools/alpha/numbering", data={"_csrf_token": token, "candidate_pattern": "{school}-{seq:4}",
                                                       "student_pattern": "", "first_number": "1"}, base_url=PL)
check("with no student pattern, student_number() returns None and the policy decides",
      in_school(info_alpha, lambda: N.student_number("ALPHA", 1, YEAR, 5, 4)) is None)
console.post("/platform/schools/alpha/numbering", data={"_csrf_token": token, "candidate_pattern": "{school}-{seq:4}",
                                                       "student_pattern": "{school|lower}/{yy}/{seq:4}", "first_number": "1"}, base_url=PL)
check("with one, it writes the number as the pattern says, from the running number and year it is given",
      in_school(info_alpha, lambda: N.student_number("XYZ", 0, 2031, 12, 6)) == "alpha/31/0012")
console.post("/platform/schools/alpha/numbering", data={"_csrf_token": token, "candidate_pattern": "{school}-{seq:4}",
                                                       "student_pattern": "{prefix}-{random:3}", "first_number": "1"}, base_url=PL)
sql(info_alpha, "INSERT INTO student_number_allocations (school_id, student_number, sequence_number, allocation_year, source, allocated_at, active) "
                "SELECT id, 'ALPHA-777', 1, :y, 'existing', '2026-01-01T00:00:00+00:00', 1 FROM schools LIMIT 1", y=YEAR)
picks = iter("777888")
with mock.patch.object(NP.secrets, "choice", side_effect=lambda alphabet: next(picks)):
    number = in_school(info_alpha, lambda: N.student_number("ALPHA", 1, YEAR, 9, 4))
check("a random student number that is already in the ledger is made again", number == "ALPHA-888", str(number))
console.post("/platform/schools/alpha/numbering", data={"_csrf_token": token, "candidate_pattern": "{school}-{seq:4}",
                                                       "student_pattern": "", "first_number": "1"}, base_url=PL)

# ================================================================ 9. a numbering.json that is wrong
add_candidate(info_alpha, "ALPHA-0100")
candidates_before = sql(info_alpha, "SELECT count(*) FROM candidates")[0][0]
for label, content in (("is not JSON", "{ not json"), ("has a pattern that is not allowed", json.dumps({**GOOD, "candidate_pattern": "{__import__}-{seq}"})),
                       ("is from another version", json.dumps({**GOOD, "version": 9})), ("has an extra entry", json.dumps({**GOOD, "hook": "x"}))):
    with open(rules_file("alpha"), "w", encoding="utf-8") as fh:
        fh.write(content)
    before_bytes = raw("alpha")
    message = refusal(lambda: new_code(info_alpha))
    check(f"a numbering.json that {label} is refused with a plain message and never falls back to another rule",
          message is not None and "numbering.json" in message and "ALPHA-0101" not in message, str(message))
    check("…and the file is left exactly as it was", raw("alpha") == before_bytes)
    check("…student numbers are refused too, not numbered by some other rule",
          refusal(lambda: in_school(info_alpha, lambda: N.student_number("ALPHA", 1, YEAR, 1, 4))) is not None)

with open(rules_file("alpha"), "w", encoding="utf-8") as fh:
    fh.write("{ not json")
r = register(c_alpha, u_alpha)
body = html.unescape(r.get_data(as_text=True))
check("a broken file shows a message on the candidate form instead of an error page, and registers nobody",
      r.status_code == 200 and "numbering.json" in body and "Traceback" not in body
      and sql(info_alpha, "SELECT count(*) FROM candidates")[0][0] == candidates_before)
r = new_student(c_alpha, u_alpha, "Eve", "Bello")
check("…and a broken file refuses a student too, without a crash", r.status_code == 200 and sql(info_alpha, "SELECT count(*) FROM students WHERE first_name = 'Eve'")[0][0] == 0)
page = console.get("/platform/schools/alpha", base_url=PL).get_data(as_text=True)
check("the school's page still opens, and says the file cannot be used (never hiding it)",
      "This school&#39;s numbering file cannot be used" in page or "This school's numbering file cannot be used" in page)
check("…the console's own preview still works", preview(slug="alpha").status_code == 200)
check("…and nothing replaced the file by itself", raw("alpha") == b"{ not json")
r = console.post("/platform/schools/alpha/numbering", data={"_csrf_token": token, "candidate_pattern": "{school}-{seq:5}",
                                                            "student_pattern": "", "first_number": "1"}, base_url=PL)
check("an explicit, checked save from the console puts a broken school right",
      r.status_code == 302 and read_json("alpha")["candidate_pattern"] == "{school}-{seq:5}")
check("…keeping a copy of the file it replaced", any(open(p, "rb").read() == b"{ not json" for p in glob.glob(os.path.join(rules_dir("alpha"), "numbering.unreadable-*.json"))))
check("…and the audit trail says the old file could not be read", "could not be read and was replaced" in audit_rows()[-1][0], audit_rows()[-1][0])
check("…after which the school numbers again, by the pattern just saved", new_code(info_alpha) == "ALPHA-00001")

# a link that leads out of the school's folder is never followed
outside = os.path.join(TMP, "outside.json")
with open(outside, "w", encoding="utf-8") as fh:
    json.dump({**GOOD, "candidate_pattern": "OUT-{seq:4}"}, fh)
os.remove(rules_file("alpha"))
try:
    os.symlink(outside, rules_file("alpha"))
except (OSError, NotImplementedError):
    check("a link leading out of the school's folder is never followed (skipped: this machine cannot make links)", True)
else:
    message = refusal(lambda: new_code(info_alpha))
    check("a link leading out of the school's folder is never followed", message is not None and "inside the school's own folder" in message, str(message))
    console.post("/platform/schools/alpha/numbering", data={"_csrf_token": token, "candidate_pattern": "{school}-{seq:4}",
                                                           "student_pattern": "", "first_number": "1"}, base_url=PL)
    check("…and a save replaces only the link, never what it pointed at",
          not os.path.islink(rules_file("alpha")) and json.load(open(outside, encoding="utf-8"))["candidate_pattern"] == "OUT-{seq:4}")
check("no temporary files are left in any school's folder", not glob.glob(os.path.join(TMP, "tenants", "*", ".numbering-*")))

# the old Python-file mechanism is gone: a numbering.py in a school's folder is never run
with open(os.path.join(rules_dir("alpha"), "numbering.py"), "w", encoding="utf-8") as fh:
    fh.write("raise SystemExit('this must never run')\ndef candidate_code(ctx):\n    return 'PYTHON-1'\n")
with open(rules_file("alpha"), "w", encoding="utf-8") as fh:
    json.dump(GOOD, fh)
code = new_code(info_alpha)
check("a numbering.py left in a school's folder is ignored: it is never imported or run", code.startswith("ALPHA-") and "PYTHON" not in code, code)
check("the platform has no code that loads a school's Python file",
      not hasattr(N, "install_rules_file") and "importlib" not in open(os.path.join(ROOT, "core", "numbering.py"), encoding="utf-8").read())

dispose_engines()
DROP_TEST_DATABASES()
shutil.rmtree(TMP, ignore_errors=True)
print()
failed = [x for x in results if not x[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
