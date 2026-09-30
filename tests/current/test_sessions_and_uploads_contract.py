"""Sign-in lifetime, response headers, proxy trust and uploaded-file access: the fast guards.

The behavioural proof (real databases, cookies, restarts) is
tests/verification/write_paths_session_guard.py and write_paths_upload_access.py.
"""
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

APP = (ROOT / "app.py").read_text(encoding="utf-8")
GUARD = (ROOT / "core" / "session_guard.py").read_text(encoding="utf-8")
LAUNCH = (ROOT / "control_plane" / "launch.py").read_text(encoding="utf-8")
CONSOLE = (ROOT / "control_plane" / "console.py").read_text(encoding="utf-8")
RESOLVER = (ROOT / "control_plane" / "resolver.py").read_text(encoding="utf-8")
AUTH = (ROOT / "blueprints" / "auth" / "routes.py").read_text(encoding="utf-8")
ACCESS = (ROOT / "core" / "upload_access.py").read_text(encoding="utf-8")


def _code_files():
    for folder in ("blueprints", "core", "control_plane", "services", "models"):
        yield from (ROOT / folder).rglob("*.py")
    yield ROOT / "app.py"


def test_the_session_guard_runs_right_after_the_school_resolver_and_before_every_other_hook():
    resolver = APP.index("_install_multitenancy(app)")
    guard = APP.index("_install_session_guard(app)")
    first_hook = APP.index("@app.before_request")
    assert resolver < guard < first_hook
    assert "app.before_request(guard_session)" in GUARD


def test_the_guard_skips_health_and_static_files():
    hook = GUARD[GUARD.index("def guard_session"):]
    assert "path == '/health' or path.startswith('/static/')" in hook
    assert hook.index("path == '/health'") < hook.index("assess()")


def test_a_launch_id_is_written_once_at_start_in_the_registry_and_only_read_per_request():
    assert "class PlatformState" in (ROOT / "control_plane" / "models.py").read_text(encoding="utf-8")
    assert "def record_new_launch" in LAUNCH and "def current_launch_id" in LAUNCH
    assert "secrets.token_hex" in LAUNCH
    # Written by `python app.py` after the schools are upgraded and before it serves, by the
    # deploy commands, and by the platform Settings page's own gated Terminal actions (an
    # authorised, highly-trusted admin's deliberate equivalent of running the CLI command,
    # audited the same way every other console action is) - never by an ordinary request.
    main = APP[APP.index("if __name__=='__main__':"):]
    assert main.index("upgrade_all_tenants()") < main.index("record_new_launch()") < main.index("app.run(")
    cli = (ROOT / "control_plane" / "cli.py").read_text(encoding="utf-8")
    assert "record_new_launch" in cli and "'new-launch'" in cli
    settings_console = (ROOT / "control_plane" / "settings_console.py").read_text(encoding="utf-8")
    assert "record_new_launch" in settings_console and "@settings_required" in settings_console
    for path in _code_files():
        if path.name in ("launch.py", "cli.py", "settings_console.py") or path == ROOT / "app.py":
            continue
        assert "record_new_launch" not in path.read_text(encoding="utf-8"), path.name


def test_an_unreadable_registry_or_school_never_signs_anyone_out():
    # The registry: the last value seen (or "unknown") is used, and unknown means nobody is signed out.
    read = LAUNCH[LAUNCH.index("def current_launch_id"):]
    assert "except Exception" in read and "return _value" in read
    assert "stale = launch is not None and" in GUARD
    # An exam check that cannot be made counts as "in an exam".
    exam = GUARD[GUARD.index("def sitting_exam"):GUARD.index("# ------", GUARD.index("def sitting_exam"))]
    assert "except Exception:" in exam and exam.rstrip().endswith("return True")


def test_exam_sitters_are_kept_by_what_the_database_says_is_in_progress():
    exam = GUARD[GUARD.index("def sitting_exam"):]
    for model in ("Attempt", "SchoolAssessmentAttempt", "SchoolAssignmentAttempt"):
        assert model in exam
    assert "status == 'active'" in exam and "expires_at >" in exam


