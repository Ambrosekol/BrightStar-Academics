"""Multi-tenancy contract: the properties that must never quietly regress.

The behavioural proof (real databases, hostnames, cookies) is
tests/verification/write_paths_multitenancy.py; these are the fast guards.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

APP = (ROOT / "app.py").read_text(encoding="utf-8")
ROUTING = (ROOT / "control_plane" / "routing.py").read_text(encoding="utf-8")
RESOLVER = (ROOT / "control_plane" / "resolver.py").read_text(encoding="utf-8")
BASE = (ROOT / "models" / "base.py").read_text(encoding="utf-8")


def test_multitenancy_cannot_be_switched_off():
    """There is no flag: one school per database, chosen per request, is the
    architecture. A toggle would mean a second, untested code path."""
    import control_plane.config as config
    for name in ("multitenancy_enabled", "BRIGHTSTARS_MULTITENANT"):
        assert not hasattr(config, name)
    for rel in ("app.py", "core/storage.py", "control_plane/routing.py",
                "control_plane/resolver.py", "core/branding.py"):
        assert "BRIGHTSTARS_MULTITENANT" not in (ROOT / rel).read_text(encoding="utf-8")


def test_the_platform_registry_url_must_be_configured():
    """A default would silently create an empty registry and make every school
    look as though it did not exist."""
    import os
    import control_plane.config as config
    saved = os.environ.pop("BRIGHTSTARS_PLATFORM_DB", None)
    try:
        try:
            config.platform_db_url()
        except RuntimeError:
            pass
        else:
            raise AssertionError("platform_db_url() must refuse to guess")
    finally:
        if saved is not None:
            os.environ["BRIGHTSTARS_PLATFORM_DB"] = saved


def test_school_resolver_is_installed_before_any_other_request_hook():
    """Every other before_request hook queries the database and needs the school
    selected first, so installing later would run them with no school."""
    install = APP.index("_install_multitenancy(app)")
    first_hook = APP.index("@app.before_request")
    assert install < first_hook


def test_session_class_routes_to_the_school_and_never_falls_back():
    assert "TenantSession" in BASE and "session_options" in BASE
    branch = ROUTING[ROUTING.index("class TenantSession"):ROUTING.index("def current_engine")]
    assert "engine_for(current_tenant())" in branch
    # No school selected must raise (current_tenant() default), not use a default database.
    from control_plane.context import current_tenant, NoTenantError
    try:
        current_tenant()
    except NoTenantError:
        pass
    else:
        raise AssertionError("current_tenant() must raise when no school is selected")


def test_startup_helpers_use_the_current_schools_engine_not_the_default_one():
    start = APP.index("def _add_missing_columns")
    end = APP.index("def _seed_school_tenant")
    assert "db.engine" not in APP[start:end]
    # create_all() names the default database; only a call statement matters, not docstrings.
    assert not re.search(r"^\s+db\.create_all\(\)\s*$", APP, re.M)


def test_sessions_are_bound_to_their_school():
    assert "session.get('tenant_id') != tenant.id" in RESOLVER
    assert "session.clear()" in RESOLVER


def test_school_is_chosen_from_the_host_header_only():
    fn = RESOLVER[RESOLVER.index("def resolve_tenant"):RESOLVER.index("def release_tenant")]
    assert "request.host" in fn
    for forbidden in ("request.args", "request.form", "request.cookies", "request.headers"):
        assert forbidden not in fn


def test_files_are_scoped_per_school():
    core_entrance = (ROOT / "core" / "entrance.py").read_text(encoding="utf-8")
    assert "data_dir()" in core_entrance and "from app import DATA" not in core_entrance
    uploads = (ROOT / "core" / "uploads.py").read_text(encoding="utf-8")
    # save_upload_bytes() (core/storage.py) is the per-school-scoping entry point every upload
    # goes through, under either storage backend (BRIGHTSTARS_STORAGE_BACKEND).
    assert "save_upload_bytes(" in uploads
    assert "@app.route('/static/uploads/<path:filename>')" in APP


def test_no_account_is_ever_seeded_into_a_school():
    """The platform's operators are the super admins; a school's first admin is
    created deliberately, so its one-time password reaches a named person."""
    assert "def init_admin_security():" in APP
    security = APP[APP.index("def init_admin_security():"):APP.index("def admin_role_names")]
    assert "generate_password_hash" not in security
    assert "BRIGHTSTARS_ADMIN_PASSWORD" not in APP and "ADMIN_PASSWORD" not in APP


def test_the_code_base_carries_no_school_name_of_its_own():
    """Crainbow is a tenant. A hard-coded school name anywhere shared would put
    one school's identity on every other school's pages and paperwork."""
    import re
    for folder in ("core", "blueprints", "models", "services", "control_plane", "templates"):
        for path in (ROOT / folder).rglob("*"):
            if path.suffix not in (".py", ".html") or "__pycache__" in str(path):
                continue
            assert not re.search(r"crainbow|creative rainbow", path.read_text(encoding="utf-8"), re.I), path
    assert not re.search(r"crainbow|creative rainbow", APP, re.I)
    # A school's short code is as much its identity as its name: "CRMS-" once prefixed every
    # school's candidate numbers.
    for folder in ("core", "blueprints", "models", "services", "templates"):
        for path in (ROOT / folder).rglob("*"):
            if path.suffix in (".py", ".html") and "__pycache__" not in str(path):
                assert not re.search(r"\bcrms\b", path.read_text(encoding="utf-8"), re.I), path


def test_sql_is_written_for_postgresql_not_just_sqlite():
    """PostgreSQL is the only supported database, so nothing may reach for a
    SQLite-only construct directly."""
    import re
    helpers = (ROOT / "core" / "db_helpers.py").read_text(encoding="utf-8")
    assert "def insert_stmt" in helpers and "def group_concat" in helpers
    for folder in ("core", "blueprints", "models", "control_plane"):
        for path in (ROOT / folder).rglob("*.py"):
            if "__pycache__" in str(path) or path.name == "db_helpers.py":
                continue
            body = path.read_text(encoding="utf-8")
            assert "dialects.sqlite" not in body, path
            assert "func.group_concat" not in body, path
    # Partial indexes must be declared for PostgreSQL as well as SQLite.
    for path in (ROOT / "models").rglob("*.py"):
        body = path.read_text(encoding="utf-8")
        assert body.count("sqlite_where") == body.count("postgresql_where"), path


def test_hostname_and_slug_validation():
    from control_plane.registry import normalise_host, validate_hostname, validate_slug
    assert normalise_host("School.Example.COM:8443") == "school.example.com"
    assert normalise_host("[::1]:5000") == "::1"
    assert normalise_host("host.example.") == "host.example"
    assert validate_hostname("Portal.School.ng") == "portal.school.ng"
    for bad in ("http://x.test/p", "x.test:80", "a b", "", "-x.test", "x..test", "x.test/p"):
        try:
            validate_hostname(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted bad hostname {bad!r}")
    assert validate_slug("crainbow") == "crainbow"
    for bad in ("Crainbow", "a_b", "../x", "", "-x", "x-", "a" * 41):
        try:
            validate_slug(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted bad slug {bad!r}")


def test_database_url_can_come_from_the_environment_and_schema_names_are_validated():
    import os
    from control_plane.routing import build_engine, resolve_db_url
    os.environ["BS_TEST_TENANT_URL"] = "sqlite:///x.db"
    try:
        assert resolve_db_url("env:BS_TEST_TENANT_URL") == "sqlite:///x.db"
    finally:
        del os.environ["BS_TEST_TENANT_URL"]
    try:
        resolve_db_url("env:BS_DEFINITELY_UNSET_VARIABLE")
    except RuntimeError:
        pass
    else:
        raise AssertionError("an unset env: URL must fail loudly")
    for bad in ('x"; DROP SCHEMA public; --', "Has Space", "1abc", ""):
        if not bad:
            continue
        try:
            build_engine("postgresql://u:p@h/db", bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted bad schema {bad!r}")


def test_upload_paths_cannot_escape_the_schools_folder():
    src = (ROOT / "core" / "storage.py").read_text(encoding="utf-8")
    assert "os.path.commonpath" in src and "realpath" in src
    # Windows-style separators in a stored value are normalised before the check.
    assert r"replace('\\', '/')" in src


def test_a_school_gets_a_portal_not_a_website():
    """A school's address serves the portal only, and so does the platform's: its
    marketing site is hosted elsewhere, so "/" there opens straight on the console."""
    fn = RESOLVER[RESOLVER.index("def resolve_tenant"):RESOLVER.index("def release_tenant")]
    assert "request.path.startswith('/school/')" in fn
    # "/" redirects into the portal instead of rendering a marketing homepage.
    index = APP[APP.index("def index():"):APP.index("@app.after_request")]
    assert "_portal_front_door()" in index
    assert "platform_login" in index and "platform_dashboard" in index
    assert not (ROOT / "control_plane" / "site.py").exists()
    assert not (ROOT / "templates" / "platform" / "site.html").exists()


def test_every_school_gets_an_issued_portal_hostname():
    from control_plane import config
    import os
    saved = os.environ.get('BRIGHTSTARS_PORTAL_DOMAIN')
    os.environ['BRIGHTSTARS_PORTAL_DOMAIN'] = 'Schools.Example.COM.'
    try:
        # Case and a trailing dot are normalised, so the generated hostname is
        # always what the registry lookup will actually match.
        assert config.portal_hostname('alpha') == 'alpha.schools.example.com'
    finally:
        os.environ.pop('BRIGHTSTARS_PORTAL_DOMAIN', None)
        if saved is not None:
            os.environ['BRIGHTSTARS_PORTAL_DOMAIN'] = saved
    provisioning = (ROOT / "control_plane" / "provisioning.py").read_text(encoding="utf-8")
    # The portal hostname is issued on creation and can never be removed.
    assert "config.portal_hostname(slug)" in provisioning
    assert "portal address and cannot be removed" in provisioning


def test_branding_is_captured_when_a_school_is_created():
    provisioning = (ROOT / "control_plane" / "provisioning.py").read_text(encoding="utf-8")
    assert "def apply_branding" in provisioning
    # Branding is applied as part of creating the school, not as a later step.
    create = provisioning[provisioning.index("def create_tenant"):provisioning.index("BRANDING_FIELDS = ")]
    assert "apply_branding(info," in create
    # A school always carries its own name, even created from the command line
    # with no branding at all, so it never shows another school's name.
    assert "branding['school_name'] = info.name" in provisioning
    console = (ROOT / "control_plane" / "console.py").read_text(encoding="utf-8")
    assert "branding=branding, logo=logo" in console
    form = (ROOT / "templates" / "platform" / "school_new.html").read_text(encoding="utf-8")
    assert 'enctype="multipart/form-data"' in form and 'name="logo"' in form
    for field in ('school_motto', 'school_tagline', 'school_phone', 'school_email', 'school_address'):
        assert f'name="{field}"' in form


def test_every_school_file_lives_in_one_folder():
    provisioning = (ROOT / "control_plane" / "provisioning.py").read_text(encoding="utf-8")
    assert "TENANT_SUBFOLDERS = ('data', 'uploads')" in provisioning
    assert "def tenant_folder" in provisioning and "def ensure_tenant_folders" in provisioning


def test_each_school_gets_its_own_postgresql_database():
    from control_plane import config
    assert config.school_db_name("st-marys") == "brightstars_st_marys"
    routing = (ROOT / "control_plane" / "routing.py").read_text(encoding="utf-8")
    assert "CREATE DATABASE" in routing and "AUTOCOMMIT" in routing


def test_postgresql_is_documented_as_the_only_database():
    doc = (ROOT / "docs" / "architecture" / "MULTI_TENANCY.md").read_text(encoding="utf-8")
    assert "PostgreSQL is the only supported database" in doc
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    # Local development needs PostgreSQL, so the requirement is stated up front.
    assert "PostgreSQL" in readme and "### Requirements" in readme
    assert "psycopg[binary]" in (ROOT / "requirements.txt").read_text(encoding="utf-8")


def test_a_brand_colour_is_only_ever_a_plain_hex_value():
    """A colour is written into a <style> block on every page of a school's
    portal, so anything else must be refused, not escaped."""
    from core import theme

    for hostile in ("red;}body{display:none", "#12345", "#gggggg", "url(x)", "#fff;}", "expression(1)"):
        try:
            theme.normalise_hex(hostile)
        except ValueError:
            continue
        raise AssertionError(f"{hostile!r} was accepted as a colour")
    assert theme.normalise_hex(" #ABC ") == "#aabbcc" and theme.normalise_hex("") == ""
    # Both colours sit behind white text, so a light one is refused.
    for light in ("#ffffff", "#ffee88", "#cccccc"):
        try:
            theme.check_colour(light, "main")
        except ValueError:
            continue
        raise AssertionError(f"{light} was accepted although white text is unreadable on it")
    assert theme.check_colour(theme.DEFAULT_PRIMARY, "main") == theme.DEFAULT_PRIMARY
    css = theme.theme_css("#7a1f3d", "#0b6e4f")
    assert re.fullmatch(r"[A-Za-z0-9#:;,.()\-{}! %]+", css), css
    assert theme.theme_css("", "") == ""


def test_the_portal_only_prints_colours_it_has_validated():
    branding = (ROOT / "core" / "branding.py").read_text(encoding="utf-8")
    assert "theme.check_colour(" in branding, "stored colours must be re-validated before use"
    # Only valid colours may reach a template: no raw setting is passed through.
    assert "'primary': primary" in branding and "'theme_css': theme.theme_css(primary, accent)" in branding


def test_the_gallery_only_serves_files_from_the_schools_own_branding_folder():
    from core import theme

    assert theme.parse_gallery('["uploads/branding/a.png"]') == ["uploads/branding/a.png"]
    for hostile in ('["uploads/../../etc/passwd"]', '["uploads/branding/../x.png"]', '["/etc/passwd"]',
                    '["static/x.png"]', '["uploads\\branding\\x.png"]', "not json", '{"a": 1}', "[1, null]"):
        assert theme.parse_gallery(hostile) == [], hostile


def test_the_brightstars_logo_belongs_to_the_platform_and_never_to_a_school():
    logo = "brand/brightstars-logo.png"
    assert (ROOT / "static" / "brand" / "brightstars-logo.png").is_file()
    for page in ("base.html", "login.html"):
        assert logo in (ROOT / "templates" / "platform" / page).read_text(encoding="utf-8"), page
    # A school's portal must look like the school's own, not like the platform's. The public
    # marketing page and the privacy statement are the platform's own, not any school's, so they
    # are allowed to carry the mark (the privacy statement renders identically wherever it is
    # opened, including from a school's own address, and is deliberately never school-branded).
    for template in (ROOT / "templates").glob("*.html"):
        if template.name in ("marketing.html", "privacy.html"):
            continue
        assert "brightstars-logo" not in template.read_text(encoding="utf-8", errors="ignore"), template.name


def test_creating_a_school_checks_its_images_and_colours_before_building_anything():
    provisioning = (ROOT / "control_plane" / "provisioning.py").read_text(encoding="utf-8")
    create = provisioning[provisioning.index("def create_tenant"):provisioning.index("BRANDING_FIELDS")]
    assert create.index("check_branding_inputs(") < create.index("init_platform_db()") < create.index("upgrade_tenant(info)")
    console = (ROOT / "control_plane" / "console.py").read_text(encoding="utf-8")
    # The form takes a logo and photographs, so it needs its own request limit,
    # and that limit must be raised before the CSRF check reads the form.
    for route in ("platform_school_new", "platform_school_branding"):
        head = console[:console.index(f"def {route}")]
        decorators = head[head.rindex("@app."):]
        assert decorators.index("@allow_branding_upload") < decorators.index("@csrf_protect"), route


def test_a_schools_own_branding_page_is_guarded_and_shares_the_platforms_rules():
    from core.security import ADMIN_ENDPOINT_PERMISSIONS, ADMIN_PERMISSION_DEFS

    assert "branding.manage" in {code for code, *_ in ADMIN_PERMISSION_DEFS}
    for endpoint in ("admin_school_branding", "admin_school_branding_save"):
        assert ADMIN_ENDPOINT_PERMISSIONS.get(endpoint) == "branding.manage", endpoint
    route = (ROOT / "blueprints" / "school" / "branding.py").read_text(encoding="utf-8")
    save = route[route.index("@app.post('/admin/school/branding/save')"):route.index("def admin_school_branding_save")]
    # The limit must be raised before anything reads the form, so it goes outermost.
    assert save.index("@_allow_branding_upload") < save.index("@admin_required") < save.index("@csrf_protect")
    # One implementation, used by both the console and the school's own admin area.
    provisioning = (ROOT / "control_plane" / "provisioning.py").read_text(encoding="utf-8")
    assert "branding_core.store_branding(" in provisioning and "check_branding_inputs" in route
    assert "SchoolPublicSetting(" not in provisioning, "the storage rules must not be written twice"


def test_only_the_super_admin_manages_the_team_and_reads_its_logs():
    console = (ROOT / "control_plane" / "console.py").read_text(encoding="utf-8")
    team = (ROOT / "control_plane" / "team.py").read_text(encoding="utf-8")
    for route in ("platform_team", "platform_team_new", "platform_team_remove",
                  "platform_team_restore", "platform_team_reset"):
        head = console[:console.index(f"def {route}(")]
        decorators = head[head.rindex("@app."):]
        assert "@platform_required" in decorators and "@superadmin_required" in decorators, route
        assert decorators.index("@platform_required") < decorators.index("@superadmin_required"), route
        if route != "platform_team":
            assert "@csrf_protect" in decorators, f"{route} changes the team and needs a form token"
    # The activity page is open to every admin, but must scope what it shows to the viewer.
    activity = console[console.index("def platform_activity("):console.index("def platform_team(")]
    assert "me['is_super']" in activity and "me['id']" in activity
    # The console can only ever create ordinary admins, and the super admin can never be removed.
    create = team[team.index("def create_admin"):team.index("def _target")]
    assert "role=ROLE_ADMIN" in create and "ROLE_SUPER" not in create
    remove = team[team.index("def remove_admin"):team.index("def revoke_school_access")]
    assert "ROLE_SUPER" in remove and "cannot be removed" in remove
    # Removing an admin keeps the account (so its log survives) and cuts access inside schools.
    assert "sa.delete(PlatformAdmin" not in team and "session.delete(" not in team
    assert "revoke_school_access(username)" in remove
    # "@" is the reserved namespace for operators' accounts inside schools.
    assert "@" not in team[team.index("USERNAME_RE"):team.index("USERNAME_RE") + 60]


def test_a_temporary_password_must_be_replaced_before_the_console_opens():
    console = (ROOT / "control_plane" / "console.py").read_text(encoding="utf-8")
    guard = console[console.index("def platform_required"):console.index("def superadmin_required")]
    assert "password_must_change" in guard and "platform_password" in guard and "platform_logout" in guard


def test_every_sign_in_outcome_on_a_real_account_is_logged():
    entry = (ROOT / "control_plane" / "entry.py").read_text(encoding="utf-8")
    auth = entry[entry.index("def authenticate_platform_admin"):entry.index("def platform_admin_by_id")]
    for action in ("platform_admin.login'", "platform_admin.login_failed", "platform_admin.login_refused"):
        assert action in auth, action
    # An unknown username costs as much time as a wrong password.
    assert "_DUMMY_HASH" in auth


def test_the_no_such_address_page_is_standalone_and_safe():
    resolver = RESOLVER
    notice = resolver[resolver.index("def _notice"):resolver.index("def _discard_school_identity")
                      if "_discard_school_identity" in resolver else resolver.index("def resolve_tenant")]
    # It must not go through render_template: context processors look up the signed-in
    # user in a school database, which does not exist on this kind of request.
    assert "render_template(" not in notice and "jinja_env.get_template" in notice
    assert "is_production()" in notice, "the development hint must never be shown in production"
    page = (ROOT / "templates" / "platform" / "notice.html").read_text(encoding="utf-8")
    assert "{% extends" not in page and "<script" not in page and 'rel="stylesheet"' not in page
    assert "|safe" not in page
    assert "/static/brand/" in resolver, "the logo must be servable on an address that belongs to no school"


def test_console_templates_never_put_user_text_inside_javascript():
    for path in (ROOT / "templates" / "platform").glob("*.html"):
        body = path.read_text(encoding="utf-8")
        assert "|safe" not in body, path.name
        assert "confirm('" not in body, f"{path.name}: pass text through a data attribute, not a JS string"


def test_reading_what_admins_did_inside_schools_is_read_only_and_bounded():
    team = (ROOT / "control_plane" / "team.py").read_text(encoding="utf-8")
    console = (ROOT / "control_plane" / "console.py").read_text(encoding="utf-8")
    reader = team[team.index("class _InsideSchools"):team.index("def activity(")]
    # It only ever reads a school's audit trail.
    for verb in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER"):
        assert verb not in reader.upper().replace("UPDATED", ""), verb
    # The account name is a bound value, never formatted into the SQL.
    assert ":who" in reader and "username_snapshot = :who" in reader
    assert "{self.username" not in reader.split("def _who")[1].split("def _count")[0].split("return")[0]
    # Only schools the admin actually entered are opened, not every school on the platform.
    assert "tenant.enter" in reader
    # Both sources are cut by the key the merged log is sorted on, or pages would repeat entries.
    assert "ORDER BY created_at DESC" in reader
    activity = team[team.index("def activity("):]
    assert "created_at.desc()" in activity
    # The dashboard's feed must not open school databases.
    assert "inside=False" in console[console.index("def platform_dashboard"):console.index("def platform_school_new")]

def _shared_code():
    """Every file of school-level code: not the platform (control_plane), not history."""
    for folder in ("blueprints", "core", "services", "models"):
        for path in (ROOT / folder).rglob("*.py"):
            if "__pycache__" not in str(path):
                yield path
    yield ROOT / "app.py"


def test_no_query_relies_on_a_sqlite_only_or_case_sensitive_construct():
    """PostgreSQL's LIKE is case-sensitive and it has no date('now'); a query written against
    SQLite's leniency fails, or quietly finds less, once a school is on PostgreSQL. These were
    all found in real pages. tests/verification/write_paths_pg_smoke.py opens every page to
    catch what a pattern cannot (such as a GROUP BY PostgreSQL rejects)."""
    banned = (".like(", ".notlike(", "func.date('now')", "func.datetime(", "func.strftime(", "julianday",
              "func.group_concat", "INSERT OR", "OR IGNORE")
    for path in _shared_code():
        body = path.read_text(encoding="utf-8")
        for pattern in banned:
            assert pattern not in body or path.name == "db_helpers.py", f"{path.name} uses {pattern}"


def test_the_top_level_role_is_found_by_its_flag_never_by_its_name():
    """A custom role that happens to be called "School Admin" must not be mistaken for the
    top-level role and be handed every permission."""
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    init = app[app.index("def init_admin_security"):app.index("def admin_role_names")]
    assert "super_role=one_scalar(select(AdminType.id).where(AdminType.is_system==1)" in init
    assert "AdminType.name==SCHOOL_ADMIN_ROLE))" not in init.split("_ignore_insert(AdminType")[1].split("db.session.flush()")[1]
    from core.security import SCHOOL_ADMIN_ROLE, LEGACY_TOP_ROLE_NAMES
    assert SCHOOL_ADMIN_ROLE == "School Admin" and "Super Admin" in LEGACY_TOP_ROLE_NAMES
    for path in _shared_code():
        assert "Super Admin" not in path.read_text(encoding="utf-8") or path.name == "security.py", path.name
    for path in (ROOT / "templates").glob("*.html"):
        assert "Super Admin" not in path.read_text(encoding="utf-8", errors="ignore"), path.name


def test_the_public_website_editor_is_gone():
    from core.security import ADMIN_PERMISSION_DEFS, ADMIN_ENDPOINT_PERMISSIONS, ADMIN_ROLE_PRESETS

    assert not [c for c, *_ in ADMIN_PERMISSION_DEFS if c.startswith("website.")]
    assert not [e for e in ADMIN_ENDPOINT_PERMISSIONS if "website" in e or "enquir" in e or "news" in e]
    assert "Website & Content Manager" not in ADMIN_ROLE_PRESETS and "School Profile Manager" in ADMIN_ROLE_PRESETS
    routes = (ROOT / "blueprints" / "school" / "routes.py").read_text(encoding="utf-8")
    for name in ("admin_school_website", "admin_school_news", "admin_school_enquir", "SchoolPublicPage",
                 "SchoolPublicNews", "SchoolPublicEnquiry"):
        assert name not in routes, name
    for template in ("admin_school_website.html", "admin_school_news_form.html", "admin_school_enquiry_detail.html"):
        assert not (ROOT / "templates" / template).exists(), template


def test_a_school_delivery_secret_is_encrypted_isolated_and_never_returned():
    import inspect

    from core import delivery

    assert {"smtp_password", "sms_api_token"} <= delivery.SECRET_KEYS
    save = inspect.getsource(delivery.save_email)
    assert "encrypt(password)" in save and "resolve_public(host)" in save
    # The connection goes to the address that was checked, and again checked at send time.
    opened = inspect.getsource(delivery._open_smtp)
    assert "resolve_public(settings.host)" in opened and "client.connect(target" in opened
    assert delivery.ALLOWED_SMTP_PORTS == (25, 465, 587, 2525)
    # What the page is given carries facts about the secrets, never the secrets.
    status = inspect.getsource(delivery.status)
    assert "bool(own.get('smtp_password'))" in status and "bool(own.get('sms_api_token'))" in status
    assert "'password'" not in status and "'token'" not in status
    # A half-set-up school never borrows the platform's account.
    assert inspect.getsource(delivery.email_settings).count("half set up") == 1
    # Nothing else reads the delivery environment: every sender goes through this module.
    for path in _shared_code():
        if path.name == "delivery.py":
            continue
        body = path.read_text(encoding="utf-8")
        assert "BRIGHTSTARS_SMTP" not in body and "BRIGHTSTARS_SMS" not in body, path.name


def test_the_uploads_route_decides_on_the_path_it_will_actually_serve():
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    route = app[app.index("def uploaded_file"):app.index("if __name__=='__main__'")]
    assert "PRIVATE_UPLOAD_FOLDERS=frozenset({'messages'})" in app
    assert "PUBLIC_UPLOAD_FOLDERS=frozenset({'branding'})" in app
    # Normalised first, judged second, and the normalised path is what is served: otherwise
    # "students/../messages/x" is judged as a student photo and served as a message.
    assert route.index("posixpath.normpath") < route.index("PRIVATE_UPLOAD_FOLDERS") < route.index("send_from_directory(uploads_dir(),clean)")
    assert "send_from_directory(uploads_dir(),clean)" in route
    assert "send_from_directory(uploads_dir(),filename)" not in route


def test_the_who_is_online_ping_only_exists_where_a_school_is_selected():
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    hook = app[app.index("def track_live_presence"):app.index("def presence_heartbeat")]
    assert "g.get('tenant') is None" in hook
    headers = app[app.index("def apply_security_headers"):app.index("def apply_school_theme")]
    # The script is added only on a school's portal, or a page on an address that belongs to no
    # school (with a stale sign-in in the cookie) would keep pinging a route it does not serve.
    assert "g.get('tenant') is not None and _presence_identity()[0]" in headers
    assert "_discard_school_identity()" in RESOLVER[RESOLVER.index("if host in config.platform_hosts()"):]


def test_each_school_numbers_its_people_by_a_pattern_in_its_own_folder():
    import inspect

    from core import numbering, numbering_pattern

    # The rule is data, not code: a small JSON file in the school's own folder, never a program.
    assert numbering.RULES_FILE == "numbering.json"
    assert not (ROOT / "tenant_starter").exists(), "there is no starter program any more: the default is a pattern"
    assert numbering.DEFAULT_CANDIDATE_PATTERN == "{school}-{year}-{seq:4}"
    assert numbering.DEFAULT_STUDENT_PATTERN == "" and numbering.DEFAULT_FIRST_NUMBER == 1
    assert not hasattr(numbering, "install_rules_file") and not hasattr(numbering, "STARTER_FILE")
    # A new school is given its file from the create-school form (default pre-filled), checked before
    # anything is created; the platform never copies a file that is then executed.
    provisioning = (ROOT / "control_plane" / "provisioning.py").read_text(encoding="utf-8")
    assert "install_rules_file" not in provisioning
    create = provisioning[provisioning.index("def create_tenant("):provisioning.index("# A school's own identity")]
    assert "check_numbering_inputs(" in create and "numbering.write_rules(" in create
    assert create.index("check_numbering_inputs(") < create.index("init_platform_db()") < create.index("upgrade_tenant(info)")
    # The platform decides nothing about what a candidate code looks like: the one place that
    # used to build it now only asks the school's own rule.
    entrance = (ROOT / "core" / "entrance.py").read_text(encoding="utf-8")
    body = entrance[entrance.index("def _new_candidate_code"):entrance.index("def _new_candidate_password")]
    assert "new_candidate_code(" in body and "code_prefix" not in body and "{year}" not in body
    # What comes back is checked; a failing rule is refused and never replaced by another rule.
    assert numbering.CANDIDATE_CODE.pattern.startswith("^[A-Z0-9]")
    assert not numbering.CANDIDATE_CODE.match("../X1") and not numbering.CANDIDATE_CODE.match("AB/123")
    assert not numbering.CANDIDATE_CODE.match("A B12") and not numbering.CANDIDATE_CODE.match("A" * 41)
    source = inspect.getsource(numbering.new_candidate_code)
    assert "MAX_ATTEMPTS" in source and "read_rules(" in source
    assert "DEFAULT_RULES" not in source, "a rule that fails must be refused, never swapped for the default"
    # Written whole and atomically, and only after the checks; a file that is wrong is refused, not replaced.
    assert "os.replace(" in inspect.getsource(numbering._write) and "os.fsync(" in inspect.getsource(numbering._write)
    assert "check_rules(" in inspect.getsource(numbering.write_rules)
    assert "check_rules(" in inspect.getsource(numbering.save_rules)
    import re as _re
    assert not _re.search(r"(?<![A-Za-z_])open\(", inspect.getsource(numbering)) and inspect.getsource(numbering).count("os.fdopen(") == 1
    # A student number may be written the school's way, but the running number and the ledger
    # that stops a number being issued twice stay the platform's.
    generator = (ROOT / "services" / "student_number_generator.py").read_text(encoding="utf-8")
    assert generator.index("numbering.student_number(") < generator.index("Collision check #1")
    assert numbering_pattern.PALETTE, "the console's palette and reference table come from one list"


def test_a_numbering_pattern_is_read_and_never_run():
    """Letting the platform's operators type a school's numbering rule must not let them run code on
    a server that can reach every school's database. So the two modules that handle a pattern only
    ever read it: no eval, exec, compile, import machinery, template engine, ``format`` on typed text,
    pickle, subprocess or shell, and nothing typed is used as a regular expression."""
    import ast

    banned_calls = {"eval", "exec", "compile", "__import__", "setattr", "globals", "locals", "open", "system", "popen", "Popen", "loads_module", "import_module", "exec_module", "spec_from_file_location", "format", "format_map", "safe_substitute", "substitute", "Template", "literal_eval"}
    banned_imports = {"importlib", "runpy", "pickle", "marshal", "shelve", "subprocess", "ctypes", "imp", "code", "codeop", "ast", "string.Template", "jinja2", "yaml", "cffi"}
    for name in ("numbering.py", "numbering_pattern.py"):
        source = (ROOT / "core" / name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                function = node.func
                called = function.id if isinstance(function, ast.Name) else function.attr if isinstance(function, ast.Attribute) else ""
                regex_compile = (isinstance(function, ast.Attribute) and called == "compile"
                                 and isinstance(function.value, ast.Name) and function.value.id == "re")
                if called in banned_calls and not regex_compile:  # re.compile only: see the matcher check below
                    # str.format is fine on OUR OWN literals only; none of these files uses it at all.
                    raise AssertionError(f"core/{name} line {node.lineno} calls {called}(): typed text must never be run or formatted")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in banned_imports, f"core/{name} imports {alias.name}"
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in banned_imports, f"core/{name} imports from {node.module}"
    pattern_source = (ROOT / "core" / "numbering_pattern.py").read_text(encoding="utf-8")
    matcher = pattern_source[pattern_source.index("    def matcher("):pattern_source.index("    def next_number(")]
    assert "re.escape(token.text.upper())" in matcher and "re.escape(self._value(" in matcher
    # The retired Python-file mechanism is gone for good.
    for path in (ROOT / "core", ROOT / "control_plane", ROOT / "services"):
        for file in path.glob("*.py"):
            code = file.read_text(encoding="utf-8")
            assert "exec_module" not in code and "spec_from_file_location" not in code, file.name
    # The console routes that take a pattern never pass it anywhere but the checked functions.
    console = (ROOT / "control_plane" / "console.py").read_text(encoding="utf-8")
    for route in ("platform_school_numbering", "platform_numbering_preview"):
        block = console[console.index(f"def {route}("):]
        block = block[:block.index("\n@app.") if "\n@app." in block else len(block)]
        for word in ("eval(", "exec(", "compile(", "importlib", "|safe"):
            assert word not in block, f"{route} uses {word}"
    editor = (ROOT / "templates" / "platform" / "_numbering.html").read_text(encoding="utf-8")
    assert "|safe" not in editor and "innerHTML" not in (ROOT / "static" / "platform-numbering.js").read_text(encoding="utf-8")


def test_only_signed_in_platform_admins_can_set_a_schools_numbering():
    console = (ROOT / "control_plane" / "console.py").read_text(encoding="utf-8")
    for route in ("platform_school_numbering", "platform_numbering_preview"):
        start = console.index(f"def {route}(")
        decorators = console[console.rindex("@app.post", 0, start):start]
        assert "@platform_host_only" in decorators and "@platform_required" in decorators and "@csrf_protect" in decorators, route
        assert decorators.index("@platform_host_only") < decorators.index("@platform_required") < decorators.index("@csrf_protect"), route
    # Every save is recorded with the old and the new pattern.
    provisioning = (ROOT / "control_plane" / "provisioning.py").read_text(encoding="utf-8")
    update = provisioning[provisioning.index("def update_numbering("):provisioning.index("def preview_numbering(")]
    assert "record('tenant.numbering_update'" in update and "_describe_rules(old, new)" in update
    # The preview only ever reads a school's own database.
    preview = provisioning[provisioning.index("def _read_school("):provisioning.index("def update_numbering(")]
    for verb in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "COMMIT"):
        assert verb not in preview.upper().replace("UPDATED", ""), verb
    assert preview.count("postgresql_readonly=True") >= 3


def test_retired_tables_are_only_dropped_when_nothing_would_be_lost():
    from core import retired_tables

    assert set(retired_tables.RETIRED_TABLES) == {"school_public_pages", "school_public_news",
                                                   "school_public_enquiries"}
    upgrade = APP[APP.index("def _init_db"):APP.index("def csrf_token")]
    assert "drop_empty()" in upgrade
    import inspect
    assert "rows == 0" in inspect.getsource(retired_tables.drop_empty)
    cli = (ROOT / "control_plane" / "cli.py").read_text(encoding="utf-8")
    assert "args.yes" in cli and "--yes" in cli


def test_every_write_route_is_tested_on_postgres_or_exempt():
    """A form submission that fails only on PostgreSQL is the bug SQLite-era code hides. Every route
    that accepts a POST is submitted for real by tests/verification/write_paths_pg_posts.py; a new
    one fails here until it is added there (or listed, with a reason, as exempt)."""
    import json
    import subprocess

    # In a process of its own: importing the application switches on SQLite foreign keys for the
    # whole process, which would change how the older tests after this one behave. It also needs the
    # platform registry, so with no PostgreSQL server to reach there is nothing to check.
    program = """
