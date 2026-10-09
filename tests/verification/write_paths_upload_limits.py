"""Uploaded pictures: the limit is told beforehand, and every refusal says why, on PostgreSQL.

The owner tried to give the School Admin a profile picture larger than five megabytes and could not
tell why it did not go through: the limit was never printed anywhere. This suite proves that is fixed
everywhere a picture (or attachment) can be uploaded, not only for that one page, because every upload
box shares one rule (core/uploads.py) and one script (static/upload-limit.js).

It drives the real application over HTTP with real CSRF tokens against a throwaway school, and for each
upload box (the School Admin's photo, an ordinary staff administrator's photo and a new one, a student
(new and edited), a candidate, an entrance question image (new and edited), a school test question image
(new and edited), the receipt signature, a staff member's report-card signature, the school's own
branding logo and sign-in photographs, the platform console's school logo, and a new school's logo)
it proves, in plain words:

Before the person submits
* the page says what the box takes, with the real limit ("PNG, JPG, GIF or WEBP, up to 5 MB."), and
  carries the limit in a data attribute and loads static/upload-limit.js; the old vague sentence is gone;
* the script itself is plain, dependency-free and Content-Security-Policy safe, and (run in a real
  browser when Chrome is installed) refuses a file that is too large or of the wrong type right beside
  the box, naming the file and both sizes, clears the selection, and shows the name and size of a good one.

After they submit
* a picture just under the limit is accepted and stored;
* a picture over the limit is refused with the specific sentence naming the file, its size and the
  limit ("photo.jpg is 5.5 MB. The limit is 5 MB. Choose a smaller picture, or reduce this one first.")
  on the same form, and nothing is stored;
* a text file with an image name is refused as "not a valid image", a PDF as "not a picture we can use";
* a whole submission over the request limit (8 MB; the branding pages, which take a logo and a set of
  photographs, have their own larger limit) is answered with a friendly explanation naming the limit
  and what was sent, back on the form the person was on, never a bare error page; from anywhere the
  server cannot safely send them back to it is a friendly page with the same words, and a script asking
  for JSON gets the words as JSON;
* the limit is one setting: changing BRIGHTSTARS_MAX_UPLOAD_BYTES changes the hint, the attribute and the
  refusal together.

Run:  python tests/verification/write_paths_upload_limits.py
"""
import atexit
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from urllib.parse import urlsplit

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # a failure may quote text from a page

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_uploadlimits_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('uploadlimits')
atexit.register(DROP_TEST_DATABASES)  # even if this script stops half way, its databases go
atexit.register(shutil.rmtree, TMP, True)
os.environ.update({
    "BRIGHTSTARS_TENANTS_DIR": os.path.join(TMP, "tenants"),
    "BRIGHTSTARS_PLATFORM_HOSTS": "platform.test",
    "BRIGHTSTARS_PORTAL_DOMAIN": "portal.test",
    "BRIGHTSTARS_REGISTRY_CACHE_SECONDS": "0",
    "BRIGHTSTARS_SECRET": "x" * 40,
    "BRIGHTSTARS_ENV": "development",
})
# The defaults are what is being tested: five megabytes a picture, eight a submission.
os.environ.pop("BRIGHTSTARS_MAX_UPLOAD_BYTES", None)
os.environ.pop("BRIGHTSTARS_MAX_REQUEST_BYTES", None)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
os.chdir(ROOT)

import sqlalchemy as sa  # noqa: E402
from PIL import Image  # noqa: E402  (already installed with reportlab)

import app as A  # noqa: E402
from control_plane import provisioning as pv  # noqa: E402
from control_plane.context import tenant_context  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import engine_for  # noqa: E402
from core import theme, uploads  # noqa: E402
from core.entrance import load_banks  # noqa: E402

results = []
PL = "http://platform.test"
ALPHA = "http://alpha.portal.test"
MB = 1024 * 1024


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)


# ------------------------------------------------------------------ small helpers
def csrf_from(page):
    found = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', page)
    return found.group(1) if found else ""


def png_of_size(size):
    """A real PNG of exactly ``size`` bytes (a decoder ignores what follows the picture)."""
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (30, 120, 200)).save(buf, "PNG")
    data = buf.getvalue()
    return data + b"\0" * max(0, size - len(data))


NOT_A_PICTURE = b"this is not a picture at all, it is only text"


