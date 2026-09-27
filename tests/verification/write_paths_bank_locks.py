"""Locking a question bank, and what the lock does to every way of changing it. Proven on PostgreSQL.

The rules, in plain words:

* The School Admin (the school's top-level role) can lock a question bank from the Controls page, giving a
  reason. The lock is recorded with the reason and who set it, and a governance item is raised for review.
* A locked bank cannot be changed by anyone, School Admin included: no question added, edited, deleted or
  reordered, no change to the bank's own settings, no replacing it by importing a file, no overwriting it by
  creating a bank with the same id. Everybody can still look at it.
* Unlocking lifts all of that, keeps the history of the lock, and closes the governance item.
* Locking is one lock per bank however many times it is asked for; each school's locks are its own.
* Only the School Admin can lock or unlock. Everyone else can see what is locked but cannot change it, even
  with a valid form token. A request with no CSRF token is refused.

Drives the real application over HTTP against throw-away PostgreSQL databases and a throw-away tenants folder.
Every check reads the bank's file, the database rows, or what a page shows.

Run:  python tests/verification/write_paths_bank_locks.py
"""
import base64
import hashlib
import html
import io
import json
import logging
import os
import re
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_locks_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('locks')
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
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import dispose_engines, engine_for  # noqa: E402

results = []
PL = "http://platform.test"
TOKEN = re.compile(r'name="_csrf_token"\s+value="([^"]+)"')
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
CONTROLS = "/admin/administration/controls"
NEW_PASSWORD = "a-brand-new-password-1"


def check(name, ok, detail=""):
    results.append((name, ok, detail))
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


class Browser:
    """One person's browser: cookies of their own, and CSRF tokens taken from real pages."""

    def __init__(self, base, form_page):
        self.c = A.app.test_client()
        self.base = base
        self.form_page = form_page  # any page of theirs that carries a form (so it carries a token)

    def get(self, path, **kw):
        return self.c.get(path, base_url=self.base, **kw)

    def text(self, path):
        return html.unescape(self.get(path).get_data(as_text=True))

    def token(self, page=None):
        body = self.get(page or self.form_page).get_data(as_text=True)
        found = TOKEN.search(body)
        if not found:
            raise AssertionError(f"no CSRF token on {page or self.form_page}")
        return found.group(1)

    def post(self, path, data=None, page=None, token=True, files=None):
        data = dict(data or {})
        if token is True:
            data["_csrf_token"] = self.token(page)
        elif token:
            data["_csrf_token"] = token
        for key, (name, content) in (files or {}).items():
            data[key] = (io.BytesIO(content), name)
        return self.c.post(path, data=data, base_url=self.base, content_type="multipart/form-data" if files else None)

    def flashes(self):
        """The messages the last request left for the next page, as plain text (and clears them)."""
        with self.c.session_transaction(base_url=self.base) as sess:
            found = [text for _, text in sess.pop("_flashes", [])]
        return found

    def said(self, r, phrase):
        """Whether a reply (or the message it left for the next page) says this, ignoring case."""
        body = html.unescape(r.get_data(as_text=True)) if r.status_code in (200, 400) else ""
        return any(phrase.lower() in text.lower() for text in self.flashes() + [body])


def info_for(slug):
    with platform_session() as s:
        tenant = get_tenant(s, slug)
        return to_info(tenant) if tenant else None


def sql(statement, **params):
    with engine_for(INFO).begin() as conn:
        result = conn.execute(sa.text(statement), params)
        return result.fetchall() if result.returns_rows else None


def one(statement, **params):
    rows = sql(statement, **params)
    return rows[0][0] if rows else None


# ================================================================ two schools, each with the standard banks
pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
console = A.app.test_client()
console.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
             "_csrf_token": TOKEN.search(console.get("/platform/login", base_url=PL).get_data(as_text=True)).group(1)}, base_url=PL)
for code, name in (("locks", "Locks School"), ("other", "Other School")):
    console.post("/platform/schools/new", data={
        "_csrf_token": TOKEN.search(console.get("/platform/schools/new", base_url=PL).get_data(as_text=True)).group(1),
        "name": name, "code": code, "starter_banks": "1", "school_brand_primary": "#0d2b52", "school_brand_accent": "#1674b9"},
        base_url=PL, content_type="multipart/form-data")
