"""Contract: the four unauthenticated sign-in-surface forms all carry and check a CSRF token.

/login, /forgot-password and /reset-password/<token> used to be the only state-changing POST
routes in the whole application with no CSRF check at all - a gap against the "login CSRF" class
of attack (a forged cross-site form can sign a visitor's browser into an attacker's account, or
spend their password-recovery attempts, without their knowledge). Every other POST route in the
application is already covered by tests/current/test_security_contract.py's endpoint-permission
guard; this is the narrower, sign-in-specific half of the same discipline. It reads source only,
and needs no database - the behavioural proof (a real POST refused, a real one accepted) is
tests/verification/write_paths_known_gaps.py and write_paths_rate_limits.py.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AUTH_ROUTES = (ROOT / "blueprints" / "auth" / "routes.py").read_text(encoding="utf-8")
TEMPLATES = ROOT / "templates"


def _function_body(name):
    match = re.search(rf"^def {name}\(.*?(?=\n(?:def |@app\.route))", AUTH_ROUTES, re.S | re.M)
    assert match, f"blueprints/auth/routes.py lost its {name}() view"
    return match.group(0)


def test_login_checks_csrf_before_touching_the_password():
    body = _function_body("login")
    assert "if not csrf_check_request():" in body
    # The check must come before the account lookup, not after: a forged request must never
    # reach _authenticate_unified at all.
    assert body.index("csrf_check_request") < body.index("_authenticate_unified")


def test_forgot_password_checks_csrf_before_spending_a_recovery_attempt():
    body = _function_body("forgot_password")
    assert "if not csrf_check_request():" in body
    assert body.index("csrf_check_request") < body.index("_rate_limit(")


def test_password_reset_checks_csrf_before_changing_the_password():
    body = _function_body("password_reset")
    assert "if not csrf_check_request():" in body
    assert body.index("csrf_check_request") < body.index("generate_password_hash")


def test_the_three_forms_carry_the_token_they_are_checked_against():
    for name in ("login.html", "forgot_password.html", "password_reset.html"):
        source = (TEMPLATES / name).read_text(encoding="utf-8")
        assert 'name="_csrf_token"' in source and "{{csrf_token()}}" in source, (
            f"templates/{name} posts without carrying a CSRF token")
