"""The rules of staff attendance (models/staff_attendance.py; the pages are blueprints/school/staff_attendance.py).

How a clock-in is trusted, and how far. No phone check can be made impossible to cheat: a phone's location can be
faked with an app, and indoors it can be off by a hundred metres. So more than one thing is checked, each by the
server and each on the server's clock (changing the phone's clock does nothing):

Times are the server's clock, in the school's own time zone (``zone``), set for each school on the platform.

* The code. A screen at the school shows a QR code (and the same as six digits) that changes every 30 seconds
  (``WINDOW``). It is made from a key only the school's database holds (``code_for``), so it cannot be worked out,
  and it is accepted for about a minute (``GRACE`` windows), so a photo of it sent home is soon useless.
* The location. The phone's position must be within the school's radius, and the phone must be sure of it to within
  the school's limit; a vague reading is refused rather than guessed at. Only the distance and the accuracy are kept,
  never the position itself, and the location is read only at the moment of clocking, never in between.
* The phone. A member of staff clocks in from one phone, registered the first time they clock in; another phone is
  refused until the manager resets it, so nobody clocks in for a colleague with their own phone.
"""

import hashlib
import hmac
import math
import re
import secrets
from datetime import date, datetime, timedelta, timezone

WINDOW = 30            # seconds a code lasts on the screen
GRACE = 2              # windows before the current one that are still accepted (about a minute in all)
METHODS = {'qr_gps': 'The screen\'s code and the phone\'s location', 'qr': 'The screen\'s code only',
           'gps': 'The phone\'s location only'}
DAY_NAMES = {'1': 'Mon', '2': 'Tue', '3': 'Wed', '4': 'Thu', '5': 'Fri', '6': 'Sat', '7': 'Sun'}
STATUS_LABELS = {'present': 'In', 'late': 'Late', 'absent': 'Absent', 'leave': 'On leave', 'waiting': 'Not in yet',
                 'closed': 'School closed', 'off': 'Not a work day', 'not_counted': '—'}
COOKIE = 'bs_staff_device'
HHMM = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')


def zone():
    """The school's own time zone, set for it on the platform (control_plane/timezones.py)."""
    from control_plane.context import current_tenant
    from control_plane.timezones import zone_of
    tenant = current_tenant(required=False)
    return zone_of(getattr(tenant, 'timezone', None))


def school_now(now=None):
    """The time now at the school (an aware datetime)."""
    return (now or datetime.now(timezone.utc)).astimezone(zone())


def is_hhmm(value):
    return bool(HHMM.match(str(value or '')))


# ---------------------------------------------------------------- the changing code
def new_secret():
    return secrets.token_hex(32)


def window_of(now=None):
    return int((now or datetime.now(timezone.utc)).timestamp()) // WINDOW


def _mac(secret, window):
    return hmac.new(secret.encode(), f'staff-clock:{window}'.encode(), hashlib.sha256).hexdigest()


def code_for(secret, window):
    """(the QR code's text, the six digits) for one window."""
    mac = _mac(secret, window)
    return f'{window}-{mac[:20]}', f'{int(mac[20:32], 16) % 1_000_000:06d}'


def code_is_current(secret, submitted, now=None):
    """Whether ``submitted`` (the QR's text or the six digits) is the code of this window or one of the GRACE before it."""
    if not secret:
        return False
    submitted = str(submitted or '').strip().replace(' ', '')
    if not submitted or len(submitted) > 40:
        return False
    current = window_of(now)
    for window in range(current - GRACE, current + 1):
        qr, digits = code_for(secret, window)
        if hmac.compare_digest(submitted, qr) or hmac.compare_digest(submitted, digits):
            return True
    return False


def seconds_left(now=None):
    stamp = (now or datetime.now(timezone.utc)).timestamp()
    return WINDOW - int(stamp) % WINDOW


def token_hash(raw):
    return hashlib.sha256((raw or '').encode('utf-8')).hexdigest()


# ---------------------------------------------------------------- the location
def distance_m(lat1, lng1, lat2, lng2):
    """Metres between two points on the earth (haversine)."""
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def read_position(form):
    """(lat, lng, accuracy) from a submitted form, or None when they are missing or not real numbers."""
    try:
        lat, lng, acc = float(form.get('lat')), float(form.get('lng')), float(form.get('acc'))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (lat, lng, acc)) or not (-90 <= lat <= 90 and -180 <= lng <= 180) or acc < 0:
        return None
    return lat, lng, acc


def check_position(settings, position):
    """(distance, accuracy, error): whether the phone is at the school, as sure as the school asks."""
    if settings.latitude is None or settings.longitude is None:
        return None, None, 'The school\'s location is not set yet. Tell the attendance manager.'
    if position is None:
        return None, None, 'Your phone did not give its location. Allow location for this page, and try again outdoors.'
    lat, lng, acc = position
    distance = distance_m(settings.latitude, settings.longitude, lat, lng)
    if acc > settings.max_accuracy_m:
        return round(distance), round(acc), (f'Your phone is only sure of where it is to within {round(acc)} m (the school asks for '
                                             f'{settings.max_accuracy_m} m). Turn on precise location, step outside or near a window, '
                                             'and try again.')
    if distance > settings.radius_m:
        return round(distance), round(acc), f'Your phone says it is {round(distance)} m from the school. Clock in at the school.'
    return round(distance), round(acc), None


# ---------------------------------------------------------------- days
def is_work_day(settings, day):
    return str(day.isoweekday()) in (settings.work_days or '12345')


def is_late(settings, hhmm):
    return bool(hhmm and settings.late_after and hhmm > settings.late_after)


def day_status(settings, day, today, now_hhmm, record, on_leave, closed, employed_from=None, left_on=None):
    """What one day was for one member of staff: present, late, absent, leave, waiting (today, not in yet), closed,
    off (not a work day) or not_counted (before attendance started, before they joined, after they left)."""
    iso = day.isoformat()
    if record and record.clock_in:
        return 'late' if record.late else 'present'
    if on_leave:
        return 'leave'
    if closed:
        return 'closed'
    if not is_work_day(settings, day):
        return 'off'
    if (not settings.started_on or iso < settings.started_on or (employed_from and iso < employed_from)
            or (left_on and iso > left_on) or day > today):
        return 'not_counted'
    if day == today:
        return 'waiting'
    return 'absent'


def month_days(year, month):
    first = date(year, month, 1)
    following = date(year + (month == 12), month % 12 + 1, 1)
    return [first + timedelta(days=i) for i in range((following - first).days)]


def parse_day(value):
    value = str(value or '').strip()
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def device_label(user_agent):
    """A short, plain description of a browser for the manager ("Android phone, Chrome")."""
    ua = str(user_agent or '')
    system = next((name for key, name in (('iPhone', 'iPhone'), ('iPad', 'iPad'), ('Android', 'Android phone'),
                                           ('Windows', 'Windows computer'), ('Mac OS', 'Mac'), ('Linux', 'Linux computer'))
                   if key in ua), 'A device')
    browser = next((name for key, name in (('Edg/', 'Edge'), ('OPR/', 'Opera'), ('Chrome/', 'Chrome'), ('Firefox/', 'Firefox'),
                                            ('Safari/', 'Safari')) if key in ua), '')
    return f'{system}, {browser}' if browser else system
