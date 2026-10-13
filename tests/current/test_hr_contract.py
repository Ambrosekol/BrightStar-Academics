"""Contract: staff records stay private, and nothing in them quietly disappears.

* every HR page is behind a permission (hr.view, hr.manage or hr.leave.approve), except My leave and cancelling, which
  check inside that the leave is the person's own (or that they may manage records);
* HR documents live in a folder no file address opens, are checked by their contents, and are served only by the
  permission-checked route, not sniffed and not cached;
* a record page and My leave are never cached, and opening a record or a document is logged; a change is logged by the
  names of the fields changed, never their values;
* career entries have no edit or delete route, only striking out with a reason;
* nobody decides leave on their own record, and approval re-checks the balance with the row locked;
* HR's private notes never reach My leave, and no HR template prints anything raw.
It reads source only, and needs no database.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROUTES = (ROOT / "blueprints" / "school" / "hr.py").read_text(encoding="utf-8")
CORE = (ROOT / "core" / "hr.py").read_text(encoding="utf-8")
SECURITY = (ROOT / "core" / "security.py").read_text(encoding="utf-8")
ACCESS = (ROOT / "core" / "upload_access.py").read_text(encoding="utf-8")
TEMPLATES = ("school_hr_staff.html", "school_hr_form.html", "school_hr_person.html", "school_hr_leave.html",
             "school_hr_leave_types.html", "school_my_leave.html", "includes/_hr_leave_rows.html")


def _route(name):
    start = ROUTES.index(f"def {name}(")
    following = ROUTES.find("\n@app.", start)
    return ROUTES[start:following if following != -1 else len(ROUTES)]


def test_every_hr_page_needs_a_permission_or_checks_inside():
    endpoints = re.findall(r"@app\.(?:route|post|get)\([^)]*\)\n@admin_required\n(?:@csrf_protect\n)?def (\w+)\(", ROUTES)
    assert len(endpoints) >= 19, endpoints
    inside = {"admin_hr_leave", "admin_hr_leave_cancel", "admin_my_leave", "admin_my_leave_request"}
    for name in endpoints:
        if name in inside:
            continue
        assert re.search(rf"'{name}':'hr\.(view|manage|leave\.approve)'", SECURITY), f"{name} has no HR permission"
    assert "admin_access_error('hr.leave.approve')" in _route("admin_hr_leave")
    assert "admin_access_error('hr.manage')" in _route("admin_hr_leave_cancel")
    assert "StaffRecord.admin_id == me['id']" in _route("admin_my_leave") and "StaffRecord.admin_id == me['id']" in _route("admin_my_leave_request")
    for name in re.findall(r"^def (admin_\w+)\(", ROUTES, re.M):
        assert "@admin_required" in ROUTES[max(0, ROUTES.index(f"def {name}(") - 200):ROUTES.index(f"def {name}(")], name


def test_documents_are_private_checked_and_served_carefully():
    assert "'hr': NEVER" in ACCESS
    assert "save_upload_bytes('hr', hr.document_name(ext)" in _route("admin_hr_document_add")
    assert "hr.check_document(upload, attachment_limit_bytes())" in _route("admin_hr_document_add")
    assert "data.startswith(signatures)" in CORE and "secrets.token_hex(12)" in CORE
    served = _route("admin_hr_document")
    assert "'nosniff'" in served and "'no-store, private'" in served and "hr_document_opened" in served


def test_records_are_never_cached_and_their_reading_is_logged_without_contents():
    person = _route("admin_hr_person")
    assert "'no-store, private'" in person and "hr_record_viewed" in person
    assert "'no-store, private'" in _route("admin_my_leave")
    edit = _route("admin_hr_edit")
    assert "{'fields': changed}" in edit


def test_career_entries_are_only_struck_out():
    career_routes = re.findall(r"@app\.\w+\('(/admin/hr/career/[^']+)'", ROUTES)
    assert career_routes == ["/admin/hr/career/<int:eid>/strike"], career_routes
    strike = _route("admin_hr_career_strike")
    assert "db.session.delete" not in strike and "struck_reason" in strike and "if not reason" in strike


def test_nobody_decides_their_own_leave_and_approval_rechecks():
    assert "not (record and record.admin_id == admin['id'])" in ROUTES
    decide = _route("admin_hr_leave_decide")
    assert ".with_for_update()" in decide and "_may_decide(me, leave, record)" in decide and "held['left']" in decide


def test_private_notes_stay_with_hr_and_nothing_is_printed_raw():
    mine = (ROOT / "templates" / "school_my_leave.html").read_text(encoding="utf-8")
    assert "notes" not in mine.replace("HR's private notes", "")
    for name in TEMPLATES:
        source = (ROOT / "templates" / name).read_text(encoding="utf-8")
        assert "|safe" not in source.replace(" ", ""), name
