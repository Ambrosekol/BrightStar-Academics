"""Phase 3E governance metadata and scheduled school-result release."""

def apply(con):
    session_cols={r['name'] for r in con.execute('PRAGMA table_info(academic_sessions)').fetchall()}
    if 'result_release_at' not in session_cols:
        con.execute('ALTER TABLE academic_sessions ADD COLUMN result_release_at TEXT')
    assessment_cols={r['name'] for r in con.execute('PRAGMA table_info(school_assessments)').fetchall()}
    if 'term' not in assessment_cols:
        con.execute('ALTER TABLE school_assessments ADD COLUMN term TEXT')
    notification_cols={r['name'] for r in con.execute('PRAGMA table_info(admin_notifications)').fetchall()}
    if 'actor_admin_id' not in notification_cols:
        con.execute('ALTER TABLE admin_notifications ADD COLUMN actor_admin_id INTEGER')
    if 'actor_username_snapshot' not in notification_cols:
        con.execute('ALTER TABLE admin_notifications ADD COLUMN actor_username_snapshot TEXT')
    if 'actor_display_name_snapshot' not in notification_cols:
        con.execute('ALTER TABLE admin_notifications ADD COLUMN actor_display_name_snapshot TEXT')

def register(con):
    apply(con)
