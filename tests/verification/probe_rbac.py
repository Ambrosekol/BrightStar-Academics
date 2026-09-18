"""Dump every RBAC answer one app gives, as JSON.

    python rbac13_probe.py ref|new  > answers.json

Run once per implementation and diff the two files. Both apps define a module
named `app`, so they cannot be imported into the same process.
"""
import json
import os
import shutil
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MODE = sys.argv[1]
ROOT = (r"C:\Users\user\Downloads\Latest Crainbow_CBT_Phase13_Current_Project\Crainbow_CBT_Phase13_Current_Project"
        if MODE == "ref" else
        r"C:\Users\user\Downloads\Crainbow_CBT_Phase3H_Live_Messages_Fix\crainbow")
SEED = r"C:\Users\user\Downloads\Latest Crainbow_CBT_Phase13_Current_Project\Crainbow_CBT_Phase13_Current_Project\cbt.db"
DB = os.path.join(HERE, f"rbac13_{MODE}.db")
shutil.copy(SEED, DB)

# ---- inventory straight from the database ----
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
ADMIN_IDS = [r[0] for r in con.execute("SELECT id FROM admins ORDER BY id")]
CODES = [r[0] for r in con.execute("SELECT code FROM permissions ORDER BY code")]
SCOPES = [(r["scope_type"], r["scope_value"]) for r in
          con.execute("SELECT DISTINCT scope_type,scope_value FROM admin_scopes")]
CLASSES = [r[0] for r in con.execute("SELECT name FROM school_classes")]
CLASS_IDS = [r[0] for r in con.execute("SELECT id FROM school_classes")]
SUBJECT_IDS = [r[0] for r in con.execute("SELECT id FROM school_subjects")]
BANKS = [r[0] for r in con.execute("SELECT DISTINCT bank_id FROM examinations")]
SESSIONS = [r[0] for r in con.execute("SELECT name FROM academic_sessions")]
con.close()

SCOPE_PROBES = ([("global", "*")]
                + [("class", c) for c in CLASSES]
                + [("bank", b) for b in BANKS]
                + [("academic_session", s) for s in SESSIONS]
                + [(t, v) for t, v in SCOPES]
                + [("class", "NO_SUCH_CLASS"), ("bank", "NO_SUCH_BANK"),
                   ("subject", "NO_SUCH_SUBJECT"), ("academic_session", "NO_SUCH_SESSION")])

os.environ["CRAINBOW_DB"] = DB
sys.path.insert(0, ROOT)
os.chdir(ROOT)
import app as A  # noqa: E402

A.DB = DB
if MODE == "new":
    A.app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB}"

answers = {}


def record():
    for aid in ADMIN_IDS:
        for code in CODES:
            answers[f"perm|{aid}|{code}"] = bool(A.admin_has_permission(aid, code))
        answers[f"codes|{aid}"] = sorted(A.admin_permission_codes(aid))
        for st, sv in SCOPE_PROBES:
            answers[f"scope|{aid}|{st}|{sv}"] = bool(A.admin_scope_allows(aid, st, sv))
        for cid in CLASS_IDS:
            answers[f"class|{aid}|{cid}"] = bool(A._school_class_allowed(aid, cid))
        for sid in SUBJECT_IDS:
            answers[f"subject|{aid}|{sid}"] = bool(A._school_subject_allowed(aid, sid))
        for cid in CLASS_IDS:
            for sid in SUBJECT_IDS:
                answers[f"pair|{aid}|{cid}|{sid}"] = bool(A._school_pair_allowed(aid, cid, sid))


if MODE == "new":
    with A.app.app_context():
        with A.app.test_request_context("/"):
            record()
else:
    with A.app.test_request_context("/"):
        record()

out = os.path.join(HERE, f"rbac13_{MODE}.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(answers, f, indent=0, sort_keys=True)
sys.stderr.write(f"{MODE}: {len(answers)} RBAC answers -> {out}\n")
