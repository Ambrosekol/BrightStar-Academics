# Brightstars Academics — Multi-Tenancy

Brightstars Academics is the platform. Each school it serves (Creative Rainbow,
"Crainbow", is the first) is a **tenant**: it has its own portal address, its
own database, and its own folder of files. The platform operators are the only
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
| CLI: create / register / upgrade / suspend / domains / platform admins | Built |
| Existing Crainbow installation → first tenant (copy, originals untouched) | Built |
| Existing Super Admin → platform admin | Built |
| **School Admin role** (renaming a school's top role away from "Super Admin") | **Not yet** |
| **Branding sweep** through the remaining templates, result cards and emails | **Partly** — see [Known gaps](#known-gaps) |
| PostgreSQL everywhere; a database per school, created on demand | Built |
| Importing an existing SQLite installation into a school's PostgreSQL database | Built |
| Crainbow reduced to an ordinary tenant; no school name anywhere in shared code | Built |
| **Verified against a live PostgreSQL server** | **Not yet** — see [Status of the PostgreSQL work](#status-of-the-postgresql-work) |

Multi-tenancy **cannot be switched off**. There is no flag: a request without a
school resolves to nothing and any query it makes raises. A toggle would mean a
second code path that nobody exercises, which is exactly where a cross-school
data leak would hide.

## How a request is served

```
https://portal.creativerainbow.example/login      (CNAME → crainbow.<portal domain>)
        │  Host header only (never a URL parameter, form field or cookie)
        ▼
control_plane.resolver     host → platform registry → school "crainbow"
        │                   platform hostname → the platform site and console
        │                   unknown host → 404, suspended school → 503
        │                   /school/… on a school → 404 (a portal has no website)
        │                   session from another school → discarded
        ▼
current school = crainbow  (a ContextVar for the request)
        │
        ├─ db.session ──► TenantSession picks crainbow's engine (its own database)
        ├─ core/storage ► tenants/crainbow/data     (question banks)
        ├─ core/branding ► the school's own name, motto and logo
        └─ /static/uploads/… ► tenants/crainbow/uploads
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
| `control_plane/provisioning.py` | Create / register-existing / upgrade schools, first admin, adopt Super Admin, suspend, domains |
| `control_plane/console.py` | The platform console: sign-in, dashboard, create school, addresses, administrators, suspend, activity, "Enter school" |
| `control_plane/entry.py` | Platform-admin sign-in, entry tickets, the reserved operator account inside a school |
| `control_plane/cli.py` | `python -m control_plane …` |
| `core/storage.py` | `data_dir()`, `uploads_dir()`, `stored_upload_path()` — per school |
| `core/branding.py` | The school's own name/motto/logo, exposed to every template as `school_brand` |
| `templates/platform/` | The console and the platform website |
| `app.py` | Installs the resolver first, serves `/static/uploads/<path>` per school, `init_db(school=…, bootstrap_super_admin=…)` |
| `tests/verification/write_paths_multitenancy.py` | End-to-end isolation checks (49) |
| `tests/verification/write_paths_platform_console.py` | End-to-end console checks (84) |
| `tests/current/test_multitenancy_contract.py` | Fast contract guards |

`models/base.py` builds `db` with `TenantSession`. Nothing else in the
blueprints changed for routing: every query already went through `db.session`.

## Local development

1. Set in `.env` (see `.env.example`): `BRIGHTSTARS_MULTITENANT=1`, a strong
   `BRIGHTSTARS_SECRET`.
2. Create the registry and yourself:

   ```bash
   python -m control_plane init
   python -m control_plane create-platform-admin ops --display-name "Ops"
   ```
3. Bring Crainbow in as the first school (copies; nothing is moved or deleted):

   ```bash
   python -m control_plane register-existing crainbow "Creative Rainbow Montessori School" \
       --from-db cbt.db --from-data data --from-uploads static/uploads
   python -m control_plane adopt-superadmin --from-db cbt.db
   ```
4. `python app.py`, then open the console at
   `http://platform.localhost:5000/platform` and create any further schools
   there — that form captures each school's branding and logo, which the CLI
   does not. Crainbow's own portal is at `http://crainbow.localhost:5000`.

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
* **Files are per school.** Uploads and question banks live under
  `BRIGHTSTARS_TENANTS_DIR/<code>/`; `/static/uploads/…` is served from the
  requesting school's folder, `send_from_directory` refuses traversal, and stored
  upload paths are confined to the school's folder (`stored_upload_path`).
* **Suspending a school** takes effect within `BRIGHTSTARS_REGISTRY_CACHE_SECONDS`
  (default 10 s) on each worker process; `0` disables the cache.
* **No credential in the registry (optional).** A school's `db_url` may be
  `env:VARIABLE_NAME`, resolved from the environment at connect time.
* **Platform actions are audited** in `platform_audit_log`, including every
  entry into a school.
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
* SMTP and WhatsApp credentials (`BRIGHTSTARS_SMTP_*`, `BRIGHTSTARS_WHATSAPP_*`) are
  still global environment settings; per-school delivery settings are a follow-up.
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

```bash
pip install "psycopg[binary]>=3.1"
python -m control_plane create-tenant crainbow "Creative Rainbow" \
    --domain portal.creativerainbow.example \
    --db-url "env:CRAINBOW_TENANT_DB_URL"           # or a literal URL
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
| `INSERT OR IGNORE` through the raw driver in `_record_schema_baseline` | the `SchemaMigration` model and a dialect-neutral insert |
| A SQLite table rebuild in `_widen_parent_feedback_reply_admin_id` | returns immediately on PostgreSQL, where the column is already nullable |

**What has not happened yet:** none of this has been exercised against a
running PostgreSQL server, because the work was done before the connection
details were available. Until `tests/verification/write_paths_multitenancy.py`
and `write_paths_platform_console.py` have been run against a live server, treat
PostgreSQL support as written-but-unproven. Both scripts create their own
throwaway databases (`bs_test_*`) and drop them afterwards, so running them is
safe.

Two things are worth watching for on the first real run:

* **Case sensitivity.** `LIKE` is case-insensitive on SQLite and case-sensitive
  on PostgreSQL. Around 21 call sites in `blueprints/` use `like`/`startswith`
  for name and username searches; each needs judging on its merits, and the
  search-box ones should become `ilike`.
* **Type strictness.** SQLite accepts any value in any column; PostgreSQL does
  not. An import from an old SQLite database can therefore fail on a row that
  the old database was happy to store.

### Moving an existing SQLite installation into a school

One command does the whole thing:

```bash
python -m control_plane register-existing crainbow "Creative Rainbow Montessori School" \
    --from-db cbt.db --from-data data --from-uploads static/uploads
```

It creates the school like any other (PostgreSQL database, portal address,
folder), then imports every row of the SQLite database into it table by table in
dependency order, resets each identity sequence past the imported ids, and copies
the question banks and uploads into the school's own folder. The source database
is opened **read-only** and is never modified, so it remains a rollback until you
choose to delete it.

Afterwards, bring the old Super Admin across as a platform operator:

```bash
python -m control_plane adopt-superadmin --from-db cbt.db
```

Check the result before retiring the old file — student, result and payment
counts in the school should match the old database.

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
  storage (or an object-store backend behind `core/storage.py`).

## Roles

| Who | Lives in | Can |
|---|---|---|
| **Platform admin** | platform registry | Create/suspend schools, manage domains, enter any school, and — like a school admin — manage that school's staff and roles |
| **School admin** | the school's own database | Manage their school's staff, roles, permissions and data. Cannot see or affect other schools |
| Staff / parent / student / candidate | the school's own database | As today |

The existing Super Admin (`adopt-superadmin`) becomes a platform admin: same
username, same password hash, no reset.

A platform admin inside a school acts through the reserved `platform@<username>`
account described above, which holds that school's system role — so they can do
everything a school admin can, and every entry is recorded on both sides.

A school admin *can* suspend or reset that reserved account from their own
Accounts page. That grants them nothing: it is an account in their own school
only, carrying authority they already hold there, and the next platform entry
re-activates it and re-randomises its password.

## Next steps

1. **Finish the branding sweep** (below) — the one thing standing between a
   newly created school and real users.
2. **School Admin role.** Inside a school the top-level system role (today named
   "Super Admin", referred to in ~5 places by name and ~89 by its `is_system`
   flag) should become "School Admin". Crainbow needs its own school-admin
   account before its current Super Admin is retired from the school.
3. **Per-school delivery settings**, so each school sends receipts and password
   resets from its own SMTP/WhatsApp account rather than the global one.
4. **Retire the school-website features that now have no audience**: the public
   page and news editors under `/admin/school/website`, and the enquiry inbox
   the removed contact page fed. The branding fields on that page are still
   used and should stay.
5. **PostgreSQL milestone** (above).

## Known gaps

* **The branding sweep is unfinished.** A school's name, motto, contact details
  and logo are captured when it is created and stored in its own database, and
  `core/branding.py` exposes them to every template as `school_brand`. The
  sign-in page — a school's front door — uses them, and receipt PDFs use the
  school's own logo. But around 250 mentions of "Crainbow" / "Creative Rainbow"
  remain across roughly 100 templates and several Python modules (result cards,
  email subjects and bodies, CSV file names, page titles). **Until those are
  swept, a newly created school is not ready to show to real users.** The fix is
  mechanical: replace each hard-coded name with `school_brand.name`, and each
  `images/school_logo.png` with `school_brand.logo_url`.
* `static/uploads/messages/…` is reachable without authentication at its
  `/static/uploads/…` URL (unguessable file names, but not access-controlled),
  alongside the permission-checked download route that exists for the same files.
  This predates multi-tenancy and behaves identically now; it is worth closing.
* The login page never displays its `error` message (the template only renders
  flashed messages), so a failed sign-in shows no explanation. Also pre-existing.
* Question banks are still per-school JSON files under `tenants/<code>/data/`.
  A new school starts with none, so its entrance examinations cannot run until
  banks are imported for it.
