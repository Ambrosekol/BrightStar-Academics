"""Whole-platform stress test: many schools, many people, all at once, against real PostgreSQL.

The README's Scale table and load_test_exam_answers.py (same folder) both look at one pressure
at a time, on one school. This script asks the question the way it actually happens: sixty
schools' worth of students logging in, parents and schools trading messages, and every school's
own exam day landing in the same few minutes - all sharing the one PostgreSQL server and the one
running application, exactly as production does (one deployment, "own database" only means own
*database*, not own *server*).

It does three things, in order:

1. Provisions ``--schools`` real, throwaway tenant databases (never a school actually in use -
   see _pg.py) and bulk-seeds each with ``--staff`` admins, ``--students`` students and a matching
   number of parent accounts linked to them.
2. Ramps up concurrent load in steps (``--ramp``), round-robining jobs across every provisioned
   school. Each job is a login (password check + the shared rate limiter, exactly like
   blueprints/auth/routes.py's login()), a parent<->school message exchange (ParentFeedback +
   ParentFeedbackReply, like the parent portal's feedback thread), or an exam-answer upsert (the
   same statement load_test_exam_answers.py fires).
3. Stops the ramp and reports the breaking point as soon as a step's error rate or p95 latency
   crosses the given thresholds, classifying *why* (which connection pool ran out, or something
   else) rather than just reporting numbers.

No pass/fail threshold beyond what you pass in: hardware and PostgreSQL's own max_connections
vary, and where a real school would consider this "broken" is a judgement call, same as
load_test_exam_answers.py.

Run: python tests/verification/stress_test_platform.py [options]
Small correctness check first: --schools 3 --students 50 --staff 5 --ramp 20,50,100
Full scenario as asked: --schools 60 --students 2000 --staff 60 (the defaults)
"""
import argparse
import itertools
import os
import random
import shutil
import statistics
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import sqlalchemy as sa

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_stress_")

p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
p.add_argument("--schools", type=int, default=60, help="how many schools to provision (default 60)")
p.add_argument("--students", type=int, default=2000, help="students per school (default 2000)")
p.add_argument("--staff", type=int, default=60, help="staff/admins per school (default 60)")
p.add_argument("--parents-per-student", type=float, default=1.0,
               help="parent accounts per student, e.g. 0.7 for one parent covering siblings (default 1.0)")
p.add_argument("--ramp", default="30,60,90,120,180,300,600,1200,2400,4800",
               help="comma list of total concurrent jobs per step, spread across every school")
p.add_argument("--workload", choices=["all", "login", "message", "exam"], default="all")
p.add_argument("--error-threshold", type=float, default=0.05, help="fraction of failed jobs that ends the ramp")
p.add_argument("--p95-threshold-ms", type=float, default=3000.0, help="p95 latency (ms) that ends the ramp")
p.add_argument("--seed-workers", type=int, default=12, help="schools seeded in parallel (default 12)")
p.add_argument("--max-minutes", type=float, default=30.0, help="hard wall-clock cap for the whole run")
p.add_argument("--keep", action="store_true", help="skip teardown, leave the throwaway databases in place")
p.add_argument("--seed-only", action="store_true", help="provision and seed, then stop before the ramp")
args = p.parse_args()

RAMP = [int(x) for x in args.ramp.split(",") if x.strip()]
DEADLINE = time.monotonic() + args.max_minutes * 60

import _pg  # noqa: E402  (same folder)

