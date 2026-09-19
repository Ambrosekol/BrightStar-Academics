# Crainbow — Current Architecture Map

## Product boundary
Crainbow is a unified Flask web platform containing public, student, parent, school-administration,
and entrance-examination workflows for a single school tenant.

## Runtime
- Flask application, entry point `app.py` (`python app.py`).
- SQLite database: `cbt.db`.
- Question-bank content represented as JSON files under `data/`.
- Jinja templates under `templates/`.
- CSS/JS/images under `static/`.
- Uploaded media under `static/uploads/`.

## Code organization
The codebase is a package per concern, not one large module:

- `app.py` — Flask/DB setup, error handlers, request hooks, the CSRF context processor,
  blueprint registration, and the handful of helpers genuinely shared across 3+ domains
  with no single clean owner (e.g. `_school_current_session`, `_active_sessions`).
- `models/` — SQLAlchemy models, one module per domain (`school.py`, `entrance.py`,
  `finance.py`, `auth.py`, `parents.py`, `public.py`, `library.py`, `admissions.py`,
  `tenancy.py`, `presence.py`, `governance.py`), re-exported through `models/__init__.py`.
- `core/` — cross-cutting helpers with no routes of their own, used by two or more
  domains: DB query helpers, RBAC/security, presence tracking, notifications, uploads,
  public-site settings, and the entrance/candidate-portal shared domain logic (question
  banks, candidate lookups, grading, result rendering).
- `blueprints/` — one package per route domain (public, auth, school, entrance,
  candidate_portal, student_portal, parents, finance, library, administration). Each
  registers routes directly on the single shared Flask `app` object rather than using
  `Blueprint` objects, so endpoint names are unchanged from before this split — see the
  root `README.md` for why.

## Major domains
1. Public gateway/login/practice.
2. Student portal: dashboard, assignments, tests, examinations, practice, results, profile/password management.
3. Parent portal: children's results/attendance/fees, feedback messaging with the school.
4. School administration: students, classes, subjects, sessions, assignments, assessments, results, promotion.
5. Entrance examination: candidates, papers, attempts, answers, scoring, rankings, retakes.
6. Finance: fee items, assessments, payments, allocation, receipts.
7. Library: book/loan catalogue.
8. Administrative governance: roles, permissions, scopes, audit logs, notifications, control items, resource locks.

## Identity model
Current persistent account domains are separate:
- `admins`
- `students`
- `candidates`
- `parents` (parent accounts, linked to one or more students)

A unified `/login` route branches into the appropriate account/session model based on the
submitted identifier. These domains remain intentionally separate; do not merge them blindly.

## Authorization
A substantial RBAC foundation exists: a fine-grained permission catalogue, role presets built
from it, and scope limits (e.g. a class teacher restricted to their own classes). Backend
enforcement (`core/security.py`) is authoritative; UI visibility is not treated as security.

## Assessment model
School assessments use `school_assessments`, `school_questions`, `school_assessment_attempts`,
`school_assessment_attempt_questions` and `school_student_results`. Both the school-assessment
flow and the entrance-examination flow use the same interaction contract: a student/candidate
explicitly starts the assessment, the timer begins only at that point, one question is shown
at a time, answers persist through Save & Next, and the server is authoritative for timing,
submission and grading against a frozen per-attempt question snapshot — never the live,
editable question bank.

## Entrance examination model
Entrance exams use `examinations`, `candidate_papers`, `attempts`, `answers`,
`attempt_questions` and `retake_grants`. Candidate timing is represented server-side with
start/expiry timestamps, and each attempt freezes its own question/answer-key snapshot at
creation time, so a later edit to the source question bank never changes an already-taken
paper.

## Key architectural facts
- Database schema is declared once in `models/` and created/upgraded automatically at
  startup (`db.create_all()` plus a column-diffing helper `_add_missing_columns()`);
  `migrations/` is a historical record of past schema changes, not a live migration runner.
- SQLite foreign-key enforcement is enabled per connection (`PRAGMA foreign_keys=ON`).
- Production content authority for question banks is still file-oriented (JSON under
  `data/`), not a database-backed content-management layer — this remains file-oriented by
  design for the current single-tenant deployment, not yet the versioned/controlled content
  model described in `TARGET_ARCHITECTURE.md`.
- The authoritative regression suite is `tests/current/` (source-level contract checks);
  `tests/verification/` covers end-to-end/runtime behavior; `tests/legacy/` is a retained,
  non-authoritative historical archive. See `tests/README.md`.
