"""Self-check for core/report_card_pdf.py, the report-card PDF drawer. No database, no Flask, no network.

It builds five fixture cards, renders them, then reads the finished PDF back WITHOUT any extra library
(streams are decompressed with zlib / ASCII85 and the text is decoded through each font's own ToUnicode
map, the same way tests/verification/write_paths_receipts.py reads receipts) and checks, in plain words:

Content
* every fact that must be on a card is there as real, extractable text: the school's name, address, phone
  and e-mail, the student, every subject, the totals, the percentage, the class average with the class size,
  the teacher's comment, the teacher's and the head's names and titles, the grading key, 'Next term begins'
  and 'Issued';
* nothing is invented: a school with no logo gets its own name in that place, no class average column when
  no subject has one, no photograph or signature when none is given, 'Head of School' when the head has no
  title, an empty comment box when there is no comment, 'No results recorded.' when there are no subjects;
* markup in a name ('<b>&amp;</b>', 'Tolu & Sons <Ltd>') prints exactly as typed;
* numbers read '8' not '8.0', with at most two decimals.

Pictures
* the logo, the photograph, the teacher's signature and the head's signature are drawn when supplied, and
  NOT drawn when they are not; a missing, corrupt or truncated picture never raises and is simply left out.

Layout
* 4 cards give at least 4 pages; a card with 20 subjects and a 900-character comment continues onto a second
  page without losing anything; a table that runs over a page repeats its header row;
* 'Page x of y' counts each card's own pages;
* the position of every piece of text and every picture is read back from the PDF: nothing runs off the page,
  nothing sits outside the margins and no two pieces of text or pictures overlap.

Run:  venv\\Scripts\\python.exe tests\\verification\\report_card_pdf_selfcheck.py
The sample PDFs are written to the system temp folder (the path is printed), never into the repository.
Set REPORT_CARD_SAMPLE_DIR to write them somewhere else.
"""
import base64
import io
import math
import os
import re
import sys
import tempfile
import zlib

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from PIL import Image  # noqa: E402  (installed with reportlab)
from reportlab.pdfbase.pdfmetrics import stringWidth  # noqa: E402

import core.report_card_pdf as RC  # noqa: E402
from core.report_card_pdf import render_report_cards_pdf  # noqa: E402

# A school's pictures are stored under its own uploads folder and read through core/storage.py, which needs a school
# to be serving a request. This check has no school and no database, so the fixtures' pictures sit as plain files and
# the one function that would look them up is pointed at them.
import core.storage as _storage  # noqa: E402


def _read_fixture_picture(path):
    return open(path, "rb").read() if isinstance(path, str) and os.path.isfile(path) else None


_storage.read_upload_bytes = _read_fixture_picture

OUT = os.environ.get("REPORT_CARD_SAMPLE_DIR") or os.path.join(tempfile.gettempdir(), "brightstars_report_card_selfcheck")
os.makedirs(OUT, exist_ok=True)
PICS = os.path.join(OUT, "pictures")
os.makedirs(PICS, exist_ok=True)

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)


# ------------------------------------------------------------------ reading a PDF without a library
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


def _decode(obj):
    """(dictionary part, decoded stream bytes or None) of one PDF object."""
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


def _mul(a, b):
    """Row-vector matrix product a x b of two PDF matrices (a b c d e f)."""
    return (a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3], a[2] * b[0] + a[3] * b[2],
            a[2] * b[1] + a[3] * b[3], a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5])


IDENTITY = (1, 0, 0, 1, 0, 0)
TOKEN = re.compile(rb"\((?:\\.|[^\\)])*\)|/[^\s/\[\]()<>]*|\[|\]|<<|>>|[^\s/\[\]()<>]+", re.S)


