<p align="center"><img src="static/brand/brightstars-logo.png" alt="Brightstars Academics" width="360"></p>

# Brightstars Academics

Brightstars Academics is a school platform. One deployment serves many schools, and each school gets a portal of its
own: its own web address, its own PostgreSQL database and its own files. No school can see, reach or affect another's
data.

A school's portal runs entrance examinations and admissions, day-to-day teaching (classes, subjects, work, tests,
results, report cards, attendance, timetables, promotion), finance and receipts, a school store, a library, and
self-service portals for students and parents, all under role-based staff access.

Two ideas shape everything:

- **A school gets a portal, not a website.** A school's address serves sign-in and the staff, student, parent and
  candidate areas, nothing else. The platform's marketing site is hosted separately.
- **The platform issues the address.** Creating a school gives it `<school-code>.<portal-domain>` at once. A school
  that wants its own domain points a CNAME at that address.

Multi-tenancy is the architecture, not a setting. There is no switch to turn it off.

**Current version:** the newest release in [release_notes.json](release_notes.json) (shown at the foot of every
sign-in page).

---

## Find it fast

| I want to… | Go to |
|---|---|
| Run it on my machine | [Get running](#1-get-running) |
| Create a school, or give it its own domain | [Creating a school](#creating-a-school) · [Its own domain](#giving-a-school-its-own-domain) |
| Know what a school can do | [What a school gets](#2-what-a-school-gets) |
| Manage the platform team, numbering, question banks, email, payments | [Running the platform](#3-running-the-platform) |
| Look up a setting or a command | [Configuration](#4-configuration) · [Command line](#command-line) |
| Understand sign-ins, timeouts and security | [Security and sign-ins](#5-security-and-sign-ins) |
| Deploy to production | [Production](#6-production) |
| Change the code, run the tests, announce a change | [Developing](#7-developing) |
| See what is still to do | [Known gaps](#known-gaps) |

## Contents

1. [Get running](#1-get-running): [requirements](#requirements) · [install](#install) · [configure](#configure) ·
   [first run](#first-run) · [creating a school](#creating-a-school) · [its own domain](#giving-a-school-its-own-domain)
2. [What a school gets](#2-what-a-school-gets): [workspaces](#workspaces-and-settings) · [staff and access](#staff-roles-and-teaching-duties) ·
   [classes and subdivisions](#classes-subdivisions-and-transfers) · [students](#students-imports-and-archiving) ·
   [teaching and results](#teaching-results-and-report-cards) · [attendance and timetables](#attendance-and-timetables) ·
   [entrance and admissions](#entrance-examinations-and-admissions) · [finance](#finance-receipts-and-online-payments) ·
   [store and library](#the-store-and-the-library) · [students and parents](#the-student-and-parent-portals) ·
   [notices and help](#whats-new-the-setup-checklist-and-the-staff-guide)
3. [Running the platform](#3-running-the-platform): [the console and team](#the-platform-console-and-team) ·
   [docs, marketing, privacy](#documentation-the-marketing-page-and-the-privacy-statement) · [numbering](#how-a-school-numbers-its-people) ·
   [question banks](#question-banks) · [email and SMS](#email-and-sms) · [command line](#command-line)
4. [Configuration](#4-configuration)
5. [Security and sign-ins](#5-security-and-sign-ins)
6. [Production](#6-production): [checklist](#deployment-checklist) · [operating](#operating)
7. [Developing](#7-developing): [layout](#project-layout) · [conventions](#conventions) · [tests](#tests) ·
   [announcing a change](#announcing-a-change)
8. [Known gaps](#known-gaps)

---

## How it is arranged

There are two kinds of hostname, and they never overlap.

| Hostname | Serves |
|---|---|
| `BRIGHTSTARS_PLATFORM_HOSTS` | The platform console (`/platform`), `/docs`, `/marketing`; `/` opens the console's sign-in |
| `<school-code>.<BRIGHTSTARS_PORTAL_DOMAIN>` | That school's portal: issued when the school is created, permanent |
| A school's own domain, CNAME'd to the above | The same portal |
| Anything else | `404`, before any application code runs |

Every request is resolved to a school by its `Host` header alone, never by a URL, form field or cookie:

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
        ├─ core/storage ───► its question banks and uploads (local folder or S3)
        └─ core/branding ──► its name, motto, logo and colours
```

- **It fails closed.** A query made with no school selected raises rather than falling back to a default database.
- **Who is in charge.** Platform operators create and suspend schools, manage addresses, and can enter any school.
  Each school's own administrators manage that school and can see nothing outside it. No account is ever seeded into
  a school: its first administrator is created deliberately, so the one-time password reaches a named person.

---

# 1. Get running

### Requirements

| Requirement | Notes |
|---|---|
| **Python 3.10+** | Developed and tested on 3.13 and 3.14. |
| **PostgreSQL 14+** | Required in development as well as production. Tested against PostgreSQL 18. |
| A role that may `CREATE DATABASE` | Schools' databases are created on demand. Locally, `postgres` is fine. On a managed provider with no database called `postgres` (Aiven's default is `defaultdb`), set `BRIGHTSTARS_PG_MAINTENANCE_DB`. |
| Optional: Chrome or Chromium | Draws a candidate's result image. Found automatically, or set `BRIGHTSTARS_CHROME`. |
| Optional: `pg_dump` | Only for `export-tenant` / `delete-tenant` (a school's export). Set `BRIGHTSTARS_PG_DUMP` if it is not on the `PATH`. |

No build toolchain, message queue or other service is needed.

### Install

```bash
git clone <this-repo>
cd BrightStar-Academics

python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS/Linux

pip install -r requirements.txt

copy .env.example .env         # Windows
# cp .env.example .env         # macOS/Linux
```

### Configure

Edit `.env`. Four values get you running; `.env.example` documents every other one, and
[Configuration](#4-configuration) lists the ones that shape a deployment.

| Variable | What to put |
|---|---|
| `BRIGHTSTARS_PLATFORM_DB` | Your PostgreSQL URL, e.g. `postgresql+psycopg://postgres:yourpassword@localhost:5432/brightstars_platform` |
| `BRIGHTSTARS_SECRET` | A real random secret: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `BRIGHTSTARS_PLATFORM_HOSTS` | `platform.localhost` for development |
| `BRIGHTSTARS_PORTAL_DOMAIN` | `localhost` for development |

Leave `BRIGHTSTARS_ENV=development` on your own machine. You do not create any database by hand.

### First run

```bash
python -m control_plane init                       # create the registry database
python -m control_plane create-platform-admin ops  # your own platform login (password prompted)
python app.py
```

Open `http://platform.localhost:5000/` and sign in. Browsers resolve `*.localhost` to your machine, so no hosts-file
editing is needed. The first platform admin is the platform's **super admin**. Plain `http://localhost:5000/` belongs
to no school; in development it prints the console's address and the shape of a school's.

### Creating a school

From the console, **Create a school** (the CLI can do it too, without branding:
`python -m control_plane create-tenant demo "Demo School" --admin-username demo_admin`).

- **Name and code.** The code is permanent: it forms the portal address and names the school's database and folder.
- **Branding.** Name, motto, tagline, contact details, a logo, up to eight sign-in photographs, and two brand colours
  (a colour too light for white text is refused: WCAG AA, 4.5:1). Changeable later by platform operators (the school's
  page, *Look of the portal*) or by the School Admin (*Settings › School profile*); both use the same rules
  (`core/branding.py`).
- **Question banks.** Ticked by default: the platform's standard entrance banks (`starter_banks/`) are copied in, so
  its entrance exam can run at once. Untick it, or pass `--no-starter-banks`.
- **Numbering.** How it writes candidate codes and student numbers (default already filled in; see
  [numbering](#how-a-school-numbers-its-people)).
- **Its own domain** and **a first administrator** (both optional). The administrator's one-time password is shown once
  and must be changed at first sign-in.

The school is reachable the moment it is created, at `http://<code>.localhost:5000` in development.

### Giving a school its own domain

The issued address always works and cannot be removed. Add the school's own address in the console (or
`add-domain`), then the school creates one DNS record, shown on its console page:

```
portal.theschool.example.   CNAME   theschool.schools.brightstars.example.
```

Your reverse proxy needs a certificate for each hostname (a wildcard for the portal domain, plus one per school
domain) and must pass the original `Host` header through unchanged, because that is what selects the school.

---

# 2. What a school gets

Each area is explained for staff in the **staff guide** (`/admin/guide` on any school) and for developers in
**`/docs`** on the platform host. This is the map.

### Workspaces and settings

- **Two workspaces.** *School* (everyday running) and *Entrance Examination*. Most staff only have the School
  workspace and go straight in; someone with both chooses on *Workspace Home* (one press, which opens the workspace in
  one request). The boundary is enforced (`enforce_admin_workspace_boundary` in `app.py`).
- **Settings** is one hub of every setting, each shown only to whoever may open it (`blueprints/school/settings.py`):
  school profile, academic sessions, class subdivisions, staff, roles, report card settings, Email & SMS, online
  payments, receipts, controls and alerts, the activity and notice logs, and who is online.
- **An installable app.** Each portal can be installed on a phone or computer in the school's own name and logo
  (`blueprints/pwa.py`). Pages load in place, with a loading outline between pages.

### Staff, roles and teaching duties

- **Roles and permissions.** Each permission is defined once (`ADMIN_PERMISSION_DEFS`, `core/security.py`); roles bundle
  them; a school starts with ready-made roles (Head Teacher, Class Teacher, Subject Teacher, Finance Manager, Store
  Keeper, Librarian…). The School Admin role holds everything. Staff, roles, Email & SMS, online payments and the
  school profile are kept for the School Admin alone.
- **The chain of authority** (`core/school_structure.py`): School Admin, then executive (bursar, admissions, secretary…),
  head teacher, class teacher, subject teacher.
- **Teaching duties** decide *where* a teacher works: class teacher of a class (or of one subdivision of it), or a
  subject in a class (and, in SSS, a department). Results move subject teacher → class teacher, who releases them.
  Set on the staff form or the *Teachers* page.

### Classes, subdivisions and transfers

- **Classes** from Crèche and Nursery to SSS 3 are seeded (early years switched off until a school runs them; their
  pupils never sign in). **SSS students belong to a department** (Science, Art, Commercial), and each subject lists the
  departments that take it.
- **Class subdivisions** (*Settings › Class subdivisions*): a JSS or SSS class can be split into JSS 1A, JSS 2 Gold,
  SS 1A (Science)…, each with its own class teacher, then locked. Students are placed automatically, in turn, into the
  subdivision with the fewest students (`core/subdivisions.py`). The class stays one class for subjects, fees and
  results; a subdivision only scopes its class teacher's register, comments and releases.
- **Class transfers** (menu, for JSS/SSS class teachers): moving a student to another subdivision or department is a
  request the receiving class teacher approves (or the School Admin or a head teacher).

### Students, imports and archiving

- **Registering** creates the record, the enrolment and a login with a generated admission number.
- **Bulk import** has three steps: students, enrolment history, past results. Each takes a **CSV or an Excel workbook
  (.xlsx)**, read defensively (`core/tabular.py`: macros and password-protected files refused, zip bombs and hostile
  XML caught, formulas never run). A bad row is skipped with its reason; every good row is imported. Each step needs
  its own permission, or `school.bulk_import`.
- **Archiving** keeps the record and ends the child's sign-in. Parents keep their children who have left, in their own
  section of the parent portal. The School Admin can restore a student.

### Teaching, results and report cards

- **Work**: assignments and projects (written or quiz), tests, examinations and practice, sharing one attempt and
  grading model; issued to a whole class at once; work with marks cannot be deleted, other work can be restored.
- **Results**: Exam 60 + CA 40, a workflow recorded → sent to the class teacher → released, and a school-wide release
  date.
- **Report cards** are worked out from the released results (never stored), as a page and a PDF: the school's identity,
  CA and exam per subject with grade and remark, class averages, trait ratings, the class teacher's comment and
  signature, the head's signature. A card appears only when every result of the term is released.
- **Promotion** between sessions, in drafts the School Admin approves.

### Attendance and timetables

- **Attendance**: one register per class per day (present, late, absent, excused), a term summary, and each student's
  and parent's own view.
- **Exam and test timetables**: built as a draft, then released at once, which notifies students and parents (in-app,
  email, SMS). Students and parents see their own class's, with a PDF.

### Entrance examinations and admissions

- **Question banks** per school (JSON files), the platform's standard set or the school's own. **Papers** are timed,
  computer-based, scored on the server, from a frozen per-attempt copy of the questions.
- **SSS 1 candidates choose a department**; the General Knowledge paper gives an equal share of each department's
  questions, and the waitlist suggests the department the candidate did best in.
- **Admissions**: every candidate who sat a paper is on the waitlist; *admitting* creates a real student (and a
  parent account), exactly like registering one; *declining* can be undone. Results are released to candidates as a
  separate step.
- **Public entrance practice** at `/entrance-practice` on the school's address.

### Finance, receipts and online payments

- **Fee items** (by class, per session, term or one-time), **billing** a student or a whole class, **payments** and
  **allocating** them to fees ("paid" means applied to a fee), balances across every session.
- **New students to bill**: every student who joins (admitted, registered or imported) is listed for the bursar until
  billed and paid in full.
- **Receipts** in the school's own design (page, print, PDF), sent to the guardian by email and SMS automatically.
- **Online payments** through the school's own Paystack account (no shared platform account): confirmed only by asking
  Paystack, never by trusting the browser, with refunds.

### The store and the library

- **Store**: items with pictures and stock that cannot be oversold, payments taken at the office or online, handing
  purchases over with a text to the parent.
- **Library**: books, members and loans.

### The student and parent portals

- **Students**: their work, tests and practice, released results, report cards, attendance, timetable.
- **Parents**: every linked child (with children who have left in their own section), fees and paying them, receipts,
  results, report cards, attendance, timetable, the store, and messages with the school.

### What's new, the setup checklist and the staff guide

- **What's new** is announced from [release_notes.json](release_notes.json) to each audience (staff, family, student),
  kept in the browser: no database read on an ordinary page.
- **The setup checklist** helps a new school through its first steps (cached for fifteen minutes).
- **The staff guide** (`/admin/guide`): seventeen plain-language pages in six groups, with search. Any staff member can
  read it.

---

# 3. Running the platform

### The platform console and team

Two kinds of platform admin, both with **full control over every school** (create, brand, suspend, enter):

| | Platform admin | Super admin |
|---|---|---|
| Create, brand, suspend and enter schools | yes | yes |
| Permanently delete a school | only if granted | **yes** |
| Add, remove, restore and reset other admins | no | **yes** |
| Read what each admin has done | only their own log | **everyone's** |
| Can be removed | by the super admin | **never** |

- The first admin created is the super admin; the console never creates another (use `--super` on the command line).
- **Adding an admin** generates a one-time password, shown once and changed at first sign-in.
- **Removing an admin** never deletes them: their access ends everywhere at once (console, tickets, their reserved
  account in every school). The super admin can restore them.
- **Entering a school** uses a single-use ticket and a reserved `platform@<username>` account inside the school, so
  every action is recorded on both sides.
- **The activity log**: choose an admin, then read what they did to schools, to the team, at sign-in, and inside
  schools.
- **Settings and actions** (`/platform/settings`): every environment variable explained, with its risk, and the
  maintenance actions a highly-trusted admin may run.

### Documentation, the marketing page and the privacy statement

- **`/docs`**: the developer documentation. Architecture, deployment, security, testing, and a reference (routes,
  tables, permissions, modules, templates) generated from the code. Only for platform admins given access (the super
  admin grants it).
- **`/marketing`**: the public page explaining the product. No sign-in.
- **`/privacy`**: the NDPR-aligned data-protection statement, on platform hosts and every school's address. Fill in the
  bracketed details (legal name, registration number, address, Data Protection Officer) before using it.

`tests/current/test_docs_contract.py` checks every file, route, table and permission the documentation names still
exists, and the access rules.

### How a school numbers its people

Each school's candidate codes and student numbers follow its own **pattern**, set by the platform team (on *Create a
school* and the school's **Numbering** section) and kept in the school's own `numbering.json`. The default candidate
pattern is `{school}-{year}-{seq:4}` (`ABC-2026-0001`); the student pattern is empty by default (the school's
numbering policy, e.g. `ABC/2026/0001`).

| Write | It gives | Example |
|---|---|---|
| `{school}` | the school's code, in capitals | `ABC` |
| `{initials}` | the initials of the school's name | `Bright Future Academy` → `BFA` |
| `{year}` `{yy}` | 4-digit / 2-digit year | `2026` / `26` |
| `{month}` `{mon}` | 2-digit / 3-letter month | `09` / `SEP` |
| `{day}` | 2-digit day | `05` |
| `{class}` | the class applied for, no spaces, capitals (candidates only) | `JSS 1` → `JSS1` |
| `{seq}` · `{seq:4}` · `{seq:4-5}` | the running number: unpadded · exactly 4 digits · at least 4, up to 5 | `7` · `0007` · `0007` |
| `{random:N}` · `{letters:N}` · `{alnum:N}` | N random digits · capitals (no I/O) · capitals and digits | `4827` · `KWTB` · `K7WB` |
| `{prefix}` | the numbering policy's prefix (students only) | `ABC` |

- A pattern needs `{seq}` or a random part. Plain text may use letters, digits and `-` `_` `.` (and `/` in student
  numbers); `|lower` / `|upper` change case.
- `{seq}` counts within codes that match in every other part, so it restarts each year if `{year}` is in it, each
  class with `{class}`, and so on. A taken code moves to the next number.
- **Nothing typed is ever run.** A pattern is read by `core/numbering_pattern.py`, never executed. Codes already issued
  never change. The console shows a live preview of the next codes.

### Question banks

- Each school's banks are JSON files in its own storage; no school can read another's.
- **The standard set** (`starter_banks/`): Mathematics, English and General Knowledge for Year 7 and SSS 1, written
  for the platform, answers reviewed independently. Have a subject teacher review them before a school relies on them.
- **A school's own bank**: *Question Banks › Import from file* (checked strictly: size, safe id, 1–500 questions, four
  options each, an answer; replacing needs a tick and the edit permission).
- **Live papers**: *Exam Configuration › Set up the standard entrance papers*, or by hand.
- **Entrance practice pool** (`practice_banks/`, built by `tools/build_practice_banks.py`): 200–475 ready-made practice
  questions per class and subject, shipped with the app and read-only. They are added to every school's practice pool
  with its earlier papers and its own practice bank, never copied into a school or used as an examination. Mathematics
  answers are computed by the generator; English and General Knowledge come from its reviewed lists. Edit the script and
  rerun it rather than editing the JSON.
- **Marks** are always out of 100 and kept as decimals (40 questions → 2.5 marks each).

### Email and SMS

What parents receive (receipts, password recovery, alerts) goes out from **the school's own accounts**, set up by the
School Admin under *Settings › Email & SMS* (a mail server, a BulkSMS Nigeria account), each with a test button.

- **The platform's shared account is the fallback** (`BRIGHTSTARS_SMTP_*`, `BRIGHTSTARS_SMS_*`) for a school that set
  up nothing; a half-set-up school never borrows it.
- **Secrets are encrypted at rest**, per school, and never shown again (`core/delivery.py`). Rotate with
  `rotate-delivery-key`.
- **The server cannot be aimed at its own network**: in production a school's mail host must resolve to public
  addresses only, on standard mail ports, checked again just before connecting.
- Sending is a **durable background job** (`core/jobs.py`): nobody waits on a mail server, and a job that never
  finished is retried (`sweep-jobs`).

### Command line

```
python -m control_plane <command>

  init                                   create the platform registry tables
  create-platform-admin USER [--super]   add a platform admin (password prompted; the first is the super admin)
  create-tenant CODE "Name" [--domain HOST] [--admin-username USER] [--no-starter-banks]
  list                                   every school, its addresses and its database
  upgrade [CODE]                         bring school databases up to the current schema (with no CODE, also records
                                         a new launch: everyone signs in again, except people mid-exam)
  new-launch                             record a new launch only
  sweep-jobs [CODE]                      retry stuck background jobs and send due birthday texts (run on a timer)
  add-domain CODE HOST [--primary]  ·  remove-domain HOST
  suspend CODE [--reason TEXT]  ·  activate CODE
  create-db-role CODE [--rotate] [--password PW]   a PostgreSQL role owning only this school's database
  set-db-url CODE URL                    point a school at another connection string (or env:VARIABLE_NAME)
  rotate-delivery-key OLD NEW [CODE]     re-encrypt every school's stored secrets, then set BRIGHTSTARS_DELIVERY_KEY
  drop-retired-tables [CODE] [--yes]     show (or drop) the removed website editor's leftover tables
  export-tenant CODE [--output DIR]      a school's whole database (pg_dump) and files, in one folder
  delete-tenant CODE [--output DIR]      permanently remove a school, after exporting it (cannot be undone)
  force-delete-tenant CODE               the same, with no export first
```

---

# 4. Configuration

Everything is read from the environment, loaded from `.env`. `.env.example` documents every variable, and the console's
*Settings* page explains each with its risk. The ones that shape a deployment:

| Variable | Purpose |
|---|---|
| `BRIGHTSTARS_ENV` | `development` or `production`. **A real deployment must say `production`**: one that looks real and does not refuses to start (see [Security](#5-security-and-sign-ins)). |
| `BRIGHTSTARS_PLATFORM_DB` | PostgreSQL URL of the platform registry. Required, no default. |
| `BRIGHTSTARS_SECRET` | Session-signing secret; at least 32 characters in production. |
| `BRIGHTSTARS_PLATFORM_HOSTS` | Hostnames serving the platform console. |
| `BRIGHTSTARS_PORTAL_DOMAIN` | Domain each school's address is issued under. |
| `BRIGHTSTARS_STORAGE_BACKEND` | `local` (default; files under `BRIGHTSTARS_TENANTS_DIR`) or `s3` (any S3-compatible store; `BRIGHTSTARS_S3_*`). |
| `BRIGHTSTARS_TRUSTED_PROXIES` | How many reverse proxies stand in front (0–5, default 0). See [Operating](#operating). |
| `BRIGHTSTARS_IDLE_MINUTES` · `BRIGHTSTARS_SESSION_HOURS` | A sign-in ends after this long unused (default 120) and in all (default 12). Never during a running exam. |
| `BRIGHTSTARS_DELIVERY_KEY` | The key schools' stored mail, SMS and Paystack secrets are encrypted under (defaults to one derived from the secret). |
| `BRIGHTSTARS_SMTP_*` · `BRIGHTSTARS_SMS_*` | The platform's shared email and SMS account (the fallback). |
| `BRIGHTSTARS_DB_POOL_SIZE` · `BRIGHTSTARS_DB_MAX_OVERFLOW` | Connections per school per instance (defaults 2 and 3). |
| `BRIGHTSTARS_REGISTRY_POOL_SIZE` · `BRIGHTSTARS_REGISTRY_MAX_OVERFLOW` | Registry connections per instance (defaults 10 and 20). |
| `BRIGHTSTARS_REGISTRY_CACHE_SECONDS` | How long a hostname lookup is cached, which bounds how fast a suspension takes effect. |
| `BRIGHTSTARS_SCHOOL_DB_TEMPLATE` · `BRIGHTSTARS_PG_MAINTENANCE_DB` | Where schools' databases are created, and which database `CREATE DATABASE` connects through. |
| `BRIGHTSTARS_MAX_UPLOAD_BYTES` · `BRIGHTSTARS_MAX_DATA_UPLOAD_BYTES` · `BRIGHTSTARS_MAX_REQUEST_BYTES` | Upload and request limits; every file box prints its limit. |
| `BRIGHTSTARS_ALERT_WEBHOOK` | Where degraded-safety alerts are posted (optional). |
| `BRIGHTSTARS_CHROME` · `BRIGHTSTARS_PG_DUMP` | The programs for result images and school exports, if not found automatically. |
| `BRIGHTSTARS_ALLOW_NON_PRODUCTION` | `1` only on a staging server that must run as development. Never on a real deployment. |

---

# 5. Security and sign-ins

The full threat map is on `/docs` (*Security*). The essentials:

- **Schools are isolated** by hostname, a database per school, sessions stamped with their school, and queries that
  fail closed.
- **Every form is protected** against forgery (a CSRF token), and the Content-Security-Policy runs no inline script
  without the page's own nonce.
- **Access** is a permission per page, plus teaching duties and scope per class, subject and bank, with refusals logged.
- **Passwords** are hashed; a new one is refused if short, common (`password123`, `school123`), a run like `12345678`,
  or built from the username (`core/passwords.py`).
- **Guessing is limited**, counted in the registry so every worker shares it: per address and username, plus wrong
  passwords per address across all usernames (40 in 15 minutes) and per username across all addresses (20). Right
  passwords never use up an allowance, so a computer room on one address is not held up. An unknown username takes as
  long to refuse as a wrong password.
- **How long a sign-in lasts** (`core/session_guard.py`):
  - a restart signs everyone out; a changed password ends the account's other sign-ins;
  - a sign-in ends after **2 hours unused** and **12 hours in all** (the presence ping does not count as use);
  - **signing out ends the sign-in everywhere**, so a copied cookie stops working too;
  - **someone sitting an exam is never signed out** by any of these while their paper runs (a power cut, a lost
    connection, a restart), and has 30 minutes more once it ends.
- **Production means production.** Outside production the app drops protections a public server needs (Secure cookies,
  HSTS, the strong-secret check, refusing private mail hosts). So a start that looks real (gunicorn/waitress/uwsgi, a real
  portal domain, or a reverse proxy) refuses unless `BRIGHTSTARS_ENV=production`.
- **Uploads**: type and size checked, pictures re-encoded, random names, per-folder access (`core/upload_access.py`), a
  plain 404 on refusal. Excel imports are read defensively (`core/tabular.py`).
- **Errors** never show internals: designed error pages, and a failure is told in words while the detail goes to the
  log (`core/failures.py`).
- **Stored secrets** (mail, SMS, Paystack) are encrypted per school.

Still to do: a second sign-in step for platform operators and School Admins (see [Known gaps](#known-gaps)).

---

# 6. Production

### Deployment checklist

1. `BRIGHTSTARS_ENV=production`, a strong `BRIGHTSTARS_SECRET`, and `BRIGHTSTARS_DELIVERY_KEY` set.
2. `pip install -r requirements.txt` on every deploy (the dependencies change: `openpyxl` and `defusedxml` are recent).
3. Run under a WSGI server behind a reverse proxy, e.g. `gunicorn -w 4 -b 127.0.0.1:8000 app:app`, never `python app.py`
   in production. Set `BRIGHTSTARS_TRUSTED_PROXIES` to the number of proxies.
4. As a deploy step, `python -m control_plane upgrade`: it brings every school's schema up to date **and** records a new
   launch (everyone signs in again, except people mid-exam).
5. TLS for every hostname (a wildcard for the portal domain, one per school domain); pass `Host` through unchanged.
6. More than one application server: `BRIGHTSTARS_STORAGE_BACKEND=s3`, or shared storage for `BRIGHTSTARS_TENANTS_DIR`.
7. A timer running `python -m control_plane sweep-jobs`, for schools quiet enough that nothing else retries their jobs.
   Run it at least hourly in the daytime: it also sends a school's birthday texts on a day nobody opens the portal
   (a weekend or holiday).
8. Many schools: a connection pooler (PgBouncer in transaction mode) in front of PostgreSQL. See below.

### Operating

- **Backups are per school.** Each school is its own database (`brightstars_<code>`) plus its own files. Restoring one
  never touches another. `export-tenant` gives a school everything it is owed.
- **Suspending** takes a school's portal offline for everyone within `BRIGHTSTARS_REGISTRY_CACHE_SECONDS`; its data
  returns on reactivation.
- **Database connections.** Each school has its own connection pool in each running instance, plus the registry's.
  They multiply with schools × instances; when new connections are refused while others sit idle, check
  `SELECT datname, state, count(*) FROM pg_stat_activity GROUP BY 1,2;` against `SHOW max_connections;`. Keep per-school
  pools small (`BRIGHTSTARS_DB_POOL_SIZE=1`, `BRIGHTSTARS_DB_MAX_OVERFLOW=1` for many schools), lower the registry pool,
  and put a pooler in front. With PgBouncer in transaction mode, use a database per school rather than a schema per
  school.
- **Behind a reverse proxy.** Leave `BRIGHTSTARS_TRUSTED_PROXIES=0` unless you run one: then forwarded headers are
  ignored, because anyone can write them. With proxies, set it to how many (1 for nginx or Caddy, 2 with a load
  balancer in front). Only the address and the scheme are believed, never the host.
- **Limits are shared** by every worker through the registry (`rate_limits`); if the registry cannot be reached each
  worker counts on its own and logs it. Nobody is locked out by a registry fault.
- **Leftover tables** from the removed website editor are dropped on upgrade when empty; `drop-retired-tables` shows the
  rest.
- **Load.** Menu counts (notifications, unread messages, unallocated payments, transfers, new students to bill) are kept
  for a few seconds per process and dropped by any change, so an ordinary staff page reads only its own content.
  `tests/verification/load_test_exam_answers.py` measures the exam-answer write rate before a big entrance day.

---

# 7. Developing

### Project layout

```
BrightStar-Academics/
├── app.py                 # Flask/SQLAlchemy setup, request hooks, error pages, security headers, the
│                          #   production guard, blueprint registration, shared helpers
├── release_notes.json     # What's new, by audience (see Announcing a change)
├── requirements.txt
├── control_plane/         # The platform itself (its own registry database)
│   ├── resolver.py        #   hostname → school; binds each session to its school
│   ├── routing.py         #   per-school engines and databases
│   ├── registry.py · models.py   registry tables: schools, domains, platform admins, tickets, audit,
│   │                      #   rate limits, ended sign-ins, launch state
│   ├── provisioning.py    #   create/upgrade/export/delete schools, branding, domains, database roles
│   ├── console.py · team.py · entry.py   the console, the team and its log, entering a school
│   ├── settings_console.py · settings_registry.py   /platform/settings: every variable and action
│   ├── ratelimit.py · ended_sessions.py · launch.py   shared limits, signed-out sessions, restarts
│   ├── docsgen.py · platform_pages.py   /docs, /marketing, /privacy
│   └── cli.py             #   python -m control_plane …
├── models/                # One school's schema, one module per area (auth, school, entrance, finance, store,
│                          #   subdivisions, parents, library, admissions, tenancy, presence, resilience)
├── core/                  # Cross-cutting helpers, no routes. Among them: security (permissions, CSRF, audit),
│                          #   session_guard, passwords, accounts, branding/theme, storage/object_store, uploads/
│                          #   upload_access, tabular (import files), delivery/payments (encrypted secrets),
│                          #   jobs/idempotency, subdivisions, school_structure, numbering, report_card_pdf, failures
├── blueprints/            # Routes, one package per area: auth, administration, school, entrance,
│   │                      #   candidate_portal, student_portal, parents, finance, store, library
│   └── admin_dock.py · family_notices.py · pwa.py · portal_shell.py · platform_version.py
├── services/              # Small dependency-free utilities (the student number generator)
├── starter_banks/         # The standard entrance question banks
├── practice_banks/        # Ready-made entrance practice questions (built by tools/build_practice_banks.py)
├── tools/                 # Maintenance scripts run by hand
├── templates/             # Jinja templates: school_guide/ (the staff guide), platform/ (console and /docs)
├── static/                # Shared CSS/JS/fonts/images; school-ui.css is the design system
├── tenants/               # Per school, with local storage: data/, uploads/, numbering.json (gitignored)
├── tests/current/         # The contract suite (fast, no database)
├── tests/verification/    # End-to-end suites on throwaway PostgreSQL databases
└── docs/architecture/     # MULTI_TENANCY.md and two product requirements the tests enforce
```

The full, always-current module, route, table and template reference is generated on `/docs`.

### Conventions

- **Routes use plain `@app.route`**, not `Blueprint` objects: every module does `from app import app`, keeping every
  endpoint name stable. A new module is imported near the other `import blueprints…` lines in `app.py`.
- **Every admin route is in `ADMIN_ENDPOINT_PERMISSIONS`** (`core/security.py`); an unmapped one falls back to
  `admin.access`, which nearly everyone holds.
- **Never use `db.engine`** (it is the registry). Use `control_plane.routing.current_engine()`.
- **PostgreSQL only.** Upserts go through `core.db_helpers.insert_stmt()`, string aggregation through `group_concat()`.
- **Never hard-code a school's name, motto or logo**: `school_brand` in templates, `core.branding.school_name()` in code.
  Inside a `<script>`, a school's name goes through `|tojson`.
- **One design system**: `static/school-ui.css` (tokens and components) and `static/academics.css` (the dense staff
  pages). Fonts are self-hosted. Who sees which menu item is one list in `templates/admin_base.html`; icons are in
  `templates/includes/_nav_icons.html`.
- **The schema lives in `models/` alone.** `create_all()` plus a column-diffing helper add new tables and columns at
  start-up or `upgrade`; there are no migration scripts. Until then `test_models_match_db.py` reports the drift.
- **Files go through `core/storage.py`**, never `os.path`, so they work on local disk and S3.
- **Scripts that send forms read `form.getAttribute('method')`**, never `form.method` (a field named `method` hides it).

### Tests

```bash
python tests/current/run_current.py      # the contract suite: run after every change (no database needed)
```

The end-to-end suites drive the real application against PostgreSQL. Each creates its own throwaway databases
(`bs_test_*`) and drops them; they never touch real data. Run the ones for the area you changed, and the two big ones
before a release:

```bash
python tests/verification/write_paths_pg_smoke.py    # every page opens
python tests/verification/write_paths_pg_posts.py    # every form, valid and hostile; fails on any 500
```

| Area | Suites (`tests/verification/write_paths_*.py`) |
|---|---|
| Platform and schools | `multitenancy`, `platform_console`, `platform_team`, `portal_branding`, `numbering_rules`, `error_pages`, `retired_tables` |
| Sign-in and security | `session_guard`, `rate_limits`, `upload_access`, `upload_limits`, `resilience`, `known_gaps` |
| Staff and structure | `staff_hierarchy`, `subdivisions`, `guide`, `admin_dock` |
| Students | `student_import`, `student_history_import`, `results_import`, `student_archive`, `admissions`, `departments` |
| Teaching | `term_grading`, `report_cards`, `attendance`, `timetable`, `issue_to_class`, `work_delete`, `subject_selectors`, `fractional_marks` |
| Entrance | `question_banks`, `bank_locks`, `candidate_results` (needs Chrome) |
| Money and messages | `finance`, `receipts`, `class_billing`, `new_students`, `paystack`, `store`, `delivery` |

**The coverage guard:** every route that accepts a POST must be submitted by `write_paths_pg_posts.py` or listed with a
reason in `tests/verification/pg_posts_coverage.py`, or the contract suite fails. The `/docs` *Testing* page describes
every suite and contract test.

### Announcing a change

Add a release to the top of `releases` in [release_notes.json](release_notes.json) with a higher version, and give each
change an audience: `staff` (may link one staff guide page with `help`), `family` or `student` (never a `help` link).
Do not reword a release people may already have seen; add a new one. `test_release_notes_contract.py` checks the rules.
The newest version is what the sign-in page shows.

---

# Known gaps

### To do: two-step sign-in (security audit, H1)

- [ ] **Add a second sign-in step for the most powerful accounts.** Platform operators (who can enter and change
  every school) and each school's School Admin sign in with a password alone. One stolen or reused password is
  then enough. Plan: an authenticator-app code (TOTP) for platform operators first, then for School Admins, with
  one-time recovery codes; SMS codes only as a fallback, since SIM swaps are common. Until then, keep these
  accounts to long, unique passwords, and keep the number of platform operators small.

### Still true

- **A live school database can be behind the models** until the app initialises it or `upgrade` runs. The schema-drift
  contract test reports this.
- **`admins.last_seen_release` is no longer used.** It is kept only so an existing database still matches the model; a
  later migration can drop it.
- **Connection limits** on a managed database can be reached with many schools; see *Database connections* under
  [Operating](#operating).
