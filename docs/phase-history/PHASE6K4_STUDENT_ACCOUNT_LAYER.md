# Phase 6K.4 — Student Account Layer

## Purpose

This release activates the student authentication boundary that was already provisioned in the Phase 6K/6K.3 database design.

The existing distinction remains intact:

- **Candidate** = entrance-examination identity.
- **Student** = enrolled school-pupil identity.
- Candidate authentication continues to use `candidates.candidate_code` and its existing password hash/session.
- Student authentication uses `students.login_username` and `students.login_password_hash` and establishes `session['student_id']`.

## Student account lifecycle

When an administrator registers a new student:

1. The normal `students` record is created.
2. The student's enrolment is created exactly as before.
3. A student login username is provisioned from the admission number (upper-cased).
4. A random temporary password is generated.
5. Only the password hash is stored in SQLite.
6. The temporary password is displayed once on the account-created screen.
7. The student is required to change that password after first login.

Existing students whose accounts were never provisioned can be given an account from the Student Profile using **Create Student Account**. Passwords can be regenerated with **Reset Password**.

## Account controls

The administrator Student Profile now exposes:

- Student ID / Username
- Account status
- Last login
- Password state
- Create Student Account
- Reset Password
- Enable/Disable Login
- Print Login ID

Passwords are never retrieved from the database in plaintext.

## Authentication routing

The existing `/login` unified authentication surface remains the entry point:

- Admin credentials → Admin Workspace Home
- Candidate credentials → Candidate Dashboard
- Student credentials → Student Dashboard

The candidate login code and candidate session key remain unchanged.

## Student portal

The student dashboard is now a real authenticated student-facing surface with:

- school branding/logo
- student's uploaded photograph
- student ID/admission number
- current class/session
- assignments preview
- recent academic results preview
- practice-test access
- clearly separated future Tests, Examinations, Performance and Results areas
- password-change access

Only the student's own records are queried for the student dashboard.

## Security boundary

Student login is disabled when either the student record (`active`) or account (`account_active`) is disabled.

Temporary credentials use a one-way password hash and are not stored as plaintext.

State-changing student account operations are CSRF protected and audited.

## Regression guardrail

No candidate table, candidate route, candidate attempt, candidate answer, candidate paper, entrance-examination scoring, result, or ranking logic was intentionally changed by this module.

The existing Phase 6K.3 student route/session-boundary regression check remains passing.