PREFIX, teardown = _pg.setup("stress")
os.environ.update({
    "BRIGHTSTARS_TENANTS_DIR": os.path.join(TMP, "tenants"),
    "BRIGHTSTARS_PLATFORM_HOSTS": "platform.test",
    "BRIGHTSTARS_PORTAL_DOMAIN": "portal.test",
    "BRIGHTSTARS_REGISTRY_CACHE_SECONDS": "0",
    "BRIGHTSTARS_SECRET": "x" * 40,
})
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import app as A  # noqa: E402
from control_plane import provisioning as pv  # noqa: E402
from control_plane.context import tenant_context  # noqa: E402
from control_plane.registry import platform_engine  # noqa: E402
from control_plane.ratelimit import allow as rate_limit_allow  # noqa: E402
from blueprints.auth.routes import _authenticate_unified  # noqa: E402
from core.db_helpers import insert_stmt  # noqa: E402
from models import (  # noqa: E402
    Admin, AdminType, Answer, ParentAccount, ParentFeedback, ParentFeedbackReply,
    ParentStudentLink, Student, db,
)
from werkzeug.security import generate_password_hash  # noqa: E402

PASSWORD = "LoadTest#12345"
# Hashed once and reused for every seeded account: the point of the login workload is the real
# per-attempt cost (the hash check plus the shared rate limiter), not re-paying the (deliberately
# slow) hashing cost for a few hundred thousand accounts during seeding.
PASSWORD_HASH = generate_password_hash(PASSWORD)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def out_of_time():
    return time.monotonic() > DEADLINE


# --------------------------------------------------------------------------------------------
# Phase 1: provision
# --------------------------------------------------------------------------------------------

print(f"=== Phase 1: provisioning {args.schools} throwaway schools ===")
schools = []
provision_failures = []
t0 = time.perf_counter()
for i in range(args.schools):
    if out_of_time():
        print(f"Stopped provisioning early at school {i}: --max-minutes budget spent.")
        break
    slug = f"lt{i:04d}"
    try:
        info, _pw = pv.create_tenant(slug, f"Load Test School {i}", starter_banks=False, actor="stress-test")
        schools.append(info)
    except Exception as exc:
        provision_failures.append((i, type(exc).__name__, str(exc)[:200]))
        print(f"  school {i} FAILED to provision: {type(exc).__name__}: {str(exc)[:200]}")
print(f"Provisioned {len(schools)}/{args.schools} schools in {time.perf_counter() - t0:.1f}s "
      f"({len(provision_failures)} failures).")
if provision_failures:
    print("Provisioning itself is a breaking point worth reporting on its own: schools failed to "
          "come up before any load was even applied.")

if not schools:
    print("No school could be provisioned; nothing to stress. Exiting.")
    teardown()
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1)


# --------------------------------------------------------------------------------------------
# Phase 2: seed
# --------------------------------------------------------------------------------------------

def seed_one(info):
    n_students = args.students
    n_staff = args.staff
    n_parents = max(1, round(args.students * args.parents_per_student)) if args.students else 0
    with A.app.app_context(), tenant_context(info):
        admin_type = db.session.scalars(
            sa.select(AdminType).where(AdminType.is_system == 1).order_by(AdminType.id)).first()
        if admin_type is None:
            admin_type = AdminType(name="Stress Test Staff", is_system=1, active=1, created_at=now_iso())
            db.session.add(admin_type)
            db.session.flush()
        admin_type_id = admin_type.id
        created = now_iso()

        def batches(rows, size=1000):
            for start in range(0, len(rows), size):
                yield rows[start:start + size]

        staff_rows = [dict(username=f"staff{j}", display_name=f"Staff {j}", password_hash=PASSWORD_HASH,
                            admin_type_id=admin_type_id, active=1, created_at=created)
                      for j in range(n_staff)]
        for batch in batches(staff_rows):
            db.session.execute(sa.insert(Admin), batch)

        student_rows = [dict(admission_no=f"{info.slug}-{j:06d}", first_name=f"Student{j}", last_name="Test",
                              created_at=created, active=1, login_username=f"stu{j}",
                              login_password_hash=PASSWORD_HASH, account_active=1, password_must_change=0)
                         for j in range(n_students)]
        for batch in batches(student_rows):
            db.session.execute(sa.insert(Student), batch)

        parent_rows = [dict(username=f"par{j}", display_name=f"Parent {j}", password_hash=PASSWORD_HASH,
                             active=1, password_must_change=0, created_at=created)
                       for j in range(n_parents)]
        for batch in batches(parent_rows):
            db.session.execute(sa.insert(ParentAccount), batch)

        link_rows = [dict(parent_id=(j % n_parents) + 1, student_id=j + 1, active=1, created_at=created)
                     for j in range(n_students)] if n_parents else []
        for batch in batches(link_rows):
            db.session.execute(sa.insert(ParentStudentLink), batch)

        db.session.commit()
    return n_staff, n_students, n_parents


