# Brightstars Academics — Multi-Tenancy

Brightstars Academics is the platform. Each school it serves is a **tenant**: it has its
own portal address, its own database, and its own folder of files. The platform operators are the only
super admins; a school manages its own staff and roles.

Two rules shape everything below:

* **A school gets a portal, not a website.** A school's address serves sign-in
  and the admin, student, parent and candidate areas — nothing else. The
  deployment serves no public website at all: the platform's own marketing site
  is hosted separately (for example `brightstars.com`), and `/` on a platform
  hostname redirects to the console.
* **The platform issues the address.** Every school is given
  `<code>.<portal domain>` when it is created, and it works immediately. A
  school that wants its own domain points a CNAME record at that address.

> **PostgreSQL is the only supported database**, in development and in
> production. The platform registry is one database (`brightstars_platform`)
> and every school gets its own (`brightstars_<code>`), created as the school
> is created. There is no SQLite application database and no `cbt.db`.

## Status

| Piece | State |
|---|---|
| Platform registry (schools, addresses, platform admins, audit log) | Built (`control_plane/`) |
| School chosen from the request's hostname; unknown/suspended hosts refused | Built |
| One database per school, chosen per request, fail-closed | Built |
| Issued portal address per school, working immediately; CNAME for the school's own domain | Built |
| Schools have no public website; the deployment serves none, and `/` on a platform host opens the console | Built |
| Everything a school owns in one `tenants/<code>/` folder | Built |
| Session cookies bound to their school | Built |
| **Platform console**: sign-in, dashboard, create school, addresses, administrators, suspend, activity | Built |
| **Create-school form captures branding** (name, motto, contact, logo) and applies it at creation | Built |
| **"Enter school"** — single-use, expiring, school-bound ticket | Built |
| CLI: create / upgrade / suspend / domains / platform admins | Built |
| **School Admin role** (a school's top role is "School Admin"; existing schools are renamed in place at start-up) | Built |
| **Branding sweep**: no school's name or code appears in shared code (enforced by a contract test) | Built |
| **Per-school email and WhatsApp**, secrets encrypted at rest, with the platform's account as the fallback | Built |
| **Uploads are access-controlled** per folder: branding public, messages only via their own route, the rest for signed-in accounts | Built |
| PostgreSQL everywhere; a database per school, created on demand | Built |
| **Verified against a live PostgreSQL server**: every page opened and every form submitted | Built — see [Status of the PostgreSQL work](#status-of-the-postgresql-work) |

Multi-tenancy **cannot be switched off**. There is no flag: a request without a
school resolves to nothing and any query it makes raises. A toggle would mean a
second code path that nobody exercises, which is exactly where a cross-school
data leak would hide.

## How a request is served

```
https://portal.theschool.example/login      (CNAME → theschool.<portal domain>)
        │  Host header only (never a URL parameter, form field or cookie)
        ▼
control_plane.resolver     host → platform registry → school "theschool"
        │                   platform hostname → the platform site and console
        │                   unknown host → 404, suspended school → 503
        │                   /school/… on a school → 404 (a portal has no website)
        │                   session from another school → discarded
        ▼
current school = theschool  (a ContextVar for the request)
        │
        ├─ db.session ──► TenantSession picks theschool's engine (its own database)
        ├─ core/storage ► tenants/theschool/data     (question banks)
        ├─ core/branding ► the school's own name, motto and logo
        └─ /static/uploads/… ► tenants/theschool/uploads
```

### What each hostname serves

| Hostname | Serves |
|---|---|
| `BRIGHTSTARS_PLATFORM_HOSTS` | The platform console under `/platform`; `/` redirects to its sign-in page |
| `<code>.<BRIGHTSTARS_PORTAL_DOMAIN>` | That school's portal (issued on creation, permanent) |
| A school's own domain (CNAME → the above) | The same portal |
| Anything else | 404, before any application code runs |

**Fail-closed.** If code touches the database with no school selected,
`NoTenantError` is raised. It never falls back to a default database, because
guessing a school could show one school's data to another.

**Platform host.** Hostnames in `BRIGHTSTARS_PLATFORM_HOSTS` belong to the
platform, not to any school: they redirect `/` to the console, serve the
console under `/platform`, shared static assets and `/health`, and 404 for
everything else. A school's portal is never served there, and a school's
uploads are never served there either.

## Code map

| Path | Role |
|---|---|
| `control_plane/models.py` | Registry tables: `Tenant`, `TenantDomain`, `PlatformAdmin`, `PlatformAuditLog` — in their own database, own declarative base |
| `control_plane/registry.py` | Registry engine/session, host → school lookup (positive-hit cache), slug/hostname validation |
| `control_plane/context.py` | `TenantInfo` (immutable snapshot), the per-request current school, `tenant_context()` |
| `control_plane/routing.py` | `TenantSession`, per-school engine cache, `build_engine()` (SQLite and PostgreSQL) |
| `control_plane/resolver.py` | `before_request` hook + session-to-school binding; `install(app)` |
| `control_plane/launch.py` | The server's launch id: written to the registry (`platform_state`) once at each start, read (cached a few seconds) by every worker |
| `core/session_guard.py` | The hook right behind the resolver: a restart or a changed password ends a sign-in (exam sitters excepted) |
| `core/upload_access.py` | Which people may open which folder of a school's uploads |
| `control_plane/provisioning.py` | Create / upgrade schools, first admin, suspend, domains |
| `control_plane/console.py` | The platform console: sign-in, dashboard, create school, addresses, administrators, suspend, activity, "Enter school" |
| `control_plane/entry.py` | Platform-admin sign-in, entry tickets, the reserved operator account inside a school |
| `control_plane/cli.py` | `python -m control_plane …` |
| `core/storage.py` | `data_dir()`, `uploads_dir()`, `stored_upload_path()` (local backend) and the backend-aware `read_upload_bytes()`, `upload_exists()`, `delete_upload()`, `save_upload_bytes()`, `list_data_names()`, `read_data_text()`, `write_data_bytes()` — per school |
| `core/object_store.py` | The S3-compatible client used when `BRIGHTSTARS_STORAGE_BACKEND=s3` |
| `core/branding.py` | The school's own name/motto/logo, exposed to every template as `school_brand` |
| `core/numbering.py`, `core/numbering_pattern.py` | Each school's numbering rules: a pattern per school in `tenants/<code>/numbering.json`, set on the console, read and checked and never run |
| `templates/platform/` | The console and the platform website |
| `app.py` | Installs the resolver first, serves `/static/uploads/<path>` per school, `init_db(school=…, bootstrap_super_admin=…)` |
| `tests/verification/write_paths_multitenancy.py` | End-to-end isolation checks (49) |
| `tests/verification/write_paths_session_guard.py` | Restart sign-out (exam sitters kept), password changes ending sign-ins, referrer/caching headers, trusted proxies |
| `tests/verification/write_paths_upload_access.py` | Every uploads folder against every kind of person |
| `tests/verification/write_paths_numbering_rules.py` | The numbering patterns: the language, the console editor and preview, audit, candidates and students, a corrupt file |
| `tests/verification/write_paths_platform_console.py` | End-to-end console checks (84) |
| `tests/current/test_multitenancy_contract.py` | Fast contract guards |

`models/base.py` builds `db` with `TenantSession`. Nothing else in the
blueprints changed for routing: every query already went through `db.session`.

## Local development

1. Set in `.env` (see `.env.example`): `BRIGHTSTARS_PLATFORM_DB` and a strong
   `BRIGHTSTARS_SECRET`.
2. Create the registry and yourself:

   ```bash
   python -m control_plane init
   python -m control_plane create-platform-admin ops --display-name "Ops"
   ```
3. `python app.py`, then open the console at
   `http://platform.localhost:5000/platform` and create your schools there — that form
   captures each school's branding and logo, which the CLI does not. A school's portal is at
   `http://<school-code>.localhost:5000`.

`*.localhost` resolves to the loopback address in current browsers, so no
hosts-file edit is needed in development.

Other commands: `list`, `upgrade [CODE]`, `create-tenant CODE "Name"`,
`add-domain CODE HOST [--primary]`, `remove-domain HOST`,
`suspend CODE --reason …`, `activate CODE`.

`python app.py` in multi-tenant mode upgrades every registered school's schema
before serving.

## Addresses: the issued portal address, and the school's own domain

**The portal address is issued, immediate and permanent.** Creating a school
with the code `alpha` registers `alpha.<BRIGHTSTARS_PORTAL_DOMAIN>` as its
`portal` domain, marked primary. It needs no DNS work by the school, it is the
address "Enter school" always uses, and it cannot be removed. Point a wildcard
`*.<portal domain>` at your server once and every future school works the
moment it is created.

**A school's own domain is a CNAME onto that.** Add it in the console (or with
`add-domain`), then the school creates one DNS record:

```
portal.theschool.example.   CNAME   alpha.schools.brightstars.example.
```

The console shows exactly that record on the school's page. A school's own
domain is stored as a `custom` domain and can be added, made primary or removed
without ever affecting the portal address.

**TLS.** The reverse proxy in front of Flask must hold a certificate for every
hostname it serves — a wildcard for the portal domain, plus one per school
domain (e.g. Caddy's on-demand TLS restricted to hostnames the registry knows,
or certbot). It must **pass the original `Host` header through unchanged**,
because the application picks the school from it.

Only hostnames in the registry are ever served, so a stray or malicious `Host`
header gets a 404, not a school.

## Security model

* **School selection is by hostname only.** Not by URL, form, or cookie.
* **Sessions are bound to a school.** All schools share one signing key, so a
  cookie signed for school A is cryptographically valid on school B. Each session
  is stamped with its school id; on any mismatch it is cleared before any account
  lookup runs. (Without this, admin #3 at school A could be admin #3 at school B.)
  This also signs everyone out once at cut-over.
* **A restart signs everyone out, except people sitting an exam.** Each server start
  writes a new random *launch id* into the registry (`platform_state`, key `launch_id`),
  from `python app.py`, `python -m control_plane upgrade` or `python -m control_plane
  new-launch`, before any worker serves a request; workers only read it, cached for a
  few seconds, so several workers of one start agree and the next start differs. Every
  session is stamped with the id at sign-in. A request whose stamp is not the current
  id is a sign-in from before the restart: if that person has an exam running (a
  candidate with an unfinished paper; a student with an unfinished test, examination,
  practice paper, or quiz) the session is kept and stamped again, so they carry on
  where they were; anyone else is signed out and meets the sign-in page (staff,
  students, parents, candidates and the platform console alike). The exam clock is the
  server's and keeps running while it is off, so a paper that has run out of time by
  the restart protects nobody. If the registry (or the school's database, for the exam
  check) cannot be read, or no launch was ever recorded, nobody is signed out. The
  check does not run for `/health` or shared static files. Where the server is started
  by some other means than `python app.py` (a WSGI server), run `python -m
  control_plane upgrade` (or `new-launch`) as the deploy step, or restarts will not
  sign anyone out.
* **A changed password ends the account's other sign-ins.** Each session also carries
  a fingerprint of the password hash the account had at sign-in. Any change of the
  hash (the person, an administrator's reset, an emailed reset link, a command)
  invalidates every session made with the old one; the person who changed their own
  password is stamped again and stays in. It is judged from the stored hash, so no
  route can forget to do it. A platform operator's reserved account inside a school
  is exempt (its hash changes on every entry and is never a way in).
* **Uploaded files have a rule per folder** (`core/upload_access.py`, decided on the
  normalised path): `branding/` public; `messages/` never served there; `admins/`
  and `signatures/` staff; `candidates/` staff and that candidate; `students/` staff,
  that student and a parent actively linked to them (matched through the owning row's
  `photo_path`); `questions/` staff, students and candidates; `assignments/` staff and
  students; any other folder staff only. A refusal is a plain 404. A sign-in that has
  ended, a switched-off account and a cookie of another school open nothing.
* **Proxies.** By default no proxy is trusted: the application uses the address and
  scheme of the connection and ignores every `X-Forwarded-*` header, so the audit log
  (which stores `request.remote_addr`, cut to 64 characters) and the rate limits cannot
  be fooled by a forged header. `BRIGHTSTARS_TRUSTED_PROXIES=n` (n >= 1, at most 5)
  wraps the app in werkzeug's `ProxyFix(x_for=n, x_proto=n)` only. Never the host or
  the prefix: the `Host` header chooses the school.
* **Referrers and caching.** Every response carries `Referrer-Policy: same-origin`; the
  password-reset pages (`/reset-password/<token>`, `/forgot-password`) also send
  `no-referrer` and `Cache-Control: no-store`, so a reset token in a URL is neither
  kept nor named to anyone.
* **A school's numbering rules are data, not code.** Its candidate-code and student-number
  patterns are set on the platform console and kept in `tenants/<code>/numbering.json`. A pattern
  is read and checked (`core/numbering_pattern.py`), never executed, so being able to set one does
  not let anyone run code on a server that can reach every school's database. Every save is in the
  platform audit trail with the old and the new pattern.
* **Files are per school.** Uploads, question banks and numbering rules live under
  `BRIGHTSTARS_TENANTS_DIR/<code>/` (the default `local` storage backend) or under
  that school's own key prefix in an S3-compatible bucket (`BRIGHTSTARS_STORAGE_BACKEND=s3`
  — see "Storage backend" below); `/static/uploads/…` is served from the requesting
  school's folder either way, and a stored upload value is confined to the school's
  own folder or prefix under either backend (`core/storage.py`'s
  `_sanitize_upload_relative`, shared by both).
* **Suspending a school** takes effect within `BRIGHTSTARS_REGISTRY_CACHE_SECONDS`
  (default 10 s) on each worker process; `0` disables the cache.
* **No credential in the registry (optional).** A school's `db_url` may be
  `env:VARIABLE_NAME`, resolved from the environment at connect time.
* **Platform actions are audited** in `platform_audit_log`, including every
  entry into a school, every sign-in and failed or refused attempt on a real
  account, and every change to the team. Each entry carries the admin's id (and
  a username snapshot), so it stays readable, and attributable, after that admin
  is removed. Removing an admin switches the account off and switches off their
  reserved `platform@<username>` account in every school; nothing is deleted.
  What an admin does *inside* a school is recorded in that school's own
  `audit_logs`, under that reserved account; the platform's activity log reads
  it from there, read-only and only from schools the admin has entered, so one
  log shows an admin's whole footprint.
* **The console is invisible from a school.** `/platform…` 404s on a school's
  address, and a school's portal 404s on the platform host. A platform session
  (`platform_admin_id`) is never a school session (`admin_id`).
* **Entering a school** uses a ticket that is single-use, expires in two
  minutes, is bound to one school, and is stored only as a SHA-256 hash.
  Redeeming it signs the operator in against a reserved account in that
  school's own database, named `platform@<username>`. A school's own account
  form rejects `@`, so no school can create or impersonate that name, and the
  account's password is replaced with the hash of a fresh random secret on
  every entry, so it can never be signed into through the school's login form.
* **A school's branding is set before it opens**, so no portal is ever shown
  under another school's name.

### Not yet isolated per school

* One `BRIGHTSTARS_SECRET` signs every school's sessions (fine for now; a per-school
  key derived from it is a possible later hardening).
