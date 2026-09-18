from datetime import datetime, timezone


DATE_DISPLAY_SETTING = "date_display_format"

DEFAULT_DATE_DISPLAY_FORMAT = "DD/MM/YYYY"

SUPPORTED_DATE_DISPLAY_FORMATS = (
    "DD/MM/YYYY",
    "MM/DD/YYYY",
    "YYYY-MM-DD",
)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def clean(value):
    if value is None:
        return ""
    return str(value).strip()


def get_school_setting(con, school_id, setting_key, default=None):
    row = con.execute(
        """
        SELECT setting_value
        FROM school_settings
        WHERE school_id = ?
          AND setting_key = ?
        LIMIT 1
        """,
        (school_id, setting_key),
    ).fetchone()

    if row is None:
        return default

    value = clean(row["setting_value"])

    return value if value else default


def get_date_display_format(con, school_id):
    value = get_school_setting(
        con,
        school_id,
        DATE_DISPLAY_SETTING,
        DEFAULT_DATE_DISPLAY_FORMAT,
    )

    if value not in SUPPORTED_DATE_DISPLAY_FORMATS:
        return DEFAULT_DATE_DISPLAY_FORMAT

    return value


def set_school_setting(
    con,
    school_id,
    setting_key,
    setting_value,
    updated_by=None,
):
    con.execute(
        """
        INSERT INTO school_settings
            (
                school_id,
                setting_key,
                setting_value,
                updated_at,
                updated_by
            )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(school_id, setting_key)
        DO UPDATE SET
            setting_value = excluded.setting_value,
            updated_at = excluded.updated_at,
            updated_by = excluded.updated_by
        """,
        (
            school_id,
            setting_key,
            setting_value,
            utc_now(),
            updated_by,
        ),
    )