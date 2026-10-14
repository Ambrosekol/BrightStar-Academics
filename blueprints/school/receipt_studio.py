"""The receipt studio: the school designs its own payment receipt - the page, its colours, the payment's details, the
amount, the balance, a PAID stamp, the authorised signature - and decides whether its receipts use it. The rules are
core/receipt_design.py; the PDF is core/receipt_design_pdf.py; on screen it is drawn by static/id-card-render.js with
static/report-card-render.js and static/receipt-render.js, the same code the studio uses.

Who may do what. Everything here needs finance.manage (the right to set up receipts). A locked design cannot be saved
until it is unlocked. Its pictures come from the school's design images, shared with the other studios (IdCardAsset).
Every save, lock, switch and template is in the activity log, and each save is kept as a version (the newest
VERSIONS). A voided receipt is always marked VOID, whatever the design (core/receipt_design_pdf.py).

Nothing a browser sends is trusted: a design is rebuilt from known keys only before it is saved, and the preview is
drawn from that rebuilt design and a made-up payment, never a real one.
"""

import json
from datetime import datetime, timezone

from flask import Response, abort, flash, jsonify, redirect, render_template, request, url_for
from sqlalchemy import delete as sa_delete, select

from app import app
from blueprints.finance.helpers import _receipt_signature_relpath, _school_logo_stored, forget_receipt_design
from blueprints.school.design_studios import phone_notice
from blueprints.school.id_cards import _asset_list, _asset_rows, _static, remove_design_image, upload_design_image
from core import receipt_design as rd
from core import theme
from core.branding import school_brand
from core.receipt_design_pdf import render_designed_receipt
from core.security import admin_required, audit_log, csrf_protect, current_admin
from models import Admin, ReceiptDesign, ReceiptDesignVersion, db

VERSIONS = 20


def _now():
    return datetime.now(timezone.utc).isoformat()


def _row():
    return db.session.scalars(select(ReceiptDesign).order_by(ReceiptDesign.id)).first()


def _layout(row, assets):
    if row:
        try:
            return rd.clean_layout(row.layout_json, assets)
        except ValueError:
            app.logger.exception('A stored receipt design could not be read; using the default')
    return rd.default_layout()


def _versions():
    rows = db.session.execute(select(ReceiptDesignVersion.id, ReceiptDesignVersion.saved_at, Admin.display_name)
                              .outerjoin(Admin, Admin.id == ReceiptDesignVersion.saved_by)
                              .order_by(ReceiptDesignVersion.id.desc()).limit(VERSIONS)).all()
    return [{'id': vid, 'saved_at': (at or '')[:16].replace('T', ' '), 'by': by or ''} for vid, at, by in rows]


def _keep_version(stored, admin_id):
    """Keep this save as a version, unless it is the same design as the last one, and only the newest VERSIONS."""
    last = db.session.scalars(select(ReceiptDesignVersion.layout_json).order_by(ReceiptDesignVersion.id.desc())).first()
    if last is not None and json.loads(last) == json.loads(stored):     # the same design (2 and 2.0 alike)
        return
    db.session.add(ReceiptDesignVersion(layout_json=stored, saved_at=_now(), saved_by=admin_id))
    db.session.flush()
    old = db.session.scalars(select(ReceiptDesignVersion.id).order_by(ReceiptDesignVersion.id.desc()).offset(VERSIONS)).all()
    if old:
        db.session.execute(sa_delete(ReceiptDesignVersion).where(ReceiptDesignVersion.id.in_(old)))


def _ensure_row(admin_id):
    row = _row()
    if row is None:
        row = ReceiptDesign(layout_json=json.dumps(rd.default_layout(), separators=(',', ':'), sort_keys=True),
                            updated_at=_now(), updated_by=admin_id)
        db.session.add(row)
    return row


def _sample():
    """A made-up payment with the school's own name, colours, logo and authorised signature."""
    brand = school_brand()
    school = {'name': brand.get('name') or '', 'motto': brand.get('motto') or '', 'address': brand.get('address') or '',
              'phone': brand.get('phone') or '', 'email': brand.get('email') or ''}
    sheet = rd.sample_sheet(school, brand.get('primary') or theme.DEFAULT_PRIMARY, brand.get('accent') or theme.DEFAULT_ACCENT)
    sheet.update(logo_path=_school_logo_stored(), signature_relpath=_receipt_signature_relpath())
    return sheet, brand


