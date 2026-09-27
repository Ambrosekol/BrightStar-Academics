"""Contract: a route guarded against being run twice actually carries the hidden field its own
guard is checked against - the two must always ship together, or the decorator silently protects
nothing (core/idempotency.py: a missing key just falls through, by design, so a route can add the
decorator and forget the template and never notice in testing that only sends the fields it
already knows about).

This reads source only, and needs no database - the behavioural proof (a resubmitted click, an
exception raised by an unknown key, a genuinely new click) is
tests/verification/write_paths_resilience.py.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"

# A route that only ever redirects (never renders a template of its own) has its form on
# whatever page links to it instead - named here so the check still knows where to look.
FORM_ELSEWHERE = {
    "parent.pay_online": "parent_child_finance.html",
}


def _decorated_routes():
    """{scope: (file, view_name)} for every @idempotent_write('scope') in the codebase."""
    found = {}
    for folder in ("blueprints",):
        for path in (ROOT / folder).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            for match in re.finditer(
                    r"@idempotent_write\(['\"]([\w.-]+)['\"]\)\s*\n(?:@\w+[^\n]*\n)*def (\w+)\(", source):
                found[match.group(1)] = (path.relative_to(ROOT), match.group(2))
    return found


def test_idempotency_key_is_a_template_global_like_csrf_token():
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "from core.idempotency import idempotency_key" in app_source
    assert "'idempotency_key': idempotency_key," in app_source


def test_at_least_the_payment_form_is_guarded():
    routes = _decorated_routes()
    assert "finance.record_payment" in routes, "the payment-recording route lost its idempotency guard"


def test_every_guarded_route_has_a_form_carrying_the_hidden_field():
    for scope, (path, view_name) in _decorated_routes().items():
        if scope in FORM_ELSEWHERE:
            template_names = {FORM_ELSEWHERE[scope]}
        else:
            source = path.read_text(encoding="utf-8") if path.is_absolute() else (ROOT / path).read_text(encoding="utf-8")
            # Find the template this view renders (render_template('name.html', ...)) and check
            # it, not just that *some* template somewhere has the field.
            func_match = re.search(rf"def {re.escape(view_name)}\(.*?(?=\ndef |\Z)", source, re.S)
            assert func_match, f"could not isolate {view_name} in {path}"
            template_names = set(re.findall(r"render_template\(\s*['\"]([\w./-]+\.html)['\"]", func_match.group(0)))
            assert template_names, (
                f"{path}:{view_name} (scope {scope!r}) renders no template by a literal name - "
                f"if its form lives on a different page, add it to FORM_ELSEWHERE above")
        for name in template_names:
            template_source = (TEMPLATES / name).read_text(encoding="utf-8")
            assert 'name="_idempotency_key"' in template_source and "idempotency_key()" in template_source, (
                f"templates/{name} (rendered by {view_name}, scope {scope!r}) has no idempotency field")
