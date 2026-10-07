"""Two resilience primitives, proven end to end on PostgreSQL: durable background work
(core/jobs.py) and retry-safe form writes (core/idempotency.py).

Durable jobs: a thread started and forgotten (the module this replaces, core/background.py)
loses its work silently if the process restarts, the thread raises, or a connection drops
mid-send: nothing is left to say the work was ever meant to happen. This proves the replacement:

* a job succeeds, and its row says so (status, completed_at);
* a job whose handler raises is retried, up to a limit, and then left failed with the reason,
  not retried forever;
* a job whose thread died mid-flight (a 'running' row that never finished) is picked back up
  the next time anything of the same kind is enqueued, and finishes;
* an unknown job kind is refused immediately, before anything is written;
* the real payment-receipt job (blueprints/finance/helpers.py) leaves exactly this trail: a
  background_jobs row, done, for a payment recorded through the real form.

Retry-safe writes: a double-click, two tabs, or a browser silently retrying a POST it never saw
a reply to must never record the same payment twice. This proves that a resubmission of the
identical click (the same rendered form's hidden `_idempotency_key`) is sent to wherever the
first attempt ended up without running the write again, while a genuinely different click (its
own key) still records its own payment.

Exam-taking resilience: the entrance exam's answer-autosave script (templates/exam.html) used to
treat a dropped connection exactly like the paper being over - a network failure or an unexpected
server error sent the candidate straight to their result, silently, with no chance to retry. This
runs that script's own retry logic in a real headless browser (Chrome, skipped if not installed):
a save that fails twice over the network then succeeds ends up saved; one that always fails over
the network, or one the server always answers with a transient error, is retried a bounded number
of times and then says so in plain words - neither ever ends the candidate's paper on its own.

Offline-friendly UX: static/connectivity.js, run in the same browser, shows a banner the moment
the browser goes offline and says so again when it comes back - the piece every other page in the
portal (not just the exam) relies on to say out loud what would otherwise be a silent connection
drop.

Run:  python tests/verification/write_paths_resilience.py
"""
import html
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_resilience_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup("resilience")
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
from control_plane.context import tenant_context  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import engine_for  # noqa: E402
from core import jobs  # noqa: E402
from core.entrance import _find_chrome  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
info, _ = pv.create_tenant("alpha", "Alpha School", actor="test")
ENGINE = engine_for(info)


def sql(statement, **params):
    with ENGINE.connect() as conn:
        result = conn.execute(sa.text(statement), params)
        rows = result.fetchall() if result.returns_rows else []
        conn.commit()
        return rows


def job_row(job_id):
    row = sql("SELECT id, kind, status, attempts, last_error, completed_at FROM background_jobs WHERE id = :i", i=job_id)
    return dict(zip(("id", "kind", "status", "attempts", "last_error", "completed_at"), row[0])) if row else None


def in_school(fn):
    with A.app.app_context(), tenant_context(info):
        return fn()


A.app.config["BACKGROUND_INLINE"] = True  # deterministic: a job finishes before enqueue() returns

# ================================================================ a handler, made for this test
CALLS = []


@jobs.job_handler("test_resilience_ping")
def _ping(who, fail=False):
    CALLS.append(who)
    if fail:
        raise ValueError(f"told to fail for {who}")


# ================================================================ a job that succeeds
job_id = in_school(lambda: jobs.enqueue("test_resilience_ping", who="first"))
row = in_school(lambda: job_row(job_id))
check("a successful job is marked done", row["status"] == "done" and row["completed_at"] is not None, str(row))
check("…and its handler really ran", "first" in CALLS)

# ================================================================ an unknown kind is refused outright
raised = False
try:
    in_school(lambda: jobs.enqueue("no-such-job-kind"))
except ValueError:
    raised = True
check("an unknown job kind is refused immediately, nothing written", raised)
check("…and nothing was written for it",
      in_school(lambda: sql("SELECT count(*) FROM background_jobs WHERE kind = 'no-such-job-kind'")[0][0]) == 0)

