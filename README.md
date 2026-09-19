# Crainbow

Crainbow is the web platform used by Creative Rainbow Montessori School to run its entrance
examinations and day-to-day school administration from one Flask application: a public
marketing site, a computer-based entrance-examination engine, a full school portal (classes,
subjects, assignments, tests, results, promotion), student/parent self-service portals,
finance/receipting, a library catalogue, and role-based admin governance (accounts,
permissions, audit log, messaging).

It is a single-tenant, single-server deployment: one Flask process, one SQLite database, no
external services required to run it locally.

## Contents

- [Features](#features)
- [Tech stack](#tech-stack)
- [Project layout](#project-layout)
- [Getting started](#getting-started)
- [Configuration](#configuration)
- [Running the app](#running-the-app)
- [Testing](#testing)
- [Deployment](#deployment)
- [Notes for contributors](#notes-for-contributors)

## Features

**Public site** — marketing pages (home, about, academics, school life, admissions, news,
contact) and a no-login practice-test gateway.

**Unified login** — one `/login` form for every account type. The identifier the visitor
submits determines whether they're signed in as staff, a parent, a school student, or an
entrance-exam candidate; each lands on its own dashboard.

**Entrance examinations** — question-bank management, candidate registration/credentialing,
per-paper timed attempts with server-side timing and scoring, a frozen per-attempt question
snapshot (so a later bank edit never changes an already-taken paper), retake grants,
rankings, and CSV/JSON export of results.

**School portal** — academic sessions, classes, subjects, student enrolment and promotion
between sessions, assignments and projects (with written and CBT-style quiz variants), school
tests/practice/examinations sharing the same attempt/grading model as the entrance exam,
term results with a staged verify → approve → release governance workflow, and admissions
history.

**Student & parent portals** — students take assignments and school assessments, track
results once released, and manage their password; parents view their children's results,
attendance/fee status, and exchange messages with the school.

**Finance** — fee items and per-student assessments, payment recording and allocation,
outstanding-balance tracking, and PDF/emailed/WhatsApp receipt delivery.

**Library** — a simple book/loan catalogue for admin use.

**Administration & governance** — staff accounts, custom roles built from a fine-grained
permission catalogue, scope-limited access (e.g. a class teacher restricted to their own
classes), an audit log, an internal notification/messaging system, and resource locks (e.g.
locking a question bank against edits during a live exam).

## Tech stack

- **Python 3** / **Flask** — web framework and routing
- **Flask-SQLAlchemy** / **SQLAlchemy 2.x** — ORM and schema
- **SQLite** — database (one file, `cbt.db`)
- **Jinja2** — server-rendered templates
- **ReportLab** — PDF generation (receipts, result cards)
- **python-dotenv** — `.env` configuration loading

There is no separate frontend build step — templates, CSS and JS are served directly by
Flask from `templates/` and `static/`.

## Project layout

```
crainbow/
├── app.py                    # Flask/DB setup, error handlers, request hooks, context
│                              #   processor, blueprint registration, a handful of helpers
│                              #   shared across 3+ domains with no single owner
├── models/                   # SQLAlchemy models, one module per domain
│   ├── base.py                #   declarative Base + db = SQLAlchemy(...)
│   ├── auth.py                #   Admin, AdminType, Permission, AuditLog, AdminMessage,
│   │                          #     AdminControlItem, AdminResourceLock, ...
│   ├── school.py              #   AcademicSession, SchoolClass, Student, assignments/
│   │                          #     assessments/results/promotion
│   ├── entrance.py            #   Examination, Attempt, Answer, Candidate, EntranceBankConfig
│   ├── finance.py             #   FinanceFeeItem, FinancePayment, FinancePaymentAllocation, ...
│   ├── library.py             #   LibraryBook, LibraryLoan
│   ├── parents.py             #   ParentAccount, ParentStudentLink, ParentFeedback(Reply)
│   ├── public.py              #   SchoolPublicPage/News/Setting/Enquiry
│   ├── admissions.py          #   StudentAdmissionProfile/Contact, StudentEnrollmentHistory
│   ├── tenancy.py             #   School, SchoolSetting, StudentNumberAllocation
│   ├── presence.py            #   PresenceSession, SchoolNotification, PasswordResetToken
│   └── governance.py          #   SchemaMigration (historical migration record)
├── core/                     # Cross-cutting helpers used by 2+ domains — no routes here
│   ├── db_helpers.py           #   one/all_rows/tuples/obj — thin wrappers returning
│   │                          #     sqlite3.Row-like mappings from SQLAlchemy queries
│   ├── security.py             #   RBAC (permissions/roles/scopes), admin_required,
│   │                          #     csrf_protect, audit_log
│   ├── accounts.py             #   shared login/logout/session-clearing helpers
│   ├── presence.py             #   "who's online" tracking
│   ├── notifications.py        #   guardian email/WhatsApp senders
│   ├── uploads.py              #   image upload validation and storage
│   ├── public_settings.py      #   public-site settings/page lookup
│   └── entrance.py             #   question-bank loading, candidate lookups, grading,
│                              #     result-card rendering — shared by the entrance-admin
│                              #     and candidate-portal blueprints below
├── blueprints/                # One package per route domain (plain @app.route, not
│   ├── public/                  #   Flask Blueprint objects — see "Notes for contributors")
│   ├── auth/                    #   login/logout/password recovery
│   ├── school/                  #   the school portal (see Features above)
│   ├── entrance/                #   entrance-exam admin: config, banks, candidates, results
│   ├── candidate_portal/        #   the entrance-candidate self-service side
│   ├── student_portal/          #   the student self-service side
│   ├── parents/                 #   parent portal + admin parent management
│   ├── finance/                 #   fees, payments, receipts
│   ├── library/                 #   library admin
│   └── administration/          #   accounts/roles/permissions, messaging, notifications
├── services/                  # Small, dependency-free utilities (date formatting,
│                              #   student-number allocation)
├── migrations/                # Historical schema-change record (see Notes below)
├── templates/                 # Jinja templates, one subtree per portal/section
├── static/                    # CSS/JS/images; static/uploads/ holds user-uploaded files
├── data/                      # Entrance question banks, stored as JSON files
├── tests/
│   ├── current/                 #   the authoritative contract/regression suite
│   ├── verification/            #   end-to-end write-path and security probes
│   └── legacy/                  #   retained historical scripts, not authoritative
├── security/                  # Standalone security checks and the manual QA test plan
├── deployment/                 # LAN/Windows deployment guide and helper scripts
└── docs/                      # Architecture notes and phase history
```

Every blueprint's `routes.py` registers routes on the single shared Flask `app` object
(`from app import app`), so route/endpoint names are unchanged from before the codebase was
split into packages — nothing outside this repo needed to change because of the reorganization.

## Getting started

Requires Python 3.10 or newer (developed and tested on 3.13). No external database server,
message queue, or build toolchain is needed.

```bash
git clone <this-repo>
cd crainbow

python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS/Linux

pip install -r requirements.txt

copy .env.example .env       # Windows
# cp .env.example .env       # macOS/Linux
```

Then edit `.env` — at minimum set `CRAINBOW_SECRET` (a real random secret, not the
placeholder) and `CRAINBOW_ADMIN_PASSWORD` (the initial Super Admin password). See
[Configuration](#configuration) below.

## Configuration

All configuration is read from environment variables, loaded from `.env` via
`python-dotenv` (a variable already set in the real environment always wins over `.env`).
`.env.example` documents every supported variable in full; the highlights:

| Variable | Purpose |
|---|---|
| `CRAINBOW_ENV` | `development` or `production`. In production, `CRAINBOW_SECRET` must be at least 32 characters or the app refuses to start. |
| `CRAINBOW_SECRET` | Flask session-signing secret. Leaving it blank generates a new one on every restart, which logs everyone out. Generate one with `python -c "import secrets; print(secrets.token_hex(32))"`. |
| `CRAINBOW_SUPERADMIN_USERNAME` / `CRAINBOW_ADMIN_PASSWORD` | Read **only** the first time the app initializes a database with no Super Admin account yet. Once one exists, the database is authoritative and these are ignored on every later startup. |
| `CRAINBOW_DB` / `CRAINBOW_DATA` | Override the database file / question-bank data folder. Leave unset for the normal layout (`cbt.db` and `data/` next to `app.py`). Tests and verification scripts set `CRAINBOW_DB` to a throwaway copy so they never touch the real database. |
| `CRAINBOW_MAX_UPLOAD_BYTES` / `CRAINBOW_MAX_REQUEST_BYTES` | Upload and request size limits. |
| `CRAINBOW_SMTP_*` | Outgoing mail for receipts and password resets. Leave `CRAINBOW_SMTP_HOST` blank to disable email delivery. |
| `CRAINBOW_WHATSAPP_*` | WhatsApp Business Cloud API credentials for receipt delivery. Leave `CRAINBOW_WHATSAPP_TOKEN` blank to disable it. |
| `PORT` | Port for the built-in development server (default `5000`). |

## Running the app

```bash
python app.py
```

This creates/upgrades the SQLite schema, seeds reference data (class structure, permission
catalogue, the bootstrap Super Admin) and starts the Flask development server on
`http://0.0.0.0:5000` (or `PORT`). On Windows, `run_windows.bat` does the same after
installing dependencies.

- `/` — public site
- `/login` — unified login (staff, parents, students, candidates)
- `/admin` — staff entry point (redirects to the neutral Workspace Home)
- `/practice` — no-login practice-test gateway
- `/health` — liveness check, returns `{"status": "ok"}`

For a LAN/production-style deployment, see [Deployment](#deployment).

## Testing

```bash
python tests/current/run_current.py
```

This is the authoritative contract suite (architecture, security, assessment-flow, and
RBAC/governance checks) and the one to run after any change.

`tests/verification/` holds deeper, end-to-end scripts against a real (throwaway) database —
one script per feature area (`write_paths_*.py`), plus `smoke_entrypoint.py` (boots the real
`python app.py` process and hits it over HTTP), `verify_admin_security.py`, `test_env_config.py`
and `probe_rbac.py`. Run any of them directly with `python tests/verification/<script>.py`.

`tests/legacy/` holds retained historical scripts kept for reference; they are not part of
the authoritative suite.

## Deployment

`deployment/` contains the LAN deployment guide (`PHASE6G_DEPLOYMENT_GUIDE.md`), an exam-day
checklist, and Windows helper scripts for finding the server's LAN IP, opening the firewall
port, and starting the server. Inspect any `.bat` script before running it in your own
environment.

## Notes for contributors

- **Blueprints use plain `@app.route`, not `Blueprint` objects.** Every `blueprints/*/routes.py`
  does `from app import app` and decorates routes directly on that shared instance. This was a
  deliberate choice when the codebase was split out of a single `app.py`: it keeps every
  endpoint name (and therefore every `url_for(...)` call and every raw permission-mapping
  entry) exactly as it was, at the cost of not getting Blueprint features like URL prefixes.
- **`migrations/` is a historical record, not a live migration runner.** The schema is
  declared once in `models/` and created/upgraded automatically at startup
  (`db.create_all()` plus a column-diffing helper); the SQL bodies under `migrations/` no
  longer execute — they're kept so `schema_migrations` stays an accurate record of which
  upgrades a given database has passed.
- **Question banks are JSON files under `data/`.** `seed_banks.py` and `seed_demo_banks.py`
  are optional local-only scripts for populating sample banks; do not run
  `seed_demo_banks.py` against a real school database — its content is explicitly marked
  DEMO-only.
- **`cbt.db` is the live database.** Never delete it or run destructive operations against it
  without an explicit backup.
