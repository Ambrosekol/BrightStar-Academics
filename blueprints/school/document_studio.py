"""The documents studio: the school designs its testimonial, transcript and leaving certificate - the page, its colours,
its own words with the student's details put in, the transcript table, the head's signature, a barcode of the serial
number. The rules are core/document_design.py; the PDF is core/document_design_pdf.py; on screen it is drawn by
static/id-card-render.js with static/report-card-render.js and static/document-render.js, the code the studio uses.

Each kind has its own design, its own lock and its own saved versions (the newest VERSIONS). A kind never designed
prints from its starting design, so a document can be issued before anyone opens the studio.

Who: documents.manage, through the design studios' rule (core/security.may_use_studios: the School Admin, a head
teacher, or someone given it personally). Nothing a browser sends is trusted: a design is rebuilt from known keys only
before it is saved, and the preview is drawn from that rebuilt design and a made-up student, never a real one.
"""

import json
from datetime import datetime, timezone

from flask import Response, abort, flash, jsonify, redirect, render_template, request, url_for
from sqlalchemy import delete as sa_delete, select

from app import app
from blueprints.finance.helpers import _school_logo_stored
from blueprints.school.design_studios import phone_notice
from blueprints.school.id_cards import _asset_list, _asset_rows, _static, remove_design_image, upload_design_image
from blueprints.school.report_card_data import report_settings
from core import document_design as dd
from core import theme
from core.branding import school_brand
from core.document_design_pdf import render_document
from core.security import admin_required, audit_log, csrf_protect, current_admin
from models import Admin, DocumentDesign, DocumentDesignVersion, db

VERSIONS = 20


def _now():
    return datetime.now(timezone.utc).isoformat()


def _kind_or_404(kind):
    if kind not in dd.KINDS:
        abort(404)
    return kind


def _row(kind):
    return db.session.scalars(select(DocumentDesign).where(DocumentDesign.kind == kind)).first()


def layout_for(kind, assets=None):
    """The school's design of one kind (rebuilt from known keys), or its starting design."""
    row = _row(kind)
    if row:
        try:
            return dd.clean_layout(row.layout_json, assets if assets is not None else {a.id for a in _asset_rows()})
        except ValueError:
            app.logger.exception('A stored %s design could not be read; using the starting design', kind)
    return dd.default_layout(kind)


def _versions(kind):
    rows = db.session.execute(select(DocumentDesignVersion.id, DocumentDesignVersion.saved_at, Admin.display_name)
                              .outerjoin(Admin, Admin.id == DocumentDesignVersion.saved_by)
                              .where(DocumentDesignVersion.kind == kind)
                              .order_by(DocumentDesignVersion.id.desc()).limit(VERSIONS)).all()
    return [{'id': vid, 'saved_at': (at or '')[:16].replace('T', ' '), 'by': by or ''} for vid, at, by in rows]


def _keep_version(kind, stored, admin_id):
    last = db.session.scalars(select(DocumentDesignVersion.layout_json).where(DocumentDesignVersion.kind == kind)
                              .order_by(DocumentDesignVersion.id.desc())).first()
    if last is not None and json.loads(last) == json.loads(stored):
        return
    db.session.add(DocumentDesignVersion(kind=kind, layout_json=stored, saved_at=_now(), saved_by=admin_id))
    db.session.flush()
    old = db.session.scalars(select(DocumentDesignVersion.id).where(DocumentDesignVersion.kind == kind)
                             .order_by(DocumentDesignVersion.id.desc()).offset(VERSIONS)).all()
    if old:
        db.session.execute(sa_delete(DocumentDesignVersion).where(DocumentDesignVersion.id.in_(old)))


def _ensure_row(kind, admin_id):
    row = _row(kind)
    if row is None:
        row = DocumentDesign(kind=kind, layout_json=json.dumps(dd.default_layout(kind), separators=(',', ':'), sort_keys=True),
                             updated_at=_now(), updated_by=admin_id)
        db.session.add(row)
    return row


def _sample(kind):
    brand = school_brand()
    school = {'name': brand.get('name') or '', 'motto': brand.get('motto') or '', 'address': brand.get('address') or '',
              'phone': brand.get('phone') or '', 'email': brand.get('email') or ''}
    settings = report_settings()
    doc = dd.sample(kind, school, {'name': settings['head_name'], 'title': settings['head_title']})
    doc.update(primary=brand.get('primary') or theme.DEFAULT_PRIMARY, accent=brand.get('accent') or theme.DEFAULT_ACCENT,
               logo_path=_school_logo_stored(), signature_path=settings['head_signature'] or None, photo_path=None)
    return doc, brand


