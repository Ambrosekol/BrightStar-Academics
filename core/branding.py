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

import logging
import os

from flask import g, has_request_context, url_for
from sqlalchemy import delete, select

from control_plane.context import current_tenant
from core import theme
from core.public_settings import _public_settings

logger = logging.getLogger(__name__)

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


def code_prefix():
    """The school's own code, upper-case, for numbering things that belong to it.

    Candidate numbers begin with it. It is the school's code (which names its portal
    address and its folder), never a fixed string, so no school's numbers carry another
    school's prefix.
    """
    from models import School

    tenant = current_tenant(required=False)
    return (_school_row(School.code) or (tenant.slug if tenant else 'SCH')).upper()


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
                'email': '', 'phone': '', 'logo_url': None, 'logo_path': '',
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
        'logo_path': logo,
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


# ---------------------------------------------------------------------------
# Changing a school's look
#
# Used by both the platform console (creating a school, or changing one) and the
# school's own admin area, so the two can never disagree about what is allowed.
# Every function here raises ValueError with a message that is safe to show.
# ---------------------------------------------------------------------------

# The school's own identity, captured when it is created so its portal is never
# shown to anyone wearing another school's name. These are the keys the school
# portal and its documents already read.
BRANDING_FIELDS = ('school_name', 'school_motto', 'school_tagline',
                   'school_phone', 'school_email', 'school_address',
                   theme.PRIMARY_KEY, theme.ACCENT_KEY)
LOGO_SETTING_KEY = 'school_logo'
COLOUR_KEYS = ((theme.PRIMARY_KEY, 'main', theme.DEFAULT_PRIMARY),
               (theme.ACCENT_KEY, 'accent', theme.DEFAULT_ACCENT))

# The school's identity and contact details: setting key, column on the school's own row, label,
# and the longest value accepted. Only the name is required.
IDENTITY_FIELDS = (
    ('school_name', 'name', 'The school name', 150),
    ('school_motto', 'motto', 'The motto', 200),
    ('school_tagline', 'tagline', 'The tagline', 200),
    ('school_phone', 'phone', 'The phone number', 40),
    ('school_email', 'email', 'The email address', 200),
    ('school_address', 'address', 'The address', 300),
)


def _uploaded(files):
    """The files that were actually chosen; an empty file input still submits one."""
    return [f for f in (files or ()) if f is not None and getattr(f, 'filename', '')]


def check_branding_inputs(branding, logo=None, gallery=()):
    """Validate a school's colours and images without touching any school.

    Returns ``(branding, gallery)`` cleaned: colours normalised, a colour equal to
    the portal's own default turned into "not chosen", and empty file inputs
    dropped.
    """
    from core.uploads import validate_image_upload

    branding = dict(branding or {})
    for key, _, label, longest in IDENTITY_FIELDS:
        if key not in branding:
            continue
        value = ' '.join((branding[key] or '').split())  # trimmed, and no stray line breaks
        if len(value) > longest:
            raise ValueError(f'{label} can be at most {longest} characters.')
        if key == 'school_name' and not value:
            raise ValueError('A school must have a name.')
        if key == 'school_email' and value and ('@' not in value or ' ' in value):
            raise ValueError('The email address does not look like an email address.')
        branding[key] = value
    for key, label, default in COLOUR_KEYS:
        if key in branding:
            colour = theme.check_colour(branding[key], label)
            branding[key] = '' if colour == default else colour
    gallery = _uploaded(gallery)
    if len(gallery) > theme.MAX_GALLERY_IMAGES:
        raise ValueError(f'A school can have at most {theme.MAX_GALLERY_IMAGES} sign-in photos.')
    for label, upload in [('The logo', logo)] + [(f'"{f.filename}"', f) for f in gallery]:
        if _uploaded([upload]):
            try:
                validate_image_upload(upload)
            except ValueError as exc:
                raise ValueError(f'{label}: {exc}') from None
    return branding, gallery


def max_branding_request_bytes():
    """How large a request that carries a logo and a full gallery may be.

    The application-wide limit is sized for a single photo. The pages that take a
    logo and up to ``theme.MAX_GALLERY_IMAGES`` photographs at once raise their
    own limit to fit them.
    """
    one = int(os.environ.get('BRIGHTSTARS_MAX_UPLOAD_BYTES', 5 * 1024 * 1024))
    default = int(os.environ.get('BRIGHTSTARS_MAX_REQUEST_BYTES', 8 * 1024 * 1024))
    return max(default, one * (theme.MAX_GALLERY_IMAGES + 1) + 1024 * 1024)


