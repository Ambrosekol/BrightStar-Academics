"""Official Creative Rainbow receipt fields and print/send support."""
VERSION='0011_official_receipt_design'

def apply(con):
    cols={r['name'] for r in con.execute('PRAGMA table_info(finance_payments)').fetchall()}
    if 'payer_name' not in cols:
        con.execute('ALTER TABLE finance_payments ADD COLUMN payer_name TEXT')
    if 'receipt_notes' not in cols:
        con.execute('ALTER TABLE finance_payments ADD COLUMN receipt_notes TEXT')
    con.execute('CREATE INDEX IF NOT EXISTS idx_finance_payments_payer ON finance_payments(payer_name)')
    con.commit()
