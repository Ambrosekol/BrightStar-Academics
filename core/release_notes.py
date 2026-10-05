"""What changed in each version of the portal, shown to the people each change is for.

The notes themselves live in ``release_notes.json`` at the project root, which has comments explaining how
to write them. This module only reads that file, drops its comments, and picks out what each signed-in
person should see:

* staff (administrators) see staff changes, with a link to the staff guide where one exists;
* parents see family changes, and students see student changes, each with the whole message inside it.

A change never shows to an audience it was not written for. Which notes a person has already dismissed is
kept in their own browser, not on the server, so checking for new notes never touches the database.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

RELEASE_FILE = Path(__file__).resolve().parents[1] / 'release_notes.json'
AUDIENCES = ('staff', 'family', 'student')


def _key(version):
    return tuple(int(part) for part in str(version).split('.') if part.isdigit())


def _without_comments(value):
    """Drop every key that starts with an underscore, at any depth: those are the file's comments."""
    if isinstance(value, dict):
        return {k: _without_comments(v) for k, v in value.items() if not str(k).startswith('_')}
    if isinstance(value, list):
        return [_without_comments(v) for v in value]
    return value


def _load():
    try:
        raw = json.loads(RELEASE_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        # A broken notes file must never stop the portal from starting: it simply announces nothing.
        logger.exception('Could not read %s; no release notes will be shown', RELEASE_FILE)
        return []
    return _without_comments(raw).get('releases', [])


def problems(releases):
    """Everything wrong with a list of releases, as readable sentences. An empty list means it is valid."""
    found = []
    seen = set()
    for release in releases:
        name = release.get('version', '(no version)')
        for field in ('version', 'date', 'title', 'changes'):
            if not release.get(field):
                found.append(f'release {name} has no {field}')
        if name in seen:
            found.append(f'release {name} appears more than once')
        seen.add(name)
        try:
            _key(name)
        except ValueError:
            found.append(f'release {name}: the version must be dotted numbers such as 2.20.26.1')
        for change in release.get('changes', []):
            audience = change.get('audience')
            if audience not in AUDIENCES:
                found.append(f'release {name}: audience "{audience}" must be staff, family or student')
            if not change.get('text'):
                found.append(f'release {name}: a change has no text')
            if audience != 'staff' and change.get('help'):
                found.append(f'release {name}: a {audience} change must not have a help link (families and students cannot open the staff guide)')
            if audience == 'staff' and not change.get('help'):
                found.append(f'release {name}: a staff change should name a staff guide page in "help"')
    return found


RELEASES = _load()
# The app's version is the newest release in the file. Nothing else sets it.
APP_VERSION = max((r['version'] for r in RELEASES), key=_key, default='')


def releases_for(audience):
    """Every release that has something for this audience, newest first, with only those changes.

    This reads nothing from the database. The browser remembers which versions each person has already
    dismissed, so the server only sends the notes and the browser decides what to show. Staff changes keep
    their help link; family and student changes never have one.
    """
    if audience not in AUDIENCES:
        raise ValueError(f'unknown audience {audience!r}')
    ordered = sorted(RELEASES, key=lambda r: _key(r['version']), reverse=True)
    shown = []
    for release in ordered:
        changes = [dict(c) for c in release['changes'] if c.get('audience') == audience]
        if audience != 'staff':
            changes = [{'text': c['text']} for c in changes]
        if changes:
            shown.append({**release, 'changes': changes})
    return shown
