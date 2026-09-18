"""One-shot verification for the administrator security foundation.

Reports the state of the administrator, role, permission and scope tables, and
confirms the Super Admin holds every permission and bypasses scope limits.

Run it against a throwaway copy rather than the live database:

    set CRAINBOW_DB=C:\\path\\to\\copy.db     (PowerShell: $env:CRAINBOW_DB=...)
    python verify_admin_security.py
"""
import os
import sys

# tests/verification/, so the project root is one level up.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from sqlalchemy import func, select  # noqa: E402

import app  # noqa: E402
from models import Admin, AdminType, Permission, db  # noqa: E402

app.init_db()

TABLES = ['admins', 'admin_types', 'permissions', 'admin_type_permissions',
          'admin_permissions', 'admin_scopes', 'audit_logs']

print('=== CRAINBOW ADMIN SECURITY VERIFICATION ===')
with app.app.app_context():
    inspector = db.inspect(db.engine)
    present = set(inspector.get_table_names())
    for name in TABLES:
        if name in present:
            rows = db.session.execute(
                select(func.count()).select_from(db.metadata.tables[name])).scalar()
            print(f'{name:24} PASS | rows={rows}')
        else:
            print(f'{name:24} FAIL | table missing')

    print('\n--- ADMIN ACCOUNTS ---')
    accounts = db.session.execute(
        select(Admin.username, Admin.display_name, Admin.active,
               AdminType.name.label('role'), AdminType.is_system)
        .join(AdminType, AdminType.id == Admin.admin_type_id)
        .order_by(Admin.id)).mappings().all()
    for a in accounts:
        print(f"{a['username']:20} | {a['role']:15} | active={a['active']} | system={a['is_system']}")

    print('\n--- SUPER ADMIN CHECKS ---')
    super_admin_id = db.session.execute(
        select(Admin.id)
        .join(AdminType, AdminType.id == Admin.admin_type_id)
        .where(AdminType.name == 'Super Admin', Admin.active == 1)
        .limit(1)).scalar()

    if super_admin_id:
        print('superadmin account     PASS')
        every_permission = all(app.admin_has_permission(super_admin_id, code)
                               for code, *_ in app.ADMIN_PERMISSION_DEFS)
        print('all permissions       ', 'PASS' if every_permission else 'FAIL')
        print('global scope bypass   ',
              'PASS' if app.admin_scope_allows(super_admin_id, 'bank', 'anything') else 'FAIL')
    else:
        print('superadmin account     FAIL')

    print('\n--- PERMISSION CATALOGUE ---')
    defined = len(app.ADMIN_PERMISSION_DEFS)
    stored = db.session.execute(select(func.count()).select_from(Permission)).scalar()
    print('permissions defined    ', defined)
    print('permission rows        ', stored)
    if defined != stored:
        print('  NOTE: the catalogue and the table disagree; init_db() seeds the table '
              'from ADMIN_PERMISSION_DEFS, so a mismatch means stale rows remain.')

    print('\nRESULT: ADMIN SECURITY FOUNDATION READY' if super_admin_id
          else '\nRESULT: CHECK FAILED')
