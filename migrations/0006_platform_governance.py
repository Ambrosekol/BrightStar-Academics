"""Platform governance, entrance configurations, presence, parents and result workflow."""
VERSION='0006_platform_governance'

def apply(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS entrance_bank_configs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bank_id TEXT NOT NULL,
        entry_group TEXT NOT NULL,
        subject TEXT NOT NULL,
        session_id INTEGER NOT NULL,
        term TEXT NOT NULL DEFAULT 'Full Session',
        questions_to_serve INTEGER NOT NULL,
        marks_per_question REAL NOT NULL,
        active INTEGER NOT NULL DEFAULT 0,
        practice_enabled INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        created_by INTEGER,
        updated_at TEXT NOT NULL,
        updated_by INTEGER,
        UNIQUE(bank_id,entry_group,subject,session_id,term),
        FOREIGN KEY(session_id) REFERENCES academic_sessions(id),
        FOREIGN KEY(created_by) REFERENCES admins(id),
        FOREIGN KEY(updated_by) REFERENCES admins(id)
    );
    CREATE INDEX IF NOT EXISTS idx_entrance_config_scope
      ON entrance_bank_configs(entry_group,subject,session_id,term,active);
    CREATE INDEX IF NOT EXISTS idx_entrance_config_practice
      ON entrance_bank_configs(session_id,practice_enabled,active);

    CREATE TABLE IF NOT EXISTS presence_sessions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_type TEXT NOT NULL,
        account_id INTEGER NOT NULL,
        session_key_hash TEXT NOT NULL UNIQUE,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        user_agent TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_presence_online
      ON presence_sessions(account_type,active,last_seen);
    CREATE INDEX IF NOT EXISTS idx_presence_account
      ON presence_sessions(account_type,account_id,last_seen);

    CREATE TABLE IF NOT EXISTS parent_accounts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        display_name TEXT NOT NULL,
        email TEXT,
        phone TEXT,
        password_hash TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        password_must_change INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        last_login_at TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_parent_email
      ON parent_accounts(email) WHERE email IS NOT NULL AND email <> '';

    CREATE TABLE IF NOT EXISTS parent_student_links(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        parent_id INTEGER NOT NULL,
        student_id INTEGER NOT NULL,
        relationship TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        created_by INTEGER,
        UNIQUE(parent_id,student_id),
        FOREIGN KEY(parent_id) REFERENCES parent_accounts(id) ON DELETE CASCADE,
        FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE,
        FOREIGN KEY(created_by) REFERENCES admins(id)
    );
    CREATE INDEX IF NOT EXISTS idx_parent_links_parent
      ON parent_student_links(parent_id,active);
    CREATE INDEX IF NOT EXISTS idx_parent_links_student
      ON parent_student_links(student_id,active);

    CREATE TABLE IF NOT EXISTS result_workflow_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        result_id INTEGER NOT NULL,
        from_status TEXT,
        to_status TEXT NOT NULL,
        actor_admin_id INTEGER,
        reason TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(result_id) REFERENCES school_student_results(id) ON DELETE CASCADE,
        FOREIGN KEY(actor_admin_id) REFERENCES admins(id)
    );
    CREATE INDEX IF NOT EXISTS idx_result_workflow_result
      ON result_workflow_events(result_id,created_at);

    """)
    paper_cols={r['name'] for r in con.execute('PRAGMA table_info(candidate_papers)').fetchall()}
    if 'config_id' not in paper_cols:
        con.execute("ALTER TABLE candidate_papers ADD COLUMN config_id INTEGER")
    con.execute("CREATE INDEX IF NOT EXISTS idx_candidate_papers_config ON candidate_papers(config_id)")
    result_cols={r['name'] for r in con.execute('PRAGMA table_info(school_student_results)').fetchall()}
    additions={
        'source_type': "TEXT NOT NULL DEFAULT 'cbt'",
        'component_name': "TEXT",
        'entered_by': "INTEGER",
        'verified_by': "INTEGER",
        'approved_by': "INTEGER",
        'released_at': "TEXT",
        'override_reason': "TEXT",
        'updated_at': "TEXT",
        'updated_by': "INTEGER",
    }
    for col,ddl in additions.items():
        if col not in result_cols:
            con.execute(f"ALTER TABLE school_student_results ADD COLUMN {col} {ddl}")
    con.execute("CREATE INDEX IF NOT EXISTS idx_school_results_workflow ON school_student_results(status,session_id,student_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_school_results_source ON school_student_results(source_type,student_id,session_id)")
    # Existing pending CBT records were already part of the prior approved release pipeline.
    con.execute("UPDATE school_student_results SET status='approved' WHERE status='pending'")

    # Backfill entrance configurations from the existing verified production banks.
    current=con.execute("SELECT id FROM academic_sessions WHERE is_current=1 AND active=1 ORDER BY id DESC LIMIT 1").fetchone()
    if current:
        import json, os
        base=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        data_dir=os.path.join(base,'data')
        now=__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
        for fn in os.listdir(data_dir):
            if not fn.endswith('.json') or fn=='manifest.json':
                continue
            try:
                with open(os.path.join(data_dir,fn),encoding='utf-8') as f: b=json.load(f)
            except Exception:
                continue
            if not isinstance(b,dict) or not b.get('id') or not isinstance(b.get('questions'),list) or not b.get('questions'):
                continue
            value=' '.join(str(b.get(k,'')) for k in ('subject','name','id')).lower()
            subject=('general_knowledge' if ('general knowledge' in value or 'general_knowledge' in value or 'generalknowledge' in value)
                     else 'mathematics' if 'mathemat' in value else 'english' if 'english' in value else None)
            level=' '.join(str(b.get(k,'')) for k in ('level','name','id')).lower()
            group=('year7' if any(x in level for x in ('jss1','year7')) else
                   'year10' if any(x in level for x in ('sss1','ss1','year10')) else None)
            if not subject:
                continue
            groups=[group] if group else (['year7','year10'] if subject=='general_knowledge' else [])
            count=len(b['questions'])
            marks=round(100/count,2)
            if count and round(marks*count,2)==100:
                for g in groups:
                    con.execute("""INSERT OR IGNORE INTO entrance_bank_configs
                      (bank_id,entry_group,subject,session_id,term,questions_to_serve,marks_per_question,active,practice_enabled,created_at,updated_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                      (b['id'],g,subject,current['id'],'Full Session',count,marks,1,0,now,now))
    # Attach existing candidate papers to the matching current configuration when possible.
    if current:
        papers=con.execute("SELECT id,candidate_id,bank_id FROM candidate_papers WHERE config_id IS NULL").fetchall()
        for paper in papers:
            cand=con.execute("SELECT target_class FROM candidates WHERE id=?",(paper['candidate_id'],)).fetchone()
            target=(cand['target_class'] if cand else '').lower()
            group='year7' if ('jss 1' in target or 'year 7' in target) else 'year10'
            cfg=con.execute("SELECT id FROM entrance_bank_configs WHERE bank_id=? AND entry_group=? AND session_id=? AND active=1 ORDER BY id DESC LIMIT 1",(paper['bank_id'],group,current['id'])).fetchone()
            if cfg:
                con.execute("UPDATE candidate_papers SET config_id=? WHERE id=?",(cfg['id'],paper['id']))
    con.commit()

def register(con):
    apply(con)
