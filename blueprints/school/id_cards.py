"""ID cards: printing them for students and staff, the school's card designer, cancelling a lost card, and the
public page a card's QR code opens. The design rules and the QR code are core/id_cards.py.

Who may do what. Printing needs id_cards.print (and, for students, the classes the person may see; for teaching
staff, only the class - or the subdivision - they are class teacher of, and never staff cards); designing,
locking, cancelling and replacing need id_cards.manage, as do the school's own card images (uploaded pictures for
designs) and bringing back an earlier version of a design. A locked design cannot be saved or reset until someone
with id_cards.manage unlocks it, and an image a locked design uses cannot be removed. The school's design images are
shared with the report card studio (blueprints/school/report_card_studio.py), which reaches them with
report_cards.manage through upload_design_image() and remove_design_image(). Every save, lock, print,
cancel, replacement, upload and removal is in the activity log. Each save is also kept as a version (the newest
VERSIONS for each kind of card).

The verification page (/id/<token>) needs no sign-in: it answers only for a real card's random token, shows only the
school, the name, the photo, the class or role and whether the card is valid, and is never cached or indexed.
"""

import json
import mimetypes
import re
from datetime import datetime, timezone

from flask import abort, flash, jsonify, make_response, redirect, render_template, request, url_for
from sqlalchemy import and_, delete as sa_delete, func, select

from app import _school_current_session, app
from blueprints.school.helpers import _school_class_allowed
from blueprints.school.report_card_data import report_settings
from core import id_cards as ic
from core.branding import school_brand
from core.db_helpers import all_rows, one, tuples
from core.security import (
    admin_access_error, admin_has_permission, admin_required, audit_log, csrf_protect, current_admin, is_teaching_staff,
    may_use_studios, teaching_duties,
)
from core.storage import delete_upload, read_upload_bytes
from core.uploads import _save_image_upload
from models import (
    AcademicSession, Admin, AdminType, IdCard, IdCardAsset, IdCardDesign, IdCardDesignVersion, ReceiptDesign, ReportCardDesign, SchoolClass,
    StaffRecord, Student, StudentEnrolment, db,
)

PRINT_LIMIT = 300
VERSIONS = 20


def _now():
    return datetime.now(timezone.utc).isoformat()


def _kind(value):
    return value if value in ic.KINDS else 'student'


def _design_row(kind):
    return db.session.scalars(select(IdCardDesign).where(IdCardDesign.kind == kind)).first()


def _asset_rows():
    return db.session.scalars(select(IdCardAsset).order_by(IdCardAsset.id)).all()


def _asset_list(rows=None):
    return [{'id': a.id, 'name': a.name, 'url': _static(a.file_path)} for a in (rows if rows is not None else _asset_rows())]


def _layout(kind, assets=None):
    row = _design_row(kind)
    if assets is None:
        assets = {a.id for a in _asset_rows()}
    if row:
        try:
            return ic.clean_layout(row.layout_json, kind, assets)
        except ValueError:
            app.logger.exception('A stored ID card design could not be read; using the default')
    return ic.default_layout(kind)


def _versions(kind):
    rows = db.session.execute(select(IdCardDesignVersion.id, IdCardDesignVersion.saved_at, Admin.display_name)
                              .outerjoin(Admin, Admin.id == IdCardDesignVersion.saved_by)
                              .where(IdCardDesignVersion.kind == kind).order_by(IdCardDesignVersion.id.desc()).limit(VERSIONS)).all()
    return [{'id': vid, 'saved_at': (at or '')[:16].replace('T', ' '), 'by': by or ''} for vid, at, by in rows]


def _static(path):
    return url_for('static', filename=path) if path else None


def _signature_url():
    path = (report_settings().get('head_signature') or '').strip()
    return _static(path)


def _common(brand, assets):
    """What every card on a page shares: the school's colours and logo, and its own card images."""
    return {'brand': {'primary': brand.get('primary') or '#0d2b52', 'accent': brand.get('accent') or '#1d6fd8'},
            'logo': brand.get('logo_url'), 'assets': {a['id']: a['url'] for a in assets}}