@app.route('/admin/documents/studio/<kind>')
@admin_required
def admin_document_studio(kind):
    _kind_or_404(kind)
    notice = phone_notice()
    if notice:
        return notice
    row = _row(kind)
    assets = _asset_list()
    layout = layout_for(kind, {a['id'] for a in assets})
    doc, brand = _sample(kind)
    view = dd.view_data(doc)
    view.update({'brand': {'primary': doc['primary'], 'accent': doc['accent']}, 'logo': brand.get('logo_url'),
                 'signatures': {'head': _static(doc.get('signature_path'))},
                 'assets': {a['id']: a['url'] for a in assets}, 'fields': dd.FIELDS})
    data = {'mode': 'document', 'kind': kind, 'layout': layout, 'view': view, 'sides': list(dd.SIDES),
            'fields': list(dd.FIELDS.items()), 'barcode_fields': list(dd.BARCODE_FIELDS.items()),
            'fonts': list(dd.FONTS.items()), 'default_font': 'times', 'max': dd.MAX_ELEMENTS, 'asset_list': assets,
            'versions': _versions(kind), 'locked': bool(row and row.locked), 'saved_text': 'Saved. Every new copy prints from it.',
            'templates': [{'key': kind, 'name': 'Starting design', 'text': f'The {dd.KINDS[kind].lower()} the school starts with.',
                           'layout': dd.default_layout(kind)}],
            'urls': {'save': url_for('admin_document_studio_save', kind=kind), 'upload': url_for('admin_document_asset_upload'),
                     'asset_delete': url_for('admin_document_asset_delete', asset_id=0),
                     'version': url_for('admin_document_studio_version', kind=kind, vid=0),
                     'preview': url_for('admin_document_studio_preview', kind=kind)}}
    return render_template('school_document_studio.html', design=row, data=data, kind=kind, kinds=dd.KINDS,
                           has_signature=bool(doc.get('signature_path')))


@app.post('/admin/documents/studio/<kind>')
@admin_required
@csrf_protect
def admin_document_studio_save(kind):
    _kind_or_404(kind)
    me = current_admin()
    row = _row(kind)
    if row and row.locked:
        return jsonify(ok=False, error='The design is locked. Unlock it to make changes.'), 409
    try:
        layout = dd.clean_layout(request.form.get('layout', ''), {a.id for a in _asset_rows()})
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    row = _ensure_row(kind, me['id'])
    stored = json.dumps(layout, separators=(',', ':'), sort_keys=True)
    row.layout_json, row.updated_at, row.updated_by = stored, _now(), me['id']
    _keep_version(kind, stored, me['id'])
    db.session.commit()
    audit_log('document_design_saved', 'school', 'document_design', kind, {'items': len(layout['sides']['page1'])})
    return jsonify(ok=True, layout=layout, saved_at=row.updated_at, versions=_versions(kind))


@app.post('/admin/documents/studio/<kind>/lock')
@admin_required
@csrf_protect
def admin_document_studio_lock(kind):
    _kind_or_404(kind)
    me = current_admin()
    row = _ensure_row(kind, me['id'])
    lock = request.form.get('lock') == '1'
    row.locked = 1 if lock else 0
    row.locked_by, row.locked_at = (me['id'], _now()) if lock else (None, None)
    db.session.commit()
    audit_log('document_design_locked' if lock else 'document_design_unlocked', 'school', 'document_design', kind, {})
    flash('Design locked: it cannot change until someone unlocks it.' if lock else 'Design unlocked: it can be changed again.',
          'success')
    return redirect(url_for('admin_document_studio', kind=kind))


@app.route('/admin/documents/studio/<kind>/versions/<int:vid>')
@admin_required
def admin_document_studio_version(kind, vid):
    _kind_or_404(kind)
    row = db.session.get(DocumentDesignVersion, vid)
    if row is None or row.kind != kind:
        abort(404)
    try:
        layout = dd.clean_layout(row.layout_json, {a.id for a in _asset_rows()})
    except ValueError:
        return jsonify(ok=False), 400
    return jsonify(ok=True, layout=layout)


@app.post('/admin/documents/studio/<kind>/preview')
@admin_required
@csrf_protect
def admin_document_studio_preview(kind):
    _kind_or_404(kind)
    rows = _asset_rows()
    try:
        layout = dd.clean_layout(request.form.get('layout', ''), {a.id for a in rows})
    except ValueError as exc:
        return Response(str(exc), status=400, mimetype='text/plain')
    doc, _ = _sample(kind)
    pdf = render_document(doc, layout, {a.id: a.file_path for a in rows})
    response = Response(pdf, mimetype='application/pdf')
    response.headers['Content-Disposition'] = f'inline; filename="{kind}-preview.pdf"'
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.post('/admin/documents/studio/assets')
@admin_required
@csrf_protect
def admin_document_asset_upload():
    return upload_design_image()


@app.post('/admin/documents/studio/assets/<int:asset_id>/delete')
@admin_required
@csrf_protect
def admin_document_asset_delete(asset_id):
    return remove_design_image(asset_id)