# ================================================================ a failing handler is retried, then given up on
fail_id = in_school(lambda: jobs.enqueue("test_resilience_ping", who="flaky", fail=True))
row = in_school(lambda: job_row(fail_id))
check("a failing job is left pending (attempts remain) rather than failed outright",
      row["status"] == "pending" and row["attempts"] == 1 and "told to fail" in (row["last_error"] or ""), str(row))

# Force it old enough to be picked up as "failed, with attempts left" by the next enqueue of the same kind.
old = (datetime.now(timezone.utc) - timedelta(seconds=jobs.RETRY_FAILED_AFTER_SECONDS + 5)).isoformat()
in_school(lambda: sql("UPDATE background_jobs SET created_at = :c WHERE id = :i", c=old, i=fail_id))
in_school(lambda: jobs.enqueue("test_resilience_ping", who="second"))  # any job of this kind sweeps stuck ones first
row = in_school(lambda: job_row(fail_id))
check("…and is retried (and fails again) the next time this kind is enqueued",
      row["status"] == "pending" and row["attempts"] == 2, str(row))

for _ in range(jobs.MAX_ATTEMPTS - 2):
    in_school(lambda: sql("UPDATE background_jobs SET created_at = :c WHERE id = :i", c=old, i=fail_id))
    in_school(lambda: jobs.enqueue("test_resilience_ping", who="filler"))
row = in_school(lambda: job_row(fail_id))
check(f"after {jobs.MAX_ATTEMPTS} failed attempts it is left failed, not retried forever",
      row["status"] == "failed" and row["attempts"] == jobs.MAX_ATTEMPTS, str(row))
in_school(lambda: sql("UPDATE background_jobs SET created_at = :c WHERE id = :i", c=old, i=fail_id))
before = row["attempts"]
in_school(lambda: jobs.enqueue("test_resilience_ping", who="filler-2"))
row = in_school(lambda: job_row(fail_id))
check("…and staying failed does not get swept again", row["attempts"] == before)

# ================================================================ alerting on the four things worth paging on
import core.alerting as alerting  # noqa: E402

ALERTS = []
_real_alert = alerting.alert
alerting.alert = lambda category, message, **details: ALERTS.append((category, message, details))

fail2_id = in_school(lambda: jobs.enqueue("test_resilience_ping", who="alert-check", fail=True))
for _ in range(jobs.MAX_ATTEMPTS - 1):
    in_school(lambda: sql("UPDATE background_jobs SET created_at = :c WHERE id = :i", c=old, i=fail2_id))
    in_school(lambda: jobs.enqueue("test_resilience_ping", who="alert-check-filler", fail=True))
row = in_school(lambda: job_row(fail2_id))
check("(set-up) this second job also exhausted its retries", row["status"] == "failed", str(row))
check("a job exhausting its retries alerts, once, naming the job",
      sum(1 for c, m, d in ALERTS if c == "job_exhausted_retries" and d.get("job_id") == fail2_id) == 1,
      ALERTS)

in_school(lambda: alerting.note_delivery_failure("email", "SMTP said no"))
check("a single delivery failure on its own does not alert (only a burst does)",
      not any(c == "delivery_failure_burst" for c, m, d in ALERTS), ALERTS)
for _ in range(5):  # the burst limit itself (see core/alerting.py: limit=5, window=600)
    in_school(lambda: alerting.note_delivery_failure("email", "SMTP said no"))
check("a burst of delivery failures (not a single one) alerts",
      any(c == "delivery_failure_burst" and d.get("channel") == "email" for c, m, d in ALERTS), ALERTS)

alert_client = A.app.test_client()
alert_base = "http://alpha.portal.test"


def _login_page_csrf():
    body = alert_client.get("/login", base_url=alert_base).get_data(as_text=True)
    marker = 'name="_csrf_token" value="'
    start = body.index(marker) + len(marker)
    return body[start:body.index('"', start)]


