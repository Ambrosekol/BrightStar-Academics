# Phase 13 merge into the SQLAlchemy codebase

This records what was merged, how it was verified, and every defect found along
the way. Written for whoever maintains this codebase next.

## What was merged

The Phase 13 tree turned out to be a strict superset of this project: nothing
existed here that was missing there.

| | only here | new from Phase 13 |
|---|---|---|
| routes | 0 | 58 |
| top-level functions | 0 | 133 |
| templates | 0 | 57 |
| database tables | 0 | 37 |
| migrations | 0 | 11 (0006–0019) |

The only thing this project had that Phase 13 lacked was the SQLAlchemy layer.
So rather than porting 58 routes across and hand-reconciling 93 drifted shared
functions, the Phase 13 `app.py` became the base and the SQLAlchemy conversion
was re-applied to it.

New feature areas: finance (fee structure, assessments, payments, allocation,
receipts, delivery logs), library (catalogue, issue, return), the parent portal
(dashboard, child detail, feedback), the public website (pages, news,
enquiries), the end-of-session promotion engine, the result approval workflow
(entered → verified → approved → released), entrance examination configuration
with historical practice, live presence tracking, password recovery, student
number allocation, and the full admissions record.

## Architecture

* `app.py` contains no raw SQL. Every query goes through SQLAlchemy.
* `models.py` declares all 72 tables and is the single description of the
  schema. A test asserts it matches the live database.
* `services/student_number_generator.py` takes the SQLAlchemy session rather
  than a DBAPI connection, so allocating a student number and creating the
  student commit or roll back together.

### Database bootstrap

`init_db()` now works on an empty database, an older one, and an up-to-date
one, and is safe to run repeatedly:

1. `db.create_all()` creates any missing table from `models.py`.
2. `_add_missing_columns()` adds any column the models declare that an older
   database lacks, carrying NOT NULL and defaults across so an upgraded
   database ends up identical to a freshly created one.
3. `_seed_school_tenant()` creates the school, its numbering policy and the
   `school_id` backfill idempotently.
4. `_record_schema_baseline()` records the historical migrations as applied.

The migrations under `migrations/` are **not** replayed. They are redundant for
schema now that `models.py` owns it, and several are unsafe to re-run: `0012`
aborts with "expected 5 students; found N" because it asserts a row count
rather than a schema shape. Its seed data is handled by `_seed_school_tenant()`
instead, which works on any database.

## Verification

| check | result |
|---|---|
| Routes crawled under 5 identities (anonymous, admin, student, candidate, parent) | 145 |
| Byte-identical to the Phase 13 app | 132 |
| Differing | 13 — every one a deliberate fix listed below |
| Routes that error | **0** (Phase 13: 5) |
| RBAC probes: permission, scope, class, subject, class+subject pair | **895/895 identical** |
| Write paths: finance, library, parents, results workflow, messaging, assessment cycle | 48/48 |
| Write paths: admissions registration, promotion engine | 26/26 |
| Write paths: bank lock and unlock | 21/21 |
| Contract tests | 8/8 |

The write sweeps assert database state, not just HTTP status, and include the
negative cases: duplicate fee assessment refused, over-allocation refused,
double void refused, approve-before-verify refused, commit-before-approval
refused, second commit creating no duplicate enrolments, and re-grading being
idempotent.

## Defects found and fixed

All pre-existing in the Phase 13 code; none caused by the conversion.

1. **No reproducible install.** `init_db()` created 17 of 72 tables then died
   with `no such table: admin_notifications`, because migrations `0003`–`0010`
   have empty bodies. The live `cbt.db` only worked because it accreted over
   months. Fixed by the bootstrap described above.
2. **`migrations/runner.py` entry `0011` was a tuple, not SQL**, so
   `apply_migrations()` raised `AttributeError` on any database that had not
   already recorded it.
3. **Student registration and editing returned HTTP 500.**
   `school_student_form.html` called `url_for('school_students')`; the endpoint
   is `admin_school_students`.
4. **Six features were unusable because their forms omitted the CSRF field.**
   The form posts to a route decorated with `@csrf_protect`, which rejects it
   with 403. Affected: entrance configuration, promotion draft generation,
   promotion progression rules, promotion approve/commit, student
   registration/edit (which spelled the field `csrf_token` rather than
   `_csrf_token`), and starting a CBT assignment.
5. **`/admin/finance/students/<id>/account` returned HTTP 500** — its template
   did not exist. Written to match the other finance pages.
6. **`/admin/candidates/<id>/result/image` could never return an image.**
   `_chrome_result_png()` returns `(png_path, html_path)`, and the route passed
   the whole tuple to `send_file()`.