INFO, INFO_OTHER = info_for("locks"), info_for("other")
SCHOOL = "http://locks.portal.test"


def operator_in(code):
    """The platform operator enters a school and is its School Admin (the top-level role)."""
    base = f"http://{code}.portal.test"
    page = console.get(f"/platform/schools/{code}", base_url=PL).get_data(as_text=True)
    r = console.post(f"/platform/schools/{code}/enter", data={"_csrf_token": TOKEN.search(page).group(1)}, base_url=PL)
    b = Browser(base, "/admin/password")
    b.get(r.headers["Location"][len(base):])
    b.get("/admin/workspace/entrance")  # an administrator chooses the entrance area before its pages open
    return b


admin = operator_in("locks")
ADMIN_ID = one("SELECT id FROM admins WHERE username = 'platform@ops'")
check("the School Admin is inside the school, and holds the top-level role",
      admin.get("/admin/banks").status_code == 200
      and one("SELECT t.is_system FROM admins a JOIN admin_types t ON t.id = a.admin_type_id WHERE a.id = :i", i=ADMIN_ID) == 1)

BANK, BANK2 = "starter_year7_mathematics", "starter_year7_english"
DATA = os.path.join(TMP, "tenants", "locks", "data")
OTHER_DATA = os.path.join(TMP, "tenants", "other", "data")
LOCK_URL = "/admin/administration/resources/bank/{}/lock"
UNLOCK_URL = "/admin/administration/resources/bank/{}/unlock"


def bank_file(bid, folder=None):
    return json.load(open(os.path.join(folder or DATA, f"{bid}.json"), encoding="utf-8"))


def fingerprint(bid, folder=None):
    """The bank's file, as a hash: any change to it, however small, changes this."""
    return hashlib.sha256(open(os.path.join(folder or DATA, f"{bid}.json"), "rb").read()).hexdigest()


def qids(bid, folder=None):
    return [q["id"] for q in bank_file(bid, folder)["questions"]]


def locked(bid):
    return one("SELECT count(*) FROM admin_resource_locks WHERE resource_type = 'bank' AND resource_id = :b AND unlocked_at IS NULL", b=bid) == 1


def lock_rows(bid):
    return sql("SELECT reason, locked_by, unlocked_by, unlocked_at FROM admin_resource_locks WHERE resource_type = 'bank' AND resource_id = :b", b=bid)


def governance(bid, status="open"):
    return one("SELECT count(*) FROM admin_control_items WHERE target_type = 'bank' AND target_id = :b AND category = 'governance' AND status = :s",
               b=bid, s=status)


def count_uploads(bid, folder="locks"):
    """Question pictures saved for a bank (a picture is kept under a name that starts with its bank)."""
    found = 0
    for base, _, names in os.walk(os.path.join(TMP, "tenants", folder)):
        found += sum(1 for n in names if n.startswith(f"entrance_{bid}"))
    return found


def controls(actor=None):
    return (actor or admin).text(CONTROLS)


def lock(bid, reason="probe freeze", actor=None, page=CONTROLS, **kw):
    return (actor or admin).post(LOCK_URL.format(bid), {"reason": reason}, page=page, **kw)


def unlock(bid, actor=None, page=CONTROLS, **kw):
    return (actor or admin).post(UNLOCK_URL.format(bid), {}, page=page, **kw)


def question_form(text="A new question?", answer="1", points="2"):
    return {"text": text, "instruction": "", "option0": "one", "option1": "two", "option2": "three", "option3": "four",
            "answer": answer, "points": points}


def audit_count(action, bid):
    return one("SELECT count(*) FROM audit_logs WHERE action = :a AND target_id = :b", a=action, b=bid)


# ================================================================ the Controls page: the lock panel
page = controls()
check("the Controls page renders", len(page) > 1000)
check("it has the 'Protected examination resources' panel, which says what a lock does",
      "Protected examination resources" in page and "blocks all edits" in page)