class Person:
    """One person at one address: a browser with its own cookies."""

    count = [0]

    def __init__(self, base, form_page="/admin/password"):
        self.base, self.form_page, self.client = base, form_page, A.app.test_client()
        Person.count[0] += 1
        self.addr = f"10.60.{Person.count[0]}.1"  # sign-in limits are per address

    def get(self, path, **kw):
        return self.client.get(path, base_url=self.base, environ_base={"REMOTE_ADDR": self.addr}, **kw)

    def text(self, path):
        return html.unescape(self.get(path).get_data(as_text=True))

    def post(self, path, data=None, page=None, files=None, referer=True, headers=None):
        """Submit a form the way a browser does: the token comes from a real page, and the browser says
        which page the form was on."""
        page = page or self.form_page
        data = dict(data or {})
        data["_csrf_token"] = csrf_from(self.get(page).get_data(as_text=True))
        for key, items in (files or {}).items():
            items = items if isinstance(items, list) else [items]
            data[key] = [(io.BytesIO(content), filename) for filename, content in items]
            if len(data[key]) == 1:
                data[key] = data[key][0]
        head = dict(headers or {})
        if referer and "Referer" not in head:
            head["Referer"] = self.base + page
        return self.client.post(path, data=data, base_url=self.base, headers=head,
                                environ_base={"REMOTE_ADDR": self.addr},
                                content_type="multipart/form-data" if files else None)

    def seen(self, response):
        """Everything the person is shown after this submission: the page itself, or the page they are
        sent on to (a message left for the next page is only read once that page is opened)."""
        if response.status_code in (301, 302, 303, 307, 308):
            target = urlsplit(response.headers["Location"])
            return self.text(target.path + ("?" + target.query if target.query else ""))
        return html.unescape(response.get_data(as_text=True))

    def session(self):
        with self.client.session_transaction(base_url=self.base) as sess:
            return dict(sess)


def info_for(slug):
    with platform_session() as s:
        tenant = get_tenant(s, slug)
        return to_info(tenant) if tenant else None


class School:
    def __init__(self, code, base):
        self.code, self.base, self.info = code, base, info_for(code)

    def sql(self, statement, **params):
        with engine_for(self.info).begin() as conn:
            result = conn.execute(sa.text(statement), params)
            return result.fetchall() if result.returns_rows else None

    def one(self, statement, **params):
        rows = self.sql(statement, **params)
        return rows[0][0] if rows else None

    def run(self, fn):
        with A.app.app_context(), tenant_context(self.info):
            return fn()


def read_js_free_of_hazards(source):
    """The upload script must be plain: nothing that a Content-Security-Policy or a review would flag."""
    banned = ("eval(", "new Function", "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
              "setTimeout(\"", "setInterval(\"", "XMLHttpRequest", "fetch(", "import(", "http://", "https://")
    return [word for word in banned if word in source]


# =============================================================== 0. the words themselves, no browser needed
check("five megabytes is the picture limit and eight the submission limit, by default",
      uploads.image_limit_bytes() == 5 * MB and uploads.DEFAULT_REQUEST_LIMIT_BYTES == 8 * MB
      and A.app.config["MAX_CONTENT_LENGTH"] == 8 * MB)
check("a size is written for people", [uploads.format_bytes(n) for n in (900, 840 * 1024, 5 * MB, int(7.2 * MB), 3 * 1024 * MB)]
      == ["900 bytes", "840 KB", "5 MB", "7.2 MB", "3 GB"])
check("a limit is written as 5 MB, and a size just over it never looks equal to it",
      uploads.format_limit(5 * MB) == "5 MB" and uploads.format_size_over(5 * MB + 1, 5 * MB) == "5.01 MB"
      and uploads.format_size_over(int(7.2 * MB), 5 * MB) == "7.2 MB")
check("the sentence for a too-large picture names the file, its size and the limit",
      uploads.too_large_message('"photo.jpg"', int(7.2 * MB), 5 * MB)
      == '"photo.jpg" is 7.2 MB. The limit is 5 MB. Choose a smaller picture, or reduce this one first.')
check("the sentence for a too-large submission names the limit and what was sent",
      "larger than the limit of 8 MB" in uploads.request_too_large_message(9 * MB, 8 * MB)
      and "9 MB" in uploads.request_too_large_message(9 * MB, 8 * MB))

script = open(os.path.join(ROOT, "static", "upload-limit.js"), encoding="utf-8").read()
check("the upload script is plain, dependency-free and CSP-safe (no eval, no HTML strings, no other origin)",
      not read_js_free_of_hazards(script), str(read_js_free_of_hazards(script)))

# =============================================================== 1. a school, and the people who work in it
pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
console = Person(PL, "/platform/login")
console.post("/platform/login", {"username": "ops", "password": "a-long-platform-password"}, page="/platform/login")
console.post("/platform/schools/new", {"name": "Alpha School", "code": "alpha", "starter_banks": "1"},
             page="/platform/schools/new", files={"logo": ("logo.png", png_of_size(2000))})
