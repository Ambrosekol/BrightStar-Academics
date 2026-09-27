"""Contract: the entrance exam's answer-autosave script never treats a dropped connection as the
paper being over.

A candidate on a weak connection must have their answer retried, not silently lost, and must
never be sent to their result page over a network hiccup or a transient server error - only a
real end state (403 the session has ended, 410 time ran out) does that. This reads source only;
the behavioural proof (a real headless browser running the actual script against a faked network)
is tests/verification/write_paths_resilience.py.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXAM_HTML = (ROOT / "templates" / "exam.html").read_text(encoding="utf-8")


def _script():
    match = re.search(r"<script>\s*\nconst qid=.*?\n</script>", EXAM_HTML, re.S)
    assert match, "templates/exam.html lost its answer-autosave script"
    return match.group(0)


def test_a_fetch_failure_is_caught_not_left_to_crash_silently():
    script = _script()
    assert "try{" in script and "}catch(networkError){" in script, (
        "the fetch call is not wrapped in try/catch: a dropped connection would throw unhandled")


def test_only_a_real_end_state_sends_the_candidate_to_their_result():
    script = _script()
    assert "res.status===403||res.status===410" in script
    # Nothing else in the retry function may redirect to /result - only that one check.
    body = script.split("async function attemptSave", 1)[1]
    redirects = re.findall(r"location\.href='/result'", body)
    assert len(redirects) == 1, f"expected exactly one /result redirect in attemptSave, found {len(redirects)}"


def test_a_network_or_server_failure_is_retried_before_giving_up():
    script = _script()
    assert "triesLeft>0" in script and "return attemptSave(fd,triesLeft-1)" in script
    assert "save(fd,3)" in script.replace("attemptSave", "save"), (
        "attemptSave is no longer called with a positive retry budget")


def test_a_giving_up_message_is_shown_in_plain_words():
    script = _script()
    assert "Could not save your answer" in script


def test_the_page_carries_a_visible_status_the_person_can_read():
    assert 'id="saveStatus"' in EXAM_HTML and 'role="status"' in EXAM_HTML
    assert ".save-status" in (ROOT / "static" / "app.css").read_text(encoding="utf-8")
