# Verification harness

These scripts were written to verify the Phase 13 merge. They are kept because
they answer questions the contract tests in `tests/current/` cannot: they drive
the running application rather than reading its source.

They are not unit tests. Each one is run by hand and prints a report.

## Route characterisation

Crawls every GET route as an anonymous visitor, an administrator, a student, a
candidate and a parent, and records the status, redirect target and a hash of
the normalised body. CSRF tokens and timestamps are normalised out so two runs
are comparable.

    python characterize_routes.py ref    # a reference tree, path set inside
    python characterize_routes.py new    # this tree
    python diff_routes.py

Use it before and after any wide-reaching change: convert the whole app, upgrade
Flask, refactor the templates. Capture `ref` from the unchanged code first.

## RBAC equivalence

Dumps every answer the permission system gives, for every administrator, across
permission codes, scope probes, class access, subject access and class+subject
pairs. Run it against two trees and diff the JSON.

    python probe_rbac.py ref
    python probe_rbac.py new

A privilege change is the quietest and worst thing a refactor can do, so this
is exhaustive rather than sampled.

## Write paths

Route characterisation only covers GETs. These drive the mutating routes and
assert on the resulting database rows, including the negative cases (a duplicate
fee assessment must be refused, a promotion must not commit before approval).

    python write_paths.py                            # finance, library, parents, results, messaging, assessments
    python write_paths_admissions.py                 # student registration and the promotion engine, fixed field names
    python write_paths_bank_locks.py                 # locking and unlocking a question bank
    python write_paths_student_registration_ui.py    # student registration through the ACTUAL rendered form
    python write_paths_receipt_email.py               # emailing a fee receipt through the real admin route
    python write_paths_receipt_settings.py             # authorised signature: draw, upload, remove, and its effect on the PDF
    python write_paths_presence_page.py                # /admin/presence shows real live data, not the old hardcoded placeholder
    python write_paths_assignment_resume.py            # a student can actually resume an in-progress CBT assignment
    python write_paths_school_work_notify.py           # guardians are emailed/WhatsApp'd when an assignment or project is assigned
    python write_paths_parent_feedback_chat.py          # /parent/feedback renders as a chat thread; a reply notifies the parent
    python write_paths_academic_sessions.py             # create/switch/archive academic sessions; Super-Admin-only
    python write_paths_term_grading.py                  # Exam(60)+CA(40) term grading across tests/exams/assignments/projects
    python write_paths_student_ui_redesign.py            # class register and profile sections open in modals, not stacked/scrolled
    python write_paths_academic_list_modals.py           # Assignments/Projects/Tests/Practice Tests/Examinations lists open in modals too
    python write_paths_finance_fee_items.py               # fee category/billing-rule catalogue, and fee items now link to paid/unpaid status
    python write_paths_parent_finance.py                   # parent portal shows fees/payments and is alerted on assessment/payment
    python write_paths_finance_followups.py                # paid fees can't be re-allocated/re-picked; no session's balance is ever hidden
    python write_paths_error_pages.py                       # branded 404/403/500 pages replace Werkzeug's defaults; 500 never leaks details

Each seeds a throwaway copy of the database, so they never touch `cbt.db`.

`write_paths_bank_locks.py` exists because the lock panel was once removed from
the Controls page while the routes and their enforcement stayed, stranding any
locked bank. It walks the whole cycle: the panel renders, a lock is recorded
with its reason and author, the lock actually blocks edits, unlocking clears it
and resolves the governance item, a request without a CSRF token is refused,
and a non-Super-Admin can see the state but cannot change it.

`write_paths_student_registration_ui.py` exists because
`write_paths_admissions.py` posts a hand-picked dict of field names, which
stays green even if the template drifts to different names than the route
reads — exactly what happened when `templates/school_student_form.html` was
at one point replaced with a version using `surname` instead of `last_name`
and no `class_id` field at all, silently breaking registration while the
backend-only test kept passing. This one instead scrapes the live HTML for
every real `<input>`/`<select>`/`<textarea>` name and posts using only those,
so a template/route mismatch is caught immediately. It also checks that the
type-sensitive attributes (patterns, `type=email`/`date`/`tel`, constrained
`<select>` fields) are actually present in the rendered output, that a
`state_of_origin` value the `<select>` never offered is rejected server-side
(not just hidden from the UI), and that the post-registration page reads
"Registration Confirmed".

