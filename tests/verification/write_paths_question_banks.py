"""Question banks: what a new school starts with, and how a school brings its own.

Drives the real application over HTTP against throw-away PostgreSQL databases and a
throw-away tenants folder.

* The platform's standard entrance banks (``starter_banks/``) are copied into a new school's
  own ``data/`` folder and its examinations table when the console creates it, unless the
  operator unticks the box. They carry no school's name, and one school's banks are never
  another's.
* A school's administrators can import a bank from a JSON file. The file is checked strictly,
  an existing bank is only replaced when they say so, it is written only inside that school's
  folder, and the permission, the CSRF token and the audit log all apply.
* A brand-new school, using only the standard banks, can register a candidate who can start,
  answer and submit a paper.

Run:  python tests/verification/write_paths_question_banks.py
"""
import hashlib
import html
import inspect
import io
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_banks_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('banks')
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
from werkzeug.security import generate_password_hash  # noqa: E402

import app as A  # noqa: E402
from control_plane import provisioning as pv  # noqa: E402
from control_plane.context import tenant_context  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import dispose_engines  # noqa: E402
from core import banks as B  # noqa: E402
from core.entrance import (  # noqa: E402
    _valid_question_configuration, entrance_bank_display_name, load_banks,
)
from models import (  # noqa: E402
    AcademicSession, AdminResourceLock, Attempt, AttemptQuestion, AuditLog, Candidate,
    EntranceBankConfig, Examination,
)

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


def client(host):
    return A.app.test_client(), f"http://{host}"


def csrf(c, url, path):
    body = c.get(path, base_url=url).get_data(as_text=True)
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


def data_folder(slug):
    return os.path.join(TMP, "tenants", slug, "data")


def folder_state(slug):
    """Every file in a school's data folder and a fingerprint of its content."""
    folder = data_folder(slug)
    return {n: hashlib.sha256(open(os.path.join(folder, n), "rb").read()).hexdigest()
            for n in sorted(os.listdir(folder))}


def bank_files(slug):
    return sorted(n for n in os.listdir(data_folder(slug)) if n.endswith(".json"))


def exam_rows(slug):
    def read():
        return {r.bank_id: r for r in A.db.session.scalars(sa.select(Examination)).all()}
    return in_school(slug, read)


def tree_fingerprint(path):
    out = {}
    for base, _, names in os.walk(path):
        for n in names:
            full = os.path.join(base, n)
            out[os.path.relpath(full, path)] = hashlib.sha256(open(full, "rb").read()).hexdigest()
    return out


STARTER_BEFORE = tree_fingerprint(B.STARTER_DIR)


def good_bank(bank_id="alpha_extra", name="Extra Practice Bank", n=3, **over):
    bank = {"id": bank_id, "name": name, "level": "Year 7", "entry_group": "year7",
            "subject": "Mathematics", "duration_seconds": 1200,
            "questions": [{"id": i, "text": f"What is {i} + {i}?",
                           "options": [str(2 * i - 1), str(2 * i), str(2 * i + 1), str(2 * i + 2)],
                           "answer": 1} for i in range(1, n + 1)]}
    bank.update(over)
    return bank


def as_bytes(payload):
    return payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")


def post_import(c, url, payload, replace=False, filename="bank.json", token=None):
    token = token or csrf(c, url, "/admin/banks/import")
    data = {"_csrf_token": token}
    if payload is not None:
        data["bank_file"] = (io.BytesIO(as_bytes(payload)), filename)
    if replace:
        data["replace"] = "1"
    return c.post("/admin/banks/import", data=data, base_url=url, content_type="multipart/form-data")


def text_of(r):
    return html.unescape(r.get_data(as_text=True))


def sign_in(slug, username, password, entrance_area=True):
    c, u = client(f"{slug}.portal.test")
    r = c.post("/login", data={"username": username, "password": password,
                               "_csrf_token": csrf(c, u, "/login")}, base_url=u)
    if entrance_area:
        # An administrator chooses the entrance-examination area before its pages open.
        c.get("/admin/workspace/entrance", base_url=u)
    return c, u, r


def make_admin_known(slug, username, password):
    """Give a school's first administrator a password we know, without the change-it step."""
    def go():
        admin = A.db.session.scalars(sa.select(A.Admin).where(A.Admin.username == username)).first()
        admin.password_hash = generate_password_hash(password)
        admin.password_must_change = 0
        A.db.session.commit()
        return admin.id
    return in_school(slug, go)


# ============================================================ the standard set, on its own
entries = B.starter_entries()
manifest = json.load(open(os.path.join(B.STARTER_DIR, "manifest.json"), encoding="utf-8"))
check("the platform has a starter set of six banks, with a manifest",
      len(entries) == 6 and len(manifest["banks"]) == 6, str(len(entries)))
check("it covers Mathematics, English and General Knowledge for both entry classes",
      {(e["entry_group"], e["subject"]) for e in entries} ==
      {(g, s) for g in ("year7", "year10") for s in ("mathematics", "english", "general_knowledge")})
starter_texts = {}
for name in sorted(os.listdir(B.STARTER_DIR)):
    starter_texts[name] = open(os.path.join(B.STARTER_DIR, name), encoding="utf-8").read()
check("nothing in the starter folder names a school",
      not [n for n, t in starter_texts.items() if re.search(r"creative rainbow|crainbow|montessori|crms", t, re.I)])
