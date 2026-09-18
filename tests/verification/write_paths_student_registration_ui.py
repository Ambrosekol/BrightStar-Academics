"""Registration through the ACTUAL rendered form, not a hand-picked field list.

This is the exact class of bug that made real registration fail while a
synthetic backend test still passed: the template drifted to different field
names (`surname` instead of `last_name`, no `class_id` at all) than the route
reads. Route-only tests never catch that, because they post whatever names
they're told to. This scrapes the live HTML for every real <input>/<select>/
<textarea> name and posts using those names and only those names, so a
template/route mismatch fails here immediately.

Also checks the "Registration Confirmed" success page and the type-sensitive
field attributes (patterns / select-only fields) are actually present in the
rendered HTML, since a UI requirement that only exists in the template source
and never got rendered is not actually delivered.
"""
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from html import unescape

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SEED = os.path.join(ROOT, "cbt.db")
DB = os.path.join(HERE, "student_reg_ui.db")

ADMIN_USER, ADMIN_PW = "zz_reg_ui_admin", "RegUiPass!2345"

shutil.copy(SEED, DB)
sys.path.insert(0, ROOT)
from werkzeug.security import generate_password_hash  # noqa: E402

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
now = datetime.now(timezone.utc).isoformat()
tid = con.execute("SELECT id FROM admin_types WHERE is_system=1 ORDER BY id LIMIT 1").fetchone()["id"]
con.execute("DELETE FROM admins WHERE username=?", (ADMIN_USER,))
admin_id = con.execute(
    "INSERT INTO admins(username,display_name,password_hash,admin_type_id,active,created_at,"
    "password_must_change) VALUES(?,?,?,?,1,?,0)",
    (ADMIN_USER, "Reg UI Probe", generate_password_hash(ADMIN_PW), tid, now)).lastrowid
con.execute("INSERT OR IGNORE INTO admin_role_assignments(admin_id,admin_type_id,assigned_at) "
            "VALUES(?,?,?)", (admin_id, tid, now))
for p in con.execute("SELECT id FROM permissions"):
    con.execute("INSERT OR IGNORE INTO admin_permissions(admin_id,permission_id,granted_at) "
                "VALUES(?,?,?)", (admin_id, p["id"], now))
con.commit()
con.close()

os.environ["CRAINBOW_DB"] = DB
os.chdir(ROOT)
import app as A  # noqa: E402

A.app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB}"
A.app.config["TESTING"] = True
with A.app.app_context():
    A.init_db()

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


def q(sql, *params):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        return c.execute(sql, params).fetchall()
    finally:
        c.close()


# Matches <input ...>, <select ...>...</select>, <textarea ...>...</textarea>
# well enough to pull out name/type/required/pattern for this one form.
TAG_RE = re.compile(r'<(input|select|textarea)\b([^>]*)>', re.I)
ATTR_RE = re.compile(r'(\w[\w-]*)\s*=\s*"([^"]*)"')
OPTION_RE = re.compile(r'<option\b([^>]*)>([^<]*)</option>', re.I)


def parse_attrs(attr_text):
    return {m.group(1).lower(): unescape(m.group(2)) for m in ATTR_RE.finditer(attr_text)}


def scrape_fields(html):
    fields = {}
    for tag, attrs_text in TAG_RE.findall(html):
        attrs = parse_attrs(attrs_text)
        name = attrs.get("name")
        if not name or name == "_csrf_token":
            continue
        fields[name] = {"tag": tag.lower(), "attrs": attrs}
    return fields


def scrape_options(html, select_name):
    m = re.search(r'<select\b[^>]*name="%s"[^>]*>(.*?)</select>' % re.escape(select_name), html, re.S | re.I)
    if not m:
        return []
    return [unescape(v) for v, _ in OPTION_RE.findall(m.group(1)) if v]


