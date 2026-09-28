"""Load-test one school's exam-answer write rate, against real PostgreSQL.

recommendations.html's Scale table names "an exam day: hundreds of candidates at once" as the
pressure to watch, and "each answer is one small upsert" as why it should be fine - this is the
tool to actually check that, against a school's own database, before its first big entrance day,
rather than trusting the claim. It fires many of the exact upsert `/answer` performs
(insert_stmt(Answer).on_conflict_do_update(...), blueprints/candidate_portal/routes.py) at once,
from a pool of threads, and reports how many completed per second and how long the slowest one
took - the two numbers that matter for "will candidates notice a delay when they select an
answer". It has no pass/fail threshold of its own: hardware varies, and a number worth acting on is
a judgement call for whoever runs this against their own database, not a portable assertion.

Run against a throwaway database (the default): python tests/verification/load_test_exam_answers.py
Run against a real school's own database, to test the machine it will really run on:
    BRIGHTSTARS_PLATFORM_DB=<its platform db> python tests/verification/load_test_exam_answers.py --school CODE
(never point this at a school actually in use - it writes real rows to the answers table)
"""
import argparse
import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_load_")

p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
p.add_argument("--candidates", type=int, default=300, help="how many candidates 'sit' at once (default 300)")
p.add_argument("--answers-each", type=int, default=3, help="answers each one saves (default 3)")
p.add_argument("--workers", type=int, default=40, help="concurrent worker threads (default 40)")
p.add_argument("--school", help="an existing school's code, instead of a fresh throwaway one")
args = p.parse_args()

import _pg  # noqa: E402  (same folder)

teardown = None
if not args.school:
    PREFIX, teardown = _pg.setup("loadtest")
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
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from models import Answer, db  # noqa: E402
from core.db_helpers import insert_stmt  # noqa: E402

if args.school:
    with platform_session() as session:
        tenant = get_tenant(session, args.school)
        if not tenant:
            print(f"No school with code {args.school!r}."); sys.exit(2)
        info = to_info(tenant)
else:
    info, _ = pv.create_tenant("loadtest", "Load Test School", starter_banks=False, actor="load-test")

total_writes = args.candidates * args.answers_each
print(f"Simulating {args.candidates} candidates, {args.answers_each} answer(s) each "
      f"({total_writes} writes total), {args.workers} workers, against {info.slug!r}.")


def save_one(job):
    attempt_id, question_id = job
    with A.app.app_context(), tenant_context(info):
        stmt = insert_stmt(Answer).values(
            attempt_id=attempt_id, question_id=question_id, option_index=question_id % 4,
            answered_at=datetime.now(timezone.utc).isoformat())
        started = time.perf_counter()
        db.session.execute(stmt.on_conflict_do_update(
            index_elements=["attempt_id", "question_id"],
            set_={"option_index": stmt.excluded.option_index, "answered_at": stmt.excluded.answered_at}))
        db.session.commit()
        return time.perf_counter() - started


jobs = [(candidate, q) for candidate in range(args.candidates) for q in range(args.answers_each)]

started = time.perf_counter()
with ThreadPoolExecutor(max_workers=args.workers) as pool:
    latencies = list(pool.map(save_one, jobs))
elapsed = time.perf_counter() - started

latencies.sort()
p50 = latencies[len(latencies) // 2]
p95 = latencies[int(len(latencies) * 0.95)]
worst = latencies[-1]

print()
print(f"{total_writes} writes in {elapsed:.2f}s -> {total_writes / elapsed:.0f} writes/second")
print(f"per-write latency: median {p50 * 1000:.1f}ms, p95 {p95 * 1000:.1f}ms, worst {worst * 1000:.1f}ms")
print()
print("No pass/fail threshold: judge these against how many candidates this school's own entrance "
      "day actually seats at once, and how fast a page feels past a few hundred milliseconds.")

if teardown:
    teardown()
    shutil.rmtree(TMP, ignore_errors=True)