# ---------------------------------------------------------------- the people
def _teacher_reach(admin_id):
    """For teaching staff, the students whose cards they handle: ``{class_id: subdivision ids, or None for the whole
    class}`` for each class they are class teacher of (a subject they teach there does not count). None for anyone
    else, whose usual class limits apply."""
    if not is_teaching_staff(admin_id):
        return None
    reach = {}
    for d in teaching_duties(admin_id):
        if d['subject_id'] is not None:
            continue
        cid, sub = d['class_id'], d.get('subdivision_id')
        if sub is None:
            reach[cid] = None
        elif cid not in reach or reach[cid] is not None:
            reach.setdefault(cid, set()).add(sub)
    return reach


def _students(class_id=None, ids=None):
    current = _school_current_session()
    if not current:
        return []
    stmt = (select(Student.id, Student.first_name, Student.middle_name, Student.last_name, Student.admission_no,
                   Student.gender, Student.blood_group, Student.guardian_phone, Student.department, Student.photo_path,
                   Student.date_of_birth, Student.genotype, Student.state_of_origin, Student.religion, Student.guardian_name,
                   SchoolClass.id.label('class_id'), SchoolClass.name.label('class_name'),
                   AcademicSession.name.label('session_name'), StudentEnrolment.subdivision_id)
            .join(StudentEnrolment, and_(StudentEnrolment.student_id == Student.id, StudentEnrolment.active == 1,
                                         StudentEnrolment.session_id == current['id']))
            .join(SchoolClass, SchoolClass.id == StudentEnrolment.class_id)
            .join(AcademicSession, AcademicSession.id == StudentEnrolment.session_id)
            .where(Student.active == 1, Student.archived_at.is_(None))
            .order_by(SchoolClass.level_order, SchoolClass.name, Student.last_name, Student.first_name))
    if class_id:
        stmt = stmt.where(SchoolClass.id == class_id)
    if ids is not None:
        stmt = stmt.where(Student.id.in_(list(ids) or [0]))
    me = current_admin()
    reach = _teacher_reach(me['id'])
    if reach is not None:
        # A class teacher: their own class (or subdivision) only.
        return [dict(r) for r in all_rows(stmt) if r['class_id'] in reach
                and (reach[r['class_id']] is None or r['subdivision_id'] in reach[r['class_id']])]
    allowed = {}
    out = []
    for r in all_rows(stmt):
        if r['class_id'] not in allowed:
            allowed[r['class_id']] = _school_class_allowed(me['id'], r['class_id'])
        if allowed[r['class_id']]:
            out.append(dict(r))
    return out


def _staff(ids=None):
    stmt = (select(Admin.id, Admin.display_name, Admin.phone, Admin.email, Admin.photo_path, Admin.signature_path,
                   AdminType.name.label('role'), StaffRecord.staff_no, StaffRecord.job_title, StaffRecord.department,
                   StaffRecord.title, StaffRecord.first_name, StaffRecord.last_name, StaffRecord.gender, StaffRecord.date_of_birth,
                   StaffRecord.state_of_origin, StaffRecord.category, StaffRecord.employment_type, StaffRecord.employment_date,
                   StaffRecord.nok_name, StaffRecord.nok_phone)
            .outerjoin(AdminType, AdminType.id == Admin.admin_type_id)
            .outerjoin(StaffRecord, StaffRecord.admin_id == Admin.id)
            .where(Admin.active == 1, ~Admin.username.startswith('platform@'))
            .order_by(Admin.display_name))
    if ids is not None:
        stmt = stmt.where(Admin.id.in_(list(ids) or [0]))
    return [dict(r) for r in all_rows(stmt)]


def _cards(kind, person_ids):
    """``{person_id: newest card row}``."""
    found = {}
    for card in db.session.scalars(select(IdCard).where(IdCard.kind == kind, IdCard.person_id.in_(list(person_ids) or [0]))
                                   .order_by(IdCard.id)):
        if card.status == 'active' or card.person_id not in found or found[card.person_id].status != 'active':
            found[card.person_id] = card
    return found


def _issue(kind, person_id, actor_id):
    card = IdCard(kind=kind, person_id=person_id, token=ic.new_token(), serial='pending', status='active',
                  issued_at=_now(), issued_by=actor_id)
    db.session.add(card)
    db.session.flush()
    card.serial = ic.serial_for(kind, card.id)
    return card