alpha = School("alpha", ALPHA)
check("(set-up) the school exists", alpha.one("SELECT count(*) FROM schools") == 1)


def operator_in(workspace):
    """The platform operator enters the school with a single-use ticket and is its School Admin there."""
    r = console.post("/platform/schools/alpha/enter", page="/platform/schools/alpha")
    person = Person(ALPHA)
    person.get(r.headers["Location"][len(ALPHA):])
    person.get(f"/admin/workspace/{workspace}")
    return person


op = operator_in("school")          # the School Admin, in the school workspace
op_ent = operator_in("entrance")    # the same person, in the entrance-examination workspace
SA = op.session()["admin_id"]
check("(set-up) the operator is the School Admin", alpha.one(
    "SELECT t.name FROM admins a JOIN admin_types t ON t.id = a.admin_type_id WHERE a.id = :i", i=SA) == "School Admin")

ADMIN_ROOT = "/admin/administration"
op.post(f"{ADMIN_ROOT}/roles/new", {"name": "Records Clerk", "description": "Keeps student records",
                                    "permissions": [r[0] for r in alpha.sql(
                                        "SELECT id FROM permissions WHERE code = ANY(:c)",
                                        c=["admin.access", "school.view", "school.students.view"])]},
        page=f"{ADMIN_ROOT}/roles/new")
ROLE = alpha.one("SELECT id FROM admin_types WHERE name = 'Records Clerk'")
op.post(f"{ADMIN_ROOT}/admins/new", {"username": "clerk1", "display_name": "Clerk One", "admin_type_ids": [ROLE],
                                     "scope_type": "global"}, page=f"{ADMIN_ROOT}/admins/new")
CLERK = alpha.one("SELECT id FROM admins WHERE username = 'clerk1'")
check("(set-up) an ordinary staff administrator exists", CLERK is not None and CLERK != SA)

JSS1 = alpha.one("SELECT id FROM school_classes WHERE name = 'JSS 1'")
CURRENT = alpha.one("SELECT id FROM academic_sessions WHERE is_current = 1")
op.post("/admin/school/subjects/new", {"name": "Mathematics", "code": "MTH", "class_ids": [JSS1]},
        page="/admin/school/subjects/new")
MATHS = alpha.one("SELECT id FROM school_subjects WHERE name = 'Mathematics'")
op.post("/admin/school/tests/new", {"title": "Upload test", "instructions": "Answer all.", "class_id": JSS1,
                                    "subject_id": MATHS, "session_id": CURRENT, "term": "First Term",
                                    "duration_minutes": "20"}, page="/admin/school/tests/new")
TEST = alpha.one("SELECT id FROM school_assessments WHERE title = 'Upload test'")
QUESTION = {"question_text": "What is 2 + 2?", "instruction": "", "option_a": "3", "option_b": "4", "option_c": "5",
            "option_d": "6", "correct_option": "1", "points": "1"}
op.post(f"/admin/school/assessments/{TEST}/questions/new", QUESTION, page=f"/admin/school/assessments/{TEST}")
TEST_Q = alpha.one("SELECT id FROM school_questions WHERE assessment_id = :a", a=TEST)

STATE_ROW = {"gender": "Female", "date_of_birth": "2014-05-01", "state_of_origin": "Lagos", "blood_group": "O+",
             "genotype": "AA"}


def student_form(first, last):
    return {**STATE_ROW, "first_name": first, "middle_name": "", "last_name": last, "class_id": JSS1,
            "guardian_name": f"{last} Family", "guardian_phone": "08030000000", "guardian_email": f"{last.lower()}@family.test"}


op.post("/admin/school/students/new", student_form("Ada", "Eze"), page="/admin/school/students/new")
STUDENT = alpha.one("SELECT id FROM students WHERE first_name = 'Ada'")
op.post("/admin/school/assignments/new", {
    "title": "Fractions homework", "instructions": "Do all questions.", "class_id": JSS1, "subject_id": MATHS,
    "session_id": CURRENT, "term": "First Term", "assignment_type": "quiz", "timing_mode": "overall",
    "time_limit_minutes": "30", "due_date": "2026-12-01", "date_given": "2026-10-01", "student_ids": [STUDENT]},
    page="/admin/school/assignments/new")
QUIZ = alpha.one("SELECT id FROM school_assignments WHERE title = 'Fractions homework'")

op_ent.post("/admin/entrance-config/standard", {}, page="/admin/candidates/new")
op_ent.post("/admin/banks/new", {"name": "Upload Drill", "level": "JSS 1", "duration_minutes": "30", "version": "1.0"},
            page="/admin/banks/new")