print(f"\n=== Phase 2: seeding {args.staff} staff + {args.students} students + parents per school ===")
t0 = time.perf_counter()
seeded = []
seed_errors = []
with ThreadPoolExecutor(max_workers=args.seed_workers) as pool:
    futures = {pool.submit(seed_one, info): info for info in schools}
    for fut, info in futures.items():
        try:
            seeded.append((info, fut.result()))
        except Exception as exc:
            seed_errors.append((info.slug, type(exc).__name__, str(exc)[:200]))
            print(f"  seeding {info.slug} FAILED: {type(exc).__name__}: {str(exc)[:200]}")
elapsed = time.perf_counter() - t0
total_rows = sum(a + b + c for _, (a, b, c) in seeded)
print(f"Seeded {len(seeded)}/{len(schools)} schools, {total_rows} accounts total, in {elapsed:.1f}s "
      f"({total_rows / elapsed:.0f} rows/s)." if elapsed > 0 else "Seeded instantly.")
schools = [info for info, _ in seeded]

if args.seed_only or not schools:
    print("\n--seed-only set (or nothing seeded): stopping before the load ramp.")
    if not args.keep:
        teardown()
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(0)


# --------------------------------------------------------------------------------------------
# Phase 3: ramp
# --------------------------------------------------------------------------------------------

_attempt_counter = itertools.count(1)
_counter_lock = threading.Lock()


def next_attempt_id():
    with _counter_lock:
        return next(_attempt_counter)


def job_login(info, i):
    roll = i % 10
    if roll < 7:
        identifier, kind = f"stu{i % args.students}", "student"
    elif roll < 9:
        identifier, kind = f"par{i % max(1, round(args.students * args.parents_per_student))}", "parent"
    else:
        identifier, kind = f"staff{i % args.staff}", "admin"
    with A.app.app_context(), tenant_context(info):
        key = f"stress:{info.slug}:{identifier}"
        rate_limit_allow(key, limit=10 ** 9, window=300)  # exercise the shared limiter, never block the test on it
        kind_found, account = _authenticate_unified(identifier, PASSWORD)
        if account is None:
            raise RuntimeError(f"login did not authenticate {kind}:{identifier} (kind found: {kind_found})")


def job_message(info, i):
    parents = max(1, round(args.students * args.parents_per_student))
    parent_id = (i % parents) + 1
    student_id = (i % args.students) + 1
    with A.app.app_context(), tenant_context(info):
        created = now_iso()
        feedback = ParentFeedback(parent_id=parent_id, student_id=student_id,
                                   subject="Stress test message", body="How is my child doing this week?",
                                   status="open", created_at=created, updated_at=created)
        db.session.add(feedback)
        db.session.flush()
        reply = ParentFeedbackReply(feedback_id=feedback.id, admin_id=(i % args.staff) + 1,
                                     body="Thanks for reaching out, we'll update you shortly.",
                                     created_at=now_iso())
        db.session.add(reply)
        db.session.commit()


def job_exam(info, i):
    attempt_id = next_attempt_id()
    with A.app.app_context(), tenant_context(info):
        for q in range(3):
            stmt = insert_stmt(Answer).values(
                attempt_id=attempt_id, question_id=q, option_index=q % 4, answered_at=now_iso())
            db.session.execute(stmt.on_conflict_do_update(
                index_elements=["attempt_id", "question_id"],
                set_={"option_index": stmt.excluded.option_index, "answered_at": stmt.excluded.answered_at}))
        db.session.commit()