def _card_view(kind, person, card, brand, layout, head_signature=None, every_detail=False):
    """What one card shows, for static/id-card-render.js: its details, photograph, QR code, the barcodes its design uses,
    the signatures and the initials for a missing photograph. Plain values only, and only the details the design shows
    (``every_detail`` for the studio's sample person)."""
    full = data = ic.card_data(kind, person, brand, card.serial if card else ic.serial_for(kind, 0), card.issued_at if card else _now())
    name = data.get('name') or ''
    if not every_detail:
        shown = {el['field'] for side in ic.SIDES for el in layout['sides'][side] if el['type'] in ('field', 'barcode')}
        data = {k: v for k, v in data.items() if k in shown}
    verify = url_for('id_card_verify', token=card.token, _external=True) if card else 'SAMPLE'
    wanted = {el['field'] for side in ic.SIDES for el in layout['sides'][side] if el['type'] == 'barcode'}
    barcodes = {}
    for field in wanted:
        code = ic.barcode_path(full.get(field))
        if code:
            barcodes[field] = code
    return {'data': data, 'photo': _static(person.get('photo_path')), 'qr': ic.qr_path(verify), 'barcodes': barcodes,
            'signatures': {'head': head_signature, 'holder': _static(person.get('signature_path')) if kind == 'staff' else None},
            'initials': ''.join(p[:1] for p in name.split()[:2]).upper()}


# ---------------------------------------------------------------- the list, and printing
@app.route('/admin/id-cards')
@admin_required
def admin_id_cards():
    me = current_admin()
    reach = _teacher_reach(me['id'])
    if reach is not None and not reach:
        return admin_access_error('ID cards for your own class (class teachers only)')
    kind = 'student' if reach is not None else _kind(request.args.get('kind'))
    class_id = request.args.get('class', type=int)
    q = request.args.get('q', '').strip()[:80]
    people = _students(class_id) if kind == 'student' else _staff()
    if q:
        needle = q.casefold()
        people = [p for p in people if needle in ' '.join(str(p.get(k) or '') for k in
                  ('first_name', 'last_name', 'admission_no', 'display_name', 'class_name', 'role')).casefold()]
    cards = _cards(kind, [p['id'] for p in people])
    classes = [] if kind != 'student' else [c for c in all_rows(select(SchoolClass.id, SchoolClass.name)
                                                                   .where(SchoolClass.active == 1).order_by(SchoolClass.level_order, SchoolClass.name))
                                           if (c['id'] in reach if reach is not None else _school_class_allowed(me['id'], c['id']))]
    design = _design_row(kind)
    return render_template('school_id_cards.html', kind=kind, people=people, cards=cards, classes=classes,
                           class_id=class_id, q=q, design=design, limit=PRINT_LIMIT, staff_allowed=reach is None,
                           can_manage=admin_has_permission(me['id'], 'id_cards.manage'),
                           can_design=may_use_studios(me['id'], 'id_cards.manage'))


@app.post('/admin/id-cards/print')
@admin_required
@csrf_protect
def admin_id_cards_print():
    me = current_admin()
    kind = _kind(request.form.get('kind'))
    if kind == 'staff' and is_teaching_staff(me['id']):
        audit_log('authorization_denied', 'school', 'id_cards', None, {'kind': kind, 'reason': 'teaching staff'}, False, me)
        return admin_access_error('staff ID cards')
    ids = {int(v) for v in request.form.getlist('person') if str(v).isdigit()}
    if not ids:
        flash('Tick the people to print cards for.', 'error')
        return redirect(url_for('admin_id_cards', kind=kind))
    if len(ids) > PRINT_LIMIT:
        flash(f'Print at most {PRINT_LIMIT} cards at a time.', 'error')
        return redirect(url_for('admin_id_cards', kind=kind))
    people = _students(ids=ids) if kind == 'student' else _staff(ids=ids)
    if len(people) != len(ids):
        # Someone outside the people this account may see (another class, a left student, a removed staff member).
        audit_log('authorization_denied', 'school', 'id_cards', None, {'kind': kind, 'asked': len(ids), 'allowed': len(people)}, False, me)
        flash('Some of those people are not ones you can print cards for. Nothing was printed.', 'error')
        return redirect(url_for('admin_id_cards', kind=kind))
    cards = _cards(kind, ids)
    issued = 0
    for p in people:
        card = cards.get(p['id'])
        if card is None or card.status != 'active':
            cards[p['id']] = _issue(kind, p['id'], me['id'])
            issued += 1
        cards[p['id']].printed_count = (cards[p['id']].printed_count or 0) + 1
    db.session.flush()
    brand = school_brand()
    assets = _asset_list()
    layout = _layout(kind, {a['id'] for a in assets})
    head = _signature_url()
    views = [_card_view(kind, p, cards[p['id']], brand, layout, head) for p in people]
    db.session.commit()
    response = make_response(render_template('school_id_card_sheet.html', layout=layout, views=views, kind=kind,
                                             backs=request.form.get('backs') == '1', common=_common(brand, assets)))
    response.headers['Cache-Control'] = 'no-store, private'
    # Logged once the sheet is made: logging commits, and the cards would otherwise be read again one by one.
    audit_log('id_cards_printed', 'school', 'id_cards', None, {'kind': kind, 'cards': len(people), 'issued_new': issued})
    return response


