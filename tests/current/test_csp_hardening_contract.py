"""Contract: no inline event-handler attribute, and no un-nonced inline <script>, anywhere.

script-src in the Content-Security-Policy header (app.py's apply_security_headers) trusts only
'self' and this one response's own nonce - never 'unsafe-inline' - so an inline onclick/onchange/
onsubmit attribute, or an inline <script> with no matching nonce, would simply not run in a real
browser. static/interactions.js is where every one of those attributes was moved to instead (as a
data-* attribute a delegated listener reads), and every inline <script> left is one that render its
own nonce via {{ csp_nonce() }}.

This reads source only, and needs no database; the actual behaviour of interactions.js (a real
browser, real clicks) is tests/verification/write_paths_resilience.py.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = list((ROOT / "templates").rglob("*.html"))
APP = (ROOT / "app.py").read_text(encoding="utf-8")

# Matches a real HTML tag carrying an inline event-handler attribute - not, say, the word
# "download" or a JS property assignment (`el.onclick = fn`) inside an already-nonced <script>.
INLINE_HANDLER = re.compile(
    r"<[a-zA-Z][^>]*\son(click|change|submit|load|input|keyup|keydown|focus|blur)=", re.S)


def test_no_template_has_an_inline_event_handler_attribute():
    offenders = []
    for path in TEMPLATES:
        text = path.read_text(encoding="utf-8")
        if INLINE_HANDLER.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"these templates still have an inline onclick/onchange/onsubmit attribute: {offenders}"


def test_every_inline_script_carries_the_nonce():
    offenders = []
    for path in TEMPLATES:
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"<script(\s[^>]*)?>", text):
            tag = m.group(0)
            if "src=" in tag:
                continue  # an external file; CSP's 'self' already covers it, no nonce needed
            if "nonce=" not in tag:
                offenders.append(f"{path.relative_to(ROOT)}: {tag}")
    assert not offenders, f"these inline <script> tags have no nonce, so a real browser would refuse to run them: {offenders}"


def _csp_header_line():
    start = APP.index("response.headers.setdefault('Content-Security-Policy'")
    return APP[start:APP.index("\n", start)]


def test_script_src_no_longer_allows_unsafe_inline():
    header_line = _csp_header_line()
    assert "script-src" in header_line
    script_src = header_line.split("script-src", 1)[1].split(";", 1)[0]
    assert "unsafe-inline" not in script_src, "script-src still allows 'unsafe-inline'"
    assert "nonce-" in script_src, "script-src has no nonce source"


def test_style_src_keeps_unsafe_inline_deliberately():
    """Only script-src was tightened - every page's colours are a school's own choice rendered
    into a <style> block (core/theme.py), already restricted to #rrggbb before they ever reach
    one, where a nonce buys nothing a value check does not already give."""
    header_line = _csp_header_line()
    style_src = header_line.split("style-src", 1)[1].split(";", 1)[0]
    assert "unsafe-inline" in style_src


def test_csp_nonce_is_a_template_global():
    assert "'csp_nonce': csp_nonce" in APP


def test_interactions_js_is_loaded_everywhere_a_migrated_attribute_is_used():
    """Every template using one of the data-* attributes interactions.js reads must actually load
    it - directly, or by extending a base template that does."""
    interactions = (ROOT / "static" / "interactions.js").read_text(encoding="utf-8")
    assert interactions, "static/interactions.js is empty"
    bases_with_it = set()
    for base in ("admin_base.html", "auth_base.html", "parent_base.html", "student_base.html", "platform/base.html", "platform/docs/_layout.html"):
        text = (ROOT / "templates" / base).read_text(encoding="utf-8")
        if "interactions.js" in text:
            bases_with_it.add(base)
    assert bases_with_it == {"admin_base.html", "auth_base.html", "parent_base.html", "student_base.html", "platform/base.html", "platform/docs/_layout.html"}, (
        f"a base template lost its interactions.js include: {bases_with_it}")

    # Fragments have no <html> of their own and are {% include %}-d into a page that already
    # extends one of the bases above (checked once, here, rather than at every include site).
    FRAGMENTS = {"_signature_editor.html", "includes/_chat_attachment_input.html", "_finance_assessment_history.html"}

    data_attrs = re.compile(r'data-(confirm|autosubmit|modal-open|modal-close|print|reload|go-back|call|toggle-class|onchange|show-when)[=\s>]')
    for path in TEMPLATES:
        rel = str(path.relative_to(ROOT / "templates")).replace("\\", "/")
        if rel in FRAGMENTS:
            continue
        text = path.read_text(encoding="utf-8")
        if not data_attrs.search(text):
            continue
        if "interactions.js" in text:
            continue
        extends = re.search(r"\{%-?\s*extends\s+['\"]([^'\"]+)['\"]", text)
        assert extends and extends.group(1) in bases_with_it, (
            f"{path.relative_to(ROOT)} uses a data-* attribute interactions.js reads, but neither "
            f"it nor its base template loads interactions.js")
