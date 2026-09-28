# Verification suites

End-to-end scripts that drive the real application over HTTP against **PostgreSQL**. Each one
builds its own throwaway databases (named `bs_test_*`), drops them when it finishes, and never
touches a real school. Run them by hand; each prints its own report and exits non-zero on a failure.
The project README's *Testing* section says what each covers.

| Script | Covers |
|---|---|
| `write_paths_multitenancy.py` | isolation between schools: hostnames, databases, cookies, files |
| `write_paths_platform_console.py` | the platform console, end to end |
| `write_paths_platform_team.py` | the team, roles, removal and activity logs |
| `write_paths_portal_branding.py` | colours, logo and sign-in photographs |
| `write_paths_delivery.py` | a school's own email and WhatsApp |
| `write_paths_finance.py` | fee items, paid / part paid / unpaid, payments and allocations, what a parent sees and is told, the site-wide banner and list for a payment nobody has allocated yet |
| `write_paths_receipts.py` | the receipt PDF, its email and WhatsApp, the authorised signature, one school never wearing another's |
| `write_paths_known_gaps.py` | roles, the profile page, uploads, search, sign-in errors |
| `write_paths_numbering_rules.py` | each school's numbering patterns: the language, the console editor and preview, audit, candidate registration and student creation, a corrupt file |
| `write_paths_question_banks.py` | the standard banks, importing banks, a new school's first exam |
| `write_paths_fractional_marks.py` | a 40-question paper is 100 marks; older schools upgraded |
| `write_paths_retired_tables.py` | clearing the removed website editor's leftover tables |
| `write_paths_term_grading.py` | the Exam 60 + CA 40 term result: weights, scaling, rounding, released-only visibility, who may do what |
| `write_paths_bank_locks.py` | locking a question bank and everything a lock must block, per school |
| `write_paths_report_cards.py` | report cards: when one is ready, what it shows, comments, traits, signatures, branding, the student and parent portals |
| `write_paths_attendance.py` | taking the daily register, scope, re-marking and clearing, the term summary, permissions and CSRF, the student and parent portals |
| `write_paths_timetable.py` | exam/test timetables: draft entries, releasing (and the notifications it sends), scope, permissions and CSRF, the student and parent portals |
| `write_paths_student_import.py` | bulk CSV student import: valid rows created, bad rows skipped with a reason, class scope, generated numbers and credentials, permissions and CSRF, isolation |
| `write_paths_student_history_import.py` | bulk enrolment-history import: valid rows recorded against an already-existing student, bad rows skipped with a reason, an archived (no longer active) session accepted, permissions and CSRF, isolation |
| `write_paths_guide.py` | the staff guide (/admin/guide): every real page renders, no permission of its own, either workspace, search, isolation |
| `write_paths_admissions.py` | the admissions funnel: the waitlist, admitting (student + optional parent account), declining and undoing it, scope, permissions and CSRF, isolation |
| `write_paths_resilience.py` | durable background jobs (core/jobs.py): a job that succeeds, one that fails and is retried then given up on, one whose thread died mid-flight and is picked back up, an unknown kind refused outright, and the real payment-receipt job's own trail; retry-safe writes (core/idempotency.py): a resubmitted click never records a payment twice, a genuinely different click still does; the exam page's own connection-drop retry logic and the site-wide connectivity banner (static/connectivity.js), both run in a real browser |
| `write_paths_paystack.py` | a school's own Paystack account: encrypted keys, starting a payment, confirming it only by asking Paystack itself (never a callback's query string or a webhook's body alone), a callback and a webhook racing for the same reference never both acting on it, a webhook with a bad or missing signature refused outright, permissions and isolation |
| `report_card_pdf_selfcheck.py` | the report card PDF on its own (layout, pictures, hostile text); needs no database |
| `write_paths_candidate_results.py` | a candidate's result image and share page: the school's own logo and photo, nothing of another school, nothing left behind |
| `write_paths_error_pages.py` | the 404, 403 and 500 pages: each school's own, "Go Back" for signed-in people, nothing leaked |
| `write_paths_rate_limits.py` | limits shared by every worker process |
| `write_paths_session_guard.py` | a restart signs everyone out except people sitting an exam (who carry on through answers, heartbeat and submit), a changed password ends the account's other sign-ins, referrer and no-store headers, the trusted-proxy setting |
| `write_paths_upload_access.py` | who may open each uploads folder: staff, the student and their parent, the candidate, another student, an unrelated parent, signed out, another school |
| `write_paths_upload_limits.py` | every picture upload (staff, student, candidate, question, signature, branding, console): the 5 MB limit printed beside the box, a refusal that names the file and both sizes, a friendly answer to an over-8 MB submission, and the browser script run in Chrome |
| `write_paths_pg_smoke.py` | every page, opened with data behind it |
| `write_paths_pg_posts.py` | every form submission, valid and hostile |
| `load_test_exam_answers.py` | not a correctness suite: writes/second and per-write latency for the real exam-answer upsert, under concurrency, against a throwaway database or (`--school CODE`) a real one - run before a school's first big entrance day |

Helpers: `_pg.py` (creates and drops the throwaway databases; several scripts may run at once
without deleting each other's) and `pg_posts_coverage.py` (the list of write routes the form
suite submits, checked by the contract suite so a new route cannot ship untested).
