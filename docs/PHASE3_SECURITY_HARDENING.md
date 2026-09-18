# Crainbow Phase 3 — Security Hardening

Status: **Implemented in the development working copy; not yet production-certified.**

## Implemented now

1. **Production secret handling**
   - Production refuses to start without `CRAINBOW_SECRET` of at least 32 characters.
   - Development generates a per-process secret when none is supplied.
2. **Default credentials**
   - Removed the known `admin123` fallback.
   - Fresh databases receive a generated bootstrap password when no explicit `CRAINBOW_ADMIN_PASSWORD` is supplied; it is emitted once to stderr for controlled local bootstrap.
   - Existing administrator records are not overwritten.
3. **Student answer-key exposure**
   - Student assessment payloads strip `correct_option`.
   - Entrance examination pages are rendered from frozen attempt snapshots that are sanitized before reaching the browser.
4. **Server-enforced assessment timing**
   - Student assessments now have persistent attempts with `started_at` and `expires_at`.
   - Opening an assessment does not start the timer.
   - Starting explicitly creates the attempt and starts the timer.
   - Answer save and submission enforce the server deadline.
5. **Examination attempt integrity**
   - New entrance attempts receive a frozen question/option/answer/points snapshot.
   - Grading uses the snapshot, not a mutable current JSON bank.
6. **SQLite foreign-key enforcement**
   - Every application DB connection enables `PRAGMA foreign_keys=ON`.
   - A busy timeout is also configured.
7. **Migration architecture**
   - A versioned `schema_migrations` table and migration runner are established.
   - New schema changes belong in `migrations/runner.py`.
   - PostgreSQL/Alembic remains the production database migration target for the database-modernization phase.
8. **CSRF consistency**
   - Candidate start/answer/submit endpoints are CSRF protected.
   - AJAX-compatible CSRF header support is available.
9. **Session security**
   - HTTPOnly and SameSite=Lax are enabled.
   - Secure cookies are automatically enabled in production.
10. **Rate limiting**
   - Login attempts are rate-limited per IP + identifier in the current single-process build.
   - Production deployment must additionally enforce shared rate limiting at the reverse-proxy/shared-store layer.
11. **Security headers**
   - CSP, X-Content-Type-Options, X-Frame-Options, Referrer-Policy and Permissions-Policy are applied.
   - HSTS is enabled in production.
12. **Upload security**
   - Global request size limit.
   - Image upload size limit.
   - Extension plus file-signature validation.
   - Randomized filenames and safe prefixes.

## Student Test/Examination interaction

Student Tests and Examinations now use the same fundamental interaction contract as the entrance examination:

**Start → timer begins → one question → select option → Save & Next → review → Submit**

The timer is authoritative on the server.

## Important remaining production work

This phase does **not** mean production certification. Before deployment we still need:

- PostgreSQL migration and verification
- shared/distributed rate limiting
- full security penetration testing
- production secret management
- formal backup/restore testing
- concurrency/load testing
- full examination integrity testing
- modular Flask refactor
- staging/production deployment
- final security review