* `BRIGHTSTARS_SMTP_*` / `BRIGHTSTARS_WHATSAPP_*` are the platform's *shared* account, the
  fallback for a school that has set up none of its own (core/delivery.py).
* Login rate limiting and presence caches are in process memory (already
  documented as single-worker-only in `core/accounts.py`).

## PostgreSQL (production)

### Layout

Two supported shapes; the registry row says which a school uses:

| Shape | `db_url` | `db_schema` | Notes |
|---|---|---|---|
| Database per school | one URL per school | empty | Strongest isolation, simplest backup/restore/delete of one school. |
| Schema per school | the same URL for many schools | `school_<code>` | Fewer databases and connections to manage; applied as the connection's `search_path`. |

Recommended starting point: **database per school** until the number of schools
makes that unwieldy. Because `build_engine()` supports both, switching a school
later is a data move, not a code change.

The platform registry (`BRIGHTSTARS_PLATFORM_DB`) is its own database, e.g.
`postgresql+psycopg://user:pass@host/brightstars_platform`.

**Managed providers without a database called `postgres`.** Creating a school's database
(or the registry's own, the first time) runs `CREATE DATABASE` against the server's
maintenance database, which a stock PostgreSQL install always has one of, named
`postgres`. Some managed providers don't ship one under that name — Aiven's default
database is `defaultdb`, for instance — so `ensure_database_exists()`
(`control_plane/routing.py`) tries the target database directly first (already there,
the common case, needs nothing else), and only falls back to a maintenance connection
for a database that is genuinely missing. Set `BRIGHTSTARS_PG_MAINTENANCE_DB` to whatever
database is guaranteed to exist on your server when it isn't `postgres` (`defaultdb` on
Aiven).

