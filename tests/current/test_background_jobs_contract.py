"""Contract: nothing slow is handed to a bare thread any more.

core/background.py's run_in_background started a thread and forgot it - if the process
restarted, crashed, or the connection to a mail/WhatsApp server dropped mid-send, the work was
lost with no record it had ever been meant to happen. core/jobs.py replaced it with a durable
row (background_jobs) written before the thread starts, so this checks that replacement is
actually used everywhere something slow is sent, not just added alongside the old way. It reads
source only, and needs no database - the behavioural proof (a real job that succeeds, fails,
gets stuck, and is retried) is tests/verification/write_paths_resilience.py.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _text(*parts):
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def test_the_bare_thread_module_is_gone():
    assert not (ROOT / "core" / "background.py").exists(), (
        "core/background.py still exists - a new caller may have started using it again")
    for folder in ("blueprints", "core", "control_plane", "models", "services"):
        for path in (ROOT / folder).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert "run_in_background" not in source, f"{path.relative_to(ROOT)} still calls run_in_background"


def test_every_registered_job_kind_has_exactly_one_handler():
    jobs_source = _text("core", "jobs.py")
    assert "def job_handler(kind):" in jobs_source
    assert "def enqueue(kind, **payload):" in jobs_source
    kinds = []
    for path in (ROOT / "blueprints").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        kinds += re.findall(r"@job_handler\(['\"]([\w.-]+)['\"]\)", source)
        kinds += [None] * 0  # (nothing; keeps the loop shape obvious if extended)
    assert len(kinds) >= 3, f"expected at least the three known jobs, found {kinds}"
    assert len(kinds) == len(set(kinds)), f"a job kind is registered more than once: {kinds}"


def test_the_three_known_slow_notifications_go_through_enqueue():
    checks = {
        "blueprints/finance/routes.py": ("enqueue('send_payment_receipt'", "@job_handler('send_payment_receipt')"),
        "blueprints/school/result_notices.py": ("enqueue('announce_report_cards'", "@job_handler('announce_report_cards')"),
        "blueprints/school/timetable_notices.py": ("enqueue('announce_timetable'", "@job_handler('announce_timetable')"),
    }
    for rel, (call, handler) in checks.items():
        source = _text(*rel.split("/"))
        assert call in source, f"{rel} no longer enqueues its job the expected way"
    helpers = _text("blueprints", "finance", "helpers.py")
    assert "@job_handler('send_payment_receipt')" in helpers
    for rel in ("blueprints/school/result_notices.py", "blueprints/school/timetable_notices.py"):
        source = _text(*rel.split("/"))
        assert "@job_handler(" in source, f"{rel} lost its @job_handler registration"
