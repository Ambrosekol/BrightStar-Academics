"""Contract: the scripts that send forms read a form's method, action and target from its attributes, never from
form.method, form.action or form.target.

A form's own fields shadow those properties: in the Store's Record a payment form the radio buttons are named
"method" (Cash, Bank transfer, POS), so form.method was the list of radio buttons, not "post". static/inplace.js
then failed on .toLowerCase(), the form was never sent, and the button spun for ever. form.getAttribute('method')
is always the form's own. This reads source only, and needs no database.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHADOWED = re.compile(r"\b(?:form|f|el)\.(method|action|target|elements\.method)\b(?!\s*\()")


def test_form_scripts_read_attributes_not_shadowable_properties():
    offenders = []
    for path in sorted((ROOT / "static").glob("*.js")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip().startswith("//"):
                continue
            for match in SHADOWED.finditer(line):
                offenders.append(f"static/{path.name}:{number}: form.{match.group(1)}")
    assert not offenders, ("read the attribute instead (a field of that name hides the property):\n  "
                           + "\n  ".join(offenders))