for i in range(9):  # the login limit itself is 8 tries per 5 minutes (blueprints/auth/routes.py)
    alert_client.post("/login", data={"username": "nobody-at-all", "password": "wrong",
                                      "_csrf_token": _login_page_csrf()}, base_url=alert_base)
check("a sign-in refusal streak (the rate limit itself being hit) alerts",
      any(c == "sign_in_refusal_streak" for c, m, d in ALERTS), ALERTS)

alerting.alert = _real_alert

# ================================================================ a thread that died mid-flight is picked up again
stuck_id = in_school(lambda: jobs.enqueue("test_resilience_ping", who="will-be-stuck"))
CALLS.clear()
old_start = (datetime.now(timezone.utc) - timedelta(seconds=jobs.STUCK_RUNNING_AFTER_SECONDS + 5)).isoformat()
in_school(lambda: sql("UPDATE background_jobs SET status = 'running', started_at = :s WHERE id = :i", s=old_start, i=stuck_id))
row = in_school(lambda: job_row(stuck_id))
check("(set-up) the job now looks like a thread that started long ago and never finished", row["status"] == "running")
in_school(lambda: jobs.enqueue("test_resilience_ping", who="trigger"))  # sweeps this same kind's stuck jobs
row = in_school(lambda: job_row(stuck_id))
check("a 'running' job whose thread never finished is run again and completes",
      row["status"] == "done" and "will-be-stuck" in CALLS, str(row))

# ================================================================ the real payment-receipt job leaves this trail
console = A.app.test_client()
PL = "http://platform.test"
ALPHA = "http://alpha.portal.test"


def csrf(c, path, base):
    body = c.get(path, base_url=base).get_data(as_text=True)
    marker = 'name="_csrf_token" value="'
    start = body.index(marker) + len(marker)
    return body[start:body.index('"', start)]


console.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                      "_csrf_token": csrf(console, "/platform/login", PL)}, base_url=PL)
r = console.post(f"/platform/schools/{info.slug}/enter",
                 data={"_csrf_token": csrf(console, f"/platform/schools/{info.slug}", PL)}, base_url=PL)
boss = A.app.test_client()
boss.get(r.headers["Location"][len(ALPHA):], base_url=ALPHA)
boss.get("/admin/workspace/school", base_url=ALPHA)

sessions = in_school(lambda: sql("SELECT id FROM academic_sessions WHERE active = 1 ORDER BY id DESC LIMIT 1"))
session_id = sessions[0][0]
students = in_school(lambda: sql(
    "INSERT INTO students (admission_no, first_name, last_name, gender, guardian_email, active, "
    "created_at, student_number, student_number_source) VALUES "
    "('RES-0001', 'Resi', 'Lience', 'Female', 'guardian@example.test', 1, :now, 'RES-0001', 'generated') RETURNING id",
    now=datetime.now(timezone.utc).isoformat()))
student_id = students[0][0]

before_jobs = in_school(lambda: sql("SELECT count(*) FROM background_jobs WHERE kind = 'send_payment_receipt'")[0][0])
r = boss.post("/admin/finance/payments/new", data={
    "_csrf_token": csrf(boss, "/admin/finance/payments/new", ALPHA),
    "student_id": student_id, "session_id": session_id, "amount": "5000", "category": "School Fees",
    "method": "Cash", "reference": "res-test-1",
}, base_url=ALPHA)
check("recording a payment through the real form succeeds", r.status_code == 302, r.status_code)
after_jobs = in_school(lambda: sql("SELECT count(*), max(status) FROM background_jobs WHERE kind = 'send_payment_receipt'"))
check("it leaves exactly one more durable 'send_payment_receipt' job, done",
      after_jobs[0][0] == before_jobs + 1 and after_jobs[0][1] == "done", str(after_jobs[0]))

# ================================================================ the dedicated poller (python -m control_plane sweep-jobs)
# A real kind (send_payment_receipt), not a test-only handler: the poller runs as its own process,
# which never registered this script's own in-process test handlers.
payment_id = in_school(lambda: sql(
    "SELECT id FROM finance_payments WHERE student_id = :s ORDER BY id LIMIT 1", s=student_id)[0][0])