BANK = "upload_drill_jss_1"
BANK_Q = {"text": "What is 1 + 1?", "instruction": "", "option0": "1", "option1": "2", "option2": "3", "option3": "4",
          "answer": "1", "points": "1"}
op_ent.post(f"/admin/banks/{BANK}/questions/new", BANK_Q, page=f"/admin/banks/{BANK}/questions/new")
bank_questions = lambda: alpha.run(lambda: load_banks()[BANK]["questions"])  # noqa: E731
BANK_QID = bank_questions()[0]["id"] if bank_questions() else None
check("(set-up) a student, a homework quiz, a test question and a bank question exist to be edited",
      STUDENT is not None and QUIZ is not None and TEST_Q is not None and BANK_QID is not None)

counter = [0]


def unique(prefix):
    counter[0] += 1
    return f"{prefix}{counter[0]}"


def clerk_edit_data():
    return {"display_name": "Clerk One", "admin_type_ids": [ROLE], "scope_type": "global"}


def student_edit_data():
    return {**student_form("Ada", "Eze"), "admission_no": alpha.one("SELECT admission_no FROM students WHERE id = :i", i=STUDENT)}


def candidate_data():
    return {"candidate_name": unique("Candidate "), "target_class": "JSS 1", "school_attended": "Sunrise Primary",
            "parent_guardian_name": "Mrs Parent", "parent_guardian_relationship": "Mother",
            "primary_mobile": "08030000001"}


def logo_setting():
    return alpha.one("SELECT setting_value FROM school_public_settings WHERE setting_key = :k", k="school_logo")


def gallery_setting():
    return alpha.one("SELECT setting_value FROM school_public_settings WHERE setting_key = :k", k=theme.GALLERY_KEY)


def bank_question_image():
    return next((q.get("image_path") for q in bank_questions() if q["id"] == BANK_QID), None)


