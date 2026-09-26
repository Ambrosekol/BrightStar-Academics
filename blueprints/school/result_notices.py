"""Telling parents, once, that a child's term results are out and the report card is ready.

A card is ready when *every* official result of the student in that term has been released (see
``report_card_data``), so releasing one subject of several says nothing yet, and releasing the last
one says it. Every way a result can be released comes here afterwards with the (student, session,
term) it touched, and only the ones whose card really is ready are announced.

The messages go out on a thread of their own (``core.background``): an administrator releasing a
class's results must not wait on a mail server, and a message that cannot be sent must never undo
the release, which has already been committed by the time this is called.
"""

from flask import url_for
from sqlalchemy import select

from blueprints.school.report_card_data import _is_ready, _official_result_counts, term_slug
from core.background import run_in_background
from core.notifications import _notify_parents_report_card_ready
from models import AcademicSession, db
from core.db_helpers import one_scalar


def announce_ready_report_cards(periods, admin_id=None):
    """Announce every ``(student_id, session_id, term)`` in ``periods`` whose report card is ready.

    Call it after the release has been committed. Returns nothing and never raises.
    """
    periods = sorted({(int(s), int(sess), term or 'Full Session') for s, sess, term in periods})
    if periods:
        run_in_background(_announce, periods, admin_id)


def _announce(periods, admin_id):
    for student_id, session_id, term in periods:
        try:
            counts = _official_result_counts([student_id], session_id, term)
            if not _is_ready(counts.get((student_id, session_id, term))):
                continue
            session_name = one_scalar(select(AcademicSession.name).where(AcademicSession.id == session_id), '')
            view_url = url_for('parent_report_card_view', student_id=student_id, session_id=session_id,
                               slug=term_slug(term), _external=True)
            _notify_parents_report_card_ready(student_id, session_name, term, view_url, admin_id)
        except Exception:
            db.session.rollback()
            from flask import current_app
            current_app.logger.exception('Report card notice failed for student %s (%s)', student_id, term)