```bash
pip install "psycopg[binary]>=3.1"
python -m control_plane create-tenant theschool "The School" \
    --domain portal.theschool.example \
    --db-url "env:THESCHOOL_TENANT_DB_URL"           # or a literal URL
python -m control_plane create-tenant demo "Demo" --domain demo.example \
    --db-url "env:SHARED_TENANT_DB_URL" --db-schema school_demo
```

### Status of the PostgreSQL work

Every SQLite-only construct has been replaced:

| Was | Now |
|---|---|
| `sqlalchemy.dialects.sqlite.insert(...)` upserts in 8 modules | `core.db_helpers.insert_stmt()`, which picks the dialect from the school's own database |
| `func.group_concat` | `core.db_helpers.group_concat()` (`string_agg` on PostgreSQL, with a cast) |
| Partial indexes declared only with `sqlite_where=` | `postgresql_where=` declared alongside, and a contract test keeps the two counts equal |
| A SQLite table rebuild in `_widen_parent_feedback_reply_admin_id` | returns immediately on PostgreSQL, where the column is already nullable |

**Verified.** `tests/verification/write_paths_pg_smoke.py` opens every page and
`write_paths_pg_posts.py` submits every form, on a real PostgreSQL server; the other suites in
`tests/verification/` cover isolation between schools and the platform console. Each creates its
own throwaway databases (`bs_test_*`) and drops them afterwards, so running them is safe. The two
things that most often differ from SQLite are dealt with: `LIKE` is case-sensitive on PostgreSQL,
so searches use `icontains(..., autoescape=True)`; and PostgreSQL refuses a value that does not fit
its column, which the form-submission suite provokes on every route.

