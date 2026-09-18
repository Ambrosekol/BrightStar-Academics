# Phase A — Administrator Security Foundation

This package is the controlled Phase A modification for Crainbow CBT.

## What changed

The existing Phase 6G entrance-examination engine remains in place. The application now adds a database-backed administrator security foundation:

- individual administrator accounts (`admins`)
- administrator types / roles (`admin_types`)
- canonical permission catalogue (`permissions`)
- role permissions (`admin_type_permissions`)
- account-specific permissions (`admin_permissions`)
- administrator scopes (`admin_scopes`)
- audit history (`audit_logs`)

The existing single-password admin login is replaced by a database-backed login while preserving the configured `CRAINBOW_ADMIN_PASSWORD` as the initial Super Admin password during first-time migration.

## Initial login

Username: `superadmin`

Password: the value of `CRAINBOW_ADMIN_PASSWORD`; if not configured for local testing, the existing default is `admin123`.

Change this before any real deployment.

## New Administration pages

- `/admin/administration`
- `/admin/administration/admins`
- `/admin/administration/admins/new`
- `/admin/administration/roles`
- `/admin/administration/roles/new`
- `/admin/administration/permissions`
- `/admin/administration/scopes`
- `/admin/administration/audit-logs`

## Important preservation rule

Do not replace the question banks or rebuild the entrance-examination tables. On startup, `init_db()` performs additive SQLite migrations and then continues the existing examination-bank synchronization.

## Verification

From the project folder, after the normal Flask requirements are installed:

```powershell
python verify_admin_security.py
```

The expected final line is:

`RESULT: ADMIN SECURITY FOUNDATION READY`

Then start the application normally:

```powershell
python app.py
```

and sign in to the Admin area with the initial Super Admin credentials.
