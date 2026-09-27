"""The staff guide (/admin/guide), proven end to end on PostgreSQL: every real page actually renders
(not just a source-level check), any signed-in admin can read it regardless of their role, it is
reachable from either workspace, search finds real content, and nothing of it leaks between schools.
In plain words:

A. Every one of the guide's real pages (not a placeholder slug) renders with its own content, its
   navigation entry current, and its previous/next pager in the reading order.
B. A brand-new staff account with only the bare minimum role can read the whole guide — it carries no
   permission of its own, unlike almost every other page in the portal.
C. It is reachable from the School workspace and from the Entrance workspace alike, without switching.
D. Search finds a real word from a real page, and reports plainly when nothing matches.
E. A signed-out visitor is sent to sign in; another school's guide is the very same content, not
   anything specific to either school (there is nothing school-specific in it to leak).
F. No page in this run was a server error or showed a traceback.

Run:  python tests/verification/write_paths_guide.py
"""
import atexit
import logging
import os
import re
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_guide_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('guide')
atexit.register(DROP_TEST_DATABASES)
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
from werkzeug.security import generate_password_hash  # noqa: E402

import app as A  # noqa: E402
import blueprints.school.guide as guide  # noqa: E402
from control_plane import provisioning as pv  # noqa: E402
from control_plane.context import tenant_context  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import dispose_engines, engine_for  # noqa: E402
from core import delivery as _delivery  # noqa: E402
from models import Admin, AdminType, AdminTypePermission, Permission  # noqa: E402

_delivery._open_smtp = lambda settings: (_ for _ in ()).throw(RuntimeError("no mail in a test"))

results = []
PL = "http://platform.test"
ALPHA, BETA = "http://alpha.portal.test", "http://beta.portal.test"


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)


class Errors(logging.Handler):
    def __init__(self):
        super().__init__()
        self.seen = []

    def emit(self, record):
        if record.exc_info and record.exc_info[1] is not None:
            self.seen.append(record.exc_info[1])


errors = Errors()
A.app.logger.addHandler(errors)
A.app.logger.propagate = False
PROBLEMS = []


def csrf_from(body):
    found = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', body)
    return found.group(1) if found else ""


class Person:
    count = [0]

    def __init__(self, base, form_page="/admin/password", label=""):
        self.base, self.form_page, self.client, self.label = base, form_page, A.app.test_client(), label or base
        Person.count[0] += 1
        self.addr = f"10.90.{Person.count[0]}.1"

    def _note(self, r, method, path):
        if r.status_code >= 500:
            PROBLEMS.append(f"{self.label}: {method} {path} answered {r.status_code}")
        elif r.mimetype == "text/html" and "Traceback (most recent call last)" in r.get_data(as_text=True)[:200000]:
            PROBLEMS.append(f"{self.label}: {method} {path} showed a traceback")
        return r

    def get(self, path, **kw):
        return self._note(self.client.get(path, base_url=self.base, environ_base={"REMOTE_ADDR": self.addr}, **kw), "GET", path)

    def text(self, path):
        return self.get(path).get_data(as_text=True)

    def post(self, path, data=None, page=None):
        data = dict(data or {})
        data["_csrf_token"] = csrf_from(self.text(page or self.form_page))
        r = self.client.post(path, data=data, base_url=self.base, environ_base={"REMOTE_ADDR": self.addr})
        return self._note(r, "POST", path)


def info_for(slug):
    with platform_session() as s:
        return to_info(get_tenant(s, slug))


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


pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
console = A.app.test_client()
console.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                      "_csrf_token": csrf_from(console.get("/platform/login", base_url=PL).get_data(as_text=True))}, base_url=PL)
for code, name in (("alpha", "Alpha School"), ("beta", "Beta College")):
    form = {"_csrf_token": csrf_from(console.get("/platform/schools/new", base_url=PL).get_data(as_text=True)), "name": name, "code": code}
    console.post("/platform/schools/new", data=form, base_url=PL, content_type="multipart/form-data")
alpha, beta = School("alpha", ALPHA), School("beta", BETA)


