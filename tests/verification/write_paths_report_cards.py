"""Report cards, proven end to end on PostgreSQL: who sees a card and when, what is on it, and that it is true.

Three throw-away schools: "alpha" (its own red logo, address, phone, email, motto and colours), "beta" (a
different blue logo and details) and "gamma" (no logo and no contact details). Everything is done through the
real routes with CSRF tokens read from real pages. Ada's card is produced the honest way: she takes a test and an
examination in her own portal, her assignment and project are marked, and staff move her results through
entered -> verified -> approved -> released. The other students (the class around her, and 40 more for the
efficiency check) are put in with direct inserts, because the point of those is the arithmetic, not the typing.
The PDF is read without adding a library (streams are decompressed with zlib and the text is decoded through the
fonts' own maps, the way write_paths_receipts.py does it). In plain words, it proves:

A. Readiness
   * a card is not ready while any result is entered, verified or approved, and ready only when every one is
     released; one late unreleased result takes it away again; practice tests neither count nor block;
   * each term is separate, so is each session; a student with no results has no card;
   * staff are told "Waiting for N results" with the right N;
   * a release date that has passed releases the approved results and the card shows in the student's and the
     parent's portal on the next visit, with nobody on the staff side doing anything.
B. Content and arithmetic
   * every subject row equals the school's own term result and the numbers worked out here by hand (CA parts
     scaled by the CA weights, exam scaled to 60, rounded to two places); removed work, work with no maximum,
     other terms, other sessions and practice tests stay out;
   * grades and remarks follow the scale on the card at every boundary (70, 69.99, 60, 50, 45, 40, 39.99);
   * the overall percentage, the class average and each subject's class average are the hand-worked figures, and a
     classmate whose own card is not ready is not in them, nor in the class size;
   * a hostile name shows literally and safely in the page and literally in the PDF.
C. The teacher's comment and signature
   * the comment shows with its own author's name and signature (page and PDF); another teacher editing it takes
     over; saving the page unchanged does not; clearing a box removes it; too long is refused and nothing at all
     is saved; a card with no comment still appears; a teacher with no signature gets a blank line and a name;
     replacing or removing a signature deletes the old file; bad signatures are refused and change nothing.
D. The head
   * title, name and signature on every card; "Head of School" and a blank line when nothing is set; a custom
     title; "next term begins"; a warning on the staff page; each school has its own.
E. Branding
   * the card wears THAT school's logo (drawn in the PDF, embedded in the page), name, motto, address, phone,
     email and colour, never another school's; a school without a logo prints its name in its place.
F. Who can see what: class-scoped staff, permissions, CSRF, signed-out visitors, students, parents, other schools.
G. The PDF: headers, text that matches the page, the class bundle, the audit log.
H. Efficiency: the number of SQL statements for a whole class does not grow with the class.
I. Robustness: no class, tampered addresses, an annual card, long lists and comments, photographs, no tracebacks.
J. The new routes are in the guard files.

Run:  python tests/verification/write_paths_report_cards.py
"""
import atexit
import base64
import html
import io
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import urllib.parse
import zlib
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # a failure may quote text from a page

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_cards_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('cards')
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
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
os.chdir(ROOT)

import sqlalchemy as sa  # noqa: E402
from PIL import Image  # noqa: E402  (already installed with reportlab)
from werkzeug.security import generate_password_hash  # noqa: E402

import app as A  # noqa: E402
import blueprints.school.helpers as SCH  # noqa: E402
import blueprints.school.report_card_data as RCD  # noqa: E402
from control_plane import provisioning as pv  # noqa: E402
from control_plane.context import tenant_context  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import dispose_engines, engine_for  # noqa: E402
from core import delivery as _delivery  # noqa: E402
from core.security import ADMIN_ENDPOINT_PERMISSIONS  # noqa: E402
from core.storage import uploads_dir  # noqa: E402
from models import (  # noqa: E402
    AcademicSession, Admin, AdminScope, AdminType, AdminTypePermission, AssignmentStudent, ParentAccount,
    ParentStudentLink, Permission, ProjectStudent, ReportCardComment, School as SchoolRow, SchoolAssignment, SchoolProject,
    SchoolStudentResult, SchoolSubject, Student, StudentEnrolment,
)

# Nothing in this run may reach the outside world (a guardian is emailed when work is set).
_delivery._open_smtp = lambda settings: (_ for _ in ()).throw(RuntimeError("no mail in a test"))

results = []
PL = "http://platform.test"
ALPHA, BETA, GAMMA = "http://alpha.portal.test", "http://beta.portal.test", "http://gamma.portal.test"
TERM = "First Term"
SLUG = "first-term"


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)


class Errors(logging.Handler):
    """Remembers every exception the application logs while it answers a request."""

    def __init__(self):
        super().__init__()
        self.seen = []

    def emit(self, record):
        if record.exc_info and record.exc_info[1] is not None:
            self.seen.append(record.exc_info[1])


errors = Errors()
A.app.logger.addHandler(errors)
A.app.logger.propagate = False
PROBLEMS = []  # every response that was a server error, or that showed a traceback


# ================================================================ reading a PDF without a library
def _unescape(raw):
    """A PDF literal string (backslash escapes, octal codes) as bytes."""
    out, i = bytearray(), 0
    named = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}
    while i < len(raw):
        if raw[i] != 0x5C:
            out.append(raw[i])
            i += 1
        elif 0x30 <= raw[i + 1] <= 0x37:
            j = i + 1
            while j < len(raw) and j < i + 4 and 0x30 <= raw[j] <= 0x37:
                j += 1
            out.append(int(raw[i + 1:j], 8) & 255)
            i = j
        else:
            out.append(named.get(raw[i + 1], raw[i + 1]))
            i += 2
    return bytes(out)


def pdf_read(data):
    """A report card PDF as plain data: ``lines`` (every piece of text drawn, in order), ``pages`` (the same, one
    list per page), ``text`` (all of it), ``images`` (every picture's raw pixels), ``colours`` (every fill colour
    as an (r, g, b) tuple of 0..255) and ``page_count``."""
    objects = {int(m.group(1)): m.group(2) for m in re.finditer(rb"(\d+) 0 obj\s*(.*?)\s*endobj", data, re.S)}

    def decode(obj):
        head, keyword, rest = obj.partition(b"stream")
        if not keyword:
            return head, None
        body = rest.lstrip(b"\r\n").rsplit(b"endstream", 1)[0]
        try:
            for kind in re.findall(rb"/(ASCII85Decode|FlateDecode)", head):
                body = base64.a85decode(body.strip().removesuffix(b"~>")) if kind == b"ASCII85Decode" else zlib.decompress(body)
        except Exception:
            return head, None
        return head, body

    maps = {}
    for number, obj in objects.items():
        found = re.search(rb"/ToUnicode (\d+) 0 R", obj)
        if found and b"/Type /Font" in obj:
            _, cmap = decode(objects[int(found.group(1))])
            maps[number] = {int(code, 16): chr(int(char, 16)) for code, char in
                            re.findall(rb"<([0-9A-Fa-f]{2})>\s*<([0-9A-Fa-f]{4})>", cmap or b"")}
    fonts = {}
    for obj in objects.values():
        if b"stream" not in obj and b"/Type" not in obj:
            fonts.update({name.decode(): int(number) for name, number in re.findall(rb"/(F[\w+]+) (\d+) 0 R", obj)})
    pages, images, colours = [], [], set()
    for obj in objects.values():
        head, body = decode(obj)
        if body is None:
            continue
        if b"/Subtype /Image" in head:
            images.append(body)
        elif b" Tf" in body:
            words, font = [], None
            for m in re.finditer(rb"/(F[\w+]+) [\d.]+ Tf|\(((?:\\.|[^\\)])*)\) Tj", body, re.S):
                if m.group(1):
                    font = maps.get(fonts.get(m.group(1).decode()))
                else:
                    raw = _unescape(m.group(2))
                    words.append("".join(font.get(b, "") for b in raw) if font else raw.decode("cp1252", "replace"))
            pages.append(words)
            for m in re.finditer(rb"(-?[\d.]+) (-?[\d.]+) (-?[\d.]+) rg", body):
                colours.add(tuple(int(round(float(x) * 255)) for x in m.groups()))
    lines = [w for page in pages for w in page]
    return {"lines": lines, "pages": pages, "text": "\n".join(lines), "images": images, "colours": colours,
            "page_count": len(re.findall(rb"/Type /Page\b", data))}


def solid(rgb, size=(64, 24), kind="PNG"):
    """A one-colour picture, and the pixels a PDF holds for it."""
    picture = Image.new("RGBA", size, rgb + (255,))
    buf = io.BytesIO()
    picture.save(buf, kind)
    return buf.getvalue(), bytes(rgb) * (size[0] * size[1])


def draws(pdf, pixels):
    """Whether the PDF contains a picture with exactly these pixels."""
    return any(pixels in image for image in pdf["images"])


RED_LOGO, RED = solid((200, 30, 30), (90, 40))            # Alpha's logo
BLUE_LOGO, BLUE = solid((30, 60, 200), (90, 40))          # Beta's logo
OLD_LOGO, OLD = solid((250, 200, 20), (90, 40))           # nobody's logo: the school this platform grew out of
GREEN_SIGN, GREEN = solid((20, 160, 60))                  # teacher A, drawn
TEAL_SIGN, TEAL = solid((0, 150, 150))                    # teacher A, uploaded later
PURPLE_SIGN, PURPLE = solid((120, 30, 160))               # teacher B, uploaded
ORANGE_SIGN, ORANGE = solid((240, 120, 10))               # alpha's head, drawn
NAVY_SIGN, NAVY = solid((10, 20, 120))                    # alpha's head, uploaded later
PINK_SIGN, PINK = solid((230, 60, 140))                   # beta's teacher
GREY_SIGN, GREY = solid((70, 70, 70))                     # beta's head
PHOTO_PNG = solid((10, 180, 10), (60, 80))[0]             # a student's photograph
OLD_SCHOOL_WORDS = ("CREATIVE", "RAINBOW", "MONTESSORI", "Charles Okeke", "Alahun", "Maza-Maza", "0803 123 4567", "0810 987 6543")
ALPHA_PRIMARY, ALPHA_ACCENT = "#b00020", "#7a4b00"
BETA_PRIMARY, BETA_ACCENT = "#0a7d3c", "#006f8c"


def hex_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def pixel_of(data_uri):
    """The colour of a picture embedded in a page as a data: URI, or None."""
    try:
        return Image.open(io.BytesIO(base64.b64decode(data_uri.split(",", 1)[1]))).convert("RGB").getpixel((3, 3))
    except Exception:
        return None


# ================================================================ people, browsers
def csrf_from(body):
    found = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', body)
    return found.group(1) if found else ""


class Person:
    """One signed-in (or signed-out) person at one school: a browser with its own cookies."""

    count = [0]

    def __init__(self, base, form_page="/admin/password", label=""):
        self.base, self.form_page, self.client, self.label = base, form_page, A.app.test_client(), label or base
        Person.count[0] += 1
        self.addr = f"10.50.{Person.count[0]}.1"  # sign-in limits are per address

    def _note(self, r, method, path):
        if r.status_code >= 500:
            PROBLEMS.append(f"{self.label}: {method} {path} answered {r.status_code}")
        elif "Traceback (most recent call last)" in r.get_data(as_text=True)[:200000] if r.mimetype == "text/html" else False:
            PROBLEMS.append(f"{self.label}: {method} {path} showed a traceback")
        return r

    def get(self, path, **kw):
        return self._note(self.client.get(path, base_url=self.base, environ_base={"REMOTE_ADDR": self.addr}, **kw), "GET", path)

    def text(self, path):
        return self.get(path).get_data(as_text=True)

    def post(self, path, data=None, page=None, files=None, token=True):
        """Submit a form the way a browser does: the token comes from a real page that carries a form."""
        data = dict(data or {})
        if token is True:
            data["_csrf_token"] = csrf_from(self.text(page or self.form_page))
        elif token:
            data["_csrf_token"] = token
        for key, (filename, content) in (files or {}).items():
            data[key] = (io.BytesIO(content), filename)
        r = self.client.post(path, data=data, base_url=self.base, environ_base={"REMOTE_ADDR": self.addr},
                             content_type="multipart/form-data" if files else None)
        return self._note(r, "POST", path)

    def said(self):
        """The messages left for the next page, as one lowercase string (and clears them)."""
        with self.client.session_transaction(base_url=self.base) as sess:
            found = list(sess.pop("_flashes", []))
        return " | ".join(text for _, text in found).lower()


def info_for(slug):
    with platform_session() as s:
        return to_info(get_tenant(s, slug))


class School:
    """A school under test: its database, and helpers to read it."""

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

    def uploads(self):
        return self.run(uploads_dir)


def signature_files(school):
    folder = os.path.join(school.uploads(), "signatures")
    return sorted(os.listdir(folder)) if os.path.isdir(folder) else []


pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
console = A.app.test_client()
console.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                      "_csrf_token": csrf_from(console.get("/platform/login", base_url=PL).get_data(as_text=True))}, base_url=PL)
SCHOOLS = (
    # code, name, motto, logo, address, phone, email, primary, accent
    ("alpha", "Alpha School", "Excellence in Learning", ("logo.png", RED_LOGO), "12 Palm Avenue, Ikeja, Lagos",
     "0803 111 2222", "info@alpha-school.example", ALPHA_PRIMARY, ALPHA_ACCENT),
    ("beta", "Beta College", "Knowledge is Light", ("logo.png", BLUE_LOGO), "4 River Road, Abuja",
     "0809 555 6666", "hello@beta-college.example", BETA_PRIMARY, BETA_ACCENT),
    ("gamma", "Gamma Academy", "", None, "", "", "", "#0d2b52", "#1674b9"),
)
for code, name, motto, logo, address, phone, email, primary, accent in SCHOOLS:
    form = {"_csrf_token": csrf_from(console.get("/platform/schools/new", base_url=PL).get_data(as_text=True)), "name": name,
            "code": code, "school_brand_primary": primary, "school_brand_accent": accent, "school_address": address,
            "school_phone": phone, "school_email": email, "school_motto": motto}
    if logo:
        form["logo"] = (io.BytesIO(logo[1]), logo[0])
    console.post("/platform/schools/new", data=form, base_url=PL, content_type="multipart/form-data")
alpha, beta, gamma = School("alpha", ALPHA), School("beta", BETA), School("gamma", GAMMA)


def operator_in(school):
    """The platform operator enters a school with a single-use ticket and is its School Admin there."""
    page = console.get(f"/platform/schools/{school.code}", base_url=PL).get_data(as_text=True)
    r = console.post(f"/platform/schools/{school.code}/enter", data={"_csrf_token": csrf_from(page)}, base_url=PL)
    person = Person(school.base, label=f"{school.code} School Admin")
    person.get(r.headers["Location"][len(school.base):])
    person.get("/admin/workspace/school")
    return person


op, op_beta, op_gamma = operator_in(alpha), operator_in(beta), operator_in(gamma)
check("three schools were made: two with their own logo and details, one with neither",
      alpha.one("SELECT setting_value FROM school_public_settings WHERE setting_key = 'school_logo'") is not None
      and beta.one("SELECT setting_value FROM school_public_settings WHERE setting_key = 'school_logo'") is not None
      and gamma.one("SELECT setting_value FROM school_public_settings WHERE setting_key = 'school_logo'") is None)

CUR = alpha.one("SELECT id FROM academic_sessions WHERE is_current = 1")
J1, J2, J3, SSS1 = (alpha.one("SELECT id FROM school_classes WHERE name = :n", n=n) for n in ("JSS 1", "JSS 2", "JSS 3", "SSS 1"))
OP_ID = alpha.one("SELECT id FROM admins ORDER BY id LIMIT 1")
RC = "/admin/school/report-cards"
NOW = "2026-09-01T09:00:00+00:00"
REL = "2026-12-18T10:00:00+00:00"          # when the fixture results were released: "18 December 2026"
PASSWORD = "Fixture-password-9"
HASH = generate_password_hash(PASSWORD, method="pbkdf2:sha256:1000")  # quick to check, and never a real password