@app.post('/admin/id-cards/cards/<int:card_id>/cancel')
@admin_required
@csrf_protect
def admin_id_card_cancel(card_id):
    me = current_admin()
    card = db.session.get(IdCard, card_id)
    if card is None:
        abort(404)
    if not _may_handle(card):
        flash('That card is not one you can cancel.', 'error')
        return redirect(url_for('admin_id_cards', kind=card.kind))
    reason = ' '.join(request.form.get('reason', '').split())[:200]
    if card.status == 'active':
        card.status, card.cancelled_at, card.cancelled_by, card.cancel_reason = 'cancelled', _now(), me['id'], reason or 'Cancelled'
        db.session.commit()
        audit_log('id_card_cancelled', 'school', 'id_card', card_id, {'kind': card.kind, 'person_id': card.person_id, 'reason': reason})
        flash(f'Card {card.serial} cancelled. Its QR code now says it is no longer valid.', 'success')
    return redirect(url_for('admin_id_cards', kind=card.kind))


@app.post('/admin/id-cards/cards/<int:card_id>/replace')
@admin_required
@csrf_protect
def admin_id_card_replace(card_id):
    """A lost or damaged card: cancel it and issue a new one (with a new QR code), ready to print."""
    me = current_admin()
    card = db.session.get(IdCard, card_id)
    if card is None or card.status != 'active':
        abort(404)
    people = (_students(ids=[card.person_id]) if card.kind == 'student' else _staff(ids=[card.person_id])) if _may_handle(card) else []
    if not people:
        flash('That person is not one you can issue cards for.', 'error')
        return redirect(url_for('admin_id_cards', kind=card.kind))
    reason = ' '.join(request.form.get('reason', '').split())[:200] or 'Replaced (lost or damaged)'
    card.status, card.cancelled_at, card.cancelled_by, card.cancel_reason = 'cancelled', _now(), me['id'], reason
    new = _issue(card.kind, card.person_id, me['id'])
    db.session.commit()
    audit_log('id_card_replaced', 'school', 'id_card', card_id, {'kind': card.kind, 'person_id': card.person_id,
                                                                 'new_card': new.serial, 'reason': reason})
    flash(f'Card {card.serial} cancelled and {new.serial} issued. Tick the person and print their new card.', 'success')
    return redirect(url_for('admin_id_cards', kind=card.kind))


def _may_handle(card):
    """Whether the person signed in may cancel or replace this card: anyone may whom the route lets in, except that
    teaching staff are kept to their own class's students."""
    me = current_admin()
    if _teacher_reach(me['id']) is None:
        return True
    return card.kind == 'student' and bool(_students(ids=[card.person_id]))


# ---------------------------------------------------------------- the designer
def _sample(kind):
    """A real person to preview the design with (one with a photograph if there is one), or a made-up one."""
    if kind == 'student':
        people = _students()
        person = next((p for p in people if p.get('photo_path')), people[0] if people else None)
        person = person or {'first_name': 'Ada', 'last_name': 'Okafor', 'admission_no': 'ADM/0001', 'class_name': 'JSS 1',
                            'session_name': '2026/2027', 'gender': 'Female'}
    else:
        people = _staff()
        person = next((p for p in people if p.get('photo_path')), people[0] if people else None)
        person = person or {'display_name': 'Mrs A. Bello', 'role': 'Class Teacher', 'phone': '0803 000 0000'}
    return person


