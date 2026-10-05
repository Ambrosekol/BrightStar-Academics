"""The running platform version, shown at the bottom of the sign-in page.

It is read from release_notes.json through core/release_notes.py, so the version a person sees is always the
newest release in that file. Nothing here reads the database.
"""

from app import app
from core.release_notes import APP_VERSION, RELEASES


@app.context_processor
def platform_version_context():
    newest = RELEASES[0] if RELEASES else {}
    return {'platform_version': {'version': APP_VERSION, 'released': newest.get('date', '')}}
