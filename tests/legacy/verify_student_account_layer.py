from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parent
APP = (ROOT / 'app.py').read_text(encoding='utf-8')

ast.parse(APP)

required = [
    "login_username TEXT",
    "login_password_hash TEXT",
    "account_active INTEGER NOT NULL DEFAULT 1",
    "password_must_change INTEGER NOT NULL DEFAULT 1",
    "last_login_at TEXT",
    "def _provision_student_account",
    "def student_password_change",
    "def student_required",
    "student_id",
    "return 'candidate',candidate",
    "return 'student',student",
    "school_student_account_reset",
    "school_student_account_toggle",
]
for token in required:
    assert token in APP, f'missing student-account boundary token: {token}'

# Candidate authentication remains present as its own branch and session key.
assert "candidate=con.execute('SELECT * FROM candidates WHERE candidate_code=? AND active=1'" in APP
assert "session['candidate_id']=account['id']" in APP

# New student registration must provision credentials in the same transaction.
new_route_start = APP.index("def admin_school_student_new():")
new_route_end = APP.index("@app.route('/admin/school/students/<int:sid>/edit'", new_route_start)
new_route = APP[new_route_start:new_route_end]
assert '_provision_student_account(con,sid,admission)' in new_route
assert 'login_username=login_username' in new_route
assert 'temporary_password=temporary_password' in new_route

print('PASS: Phase 6K.4 student account static boundary checks')
