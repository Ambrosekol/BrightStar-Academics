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

Run:  python tests/verification/write_paths_resilience.py
"""
import os
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

print(f"\n{sum(1 for _, ok, _ in results if ok)}/{len(results)} checks passed")
if DROP_TEST_DATABASES:
    DROP_TEST_DATABASES()
import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if all(ok for _, ok, _ in results) else 1)