admin_id = in_school(lambda: sql("SELECT id FROM admins ORDER BY id LIMIT 1")[0][0])
cli_stuck_id = in_school(lambda: jobs.enqueue("send_payment_receipt", payment_id=payment_id, actor_id=admin_id))
old_created = (datetime.now(timezone.utc) - timedelta(seconds=jobs.RETRY_FAILED_AFTER_SECONDS + 5)).isoformat()
in_school(lambda: sql("UPDATE background_jobs SET status = 'failed', attempts = 1, created_at = :c WHERE id = :i",
                       c=old_created, i=cli_stuck_id))
row = in_school(lambda: job_row(cli_stuck_id))
check("(set-up) a failed job old enough to be worth retrying, with no traffic of its own kind to trigger it",
      row["status"] == "failed")
cli = subprocess.run([sys.executable, "-m", "control_plane", "sweep-jobs", "alpha"],
                     cwd=ROOT, capture_output=True, text=True, timeout=60)
row = in_school(lambda: job_row(cli_stuck_id))
check("the dedicated poller (recommendations.html's Hardening section) restarts it from outside any request",
      row["status"] == "done", (cli.stdout[-500:], cli.stderr[-500:], str(row)))
check("…and its own report names the school and how many it restarted",
      "alpha" in cli.stdout and "1 job(s) restarted" in cli.stdout, cli.stdout)

# ================================================================ a resubmitted click never records the payment twice
count_before = in_school(lambda: sql(
    "SELECT count(*) FROM finance_payments WHERE student_id = :s", s=student_id)[0][0])
idem_key = "same-click-abc123"
payment_data = {
    "_csrf_token": csrf(boss, "/admin/finance/payments/new", ALPHA),
    "_idempotency_key": idem_key,
    "student_id": student_id, "session_id": session_id, "amount": "7500", "category": "School Fees",
    "method": "Cash", "reference": "res-idem-1",
}
r1 = boss.post("/admin/finance/payments/new", data=payment_data, base_url=ALPHA)
check("a payment carrying an idempotency key is recorded normally", r1.status_code == 302, r1.status_code)
count_after_one = in_school(lambda: sql(
    "SELECT count(*) FROM finance_payments WHERE student_id = :s", s=student_id)[0][0])
check("…exactly one new payment", count_after_one == count_before + 1, (count_before, count_after_one))

# The exact same click again: same CSRF token (still valid), same idempotency key, same everything.
r2 = boss.post("/admin/finance/payments/new", data=payment_data, base_url=ALPHA)
check("resubmitting the identical click is sent to the same place, not refused",
      r2.status_code == 302 and r2.headers.get("Location") == r1.headers.get("Location"),
      (r2.status_code, r2.headers.get("Location"), r1.headers.get("Location")))
count_after_two = in_school(lambda: sql(
    "SELECT count(*) FROM finance_payments WHERE student_id = :s", s=student_id)[0][0])
check("…and no second payment was recorded for it", count_after_two == count_after_one, (count_after_one, count_after_two))

# A different click (a new key) for the same student is a genuinely new payment.
payment_data2 = dict(payment_data, _idempotency_key="a-different-click-xyz789",
                     _csrf_token=csrf(boss, "/admin/finance/payments/new", ALPHA), reference="res-idem-2")
r3 = boss.post("/admin/finance/payments/new", data=payment_data2, base_url=ALPHA)
count_after_three = in_school(lambda: sql(
    "SELECT count(*) FROM finance_payments WHERE student_id = :s", s=student_id)[0][0])
check("…while a genuinely different click (its own key) records its own payment",
      r3.status_code == 302 and count_after_three == count_after_two + 1, (r3.status_code, count_after_three))

