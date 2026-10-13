"""The time zone each school keeps its day in (``tenants.timezone``): when a day starts, and when a member of staff
is late (core/staff_attendance.py). Set by a platform admin when the school is created, and changeable on its page.

It is chosen from a short list rather than typed, so a mistyped name can never reach a school. All of Nigeria is one
zone (West Africa Time, UTC+1), whose standard name is Africa/Lagos: a school in Kano, Abuja or Port Harcourt uses it
too. Add a zone here when the platform first serves a school in it.
"""

from zoneinfo import ZoneInfo

DEFAULT = 'Africa/Lagos'

# (standard zone name, what a platform admin reads)
CHOICES = (
    ('Africa/Lagos', 'Nigeria, Cameroon, Benin, Niger, Chad (West Africa Time, UTC+1)'),
    ('Africa/Accra', 'Ghana, Sierra Leone, Liberia, The Gambia, Senegal, Côte d\'Ivoire (UTC+0)'),
    ('Africa/Kinshasa', 'DR Congo, west (UTC+1)'),
    ('Africa/Johannesburg', 'South Africa (UTC+2)'),
    ('Africa/Cairo', 'Egypt (UTC+2 or +3)'),
    ('Africa/Nairobi', 'Kenya, Uganda, Tanzania (East Africa Time, UTC+3)'),
    ('Europe/London', 'United Kingdom (UTC+0 or +1)'),
    ('UTC', 'UTC'),
)
NAMES = dict(CHOICES)


def clean(value):
    """``value`` when it is one of the choices, else None."""
    value = str(value or '').strip()
    return value if value in NAMES else None


def zone_of(name):
    """The ZoneInfo for a school's stored zone; the default for anything unknown or unreadable."""
    try:
        return ZoneInfo(clean(name) or DEFAULT)
    except Exception:
        return ZoneInfo(DEFAULT)
