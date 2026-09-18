# Phase 3E — Administrator Access & Authentication Hotfix

## Scope
This hotfix addresses the verified People & Access defects reported during LAN testing while preserving the existing Crainbow CBT visual language and workspace architecture.

### Fixed
- Administrator creation now generates a one-time temporary password automatically.
- Newly created administrators are flagged `password_must_change=1` and are forced through the same first-login password-change pattern used by Student Portal accounts.
- Creation returns a one-time Administrator Login Details page with print support. Plaintext passwords are never stored.
- Administrator access boundaries now support multiple values for Academic Session, Class, Subject and Question Bank.
- Multiple boundary types may be combined. Example: Subject = Mathematics + Chemistry + Further Mathematics AND Class = SSS 1 + SSS 2 + SSS 3.
- Delegated administrators may assign only roles whose permissions are contained within their own effective permissions.
- Delegated administrators may only assign boundary values that fall inside their own effective boundary.
- Direct-message sending now uses an explicit transaction and retry path for transient SQLite lock contention.
- The notification actor bug was fixed: notification creation now resolves the actor even when the actor is excluded from the recipient list. Notifications therefore retain actor identity and timestamps reliably.
- Existing message schema/indexes and administrator authentication columns are repaired through migration `0005_admin_auth_scope_messaging`.

## Authentication rule
A newly created administrator receives:
1. Permanent Login ID / Username.
2. One-time temporary password.
3. Mandatory first-login password replacement.

After the private password is saved, normal workspace selection and role-based authorization resume.

## Boundary rule
No boundary means the administrator can work across the whole area granted by their roles.

If boundaries exist, each selected boundary type is an additional constraint. Multiple values inside a type are OR'd; different types are AND'd.

Examples:
- Subjects {Mathematics, Chemistry} + Classes {SSS 1, SSS 2, SSS 3}
- Subject {History} + Classes {JSS 1, JSS 2, JSS 3, SSS 1, SSS 2, SSS 3}
- Question Banks {Bank A, Bank B}

## Verification
- `pytest -q tests/current`: **36 passed**
- New Phase 3E admin hardening suite: **6 passed**
- Python compilation: **passed**
- Modified Jinja templates syntax compilation: **passed**
- SQLite `PRAGMA integrity_check`: **ok**
- Migration state: **0005_admin_auth_scope_messaging**
- Existing Phase 6H administration static verification: **passed**
- Existing student account and student-route regression checks: **passed**

Legacy phase-numbered scripts that assert obsolete phase identifiers were intentionally not treated as product failures.
