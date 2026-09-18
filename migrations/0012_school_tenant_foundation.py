from datetime import datetime, timezone


VERSION = "0012"


def _columns(con, table_name):
    return {
        row[1]
        for row in con.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    }


def _table_exists(con, table_name):
    return bool(
        con.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type='table' AND name=?
            """,
            (table_name,),
        ).fetchone()
    )


def _add_column(con, table_name, column_name, definition):
    if column_name not in _columns(con, table_name):
        con.execute(
            f"ALTER TABLE {table_name} ADD COLUMN "
            f"{column_name} {definition}"
        )


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def apply(con):
    """
    Phase 0012 — Brightstars Academics school/tenant foundation.

    Additive and migration-safe.

    Existing student IDs and admission numbers are preserved.
    Application routes are intentionally not changed here.
    """

    now = _utc_now()

    # --------------------------------------------------------
    # 1. SCHOOL / TENANT REGISTRY
    # --------------------------------------------------------

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS schools (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            motto TEXT,
            tagline TEXT,
            address TEXT,
            phone TEXT,
            email TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
        """
    )

    # --------------------------------------------------------
    # 2. NUMBERING POLICY
    # --------------------------------------------------------

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS school_numbering_policies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_id INTEGER NOT NULL UNIQUE,
            label TEXT NOT NULL DEFAULT 'Registration Number',
            prefix TEXT,
            include_year INTEGER NOT NULL DEFAULT 1,
            sequence_start INTEGER NOT NULL DEFAULT 1,
            next_sequence INTEGER NOT NULL DEFAULT 1,
            padding INTEGER NOT NULL DEFAULT 4,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            FOREIGN KEY (school_id) REFERENCES schools(id)
        )
        """
    )

    # --------------------------------------------------------
    # 3. SCHOOL OWNERSHIP
    # --------------------------------------------------------

    school_owned_tables = (
        "students",
        "student_enrolments",
        "parent_accounts",
        "parent_student_links",
        "school_classes",
        "academic_sessions",
    )

    for table_name in school_owned_tables:
        if _table_exists(con, table_name):
            _add_column(
                con,
                table_name,
                "school_id",
                "INTEGER",
            )

    # --------------------------------------------------------
    # 4. STUDENT IDENTITY EXTENSIONS
    # --------------------------------------------------------

    _add_column(
        con,
        "students",
        "legacy_student_id",
        "TEXT",
    )

    _add_column(
        con,
        "students",
        "student_number_source",
        "TEXT NOT NULL DEFAULT 'existing'",
    )

    # --------------------------------------------------------
    # 5. CURRENT SCHOOL PROFILE
    # --------------------------------------------------------

    settings = {}

    if _table_exists(con, "school_public_settings"):
        settings = {
            row[0]: row[1]
            for row in con.execute(
                """
                SELECT setting_key, setting_value
                FROM school_public_settings
                """
            ).fetchall()
        }

    school_name = (
        settings.get("school_name")
        or "Creative Rainbow Montessori School"
    )

    motto = settings.get("school_motto")
    tagline = settings.get("school_tagline")
    address = settings.get("school_address")
    phone = settings.get("school_phone")
    email = settings.get("school_email")

    # Stable code for the current single-school installation.
    school_code = "CRMS"

    # --------------------------------------------------------
    # 6. CREATE / REUSE CURRENT SCHOOL
    # --------------------------------------------------------

    row = con.execute(
        """
        SELECT id
        FROM schools
        WHERE code=?
        """,
        (school_code,),
    ).fetchone()

    if row:
        school_id = row[0]

        con.execute(
            """
            UPDATE schools
            SET name=?,
                motto=?,
                tagline=?,
                address=?,
                phone=?,
                email=?,
                updated_at=?
            WHERE id=?
            """,
            (
                school_name,
                motto,
                tagline,
                address,
                phone,
                email,
                now,
                school_id,
            ),
        )
    else:
        cur = con.execute(
            """
            INSERT INTO schools (
                code,
                name,
                motto,
                tagline,
                address,
                phone,
                email,
                active,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                school_code,
                school_name,
                motto,
                tagline,
                address,
                phone,
                email,
                now,
                now,
            ),
        )

        school_id = cur.lastrowid

    # --------------------------------------------------------
    # 7. CREATE NUMBERING POLICY
    # --------------------------------------------------------

    policy = con.execute(
        """
        SELECT id
        FROM school_numbering_policies
        WHERE school_id=?
        """,
        (school_id,),
    ).fetchone()

    if not policy:
        con.execute(
            """
            INSERT INTO school_numbering_policies (
                school_id,
                label,
                prefix,
                include_year,
                sequence_start,
                next_sequence,
                padding,
                active,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, 1, 1, 1, 4, 1, ?, ?)
            """,
            (
                school_id,
                "Registration Number",
                school_code,
                now,
                now,
            ),
        )

    # --------------------------------------------------------
    # 8. BACKFILL EXISTING SCHOOL RECORDS
    # --------------------------------------------------------

    for table_name in school_owned_tables:
        if not _table_exists(con, table_name):
            continue

        if "school_id" in _columns(con, table_name):
            con.execute(
                f"""
                UPDATE {table_name}
                SET school_id=?
                WHERE school_id IS NULL
                """,
                (school_id,),
            )

    # --------------------------------------------------------
    # 9. PRESERVE EXISTING STUDENT NUMBERS
    # --------------------------------------------------------

    # Existing admission_no values remain untouched.
    # They are the current school's existing registration numbers.
    con.execute(
        """
        UPDATE students
        SET student_number_source='existing'
        WHERE student_number_source IS NULL
           OR student_number_source=''
        """
    )

    # --------------------------------------------------------
    # 10. HARD SAFETY CHECKS
    # --------------------------------------------------------

    total_students = con.execute(
        "SELECT COUNT(*) FROM students"
    ).fetchone()[0]

    assigned_students = con.execute(
        """
        SELECT COUNT(*)
        FROM students
        WHERE school_id=?
        """,
        (school_id,),
    ).fetchone()[0]

    if total_students != 5:
        raise RuntimeError(
            f"0012 SAFETY FAILURE: expected 5 students; "
            f"found {total_students}."
        )

    if assigned_students != total_students:
        raise RuntimeError(
            "0012 SAFETY FAILURE: not every student is assigned "
            "to the initial school."
        )

    duplicate = con.execute(
        """
        SELECT admission_no
        FROM students
        GROUP BY admission_no
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    ).fetchone()

    if duplicate:
        raise RuntimeError(
            "0012 SAFETY FAILURE: duplicate admission number detected."
        )

    if not con.execute(
        """
        SELECT 1
        FROM schools
        WHERE id=? AND code='CRMS'
        """,
        (school_id,),
    ).fetchone():
        raise RuntimeError(
            "0012 SAFETY FAILURE: initial school tenant missing."
        )

    if not con.execute(
        """
        SELECT 1
        FROM school_numbering_policies
        WHERE school_id=?
        """,
        (school_id,),
    ).fetchone():
        raise RuntimeError(
            "0012 SAFETY FAILURE: numbering policy missing."
        )