# ---------------------------------------------------------------- the studio
@app.route('/admin/finance/receipts/studio')
@admin_required
def admin_receipt_studio():
    notice = phone_notice()
    if notice:
        return notice
    row = _row()
    assets = _asset_list()
    layout = _layout(row, {a['id'] for a in assets})
    sheet, brand = _sample()
    view = rd.view_data(sheet)
    view.update({'brand': {'primary': sheet['primary'], 'accent': sheet['accent']}, 'logo': brand.get('logo_url'),
                 'signatures': {'authorised': _static(sheet.get('signature_relpath'))},
                 'assets': {a['id']: a['url'] for a in assets}, 'fields': rd.FIELDS})
    # Lists of choices as [key, name] pairs, so they keep their order (static/id-card-designer.js, ordered()).
    data = {'mode': 'receipt', 'layout': layout, 'view': view, 'sides': list(rd.SIDES), 'fields': list(rd.FIELDS.items()),
            'barcode_fields': list(rd.BARCODE_FIELDS.items()), 'fonts': list(rd.FONTS.items()), 'default_font': 'dejavu',
            'max': rd.MAX_ELEMENTS, 'summary_items': list(rd.SUMMARY_ITEMS.items()), 'page_choices': rd.PAGE_CHOICES,
            'asset_list': assets, 'versions': _versions(), 'locked': bool(row and row.locked),
            'saved_text': 'Saved.' + ('' if row and row.in_use else ' Receipts use it once you switch it on.'),
            'templates': [{'key': key, 'name': text.split(':')[0], 'text': text.split(':', 1)[1].strip(),
                           'layout': rd.default_layout(key)} for key, text in rd.TEMPLATES.items()],
            'urls': {'save': url_for('admin_receipt_studio_save'), 'upload': url_for('admin_receipt_asset_upload'),
                     'asset_delete': url_for('admin_receipt_asset_delete', asset_id=0),
                     'version': url_for('admin_receipt_studio_version', vid=0),
                     'preview': url_for('admin_receipt_studio_preview')}}
    return render_template('school_receipt_studio.html', design=row, data=data,
                           has_signature=bool(sheet.get('signature_relpath')))


@app.post('/admin/finance/receipts/studio')
@admin_required
@csrf_protect
def admin_receipt_studio_save():
    me = current_admin()
    row = _row()
    if row and row.locked:
        return jsonify(ok=False, error='The design is locked. Unlock it to make changes.'), 409
    try:
        layout = rd.clean_layout(request.form.get('layout', ''), {a.id for a in _asset_rows()})
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    row = _ensure_row(me['id'])
    stored = json.dumps(layout, separators=(',', ':'), sort_keys=True)
    row.layout_json, row.updated_at, row.updated_by = stored, _now(), me['id']
    _keep_version(stored, me['id'])
    db.session.commit()
    forget_receipt_design()
    audit_log('receipt_design_saved', 'finance', 'receipt_design', None,
              {'items': len(layout['sides']['page1']), 'page': f"{layout['page']} {layout['orientation']}"})
    return jsonify(ok=True, layout=layout, saved_at=row.updated_at, versions=_versions())


@app.post('/admin/finance/receipts/studio/lock')
@admin_required
@csrf_protect
def admin_receipt_studio_lock():
    me = current_admin()
    row = _ensure_row(me['id'])
    lock = request.form.get('lock') == '1'
    row.locked = 1 if lock else 0
    row.locked_by, row.locked_at = (me['id'], _now()) if lock else (None, None)
    db.session.commit()
    forget_receipt_design()
    audit_log('receipt_design_locked' if lock else 'receipt_design_unlocked', 'finance', 'receipt_design', None, {})
    flash('Design locked: it cannot change until someone unlocks it.' if lock else 'Design unlocked: it can be changed again.', 'success')
    return redirect(url_for('admin_receipt_studio'))


@app.post('/admin/finance/receipts/studio/use')
@admin_required
@csrf_protect
def admin_receipt_studio_use():
    """Switch the school's receipts (the PDF, the one emailed to parents, the receipt page and its printout) to its own
    design, or back to the standard receipt."""
    me = current_admin()
    row = _ensure_row(me['id'])
    use = request.form.get('use') == '1'
    row.in_use = 1 if use else 0
    db.session.commit()
    forget_receipt_design()
    audit_log('receipt_design_used' if use else 'receipt_design_standard', 'finance', 'receipt_design', None, {})
    flash('Receipts now use the school\'s own design.' if use else 'Receipts now use the standard design.', 'success')
    return redirect(url_for('admin_receipt_studio'))


@app.route('/admin/finance/receipts/studio/versions/<int:vid>')
@admin_required
def admin_receipt_studio_version(vid):
    """An earlier saved design, to bring back into the studio (it is saved only when the person saves it)."""
    row = db.session.get(ReceiptDesignVersion, vid)
    if row is None:
        abort(404)
    try:
        layout = rd.clean_layout(row.layout_json, {a.id for a in _asset_rows()})
    except ValueError:
        return jsonify(ok=False), 400
    return jsonify(ok=True, layout=layout)


@app.post('/admin/finance/receipts/studio/preview')
@admin_required
@csrf_protect
def admin_receipt_studio_preview():
    """The design on screen as the PDF it prints, with a made-up payment."""
    rows = _asset_rows()
    try:
        layout = rd.clean_layout(request.form.get('layout', ''), {a.id for a in rows})
    except ValueError as exc:
        return Response(str(exc), status=400, mimetype='text/plain')
    sheet, _ = _sample()
    pdf = render_designed_receipt(sheet, layout, {a.id: a.file_path for a in rows})
    response = Response(pdf, mimetype='application/pdf')
    response.headers['Content-Disposition'] = 'inline; filename="Receipt-Preview.pdf"'
    response.headers['Cache-Control'] = 'no-store'
    return response


# ---------------------------------------------------------------- the school's design images, from this studio
@app.post('/admin/finance/receipts/studio/assets')
@admin_required
@csrf_protect
def admin_receipt_asset_upload():
    return upload_design_image()


@app.post('/admin/finance/receipts/studio/assets/<int:asset_id>/delete')
@admin_required
@csrf_protect
def admin_receipt_asset_delete(asset_id):
    return remove_design_image(asset_id)
