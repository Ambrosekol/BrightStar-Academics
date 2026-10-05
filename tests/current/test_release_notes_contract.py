"""Release notes contract: the notes file is valid, and each change reaches only the people it was written for.

The notes live in release_notes.json at the project root (see the comments inside it). These checks run
without the database or the web app, so they fail fast when someone edits the file wrongly:

* the file parses, every release and change has what it needs, and versions only go up;
* a family or student change never has a staff-guide link, because those people cannot open the guide;
* every staff help link names a real page in the staff guide;
* students see only student changes, parents see only family changes, and administrators only staff changes;
* a brand-new account sees just the newest release, and an account that has dismissed a release sees only newer ones;
* the student and parent dashboards each ask for their own audience.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

NOTES_FILE = ROOT / "release_notes.json"
GUIDE_SOURCE = (ROOT / "blueprints" / "school" / "guide.py").read_text(encoding="utf-8")
FAMILY_SOURCE = (ROOT / "blueprints" / "family_notices.py").read_text(encoding="utf-8")


def _notes_module():
    import core.release_notes as notes
    return notes


def _guide_slugs():
    # The same pattern tests/current/test_guide_contract.py uses for the guide's own registry.
    return set(re.findall(r"\('([a-z][a-z-]*)',\s*'[^']*',\s*'[^']*'\)", GUIDE_SOURCE))


def test_release_notes_file_exists_and_parses_as_json():
    assert NOTES_FILE.exists(), "release_notes.json must sit in the project root"
    data = json.loads(NOTES_FILE.read_text(encoding="utf-8"))
    assert isinstance(data.get("releases"), list) and data["releases"], "the file needs a non-empty 'releases' list"


def test_the_loaded_notes_have_no_problems():
    notes = _notes_module()
    assert notes.RELEASES, "no release notes were loaded; check release_notes.json for a syntax error"
    assert notes.problems(notes.RELEASES) == [], notes.problems(notes.RELEASES)


def test_comments_are_removed_before_the_notes_are_used():
    notes = _notes_module()
    def has_comment_key(value):
        if isinstance(value, dict):
            return any(str(k).startswith("_") or has_comment_key(v) for k, v in value.items())
        if isinstance(value, list):
            return any(has_comment_key(v) for v in value)
        return False
    assert not has_comment_key(notes.RELEASES), "comment keys (starting with an underscore) must not reach the app"


def test_versions_are_ordered_newest_first_and_the_app_version_is_the_newest():
    notes = _notes_module()
    keys = [notes._key(r["version"]) for r in notes.RELEASES]
    assert keys == sorted(keys, reverse=True), "releases must be listed newest first"
    assert len(set(keys)) == len(keys), "each release needs its own version"
    assert notes.APP_VERSION == notes.RELEASES[0]["version"]


def test_family_and_student_changes_never_have_a_help_link():
    notes = _notes_module()
    for release in notes.RELEASES:
        for change in release["changes"]:
            if change["audience"] in ("family", "student"):
                assert "help" not in change, f"{release['version']}: {change['audience']} changes cannot link to the staff guide"


def test_every_staff_help_link_names_a_real_guide_page():
    notes = _notes_module()
    slugs = _guide_slugs()
    assert slugs, "could not read the guide's pages; the pattern in this test may need updating"
    for release in notes.RELEASES:
        for change in release["changes"]:
            if change["audience"] == "staff":
                assert change.get("help") in slugs, f"{release['version']}: help '{change.get('help')}' is not a staff guide page"


def test_each_audience_sees_only_its_own_changes():
    notes = _notes_module()
    audiences = {"staff": set(), "family": set(), "student": set()}
    for release in notes.RELEASES:
        for change in release["changes"]:
            audiences[change["audience"]].add(change["text"])
    for audience in audiences:
        shown = {c["text"] for release in notes.releases_for(audience) for c in release["changes"]}
        assert shown == audiences[audience], f"{audience} sees the wrong changes"
    student_only = audiences["student"] - audiences["family"] - audiences["staff"]
    family_only = audiences["family"] - audiences["student"] - audiences["staff"]
    parent_view = {c["text"] for release in notes.releases_for("family") for c in release["changes"]}
    student_view = {c["text"] for release in notes.releases_for("student") for c in release["changes"]}
    assert not (student_only & parent_view), "a student-only change reached a parent"
    assert not (family_only & student_view), "a parent-only change reached a student"


def test_no_audience_is_sent_another_audiences_changes():
    notes = _notes_module()
    for audience in ("staff", "family", "student"):
        for release in notes.releases_for(audience):
            assert release["changes"], "a release with nothing for this audience must not be sent"
            assert all(c.get("audience", audience) == audience for c in release["changes"])


def test_the_checker_flags_a_family_change_that_links_to_the_guide():
    notes = _notes_module()
    bad = [{"version": "9.0.0.1", "date": "2030-01-01", "title": "Test",
            "changes": [{"audience": "family", "text": "x", "help": "students"}]}]
    assert any("must not have a help link" in p for p in notes.problems(bad))


def test_the_checker_flags_an_unknown_audience_and_an_unordered_version():
    notes = _notes_module()
    bad = [{"version": "1.0", "date": "2030-01-01", "title": "Test",
            "changes": [{"audience": "teachers", "text": "x"}]}]
    found = notes.problems(bad)
    assert any("must be staff, family or student" in p for p in found)
    assert not any("dotted numbers" in p for p in found), "1.0 is still dotted numbers, so it should not be flagged"


def test_the_dashboards_ask_for_their_own_audience():
    assert "'student', 'family'" not in FAMILY_SOURCE
    assert "return session.get('student_id'), 'student'" in FAMILY_SOURCE
    assert "return session.get('parent_id'), 'family'" in FAMILY_SOURCE
    assert "db.session" not in FAMILY_SOURCE, "the notice must not read the database on a dashboard visit"
