# Phase 3E — Governance, Result Release & Administrator UX

## Implemented
- Notifications now record actor name/username plus exact date/time (UTC) and display `By`, `Date`, and `Time`.
- Administrator access boundaries now behave as true narrowing constraints: no explicit boundary means the whole permitted area; a class/subject/bank boundary only narrows that dimension.
- School Portal navigation is capability-filtered so administrators see only tools their assigned roles can use.
- Workspace entry recognises any permission within the workspace, not only the umbrella `school.view`/dashboard permission.
- Restricted entrance administrators see only question banks within their bank boundary.
- Delegated administrators who are allowed to manage staff cannot grant job roles whose permissions exceed their own effective capabilities. Super Admin remains unrestricted.
- Subject creation now handles an empty class catalogue explicitly and no longer presents a misleading form with an impossible selection.
- Scheduled school result release: tests and examinations are recorded as `pending` after submission and become visible to students only when the current academic session release date/time arrives. Practice-test results remain immediate.
- Super Admin / authorised result-release administrator can set the current session release date/time from School Portal → Results & Records.
- Student result page no longer exposes test/examination scores before release; it shows a professional submission-confirmation state instead.

## Regression
Current contract suite: 5 passed, 0 failed.
Python syntax compilation: passed.
SQLite permission-union query smoke test: passed.