def test_every_sign_in_is_stamped_and_every_change_of_own_password_refreshes_the_stamp():
    assert "stamp_sign_in(kind,account)" in AUTH
    assert re.search(r"_clear_identity_sessions\(\)\s*\n\s*stamp_sign_in", AUTH)
    assert "stamp_platform_sign_in(admin['id'])" in CONSOLE
    assert "stamp_session(fingerprint='operator')" in CONSOLE  # "Enter school"
    # Each place a signed-in person changes their own password stays signed in there.
    for path, marker in (("app.py", "def admin_password_change"),
                         ("blueprints/student_portal/routes.py", "def student_password_change"),
                         ("blueprints/parents/routes.py", "def parent_password_change"),
                         ("control_plane/console.py", "def platform_password")):
        text = (ROOT / path).read_text(encoding="utf-8")
        body = text[text.index(marker):]
        body = body[:body.index("\n@app.")] if "\n@app." in body else body
        assert "refresh_password_stamp()" in body, path


def test_the_password_marker_is_judged_from_the_stored_hash_so_no_route_can_forget_it():
    assert "password_fingerprint" in GUARD and "hashlib.sha256" in GUARD
    for column in ("Admin.password_hash", "Student.login_password_hash", "ParentAccount.password_hash",
                   "Candidate.password_hash", "PlatformAdmin.password_hash"):
        assert column in GUARD, column
    # the reserved operator account's hash changes on every entry, so it is never the marker
    assert "OPERATOR_PREFIX = 'platform@'" in GUARD


def test_a_login_post_is_never_turned_away_by_its_own_wiped_csrf_token():
    """A stale identity/password stamp must not clear the session (and with it the CSRF token a
    login POST is checked against) before the code even asks whether this request is itself a
    sign-in submission — otherwise a correct sign-in fails with a confusing "invalid token" 403."""
    body = GUARD[GUARD.index("def sign_out"):GUARD.index("def take_sign_out_reason")]
    guard_check = body.index("SIGN_IN_ENDPOINTS")
    clear_call = body.index("session.clear()")
    assert guard_check < clear_call, "the sign-in-endpoint check must run before the session is cleared"
    assert "request.method in ('POST', 'PUT', 'PATCH', 'DELETE')" in body[:clear_call]


def test_the_session_guard_and_the_console_agree_on_the_platform_session_key():
    match = re.search(r"^SESSION_KEY = '([a-z_]+)'", CONSOLE, re.M)
    assert match and f"PLATFORM_KEY = '{match.group(1)}'" in GUARD


def test_the_stamps_survive_a_platform_host_discarding_a_stale_school_sign_in():
    body = RESOLVER[RESOLVER.index("def _discard_school_identity"):RESOLVER.index("def resolve_tenant")]
    assert "'launch'" in body and "'pwv'" in body
    assert "'pwv'" in (ROOT / "core" / "accounts.py").read_text(encoding="utf-8")  # signing out drops it


def test_every_response_names_a_referrer_policy_and_reset_pages_are_never_cached():
    headers = APP[APP.index("def apply_security_headers"):APP.index("@app.after_request", APP.index("def apply_security_headers"))]
    assert "response.headers.setdefault('Referrer-Policy','same-origin')" in headers
    assert "SECRET_URL_ENDPOINTS" in headers and "no-store" in headers and "'no-referrer'" in headers
    assert "'password_reset'" in APP[APP.index("SECRET_URL_ENDPOINTS="):APP.index("@app.after_request", APP.index("SECRET_URL_ENDPOINTS="))]
    # Why it is "same-origin" and not "no-referrer" everywhere: these read the Referer of our own pages.
    readers = "".join((ROOT / p).read_text(encoding="utf-8") for p in
                      ("blueprints/administration/routes.py", "core/request_errors.py"))
    assert "request.referrer" in readers and "'Referer'" in readers


def test_no_code_believes_a_client_written_forwarding_header():
    pattern = re.compile(r"""headers(?:\.get\(|\[)\s*['"]X-Forwarded""", re.I)
    for path in _code_files():
        assert not pattern.search(path.read_text(encoding="utf-8", errors="ignore")), path
    assert "X-Forwarded" not in (ROOT / "core" / "security.py").read_text(encoding="utf-8")
    assert "(request.remote_addr or '')[:64]" in (ROOT / "core" / "security.py").read_text(encoding="utf-8")


