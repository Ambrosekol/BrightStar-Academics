# Phase 6K3 — Student Route Regression Fix

## Fixed
The `/admin/school/students` 500 error was caused by a template-context name collision.

Flask's global `session` object is used by `admin_base.html` for authentication/workspace state:
`session.get('admin_workspace')`.

The school student routes were passing an SQLite `sqlite3.Row` as a template variable named `session`. That shadowed Flask's session object, so Jinja attempted `.get()` on the SQLite row and raised:

`jinja2.exceptions.UndefinedError: 'sqlite3.Row object' has no attribute 'get'`

## Permanent boundary fix
- Flask authentication/session state remains named `session`.
- School academic-session data is now passed as `school_session`.
- Student rows sent to the student-register template are normalized with `dict(row)` at the template boundary.
- All student registration/edit error paths were updated so they cannot reintroduce the same `session` collision.
- Existing database schema, authentication, entrance examination, question banks, scoring, rankings, results and Phase 6K2 architecture are not replaced.

## Regression check
Static verification confirms there are no remaining `render_template(..., session=...)` calls in `app.py`, and `school_students.html` reads `school_session`.

## Runtime note
The provided development container does not have Flask installed, so a live Flask test-client smoke test could not be executed here. The source was syntax-compiled successfully and the exact collision was traced to `admin_base.html` line 10 plus the school-student route context.
