def apply(con):
    con.executescript(
        """
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

        CREATE INDEX IF NOT EXISTS
            idx_student_admission_profiles_student
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

        CREATE INDEX IF NOT EXISTS
            idx_student_admission_contacts_student
        ON student_admission_contacts(student_id);

        CREATE INDEX IF NOT EXISTS
            idx_student_admission_contacts_parent
        ON student_admission_contacts(parent_id);
        """
    )

    tables = {
        row[0]
        for row in con.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type='table'
            """
        )
    }

    required = {
        "student_admission_profiles",
        "student_admission_contacts",
    }

    missing = required - tables

    if missing:
        raise RuntimeError(
            "0019 verification failed; missing tables: "
            + ", ".join(sorted(missing))
        )

    duplicate_profiles = con.execute(
        """
        SELECT student_id, COUNT(*)
        FROM student_admission_profiles
        GROUP BY student_id
        HAVING COUNT(*) > 1
        """
    ).fetchall()

    duplicate_contacts = con.execute(
        """
        SELECT student_id, role, COUNT(*)
        FROM student_admission_contacts
        GROUP BY student_id, role
        HAVING COUNT(*) > 1
        """
    ).fetchall()

    if duplicate_profiles:
        raise RuntimeError(
            "0019 verification failed; duplicate student profiles."
        )

    if duplicate_contacts:
        raise RuntimeError(
            "0019 verification failed; duplicate admission contacts."
        )