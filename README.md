# Brightstars Academics

Brightstars Academics is a multi-school platform. One deployment serves many schools; each
school gets its own portal, its own PostgreSQL database and its own folder of files, and can
never see another school's data. Creative Rainbow Montessori School ("crainbow") is a school on
the platform, not the platform itself.

Each school's portal runs entrance examinations, day-to-day school administration (classes,
subjects, assignments, tests, results, promotion), student and parent self-service, finance and
receipting, a library catalogue, and role-based admin governance.

**A school's address is a portal, not a website.** The only public website is the platform's own,
served at `/` on the platform hostnames. The platform console lives at `/platform` on those same
hostnames: sign in there to create a school, manage its addresses and administrators, suspend it,
or enter it.

Multi-tenancy is not optional and cannot be switched off — it is the architecture.

## Contents

- [Features](#features)
- [Tech stack](#tech-stack)
- [Project layout](#project-layout)
- [Getting started](#getting-started)
- [Configuration](#configuration)
- [Running the app](#running-the-app)
- [Multi-tenancy](#multi-tenancy)
- [Testing](#testing)
- [Deployment](#deployment)
- [Notes for contributors](#notes-for-contributors)

## Features

**Platform website and console** — the platform's own public site at `/`, and the console at
`/platform` where schools are created and managed.

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
- **PostgreSQL** — database; one for the platform registry, plus one per school
- **psycopg 3** — PostgreSQL driver
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
├── control_plane/            # Multi-tenancy: platform registry, hostname → school, per-school
│                              #   database routing, provisioning, the platform console and
│                              #   the platform's own website (off unless enabled)
├── core/                     # Cross-cutting helpers used by 2+ domains — no routes here
│   ├── db_helpers.py           #   one/all_rows/tuples/obj — thin wrappers returning
│   │                          #     sqlite3.Row-like mappings from SQLAlchemy queries
│   ├── security.py             #   RBAC (permissions/roles/scopes), admin_required,
│   │                          #     csrf_protect, audit_log
│   ├── accounts.py             #   shared login/logout/session-clearing helpers
│   ├── presence.py             #   "who's online" tracking
│   ├── notifications.py        #   guardian email/WhatsApp senders
│   ├── uploads.py              #   image upload validation and storage
│   ├── storage.py              #   per-school data/ and uploads/ folders
│   ├── branding.py             #   the school's own name/motto/logo for templates
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

### Requirements

| Requirement | Notes |
|---|---|
| **Python 3.10+** | Developed and tested on 3.13. |
| **PostgreSQL 14+** | Required. Tested against PostgreSQL 18. Must be running and reachable on `localhost:5432` for local development. |
| A PostgreSQL role that may `CREATE DATABASE` | The platform creates one database per school as schools are added. The `postgres` superuser is fine locally. |

There is no build toolchain, message queue or other service to run. `psycopg[binary]` is
installed from `requirements.txt`, so no PostgreSQL client libraries need to be on the PATH.

### Install

```bash
git clone <this-repo>
cd Academics

python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS/Linux

pip install -r requirements.txt

copy .env.example .env       # Windows
# cp .env.example .env       # macOS/Linux
```

### Configure

Edit `.env` and set at least:

| Variable | What to put |
|---|---|
| `BRIGHTSTARS_SECRET` | A real random secret. `python -c "import secrets; print(secrets.token_hex(32))"` |
| `BRIGHTSTARS_PLATFORM_DB` | Your PostgreSQL URL, e.g. `postgresql+psycopg://postgres:yourpassword@localhost:5432/brightstars_platform` |
| `BRIGHTSTARS_PLATFORM_HOSTS` | `platform.localhost` for local development |
| `BRIGHTSTARS_PORTAL_DOMAIN` | `localhost` for local development, so a school is reachable at `<code>.localhost` |

You do **not** need to create any database by hand: `python -m control_plane init` creates the
registry database, and each school's database is created when the school is created.

### Set the platform up

```bash
python -m control_plane init                          # create the registry
python -m control_plane create-platform-admin ops     # your own platform login (password prompted)
python app.py                                         # http://platform.localhost:5000
```

Then open `http://platform.localhost:5000/platform`, sign in, and use **Create a school**. Each
school you create is reachable immediately at `http://<school-code>.localhost:5000`.

`*.localhost` resolves to the loopback address in current browsers, so no hosts-file editing is
needed in development.

## Configuration

All configuration is read from environment variables, loaded from `.env` via
`python-dotenv` (a variable already set in the real environment always wins over `.env`).
`.env.example` documents every supported variable in full; the highlights:

| Variable | Purpose |
|---|---|
| `BRIGHTSTARS_ENV` | `development` or `production`. In production, `BRIGHTSTARS_SECRET` must be at least 32 characters or the app refuses to start. |
| `BRIGHTSTARS_SECRET` | Flask session-signing secret. Leaving it blank generates a new one on every restart, which logs everyone out. Generate one with `python -c "import secrets; print(secrets.token_hex(32))"`. |
| `BRIGHTSTARS_MAX_UPLOAD_BYTES` / `BRIGHTSTARS_MAX_REQUEST_BYTES` | Upload and request size limits. |
| `BRIGHTSTARS_SMTP_*` | Outgoing mail for receipts and password resets. Leave `BRIGHTSTARS_SMTP_HOST` blank to disable email delivery. |
| `BRIGHTSTARS_WHATSAPP_*` | WhatsApp Business Cloud API credentials for receipt delivery. Leave `BRIGHTSTARS_WHATSAPP_TOKEN` blank to disable it. |
| `BRIGHTSTARS_PLATFORM_DB` / `BRIGHTSTARS_PLATFORM_HOSTS` / `BRIGHTSTARS_TENANTS_DIR` | Platform registry database (PostgreSQL in production), the hostnames that serve the platform console and the platform's own website, and the folder holding each school's files. |
| `BRIGHTSTARS_PORTAL_DOMAIN` | The domain each school's portal address is issued under, e.g. `schools.brightstars.example`. |
| `PORT` | Port for the built-in development server (default `5000`). |

## Running the app

```bash
python app.py
```

This brings every registered school's schema up to date and starts the Flask development server on
`http://0.0.0.0:5000` (or `PORT`). On Windows, `run_windows.bat` does the same after
installing dependencies.

- `/` — public site
- `/login` — unified login (staff, parents, students, candidates)
- `/admin` — staff entry point (redirects to the neutral Workspace Home)
- `/practice` — no-login practice-test gateway
- `/health` — liveness check, returns `{"status": "ok"}`

For a LAN/production-style deployment, see [Deployment](#deployment).

## Multi-tenancy

Each school is a *tenant*: its own portal address, its own PostgreSQL database
(`brightstars_<code>`), its own folder of files, and no way to see another school's data. The
platform registry (`brightstars_platform`) holds which schools exist, their addresses and the
platform administrators.

Every school is issued a portal address the moment it is created,
`<code>.<BRIGHTSTARS_PORTAL_DOMAIN>`, which works immediately. A school that wants to use its own
domain points a CNAME record at that address; the console shows the exact DNS record.

Schools are normally created from the console. The CLI covers the same ground and a little more:

```bash
python -m control_plane init                                  # create the registry database
python -m control_plane create-platform-admin ops             # a platform login
python -m control_plane create-tenant demo "Demo School" --admin-username demo_admin
python -m control_plane list
python -m control_plane suspend demo --reason "invoice overdue"
python -m control_plane upgrade                               # bring every school's schema up to date
```

To bring an existing single-school SQLite installation in as a school, including all its data:

```bash
python -m control_plane register-existing crainbow "Creative Rainbow Montessori School"     --from-db cbt.db --from-data data --from-uploads static/uploads
python -m control_plane adopt-superadmin --from-db cbt.db     # its Super Admin becomes a platform admin
```

`docs/architecture/MULTI_TENANCY.md` has the design, the security model and the known gaps.

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

`tests/verification/write_paths_multitenancy.py` proves school isolation end to end (hostname
routing, per-school databases, session cookies copied between schools, uploads, question banks,
suspension) against throwaway databases, and
`tests/verification/write_paths_platform_console.py` drives the platform console the same way
(creating a school with its branding, portal addresses and CNAMEs, entering a school, and the
hostile cases around all of it).

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
- **Each school has its own PostgreSQL database**, named `brightstars_<code>`, plus one
  `brightstars_platform` registry. Back them up per school; restoring one school never touches
  another.
