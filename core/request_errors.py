"""Requests the application cannot take, answered with a plain explanation instead of a crash.

Two kinds are handled here, both the caller's mistake and neither a fault in the application.

A value the database cannot hold. PostgreSQL has fixed-size integer columns. A form field that
carries 99999999999999999999 (a hand-edited request, a broken client, someone testing the site)
passes every check a route makes, because Python integers have no limit, and then fails in the
INSERT or UPDATE with "integer out of range". Without this it is an "unhandled exception" and a 500
page, on nearly every route that reads an id or a count from a form. Text containing a NUL byte is
refused by PostgreSQL the same way. Both are answered with one ordinary 400 page, and nothing else is
touched: any *other* database error still goes to the application's own error handler, which logs it
and shows the 500 page.

A submission that is larger than the whole application allows (HTTP 413, from Flask's
``MAX_CONTENT_LENGTH``). The person is told the limit, and how large what they sent was, in plain
words (the sentence lives in core/uploads.py with every other upload message). If they came from a
form in the staff or platform workspace they are sent back to that form with the explanation shown
there; otherwise they get the ordinary error page carrying it.

It also puts the upload helpers of core/uploads.py in every template (``upload_attrs`` and
``upload_hint``), so a file box can say what it takes and static/upload-limit.js can say so before
anything is sent.

Registered once, on import, for the whole application.
"""

from urllib.parse import urlsplit

import sqlalchemy as sa
from flask import flash, jsonify, redirect, request
from werkzeug.exceptions import BadRequest, HTTPException, RequestEntityTooLarge

from app import app
from core import uploads
from models import db

# psycopg's own names for "this number does not fit" and "this text is too long for its column".
_BAD_INPUT = ('NumericValueOutOfRange', 'StringDataRightTruncation')

# The people who signed in to these parts of the site see a flashed message on the page they came from.
_PAGES_THAT_SHOW_A_MESSAGE = ('/admin/', '/platform/')

app.add_template_global(uploads.upload_attrs, 'upload_attrs')
app.add_template_global(uploads.upload_hint, 'upload_hint')

# How the school is organised (early years, SSS departments, staff levels), for any template to ask.
from core import school_structure  # noqa: E402
app.add_template_global(school_structure.is_early_years, 'is_early_years')
app.add_template_global(school_structure.is_senior, 'is_senior_class')
app.add_template_global(school_structure.DEPARTMENTS, 'SSS_DEPARTMENTS')
app.add_template_global(school_structure.parse_departments, 'parse_departments')
app.add_template_global(school_structure.STAFF_LEVELS, 'STAFF_LEVELS')
app.add_template_global(school_structure.level_or_default, 'staff_level_of')
app.add_template_global(school_structure.TOP_LEVEL_LABEL, 'TOP_STAFF_LEVEL_LABEL')
from core.security import subject_reach  # noqa: E402
app.add_template_global(subject_reach, 'subject_reach')


def _is_bad_input(exc):
    original = getattr(exc, 'orig', None)
    return type(original).__name__ in _BAD_INPUT or 'NUL (0x00)' in str(original)


@app.errorhandler(sa.exc.DataError)
def refuse_a_value_the_database_cannot_hold(exc):
    # Imported here: app.py defines its handlers after it has imported every route module.
    from app import handle_http_exception, handle_unexpected_exception

    if not _is_bad_input(exc):
        return handle_unexpected_exception(exc)
    db.session.rollback()
    # A warning without a traceback: this is a bad request, not a fault in the application.
    app.logger.warning('Refused a value the database cannot hold: %s %s', request.method, request.path)
    return handle_http_exception(BadRequest(
        description='One of the values sent is too large, or contains characters that cannot be stored. '
                    'Please check it and try again.'))


def _form_the_person_was_on():
    """Where to send someone back to, or None when that cannot be done safely.

    Only a page of this same site (the address it came from must be this request's own host), only
    in the parts that show a flashed message, and only one that can be opened by a plain GET, so the
    person never lands on a "method not allowed" page or on another site.
    """
    referer = urlsplit(request.headers.get('Referer', ''))
    if referer.scheme not in ('http', 'https') or referer.netloc != request.host:
        return None
    path = referer.path
    if not path.startswith(_PAGES_THAT_SHOW_A_MESSAGE):
        return None
    try:
        app.url_map.bind_to_environ(request.environ).match(path, method='GET')
    except HTTPException:
        return None
    return path + ('?' + referer.query if referer.query else '')


def _wants_json():
    return request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest'


@app.errorhandler(RequestEntityTooLarge)
def explain_a_request_that_is_too_large(exc):
    # Imported here: app.py defines its handlers after it has imported every route module.
    from app import handle_http_exception

    message = uploads.request_too_large_message(request.content_length, request.max_content_length)
    app.logger.info('Refused a submission over the size limit: %s %s (%s bytes)', request.method,
                    request.path, request.content_length)
    if _wants_json():
        return jsonify(error=message), 413
    back = _form_the_person_was_on()
    if back:
        flash(message, 'error')
        return redirect(back, 303)
    return handle_http_exception(RequestEntityTooLarge(description=message))