# ------------------------------------------------------------------ the upload boxes
# Each: where the form is, how to submit it with a file, and something that changes when a file is taken.
BRANDING_LIMIT = uploads.request_limit_bytes("branding")   # a logo and a set of photographs travel together
CASES = [
    dict(name="the School Admin's profile photo", who=op, page=f"{ADMIN_ROOT}/admins/{SA}/edit", field="photo",
         action=f"{ADMIN_ROOT}/admins/{SA}/edit", data=lambda: {"display_name": "School Admin"},
         stored=lambda: alpha.one("SELECT photo_path FROM admins WHERE id = :i", i=SA)),
    dict(name="an ordinary staff administrator's profile photo", who=op, page=f"{ADMIN_ROOT}/admins/{CLERK}/edit",
         field="photo", action=f"{ADMIN_ROOT}/admins/{CLERK}/edit", data=clerk_edit_data,
         stored=lambda: alpha.one("SELECT photo_path FROM admins WHERE id = :i", i=CLERK)),
    dict(name="a new staff administrator's photo", who=op, page=f"{ADMIN_ROOT}/admins/new", field="photo",
         action=f"{ADMIN_ROOT}/admins/new",
         data=lambda: {"username": unique("staff"), "display_name": "New Staff", "admin_type_ids": [ROLE],
                       "scope_type": "global"},
         stored=lambda: alpha.one("SELECT count(*) FROM admins")),
    dict(name="a new student's photo", who=op, page="/admin/school/students/new", field="photo",
         action="/admin/school/students/new", data=lambda: student_form(unique("Stu"), "Photo"),
         stored=lambda: alpha.one("SELECT count(*) FROM students")),
    dict(name="an edited student's photo", who=op, page=f"/admin/school/students/{STUDENT}/edit", field="photo",
         action=f"/admin/school/students/{STUDENT}/edit", data=student_edit_data,
         stored=lambda: alpha.one("SELECT photo_path FROM students WHERE id = :i", i=STUDENT)),
    dict(name="a candidate's photo", who=op_ent, page="/admin/candidates/new", field="photo",
         action="/admin/candidates/new", data=candidate_data,
         stored=lambda: alpha.one("SELECT count(*) FROM candidates")),
    dict(name="an entrance question's image (new)", who=op_ent, page=f"/admin/banks/{BANK}/questions/new",
         field="image", action=f"/admin/banks/{BANK}/questions/new", data=lambda: dict(BANK_Q, text=unique("Q")),
         stored=lambda: len(bank_questions())),
    dict(name="an entrance question's image (edited)", who=op_ent, page=f"/admin/banks/{BANK}/questions/{BANK_QID}/edit",
         field="image", action=f"/admin/banks/{BANK}/questions/{BANK_QID}/edit", data=lambda: BANK_Q,
         stored=bank_question_image),
    dict(name="a school test question's image (new)", who=op, page=f"/admin/school/assessments/{TEST}", field="image",
         action=f"/admin/school/assessments/{TEST}/questions/new", data=lambda: dict(QUESTION, question_text=unique("Q")),
         stored=lambda: alpha.one("SELECT count(*) FROM school_questions WHERE assessment_id = :a", a=TEST)),
    dict(name="a school test question's image (edited)", who=op,
         page=f"/admin/school/assessments/{TEST}/questions/{TEST_Q}/edit", field="image",
         action=f"/admin/school/assessments/{TEST}/questions/{TEST_Q}/edit", data=lambda: QUESTION,
         stored=lambda: alpha.one("SELECT image_path FROM school_questions WHERE id = :i", i=TEST_Q)),
    dict(name="a homework quiz question's image", who=op, page=f"/admin/school/assignments/{QUIZ}", field="image",
         action=f"/admin/school/assignments/{QUIZ}/questions", data=lambda: dict(QUESTION, question_text=unique("Q")),
         stored=lambda: alpha.one("SELECT count(*) FROM assignment_questions WHERE assignment_id = :a", a=QUIZ)),
    dict(name="the receipt's authorised signature", who=op, page="/admin/finance/receipt-settings",
         field="signature_file", action="/admin/finance/receipt-settings", data=lambda: {"action": "upload"},
         stored=lambda: alpha.one("SELECT setting_value FROM school_settings WHERE setting_key = :k",
                                  k="receipt_authorised_signature")),
    dict(name="a staff member's own report-card signature", who=op, page="/admin/school/report-cards",
         field="signature_file", action="/admin/school/report-cards/my-signature", data=lambda: {"action": "upload"},
         stored=lambda: alpha.one("SELECT signature_path FROM admins WHERE id = :i", i=SA)),
    dict(name="the head's signature on report cards", who=op, page="/admin/school/report-cards/settings",
         field="signature_file", action="/admin/school/report-cards/settings", data=lambda: {"action": "upload"},
         stored=lambda: alpha.one("SELECT setting_value FROM school_settings WHERE setting_key = :k",
                                  k="report_head_signature")),
    dict(name="the school's own logo (branding page)", who=op, page="/admin/school/branding", field="logo",
         action="/admin/school/branding/save", data=lambda: {theme.PRIMARY_KEY: "#0d2b52", theme.ACCENT_KEY: "#1674b9"},
         stored=logo_setting, request_limit=BRANDING_LIMIT),
    dict(name="the school's sign-in photographs (branding page)", who=op, page="/admin/school/branding",
         field="gallery", action="/admin/school/branding/save",
         data=lambda: {theme.PRIMARY_KEY: "#0d2b52", theme.ACCENT_KEY: "#1674b9"},
         stored=gallery_setting, request_limit=BRANDING_LIMIT, multiple=True),
    dict(name="a school's logo on the platform console", who=console, page="/platform/schools/alpha", field="logo",
         action="/platform/schools/alpha/branding", data=lambda: {theme.PRIMARY_KEY: "#0d2b52", theme.ACCENT_KEY: "#1674b9"},
         stored=logo_setting, request_limit=BRANDING_LIMIT),
    dict(name="a school's sign-in photographs on the platform console", who=console, page="/platform/schools/alpha",
         field="gallery", action="/platform/schools/alpha/branding",
         data=lambda: {theme.PRIMARY_KEY: "#0d2b52", theme.ACCENT_KEY: "#1674b9"},
         stored=gallery_setting, request_limit=BRANDING_LIMIT, multiple=True),
    dict(name="a new school's logo on the platform console", who=console, page="/platform/schools/new", field="logo",
         action="/platform/schools/new", data=lambda: {"name": "Zeta School", "code": "zeta"},
         stored=lambda: 1 if info_for("zeta") else 0, request_limit=BRANDING_LIMIT, accepts=False),
]


def upload(case, filename, content, **kw):
    return case["who"].post(case["action"], case["data"](), page=case["page"], files={case["field"]: (filename, content)}, **kw)


def send(case, size):
    """A picture of ``size`` bytes, sent through the case's own form."""
    before = case["stored"]()
    r = upload(case, "picture.png", png_of_size(size))
    return before, r, case["who"].seen(r)


LIMIT_SENTENCE = re.compile(r'"picture\.png" is (?P<size>[\d.]+ MB)\. The limit is (?P<limit>[\d.]+ MB)\. '
                            r'Choose a smaller picture, or reduce this one first\.')