check("the starter banks have neutral ids of their own",
      all(e["id"].startswith("starter_") for e in entries), str([e["id"] for e in entries]))
problems = []
for e in entries:
    try:
        b = B.read_starter_bank(e)
    except B.BankError as exc:
        problems.append(f'{e["id"]}: {exc}')
        continue
    served = e["questions_to_serve"]
    if len(b["questions"]) != e["question_count"]:
        problems.append(f'{e["id"]}: manifest says {e["question_count"]} questions, file has {len(b["questions"])}')
    marks = 100 / served if served else 0
    if not _valid_question_configuration(served)[0] or served > len(b["questions"]) or marks != int(marks):
        problems.append(f'{e["id"]}: cannot serve {served}')
    if any(q["answer"] is None for q in b["questions"]):
        problems.append(f'{e["id"]}: has an unanswerable question')
check("every starter bank passes the same strict checks as an uploaded one, and its manifest entry is true",
      not problems, "; ".join(problems))

# ================================================================= the console creates schools
pv.create_platform_admin("ops", "Ops Team", "a-long-platform-password")
c_pl, u_pl = client("platform.test")
token = csrf(c_pl, u_pl, "/platform/login")
c_pl.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                   "_csrf_token": token}, base_url=u_pl)
form_page = text_of(c_pl.get("/platform/schools/new", base_url=u_pl))
check("the create form offers 'Start with the standard entrance question banks', ticked by default",
      "Start with the standard entrance question banks" in form_page
      and re.search(r'<input type="checkbox" id="starter_banks" name="starter_banks"[^>]*\schecked', form_page) is not None)


def create_school(name, code, admin, opt_in=True):
    token = csrf(c_pl, u_pl, "/platform/schools/new")
    data = {"name": name, "code": code, "admin_username": admin, "admin_display_name": admin.title(),
            "_csrf_token": token}
    if opt_in:
        data["starter_banks"] = "1"
    r = c_pl.post("/platform/schools/new", data=data, base_url=u_pl, content_type="multipart/form-data")
    return r


NAMES = {"alpha": "Alpha Academy", "beta": "Beta College", "gamma": "Gamma School"}
r = create_school(NAMES["alpha"], "alpha", "alpha_admin")
check("the console creates a school with the standard banks ticked", "Alpha Academy is ready" in text_of(r), str(r.status_code))
r = create_school(NAMES["beta"], "beta", "beta_admin", opt_in=False)
check("…and one with the box unticked", "Beta College is ready" in text_of(r), str(r.status_code))
r = create_school(NAMES["gamma"], "gamma", "gamma_admin")
check("…and a third, also with the standard banks", "Gamma School is ready" in text_of(r), str(r.status_code))
ids = {slug: make_admin_known(slug, f"{slug}_admin", f"{slug.title()}-admin-pass-1") for slug in NAMES}

# ------------------------------------------------------- what each school was given
starter_ids = sorted(e["id"] for e in entries)
for slug in ("alpha", "gamma"):
    files = bank_files(slug)
    check(f"{slug}: the six standard banks are in its own data folder, one file each",
          files == sorted(f"{i}.json" for i in starter_ids), str(files))
    check(f"{slug}: nothing else is in the folder (no half-written files)",
          sorted(os.listdir(data_folder(slug))) == files, str(os.listdir(data_folder(slug))))
    rows = exam_rows(slug)
    check(f"{slug}: its examinations table lists all six, with the right question counts",
          sorted(rows) == starter_ids and all(
              rows[e["id"]].question_count == e["question_count"] for e in entries),
          str(sorted(rows)))
    check(f"{slug}: they are not live until an administrator activates them",
          all(r.active == 0 for r in rows.values()))

all_names = [n for _, n, *_ in pv.list_tenants()]
leaked = []
for slug in ("alpha", "gamma"):
    for name in bank_files(slug):
        text = open(os.path.join(data_folder(slug), name), encoding="utf-8").read()
        for school in all_names + ["Creative Rainbow", "Crainbow", "Montessori"]:
            if school.lower() in text.lower():
                leaked.append((slug, name, school))
check("no bank in any school names any school, including its own or the first one", not leaked, str(leaked))

check("the school that unticked the box has no bank files at all",
      bank_files("beta") == [] and exam_rows("beta") == {}, str(bank_files("beta")))
check("the standard banks were copied, not moved: the platform's own copy is intact",
      len(entries) == 6 and all(os.path.isfile(os.path.join(B.STARTER_DIR, e["file"])) for e in entries))
check("the platform's own starter files were not touched by making schools",
      tree_fingerprint(B.STARTER_DIR) == STARTER_BEFORE)

visible = {}
for slug in ("alpha", "gamma"):
    visible[slug] = in_school(slug, lambda: {i: entrance_bank_display_name(i) for i in starter_ids})
expected = {"starter_year7_mathematics": "Mathematics - Entrance Examination into Year 7 / JSS 1",
            "starter_sss1_english": "English - Entrance Examination into Year 10 / SSS 1",
            "starter_sss1_general_knowledge": "General Knowledge - Entrance Examination into Year 10 / SSS 1"}
check("the names an administrator sees are neutral and say what each paper is",
      all(visible["alpha"][k] == v for k, v in expected.items()), str(visible["alpha"]))
check("…and are the same in every school, because they name no school",
      visible["alpha"] == visible["gamma"])

