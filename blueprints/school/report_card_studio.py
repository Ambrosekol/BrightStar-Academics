"""The report card studio: the school designs its own report card - the page, its colours, the student's details and
attendance, the results table and every other block - and decides whether its report cards use it. The design rules
are core/report_card_design.py; the PDF is core/report_card_design_pdf.py; the page in the browser is drawn by
static/id-card-render.js with static/report-card-render.js, the same code the studio uses.

Who may do what. Everything here needs report_cards.manage. A locked design cannot be saved until it is unlocked.
The pictures a design uses come from the school's design images, shared with the ID card studio (IdCardAsset). Every
save, lock, switch and template is in the activity log, and each save is kept as a version (the newest VERSIONS).

Nothing a browser sends is trusted: a design is rebuilt from known keys only (clean_layout) before it is saved, and the
preview is drawn from that rebuilt design and a made-up student, never a real one.
"""

import json
from datetime import datetime, timezone

import mimetypes

from flask import Response, abort, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import delete as sa_delete, select

from app import app
from blueprints.school.id_cards import _asset_list, _asset_rows, _static, remove_design_image, upload_design_image
from blueprints.school.design_studios import phone_notice
from blueprints.school.report_card_data import _file, forget_design, grading_key, report_settings, school_design
from core import report_card_design as rcd
from core.branding import school_brand
from core.report_card_design_pdf import render_designed_pdf
from core.security import admin_has_permission, admin_required, audit_log, csrf_protect, current_admin, may_use_studios
from core.storage import read_upload_bytes
from models import Admin, ParentAccount, ReportCardDesign, ReportCardDesignVersion, Student, db

VERSIONS = 20


def _now():
    return datetime.now(timezone.utc).isoformat()


def _row():
    return db.session.scalars(select(ReportCardDesign).order_by(ReportCardDesign.id)).first()


def _layout(row, assets):
    if row:
        try:
            return rcd.clean_layout(row.layout_json, assets)
        except ValueError:
            app.logger.exception('A stored report card design could not be read; using the default')
    return rcd.default_layout()


def _versions():
    rows = db.session.execute(select(ReportCardDesignVersion.id, ReportCardDesignVersion.saved_at, Admin.display_name)
                              .outerjoin(Admin, Admin.id == ReportCardDesignVersion.saved_by)
                              .order_by(ReportCardDesignVersion.id.desc()).limit(VERSIONS)).all()
    return [{'id': vid, 'saved_at': (at or '')[:16].replace('T', ' '), 'by': by or ''} for vid, at, by in rows]


def _keep_version(stored, admin_id):
    """Keep this save as a version, unless it is the same as the last one, and only the newest VERSIONS."""
    last = db.session.scalars(select(ReportCardDesignVersion.layout_json).order_by(ReportCardDesignVersion.id.desc())).first()
    if last is not None and json.loads(last) == json.loads(stored):     # the same design (2 and 2.0 alike)
        return
    db.session.add(ReportCardDesignVersion(layout_json=stored, saved_at=_now(), saved_by=admin_id))
    db.session.flush()
    old = db.session.scalars(select(ReportCardDesignVersion.id).order_by(ReportCardDesignVersion.id.desc()).offset(VERSIONS)).all()
    if old:
        db.session.execute(sa_delete(ReportCardDesignVersion).where(ReportCardDesignVersion.id.in_(old)))


def _ensure_row(admin_id):
    row = _row()
    if row is None:
        row = ReportCardDesign(layout_json=json.dumps(rcd.default_layout(), separators=(',', ':'), sort_keys=True), updated_at=_now(),
                               updated_by=admin_id)
        db.session.add(row)
    return row


def _sample():
    """A made-up card with the school's own name, colours, logo, head and grading key."""
    brand = school_brand()
    settings = report_settings()
    school = {'name': brand.get('name') or '', 'motto': brand.get('motto') or '', 'address': brand.get('address') or '',
              'phone': brand.get('phone') or '', 'email': brand.get('email') or '', 'logo_path': _file(brand.get('logo_path')),
              'primary': brand.get('primary') or '', 'accent': brand.get('accent') or ''}
    head = {'title': settings['head_title'], 'name': settings['head_name'] or 'Mrs O. Adeyemi',
            'signature_path': _file(settings['head_signature'])}
    return rcd.sample_card(school, head, grading_key()), brand


