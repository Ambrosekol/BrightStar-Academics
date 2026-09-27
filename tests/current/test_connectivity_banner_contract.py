"""Contract: every real page says out loud when the connection drops or comes back
(static/connectivity.js, core/connectivity.py) - a person on a weak connection is not left
guessing whether a click did anything.

A page that {% extends %} admin_base.html or auth_base.html inherits the banner from there; a
page with its own <body> (the student, parent and candidate portals, several standalone admin
pages) must carry the call itself. This reads source only, and needs no database.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"

# Deliberately without the banner:
# - print-only pages: no interactivity, and a fixed banner would sit in the printed output;
# - error pages: what a 404/500 needs is an explanation of the error, not connectivity chatter;
# - public marketing/privacy pages: no account, no form, nothing here depends on staying online;
# - the two shared layouts themselves are covered by the second test below, not this one;
# - exam.html has its own bespoke, more specific save-status banner instead of the generic one.
EXEMPT = {
    "admin_base.html", "auth_base.html", "exam.html",
    "admin_candidate_credentials_print.html", "admin_candidate_result_image.html",
    "admin_candidate_result_print.html", "admin_result_print.html",
    "admin_results_summary_print.html", "finance_receipt_print.html",
    "student_credentials_print.html",
    "error_4xx.html", "error_5xx.html", "marketing.html", "privacy.html",
}


def _standalone_pages():
    """Every template with its own <body> tag that does not {% extends %} a shared layout."""
    pages = []
    for path in sorted(TEMPLATES.glob("*.html")):
        source = path.read_text(encoding="utf-8")
        if "{% extends" in source or "<body" not in source:
            continue
        pages.append(path)
    return pages


def test_every_standalone_page_says_when_the_connection_drops():
    missing = [path.name for path in _standalone_pages()
               if path.name not in EXEMPT and "connectivity_banner()" not in path.read_text(encoding="utf-8")]
    assert not missing, f"these standalone pages carry no connectivity banner: {missing}"
    assert len(_standalone_pages()) >= 30, "the search found suspiciously few standalone pages"


def test_the_two_shared_layouts_carry_it_so_everything_extending_them_does_too():
    for name in ("admin_base.html", "auth_base.html"):
        source = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "connectivity_banner()" in source, f"{name} lost its connectivity banner"


def test_it_is_registered_as_a_template_global_like_csrf_token():
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "from core.connectivity import connectivity_banner" in app_source
    assert "'connectivity_banner': connectivity_banner," in app_source