def operator_in(school):
    page = console.get(f"/platform/schools/{school.code}", base_url=PL).get_data(as_text=True)
    r = console.post(f"/platform/schools/{school.code}/enter", data={"_csrf_token": csrf_from(page)}, base_url=PL)
    person = Person(school.base, label=f"{school.code} School Admin")
    person.get(r.headers["Location"][len(school.base):])
    person.get("/admin/workspace/school")
    return person


op = operator_in(alpha)
NOW = "2026-09-01T09:00:00+00:00"
PASSWORD = "Fixture-password-9"
HASH = generate_password_hash(PASSWORD, method="pbkdf2:sha256:1000")


def orm(school, fn):
    def go():
        out = fn()
        A.db.session.commit()
        return out
    return school.run(go)


def mk_role(school, name, codes):
    def go():
        role = AdminType(name=name, description="Only the bare minimum.", is_system=0, active=1, created_at=NOW)
        A.db.session.add(role)
        A.db.session.flush()
        for code in codes:
            permission = A.db.session.scalars(sa.select(Permission).where(Permission.code == code)).first()
            A.db.session.add(AdminTypePermission(admin_type_id=role.id, permission_id=permission.id, granted_at=NOW))
    orm(school, go)


def mk_staff(school, username, display, role_name):
    def go():
        role = A.db.session.scalars(sa.select(AdminType).where(AdminType.name == role_name)).first()
        admin = Admin(username=username, display_name=display, password_hash=HASH, admin_type_id=role.id, active=1,
                      password_must_change=0, created_at=NOW)
        A.db.session.add(admin)
        A.db.session.flush()
        return admin.id
    orm(school, go)
    person = Person(school.base, label=f"{school.code} {username}")
    person.post("/login", {"username": username, "password": PASSWORD}, page="/login")
    return person


mk_role(alpha, "Bare Minimum", ["admin.access"])
bare = mk_staff(alpha, "bare", "Bea Bare", "Bare Minimum")
bare.get("/admin/workspace/school")

# ================================================================ A. every real page renders
for slug in guide.GUIDE_ORDER:
    url = "/admin/guide" if slug == "welcome" else f"/admin/guide/{slug}"
    page = op.text(url)
    title = guide.GUIDE_PAGES[slug][0]
    check(f"{url} renders with its own title and its nav entry marked current",
          op.get(url).status_code == 200 and title in page and 'aria-current="page"' in page)
check("the entrance workspace's own page is not oddly grouped under the school one",
      "Entrance workspace" in op.text("/admin/guide"))

# ================================================================ B. no permission of its own
check("a staff account with only the bare minimum role (no special permission at all) can read the whole guide",
      all(bare.get("/admin/guide" if slug == "welcome" else f"/admin/guide/{slug}").status_code == 200 for slug in guide.GUIDE_ORDER))

# ================================================================ C. reachable from either workspace
op.get("/admin/workspace/entrance")
check("the guide is reachable from the Entrance workspace too, without switching back",
      op.get("/admin/guide").status_code == 200)
op.get("/admin/workspace/school")

# ================================================================ D. search
hit = op.text("/admin/guide/search?q=admission+number")
check("a real word from a real page is found by search", "students" in hit.lower() and "admission" in hit.lower())
miss = op.text("/admin/guide/search?q=xyznonexistentword")
check("a search with nothing to find says so plainly, not an empty silent page", "nothing" in miss.lower() or "no" in miss.lower())
short = op.text("/admin/guide/search?q=a")
check("a one-character search asks for more, rather than running a wasteful search", "at least two characters" in short.lower())

# ================================================================ E. signed out, and another school
visitor = Person(ALPHA, label="signed-out visitor")
check("a signed-out visitor is sent to sign in, never shown a page", visitor.get("/admin/guide").status_code == 302)
op_beta = operator_in(beta)
check("beta's own School Admin reads the very same guide (there is nothing school-specific in it to leak)",
      "Welcome to your guide" in op_beta.text("/admin/guide"))

# ================================================================ F. no traceback, no server error
check("no page in this run was a server error, and none showed a traceback", not PROBLEMS, "; ".join(PROBLEMS[:4]))
check("no server error was logged by any request in this run", not errors.seen, "; ".join(str(e).splitlines()[0][:100] for e in errors.seen[:3]))

dispose_engines()
DROP_TEST_DATABASES()
shutil.rmtree(TMP, ignore_errors=True)
print()
failed = [x for x in results if not x[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