for case in CASES:
    name = case["name"]
    print(f"\n--- {name}")
    who, page = case["who"], case["page"]
    request_limit = case.get("request_limit", uploads.DEFAULT_REQUEST_LIMIT_BYTES)

    # ---- what the page says before anything is sent
    body = who.text(page)
    attrs = re.findall(r'<input[^>]*name="%s"[^>]*>' % re.escape(case["field"]), body)
    tag = attrs[0] if attrs else ""
    check(f"{name}: the page says what the box takes, with the real limit",
          "PNG, JPG, GIF or WEBP, up to 5 MB" in body and "platform security policy" not in body)
    check(f"{name}: the box carries the limit for the script",
          f'data-upload-limit="{5 * MB}"' in tag and 'data-upload-limit-label="5 MB"' in tag
          and f'data-upload-request-limit="{request_limit}"' in tag and "data-upload-types=" in tag, tag[:200])
    check(f"{name}: the page loads the upload script, once, from the site's own address",
          len(re.findall(r'<script src="/static/upload-limit\.js\?v=\d+" defer></script>', body)) == 1)
    check(f"{name}: there is a live place for the script's message beside the box",
          'data-upload-feedback aria-live="polite"' in body)
    if case.get("multiple"):
        check(f"{name}: several pictures are each held to the limit", "up to 5 MB each" in body)

    # ---- a picture just under the limit is accepted (where a school is not made by it)
    if case.get("accepts", True):
        before, r, seen = send(case, 5 * MB - 1024)
        after = case["stored"]()
        check(f"{name}: a picture just under 5 MB is accepted and stored", after != before, f"{r.status_code} {before!r} -> {after!r}")

    # ---- a picture over the limit says exactly why, on the same form
    before, r, seen = send(case, int(5.5 * MB))
    found = LIMIT_SENTENCE.search(seen)
    check(f"{name}: a 5.5 MB picture is refused, naming the file, its size and the limit",
          found is not None and found.group("size") == "5.5 MB" and found.group("limit") == "5 MB", seen[:200] if not found else "")
    check(f"{name}: …and nothing was stored", case["stored"]() == before)

    # ---- a text file with a picture's name, and a file of another kind
    before = case["stored"]()
    r = upload(case, "notes.jpg", NOT_A_PICTURE)
    seen = who.seen(r)
    check(f"{name}: a text file renamed .jpg is refused as not a real image",
          '"notes.jpg" does not appear to be a valid image' in seen and "real PNG, JPG, GIF or WEBP" in seen)
    check(f"{name}: …and nothing was stored (text)", case["stored"]() == before)
    before = case["stored"]()
    r = upload(case, "report.pdf", b"%PDF-1.4 not a picture")
    seen = who.seen(r)
    check(f"{name}: a PDF is refused as not a picture we can use",
          '"report.pdf" is not a picture we can use. Please choose a PNG, JPG, GIF or WEBP image.' in seen)
    check(f"{name}: …and nothing was stored (pdf)", case["stored"]() == before)

    # ---- a whole submission over the request limit: a friendly explanation on the form, never a bare error
    before = case["stored"]()
    too_big = request_limit + MB
    r = upload(case, "huge.png", png_of_size(too_big))
    limit_words = f"larger than the limit of {uploads.format_limit(request_limit)} for one submission"
    check(f"{name}: a submission over {uploads.format_limit(request_limit)} sends the person back to the form",
          r.status_code == 303 and urlsplit(r.headers["Location"]).path == page, f"{r.status_code} {r.headers.get('Location')}")
    seen = who.seen(r)
    check(f"{name}: …with the limit, what was sent, and the one-picture limit named",
          limit_words in seen and re.search(r"The file you sent is [\d.]+ (MB|GB), which is", seen) is not None
          and "A single picture can be up to 5 MB" in seen, seen[:200])
    check(f"{name}: …and nothing was stored (too big)", case["stored"]() == before)

# =============================================================== 2. a submission too large, from wherever it comes
print("\n--- a submission too large, sent from anywhere")
case = CASES[0]
big = png_of_size(9 * MB)
r = op.post(case["action"], case["data"](), page=case["page"], files={"photo": ("huge.png", big)}, referer=False)
seen = html.unescape(r.get_data(as_text=True))
check("with no address to send the person back to, the friendly page is shown (a 413, not a redirect)",
      r.status_code == 413 and "larger than the limit of 8 MB" in seen)
check("…in the school's own error page, with a plain heading and a way back",
      "That File Is Too Large" in seen and "Go Back" in seen)
check("…and it never shows a stack trace or the words of a server error",
      "Traceback" not in seen and "Request Entity Too Large" not in seen)