def branding_settings():
    """Every branding-related setting of the current school, as a dict."""
    from models import SchoolPublicSetting, db

    keys = set(BRANDING_FIELDS) | {LOGO_SETTING_KEY, theme.GALLERY_KEY}
    rows = db.session.execute(select(SchoolPublicSetting.setting_key,
                                     SchoolPublicSetting.setting_value)).all()
    return {key: value for key, value in rows if key in keys}


def store_branding(slug, branding=None, logo=None, gallery=(), remove_gallery=()):
    """Write the current school's name, colours and contact details, and store its
    logo and sign-in photographs inside its own folder.

    Runs in the current school's context (its database and its uploads folder), and
    expects ``branding`` and the files to have passed :func:`check_branding_inputs`.
    ``remove_gallery`` lists photographs (by stored path) to take away. A field that
    is not passed is left exactly as it is; one that is passed blank is cleared — a colour
    returns to the portal's own, a contact detail is removed. The name cannot be blank.
    """
    from datetime import datetime, timezone

    from core.storage import stored_upload_path
    from core.uploads import _save_image_upload
    from models import School, SchoolPublicSetting, db

    def now():
        return datetime.now(timezone.utc).isoformat()

    branding = {k: (v or '').strip() for k, v in (branding or {}).items() if k in BRANDING_FIELDS}
    gallery = _uploaded(gallery)

    school = db.session.scalars(select(School).order_by(School.id)).first()
    if school is not None:
        for key, column, _, _ in IDENTITY_FIELDS:
            if key in branding and (branding[key] or column != 'name'):
                # A field that is passed is set, and a blank one clears it. Only the name can
                # never be blank: a school always has one.
                setattr(school, column, branding[key] or None)
        school.updated_at = now()

    values = dict(branding)
    if _uploaded([logo]):
        values[LOGO_SETTING_KEY] = _save_image_upload(logo, 'branding', f'{slug}_logo')

    removed = []
    if gallery or remove_gallery:
        current_row = db.session.scalars(select(SchoolPublicSetting).where(
            SchoolPublicSetting.setting_key == theme.GALLERY_KEY)).first()
        current = theme.parse_gallery(current_row.setting_value if current_row else '')
        removed = [p for p in current if p in set(remove_gallery)]
        kept = [p for p in current if p not in removed]
        if len(kept) + len(gallery) > theme.MAX_GALLERY_IMAGES:
            raise ValueError(f'A school can have at most {theme.MAX_GALLERY_IMAGES} sign-in photos; '
                             f'this one already has {len(kept)}.')
        added = [_save_image_upload(f, 'branding', f'{slug}_photo') for f in gallery]
        values[theme.GALLERY_KEY] = theme.dump_gallery(kept + added)

    clearable = {theme.PRIMARY_KEY, theme.ACCENT_KEY} | {k for k, _, _, _ in IDENTITY_FIELDS if k != 'school_name'}
    for key, value in values.items():
        if not value:
            if key in clearable:
                # A colour goes back to the portal's own; a blank contact detail is simply absent.
                db.session.execute(delete(SchoolPublicSetting).where(
                    SchoolPublicSetting.setting_key == key))
            continue
        row = db.session.scalars(select(SchoolPublicSetting).where(
            SchoolPublicSetting.setting_key == key)).first()
        if row is None:
            db.session.add(SchoolPublicSetting(setting_key=key, setting_value=value, updated_at=now()))
        else:
            row.setting_value = value
            row.updated_at = now()
    db.session.commit()

    # Only once the new list is safely stored: a failure above must not have
    # already deleted a photograph the school still shows.
    for path in removed:
        target = stored_upload_path(path)
        try:
            if target and os.path.isfile(target):
                os.remove(target)
        except OSError:
            # The change is already saved and the school no longer shows the
            # picture. A file still open elsewhere (Windows will not delete one
            # that is being served) must not turn that into an error; the orphan
            # is harmless and is logged for clean-up.
            logger.warning('Could not delete the removed photograph %s of %s', path, slug)
