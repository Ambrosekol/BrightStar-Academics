# Crainbow CBT — Administrator Security Foundation

Implemented as the first security/administration foundation without replacing the existing entrance-examination engine.

## Database model

- `admins` — individual administrator accounts, hashed passwords, role, active state and login history.
- `admin_types` — reusable administrator roles, including protected system role `Super Admin` and `Ordinary Admin`.
- `permissions` — canonical permission catalogue.
- `admin_type_permissions` — role-level permission grants.
- `admin_permissions` — account-specific additional permission grants.
- `admin_scopes` — account-level boundaries (`global`, `academic_session`, `class`, `subject`, `bank`).
- `audit_logs` — security and administrative activity history.

## Authorization rules

1. Super Admin is a system role and bypasses ordinary permission/scope restrictions.
2. Ordinary Admin access is the union of role permissions and direct account permissions.
3. Resource routes enforce scope where a bank, candidate or attempt is identifiable.
4. Current legacy collection pages require a global scope until their queries become resource-aware; this avoids accidentally exposing records outside an administrator's class/bank scope.
5. Failed authorization and scope checks are themselves audited.

## Initial account

On first database initialization, the application creates:

- Username: `superadmin` (or `CRAINBOW_SUPERADMIN_USERNAME`)
- Display name: `Super Admin`
- Password: `CRAINBOW_ADMIN_PASSWORD` (defaults to `admin123` for local testing only)

The password is stored as a Werkzeug password hash, never as plaintext.

## Administration routes

- `/admin/administration`
- `/admin/administration/admins`
- `/admin/administration/admins/new`
- `/admin/administration/roles`
- `/admin/administration/roles/new`
- `/admin/administration/permissions`
- `/admin/administration/scopes`
- `/admin/administration/audit-logs`

## Preservation guardrail

The existing entrance-examination functionality remains in the same Flask application and database. This modification adds the security foundation and access-control layer; it does not replace the question engine, timer, grading, candidate session, retake or results logic.
