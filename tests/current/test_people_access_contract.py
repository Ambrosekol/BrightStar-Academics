from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
APP=(ROOT/'app.py').read_text(encoding='utf-8')
ADMIN_ROUTES=(ROOT/'blueprints'/'administration'/'routes.py').read_text(encoding='utf-8')
ADMIN_HELPERS=(ROOT/'blueprints'/'administration'/'helpers.py').read_text(encoding='utf-8')
FORM=(ROOT/'templates/admin_account_form.html').read_text(encoding='utf-8')
BASE=(ROOT/'templates/admin_base.html').read_text(encoding='utf-8')
MSG=(ROOT/'templates/admin_messages.html').read_text(encoding='utf-8')
MIG=(ROOT/'migrations/0003_admin_people_messaging.py').read_text(encoding='utf-8')

def test_multi_role_model_and_ui():
    # The join table is declared under models/ (a package split by domain);
    # app.py uses the mapped class.
    MODELS="\n".join(p.read_text(encoding='utf-8') for p in sorted((ROOT/'models').glob('*.py')))
    assert "__tablename__ = 'admin_role_assignments'" in MODELS
    assert 'AdminRoleAssignment' in APP
    assert 'name="admin_type_ids"' in FORM
    assert 'type="checkbox"' in FORM
    assert '_sync_admin_roles' in ADMIN_HELPERS

def test_question_bank_scope_is_listed():
    assert 'scope-bank' in FORM
    assert 'entrance_bank_display_name' in FORM
    assert 'name="scope_value"' in FORM

def test_admin_contact_and_photo_fields():
    for field in ('email','phone','whatsapp','photo'):
        assert f'name="{field}"' in FORM
    assert 'image/jpeg' in FORM
    assert "_save_image_upload(photo,'admins'" in ADMIN_ROUTES

def test_admin_messaging_is_available_and_csrf_protected():
    assert "@app.route('/admin/administration/messages')" in ADMIN_ROUTES
    assert "@app.post('/admin/administration/messages/send')" in ADMIN_ROUTES
    assert '@csrf_protect' in ADMIN_ROUTES
    assert 'admin_messages' in BASE
    assert 'admin_message_send' in MSG

def test_migration_backfills_roles():
    assert 'admin_role_assignments' in MIG
    assert 'INSERT OR IGNORE INTO admin_role_assignments' in MIG
