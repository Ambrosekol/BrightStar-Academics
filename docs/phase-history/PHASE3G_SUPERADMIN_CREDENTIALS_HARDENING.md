# Phase 3G — Super Admin Authority & Administrator Credential Recovery

## Corrections

- Super Admin accounts are system-authority accounts and are never restricted by `admin_scopes`.
- Stale scope rows attached to Super Admin accounts are removed during security initialization and from the packaged demo database.
- The Administrators directory displays Super Admin access as **Unrestricted**, not as a boundary count.
- Super Admin profile management is available to a Super Admin, while the protected system role itself cannot be replaced or narrowed through the ordinary staff editor.
- Staff administrators retain cumulative multi-role assignment and multi-value access boundaries.
- Administrators with `admins.edit` can regenerate a staff member's login credentials. The existing password is invalidated, a new one-time temporary password is generated, and `password_must_change=1` forces the administrator to choose a private password at next sign-in.
- The regenerated credentials are shown once on the secure credentials screen; plaintext passwords are never persisted.
- The same reset action is available from the administrator Manage screen and the directory row action.

## Verification

- Python compilation: PASS
- Jinja template compilation: PASS (`admin_account_form.html`, `admin_accounts.html`, `admin_credentials.html`)
- Current regression suite: PASS — 36/36
- Packaged database Super Admin stale boundaries: removed — 0 remaining
- Credential reset route and UI references: present
- Super Admin bypass remains enforced by `admin_has_permission()` and `admin_scope_allows()`