# Running the copy again changes nothing, and never overwrites what a school has done.
before = folder_state("alpha")
again = pv.add_starter_banks(info_for("alpha"))
check("copying the standard banks again is safe: everything is skipped, nothing changes",
      again["created"] == [] and len(again["skipped"]) == 6 and folder_state("alpha") == before, str(again))

pv.create_tenant("delta", "Delta School", starter_banks=False)
delta_folder = data_folder("delta")
check("create_tenant(starter_banks=False) gives a school none",
      bank_files("delta") == [] and exam_rows("delta") == {})
with open(os.path.join(delta_folder, "starter_year7_english.json"), "w", encoding="utf-8") as f:
    f.write("the school's own file, which must survive")
result = pv.add_starter_banks(info_for("delta"))
check("a file that is already there is never overwritten; the rest are added",
      open(os.path.join(delta_folder, "starter_year7_english.json"), encoding="utf-8").read()
      == "the school's own file, which must survive"
      and "starter_year7_english" in result["skipped"] and len(result["created"]) == 5, str(result))

# ================================================================== the admin area, as each school
c_a, u_a, r = sign_in("alpha", "alpha_admin", "Alpha-admin-pass-1")
check("alpha's administrator signs in", r.status_code == 302 and "/admin" in r.headers["Location"], r.headers.get("Location", ""))
c_b, u_b, r = sign_in("beta", "beta_admin", "Beta-admin-pass-1")
c_g, u_g, r = sign_in("gamma", "gamma_admin", "Gamma-admin-pass-1")

page = text_of(c_a.get("/admin/banks?level=year7", base_url=u_a))
check("alpha's bank list shows the standard Year 7 banks by their neutral names",
      "Mathematics - Entrance Examination into Year 7 / JSS 1" in page
      and "English - Entrance Examination into Year 7 / JSS 1" in page
      and "General Knowledge - Entrance Examination into Year 7 / JSS 1" in page)
check("…and no other school's name, and none of the first school's",
      "Beta College" not in page and "Gamma School" not in page and "Creative Rainbow" not in page)

page = text_of(c_b.get("/admin/banks", base_url=u_b))
r_beta_cfg = c_b.get("/admin/entrance-config", base_url=u_b)
cfg = text_of(r_beta_cfg)
check("a school with no banks is told so in plain words, not shown an empty page",
      "There are no question banks here yet" in page and "Import a question bank" in page
      and "Ask the platform team" in page)
check("…the level cards are not shown as empty boxes", "QUESTION BANK SET" not in page)
check("…and the exam configuration page explains what to do too",
      r_beta_cfg.status_code == 200 and "There are no question banks to set papers from" in cfg
      and "Import a question bank" in cfg and "Ask the platform team" in cfg)
imp = text_of(c_b.get("/admin/banks/import", base_url=u_b))
check("the import page for such a school says it has none yet", "This school has no question banks yet" in imp)
check("a school that does have banks does not get that message",
      "There are no question banks here yet" not in text_of(c_a.get("/admin/banks", base_url=u_a)))

# ================================================================== importing: the happy path
r = post_import(c_a, u_a, good_bank())
check("an administrator can import a valid bank", r.status_code == 302 and r.headers["Location"].endswith("/admin/banks/alpha_extra"),
      f"{r.status_code} {r.headers.get('Location')}")
landing = text_of(c_a.get(r.headers["Location"], base_url=u_a))
check("…and is shown the result: what was imported and how many questions",
      'Imported "Extra Practice Bank" with 3 questions' in landing)
check("the bank is written inside that school's own folder",
      "alpha_extra.json" in bank_files("alpha") and len(bank_files("alpha")) == 7)
saved = json.load(open(os.path.join(data_folder("alpha"), "alpha_extra.json"), encoding="utf-8"))
check("…in the clean form the engine reads (only the fields it uses)",
      saved["id"] == "alpha_extra" and saved["duration_seconds"] == 1200
      and set(saved) == {"id", "name", "level", "entry_group", "subject", "duration_seconds", "version", "source_status", "questions"},
      str(sorted(saved)))
rows = exam_rows("alpha")
check("its examinations row exists (sync ran), not live until activated",
      "alpha_extra" in rows and rows["alpha_extra"].question_count == 3 and rows["alpha_extra"].active == 0)
check("a warning is not shown for a bank that says its subject and class", "cannot be used as an entrance paper" not in landing)

r = post_import(c_a, u_a, {**good_bank("alpha_loose", "Loose Bank"), "subject": "", "level": "", "entry_group": ""})
check("a bank that names no subject or class imports, with a plain warning that it cannot be an entrance paper yet",
      r.status_code == 302 and "cannot be used as an entrance paper" in text_of(c_a.get(r.headers["Location"], base_url=u_a)))

# ---------------------------------------------------------------- the two schools are independent
check("one school's import is not in another's folder or table",
      "alpha_extra.json" not in bank_files("gamma") and "alpha_extra" not in exam_rows("gamma")
      and bank_files("beta") == [])
gamma_view = in_school("gamma", lambda: sorted(load_banks()))
check("…and gamma's own banks are still just the six standard ones", gamma_view == starter_ids, str(gamma_view))
r = post_import(c_b, u_b, good_bank("beta_own", "Beta's Own Bank"))
check("a school that started with none can import its first bank",
      r.status_code == 302 and bank_files("beta") == ["beta_own.json"])
check("…which alpha and gamma cannot see",
      "beta_own" not in in_school("alpha", lambda: sorted(load_banks()))
      and "beta_own" not in in_school("gamma", lambda: sorted(load_banks())))
