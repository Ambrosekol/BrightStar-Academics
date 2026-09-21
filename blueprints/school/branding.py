"""A school changing its own look: brand colours, logo and sign-in photographs.

The same choices a platform operator makes when creating a school, available to
the school's own administrators from their admin area. The rules (which colours
are allowed, which images, how many) live in core/branding.py and are shared with
the platform console, so the two can never disagree.

Guarded by the ``branding.manage`` permission, which the school's top-level
administrator holds and can grant to a role.
"""

from functools import wraps

from flask import flash, redirect, render_template, request, url_for

from app import app
from control_plane.context import current_tenant
from core import theme
from core.branding import (
    branding_settings, check_branding_inputs, max_branding_request_bytes, school_brand,
    store_branding,
)
from core.security import admin_required, audit_log, csrf_protect
from models import db


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
    brand = school_brand()
    gallery = theme.parse_gallery(branding_settings().get(theme.GALLERY_KEY))
    return render_template(
        'admin_school_branding.html',
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
    colours = {key: request.form.get(key, '') for key in (theme.PRIMARY_KEY, theme.ACCENT_KEY)}
    remove = [f'{theme.GALLERY_FOLDER}{name}' for name in request.form.getlist('remove_photo')]
    logo = request.files.get('logo')
    try:
        branding, gallery = check_branding_inputs(colours, logo, request.files.getlist('gallery'))
        store_branding(current_tenant().slug, branding, logo, gallery, remove)
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'error')
    else:
        audit_log('school_branding_updated', 'school', 'settings', None,
                  {'colours': {k: v or 'default' for k, v in branding.items()},
                   'photos_added': len(gallery), 'photos_removed': len(remove),
                   'logo_replaced': bool(getattr(logo, 'filename', ''))})
        flash('Your school branding has been saved.', 'success')
    return redirect(url_for('admin_school_branding'))
