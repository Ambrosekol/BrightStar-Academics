"""The staff guide (/admin/guide): source-level contracts.

Needs no database and does not start the application. It reads the source, because what it guards
is written there:

* every ``url_for(...)`` in a guide page names a real endpoint;
* every ``page('slug', ...)`` link between guide pages names a real page;
* every slug in the guide's own registry (``GUIDE_PAGES``) has a template, and every template under
  templates/school_guide/ (besides the shared partials) is registered as a page — neither can drift
  from the other;
* the guide carries no permission of its own in the endpoint map, so any signed-in admin can read it
  (the ``admin.access`` fallback core/security.py gives an unmapped endpoint);
* the navigation rail links to it.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUIDE_DIR = ROOT / "templates" / "school_guide"
GUIDE_SOURCE = (ROOT / "blueprints" / "school" / "guide.py").read_text(encoding="utf-8")
SECURITY_SOURCE = (ROOT / "core" / "security.py").read_text(encoding="utf-8")
ADMIN_BASE = (ROOT / "templates" / "admin_base.html").read_text(encoding="utf-8")

_SKIP = {"venv", "__pycache__", "tenants", "graphify-out", "node_modules", ".git"}


def _python_sources():
    for path in ROOT.rglob("*.py"):
        if _SKIP.isdisjoint(path.relative_to(ROOT).parts):
            yield path.read_text(encoding="utf-8", errors="replace")


def _guide_slugs():
    return set(re.findall(r"\('([a-z][a-z-]*)',\s*'[^']*',\s*'[^']*'\)", GUIDE_SOURCE))


def _guide_templates():
    return {p.stem for p in GUIDE_DIR.glob("*.html") if not p.stem.startswith("_") and p.stem != "search"}


def test_every_guide_slug_has_a_template():
    slugs = _guide_slugs()
    templates = _guide_templates()
    assert slugs, "GUIDE_PAGES parsed as empty — the slug pattern in this test may need updating"
    assert not (slugs - templates), f"guide pages with no template: {slugs - templates}"
    assert not (templates - slugs), f"guide templates not registered as a page: {templates - slugs}"


def test_every_url_for_in_a_guide_page_names_a_real_endpoint():
    sources = "\n".join(_python_sources())
    missing = []
    for path in sorted(GUIDE_DIR.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        for name in re.findall(r"url_for\(\s*'([a-zA-Z_][a-zA-Z0-9_]*)'", text):
            if name == "static":
                continue
            if not re.search(r"^def " + re.escape(name) + r"\(", sources, re.M):
                missing.append((path.name, name))
    assert not missing, f"guide pages link to endpoints that do not exist: {missing}"


def test_every_page_macro_link_names_a_real_guide_slug():
    slugs = _guide_slugs()
    missing = []
    for path in sorted(GUIDE_DIR.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        for slug in re.findall(r"\{\{-?\s*page\(\s*'([^']+)'", text):
            if slug not in slugs:
                missing.append((path.name, slug))
    assert not missing, f"guide pages link to a page slug that is not registered: {missing}"


def test_the_guide_carries_no_permission_of_its_own():
    """An unmapped admin endpoint falls back to admin.access, which nearly every staff account holds
    — the deliberate design here (see the module docstring of blueprints/school/guide.py). If one of
    the guide's endpoints is ever added to ADMIN_ENDPOINT_PERMISSIONS, it must stay admin.access."""
    for name in ("admin_guide_home", "admin_guide_page", "admin_guide_search"):
        mapped = re.search(r"'" + name + r"'\s*:\s*'([^']+)'", SECURITY_SOURCE)
        assert mapped is None or mapped.group(1) == "admin.access", (
            f"{name} is mapped to {mapped.group(1) if mapped else None}, narrowing who may read the guide")


def test_the_navigation_rail_links_to_the_guide():
    assert "admin_guide_home" in ADMIN_BASE, "no navigation entry points at the guide"