check("…and once it has a bank, the 'no banks' guidance goes away",
      "There are no question banks here yet" not in text_of(c_b.get("/admin/banks", base_url=u_b)))
check("the same id can exist in two schools without either affecting the other",
      post_import(c_g, u_g, good_bank("alpha_extra", "Gamma's Version", n=5)).status_code == 302
      and json.load(open(os.path.join(data_folder("gamma"), "alpha_extra.json"), encoding="utf-8"))["name"] == "Gamma's Version"
      and json.load(open(os.path.join(data_folder("alpha"), "alpha_extra.json"), encoding="utf-8"))["name"] == "Extra Practice Bank")

# ================================================================ importing: what is refused
def rejected(name, payload, expect, **kw):
    before_files = folder_state("alpha")
    before_rows = sorted(exam_rows("alpha"))
    r = post_import(c_a, u_a, payload, **kw)
    body = text_of(r)
    ok = (r.status_code == 400 and expect.lower() in body.lower()
          and folder_state("alpha") == before_files and sorted(exam_rows("alpha")) == before_rows)
    check(f"refused: {name}", ok, f"{r.status_code}; wanted '{expect}'")


def with_question(**over):
    bank = good_bank("alpha_bad")
    bank["questions"][1] = {**bank["questions"][1], **over}
    return bank


rejected("not JSON at all", b"this is { not json", "not valid JSON")
rejected("a truncated file", b'{"id": "alpha_bad", "name": "x", "questions": [', "not valid JSON")
rejected("not UTF-8 text", b"\xff\xfe\x00\x01 binary", "UTF-8")
rejected("an empty file", b"   ", "empty")
rejected("JSON that is a list, not a bank", b"[1, 2, 3]", "one question bank")
rejected("a bare number", b"42", "one question bank")
rejected("NaN in place of a number", b'{"id":"alpha_bad","name":"n","duration_seconds":NaN,"questions":[]}', "not valid JSON")
rejected("absurdly deep nesting", b"[" * 200000, "not valid JSON")
rejected("a file over the size limit",
         b'{"id":"alpha_bad","name":"n","pad":"' + b"a" * (B.MAX_BANK_BYTES + 10) + b'"}', "too big")
rejected("no file chosen", None, "Choose a bank file")
rejected("no id", {k: v for k, v in good_bank("x").items() if k != "id"}, 'needs an "id"')
for unsafe in ("../x", "..\\x", "a/b", "C:\\evil", "x.json", ".hidden", "a b", "UPPER", "x" * 65, "con", "NUL",
               "manifest", "import", "new", 123, ["a"]):
    rejected(f"the unsafe id {unsafe!r}", good_bank(unsafe), "bank id")
rejected("no name", {k: v for k, v in good_bank().items() if k != "name"}, "name")
rejected("no questions list", {k: v for k, v in good_bank().items() if k != "questions"}, '"questions" list')
rejected("questions that is not a list", good_bank(questions={"1": "x"}), '"questions" list')
rejected("an empty questions list", good_bank(questions=[]), "no questions")
rejected("more questions than allowed", good_bank(n=B.MAX_QUESTIONS + 1), "most one bank may hold")
rejected("a question with no id", with_question(id=None), "Question 2 needs a whole-number")
rejected("a question id that is text", with_question(id="two"), "Question 2 needs a whole-number")
rejected("a question id of zero", with_question(id=0), "Question 2 needs a whole-number")
dup = good_bank("alpha_bad")
dup["questions"][2]["id"] = 1
rejected("duplicate question ids", dup, "repeats the id 1")
rejected("a question with no text", with_question(text=""), "Question 2 \"text\" is required")
rejected("a question whose text is not text", with_question(text=5), "must be text")
rejected("too few options", with_question(options=["a", "b"]), "exactly 4")
rejected("too many options", with_question(options=["a", "b", "c", "d", "e"]), "exactly 4")
rejected("options that are not a list", with_question(options="abcd"), "exactly 4")
rejected("a blank option", with_question(options=["a", "b", "", "d"]), "option 3")
rejected("two options that are the same", with_question(options=["a", "b", "A", "d"]), "two options that are the same")
rejected("an answer past the last option", with_question(answer=4), "answer 4")
rejected("a negative answer", with_question(answer=-1), "answer -1")
rejected("an answer that is text", with_question(answer="1"), '"answer"')
rejected("an answer that is true/false", with_question(answer=True), '"answer"')
rejected("an answer with a fraction", with_question(answer=1.5), '"answer"')
rejected("a missing answer", {**with_question(), "questions": [{k: v for k, v in q.items() if k != "answer"} for q in good_bank()["questions"]]},
         '"answer"')
rejected("points of zero", with_question(points=0), "points")
rejected("a duration under a minute", good_bank(duration_seconds=30), "duration")
rejected("a duration over six hours", good_bank(duration_seconds=7 * 3600), "duration")
rejected("a duration that is text", good_bank(duration_seconds="60"), "duration")
rejected("an unknown entry class", good_bank(entry_group="year9"), "entry_group")
rejected("a control character in the text", with_question(text="bad\x00text"), "control character")
many = good_bank("alpha_bad")
for q in many["questions"]:
    q["answer"] = 9
rejected("every problem is reported together, not one at a time", many, "Question 3")
big_report = good_bank("alpha_bad", n=40)
for q in big_report["questions"]:
    q["answer"] = 9
