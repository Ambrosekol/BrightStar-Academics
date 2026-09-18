from datetime import datetime, timezone

VERSION='0009_public_environment_password_recovery'

def apply(con):
    con.executescript('''
    CREATE TABLE IF NOT EXISTS school_public_settings(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        setting_key TEXT UNIQUE NOT NULL,
        setting_value TEXT,
        updated_at TEXT NOT NULL,
        updated_by INTEGER,
        FOREIGN KEY(updated_by) REFERENCES admins(id)
    );
    CREATE TABLE IF NOT EXISTS school_public_pages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE NOT NULL,
        title TEXT NOT NULL,
        content TEXT,
        published INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL,
        updated_by INTEGER,
        FOREIGN KEY(updated_by) REFERENCES admins(id)
    );
    CREATE TABLE IF NOT EXISTS school_public_news(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE NOT NULL,
        title TEXT NOT NULL,
        excerpt TEXT,
        body TEXT,
        published INTEGER NOT NULL DEFAULT 0,
        published_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        created_by INTEGER,
        updated_by INTEGER,
        FOREIGN KEY(created_by) REFERENCES admins(id),
        FOREIGN KEY(updated_by) REFERENCES admins(id)
    );
    CREATE INDEX IF NOT EXISTS idx_school_public_news_published ON school_public_news(published,published_at DESC);
    CREATE TABLE IF NOT EXISTS school_public_enquiries(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT,
        phone TEXT,
        subject TEXT,
        message TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'new',
        created_at TEXT NOT NULL,
        handled_by INTEGER,
        handled_at TEXT,
        FOREIGN KEY(handled_by) REFERENCES admins(id)
    );
    CREATE INDEX IF NOT EXISTS idx_school_public_enquiries_status ON school_public_enquiries(status,created_at DESC);
    CREATE TABLE IF NOT EXISTS password_reset_tokens(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_type TEXT NOT NULL,
        account_id INTEGER NOT NULL,
        token_hash TEXT UNIQUE NOT NULL,
        expires_at TEXT NOT NULL,
        used_at TEXT,
        created_at TEXT NOT NULL,
        requested_ip TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_password_reset_lookup ON password_reset_tokens(account_type,account_id,expires_at,used_at);
    ''')
    now=datetime.now(timezone.utc).isoformat()
    defaults={
        'school_name':'Creative Rainbow Montessori School',
        'school_motto':'Growing in humility and fear of God',
        'school_tagline':'Growing in humility and fear of God',
        'school_phone':'',
        'school_email':'',
        'school_address':'',
        'homepage_headline':'Welcome to Creative Rainbow Montessori School',
        'homepage_intro':'A caring learning environment where every child is encouraged to grow, learn and flourish.',
        'homepage_cta':'Explore Our School',
    }
    for key,value in defaults.items():
        con.execute('INSERT OR IGNORE INTO school_public_settings(setting_key,setting_value,updated_at) VALUES(?,?,?)',(key,value,now))
    pages={
        'about':('About Us','Creative Rainbow Montessori School is committed to providing a nurturing, disciplined and engaging environment for children to learn and grow. This page can be updated by authorised school staff.'),
        'admissions':('Admissions','Admission information, requirements, fees and important instructions for parents can be published here. The school can update this page whenever its admission process changes.'),
    }
    for slug,(title,content) in pages.items():
        con.execute('INSERT OR IGNORE INTO school_public_pages(slug,title,content,published,updated_at) VALUES(?,?,?,?,?)',(slug,title,content,1,now))
