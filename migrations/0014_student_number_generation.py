from datetime import datetime, timezone


VERSION = "0014_student_number_generation"


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _table_exists(con, name):
    row = con.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type='table'
          AND name=?
        """,
        (name,),
    ).fetchone()

    return row is not None


def apply(con):
    # --------------------------------------------------------
    # Required Phase 0012/0013 foundations.
    # --------------------------------------------------------

    if not _table_exists(con, "schools"):
        raise RuntimeError(
            "0014 requires schools table."
        )

    if not _table_exists(con, "school_numbering_policies"):
        raise RuntimeError(
            "0014 requires school_numbering_policies table."
        )

    if not _table_exists(con, "students"):
        raise RuntimeError(
            "0014 requires students table."
        )

    # --------------------------------------------------------
    # Allocation ledger.
    #
    # This records every school student number allocation.
    # Existing migrated numbers are marked "existing".
    # Future automatically generated numbers will be marked
    # "generated".
    #
    # This table does NOT replace students.student_number.
    # It provides an auditable allocation history.
    # --------------------------------------------------------

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS student_number_allocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            school_id INTEGER NOT NULL,

            student_number TEXT NOT NULL,

            sequence_number INTEGER,

            allocation_year INTEGER,

            source TEXT NOT NULL DEFAULT 'generated',

            student_id INTEGER,

            allocated_at TEXT NOT NULL,

            allocated_by TEXT,

            active INTEGER NOT NULL DEFAULT 1,

            FOREIGN KEY (school_id)
                REFERENCES schools(id)
                ON DELETE RESTRICT,

            FOREIGN KEY (student_id)
                REFERENCES students(id)
                ON DELETE SET NULL
        )
        """
    )

    # --------------------------------------------------------
    # A school cannot allocate the same student number twice.
    # --------------------------------------------------------

    con.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
        uq_student_number_allocations_school_number
        ON student_number_allocations(
            school_id,
            student_number
        )
        WHERE TRIM(student_number) <> ''
        """
    )

    con.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_student_number_allocations_school
        ON student_number_allocations(school_id)
        """
    )

    con.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_student_number_allocations_student
        ON student_number_allocations(student_id)
        """
    )

    con.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_student_number_allocations_sequence
        ON student_number_allocations(
            school_id,
            sequence_number
        )
        """
    )

    # --------------------------------------------------------
    # Preserve existing student numbers in the allocation
    # ledger.
    #
    # IMPORTANT:
    # This does NOT generate numbers.
    # This does NOT change students.
    # It only records the already-existing identities.
    # --------------------------------------------------------

    con.execute(
        """
        INSERT OR IGNORE INTO student_number_allocations (
            school_id,
            student_number,
            sequence_number,
            allocation_year,
            source,
            student_id,
            allocated_at,
            allocated_by,
            active
        )
        SELECT
            s.school_id,
            s.student_number,
            NULL,
            NULL,
            COALESCE(
                NULLIF(
                    TRIM(s.student_number_source),
                    ''
                ),
                'existing'
            ),
            s.id,
            COALESCE(
                s.created_at,
                ?
            ),
            '0014_migration',
            CASE
                WHEN s.active = 1 THEN 1
                ELSE 0
            END
        FROM students s
        WHERE s.school_id IS NOT NULL
          AND s.student_number IS NOT NULL
          AND TRIM(s.student_number) <> ''
        """,
        (_utc_now(),),
    )

    # --------------------------------------------------------
    # Hard safety checks inside migration.
    # --------------------------------------------------------

    duplicate = con.execute(
        """
        SELECT
            school_id,
            student_number,
            COUNT(*) AS allocation_count
        FROM student_number_allocations
        WHERE TRIM(student_number) <> ''
        GROUP BY
            school_id,
            student_number
        HAVING COUNT(*) > 1
        """
    ).fetchone()

    if duplicate:
        raise RuntimeError(
            "0014 safety failure: duplicate school-scoped "
            "student-number allocation."
        )

    student_count = con.execute(
        """
        SELECT COUNT(*)
        FROM students
        WHERE student_number IS NOT NULL
          AND TRIM(student_number) <> ''
        """
    ).fetchone()[0]

    allocation_count = con.execute(
        """
        SELECT COUNT(*)
        FROM student_number_allocations
        """
    ).fetchone()[0]

    if allocation_count < student_count:
        raise RuntimeError(
            "0014 safety failure: allocation ledger does not "
            "cover all existing student numbers."
        )