def test_trusted_proxies_are_off_by_default_and_only_the_client_address_and_scheme_are_believed():
    from control_plane import config

    saved = os.environ.pop("BRIGHTSTARS_TRUSTED_PROXIES", None)
    try:
        assert config.trusted_proxies() == 0
        os.environ["BRIGHTSTARS_TRUSTED_PROXIES"] = "2"
        assert config.trusted_proxies() == 2
        for bad in ("abc", "-1", "1.5", "50"):
            os.environ["BRIGHTSTARS_TRUSTED_PROXIES"] = bad
            try:
                config.trusted_proxies()
            except RuntimeError as exc:
                assert "BRIGHTSTARS_TRUSTED_PROXIES" in str(exc)
            else:
                raise AssertionError(f"{bad!r} was accepted")
    finally:
        os.environ.pop("BRIGHTSTARS_TRUSTED_PROXIES", None)
        if saved is not None:
            os.environ["BRIGHTSTARS_TRUSTED_PROXIES"] = saved
    # The Host header chooses the school, so it and the prefix are never taken from a proxy.
    assert "x_for=count,x_proto=count,x_host=0,x_prefix=0,x_port=0" in APP
    assert "apply_trusted_proxies(app,trusted_proxies())" in APP
    for setting in (ROOT / "README.md", ROOT / ".env.example"):
        assert "BRIGHTSTARS_TRUSTED_PROXIES" in setting.read_text(encoding="utf-8")


def test_every_folder_the_application_writes_uploads_to_has_a_rule_for_who_may_open_it():
    from core.upload_access import DEFAULT_RULE, FOLDER_RULES, NEVER, PUBLIC

    written = set()
    for path in _code_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        written.update(re.findall(r"_save_image_upload\([^()]*?(?:\([^()]*\)[^()]*?)*,\s*'([a-z_]+)'", text))
        written.update(re.findall(r"os\.path\.join\(uploads_dir\(\),\s*'([a-z_]+)'\)", text))
        # save_upload_bytes(subdir, filename, data, ...) (core/storage.py) is the backend-aware
        # entry point every upload goes through now, including a literal subdir like 'messages'.
        written.update(re.findall(r"save_upload_bytes\(\s*'([a-z_]+)'", text))
    assert written >= {"admins", "candidates", "students", "questions", "signatures", "assignments", "branding", "messages"}, written
    assert written <= set(FOLDER_RULES), f"no rule for: {sorted(written - set(FOLDER_RULES))}"
    assert FOLDER_RULES["branding"] == PUBLIC and FOLDER_RULES["messages"] == NEVER
    assert DEFAULT_RULE == frozenset({"admin"})            # a folder with no rule is for staff
    for folder in ("admins", "signatures"):
        assert FOLDER_RULES[folder] == frozenset({"admin"})
    assert "parent" not in "".join(FOLDER_RULES["questions"]) and "parent_of_student" in FOLDER_RULES["students"]


def test_the_uploads_route_asks_the_folder_rule_after_cleaning_the_path_and_refuses_with_a_plain_404():
    route = APP[APP.index("def uploaded_file"):APP.index("if __name__=='__main__'")]
    assert route.index("posixpath.normpath") < route.index("PRIVATE_UPLOAD_FOLDERS") < route.index("may_open_upload(folder,clean)")
    assert route.index("may_open_upload(folder,clean)") < route.index("send_from_directory(uploads_dir(),clean)")
    assert route.count("abort(404)") >= 3 and "abort(403)" not in route
    assert "signed_in_to_school" not in APP          # the blanket "any signed-in account" rule is gone


def test_file_access_ignores_a_sign_in_of_another_school_or_one_that_has_ended():
    who = ACCESS[ACCESS.index("def _who"):ACCESS.index("def _is_active")]
    assert "session.get('tenant_id') != tenant.id" in who and "usable_for_files()" in who
    # Read-only: a picture must never rewrite the cookie a page loading beside it is also writing.
    usable = GUARD[GUARD.index("def usable_for_files"):GUARD.index("# ------", GUARD.index("def usable_for_files"))]
    assert "session[" not in usable and "sign_out" not in usable and "flash(" not in usable
