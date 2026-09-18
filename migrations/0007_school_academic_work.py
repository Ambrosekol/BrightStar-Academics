"""School subject governance, rich assignments/projects, notifications and parent feedback."""
VERSION='0007_school_academic_work'

def apply(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS school_notifications(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recipient_type TEXT NOT NULL,
        recipient_id INTEGER NOT NULL,
        student_id INTEGER,
        category TEXT NOT NULL,
        title TEXT NOT NULL,
        message TEXT NOT NULL,
        action_url TEXT,
        created_at TEXT NOT NULL,
        read_at TEXT,
        created_by INTEGER,
        FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE,
        FOREIGN KEY(created_by) REFERENCES admins(id)
    );
    CREATE INDEX IF NOT EXISTS idx_school_notifications_recipient ON school_notifications(recipient_type,recipient_id,read_at,created_at DESC);

    CREATE TABLE IF NOT EXISTS assignment_questions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        assignment_id INTEGER NOT NULL,
        question_text TEXT NOT NULL,
        instruction TEXT,
        image_path TEXT,
        option_a TEXT NOT NULL,
        option_b TEXT NOT NULL,
        option_c TEXT NOT NULL,
        option_d TEXT NOT NULL,
        correct_option INTEGER NOT NULL,
        points REAL NOT NULL DEFAULT 1,
        sort_order INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY(assignment_id) REFERENCES school_assignments(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_assignment_questions_assignment ON assignment_questions(assignment_id,sort_order);

    CREATE TABLE IF NOT EXISTS school_assignment_attempts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        assignment_id INTEGER NOT NULL,
        student_id INTEGER NOT NULL,
        started_at TEXT NOT NULL,
        expires_at TEXT,
        submitted_at TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        score REAL,
        max_score REAL,
        percentage REAL,
        UNIQUE(assignment_id,student_id),
        FOREIGN KEY(assignment_id) REFERENCES school_assignments(id) ON DELETE CASCADE,
        FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS school_assignment_attempt_questions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        attempt_id INTEGER NOT NULL,
        question_id INTEGER NOT NULL,
        question_order INTEGER NOT NULL,
        question_text TEXT NOT NULL,
        option_a TEXT NOT NULL,
        option_b TEXT NOT NULL,
        option_c TEXT NOT NULL,
        option_d TEXT NOT NULL,
        instruction TEXT,
        image_path TEXT,
        correct_option INTEGER NOT NULL,
        points REAL NOT NULL DEFAULT 1,
        UNIQUE(attempt_id,question_id),
        UNIQUE(attempt_id,question_order),
        FOREIGN KEY(attempt_id) REFERENCES school_assignment_attempts(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS school_assignment_answers(
        attempt_id INTEGER NOT NULL,
        question_id INTEGER NOT NULL,
        option_index INTEGER,
        answered_at TEXT NOT NULL,
        PRIMARY KEY(attempt_id,question_id),
        FOREIGN KEY(attempt_id) REFERENCES school_assignment_attempts(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS school_projects(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        instructions TEXT,
        class_id INTEGER NOT NULL,
        subject_id INTEGER NOT NULL,
        date_given TEXT NOT NULL,
        due_date TEXT,
        max_score REAL,
        created_by INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY(class_id) REFERENCES school_classes(id),
        FOREIGN KEY(subject_id) REFERENCES school_subjects(id),
        FOREIGN KEY(created_by) REFERENCES admins(id)
    );
    CREATE TABLE IF NOT EXISTS project_students(
        project_id INTEGER NOT NULL,
        student_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'not_done',
        score REAL,
        remark TEXT,
        graded_by INTEGER,
        graded_at TEXT,
        PRIMARY KEY(project_id,student_id),
        FOREIGN KEY(project_id) REFERENCES school_projects(id) ON DELETE CASCADE,
        FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE,
        FOREIGN KEY(graded_by) REFERENCES admins(id)
    );
    CREATE INDEX IF NOT EXISTS idx_school_projects_class_subject ON school_projects(class_id,subject_id,active);
    CREATE INDEX IF NOT EXISTS idx_project_students_student ON project_students(student_id,status);

    CREATE TABLE IF NOT EXISTS parent_feedback(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        parent_id INTEGER NOT NULL,
        student_id INTEGER,
        subject TEXT NOT NULL,
        body TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        assigned_admin_id INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        closed_at TEXT,
        FOREIGN KEY(parent_id) REFERENCES parent_accounts(id) ON DELETE CASCADE,
        FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE SET NULL,
        FOREIGN KEY(assigned_admin_id) REFERENCES admins(id)
    );
    CREATE INDEX IF NOT EXISTS idx_parent_feedback_status ON parent_feedback(status,created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_parent_feedback_parent ON parent_feedback(parent_id,created_at DESC);
    CREATE TABLE IF NOT EXISTS parent_feedback_replies(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        feedback_id INTEGER NOT NULL,
        admin_id INTEGER NOT NULL,
        body TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(feedback_id) REFERENCES parent_feedback(id) ON DELETE CASCADE,
        FOREIGN KEY(admin_id) REFERENCES admins(id)
    );
    CREATE INDEX IF NOT EXISTS idx_parent_feedback_replies_feedback ON parent_feedback_replies(feedback_id,created_at);
    """)
    # Subject locks: ordinary lock and irreversible Super Admin final lock.
    cols={r['name'] for r in con.execute('PRAGMA table_info(class_subjects)').fetchall()}
    for col,ddl in {
        'final_locked': 'INTEGER NOT NULL DEFAULT 0',
        'final_locked_by': 'INTEGER',
        'final_locked_at': 'TEXT'
    }.items():
        if col not in cols:
            con.execute(f'ALTER TABLE class_subjects ADD COLUMN {col} {ddl}')
    # Rich assignment fields.
    cols={r['name'] for r in con.execute('PRAGMA table_info(school_assignments)').fetchall()}
    additions={
        'assignment_type': "TEXT NOT NULL DEFAULT 'written'",
        'date_given': 'TEXT',
        'timing_mode': "TEXT NOT NULL DEFAULT 'untimed'",
        'time_limit_seconds': 'INTEGER',
        'per_question_seconds': 'INTEGER',
        'max_score': 'REAL'
    }
    for col,ddl in additions.items():
        if col not in cols:
            con.execute(f'ALTER TABLE school_assignments ADD COLUMN {col} {ddl}')
    con.execute("UPDATE school_assignments SET date_given=substr(created_at,1,10) WHERE date_given IS NULL OR date_given='' ")
    # Per-student assignment record fields.
    cols={r['name'] for r in con.execute('PRAGMA table_info(assignment_students)').fetchall()}
    additions={
        'status': "TEXT NOT NULL DEFAULT 'undone'",
        'score': 'REAL',
        'max_score': 'REAL',
        'remark': 'TEXT',
        'started_at': 'TEXT',
        'submitted_at': 'TEXT',
        'graded_at': 'TEXT',
        'graded_by': 'INTEGER'
    }
    for col,ddl in additions.items():
        if col not in cols:
            con.execute(f'ALTER TABLE assignment_students ADD COLUMN {col} {ddl}')
    con.execute("UPDATE assignment_students SET status='undone' WHERE status IS NULL OR status='' ")
    con.execute('CREATE INDEX IF NOT EXISTS idx_assignment_students_status ON assignment_students(student_id,status)')
    con.commit()

def register(con):
    apply(con)
