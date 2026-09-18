"""
Phase 0013 — Student Number Identity Foundation.

Separates the school-facing student number from the legacy
admission_no field while preserving all existing values.

Design:
- admission_no remains untouched for backward compatibility.
- student_number becomes the canonical school-facing number.
- Existing admission_no values are copied into student_number.
- Uniqueness is scoped to school_id.
- No automatic new-number generation occurs in this migration.
"""

from datetime import datetime, timezone


VERSION = "0013_student_number_identity"


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def apply(con):
    if not _table_exists(con, "students"):
        raise RuntimeError("students table does not exist")

    columns = _columns(con, "students")

    # --------------------------------------------------------
    # 1. ADD CANONICAL SCHOOL STUDENT NUMBER
    # --------------------------------------------------------

    if "student_number" not in columns:
        con.execute(
            "ALTER TABLE students ADD COLUMN student_number TEXT"
        )

    # --------------------------------------------------------
    # 2. PRESERVE EXISTING NUMBERS
    #
    # Never overwrite a populated student_number.
    # Existing admission_no remains untouched.
    # --------------------------------------------------------

    con.execute(
        """
        UPDATE students
        SET student_number = admission_no
        WHERE student_number IS NULL
          AND admission_no IS NOT NULL
          AND TRIM(admission_no) <> ''
        """
    )

    # --------------------------------------------------------
    # 3. SCHOOL-SCOPED UNIQUENESS
    #
    # This deliberately does NOT replace the existing global
    # admission_no uniqueness constraint.
    # It establishes the new canonical number boundary.
    # --------------------------------------------------------

    con.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
        uq_students_school_student_number
        ON students(school_id, student_number)
        WHERE student_number IS NOT NULL
          AND TRIM(student_number) <> ''
        """
    )

    # --------------------------------------------------------
    # 4. INDEX FOR LOOKUPS / MIGRATION MATCHING
    # --------------------------------------------------------

    con.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_students_school_student_number
        ON students(school_id, student_number)
        """
    )

    # --------------------------------------------------------
    # 5. SAFETY ASSERTIONS
    # --------------------------------------------------------

    row = con.execute(
        """
        SELECT COUNT(*)
        FROM students
        WHERE student_number IS NULL
           OR TRIM(student_number) = ''
        """
    ).fetchone()

    if row[0] != 0:
        raise RuntimeError(
            f"Student-number backfill incomplete: {row[0]} student(s) missing"
        )

    duplicate = con.execute(
        """
        SELECT school_id, student_number, COUNT(*) AS c
        FROM students
        WHERE student_number IS NOT NULL
          AND TRIM(student_number) <> ''
        GROUP BY school_id, student_number
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    ).fetchone()

    if duplicate:
        raise RuntimeError(
            "Duplicate school-scoped student number detected: "
            f"school_id={duplicate[0]}, "
            f"student_number={duplicate[1]!r}, "
            f"count={duplicate[2]}"
        )

