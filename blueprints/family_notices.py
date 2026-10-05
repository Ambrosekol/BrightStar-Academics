"""What's-new notices for students and parents, shown on their own dashboards.

The notes come from core/release_notes.py, and a student only ever receives 'student' changes while a parent
only ever receives 'family' changes. A notice is self-contained: it carries all of its information and never
links to the staff guide, which students and parents cannot open.

Nothing here reads or writes the database. The browser keeps the version each person last dismissed, so a
dashboard visit costs the server nothing beyond the page itself.
"""

from flask import request, session

from app import app
from core.release_notes import APP_VERSION, releases_for

DASHBOARDS = ('student_dashboard', 'parent_dashboard')


def _family_account():
    """The signed-in student or parent as (id, audience), or (None, None) when nobody family is signed in.

    The audience decides the notes: a student gets 'student' changes and a parent gets 'family' changes, so
    a change written for one never appears on the other's dashboard.
    """
    if session.get('student_id'):
        return session.get('student_id'), 'student'
    if session.get('parent_id'):
        return session.get('parent_id'), 'family'
    return None, None


@app.context_processor
def family_notice_context():
    if request.endpoint not in DASHBOARDS:
        return {}
    account_id, audience = _family_account()
    if not account_id:
        return {}
    return {'family_notice': {
        'account': account_id,
        'audience': audience,
        'app_version': APP_VERSION,
        'releases': releases_for(audience),
    }}
