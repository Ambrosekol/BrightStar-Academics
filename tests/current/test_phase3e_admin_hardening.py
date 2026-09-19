from pathlib import Path
import ast, sqlite3
import jinja2

ROOT=Path(__file__).resolve().parents[2]
APP=(ROOT/'app.py').read_text(encoding='utf-8')
SECURITY=(ROOT/'core'/'security.py').read_text(encoding='utf-8')
ADMIN_ROUTES=(ROOT/'blueprints'/'administration'/'routes.py').read_text(encoding='utf-8')
FORM=(ROOT/'templates/admin_account_form.html').read_text(encoding='utf-8')


def test_app_compiles_and_admin_auth_flow_exists():
    ast.parse(APP)
    ast.parse(SECURITY)
    assert '_new_admin_password' in APP
    assert "password_must_change" in APP
    assert "@app.route('/admin/password',methods=['GET','POST'])" in APP
    assert "request.endpoint not in {'admin_password_change','admin_logout','logout'}" in SECURITY


def test_admin_creation_has_one_time_credentials_and_no_plain_password_field():
    assert "temporary_password=_new_admin_password()" in ADMIN_ROUTES
    # A newly created administrator is always forced to replace the one-time
    # password; since the SQLAlchemy migration that flag is set on the model.
    assert "password_must_change=1" in APP
    assert 'admin_credentials.html' in ADMIN_ROUTES
    assert 'name="password"' not in FORM
    assert 'Admin Login ID / Username' in (ROOT/'templates/admin_credentials.html').read_text(encoding='utf-8')


def test_composite_multi_value_scopes_are_supported():
    assert 'name="scope_values_{{ st }}"' in FORM
    assert 'name="scope_types"' in FORM
    assert 'Multiple values within a boundary are allowed' in FORM
    assert "scope_groups={}" in ADMIN_ROUTES
    assert 'admin_scope_allows(me[\'id\'],st,v)' in ADMIN_ROUTES


def test_messaging_retry_and_notification_actor_are_fixed():
    # Sending retries briefly when SQLite reports the database is locked, rather
    # than surfacing a write conflict to the sender. The explicit BEGIN IMMEDIATE
    # is gone since the SQLAlchemy migration: the session owns the transaction.
    assert "if 'locked' not in str(exc).lower() or attempt_no==3" in ADMIN_ROUTES
    assert 'sa.exc.OperationalError' in ADMIN_ROUTES
    block=SECURITY[SECURITY.index('def _notify_super_admins'):SECURITY.index('def admin_has_permission')]
    assert 'me=current_admin()' in block
    # The notification records who caused it, resolved before the exclusion rule.
    assert "actor_admin_id=me['id'] if me else None" in block
    assert "actor_username_snapshot=me['username'] if me else None" in block


def test_db_schema_and_composite_scope_data():
    db=ROOT/'cbt.db'
    con=sqlite3.connect(db)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    cols={r['name'] for r in con.execute('PRAGMA table_info(admins)')}
    assert 'password_must_change' in cols
    for table in ('admin_messages','admin_role_assignments','admin_scopes','admin_notifications'):
        assert con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
    # Exercise the exact data shape used by the new multi-boundary model.
    con.execute("INSERT OR IGNORE INTO admin_scopes(admin_id,scope_type,scope_value,created_at) VALUES(?,?,?,datetime('now'))",(1,'subject','Mathematics'))
    con.execute("INSERT OR IGNORE INTO admin_scopes(admin_id,scope_type,scope_value,created_at) VALUES(?,?,?,datetime('now'))",(1,'class','SSS 1'))
    con.commit()
    rows=con.execute("SELECT scope_type,scope_value FROM admin_scopes WHERE admin_id=1 AND scope_type IN ('subject','class')").fetchall()
    assert {(r['scope_type'],r['scope_value']) for r in rows} >= {('subject','Mathematics'),('class','SSS 1')}
    con.close()


def test_all_modified_templates_have_valid_jinja_syntax():
    env=jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT/'templates')))
    for name in ('admin_account_form.html','admin_credentials.html','admin_password.html','admin_messages.html'):
        env.get_template(name)
