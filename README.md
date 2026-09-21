<p align="center"><img src="static/brand/brightstars-logo.png" alt="Brightstars Academics" width="360"></p>

# Brightstars Academics

Brightstars Academics is a school platform. One deployment serves many schools, and each school
gets a portal of its own: its own web address, its own PostgreSQL database and its own folder of
files. No school can see, reach or affect another's data.

A school's portal runs entrance examinations, day-to-day administration (classes, subjects,
assignments, tests, results, promotion), student and parent self-service, finance and receipting,
a library catalogue, and role-based staff governance.

Two ideas shape everything here:

- **A school gets a portal, not a website.** A school's address serves sign-in and the staff,
  student, parent and candidate areas — nothing else. The deployment serves no public website
  at all: the platform's own marketing site is hosted separately, and its console address opens
  straight on the sign-in page.
- **The platform issues the address.** Creating a school immediately gives it a working address
  at `<school-code>.<portal-domain>`. A school that wants to use its own domain points a CNAME
  record at that address.

Multi-tenancy is the architecture, not a setting. There is no switch to turn it off.

## Contents

- [How it is arranged](#how-it-is-arranged)
- [What a school gets](#what-a-school-gets)
- [Getting started](#getting-started)
- [Creating a school](#creating-a-school)
- [Giving a school its own domain](#giving-a-school-its-own-domain)
- [The platform team and its activity log](#the-platform-team-and-its-activity-log)
- [Command line](#command-line)
- [Configuration](#configuration)
- [Project layout](#project-layout)
- [Testing](#testing)
- [Operating](#operating)
- [Importing an existing single-school installation](#importing-an-existing-single-school-installation)
- [Notes for contributors](#notes-for-contributors)
- [Known gaps](#known-gaps)

## How it is arranged

There are two kinds of hostname, and they never overlap.

| Hostname | Serves |
|---|---|
| `BRIGHTSTARS_PLATFORM_HOSTS` | The platform console (`/platform`); `/` on these hostnames goes to its sign-in page |
| `<school-code>.<BRIGHTSTARS_PORTAL_DOMAIN>` | That school's portal — issued when the school is created, and permanent |
| A school's own domain, CNAME'd to the above | The same portal |
| Anything else | `404`, before any application code runs |

Every request is resolved to a school by its `Host` header alone — never by a URL, form field or
cookie:

```
https://portal.theschool.example/login      (CNAME → theschool.<portal domain>)
        │
        ▼
control_plane.resolver     hostname → registry → school "theschool"
        │                  unknown host → 404 · suspended school → 503
        │                  a session issued for another school → discarded
        ▼
the selected school (a ContextVar, for this request only)
        │
        ├─ db.session ─────► that school's own PostgreSQL database
        ├─ core/storage ───► tenants/theschool/data     (question banks)
        ├─ core/branding ──► that school's name, motto and logo
        └─ /static/uploads ► tenants/theschool/uploads
```

**It fails closed.** A query made with no school selected raises rather than falling back to a
default database, because guessing a school could show one school's data to another.

**Who is in charge.** Platform operators are the only super admins: they create and suspend
schools, manage addresses, and can enter any school. Each school has its own administrators who
manage that school's staff, roles and data, and can see nothing outside it. No account is ever
seeded into a school — its first administrator is created deliberately, so the one-time password
reaches a named person.

## What a school gets

**Entrance examinations** — question banks, candidate registration and credentials, timed
computer-based papers with server-side timing and scoring, a frozen per-attempt question snapshot
(so editing a bank never changes an exam already taken), retake grants, rankings, and CSV/JSON
export.

**School administration** — academic sessions, classes, subjects, student enrolment and
promotion between sessions, assignments and projects (written and quiz variants), tests, practice
and examinations sharing the exam engine's attempt and grading model, and term results with a
staged verify → approve → release workflow.

**Students and parents** — students sit assignments and assessments and track results once
released; parents follow their children's results, attendance and fee status, and exchange
messages with the school.

**Finance** — fee items and per-student assessments, payment recording and allocation,
outstanding balances, and receipts delivered as PDF, email or WhatsApp.

**Library** — a book and loan catalogue.

**Governance** — staff accounts, custom roles built from a fine-grained permission catalogue,
scope-limited access (a class teacher restricted to their own classes), an audit log, internal
messaging, and resource locks such as freezing a question bank during a live exam.

**Its own identity** — name, motto, tagline, contact details and logo, captured when the school
is created and shown across its portal, result cards and receipts. A school also gets its own
**brand colours** (its menus, headers, buttons and sign-in page follow them) and up to eight
**photographs** that fade one into the next beside its sign-in form.

## Getting started

### Requirements

| Requirement | Notes |
|---|---|
| **Python 3.10+** | Developed and tested on 3.13. |
| **PostgreSQL 14+** | Required, in development as well as production. Tested against PostgreSQL 18. |
| A role that may `CREATE DATABASE` | Schools' databases are created on demand. Locally, the `postgres` superuser is fine. |

No build toolchain, message queue or other service is needed. The PostgreSQL driver
(`psycopg[binary]`) installs from `requirements.txt`, so no client libraries need to be on the
`PATH`.

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

Edit `.env`. Four values matter to get running; `.env.example` documents the rest.

| Variable | What to put |
|---|---|
| `BRIGHTSTARS_PLATFORM_DB` | Your PostgreSQL URL, e.g. `postgresql+psycopg://postgres:yourpassword@localhost:5432/brightstars_platform` |
| `BRIGHTSTARS_SECRET` | A real random secret — `python -c "import secrets; print(secrets.token_hex(32))"` |
| `BRIGHTSTARS_PLATFORM_HOSTS` | `platform.localhost` for development |
| `BRIGHTSTARS_PORTAL_DOMAIN` | `localhost` for development |

You do not need to create any database by hand. `python -m control_plane init` creates the
registry, and each school's database is created with the school.

### First run

```bash
python -m control_plane init                       # create the registry database
python -m control_plane create-platform-admin ops  # your own platform login (password prompted)
python app.py
```

Open `http://platform.localhost:5000/` and sign in. Browsers resolve `*.localhost` to the
loopback address, so no hosts-file editing is needed in development. The first platform admin you
create is the platform's **super admin** (see below).

Plain `http://localhost:5000/` belongs to no school and no console, so it shows an "address not
found" page. In development that page also prints the console's address and the shape of a
school's, which is the usual thing you were looking for.

## Creating a school

From the console — **Create a school** — which is the normal path, because that form also captures
the school's branding and logo:

- **Name and code.** The code is permanent: it forms the portal address and names the school's
  database and folder.
- **Branding.** Name, motto, tagline, phone, email, address and a logo. Set now so nobody ever
  sees the portal wearing the wrong name.
- **Sign-in photographs.** Up to eight pictures of the school, chosen together in one go. They are
  stored in the school's own folder and shown on its sign-in page.
- **Brand colours.** A main and an accent colour, with a live preview. Both sit behind white text,
  so a colour too light to read it is refused (WCAG AA contrast, 4.5:1) — the form warns as you
  pick, and the server enforces it. Choosing the portal's own colours means "no choice".

Colours, logo and photographs can all be changed afterwards, by either side:

- **Platform operators**, from the school's page in the console (**Look of the portal**).
- **The school's own administrators**, from **Branding** in their admin area's menu. It is
  guarded by the `branding.manage` permission, which the school's top-level administrator holds
  and can grant to any role. A school that existed before the permission was introduced gains it
  on the next start; no other role is given it automatically.

Both use the same rules (`core/branding.py`), so they cannot disagree about what is allowed.
Images must be PNG, JPG, GIF or WEBP, each up to `BRIGHTSTARS_MAX_UPLOAD_BYTES` (5 MB by
default). The pages that take a logo plus a full gallery raise the request limit to fit it; every
other request keeps `BRIGHTSTARS_MAX_REQUEST_BYTES`.
- **Its own domain** (optional, addable later).
- **First administrator** (optional). A one-time password is shown once and must be changed at
  first sign-in.

The school is reachable the moment it is created, at `http://<code>.localhost:5000` in
development.

The CLI can do the same without branding:

```bash
python -m control_plane create-tenant demo "Demo School" --admin-username demo_admin
```

## Giving a school its own domain

The issued portal address always works and cannot be removed. To use the school's own address as
well, add it in the console (or with `add-domain`), then have the school create one DNS record:

```
portal.theschool.example.   CNAME   theschool.schools.brightstars.example.
```

The console shows the exact record on the school's page. Your reverse proxy needs a certificate
for each hostname it serves — a wildcard for the portal domain, plus one per school domain — and
must pass the original `Host` header through unchanged, because that is what selects the school.

## The platform team and its activity log

There are two kinds of platform admin, and both have **full control over every school** — create,
brand, suspend, and enter any of them.

| | Platform admin | Super admin |
|---|---|---|
| Create, brand, suspend and enter schools | yes | yes |
| Add, remove, restore and reset other admins | no | **yes** |
| Read what each admin has done | only their own log | **everyone's** |
| Can be removed | by the super admin | **never** |

The super admin is the overall admin who can always look over and protect the system. The first
admin created becomes one; the console can never create another (do that deliberately from the
command line with `--super`). A platform that already had admins before roles existed promotes
its earliest admin automatically, so upgrading needs no manual step.

**Adding an admin** (Team page, super admin only) generates a one-time password that is shown once.
The new admin must replace it the first time they sign in, and nothing else in the console opens
until they have.

**Removing an admin** never deletes the account. It switches their access off everywhere at once:
their console session ends, tickets they had not yet used die, and their reserved account inside
every school is switched off, so a session they already had open *inside a school* stops at the
next click. The account and its whole log are kept, and the super admin can restore them (with a
new temporary password) or reset any admin's password. If a school cannot be reached while
removing someone, the console says which, so it is never silently missed.

**The activity log** is arranged as *choose an admin, then read their log*: the super admin picks
anyone (or Everyone, or a removed admin) and reads their entries, filtered by Schools, Accounts &
team, or Sign-ins. It records what each admin did to schools (create, suspend, reactivate, addresses,
branding, first administrators, every entry into a school) and within the platform itself (adding,
removing, restoring and resetting admins, password changes, and every sign-in, sign-out and
failed or refused attempt on a real account, with the address it came from). It also shows what
each admin did **inside schools** (the *Inside schools* view, and merged into *Everything*). That is
read, read-only, from each school's own audit trail, where an admin's actions are recorded under
their reserved `platform@<username>` account. Only schools the admin has entered are opened, and a
school that cannot be reached is named on the page rather than silently leaving entries out. Any
other admin sees only their own log.

## Command line

```
python -m control_plane <command>

  init                          create the platform registry database and tables
  create-platform-admin USER [--super]   add a platform admin (the first is the super admin)
  create-tenant CODE "Name"     create a school (portal address issued automatically)
  register-existing CODE "Name" --from-db FILE    import a single-school SQLite installation
  adopt-superadmin --from-db FILE                 make its Super Admin a platform operator
  list                          every school, its addresses and its database
  upgrade [CODE]                bring school database(s) up to the current schema
  add-domain CODE HOST [--primary] / remove-domain HOST
  suspend CODE [--reason TEXT] / activate CODE
```

## Configuration

Everything is read from the environment, loaded from `.env`. `.env.example` documents every
variable; the ones that shape the deployment:

| Variable | Purpose |
|---|---|
| `BRIGHTSTARS_PLATFORM_DB` | PostgreSQL URL of the platform registry. Required — there is no default, because a wrong guess would silently create an empty registry and make every school look as though it did not exist. |
| `BRIGHTSTARS_PLATFORM_HOSTS` | Hostnames serving the platform website and console. |
| `BRIGHTSTARS_PORTAL_DOMAIN` | Domain each school's portal address is issued under. |
| `BRIGHTSTARS_TENANTS_DIR` | Folder holding each school's files. |
| `BRIGHTSTARS_SECRET` | Session-signing secret. In production it must be at least 32 characters or the app refuses to start. |
| `BRIGHTSTARS_ENV` | `development` or `production`. |
| `BRIGHTSTARS_SCHOOL_DB_TEMPLATE` | Optional: place schools' databases on another server. |
| `BRIGHTSTARS_REGISTRY_CACHE_SECONDS` | How long a hostname lookup is cached per worker, which bounds how quickly a suspension takes effect. |
| `BRIGHTSTARS_SMTP_*` / `BRIGHTSTARS_WHATSAPP_*` | Receipt and password-reset delivery. |

## Project layout

```
Academics/
├── app.py                    # Flask/SQLAlchemy setup, request hooks, error handlers,
│                             #   context processor, blueprint registration, shared helpers
├── control_plane/            # The platform itself
│   ├── models.py             #   registry tables: Tenant, TenantDomain, PlatformAdmin,
│   │                         #     PlatformEntryToken, PlatformAuditLog (own database)
│   ├── registry.py           #   registry engine/session, hostname → school, validation
│   ├── resolver.py           #   the before_request hook; binds sessions to their school
│   ├── routing.py            #   TenantSession, per-school engines, database creation
│   ├── context.py            #   the current school for this request
│   ├── provisioning.py       #   create/import/upgrade schools, branding, domains, suspend
│   ├── entry.py              #   platform sign-in, entry tickets, "Enter school"
│   ├── team.py               #   platform admins: add/remove/restore/reset, and the activity log
│   ├── console.py            #   the platform console
│   └── cli.py                #   python -m control_plane …
├── models/                   # One school's schema, one module per domain
│   ├── base.py               #   the shared db, built on TenantSession
│   ├── auth.py               #   Admin, AdminType, Permission, AuditLog, messaging, locks
│   ├── school.py             #   sessions, classes, students, assignments, results, promotion
│   ├── entrance.py           #   Examination, Attempt, Answer, Candidate
│   ├── finance.py            #   fee items, payments, allocations
│   ├── library.py            #   books and loans
│   ├── parents.py            #   parent accounts, links, feedback
│   ├── public.py             #   the school's settings, pages and news
│   ├── admissions.py         #   admission profiles and enrolment history
│   ├── tenancy.py            #   the school's own row, settings and number allocations
│   ├── presence.py           #   presence, notifications, password-reset tokens
│   └── governance.py         #   SchemaMigration (historical record)
├── core/                     # Cross-cutting helpers — no routes
│   ├── db_helpers.py         #   query helpers, and the dialect-neutral upsert/aggregate
│   ├── security.py           #   RBAC, admin_required, csrf_protect, audit_log
│   ├── branding.py           #   the school's own name, motto, logo, colours, gallery, receipt prefix
│   ├── theme.py              #   brand-colour and gallery rules: validation, contrast, theme CSS
│   ├── storage.py            #   the school's data/ and uploads/ folders
│   ├── accounts.py           #   shared sign-in/out helpers
│   ├── entrance.py           #   question banks, grading, result rendering
│   ├── notifications.py      #   guardian email/WhatsApp
│   ├── presence.py           #   who is online
│   ├── public_settings.py    #   the school's settings lookup
│   └── uploads.py            #   image upload validation
├── blueprints/               # One package per route domain
│   ├── auth/                 #   sign-in, sign-out, password recovery
│   ├── school/               #   the school portal
│   ├── entrance/             #   entrance-exam administration
│   ├── candidate_portal/     #   candidates' own side
│   ├── student_portal/       #   students' own side
│   ├── parents/              #   parent portal and parent administration
│   ├── finance/              #   fees, payments, receipts
│   ├── library/              #   library administration
│   └── administration/       #   accounts, roles, permissions, messaging
├── services/                 # Small dependency-free utilities
├── templates/                # Jinja templates; templates/platform/ is the console and site
├── static/                   # CSS/JS/images shared by every school
├── tenants/                  # Per school: <code>/data and <code>/uploads (gitignored)
├── migrations/               # Historical schema-change record (see Notes)
├── tests/
│   ├── current/              #   the authoritative contract suite
│   ├── verification/         #   end-to-end scripts against real databases
│   └── legacy/               #   retained historical scripts, not authoritative
├── deployment/               # Deployment notes and helper scripts
└── docs/architecture/        # Design notes, including MULTI_TENANCY.md
```

Blueprints register routes on the shared Flask `app` with plain `@app.route` rather than
`Blueprint` objects — see [Notes for contributors](#notes-for-contributors).

## Testing

```bash
python tests/current/run_current.py
```

The authoritative contract suite: architecture, security, assessment flow, RBAC and
multi-tenancy invariants. Run it after any change. It needs no database, except that the
schema-drift check compares the models against the first registered school when one is reachable.

Two end-to-end suites drive the real application over HTTP against PostgreSQL. Each creates its
own throwaway databases (`bs_test_*`), drops them afterwards, and clears any left behind by a
crashed run — they never touch real data.

```bash
python tests/verification/write_paths_multitenancy.py      # isolation between schools
python tests/verification/write_paths_platform_console.py  # the console, end to end
python tests/verification/write_paths_portal_branding.py   # colours, logo and sign-in photographs
python tests/verification/write_paths_platform_team.py     # the team, roles, removal, activity logs
```

Between them they cover hostname routing, per-school databases, session cookies copied between
schools, path traversal, uploads, question banks, suspension, creating a school with its branding
and logo, colours and photographs (including hostile input), portal addresses and CNAMEs, and
entering a school. `tests/verification/` also holds
per-feature write-path scripts, and `tests/legacy/` retained historical ones.

## Operating

**Backups are per school.** Each school is a separate PostgreSQL database (`brightstars_<code>`)
plus a folder under `tenants/<code>/`. Restoring one school never touches another.

**Suspending** a school takes its portal offline for everyone — staff, parents and students —
within `BRIGHTSTARS_REGISTRY_CACHE_SECONDS` on each worker. Its data is untouched and returns on
reactivation.

**Upgrades.** `python app.py` brings every registered school's schema up to date before serving.
With many schools, run `python -m control_plane upgrade` as a deploy step instead.

**Connections.** Each school has its own connection pool, so total connections scale with
schools × pool size × workers. Size PostgreSQL's `max_connections` accordingly, or put a pooler
in front. With PgBouncer in transaction mode, use a database per school rather than a schema per
school.

**More than one application server** needs `BRIGHTSTARS_TENANTS_DIR` on shared storage, or an
object-store backend behind `core/storage.py`.

## Importing an existing single-school installation

One command creates the school and imports a SQLite database into it, table by table in
dependency order, resetting identity sequences afterwards:

```bash
python -m control_plane register-existing theschool "The School" \
    --from-db old.db --from-data data --from-uploads static/uploads
python -m control_plane adopt-superadmin --from-db old.db
```

The source is opened read-only and never modified, so it remains a rollback until you retire it.
Check student, result and payment counts against the old database before doing so.

## Notes for contributors

- **Blueprints use plain `@app.route`, not `Blueprint` objects.** Every `blueprints/*/routes.py`
  does `from app import app` and decorates that shared instance. This keeps every endpoint name —
  and therefore every `url_for(...)` and every permission-mapping entry — stable, at the cost of
  Blueprint features like URL prefixes.
- **Never use `db.engine`.** It names the default bind, which is the registry and holds no school
  data. Use `control_plane.routing.current_engine()`.
- **Never reach for a SQLite-only construct.** Upserts go through `core.db_helpers.insert_stmt()`
  and string aggregation through `group_concat()`; partial indexes declare `postgresql_where`
  alongside `sqlite_where`. A contract test enforces this.
- **Never hard-code a school's name, motto or logo.** Use `school_brand` in templates and
  `core.branding.school_name()` in code. A contract test fails the build on any occurrence.
- **`migrations/` is a historical record, not a runner.** The schema is declared in `models/` and
  realised by `create_all()` plus a column-diffing helper at startup; the SQL bodies no longer
  execute and are kept so `schema_migrations` stays an accurate record.
- **Question banks are JSON files** under `tenants/<code>/data/`. A new school starts with none.

## Known gaps

- **A school's top-level role is still called "Super Admin" internally.** It behaves as a school
  administrator and platform operators outrank it, but the rename has not been done.
- **Delivery settings are deployment-wide.** Every school sends receipts and password resets
  through the same SMTP and WhatsApp accounts; per-school settings are not built.
- **`LIKE` searches are now case-sensitive.** PostgreSQL is stricter than SQLite here, and around
  21 name and username search call sites in `blueprints/` have not been reviewed or converted to
  `ilike`. The test suites do not cover those paths.
- **The school website editor edits pages nobody can see.** `/admin/school/website` still offers
  page and news editing, and an enquiry inbox fed by a contact page that no longer exists. Its
  branding fields are still used and should stay.
- **Message attachments are reachable without authentication** at their `/static/uploads/…` URL
  if the exact file name is known, alongside the permission-checked download route for the same
  files.
- **A failed sign-in shows no explanation** — the login template renders only flashed messages,
  not the `error` value the view passes it.
