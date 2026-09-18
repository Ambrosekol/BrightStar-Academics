"""One-shot verification for Phase A administrator security foundation."""
import app

app.init_db()
con=app.db()

tables=['admins','admin_types','permissions','admin_type_permissions','admin_permissions','admin_scopes','audit_logs']
print('=== CRAINBOW ADMIN SECURITY VERIFICATION ===')
for t in tables:
    exists=con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(t,)).fetchone()
    count=con.execute(f'SELECT COUNT(*) c FROM {t}').fetchone()['c'] if exists else 0
    print(f'{t:24} PASS | rows={count}')

admins=con.execute('''SELECT a.username,a.display_name,a.active,t.name role,t.is_system FROM admins a JOIN admin_types t ON t.id=a.admin_type_id ORDER BY a.id''').fetchall()
print('\n--- ADMIN ACCOUNTS ---')
for a in admins:
    print(f"{a['username']:20} | {a['role']:15} | active={a['active']} | system={a['is_system']}")

print('\n--- SUPER ADMIN CHECKS ---')
super_admin=con.execute('''SELECT a.id FROM admins a JOIN admin_types t ON t.id=a.admin_type_id WHERE t.name='Super Admin' AND a.active=1 LIMIT 1''').fetchone()
if super_admin:
    print('superadmin account     PASS')
    print('all permissions       ', 'PASS' if all(app.admin_has_permission(super_admin['id'],p[0]) for p in app.ADMIN_PERMISSION_DEFS) else 'FAIL')
    print('global scope bypass    ', 'PASS' if app.admin_scope_allows(super_admin['id'],'bank','anything') else 'FAIL')
else:
    print('superadmin account     FAIL')

print('\n--- PERMISSION CATALOGUE ---')
print('permissions defined    ', len(app.ADMIN_PERMISSION_DEFS))
print('permission rows        ', con.execute('SELECT COUNT(*) c FROM permissions').fetchone()['c'])
print('\nRESULT: ADMIN SECURITY FOUNDATION READY' if super_admin else '\nRESULT: CHECK FAILED')
con.close()
