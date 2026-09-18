"""School master data, editable fee catalogue, correction controls and early-years structure."""
VERSION='0010_school_master_data_and_corrections'

def apply(con):
    cols={r['name'] for r in con.execute('PRAGMA table_info(students)').fetchall()}
    for col in ('blood_group','genotype'):
        if col not in cols:
            con.execute(f'ALTER TABLE students ADD COLUMN {col} TEXT')

    now=__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
    early_years=[
        ('Daycare','Daycare',-10,0,1), ('Toddler 1','Playgroup',-9,0,1),
        ('Toddler 2','Playgroup',-8,0,1), ('Junior Reception','Nursery',-7,0,1),
        ('Middle Reception','Nursery',-6,0,1), ('Senior Reception','Nursery',-5,0,1),
    ]
    for name,stage,order,optional,active in early_years:
        con.execute('INSERT OR IGNORE INTO school_classes(name,stage,level_order,optional,active,created_at) VALUES(?,?,?,?,?,?)',
                    (name,stage,order,optional,active,now))

    con.execute("""CREATE TABLE IF NOT EXISTS finance_fee_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'School Fees',
        stage TEXT NOT NULL DEFAULT 'All',
        amount REAL NOT NULL,
        applicability TEXT NOT NULL DEFAULT 'Annual',
        required INTEGER NOT NULL DEFAULT 1,
        optional INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        effective_from TEXT,
        effective_to TEXT,
        notes TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        created_by INTEGER,
        updated_by INTEGER,
        FOREIGN KEY(created_by) REFERENCES admins(id),
        FOREIGN KEY(updated_by) REFERENCES admins(id)
    )""")
    con.execute('CREATE INDEX IF NOT EXISTS idx_finance_fee_items_stage_active ON finance_fee_items(stage,active,effective_from)')
    acols={r['name'] for r in con.execute('PRAGMA table_info(finance_fee_assessments)').fetchall()}
    if 'fee_item_id' not in acols:
        con.execute('ALTER TABLE finance_fee_assessments ADD COLUMN fee_item_id INTEGER')
    if 'term' not in acols:
        con.execute("ALTER TABLE finance_fee_assessments ADD COLUMN term TEXT DEFAULT 'Full Session'")
    if 'voided_at' not in acols:
        con.execute('ALTER TABLE finance_fee_assessments ADD COLUMN voided_at TEXT')
    pcols={r['name'] for r in con.execute('PRAGMA table_info(finance_payments)').fetchall()}
    for col,ddl in {'voided_at':'TEXT','voided_by':'INTEGER','void_reason':'TEXT','correction_of_payment_id':'INTEGER'}.items():
        if col not in pcols:
            con.execute(f'ALTER TABLE finance_payments ADD COLUMN {col} {ddl}')
    con.execute('CREATE INDEX IF NOT EXISTS idx_finance_payments_status ON finance_payments(status,paid_at)')
    con.execute('CREATE INDEX IF NOT EXISTS idx_finance_payments_correction ON finance_payments(correction_of_payment_id)')

    hcols={r['name'] for r in con.execute('PRAGMA table_info(student_enrollment_history)').fetchall()}
    for col,ddl in {'corrected_at':'TEXT','corrected_by':'INTEGER','correction_reason':'TEXT'}.items():
        if col not in hcols:
            con.execute(f'ALTER TABLE student_enrollment_history ADD COLUMN {col} {ddl}')

    if not con.execute('SELECT 1 FROM finance_fee_items LIMIT 1').fetchone():
        items=[
            ('Tuition Fee','Tuition','Nursery',75000,'Annual',1,0),
            ('Maintenance Fee','Maintenance','Nursery',50000,'Annual',1,0),
            ('Diction','Diction','Nursery',5000,'Annual',1,0),
            ('Christmas Party Fee','Activity','Nursery',5000,'Annual',1,0),
            ('Social Activities Fees','Activity','Nursery',20000,'Second and third term only',1,0),
            ('Registration Fee','Registration','Nursery',5000,'One-time',1,0),
            ('School Uniforms (a pair)','Uniform','Nursery',26000,'One-time',1,0),
            ('Sportswear','Sports','Nursery',15000,'One-time',1,0),
            ('Cardigan','Uniform','Nursery',9000,'One-time',1,0),
            ('Stationery','Stationery','Nursery',60000,'One-time',1,0),
            ('Tuition Fee','Tuition','Primary',80000,'Annual',1,0),
            ('Maintenance Fee','Maintenance','Primary',50000,'Annual',1,0),
            ('Diction','Diction','Primary',10000,'Annual',1,0),
            ('Christmas Party Fees','Activity','Primary',5000,'Annual',1,0),
            ('Social Activities Fees','Activity','Primary',20000,'Second and third term only',1,0),
            ('Registration Fee','Registration','Primary',5000,'One-time',1,0),
            ('School Uniform (a pair)','Uniform','Primary',28000,'One-time',1,0),
            ('Sports Wear','Sports','Primary',15000,'One-time',1,0),
            ('Cardigan','Uniform','Primary',10000,'One-time',1,0),
            ('Stationery','Stationery','Primary',70000,'One-time',1,0),
            ('Transport: Monkey Village (To & Fro)','Transport','All',75000,'Optional',0,1),
            ('Transport: Outside Monkey Village (To & Fro)','Transport','All',85000,'Optional',0,1),
            ('Transport: Monkey Village (Only To or Fro)','Transport','All',60000,'Optional',0,1),
            ('Transport: Outside Monkey Village (Only To or Fro)','Transport','All',70000,'Optional',0,1),
            ('Ballet','Club','All',20000,'Optional',0,1),
            ('Football Club','Club','All',25000,'Optional',0,1),
            ('Music Club','Club','All',20000,'Optional',0,1),
        ]
        for name,category,stage,amount,applicability,required,optional in items:
            con.execute('INSERT INTO finance_fee_items(name,category,stage,amount,applicability,required,optional,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,1,?,?)',
                        (name,category,stage,amount,applicability,required,optional,now,now))
    con.commit()