@app.route('/admin/id-cards/design/<kind>')
@admin_required
def admin_id_card_designer(kind):
    if kind not in ic.KINDS:
        abort(404)
    from blueprints.school.design_studios import phone_notice
    notice = phone_notice()
    if notice:
        return notice
    brand = school_brand()
    row = _design_row(kind)
    assets = _asset_list()
    layout = _layout(kind, {a['id'] for a in assets})
    sample = _sample(kind)
    view = _card_view(kind, sample, None, brand, {'sides': {'front': [{'type': 'barcode', 'field': f} for f in ic.BARCODE_FIELDS[kind]],
                                                            'back': []}}, _signature_url(), every_detail=True)
    view['qr'] = ic.qr_path('https://example.com/id/sample')
    view.update(_common(brand, assets))
    view['fields'] = ic.fields_for(kind)
    # Lists of choices as [key, name] pairs, so they keep their order (static/id-card-designer.js, ordered()).
    data = {'layout': layout, 'kind': kind, 'view': view, 'fields': list(ic.fields_for(kind).items()),
            'barcode_fields': list(ic.BARCODE_FIELDS[kind].items()), 'fonts': list(ic.FONTS.items()), 'max': ic.MAX_ELEMENTS, 'asset_list': assets, 'versions': _versions(kind),
            'templates': [{'key': key, 'name': text.split(':')[0], 'text': text.split(':', 1)[1].strip(),
                           'layout': ic.default_layout(kind, key)} for key, text in ic.TEMPLATES.items()],
            'locked': bool(row and row.locked),
            'urls': {'save': url_for('admin_id_card_design_save', kind=kind), 'upload': url_for('admin_id_card_asset_upload'),
                     'asset_delete': url_for('admin_id_card_asset_delete', asset_id=0),
                     'version': url_for('admin_id_card_design_version', kind=kind, vid=0)}}
    return render_template('school_id_card_designer.html', kind=kind, design=row, data=data, sample_name=view['data'].get('name'),
                           person_fields=ic.FIELDS[kind], school_fields=ic.SCHOOL_FIELDS,
                           templates=ic.TEMPLATES, brand=brand, has_staff_signature=bool(sample.get('signature_path')))


@app.post('/admin/id-cards/design/<kind>')
@admin_required
@csrf_protect
def admin_id_card_design_save(kind):
    if kind not in ic.KINDS:
        abort(404)
    me = current_admin()
    row = _design_row(kind)
    if row and row.locked:
        return jsonify(ok=False, error='The design is locked. Unlock it to make changes.'), 409
    try:
        layout = ic.clean_layout(request.form.get('layout', ''), kind, {a.id for a in _asset_rows()})
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    if row is None:
        row = IdCardDesign(kind=kind, layout_json='{}', updated_at=_now())
        db.session.add(row)
    stored = json.dumps(layout, separators=(',', ':'), sort_keys=True)
    row.layout_json, row.updated_at, row.updated_by = stored, _now(), me['id']
    _keep_version(kind, stored, me['id'])
    db.session.commit()
    audit_log('id_card_design_saved', 'school', 'id_card_design', kind,
              {'front': len(layout['sides']['front']), 'back': len(layout['sides']['back']), 'orientation': layout['orientation']})
    return jsonify(ok=True, layout=layout, saved_at=row.updated_at, versions=_versions(kind))


def _keep_version(kind, stored, admin_id):
    """Keep this save as a version, unless it is the same as the last one, and only the newest VERSIONS."""
    last = db.session.scalars(select(IdCardDesignVersion.layout_json).where(IdCardDesignVersion.kind == kind)
                              .order_by(IdCardDesignVersion.id.desc())).first()
    if last is not None and json.loads(last) == json.loads(stored):     # the same design (2 and 2.0 alike)
        return
    db.session.add(IdCardDesignVersion(kind=kind, layout_json=stored, saved_at=_now(), saved_by=admin_id))
    db.session.flush()
    old = db.session.scalars(select(IdCardDesignVersion.id).where(IdCardDesignVersion.kind == kind)
                             .order_by(IdCardDesignVersion.id.desc()).offset(VERSIONS)).all()
    if old:
        db.session.execute(sa_delete(IdCardDesignVersion).where(IdCardDesignVersion.id.in_(old)))


