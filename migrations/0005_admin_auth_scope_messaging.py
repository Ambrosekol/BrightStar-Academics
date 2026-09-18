"""Administrator authentication, composite scope and messaging hardening."""
VERSION='0005_admin_auth_scope_messaging'

def apply(con):
    cols={r['name'] for r in con.execute('PRAGMA table_info(admins)').fetchall()}
    if 'password_must_change' not in cols:
        con.execute("ALTER TABLE admins ADD COLUMN password_must_change INTEGER NOT NULL DEFAULT 0")
    con.execute("UPDATE admins SET password_must_change=0 WHERE password_must_change IS NULL")
    
    
    con.executescript('''
    CREATE TABLE IF NOT EXISTS admin_messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_admin_id INTEGER NOT NULL,
        recipient_admin_id INTEGER NOT NULL,
        body TEXT NOT NULL,
        sent_at TEXT NOT NULL,
        read_at TEXT,
        FOREIGN KEY(sender_admin_id) REFERENCES admins(id) ON DELETE CASCADE,
        FOREIGN KEY(recipient_admin_id) REFERENCES admins(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_admin_messages_recipient ON admin_messages(recipient_admin_id,read_at,sent_at DESC);
    CREATE INDEX IF NOT EXISTS idx_admin_messages_thread ON admin_messages(sender_admin_id,recipient_admin_id,sent_at DESC);
    CREATE INDEX IF NOT EXISTS idx_admin_scopes_lookup ON admin_scopes(admin_id,scope_type,scope_value);
    ''')

def register(con):
    apply(con)
