"""Every 4xx and 5xx response used to fall through to Werkzeug's plain
default error page ("Not Found", "Forbidden", "Internal Server Error" —
unbranded, with no navigation back into the school's site). This adds two
generic, branded templates (error_4xx.html, error_5xx.html) wired up via
app.errorhandler(HTTPException) and app.errorhandler(Exception), covering
every abort() and every otherwise-unhandled exception in the app.

The 500 handler in particular must never leak the actual exception message
or a traceback to the visitor — it's logged server-side only. And a route
that already renders its own deliberate 403 page (admin_access_error ->
admin_forbidden.html, returned as a normal response rather than raised) must
keep doing exactly that; the new generic handler only ever fires for a
raised HTTPException/Exception, so it must never intercept that path.

A signed-in visitor (admin/parent/student/candidate) who hits an error must
never be dropped onto the public marketing homepage — that's someone else's
front door, not theirs. Both pages instead offer a "Go Back" action that
prefers the actual previous page (document.referrer), falls back to browser
history, and only falls back to that visitor's own dashboard (never the
public homepage) if there's no history at all. An anonymous visitor keeps
the plain "Back to Homepage" link, since they have no dashboard to return to.
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DB = os.path.join(HERE, "error_pages.db")
shutil.copy(os.path.join(ROOT, "cbt.db"), DB)
sys.path.insert(0, ROOT)

os.environ["CRAINBOW_DB"] = os.path.abspath(DB)
os.chdir(ROOT)
import app as A  # noqa: E402

A.app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.abspath(DB)}"
with A.app.app_context():
    A.init_db()

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


# A throwaway route that always raises, purely to exercise the 500 handler
# for real through a full Flask dispatch, the same way a genuine bug would.
@A.app.route("/__test_boom__")
def _boom():
    raise RuntimeError("deliberate test explosion - must never reach the response body")


with A.app.test_client() as c:
    r404 = c.get("/this-route-definitely-does-not-exist")
    body404 = r404.get_data(as_text=True)
    check("an unknown route returns 404", r404.status_code == 404)
    check("the 404 page is the branded template, not Werkzeug's default",
          "Page Not Found" in body404 and "err-code" in body404)
    check("the 404 page does not show Werkzeug's default wording",
          "was not found on the server" not in body404)
    check("the 404 page links back to the homepage", 'href="/"' in body404 or "Back to Homepage" in body404)
    check("the 404 page carries the school's branding", "school_logo.png" in body404)

    r403 = c.post("/logout", data={})
    body403 = r403.get_data(as_text=True)
    check("a missing CSRF token returns 403", r403.status_code == 403)
    check("the 403 page is the branded template", "Access Denied" in body403)
    check("the 403 page still shows the real, specific reason (not a generic message)",
          "Invalid or missing CSRF token" in body403)

    r500 = c.get("/__test_boom__")
    body500 = r500.get_data(as_text=True)
    check("an unhandled exception returns 500", r500.status_code == 500)
    check("the 500 page is the branded template, not Werkzeug's default",
          "Something Went Wrong" in body500)
    check("the 500 page never leaks the exception message",
          "deliberate test explosion" not in body500)
    check("the 500 page never leaks the exception type or a traceback",
          "RuntimeError" not in body500 and "Traceback" not in body500)
    check("the 500 page still offers a way back to the homepage",
          "Back to Homepage" in body500)

    # A route registered for GET only, hit with a disallowed method, proves
    # the generic handler covers HTTPException subclasses beyond 404/403/500.
    r405 = c.post("/health")
    check("a disallowed HTTP method still renders the branded 4xx page",
          r405.status_code == 405 and "err-code" in r405.get_data(as_text=True))

    # A genuinely permission-denied admin action must keep using its own,
    # pre-existing branded page (admin_forbidden.html) - a normal response,
    # not a raised exception - so the new generic handler must not interfere.
    admin_c = A.app.test_client()
    from werkzeug.security import generate_password_hash
    import sqlite3
    from datetime import datetime, timezone
    con = sqlite3.connect(DB)
    now = datetime.now(timezone.utc).isoformat()
    staff_tid = con.execute("SELECT id FROM admin_types WHERE is_system=0 AND active=1 ORDER BY id LIMIT 1").fetchone()
    if staff_tid:
        staff_tid = staff_tid[0]
        admin_id = con.execute(
            "INSERT INTO admins(username,display_name,password_hash,admin_type_id,active,created_at,"
            "password_must_change) VALUES(?,?,?,?,1,?,0)",
            ("zz_errpage_staff", "Errpage Staff", generate_password_hash("StaffPass!123"), staff_tid, now)).lastrowid
        con.execute("INSERT OR IGNORE INTO admin_role_assignments(admin_id,admin_type_id,assigned_at) VALUES(?,?,?)",
                    (admin_id, staff_tid, now))
        con.commit()
        con.close()
        admin_c.post("/login", data={"username": "zz_errpage_staff", "password": "StaffPass!123"})
        admin_c.get("/admin/workspace/school")

        r_perm = admin_c.get("/admin/school/sessions")
        body_perm = r_perm.get_data(as_text=True)
        check("a genuine permission-denied page keeps its own dedicated template",
              r_perm.status_code == 403 and "not available to you" in body_perm)
        check("the permission-denied page is NOT the new generic 4xx template",
              "err-code" not in body_perm)

        # A signed-in admin hitting a generic error must see "Go Back", not
        # "Back to Homepage" - they have a dashboard, not a public front door.
        r404_admin = admin_c.get("/this-route-definitely-does-not-exist")
        body404_admin = r404_admin.get_data(as_text=True)
        check("a signed-in admin sees 'Go Back' instead of 'Back to Homepage' on a 404",
              "Go Back" in body404_admin and "Back to Homepage" not in body404_admin)
        check("a signed-in admin does not see the anonymous 'Sign In' link on a 404",
              "Sign In" not in body404_admin)
        check("the Go Back button's fallback points at the admin's own workspace, not the public homepage",
              "admin_workspace_home" in body404_admin or "/admin/" in body404_admin)

        r500_admin = admin_c.get("/__test_boom__")
        body500_admin = r500_admin.get_data(as_text=True)
        check("a signed-in admin sees 'Go Back' instead of 'Back to Homepage' on a 500",
              "Go Back" in body500_admin and "Back to Homepage" not in body500_admin)
    else:
        con.close()

with A.app.app_context():
    A.db.session.remove()
    A.db.engine.dispose()
try:
    os.remove(DB)
except OSError:
    pass

print()
failed = [x for x in results if not x[1]]
print(f"{len(results)-len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