@app.post('/admin/id-cards/design/<kind>/lock')
@admin_required
@csrf_protect
def admin_id_card_design_lock(kind):
    if kind not in ic.KINDS:
        abort(404)
    me = current_admin()
    row = _design_row(kind)
    if row is None:
        row = IdCardDesign(kind=kind, layout_json=json.dumps(ic.default_layout(kind)), updated_at=_now(), updated_by=me['id'])
        db.session.add(row)
    lock = request.form.get('lock') == '1'
    row.locked = 1 if lock else 0
    row.locked_by, row.locked_at = (me['id'], _now()) if lock else (None, None)
    db.session.commit()
    audit_log('id_card_design_locked' if lock else 'id_card_design_unlocked', 'school', 'id_card_design', kind, {})
    flash('Design locked: it cannot change until someone unlocks it.' if lock else 'Design unlocked: it can be changed again.', 'success')
    return redirect(url_for('admin_id_card_designer', kind=kind))


@app.post('/admin/id-cards/design/<kind>/reset')
@admin_required
@csrf_protect
def admin_id_card_design_reset(kind):
    if kind not in ic.KINDS:
        abort(404)
    me = current_admin()
    row = _design_row(kind)
    if row and row.locked:
        flash('The design is locked. Unlock it to make changes.', 'error')
        return redirect(url_for('admin_id_card_designer', kind=kind))
    template = request.form.get('template') if request.form.get('template') in ic.TEMPLATES else 'classic'
    if row is None:
        row = IdCardDesign(kind=kind, layout_json='{}', updated_at=_now())
        db.session.add(row)
    stored = json.dumps(ic.default_layout(kind, template), separators=(',', ':'), sort_keys=True)
    row.layout_json, row.updated_at, row.updated_by = stored, _now(), me['id']
    _keep_version(kind, stored, me['id'])
    db.session.commit()
    audit_log('id_card_design_reset', 'school', 'id_card_design', kind, {'template': template})
    flash(f'The design now starts from the {template} template. The one before is kept in Versions.', 'success')
    return redirect(url_for('admin_id_card_designer', kind=kind))


@app.route('/admin/id-cards/design/<kind>/versions/<int:vid>')
@admin_required
def admin_id_card_design_version(kind, vid):
    """An earlier saved design, to bring back into the designer (it is saved only when the person saves it)."""
    if kind not in ic.KINDS:
        abort(404)
    row = db.session.get(IdCardDesignVersion, vid)
    if row is None or row.kind != kind:
        abort(404)
    try:
        layout = ic.clean_layout(row.layout_json, kind, {a.id for a in _asset_rows()})
    except ValueError:
        return jsonify(ok=False), 400
    return jsonify(ok=True, layout=layout)


# ---------------------------------------------------------------- the school's own card images
@app.post('/admin/id-cards/assets')
@admin_required
@csrf_protect
def admin_id_card_asset_upload():
    return upload_design_image()


def upload_design_image():
    """An image for the school's designs (a pattern, a badge, a stamp): checked, re-encoded and kept under
    uploads/idcards/. The caller has checked who may do this."""
    me = current_admin()
    if db.session.scalar(select(func.count(IdCardAsset.id))) >= ic.MAX_ASSETS:
        return jsonify(ok=False, error=f'The school can keep at most {ic.MAX_ASSETS} card images. Remove one first.'), 400
    upload = request.files.get('image')
    if not upload or not upload.filename:
        return jsonify(ok=False, error='Choose an image to upload.'), 400
    try:
        path = _save_image_upload(upload, 'idcards', 'card')
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    if not path:
        return jsonify(ok=False, error='Choose an image to upload.'), 400
    stem = ' '.join(re.sub(r'[^\w .-]', ' ', (upload.filename or '').rsplit('.', 1)[0]).split())[:40] or 'Image'
    asset = IdCardAsset(name=stem, file_path=path, created_at=_now(), created_by=me['id'])
    db.session.add(asset)
    db.session.commit()
    from blueprints.school.report_card_data import forget_design
    from blueprints.finance.helpers import forget_receipt_design
    forget_design(); forget_receipt_design()
    audit_log('id_card_asset_uploaded', 'school', 'id_card_asset', asset.id, {})
    return jsonify(ok=True, asset={'id': asset.id, 'name': asset.name, 'url': _static(asset.file_path)})


@app.post('/admin/id-cards/assets/<int:asset_id>/delete')
@admin_required
@csrf_protect
def admin_id_card_asset_delete(asset_id):
    return remove_design_image(asset_id)