check("the School Admin is offered a lock form for every bank, with a reason box",
      all(LOCK_URL.format(b) in page for b in (BANK, BANK2)) and 'name="reason"' in page and page.count("/lock") >= 6)
check("the panel carries a CSRF token", TOKEN.search(page) is not None)
check("nothing is locked yet, and there is no Unlock button", not locked(BANK) and "/unlock" not in page and "locked</span>" not in page)
check("the bank list shows no 'Locked' marker yet", admin.text("/admin/banks?level=year7").count("Locked</span>") == 0)

# ================================================================ locking: reason, author, review item
BEFORE_ITEMS = one("SELECT count(*) FROM admin_control_items")
r = lock(BANK, "probe freeze")
check("locking is accepted", r.status_code in (302, 303), str(r.status_code))
check("the bank is locked", locked(BANK))
row = lock_rows(BANK)
check("the reason is stored", len(row) == 1 and row[0][0] == "probe freeze", str(row))
check("…and the lock is attributed to the School Admin who set it", row[0][1] == ADMIN_ID and row[0][2] is None)
check("a governance review item was raised for it", governance(BANK) == 1
      and one("SELECT title FROM admin_control_items WHERE target_type = 'bank' AND target_id = :b AND category = 'governance'", b=BANK) == "Resource locked"
      and one("SELECT requested_by FROM admin_control_items WHERE target_type = 'bank' AND target_id = :b AND category = 'governance'", b=BANK) == ADMIN_ID)
check("…and the lock is in the audit log, with its reason",
      audit_count("resource_locked", BANK) == 1 and "probe freeze" in one("SELECT details FROM audit_logs WHERE action = 'resource_locked' AND target_id = :b", b=BANK))
page = controls()
check("the Controls page shows the locked state with its reason, and offers Unlock instead of Lock",
      "probe freeze" in page and UNLOCK_URL.format(BANK) in page and LOCK_URL.format(BANK) not in page and LOCK_URL.format(BANK2) in page)
check("…and counts it: '1 locked'", "1 locked" in page)
check("the review queue lists the item for the School Admin to resolve", "Resource locked" in page and "/resolve" in page)
check("the bank list marks that bank, and only that bank, as Locked",
      admin.text("/admin/banks?level=year7").count("Locked</span>") == 1)
check("the other bank is not locked", not locked(BANK2))

# ================================================================ the lock really blocks every way of changing the bank
FIRST_Q = qids(BANK)[0]
ORIGINAL = fingerprint(BANK)
ORIGINAL_QS = qids(BANK)
ORIGINAL_NAME = bank_file(BANK)["name"]
NOTIFICATIONS = one("SELECT count(*) FROM admin_notifications")
PICTURES = count_uploads(BANK)


def untouched(label):
    check(f"{label}: the bank's file is byte for byte as it was", fingerprint(BANK) == ORIGINAL)


r = admin.post(f"/admin/banks/{BANK}/questions/new", question_form("should not be added"), page=f"/admin/banks/{BANK}/questions/new")
check("adding a question to the locked bank is refused, and says why", admin.said(r, "locked by School Admin") and "should not be added" not in json.dumps(bank_file(BANK)))
untouched("adding a question")
r = admin.post(f"/admin/banks/{BANK}/questions/new", question_form("with a picture"), page=f"/admin/banks/{BANK}/questions/new",
               files={"image": ("q.png", PNG)})
check("…even with a picture attached: the picture is not saved either", count_uploads(BANK) == PICTURES and admin.said(r, "locked"))
r = admin.post(f"/admin/banks/{BANK}/questions/{FIRST_Q}/edit", question_form("rewritten"), page=f"/admin/banks/{BANK}/questions/{FIRST_Q}/edit")
check("editing a question is refused", admin.said(r, "locked by School Admin") and bank_file(BANK)["questions"][0]["text"] != "rewritten")
r = admin.post(f"/admin/banks/{BANK}/questions/{FIRST_Q}/edit", question_form("rewritten"), page=f"/admin/banks/{BANK}/questions/{FIRST_Q}/edit",
               files={"image": ("q.png", PNG)})
