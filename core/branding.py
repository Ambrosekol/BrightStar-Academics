"""The identity a school shows its own people: name, motto, logo and the
prefix on its receipt numbers.

Each school's branding is captured when the school is created and lives in that
school's own database, so no school's portal, email or document ever appears
under another school's name. There is deliberately no default school name here:
this code base grew out of one school, and a leftover default would quietly put
that school's name on everybody else's paperwork.

``school_brand()`` is exposed to every template as ``school_brand`` by a
context processor in app.py. ``school_name()`` is the same name for code that
builds emails and documents, and needs no request context.

Deliberately defensive: these run on every page render, including on the
platform host where there is no school at all, and on a database mid-upgrade.
"""

from flask import has_request_context, url_for
from sqlalchemy import select

from control_plane.context import current_tenant
from core.public_settings import _public_settings

PLATFORM_NAME = 'Brightstars Academics'
# Used only until a school uploads its own logo.
PLACEHOLDER_LOGO = 'images/school_placeholder_logo.png'
RECEIPT_PREFIX_KEY = 'receipt_prefix'


def _setting(key):
    return (_public_settings().get(key) or '').strip()


def _school_row(column):
    """One column of the school's own row, or ''. Guarded: this runs on every
    render, including on a database that is mid-upgrade."""
    from models import School, db

    try:
        return (db.session.scalars(select(column).order_by(School.id)).first() or '').strip()
    except Exception:
        db.session.rollback()
        return ''


def school_name():
    """The current school's own name.

    Falls back to the registry's name for the school, then to the platform's
    name — never to any particular school's name.
    """
    from models import School

    tenant = current_tenant(required=False)
    if tenant is None:
        return PLATFORM_NAME
    return _setting('school_name') or _school_row(School.name) or tenant.name or PLATFORM_NAME


def receipt_prefix():
    """The prefix on this school's receipt numbers.

    Kept as a per-school setting rather than derived, so that a school which has
    already issued receipts keeps numbering them the same way for ever.
    """
    from models import School

    return (_setting(RECEIPT_PREFIX_KEY)
            or _school_row(School.code)
            or (current_tenant(required=False).slug.upper() if current_tenant(required=False) else 'RCPT'))


def school_brand():
    """``{'name', 'motto', 'tagline', 'logo_url'}`` for the current school."""
    from models import School

    if current_tenant(required=False) is None:
        # The platform host: no school, so nothing to brand.
        return {'name': PLATFORM_NAME, 'motto': '', 'tagline': '',
                'email': '', 'phone': '', 'logo_url': None}

    logo = _setting('school_logo')
    return {
        'name': school_name(),
        'motto': _setting('school_motto') or _school_row(School.motto),
        'tagline': _setting('school_tagline') or _school_row(School.tagline),
        'email': _setting('school_email') or _school_row(School.email),
        'phone': _setting('school_phone') or _school_row(School.phone),
        # A school's own logo is served out of its own uploads folder.
        'logo_url': url_for('static', filename=logo or PLACEHOLDER_LOGO) if has_request_context() else None,
    }