`write_paths_receipt_email.py` exists because emailing a fee receipt to a
parent/guardian timed out in production: the code always did a plaintext
`SMTP()` handshake then `.starttls()`, but the configured mail server uses
port 465, which is implicit-TLS-only (SMTPS) and never answers a plaintext
EHLO — the connection just hangs until the socket times out. The fix
auto-detects port 465 and uses `SMTP_SSL` from the first byte instead (with a
`BRIGHTSTARS_SMTP_SSL` override for servers that don't follow the convention).
This test drives the real `/admin/finance/receipts/<id>/email` route — PDF
generation, attachment, delivery-log write, and the actual SMTP send — against
whatever server `.env` configures, addressed only to the school's own
no-reply address, so it proves the fix through the full stack without ever
emailing a real parent/guardian. It requires network access and real SMTP
credentials to mean anything; if `BRIGHTSTARS_SMTP_HOST` isn't set, the
configuration check at the top fails loudly rather than silently no-op'ing.

`write_paths_receipt_settings.py` exists because the printed/PDF/emailed
receipt gained a real "Authorised Signature" image (previously always a blank
line): an admin can either draw one on a canvas or upload an image, and the
result is stored once per school and reused on every receipt from then on.
This test drives the actual settings page — drawing (posted as a canvas
`data:image/png;base64,...` URL), uploading a file, and removing — and checks
that each action leaves exactly one signature file on disk (replacing, not
stacking, the previous one, and deleting it on removal), that the PDF
generator embeds whichever signature is currently configured, and that it
still renders cleanly with none configured at all.

`write_paths_presence_page.py` exists because `templates/admin_presence.html`
was entirely hardcoded placeholder markup — it always showed "1 admin, 0
students, 0 parents" and one fake "Super Admin" row, ignoring the real
`counts`/`rows` the route passed in, and used Bootstrap grid/table classes
this app never loads. This drives a real login and heartbeat and checks the
page reflects the real signed-in admin and a real WAT-converted timestamp.

`write_paths_assignment_resume.py` exists because a student who started a CBT
assignment but didn't finish it had no way to continue: the POST handler only
branched on "never started" or "already submitted," so an in-progress attempt
fell through both checks and the page silently reloaded itself. This drives a
real start, answers one question, drops the Flask session entirely (as if the
browser were closed), then clicks Continue again and checks it resumes at the
next unanswered question rather than restarting or doing nothing.

`write_paths_school_work_notify.py` exists because assigning a CBT assignment
or project to students never told their guardians outside the in-app
notification. This drives real assignment/project creation through the admin
routes with a student whose guardian_email is the school's own no-reply
address (never a real parent), and checks a real email is delivered via the
configured SMTP server, that WhatsApp being unconfigured degrades gracefully,
and that a student with no guardian contact at all never blocks creation for
the rest of the class.

`write_paths_parent_feedback_chat.py` exists because `/parent/feedback` was
redesigned to look like a chat thread (parent messages as outgoing bubbles,
admin replies as incoming bubbles) and an admin reply now also emails and
WhatsApps the parent, not just an in-app notification. The live SMTP/WhatsApp
round trip itself is already proven by `write_paths_receipt_email.py` and
`write_paths_school_work_notify.py`, so this test mocks the two notifier
functions to verify the *wiring* — a reply calls them with the parent's real
contact details — and separately proves a reply still succeeds even when both
channels raise, which is the actual "never blocks" contract. It also covers
the follow-up: `parent_feedback_replies.admin_id` used to be NOT NULL, so
only staff could ever post into a thread — it's now nullable (NULL = written
by the parent who owns the thread), and this test drives a real parent reply
into an in-progress thread, checks it renders as an outgoing bubble, and
confirms a resolved thread refuses a reply (no reply box, no row written)
while an open/in-progress one accepts it.

`write_paths_academic_sessions.py` exists because there was previously no
admin UI at all to create a new academic session or change which one is
current — `is_current` was only ever set once, by the fresh-install seed row.
This drives the new `/admin/school/sessions` page end to end: a non-Super-
Admin is refused outright, creating a session with "make current" switches it
atomically (asserts only one session is ever current at a time), a session
can't be archived while it's current, an archived session can't be made
current until reactivated, and `_school_current_session()` — the function
every dashboard, receipt and assessment relies on — actually picks up the
change.

