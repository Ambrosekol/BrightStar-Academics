"""Versioned SQLite migration runner for Crainbow."""
from datetime import datetime, timezone

MIGRATIONS = [
    ("0002_security_assessment_exam_integrity", """
        CREATE TABLE IF NOT EXISTS attempt_questions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            question_order INTEGER NOT NULL,
            question_text TEXT NOT NULL,
            options_json TEXT NOT NULL,
            instruction TEXT,
            image_path TEXT,
            correct_option INTEGER NOT NULL,
            points INTEGER NOT NULL DEFAULT 1,
            UNIQUE(attempt_id, question_id),
            UNIQUE(attempt_id, question_order),
            FOREIGN KEY(attempt_id) REFERENCES attempts(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_attempt_questions_attempt
            ON attempt_questions(attempt_id, question_order);

        CREATE TABLE IF NOT EXISTS school_assessment_attempts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            assessment_id INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            submitted_at TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            score INTEGER,
            max_score INTEGER,
            percentage REAL,
            UNIQUE(student_id, assessment_id),
            FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE,
            FOREIGN KEY(assessment_id) REFERENCES school_assessments(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_school_assessment_attempt_student
            ON school_assessment_attempts(student_id, status);
        CREATE INDEX IF NOT EXISTS idx_school_assessment_attempt_assessment
            ON school_assessment_attempts(assessment_id, status);

        CREATE TABLE IF NOT EXISTS school_assessment_attempt_questions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            question_order INTEGER NOT NULL,
            question_text TEXT NOT NULL,
            option_a TEXT,
            option_b TEXT,
            option_c TEXT,
            option_d TEXT,
            instruction TEXT,
            image_path TEXT,
            correct_option INTEGER NOT NULL,
            points INTEGER NOT NULL DEFAULT 1,
            UNIQUE(attempt_id, question_id),
            UNIQUE(attempt_id, question_order),
            FOREIGN KEY(attempt_id) REFERENCES school_assessment_attempts(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_school_assessment_attempt_questions
            ON school_assessment_attempt_questions(attempt_id, question_order);

        CREATE TABLE IF NOT EXISTS school_assessment_answers(
            attempt_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            option_index INTEGER,
            answered_at TEXT NOT NULL,
            PRIMARY KEY(attempt_id, question_id),
            FOREIGN KEY(attempt_id) REFERENCES school_assessment_attempts(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS security_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            identifier_hash TEXT,
            ip_address TEXT,
            user_agent TEXT,
            details TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_security_events_type_time
            ON security_events(event_type, created_at);
    """),
    ("0003_admin_people_messaging", """
    """),
    ("0004_governance_result_release", """
    """),
    ("0005_admin_auth_scope_messaging", """
    """),
    ("0006_platform_governance", """
    """),
    ("0007_school_academic_work", """
    """),
    ("0008_school_management_finance_library", """
    """),
    ("0009_public_environment_password_recovery", """
    """),
    ("0010_school_master_data_and_corrections", """
    """),
    # This entry's SQL was accidentally written as a tuple containing the 0017
    # entry, which made apply_migrations() raise AttributeError on any database
    # that had not already recorded 0011. 0017 has its own entry below, and the
    # columns 0011 introduced are declared in models.py, so an empty body is
    # both correct and what the surrounding entries already do.
    ("0011_official_receipt_design", "\n    "),
    ("0012_school_tenant_foundation", "\n    "),
    ("0013_student_number_identity", "\n    "),
    ("0014_student_number_generation", "\n    "),
    ("0017_school_settings_foundation", ""),
    ("0019_complete_admissions", """
    CREATE TABLE IF NOT EXISTS student_admission_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL UNIQUE,
        previous_school TEXT,
        reason_for_leaving TEXT,
        religion TEXT,
        denomination TEXT,
        blood_group TEXT,
        genotype TEXT,
        convulsion_history TEXT,
        asthma_history TEXT,
        medical_frequency TEXT,
        medical_treatment TEXT,
        immunization TEXT,
        food_allergies TEXT,
        drug_allergies TEXT,
        other_health_challenges TEXT,
        disability TEXT,
        disability_indication TEXT,
        parent_signature TEXT,
        parent_signature_date TEXT,
        updated_at TEXT,
        updated_by INTEGER,
        FOREIGN KEY(student_id) REFERENCES students(id)
    );
    CREATE INDEX IF NOT EXISTS idx_student_admission_profiles_student
        ON student_admission_profiles(student_id);

    CREATE TABLE IF NOT EXISTS student_admission_contacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL,
        parent_id INTEGER,
        role TEXT NOT NULL,
        name TEXT,
        address TEXT,
        office_phone TEXT,
        mobile TEXT,
        email TEXT,
        occupation TEXT,
        created_at TEXT,
        updated_at TEXT,
        updated_by INTEGER,
        FOREIGN KEY(student_id) REFERENCES students(id),
        FOREIGN KEY(parent_id) REFERENCES parent_accounts(id),
        UNIQUE(student_id, role)
    );

    CREATE INDEX IF NOT EXISTS idx_student_admission_contacts_student
        ON student_admission_contacts(student_id);

    CREATE INDEX IF NOT EXISTS idx_student_admission_contacts_parent
        ON student_admission_contacts(parent_id);
    """),
]

def apply_migrations(con):
    con.execute("""CREATE TABLE IF NOT EXISTS schema_migrations(
        version TEXT PRIMARY KEY,
        applied_at TEXT NOT NULL
    )""")
    applied={r[0] for r in con.execute("SELECT version FROM schema_migrations").fetchall()}
    for version, sql in MIGRATIONS:
        if version in applied:
            continue
        if version in (
               '0012_school_tenant_foundation',
               '0013_student_number_identity',
               '0014_student_number_generation',
               '0017_school_settings_foundation',
               '0019_complete_admissions',
           ):
            import importlib
            importlib.import_module("migrations." + version).apply(con)
        elif sql.strip():
            con.executescript(sql)
        con.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?,?)",
                    (version, datetime.now(timezone.utc).isoformat()))
    con.commit()
