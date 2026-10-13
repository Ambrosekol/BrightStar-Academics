"""Contract: payroll keeps salaries and bank details private, needs four eyes, and never loses a kobo.

* money is integer kobo, and every percentage is rounded half up in one place (``pct``); no float reaches a payslip;
* bank account numbers, pension PINs and tax IDs are encrypted (a per-school key), rotated with the school's other
  secrets, and decrypted only by the two exports, which need an approved run and someone who may manage or approve;
* a run is approved only by someone other than whoever last prepared or changed it; nobody edits their own pay or
  payslip; an approved run refuses changes and a paid one is final;
* every page with salaries is never cached; the logs never carry amounts, account numbers or PINs;
* the exports defuse formula-looking cells, and no payroll template prints anything raw.
It reads source only, and needs no database.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROUTES = (ROOT / "blueprints" / "school" / "payroll.py").read_text(encoding="utf-8")
CORE = (ROOT / "core" / "payroll.py").read_text(encoding="utf-8")
MODELS = (ROOT / "models" / "payroll.py").read_text(encoding="utf-8")
SECURITY = (ROOT / "core" / "security.py").read_text(encoding="utf-8")
ROTATION = (ROOT / "core" / "secrets_rotation.py").read_text(encoding="utf-8")
TEMPLATES = ("school_payroll.html", "school_payroll_run.html", "school_payroll_staff.html", "school_payroll_staff_edit.html",
             "school_payroll_settings.html", "payroll_payslip.html", "school_my_payslips.html")


def _route(name):
    start = ROUTES.index(f"def {name}(")
    following = ROUTES.find("\n@app.", start)
    return ROUTES[start:following if following != -1 else len(ROUTES)]


def test_money_is_whole_kobo():
    assert "Float" not in MODELS and "float(" not in CORE
    assert "(amount * bp + 5000) // 10000" in CORE


def test_secrets_are_encrypted_rotated_and_decrypted_only_to_export():
    assert "b'brightstars-payroll-v1:' + current_tenant().slug.encode()" in CORE
    assert "_PAYROLL_FIELDS = ('account_number_enc', 'rsa_pin_enc', 'tax_id_enc')" in ROTATION
    users = [m.start() for m in re.finditer(r"pr\.decrypt\(", ROUTES)]
    assert users, "nothing decrypts?"
    for at in users:
        owner = ROUTES.rfind("\ndef ", 0, at)
        name = ROUTES[owner + 5:ROUTES.index("(", owner)]
        assert name in ("admin_payroll_run_bank", "admin_payroll_run_returns"), name
    allowed = _route("_export_allowed")
    assert "_may('payroll.manage') or _may('payroll.approve')" in allowed and "('approved', 'paid')" in allowed
    for name in ("admin_payroll_run_bank", "admin_payroll_run_returns"):
        assert "_export_allowed(run)" in _route(name) and "audit_log(" in _route(name)


def test_four_eyes_and_locks():
    approve = _route("admin_payroll_run_approve")
    assert "run.prepared_by == me['id']" in approve and "run.status != 'draft'" in approve and ".with_for_update()" in ROUTES
    for name in ("admin_payroll_run_refresh", "admin_payroll_run_adjust", "admin_payroll_run_reopen"):
        assert "_changed(run, me)" in _route(name), name
    assert "run.status != 'draft'" in _route("admin_payroll_run_adjust") and "run.status != 'approved'" in _route("admin_payroll_run_reopen")
    assert "_is_own(me, sid)" in _route("admin_payroll_staff_edit") and _route("admin_payroll_run_adjust").count("_is_own(me, ") >= 2
    assert "'admin_payroll_run_approve':'payroll.approve'" in SECURITY and "'admin_payroll_run_adjust':'payroll.manage'" in SECURITY


def test_salaries_stay_private():
    for name in ("admin_payroll", "admin_payroll_run", "admin_payroll_staff", "admin_payroll_staff_edit", "admin_payroll_payslip",
                 "admin_my_payslips", "admin_payroll_settings"):
        assert "_private(" in _route(name), name
    payslip = _route("admin_payroll_payslip")
    assert "_may('payroll.view')" in payslip and "run.status in ('approved', 'paid')" in payslip
    for logged in re.findall(r"audit_log\('payroll_pay_saved'[^)]*\)", ROUTES, re.S):
        assert "'basic':" not in logged and "account_number" not in logged and "rsa_pin" not in logged


def test_exports_and_pages_are_safe():
    assert "csv_safe(c) if isinstance(c, str) else c" in _route("_csv")
    for name in TEMPLATES:
        source = (ROOT / "templates" / name).read_text(encoding="utf-8")
        assert "|safe" not in source.replace(" ", ""), name
        assert "innerHTML" not in source, name