WORKLOADS = {"login": job_login, "message": job_message, "exam": job_exam}
CYCLE = list(WORKLOADS) if args.workload == "all" else [args.workload]


def run_one(job):
    i, kind = job
    info = schools[i % len(schools)]
    fn = WORKLOADS[kind]
    started = time.perf_counter()
    try:
        fn(info, i)
        return time.perf_counter() - started, None
    except Exception as exc:
        return time.perf_counter() - started, type(exc).__name__


def registry_pool_status():
    pool = platform_engine().pool
    try:
        return f"registry pool: {pool.checkedout()}/{pool.size()} checked out, {pool.overflow()} overflow"
    except Exception:
        return "registry pool: (status unavailable)"


print(f"\n=== Phase 3: ramping load across {len(schools)} live schools ===")
print(f"Workload: {args.workload}   Stop conditions: error rate > {args.error_threshold:.0%} "
      f"or p95 > {args.p95_threshold_ms:.0f}ms\n")

broke_at = None
break_reason = None
for level in RAMP:
    if out_of_time():
        print(f"Stopping ramp: --max-minutes budget spent before reaching {level}.")
        break
    jobs = [(i, CYCLE[i % len(CYCLE)]) for i in range(level)]
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=level) as pool:
        results = list(pool.map(run_one, jobs))
    elapsed = time.perf_counter() - started

    latencies = sorted(r[0] for r in results if r[1] is None)
    errors = [r[1] for r in results if r[1] is not None]
    error_rate = len(errors) / len(results)
    p50 = latencies[len(latencies) // 2] * 1000 if latencies else float("nan")
    p95 = latencies[int(len(latencies) * 0.95)] * 1000 if latencies else float("nan")
    worst = latencies[-1] * 1000 if latencies else float("nan")
    ops_per_sec = level / elapsed

    error_counts = {}
    for e in errors:
        error_counts[e] = error_counts.get(e, 0) + 1
    error_summary = ", ".join(f"{k}x{v}" for k, v in sorted(error_counts.items(), key=lambda kv: -kv[1])[:5])

    print(f"[{level:>5} concurrent] {ops_per_sec:7.0f} ops/s | latency p50={p50:6.0f}ms p95={p95:6.0f}ms "
          f"worst={worst:6.0f}ms | errors={len(errors)}/{len(results)} ({error_rate:.1%}) {error_summary}")
    print(f"                {registry_pool_status()}")

    if error_rate > args.error_threshold or (latencies and p95 > args.p95_threshold_ms):
        broke_at = level
        break_reason = (f"error rate {error_rate:.1%} (> {args.error_threshold:.0%})" if error_rate > args.error_threshold
                         else f"p95 latency {p95:.0f}ms (> {args.p95_threshold_ms:.0f}ms)")
        print(f"\n>>> BREAKING POINT: {level} concurrent jobs across {len(schools)} schools "
              f"({level / len(schools):.1f} per school) — {break_reason}")
        print(f">>> Dominant error(s): {error_summary or 'none (latency-driven)'}")
        break

print("\n=== Summary ===")
print(f"Schools live: {len(schools)}/{args.schools}   Provisioning failures: {len(provision_failures)}   "
      f"Seeding failures: {len(seed_errors)}")
if broke_at:
    print(f"Broke at {broke_at} concurrent jobs ({break_reason}).")
else:
    print(f"Did not break within the tested range (up to {RAMP[-1]} concurrent jobs) "
          f"under the given thresholds.")
print("No portable pass/fail line: judge this against how many people this deployment's own exam "
      "day, message rush or sign-in wave actually puts online at once, on the hardware it will "
      "really run on.")

if not args.keep:
    teardown()
    shutil.rmtree(TMP, ignore_errors=True)
else:
    print(f"\n--keep set: throwaway databases left in place with prefix {PREFIX!r} "
          "(drop them yourself, or rerun without --keep).")
