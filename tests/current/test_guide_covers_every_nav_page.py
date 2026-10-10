"""Contract: the staff guide mentions every real page a school admin can navigate to.

templates/admin_base.html's navigation rail (workspace_groups for the School workspace, entrance_items for the
Entrance workspace, administration_items and the foot of the menu) is the definitive list of "every page for
school admins" the user asked the guide to cover - it is also exactly what a person actually sees
in the menu, so it cannot silently drift from what the guide describes. A small, explicit list of
endpoints is exempt (sign-in/out, the guide's own pages, and pages that are themselves a step
inside a flow already covered by the page that leads to them). Everything else must be named by a
literal ``url_for('endpoint'`` somewhere under templates/school_guide/.

This reads source only, and needs no database.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADMIN_BASE = (ROOT / "templates" / "admin_base.html").read_text(encoding="utf-8")
GUIDE_DIR = ROOT / "templates" / "school_guide"

# A page reached only as a detail/edit/print/pdf view of something a list page already covers -
# the guide describes what you can do once you are there, in prose, rather than naming every one
# of these by its own url_for (many take a required id and cannot be linked without one anyway).
EXEMPT = {
    "admin_workspace_home",  # "Switch workspace" - covered as a concept (switching workspaces), welcome.html
    "admin_guide_home",  # the guide's own home page
}


def _nav_endpoints():
    """Every 'endpoint':'name' the navigation rail can show, for a signed-in school admin."""
    return set(re.findall(r"'endpoint'\s*:\s*'(\w+)'", ADMIN_BASE))


def _guide_text():
    return "\n".join(p.read_text(encoding="utf-8") for p in GUIDE_DIR.glob("*.html") if p.stem != "search")


def test_every_navigation_endpoint_is_mentioned_in_the_guide():
    guide_text = _guide_text()
    missing = []
    for endpoint in sorted(_nav_endpoints() - EXEMPT):
        if not re.search(r"url_for\(\s*'" + re.escape(endpoint) + r"'", guide_text):
            missing.append(endpoint)
    assert not missing, f"these navigation pages are never linked from the staff guide: {missing}"


def test_the_search_found_at_least_twenty_navigation_pages():
    # A floor, not a ceiling: catches the extraction regex silently matching nothing (which would
    # make the test above vacuously pass) far more reliably than a hand-counted exact number.
    assert len(_nav_endpoints()) >= 20, f"only found {len(_nav_endpoints())} nav endpoints - check the regex"