check("…with a picture too", count_uploads(BANK) == PICTURES)
untouched("editing a question")
r = admin.post(f"/admin/banks/{BANK}/questions/{FIRST_Q}/delete", {}, page=f"/admin/banks/{BANK}")
check("deleting a question is refused", admin.said(r, "locked by School Admin") and qids(BANK) == ORIGINAL_QS)
untouched("deleting a question")
r = admin.post(f"/admin/banks/{BANK}/questions/reorder", {"order": ",".join(str(i) for i in reversed(ORIGINAL_QS))}, page=f"/admin/banks/{BANK}")
check("reordering the questions is refused (the answer is not 'ok')", not (r.mimetype == "application/json" and r.get_json().get("ok")) and qids(BANK) == ORIGINAL_QS,
      f"{r.status_code} {r.mimetype}")
admin.flashes()
untouched("reordering")
r = admin.post(f"/admin/banks/{BANK}/edit", {"name": "Renamed while locked", "level": "Year 7", "version": "9.9", "duration_minutes": "5"},
               page=f"/admin/banks/{BANK}/edit")
check("changing the bank's own settings (name, version, time) is refused",
      admin.said(r, "locked by School Admin") and bank_file(BANK)["name"] == ORIGINAL_NAME)
untouched("changing its settings")


def import_file(payload, replace=False, actor=None):
    return (actor or admin).post("/admin/banks/import", {"replace": "1"} if replace else {}, page="/admin/banks/import",
                                 files={"bank_file": ("bank.json", json.dumps(payload).encode())})


REPLACEMENT = {"id": BANK, "name": "Sneaky replacement", "level": "Year 7", "entry_group": "year7", "subject": "Mathematics", "duration_seconds": 1200,
               "questions": [{"id": i, "text": f"What is {i} + {i}?", "options": [str(2 * i - 1), str(2 * i), str(2 * i + 1), str(2 * i + 2)], "answer": 1}
                             for i in range(1, 6)]}
r = import_file(REPLACEMENT, replace=True)
check("replacing it by importing a file, even with 'Replace it' ticked, is refused: it says the bank is locked",
      r.status_code == 400 and "locked" in html.unescape(r.get_data(as_text=True)).lower())
untouched("importing over it")
r = import_file(REPLACEMENT, replace=False)
check("importing a file with its id, without 'Replace it', is refused too", r.status_code == 400 and "already here" in html.unescape(r.get_data(as_text=True)))
untouched("importing without replace")
r = admin.post("/admin/banks/new", {"id": BANK, "name": "Duplicate", "level": "Year 7", "duration_minutes": "20", "version": "1"}, page="/admin/banks/new")
check("creating a new bank with the locked bank's id cannot overwrite it", r.status_code == 200 and "already exists" in html.unescape(r.get_data(as_text=True)))
untouched("creating a bank with its id")
check("none of the refused changes reached the audit log or the School Admin's notifications",
      all(audit_count(a, BANK) == 0 for a in ("question_added", "question_updated", "question_deleted", "questions_reordered", "question_bank_updated",
                                              "question_bank_replaced")) and one("SELECT count(*) FROM admin_notifications") == NOTIFICATIONS)
check("nothing raised a review item for a change that did not happen",
      one("SELECT count(*) FROM admin_control_items WHERE target_id = :b AND category = 'question_bank_review'", b=BANK) == 0)

check("everyone can still look at the locked bank: its page, a question's form and the export",
      admin.get(f"/admin/banks/{BANK}").status_code == 200 and admin.get(f"/admin/banks/{BANK}/questions/{FIRST_Q}/edit").status_code == 200
      and admin.get(f"/admin/banks/{BANK}/questions/new").status_code == 200 and admin.get(f"/admin/api/banks/{BANK}/export").status_code == 200)
before = len(qids(BANK2))
r = admin.post(f"/admin/banks/{BANK2}/questions/new", question_form("goes into the other bank"), page=f"/admin/banks/{BANK2}/questions/new")
check("(the other bank takes a new question as usual)", r.status_code in (302, 303) and len(qids(BANK2)) == before + 1 and audit_count("question_added", BANK2) == 1)