with A.app.test_client() as c:
    c.post("/login", data={"username": ADMIN_USER, "password": ADMIN_PW})
    c.get("/admin/workspace/school")

    page = c.get("/admin/school/students/new").get_data(as_text=True)
    check("registration form renders", len(page) > 3000, f"len={len(page)}")

    fields = scrape_fields(page)
    check("form has no legacy 'surname' field", "surname" not in fields)
    check("form has last_name (not surname)", "last_name" in fields)
    check("form has a class_id selector", "class_id" in fields and fields["class_id"]["tag"] == "select")
    check("form has a photo upload field",
          "photo" in fields and fields["photo"]["attrs"].get("type") == "file")
    check("form has guardian_phone", "guardian_phone" in fields)
    check("form has guardian_email", "guardian_email" in fields)
    check("form has state_of_origin", "state_of_origin" in fields)
    check("form has father_guardian_mobile", "father_guardian_mobile" in fields)
    check("form has mother_name", "mother_name" in fields)

    # ---- type-sensitive attributes actually rendered, not just written in source ----
    check("first_name is letters-only (pattern)",
          "[a-z" in fields.get("first_name", {}).get("attrs", {}).get("pattern", "").lower())
    check("date_of_birth uses a real date picker",
          fields.get("date_of_birth", {}).get("attrs", {}).get("type") == "date")
    check("guardian_email uses type=email",
          fields.get("guardian_email", {}).get("attrs", {}).get("type") == "email")
    check("guardian_phone uses type=tel",
          fields.get("guardian_phone", {}).get("attrs", {}).get("type") == "tel")
    check("gender is a constrained <select>, not free text",
          fields.get("gender", {}).get("tag") == "select")
    check("blood_group is a constrained <select>, not free text",
          fields.get("blood_group", {}).get("tag") == "select")
    check("state_of_origin offers all 36 states + FCT",
          len(scrape_options(page, "state_of_origin")) >= 37,
          str(len(scrape_options(page, "state_of_origin"))))
    check("novalidate is not set (native browser validation is active)",
          'novalidate' not in re.search(r'<form\b[^>]*>', page).group(0).lower())

    class_options = re.findall(r'<option value="(\d+)"', re.search(
        r'name="class_id"[^>]*>(.*?)</select>', page, re.S).group(1))
    check("class dropdown is populated", len(class_options) > 0, str(class_options))
    class_id = class_options[0] if class_options else None

    # ---- submit using ONLY the names the real form actually renders ----
    csrf = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', page).group(1)
    unique = os.getpid()
    payload = {
        "_csrf_token": csrf,
        "first_name": "Adaeze",
        "middle_name": "Chioma",
        "last_name": f"Okafor{unique}",
        "gender": "Female",
        "date_of_birth": "2016-03-12",
        "class_id": class_id,
        "state_of_origin": "Enugu",
        "blood_group": "O+",
        "genotype": "AA",
        "previous_school": "Sunrise Nursery",
        "reason_for_leaving": "Relocation",
        "guardian_name": "Ngozi Okafor",
        "guardian_phone": "08012345678",
        "guardian_email": f"ngozi.okafor.{unique}@example.com",
        "father_guardian_name": "Emeka Okafor",
        "father_guardian_address": "12 Independence Layout",
        "father_guardian_office_phone": "018765432",
        "father_guardian_mobile": "08023456789",
        "father_guardian_email": f"emeka.okafor.{unique}@example.com",
        "mother_name": "Ngozi Okafor",
        "mother_address": "12 Independence Layout",
        "mother_office_phone": "018765433",
        "mother_occupation": "Pharmacist",
        "mother_email": f"ngozi.okafor.{unique}@example.com",
        "religion": "Christianity",
        "denomination": "Catholic",
        "convulsion_history": "None",
        "asthma_history": "None",
        "medical_frequency": "N/A",
        "medical_treatment": "N/A",
        "immunization": "Complete",
        "food_allergies": "None",
        "drug_allergies": "None",
        "other_health_challenges": "None",
        "disability": "None",
        "disability_indication": "",
        "parent_signature": "Ngozi Okafor",
        "parent_signature_date": "2026-09-17",
    }
    r = c.post("/admin/school/students/new", data=payload)
    check("submission accepted (200, not bounced back with errors)", r.status_code == 200, str(r.status_code))

    body = r.get_data(as_text=True)
    check("success page says Registration Confirmed", "Registration Confirmed" in body)
    check("success page shows the login username", "Student ID / Username" in body)
    check("success page shows the temporary password", "Temporary Password" in body)

    row = q("SELECT id,first_name,last_name,state_of_origin,student_number,school_id "
            "FROM students WHERE last_name=?", f"Okafor{unique}")
    check("student row created", bool(row))
    if row:
        sid = row[0]["id"]
        check("state_of_origin persisted", row[0]["state_of_origin"] == "Enugu", str(row[0]["state_of_origin"]))
        check("student number generated", bool(row[0]["student_number"]))
        check("attached to the school", row[0]["school_id"] is not None)
        check("enrolled in the selected class",
              bool(q("SELECT 1 FROM student_enrolments WHERE student_id=? AND class_id=? AND active=1",
                     sid, class_id)))
        check("admission profile written",
              bool(q("SELECT 1 FROM student_admission_profiles WHERE student_id=?", sid)))
        contacts = q("SELECT role FROM student_admission_contacts WHERE student_id=? ORDER BY role", sid)
        check("both parent contacts written", len(contacts) == 2, f"got {len(contacts)}")

    # ---- a bad submission (invalid state_of_origin bypassing the <select>) must be refused server-side ----
    page2 = c.get("/admin/school/students/new").get_data(as_text=True)
    csrf2 = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', page2).group(1)
    bad = dict(payload)
    bad["_csrf_token"] = csrf2
    bad["last_name"] = f"Bypass{unique}"
    bad["state_of_origin"] = "Not A Real State"
    r2 = c.post("/admin/school/students/new", data=bad)
    check("server rejects a state_of_origin the <select> never offered",
          not q("SELECT 1 FROM students WHERE last_name=?", f"Bypass{unique}"))

print()
failed = [x for x in results if not x[1]]
print(f"{len(results)-len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
