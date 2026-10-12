"""Contract: the fixes from the security audit stay fixed. Reads source and calls small functions; needs no database.

* a real deployment that is not running as production refuses to start (app.py, refuse_unsafe_environment);
* new passwords that are too short, common, repeated, a straight run or the username are refused (core/passwords.py);
* a failure shown to a person never carries the exception's own text (core/failures.py);
* a school's name is never written raw inside a script, where it could end a comment or a string;
* the result-image renderer does not let Chrome read other files on the server;
* a student's account page is refused to anyone who does not see the whole school's finance.
"""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _guard():
    """refuse_unsafe_environment and production_signs, taken from app.py without importing the application."""
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {"production_signs", "refuse_unsafe_environment", "_DEV_DOMAIN_ENDINGS"}
    nodes = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name in wanted)
             or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") in wanted for t in n.targets))]
    namespace = {"os": __import__("os"), "sys": __import__("sys")}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "app.py", "exec"), namespace)
    return namespace["refuse_unsafe_environment"]


def test_a_real_deployment_must_run_as_production():
    refuse = _guard()
    developer = {"BRIGHTSTARS_PORTAL_DOMAIN": "localhost", "BRIGHTSTARS_STORAGE_BACKEND": "s3"}
    refuse("development", developer, {})                                  # a developer's machine, even on S3
    refuse("development", {"BRIGHTSTARS_PORTAL_DOMAIN": "portal.test"}, {})
    for environ, modules in (({"BRIGHTSTARS_PORTAL_DOMAIN": "myschools.ng"}, {}),
                             ({}, {"gunicorn": object()}),
                             ({"SERVER_SOFTWARE": "waitress"}, {}),
                             ({"BRIGHTSTARS_TRUSTED_PROXIES": "1"}, {})):
        try:
            refuse("development", environ, modules)
        except RuntimeError as exc:
            assert "BRIGHTSTARS_ENV=production" in str(exc)
        else:
            raise AssertionError(f"a real deployment ({environ}, {list(modules)}) was allowed to start as development")
        refuse("production", environ, modules)                           # as production it starts
    refuse("development", {"BRIGHTSTARS_PORTAL_DOMAIN": "myschools.ng", "BRIGHTSTARS_ALLOW_NON_PRODUCTION": "1"}, {})


def test_weak_new_passwords_are_refused():
    from core.passwords import OPERATOR_MINIMUM, password_problem

    for bad in ("short1", "password", "Password1!", "12345678", "aaaaaaaa", "abcdefgh", "school123", "qwertyuiop"):
        assert password_problem(bad), bad
    assert password_problem("ada.okafor-2026", usernames=("ada.okafor",))
    assert password_problem("Fixture-password-9") is None
    assert password_problem("blue-mango-river-7") is None
    assert password_problem("nine-char", minimum=OPERATOR_MINIMUM)
    assert password_problem("a-long-platform-password", minimum=OPERATOR_MINIMUM) is None


def test_failures_are_told_plainly():
    import socket
    import smtplib
    from core.failures import plain_failure

    secret = "INSERT INTO students VALUES ('Ada', 'secret-value') at 10.0.0.5:5432"
    assert "secret-value" not in plain_failure(RuntimeError(secret))
    assert "10.0.0.5" not in plain_failure(OSError(secret))
    assert "username or password" in plain_failure(smtplib.SMTPAuthenticationError(535, b"no"))
    assert "in time" in plain_failure(socket.timeout())
    assert plain_failure(ValueError("That mail server address is not allowed.")) == "That mail server address is not allowed."


def test_the_school_name_is_never_raw_inside_a_script():
    offenders = []
    for path in sorted((ROOT / "templates").rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for block in re.findall(r"<script[^>]*>(.*?)</script>", text, re.S):
            for expr in re.findall(r"\{\{(.*?)\}\}", block):
                if "school_brand.name" in expr and "tojson" not in expr:
                    offenders.append(f"{path.relative_to(ROOT)}: {{{{{expr}}}}}")
    assert not offenders, "use |tojson for a name inside a script:\n  " + "\n  ".join(offenders)


def test_the_result_renderer_cannot_read_server_files():
    source = (ROOT / "core" / "entrance.py").read_text(encoding="utf-8")
    assert '"--allow-file-access-from-files"' not in source


def test_a_students_fee_account_needs_the_whole_schools_finance():
    source = (ROOT / "blueprints" / "finance" / "routes.py").read_text(encoding="utf-8")
    body = source[source.index("def admin_finance_student_account("):]
    body = body[:body.index("\n@app.")]
    assert "_finance_can_view_all(me)" in body and "admin_access_error('finance.view_all')" in body


def test_an_import_file_is_read_defensively():
    from core.tabular import ImportFileError, read_table

    names, rows = read_table("ok.csv", b"first_name,last_name\r\nAda,Obi\r\n", 10)
    assert names == ["first_name", "last_name"] and [dict(r) for r in rows] == [{"first_name": "Ada", "last_name": "Obi"}]
    for filename, raw in (("macros.xlsm", b"PK\x03\x04"), ("fake.xlsx", b"first_name\r\nA\r\n"), ("old.xls", b"\xd0\xcf")):
        try:
            read_table(filename, raw, 10)
        except ImportFileError:
            continue
        raise AssertionError(f"{filename} was accepted")
