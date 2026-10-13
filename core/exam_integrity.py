"""Exam mode: what happens around a paper while it is being taken, recorded for the school.

The page (static/paper.js) asks for full screen, blocks copying, pasting and the right-click menu, and tells the
server when the student or candidate leaves the page or full screen, or tries to copy or paste. Those reports are
written here, as ExamIntegrityEvent rows, capped per attempt so a page cannot flood the table. A browser can stop
reporting (it is the person's own machine), so nothing here is proof on its own: it is a record for the teacher.

Another device. The browser that starts a paper is given a random tag in a cookie (only its hash is stored on the
attempt). A paper opened without that tag - another computer, another browser - is recorded as "opened on another
device", never refused: after a power cut a student must be able to carry on elsewhere.

Ending a paper. A school test or examination can be set to end once the student has left the page more than a set
number of times (blueprints/student_portal/routes.py enforces it on the server). Off unless a teacher sets it.
"""

import hashlib
import hmac
import secrets
from datetime import datetime, timezone

from flask import request
from sqlalchemy import func, select

from core.db_helpers import tuples
from models import ExamIntegrityEvent, db

SCHOOL = 'school'
ENTRANCE = 'entrance'
KINDS = {
    'left_page': 'Left the page',
    'fullscreen_exit': 'Left full screen',
    'copy': 'Tried to copy',
    'paste': 'Tried to paste',
    'context_menu': 'Right-clicked',
    'new_device': 'Opened on another device',
    'fullscreen_unavailable': 'Full screen not available',
}
# Reported by the page itself (new_device is decided by the server).
PAGE_KINDS = frozenset(KINDS) - {'new_device'}
EVENT_CAP = 300
COOKIE = 'bs_exam_'


def _now():
    return datetime.now(timezone.utc).isoformat()


def new_device_tag():
    """(the raw tag for the cookie, its hash for the attempt)."""
    raw = secrets.token_urlsafe(24)
    return raw, tag_hash(raw)


def tag_hash(raw):
    return hashlib.sha256((raw or '').encode('utf-8')).hexdigest()


def cookie_name(scope, attempt_id):
    return f'{COOKIE}{scope}_{int(attempt_id)}'


def set_device_cookie(response, scope, attempt_id, raw):
    response.set_cookie(cookie_name(scope, attempt_id), raw, max_age=2 * 24 * 3600, httponly=True,
                        samesite='Lax', secure=request.is_secure)
    return response


def same_device(scope, attempt_id, stored_hash):
    """True when this browser holds the attempt's tag (or the attempt has none, as older ones do)."""
    if not stored_hash:
        return True
    raw = request.cookies.get(cookie_name(scope, attempt_id))
    return bool(raw) and hmac.compare_digest(tag_hash(raw), stored_hash)


def record(scope, attempt_id, kind, question_number=None):
    """Write one event. Returns False (and writes nothing) for an unknown kind or once the attempt has reached the
    cap. The caller has already checked the attempt is the person's own and still open."""
    if kind not in KINDS:
        return False
    have = db.session.scalar(select(func.count()).select_from(ExamIntegrityEvent).where(
        ExamIntegrityEvent.scope == scope, ExamIntegrityEvent.attempt_id == attempt_id)) or 0
    if have >= EVENT_CAP:
        return False
    try:
        number = int(question_number) if question_number not in (None, '') else None
    except (TypeError, ValueError):
        number = None
    if number is not None and not 0 < number < 10000:
        number = None
    db.session.add(ExamIntegrityEvent(scope=scope, attempt_id=attempt_id, kind=kind, question_number=number,
                                      created_at=_now(), ip_address=(request.remote_addr or '')[:64]))
    db.session.commit()
    return True


def count_of(scope, attempt_id, kind):
    return db.session.scalar(select(func.count()).select_from(ExamIntegrityEvent).where(
        ExamIntegrityEvent.scope == scope, ExamIntegrityEvent.attempt_id == attempt_id,
        ExamIntegrityEvent.kind == kind)) or 0


def counts(scope, attempt_ids):
    """``{attempt_id: {kind: n}}``."""
    out = {}
    if not attempt_ids:
        return out
    for attempt_id, kind, n in tuples(select(ExamIntegrityEvent.attempt_id, ExamIntegrityEvent.kind, func.count())
                                      .where(ExamIntegrityEvent.scope == scope,
                                             ExamIntegrityEvent.attempt_id.in_(list(attempt_ids)))
                                      .group_by(ExamIntegrityEvent.attempt_id, ExamIntegrityEvent.kind)):
        out.setdefault(attempt_id, {})[kind] = n
    return out


def timeline(scope, attempt_id, limit=200):
    """The attempt's events, oldest first: ``[(created_at, label, question_number)]``."""
    rows = tuples(select(ExamIntegrityEvent.created_at, ExamIntegrityEvent.kind, ExamIntegrityEvent.question_number)
                  .where(ExamIntegrityEvent.scope == scope, ExamIntegrityEvent.attempt_id == attempt_id)
                  .order_by(ExamIntegrityEvent.id).limit(limit))
    return [(at, KINDS.get(kind, kind), number) for at, kind, number in rows]