rejected("a very long list of problems is cut short and says so", big_report, "more problem")
check("nothing was ever written outside a school's data folder by any of those",
      # numbering.json is the school's own numbering rules (a separate, legitimate feature),
      # not a bank file, and every school has one from the moment it is created.
      not [n for n in os.listdir(os.path.join(TMP, "tenants", "alpha"))
           if n.endswith(".json") and n != "numbering.json"]
      and not os.path.exists(os.path.join(TMP, "tenants", "x.json"))
      and not os.path.exists(os.path.join(TMP, "x.json")) and not os.path.exists(os.path.join(TMP, "tenants", "alpha", "x.json")))
check("…and no stray temporary files were left behind",
      not [n for n in os.listdir(data_folder("alpha")) if n.endswith(".tmp")])

# ============================================================ replacing needs an explicit yes
with open(os.path.join(data_folder("alpha"), "alpha_extra.json"), "rb") as f:
    original = f.read()
before_rows = exam_rows("alpha")["alpha_extra"].name
r = post_import(c_a, u_a, good_bank("alpha_extra", "A Different Bank", n=4))
body = text_of(r)
check("importing an id that already exists is refused unless the administrator confirms",
      r.status_code == 400 and "already here" in body and "Replace it" in body)
check("…and the existing bank is byte-for-byte unchanged",
      open(os.path.join(data_folder("alpha"), "alpha_extra.json"), "rb").read() == original)
r = post_import(c_a, u_a, good_bank("alpha_extra", "A Different Bank", n=4), replace=True)
check("with 'Replace it' ticked, the bank is replaced", r.status_code == 302
      and 'Replaced "A Different Bank" with 4 questions' in text_of(c_a.get(r.headers["Location"], base_url=u_a)))
saved = json.load(open(os.path.join(data_folder("alpha"), "alpha_extra.json"), encoding="utf-8"))
check("…in the same single file", saved["name"] == "A Different Bank" and len(saved["questions"]) == 4
      and bank_files("alpha").count("alpha_extra.json") == 1 and len(bank_files("alpha")) == 8)
check("…and its examinations row follows", exam_rows("alpha")["alpha_extra"].name == "A Different Bank"
      and exam_rows("alpha")["alpha_extra"].question_count == 4)

# An older school's bank may live in a file with a different name; replacing it must not leave two.
legacy = {"id": "legacy_bank", "name": "Older Bank", "duration_seconds": 600,
          "questions": good_bank()["questions"]}
with open(os.path.join(data_folder("alpha"), "Old_Style_Name.json"), "w", encoding="utf-8") as f:
    json.dump(legacy, f)
r = post_import(c_a, u_a, good_bank("legacy_bank", "Newer Bank"), replace=True)
check("a bank kept in a file with another name is replaced in that file, not duplicated",
      r.status_code == 302 and "legacy_bank.json" not in bank_files("alpha")
      and json.load(open(os.path.join(data_folder("alpha"), "Old_Style_Name.json"), encoding="utf-8"))["name"] == "Newer Bank")

with open(os.path.join(data_folder("alpha"), "collide.json"), "w", encoding="utf-8") as f:
    f.write("not a bank, but somebody's file")
r = post_import(c_a, u_a, good_bank("collide", "Collider"))
check("a file that is already in the folder is never overwritten by a new bank",
      r.status_code == 400 and open(os.path.join(data_folder("alpha"), "collide.json"), encoding="utf-8").read()
      == "not a bank, but somebody's file", str(r.status_code))
os.remove(os.path.join(data_folder("alpha"), "collide.json"))

in_school("alpha", lambda: A._lock_resource("bank", "alpha_extra", "under review", ids["alpha"]))
r = post_import(c_a, u_a, good_bank("alpha_extra", "Sneaky Change", n=2), replace=True)
check("a bank that School Admin has locked cannot be replaced",
      r.status_code == 400 and "locked" in text_of(r).lower()
      and json.load(open(os.path.join(data_folder("alpha"), "alpha_extra.json"), encoding="utf-8"))["name"] == "A Different Bank")
in_school("alpha", lambda: A._unlock_resource("bank", "alpha_extra", ids["alpha"]))

# ======================================================================== who may import
def add_clerk():
    def go():
        ordinary = A.db.session.scalars(sa.select(A.AdminType).where(A.AdminType.name == "Ordinary Admin")).first()
        A.db.session.add(A.Admin(username="clerk", display_name="Clerk",
                                 password_hash=generate_password_hash("clerk-password-123"),
                                 admin_type_id=ordinary.id, active=1, password_must_change=0,
                                 created_at="2026-01-01T00:00:00+00:00"))
        A.db.session.commit()
        return ordinary.id
    return in_school("alpha", go)


def grant(role_id, code):
    def go():
        perm = A.db.session.scalars(sa.select(A.Permission).where(A.Permission.code == code)).first()
        A.db.session.add(A.AdminTypePermission(admin_type_id=role_id, permission_id=perm.id,
                                               granted_at="2026-01-01T00:00:00+00:00"))
        A.db.session.commit()
    in_school("alpha", go)


role_id = add_clerk()
c_c, u_c, r = sign_in("alpha", "clerk", "clerk-password-123")
with c_c.session_transaction(base_url=u_c) as sess:
    sess["_csrf_token"] = "t" * 32
files_before = folder_state("alpha")
r = c_c.get("/admin/banks/import", base_url=u_c)
check("an administrator without the permission cannot open the import page",
      r.status_code == 403 and "Import bank" not in text_of(r), str(r.status_code))