# ---- every route that could change a bank is one of the routes checked above
tested = {"/admin/banks/new", "/admin/banks/<bid>/edit", "/admin/banks/<bid>/questions/new", "/admin/banks/<bid>/questions/<int:qid>/edit",
          "/admin/banks/<bid>/questions/<int:qid>/delete", "/admin/banks/<bid>/questions/reorder", "/admin/banks/import"}
writes = {rule.rule for rule in A.app.url_map.iter_rules()
          if "POST" in rule.methods and "/banks" in rule.rule and "/administration/" not in rule.rule}
check("GUARD: every route that writes to a bank is one this script proves is blocked by the lock (a new one must be added here)",
      writes == tested, f"not covered: {sorted(writes - tested)}; not routes: {sorted(tested - writes)}")

# ================================================================ locking twice
r = lock(BANK, "second reason")
check("locking an already locked bank is harmless: still one lock row, with the newest reason and its author",
      r.status_code in (302, 303) and len(lock_rows(BANK)) == 1 and lock_rows(BANK)[0][0] == "second reason" and locked(BANK), str(lock_rows(BANK)))
check("…and it is still locked against edits", fingerprint(BANK) == ORIGINAL and admin.said(
    admin.post(f"/admin/banks/{BANK}/questions/new", question_form("still no"), page=f"/admin/banks/{BANK}/questions/new"), "locked"))
panel = controls()
panel = panel[panel.index("Protected examination resources"):]
check("…and the panel shows the newest reason, not the first", "second reason" in panel and "probe freeze" not in panel)
r = lock(BANK2, "   ")
check("a lock with a blank reason is given the standard one", one("SELECT reason FROM admin_resource_locks WHERE resource_id = :b", b=BANK2)
      == "Locked by School Admin pending review.")
unlock(BANK2)
check("(and that lock lifts cleanly again)", not locked(BANK2))

# ================================================================ things that cannot be locked
LOCKS_BEFORE = one("SELECT count(*) FROM admin_resource_locks")
for label, path in (("a bank that does not exist", "/admin/administration/resources/bank/no_such_bank/lock"),
                    ("a path that is not a bank id", "/admin/administration/resources/bank/a/b/lock"),
                    ("something that is not a bank", f"/admin/administration/resources/examination/{BANK}/lock"),
                    ("a made-up kind of resource", f"/admin/administration/resources/candidate/{BANK}/lock")):
    r = admin.post(path, {"reason": "x"}, page=CONTROLS)
    check(f"locking {label} is refused with 'not found', and no lock is created", r.status_code == 404 and one("SELECT count(*) FROM admin_resource_locks") == LOCKS_BEFORE,
          str(r.status_code))

# ================================================================ a request with no CSRF token
r = admin.post(LOCK_URL.format(BANK2), {"reason": "no token"}, token=False)
check("locking without a CSRF token is refused", r.status_code == 403 and not locked(BANK2))
r = admin.post(LOCK_URL.format(BANK2), {"reason": "wrong token"}, token="not-the-token")
check("…and so is locking with a wrong one", r.status_code == 403 and not locked(BANK2))
r = admin.post(UNLOCK_URL.format(BANK), {}, token=False)
check("unlocking without a CSRF token is refused, and the bank stays locked", r.status_code == 403 and locked(BANK))
r = admin.post(UNLOCK_URL.format(BANK), {}, token="not-the-token")
check("…also with a wrong token", r.status_code == 403 and locked(BANK))
nobody = Browser(SCHOOL, "/login")
r = nobody.post(LOCK_URL.format(BANK2), {"reason": "anonymous"}, token=False)
check("somebody who is not signed in is sent to sign in, and locks nothing", r.status_code in (302, 403) and not locked(BANK2)
      and (r.status_code == 403 or "/login" in r.headers.get("Location", "")))
r = nobody.post(UNLOCK_URL.format(BANK), {}, token=False)
check("…and cannot unlock either", locked(BANK))


