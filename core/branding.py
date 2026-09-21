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

from flask import g, has_request_context, url_for
from sqlalchemy import select

from control_plane.context import current_tenant
from core import theme
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


def _brand_colour(key, label):
    """A stored brand colour, or '' if none was chosen or the stored value is not
    one — a bad value must never reach a page, only fall back to the default."""
    try:
        return theme.check_colour(_setting(key), label)
    except ValueError:
        return ''


def school_brand():
    """``{'name', 'motto', 'tagline', 'logo_url', 'primary', 'accent', 'gallery', ...}``
    for the current school.

    ``primary`` and ``accent`` are the school's own colours, or '' where it has
    not chosen one. ``gallery`` is the list of photographs shown on its sign-in
    page. Computed once per request: it is read by many templates and by the
    hook that applies the colours.
    """
    from models import School

    if current_tenant(required=False) is None:
        # The platform host: no school, so nothing to brand.
        return {'name': PLATFORM_NAME, 'motto': '', 'tagline': '',
                'email': '', 'phone': '', 'logo_url': None,
                'primary': '', 'accent': '', 'primary_dark': '', 'gallery': [], 'theme_css': ''}

    if has_request_context() and '_school_brand' in g:
        return g._school_brand

    logo = _setting('school_logo')
    primary = _brand_colour(theme.PRIMARY_KEY, 'main')
    accent = _brand_colour(theme.ACCENT_KEY, 'accent')
    brand = {
        'name': school_name(),
        'motto': _setting('school_motto') or _school_row(School.motto),
        'tagline': _setting('school_tagline') or _school_row(School.tagline),
        'email': _setting('school_email') or _school_row(School.email),
        'phone': _setting('school_phone') or _school_row(School.phone),
        # A school's own logo is served out of its own uploads folder.
        'logo_url': url_for('static', filename=logo or PLACEHOLDER_LOGO) if has_request_context() else None,
        'primary': primary,
        'accent': accent,
        'primary_dark': theme.shade(primary, -0.35) if primary else '',
        'gallery': ([url_for('static', filename=path)
                     for path in theme.parse_gallery(_setting(theme.GALLERY_KEY))]
                    if has_request_context() else []),
        'theme_css': theme.theme_css(primary, accent),
    }
    if has_request_context():
        g._school_brand = brand
    return brand