r = post_import(c_c, u_c, good_bank("clerk_bank"), token="t" * 32)
check("…nor import, even with a valid form token", r.status_code == 403 and folder_state("alpha") == files_before, str(r.status_code))
check("…and the bank list does not offer the import button to them",
      "Import from file" not in text_of(c_c.get("/admin/banks", base_url=u_c)))

anon, u_anon = client("alpha.portal.test")
check("a signed-out visitor is sent to sign in, for the page",
      anon.get("/admin/banks/import", base_url=u_anon).status_code == 302
      and "/login" in anon.get("/admin/banks/import", base_url=u_anon).headers["Location"])
r = anon.post("/admin/banks/import", data={"bank_file": (io.BytesIO(as_bytes(good_bank("anon_bank"))), "b.json"),
                                          "_csrf_token": "x"}, base_url=u_anon, content_type="multipart/form-data")
check("…and for the import itself, which writes nothing",
      r.status_code in (302, 403) and "anon_bank.json" not in bank_files("alpha") and folder_state("alpha") == files_before,
      str(r.status_code))
check("the import page does not exist on the platform host",
      c_pl.get("/admin/banks/import", base_url=u_pl).status_code == 404)

grant(role_id, "question_banks.create")
r = c_c.get("/admin/banks/import", base_url=u_c)
check("once a role is granted question_banks.create, its holders can open the page",
      r.status_code == 200 and "Import bank" in text_of(r))
r = post_import(c_c, u_c, good_bank("clerk_bank", "Clerk's Bank"), token="t" * 32)
check("…and import a new bank", r.status_code == 302 and "clerk_bank.json" in bank_files("alpha"), str(r.status_code))
files_before = folder_state("alpha")
r = post_import(c_c, u_c, good_bank("clerk_bank", "Clerk Overwrites", n=2), replace=True, token="t" * 32)
check("…but replacing a bank needs question_banks.edit as well",
      r.status_code == 403 and folder_state("alpha") == files_before, str(r.status_code))
grant(role_id, "question_banks.edit")
r = post_import(c_c, u_c, good_bank("clerk_bank", "Clerk Overwrites", n=2), replace=True, token="t" * 32)
check("…which then allows it", r.status_code == 302
      and json.load(open(os.path.join(data_folder("alpha"), "clerk_bank.json"), encoding="utf-8"))["name"] == "Clerk Overwrites")

files_before = folder_state("alpha")
r = c_a.post("/admin/banks/import", data={"bank_file": (io.BytesIO(as_bytes(good_bank("nocsrf"))), "b.json")},
             base_url=u_a, content_type="multipart/form-data")
check("an import without the form's CSRF token is refused", r.status_code == 403 and folder_state("alpha") == files_before)
r = post_import(c_a, u_a, good_bank("wrongcsrf"), token="not-the-token")
check("…and so is one with a wrong token", r.status_code == 403 and folder_state("alpha") == files_before)

# ----------------------------------------------------------------------------- the audit trail
def audit_rows(action):
    def go():
        return [(a.username_snapshot, a.target_id, json.loads(a.details or "{}"))
                for a in A.db.session.scalars(sa.select(AuditLog).where(AuditLog.action == action)).all()]
    return in_school("alpha", go)


imported = audit_rows("question_bank_imported")
replaced = audit_rows("question_bank_replaced")
check("each import is in the school's audit log, with who, which bank and how many questions",
      ("alpha_admin", "alpha_extra", {"name": "Extra Practice Bank", "questions": 3, "file": "bank.json"}) in imported
      and ("clerk", "clerk_bank", {"name": "Clerk's Bank", "questions": 3, "file": "bank.json"}) in imported, str(imported))
check("…and each replacement is logged as one", any(u == "alpha_admin" and t == "alpha_extra" for u, t, _ in replaced)
      and any(u == "clerk" and t == "clerk_bank" for u, t, _ in replaced), str(replaced))
denied = in_school("alpha", lambda: A.db.session.scalars(sa.select(AuditLog).where(
    AuditLog.action == "authorization_denied", AuditLog.username_snapshot == "clerk")).all())
check("a refused attempt is logged too", any("question_banks.create" in (d.details or "") for d in denied))
check("no other school's audit log has alpha's imports",
      in_school("gamma", lambda: [a for a in A.db.session.scalars(sa.select(AuditLog).where(
          AuditLog.target_id == "clerk_bank")).all()]) == [])
check("nothing in a school's audit log or files is another school's",
      "alpha_admin" not in json.dumps(in_school("gamma", lambda: [a.username_snapshot for a in A.db.session.scalars(sa.select(AuditLog)).all()])))

# ==================================================== a brand-new school can run an entrance exam
candidate_form = {"candidate_name": "Ada Obi", "target_class": "JSS 1", "school_attended": "Sunrise Primary",
                  "parent_guardian_name": "Mrs Obi", "parent_guardian_relationship": "Mother",
                  "primary_mobile": "08030000001"}


def register(c, u, **over):
    token = csrf(c, u, "/admin/candidates/new")
    return c.post("/admin/candidates/new", data={**candidate_form, **over, "_csrf_token": token}, base_url=u)


def candidate_count(slug):
    return in_school(slug, lambda: A.db.session.scalar(sa.select(sa.func.count()).select_from(Candidate)))