# ---------------------------------------------------------------- the studio
@app.route('/admin/report-cards/studio')
@admin_required
def admin_report_card_studio():
    notice = phone_notice()
    if notice:
        return notice
    row = _row()
    assets = _asset_list()
    layout = _layout(row, {a['id'] for a in assets})
    card, brand = _sample()
    view = rcd.view_data(card)
    view.update({'brand': {'primary': brand.get('primary') or '#0d2b52', 'accent': brand.get('accent') or '#1d6fd8'},
                 'logo': brand.get('logo_url'), 'photo': None, 'initials': 'AO',
                 'signatures': {'head': _static(card['head'].get('signature_path')), 'teacher': None},
                 'assets': {a['id']: a['url'] for a in assets}, 'fields': rcd.FIELDS})
    # Lists of choices as [key, name] pairs, so they keep their order (static/id-card-designer.js, ordered()).
    data = {'mode': 'report', 'layout': layout, 'view': view, 'sides': list(rcd.SIDES), 'fields': list(rcd.FIELDS.items()),
            'barcode_fields': list(rcd.BARCODE_FIELDS.items()), 'fonts': list(rcd.FONTS.items()), 'default_font': 'dejavu',
            'max': rcd.MAX_ELEMENTS, 'result_columns': list(rcd.RESULT_COLUMNS.items()),
            'summary_items': list(rcd.SUMMARY_ITEMS.items()),
            'asset_list': assets, 'versions': _versions(), 'locked': bool(row and row.locked),
            'saved_text': 'Saved.' + ('' if row and row.in_use else ' Report cards use it once you switch it on.'),
            'templates': [{'key': key, 'name': text.split(':')[0], 'text': text.split(':', 1)[1].strip(),
                           'layout': rcd.default_layout(key)} for key, text in rcd.TEMPLATES.items()],
            'urls': {'save': url_for('admin_report_card_studio_save'), 'upload': url_for('admin_report_card_asset_upload'),
                     'asset_delete': url_for('admin_report_card_asset_delete', asset_id=0),
                     'version': url_for('admin_report_card_studio_version', vid=0),
                     'preview': url_for('admin_report_card_studio_preview')}}
    return render_template('school_report_card_studio.html', design=row, data=data)


@app.post('/admin/report-cards/studio')
@admin_required
@csrf_protect
def admin_report_card_studio_save():
    me = current_admin()
    row = _row()
    if row and row.locked:
        return jsonify(ok=False, error='The design is locked. Unlock it to make changes.'), 409
    try:
        layout = rcd.clean_layout(request.form.get('layout', ''), {a.id for a in _asset_rows()})
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    row = _ensure_row(me['id'])
    stored = json.dumps(layout, separators=(',', ':'), sort_keys=True)
    row.layout_json, row.updated_at, row.updated_by = stored, _now(), me['id']
    _keep_version(stored, me['id'])
    db.session.commit()
    forget_design()
    audit_log('report_card_design_saved', 'school', 'report_card_design', None,
              {side: len(layout['sides'][side]) for side in rcd.SIDES})
    return jsonify(ok=True, layout=layout, saved_at=row.updated_at, versions=_versions())


@app.post('/admin/report-cards/studio/lock')
@admin_required
@csrf_protect
def admin_report_card_studio_lock():
    me = current_admin()
    row = _ensure_row(me['id'])
    lock = request.form.get('lock') == '1'
    row.locked = 1 if lock else 0
    row.locked_by, row.locked_at = (me['id'], _now()) if lock else (None, None)
    db.session.commit()
    forget_design()
    audit_log('report_card_design_locked' if lock else 'report_card_design_unlocked', 'school', 'report_card_design', None, {})
    flash('Design locked: it cannot change until someone unlocks it.' if lock else 'Design unlocked: it can be changed again.', 'success')
    return redirect(url_for('admin_report_card_studio'))