# ================================================================ a member of staff: can look, cannot change
def add_staff(role_name, permissions, username):
    """A role with just these permissions, and a member of staff holding it, made through the real forms."""
    admin.post("/admin/administration/roles/new", {
        "name": role_name, "description": "Only what is needed.",
        "permissions": [one("SELECT id FROM permissions WHERE code = :c", c=c) for c in permissions]}, page="/admin/administration/roles/new")
    role = one("SELECT id FROM admin_types WHERE name = :n", n=role_name)
    r = admin.post("/admin/administration/admins/new", {
        "username": username, "display_name": username.title(), "email": f"{username}@locks.test", "phone": "08000000000",
        "whatsapp": "08000000000", "admin_type_ids": [role], "scope_type": "global"}, page="/admin/administration/admins/new")
    temp = re.search(r'credential-password">(.+?)</strong>', html.unescape(r.get_data(as_text=True)), re.S)
    check(f"{username} was made, holding only the role '{role_name}'", temp is not None and role is not None)
    b = Browser(SCHOOL, "/admin/password")
    b.post("/login", {"username": username, "password": temp.group(1).strip()}, page="/login")
    b.post("/admin/password", {"current_password": temp.group(1).strip(), "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
    b.get("/admin/workspace/entrance")
    return b


BANK_WORK = ["admin.access", "question_banks.view", "question_banks.create", "question_banks.edit", "questions.create", "questions.edit",
             "questions.delete", "questions.reorder"]
reviewer = add_staff("Bank Reviewer", BANK_WORK + ["audit.view"], "reviewer")
check("the reviewer is not the School Admin",
      one("SELECT t.is_system FROM admins a JOIN admin_types t ON t.id = a.admin_type_id WHERE a.username = 'reviewer'") == 0)
page = controls(reviewer)
check("the reviewer can see the panel and which bank is locked, with the reason",
      "Protected examination resources" in page and "second reason" in page and "School Admin only" in page)
check("…but is offered no way to lock or unlock: no reason box, no Lock or Unlock forms, no Resolve button",
      'name="reason"' not in page and "/lock" not in page and "/unlock" not in page and "/resolve" not in page)
check("…while the School Admin, on the same page, has all of them", "/unlock" in controls() and 'name="reason"' in controls())

r = lock(BANK2, "staff attempt", actor=reviewer, page="/admin/password")
check("the reviewer cannot lock a bank, even with a real form token", r.status_code == 403 and not locked(BANK2), str(r.status_code))
r = unlock(BANK, actor=reviewer, page="/admin/password")
check("…cannot unlock one", r.status_code == 403 and locked(BANK) and lock_rows(BANK)[0][3] is None, str(r.status_code))
ITEM = one("SELECT id FROM admin_control_items WHERE target_type = 'bank' AND target_id = :b AND category = 'governance' AND status = 'open' LIMIT 1", b=BANK)
r = reviewer.post(f"/admin/administration/controls/{ITEM}/resolve", {}, page="/admin/password")
check("…and cannot mark the review item resolved", r.status_code == 403 and one("SELECT status FROM admin_control_items WHERE id = :i", i=ITEM) == "open")
check("what the refusals left: the lock is exactly as the School Admin set it",
      len(lock_rows(BANK)) == 1 and lock_rows(BANK)[0][:2] == ("second reason", ADMIN_ID))

before = fingerprint(BANK)
r = reviewer.post(f"/admin/banks/{BANK}/questions/new", question_form("staff add"), page=f"/admin/banks/{BANK}/questions/new")
check("a member of staff who is allowed to add questions still cannot add one to a locked bank", reviewer.said(r, "locked by School Admin") and fingerprint(BANK) == before)
r = reviewer.post(f"/admin/banks/{BANK}/questions/{FIRST_Q}/edit", question_form("staff edit"), page=f"/admin/banks/{BANK}/questions/{FIRST_Q}/edit")
check("…or edit one", reviewer.said(r, "locked") and fingerprint(BANK) == before)
r = reviewer.post(f"/admin/banks/{BANK}/questions/{FIRST_Q}/delete", {}, page=f"/admin/banks/{BANK}")
check("…or delete one", reviewer.said(r, "locked") and fingerprint(BANK) == before)
r = reviewer.post(f"/admin/banks/{BANK}/edit", {"name": "Staff rename", "level": "Year 7", "version": "2", "duration_minutes": "20"}, page=f"/admin/banks/{BANK}/edit")
check("…or change the bank's settings", reviewer.said(r, "locked") and fingerprint(BANK) == before)
r = import_file(REPLACEMENT, replace=True, actor=reviewer)
check("…or replace it with an import (which needs the edit permission, and is then stopped by the lock)",
      r.status_code == 400 and "locked" in html.unescape(r.get_data(as_text=True)).lower() and fingerprint(BANK) == before)

watcher = add_staff("Bank Editor", BANK_WORK, "editor")
check("a member of staff without the 'view controls' permission cannot open the Controls page", watcher.get(CONTROLS).status_code == 403)
check("…but the bank list still shows them which bank is locked", watcher.text("/admin/banks?level=year7").count("Locked</span>") == 1)
r = watcher.post(f"/admin/banks/{BANK}/questions/new", question_form("editor add"), page=f"/admin/banks/{BANK}/questions/new")
check("…and the lock stops them editing it", watcher.said(r, "locked") and fingerprint(BANK) == before)
r = lock(BANK2, "editor attempt", actor=watcher, page="/admin/password")
check("…and they cannot lock or unlock (the request is refused, whatever the token)", r.status_code == 403 and not locked(BANK2)
      and unlock(BANK, actor=watcher, page="/admin/password").status_code == 403 and locked(BANK))

# ================================================================ another school has its own locks
other = operator_in("other")
check("the other school has the same standard bank, and it is not locked by this school's lock",
      os.path.exists(os.path.join(OTHER_DATA, f"{BANK}.json")) and one("SELECT count(*) FROM admin_resource_locks") >= 1)
with engine_for(INFO_OTHER).begin() as conn:
    other_locks = conn.execute(sa.text("SELECT count(*) FROM admin_resource_locks")).scalar()
check("…it has no locks of its own", other_locks == 0)
before_other = len(qids(BANK, OTHER_DATA))
r = other.post(f"/admin/banks/{BANK}/questions/new", question_form("in the other school"), page=f"/admin/banks/{BANK}/questions/new")
check("…so the other school can add a question to it", r.status_code in (302, 303) and len(qids(BANK, OTHER_DATA)) == before_other + 1)
check("…and this school's copy was not touched by that", fingerprint(BANK) == before)
other.post(LOCK_URL.format(BANK), {"reason": "other school's own"}, page=CONTROLS)
check("locking it there does not touch this school's lock", locked(BANK) and one("SELECT reason FROM admin_resource_locks WHERE resource_id = :b", b=BANK) == "second reason")
other.post(UNLOCK_URL.format(BANK), {}, page=CONTROLS)
check("…and unlocking it there leaves this school's bank locked", locked(BANK))

# ================================================================ unlocking
GOV_OTHER_BANK = governance(BANK2)
lock(BANK2, "a second, separate lock")
check("a second bank is locked alongside the first, each with its own review item", locked(BANK2) and governance(BANK) == 2 and governance(BANK2) == GOV_OTHER_BANK + 1,
      f"{governance(BANK)} {governance(BANK2)}")
r = unlock(BANK)
check("unlocking is accepted", r.status_code in (302, 303), str(r.status_code))
check("the bank is unlocked, the lock's history is kept (who lifted it, and when)",
      not locked(BANK) and len(lock_rows(BANK)) == 1 and lock_rows(BANK)[0][2] == ADMIN_ID and lock_rows(BANK)[0][3] is not None, str(lock_rows(BANK)))
check("the governance items for that bank are resolved, by the School Admin — both of them (it was locked twice)",
      governance(BANK) == 0 and governance(BANK, "resolved") == 2
      and one("SELECT count(*) FROM admin_control_items WHERE target_id = :b AND category = 'governance' AND resolved_by = :a AND resolved_at IS NOT NULL", b=BANK, a=ADMIN_ID) == 2)
check("…but the other bank's lock and review item are untouched", locked(BANK2) and governance(BANK2) == GOV_OTHER_BANK + 1)
check("…and the unlock is in the audit log", audit_count("resource_unlocked", BANK) == 1)
page = controls()
check("the Controls page shows it as unlocked again (Lock offered, no reason left over) and counts one locked",
      LOCK_URL.format(BANK) in page and UNLOCK_URL.format(BANK) not in page and "second reason" not in page and "1 locked" in page)
check("the bank list no longer marks it", admin.text("/admin/banks?level=year7").count("Locked</span>") == 1)  # only BANK2 (English) is left

# ---- and every change that was blocked now works
before = len(qids(BANK))
r = admin.post(f"/admin/banks/{BANK}/questions/new", question_form("added once unlocked"), page=f"/admin/banks/{BANK}/questions/new")
check("a question can be added again", r.status_code in (302, 303) and len(qids(BANK)) == before + 1 and bank_file(BANK)["questions"][-1]["text"] == "added once unlocked"
      and audit_count("question_added", BANK) == 1)
new_q = qids(BANK)[-1]
r = admin.post(f"/admin/banks/{BANK}/questions/{new_q}/edit", question_form("edited once unlocked"), page=f"/admin/banks/{BANK}/questions/{new_q}/edit")
check("…edited", bank_file(BANK)["questions"][-1]["text"] == "edited once unlocked")
order = list(reversed(qids(BANK)))
r = admin.post(f"/admin/banks/{BANK}/questions/reorder", {"order": ",".join(str(i) for i in order)}, page=f"/admin/banks/{BANK}")
check("…reordered", r.get_json() == {"ok": True} and qids(BANK) == order)
r = admin.post(f"/admin/banks/{BANK}/questions/{new_q}/delete", {}, page=f"/admin/banks/{BANK}")
check("…deleted", new_q not in qids(BANK))
r = admin.post(f"/admin/banks/{BANK}/edit", {"name": "Renamed after unlock", "level": "Year 7", "version": "2.0", "duration_minutes": "20"}, page=f"/admin/banks/{BANK}/edit")
check("…the bank's own settings changed", bank_file(BANK)["name"] == "Renamed after unlock" and bank_file(BANK)["version"] == "2.0")
r = import_file(REPLACEMENT, replace=True)
check("…and the bank replaced by an imported file", r.status_code in (302, 303) and bank_file(BANK)["name"] == "Sneaky replacement" and len(qids(BANK)) == 5)
check("the same changes by the reviewer work now too", reviewer.post(f"/admin/banks/{BANK}/questions/new", question_form("reviewer add"),
      page=f"/admin/banks/{BANK}/questions/new").status_code in (302, 303) and bank_file(BANK)["questions"][-1]["text"] == "reviewer add")

# ---- locking again after an unlock works, and unlocking what is not locked is harmless
r = lock(BANK, "frozen again")
check("a bank that was unlocked can be locked again: the same row is used, its lifted state cleared",
      locked(BANK) and len(lock_rows(BANK)) == 1 and lock_rows(BANK)[0][0] == "frozen again" and lock_rows(BANK)[0][3] is None and lock_rows(BANK)[0][2] is None, str(lock_rows(BANK)))
frozen = fingerprint(BANK)
r = admin.post(f"/admin/banks/{BANK}/questions/new", question_form("blocked again"), page=f"/admin/banks/{BANK}/questions/new")
check("…and blocks edits again at once", admin.said(r, "locked") and fingerprint(BANK) == frozen)
unlock(BANK)
r = unlock(BANK)
check("unlocking a bank that is not locked does nothing and does not fail", r.status_code in (302, 303) and not locked(BANK))
unlock(BANK2)
check("nothing is locked at the end, and no review item is left open for a lock",
      one("SELECT count(*) FROM admin_resource_locks WHERE unlocked_at IS NULL") == 0
      and one("SELECT count(*) FROM admin_control_items WHERE category = 'governance' AND status = 'open'") == 0)

# ================================================================ the end
check("no database error was logged by any request in this run", not errors.seen,
      "; ".join(str(e).splitlines()[0][:100] for e in errors.seen[:3]))
dispose_engines()
DROP_TEST_DATABASES()
shutil.rmtree(TMP, ignore_errors=True)
print()
failed = [x for x in results if not x[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