### Operating notes

* Each school has its own connection pool. Total connections ≈ schools × pool
  size × worker processes — size PostgreSQL `max_connections` (or put a pooler in
  front) accordingly. With PgBouncer in *transaction* mode, `search_path` set via
  connection options is not preserved: use database-per-school, or session
  pooling, with schema-per-school.
* Startup upgrades every school sequentially; with many schools, move to
  upgrade-on-deploy (`python -m control_plane upgrade`) and skip it at start.
* Backups are per database (or per schema) — one school can be restored without
  touching another.
* More than one application server needs `BRIGHTSTARS_TENANTS_DIR` on shared
  storage, or `BRIGHTSTARS_STORAGE_BACKEND=s3` (see "Storage backend" below).

## Storage backend

`BRIGHTSTARS_STORAGE_BACKEND` chooses where a school's uploads, question banks and
numbering rules live:

* **`local`** (the default): a folder on this machine, `BRIGHTSTARS_TENANTS_DIR/<code>/`.
  Simplest for a single application server with a persistent disk.
* **`s3`**: an S3-compatible object store (`core/object_store.py`) — real AWS S3, or
  Cloudflare R2, Backblaze B2, DigitalOcean Spaces or MinIO by also setting
  `BRIGHTSTARS_S3_ENDPOINT_URL`. Needed whenever the application server's disk is not
  persistent (many hosting platforms wipe it on every redeploy or restart), or more
  than one application server must share the same files. Requires
  `BRIGHTSTARS_S3_BUCKET`, `BRIGHTSTARS_S3_ACCESS_KEY_ID` and
  `BRIGHTSTARS_S3_SECRET_ACCESS_KEY`; see `.env.example` for the full list, and
  `pip install boto3` (or uncomment it in `requirements.txt`).

