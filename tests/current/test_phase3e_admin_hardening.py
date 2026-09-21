from pathlib import Path
import ast
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
    block=SECURITY[SECURITY.index('def _notify_school_admins'):SECURITY.index('def admin_has_permission')]
    assert 'me=current_admin()' in block
    # The notification records who caused it, resolved before the exclusion rule.
    assert "actor_admin_id=me['id'] if me else None" in block
    assert "actor_username_snapshot=me['username'] if me else None" in block


def test_db_schema_and_composite_scope_data():
    """The governance tables and the multi-boundary scope shape.

    Built from the models into a throwaway database rather than read out of a
    live one: this test used to INSERT into the real school database, which is
    both a side effect on production data and a reason the test could not run
    on a fresh checkout.
    """
    import tempfile
    import sqlalchemy as sa

    from models import db as _db

    with tempfile.TemporaryDirectory() as tmp:
        engine = sa.create_engine(f'sqlite:///{Path(tmp).as_posix()}/schema_check.db')
        _db.metadata.create_all(engine)
        inspector = sa.inspect(engine)

        cols = {c['name'] for c in inspector.get_columns('admins')}
        assert 'password_must_change' in cols
        tables = set(inspector.get_table_names())
        for table in ('admin_messages', 'admin_role_assignments', 'admin_scopes',
                      'admin_notifications'):
            assert table in tables, table

        # Exercise the exact data shape the multi-boundary model relies on.
        scopes = _db.metadata.tables['admin_scopes']
        with engine.begin() as con:
            con.execute(scopes.insert(), [
                {'admin_id': 1, 'scope_type': 'subject', 'scope_value': 'Mathematics',
                 'created_at': '2026-01-01T00:00:00+00:00'},
                {'admin_id': 1, 'scope_type': 'class', 'scope_value': 'SSS 1',
                 'created_at': '2026-01-01T00:00:00+00:00'},
            ])
            rows = con.execute(sa.select(scopes.c.scope_type, scopes.c.scope_value).where(
                scopes.c.admin_id == 1,
                scopes.c.scope_type.in_(('subject', 'class')))).all()
        assert set(rows) >= {('subject', 'Mathematics'), ('class', 'SSS 1')}
        engine.dispose()


def test_all_modified_templates_have_valid_jinja_syntax():
    env=jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT/'templates')))
    for name in ('admin_account_form.html','admin_credentials.html','admin_password.html','admin_messages.html'):
        env.get_template(name)
