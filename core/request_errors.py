"""A value in a request that the database cannot hold is the caller's mistake, not a crash.

PostgreSQL has fixed-size integer columns. A form field that carries 99999999999999999999 (a
hand-edited request, a broken client, someone testing the site) passes every check a route makes,
because Python integers have no limit, and then fails in the INSERT or UPDATE with "integer out of
range". Without this it is an "unhandled exception" and a 500 page, on nearly every route that reads
an id or a count from a form. Text containing a NUL byte is refused by PostgreSQL the same way.

Both are answered here with one ordinary 400 page, and nothing else is touched: any *other* database
error still goes to the application's own error handler, which logs it and shows the 500 page.

Registered once, on import, for the whole application.
"""

import sqlalchemy as sa
from flask import request
from werkzeug.exceptions import BadRequest

from app import app
from models import db

# psycopg's own names for "this number does not fit" and "this text is too long for its column".
_BAD_INPUT = ('NumericValueOutOfRange', 'StringDataRightTruncation')


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