7. **The public homepage printed `<generator object sync_do_slice at 0x...>`**
   in place of each news excerpt. Jinja's `slice` filter splits a sequence into
   N groups; it is not a substring operation.
8. **"Read story" links on the homepage all pointed at the news index** rather
   than the article, because the template calls `item.get('slug')` and
   `sqlite3.Row` has no `.get`. ORM rows do, so the links now work.
9. **Mojibake em dashes.** Eleven occurrences of `â€”` (a UTF-8 em dash read as
   Latin-1) were shown to users in results, grades and the emailed/WhatsApped
   result text. Replaced with a real em dash.
10. **`_school_current_session` was defined twice** with different behaviour.
    Python kept the second, so the live behaviour was the one with a fallback to
    the most recent active session. Now defined once, with that behaviour and a
    comment explaining why.
11. **A connection leak** in the promotion progressions page: `_promotion_target(db(), ...)`
    opened a connection whose result was never used and never closed.
12. `reportlab` was pinned `>=3.6,<4` here but Phase 13 needs 4.x for PDF
    receipts.
13. **Bank lock/unlock had become unreachable.** The Phase 13 rewrite of
    `admin_controls.html` dropped the "Protected examination resources" panel
    while leaving the `admin_lock_resource` / `admin_unlock_resource` routes and
    all five enforcement points in place. A locked question bank therefore could
    not be unlocked through the interface, and the error message staff saw
    ("Unlock it from Administration → Controls") pointed at a page with no such
    control. The panel is restored — the CSS classes it uses (`.resource-row`,
    `.lock-note`, `.inline-form`) were still present but unused, which suggests
    the removal was accidental. A Super Admin now gets lock and unlock controls;
    other administrators see which banks are locked and why, read-only, so the
    error message makes sense to them. Covered by
    `tests/verification/write_paths_bank_locks.py`.

## A note on `init_db()`

`init_db()` pushes its own Flask application context when there is none, so it
is callable from `python app.py`, from a shell, and from inside a request. It
originally required the caller to supply one, which crashed the plain
`python app.py` entry point with `RuntimeError: Working outside of application
context` — the one path none of the tests exercised, because they all import
the module and push a context themselves. `tests/verification/smoke_entrypoint.py`
now starts the real process and covers it.

## Legacy scripts

`tests/legacy/verify_phase6b.py`, `verify_phase6c.py` and friends post to
`/login` with a bare `candidate` field. That form of login was replaced by the
unified username/password login several phases ago — before this merge and
before the SQLAlchemy work — so these scripts have been failing for a long
time. The same applies to `security/test_candidate_result_access.py`, which
additionally does not put the repository root on `sys.path`, so its documented
invocation (`python security/test_candidate_result_access.py`) cannot import
`app`.

They are kept because they document what each phase once guaranteed (see
`tests/README.md`). Treat `tests/current/` and `tests/verification/` as the
live suites. A later cleanup pass moved duplicate copies of these scripts out
of the repository root — `tests/legacy/` was always their canonical home — and
retired `verify_phase3h.py` there too, fixing its path assumptions in the move.

`verify_admin_security.py` was rewritten for SQLAlchemy and works; it lives in
`tests/verification/` rather than `tests/legacy/`, since unlike its
pre-SQLAlchemy sibling of the same name in `tests/legacy/`, it is current,
functioning tooling. Point it at a copy rather than the live database:

    $env:CRAINBOW_DB = "C:\\path\\to\\copy.db"
    python tests/verification/verify_admin_security.py

## Still outstanding

* **There is no version control.** `git init` plus one commit would give a real
  diff and revert path. The pre-merge backup copies
  (`BACKUP_BEFORE_PHASE13_MERGE_20260916_141232/`, `app_before_sqlalchemy_migration.py`,
  `app_before_admin_security_phase_a.py`, `cbt_before_sqlalchemy_migration.db`)
  that served as the only safety net during this merge have since been deleted,
  once the migration was verified stable — there is currently no fallback other
  than redoing the conversion from the Phase 13 source tree.
* **`services/admissions.py` (671 lines) and `services/school_settings.py` are
  dead code** — imported nowhere. They still contain raw SQL, so anyone wiring
  them up should convert them first.
* The `admin_lock_resource` route also accepts `resource_type='examination'`,
  but nothing checks an examination lock any more — `toggle_exam` was
  deprecated in favour of the entrance configuration layer. Only bank locks are
  enforced, so only banks are offered in the UI. The dead branch is harmless
  but could be removed.
* The `attachment_path` / `file_type` columns exist on every `admin*` table but
  only `admin_messages` uses them. They are modelled via the
  `_LegacyAttachmentColumns` mixin so a fresh database matches an upgraded one.