def configs(slug):
    def go():
        return [(c.entry_group, c.subject, c.bank_id, c.questions_to_serve, c.active)
                for c in A.db.session.scalars(sa.select(EntranceBankConfig).order_by(EntranceBankConfig.id)).all()]
    return in_school(slug, go)


current = in_school("alpha", lambda: A.db.session.scalars(sa.select(AcademicSession).where(
    AcademicSession.is_current == 1)).first().name)
check("a brand-new school already has a current academic session", bool(current), str(current))
check("…but no entrance paper is configured yet", configs("alpha") == [])

r = register(c_a, u_a)
check("before any paper is set up, registering a candidate is refused, and says why",
      r.status_code == 200 and "not available" in text_of(r) and candidate_count("alpha") == 0)

cfg_page = text_of(c_a.get("/admin/entrance-config", base_url=u_a))
check("the Exam Configuration page offers to set up the standard papers, for the current session",
      "Set up the standard entrance papers" in cfg_page and current in cfg_page and "6 of 6 still to set up" in cfg_page)
check("…and its form posts to the address that saves a configuration",
      'action="/admin/entrance-config/save"' in cfg_page and 'name="session_id"' in cfg_page and 'name="entry_group"' in cfg_page)

files_before = folder_state("alpha")
token = csrf(c_a, u_a, "/admin/entrance-config")
r = c_a.post("/admin/entrance-config/standard", data={"_csrf_token": token}, base_url=u_a)
check("one click sets up the standard papers", r.status_code == 302)
landing = text_of(c_a.get("/admin/entrance-config", base_url=u_a))
check("…and says what it did", "6 standard entrance papers set up and live" in landing)
check("…six live configurations, one for each class and subject, using the school's own banks",
      sorted((g, s) for g, s, _, _, a in configs("alpha") if a == 1) ==
      sorted((g, s) for g in ("year7", "year10") for s in ("mathematics", "english", "general_knowledge"))
      and all(bank_id.startswith("starter_") for _, _, bank_id, _, _ in configs("alpha")), str(configs("alpha")))
check("…each serving a number of questions that gives a clean 100 marks",
      all(_valid_question_configuration(n)[0] for *_, n, _ in configs("alpha")), str(configs("alpha")))
check("…and the papers now show as live on the page", "Live" in landing and "Set up the standard entrance papers" not in landing)
check("setting up the papers does not touch the bank files", folder_state("alpha") == files_before)
r = c_a.post("/admin/entrance-config/standard", data={"_csrf_token": csrf(c_a, u_a, "/admin/entrance-config")}, base_url=u_a)
check("running it again changes nothing", r.status_code == 302 and len(configs("alpha")) == 6
      and "Nothing changed" in text_of(c_a.get("/admin/entrance-config", base_url=u_a)))
check("the set-up is in the audit log", len(audit_rows("entrance_standard_papers_set_up")) == 2)

r = register(c_a, u_a)
body = text_of(r)
codes = re.findall(r'class="credential-code">([^<]+)<', body)
check("the school can now register an entrance candidate using only the standard banks",
      r.status_code == 200 and "Registered Successfully" in body and len(codes) == 2 and candidate_count("alpha") == 1,
      body[:200] if len(codes) != 2 else "")
r2 = register(c_a, u_a, candidate_name="Chidi Eze", target_class="SSS 1")
check("…for SSS 1 as well", "Registered Successfully" in text_of(r2) and candidate_count("alpha") == 2)

code, password = codes
c_cand, u_cand, r = sign_in("alpha", code, password, entrance_area=False)
check("the candidate signs in", r.status_code == 302 and "/candidate/dashboard" in r.headers["Location"], r.headers.get("Location", ""))
dash = text_of(c_cand.get("/candidate/dashboard", base_url=u_cand))
paper_ids = re.findall(r"/candidate/papers/(\d+)/start", dash)
check("the candidate's dashboard shows three papers, named for their subject",
      len(paper_ids) == 3 and "Paper 1: Mathematics" in dash and "Paper 2: English" in dash
      and "Paper 3: General Knowledge" in dash, str(paper_ids))
dash_token = re.search(r'name="_csrf_token" value="([^"]+)"', dash).group(1)
r = c_cand.post(f"/candidate/papers/{paper_ids[0]}/start", data={"_csrf_token": dash_token}, base_url=u_cand)
check("the candidate starts the Mathematics paper", r.status_code == 302 and "/exam" in r.headers["Location"], r.headers.get("Location", ""))
exam = text_of(c_cand.get("/exam?q=1", base_url=u_cand))
served = re.search(r"Question 1 of (\d+)", exam)
check("the paper is on screen, with a question and four options",
      served is not None and 'name="option"' in exam and exam.count('name="option"') == 4, str(served))


def attempt_rows():
    def go():
        a = A.db.session.scalars(sa.select(Attempt).order_by(Attempt.id.desc())).first()
        n = A.db.session.scalar(sa.select(sa.func.count()).select_from(AttemptQuestion).where(AttemptQuestion.attempt_id == a.id))
        return a.id, a.status, a.bank_id, n
    return in_school("alpha", go)


attempt_id, status, bank_id, snapshot = attempt_rows()
check("the attempt is recorded against the standard bank and freezes the questions it served",
      status == "active" and bank_id == "starter_year7_mathematics" and served and snapshot == int(served.group(1)),
      f"{status} {bank_id} {snapshot}")
