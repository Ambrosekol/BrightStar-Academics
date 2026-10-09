"""Contract: four settings areas are restricted to the school's own top-level administrator.

Email & WhatsApp, Administration (accounts, roles, permissions, scopes), Online Payments and School
profile all change something a member of staff could otherwise abuse to impersonate the school (send
mail/WhatsApp as it, create or elevate an administrator account, redirect its money, or change its
name and branding). None of the four is a grantable permission a custom role can be given - every
route under them calls ``is_school_admin()`` directly, the same gate Academic Sessions and Promotion
already use, so a platform operator (who enters a school through its own top-level admin account -
see write_paths_platform_console.py) and the school's own top-level administrator are the only ones
who can ever reach them, regardless of what permissions a school assigns to another admin account.

This reads source only, and needs no database.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FILES = {
    "branding": (ROOT / "blueprints" / "school" / "branding.py").read_text(encoding="utf-8"),
    "delivery": (ROOT / "blueprints" / "school" / "delivery.py").read_text(encoding="utf-8"),
    "paystack": (ROOT / "blueprints" / "finance" / "paystack.py").read_text(encoding="utf-8"),
    "administration": (ROOT / "blueprints" / "administration" / "routes.py").read_text(encoding="utf-8"),
}

# name -> which file it lives in
ROUTES = {
    "branding": [
        "admin_school_branding", "admin_school_branding_save",
    ],
    "delivery": [
        "admin_school_delivery", "admin_school_delivery_email_save",
        "admin_school_delivery_email_clear", "admin_school_delivery_email_test",
        "admin_school_delivery_sms_save", "admin_school_delivery_sms_clear",
        "admin_school_delivery_sms_check", "admin_school_delivery_sms_balance",
    ],
    "paystack": [
        "admin_finance_paystack_settings", "admin_finance_paystack_save",
        "admin_finance_paystack_clear", "admin_finance_paystack_test",
    ],
    "administration": [
        "admin_administration", "admin_accounts", "admin_account_new", "admin_account_edit",
        "admin_account_credentials_reset", "admin_account_toggle", "admin_roles",
        "admin_role_new", "admin_role_edit", "admin_permissions_catalogue", "admin_scopes",
    ],
}


def _body(source, name):
    """The source of one view function, up to the next top-level route or its decorators."""
    start = source.index(f"def {name}(")
    rest = source[start:]
    m = re.search(r"\n@app\.(route|post|get)\(", rest[1:])
    return rest[: m.start() + 1] if m else rest


def test_every_settings_route_requires_the_top_level_admin():
    missing = []
    for module, names in ROUTES.items():
        source = FILES[module]
        for name in names:
            body = _body(source, name)
            if "is_school_admin(" not in body:
                missing.append(f"{module}.{name}")
    assert not missing, f"these settings routes no longer check is_school_admin(): {missing}"


def test_none_of_the_four_areas_is_a_grantable_permission_any_more():
    """finance.paystack.manage and branding.manage still exist (for docs/audit history and the one
    legacy preset kept for schools that already had it - see core/security.py), but neither is
    handed out by a starter role preset any more, and delivery.manage was never in one."""
    from core.security import ADMIN_ROLE_PRESETS

    for role_name, spec in ADMIN_ROLE_PRESETS.items():
        for code in ("finance.paystack.manage", "branding.manage", "delivery.manage",
                     "admins.view", "admins.create", "admins.edit", "admins.deactivate",
                     "roles.view", "permissions.view", "scopes.view"):
            assert code not in spec["permissions"], f"{role_name} still grants {code}"


def test_the_settings_page_only_offers_these_four_to_the_top_level_admin():
    """The four pages are reached from the Settings page (blueprints/school/settings.py). Each entry
    there must be shown on the school-administrator check alone, never on a grantable permission, and
    the navigation rail must not offer any of the four to anyone else on its own."""
    settings = (ROOT / "blueprints" / "school" / "settings.py").read_text(encoding="utf-8")
    admin_base = (ROOT / "templates" / "admin_base.html").read_text(encoding="utf-8")
    for endpoint in ("admin_school_delivery", "admin_administration",
                     "admin_finance_paystack_settings", "admin_school_branding"):
        start = settings.find(f"'{endpoint}'")
        assert start != -1, f"{endpoint} is missing from the Settings page"
        entry = settings[start: settings.index("),\n", start)]
        assert re.match(rf"'{endpoint}',\s*'[\w-]+',\s*admin\b(?!\s+and)", entry), (
            f"{endpoint}'s Settings entry must be shown on is_school_admin() alone: {entry}")
        assert "can(" not in entry, f"{endpoint}'s Settings entry offers itself to a granted permission: {entry}"
        pattern = re.escape(f"'endpoint':'{endpoint}'")
        in_rail = re.search(pattern, admin_base)
        if in_rail:
            rail_entry = admin_base[in_rail.start(): admin_base.index("},", in_rail.start())]
            assert "is_school_admin_ui" in rail_entry and "admin_has_permission" not in rail_entry, (
                f"{endpoint}'s nav entry must check is_school_admin_ui only: {rail_entry}")
