import datetime as _dt
from datetime import date, datetime


def format_display_date(value):
    """
    Central Brightstars Academics display-date policy.

    Storage/API values remain ISO:
        YYYY-MM-DD

    User-facing display:
        DD/MM/YYYY

    Invalid/empty values are returned safely.
    """

    if value is None:
        return ""

    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y")

    if isinstance(value, date):
        return value.strftime("%d/%m/%Y")

    text = str(value).strip()

    if not text:
        return ""

    # ISO date / datetime.
    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
    ):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.strftime("%d/%m/%Y")
        except ValueError:
            pass

    # Already user-facing.
    for fmt in (
        "%d/%m/%Y",
        "%d-%m-%Y",
    ):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.strftime("%d/%m/%Y")
        except ValueError:
            pass

    # Do not silently reinterpret unknown values.
    return text

def format_display_date_configured(value, display_format="DD/MM/YYYY"):
    if display_format == "DD/MM/YYYY":
        return format_display_date(value)

    if value is None:
        return ""

    if hasattr(value, "strftime"):
        formats = {
            "DD/MM/YYYY": "%d/%m/%Y",
            "MM/DD/YYYY": "%m/%d/%Y",
            "YYYY-MM-DD": "%Y-%m-%d",
        }
        return value.strftime(
            formats.get(display_format, "%d/%m/%Y")
        )

    text = str(value).strip()

    if not text:
        return ""

    parsed = None

    try:
        parsed = _dt.datetime.fromisoformat(
            text.replace("Z", "+00:00")
        )
    except Exception:
        try:
            parsed = _dt.date.fromisoformat(text[:10])
        except Exception:
            parsed = None

    if parsed is None:
        return text

    formats = {
        "DD/MM/YYYY": "%d/%m/%Y",
        "MM/DD/YYYY": "%m/%d/%Y",
        "YYYY-MM-DD": "%Y-%m-%d",
    }

    return parsed.strftime(
        formats.get(display_format, "%d/%m/%Y")
    )

