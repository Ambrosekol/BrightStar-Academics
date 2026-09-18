def apply(con):
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS school_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_id INTEGER NOT NULL,
            setting_key TEXT NOT NULL,
            setting_value TEXT,
            updated_at TEXT,
            updated_by INTEGER,
            FOREIGN KEY (school_id)
                REFERENCES schools(id)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS
            uq_school_settings_school_key
        ON school_settings (
            school_id,
            setting_key
        );

        CREATE INDEX IF NOT EXISTS
            ix_school_settings_school
        ON school_settings (
            school_id
        );

        CREATE INDEX IF NOT EXISTS
            ix_school_settings_key
        ON school_settings (
            setting_key
        );

        INSERT OR IGNORE INTO school_settings
            (
                school_id,
                setting_key,
                setting_value,
                updated_at,
                updated_by
            )
        SELECT
            id,
            'date_display_format',
            'DD/MM/YYYY',
            CURRENT_TIMESTAMP,
            NULL
        FROM schools
        WHERE active = 1;
        """
    )

    table_exists = con.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = 'school_settings'
        """
    ).fetchone()

    if table_exists is None:
        raise RuntimeError(
            "Migration 0017 failed: school_settings table missing."
        )

    duplicate = con.execute(
        """
        SELECT school_id, setting_key, COUNT(*)
        FROM school_settings
        GROUP BY school_id, setting_key
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    ).fetchone()

    if duplicate is not None:
        raise RuntimeError(
            "Migration 0017 failed: duplicate school setting."
        )

    active_school_count = con.execute(
        """
        SELECT COUNT(*)
        FROM schools
        WHERE active = 1
        """
    ).fetchone()[0]

    settings_count = con.execute(
        """
        SELECT COUNT(*)
        FROM school_settings
        WHERE setting_key = 'date_display_format'
        """
    ).fetchone()[0]

    if settings_count != active_school_count:
        raise RuntimeError(
            "Migration 0017 failed: date setting coverage mismatch."
        )