# ================================================================ the exam page's own retry logic, in a real browser
CHROME = str(_find_chrome()) if _find_chrome() else None
if not CHROME:
    print("SKIP the exam save-retry script in a real browser: Chrome was not found (set BRIGHTSTARS_CHROME).")
else:
    exam_source = open(os.path.join(ROOT, "templates", "exam.html"), encoding="utf-8").read()
    script_match = re.search(r"<script[^>]*>\s*\nconst qid=.*?\n</script>", exam_source, re.S)
    script_body = re.sub(r"^<script[^>]*>|</script>$", "", script_match.group(0)).replace(
        "{{q.id}}", "1").replace("{{remaining}}", "600")
    page = f"""<!doctype html><html><body>
<div class="timer" id="timer">--:--</div><div class="save-status" id="saveStatus"></div>
<input type="hidden" id="csrfToken" value="test-token">
<label><input type="radio" name="option" value="0" checked></label>
<a id="next" href="/exam?q=2">Save &amp; Next</a>
<pre id="out"></pre>
<script>
window.__calls = 0;
window.__mode = 'retry-then-success';
window.fetch = function(url, opts) {{
  // Only answer saves are counted. The page's liveness ping (/exam/heartbeat) also goes through
  // fetch every few seconds, so counting it made these checks depend on timing.
  if (url !== '/answer') return Promise.resolve({{ok: true, status: 200}});
  window.__calls += 1;
  if (window.__mode === 'retry-then-success') {{
    if (window.__calls < 3) return Promise.reject(new TypeError('network error'));
    return Promise.resolve({{ok: true, status: 200}});
  }}
  if (window.__mode === 'always-fail-network') return Promise.reject(new TypeError('network error'));
  if (window.__mode === 'always-fail-server') return Promise.resolve({{ok: false, status: 500}});
  return Promise.resolve({{ok: true, status: 200}});
}};
</script>
<script>{script_body}</script>
<script>
(async function () {{
  await save();
  var afterSuccess = {{calls: window.__calls, status: document.getElementById('saveStatus').textContent}};

  window.__mode = 'always-fail-network'; window.__calls = 0;
  await save();
  var afterNetwork = {{calls: window.__calls, status: document.getElementById('saveStatus').textContent}};

  window.__mode = 'always-fail-server'; window.__calls = 0;
  await save();
  var afterServer = {{calls: window.__calls, status: document.getElementById('saveStatus').textContent}};

  document.getElementById('out').textContent = JSON.stringify(
    {{afterSuccess: afterSuccess, afterNetwork: afterNetwork, afterServer: afterServer}});
}})();
</script>
</body></html>"""
    path = os.path.join(TMP, "exam_retry_check.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)
    run = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--allow-file-access-from-files",
                          "--virtual-time-budget=15000", "--dump-dom",
                          "file:///" + path.replace(os.sep, "/")], capture_output=True, text=True, timeout=60)
    found = re.search(r'<pre id="out">(.*?)</pre>', run.stdout, re.S)
    outcome = json.loads(html.unescape(found.group(1))) if found else {}
    check("the exam page's retry script ran in a real browser", bool(outcome), run.stderr[-300:] if not outcome else "")
    if outcome:
        check("two network failures then a success end up saved, not abandoned",
              outcome["afterSuccess"]["calls"] == 3 and outcome["afterSuccess"]["status"] == "Saved",
              outcome["afterSuccess"])
        check("a save that always fails over the network is retried a bounded number of times, then says so plainly",
              outcome["afterNetwork"]["calls"] == 4  # the first try plus 3 retries
              and "Could not save" in outcome["afterNetwork"]["status"], outcome["afterNetwork"])
        check("a transient server error (not a real end state) is retried the same way, never ending the paper on its own",
              outcome["afterServer"]["calls"] == 4 and "Could not save" in outcome["afterServer"]["status"],
              outcome["afterServer"])

    # ================================================================ the generic connectivity banner, in the same browser
    banner_page = f"""<!doctype html><html><body>
<div class="connectivity-banner" data-connectivity-banner></div>
<pre id="out"></pre>
<script src="file:///{os.path.join(ROOT, 'static', 'connectivity.js').replace(os.sep, '/')}"></script>
<script>
var el = document.querySelector('[data-connectivity-banner]');
window.dispatchEvent(new Event('offline'));
var afterOffline = {{text: el.textContent, offline: el.classList.contains('connectivity-banner-offline'),
                     visible: el.classList.contains('connectivity-banner-visible')}};
window.dispatchEvent(new Event('online'));
var afterOnline = {{text: el.textContent, offline: el.classList.contains('connectivity-banner-offline'),
                    visible: el.classList.contains('connectivity-banner-visible')}};
document.getElementById('out').textContent = JSON.stringify({{afterOffline: afterOffline, afterOnline: afterOnline}});
</script>
</body></html>"""
    banner_path = os.path.join(TMP, "connectivity_check.html")
    with open(banner_path, "w", encoding="utf-8") as fh:
        fh.write(banner_page)
    run2 = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--allow-file-access-from-files", "--dump-dom",
                           "file:///" + banner_path.replace(os.sep, "/")], capture_output=True, text=True, timeout=30)
    found2 = re.search(r'<pre id="out">(.*?)</pre>', run2.stdout, re.S)
    banner_outcome = json.loads(html.unescape(found2.group(1))) if found2 else {}
    check("the connectivity banner ran in a real browser", bool(banner_outcome), run2.stderr[-300:] if not banner_outcome else "")
    if banner_outcome:
        check("going offline shows the banner, in its offline colour",
              banner_outcome["afterOffline"]["visible"] and banner_outcome["afterOffline"]["offline"]
              and "offline" in banner_outcome["afterOffline"]["text"].lower(), banner_outcome["afterOffline"])
        check("coming back online says so, no longer in the offline colour",
              banner_outcome["afterOnline"]["visible"] and not banner_outcome["afterOnline"]["offline"]
              and "online" in banner_outcome["afterOnline"]["text"].lower(), banner_outcome["afterOnline"])

    # ================================================================ static/interactions.js, in the same browser
    # This one script is what every onclick/onchange/onsubmit attribute in every template was
    # moved to (see recommendations.html's Hardening section: script-src no longer allows
    # 'unsafe-inline'), so proving this ONE shared mechanism works in a real browser stands in for
    # separately testing it on each of the ~90 pages that now depend on it.
    interactions_page = f"""<!doctype html><html><body>
<form id="confirmForm" data-confirm="are you sure?">
  <button type="submit" id="confirmBtn">Delete</button>
</form>
<form><select id="autoSubmitSelect" data-autosubmit><option value="a">a</option><option value="b">b</option></select></form>
<dialog id="theDialog"></dialog>
<button id="openBtn" data-modal-open="theDialog">Open</button>
<button id="closeBtn" data-modal-close="theDialog">Close</button>
<button id="toggleBtn" data-toggle-class="nav-open">Toggle</button>
<button id="callBtn" data-call="myNamedFunction" data-call-arg="42">Call</button>
<button id="printBtn" data-print>Print</button>
<pre id="out"></pre>
<script src="file:///{os.path.join(ROOT, 'static', 'interactions.js').replace(os.sep, '/')}"></script>
<script>
// Registered on document, same as interactions.js's own delegated listener, and after it (this
// script loads after interactions.js) - so for the SAME target, this one runs second and sees
// whatever interactions.js already decided, the same way two document-level listeners genuinely
// would (a form-level listener would run first, during the bubble phase, before interactions.js
// ever saw the event, and so would prove nothing).
window.__submitDefaultPrevented = null;
document.addEventListener('submit', function (e) {{
  window.__submitDefaultPrevented = e.defaultPrevented; e.preventDefault();
}});
window.__autosubmitCalled = false;
document.getElementById('autoSubmitSelect').form.submit = function () {{ window.__autosubmitCalled = true; }};
window.__namedCallArgs = null;
window.myNamedFunction = function (el, arg) {{ window.__namedCallArgs = [el.id, arg]; }};
window.__printCalled = false;
window.print = function () {{ window.__printCalled = true; }};

var errors = [];
function step(name, fn) {{ try {{ return fn(); }} catch (e) {{ errors.push(name + ': ' + e); return null; }} }}

window.confirm = function () {{ return false; }};
document.getElementById('confirmBtn').click();
var declined = {{defaultPrevented: window.__submitDefaultPrevented}};

window.__submitDefaultPrevented = null;
window.confirm = function () {{ return true; }};
document.getElementById('confirmBtn').click();
var accepted = {{defaultPrevented: window.__submitDefaultPrevented}};

document.getElementById('autoSubmitSelect').value = 'b';
document.getElementById('autoSubmitSelect').dispatchEvent(new Event('change', {{bubbles: true}}));
var autosubmit = {{called: window.__autosubmitCalled}};

var openedState = step('open', function () {{
  document.getElementById('openBtn').click();
  return document.getElementById('theDialog').open;
}});
var closedState = step('close', function () {{
  document.getElementById('closeBtn').click();
  return document.getElementById('theDialog').open;
}});

document.getElementById('toggleBtn').click();
var toggledOnState = document.body.classList.contains('nav-open');
document.getElementById('toggleBtn').click();
var toggledOffState = document.body.classList.contains('nav-open');

document.getElementById('callBtn').click();
var calledArgs = window.__namedCallArgs;

document.getElementById('printBtn').click();
var printedState = window.__printCalled;

document.getElementById('out').textContent = JSON.stringify({{
  declined: declined, accepted: accepted, autosubmit: autosubmit,
  openedState: openedState, closedState: closedState,
  toggledOnState: toggledOnState, toggledOffState: toggledOffState,
  calledArgs: calledArgs, printedState: printedState, errors: errors
}});
</script>
</body></html>"""
    interactions_path = os.path.join(TMP, "interactions_check.html")
    with open(interactions_path, "w", encoding="utf-8") as fh:
        fh.write(interactions_page)
    run3 = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--allow-file-access-from-files", "--dump-dom",
                           "file:///" + interactions_path.replace(os.sep, "/")], capture_output=True, text=True, timeout=30)
    found3 = re.search(r'<pre id="out">(.*?)</pre>', run3.stdout, re.S)
    js_outcome = json.loads(html.unescape(found3.group(1))) if found3 else {}
    check("interactions.js ran in a real browser", bool(js_outcome), run3.stderr[-300:] if not js_outcome else "")
    if js_outcome:
        check("no unexpected error was thrown while exercising it", not js_outcome.get("errors"), js_outcome.get("errors"))
        check("a declined confirm on a button never lets its form submit",
              js_outcome["declined"]["defaultPrevented"] is True, js_outcome["declined"])
        check("an accepted confirm lets the very same form submit reach its handler",
              js_outcome["accepted"]["defaultPrevented"] is False, js_outcome["accepted"])
        check("a data-autosubmit field submits its form on change",
              js_outcome["autosubmit"]["called"] is True, js_outcome["autosubmit"])
        check("data-modal-open opens the named <dialog>, data-modal-close closes it",
              js_outcome["openedState"] is True and js_outcome["closedState"] is False, js_outcome)
        check("data-toggle-class toggles a class on <body>, on and off",
              js_outcome["toggledOnState"] is True and js_outcome["toggledOffState"] is False, js_outcome)
        check("data-call dispatches to the named global function with its data-call-arg",
              js_outcome["calledArgs"] == ["callBtn", "42"], js_outcome["calledArgs"])
        check("data-print calls window.print()",
              js_outcome["printedState"] is True, js_outcome["printedState"])

print(f"\n{sum(1 for _, ok, _ in results if ok)}/{len(results)} checks passed")
if DROP_TEST_DATABASES:
    DROP_TEST_DATABASES()
import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if all(ok for _, ok, _ in results) else 1)
