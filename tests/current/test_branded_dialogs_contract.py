"""Contract: questions and notices are shown in the school's own dialog, never the browser's.

static/interactions.js provides window.brandDialog (confirm, alert, confirmSubmit) in the school's colours, and asks
every data-confirm with it. A bare confirm() or alert() in a script or template would bring the browser's grey box
back, so none may remain; the one allowed fallback is inside interactions.js itself, for a browser with no <dialog>.
A page that asks or tells something must also load interactions.js. It reads source only, and needs no database.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NATIVE = re.compile(r"(?<![\w.])(?:window\.)?(confirm|alert)\s*\(")
# window.alert.bind(window) is the fallback for a page that might load without interactions.js.
ALLOWED_FALLBACK = "window.alert.bind(window)"


def _sources():
    for path in sorted((ROOT / "static").glob("*.js")):
        yield path
    for path in sorted((ROOT / "templates").rglob("*.html")):
        if "platform/docs" not in path.as_posix():
            yield path


def test_no_browser_confirm_or_alert_remains():
    found = []
    for path in _sources():
        if path.name == "interactions.js":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if NATIVE.search(line.replace(ALLOWED_FALLBACK, "")):
                found.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()[:100]}")
    assert not found, "use window.brandDialog instead of the browser's own dialog:\n" + "\n".join(found)


def test_interactions_asks_data_confirm_with_the_branded_dialog():
    source = (ROOT / "static" / "interactions.js").read_text(encoding="utf-8")
    assert "window.brandDialog" in source
    assert "var(--navy" in source and "var(--blue" in source, "the dialog must use the school's colours"
    # data-confirm is asked in the capture phase, before any other handler sees the press or the submit.
    assert re.search(r'addEventListener\("click",[\s\S]*?\}, true\);', source)
    assert re.search(r'addEventListener\("submit",[\s\S]*?\}, true\);', source)


def test_pages_that_ask_load_the_dialog():
    for path in (ROOT / "templates").rglob("*.html"):
        source = path.read_text(encoding="utf-8")
        if "brandDialog.confirmSubmit" in source and "{% extends" not in source:
            assert "interactions.js" in source, f"{path.relative_to(ROOT)} asks with brandDialog but does not load it"