def remove_design_image(asset_id):
    """Remove one of the school's design images, unless a locked design (an ID card's or the report card's) uses it."""
    from core import report_card_design as rcd
    asset = db.session.get(IdCardAsset, asset_id)
    if asset is None:
        abort(404)
    for row in db.session.scalars(select(IdCardDesign).where(IdCardDesign.locked == 1)).all():
        try:
            used = ic.assets_used(ic.clean_layout(row.layout_json, row.kind, {asset_id}))
        except ValueError:
            used = set()
        if asset_id in used:
            return jsonify(ok=False, error=f'The {row.kind} card design is locked and uses this image. Unlock it first.'), 409
    for row in db.session.scalars(select(ReportCardDesign).where(ReportCardDesign.locked == 1)).all():
        try:
            used = rcd.assets_used(rcd.clean_layout(row.layout_json, {asset_id}))
        except ValueError:
            used = set()
        if asset_id in used:
            return jsonify(ok=False, error='The report card design is locked and uses this image. Unlock it first.'), 409
    from core import receipt_design as recd
    for row in db.session.scalars(select(ReceiptDesign).where(ReceiptDesign.locked == 1)).all():
        try:
            used = recd.assets_used(recd.clean_layout(row.layout_json, {asset_id}))
        except ValueError:
            used = set()
        if asset_id in used:
            return jsonify(ok=False, error='The receipt design is locked and uses this image. Unlock it first.'), 409
    path = asset.file_path
    db.session.delete(asset)
    db.session.commit()
    from blueprints.school.report_card_data import forget_design
    from blueprints.finance.helpers import forget_receipt_design
    forget_design(); forget_receipt_design()
    delete_upload(path)
    audit_log('id_card_asset_removed', 'school', 'id_card_asset', asset_id, {})
    return jsonify(ok=True)


# ---------------------------------------------------------------- the public page a card's QR code opens
def _card_by_token(token):
    if not token or len(token) > 64:
        return None
    return db.session.scalars(select(IdCard).where(IdCard.token == token)).first()


def _holder(card):
    if card.kind == 'student':
        row = one(select(Student.first_name, Student.middle_name, Student.last_name, Student.photo_path, Student.active,
                         Student.archived_at).where(Student.id == card.person_id))
        if not row:
            return None
        current = _school_current_session()
        class_name = one(select(SchoolClass.name).join(StudentEnrolment, StudentEnrolment.class_id == SchoolClass.id)
                         .where(StudentEnrolment.student_id == card.person_id, StudentEnrolment.active == 1,
                                StudentEnrolment.session_id == (current['id'] if current else 0)))
        return {'name': ' '.join(p for p in (row['first_name'], row['middle_name'], row['last_name']) if p),
                'photo_path': row['photo_path'], 'detail': (class_name['name'] if class_name else ''),
                'current': bool(row['active'] and not row['archived_at']), 'label': 'Student'}
    row = one(select(Admin.display_name, Admin.photo_path, Admin.active, AdminType.name.label('role'))
              .outerjoin(AdminType, AdminType.id == Admin.admin_type_id).where(Admin.id == card.person_id))
    if not row:
        return None
    return {'name': row['display_name'], 'photo_path': row['photo_path'], 'detail': row['role'] or '',
            'current': bool(row['active']), 'label': 'Staff'}


def _private(response):
    response.headers['Cache-Control'] = 'no-store, private'
    response.headers['X-Robots-Tag'] = 'noindex, nofollow'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


@app.route('/id/<token>')
def id_card_verify(token):
    card = _card_by_token(token)
    holder = _holder(card) if card else None
    valid = bool(card and holder and card.status == 'active' and holder['current'])
    response = make_response(render_template('id_card_verify.html', card=card, holder=holder, valid=valid), 200 if card else 404)
    return _private(response)


@app.route('/id/<token>/photo')
def id_card_photo(token):
    card = _card_by_token(token)
    holder = _holder(card) if card and card.status == 'active' else None
    if not holder or not holder['current'] or not holder['photo_path']:
        abort(404)
    data = read_upload_bytes(holder['photo_path'])
    if data is None:
        abort(404)
    response = make_response(data)
    response.headers['Content-Type'] = mimetypes.guess_type(holder['photo_path'])[0] or 'application/octet-stream'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return _private(response)


def id_cards_shown():
    admin = current_admin()
    if not (admin and admin_has_permission(admin['id'], 'id_cards.print')):
        return False
    reach = _teacher_reach(admin['id'])
    return reach is None or bool(reach)


app.jinja_env.globals.update(id_cards_shown=id_cards_shown)
