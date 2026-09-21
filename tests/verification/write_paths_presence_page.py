"""The /admin/presence page must show real data, not a hardcoded placeholder.

templates/admin_presence.html previously ignored the `counts`/`rows` the route
passed in entirely and always rendered "1 admin, 0 students, 0 parents" plus
one fake "School Admin" row, using Bootstrap classes that render unstyled since
this app doesn't load Bootstrap. This drives a real login, sends a real
heartbeat, and checks the page reflects it.
"""
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DB = os.path.join(HERE, "presence.db")
shutil.copy(os.path.join(ROOT, "cbt.db"), DB)
sys.path.insert(0, ROOT)
from werkzeug.security import generate_password_hash  # noqa: E402

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
now = datetime.now(timezone.utc).isoformat()
tid = con.execute("SELECT id FROM admin_types WHERE is_system=1 ORDER BY id LIMIT 1").fetchone()["id"]
admin_id = con.execute(
    "INSERT INTO admins(username,display_name,password_hash,admin_type_id,active,created_at,"
    "password_must_change) VALUES(?,?,?,?,1,?,0)",
    ("zz_presence_admin", "Presence Probe", generate_password_hash("PresencePass!1"), tid, now)).lastrowid
con.execute("INSERT OR IGNORE INTO admin_role_assignments(admin_id,admin_type_id,assigned_at) VALUES(?,?,?)",
            (admin_id, tid, now))
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


with A.app.test_client() as c:
    c.post("/login", data={"username": "zz_presence_admin", "password": "PresencePass!1"})
    c.get("/admin/workspace/school")
    c.get("/admin/presence")  # a real page load also counts as a heartbeat via track_live_presence
    c.get("/presence/heartbeat")

    page = c.get("/admin/presence").get_data(as_text=True)
    check("page renders", "Live Presence" in page or "Online now" in page)
    check("no hardcoded fake row remains", "2026-09-10 00:32:03" not in page)
    check("shows at least 1 administrator online", bool(re.search(r'presence-kpi-value">\s*1\s*<', page)))
    check("lists the real logged-in admin, not a fake one", "Presence Probe" in page)
    check("shows a WAT-labelled timestamp", "WAT" in page)
    check("does not reference bootstrap-only classes", 'class="row g-3' not in page)

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
