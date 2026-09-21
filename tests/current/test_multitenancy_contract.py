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
    assert "uploads_dir()" in uploads
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
    """A school's address serves the portal only. The public marketing pages
    belong to the platform's own site, which is the only website there is."""
    fn = RESOLVER[RESOLVER.index("def resolve_tenant"):RESOLVER.index("def release_tenant")]
    assert "request.path.startswith('/school/')" in fn
    # "/" redirects into the portal instead of rendering a marketing homepage.
    index = APP[APP.index("def index():"):APP.index("@app.after_request")]
    assert "_portal_front_door()" in index and "platform_home()" in index


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
    create = provisioning[provisioning.index("def create_tenant"):provisioning.index("BRANDING_FIELDS = (")]
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
    provisioning = (ROOT / "control_plane" / "provisioning.py").read_text(encoding="utf-8")
    # Imported rows must not collide with ids handed out afterwards.
    assert "def _reset_sequences" in provisioning and "pg_get_serial_sequence" in provisioning


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
    for page in ("base.html", "site.html", "login.html"):
        assert logo in (ROOT / "templates" / "platform" / page).read_text(encoding="utf-8"), page
    # A school's portal must look like the school's own, not like the platform's.
    for template in (ROOT / "templates").glob("*.html"):
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
