# Crainbow — Current Architecture Map (Phase6K4 Baseline)

## Product boundary
Crainbow is currently a unified Flask web platform containing public, student, school-administration, and entrance-examination workflows.

## Runtime
- Flask application concentrated primarily in `app.py`.
- SQLite database: `cbt.db`.
- Question-bank content primarily represented as JSON files under `data/`.
- Jinja templates under `templates/`.
- CSS/JS/images under `static/`.
- Uploaded media under `static/uploads/`.

## Major domains
1. Public gateway/login/practice.
2. Student portal: dashboard, assignments, tests, examinations, practice, results, profile/password management.
3. School administration: students, classes, subjects, sessions, assignments, assessments, results.
4. Entrance examination: candidates, papers, attempts, answers, scoring, rankings, retakes.
5. Administrative governance: roles, permissions, scopes, audit logs, notifications, control items, resource locks.

## Identity model
Current persistent account domains are separate:
- `admins`
- `students`
- `candidates`

A unified login route branches into the appropriate account/session model. The long-term target is one identity/authentication architecture with role-specific profiles; do not merge these domains blindly.

## Authorization
The current system has a substantial RBAC foundation with predefined roles, permissions and scopes. Backend enforcement must remain authoritative; UI visibility is not treated as security.

## Assessment model
School assessments currently use `school_assessments`, `school_questions` and `school_student_results`. The student take flow currently renders all questions on one page and submits the whole form.

## Entrance examination model
Entrance exams use `examinations`, `candidate_papers`, `attempts`, `answers` and `retake_grants`. Candidate timing is represented server-side with start/expiry timestamps. Historical question/answer snapshots are not yet fully isolated from mutable source banks.

## Key architectural observations
- The application is functionally broad but implementation is concentrated in a large Flask module.
- Database schema is created/evolved at application startup with ad-hoc `CREATE TABLE`/`ALTER TABLE` logic.
- SQLite foreign-key enforcement is not currently enabled by default.
- Production content authority for question banks is still too file-oriented.
- School assessments do not yet share the same robust attempt/response lifecycle as entrance examinations.
- Existing verification scripts include historical phase-specific checks and need consolidation into a current authoritative suite.
