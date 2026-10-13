"""Contract: staff attendance checks on the server, keeps no positions, and its screen gives nothing away.

* the school's day is kept in its own time zone, from its platform record, never a setting shared by every school;
* a clock-in is checked on the server: the changing code (HMAC of the 30-second window, compared in constant time),
  the phone's distance and accuracy, the one registered phone (a hash of a random tag in an HttpOnly cookie), a rate
  limit; the time is the server's, in the school's time zone;
* no latitude or longitude is stored for a clock-in, nor written to the log;
* the screen answers only for the hash of its random token, and is never cached, indexed or referred; its link is shown
  once, never put in an address;
* only the clock and settings pages may ask for the location; every other page keeps it off;
* hand-marking needs a note and never overwrites a phone's time; nobody decides their own correction;
* the CSV defuses formula-looking cells; no template prints anything raw, and the scripts never use innerHTML.
It reads source only, and needs no database.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROUTES = (ROOT / "blueprints" / "school" / "staff_attendance.py").read_text(encoding="utf-8")
CORE = (ROOT / "core" / "staff_attendance.py").read_text(encoding="utf-8")
MODELS = (ROOT / "models" / "staff_attendance.py").read_text(encoding="utf-8")
SECURITY = (ROOT / "core" / "security.py").read_text(encoding="utf-8")
APP = (ROOT / "app.py").read_text(encoding="utf-8")
TEMPLATES = ("school_clock.html", "school_staff_attendance.html", "school_staff_attendance_month.html",
             "school_staff_attendance_settings.html", "staff_attendance_display.html")


def _route(name):
    start = ROUTES.index(f"def {name}(")
    following = ROUTES.find("\n@app.", start)
    return ROUTES[start:following if following != -1 else len(ROUTES)]


def test_a_clock_in_is_checked_on_the_server():
    punch = _route("admin_clock_punch")
    for rule in ("sa.code_is_current(settings.code_secret", "sa.check_position(settings, sa.read_position(request.form))",
                 "hmac.compare_digest(sa.token_hash(request.cookies.get(sa.COOKIE)), device.tag_hash)", "allow(f'staff-clock:",
                 "sa.school_now()", "httponly=True"):
        assert rule in punch, rule
    assert "hmac.compare_digest" in CORE and "hmac.new(secret.encode()" in CORE and "range(current - GRACE, current + 1)" in CORE
    assert "math.isfinite" in CORE and "acc > settings.max_accuracy_m" in CORE and "distance > settings.radius_m" in CORE


def test_each_school_keeps_its_own_time():
    assert "current_tenant(required=False)" in CORE and "zone_of(getattr(tenant, 'timezone', None))" in CORE
    assert "BRIGHTSTARS_TIMEZONE" not in (ROOT / "control_plane" / "config.py").read_text(encoding="utf-8")
    zones = (ROOT / "control_plane" / "timezones.py").read_text(encoding="utf-8")
    assert "DEFAULT = 'Africa/Lagos'" in zones and "value if value in NAMES else None" in zones
    console = (ROOT / "control_plane" / "console.py").read_text(encoding="utf-8")
    start = console.index("def platform_school_timezone(")
    assert "@platform_required" in console[start - 200:start] and "@csrf_protect" in console[start - 200:start]


def test_no_position_is_kept():
    assert not re.search(r"\b(latitude|longitude|lat|lng)\s*=\s*db\.Column", MODELS.split("class StaffAttendanceDevice")[1])
    for logged in re.findall(r"audit_log\([^)]*\{[^}]*\}", ROUTES):
        assert "lat" not in logged and "lng" not in logged, logged


def test_the_screen_gives_nothing_away():
    screen = _route("_display_settings")
    assert "hmac.compare_digest(sa.token_hash(token), facts['token_hash'])" in screen and "remember('staff-screen', 5" in screen
    for header in ("'no-store, private'", "'noindex, nofollow'", "'no-referrer'"):
        assert header in _route("_screen"), header
    assert "session['staff_display_link'] = raw" in _route("admin_staff_attendance_display")
    assert "link=raw" not in ROUTES


def test_only_the_clock_and_settings_pages_may_ask_for_the_location():
    assert "geolocation=()" in APP
    assert ROUTES.count("geo=True") == 2
    assert "geo=True" in _route("admin_clock") and "geo=True" in _route("admin_staff_attendance_settings")


def test_hand_marking_and_corrections_are_honest():
    mark = _route("admin_staff_attendance_mark")
    assert "if not note:" in mark and "rec and rec.clock_in" in mark
    assert "c.requested_by == me['id']" in _route("admin_staff_attendance_correction_decide")
    for name in ("admin_staff_attendance_mark", "admin_staff_attendance_correction_decide", "admin_staff_attendance_settings",
                 "admin_staff_attendance_display", "admin_staff_attendance_device_reset", "admin_staff_attendance_closed"):
        assert f"'{name}':'staff_attendance.manage'" in SECURITY, name
    for name in ("admin_staff_attendance", "admin_staff_attendance_month"):
        assert f"'{name}':'staff_attendance.view'" in SECURITY, name


def test_exports_and_pages_are_safe():
    month = _route("admin_staff_attendance_month")
    assert "csv_safe(hr.full_name(p, False))" in month and "csv_safe(p.job_title" in month
    for name in TEMPLATES:
        source = (ROOT / "templates" / name).read_text(encoding="utf-8")
        assert "|safe" not in source.replace(" ", ""), name
        assert "innerHTML" not in source, name