r = op.post(case["action"], case["data"](), page=case["page"], files={"photo": ("huge.png", big)},
            headers={"Referer": "https://evil.example/admin/administration/admins"})
check("a submission that claims to come from another website is never sent back there",
      r.status_code == 413 and "evil.example" not in (r.headers.get("Location") or ""))
r = op.post(case["action"], case["data"](), page=case["page"], files={"photo": ("huge.png", big)},
            headers={"Referer": ALPHA + "/admin/administration/messages/send"})
check("a page that cannot be opened by a plain GET is never the place to send them back to (friendly page instead)",
      r.status_code == 413 and "larger than the limit of 8 MB" in html.unescape(r.get_data(as_text=True)))
r = op.post(case["action"], case["data"](), page=case["page"], files={"photo": ("huge.png", big)},
            headers={"Referer": ALPHA + "//evil.example/admin/x"})
check("a doubled slash in the address cannot turn the way back into another website",
      r.status_code == 413 and "evil.example" not in (r.headers.get("Location") or ""))
r = op.post(case["action"], case["data"](), page=case["page"], files={"photo": ("huge.png", big)},
            headers={"X-Requested-With": "XMLHttpRequest"}, referer=False)
check("a script asking for JSON gets the same words as JSON",
      r.status_code == 413 and "larger than the limit of 8 MB" in r.get_json()["error"])
r = op.post(case["action"], case["data"](), page=case["page"], files={"photo": ("huge.png", big)})
check("the redirect carries the words and the person is told once",
      "larger than the limit of 8 MB" in op.seen(r) and "larger than the limit of 8 MB" not in op.text(case["page"]))

# the message attachment box: no picture rule, only the size of the whole submission
MESSAGES = f"{ADMIN_ROOT}/messages?with={CLERK}"   # the box appears once a conversation is open
body = op.text(MESSAGES)
check("the message attachment box tells the person how large a file may be",
      "up to 7.9 MB" in body and f'data-upload-limit="{8 * MB - 64 * 1024}"' in body and 'data-upload-kind="attachment"' in body
      and re.search(r'<script src="/static/upload-limit\.js\?v=\d+" defer></script>', body) is not None)
r = op.post(f"{ADMIN_ROOT}/messages/send", {"recipient_id": CLERK, "body": "See attached"}, page=MESSAGES,
            files={"attachment": ("report.pdf", b"%PDF-1.4 " + b"0" * (9 * MB))})
check("a message attachment over the limit sends the person back to the messages with the limit named",
      r.status_code == 303 and "larger than the limit of 8 MB" in op.seen(r))

# =============================================================== 3. one setting changes every word
print("\n--- one setting")
os.environ["BRIGHTSTARS_MAX_UPLOAD_BYTES"] = str(2 * MB)
try:
    case = CASES[0]
    body = op.text(case["page"])
    check("changing BRIGHTSTARS_MAX_UPLOAD_BYTES changes the hint and the attribute together",
          "up to 2 MB" in body and f'data-upload-limit="{2 * MB}"' in body and "up to 5 MB" not in body)
    before, r, seen = send(case, 3 * MB)
    check("…and the refusal", '"picture.png" is 3 MB. The limit is 2 MB.' in seen and case["stored"]() == before, seen[:160])
    before, r, seen = send(case, 2 * MB - 1024)
    check("…and what is now allowed", case["stored"]() != before)
finally:
    os.environ.pop("BRIGHTSTARS_MAX_UPLOAD_BYTES", None)
check("with the setting removed the limit is five megabytes again", uploads.image_limit_bytes() == 5 * MB)

# =============================================================== 4. the script, served and run in a real browser
print("\n--- the script")
r = op.get("/static/upload-limit.js")
check("the script is served from the site's own address as JavaScript",
      r.status_code == 200 and "javascript" in r.headers.get("Content-Type", "")
      and "script-src 'self'" in r.headers.get("Content-Security-Policy", ""))


def chrome():
    from core.entrance import _find_chrome
    found = _find_chrome()
    return str(found) if found else None


CHROME = chrome()
if not CHROME:
    print("SKIP the upload script in a real browser: Chrome was not found (set BRIGHTSTARS_CHROME).")
