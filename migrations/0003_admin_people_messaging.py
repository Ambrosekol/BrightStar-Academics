"""Admin people, multi-role access and direct messaging migration."""
import sqlite3

VERSION='0003_admin_people_messaging'

def apply(con):
    cols={r['name'] for r in con.execute('PRAGMA table_info(admins)').fetchall()}
    for col in ('email','phone','whatsapp','photo_path'):
        if col not in cols:
            con.execute(f'ALTER TABLE admins ADD COLUMN {col} TEXT')
    con.executescript('''
    CREATE TABLE IF NOT EXISTS admin_role_assignments(
        admin_id INTEGER NOT NULL,
        admin_type_id INTEGER NOT NULL,
        assigned_at TEXT NOT NULL,
        assigned_by INTEGER,
        PRIMARY KEY(admin_id,admin_type_id),
        FOREIGN KEY(admin_id) REFERENCES admins(id) ON DELETE CASCADE,
        FOREIGN KEY(admin_type_id) REFERENCES admin_types(id) ON DELETE CASCADE,
        FOREIGN KEY(assigned_by) REFERENCES admins(id)
    );
    CREATE INDEX IF NOT EXISTS idx_admin_role_assignments_admin ON admin_role_assignments(admin_id);
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
    ''')
    
    now='migration'
    rows=con.execute('SELECT id,admin_type_id,created_at FROM admins').fetchall()
    for r in rows:
        con.execute('INSERT OR IGNORE INTO admin_role_assignments(admin_id,admin_type_id,assigned_at) VALUES(?,?,?)',(r['id'],r['admin_type_id'],r['created_at'] or now))

def register(con):
    apply(con)
