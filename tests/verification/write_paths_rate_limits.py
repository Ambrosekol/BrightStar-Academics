"""Rate limits that every worker process shares, proven end to end on PostgreSQL.

Sign-in, password recovery and test-message limits used to be counted in each worker's own
memory, so several workers multiplied them. They are now counted in the registry database
(table ``rate_limits``, see control_plane/ratelimit.py). This proves:

* the limit holds, and different keys are counted separately;
* two separate PROCESSES sharing one database together respect one limit, not one each;
* a window that has run out starts again, and finished windows are cleaned up;
* only a SHA-256 of the key is stored, never the address or username inside it;
* an existing registry gains the table on the next start;
* when the registry cannot be reached nobody is locked out, nothing crashes, the fallback
  counts in the worker's own memory, and only one warning a minute is logged;
* the real school sign-in, password-recovery and platform sign-in pages still refuse after
  too many attempts, and still work when the shared store fails in the middle of a request.

Run:  python tests/verification/write_paths_rate_limits.py
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

# ---------------------------------------------------------------- worker mode
# The test starts copies of itself as separate processes. A worker must NOT create a database:
# it inherits the test's registry from the environment and just tries the limiter in a loop.
if len(sys.argv) > 1 and sys.argv[1] == "--worker":
    sys.path.insert(0, ROOT)
    from core.accounts import _rate_limit  # the very function the sign-in page calls

    _, _, worker_key, worker_limit, worker_window, worker_attempts, worker_start = sys.argv
    while time.time() < float(worker_start):  # every worker starts at the same instant
        time.sleep(0.001)
    allowed = sum(1 for _ in range(int(worker_attempts))
                  if _rate_limit(worker_key, int(worker_limit), int(worker_window)))
    print(f"ALLOWED {allowed}")
    sys.exit(0)

import logging  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402

TMP = tempfile.mkdtemp(prefix="brightstars_ratelimits_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup("ratelimits")
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

import hashlib  # noqa: E402

import sqlalchemy as sa  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

import app as A  # noqa: E402
from control_plane import provisioning as pv  # noqa: E402
from control_plane import ratelimit  # noqa: E402
from control_plane.registry import (  # noqa: E402
    dispose_platform_engine, init_platform_db, platform_engine, platform_session,
)
from control_plane.routing import dispose_engines  # noqa: E402
from core.accounts import _rate_limit  # noqa: E402

results = []
PL = "http://platform.test"
ALPHA = "http://alpha.portal.test"


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


def csrf(c, path, base):
    body = c.get(path, base_url=base).get_data(as_text=True)
    marker = 'name="_csrf_token" value="'
    start = body.index(marker) + len(marker)
    return body[start:body.index('"', start)]


def registry(statement, **params):
    with platform_session() as s:
        return s.execute(sa.text(statement), params).fetchall()


def tries(key, n, limit, window):
    return [_rate_limit(key, limit, window) for _ in range(n)]


init_platform_db()

# ================================================================ the limit holds
first = tries("t-basic", 5, 3, 60)
check("the first `limit` tries are allowed and every one after is refused", first == [True, True, True, False, False], first)
check("a refused key stays refused while its window is open", _rate_limit("t-basic", 3, 60) is False)
check("the count is in the registry database, not in this process's memory",
      registry("SELECT hits FROM rate_limits WHERE key_hash = :h", h=ratelimit.key_hash("t-basic"))[0][0] >= 4
      and "t-basic" not in ratelimit._LOCAL_BUCKETS)

# ================================================================ keys are independent
check("a different key is not affected by an exhausted one", _rate_limit("t-other", 3, 60) is True)
tries("t-one", 3, 3, 60)
check("…nor the same identifier at another school (the sign-in key includes the school)",
      _rate_limit("login:alpha:1.2.3.4:sam", 1, 60) is True and _rate_limit("login:alpha:1.2.3.4:sam", 1, 60) is False
      and _rate_limit("login:beta:1.2.3.4:sam", 1, 60) is True)
check("the limit and window are the caller's own, per call",
      _rate_limit("t-generous", 100, 60) and _rate_limit("t-generous", 100, 60)
      and not _rate_limit("t-strict", 0, 60))

# ================================================================ a finished window starts again
check("within a window: two allowed, then refused", tries("t-expire", 3, 2, 1) == [True, True, False])
time.sleep(1.4)
again = tries("t-expire", 3, 2, 1)
check("after the window ends the count starts again", again == [True, True, False], again)

# ================================================================ finished windows are cleaned up
with platform_session() as s:
    s.execute(sa.text("INSERT INTO rate_limits (key_hash, hits, expires_at) VALUES ('old-dead-row', 9, 1.0)"))
    s.commit()
ratelimit._last_purge = float("-inf")
_rate_limit("t-purge-trigger", 5, 60)
check("an expired row is removed in passing", registry("SELECT count(*) FROM rate_limits WHERE key_hash = 'old-dead-row'")[0][0] == 0)
check("…while a live one is kept",
      registry("SELECT count(*) FROM rate_limits WHERE key_hash = :h", h=ratelimit.key_hash("t-basic"))[0][0] == 1)
with platform_session() as s:
    s.execute(sa.text("INSERT INTO rate_limits (key_hash, hits, expires_at) VALUES ('old-dead-row-2', 9, 1.0)"))
    s.commit()
_rate_limit("t-purge-again", 5, 60)  # within the same minute: no clean-up is done on every call
check("clean-up is not run on every call (at most about once a minute)",
      registry("SELECT count(*) FROM rate_limits WHERE key_hash = 'old-dead-row-2'")[0][0] == 1)

# ================================================================ only a hash is stored
secret_key = "login:alpha:203.0.113.77:someone.private@example.org"
_rate_limit(secret_key, 5, 60)
everything = " ".join(str(r) for r in registry("SELECT * FROM rate_limits"))
check("the key itself is not stored anywhere in the table",
      "203.0.113.77" not in everything and "someone.private" not in everything and "login:alpha" not in everything)
check("what is stored is its SHA-256",
      registry("SELECT count(*) FROM rate_limits WHERE key_hash = :h",
               h=hashlib.sha256(secret_key.encode()).hexdigest())[0][0] == 1)

# ================================================================ an existing registry gains the table
with platform_session() as s:
    s.execute(sa.text("DROP TABLE rate_limits"))
    s.commit()
init_platform_db()
check("a registry without the table gets it on the next start (the normal upgrade path)",
      registry("SELECT count(*) FROM rate_limits")[0][0] == 0
      and registry("SELECT to_regclass('ix_rate_limits_expires') IS NOT NULL")[0][0])
check("…and the limiter works on it straight away", tries("t-after-upgrade", 3, 2, 60) == [True, True, False])

# ================================================================ separate processes share one limit
dispose_platform_engine()  # the workers open their own connections; keep this process's pool small


def run_workers(key, limit, window, attempts, workers=2):
    start_at = time.time() + 6
    procs = [subprocess.Popen([sys.executable, os.path.abspath(__file__), "--worker", key, str(limit),
                               str(window), str(attempts), str(start_at)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=ROOT)
             for _ in range(workers)]
    counts, problems = [], []
    for p in procs:
        out, err = p.communicate(timeout=120)
        line = [x for x in out.splitlines() if x.startswith("ALLOWED ")]
        if p.returncode != 0 or not line:
            problems.append((p.returncode, err[-400:]))
        else:
            counts.append(int(line[0].split()[1]))
    return counts, problems


counts, problems = run_workers("t-two-processes", limit=25, window=120, attempts=40)
check("two worker processes started together do not fail", not problems and len(counts) == 2, problems)
check("…and together they were allowed exactly the limit (25), not 25 each",
      sum(counts) == 25, f"per process {counts}")
check("…each process really was refused some tries (40 attempts each)", all(c < 40 for c in counts), counts)
counts3, problems3 = run_workers("t-three-processes", limit=10, window=120, attempts=10, workers=3)
check("three processes, one limit of 10", not problems3 and sum(counts3) == 10, f"{counts3} {problems3}")

# ================================================================ the registry cannot be reached
class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


capture = Capture()
ratelimit.log.addHandler(capture)
ratelimit.log.setLevel(logging.WARNING)
real_url = os.environ["BRIGHTSTARS_PLATFORM_DB"]
password = make_url(real_url).password or ""

original_session = ratelimit.platform_session


def broken():
    raise sa.exc.OperationalError("SELECT 1", {}, Exception("the registry database is down"))


ratelimit.platform_session = broken
ratelimit._registry_down_until = 0.0
ratelimit._last_warning = float("-inf")
down = []
for _ in range(5):
    down.append(_rate_limit("t-down", 3, 60))
    ratelimit._registry_down_until = 0.0  # force a fresh attempt at the registry every time
check("with the registry down, nothing crashes and the first tries are allowed", down[:3] == [True, True, True], down)
check("…and the fallback still enforces the limit, in this worker's own memory", down[3:] == [False, False], down)
check("…a warning is logged, but only once even though every call failed", len(capture.lines) == 1, capture.lines)
check("…and it names the problem without any connection detail",
      "OperationalError" in capture.lines[0] and (not password or password not in capture.lines[0]))
ratelimit._last_warning -= ratelimit.WARN_EVERY_SECONDS + 1  # a minute later
ratelimit._registry_down_until = 0.0
_rate_limit("t-down", 3, 60)
check("…and again a minute later if it is still down", len(capture.lines) == 2, capture.lines)
ratelimit.platform_session = original_session
ratelimit._registry_down_until = 0.0
check("when the registry is back the shared count is used again (it has not seen 't-down')",
      tries("t-down", 4, 3, 60) == [True, True, True, False])

# a registry that really is unreachable: nothing listens on that port
dispose_platform_engine()
os.environ["BRIGHTSTARS_PLATFORM_DB"] = make_url(real_url).set(host="127.0.0.1", port=1).render_as_string(hide_password=False)
ratelimit._registry_down_until = 0.0
capture.lines.clear()
ratelimit._last_warning = float("-inf")
try:
    started = time.monotonic()
    real_down = tries("t-unreachable", 4, 3, 60)
    first_cost = time.monotonic() - started
    started = time.monotonic()
    more = tries("t-unreachable-2", 20, 50, 60)
    quick = time.monotonic() - started
finally:
    os.environ["BRIGHTSTARS_PLATFORM_DB"] = real_url
    dispose_platform_engine()
    ratelimit._registry_down_until = 0.0
check("with the server itself unreachable, the limit is still enforced and nobody is locked out",
      real_down == [True, True, True, False], real_down)
check("…a connection that hangs is given up on after a few seconds, not the operating system's minutes",
      first_cost < 15, f"{first_cost:.1f}s")
check("…and a dead registry is tried once, not before every sign-in attempt (the rest were instant)",
      all(more) and quick < 1.0, f"{quick:.2f}s")
check("…one warning for the lot", len(capture.lines) == 1, capture.lines)
check("…and once it is back the shared counters work again", tries("t-recovered", 3, 2, 60) == [True, True, False])
ratelimit.log.removeHandler(capture)

# ================================================================ the real pages
pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
console = A.app.test_client()
r = console.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                          "_csrf_token": csrf(console, "/platform/login", PL)}, base_url=PL)
check("a platform admin can still sign in", r.status_code == 302, r.status_code)
console.post("/platform/schools/new", data={
    "_csrf_token": csrf(console, "/platform/schools/new", PL), "name": "Alpha School", "code": "alpha",
    "school_brand_primary": "#0d2b52", "school_brand_accent": "#1674b9"}, base_url=PL,
    content_type="multipart/form-data")

anon = A.app.test_client()
statuses = [anon.post("/login", data={"username": "flood", "password": "x"}, base_url=ALPHA) for _ in range(9)]
check("the school sign-in refuses attempt 9 with its error page (429)",
      statuses[-1].status_code == 429 and "Too many sign-in attempts" in statuses[-1].get_data(as_text=True))
check("…after eight ordinary failed sign-ins (the limit is unchanged)",
      all(s.status_code == 200 and "We could not verify" in s.get_data(as_text=True) for s in statuses[:8]))
other = anon.post("/login", data={"username": "somebody-else", "password": "x"}, base_url=ALPHA)
check("…and another username from the same address is not caught up in it",
      other.status_code == 200 and "We could not verify" in other.get_data(as_text=True))
check("the sign-in count is in the registry, under a hash of the key",
      registry("SELECT count(*) FROM rate_limits WHERE key_hash = :h",
               h=hashlib.sha256(b"login:alpha:127.0.0.1:flood").hexdigest())[0][0] == 1)

# ...even when the shared store fails partway through a request
ratelimit.platform_session = broken
ratelimit._registry_down_until = 0.0
during = []
for _ in range(9):
    during.append(anon.post("/login", data={"username": "flood-down", "password": "x"}, base_url=ALPHA))
    ratelimit._registry_down_until = 0.0
ratelimit.platform_session = original_session
ratelimit._registry_down_until = 0.0
check("with the registry failing, the sign-in page still answers (no error page) and still refuses attempt 9",
      all(s.status_code == 200 for s in during[:8]) and during[8].status_code == 429,
      [s.status_code for s in during])
check("…and the request's own school session is left healthy afterwards",
      anon.get("/login", base_url=ALPHA).status_code == 200)

last = None
for _ in range(6):
    last = anon.post("/forgot-password", data={"identifier": "nobody"},
                     base_url=ALPHA, follow_redirects=True)
check("password recovery refuses the sixth request in a row (limit 5)",
      "Too many password-recovery requests" in last.get_data(as_text=True))

bad = [console.post("/platform/login", data={"username": "nobody", "password": "wrong",
                                             "_csrf_token": csrf(console, "/platform/login", PL)}, base_url=PL)
       for _ in range(9)]
check("the platform sign-in refuses attempt 9 (429), after eight ordinary refusals (401)",
      [b.status_code for b in bad[:8]] == [401] * 8 and bad[8].status_code == 429
      and "Too many sign-in attempts" in bad[8].get_data(as_text=True), [b.status_code for b in bad])
elsewhere = A.app.test_client()
r = elsewhere.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                            "_csrf_token": csrf(elsewhere, "/platform/login", PL)},
                   base_url=PL, environ_base={"REMOTE_ADDR": "198.51.100.7"})
check("…and a real operator, on another address or account, can still sign in", r.status_code == 302, r.status_code)
r = console.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                          "_csrf_token": csrf(console, "/platform/login", PL)}, base_url=PL)
check("…including the same address when it is a different account", r.status_code == 302, r.status_code)

dispose_engines()
dispose_platform_engine()
DROP_TEST_DATABASES()
shutil.rmtree(TMP, ignore_errors=True)
print()
failed = [x for x in results if not x[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