# ================================================================ the arithmetic, worked out here by hand
# The rules (from the school's own term result, not from the code under test):
#   Exam = raw marks added up, scaled to 60.  Test / Assignment / Project each = raw marks added up, scaled to their
#   share of the 40 CA marks (20 / 10 / 10 until the school changes it).  Every part is rounded to two places, CA is
#   the three parts added, the subject total is Exam + CA, out of 100.  The overall percentage is the average of the
#   subject totals.  The class average is the average of the percentages of the students whose card is ready.
def r2(x):
    """Round half up to two places, the way a person does (Python's round() goes to the even digit on a tie)."""
    return float(Decimal(repr(float(x))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def part(pairs, cap):
    got, out_of = sum(a for a, _ in pairs), sum(b for _, b in pairs)
    return 0.0 if not out_of else min(float(cap), r2(got / out_of * cap))


def subject_model(spec):
    exam = part(spec.get("E", []), 60)
    test, assignment, project = part(spec.get("T", []), 20), part(spec.get("A", []), 10), part(spec.get("P", []), 10)
    ca = r2(test + assignment + project)
    return {"test": test, "assignment": assignment, "project": project, "ca": ca, "exam": exam, "total": r2(exam + ca)}


def grade_model(total):
    for low, letter, remark in ((70, "A", "Excellent"), (60, "B", "Very Good"), (50, "C", "Good"),
                                (45, "D", "Fair"), (40, "E", "Pass"), (0, "F", "Fail")):
        if total >= low:
            return letter, remark


SPEC = {}         # (key, term) -> {subject name: {"T": [(score, max)], "E": [...], "A": [...], "P": [...]}}
SID = {}          # key -> student id
KEY_CLASS = {}    # key -> class id (None: in no class that session)
KEY_SESSION = {}  # key -> session id
READY = set()     # (key, term) whose card is ready right now: kept up to date by this script


def card_model(key, term):
    rows = {name: subject_model(spec) for name, spec in SPEC[(key, term)].items()}
    total = r2(sum(r["total"] for r in rows.values()))
    return rows, total, 100.0 * len(rows), r2(total / (100.0 * len(rows)) * 100)


def class_model(key, term):
    """(class average %, class size, {subject: class average}) over the ready students of this key's class."""
    cls, sess = KEY_CLASS[key], KEY_SESSION[key]
    if cls is None:
        return None, 0, {}
    cards = {k: card_model(k, t) for (k, t) in READY if t == term and KEY_CLASS[k] == cls and KEY_SESSION[k] == sess}
    average = r2(sum(c[3] for c in cards.values()) / len(cards)) if cards else None
    per = {}
    for c in cards.values():
        for name, row in c[0].items():
            per.setdefault(name, []).append(row["total"])
    return average, len(cards), {n: r2(sum(v) / len(v)) for n, v in per.items()}


def expected_rows(key, term):
    """[(subject, test, assignment, project, ca, exam, total, class average or None, grade, remark)] by subject name."""
    rows = card_model(key, term)[0]
    averages = class_model(key, term)[2]
    out = []
    for name in sorted(rows):
        r = rows[name]
        grade, remark = grade_model(r["total"])
        out.append((name, r["test"], r["assignment"], r["project"], r["ca"], r["exam"], r["total"], averages.get(name), grade, remark))
    return out


def same(a, b, tol=0.005):
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= tol


def rows_equal(actual, expected):
    if len(actual) != len(expected):
        return False
    for got, want in zip(actual, expected):
        if got[0] != want[0] or got[8] != want[8] or got[9] != want[9]:
            return False
        if not all(same(g, w) for g, w in zip(got[1:8], want[1:8])):
            return False
    return True


def issued_text(latest):
    moment = datetime.fromisoformat(latest.replace("Z", "+00:00"))
    return f"{moment.day} {moment.strftime('%B %Y')}"


# ================================================================ reading a report card page
def strip_tags(fragment):
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def number_or_none(text):
    text = text.strip()
    if text in ("", "—", "-"):
        return None
    return float(text)


def parse_page(body):
    """The parts of a report card page as plain data."""
    def first(pattern, default="", flags=re.S):
        found = re.search(pattern, body, flags)
        return found.group(1) if found else default

    head_block = first(r'<header class="rc-head">(.*?)</header>')
    contact = first(r'<div class="contact">(.*?)</div>')
    table = first(r'<table class="rc-results">(.*?)</table>')
    header_cells = [strip_tags(c) for c in re.findall(r"<th[^>]*>(.*?)</th>", table, re.S)]
    rows = []
    for tr in re.findall(r"<tr>(.*?)</tr>", table.split("</thead>")[-1], re.S):
        cells = [strip_tags(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) >= 9:
            has_avg = len(cells) == 10
            base = cells[:7]
            rows.append((cells[0], *[number_or_none(c) for c in base[1:7]], number_or_none(cells[7]) if has_avg else None,
                         cells[-2], cells[-1]))
    summary = {strip_tags(a): strip_tags(b) for a, b in re.findall(r"<div><span>(.*?)</span><strong>(.*?)</strong></div>",
                                                                   first(r'<section class="rc-summary">(.*?)</section>'), re.S)}
    signs = []
    sign_html = first(r'<section class="rc-signs">(.*?)</section>')
    for block in re.split(r'<div class="rc-sign">', sign_html)[1:]:
        image = re.search(r'<img[^>]*src="([^"]+)"', block)
        signs.append({"image": image.group(1) if image else None, "who": strip_tags(first_in(block, r'<div class="who">(.*?)</div>')),
                      "role": strip_tags(first_in(block, r'<div class="role">(.*?)</div>'))})
    student = {strip_tags(a): strip_tags(b) for a, b in re.findall(r"<dt>(.*?)</dt><dd>(.*?)</dd>", first(r'<section class="rc-student">(.*?)</section>'), re.S)}
    logo = re.search(r'<img class="rc-logo" src="([^"]+)"', body)
    photo = re.search(r'<img class="rc-photo" src="([^"]+)"', body)
    return {
        "title": strip_tags(first(r'<div class="rc-title">\s*<h2>(.*?)</h2>')),
        "term_line": strip_tags(first(r'<div class="rc-title">.*?<p>(.*?)</p>')),
        "school": strip_tags(first(r"<h1>(.*?)</h1>")), "motto": strip_tags(first(r'<div class="motto">(.*?)</div>')),
        "contact": html.unescape(re.sub(r"<[^>]+>", "\n", contact.replace("<br>", "\n"))).strip(),
        "logo": logo.group(1) if logo else None, "photo": photo.group(1) if photo else None,
        "colour": first(r"--rc-primary:\s*([^;]+);", "").strip(), "accent": first(r"--rc-accent:\s*([^;]+);", "").strip(),
        "student": student, "header": header_cells, "rows": rows, "summary": summary,
        "comment": strip_tags(first(r'<section class="rc-comment">.*?<p>(.*?)</p>')),
        "signs": signs, "grading": [tuple(strip_tags(c) for c in re.findall(r"<td>(.*?)</td>", tr, re.S))
                                    for tr in re.findall(r"<tr>(.*?)</tr>", first(r'<section class="rc-grading">(.*?)</section>'), re.S)],
        "issued": strip_tags(first(r"<span>Issued: (.*?)</span>")), "next_term": strip_tags(first(r"<span>Next term begins: (.*?)</span>")),
        "download": first(r'<a class="primary" href="([^"]+)"'), "raw": body,
    }


def first_in(text, pattern):
    found = re.search(pattern, text, re.S)
    return found.group(1) if found else ""


def pdf_row(pdf, name, with_average):
    """The tokens drawn after a subject's name in the PDF's results table."""
    lines = pdf["lines"]
    if name not in lines:
        return None
    start = lines.index(name)
    width = 9 if with_average else 8
    return lines[start + 1:start + 1 + width]


def pdf_number(text):
    return None if text in ("-", "") else float(text)


# ================================================================ making people and things (direct inserts)
_counter = {"student": 0, "work": 0}


def orm(school, fn):
    def go():
        out = fn()
        A.db.session.commit()
        return out
    return school.run(go)


def mk_students(school, specs):
    """specs: [(first, last, class_id or None, session_id, options)] -> [student ids]. options: login, middle, photo, active."""
    def go():
        school_id = A.db.session.scalar(sa.select(SchoolRow.id))
        ids = []
        for first, last, class_id, session_id, opts in specs:
            _counter["student"] += 1
            code = f"{school.code[:3].upper()}/{_counter['student']:04d}"
            student = Student(admission_no=code, student_number=code, first_name=first, middle_name=opts.get("middle") or None,
                              last_name=last, gender=opts.get("gender", "Female"), created_at=NOW, active=opts.get("active", 1),
                              school_id=school_id, login_username=opts.get("login"),
                              login_password_hash=HASH if opts.get("login") else None, account_active=1, password_must_change=0,
                              photo_path=opts.get("photo"), guardian_name="A Guardian")
            A.db.session.add(student)
            A.db.session.flush()
            if class_id:
                A.db.session.add(StudentEnrolment(student_id=student.id, class_id=class_id, session_id=session_id, enrolled_at=NOW,
                                                  active=opts.get("enrolment_active", 1), school_id=school_id))
            ids.append(student.id)
        return ids
    return orm(school, go)


def mk_subjects(school, names):
    def go():
        rows = [SchoolSubject(name=n, code=n[:3].upper(), created_at=NOW, active=1) for n in names]
        A.db.session.add_all(rows)
        A.db.session.flush()
        return [r.id for r in rows]
    return orm(school, go)


def put(school, student_id, subject_id, spec, term=TERM, session=None, status="released", class_id=None, released_at=REL, admin_id=None):
    """A student's marks for one subject: Test / Exam rows in the results table, assignments and projects as work."""
    session = session or CUR

    def go():
        for component, key in (("Test", "T"), ("Exam", "E")):
            for score, out_of in spec.get(key, []):
                A.db.session.add(SchoolStudentResult(
                    student_id=student_id, subject_id=subject_id, score=float(score), max_score=float(out_of), term=term,
                    session_id=session, status=status, created_at=NOW, source_type="manual", component_name=component,
                    released_at=released_at if status == "released" else None))
        for score, out_of in spec.get("A", []):
            _counter["work"] += 1
            work = SchoolAssignment(title=f"Fixture assignment {_counter['work']}", class_id=class_id or J1, subject_id=subject_id,
                                    created_by=admin_id or OP_ID, created_at=NOW, active=1, max_score=float(out_of), session_id=session,
                                    term=term, due_date="2027-01-01", date_given="2026-09-10")
            A.db.session.add(work)
            A.db.session.flush()
            A.db.session.add(AssignmentStudent(assignment_id=work.id, student_id=student_id, status="done", score=float(score)))
        for score, out_of in spec.get("P", []):
            _counter["work"] += 1
            work = SchoolProject(title=f"Fixture project {_counter['work']}", class_id=class_id or J1, subject_id=subject_id,
                                 date_given="2026-09-10", due_date="2026-11-30", max_score=float(out_of), created_by=admin_id or OP_ID,
                                 created_at=NOW, active=1, session_id=session, term=term)
            A.db.session.add(work)
            A.db.session.flush()
            A.db.session.add(ProjectStudent(project_id=work.id, student_id=student_id, status="done", score=float(score)))
    return orm(school, go)


def put_status_rows(school, student_id, subject_id, session, term, rows):
    """rows: [(component, score, max, status)] — results in a given step of the workflow."""
    def go():
        for component, score, out_of, status in rows:
            A.db.session.add(SchoolStudentResult(
                student_id=student_id, subject_id=subject_id, score=score, max_score=out_of, term=term, session_id=session,
                status=status, created_at=NOW, source_type="manual", component_name=component,
                released_at=REL if status == "released" else None))
    return orm(school, go)


def mk_staff(school, username, display, role_name, scope_class=None):
    """A member of staff holding one of the school's roles, who then signs in for real."""
    def go():
        role = A.db.session.scalars(sa.select(AdminType).where(AdminType.name == role_name)).first()
        admin = Admin(username=username, display_name=display, password_hash=HASH, admin_type_id=role.id, active=1,
                      password_must_change=0, created_at=NOW)
        A.db.session.add(admin)
        A.db.session.flush()
        if scope_class:
            A.db.session.add(AdminScope(admin_id=admin.id, scope_type="class", scope_value=scope_class, created_at=NOW))
        return admin.id
    admin_id = orm(school, go)
    person = Person(school.base, label=f"{school.code} {username}")
    person.post("/login", {"username": username, "password": PASSWORD}, page="/login")
    person.get("/admin/workspace/school")
    return person, admin_id


def mk_role(school, name, codes):
    def go():
        role = AdminType(name=name, description="Only what is needed.", is_system=0, active=1, created_at=NOW)
        A.db.session.add(role)
        A.db.session.flush()
        for code in codes:
            permission = A.db.session.scalars(sa.select(Permission).where(Permission.code == code)).first()
            A.db.session.add(AdminTypePermission(admin_type_id=role.id, permission_id=permission.id, granted_at=NOW))
    orm(school, go)


def mk_parent(school, username, name, student_ids):
    def go():
        parent = ParentAccount(username=username, display_name=name, email=f"{username}@family.example", password_hash=HASH,
                               active=1, password_must_change=0, created_at=NOW)
        A.db.session.add(parent)
        A.db.session.flush()
        for sid in student_ids:
            A.db.session.add(ParentStudentLink(parent_id=parent.id, student_id=sid, relationship="Mother", active=1, created_at=NOW))
    orm(school, go)
    person = Person(school.base, "/parent/password", label=f"{school.code} parent {username}")
    person.post("/login", {"username": username, "password": PASSWORD}, page="/login")
    return person


def sign_in_student(school, username):
    person = Person(school.base, "/student/password", label=f"{school.code} student {username}")
    person.post("/login", {"username": username, "password": PASSWORD}, page="/login")
    return person


# ---------------------------------------------------------------- what staff, students and parents are shown
def s_view(session_id, slug=SLUG):
    return f"/student/report-cards/{session_id}/{slug}"


def p_view(child, session_id, slug=SLUG):
    return f"/parent/children/{child}/report-cards/{session_id}/{slug}"


def staff_view(student, session_id=None, slug=SLUG):
    return f"{RC}/{student}/{session_id or CUR}/{slug}"


def staff_list(person, class_id, term=TERM, session_id=None):
    """{student name: what the Card column says} from the staff page for a class."""
    body = person.text(f"{RC}?class_id={class_id}&session_id={session_id or CUR}&term={urllib.parse.quote(term)}")
    out = {}
    for tr in re.findall(r"<tr>(.*?)</tr>", body, re.S):
        cells = [strip_tags(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) >= 4:
            out[cells[0]] = cells[2]
    return out


def has_panel(body):
    return 'id="report-cards"' in body


def panel_links(body):
    return sorted(set(re.findall(r'href="(/(?:student|parent)/[^"]*report-cards[^"]*)"', html.unescape(body))))


def pdf_headers_ok(r):
    disposition = r.headers.get("Content-Disposition", "")
    return (r.status_code == 200 and r.data.startswith(b"%PDF") and "no-store" in r.headers.get("Cache-Control", "")
            and re.fullmatch(r'inline; filename="[A-Za-z0-9._-]+\.pdf"', disposition) is not None)


def comments_url(class_id, session_id=None, term=TERM):
    return f"{RC}/comments?class_id={class_id}&session_id={session_id or CUR}&term={urllib.parse.quote(term)}"


def post_comments(person, class_id, values, term=TERM, session_id=None):
    """Save comments the way the page does. ``values`` maps a student id to the text of their box."""
    return person.post(comments_url(class_id, session_id, term), {f"comment_{k}": v for k, v in values.items()},
                       page=comments_url(class_id, session_id, term))


def comment_row(student, term=TERM, session_id=None):
    rows = alpha.sql("SELECT comment, author_admin_id FROM report_card_comments WHERE student_id = :s AND term = :t AND session_id = :c",
                     s=student, t=term, c=session_id or CUR)
    return tuple(rows[0]) if rows else None


def boxes(person, class_id, term=TERM):
    """What every comment box on the page holds right now, as a browser would send it back (line breaks as CRLF)."""
    body = person.text(comments_url(class_id, term=term))
    return {int(sid): html.unescape(text).replace("\r\n", "\n").replace("\n", "\r\n")
            for sid, text in re.findall(r'<textarea name="comment_(\d+)"[^>]*>(.*?)</textarea>', body, re.S)}


def traits_url(class_id, session_id=None, term=TERM):
    return f"{RC}/traits?class_id={class_id}&session_id={session_id or CUR}&term={urllib.parse.quote(term)}"


def post_traits(person, class_id, values, term=TERM, session_id=None):
    """Save trait ratings the way the page does. ``values`` maps a student id to {trait key: 1..5}."""
    data = {}
    for student_id, ratings in values.items():
        for key, rating in ratings.items():
            data[f"trait_{student_id}_{key}"] = str(rating)
    return person.post(traits_url(class_id, session_id, term), data, page=traits_url(class_id, session_id, term))


def trait_row(student, term=TERM, session_id=None):
    rows = alpha.sql("SELECT ratings, author_admin_id FROM report_card_traits WHERE student_id = :s AND term = :t AND session_id = :c",
                     s=student, t=term, c=session_id or CUR)
    return (json.loads(rows[0][0]), rows[0][1]) if rows else None


def head_setting(school, key):
    return school.one("SELECT setting_value FROM school_settings WHERE setting_key = :k", k=key)


def admin_signature(admin_id):
    return alpha.one("SELECT signature_path FROM admins WHERE id = :a", a=admin_id)


def drawn(png):
    return "data:image/png;base64," + base64.b64encode(png).decode()


def verify_card(label, viewer, page_url, pdf_url, key, term, school=None, extra=None):
    """Open a card's page and its PDF and check both against the hand-worked figures for ``key``.

    Returns ``(parsed page, parsed pdf)``. ``extra`` is a dict of expected details:
    student (name), admission, class, gender, comment, teacher, head_name, head_title, next_term.
    """
    extra = extra or {}
    r = viewer.get(page_url)
    check(f"{label}: the page opens", r.status_code == 200, str(r.status_code))
    page = parse_page(r.get_data(as_text=True))
    rp = viewer.get(pdf_url)
    check(f"{label}: the PDF opens with the right headers", pdf_headers_ok(rp), f"{rp.status_code} {rp.headers.get('Content-Disposition')}")
    pdf = pdf_read(rp.data) if rp.status_code == 200 else pdf_read(b"")
    rows, total, out_of, pct = card_model(key, term)
    average, size, _ = class_model(key, term)
    expected = expected_rows(key, term)
    check(f"{label}: the page's subject rows are the hand-worked figures", rows_equal(page["rows"], expected),
          f"page {page['rows']} expected {expected}")
    pdf_ok = True
    for row in expected:
        got = pdf_row(pdf, row[0], average is not None)
        want = [row[1], row[2], row[3], row[4], row[5], row[6]] + ([row[7]] if average is not None else [])
        if got is None or len(got) < len(want) + 2 or not all(same(pdf_number(g), w) for g, w in zip(got, want)) \
                or got[len(want)] != row[8] or got[len(want) + 1] != row[9]:
            pdf_ok = False
            print(f"    pdf row for {row[0]}: {got} vs {row}")
    check(f"{label}: the PDF says the same, subject by subject", pdf_ok)
    summary = page["summary"]
    total_text = summary.get("Total", "")
    check(f"{label}: total and percentage on the page ({total} out of {out_of}, {pct}%)",
          same(float(total_text.split("/")[0]), total) and same(float(total_text.split("/")[1]), out_of)
          and same(float(summary.get("Percentage", "x%").rstrip("%")), pct, 0.051), str(summary))
    check(f"{label}: …and the overall grade", summary.get("Overall grade") == "{} — {}".format(*grade_model(pct)), str(summary.get("Overall grade")))
    class_label = next((k for k in summary if k.startswith("Class average")), None)
    if average is None:
        check(f"{label}: no class average is shown (this student is in no class that session)", class_label is None, str(summary))
    else:
        check(f"{label}: the class average is {average}% over {size} student(s), on the page",
              class_label == f"Class average ({size} students)" and same(float(summary[class_label].rstrip("%")), average, 0.051),
              f"{class_label} {summary.get(class_label)}")
    check(f"{label}: …and the number of subjects", summary.get("Subjects") == str(len(rows)))
    text = pdf["text"]
    lines = pdf["lines"]
    check(f"{label}: the PDF has the total, percentage, grade and class average",
          f"{total:g} / {out_of:g}" in lines and any(l.startswith("%.1f%%" % pct) for l in lines) and any(
              l == "{} - {}".format(*grade_model(pct)) for l in lines)
          and (any(l.startswith("%.1f%%" % average) and f"({size} student" in l for l in lines) if average is not None
               else not any("student)" in l or "students)" in l for l in lines)),
          str([l for l in lines if "%" in l or "/" in l][:8]))
    check(f"{label}: the grading key is on the page and in the PDF",
          page["grading"] == [("70 - 100", "A", "Excellent"), ("60 - 69", "B", "Very Good"), ("50 - 59", "C", "Good"),
                              ("45 - 49", "D", "Fair"), ("40 - 44", "E", "Pass"), ("0 - 39", "F", "Fail")]
          and all(s in lines for s in ("A: 70 - 100 (Excellent)", "B: 60 - 69 (Very Good)", "C: 50 - 59 (Good)",
                                       "D: 45 - 49 (Fair)", "E: 40 - 44 (Pass)", "F: 0 - 39 (Fail)")), str(page["grading"]))
    latest = alpha.one("SELECT max(released_at) FROM school_student_results WHERE student_id = :s AND term = :t AND session_id = :c",
                       s=SID[key], t=term, c=KEY_SESSION[key])
    if latest:
        check(f"{label}: 'Issued' is the day the last result was released ({issued_text(latest)})",
              page["issued"] == issued_text(latest) and f"Issued: {issued_text(latest)}" in lines, page["issued"])
    if "student" in extra:
        check(f"{label}: the student's name is {extra['student']!r} in the page and the PDF",
              page["student"].get("Name") == extra["student"] and extra["student"] in lines, str(page["student"]))
    if "class" in extra:
        check(f"{label}: the class is {extra['class']!r}", page["student"].get("Class") == extra["class"] and extra["class"] in lines)
    if "next_term" in extra:
        check(f"{label}: 'Next term begins' is shown, page and PDF",
              page["next_term"] == extra["next_term"] and f"Next term begins: {extra['next_term']}" in lines, page["next_term"])
    return page, pdf


# ================================================================ 1. the subjects, the staff, the class around Ada
# Subjects through the real form (Ada's subjects), the rest directly.
op.post("/admin/school/subjects/new", {"name": "Mathematics", "code": "MTH", "class_ids": [J1]}, page="/admin/school/subjects/new")
op.post("/admin/school/subjects/new", {"name": "English Language", "code": "ENG", "class_ids": [J1]}, page="/admin/school/subjects/new")
MATHS = alpha.one("SELECT id FROM school_subjects WHERE name = 'Mathematics'")
ENGLISH = alpha.one("SELECT id FROM school_subjects WHERE name = 'English Language'")
check("two subjects were made through the real form", MATHS is not None and ENGLISH is not None)
BOUNDARIES = ("Boundary 70", "Boundary 69.99", "Boundary 60", "Boundary 50", "Boundary 45", "Boundary 40", "Boundary 39.99")
BOUNDARY_ID = dict(zip(BOUNDARIES, mk_subjects(alpha, BOUNDARIES)))
SUBJECT = {"Mathematics": MATHS, "English Language": ENGLISH, **BOUNDARY_ID}

op.post("/admin/school/sessions", {"action": "create", "name": "2027/2028", "start_date": "2027-09-01", "end_date": "2028-07-31"},
        page="/admin/school/sessions")
NEXT = alpha.one("SELECT id FROM academic_sessions WHERE name = '2027/2028'")
check("a second session, 2027/2028, was made through the real form", NEXT is not None and NEXT != CUR)

# Staff. Three teachers each limited to their own class, a member of staff who may only look, one who may do nothing
# here, the two preset roles that carry all three report card permissions.
mk_role(alpha, "Report Viewer", ["school.view", "school.students.view", "report_cards.view"])
teacher_a, TEACHER_A = mk_staff(alpha, "teacher.a", "Mrs Amaka Tutor", "Primary Class Teacher", "JSS 1")
teacher_b, TEACHER_B = mk_staff(alpha, "teacher.b", "Mr Babatunde Guide", "Primary Class Teacher", "JSS 1")
teacher_c, TEACHER_C = mk_staff(alpha, "teacher.c", "Ms Chioma Mentor", "Primary Class Teacher", "JSS 2")
viewer, _ = mk_staff(alpha, "viewer", "Vera Viewer", "Report Viewer")
nobody, _ = mk_staff(alpha, "librarian", "Lola Librarian", "Librarian")
officer, _ = mk_staff(alpha, "officer", "Olu Officer", "Report Card Officer")
academic, _ = mk_staff(alpha, "academic", "Ada Academic", "School Academic Administrator")

# ================================================================ 2. Ada: the real way
STUDENT_FORM = {"gender": "Female", "date_of_birth": "2014-05-01", "state_of_origin": "Lagos", "class_id": J1,
                "guardian_name": "A Guardian", "guardian_phone": "08031234567", "guardian_email": "guardian@example.test"}
r = op.post("/admin/school/students/new", {**STUDENT_FORM, "first_name": "Ada", "middle_name": "", "last_name": "Obi"},
            page="/admin/school/students/new")
body = html.unescape(r.get_data(as_text=True))
login = re.search(r"Student ID / Username</span><strong>(.+?)</strong>", body, re.S)
temp = re.search(r'credential-password">(.+?)</strong>', body, re.S)
check("Ada was registered through the real form and given a login", bool(login and temp))
ADA_LOGIN, ADA_TEMP = login.group(1).strip(), temp.group(1).strip()
SID["ADA"] = alpha.one("SELECT id FROM students WHERE login_username = :u", u=ADA_LOGIN)
ADA = SID["ADA"]
KEY_CLASS["ADA"], KEY_SESSION["ADA"] = J1, CUR

ASSESS = {"test": "tests", "examination": "examinations", "practice": "practice-tests"}


def make_assessment(kind, title, points, subject=None, term=TERM):
    """A test / examination / practice test made through its real form, then given its questions and opened."""
    op.post(f"/admin/school/{ASSESS[kind]}/new", {"title": title, "instructions": "Answer all.", "class_id": J1,
                                                  "subject_id": subject or MATHS, "session_id": CUR, "term": term,
                                                  "duration_minutes": "30"}, page=f"/admin/school/{ASSESS[kind]}/new")
    aid = alpha.one("SELECT id FROM school_assessments WHERE title = :t", t=title)
    detail = f"/admin/school/assessments/{aid}"
    for n, pts in enumerate(points):
        op.post(f"{detail}/questions/new", {"question_text": f"{title} question {n + 1}?", "instruction": "", "option_a": "A",
                                            "option_b": "B", "option_c": "C", "option_d": "D", "correct_option": n % 4,
                                            "points": pts}, page=detail)
    op.post(f"{detail}/toggle", {}, page=detail)
    return aid


def take(person, aid, right):
    """A student takes an assessment in the portal, getting the first ``right`` questions right and the rest wrong."""
    page = f"/student/assessments/{aid}"
    person.post(f"{page}/start", {}, page=page)
    total = alpha.one("SELECT question_count FROM school_assessments WHERE id = :a", a=aid)
    for q in range(1, total + 1):
        body = person.get(f"{page}?q={q}").get_data(as_text=True)
        attempt = re.search(r'name="attempt_id" value="(\d+)"', body).group(1)
        qid = int(re.search(r'name="question_id" value="(\d+)"', body).group(1))
        correct = alpha.one("SELECT correct_option FROM school_assessment_attempt_questions WHERE attempt_id = :t AND question_id = :q",
                            t=int(attempt), q=qid)
        data = {"attempt_id": attempt, "question_id": qid, "option_index": correct if q <= right else (correct + 1) % 4, "next_q": q + 1}
        if q == total:
            data["submit_assessment"] = "1"
        person.post(f"{page}/answer", data, token=csrf_from(body))
    return alpha.one("SELECT status FROM school_assessment_attempts WHERE assessment_id = :a ORDER BY id DESC LIMIT 1", a=aid)


TEST1 = make_assessment("test", "Maths test one", [1] * 10)
EXAM = make_assessment("examination", "Maths exam", [5] * 10)
PRACTICE = make_assessment("practice", "Maths practice", [1] * 3, term="")
op.post("/admin/school/assignments/new", {"title": "Ada assignment", "instructions": "", "due_date": "2027-01-01", "assignment_type": "written",
                                          "timing_mode": "untimed", "class_id": J1, "subject_id": MATHS, "session_id": CUR, "term": TERM,
                                          "student_ids": [ADA], "max_score": "10"}, page="/admin/school/assignments/new")
op.post("/admin/school/projects/new", {"title": "Ada project", "instructions": "", "date_given": "2026-10-01", "due_date": "2026-11-30",
                                       "class_id": J1, "subject_id": MATHS, "session_id": CUR, "term": TERM, "student_ids": [ADA],
                                       "max_score": "10"}, page="/admin/school/projects/new")
A1 = alpha.one("SELECT id FROM school_assignments WHERE title = 'Ada assignment'")
P1 = alpha.one("SELECT id FROM school_projects WHERE title = 'Ada project'")
check("a test, an examination, a practice test, an assignment and a project were made through their real forms",
      all(x is not None for x in (TEST1, EXAM, PRACTICE, A1, P1)))

# ================================================================ 3. the class around Ada (direct inserts)
PHOTO_FILE = os.path.join(alpha.uploads(), "students", "bola.png")
os.makedirs(os.path.dirname(PHOTO_FILE), exist_ok=True)
with open(PHOTO_FILE, "wb") as fh:
    fh.write(PHOTO_PNG)
HOSTILE_FIRST, HOSTILE_LAST = '<b>x</b> & "Tolu"', "O'Brien"
HOSTILE_NAME = f"{HOSTILE_FIRST} {HOSTILE_LAST}"
people = mk_students(alpha, [
    ("Bola", "Eze", J1, CUR, {"login": "bola", "photo": "uploads/students/bola.png"}),
    ("Chidi", "Nwosu", J1, CUR, {"login": "chidi"}),
    ("Dayo", "Bello", J1, CUR, {"login": "dayo"}),
    ("Emeka", "Okoro", J1, CUR, {"login": "emeka"}),
    ("Fola", "Ade", J1, CUR, {}),
    (HOSTILE_FIRST, HOSTILE_LAST, J1, CUR, {}),
    ("Ann", "Annual", J1, CUR, {}),
    ("Gone", "Away", J1, CUR, {"active": 0, "enrolment_active": 0}),
    ("Tobi", "Boundary", J2, CUR, {}),
    ("Ife", "Bright", J2, CUR, {}),
    ("Uche", "Late", J2, CUR, {}),
    ("Long", "Lister", SSS1, CUR, {}),
    ("Ọlá", "Ṣadé", SSS1, CUR, {"middle": ""}),
    ("Timmy", "Timed", J1, NEXT, {"login": "timmy"}),
    ("Pia", "Parented", J1, NEXT, {}),
])
for key, sid in zip(("BOLA", "CHIDI", "DAYO", "EMEKA", "FOLA", "HOST", "ANN", "GONE", "TOBI", "IFE", "UCHE", "LONG", "OLA", "TIMMY", "PIA"), people):
    SID[key] = sid
for key, cls, sess in (("BOLA", J1, CUR), ("CHIDI", J1, CUR), ("DAYO", J1, CUR), ("EMEKA", J1, CUR), ("FOLA", J1, CUR), ("HOST", J1, CUR),
                       ("ANN", J1, CUR), ("GONE", J1, CUR), ("TOBI", J2, CUR), ("IFE", J2, CUR), ("UCHE", J2, CUR), ("LONG", SSS1, CUR),
                       ("OLA", SSS1, CUR), ("TIMMY", J1, NEXT), ("PIA", J1, NEXT)):
    KEY_CLASS[key], KEY_SESSION[key] = cls, sess
SID["BOLA_S2"] = SID["BOLA"]
KEY_CLASS["BOLA_S2"], KEY_SESSION["BOLA_S2"] = None, NEXT       # Bola has a result in the next session but is enrolled in no class there
SID["BOLA_T2"] = SID["BOLA"]
KEY_CLASS["BOLA_T2"], KEY_SESSION["BOLA_T2"] = J1, CUR           # Bola's Second Term

MATHS_SPEC = "Mathematics"
SPEC[("ADA", TERM)] = {"Mathematics": {"T": [(8, 10)], "E": [(45, 50)], "A": [(9, 10)], "P": [(7, 10)]}}   # what the real flow makes
ADA_ENGLISH = {"T": [(16, 20)], "E": [(42, 60)]}                                                        # entered later, by hand
SPEC[("BOLA", TERM)] = {"Mathematics": {"T": [(8, 10), (10, 20)], "E": [(30, 50)], "A": [(9, 10), (5, 20)], "P": [(7, 10), (1, 5)]},
                        "English Language": {"T": [(9, 10)], "E": [(48, 60)]}}
SPEC[("CHIDI", TERM)] = {"Mathematics": {"T": [(5, 10)], "E": [(31, 60)]}}
SPEC[("HOST", TERM)] = {"Mathematics": {"T": [(7, 10)], "E": [(34, 60)]}}
SPEC[("DAYO", TERM)] = {"Mathematics": {"T": [(6, 10)], "E": [(40, 60)]}, "English Language": {"E": [(30, 60)]}}
SPEC[("ANN", "Full Session")] = {"Mathematics": {"T": [(8, 10)], "E": [(36, 60)]}}
SPEC[("BOLA_T2", "Second Term")] = {"Mathematics": {"T": [(9, 10)], "E": [(45, 60)], "A": [(10, 10)]}}
SPEC[("BOLA_S2", TERM)] = {"Mathematics": {"T": [(10, 10)], "E": [(54, 60)], "A": [(10, 10)]}}
BOUNDARY_EXAM = {"Boundary 70": 50, "Boundary 69.99": 49.99, "Boundary 60": 40, "Boundary 50": 30, "Boundary 45": 25,
                 "Boundary 40": 20, "Boundary 39.99": 19.99}
SPEC[("TOBI", TERM)] = {n: {"T": [(10, 10)], "E": [(BOUNDARY_EXAM[n], 60)]} for n in BOUNDARIES}
SPEC[("IFE", TERM)] = {"Boundary 70": {"T": [(5, 10)], "E": [(10.02, 60)]}, "Boundary 60": {"T": [(5, 10)], "E": [(10, 60)]}}
SPEC[("TIMMY", TERM)] = {"Mathematics": {"T": [(10, 10)], "E": [(45, 60)]}}
SPEC[("PIA", TERM)] = {"Mathematics": {"T": [(9, 10)], "E": [(42, 60)]}}
SPEC[("OLA", TERM)] = {"Mathematics": {"T": [(10, 10)], "E": [(40, 60)]}}
LONG_SUBJECTS = [f"Long Subject {n:02d}" for n in range(1, 31)]
SUBJECT.update(dict(zip(LONG_SUBJECTS, mk_subjects(alpha, LONG_SUBJECTS))))
SPEC[("LONG", TERM)] = {n: {"T": [(5, 10)], "E": [(30, 60)]} for n in LONG_SUBJECTS}


def load(key, term, **kw):
    for name, spec in SPEC[(key, term)].items():
        put(alpha, SID[key], SUBJECT[name], spec, term=term, session=KEY_SESSION[key], class_id=KEY_CLASS[key] or J1, **kw)


for key in ("BOLA", "CHIDI", "HOST", "TOBI", "IFE", "OLA", "LONG"):
    load(key, TERM)
load("BOLA_T2", "Second Term")
load("BOLA_S2", TERM)
load("ANN", "Full Session")
# Dayo is part way through the workflow: 3 results are not released yet (a test entered, an exam approved, another exam verified).
put_status_rows(alpha, SID["DAYO"], MATHS, CUR, TERM, [("Test", 6, 10, "entered"), ("Exam", 40, 60, "approved")])
put_status_rows(alpha, SID["DAYO"], ENGLISH, CUR, TERM, [("Exam", 30, 60, "verified")])
put_status_rows(alpha, SID["UCHE"], SUBJECT["Boundary 70"], CUR, TERM, [("Test", 5, 10, "entered")])
# What must never count and never block, on Bola's First Term Mathematics: a practice test (not yet released!), work that was
# removed, work with no maximum, the same student's work in another term and another session (loaded above).
def bola_extras():
    practice = A.SchoolAssessment(assessment_type="practice", title="Bola practice", class_id=J1, subject_id=MATHS, session_id=CUR,
                                  question_count=3, active=1, created_by=OP_ID, created_at=NOW, term="First Term")
    A.db.session.add(practice)
    A.db.session.flush()
    A.db.session.add(SchoolStudentResult(student_id=SID["BOLA"], assessment_id=practice.id, subject_id=MATHS, score=3.0, max_score=3.0,
                                         term=TERM, session_id=CUR, status="entered", created_at=NOW, source_type="cbt"))
    for title, maximum, active in (("Removed assignment", 10.0, 0), ("Assignment with no maximum", None, 1)):
        work = SchoolAssignment(title=title, class_id=J1, subject_id=MATHS, created_by=OP_ID, created_at=NOW, active=active,
                                max_score=maximum, session_id=CUR, term=TERM, due_date="2027-01-01", date_given="2026-09-10")
        A.db.session.add(work)
        A.db.session.flush()
        A.db.session.add(AssignmentStudent(assignment_id=work.id, student_id=SID["BOLA"], status="done", score=10.0 if active == 0 else 5.0))
    for title, maximum, active in (("Removed project", 10.0, 0), ("Project with no maximum", None, 1)):
        work = SchoolProject(title=title, class_id=J1, subject_id=MATHS, date_given="2026-09-10", due_date="2026-11-30", max_score=maximum,
                             created_by=OP_ID, created_at=NOW, active=active, session_id=CUR, term=TERM)
        A.db.session.add(work)
        A.db.session.flush()
        A.db.session.add(ProjectStudent(project_id=work.id, student_id=SID["BOLA"], status="done", score=10.0 if active == 0 else 5.0))


orm(alpha, bola_extras)
# Fola has only a practice result (released at once, as practice results are), Timmy and Pia wait for a release date.
def fola_practice():
    practice = A.SchoolAssessment(assessment_type="practice", title="Fola practice", class_id=J1, subject_id=MATHS, session_id=CUR,
                                  question_count=3, active=1, created_by=OP_ID, created_at=NOW, term="Full Session")
    A.db.session.add(practice)
    A.db.session.flush()
    A.db.session.add(SchoolStudentResult(student_id=SID["FOLA"], assessment_id=practice.id, subject_id=MATHS, score=3.0, max_score=3.0,
                                         term="Full Session", session_id=CUR, status="released", created_at=NOW, source_type="cbt", released_at=REL))


orm(alpha, fola_practice)
for key in ("TIMMY", "PIA"):
    load(key, TERM, status="approved")
alpha.sql("UPDATE academic_sessions SET result_release_at = '2999-01-01T09:00:00+00:00' WHERE id = :s", s=NEXT)
# "Gone" left the school but still has released results: withdrawn students are not in the class figures.
put(alpha, SID["GONE"], MATHS, {"T": [(10, 10)], "E": [(60, 60)]}, session=CUR)
check("the fixtures are in: released, entered, verified and approved results, practice work and removed work",
      alpha.one("SELECT count(*) FROM school_student_results WHERE status = 'released'") > 30
      and alpha.one("SELECT count(*) FROM school_student_results WHERE status IN ('entered','verified','approved')") >= 9)
fractions = (237 / 4, 223 / 4, 264 / 5, 73.58 / 2, 90.02 / 2, 285 / 5, 154 / 3, 233 / 4, 124 / 2, 374.98 / 7)
check("the fixtures avoid the exact half-way cases, so 'round half up' (by hand) and Python's rounding cannot disagree",
      all(r2(x) == round(x, 2) for x in fractions), str([x for x in fractions if r2(x) != round(x, 2)]))

MUM_ADA = mk_parent(alpha, "mum.ada", "Mrs Obi", [ADA])
MUM_BOLA = mk_parent(alpha, "mum.bola", "Mrs Eze", [SID["BOLA"]])
MUM_PIA = mk_parent(alpha, "mum.pia", "Mrs Parented", [SID["PIA"]])
MUM_OLA = mk_parent(alpha, "mum.ola", "Mrs Sade", [SID["OLA"]])
bola_portal = sign_in_student(alpha, "bola")
chidi_portal = sign_in_student(alpha, "chidi")
dayo_portal = sign_in_student(alpha, "dayo")
emeka_portal = sign_in_student(alpha, "emeka")
timmy_portal = sign_in_student(alpha, "timmy")

ada_portal = Person(ALPHA, "/student/password", label="alpha student Ada")
ada_portal.post("/login", {"username": ADA_LOGIN, "password": ADA_TEMP}, page="/login")
ada_portal.post("/student/password", {"current_password": ADA_TEMP, "new_password": "a-brand-new-password-1",
                                      "confirm_password": "a-brand-new-password-1"})

# Who is ready at the start: Bola, Chidi and the hostile-named student. (Dayo, Uche wait; Emeka, Fola, Ann have nothing for this term.)
READY.update({("BOLA", TERM), ("CHIDI", TERM), ("HOST", TERM), ("BOLA_T2", "Second Term"), ("BOLA_S2", TERM), ("TOBI", TERM),
              ("IFE", TERM), ("OLA", TERM), ("LONG", TERM), ("ANN", "Full Session")})

# ================================================================ A. READINESS, the honest way
check("Ada takes her test (8 of 10) and her examination (9 of 10 right, 45 of 50) in her own portal",
      [take(ada_portal, TEST1, 8), take(ada_portal, EXAM, 9)] == ["submitted"] * 2)
ada_portal.get(f"/student/practice/{PRACTICE}")
ada_portal.post(f"/student/practice/{PRACTICE}", {f"q_{qid}": right for qid, right in alpha.sql(
    "SELECT id, correct_option FROM school_questions WHERE assessment_id = :a", a=PRACTICE)}, page=f"/student/practice/{PRACTICE}")
check("…and she practises too: it is marked, and leaves no result behind, so it can neither count nor hold a card back",
      alpha.one("SELECT count(*) FROM school_student_results WHERE assessment_id = :a", a=PRACTICE) == 0)
op.post(f"/admin/school/assignments/{A1}/students/{ADA}", {"status": "done", "score": "9"}, page=f"/admin/school/assignments/{A1}")
op.post(f"/admin/school/projects/{P1}/students/{ADA}", {"status": "done", "score": "7"}, page=f"/admin/school/projects/{P1}")


def rid(aid):
    return alpha.one("SELECT id FROM school_student_results WHERE student_id = :s AND assessment_id = :a", s=ADA, a=aid)


def status_of(result):
    return alpha.one("SELECT status FROM school_student_results WHERE id = :r", r=result)


def workflow(result, action, actor=None):
    return (actor or op).post(f"/admin/school/results/{result}/workflow", {"action": action, "reason": "Checked"}, page="/admin/school/results")


R_TEST, R_EXAM = rid(TEST1), rid(EXAM)
check("the workflow starts as it should: test and exam entered",
      [status_of(R_TEST), status_of(R_EXAM)] == ["entered", "entered"])


def ada_snapshot(label, ready, waiting=None, extra_lines=""):
    """Who sees what for Ada right now: her staff row, her dashboard, her page and PDF, her parent's view."""
    row = staff_list(op, J1).get("Ada Obi", "")
    if ready:
        check(f"{label}: the staff page says her card is Ready", row == "Ready", row)
    else:
        want = f"Waiting for {waiting} result{'' if waiting == 1 else 's'} to be released"
        check(f"{label}: the staff page says '{want}'", row == want, row)
    dash = ada_portal.text("/student/dashboard")
    check(f"{label}: her dashboard {'has' if ready else 'has no'} 'Report cards' panel",
          has_panel(dash) == ready and (s_view(CUR) in dash) == ready)
    check(f"{label}: her card page is {'open' if ready else 'a 404'} and so is its PDF",
          ada_portal.get(s_view(CUR)).status_code == (200 if ready else 404)
          and ada_portal.get(s_view(CUR) + "/pdf").status_code == (200 if ready else 404))
    mum = MUM_ADA.text(f"/parent/children/{ADA}")
    check(f"{label}: her parent's page {'shows' if ready else 'does not show'} the panel and the card {'opens' if ready else 'is a 404'}",
          has_panel(mum) == ready and MUM_ADA.get(p_view(ADA, CUR)).status_code == (200 if ready else 404)
          and MUM_ADA.get(p_view(ADA, CUR) + "/pdf").status_code == (200 if ready else 404))
    check(f"{label}: staff can {'open' if ready else 'not open'} the card and its PDF",
          op.get(staff_view(ADA, CUR)).status_code == (200 if ready else 302)
          and op.get(staff_view(ADA, CUR) + "/pdf").status_code == (200 if ready else 404))
    op.said()


ada_snapshot("Ada, results entered", False, waiting=2)
workflow(R_TEST, "verify")
workflow(R_EXAM, "verify")
workflow(R_EXAM, "approve")
check("test verified, exam approved: still not released", [status_of(R_TEST), status_of(R_EXAM)] == ["verified", "approved"])
ada_snapshot("Ada, one verified and one approved", False, waiting=2)
workflow(R_EXAM, "release")
ada_snapshot("Ada, the exam released but not the test", False, waiting=1)
check("…yet her dashboard already lists the released exam mark (the card is stricter than the marks)",
      "45" in ada_portal.text("/student/dashboard"))
workflow(R_TEST, "approve")
ada_snapshot("Ada, the test approved but not released", False, waiting=1)

# practice never blocks: an unreleased practice result does not hold a card back, and does not count when it is released.
# The portal no longer writes one (practice leaves no trace), but a school upgraded from an earlier version may still hold old
# ones, so one is planted here and the card must go on ignoring it.
alpha.sql("INSERT INTO school_student_results (student_id, assessment_id, subject_id, score, max_score, term, session_id, status, created_at) "
          "SELECT :s, :a, subject_id, 3, 3, 'First Term', session_id, 'released', :t FROM school_assessments WHERE id = :a", s=ADA, a=PRACTICE, t=NOW)
R_PRACTICE = rid(PRACTICE)
alpha.sql("UPDATE school_student_results SET status = 'entered', released_at = NULL WHERE id = :r", r=R_PRACTICE)
workflow(R_TEST, "release")
check("her last result is released", [status_of(R_TEST), status_of(R_EXAM)] == ["released", "released"])
READY.add(("ADA", TERM))
ada_snapshot("Ada, everything released (her practice result still 'entered', which does not matter)", True)
alpha.sql("UPDATE school_student_results SET status = 'released', released_at = :t WHERE id = :r", r=R_PRACTICE, t=NOW)

# ================================================================ B. what is on the card, with Maths only
page, pdf = verify_card("Ada's card (Maths only)", ada_portal, s_view(CUR), s_view(CUR) + "/pdf", "ADA", TERM,
                        extra={"student": "Ada Obi", "class": "JSS 1", "gender": "Female"})
check("Ada's real marks are 16 + 9 + 7 = 32 CA and 54 exam, 86 in all (as the hand arithmetic says)",
      page["rows"] and same(page["rows"][0][4], 32) and same(page["rows"][0][5], 54) and same(page["rows"][0][6], 86)
      and same(page["rows"][0][7], 58.25), str(page["rows"]))
check("…and the class average is over four ready students only: Ada, Bola, Chidi and the hostile-named one (Dayo waits, Gone left)",
      page["summary"].get("Class average (4 students)") is not None and same(class_model("ADA", TERM)[0], 59.25)
      and class_model("ADA", TERM)[1] == 4, str(page["summary"]))
staff_page, staff_pdf = verify_card("the same card as the school sees it", op, staff_view(ADA), staff_view(ADA) + "/pdf", "ADA", TERM)
parent_page, parent_pdf = verify_card("…and as her parent sees it", MUM_ADA, p_view(ADA, CUR), p_view(ADA, CUR) + "/pdf", "ADA", TERM)
check("the three views are the same card, word for word", page["rows"] == staff_page["rows"] == parent_page["rows"]
      and page["summary"] == staff_page["summary"] == parent_page["summary"] and pdf["lines"] == staff_pdf["lines"] == parent_pdf["lines"])

# the app's own dictionary against the same figures, and against the school's term result subject by subject
def app_card(key, term=TERM, session_id=None):
    return alpha.run(lambda: RCD.build_card(SID[key], session_id or KEY_SESSION[key], term))


def app_term_report(key, subject, term=TERM):
    return alpha.run(lambda: SCH._term_subject_report(SID[key], KEY_SESSION[key], term, SUBJECT[subject]))


def compare_with_term_result(key, term=TERM):
    card = app_card(key, term)
    ok = card is not None
    for row in (card["subjects"] if card else []):
        subject = next(n for n in SPEC[(key, term)] if n == row["name"])
        report = app_term_report(key, subject, term)
        ok = ok and all(same(row[k], report[v]) for k, v in (("test", "test_score"), ("assignment", "assignment_score"),
                                                              ("project", "project_score"), ("ca", "ca_score"), ("exam", "exam_score"),
                                                              ("total", "total_score")))
        ok = ok and row["ca_max"] == 40 and row["exam_max"] == 60 and row["total_max"] == 100
    return ok


check("every subject row on Ada's card equals the school's own term result (_term_subject_report), with CA out of 40, exam out of 60",
      compare_with_term_result("ADA"))
check("Bola's rows do too (two tests, two assignments, two projects combined; practice, removed and unmarked work left out)",
      compare_with_term_result("BOLA"))

# ---- exact hand-worked numbers for Bola, written out in full (the arithmetic is spelled out, not just re-run)
bola_page, bola_pdf = verify_card("Bola's card", bola_portal, s_view(CUR), s_view(CUR) + "/pdf", "BOLA", TERM,
                                  extra={"student": "Bola Eze", "class": "JSS 1"})
by_name = {r[0]: r for r in bola_page["rows"]}
maths = by_name.get("Mathematics")
check("Bola's Mathematics: two tests 8/10 and 10/20 = 18/30 of 20 = 12; exam 30/50 of 60 = 36; assignments 14/30 of 10 = 4.67; "
      "projects 8/15 of 10 = 5.33; CA 22; total 58 — nothing from the practice test, the removed or unmarked work, or other terms",
      maths is not None and [same(maths[i], v) for i, v in ((1, 12), (2, 4.67), (3, 5.33), (4, 22), (5, 36), (6, 58))] == [True] * 6, str(maths))
english = by_name.get("English Language")
check("…English: test 9/10 of 20 = 18, exam 48/60 = 48, total 66; overall (58 + 66) / 2 = 62%",
      english is not None and same(english[6], 66) and same(bola_page["summary"]["Percentage"].rstrip("%"), 62.0, 0.051)
      and bola_page["summary"]["Overall grade"] == "B — Very Good", str(bola_page["summary"]))
check("…Bola's unreleased practice result did not hold her card back", ("BOLA", TERM) in READY and bola_page["rows"] != [])

# ---- one card per term: Bola's Second Term and the next session
t2_page, _ = verify_card("Bola's Second Term card (separate from her First)", bola_portal, s_view(CUR, "second-term"),
                         s_view(CUR, "second-term") + "/pdf", "BOLA_T2", "Second Term")
check("…Second Term shows 18 + 10 CA, 45 exam, 73, and none of the First Term work", t2_page["rows"] and same(t2_page["rows"][0][6], 73)
      and same(t2_page["rows"][0][2], 10.0) and t2_page["title"] == "Second Term Report Card", str(t2_page["rows"]))
s2_page, _ = verify_card("Bola's card in the next session (she is in no class there)", bola_portal, s_view(NEXT), s_view(NEXT) + "/pdf",
                         "BOLA_S2", TERM)
check("…it carries no class, and the 2027/2028 session", "Class" not in s2_page["student"] and "2027/2028" in s2_page["term_line"], str(s2_page["student"]))
check("…and the First Term card has no trace of the other term's or the other session's assignment (4.67, not 10)",
      same(by_name["Mathematics"][2], 4.67))

# ---- the grade boundaries: seven subjects on one card
tobi_page, tobi_pdf = verify_card("Tobi's card (every grade boundary)", op, staff_view(SID["TOBI"]), staff_view(SID["TOBI"]) + "/pdf",
                                  "TOBI", TERM, extra={"student": "Tobi Boundary", "class": "JSS 2"})
grades = {r[0]: (r[6], r[8], r[9]) for r in tobi_page["rows"]}
check("boundaries: 70 is A Excellent; 69.99 is B Very Good; 60 is B; 50 is C Good; 45 is D Fair; 40 is E Pass; 39.99 is F Fail",
      grades == {"Boundary 70": (70.0, "A", "Excellent"), "Boundary 69.99": (69.99, "B", "Very Good"), "Boundary 60": (60.0, "B", "Very Good"),
                 "Boundary 50": (50.0, "C", "Good"), "Boundary 45": (45.0, "D", "Fair"), "Boundary 40": (40.0, "E", "Pass"),
                 "Boundary 39.99": (39.99, "F", "Fail")}, str(grades))
check("…and the PDF says the same grades and remarks",
      all(f"{g}" in tobi_pdf["lines"] and rem in tobi_pdf["lines"] for _, g, rem in grades.values()))
check("Tobi's overall 374.98 / 7 = 53.57%, grade C Good; the JSS 2 class average is (53.57 + 20.01) / 2 = 36.79 over two students",
      same(tobi_page["summary"]["Percentage"].rstrip("%"), 53.57, 0.051) and tobi_page["summary"]["Overall grade"] == "C — Good"
      and same(class_model("TOBI", TERM)[0], 36.79) and class_model("TOBI", TERM)[1] == 2
      and tobi_page["summary"].get("Class average (2 students)") is not None, str(tobi_page["summary"]))
overall_boundaries = {70: ("A", "Excellent"), 69.99: ("B", "Very Good"), 60: ("B", "Very Good"), 59.99: ("C", "Good"), 50: ("C", "Good"),
                      49.99: ("D", "Fair"), 45: ("D", "Fair"), 44.99: ("E", "Pass"), 40: ("E", "Pass"), 39.99: ("F", "Fail"), 0: ("F", "Fail")}
check("the overall grade uses the same scale at every boundary (grade_for)",
      all(RCD.grade_for(p) == v for p, v in overall_boundaries.items()), str({p: RCD.grade_for(p) for p in overall_boundaries}))
check("…and the grading key printed on every card lists all six grades", [g["grade"] for g in RCD.grading_key()] == list("ABCDEF"))

# ---- Ife: the other student in JSS 2, a subject a classmate does not have
ife_page, _ = verify_card("Ife's card (JSS 2, two subjects)", op, staff_view(SID["IFE"]), staff_view(SID["IFE"]) + "/pdf", "IFE", TERM)
check("Boundary 70's class average is (70 + 20.02) / 2 = 45.01 and Boundary 60's is (60 + 20) / 2 = 40",
      {r[0]: r[7] for r in ife_page["rows"]} == {"Boundary 60": 40.0, "Boundary 70": 45.01}, str(ife_page["rows"]))
check("Uche's unreleased result is in nobody's figures and Uche has no card", class_model("IFE", TERM)[1] == 2
      and op.get(staff_view(SID["UCHE"])).status_code == 302 and op.get(staff_view(SID["UCHE"]) + "/pdf").status_code == 404)

# ---- a hostile name shows literally and safely
host_page, host_pdf = verify_card("the hostile-named student's card", op, staff_view(SID["HOST"]), staff_view(SID["HOST"]) + "/pdf", "HOST",
                                  TERM, extra={"student": HOSTILE_NAME})
raw = host_page["raw"]
check("the hostile name is escaped in the page (no live <b> tag, the & and quotes safe) and shows literally to the reader",
      "<b>x</b>" not in raw and "&lt;b&gt;x&lt;/b&gt;" in raw and host_page["student"].get("Name") == HOSTILE_NAME and "&amp;" in raw)
title_tag = html.unescape(re.search(r"<title>(.*?)</title>", raw, re.S).group(1))
check("…in the browser tab's title and the picture caption too (escaped, then read back as the same text)",
      title_tag == f"First Term Report Card — {HOSTILE_NAME} — Alpha School" and "<b>" not in re.search(r"<title>(.*?)</title>", raw, re.S).group(1))
check("…and literally, as text, in the PDF", HOSTILE_NAME in host_pdf["lines"] and "<b>" not in host_pdf["text"].replace(HOSTILE_NAME, ""))
check("…the downloaded file's name stays safe", pdf_headers_ok(op.get(staff_view(SID["HOST"]) + "/pdf")))
check("the staff list shows the hostile name as plain text, not a tag",
      HOSTILE_NAME in staff_list(op, J1) and "<b>x</b>" not in op.text(f"{RC}?class_id={J1}&session_id={CUR}&term=First%20Term"))

# ---- who is in the class figures: Gone (withdrawn) and Dayo (waiting) are not
check("a withdrawn student's released results are not in the class average or the class size",
      class_model("ADA", TERM)[1] == 4 and page["summary"].get("Class average (4 students)") is not None)

# ================================================================ D (first part). Nothing set for the head yet
check("the staff page warns that the head's name and signature are not set",
      "The head's name and signature are not set" in html.unescape(op.text(f"{RC}?class_id={J1}")))
check("with nothing set the card says 'Head of School' and prints a blank line (no head name, no head signature)",
      page["signs"][1]["role"] == "Head of School" and page["signs"][1]["who"] == "" and page["signs"][1]["image"] is None
      and "Head of School" in pdf["lines"] and not draws(pdf, ORANGE) and not draws(pdf, NAVY), str(page["signs"]))
check("…and with no comment yet the teacher line says 'Class Teacher' and the comment box is empty",
      page["comment"] == "" and page["signs"][0]["who"] == "Class Teacher" and page["signs"][0]["image"] is None
      and "Class Teacher's Comment" in pdf["lines"], str(page["signs"]))
check("…the card is complete without a comment or any signature: it is not held back", ("ADA", TERM) in READY)

# ================================================================ A. a late result takes the card away again, and comes back
manual_url = "/admin/school/results/manual/new"
op.post(manual_url, {"class_id": J1, "session_id": CUR, "student_id": ADA, "subject_id": ENGLISH, "term": TERM, "took_test": "yes",
                     "test_score": "16", "test_max": "20", "exam_score": "42", "exam_max": "60"}, page=manual_url)
late = alpha.sql("SELECT id, component_name FROM school_student_results WHERE student_id = :s AND subject_id = :j ORDER BY id", s=ADA, j=ENGLISH)
check("a late English test and exam were entered by hand for Ada (waiting to be verified)", len(late) == 2
      and {status_of(x[0]) for x in late} == {"entered"})
READY.discard(("ADA", TERM))
ada_snapshot("Ada, two late English results entered", False, waiting=2)
check("…the class figures no longer count her while she waits: Maths class average is over three students",
      class_model("BOLA", TERM)[1] == 3 and same(class_model("BOLA", TERM)[0], 50.33), str(class_model("BOLA", TERM)))
bola_now, _ = verify_card("Bola's card while Ada waits (class of three)", bola_portal, s_view(CUR), s_view(CUR) + "/pdf", "BOLA", TERM)

# a teacher writes the comment now: comments can be written before release
r = teacher_a.post(RC + "/my-signature", {"action": "draw", "signature_data_url": drawn(GREEN_SIGN)}, page=RC + "/my-signature")
check("teacher A draws her signature", r.status_code == 302 and (admin_signature(TEACHER_A) or "").startswith("uploads/signatures/"))
A_FIRST_FILE = admin_signature(TEACHER_A)
COMMENT_ADA = "Ada is a joy to teach and works hard."
r = post_comments(teacher_a, J1, {ADA: COMMENT_ADA})
check("teacher A writes Ada's comment while her results are still unreleased", comment_row(ADA) == (COMMENT_ADA, TEACHER_A) and r.status_code == 302,
      str(comment_row(ADA)))
for late_id, _ in late:
    workflow(late_id, "verify")
    workflow(late_id, "approve")
    workflow(late_id, "release")
SPEC[("ADA", TERM)]["English Language"] = ADA_ENGLISH
READY.add(("ADA", TERM))
ada_snapshot("Ada, the English results released too", True)

page, pdf = verify_card("Ada's finished card (Maths and English)", ada_portal, s_view(CUR), s_view(CUR) + "/pdf", "ADA", TERM,
                        extra={"student": "Ada Obi", "class": "JSS 1"})
check("Ada's card now has both subjects: Maths 86, English 16 + 42 = 58; overall (86 + 58) / 2 = 72%, grade A",
      [r[0] for r in page["rows"]] == ["English Language", "Mathematics"] and same(page["summary"]["Percentage"].rstrip("%"), 72.0, 0.051)
      and page["summary"]["Overall grade"] == "A — Excellent", str(page["summary"]))
check("…class average (72 + 62 + 41 + 48) / 4 = 55.75% over four students; English class average (58 + 66) / 2 = 62",
      same(class_model("ADA", TERM)[0], 55.75) and class_model("ADA", TERM)[1] == 4 and {r[0]: r[7] for r in page["rows"]}["English Language"] == 62.0,
      str(class_model("ADA", TERM)))

# ================================================================ C. the comment and the signature
check("the comment shows with its author's name on the page and the PDF",
      page["comment"] == COMMENT_ADA and page["signs"][0]["who"] == "Mrs Amaka Tutor" and page["signs"][0]["role"] == "Class Teacher"
      and COMMENT_ADA in pdf["lines"] and "Mrs Amaka Tutor" in pdf["lines"], str(page["signs"]))
check("…and with teacher A's own signature: green in the page and in the PDF",
      page["signs"][0]["image"] and pixel_of(page["signs"][0]["image"]) == (20, 160, 60) and draws(pdf, GREEN))
check("teacher B has no signature of her own yet and has not touched the comment", admin_signature(TEACHER_B) in (None, ""))
r = teacher_b.post(RC + "/my-signature", {"action": "upload"}, page=RC + "/my-signature", files={"signature_file": ("mine.png", PURPLE_SIGN)})
B_FILE = admin_signature(TEACHER_B)
check("teacher B uploads her signature: a separate file from A's", r.status_code == 302 and B_FILE and B_FILE != admin_signature(TEACHER_A)
      and admin_signature(TEACHER_A) == A_FIRST_FILE)
_, pdf_still = verify_card("Ada's card after B added her signature", ada_portal, s_view(CUR), s_view(CUR) + "/pdf", "ADA", TERM)
check("…Ada's card still carries A's signature and not B's", draws(pdf_still, GREEN) and not draws(pdf_still, PURPLE))

# saving the page unchanged does not take over the comment (a browser sends line breaks as CRLF)
values = boxes(teacher_b, J1)
r = post_comments(teacher_b, J1, values)
check("teacher B saves the whole comments page without changing anything: the comment keeps its author, A",
      comment_row(ADA) == (COMMENT_ADA, TEACHER_A) and "no comment was changed" in teacher_b.said(), str(comment_row(ADA)))
r = post_comments(teacher_b, J1, {**values, ADA: "Ada is a joy to teach and works very hard."})
check("teacher B edits Ada's comment: B takes over as its author", comment_row(ADA) == ("Ada is a joy to teach and works very hard.", TEACHER_B),
      str(comment_row(ADA)))
page, pdf = verify_card("Ada's card after B edited the comment", ada_portal, s_view(CUR), s_view(CUR) + "/pdf", "ADA", TERM)
check("…the card shows B's name and B's purple signature, and no longer A's",
      page["signs"][0]["who"] == "Mr Babatunde Guide" and pixel_of(page["signs"][0]["image"]) == (120, 30, 160)
      and page["comment"].endswith("works very hard.") and draws(pdf, PURPLE) and not draws(pdf, GREEN)
      and "Mr Babatunde Guide" in pdf["lines"] and "Mrs Amaka Tutor" not in pdf["lines"], str(page["signs"]))

# a comment typed with plain line breaks, saved again by a browser, keeps its author
alpha.sql("UPDATE report_card_comments SET comment = :c, author_admin_id = :a WHERE student_id = :s", c="First line.\nSecond line.", a=TEACHER_A, s=ADA)
values = boxes(teacher_b, J1)
post_comments(teacher_b, J1, values)
check("a two-line comment stored with plain line breaks, resaved untouched by another teacher's browser (CRLF), keeps its author",
      comment_row(ADA)[1] == TEACHER_A, str(comment_row(ADA)))
post_comments(teacher_b, J1, {**boxes(teacher_b, J1), ADA: "Ada is a joy to teach and works very hard."})

# Bola and Chidi, and the clearing / limit / scope rules
post_comments(teacher_a, J1, {SID["BOLA"]: "Bola is bright and helpful."})
check("teacher A writes Bola's comment", comment_row(SID["BOLA"]) == ("Bola is bright and helpful.", TEACHER_A))
bola_page, bola_pdf = verify_card("Bola's card with A's comment", bola_portal, s_view(CUR), s_view(CUR) + "/pdf", "BOLA", TERM)
check("Bola's card carries A's name and A's signature, while Ada's carries B's (each teacher's own)",
      bola_page["signs"][0]["who"] == "Mrs Amaka Tutor" and pixel_of(bola_page["signs"][0]["image"]) == (20, 160, 60)
      and draws(bola_pdf, GREEN) and not draws(bola_pdf, PURPLE))
before = (comment_row(ADA), comment_row(SID["BOLA"]))
too_long = "x" * 1001
r = post_comments(teacher_a, J1, {SID["BOLA"]: "Bola has changed her mind entirely.", SID["CHIDI"]: too_long})
check("a comment over 1000 characters is refused and NOTHING in the same submission is saved (Bola's valid edit is not either)",
      (comment_row(ADA), comment_row(SID["BOLA"])) == before and comment_row(SID["CHIDI"]) is None
      and "at most 1000" in teacher_a.said().replace(",", ""), str(comment_row(SID["BOLA"])))
exact = "y" * 1000
post_comments(teacher_a, J1, {SID["CHIDI"]: exact})
check("a comment of exactly 1000 characters is accepted", comment_row(SID["CHIDI"]) == (exact, TEACHER_A))
post_comments(teacher_a, J1, {SID["CHIDI"]: ""})
check("clearing a box removes the comment", comment_row(SID["CHIDI"]) is None)
chidi_page, chidi_pdf = verify_card("Chidi's card with no comment", chidi_portal, s_view(CUR), s_view(CUR) + "/pdf", "CHIDI", TERM)
check("…the card still appears, with an empty comment box and 'Class Teacher' on a blank signature line, page and PDF",
      chidi_page["comment"] == "" and chidi_page["signs"][0]["who"] == "Class Teacher" and chidi_page["signs"][0]["image"] is None
      and "Class Teacher's Comment" in chidi_pdf["lines"] and not draws(chidi_pdf, GREEN) and not draws(chidi_pdf, PURPLE))
before = (comment_row(SID["BOLA"]), comment_row(ADA))
post_comments(teacher_a, J1, {SID["BOLA"]: "Words", SID["TOBI"]: "A JSS 2 pupil, not in this class", 999999: "nobody"})
check("boxes for students who are not in the class are ignored", comment_row(SID["TOBI"]) is None and comment_row(999999) is None
      and comment_row(SID["BOLA"]) == ("Words", TEACHER_A))
post_comments(teacher_a, J1, {SID["BOLA"]: "Bola is bright and helpful."})

# a teacher with no signature gets a blank line and her name
post_comments(teacher_c, J2, {SID["TOBI"]: "Tobi has met every target."})
tobi_page, tobi_pdf = verify_card("Tobi's card with C's comment (C has no signature)", op, staff_view(SID["TOBI"]), staff_view(SID["TOBI"]) + "/pdf",
                                  "TOBI", TERM)
check("a teacher with no signature: the card shows her name, 'Class Teacher', a blank signature line and no picture of hers",
      tobi_page["signs"][0]["who"] == "Ms Chioma Mentor" and tobi_page["signs"][0]["image"] is None
      and tobi_page["comment"] == "Tobi has met every target." and len(tobi_pdf["images"]) == 1 and "Ms Chioma Mentor" in tobi_pdf["lines"],
      f"{tobi_page['signs']} images={len(tobi_pdf['images'])}")
check("the comments page tells her she has no signature yet", "have not saved a signature" in html.unescape(teacher_c.text(comments_url(J2))))

# ---- replacing and removing a signature deletes the old file; bad signatures change nothing
folder = os.path.join(alpha.uploads(), "signatures")
before_files = signature_files(alpha)
r = teacher_a.post(RC + "/my-signature", {"action": "upload"}, page=RC + "/my-signature", files={"signature_file": ("new.png", TEAL_SIGN)})
after_files = signature_files(alpha)
A_SECOND_FILE = admin_signature(TEACHER_A)
check("teacher A replaces her signature by uploading a new one: the old file is deleted from disk, the new one is there, B's is untouched",
      A_SECOND_FILE and A_SECOND_FILE != A_FIRST_FILE and not os.path.exists(os.path.join(alpha.uploads(), A_FIRST_FILE[len("uploads/"):]))
      and os.path.isfile(os.path.join(alpha.uploads(), A_SECOND_FILE[len("uploads/"):])) and len(after_files) == len(before_files)
      and os.path.isfile(os.path.join(alpha.uploads(), B_FILE[len("uploads/"):])), f"{before_files} -> {after_files}")
_, bola_pdf2 = verify_card("Bola's card after A changed her signature", bola_portal, s_view(CUR), s_view(CUR) + "/pdf", "BOLA", TERM)
check("…Bola's card (A's comment) now shows A's new teal signature at once, and no longer the green one",
      draws(bola_pdf2, TEAL) and not draws(bola_pdf2, GREEN))
kept = (admin_signature(TEACHER_A), signature_files(alpha))
refused = {
    "a fake image (text with an image name)": ("upload", {"signature_file": ("sig.png", b"this is not a picture at all")}, "valid image"),
    "a file that is not an image type": ("upload", {"signature_file": ("notes.txt", b"hello")}, "png"),
    "no file at all": ("upload", None, "choose an image"),
    "a drawing that is not a PNG": ("draw", drawn(b"not a png"), "could not be read"),
    "an empty drawing": ("draw", "", "could not be read"),
    "a drawing that is not an image address": ("draw", "javascript:alert(1)", "could not be read"),
    "a made-up action": ("explode", None, "unrecognised"),
}
for label, (action, payload, phrase) in refused.items():
    if action == "draw":
        r = teacher_a.post(RC + "/my-signature", {"action": "draw", "signature_data_url": payload}, page=RC + "/my-signature")
    elif action == "upload":
        r = teacher_a.post(RC + "/my-signature", {"action": "upload"}, page=RC + "/my-signature", files=payload)
    else:
        r = teacher_a.post(RC + "/my-signature", {"action": action}, page=RC + "/my-signature")
    check(f"{label} is refused with a message, and the signature and the files on disk are unchanged",
          r.status_code in (400, 200) and phrase in html.unescape(r.get_data(as_text=True)).lower()
          and (admin_signature(TEACHER_A), signature_files(alpha)) == kept, f"{r.status_code}")
r = teacher_a.post(RC + "/my-signature", {"action": "draw", "signature_data_url": drawn(GREEN_SIGN)}, page=RC + "/my-signature")
check("drawing again replaces the uploaded one (its file is deleted too)", admin_signature(TEACHER_A) not in (None, A_SECOND_FILE)
      and not os.path.exists(os.path.join(alpha.uploads(), A_SECOND_FILE[len("uploads/"):])) and len(signature_files(alpha)) == len(after_files))
r = teacher_b.post(RC + "/my-signature", {"action": "remove"}, page=RC + "/my-signature")
check("removing a signature deletes its file and clears the record; A's is not touched",
      admin_signature(TEACHER_B) in ("", None) and not os.path.exists(os.path.join(alpha.uploads(), B_FILE[len("uploads/"):]))
      and (admin_signature(TEACHER_A) or "").startswith("uploads/signatures/") and len(signature_files(alpha)) == len(after_files) - 1)
page, pdf = verify_card("Ada's card after B removed her signature", ada_portal, s_view(CUR), s_view(CUR) + "/pdf", "ADA", TERM)
check("…Ada's card keeps B's name and comment, with a blank signature line",
      page["signs"][0]["who"] == "Mr Babatunde Guide" and page["signs"][0]["image"] is None and not draws(pdf, PURPLE)
      and "Mr Babatunde Guide" in pdf["lines"])
page_text = html.unescape(teacher_a.text(RC + "/my-signature"))
check("the signature page shows the current signature and can only ever show one's own",
      "Current signature" in page_text and "Remove signature" in page_text and (admin_signature(TEACHER_A) or "x") in page_text
      and (B_FILE or "y") not in page_text)

# ================================================================ C2. affective and psychomotor traits
def trait_pairs(body):
    """{trait label: rating text} from the rc-traits section of a report card page."""
    return dict(re.findall(r'<td>([^<]*)</td><td class="rating">([^<]*)</td>', body))


before_ada, before_pdf = ada_portal.text(s_view(CUR)), ada_portal.get(s_view(CUR) + "/pdf").data
check("before any trait is rated, a card shows no traits section at all (a school that never uses this sees no change)",
      "Affective Domain" not in before_ada and "Affective Domain" not in pdf_read(before_pdf)["text"])

traits_page = html.unescape(teacher_a.text(traits_url(J1)))
check("the traits page lists Ada and every catalogued trait with its five ratings",
      "Ada Obi" in traits_page and "Punctuality" in traits_page and "Psychomotor Domain" in traits_page
      and all(w in traits_page for w in ("Excellent", "Very Good", "Good", "Fair", "Poor")))

r = post_traits(teacher_a, J1, {ADA: {"punctuality": 5, "neatness": 3}})
check("teacher A rates two of Ada's traits; the rest stay unrated, and it is recorded under her name",
      r.status_code == 302 and trait_row(ADA) == ({"punctuality": 5, "neatness": 3}, TEACHER_A) and "1 student" in teacher_a.said())

after_ada = ada_portal.text(s_view(CUR))
after_pairs = trait_pairs(after_ada)
after_pdf_text = pdf_read(ada_portal.get(s_view(CUR) + "/pdf").data)["text"]
check("Ada's card now shows both domains: the two rated traits with their labels, and everything else as unrated",
      after_pairs.get("Punctuality") == "Excellent" and after_pairs.get("Neatness") == "Good"
      and after_pairs.get("Honesty") == "—" and after_pairs.get("Handwriting") == "—"
      and "Affective Domain" in after_ada and "Psychomotor Domain" in after_ada
      and all(w in after_pdf_text for w in ("Affective Domain", "Psychomotor Domain", "Punctuality", "Excellent")))

r = post_traits(teacher_b, J1, {ADA: {"punctuality": 5, "neatness": 3}})
check("teacher B saves the same ratings unchanged: the row keeps teacher A as its rater",
      trait_row(ADA) == ({"punctuality": 5, "neatness": 3}, TEACHER_A) and "no rating was changed" in teacher_b.said())

r = post_traits(teacher_b, J1, {ADA: {"punctuality": 4, "neatness": 3}})
check("teacher B changes one rating: the row now takes over under her name",
      trait_row(ADA) == ({"punctuality": 4, "neatness": 3}, TEACHER_B) and "1 student" in teacher_b.said())

kept_tobi_before = trait_row(SID["TOBI"])
r = post_traits(teacher_a, J2, {SID["TOBI"]: {"punctuality": 5}})
check("teacher A (JSS 1 only) cannot rate a JSS 2 student: nothing is written for Tobi",
      trait_row(SID["TOBI"]) == kept_tobi_before)

r = post_traits(teacher_b, J1, {ADA: {}})
check("clearing every trait removes the row entirely, and the card goes back to showing nothing",
      trait_row(ADA) is None and "1 cleared" in teacher_b.said()
      and "Affective Domain" not in ada_portal.text(s_view(CUR)))

check("viewer (report_cards.view only) is refused (403) opening and posting to the traits page, and nothing is written",
      viewer.get(traits_url(J1)).status_code == 403
      and viewer.post(traits_url(J1), {f"trait_{ADA}_punctuality": "5"}, page="/admin/password").status_code == 403
      and trait_row(ADA) is None)
check("a member of staff with no report card permission at all is refused (403) too",
      nobody.get(traits_url(J1)).status_code == 403)
check("the traits routes carry the same permission as comments in the endpoint map",
      ADMIN_ENDPOINT_PERMISSIONS.get("admin_school_report_card_traits") == "report_cards.comment"
      and ADMIN_ENDPOINT_PERMISSIONS.get("admin_school_report_card_traits_save") == "report_cards.comment")
early_visitor = Person(ALPHA, label="signed-out visitor (traits)")
check("a signed-out visitor is sent to sign in, never shown the traits page, and a student cannot open it",
      early_visitor.get(traits_url(J1)).status_code == 302 and "login" in early_visitor.get(traits_url(J1)).headers["Location"]
      and ada_portal.get(traits_url(J1)).status_code in (302, 403, 404))
check("a POST to the traits page without a CSRF token is refused (403), and nothing changes",
      op.post(traits_url(J1), {f"trait_{ADA}_punctuality": "5"}, token=False).status_code == 403 and trait_row(ADA) is None)
# Ada is left unrated again here (trait_row(ADA) is None), so every later section sees exactly the same
# card it always saw, undisturbed by this new feature.

# ================================================================ D. the head
op.said()
r = op.post(RC + "/settings", {"head_title": "Proprietress", "head_name": "Mrs A. B. Okoye", "next_term_begins": "Monday, 4 January 2027"},
            page=RC + "/settings")
check("the head's title, name and next-term date are saved", r.status_code == 302 and head_setting(alpha, "report_head_title") == "Proprietress"
      and head_setting(alpha, "report_head_name") == "Mrs A. B. Okoye" and head_setting(alpha, "report_head_signature") in (None, ""))
staff_html = html.unescape(op.text(f"{RC}?class_id={J1}"))
check("the staff page now warns only about the signature", "The head's signature is not set" in staff_html and "name and signature" not in staff_html)
page, pdf = verify_card("Ada's card with the head's name", ada_portal, s_view(CUR), s_view(CUR) + "/pdf", "ADA", TERM,
                        extra={"next_term": "Monday, 4 January 2027"})
check("the head's title and name are on the card (page and PDF), the signature line still blank",
      page["signs"][1]["role"] == "Proprietress" and page["signs"][1]["who"] == "Mrs A. B. Okoye" and page["signs"][1]["image"] is None
      and "Proprietress" in pdf["lines"] and "Mrs A. B. Okoye" in pdf["lines"] and "Head of School" not in pdf["lines"])
r = op.post(RC + "/settings", {"action": "draw", "signature_data_url": drawn(ORANGE_SIGN)}, page=RC + "/settings")
H_FIRST = head_setting(alpha, "report_head_signature")
check("the head's signature is drawn and stored", r.status_code == 302 and (H_FIRST or "").startswith("uploads/signatures/"))
check("…the warning goes away", "The head's" not in html.unescape(op.text(f"{RC}?class_id={J1}")))
page, pdf = verify_card("Ada's card with the head's signature", ada_portal, s_view(CUR), s_view(CUR) + "/pdf", "ADA", TERM)
check("the head's orange signature is on the page and drawn in the PDF", pixel_of(page["signs"][1]["image"]) == (240, 120, 10) and draws(pdf, ORANGE))
r = op.post(RC + "/settings", {"action": "upload"}, page=RC + "/settings", files={"signature_file": ("head.png", NAVY_SIGN)})
H_SECOND = head_setting(alpha, "report_head_signature")
check("uploading a new head's signature replaces it and deletes the old file",
      H_SECOND and H_SECOND != H_FIRST and not os.path.exists(os.path.join(alpha.uploads(), H_FIRST[len("uploads/"):]))
      and os.path.isfile(os.path.join(alpha.uploads(), H_SECOND[len("uploads/"):])))
kept = (head_setting(alpha, "report_head_signature"), head_setting(alpha, "report_head_name"), signature_files(alpha))
for label, form, files in (("a fake image", {"action": "upload"}, {"signature_file": ("h.png", b"not an image")}),
                           ("an empty drawing", {"action": "draw", "signature_data_url": ""}, None),
                           ("a drawing that is not a PNG", {"action": "draw", "signature_data_url": drawn(b"nope")}, None),
                           ("a title of 41 characters", {"head_title": "T" * 41, "head_name": "Changed", "next_term_begins": "x"}, None),
                           ("a name of 81 characters", {"head_title": "Principal", "head_name": "N" * 81, "next_term_begins": "x"}, None),
                           ("a next-term line of 81 characters", {"head_title": "Principal", "head_name": "Changed", "next_term_begins": "d" * 81}, None)):
    r = op.post(RC + "/settings", form, page=RC + "/settings", files=files)
    check(f"the head settings refuse {label}, and nothing is changed",
          r.status_code == 400 and (head_setting(alpha, "report_head_signature"), head_setting(alpha, "report_head_name"), signature_files(alpha)) == kept
          and head_setting(alpha, "report_head_title") == "Proprietress", f"{r.status_code}")
page, pdf = verify_card("Ada's card with the head's navy signature", ada_portal, s_view(CUR), s_view(CUR) + "/pdf", "ADA", TERM)
check("…the card wears the new head's signature and not the old", pixel_of(page["signs"][1]["image"]) == (10, 20, 120)
      and draws(pdf, NAVY) and not draws(pdf, ORANGE))
r = op.post(RC + "/settings", {"head_title": "__other__", "head_title_custom": "Chairman of the Board", "head_name": "Chief Z. Y. Bello",
                               "next_term_begins": ""}, page=RC + "/settings")
page, pdf = verify_card("Ada's card with a custom title", ada_portal, s_view(CUR), s_view(CUR) + "/pdf", "ADA", TERM)
check("a custom title works: 'Chairman of the Board' and the new name are on the card; with no date, no 'Next term begins' line",
      page["signs"][1]["role"] == "Chairman of the Board" and page["signs"][1]["who"] == "Chief Z. Y. Bello" and page["next_term"] == ""
      and "Chairman of the Board" in pdf["lines"] and not any(l.startswith("Next term begins") for l in pdf["lines"]))
settings_html = op.text(RC + "/settings")
check("the settings page shows the saved values and the head's current signature",
      "Chief Z. Y. Bello" in settings_html and "Chairman of the Board" in settings_html and H_SECOND in settings_html)
op.post(RC + "/settings", {"head_title": "Proprietress", "head_name": "Mrs A. B. Okoye", "next_term_begins": "Monday, 4 January 2027"}, page=RC + "/settings")
for who_label, viewer_p, url, key in (("Bola", bola_portal, s_view(CUR), "BOLA"), ("Chidi", chidi_portal, s_view(CUR), "CHIDI")):
    pg, pf = verify_card(f"{who_label}'s card carries the head too", viewer_p, url, url + "/pdf", key, TERM)
    check(f"{who_label}'s card: head title, name and navy signature, in the page and the PDF",
          pg["signs"][1]["role"] == "Proprietress" and pg["signs"][1]["who"] == "Mrs A. B. Okoye" and pixel_of(pg["signs"][1]["image"]) == (10, 20, 120)
          and "Proprietress" in pf["lines"] and "Mrs A. B. Okoye" in pf["lines"] and draws(pf, NAVY))
for label, viewer_p, url, key in (("Tobi, in JSS 2", op, staff_view(SID["TOBI"]), "TOBI"),):
    pg, pf = verify_card(f"{label}", viewer_p, url, url + "/pdf", key, TERM)
    check(f"{label}: the head is on every class's cards", pg["signs"][1]["who"] == "Mrs A. B. Okoye" and draws(pf, NAVY))

# ================================================================ E. branding
page, pdf = verify_card("Ada's card, for the branding checks", ada_portal, s_view(CUR), s_view(CUR) + "/pdf", "ADA", TERM)
check("alpha's card carries alpha's name, motto, address, phone and email (page and PDF)",
      page["school"] == "Alpha School" and page["motto"] == "Excellence in Learning" and "12 Palm Avenue, Ikeja, Lagos" in page["contact"]
      and "Tel: 0803 111 2222" in page["contact"] and "info@alpha-school.example" in page["contact"]
      and all(s in pdf["lines"] for s in ("Alpha School", "Excellence in Learning", "12 Palm Avenue, Ikeja, Lagos"))
      and any("0803 111 2222" in l for l in pdf["lines"]) and any("info@alpha-school.example" in l for l in pdf["lines"]), str(page["contact"]))
check("…its red logo is embedded in the page and drawn in the PDF, and nobody else's",
      page["logo"] and pixel_of(page["logo"]) == (200, 30, 30) and draws(pdf, RED) and not draws(pdf, BLUE) and not draws(pdf, OLD))
check("…in its own brand colours, in the page's style and in the PDF's fills",
      page["colour"].lower() == ALPHA_PRIMARY and page["accent"].lower() == ALPHA_ACCENT
      and hex_rgb(ALPHA_PRIMARY) in pdf["colours"] and hex_rgb(ALPHA_ACCENT) in pdf["colours"]
      and hex_rgb(BETA_PRIMARY) not in pdf["colours"] and hex_rgb(BETA_ACCENT) not in pdf["colours"], f"{page['colour']} {sorted(pdf['colours'])[:6]}")
check("…and nothing of the school this platform grew out of, on the page or in the PDF",
      not any(w in page["raw"] or w in pdf["text"] for w in OLD_SCHOOL_WORDS) and "school_logo.png" not in page["raw"])

# Beta: a different logo, colours and details, its own head, a student and a teacher of its own
BETA_MATHS = mk_subjects(beta, ["Mathematics"])[0]
BETA_J1 = beta.one("SELECT id FROM school_classes WHERE name = 'JSS 1'")
BETA_CUR = beta.one("SELECT id FROM academic_sessions WHERE is_current = 1")
CHIKE = mk_students(beta, [("Chike", "Betaman", BETA_J1, BETA_CUR, {"login": "chike"})])[0]
put(beta, CHIKE, BETA_MATHS, {"T": [(9, 10)], "E": [(48, 60)]}, session=BETA_CUR, class_id=BETA_J1, admin_id=beta.one("SELECT id FROM admins ORDER BY id LIMIT 1"))
beta_teacher, BETA_TEACHER = mk_staff(beta, "beta.teacher", "Mr Beta Tutor", "Primary Class Teacher", "JSS 1")
beta_teacher.post(RC + "/my-signature", {"action": "draw", "signature_data_url": drawn(PINK_SIGN)}, page=RC + "/my-signature")
beta_teacher.post(comments_url(BETA_J1, BETA_CUR), {f"comment_{CHIKE}": "Chike is a fine student."}, page=comments_url(BETA_J1, BETA_CUR))
op_beta.post(RC + "/settings", {"head_title": "Principal", "head_name": "Dr Beta Head", "next_term_begins": "Tuesday, 5 January 2027"}, page=RC + "/settings")
op_beta.post(RC + "/settings", {"action": "draw", "signature_data_url": drawn(GREY_SIGN)}, page=RC + "/settings")
chike_portal = Person(BETA, "/student/password", label="beta student Chike")
chike_portal.post("/login", {"username": "chike", "password": PASSWORD}, page="/login")
bp = chike_portal.get(f"/student/report-cards/{BETA_CUR}/{SLUG}")
bpdf = chike_portal.get(f"/student/report-cards/{BETA_CUR}/{SLUG}/pdf")
bpage, bpdfd = parse_page(bp.get_data(as_text=True)), pdf_read(bpdf.data)
check("beta's card opens for beta's student", bp.status_code == 200 and pdf_headers_ok(bpdf))
check("beta's card carries beta's name, motto, address, phone, email, head and teacher (page and PDF)",
      bpage["school"] == "Beta College" and bpage["motto"] == "Knowledge is Light" and "4 River Road, Abuja" in bpage["contact"]
      and "Tel: 0809 555 6666" in bpage["contact"] and "hello@beta-college.example" in bpage["contact"]
      and bpage["signs"][0]["who"] == "Mr Beta Tutor" and bpage["signs"][1]["who"] == "Dr Beta Head" and bpage["signs"][1]["role"] == "Principal"
      and bpage["next_term"] == "Tuesday, 5 January 2027" and bpage["comment"] == "Chike is a fine student."
      and all(s in bpdfd["lines"] for s in ("Beta College", "Knowledge is Light", "Dr Beta Head", "Principal", "Mr Beta Tutor", "Chike is a fine student.")), str(bpage["signs"]))
check("…beta's blue logo, pink teacher signature and grey head signature, in the page and drawn in the PDF; none of alpha's",
      pixel_of(bpage["logo"]) == (30, 60, 200) and pixel_of(bpage["signs"][0]["image"]) == (230, 60, 140) and pixel_of(bpage["signs"][1]["image"]) == (70, 70, 70)
      and draws(bpdfd, BLUE) and draws(bpdfd, PINK) and draws(bpdfd, GREY)
      and not any(draws(bpdfd, p) for p in (RED, GREEN, TEAL, PURPLE, ORANGE, NAVY, OLD)))
check("…in beta's own colours and none of alpha's",
      bpage["colour"].lower() == BETA_PRIMARY and hex_rgb(BETA_PRIMARY) in bpdfd["colours"] and hex_rgb(ALPHA_PRIMARY) not in bpdfd["colours"]
      and hex_rgb(ALPHA_ACCENT) not in bpdfd["colours"])
alpha_words = ("Alpha", "ALPHA", "Palm Avenue", "0803 111 2222", "Excellence in Learning", "Okoye", "Chairman", "Amaka", "Babatunde", "Ada Obi", "Bola")
check("…and not a word of alpha's, page or PDF, and none of the old school's",
      not any(w in bpage["raw"] or w in bpdfd["text"] for w in alpha_words + OLD_SCHOOL_WORDS),
      str([w for w in alpha_words + OLD_SCHOOL_WORDS if w in bpage["raw"] or w in bpdfd["text"]]))
check("alpha's card, likewise, has none of beta's",
      not any(w in page["raw"] or w in pdf["text"] for w in ("Beta", "River Road", "0809 555 6666", "Knowledge is Light", "Dr Beta Head", "Chike", "Tuesday, 5 January")))
check("each school has its own head settings: alpha's head is not beta's and beta's is not alpha's",
      head_setting(alpha, "report_head_name") == "Mrs A. B. Okoye" and head_setting(beta, "report_head_name") == "Dr Beta Head"
      and head_setting(alpha, "report_head_signature") != head_setting(beta, "report_head_signature"))

# Gamma: no logo, no address, no phone, no email, no motto, no head, no teacher signature
GAMMA_MATHS = mk_subjects(gamma, ["Mathematics"])[0]
GAMMA_J1 = gamma.one("SELECT id FROM school_classes WHERE name = 'JSS 1'")
GAMMA_CUR = gamma.one("SELECT id FROM academic_sessions WHERE is_current = 1")
GINA = mk_students(gamma, [("Gina", "Gammason", GAMMA_J1, GAMMA_CUR, {})])[0]
put(gamma, GINA, GAMMA_MATHS, {"T": [(6, 10)], "E": [(30, 60)]}, session=GAMMA_CUR, class_id=GAMMA_J1, admin_id=gamma.one("SELECT id FROM admins ORDER BY id LIMIT 1"))
gp = op_gamma.get(f"{RC}/{GINA}/{GAMMA_CUR}/{SLUG}")
gpdf = op_gamma.get(f"{RC}/{GINA}/{GAMMA_CUR}/{SLUG}/pdf")
gpage, gpdfd = parse_page(gp.get_data(as_text=True)), pdf_read(gpdf.data)
check("a school with no logo gets its NAME where the logo would be: no logo picture in the page or the PDF, the name as the heading",
      gp.status_code == 200 and gpage["logo"] is None and gpage["school"] == "Gamma Academy" and gpdfd["images"] == []
      and "Gamma Academy" in gpdfd["lines"] and not draws(gpdfd, OLD) and not draws(gpdfd, RED) and not draws(gpdfd, BLUE), f"{len(gpdfd['images'])}")
check("…with no address, phone, email or motto of its own it prints none, and none of anybody else's",
      gpage["contact"] == "" and gpage["motto"] == "" and not any(l.startswith(("Tel:", "Email:")) for l in gpdfd["lines"])
      and not any(w in gpage["raw"] or w in gpdfd["text"] for w in OLD_SCHOOL_WORDS + ("Palm Avenue", "River Road", "Alpha", "Beta")))
check("…and no head set and no teacher signature: 'Head of School', a blank line, and no signature pictures",
      gpage["signs"][1]["role"] == "Head of School" and gpage["signs"][1]["who"] == "" and gpage["signs"][1]["image"] is None
      and gpage["signs"][0]["image"] is None and gpdfd["images"] == [])

# ================================================================ A. terms and sessions are separate; nothing for those with nothing
check("each term is separate: Bola's First and Second Term cards are both ready; Ada's Second Term (never released) is not",
      bola_portal.get(s_view(CUR, "first-term")).status_code == 200 and bola_portal.get(s_view(CUR, "second-term")).status_code == 200
      and ada_portal.get(s_view(CUR, "second-term")).status_code == 404 and ada_portal.get(s_view(CUR, "third-term")).status_code == 404)
put_status_rows(alpha, ADA, MATHS, CUR, "Second Term", [("Test", 5, 10, "entered")])
check("…an unreleased Second Term result for Ada holds back only that term: First Term is still ready, Second Term is not",
      ada_portal.get(s_view(CUR, "first-term")).status_code == 200 and ada_portal.get(s_view(CUR, "second-term")).status_code == 404
      and staff_list(op, J1, "Second Term").get("Ada Obi") == "Waiting for 1 result to be released"
      and staff_list(op, J1, TERM).get("Ada Obi") == "Ready")
dash = bola_portal.text("/student/dashboard")
links = panel_links(dash)
check("Bola's dashboard lists her three cards, the newest session first, each with its own page and PDF link",
      links == sorted([s_view(NEXT), s_view(NEXT) + "/pdf", s_view(CUR, "first-term"), s_view(CUR, "first-term") + "/pdf",
                       s_view(CUR, "second-term"), s_view(CUR, "second-term") + "/pdf"])
      and dash.index(s_view(NEXT)) < dash.index(s_view(CUR, "first-term")) < dash.index(s_view(CUR, "second-term")), str(links))
check("another session is separate: Ada has nothing at all in 2027/2028", ada_portal.get(s_view(NEXT)).status_code == 404
      and NEXT not in [p["session_id"] for p in alpha.run(lambda: RCD.student_periods(ADA, ready_only=False))])
check("a student with no results has no card: Emeka's dashboard has no panel, his page and PDF are a 404 and staff say 'No results yet'",
      not has_panel(emeka_portal.text("/student/dashboard")) and emeka_portal.get(s_view(CUR)).status_code == 404
      and emeka_portal.get(s_view(CUR) + "/pdf").status_code == 404 and staff_list(op, J1).get("Emeka Okoro") == "No results yet")
check("practice tests alone make no card: Fola (only a practice result) has none",
      staff_list(op, J1).get("Fola Ade") == "No results yet" and op.get(staff_view(SID["FOLA"])).status_code == 302
      and alpha.run(lambda: RCD.student_periods(SID["FOLA"], ready_only=False)) == [])
listing = staff_list(op, J1)
check("the staff page: Dayo waits for 3 results (a test entered, an exam approved, another exam verified)",
      listing.get("Dayo Bello") == "Waiting for 3 results to be released", str(listing))
check("…the others in the class read Ready, Ready, Ready; the withdrawn student is not listed at all",
      listing.get("Bola Eze") == listing.get("Chidi Nwosu") == listing.get(HOSTILE_NAME) == "Ready" and "Gone Away" not in listing)
check("Dayo's dashboard has no panel and his card is a 404 while he waits", not has_panel(dayo_portal.text("/student/dashboard"))
      and dayo_portal.get(s_view(CUR)).status_code == 404)
# Dayo's results are released one by one: the count goes down, then the card appears and the class figures change
dayo_rows = alpha.sql("SELECT id, status FROM school_student_results WHERE student_id = :s AND status <> 'released' ORDER BY id", s=SID["DAYO"])
by_status = {status: rid_ for rid_, status in dayo_rows}
workflow(by_status["approved"], "release")
check("one released: the staff page says 'Waiting for 2 results'", staff_list(op, J1).get("Dayo Bello") == "Waiting for 2 results to be released")
workflow(by_status["verified"], "approve")
workflow(by_status["verified"], "release")
check("two released: the staff page says 'Waiting for 1 result' (singular)", staff_list(op, J1).get("Dayo Bello") == "Waiting for 1 result to be released")
workflow(by_status["entered"], "verify")
workflow(by_status["entered"], "approve")
workflow(by_status["entered"], "release")
READY.add(("DAYO", TERM))
check("all released: Dayo's card is ready, on his dashboard, page and PDF", staff_list(op, J1).get("Dayo Bello") == "Ready"
      and has_panel(dayo_portal.text("/student/dashboard")) and dayo_portal.get(s_view(CUR)).status_code == 200)
page, pdf = verify_card("Ada's card once Dayo is in the class figures", ada_portal, s_view(CUR), s_view(CUR) + "/pdf", "ADA", TERM)
check("…Dayo is now in the class average: (72 + 62 + 41 + 48 + 41) / 5 = 52.8% over five students, and Maths (86+58+41+48+52)/5 = 57",
      same(class_model("ADA", TERM)[0], 52.8) and class_model("ADA", TERM)[1] == 5 and page["summary"].get("Class average (5 students)") is not None
      and {r[0]: r[7] for r in page["rows"]}["Mathematics"] == 57.0, str(page["summary"]))

# ---- a release date that has passed releases the approved results with nobody doing anything on the staff side
TIMMY, PIA = SID["TIMMY"], SID["PIA"]
check("Timmy's results are approved and the session's release date is far in the future: no card yet (dashboard, page, PDF)",
      alpha.one("SELECT count(*) FROM school_student_results WHERE student_id = :s AND status = 'approved'", s=TIMMY) == 2
      and not has_panel(timmy_portal.text("/student/dashboard")) and timmy_portal.get(s_view(NEXT)).status_code == 404)
alpha.sql("UPDATE academic_sessions SET result_release_at = '2026-01-01T09:00:00+00:00' WHERE id = :s", s=NEXT)   # time passes; no staff request
dash = timmy_portal.text("/student/dashboard")
check("the release date passes: on Timmy's next visit his dashboard has the 'Report cards' panel", has_panel(dash) and s_view(NEXT) in dash, str(panel_links(dash)))
READY.update({("TIMMY", TERM), ("PIA", TERM)})
tp, tpdf = verify_card("Timmy's card, released by the date alone", timmy_portal, s_view(NEXT), s_view(NEXT) + "/pdf", "TIMMY", TERM)
check("…and it is a real card from then on: his results are 'released' in the database",
      alpha.one("SELECT count(*) FROM school_student_results WHERE student_id = :s AND status = 'released'", s=TIMMY) == 2)
# Timmy's visit released everything that was due in the session, Pia's included. To test the parent's side on its own, put Pia's
# results back to "approved" with the release date in the future, then let the date pass and let the PARENT be the first to look.
alpha.sql("UPDATE school_student_results SET status = 'approved', released_at = NULL WHERE student_id = :s", s=PIA)
alpha.sql("UPDATE academic_sessions SET result_release_at = '2999-01-01T09:00:00+00:00' WHERE id = :s", s=NEXT)
check("Pia's results are approved and the date is in the future: her parent sees no panel yet",
      not has_panel(MUM_PIA.text(f"/parent/children/{PIA}")) and MUM_PIA.get(p_view(PIA, NEXT)).status_code == 404)
alpha.sql("UPDATE academic_sessions SET result_release_at = '2026-01-01T09:00:00+00:00' WHERE id = :s", s=NEXT)
mum = MUM_PIA.text(f"/parent/children/{PIA}")
check("the date passes and Pia's parent is the first to look (no student, no staff): the panel is there on that very visit",
      has_panel(mum) and p_view(PIA, NEXT) in mum, "the parent's child page did not release the results that were due")
check("…and the parent can open Pia's card and PDF", MUM_PIA.get(p_view(PIA, NEXT)).status_code == 200 and MUM_PIA.get(p_view(PIA, NEXT) + "/pdf").status_code == 200)
READY.discard(("TIMMY", TERM))
READY.discard(("PIA", TERM))

# ================================================================ I. robustness: no class, annual, long lists, photographs
check("a student in no class that session still gets a card, with no class average and no class line (Bola in 2027/2028; checked above)",
      "Class" not in s2_page["student"] and not any(k.startswith("Class average") for k in s2_page["summary"]))
ann_page, ann_pdf = verify_card("Ann's annual card (Full Session results)", op, staff_view(SID["ANN"], slug="full-session"),
                                staff_view(SID["ANN"], slug="full-session") + "/pdf", "ANN", "Full Session")
check("Full Session results make an 'Annual Report Card', page and PDF (and a 'Full Session' line, not 'First Term')",
      ann_page["title"] == "Annual Report Card" and "Annual Report Card" in ann_pdf["lines"] and any(l.startswith("Term: Full Session") for l in ann_pdf["lines"])
      and "Full Session" in ann_page["term_line"], str(ann_page["title"]))
check("…and it is the only annual card, with its own class figure (one student)", class_model("ANN", "Full Session") == (52.0, 1, {"Mathematics": 52.0}))
check("Bola's card, with a photograph, shows it in the page and in the PDF (Bola's card has a photograph and A's signature more than Chidi's, which has neither)",
      bola_page["photo"] is not None and pixel_of(bola_page["photo"]) is not None and len(bola_pdf["images"]) == len(chidi_pdf["images"]) + 2,
      f"{len(bola_pdf['images'])} vs {len(chidi_pdf['images'])}")
check("…and Chidi, with none, shows no photograph", chidi_page["photo"] is None and 'class="rc-photo"' not in chidi_page["raw"])

long_page, long_pdf = verify_card("Long's card (thirty subjects)", op, staff_view(SID["LONG"]), staff_view(SID["LONG"]) + "/pdf", "LONG", TERM)
check("thirty subjects fit: every row is on the page and in the PDF, and the card runs on to a second page rather than being cut off",
      len(long_page["rows"]) == 30 and all(n in long_pdf["lines"] for n in LONG_SUBJECTS) and long_pdf["page_count"] >= 2
      and any(l.startswith("Page 2 of") for l in long_pdf["lines"]) and long_page["summary"]["Subjects"] == "30", f"{long_pdf['page_count']} pages")
alpha_teacher_long = "This pupil " + "works with great care and thinks before answering; " * 17
alpha_teacher_long = ("This pupil " + "works with great care and thinks before answering; " * 20)[:1000].rstrip()
check("(the long comment is a full 1000 characters, give or take a trailing space)", 995 <= len(alpha_teacher_long) <= 1000)
post_comments(academic, SSS1, {SID["LONG"]: alpha_teacher_long})
long_page2, long_pdf2 = verify_card("Long's card with a 1000-character comment", op, staff_view(SID["LONG"]), staff_view(SID["LONG"]) + "/pdf", "LONG", TERM)
check("a 1000-character comment appears in full on the page and in the PDF (wrapped over lines, nothing cut)",
      long_page2["comment"] == alpha_teacher_long.strip()
      and re.sub(r"\s+", " ", " ".join(l for l in long_pdf2["lines"])).count(alpha_teacher_long.split()[-1]) >= 1
      and " ".join(alpha_teacher_long.split()) in re.sub(r"\s+", " ", " ".join(long_pdf2["lines"])), "")

ola_page, ola_pdf = verify_card("Ọlá Ṣadé's card (letters outside Latin-1)", op, staff_view(SID["OLA"]), staff_view(SID["OLA"]) + "/pdf", "OLA", TERM,
                                extra={"student": "Ọlá Ṣadé"})
check("a name with letters outside Latin-1 (Ọlá Ṣadé) is drawn correctly in the PDF and the page", "Ọlá Ṣadé" in ola_pdf["lines"] and ola_page["student"]["Name"] == "Ọlá Ṣadé")
r = MUM_OLA.get(p_view(SID["OLA"], CUR) + "/pdf")
disposition = r.headers.get("Content-Disposition", "")
try:
    disposition.encode("latin-1")
    encodable = True
except UnicodeEncodeError:
    encodable = False
check("…and the parent's download has a file name every web server can send (plain letters and digits only)",
      r.status_code == 200 and encodable and re.fullmatch(r'inline; filename="[A-Za-z0-9._-]+\.pdf"', disposition) is not None, disposition)

# a tampered address is a clean 404, never a crash
errors.seen.clear()
tampered = ("first-term%00", "FIRST-TERM", "First-Term", "..%2F..%2Fadmin", "first_term", "first-term%20", "%E2%80%A6", "third-term", "fourth-term", "full-session")
statuses = {}
for slug in tampered:
    statuses[slug] = (ada_portal.get(f"/student/report-cards/{CUR}/{slug}").status_code, ada_portal.get(f"/student/report-cards/{CUR}/{slug}/pdf").status_code,
                      op.get(f"{RC}/{ADA}/{CUR}/{slug}").status_code, op.get(f"{RC}/{ADA}/{CUR}/{slug}/pdf").status_code,
                      MUM_ADA.get(p_view(ADA, CUR, slug)).status_code, MUM_ADA.get(p_view(ADA, CUR, slug) + "/pdf").status_code)
check("a term slug that is tampered with (%00, upper case, ../, other spellings) is a clean 404 for staff, student and parent, page and PDF",
      all(all(code in (404,) or (slug in ("third-term", "full-session") and code in (404, 302)) for code in codes) for slug, codes in statuses.items()) and not errors.seen,
      str({s: c for s, c in statuses.items() if any(x not in (404,) for x in c)}))
junk = ("abc", "-1", "0", "99999999999999999999", "1.5", "%00", "1%00")
codes = {j: (ada_portal.get(f"/student/report-cards/{j}/{SLUG}").status_code, ada_portal.get(f"/student/report-cards/{j}/{SLUG}/pdf").status_code,
             op.get(f"{RC}/{j}/{CUR}/{SLUG}").status_code, op.get(f"{RC}/{ADA}/{j}/{SLUG}").status_code,
             MUM_ADA.get(f"/parent/children/{ADA}/report-cards/{j}/{SLUG}").status_code, MUM_ADA.get(f"/parent/children/{j}/report-cards/{CUR}/{SLUG}").status_code)
         for j in junk}
check("junk in the address (letters, negative, zero, a number too big for the database, a NUL byte) is a 404 or a plain 400, never a 500",
      all(c in (400, 404) for cs in codes.values() for c in cs) and not errors.seen, str({j: c for j, c in codes.items() if any(x not in (400, 404) for x in c)}))

# ================================================================ F. who can see what
check("staff: the School Admin and the other holders of report_cards.view open the list",
      all(p.get(f"{RC}?class_id={J1}").status_code == 200 for p in (op, teacher_a, teacher_b, viewer, officer, academic)))
check("…a member of staff with no report card permission is refused (403) everywhere: list, card, PDF, class bundle, comments, traits, signature, settings",
      all(nobody.get(u).status_code == 403 for u in (f"{RC}?class_id={J1}", staff_view(ADA), staff_view(ADA) + "/pdf",
                                                     f"{RC}/class.pdf?class_id={J1}&session_id={CUR}&term=First%20Term",
                                                     RC + "/comments", RC + "/traits", RC + "/my-signature", RC + "/settings")))
# The staff guide's help page for results is named after report cards and is open to every administrator, so it is not a menu link.
check("…nor can they see the 'Report cards' link in the menu", "report-cards" not in nobody.text("/admin/school").replace("/admin/guide/results-report-cards", "") and "report-cards" in op.text("/admin/school"))
kept_comment = comment_row(ADA)
kept_sigs = (admin_signature(TEACHER_A), signature_files(alpha))
r1 = viewer.post(comments_url(J1), {f"comment_{ADA}": "The viewer must not write this."}, page="/admin/password")
r2 = viewer.post(RC + "/my-signature", {"action": "draw", "signature_data_url": drawn(GREEN_SIGN)}, page="/admin/password")
check("with report_cards.view only: comments, my-signature and settings are refused (403) to open and to submit, and nothing is written",
      viewer.get(RC + "/comments").status_code == 403 and viewer.get(RC + "/my-signature").status_code == 403 and viewer.get(RC + "/settings").status_code == 403
      and r1.status_code == 403 and r2.status_code == 403 and comment_row(ADA) == kept_comment
      and viewer.post(RC + "/settings", {"head_title": "Hacker", "head_name": "Hacker"}, page="/admin/password").status_code == 403
      and head_setting(alpha, "report_head_name") == "Mrs A. B. Okoye" and (admin_signature(TEACHER_A), signature_files(alpha)) == kept_sigs)
check("…yet the viewer can open and download cards, and reads the comment but has no comment box",
      viewer.get(staff_view(ADA)).status_code == 200 and viewer.get(staff_view(ADA) + "/pdf").status_code == 200)
check("a class teacher (view + comment, no manage) may not open the head's settings (403 on GET and POST)",
      teacher_a.get(RC + "/settings").status_code == 403 and teacher_a.post(RC + "/settings", {"head_name": "Nope"}, page="/admin/password").status_code == 403
      and head_setting(alpha, "report_head_name") == "Mrs A. B. Okoye")
check("…but may write comments and keep a signature", teacher_a.get(RC + "/comments").status_code == 200 and teacher_a.get(RC + "/my-signature").status_code == 200)
check("the Report Card Officer and the School Academic Administrator presets can use every report card page",
      all(p.get(u).status_code == 200 for p in (officer, academic) for u in (RC, RC + "/comments", RC + "/traits", RC + "/my-signature", RC + "/settings")))
perms = lambda role: {r[0] for r in alpha.sql("SELECT p.code FROM admin_types t JOIN admin_type_permissions x ON x.admin_type_id = t.id "  # noqa: E731
                                              "JOIN permissions p ON p.id = x.permission_id WHERE t.name = :n", n=role)}
check("the presets carry what they should: Primary Class Teacher view + comment (not manage); School Academic Administrator and Report Card Officer all three",
      {"report_cards.view", "report_cards.comment"} <= perms("Primary Class Teacher") and "report_cards.manage" not in perms("Primary Class Teacher")
      and {"report_cards.view", "report_cards.comment", "report_cards.manage"} <= perms("School Academic Administrator")
      and {"report_cards.view", "report_cards.comment", "report_cards.manage"} <= perms("Report Card Officer")
      and not any(p.startswith("report_cards.") for p in perms("Librarian")))
check("every staff route for report cards has its permission in the endpoint map",
      all(ADMIN_ENDPOINT_PERMISSIONS.get(e, "").startswith("report_cards.") for e in (
          "admin_school_report_cards", "admin_school_report_card_view", "admin_school_report_card_pdf", "admin_school_report_cards_class_pdf",
          "admin_school_report_card_comments", "admin_school_report_card_comments_save", "admin_my_signature", "admin_my_signature_save",
          "admin_school_report_card_settings", "admin_school_report_card_settings_save",
          "admin_school_report_card_traits", "admin_school_report_card_traits_save")))

# ---- a class teacher sees only her own class
listing_a = teacher_a.text(f"{RC}?class_id={J1}")
listing_a_wrong = teacher_a.text(f"{RC}?class_id={J2}")
check("teacher A (JSS 1 only) sees JSS 1's cards, and JSS 2 is not even a choice on her page",
      "Ada Obi" in html.unescape(listing_a) and ">JSS 2<" not in listing_a and ">JSS 1<" in listing_a)
check("…asking for JSS 2 by hand shows her none of its students",
      "Tobi Boundary" not in listing_a_wrong and "Ife Bright" not in listing_a_wrong and "Choose a class" in listing_a_wrong)
check("…another class's card page, PDF and student are a 404 for her, and a class bundle for JSS 2 is a 404",
      teacher_a.get(staff_view(SID["TOBI"])).status_code == 404 and teacher_a.get(staff_view(SID["TOBI"]) + "/pdf").status_code == 404
      and teacher_a.get(f"{RC}/class.pdf?class_id={J2}&session_id={CUR}&term=First%20Term").status_code == 404
      and teacher_a.get(staff_view(SID["LONG"])).status_code == 404)
check("…a student who is in no class that session is a 404 for her too (only the School Admin sees those)",
      teacher_a.get(staff_view(SID["BOLA"], NEXT)).status_code == 404 and op.get(staff_view(SID["BOLA"], NEXT)).status_code == 200
      and op.get(staff_view(SID["BOLA"], NEXT) + "/pdf").status_code == 200)
check("…her own class's card page and PDF and bundle are open to her",
      teacher_a.get(staff_view(ADA)).status_code == 200 and teacher_a.get(staff_view(ADA) + "/pdf").status_code == 200
      and teacher_a.get(f"{RC}/class.pdf?class_id={J1}&session_id={CUR}&term=First%20Term").status_code == 200)
kept = (comment_row(SID["TOBI"]), comment_row(ADA))
r = post_comments(teacher_a, J2, {SID["TOBI"]: "Teacher A writing in JSS 2, which she may not"})
check("…she cannot write a comment for JSS 2 (told to choose a class, nothing saved)", (comment_row(SID["TOBI"]), comment_row(ADA)) == kept
      and comment_row(SID["TOBI"]) == ("Tobi has met every target.", TEACHER_C))
check("teacher C (JSS 2 only) sees JSS 2 and not JSS 1: Ada's card is a 404 for her",
      "Tobi Boundary" in teacher_c.text(f"{RC}?class_id={J2}") and teacher_c.get(staff_view(ADA)).status_code == 404
      and teacher_c.get(staff_view(SID["TOBI"])).status_code == 200 and "Ada Obi" not in html.unescape(teacher_c.text(f"{RC}?class_id={J1}")))

# ---- CSRF, signed-out visitors
kept = (comment_row(ADA), head_setting(alpha, "report_head_name"), admin_signature(TEACHER_A))
posts = ((comments_url(J1), {f"comment_{ADA}": "no token"}), (RC + "/my-signature", {"action": "remove"}), (RC + "/settings", {"head_name": "no token"}))
check("a POST without a CSRF token is refused (403) on all three write routes, and nothing changes",
      all(op.post(path, data, token=False).status_code == 403 for path, data in posts)
      and all(op.post(path, data, token="wrong-token").status_code == 403 for path, data in posts)
      and (comment_row(ADA), head_setting(alpha, "report_head_name"), admin_signature(TEACHER_A)) == kept)
visitor = Person(ALPHA, label="signed-out visitor")
gets = (RC, staff_view(ADA), staff_view(ADA) + "/pdf", f"{RC}/class.pdf?class_id={J1}", RC + "/comments", RC + "/my-signature", RC + "/settings")
check("a signed-out visitor is sent to sign in from every staff page (never shown a card)",
      all(visitor.get(u).status_code == 302 and "login" in visitor.get(u).headers["Location"] for u in gets))
check("…and from every write route, which changes nothing",
      all(visitor.post(p, d, token=False).status_code in (302, 403) for p, d in posts) and comment_row(ADA) == kept[0])
check("…and from the student and parent card pages and PDFs",
      all(visitor.get(u).status_code == 302 and "login" in visitor.get(u).headers["Location"]
          for u in (s_view(CUR), s_view(CUR) + "/pdf", p_view(ADA, CUR), p_view(ADA, CUR) + "/pdf", "/student/dashboard")))

# ---- students
check("a student sees the 'Report cards' panel only when a card is ready, with links to her own page and PDF only",
      panel_links(ada_portal.text("/student/dashboard")) == sorted([s_view(CUR), s_view(CUR) + "/pdf"]))
check("…and can open the page and the PDF of her own card", ada_portal.get(s_view(CUR)).status_code == 200 and pdf_headers_ok(ada_portal.get(s_view(CUR) + "/pdf")))
check("…a student sees no other student's card: the address names no student, so Bola sees Bola's and Ada sees Ada's",
      parse_page(bola_portal.text(s_view(CUR)))["student"]["Name"] == "Bola Eze" and parse_page(ada_portal.text(s_view(CUR)))["student"]["Name"] == "Ada Obi"
      and "Ada Obi" not in bola_portal.text(s_view(CUR)))
check("…404 for a term that is not ready (Third Term), an unknown term, and a session she has nothing in",
      ada_portal.get(s_view(CUR, "third-term")).status_code == 404 and ada_portal.get(s_view(CUR, "fifth-term")).status_code == 404
      and ada_portal.get(s_view(987654)).status_code == 404)
check("a student cannot open the staff pages or the parent pages, and a parent cannot open the staff pages",
      all(ada_portal.get(u).status_code in (302, 403, 404) for u in (RC, staff_view(ADA), staff_view(ADA) + "/pdf", RC + "/comments", RC + "/settings", p_view(ADA, CUR)))
      and all(MUM_ADA.get(u).status_code in (302, 403, 404) for u in (RC, staff_view(ADA), staff_view(ADA) + "/pdf", RC + "/comments", RC + "/my-signature", RC + "/settings"))
      and "Ada Obi" not in ada_portal.text(RC) and "Ada Obi" not in MUM_ADA.text(RC))
check("…nor submit to a staff form, or open one another kind of person's card",
      MUM_ADA.post(RC + "/settings", {"head_name": "Parent"}, token=False).status_code in (302, 403)
      and ada_portal.post(comments_url(J1), {f"comment_{ADA}": "Student"}, token=False).status_code in (302, 403) and comment_row(ADA)[0].startswith("Ada is a joy")
      and MUM_ADA.get(s_view(CUR)).status_code in (302, 403, 404) and ada_portal.get(p_view(ADA, CUR)).status_code in (302, 403, 404))

# ---- parents
check("a parent sees the panel for her linked child and the cards open; another family's child is a 404 (page and PDF)",
      has_panel(MUM_ADA.text(f"/parent/children/{ADA}")) and MUM_ADA.get(p_view(ADA, CUR)).status_code == 200
      and MUM_ADA.get(p_view(SID["BOLA"], CUR)).status_code == 404 and MUM_ADA.get(p_view(SID["BOLA"], CUR) + "/pdf").status_code == 404
      and MUM_BOLA.get(p_view(ADA, CUR)).status_code == 404 and MUM_BOLA.get(p_view(SID["BOLA"], CUR)).status_code == 200)
check("…her child's page lists that child's cards only", panel_links(MUM_BOLA.text(f"/parent/children/{SID['BOLA']}"))
      and all(f"/parent/children/{SID['BOLA']}/" in l for l in panel_links(MUM_BOLA.text(f"/parent/children/{SID['BOLA']}")))
      and MUM_BOLA.get(f"/parent/children/{ADA}").status_code == 404)
check("…a parent is sent to sign in when signed out; and a student cannot open the parent pages",
      visitor.get(p_view(ADA, CUR)).status_code == 302 and ada_portal.get(f"/parent/children/{ADA}").status_code in (302, 403, 404))
parent_ada_page = parse_page(MUM_ADA.text(p_view(ADA, CUR)))
check("the parent's page links back to her child and to the PDF", f"/parent/children/{ADA}" in parent_ada_page["raw"]
      and parent_ada_page["download"] == p_view(ADA, CUR) + "/pdf")

# ---- other schools: alpha's ids at beta
ALPHA_HIGH = SID["OLA"]
check("(alpha's student ids used below do not exist at beta)", ALPHA_HIGH > beta.one("SELECT max(id) FROM students") and NEXT > BETA_CUR)
check("alpha's ids at beta are 404: a beta School Admin, student and parent asking for an alpha student, page or PDF",
      op_beta.get(staff_view(ALPHA_HIGH, BETA_CUR)).status_code == 404 and op_beta.get(staff_view(ALPHA_HIGH, BETA_CUR) + "/pdf").status_code == 404
      and op_beta.get(staff_view(ADA, NEXT)).status_code == 404 and chike_portal.get(s_view(NEXT)).status_code == 404
      and chike_portal.get(s_view(NEXT) + "/pdf").status_code == 404)
beta_parent = mk_parent(beta, "beta.mum", "Mrs Betaman", [CHIKE])
check("…a beta parent asking for an alpha child is a 404; a beta parent sees her own child's card",
      beta_parent.get(p_view(ALPHA_HIGH, BETA_CUR)).status_code == 404 and beta_parent.get(p_view(ALPHA_HIGH, BETA_CUR) + "/pdf").status_code == 404
      and beta_parent.get(p_view(CHIKE, BETA_CUR)).status_code == 200)
check("…and the class bundle for a class number that only alpha has is a 404 at beta (alpha's class ids are its own)",
      op_beta.get(f"{RC}/class.pdf?class_id=999999&session_id={BETA_CUR}&term=First%20Term").status_code == 404)
check("beta's staff list never shows an alpha student", "Ada Obi" not in html.unescape(op_beta.text(f"{RC}?class_id={BETA_J1}"))
      and "Chike Betaman" in html.unescape(op_beta.text(f"{RC}?class_id={BETA_J1}")))

# ================================================================ G. the PDF and the class bundle
before = alpha.one("SELECT count(*) FROM audit_logs WHERE action = 'report_card_downloaded'")
r = op.get(staff_view(ADA) + "/pdf")
check("staff download of one card: valid %PDF, inline, a safe file name, no-store; Content-Type application/pdf",
      pdf_headers_ok(r) and r.mimetype == "application/pdf" and "Report-Card-Ada-Obi" in r.headers["Content-Disposition"]
      and "First-Term" in r.headers["Content-Disposition"], r.headers.get("Content-Disposition", ""))
check("…and is recorded in the audit log", alpha.one("SELECT count(*) FROM audit_logs WHERE action = 'report_card_downloaded'") == before + 1)
bundle_url = f"{RC}/class.pdf?class_id={J1}&session_id={CUR}&term=First%20Term"
before_class = alpha.one("SELECT count(*) FROM audit_logs WHERE action = 'report_cards_class_downloaded'")
r = op.get(bundle_url)
bundle = pdf_read(r.data)
ready_now = sorted(k for (k, t) in READY if t == TERM and KEY_CLASS[k] == J1 and KEY_SESSION[k] == CUR)
names_ready = {"ADA": "Ada Obi", "BOLA": "Bola Eze", "CHIDI": "Chidi Nwosu", "HOST": HOSTILE_NAME, "DAYO": "Dayo Bello"}
check("the class bundle is one PDF with one page per ready student and only ready students",
      pdf_headers_ok(r) and bundle["page_count"] == len(ready_now) == 5
      and all(n in bundle["lines"] for n in names_ready.values()), f"{bundle['page_count']} pages for {ready_now}")
check("…none of the students who are not ready (Emeka: nothing; Fola: practice only) or who left (Gone) is in it, and nobody from another class",
      not any(n in bundle["lines"] for n in ("Emeka Okoro", "Fola Ade", "Gone Away", "Tobi Boundary", "Ann Annual")))
check("…its file name names the class, term and session, safely", re.fullmatch(r'inline; filename="Report-Cards-JSS-1-First-Term-2026-2027\.pdf"',
                                                                                r.headers["Content-Disposition"]) is not None, r.headers["Content-Disposition"])
check("…each page of the bundle is a whole card: every page has its own school name, total line and grading key",
      len(bundle["pages"]) >= 5 and sum(1 for pg in bundle["pages"] if "Grading key" in pg) == 5)
check("…and the bundle is recorded in the audit log", alpha.one("SELECT count(*) FROM audit_logs WHERE action = 'report_cards_class_downloaded'") == before_class + 1)
check("a class with nothing ready has no bundle: staff are told, nothing is downloaded",
      op.get(f"{RC}/class.pdf?class_id={J1}&session_id={CUR}&term=Third%20Term").status_code == 302
      and "no report card in this class is ready" in op.said())
check("the bundle needs a class: without one it is a 404", op.get(f"{RC}/class.pdf").status_code == 404)
order = [next((l for l in pg if l in names_ready.values()), None) for pg in bundle["pages"]]
check("the bundle's cards are in name order, one card to a page",
      order == sorted(names_ready.values(), key=str.casefold), str(order))

# ================================================================ H. efficiency
class Counter:
    """Counts the SQL statements sent to one school's database while something runs."""

    def __init__(self, school):
        self.engine, self.statements = engine_for(school.info), []

    def __enter__(self):
        sa.event.listen(self.engine, "before_cursor_execute", self.listener)
        return self

    def __exit__(self, *exc):
        sa.event.remove(self.engine, "before_cursor_execute", self.listener)

    def listener(self, conn, cursor, statement, parameters, context, executemany):
        self.statements.append(statement)


J3_SUBJECTS = ["Efficiency Subject %d" % n for n in range(1, 9)]
J3_IDS = mk_subjects(alpha, J3_SUBJECTS)


def add_j3_students(first_number, how_many):
    made = mk_students(alpha, [(f"Pupil{n:02d}", "Bulk", J3, CUR, {}) for n in range(first_number, first_number + how_many)])
    def go():
        for offset, sid in enumerate(made):
            for k, subject in enumerate(J3_IDS):
                score = 30 + (offset * 7 + k * 3) % 30
                for component, s, m in (("Test", 5 + (offset + k) % 6, 10), ("Exam", score, 60)):
                    A.db.session.add(SchoolStudentResult(student_id=sid, subject_id=subject, score=float(s), max_score=float(m), term=TERM,
                                                        session_id=CUR, status="released", created_at=NOW, source_type="manual",
                                                        component_name=component, released_at=REL))
    orm(alpha, go)
    return made


bundle_j3 = f"{RC}/class.pdf?class_id={J3}&session_id={CUR}&term=First%20Term"
add_j3_students(1, 10)
op.get(bundle_j3)          # warm up (imports, fonts): the first call of a process is not a fair measure
with Counter(alpha) as c10:
    r10 = op.get(bundle_j3)
pages10 = pdf_read(r10.data)["page_count"]
add_j3_students(11, 30)
with Counter(alpha) as c40:
    r40 = op.get(bundle_j3)
pages40 = pdf_read(r40.data)["page_count"]
n10, n40 = len(c10.statements), len(c40.statements)
check(f"the whole-class PDF has a page per student: {pages10} for 10 students, {pages40} for 40, each with 8 subjects", pages10 == 10 and pages40 == 40
      and pdf_headers_ok(r10) and pdf_headers_ok(r40), f"{pages10} {pages40}")
check(f"the number of SQL statements does NOT grow with the class: {n10} for 10 students, {n40} for 40",
      n10 > 0 and abs(n40 - n10) <= 3, f"{n10} vs {n40}")
check(f"…and is small: {n40} statements to build 40 cards of 8 subjects (ceiling 60; most of it is the fixed sign-in, permission and branding look-ups every page makes)", 0 < n40 <= 60, str(n40))
with Counter(alpha) as page_count:
    op.get(f"{RC}?class_id={J3}&session_id={CUR}&term=First%20Term")
check(f"the staff list for the class of 40 also uses a fixed, small number of statements ({len(page_count.statements)})", 0 < len(page_count.statements) <= 80,
      str(len(page_count.statements)))
j3_first = alpha.one("SELECT id FROM students WHERE first_name = 'Pupil01' AND last_name = 'Bulk'")
j3_ready = staff_list(op, J3)
check("all 40 are Ready on the staff page", len(j3_ready) == 40 and set(j3_ready.values()) == {"Ready"}, str(set(j3_ready.values())))

# ================================================================ I. no traceback anywhere, no server error
check("no page or PDF in this run was a server error, and none showed a traceback", not PROBLEMS, "; ".join(PROBLEMS[:4]))
check("no server error was logged by any request in this run", not errors.seen, "; ".join(str(e).splitlines()[0][:100] for e in errors.seen[:3]))

# ================================================================ J. the guard files
import pg_posts_coverage as coverage  # noqa: E402

card_writes = {rule.rule for rule in A.app.url_map.iter_rules()
               if rule.methods & {"POST"} and ("report-cards" in rule.rule)}
check("the four report card form routes (comments, traits, my-signature, settings) are in the form suite's list of exercised routes",
      card_writes == {RC + "/comments", RC + "/traits", RC + "/my-signature", RC + "/settings"} and card_writes <= set(coverage.EXERCISED),
      str(card_writes))
check("…and no write route of the application is unaccounted for", coverage.unaccounted(A.app.url_map) == [] and coverage.stale(A.app.url_map) == ([], []))
posts_source = open(os.path.join(HERE, "write_paths_pg_posts.py"), encoding="utf-8").read()
check("…and write_paths_pg_posts.py really submits those four (comments, traits, my-signature, settings)",
      all(s in posts_source for s in ('{RC}/comments', '{RC}/traits', '{RC}/my-signature', '{RC}/settings')))
gets_rules = {rule.rule for rule in A.app.url_map.iter_rules() if "GET" in rule.methods and "report-cards" in rule.rule}
check("the readiness, card, PDF and bundle pages are all real routes the smoke crawl will find (staff, student, parent)",
      {RC, RC + "/<int:student_id>/<int:session_id>/<slug>", RC + "/<int:student_id>/<int:session_id>/<slug>/pdf", RC + "/class.pdf",
       "/student/report-cards/<int:session_id>/<slug>", "/student/report-cards/<int:session_id>/<slug>/pdf",
       "/parent/children/<int:student_id>/report-cards/<int:session_id>/<slug>",
       "/parent/children/<int:student_id>/report-cards/<int:session_id>/<slug>/pdf"} <= gets_rules, str(sorted(gets_rules)))

dispose_engines()
DROP_TEST_DATABASES()
shutil.rmtree(TMP, ignore_errors=True)
print()
failed = [x for x in results if not x[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