`write_paths_term_grading.py` exists because grading was rebuilt around the
Nigerian three-term structure: every test, examination, assignment and
project now belongs to a specific term (First/Second/Third) within an
academic session, and a student's term result for a subject is
Exam (60) + Continuous Assessment (40) = 100, where CA is tests, assignments
and projects sharing CA_MAX_SCORE under an admin-configurable weight split
(default 20/10/10, adjustable on the Academic Sessions page, Super-Admin-only,
must always sum to exactly 40). This drives real assessment/assignment/project
creation through the actual admin forms (proving the new Term/Session fields
are wired up and enforced — a test or exam without a term is refused; a
practice test's term stays optional since practice never counts toward the
record), grades them, and checks `_term_subject_report()`'s math exactly:
each category's raw score is summed then scaled to its own cap — never just
added on top of the others — a practice-test result never contributes even
if one exists, changing the CA weighting immediately changes future
calculations, and the student profile's new "Term Results" table shows the
right numbers.

`write_paths_student_ui_redesign.py` exists because the Students list and
student profile pages were redesigned around native `<dialog>` modals: the
class register used to render as a long inline table below a full grid of
class cards, requiring a scroll to see it; it now opens in a modal that calls
`showModal()` on page load, so it's visible the instant a class is selected.
Likewise the profile page's five academic/history sections (Term Results,
Academic Record, Assignments, Projects, Enrolment History) used to be
always-rendered stacked `<section>` blocks; they're now compact tiles in a
grid, each opening its content in its own modal. This checks the actual
markup — that the register is a `<dialog>` with an auto-open script, that
all five profile sections are genuinely `<dialog>` elements rather than
visible sections, and that the `#enrolment-history` redirect target (used
after correcting a history entry) still resolves to something that opens the
right dialog now that the anchor no longer points at a visible section.

`write_paths_academic_list_modals.py` exists because Assignments, Projects,
Tests, Practice Tests and Examinations all shared the exact same "choose a
class, scroll past the whole class-card grid to reach the list" problem the
Students page had — and the assessment list template is shared by all three
of tests/practice-tests/examinations, so one template fix covers all three at
once. This applies the identical `<dialog>` treatment already verified for
Students: the list opens in a modal that auto-shows via `showModal()` the
moment a class is selected. It also catches a real pre-existing bug this fix
surfaced: the Projects page's search form had no hidden `class` field, so
searching within a class silently dropped the class filter — now fixed to
match the other two pages.

