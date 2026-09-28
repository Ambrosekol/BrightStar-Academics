"""A school changing its own profile and look: its name and contact details, brand colours,
logo and sign-in photographs.

The same choices a platform operator makes when creating a school, available to
the school's own administrators from their admin area. The rules (which colours
are allowed, which images, how many) live in core/branding.py and are shared with
the platform console, so the two can never disagree.

Restricted to the school's own top-level administrator (``is_school_admin``) - not a permission a
custom role can be given, since the school's identity and where its contact details point is not
something an ordinary staff account should be able to change.
"""

from functools import wraps

from flask import flash, redirect, render_template, request, url_for

from app import app
from control_plane.context import current_tenant
from core import theme
from core.branding import (
    IDENTITY_FIELDS, branding_settings, check_branding_inputs, max_branding_request_bytes,
    school_brand, store_branding,
)
from core.security import admin_access_error, admin_required, audit_log, csrf_protect, is_school_admin
from models import School, db
from sqlalchemy import select


def _allow_branding_upload(fn):
    """Raise the request-size limit for the view that takes a logo and photographs.

    The application-wide limit is sized for a single photo. Applied outermost so
    it is in force before anything reads the form.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        request.max_content_length = max_branding_request_bytes()
        return fn(*args, **kwargs)
    return wrapper


@app.route('/admin/school/branding')
@admin_required
def admin_school_branding():
    if not is_school_admin(): return admin_access_error('School profile')
    brand = school_brand()
    settings = branding_settings()
    gallery = theme.parse_gallery(settings.get(theme.GALLERY_KEY))
    # What the school shows is its setting, falling back to its own record for a school
    # created before settings held these.
    row = db.session.scalars(select(School).order_by(School.id)).first()
    identity = {key: settings.get(key) or (getattr(row, column, '') if row else '') or ''
                for key, column, _, _ in IDENTITY_FIELDS}
    return render_template(
        'admin_school_branding.html',
        identity=identity,
        limits={key: longest for key, _, _, longest in IDENTITY_FIELDS},
        primary=brand['primary'] or theme.DEFAULT_PRIMARY,
        accent=brand['accent'] or theme.DEFAULT_ACCENT,
        logo_url=brand['logo_url'],
        photos=[{'name': path.rsplit('/', 1)[-1], 'url': url_for('static', filename=path)}
                for path in gallery],
        max_gallery=theme.MAX_GALLERY_IMAGES,
        min_contrast=theme.MIN_CONTRAST_WITH_WHITE)


@app.post('/admin/school/branding/save')
@_allow_branding_upload
@admin_required
@csrf_protect
def admin_school_branding_save():
    if not is_school_admin(): return admin_access_error('School profile')
    colours = {key: request.form.get(key, '') for key in (theme.PRIMARY_KEY, theme.ACCENT_KEY)}
    # Only what the form actually sent: a field that is missing is left as it is, not blanked.
    colours.update({key: request.form[key] for key, _, _, _ in IDENTITY_FIELDS if key in request.form})
    remove = [f'{theme.GALLERY_FOLDER}{name}' for name in request.form.getlist('remove_photo')]
    logo = request.files.get('logo')
    try:
        branding, gallery = check_branding_inputs(colours, logo, request.files.getlist('gallery'))
        store_branding(current_tenant().slug, branding, logo, gallery, remove)
        if branding.get('school_name'):
            from control_plane.provisioning import set_display_name

            set_display_name(current_tenant().slug, branding['school_name'])
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'error')
    else:
        audit_log('school_branding_updated', 'school', 'settings', None,
                  {'fields': sorted(branding), 'colours': {k: v or 'default' for k, v in branding.items()
                                                          if k in (theme.PRIMARY_KEY, theme.ACCENT_KEY)},
                   'photos_added': len(gallery), 'photos_removed': len(remove),
                   'logo_replaced': bool(getattr(logo, 'filename', ''))})
        flash('Your school branding has been saved.', 'success')
    return redirect(url_for('admin_school_branding'))