Every stored database value (`uploads/<folder>/<file>`) is identical under either
backend — only where the bytes physically live changes — so switching backends does
not touch a school's database. It does mean *moving* existing files into the new
backend by hand if switching after schools already have uploads; there is no
migration command for that yet.

A school's `generated/` folder (Chrome-rendered result images) is always local,
under either backend: nothing in it is ever served to more than the one request that
made it, so there is nothing to share between application servers or keep in an
object store.

## Roles

| Who | Lives in | Can |
|---|---|---|
| **Super admin** | platform registry | Everything a platform admin can do, plus adding, removing, restoring and resetting platform admins and reading every admin's activity log. There is always one; it cannot be removed, and the console cannot create another |
| **Platform admin** | platform registry | Create/suspend schools, manage domains, enter any school, and — like a school admin — manage that school's staff and roles. Cannot touch the team or read anyone else's log |
| **School admin** | the school's own database | Manage their school's staff, roles, permissions and data. Cannot see or affect other schools |
| Staff / parent / student / candidate | the school's own database | As today |

A platform admin inside a school acts through the reserved `platform@<username>`
account described above, which holds that school's system role — so they can do
everything a school admin can, and every entry is recorded on both sides.

A school admin *can* suspend or reset that reserved account from their own
Accounts page. That grants them nothing: it is an account in their own school
only, carrying authority they already hold there, and the next platform entry
re-activates it and re-randomises its password.

## Known gaps

* **Only pages are exercised on PostgreSQL, not every write.** `tests/verification/write_paths_pg_smoke.py`
  opens every admin, student, parent and candidate page with data behind it and fails on any
  database error; it found and fixed queries SQLite accepted and PostgreSQL rejects (`GROUP BY`
  strictness, `date('now')`). Form submissions are covered by the feature suites, not exhaustively.
* Rate limiting and presence caches are per process; put a limit at the reverse proxy as well.
* Databases that predate the retirement of the school website editor keep its three tables, unused.
* Question banks are still per-school JSON files (under `tenants/<code>/data/`, or
  that school's own key prefix in the bucket with `BRIGHTSTARS_STORAGE_BACKEND=s3`).
  A new school starts with none, so its entrance examinations cannot run until
  banks are imported for it.
