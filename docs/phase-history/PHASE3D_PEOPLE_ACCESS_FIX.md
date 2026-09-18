# Phase 3D — People & Access / Administrator Experience

## Purpose

This release hardens the **People & Access → Add staff administrator** area without splitting Crainbow into separate applications.

## Changes

- Administrators now have a complete staff identity record: display name, username, email, phone, WhatsApp and profile image.
- Profile images use the same secure upload pipeline as student/candidate images: PNG, JPG/JPEG, GIF and WEBP, with extension + file-signature validation and upload-size limits.
- A staff administrator can hold **multiple job roles**. Effective role permissions are cumulative.
- The legacy `admins.admin_type_id` remains as the primary/compatibility role while `admin_role_assignments` becomes the authoritative multi-role association.
- Existing administrator accounts are automatically backfilled into the new role-assignment table by migration `0003_admin_people_messaging`.
- Access boundaries remain independent of roles. A boundary narrows where the selected roles may operate.
- Selecting `Specific question bank(s)` now presents the actual available question banks in a multi-select list. The stored scope value is the real bank ID, which matches the server-side bank authorization check.
- Administrator-to-administrator private messaging is available to every signed-in active administrator. Messages are server-side records, CSRF protected, length limited and restricted to the two participants in the thread.
- Unread message counts appear in the administration navigation.
- The administrator directory shows roles, contact information, profile image, status and access-boundary count.

## Security model

Role selection grants capabilities. Scope selection limits resource reach. Direct permission overrides remain a Super Admin-only exceptional mechanism.

A UI selection never replaces server-side authorization: `admin_has_permission()` now evaluates all assigned roles plus direct permissions, and existing resource scope checks continue to enforce bank/class/subject/session boundaries.

## Migration

`migrations/0003_admin_people_messaging.py` is idempotent and backfills existing administrators into the new role-assignment table.

The migration runner invokes it safely even when the contact columns already exist in a fresh database bootstrap.
