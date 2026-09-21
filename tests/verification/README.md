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
| `write_paths_finance.py` | fee items, paid / part paid / unpaid, payments and allocations, what a parent sees and is told |
| `write_paths_receipts.py` | the receipt PDF, its email and WhatsApp, the authorised signature, one school never wearing another's |
| `write_paths_known_gaps.py` | roles, the profile page, uploads, search, sign-in errors |
| `write_paths_numbering.py` | each school's own candidate and student numbering |
| `write_paths_question_banks.py` | the standard banks, importing banks, a new school's first exam |
| `write_paths_fractional_marks.py` | a 40-question paper is 100 marks; older schools upgraded |
| `write_paths_retired_tables.py` | clearing the removed website editor's leftover tables |
| `write_paths_term_grading.py` | the Exam 60 + CA 40 term result: weights, scaling, rounding, released-only visibility, who may do what |
| `write_paths_bank_locks.py` | locking a question bank and everything a lock must block, per school |
| `write_paths_candidate_results.py` | a candidate's result image and share page: the school's own logo and photo, nothing of another school, nothing left behind |
| `write_paths_error_pages.py` | the 404, 403 and 500 pages: each school's own, "Go Back" for signed-in people, nothing leaked |
| `write_paths_rate_limits.py` | limits shared by every worker process |
| `write_paths_pg_smoke.py` | every page, opened with data behind it |
| `write_paths_pg_posts.py` | every form submission, valid and hostile |

Helpers: `_pg.py` (creates and drops the throwaway databases; several scripts may run at once
without deleting each other's) and `pg_posts_coverage.py` (the list of write routes the form
suite submits, checked by the contract suite so a new route cannot ship untested).