else:
    SCRIPT_URL = "file:///" + os.path.join(ROOT, "static", "upload-limit.js").replace(os.sep, "/")
    page = f"""<!doctype html><html><body><form id="f">
<div class="field"><input type="file" id="i" data-upload-kind="image" data-upload-limit="{5 * MB}" data-upload-limit-label="5 MB"
 data-upload-request-limit="{8 * MB}" data-upload-request-label="8 MB" data-upload-types="gif,jpeg,jpg,png,webp"
 data-upload-types-label="PNG, JPG, GIF or WEBP"><small>hint</small><div id="fb" data-upload-feedback aria-live="polite"></div></div>
<div class="field"><input type="file" id="j" multiple data-upload-kind="image" data-upload-limit="{5 * MB}" data-upload-limit-label="5 MB"
 data-upload-request-limit="{8 * MB}" data-upload-request-label="8 MB" data-upload-types="gif,jpeg,jpg,png,webp"
 data-upload-types-label="PNG, JPG, GIF or WEBP"><div id="fb2" data-upload-feedback></div></div>
<div class="field"><input type="file" id="k" data-upload-kind="attachment" data-upload-limit="{8 * MB - 65536}" data-upload-limit-label="7.9 MB"
 data-upload-request-limit="{8 * MB}" data-upload-request-label="8 MB" data-upload-types="pdf,txt,png"
 data-upload-types-label="an image, PDF, Word, text or Excel file"><div id="fb3" data-upload-feedback></div></div>
</form><pre id="out"></pre>
<script src="{SCRIPT_URL}"></script>
<script>
var MB = 1024 * 1024;
function pick(input, files) {{
  var dt = new DataTransfer();
  files.forEach(function (f) {{ dt.items.add(new File([new Uint8Array(f[1])], f[0])); }});
  input.files = dt.files;
  input.dispatchEvent(new Event('change', {{bubbles: true}}));
  var box = input._uploadFeedback;
  return {{msg: box.textContent, role: box.firstChild ? box.firstChild.getAttribute('role') : null,
          kept: input.files.length, invalid: input.getAttribute('aria-invalid')}};
}}
var i = document.getElementById('i'), j = document.getElementById('j'), k = document.getElementById('k');
document.getElementById('out').textContent = JSON.stringify([
  pick(i, [['photo.jpg', Math.round(7.2 * MB)]]),
  pick(i, [['notes.txt', 10]]),
  pick(i, [['ok.png', Math.round(2.1 * MB)]]),
  pick(i, [['edge.png', 5 * MB + 1]]),
  pick(j, [['a.png', 5 * MB], ['b.png', 4 * MB]]),
  pick(k, [['big.pdf', 8 * MB - 1000]]),
  pick(k, [['small.pdf', 1000]]),
  pick(k, [['run.exe', 1000]])
]);
</script></body></html>"""
    path = os.path.join(TMP, "script_check.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)
    run = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--allow-file-access-from-files", "--dump-dom",
                          "file:///" + path.replace(os.sep, "/")], capture_output=True, text=True, timeout=120)
    found = re.search(r'<pre id="out">(.*?)</pre>', run.stdout, re.S)
    rows = json.loads(html.unescape(found.group(1))) if found else []
    check("the script ran in a real browser", len(rows) == 8, run.stderr[-300:] if not rows else "")
    if len(rows) == 8:
        big, kind, good, edge, together, att_big, att_ok, att_type = rows
        check("a picture that is too large is refused beside the box, naming the file and both sizes, as an alert",
              big["msg"] == '"photo.jpg" is 7.2 MB. The limit is 5 MB. Choose a smaller picture, or reduce this one first.'
              and big["role"] == "alert" and big["invalid"] == "true")
        check("…and the selection is cleared so it cannot be submitted by accident", big["kept"] == 0)
        check("a file of the wrong type is refused the same way",
              kind["msg"] == '"notes.txt" is not a picture we can use. Please choose a PNG, JPG, GIF or WEBP image.'
              and kind["kept"] == 0 and kind["role"] == "alert")
        check("a good picture shows its name and size and is kept",
              good["msg"] == '"ok.png" (2.1 MB) is ready to upload.' and good["kept"] == 1 and good["role"] == "status"
              and good["invalid"] is None)
        check("a picture a few bytes over the limit is not called 5 MB against a 5 MB limit",
              edge["msg"].startswith('"edge.png" is 5.01 MB. The limit is 5 MB.') and edge["kept"] == 0)
        check("files that together are more than one submission may carry are refused as a group",
              together["kept"] == 0 and "Together, the files you chose are" in together["msg"] and "at most 8 MB" in together["msg"])
        check("an attachment over what a message can carry is refused, and one within it is kept",
              att_big["kept"] == 0 and "The limit is 7.9 MB" in att_big["msg"] and att_ok["kept"] == 1)
        check("an attachment of a kind that is not accepted is refused",
              att_type["kept"] == 0 and "cannot be attached" in att_type["msg"])

# =============================================================== the report
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
if failed:
    for name, _, detail in failed:
        print("FAILED:", name, detail)
raise SystemExit(1 if failed else 0)