`write_paths_finance_fee_items.py` exists because the Fee Structure page had
drifted into three separate problems. The Add/Edit Fee Item form's Category
was free text, so the same charge could end up filed under "Uniform",
"uniforms" and "UNIFORM" depending on who typed it; it is now a fixed
dropdown, validated server-side so a replayed or scripted POST can't slip an
unlisted category into the catalogue. Applicability/Billing Rule used to
bundle terms into canned combo strings ("First Term only", "Second and third
term only"); each term now stands on its own alongside Full Session and
One-time, and an older row carrying a pre-existing value outside the new
lists still renders (flagged "(legacy)") and keeps that value until an admin
actively changes it, rather than being silently rewritten on the next save.
Third, and the actual complaint: a fee item assessed to a student was an
island — nothing on the Fee Structure page or the Finance dashboard showed
whether it had been paid, and recording a payment never touched the
assessment it was meant to settle. The paid/allocate machinery already
existed (`_finance_student_outstanding`, `/admin/finance/students/<id>/account`,
`/admin/finance/payments/<id>/allocate`) but nothing in the UI linked to it —
it was reachable only by typing the URL by hand, and both routes fell back to
bare `admin.access` instead of a real finance permission. This test drives
the real forms and checks: the dropdown/pill options match exactly and the
old combo values are gone, an invalid category is rejected without writing a
row, a legacy row's out-of-catalogue value survives an edit page load, the
Fee Structure page's Recent Assessments tab shows a real Paid/Part Paid/Unpaid
status per row (computed from actual payment allocations) with a link to that
student's account, the receipt page links to Allocate and to the account, the
dashboard's transaction ledger links to the account too, and the two
previously-orphaned routes now require `finance.view_own`/`finance.record`
respectively — a cashier with those permissions can reach them (for a payment
they recorded), while an admin with no finance permission at all is refused.

`write_paths_parent_finance.py` exists because the parent portal had no
finance visibility whatsoever — `/parent/dashboard` only ever showed academic
activity, so a parent's only way to find out what they owed or had paid was
to call the school office. It adds a fee summary to the dashboard, a full
per-child fee account page (`/parent/children/<id>/finance`) with a
Paid/Part Paid/Unpaid breakdown and downloadable receipts, and an automatic
email + WhatsApp + in-app alert whenever finance/a Super Admin assesses a new
fee or records a payment. It also fixes a real, pre-existing gap this
surfaced: `static/app.css` (loaded by every parent- and student-facing page)
never defined `.btn`, `.btn-primary` or `.btn-light` — only `admin.css`
did — so every "button" across the whole parent/student portal had always
rendered as a bare underlined link with no button styling at all; those
classes now exist in `app.css` too. On the numbers themselves: this test
originally shipped with the top-level balance (assessed/paid/outstanding) as
a raw sum of posted payments rather than the allocation-based figure the
itemised table (and the admin side) uses — the idea being a parent's balance
should move the instant they pay, without waiting on a staff member's
internal bookkeeping. `write_paths_finance_followups.py` found that this was
wrong in a worse way than the problem it solved (an overpayment or
miscategorised payment on one fee could silently erase real, unpaid debt on
a completely different fee), so the top-level balance is now allocation-based
too, exactly matching the itemised table; see that test's notes below for the
full story. This test's own assertions were updated to match: a payment
recorded but not yet allocated increases "unallocated", not "paid". This test
drives real fee assessment and
payment creation through the actual admin routes (proving the notification
hooks fire for real, not just when called directly), checks the dashboard and
fee-account numbers as exact deltas against whatever the shared fixture
database already carried (never hardcoded absolutes, since `cbt.db` is
reused across this whole suite), confirms a parent can download their own
child's receipt PDF but never another family's fee account or receipt no
matter how the URL is guessed, and confirms both notification channels
raising an exception still lets the payment save — the same "never blocks"
contract every other guardian-notification path in this app already has.

`write_paths_finance_followups.py` exists because using the fee/payment
features surfaced five more problems. First, the allocate screen listed every
assessment for the payment's session including ones already fully paid —
harmless in that the amount field capped at zero, but confusing, and nothing
stopped a hand-crafted POST from trying anyway; it now excludes anything with
no outstanding balance, and the route itself refuses a direct attempt with a
clear "already been fully paid" error rather than the generic "exceeds
balance" message. Second, the Student Billing fee-item picker had no idea
which fee items a student already had a live assessment for, so staff could
select one that would only be rejected after they submitted; a new
`/admin/finance/students/<id>/assessed-items.json` endpoint now backs the
picker, greying out (and labelling "Already paid" vs "Already charged...
(unpaid)") any fee item already assessed for the selected term the moment the
student, session or term changes. Third, the Recent Assessments tab was one
flat table that didn't scale — it's now a searchable list of students, each
opening a modal grouped Session -> Term, matching how staff actually think
about "find this student's charges." Fourth and most serious: both the parent
dashboard and the fee-account page computed outstanding balance scoped to a
single academic session, so a balance left over from a previous session (or
an under-paid item nobody had gotten back to) could show as zero simply
because the school had moved on to a new session — parents were being told
they owed nothing when they didn't. The balance shown is now always the
lifetime, all-sessions figure, with older sessions that still carry a balance
called out explicitly so they're never silently dropped. Fifth, a child's
notifications on their parent-facing profile page had no way to tell a brand
new alert from one already seen; they're now labelled New the first time
they're viewed and Old on every view after that. This test drives all five
through the real routes: seeding a student with an unresolved balance in a
past, non-current session plus current-session activity, and checking the
lifetime figure includes it, the dashboard and fee-account page both surface
it, the allocate screen hides the paid item while still listing a genuinely
unpaid one, a direct POST to the paid assessment is refused with no row
written, the assessed-items endpoint reports paid/unpaid correctly per term,
the history modal groups by session then term, and a notification flips from
New to Old between two consecutive page loads.

A sixth problem was found immediately after shipping the fourth one above: the
"lifetime, all-sessions" balance was computed as a raw sum — every posted
payment minus every assessment, with no idea which payment was for which
fee. A parent whose payments happened to add up to more than they'd been
charged overall would see "Outstanding: ₦0" even while one specific fee (say,
Transport, part-paid ₦20,000 of ₦30,000) still had a real ₦10,000 owed on
it — an unrelated overpayment on a different fee silently cancelled out a
genuine debt. The fix makes the headline balance allocation-based too, summed
from the exact same itemised breakdown the table below it shows, so the two
numbers can never disagree and the balance can never read lower than any
single fee's real remaining debt. A payment recorded but not yet allocated by
staff no longer touches "paid" or "outstanding" at all; it's reported
separately as an "unallocated" credit — visible so it's never mistaken for
having vanished, but never allowed to mask somebody else's unpaid fee. This
test's own fixture reflects that: a past-session fee genuinely part-paid via
a real allocation, plus one fully-unallocated current-session payment, with
checks that outstanding reflects only the allocated shortfall and the
unallocated portion is reported on the side.

`write_paths_error_pages.py` exists because the app had no custom error
pages at all — a 404, a CSRF-check 403, or any unhandled exception all fell
through to Werkzeug's plain default page (literally "Not Found" / "Forbidden"
/ "Internal Server Error" on a blank white background, with no branding and
no way back into the site). Two generic templates now cover every case:
`error_4xx.html` for anything under 500 (404, 403, 401, 405, 429, ...), and
`error_5xx.html` for 500 and above, wired up via `app.errorhandler
(HTTPException)` for every `abort()` in the app and `app.errorhandler
(Exception)` as the last-resort catch for anything that isn't a deliberate
abort. The 500 handler is deliberately paranoid about not leaking anything:
the actual exception is logged server-side only, and the page shown to the
visitor never contains the exception message, its type, or a traceback. A
route that already renders its own dedicated error page as a normal response
rather than raising — `admin_access_error` -> `admin_forbidden.html`, used
for permission-denied — is untouched by this, since the new handlers only
ever fire on a raised exception. This test drives a real 404, a real
CSRF-triggered 403 (checking the specific reason still comes through, not a
generic message), a real 405 from hitting a GET-only route with POST, and a
throwaway route that deliberately raises to exercise the 500 path end to end
through a full Flask dispatch — then separately confirms a genuine
permission-denied response still gets its own pre-existing page, not the new
generic one.

Both pages also adapt to whether the visitor is signed in. The first version
sent everyone to the public homepage, which is wrong for a signed-in
admin/parent/student/candidate — that's a stranger's front door to them, not
somewhere they were trying to go. The primary action is now "Go Back":
client-side JS prefers `document.referrer` (the actual page they were just
on) when it's same-origin, falls back to browser history, and only as a last
resort — no history at all, e.g. a bookmarked broken link — falls back to
that visitor's own dashboard (`admin_workspace_home`, `parent_dashboard`,
`student_dashboard`, or `candidate_dashboard`), determined server-side by the
same `_presence_identity()` check the rest of the app uses, and never the
public homepage. An anonymous visitor keeps the plain "Back to Homepage"
link, since they have no dashboard to return to; the "Sign In" link on 401/403
also only shows for them; a signed-in visitor is obviously already signed in.
This test logs in as a real admin and checks both a 404 and a 500 show "Go
Back" instead of "Back to Homepage", never show "Sign In", and that the
fallback URL baked into the page points at the admin's own workspace.