class Pdf:
    """A PDF read back: page count, per page the text runs (with positions) and pictures (with positions)."""

    def __init__(self, data):
        self.data = data
        self.objects = {int(m.group(1)): m.group(2) for m in re.finditer(rb"(\d+) 0 obj\s*(.*?)\s*endobj", data, re.S)}
        RC._fonts()   # make sure the same fonts are registered here, for measuring text
        self.fonts = self._fonts()
        self.images = self._images()
        self.pages = [self._page(number) for number in self._page_numbers()]

    # ---- objects
    def _fonts(self):
        """Font resource name -> (code -> character map, is bold)."""
        by_object = {}
        for number, obj in self.objects.items():
            if b"/Type /Font" not in obj:
                continue
            found = re.search(rb"/ToUnicode (\d+) 0 R", obj)
            cmap = {}
            if found:
                _, body = _decode(self.objects[int(found.group(1))])
                cmap = {int(code, 16): chr(int(char, 16)) for code, char in
                        re.findall(rb"<([0-9A-Fa-f]{2})>\s*<([0-9A-Fa-f]{4})>", body or b"")}
            by_object[number] = (cmap, b"Bold" in obj.split(b"/BaseFont", 1)[-1][:60])
        names = {}
        for obj in self.objects.values():
            if b"stream" not in obj and b"/Type" not in obj:
                for name, number in re.findall(rb"/(F[\w+]+) (\d+) 0 R", obj):
                    if int(number) in by_object:
                        names[name.decode()] = by_object[int(number)]
        return names

    def _images(self):
        """Picture resource name -> {'pixels': bytes, 'mean': (r,g,b), 'alpha': bool}."""
        out = {}
        # Pages refer to a picture by a name (FormXob.<hash>) that points at the picture's object.
        names = {int(number): name.decode() for name, number in re.findall(rb"/(FormXob\.[0-9a-f]+) (\d+) 0 R", self.data)}
        for number, obj in self.objects.items():
            head, body = _decode(obj)
            if body is None or b"/Subtype /Image" not in head:
                continue
            key = names.get(number, str(number))
            mean = None
            if body[:2] == b"\xff\xd8":       # a JPEG stored as it is
                try:
                    picture = Image.open(io.BytesIO(body)).convert("RGB")
                    mean = tuple(int(sum(c) / len(c)) for c in zip(*picture.resize((8, 8)).getdata()))
                except Exception:
                    pass
            out[key] = {"pixels": body, "mean": mean, "alpha": b"/SMask" in head, "object": number}
        return out

    def _page_numbers(self):
        """Object numbers of the pages, in reading order."""
        for obj in self.objects.values():
            if re.search(rb"/Type /Pages\b", obj):
                kids = obj.split(b"/Kids", 1)[1].split(b"]", 1)[0]
                return [int(n) for n in re.findall(rb"(\d+) 0 R", kids)]
        return []

    # ---- one page
    def _page(self, number):
        obj = self.objects[number]
        media = [float(v) for v in re.search(rb"/MediaBox \[([^\]]+)\]", obj).group(1).split()]
        contents = re.search(rb"/Contents (\d+) 0 R", obj)
        body = _decode(self.objects[int(contents.group(1))])[1] or b""
        runs, pictures, ops = [], [], set()
        ctm, stack = IDENTITY, []
        tm = tlm = IDENTITY
        font, size, leading, operands = None, 0.0, 0.0, []
        for token in TOKEN.findall(body):
            if token.startswith(b"(") or token.startswith(b"/") or token in (b"[", b"]", b"<<", b">>"):
                operands.append(token)
                continue
            try:
                operands.append(float(token))
                continue
            except ValueError:
                pass
            op = token.decode("latin1")
            ops.add(op)
            if op == "q":
                stack.append(ctm)
            elif op == "Q":
                ctm = stack.pop() if stack else IDENTITY
            elif op == "cm":
                ctm = _mul(tuple(operands[-6:]), ctm)
            elif op == "BT":
                tm = tlm = IDENTITY
            elif op == "Tm":
                tm = tlm = tuple(operands[-6:])
            elif op in ("Td", "TD"):
                tx, ty = operands[-2:]
                if op == "TD":
                    leading = -ty
                tm = tlm = _mul((1, 0, 0, 1, tx, ty), tlm)
            elif op == "T*":
                tm = tlm = _mul((1, 0, 0, 1, 0, -leading), tlm)
            elif op == "TL":
                leading = operands[-1]
            elif op == "Tf":
                font, size = operands[-2].decode().lstrip("/"), operands[-1]
            elif op == "Tj":
                cmap, bold = self.fonts.get(font, ({}, False))
                raw = _unescape(operands[-1][1:-1])
                text = "".join(cmap.get(b, "") for b in raw) if cmap else raw.decode("cp1252", "replace")
                width = stringWidth(text, "RCBold" if bold else "RCBody", size)
                x, y = _mul(tm, ctm)[4:]
                scale = math.hypot(*_mul(tm, ctm)[:2])
                runs.append({"text": text, "x0": x, "x1": x + width * scale, "y": y, "size": size * scale})
                tm = _mul((1, 0, 0, 1, width, 0), tm)
            elif op == "Do":
                name = operands[-1].decode().lstrip("/")
                pictures.append({"name": name, "x0": ctm[4], "y0": ctm[5], "x1": ctm[4] + ctm[0], "y1": ctm[5] + ctm[3]})
            operands = []
        return {"runs": runs, "pictures": [p for p in pictures if p["name"] in self.images], "media": media,
                "text": "\n".join(r["text"] for r in runs), "ops": ops}

    # ---- whole document
    @property
    def text(self):
        return "\n".join(page["text"] for page in self.pages)

    @property
    def norm(self):
        """All the text with runs of whitespace (and line breaks) made single spaces."""
        return " ".join(self.text.split())

    def drawn_pictures(self):
        return [(page_no, p) for page_no, page in enumerate(self.pages, 1) for p in page["pictures"]]

    def draws_pixels(self, rgb, size=None):
        """Whether a picture holding exactly this one colour is drawn on some page."""
        want = bytes(rgb)
        for _, p in self.drawn_pictures():
            pixels = self.images[p["name"]]["pixels"]
            if pixels and len(pixels) >= 3 and pixels[:3] == want and pixels == want * (len(pixels) // 3):
                return True
        return False

    def draws_jpeg_like(self, rgb, tolerance=18):
        for _, p in self.drawn_pictures():
            mean = self.images[p["name"]]["mean"]
            if mean and all(abs(a - b) <= tolerance for a, b in zip(mean, rgb)):
                return True
        return False


def squash(text):
    return " ".join(str(text).split())


# ------------------------------------------------------------------ fixture pictures (made with Pillow)
def save_picture(name, colour, size, fmt="PNG", mode="RGBA", transparent_left=False):
    path = os.path.join(PICS, name)
    picture = Image.new(mode, size, colour if mode != "RGBA" else colour + (255,))
    if transparent_left:
        for x in range(size[0] // 2):
            for y in range(size[1]):
                picture.putpixel((x, y), colour + (0,))
    picture.save(path, fmt)
    return path


RED, PURPLE, BLUE, TEAL, ORANGE = (200, 30, 30), (120, 30, 160), (30, 60, 200), (0, 150, 150), (240, 130, 20)
LOGO = save_picture("logo.png", RED, (240, 120))
LOGO_GIF = save_picture("logo.gif", ORANGE, (120, 120), fmt="GIF", mode="P")
PHOTO = save_picture("photo.jpg", TEAL, (300, 380), fmt="JPEG", mode="RGB")
TEACHER_SIG = save_picture("teacher_sig.png", PURPLE, (300, 90))
HEAD_SIG = save_picture("head_sig.png", BLUE, (300, 90))
HEAD_SIG_SEE_THROUGH = save_picture("head_sig_transparent.png", BLUE, (300, 90), transparent_left=True)
CORRUPT = os.path.join(PICS, "corrupt.png")
with open(CORRUPT, "wb") as fh:
    fh.write(b"this is not a picture at all")
TRUNCATED = os.path.join(PICS, "truncated.png")
with open(TEACHER_SIG, "rb") as src, open(TRUNCATED, "wb") as fh:
    fh.write(src.read()[:60])       # a real PNG header, cut off
MISSING = os.path.join(PICS, "no_such_file.png")

KEY = [{"range": "70 - 100", "grade": "A", "remark": "Excellent"}, {"range": "60 - 69", "grade": "B", "remark": "Very Good"},
       {"range": "50 - 59", "grade": "C", "remark": "Good"}, {"range": "45 - 49", "grade": "D", "remark": "Fair"},
       {"range": "40 - 44", "grade": "E", "remark": "Pass"}, {"range": "0 - 39", "grade": "F", "remark": "Fail"}]


def grade_of(total):
    for entry in KEY:
        low, high = (int(v) for v in entry["range"].split(" - "))
        if low <= total <= high:
            return entry["grade"], entry["remark"]
    return "F", "Fail"


def subject(name, test, assignment, project, exam, average=None, remark=None):
    ca = test + assignment + project
    total = ca + exam
    grade, meaning = grade_of(total)
    return {"name": name, "test": test, "assignment": assignment, "project": project, "ca": ca, "ca_max": 40,
            "exam": exam, "exam_max": 60, "total": total, "total_max": 100, "grade": grade,
            "remark": meaning if remark is None else remark, "class_average": average}


def summary_of(subjects, class_average=None, class_size=0):
    total = sum(s["total"] for s in subjects)
    maximum = sum(s["total_max"] for s in subjects)
    percentage = total / maximum * 100 if maximum else 0
    grade, remark = grade_of(percentage)
    return {"total": total, "total_max": maximum, "percentage": percentage, "grade": grade, "remark": remark,
            "class_average_percentage": class_average, "class_size": class_size, "subjects_count": len(subjects)}


# ---- card 1: a normal card, with a logo, a photograph, both signatures, colours, class averages
SUBJECTS_1 = [subject("Mathematics", 8, 7.5, 9, 50.25, 60.25), subject("English Language", 9, 8, 8.5, 47, 58),
              subject("Basic Science", 7, 6, 7, 44.5, 55.5), subject("Social Studies", 8.0, 9, 10, 51, 62),
              subject("Civic Education", 6, 7, 8, 40, 51), subject("Computer Studies", 9, 9, 9, 52, 66.5),
              subject("Creative Arts", 7, 8, 9, 45, 59), subject("Yoruba", 8, 7, 6, 41.376, 50)]
SUMMARY_1 = summary_of(SUBJECTS_1, 61.4, 28)
SUMMARY_1["percentage"] = 89.0     # shown as '89.0%' (one decimal)
CARD_1 = {
    "school": {"name": "Greenfield Model Academy", "motto": "Knowledge with Character", "address": "12 Adeola Odeku Street, Victoria Island, Lagos",
               "phone": "0803 555 0142", "email": "office@greenfield.example", "logo_path": LOGO, "primary": "#1b5e20", "accent": "#f9a825"},
    "title": "First Term Report Card", "term": "First Term", "session": "2026/2027",
    "student": {"name": "Ọlámidé Adéwálé Okafor", "admission_no": "GMA/2026/0417", "class_name": "JSS 2 Gold", "gender": "Female", "photo_path": PHOTO},
    "subjects": SUBJECTS_1, "summary": SUMMARY_1,
    "comment": {"text": "Olamide is a hardworking and courteous student who participates well in class. She should keep revising her Yoruba and Basic Science notes.",
                "teacher_name": "Mrs. Funke Bello", "teacher_signature_path": TEACHER_SIG},
    "head": {"title": "Head Teacher", "name": "Mr. Chukwuemeka Obi", "signature_path": HEAD_SIG},
    "grading_key": KEY, "issued_on": "21 September 2026", "next_term_begins": "5 January 2027"}

# ---- card 2: as bare as a card can be
SUBJECTS_2 = [subject("Arithmetic", 5, 5, 5, 30), subject("Reading", 6, 6, 6, 36), subject("Handwriting", 7, 7, 7, 42)]
CARD_2 = {
    "school": {"name": "Plain Academy", "motto": "", "address": "", "phone": "", "email": "", "logo_path": None, "primary": "", "accent": ""},
    "title": "Second Term Report Card", "term": "Second Term", "session": "2026/2027",
    "student": {"name": "Chidi Bare", "admission_no": "", "class_name": "Primary 4", "gender": "", "photo_path": None},
    "subjects": SUBJECTS_2, "summary": summary_of(SUBJECTS_2), "comment": None,
    "head": {"title": "", "name": "", "signature_path": None},
    "grading_key": KEY[:3], "issued_on": "21 September 2026", "next_term_begins": ""}

# ---- card 3: 20 subjects, a 900-character comment, hostile text, unreadable pictures
HOSTILE = "<b>&amp;</b>"
SUBJECTS_3 = [subject("Subject %02d %s Studies" % (i, HOSTILE) if i == 3 else "Long Subject Name Number %02d of Twenty" % i,
                      5 + i % 5, 5 + i % 4, 5 + i % 3, 20 + i * 2, 40 + i, remark="Keep working steadily on this subject %d" % i)
              for i in range(1, 21)]
COMMENT_3 = ("Tolu has had an eventful term. " * 4 + "He said <b>&amp;</b> meant it. Line one.\nLine two of the comment. " +
             " ".join("Observation %d: punctual, polite and attentive in lessons." % n for n in range(1, 12)))
COMMENT_3 = (COMMENT_3 + " " + "Well done. " * 60)[:900].rstrip()
CARD_3 = {
    "school": {"name": "Tolu & Sons <Ltd> Academy", "motto": "Work <hard> & play fair", "address": "Plot 4 & 5, <Industrial> Estate\nIkeja, Lagos",
               "phone": "+234 (0) 802 000 1111", "email": "info@tolu.example", "logo_path": CORRUPT, "primary": "#zzzzzz", "accent": "not a colour"},
    "title": "Third Term Report Card <Final>", "term": "Third Term", "session": "2026/2027",
    "student": {"name": "Tolu " + HOSTILE + " Sons <Jr>", "admission_no": "TS/0099", "class_name": "SSS 1", "gender": "Male", "photo_path": MISSING},
    "subjects": SUBJECTS_3, "summary": summary_of(SUBJECTS_3, 55.55, 31),
    "comment": {"text": COMMENT_3, "teacher_name": "Mr. Tunde <Teacher>", "teacher_signature_path": TRUNCATED},
    "head": {"title": "Proprietress & Director", "name": "Mrs. Toyin Adeyemi-Smith", "signature_path": MISSING},
    "grading_key": KEY, "issued_on": "21 September 2026", "next_term_begins": "8 September 2027"}

# ---- card 4: an empty subjects list (a logo in GIF form, a see-through signature)
CARD_4 = {
    "school": {"name": "Empty Results School", "motto": "", "address": "1 Nowhere Road", "phone": "", "email": "hello@empty.example",
               "logo_path": LOGO_GIF, "primary": "#7b1fa2", "accent": ""},
    "title": "First Term Report Card", "term": "First Term", "session": "2026/2027",
    "student": {"name": "Nkem Nothing", "admission_no": "E/1", "class_name": "Nursery 2", "gender": "Female", "photo_path": None},
    "subjects": [], "summary": summary_of([]), "comment": {"text": "", "teacher_name": "", "teacher_signature_path": None},
    "head": {"title": "Head of Nursery", "name": "Ms. Ada Empty", "signature_path": HEAD_SIG_SEE_THROUGH},
    "grading_key": KEY[:2], "issued_on": "21 September 2026", "next_term_begins": ""}

# ---- card 5: 60 subjects, to prove the table itself runs onto more pages and repeats its header
SUBJECTS_5 = [subject("Module %02d" % i, 6, 6, 6, 30 + i % 20, 50 + i % 10) for i in range(1, 61)]
CARD_5 = dict(CARD_1, subjects=SUBJECTS_5, summary=summary_of(SUBJECTS_5, 60, 40),
              student=dict(CARD_1["student"], name="Sixty Modules"))


def run():
    cards = [CARD_1, CARD_2, CARD_3, CARD_4]

    # ------------------------------------------------------------ render everything
    combined_bytes = render_report_cards_pdf(cards)
    singles_bytes = [render_report_cards_pdf([card]) for card in cards]
    with open(os.path.join(OUT, "report_cards_sample.pdf"), "wb") as fh:
        fh.write(combined_bytes)
    for number, data in enumerate(singles_bytes, 1):
        with open(os.path.join(OUT, "card_%d.pdf" % number), "wb") as fh:
            fh.write(data)
    combined = Pdf(combined_bytes)
    c1, c2, c3, c4 = (Pdf(data) for data in singles_bytes)

    check("the output is a PDF", combined_bytes.startswith(b"%PDF-") and combined_bytes.rstrip().endswith(b"%%EOF"))
    check("the module returns bytes", isinstance(combined_bytes, (bytes, bytearray)))

    # ------------------------------------------------------------ page counts
    check("4 cards give at least 4 pages", len(combined.pages) >= 4, "%d pages" % len(combined.pages))
    check("the normal, bare and empty-results cards are one page each",
          len(c1.pages) == 1 and len(c2.pages) == 1 and len(c4.pages) == 1, [len(c.pages) for c in (c1, c2, c4)])
    check("the 20-subject card with a 900-character comment continues onto a second page", len(c3.pages) >= 2, "%d pages" % len(c3.pages))
    check("the combined PDF has exactly the pages of the single-card PDFs added up",
          len(combined.pages) == sum(len(c.pages) for c in (c1, c2, c3, c4)))
    check("every page is A4 portrait", all(abs(p["media"][2] - 595.28) < 1 and abs(p["media"][3] - 841.89) < 1 for p in combined.pages))
    check("'Page x of y' counts each card's own pages",
          "Page 1 of 1" in c1.norm and "Page 1 of 1" in c2.norm and "Page 1 of %d" % len(c3.pages) in c3.norm
          and "Page %d of %d" % (len(c3.pages), len(c3.pages)) in c3.norm)
    check("in the combined PDF the long card's pages say 'Page 1 of N' and 'Page N of N' and the next card starts again at 'Page 1'",
          "Page 1 of %d" % len(c3.pages) in combined.norm and combined.norm.count("Page 1 of 1") == 3)

    # ------------------------------------------------------------ card 1: everything that must be there
    text = c1.norm
    for label, value in (("school name", "Greenfield Model Academy"), ("motto", "Knowledge with Character"),
                         ("address", "12 Adeola Odeku Street, Victoria Island, Lagos"), ("phone", "0803 555 0142"),
                         ("email", "office@greenfield.example"), ("title", "First Term Report Card"), ("term", "First Term"),
                         ("session", "2026/2027"), ("student name (with accents)", "Ọlámidé Adéwálé Okafor"),
                         ("admission number", "GMA/2026/0417"), ("class", "JSS 2 Gold"), ("gender", "Female"),
                         ("comment heading", "Class Teacher's Comment"), ("comment text", CARD_1["comment"]["text"]),
                         ("teacher name", "Mrs. Funke Bello"), ("teacher label", "Class Teacher"), ("head name", "Mr. Chukwuemeka Obi"),
                         ("head title", "Head Teacher"), ("issued", "Issued: 21 September 2026"),
                         ("next term", "Next term begins: 5 January 2027"), ("grading key heading", "Grading key")):
        check("card 1 shows the %s" % label, squash(value) in text, value)
    for s in SUBJECTS_1:
        check("card 1 shows the subject %s" % s["name"], s["name"] in text)
    check("card 1 shows the column headings",
          all(word in text for word in ("Subject", "Test", "Assignment", "Project", "Exam", "Total", "Class avg", "Grade", "Remark")))
    check("card 1 heads CA, Exam and Total with their maximums (/40 /60 /100)",
          all(re.search(pattern, text) for pattern in (r"CA /40", r"Exam /60", r"Total /100")))
    check("card 1 shows the total obtained out of obtainable", "%s / %s" % (RC._num(SUMMARY_1["total"]), RC._num(SUMMARY_1["total_max"])) in text,
          "%s / %s" % (SUMMARY_1["total"], SUMMARY_1["total_max"]))
    check("card 1 shows the overall percentage with one decimal and a % sign", "89.0%" in text)
    check("card 1 shows the overall grade and remark", "%s - %s" % (SUMMARY_1["grade"], SUMMARY_1["remark"]) in text, SUMMARY_1["grade"])
    check("card 1 shows 'Class average 61.4% (28 students)'", "Class average 61.4% (28 students)" in text)
    check("card 1 shows the number of subjects", re.search(r"Subjects 8\b", text) is not None)
    check("card 1 shows the grading key (ranges, grades and meanings)",
          all("%s: %s (%s)" % (k["grade"], k["range"], k["remark"]) in text for k in KEY))
    check("card 1 shows each subject's class average", all(RC._num(s["class_average"]) in text for s in SUBJECTS_1))
    check("scores print without a trailing .0 and with at most 2 decimals",
          re.search(r"(?<![\d.])\d+\.0(?![\d%])", text) is None and "41.38" in text and "62.38" in text and "50.25" in text and "7.5" in text,
          re.findall(r"(?<![\d.])\d+\.0(?![\d%])", text))
    check("no number is printed with three decimals", re.search(r"\d\.\d{3}", text) is None)

    # ------------------------------------------------------------ pictures
    check("card 1 draws the logo", c1.draws_pixels(RED))
    check("card 1 draws the teacher's signature", c1.draws_pixels(PURPLE))
    check("card 1 draws the head's signature", c1.draws_pixels(BLUE))
    check("card 1 draws the student's photograph", c1.draws_jpeg_like(TEAL))
    check("card 1 draws exactly four pictures", len(c1.drawn_pictures()) == 4, len(c1.drawn_pictures()))
    check("card 2 (no pictures given) draws no picture at all", len(c2.drawn_pictures()) == 0 and not c2.images, len(c2.images))
    check("card 3 (corrupt logo, truncated signature, missing files) draws no picture and does not raise",
          len(c3.drawn_pictures()) == 0 and not c3.images)
    check("card 4 draws its GIF logo", c4.draws_pixels(ORANGE))
    check("card 4 draws its see-through head signature (with an alpha mask)",
          any(c4.images[p["name"]]["alpha"] for _, p in c4.drawn_pictures()))
    check("no picture of one card leaks onto another card (logos differ between the single PDFs)",
          not c2.draws_pixels(RED) and not c3.draws_pixels(RED) and not c4.draws_pixels(RED))
    check("in the combined PDF exactly the pictures of cards 1 and 4 are drawn (4 + 2)", len(combined.drawn_pictures()) == 6,
          len(combined.drawn_pictures()))

    # unreadable pictures: no error whatever the kind of damage, and never a stand-in
    for label, path in (("a missing file", MISSING), ("a corrupt file", CORRUPT), ("a truncated PNG", TRUNCATED),
                        ("a folder", PICS), ("an empty string", ""), ("None", None), ("a number", 12345)):
        card = {"school": dict(CARD_1["school"], logo_path=path), "student": dict(CARD_1["student"], photo_path=path),
                "comment": dict(CARD_1["comment"], teacher_signature_path=path), "head": dict(CARD_1["head"], signature_path=path),
                "subjects": SUBJECTS_1, "summary": SUMMARY_1, "grading_key": KEY}
        try:
            pdf = Pdf(render_report_cards_pdf([dict(CARD_1, **card)]))
            check("%s as every picture: no error, no picture, the school's name still printed" % label,
                  not pdf.drawn_pictures() and "Greenfield Model Academy" in pdf.norm)
        except Exception as error:       # noqa: BLE001  (any error is a failure here)
            check("%s as every picture: no error" % label, False, repr(error))

    # ------------------------------------------------------------ card 2: the bare card
    text = c2.norm
    check("card 2 prints the school's name where the logo would be", "Plain Academy" in text)
    check("card 2 shows the student and the class", "Chidi Bare" in text and "Primary 4" in text)
    check("card 2 leaves out blank labels (no admission number, no gender)", "ADMISSION NO." not in text and "GENDER" not in text)
    check("card 2 leaves out blank school lines (no 'Tel:', no 'Email:')", "Tel:" not in text and "Email:" not in text)
    check("card 2 has no class-average column and no class-average figure", "Class avg" not in text and "Class average" not in text)
    check("card 2 still shows every subject and the totals", all(s["name"] in text for s in SUBJECTS_2) and "%s / %s" % (
        RC._num(sum(s["total"] for s in SUBJECTS_2)), RC._num(300)) in text)
    check("card 2 prints the comment heading over an empty box", "Class Teacher's Comment" in text)
    check("card 2 labels the teacher's line 'Class Teacher'", "Class Teacher" in text)
    check("card 2 prints 'Head of School' when the head has no title", "Head of School" in text)
    check("card 2 prints no 'Next term begins' when none is given", "Next term begins" not in text)
    check("card 2 shows the term, the session and 'Issued'", "Second Term" in text and "2026/2027" in text and "Issued: 21 September 2026" in text)
    check("card 2 shows the grading key it was given (3 grades)", all(k["range"] in text for k in KEY[:3]) and KEY[3]["range"] not in text)

    # ------------------------------------------------------------ card 3: long, hostile text
    text = c3.norm
    check("card 3 shows all 20 subject names", all(s["name"] in text for s in SUBJECTS_3))
    check("card 3 prints markup exactly as typed in a subject name", "Subject 03 <b>&amp;</b> Studies" in text)
    check("card 3 prints markup exactly as typed in the student's name", "Tolu <b>&amp;</b> Sons <Jr>" in text)
    check("card 3 prints the school's name with '&' and '<Ltd>' literally", "Tolu & Sons <Ltd> Academy" in text)
    check("card 3 prints the motto, address lines and title literally",
          "Work <hard> & play fair" in text and "Plot 4 & 5, <Industrial> Estate" in text and "Ikeja, Lagos" in text
          and "Third Term Report Card <Final>" in text)
    check("card 3 prints the whole 900-character comment, nothing cut off", squash(COMMENT_3) in text, len(COMMENT_3))
    check("card 3's comment really is 900 characters", len(COMMENT_3) >= 880, len(COMMENT_3))
    check("card 3 prints the teacher's name and the head's (long) title and name with '&' literally",
          "Mr. Tunde <Teacher>" in text and "Proprietress & Director" in text and "Mrs. Toyin Adeyemi-Smith" in text)
    check("card 3 shows the totals, the class average and its class size", "Class average 55.5% (31 students)" in text
          or "Class average 55.6% (31 students)" in text, re.findall(r"Class average [^A-Z]*", text))
    check("card 3 shows the grading key and 'Next term begins'", all(k["range"] in text for k in KEY) and "Next term begins: 8 September 2027" in text)
    check("card 3 shows the head's title on the last page too", "Proprietress & Director" in c3.pages[-1]["text"] or
          "Proprietress & Director" in c3.pages[0]["text"])
    later = c3.pages[1]["text"] if len(c3.pages) > 1 else ""
    check("card 3's second page has a running header (school, student, class)",
          "Tolu & Sons <Ltd> Academy" in later and "SSS 1" in later, later[:120].replace("\n", " | "))
    check("the head's block appears on every card (title printed)",
          all(title in pdf.norm for pdf, title in ((c1, "Head Teacher"), (c2, "Head of School"), (c3, "Proprietress & Director"), (c4, "Head of Nursery"))))

    # ------------------------------------------------------------ card 4: no subjects
    text = c4.norm
    check("card 4 prints 'No results recorded.'", "No results recorded." in text)
    check("card 4 still prints the table header", all(word in text for word in ("Subject", "Assignment", "Remark", "Grade")))
    check("card 4 prints no results summary strip", "Overall percentage" not in text)
    check("card 4 shows the empty comment box with its heading and the teacher's label",
          "Class Teacher's Comment" in text and "Class Teacher" in text)
    check("card 4 shows the head (title and name)", "Head of Nursery" in text and "Ms. Ada Empty" in text)

    # ------------------------------------------------------------ the table itself runs over a page
    long_table = Pdf(render_report_cards_pdf([CARD_5]))
    on_pages = [n for n, page in enumerate(long_table.pages, 1) if "Module" in page["text"]]
    check("a 60-subject card runs onto more pages", len(long_table.pages) >= 2, "%d pages" % len(long_table.pages))
    check("the 60 subjects really are split over at least two pages", len(on_pages) >= 2, on_pages)
    check("all 60 subjects are printed", all(s["name"] in long_table.norm for s in SUBJECTS_5))
    check("the table's header row is repeated on every page the table runs over",
          all("Assignment" in long_table.pages[n - 1]["text"] for n in on_pages), on_pages)
    check("the summary, comment, signatures and grading key all still follow the table",
          all(word in long_table.norm for word in ("Overall percentage", "Class Teacher's Comment", "Head Teacher", "Grading key")))

    # ------------------------------------------------------------ robustness with odd data
    empty = Pdf(render_report_cards_pdf([]))
    check("no cards gives a valid one-page PDF saying 'No report cards.'", len(empty.pages) == 1 and "No report cards." in empty.norm)
    check("cards=None does not raise", len(Pdf(render_report_cards_pdf(None)).pages) == 1)
    odd_cards = [{}, None, "not a card", {"school": None, "student": "x", "subjects": None, "summary": None, "comment": "x", "head": None,
                                          "grading_key": None},
                 {"subjects": [None, {}, {"name": None, "test": "abc", "ca": float("nan"), "total": "12.5", "grade": None}],
                  "summary": {"total": "x", "percentage": None, "class_size": "many", "subjects_count": None}, "grading_key": ["x", {}]}]
    try:
        odd = Pdf(render_report_cards_pdf(odd_cards))
        check("empty dictionaries, None, strings and rubbish values render without raising (one page or more per card)", len(odd.pages) >= len(odd_cards))
    except Exception as error:      # noqa: BLE001
        check("odd data renders without raising", False, repr(error))
    huge = dict(CARD_1, student=dict(CARD_1["student"], name="W" * 400, class_name="C" * 300),
                school=dict(CARD_1["school"], name="S" * 500, address="A" * 900, motto="M" * 400))
    huge["subjects"] = [dict(SUBJECTS_1[0], name="N" * 500, remark="R" * 700, grade="G" * 60)]
    huge_pdf = Pdf(render_report_cards_pdf([huge]))
    check("absurdly long values are shortened or wrapped and the card is still legible (no error)", len(huge_pdf.pages) >= 1)
    check("absurdly long names are shortened with an ellipsis", "…" in huge_pdf.text)
    thirty = render_report_cards_pdf([CARD_1] * 30)
    check("30 cards sharing one logo make a small PDF (pictures stored once)", len(thirty) < 1_500_000, "%d bytes" % len(thirty))
    check("30 cards give 30 pages", len(Pdf(thirty).pages) == 30)

    # ------------------------------------------------------------ layout: nothing overlaps, nothing leaves the page
    def layout_problems(pdf, label):
        problems = []
        left, right = RC.MARGIN_X - 1.5, RC.PAGE_W - RC.MARGIN_X + 1.5
        for number, page in enumerate(pdf.pages, 1):
            boxes = []
            for run in page["runs"]:
                if not run["text"].strip():
                    continue
                top, bottom = run["y"] + run["size"] * 0.78, run["y"] - run["size"] * 0.2
                boxes.append(("text " + run["text"][:25], run["x0"], run["x1"], bottom, top))
                if run["x0"] < left or run["x1"] > right or bottom < 0 or top > RC.PAGE_H:
                    problems.append("%s p%d: text outside the margins: %r (x %.0f-%.0f)" % (label, number, run["text"][:30], run["x0"], run["x1"]))
            for picture in page["pictures"]:
                box = ("picture", picture["x0"], picture["x1"], picture["y0"], picture["y1"])
                boxes.append(box)
                if box[1] < left or box[2] > right or box[3] < 0 or box[4] > RC.PAGE_H:
                    problems.append("%s p%d: a picture is outside the margins" % (label, number))
            for i in range(len(boxes)):
                for j in range(i + 1, len(boxes)):
                    a, b = boxes[i], boxes[j]
                    across = min(a[2], b[2]) - max(a[1], b[1])
                    down = min(a[4], b[4]) - max(a[3], b[3])
                    smaller = min(a[4] - a[3], b[4] - b[3])
                    if across > 1.0 and down > 0.3 * smaller:
                        problems.append("%s p%d: %s overlaps %s" % (label, number, a[0], b[0]))
        return problems

    for label, pdf in (("card 1", c1), ("card 2", c2), ("card 3", c3), ("card 4", c4), ("60 subjects", long_table),
                       ("combined", combined), ("odd data", odd), ("huge values", huge_pdf), ("30 cards", Pdf(thirty)), ("no cards", empty)):
        problems = layout_problems(pdf, label)
        check("layout of %s: nothing overlaps and nothing leaves the margins" % label, not problems, "; ".join(problems[:4]))

    # ------------------------------------------------------------ the colours
    check("the school's own primary colour is used for the band (#1b5e20 -> 0.106 0.369 0.125)",
          b".105882 .368627 .12549 rg" in zlib_pages(combined_bytes))
    check("a school with no colour gets the neutral dark blue (#0d2b52)", b".05098 .168627 .321569 rg" in zlib_pages(singles_bytes[1]))
    check("an unusable colour ('#zzzzzz') falls back to the neutral dark blue", b".05098 .168627 .321569 rg" in zlib_pages(singles_bytes[2]))

    print("\nSample PDFs written to: " + OUT)
    passed = sum(1 for _, ok, _ in results if ok)
    print("%d/%d checks passed" % (passed, len(results)))
    return 0 if passed == len(results) else 1


def zlib_pages(data):
    """All the decoded page content of a PDF, joined (used to look for colours)."""
    pdf = Pdf(data)
    chunks = []
    for obj in pdf.objects.values():
        head, body = _decode(obj)
        if body is not None and b"/Subtype /Image" not in head and b"/Length1" not in head:
            chunks.append(body)
    return b"\n".join(chunks)


if __name__ == "__main__":
    sys.exit(run())
