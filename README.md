# Crainbow CBT — Phase 6E

## Results & Analytics + Examination Management

Phase 6E extends Phase 6D with a school-administration results layer.

### Added
- Results & Analytics dashboard with filters and performance summary.
- Candidate result detail with answer-by-answer verification.
- Printable individual result report.
- Candidate rankings with deterministic tie handling (percentage, then score).
- CSV export of completed results.
- CSV export of rankings.
- JSON export of completed results.
- Examination activation/deactivation from the admin dashboard.
- Regrade action using the current server-side answer key.
- Admin navigation for Results & Analytics and Rankings.
- Database migration for examination active state.

### Existing functionality retained
- Candidate examination screen and answer persistence.
- Automatic submission on expiry.
- Server-side automatic marking.
- Admin authentication.
- Question-bank creation/editing.
- Question creation/editing/deletion/reordering.

### Integrity rule
Incomplete source questions are not invented. Only verified question-bank content should be imported into production.

### Run
1. Install dependencies: `pip install -r requirements.txt`
2. Run: `python app.py`
3. Open `/admin` for administration.
4. Default local admin password: `admin123` (change via `CRAINBOW_ADMIN_PASSWORD`).

### Phase 6E routes
- `/admin/results`
- `/admin/results/<attempt_id>`
- `/admin/rankings`
- `/admin/export/results.csv`
- `/admin/export/rankings.csv`
- `/admin/export/results.json`
- `/admin/examinations/<bank_id>/toggle`

## Phase 6F — Security & Testing

This release adds candidate-management/security planning and deployment testing controls around the Phase 6E system. See:

- `security/SECURITY_TEST_PLAN.md`
- `docs/phase-history/PHASE6F_DEPLOYMENT_CHECKLIST.md`

Apply schema changes only after backing up the existing database.



## Phase 6G — Local Server Deployment & Network Connection

Deployment materials are in `deployment/`.

Use `deployment/PHASE6G_DEPLOYMENT_GUIDE.md` as the primary guide.
The Windows batch files are helpers; inspect the server port and project launcher before running them.


## Phase 6G — Candidate Session Layer

Phase 6G adds the candidate/session workflow around the existing CBT engine without replacing its question, timer, answer, grading or retake logic.

- Administrator registers each candidate and assigns exactly three distinct populated question banks.
- The system generates a unique Candidate ID and password.
- Candidate signs in with those credentials and sees only the three assigned papers.
- Each paper starts independently and uses the existing per-paper timer and grading engine.
- After a paper is submitted, the candidate returns to the dashboard and may start another assigned paper when ready.
- A completed paper cannot be taken again unless the administrator grants a one-time retake.
- Candidate pages do not display scores, percentages, grades, answer keys or cumulative results.
- Administrators can review individual paper results and print a cumulative three-paper result.
- Existing legacy attempts remain in the database; new candidate-session attempts are linked to a candidate record.

The database migration is automatic on startup. Existing `attempts` and `retake_grants` tables receive nullable candidate identifiers where needed.

## Phase 6G candidate session
The administrator registers each candidate once and selects the entrance level. The system assigns exactly three papers for that level: Mathematics, English and the shared General Knowledge paper. Candidates receive generated credentials and use a dashboard to take the three papers independently. Scores are withheld from the candidate portal; individual and cumulative results are administrator-only.

See `PHASE6G_CONTROLLED_MODIFICATION.md` for the controlled change boundary.

## Unified School Gateway — Phase 6K extension

The site now has one public entry address for all account types.

- `/` — Creative Rainbow Schools public gateway.
- `/practice` — public no-login practice-test gateway.
- `/login` — unified account login. The submitted identifier determines whether the user is staff, an entrance-examination candidate, or a provisioned student account.
- `/admin` — legacy staff URL that now opens the neutral Workspace Home after authentication.
- `/admin/home` — neutral staff Workspace Home. No workspace sidebar is shown here.
- `/admin/examination` — Entrance Examination workspace dashboard.
- `/admin/school` — School Portal workspace dashboard.

The administrator must explicitly choose a workspace before the workspace sidebar appears. Switching workspaces requires exiting to Workspace Home first. This prevents entrance-examination and school records from being mixed in the same working context.

The Student Portal now has an active student-account layer. New school-student registrations automatically provision a Student ID/username and one-time temporary password; only the password hash is stored. First login requires a password change. Existing students without accounts can be provisioned or reset from their Student Profile. Candidate authentication remains separate and unchanged. See `PHASE6K4_STUDENT_ACCOUNT_LAYER.md`.
