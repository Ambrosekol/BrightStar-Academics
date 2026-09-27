"""Contract: a template's own house rule (templates/platform/docs/contributing.html: "Never use
|safe on anything that came from a person") is actually followed everywhere, not just written down.

|safe turns off Jinja's autoescaping for one expression, so anything already loaded with |e (or
|escape) first is fine - the markup it then re-adds (a literal <br>, say) is the template's own,
not the person's. An |safe with no |e/|escape earlier in the same expression renders a person's
own text as live HTML: a project's or an assignment's "instructions", typed by a member of staff,
would then run as a script in the browser of every student who opens it. This reads source only,
and needs no database.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"

# {{ <expression> |safe }} or {{<expression>|safe}} - Jinja allows a space either side of |.
SAFE_EXPR = re.compile(r"\{\{\s*([^{}]*?)\s*\|\s*safe\s*\}\}")


def test_every_safe_filter_is_preceded_by_escaping_in_the_same_expression():
    offenders = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        source = path.read_text(encoding="utf-8")
        for expr in SAFE_EXPR.findall(source):
            if not re.search(r"\|\s*(e|escape)\s*(\(|\||$)", expr):
                offenders.append(f"{path.relative_to(ROOT)}: {{{{ {expr} | safe }}}}")
    assert not offenders, "unescaped |safe on what may be a person's own text:\n" + "\n".join(offenders)


def test_the_house_rule_is_still_written_down():
    contributing = (TEMPLATES / "platform" / "docs" / "contributing.html").read_text(encoding="utf-8")
    assert "Never use <code>|safe</code> on anything that came from a person" in contributing
