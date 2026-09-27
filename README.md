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
- [Documentation, the marketing page and the privacy statement](#documentation-the-marketing-page-and-the-privacy-statement)
- [The new-school setup checklist](#the-new-school-setup-checklist)
- [Email and WhatsApp](#email-and-whatsapp)
- [How a school numbers its people](#how-a-school-numbers-its-people)
- [Question banks](#question-banks)
- [Practice tests](#practice-tests)
- [Attendance](#attendance)
- [Exam and test timetables](#exam-and-test-timetables)
- [Bulk student import](#bulk-student-import)
- [Admissions: candidate to student](#admissions-candidate-to-student)
- [The staff guide](#the-staff-guide)
- [Report cards](#report-cards)
- [Receipts and parent notices](#receipts-and-parent-notices)
- [Command line](#command-line)
- [Configuration](#configuration)
- [Project layout](#project-layout)
- [Testing](#testing)
- [Operating](#operating)
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
other request keeps `BRIGHTSTARS_MAX_REQUEST_BYTES`. Every file box in the application prints
this limit under itself and, through `static/upload-limit.js`, refuses a file that is too large or of
the wrong type beside the box before anything is sent; the server gives the same explanation
(the file's name, its size and the limit) if the script is off, and a submission over the request limit
is answered with a plain explanation rather than an error page (`core/uploads.py`,
`core/request_errors.py`).
- **Question banks.** Ticked by default: *Start with the standard entrance question banks*. The
  platform's standard set (`starter_banks/`, six banks: Mathematics, English and General Knowledge
  for Year 7 and SSS 1) is copied into the school's own `data/` folder, so its entrance
  examinations can run at once. Untick it, or pass `--no-starter-banks` on the command line, for a
  school that will bring only its own. The starter files name no school, and copying never
  overwrites a file the school already has.
- **Numbering.** How the school writes its candidate codes and student numbers: a short pattern
  with the default already filled in (see *How a school numbers its people*, below). Leave it
  alone unless the school wants something different; it can be changed later on the school's page.
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

## Documentation, the marketing page and the privacy statement

Three more pages live on the platform hostnames, alongside the console:

- **`/docs`** is the platform's own developer documentation: architecture, deployment,
  recommendations, and a reference (routes, tables, permissions, modules, templates) generated
  from the code itself so it cannot fall behind. It is visible only to a signed-in platform admin
  who has been given access — the super admin always has it and grants or withdraws it for each
  other admin, one at a time, from the Team page; everyone else meets the same 404 as an address
  that does not exist. Read from the app: [Welcome](templates/platform/docs/welcome.html) and
  [control_plane/platform_pages.py](control_plane/platform_pages.py) and
  [control_plane/docsgen.py](control_plane/docsgen.py).
- **`/marketing`** is the public page that explains the product to schools, in the Brightstars
  brand. It needs no sign-in. See [templates/marketing.html](templates/marketing.html).
- **`/privacy`** is the platform's NDPR-aligned data-protection statement. Unlike the other two it
  carries no host guard at all, so it is reachable both on a platform hostname (for a school
  considering the platform) and on every school's own address (linked from its sign-in page, for
  that school's own parents and staff). See [templates/privacy.html](templates/privacy.html) — a
  few bracketed details (legal name, registration number, registered address, Data Protection
  Officer contact) are placeholders and must be filled in before it is used in procurement.

All three are checked by `tests/current/test_docs_contract.py`: every file, route, table, permission
and template the documentation names is verified to still exist, the access rules for `/docs` are
enforced (only a signed-in admin with access; nobody else, not even by guessing the address), and
`/privacy` is confirmed to carry no such guard.

## The new-school setup checklist

A school that signs up and then stalls on setup — never adding a subject, never enrolling a
student — is a school that quietly churns. The School workspace home page (`/admin/school`) shows
an ordered checklist of the four things a school does before it can really run: review its classes
(pre-seeded, so usually already done), add subjects, enrol students, and set up fee items. Each
step's state is read live from the school's own data, never a flag anyone has to remember to set,
so the checklist can never disagree with what the school has actually done. It disappears once all
four are done, or a school can hide it early (a small link brings it back). See
[blueprints/school/onboarding.py](blueprints/school/onboarding.py).

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

## Email and WhatsApp

What parents receive — payment receipts, password-recovery emails, alerts about school work — is
sent from **the school's own accounts**. A school's administrators set them up under **Email &
WhatsApp** in their admin area (guarded by the `delivery.manage` permission, which the school's
top-level administrator holds and can grant to a role): a mail server, or a WhatsApp Business
(Cloud API) account, each with a button to test it before relying on it.

- **The platform's shared account is the fallback.** A school that has set up nothing sends through
  the `BRIGHTSTARS_SMTP_*` / `BRIGHTSTARS_WHATSAPP_*` account, if the deployment has one, and its
  page says so, showing the sender address. A school that has set up nothing and has no shared
  account to fall back on simply cannot send, and says so.
- **A half-set-up school never borrows the platform's account.** Once a school enters a mail host,
  its own settings are used exclusively, even if incomplete, rather than quietly sending its
  parents' mail from the platform's address.
- **Secrets are encrypted at rest.** A saved password or token is encrypted (Fernet) in the
  school's own database, under a key derived from the application secret and the school's code:
  a copy of a school's database is not a copy of its mail credentials, and one school's stored
  token means nothing in another. They are never shown again, are absent from the audit log
  (which records only *which* fields changed), and a blank field on save means "keep what is
  saved". Rotating `BRIGHTSTARS_SECRET` would strand them, so set `BRIGHTSTARS_DELIVERY_KEY` to keep
  them independent of it; an unreadable secret is reported as not saved and re-entered.
- **The server cannot be aimed at its own network.** A school chooses the host that *the server*
  connects to, which is a way to probe internal services, so in production a school's mail host
  must resolve only to public internet addresses (checked when saved and again just before every
  connection, and the connection goes to the very address that was checked, not a name looked up
  again), and only the standard mail ports (25, 465, 587, 2525) are allowed. In development a
  local mail catcher is accepted. The platform's own configured server is trusted and unrestricted.
  Certificates are verified.

## How a school numbers its people

Schools do not all number their entrance candidates and students the same way, so the platform
does not decide what a code looks like. **Each school has its own numbering rules, which the
platform team sets on the platform console** — on the *Create a school* form, and later in the
**Numbering** section of the school's page — in a small text-editor-like area with the default
already filled in. They are kept in the school's own folder, `tenants/<code>/numbering.json`, so
nobody edits a file in the project folder for a school.

A rule is a **pattern**: plain text with `{placeholders}` in it. The default candidate pattern is
`{school}-{year}-{seq:4}`, which makes `ABC-2026-0001`, `ABC-2026-0002`, … and starts again each
year, which is what the platform always did. A school that does not want to change it changes
nothing. The student-number pattern is empty by default, which means "use the school's numbering
policy" (a prefix, optionally the year, and a running number, e.g. `ABC/2026/0001`).

| Write | It gives | Example |
|---|---|---|
| `{school}` | the school's code, in capitals | `ABC` |
| `{initials}` | the initials of the school's name | `Bright Future Academy` gives `BFA` |
| `{year}` `{yy}` | the 4-digit / 2-digit year | `2026` / `26` |
| `{month}` `{mon}` | the 2-digit month / 3-letter month | `09` / `SEP` |
| `{day}` | the 2-digit day | `05` |
| `{class}` | the class applied for, without spaces, in capitals; empty if unknown (candidate codes only) | `JSS 1` gives `JSS1` |
| `{seq}` | the running number, not padded | `7` |
| `{seq:4}` | the running number padded with zeros to exactly 4 digits | `0007` |
| `{seq:4-5}` | padded to at least 4 digits, may grow to 5; beyond 5 is refused with a clear message | `0007`, later `12345` |
| `{random:N}` | N random digits | `4827` |
| `{letters:N}` | N random capital letters (never I or O) | `KWTB` |
| `{alnum:N}` | N random capital letters and digits (never I or O) | `K7WB` |
| `{prefix}` | the numbering policy's prefix (student numbers only) | `ABC` |

Plain text between the placeholders may use letters, digits and `-` `_` `.` (and `/` in student
numbers). `{school|lower}` or `|upper` changes the case of a word (`|lower` is for student
numbers: a candidate code is always made into capitals, because that is how candidates sign in).

- **A pattern needs `{seq}` or a random part**, or every code would be the same, and at most one
  `{seq}`. Unknown placeholders, unbalanced braces, forbidden characters and patterns over 80
  characters are refused with a message that names the problem and its position.
- **How `{seq}` counts.** The next number is one more than the biggest number already used by a
  code that has the *same text in every other part*. So the count starts again by itself each year
  if `{year}` is in the pattern, each month with `{month}`, for each class with `{class}`, and
  never if none of them is. The **first number** setting (default 1) is where it starts when
  nothing matches; it never sends the count backwards. A code that is already taken moves on to
  the next number, and a `{random}` pattern tries again (up to 50 times).
- **Student numbers.** The platform's own running number
  (`services/student_number_generator.py`) stays the source of the sequence and of the ledger that
  stops a number being issued twice; the pattern only decides how the number is *written*.
- **Safety checks that always apply.** A finished candidate code is 3 to 40 characters of letters,
  digits and `.` `_` `-`, starting with a letter or digit; a student number is 1 to 40, and may
  also contain `/`. A pattern that could make a code that fails these (for example one that begins
  with `-` when the class is not known) is refused when it is saved.
- **Codes already issued do not change.** A new pattern applies only to the codes made after it
  is saved; the school's page says so.

**Nothing typed is ever run.** A pattern is *read* by `core/numbering_pattern.py` — split into
pieces, each checked against the fixed list above, then filled in — never executed: there is no
`eval`, `exec`, import or template engine anywhere near it. So being able to set a school's
numbering does not let anyone run code on a server that can reach every school's database. (An
earlier design used a Python file per school; that mechanism has been removed. A `numbering.py`
left in a school's folder is ignored.)

The console editor has a monospace box for each pattern, a chip for every placeholder that inserts
at the cursor (hover for what it means and an example), a **live preview** of the next three codes
with any problem shown beside the pattern (with its position), **Reset to default**, and a
reference table. On a school's page the preview reads that school's own candidates (read-only) to
show its real next numbers; a school that cannot be read still gets an example preview. The server
re-checks everything on save; the preview never writes anything; input is length-capped and always
shown escaped. Every save is recorded in the platform audit trail with the old and the new
pattern, and any platform admin who may edit a school may change it. `numbering.json` is checked
every time it is read and written whole and atomically; a missing file means the default, and a
corrupt file is refused with a plain message (registering a candidate shows it on the form) and is
**never silently replaced** — the console's next explicit, checked save puts the school right and
keeps a copy of the file it replaced. (`tests/verification/write_paths_numbering_rules.py` proves
all of this.)

## Question banks

Each school's banks are JSON files in its own folder, `tenants/<code>/data/`, so copying the folder
moves them and no school can read another's.

- **A new school is not empty.** Creating a school copies the standard entrance banks from
  `starter_banks/` into it and fills its examinations list. The files use neutral names and ids
  (`starter_year7_mathematics` and so on) and name no school. Nothing is ever overwritten, and the
  platform's own starter files are never changed by making a school or by a school's own import.
- **A school can bring its own.** *Question Banks → Import from file* takes a JSON file. It needs
  the `question_banks.create` permission (replacing a bank also needs `question_banks.edit`). The
  file is checked strictly: at most 2 MB, a safe bank id, a name, 1 to 500 questions, each with
  four different options and an answer, no picture paths. Everything wrong is listed at once. It is
  written only inside the school's own folder; a bank whose id already exists is replaced only when
  the administrator ticks *Replace it*; every import is recorded in the audit log.
- **Live papers.** A bank is not an exam until it is configured for the current session.
  *Exam Configuration → Set up the standard entrance papers* does that in one click for the
  standard banks (a school that has set up some papers itself keeps them); the same page adds or
  changes a paper by hand and makes one live.

**The standard questions are the platform's own.** They were written for the platform (40 to 45 for
each class and subject, 255 in all, including short reading passages for the English papers) and
are not any school's questions; nothing in the set matches the first school's banks, and a contract
test keeps it that way. Each paper serves 25 of its questions to a candidate at random, and the
right answer is spread evenly over A to D in every bank. The Mathematics answers are computed, not
typed, and an independent reviewer answered every question without seeing the key: every key
matched, and the questions they found arguable or badly worded were fixed. Even so, have someone
who teaches the subject review the set before a school relies on it, exactly as with any bank a
school imports. Each paper is timed at one hour, which is generous for 25 questions; a school can
change that by editing the bank's duration.

**Marks.** A paper is always out of 100, so a paper of 40 questions gives 2.5 marks a question, and
a school may serve any number of questions that splits 100 into whole hundredths (20, 25, 40, 50 …).
Marks are stored as decimals, so a candidate's raw score is exact, and a whole mark reads as a whole
number everywhere ("75 / 100", not "75.0 / 100.0"). A school database made before this is upgraded
in place on the next start, and no stored mark changes.

## Practice tests

Practice is a self-study bank, never part of anyone's record. Nothing a person does in a practice
test is written to the database: no attempt, no answers, no result, so it can never reach a term
result or a report card. Every run serves the questions in a fresh random order.

- **Students** always find their class's practice tests under *Practice* in the student portal, in
  every session, and can take and retake them as often as they like. Each run is timed like a CBT
  paper, marked at once, and shows the correct answers.
- **Entrance practice is public.** `/entrance-practice` on a school's address lets anyone, with no
  registration, choose the class they are aiming for (Year 7 / JSS 1 or Year 10 / SSS 1) and then a
  subject. For each class and subject the administrator chooses (under *Practice Tests* in the
  entrance workspace) whether the questions come from **the last session's entrance examination**
  (the default, so it works without any setup) or from a **custom bank** set up for practice. The
  current session's own bank is never offered, so practice cannot reveal an examination that has not
  been sat.

## Attendance

A class teacher takes the register for one class on one date under *Attendance* in the school menu:
present, late, absent or excused for each enrolled student, or left blank for "not yet marked"
(`blueprints/school/attendance_data.py`, table `attendance_records`). One row per student per day,
never per subject. Saving the same statuses again changes nothing and keeps the original marker's
name; changing a status re-attributes the row to whoever changed it, the same "last honest edit wins"
rule report card comments use, and a status for a student outside the class roster is silently
ignored. A term summary totals present/late/absent/excused and a percentage (late counts as present)
for a whole class. Two permissions: `school.attendance.mark` (take the register, limited to the
classes the staff member may access) and `school.attendance.view` (see the term summary only). A
student and a linked parent each see their own summary and day-by-day history for a chosen session
and term, on the dashboard under *Attendance*.

## Exam and test timetables

Staff build an exam/test timetable as a **draft** — one entry per class, subject, date and time, with
an optional venue — under *Exam Timetable* in the school menu, editing and deleting freely. Nothing
outside staff sees a draft. **Releasing** the whole session-and-term timetable at once turns every
draft entry into a released one and, in the background, notifies every currently enrolled student of
the classes it covers and their parents: an in-app alert each, plus an email and a WhatsApp message to
the guardian contact. Releasing a second time releases nothing more and says so. Three permissions:
`school.timetable.view` (see the list, read-only), `school.timetable.manage` (add, edit and delete
entries) and `school.timetable.release` (the sensitive one: only this notifies students and parents).
A student and a linked parent each see only released entries for their own class, on the dashboard
under *Exam timetable*, and can download it as a PDF or print it.

## Bulk student import

A school with existing students does not type them in one at a time: *Bulk import (CSV)* on the
Students page takes a spreadsheet — `first_name`, `last_name`, `gender` and `class` required;
`middle_name`, `guardian_name`, `guardian_email`, `guardian_phone` optional — and creates exactly
what "Register Student" creates by hand for each valid row: the student record, an enrolment in the
named class for the current session, and a login with a generated admission number and a one-time
password, using the same numbering and account machinery either way. `class` is matched against the
school's own class names, case-insensitively; admission numbers are never typed, in bulk any more
than one at a time. A row that fails (a missing name, an unrecognised gender, an unknown class, a bad
guardian email, or a class outside the importing staff member's own scope) is skipped and reported by
line number and reason; every other valid row is still imported. The results page is the only place
the generated usernames and one-time passwords are ever shown — download them as a CSV from there (built
in the browser, never a second trip to the server) before leaving the page. A file with no header row,
a missing required column, or one over the size limit is refused outright, before anything is written.

## Admissions: candidate to student

The third way a student arrives, alongside registering one by hand and bulk import: the entrance
exam. Every active candidate who has attempted at least one paper is on the **admissions waitlist**
(*Admissions* in the Entrance workspace), ranked by their overall percentage; a candidate who has
never sat a paper does not appear at all. Two admission columns live directly on the candidate's own
record, since a candidate has at most one admission decision, ever: `admission_status` ('pending',
'admitted' or 'declined') and `admitted_student_id`.

- **Admitting** creates exactly what "Register Student" creates by hand — the student record, a class
  enrolment for the current session, and a login — through the same numbering and account machinery
  either way, so an admitted candidate is indistinguishable from one registered one at a time. The
  candidate's one-field name is split onto the form as a starting guess, correctable before
  confirming. If the candidate has a guardian email or phone on file and the box is left ticked, a
  linked parent portal account is created from those same details too, so the whole family reaches
  the portal in one action. **Admitting is terminal**: an admitted candidate can never be admitted
  again, declined, or reset — by then a real student and a real login exist.
- **Declining** takes a candidate off the waitlist with an optional reason, and — unlike admitting —
  can be undone, back to pending, at any time.
- One permission, `candidates.admit`, separate from `candidates.view`: seeing the waitlist at all,
  not only deciding it, needs this permission. The *Admissions Officer* role preset carries it;
  nobody else does by default. An officer limited to one entry level's candidates (by the same class
  scope every other scoped account uses) can only decide for that level.

## The staff guide

`/admin/guide` is a plain-language, hand-written user guide for the people who use the portal, not the
platform's own documentation (`/docs`, generated from the code for platform operators) and not a
public site (schools have none). Any signed-in admin can read it, in either workspace — it carries no
permission of its own, so it falls back to `admin.access`, which nearly every staff account holds.
Thirteen pages, grouped Getting started / Academics / Finance and families / Running the school /
Entrance workspace, each linking straight to the real pages it describes, with a search box and an
on-this-page outline. It is the same guide for every school; only what a reader can see in the pages
it links to depends on their own account. `tests/current/test_guide_contract.py` checks every page
links to something real (a page, a route) the same way `test_docs_contract.py` checks `/docs`; the
end-to-end behaviour is `tests/verification/write_paths_guide.py`.

## Report cards

Every school gives each student a **report card for each term**, as a page and as a PDF.

- **When it appears.** A student's card for a term is ready the moment **every one** of their results
  for that term has been released (by hand, or by the session's release date). From then on it shows
  by itself in the student's portal and in the parent's portal, on the dashboard under *Report cards*.
  If one result is still waiting (entered, verified or approved) the card is held back, so a card is
  always complete and never shows a mark the student has not been given. Practice tests are not part
  of the official record and neither count nor hold a card back.
- **What is on it.** The school's own logo (its name stands in the logo's place if it has none),
  name, motto, address, phone, email and brand colour; the student's name, admission number, class and
  photograph; each subject as CA out of 40 plus Exam out of 60, with the total, a grade and a remark;
  the overall percentage and grade; the **class average percentage** (and each subject's class
  average); **affective and psychomotor trait ratings** (Punctuality, Neatness, Leadership and the
  like under Affective Domain; Handwriting, Sports, Drawing and the like under Psychomotor Domain),
  on a five-point scale, shown only once at least one has been rated so a school that never uses this
  sees no change to its cards; the **class teacher's comment with that teacher's own signature**; the
  **head's title, name and signature** (Head Teacher, Headmistress, Proprietress, Proprietor and so
  on); the grading key; the date it was issued and, if set, when the next term begins.
- **Same numbers as everywhere else.** A card is worked out when it is asked for, from the term result
  the school already has (the same arithmetic the admin's Term Results uses), so it can never disagree
  with the results. Grades: A 70-100 Excellent, B 60-69 Very Good, C 50-59 Good, D 45-49 Fair,
  E 40-44 Pass, F below 40 Fail. The class average counts only classmates whose own card is ready.
- **Who does what.** Under *Report cards* in the school menu, staff choose a class, session and term and
  see which cards are ready (and which are waiting on how many results), open a card, download its
  PDF, or download every ready card in the class as one PDF. Three permissions: `report_cards.view`
  (see and download, limited to the classes the staff member may access), `report_cards.comment`
  (write the class teacher's comment, rate affective/psychomotor traits, and keep **your own
  signature** under *My signature*) and `report_cards.manage` (set the head's title, name and
  signature, and when the next term begins). The
  *Primary Class Teacher* and *School Academic Administrator* roles have them, and there is a
  *Report Card Officer* role. A comment can be written before results are released; a card with no
  comment still appears on release, with an empty comment box. Whoever last changes a comment is its
  author: their name and their signature are printed beside it, and saving the page without changing a
  comment never takes it over.

## Receipts and parent notices

- **Every school's receipt is its own.** The receipt (page, printout and the PDF sent to parents,
  all drawn from the one description in `blueprints/finance/helpers.py`, `core/receipt_pdf.py` and
  `templates/includes/_receipt_sheet.html`) uses the school's two brand colours, logo, name, motto,
  address, phone and email, its authorised signature, the name of the member of staff who recorded
  it, the note the bursar attached to the payment, and where the student's account stood for the
  session as of that payment. A school with no logo gets its initials in the logo's place.
- **Parents are told automatically.** Recording a payment sends the guardian the receipt itself, by
  email (PDF attached) and by WhatsApp, and adds an in-app alert for linked parent accounts; each
  attempt is logged on the receipt page. When a student's last result for a term is released, whether
  by the *Release all results* button, one result at a time, or the session's release date, the
  parents are told by email, WhatsApp and in-app that the report card is ready. Sending happens as a
  durable background job (`core/jobs.py`), so nobody waits on a mail server, a failed message never
  undoes what was saved, and a thread that never finishes (a restart, a dropped connection) is retried
  rather than lost. WhatsApp Cloud API only delivers free-form messages within 24 hours of the
  recipient's last message to the school's number; a school that needs to reach parents outside that
  window needs approved message templates, which this application does not send.
- **Results & Records** is a dialog: choose a class, search its students, choose a student, then a
  session and term, then read that term's subjects and release them all with one confirmed button.
  The button releases every *approved* result of the term; results not yet verified or approved stay
  private and hold the report card back.

## Command line

```
python -m control_plane <command>

  init                          create the platform registry database and tables
  create-platform-admin USER [--super]   add a platform admin (the first is the super admin)
  create-tenant CODE "Name"     create a school (portal address issued automatically)
  list                          every school, its addresses and its database
  upgrade [CODE]                bring school database(s) up to the current schema (with no CODE it also
                                records a new launch, like starting the server does)
  new-launch                    record a new launch: everyone signed in must sign in again, except
                                people in the middle of an exam
  drop-retired-tables [CODE] [--yes]   show, or with --yes drop, tables left by the removed website editor
  add-domain CODE HOST [--primary] / remove-domain HOST
  suspend CODE [--reason TEXT] / activate CODE
```

## Configuration

Everything is read from the environment, loaded from `.env`. `.env.example` documents every
variable; the ones that shape the deployment:

| Variable | Purpose |
|---|---|
| `BRIGHTSTARS_PLATFORM_DB` | PostgreSQL URL of the platform registry. Required — there is no default, because a wrong guess would silently create an empty registry and make every school look as though it did not exist. |
| `BRIGHTSTARS_PLATFORM_HOSTS` | Hostnames serving the platform console (`/` there opens its sign-in page). |
| `BRIGHTSTARS_PORTAL_DOMAIN` | Domain each school's portal address is issued under. |
| `BRIGHTSTARS_TENANTS_DIR` | Folder holding each school's files. |
| `BRIGHTSTARS_SECRET` | Session-signing secret. In production it must be at least 32 characters or the app refuses to start. |
| `BRIGHTSTARS_ENV` | `development` or `production`. |
| `BRIGHTSTARS_SCHOOL_DB_TEMPLATE` | Optional: place schools' databases on another server. |
| `BRIGHTSTARS_REGISTRY_CACHE_SECONDS` | How long a hostname lookup is cached per worker, which bounds how quickly a suspension takes effect. |
| `BRIGHTSTARS_SMTP_*` / `BRIGHTSTARS_WHATSAPP_*` | The platform's *shared* email and WhatsApp account, used by any school that has not set up its own (see [Email and WhatsApp](#email-and-whatsapp)). |
| `BRIGHTSTARS_CHROME` | Optional. The Chrome or Chromium program that draws a candidate's result image; found automatically on Windows and under the usual names on Linux and macOS. |
| `BRIGHTSTARS_TRUSTED_PROXIES` | Optional, default `0`. How many reverse proxies stand in front of the application; see *Behind a reverse proxy* under [Operating](#operating). |
| `BRIGHTSTARS_DELIVERY_KEY` | Optional. The key schools' saved mail and WhatsApp secrets are encrypted under; defaults to one derived from `BRIGHTSTARS_SECRET`. |

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
│   ├── ratelimit.py          #   sign-in and other limits, counted in the registry so workers share them
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
├── core/                     # Cross-cutting helpers — no routes
│   ├── db_helpers.py         #   query helpers, and the dialect-neutral upsert/aggregate
│   ├── security.py           #   RBAC, admin_required, csrf_protect, audit_log
│   ├── branding.py           #   the school's own name, motto, logo, colours, gallery, receipt prefix
│   ├── theme.py              #   brand-colour and gallery rules: validation, contrast, theme CSS
│   ├── storage.py            #   the school's data/ and uploads/ folders
│   ├── accounts.py           #   shared sign-in/out helpers
│   ├── entrance.py           #   question banks, grading, result rendering
│   ├── delivery.py           #   a school's own email/WhatsApp: encrypted secrets, safe hosts
│   ├── banks.py              #   question-bank validation, safe writing, the standard set
│   ├── numbering.py          #   each school's numbering rules (tenants/<code>/numbering.json): reading, saving, making numbers
│   ├── numbering_pattern.py  #   the pattern language: read and checked, never run
│   ├── report_card_pdf.py    #   draws report cards as a PDF (one page per student)
│   ├── marks.py              #   marks are decimals: exact totals, whole marks shown as whole numbers
│   ├── retired_tables.py     #   drops the removed website editor's tables when they are empty
│   ├── notifications.py      #   guardian email/WhatsApp alerts
│   ├── presence.py           #   who is online
│   ├── public_settings.py    #   the school's settings lookup
│   └── uploads.py            #   image upload validation, the size limit and the words that explain a refusal
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
├── starter_banks/            # The standard entrance question banks copied into a new school
├── templates/                # Jinja templates; templates/platform/ is the console and site
├── static/                   # CSS/JS/images shared by every school
├── tenants/                  # Per school: <code>/data, <code>/uploads and numbering.json (gitignored)
├── tests/
│   ├── current/              #   the authoritative contract suite
│   ├── verification/         #   end-to-end scripts against real databases
└── docs/architecture/        # MULTI_TENANCY.md, and two product requirements the tests enforce
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

The end-to-end suites drive the real application over HTTP against PostgreSQL. Each creates its
own throwaway databases (`bs_test_*`), drops them afterwards, and clears any left behind by a
crashed run — they never touch real data.

```bash
python tests/verification/write_paths_multitenancy.py      # isolation between schools
python tests/verification/write_paths_platform_console.py  # the console, end to end
python tests/verification/write_paths_portal_branding.py   # colours, logo and sign-in photographs
python tests/verification/write_paths_platform_team.py     # the team, roles, removal, activity logs
python tests/verification/write_paths_delivery.py          # a school's own email and WhatsApp
python tests/verification/write_paths_known_gaps.py        # roles, profile page, uploads, search, sign-in errors
python tests/verification/write_paths_pg_smoke.py          # every page, opened on PostgreSQL
python tests/verification/write_paths_pg_posts.py          # every form submission, sent on PostgreSQL
python tests/verification/write_paths_numbering_rules.py   # each school's numbering patterns: the console, the language, candidates and students
python tests/verification/write_paths_question_banks.py    # the standard banks, importing banks, a new school's first exam
python tests/verification/write_paths_fractional_marks.py  # a 40-question paper is 100 marks; older schools upgraded
python tests/verification/write_paths_retired_tables.py    # clearing the removed editor's leftover tables
python tests/verification/write_paths_error_pages.py       # the 404, 403 and 500 pages: branded, per school, and never leaking
python tests/verification/write_paths_term_grading.py      # Exam 60 + CA 40: weights, scaling, rounding, workflow, visibility
python tests/verification/write_paths_bank_locks.py        # locking a question bank, and what a lock blocks
python tests/verification/write_paths_finance.py           # fee items, paid / unpaid, payments, allocations, parents' alerts
python tests/verification/write_paths_receipts.py          # the receipt PDF, its email and WhatsApp, the signature
python tests/verification/write_paths_candidate_results.py # a candidate's result image carries the right school and leaks nothing
python tests/verification/write_paths_report_cards.py      # report cards: when ready, content, comments, traits, signatures, portals
python tests/verification/write_paths_attendance.py         # taking the register, scope, the term summary, student and parent portals
python tests/verification/write_paths_timetable.py           # exam/test timetables: draft, release, notifications, scope, portals
python tests/verification/write_paths_student_import.py      # bulk CSV student import: valid/skipped rows, scope, credentials, limits
python tests/verification/write_paths_guide.py                # the staff guide: every page renders, no permission of its own, search, isolation
python tests/verification/write_paths_admissions.py           # admissions: the waitlist, admitting (student + parent), declining, scope, isolation
python tests/verification/write_paths_resilience.py           # durable background jobs: success, retry-then-give-up, a dead thread picked back up, the real payment-receipt job's trail
python tests/verification/report_card_pdf_selfcheck.py     # the report card PDF drawing itself (needs no database)
python tests/verification/write_paths_rate_limits.py       # limits shared by every worker process
python tests/verification/write_paths_session_guard.py     # restart sign-out (exam sitters kept), password changes, headers, trusted proxies
python tests/verification/write_paths_upload_access.py     # who may open which uploads folder
python tests/verification/write_paths_upload_limits.py     # every picture upload: the limit told beforehand, every refusal explained
```

Between them they cover hostname routing, per-school databases, session cookies copied between
schools, path traversal, uploads, question banks, suspension, creating a school with its branding
and logo, colours and photographs (including hostile input), portal addresses and CNAMEs, and
entering a school.

### Every write is tested on PostgreSQL

`write_paths_pg_smoke.py` opens every page; `write_paths_pg_posts.py` submits every form.
It sends a real, valid submission to all 132 routes that accept a POST, as the right kind of
signed-in person (platform operator, school administrator, staff, student, parent, candidate), and
checks the row was written. Email and WhatsApp are caught at the last step, so the real sending
code (including building a receipt PDF) still runs. It then sends each route an empty form,
hostile text, huge and negative numbers, NUL bytes, no CSRF token and an id that does not exist,
and fails on any 500 or database error. This is how nine more PostgreSQL-only failures were found
and fixed (returning a library book, a parent's message, editing a parent, and forms that crashed
when shown again after a mistake, among others). A number too big for a database column, or text
containing a NUL byte, is answered with a plain 400 page (`core/request_errors.py`), not a 500.

A route that accepts a POST but is neither submitted there nor listed with a reason in
`tests/verification/pg_posts_coverage.py` fails the contract suite, so a new route cannot ship
untested on PostgreSQL.

## Operating

**Backups are per school.** Each school is a separate PostgreSQL database (`brightstars_<code>`)
plus a folder under `tenants/<code>/`. Restoring one school never touches another.

**Suspending** a school takes its portal offline for everyone — staff, parents and students —
within `BRIGHTSTARS_REGISTRY_CACHE_SECONDS` on each worker. Its data is untouched and returns on
reactivation.

**Upgrades.** `python app.py` brings every registered school's schema up to date before serving.
With many schools, run `python -m control_plane upgrade` as a deploy step instead.

**Leftover tables.** A school database made before the website editor was removed still holds its
three tables (`school_public_pages`, `school_public_news`, `school_public_enquiries`). Nothing uses
them. An upgrade drops any that are empty, so nothing can be lost; one that still holds rows is kept
and a warning says so. `python -m control_plane drop-retired-tables` shows what each school still
holds, and adding `--yes` drops it for good.

**Limits are shared by every worker.** Sign-in (schools and the console), password recovery and the
email/WhatsApp test buttons are limited to a number of tries per stretch of time, for example
8 sign-ins per 5 minutes per address and username. The counts live in the registry database (table
`rate_limits`), so several workers, or a restart, cannot multiply or reset them. Each is a fixed
window, and only a hash of the key is stored, never an address or a username. If the registry
cannot be reached each worker counts in its own memory until it can again, and logs a warning at
most once a minute; nobody is locked out. A limit at the reverse proxy in front is still worthwhile,
and behind a proxy set `BRIGHTSTARS_TRUSTED_PROXIES` (below) so the application sees the visitor's
real address, or every visitor shares one count. The registry connection gives up after 3 seconds
rather than holding a request.

**A restart signs everyone out, except people sitting an exam.** Sign-in is a signed cookie, and the
signing secret does not change, so without this a cookie would outlive every restart. Each time the
server is started it writes a new random *launch id* into the registry database (`python app.py` does
it just before it starts serving; `python -m control_plane upgrade`, or `python -m control_plane
new-launch`, does it for a deployment started some other way, so make either a deploy step there).
Every session carries the id it was opened under. After a restart, staff, students, parents,
candidates and platform admins meet the sign-in page ("The system was restarted, please sign in
again") on their next click. The exception is anyone in the middle of an exam at that moment: a
candidate with an unfinished paper, a student with an unfinished test, examination, practice paper or
quiz whose time has not run out. They are kept, and their answers, the heartbeat and the submit carry
on as if nothing happened. The exam clock keeps running while the server is off, so a paper whose time
ran out during the restart is finished from the answers already saved; the person signs in and sees it
marked. If the registry cannot be read, or no launch was ever recorded, nobody is signed out.

**A changed password ends the account's other sign-ins.** Whoever changes their own password stays
signed in where they did it; every other browser signed in to that account is signed out. The same
happens when an administrator resets someone's login, when an emailed reset link is used, and for a
platform admin's password. (The check compares a fingerprint of the stored password with the one the
session was opened under, so it holds however the password was changed.)

**Behind a reverse proxy.** `BRIGHTSTARS_TRUSTED_PROXIES` says how many reverse proxies (nginx, Caddy, a load balancer) stand
between the internet and the application. **Leave it at `0` unless you run one.** With `0` the
application takes a visitor's address and http/https from the connection itself and ignores the
`X-Forwarded-For` and `X-Forwarded-Proto` headers, because anyone can write them: trusting them
blindly would let a visitor put any address they like in the audit log and dodge the sign-in limits.

When a proxy is in front, every connection appears to come from the proxy, so the audit log and the
limits would see one "visitor". Set the number to how many proxies of yours the request passes through
(1 for the usual single nginx or Caddy; 2 if a load balancer sits in front of that; at most 5). Then the
application believes the address and the scheme the last *n* proxies report and nothing a visitor wrote
in front of them. Too high a number lets a visitor forge their address; too low records the proxy's.
Only the address and the scheme are believed, never the host: the `Host` header chooses the school, so
your proxy must pass it through unchanged. A value that is not a whole number from 0 to 5 stops the
application starting, with a message saying so. With a TLS-terminating proxy, also set it so links in
emails and the console's "Enter school" redirect use `https`.

The audit log stores the connection's address (as corrected by this setting), cut to 64 characters.
Every response carries `Referrer-Policy: same-origin`, and the password-reset pages are never cached and
send no referrer, so a reset link is not kept or shown to anyone.

**Uploaded files** (`/static/uploads/...`) are not open to every signed-in account. Each folder has its
own rule (`core/upload_access.py`): the school's logo and sign-in photographs are public; message
attachments are never served there; staff photographs and signatures are for staff; a candidate's
photograph is for staff and that candidate; a student's photograph is for staff, that student and a
parent linked to them; question and assignment pictures are for staff and the students and candidates
who sit the exams; any other folder is for staff. Ownership is read from the database row that holds the
file, and a refusal is a plain 404.

**Connections.** Each school has its own connection pool, so total connections scale with
schools × pool size × workers. Size PostgreSQL's `max_connections` accordingly, or put a pooler
in front. With PgBouncer in transaction mode, use a database per school rather than a schema per
school.

**More than one application server** needs `BRIGHTSTARS_TENANTS_DIR` on shared storage, or an
object-store backend behind `core/storage.py`.

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
- **The school interface has one design system.** `static/school-ui.css` holds the tokens (the school's colours as `--brand`,
  `--brand-dark` and `--brand-accent`, a warm-grey surface scale, type, spacing, radii, the focus ring) and the components built on them:
  the navigation rail, the workspace home, and the sign-in and password pages. Use the tokens rather than new colours or sizes. Fonts are
  self-hosted in `static/fonts/` (`fonts.css`, licences alongside): Public Sans for text, Newsreader for titles and the school's name, IBM Plex
  Mono for codes only. Who sees which menu item is one list in `templates/admin_base.html`; icons come from `templates/includes/_nav_icons.html`.
  Sign-in and password pages extend `templates/auth_base.html`. The platform console keeps its own look (`static/platform.css`).
- **The schema is declared in `models/` alone.** `create_all()` builds a school's tables and a
  column-diffing helper at start-up adds any column an older database lacks; there are no migration
  scripts to keep in step.
- **Question banks are JSON files** under `tenants/<code>/data/`, owned by the school. A new school is
  given the platform's standard set, and can import its own (see *Question banks* below).

## Known gaps

None that are currently known. When one is found it is listed here, with what it affects and what
to do about it.