import importlib.util, json, sys
sys.path.insert(0, %r)
try:
    from app import app
except Exception as exc:
    import sqlalchemy
    if isinstance(exc, (sqlalchemy.exc.SQLAlchemyError, RuntimeError)):
        print(json.dumps({"skipped": str(exc)[:200]})); sys.exit(0)
    raise
spec = importlib.util.spec_from_file_location("pg_posts_coverage", %r)
coverage = importlib.util.module_from_spec(spec); spec.loader.exec_module(coverage)
print(json.dumps({"unaccounted": sorted(map(str, coverage.unaccounted(app.url_map))),
                  "stale": [sorted(map(str, part)) for part in coverage.stale(app.url_map)]}))
""" % (str(ROOT), str(ROOT / "tests" / "verification" / "pg_posts_coverage.py"))
    done = subprocess.run([sys.executable, "-c", program], cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr[-1500:]
    answer = json.loads(done.stdout.strip().splitlines()[-1])
    if "skipped" in answer:
        return
    assert not answer["unaccounted"], answer["unaccounted"]
    assert answer["stale"] == [[], []], answer["stale"]


def test_the_standard_question_set_is_the_platforms_own_and_names_no_school():
    """The starter banks were written for the platform, so nothing in them may name a school, and each
    must serve a paper whose marks are whole hundredths (a count that splits 100 marks exactly)."""
    import json
    import re

    text = "".join(path.read_text(encoding="utf-8") for path in (ROOT / "starter_banks").glob("*.json"))
    assert len(re.findall(r'"answer":', text)) >= 200
    assert not re.search(r"creative rainbow|crainbow|montessori|crms", text, re.I)
    assert not (ROOT / "data").exists(), "the platform ships no school's questions; schools get theirs from starter_banks/ or an import"
    manifest = json.loads((ROOT / "starter_banks" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "2.0" and len(manifest["banks"]) == 6
    for entry in manifest["banks"]:
        served = entry["questions_to_serve"]
        assert abs(round(100.0 / served, 2) * served - 100.0) < 1e-9, entry
        assert entry["question_count"] >= served, entry


def test_marks_are_decimals_and_read_as_whole_numbers_when_they_are_whole():
    from core import marks
    from models import Attempt, AttemptQuestion

    for column in (AttemptQuestion.__table__.c.points, Attempt.__table__.c.score, Attempt.__table__.c.max_score):
        assert column.type.__class__.__name__ == "Float", column
    assert marks.total([2.5] * 40) == 100 and marks.tidy(100.0) == 100 and marks.tidy(62.5) == 62.5
    # Grading never rounds a mark to a whole number, and the upgrade for older schools exists.
    # (read as text: importing core.entrance would start the whole application inside this suite)
    entrance = (ROOT / "core" / "entrance.py").read_text(encoding="utf-8")
    assert "int(points" not in entrance[entrance.index("def grade"):entrance.index("def candidate_record")]
    assert "_allow_fractional_marks()" in APP[APP.index("def _init_db"):APP.index("def csrf_token")]
    assert "app.jinja_env.finalize" in APP


def test_the_platform_ships_no_schools_logo_photos_or_uploads_and_serves_no_generated_files():
    """Everything a school owns lives in its own folder under tenants/. The platform's own static
    folder holds no school's logo, photographs or uploads, a school without a logo gets a neutral
    placeholder that really exists, and a candidate's result image is made in the school's private
    folder (never a public one) and deleted after it is sent."""
    static = ROOT / "static"
    for name in ("uploads", "generated"):
        assert not (static / name).exists(), name
    assert not (static / "images" / "school").exists() and not (static / "images" / "school_logo.png").exists()
    branding = (ROOT / "core" / "branding.py").read_text(encoding="utf-8")
    placeholder = re.search(r"PLACEHOLDER_LOGO = '([^']+)'", branding).group(1)
    assert (static / placeholder).is_file(), placeholder
    entrance = (ROOT / "core" / "entrance.py").read_text(encoding="utf-8")
    routes = (ROOT / "blueprints" / "entrance" / "routes.py").read_text(encoding="utf-8")
    for body in (entrance, routes):
        assert "school_logo.png" not in body and '"static" /' not in body and "'static'," not in body
    assert "generated_dir()" in entrance and "os.remove(image_path)" in routes
    storage = (ROOT / "core" / "storage.py").read_text(encoding="utf-8")
    assert "return _folder('generated')" in storage
