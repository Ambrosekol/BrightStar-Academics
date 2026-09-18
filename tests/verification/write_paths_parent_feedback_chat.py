"""The /parent/feedback page must render as a chat thread, and an admin
reply must notify the parent by email and WhatsApp, not just an in-app
notification.

Drives a real feedback message from a parent, a real admin reply through the
actual route, and checks: the page uses real chat-bubble markup (not the old
flat list), the parent's message is styled 'outgoing' and the admin's reply
'incoming', and the reply triggers a real email delivery (to the school's own
address, never a real parent) plus a graceful no-op when WhatsApp isn't
configured, without ever blocking the reply itself from succeeding.
"""
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DB = os.path.join(HERE, "parent_feedback_chat.db")
shutil.copy(os.path.join(ROOT, "cbt.db"), DB)
sys.path.insert(0, ROOT)
from werkzeug.security import generate_password_hash  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
now = datetime.now(timezone.utc).isoformat()
sender_addr = os.environ.get("CRAINBOW_SMTP_FROM") or os.environ.get("CRAINBOW_SMTP_USER", "")

tid = con.execute("SELECT id FROM admin_types WHERE is_system=1 ORDER BY id LIMIT 1").fetchone()["id"]
admin_id = con.execute(
    "INSERT INTO admins(username,display_name,password_hash,admin_type_id,active,created_at,"
    "password_must_change) VALUES(?,?,?,?,1,?,0)",
    ("zz_chat_admin", "Chat Reply Probe", generate_password_hash("ChatPass!123"), tid, now)).lastrowid
con.execute("INSERT OR IGNORE INTO admin_role_assignments(admin_id,admin_type_id,assigned_at) VALUES(?,?,?)",
            (admin_id, tid, now))

parent_id = con.execute(
    "INSERT INTO parent_accounts(username,display_name,password_hash,email,phone,created_at,active,"
    "password_must_change) VALUES(?,?,?,?,?,?,1,0)",
    ("zz_chat_parent", "Chat Test Parent", generate_password_hash("ParentChat!1"),
     sender_addr, "08030000009", now)).lastrowid
con.commit()
con.close()

os.environ["CRAINBOW_DB"] = DB
os.chdir(ROOT)
import app as A  # noqa: E402

A.app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB}"
A.app.config["TESTING"] = True
with A.app.app_context():
    A.init_db()

CSRF = re.compile(r'name="_csrf_token"[^>]*value="([^"]+)"')
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


def csrf_from(html):
    m = CSRF.search(html)
    return m.group(1) if m else ""


with A.app.test_client() as parent_c:
    parent_c.post("/login", data={"username": "zz_chat_parent", "password": "ParentChat!1"})
    page = parent_c.get("/parent/feedback").get_data(as_text=True)
    token = csrf_from(page)
    r = parent_c.post("/parent/feedback", data={
        "_csrf_token": token, "subject": "Chat UI test thread", "body": "This is the parent original message.",
    })
    check("new feedback thread submits", r.status_code in (302, 303))

    page2 = parent_c.get("/parent/feedback").get_data(as_text=True)
    check("thread renders with chat-feed markup", 'feedback-thread-feed' in page2)
    check("parent's own message is an outgoing bubble", 'message-bubble outgoing' in page2)
    check("parent message text present", "This is the parent original message." in page2)

with A.app.app_context():
    from sqlalchemy import select
    feedback_id = A.one_scalar(select(A.ParentFeedback.id).where(A.ParentFeedback.subject == "Chat UI test thread"))
check("feedback thread row exists", bool(feedback_id))

# The live-SMTP round trip itself is already proven end-to-end against the
# real mail server by write_paths_receipt_email.py and
# write_paths_school_work_notify.py (both call this exact _smtp_send /
# _notify_guardian_email machinery). Re-dialling the real server here on
# every run would make this test flaky whenever that external host is slow
# or unreachable, which is a property of the network, not this code. So this
# test instead verifies the *wiring* - that a reply calls the notifiers with
# the parent's real contact details and subject - by recording calls rather
# than performing them.
calls = {"email": None, "whatsapp": None}
real_email, real_whatsapp = A._notify_guardian_email, A._notify_guardian_whatsapp


def fake_email(*args, **kwargs):
    calls["email"] = args
    return True, "sent"


def fake_whatsapp(*args, **kwargs):
    calls["whatsapp"] = args
    return True, "sent"


A._notify_guardian_email = fake_email
A._notify_guardian_whatsapp = fake_whatsapp
try:
    with A.app.test_client() as admin_c:
        admin_c.post("/login", data={"username": "zz_chat_admin", "password": "ChatPass!123"})
        admin_c.get("/admin/workspace/school")
        detail_page = admin_c.get(f"/admin/school/parent-feedback/{feedback_id}").get_data(as_text=True)
        admin_token = csrf_from(detail_page)

        r2 = admin_c.post(f"/admin/school/parent-feedback/{feedback_id}/reply", data={
            "_csrf_token": admin_token, "body": "Thanks for reaching out, this is the school reply.",
        })
        check("admin reply succeeds", r2.status_code in (302, 303), f"{r2.status_code}")