@app.post('/admin/report-cards/studio/use')
@admin_required
@csrf_protect
def admin_report_card_studio_use():
    """Switch the school's report cards (PDF and web page, for staff, parents and students) to its own design, or back
    to the standard card."""
    me = current_admin()
    row = _ensure_row(me['id'])
    use = request.form.get('use') == '1'
    row.in_use = 1 if use else 0
    db.session.commit()
    forget_design()
    audit_log('report_card_design_used' if use else 'report_card_design_standard', 'school', 'report_card_design', None, {})
    flash('Report cards now use the school\'s own design.' if use else 'Report cards now use the standard design.', 'success')
    return redirect(url_for('admin_report_card_studio'))


@app.route('/admin/report-cards/studio/versions/<int:vid>')
@admin_required
def admin_report_card_studio_version(vid):
    """An earlier saved design, to bring back into the studio (it is saved only when the person saves it)."""
    row = db.session.get(ReportCardDesignVersion, vid)
    if row is None:
        abort(404)
    try:
        layout = rcd.clean_layout(row.layout_json, {a.id for a in _asset_rows()})
    except ValueError:
        return jsonify(ok=False), 400
    return jsonify(ok=True, layout=layout)


@app.post('/admin/report-cards/studio/preview')
@admin_required
@csrf_protect
def admin_report_card_studio_preview():
    """The design on screen as the PDF it prints, with the made-up student."""
    rows = _asset_rows()
    try:
        layout = rcd.clean_layout(request.form.get('layout', ''), {a.id for a in rows})
    except ValueError as exc:
        return Response(str(exc), status=400, mimetype='text/plain')
    card, _ = _sample()
    pdf = render_designed_pdf([card], layout, {a.id: a.file_path for a in rows})
    response = Response(pdf, mimetype='application/pdf')
    response.headers['Content-Disposition'] = 'inline; filename="Report-Card-Preview.pdf"'
    response.headers['Cache-Control'] = 'no-store'
    return response


# ---------------------------------------------------------------- the school's design images, from this studio
@app.post('/admin/report-cards/studio/assets')
@admin_required
@csrf_protect
def admin_report_card_asset_upload():
    return upload_design_image()


# ---------------------------------------------------------------- the design's images, for the report card page
def _signed_in_here():
    """Someone of this school is signed in: staff, a parent or a student (the session belongs to this school's own
    address, so it is never another school's)."""
    if current_admin():
        return True
    if session.get('parent_id'):
        return db.session.scalar(select(ParentAccount.id).where(ParentAccount.id == session['parent_id'], ParentAccount.active == 1)) is not None
    if session.get('student_id'):
        return db.session.scalar(select(Student.id).where(Student.id == session['student_id'], Student.active == 1,
                                                          Student.account_active == 1)) is not None
    return False


@app.route('/report-card-design/images/<int:asset_id>')
def report_design_image(asset_id):
    """One of the images the school's report card design uses, for its report card pages. Only to someone signed in
    at this school, and only an image the design in use shows (never one of the school's other images). An image never
    changes under its number (a new upload is a new number), so the browser keeps it for a week."""
    design = school_design()
    if not _signed_in_here() or not design or asset_id not in design[1]:
        abort(404)
    data = read_upload_bytes(design[1][asset_id])
    if data is None:
        abort(404)
    response = Response(data, mimetype=mimetypes.guess_type(design[1][asset_id])[0] or 'application/octet-stream')
    response.headers['Cache-Control'] = 'private, max-age=604800, immutable'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Robots-Tag'] = 'noindex'
    return response


@app.post('/admin/report-cards/studio/assets/<int:asset_id>/delete')
@admin_required
@csrf_protect
def admin_report_card_asset_delete(asset_id):
    return remove_design_image(asset_id)


def report_studio_shown():
    admin = current_admin()
    return bool(admin and may_use_studios(admin['id'], 'report_cards.manage'))


app.jinja_env.globals.update(report_studio_shown=report_studio_shown)
