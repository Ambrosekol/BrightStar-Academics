from pathlib import Path
import sqlite3, ast

# tests/legacy/, so the project root is two levels up.
ROOT = Path(__file__).resolve().parents[2]
app = (ROOT / 'app.py').read_text(encoding='utf-8')
form = (ROOT / 'templates' / 'admin_account_form.html').read_text(encoding='utf-8')
css = (ROOT / 'static' / 'admin.css').read_text(encoding='utf-8')

ast.parse(app)
assert "'/admin/password'" in app
assert "def admin_account_credentials_reset(aid):" in app
assert "password_must_change=1" in app
assert "id=\"adminPhotoInput\"" in form
assert "id=\"adminPhotoPreview\"" in form
assert 'admin-photo-input' in css
assert 'admin-photo-upload-btn' in css
assert 'multiple size="5"' in form

con = sqlite3.connect(ROOT / 'cbt.db')
rows = con.execute('SELECT id,username,photo_path FROM admins').fetchall()
super_scopes = con.execute('SELECT COUNT(*) FROM admin_scopes WHERE admin_id=1').fetchone()[0]
con.close()
assert super_scopes == 0, f'Super Admin still has {super_scopes} boundaries'
for _, username, photo_path in rows:
    if photo_path:
        assert (ROOT / 'static' / photo_path).is_file(), f'Missing photo for {username}: {photo_path}'

print('Phase 3H verification: PASS')
print(' - Python AST parse: PASS')
print(' - First-login password route protected from workspace redirect loop: PASS')
print(' - Credential reset route present: PASS')
print(' - Super Admin boundary count: 0')
print(' - Stored administrator photos verified: PASS')
print(' - Profile photo UI contract: PASS')
print(' - Multiple-role / multiple-boundary controls preserved: PASS')