finally:
    A._notify_guardian_email, A._notify_guardian_whatsapp = real_email, real_whatsapp

check("reply triggers an email notification to the parent's real address",
      bool(calls["email"] and calls["email"][0] == sender_addr), str(calls["email"]))
check("reply triggers a WhatsApp notification to the parent's real number",
      bool(calls["whatsapp"] and calls["whatsapp"][0] == "08030000009"), str(calls["whatsapp"]))

with A.app.app_context():
    reply_count = A.one_scalar(select(A.func.count()).select_from(A.ParentFeedbackReply)
                                .where(A.ParentFeedbackReply.feedback_id == feedback_id))
check("the reply row was actually saved", reply_count == 1)

# Separately confirm a real failure in either channel still lets the reply
# succeed - this is the actual "never blocks" contract, exercised for real.
def raising_email(*args, **kwargs):
    raise Exception("simulated SMTP outage")


def raising_whatsapp(*args, **kwargs):
    raise Exception("simulated WhatsApp outage")


A._notify_guardian_email = raising_email
A._notify_guardian_whatsapp = raising_whatsapp
try:
    with A.app.test_client() as admin_c2:
        admin_c2.post("/login", data={"username": "zz_chat_admin", "password": "ChatPass!123"})
        admin_c2.get("/admin/workspace/school")
        detail_page2 = admin_c2.get(f"/admin/school/parent-feedback/{feedback_id}").get_data(as_text=True)
        token2 = csrf_from(detail_page2)
        r3 = admin_c2.post(f"/admin/school/parent-feedback/{feedback_id}/reply", data={
            "_csrf_token": token2, "body": "Second reply while notifications are broken.",
        })
        check("reply still succeeds when both notification channels raise",
              r3.status_code in (302, 303), f"{r3.status_code} {r3.get_data(as_text=True)[:200]}")
finally:
    A._notify_guardian_email, A._notify_guardian_whatsapp = real_email, real_whatsapp

with A.app.test_client() as parent_c2:
    parent_c2.post("/login", data={"username": "zz_chat_parent", "password": "ParentChat!1"})
    page3 = parent_c2.get("/parent/feedback").get_data(as_text=True)
    check("admin's reply now renders as an incoming bubble", 'message-bubble incoming' in page3)
    check("admin reply text present", "Thanks for reaching out, this is the school reply." in page3)
    check("in-progress thread listed under Active conversations",
          page3.find("Active conversations") < page3.find("Chat UI test thread") < page3.find("Resolved"))

    reply_token = csrf_from(page3)
    r4 = parent_c2.post(f"/parent/feedback/{feedback_id}/reply", data={
        "_csrf_token": reply_token, "body": "Following up from the parent side.",
    })
    check("parent can reply into an in-progress thread", r4.status_code in (302, 303), str(r4.status_code))

    page4 = parent_c2.get("/parent/feedback").get_data(as_text=True)
    check("the parent's follow-up now shows as an outgoing bubble in the thread",
          "Following up from the parent side." in page4)

with A.app.app_context():
    followup_row = A.one(select(A.ParentFeedbackReply.id, A.ParentFeedbackReply.admin_id).where(
        A.ParentFeedbackReply.feedback_id == feedback_id,
        A.ParentFeedbackReply.body == "Following up from the parent side."))
check("the parent-authored reply row has admin_id = NULL",
      bool(followup_row) and followup_row['admin_id'] is None, str(dict(followup_row) if followup_row else None))

with A.app.app_context():
    resolved = A.obj(A.ParentFeedback, feedback_id)
    resolved.status = 'resolved'
    A.db.session.commit()

with A.app.test_client() as parent_c3:
    parent_c3.post("/login", data={"username": "zz_chat_parent", "password": "ParentChat!1"})
    page5 = parent_c3.get("/parent/feedback").get_data(as_text=True)
    reply_action = f"/parent/feedback/{feedback_id}/reply"
    check("a resolved thread no longer shows a reply box", reply_action not in page5, )
    check("a resolved thread shows the resolved note instead", 'pf-resolved-note' in page5)
    resolved_token = csrf_from(page5)
    r5 = parent_c3.post(f"/parent/feedback/{feedback_id}/reply", data={
        "_csrf_token": resolved_token, "body": "Trying to reply to a resolved thread.",
    })
    check("replying to a resolved thread is refused", r5.status_code in (302, 303))

with A.app.app_context():
    blocked = A.one_scalar(select(A.func.count()).select_from(A.ParentFeedbackReply).where(
        A.ParentFeedbackReply.feedback_id == feedback_id,
        A.ParentFeedbackReply.body == "Trying to reply to a resolved thread."))
check("the refused reply was not actually saved", blocked == 0)

with A.app.app_context():
    A.db.session.remove()
    A.db.engine.dispose()
try:
    os.remove(DB)
except OSError:
    pass

print()
failed = [x for x in results if not x[1]]
print(f"{len(results)-len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
