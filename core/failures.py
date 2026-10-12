"""A failure, said plainly to the person, with the technical detail kept for the server log.

An exception's own text can carry what a visitor should not see: a database error quotes the SQL and the values it
was given, a network error names internal addresses and ports. Wherever a page has to say that something failed,
it says what kind of failure it was, in words, and the whole exception goes to the log for whoever runs the server.
"""

import logging
import smtplib
import socket
import ssl

log = logging.getLogger(__name__)


def plain_failure(exc, context=''):
    """A short sentence saying what kind of failure ``exc`` was. The exception itself is logged, not shown."""
    log.warning('%s failed: %r', context or 'Something', exc, exc_info=exc)
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return 'the mail server did not accept the username or password'
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return 'the mail server refused the address it was sent to'
    if isinstance(exc, smtplib.SMTPException):
        return 'the mail server refused the message'
    if isinstance(exc, ssl.SSLError):
        return 'a secure connection could not be made (check the security setting and the certificate)'
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return 'the server did not answer in time'
    if isinstance(exc, socket.gaierror):
        return 'the server address could not be found'
    if isinstance(exc, ConnectionRefusedError):
        return 'the server refused the connection (check the address and port)'
    if isinstance(exc, (ConnectionError, OSError)) and not isinstance(exc, ValueError):
        return 'the server could not be reached'
    if isinstance(exc, ValueError):
        return str(exc)[:200]     # our own checks raise ValueError with a sentence meant for the person
    try:
        from sqlalchemy.exc import SQLAlchemyError
        if isinstance(exc, SQLAlchemyError):
            return 'the database refused the change (the details are in the server log)'
    except ImportError:
        pass
    return 'an unexpected error (the details are in the server log)'