qid = in_school("alpha", lambda: A.db.session.scalars(sa.select(AttemptQuestion).where(
    AttemptQuestion.attempt_id == attempt_id).order_by(AttemptQuestion.question_order)).first().question_id)
exam_token = re.search(r'id="csrfToken" value="([^"]+)"', exam).group(1)
r = c_cand.post("/answer", data={"question_id": qid, "option_index": 1, "_csrf_token": exam_token}, base_url=u_cand)
check("the candidate can answer a question", r.status_code == 200 and r.get_json().get("ok") is True)
r = c_cand.post("/submit", data={"_csrf_token": exam_token}, base_url=u_cand)
check("…and submit the paper", r.status_code == 302 and "/candidate/dashboard" in r.headers["Location"], r.headers.get("Location", ""))


def graded():
    def go():
        a = A.db.session.get(Attempt, attempt_id)
        return a.status, a.max_score
    return in_school("alpha", go)


status, max_score = graded()
check("the paper is graded out of 100", status == "submitted" and max_score == 100, f"{status} {max_score}")
dash = text_of(c_cand.get("/candidate/dashboard", base_url=u_cand))
check("the dashboard then shows the paper as completed", "Completed" in dash)

# ---- the same steps, another school: only its own banks and choices apply
check("gamma (which also has the standard banks) is not affected by alpha's set-up",
      configs("gamma") == [] and candidate_count("gamma") == 0)
files_gamma = folder_state("gamma")
# A school's own choice is never overridden: give gamma a Year 7 Mathematics paper first.
gamma_session = in_school("gamma", lambda: A.db.session.scalars(sa.select(AcademicSession).where(AcademicSession.is_current == 1)).first().id)
token = csrf(c_g, u_g, "/admin/entrance-config")
r = c_g.post("/admin/entrance-config/save", data={
    "bank_id": "starter_year7_mathematics", "entry_group": "year7", "subject": "mathematics",
    "session_id": str(gamma_session), "questions_to_serve": "20", "_csrf_token": token}, base_url=u_g)
check("a school can still set up a paper itself, with the redesigned page's form",
      r.status_code == 302 and configs("gamma") == [("year7", "mathematics", "starter_year7_mathematics", 20, 0)],
      f"{r.status_code} {configs('gamma')}")
r = c_g.post("/admin/entrance-config/standard", data={"_csrf_token": csrf(c_g, u_g, "/admin/entrance-config")}, base_url=u_g)
landing = text_of(c_g.get("/admin/entrance-config", base_url=u_g))
check("the standard set-up skips a paper the school configured itself, leaving it exactly as it was",
      "5 standard entrance papers set up" in landing and "already had a configuration of your own" in landing
      and ("year7", "mathematics", "starter_year7_mathematics", 20, 0) in configs("gamma") and len(configs("gamma")) == 6,
      str(configs("gamma")))
check("…and the school can activate its own paper from the page",
      c_g.post(f"/admin/entrance-config/{in_school('gamma', lambda: A.db.session.scalars(sa.select(EntranceBankConfig.id).where(EntranceBankConfig.subject == 'mathematics', EntranceBankConfig.entry_group == 'year7')).first())}/activate",
               data={"_csrf_token": csrf(c_g, u_g, "/admin/entrance-config")}, base_url=u_g).status_code == 302
      and ("year7", "mathematics", "starter_year7_mathematics", 20, 1) in configs("gamma"))

# The school that opted out has nothing to set up until it brings a bank.
r = c_b.post("/admin/entrance-config/standard", data={"_csrf_token": csrf(c_b, u_b, "/admin/entrance-config")}, base_url=u_b)
check("a school without the standard banks is told so, and nothing is created",
      "none of the standard question banks" in text_of(c_b.get("/admin/entrance-config", base_url=u_b))
      and configs("beta") == [])
r = register(c_b, u_b)
check("…and it cannot register candidates until it has papers, with a message saying so",
      "not available" in text_of(r) and candidate_count("beta") == 0)

# ---- a replacement that would break a live paper is refused
files_before = folder_state("alpha")
r = post_import(c_a, u_a, good_bank("starter_year7_mathematics", "Tiny Replacement", n=5), replace=True)
check("a bank behind a live entrance paper cannot be replaced by one too small to serve it",
      r.status_code == 400 and "asks for" in text_of(r) and folder_state("alpha") == files_before, str(r.status_code))
r = post_import(c_a, u_a, good_bank("starter_year7_mathematics", "Bigger Replacement", n=30), replace=True)
check("…but one that still has enough questions replaces it",
      r.status_code == 302 and exam_rows("alpha")["starter_year7_mathematics"].question_count == 30)

# ---- everything a school's banks need is inside its own folder
check("every bank file is directly inside the school's own data folder, so copying that folder moves them",
      all(os.path.dirname(os.path.join(data_folder("alpha"), n)) == data_folder("alpha") for n in bank_files("alpha")))

# ---- the console page a school's creation shows
listing = pv.tenant_folder_listing(info_for("alpha"))
check("the platform console shows the school's data folder holding the banks",
      any(e["name"] == "data/" and not e["detail"].startswith("0 ") for e in listing["entries"]), str(listing["entries"]))
check("the platform's own starter files are still exactly as they were",
      tree_fingerprint(B.STARTER_DIR) == STARTER_BEFORE)

dispose_engines()
DROP_TEST_DATABASES()
shutil.rmtree(TMP, ignore_errors=True)
print()
failed = [x for x in results if not x[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
