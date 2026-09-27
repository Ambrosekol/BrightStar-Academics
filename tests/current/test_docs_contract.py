"""The documentation pages (/docs) and the marketing page (/marketing): source-level contracts.

Needs no database and does not start the application. It reads the source, because what it guards
is written there:

* every hand-written documentation page refers to code only through the macros, and everything a
  macro names still exists (a file, a template, an endpoint, a table, a permission), so the text can
  never point at something that has been renamed or removed;
* every page in the sidebar has a template and every template is in the sidebar;
* /docs is reachable only by a signed-in platform admin with access, and /marketing is on platform
  hosts only, so neither can be served from a school's own address;
* only the super admin can grant or withdraw access to the documentation.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "templates" / "platform" / "docs"
SITE = (ROOT / "control_plane" / "platform_pages.py").read_text(encoding="utf-8")
CONSOLE = (ROOT / "control_plane" / "console.py").read_text(encoding="utf-8")
RESOLVER = (ROOT / "control_plane" / "resolver.py").read_text(encoding="utf-8")

_SKIP = {"venv", "__pycache__", "tenants", "graphify-out", "node_modules", ".git"}


def _python_sources():
    for path in ROOT.rglob("*.py"):
        if _SKIP.isdisjoint(path.relative_to(ROOT).parts):
            yield path.read_text(encoding="utf-8", errors="replace")


def _macro_arguments(macro):
    found = []
    for path in sorted(DOCS.glob("*.html")):
        if path.name.startswith("_"):
            continue
        text = path.read_text(encoding="utf-8")
        for argument in re.findall(r"\{\{-?\s*" + macro + r"\(\s*'([^']+)'", text):
            found.append((path.name, argument))
    return found


def test_every_file_a_page_names_exists():
    missing = [(page, name) for page, name in _macro_arguments("file") if not (ROOT / name).is_file()]
    assert not missing, f"documentation names files that do not exist: {missing}"


def test_every_template_a_page_names_exists():
    missing = [(page, name) for page, name in _macro_arguments("tpl") if not (ROOT / "templates" / name).is_file()]
    assert not missing, f"documentation names templates that do not exist: {missing}"


def test_every_endpoint_a_page_names_exists():
    sources = "\n".join(_python_sources())
    missing = [(page, name) for page, name in _macro_arguments("route")
               if not re.search(r"^def " + re.escape(name) + r"\(", sources, re.M)]
    assert not missing, f"documentation names routes that do not exist: {missing}"


def test_every_table_a_page_names_exists():
    tables = set()
    for source in _python_sources():
        tables |= set(re.findall(r"__tablename__\s*=\s*'([^']+)'", source))
    missing = [(page, name) for page, name in _macro_arguments("table") if name not in tables]
    assert not missing, f"documentation names tables that do not exist: {missing}"


def test_every_permission_a_page_names_exists():
    security = (ROOT / "core" / "security.py").read_text(encoding="utf-8")
    missing = [(page, name) for page, name in _macro_arguments("perm") if f"'{name}'" not in security]
    assert not missing, f"documentation names permissions that do not exist: {missing}"


def test_every_documentation_page_is_listed_and_every_listed_page_exists():
    listed = set(re.findall(r"\(\s*'([a-z0-9-]+)'\s*,\s*'[^']+'\s*,\s*(?:'[^']+'|\"[^\"]+\")\s*\)", SITE[SITE.index("DOC_GROUPS"):SITE.index("PAGES =")]))
    templates = {p.stem for p in DOCS.glob("*.html") if not p.name.startswith("_")} - {"search"}
    assert listed == templates, f"listed but no template: {sorted(listed - templates)}; template but not listed: {sorted(templates - listed)}"


def test_documentation_pages_use_the_layout_and_stay_out_of_search_engines():
    for path in DOCS.glob("*.html"):
        if path.name.startswith("_"):
            continue
        assert '{% extends "platform/docs/_layout.html" %}' in path.read_text(encoding="utf-8"), path.name
    layout = (DOCS / "_layout.html").read_text(encoding="utf-8")
    assert 'name="robots" content="noindex, nofollow"' in layout


def test_docs_are_for_signed_in_admins_with_access_only():
    assert "def docs_required" in SITE
    body = SITE[SITE.index("def docs_required"):SITE.index("def _neighbours")]
    assert "@platform_host_only" in body and "@platform_required" in body
    assert "docs_access" in body and "abort(404)" in body
    # every /docs route is behind it
    for route in re.finditer(r"@app\.route\('(/docs[^']*)'\)\n(@[a-z_]+)", SITE):
        assert route.group(2) == "@docs_required", f"{route.group(1)} is not behind docs_required"
    assert len(re.findall(r"@app\.route\('/docs", SITE)) == 3


def test_marketing_is_public_but_only_on_platform_hosts():
    block = SITE[SITE.index("@app.route('/marketing')"):]
    assert "@platform_host_only" in block[:120] and "platform_required" not in block[:200]


def test_a_school_address_serves_neither_marketing_nor_docs():
    # the resolver lets these paths through on a platform host only; on a school's address the
    # decorator above answers 404, and the resolver's own school branch never serves them
    assert "PLATFORM_SITE_PATHS = ('/marketing', '/docs', '/privacy')" in RESOLVER
    platform_branch = RESOLVER[RESOLVER.index("if host in config.platform_hosts():"):RESOLVER.index("tenant = tenant_for_host(host)")]
    assert "PLATFORM_SITE_PATHS" in platform_branch
    marketing = SITE[SITE.index("@app.route('/marketing')"):]
    assert "@platform_host_only" in marketing[:200]
    docs = SITE[SITE.index("@app.route('/docs')\n"):]
    assert "@docs_required" in docs[:80]  # docs_required itself wraps platform_host_only + platform_required


def test_privacy_is_reachable_everywhere_signed_in_or_not():
    # unlike marketing and docs, /privacy carries no host guard at all: it must render on a
    # school's own address too, so it cannot depend on g.on_platform_host or a signed-in admin.
    block = SITE[SITE.index("@app.route('/privacy')"):]
    header = block[:block.index("def privacy")]
    for guard in ("@platform_host_only", "@platform_required", "@docs_required", "@admin_required"):
        assert guard not in header, guard


def test_only_the_super_admin_can_change_who_reads_the_documentation():
    block = CONSOLE[CONSOLE.index("@app.post('/platform/team/<int:admin_id>/docs-access')"):]
    header = block[:block.index("def platform_team_docs_access")]
    for guard in ("@platform_host_only", "@platform_required", "@superadmin_required", "@csrf_protect"):
        assert guard in header, guard
    assert header.index("@platform_host_only") < header.index("@platform_required") < header.index("@superadmin_required") < header.index("@csrf_protect")
    team = (ROOT / "control_plane" / "team.py").read_text(encoding="utf-8")
    grant = team[team.index("def set_docs_access"):team.index("def reset_password")]
    assert "audit(session, 'platform_admin.docs_access'" in grant and "ROLE_SUPER" in grant


def test_the_marketing_page_does_not_name_any_school_or_promise_numbers():
    text = (ROOT / "templates" / "marketing.html").read_text(encoding="utf-8")
    assert not re.search(r"creative rainbow|crainbow|montessori", text, re.I)
    # no invented statistics or testimonials: every figure on the page is a fact about the product
    assert "testimonial" not in text.lower()


def test_the_privacy_page_names_no_real_school_and_marks_its_placeholders():
    text = (ROOT / "templates" / "privacy.html").read_text(encoding="utf-8")
    assert not re.search(r"creative rainbow|crainbow|montessori", text, re.I)
    # legal details Claude cannot know (registration number, registered address, DPO) are left as
    # visible placeholders, never invented as if they were real
    assert "[" in text and "]" in text
    assert not re.search(r"\bRC\d{4,}\b|\bNITDA/[A-Z0-9-]+\b", text)


if __name__ == "__main__":  # pragma: no cover
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
    sys.exit(0)