## Admin security foundation

    python verify_admin_security.py

Reports the row counts of every admin/RBAC table (admins, admin_types, permissions,
admin_type_permissions, admin_permissions, admin_scopes, audit_logs) and confirms the Super
Admin holds every permission and bypasses scope limits. A one-shot sanity check to run after
touching the permission catalogue or role seeding, not an exhaustive equivalence check —
that's what RBAC equivalence above is for.

## Entry point

    python smoke_entrypoint.py

Starts `python app.py` as a real process against a throwaway database and
requests pages over HTTP. Every other test here imports the module and pushes
its own application context, so none of them touch the entry point the app is
actually started with. That gap let `RuntimeError: Working outside of
application context` ship: `init_db()` was called bare from `__main__`, and
Flask-SQLAlchemy needs a context. Run this before handing over a release.

## Configuration

    python test_env_config.py

Checks that `.env` is actually loaded, that a variable already set in the real
environment still wins over it (every other script here depends on that:
they all set `CRAINBOW_DB` before importing `app`), and the Super-Admin
bootstrap contract specifically: `CRAINBOW_SUPERADMIN_USERNAME` and
`CRAINBOW_ADMIN_PASSWORD` seed the very first Super Admin on a database that
doesn't have one yet, and are ignored — not "create a second admin", not
"rename the existing one", not "reset its password" — on every startup after
that, however they're subsequently changed. Uses two throwaway databases; the
live `cbt.db` is never touched.

## Audits

    python audit_csrf_forms.py       # POST forms with no _csrf_token field
    python audit_csrf_enforced.py    # ...and whether their route actually enforces it
    python audit_lost_definitions.py # top-level names present in one tree but not the other

The CSRF audits exist because six features were shipped unusable: the form
omitted the token, so the route rejected every submission with 403 and nothing
in the test suite noticed. `audit_lost_definitions.py` catches the opposite
failure, where a bulk edit silently drops a module-level constant.

## Paths

Each script has the tree paths at the top. Edit them when comparing against a
different reference.
