"""ID cards: printing them for students and staff, the school's card designer, cancelling a lost card, and the
public page a card's QR code opens. The design rules and the QR code are core/id_cards.py.

Who may do what. Printing needs id_cards.print (and, for students, the classes the person may see); designing,
locking, cancelling and replacing need id_cards.manage. A locked design cannot be saved or reset until someone with
id_cards.manage unlocks it. Every save, lock, print, cancel and replacement is in the activity log.

The verification page (/id/<token>) needs no sign-in: it answers only for a real card's random token, shows only the
school, the name, the photo, the class or role and whether the card is valid, and is never cached or indexed.
"""

import json
import mimetypes
from datetime import datetime, timezone

from flask import abort, flash, jsonify, make_response, redirect, render_template, request, url_for
from sqlalchemy import and_, func, select

from app import _school_current_session, app
from blueprints.school.helpers import _school_class_allowed
from blueprints.school.report_card_data import report_settings
from core import id_cards as ic
from core.branding import school_brand
from core.db_helpers import all_rows, one, tuples
from core.security import admin_has_permission, admin_required, audit_log, csrf_protect, current_admin
from core.storage import read_upload_bytes
from models import (
    AcademicSession, Admin, AdminType, IdCard, IdCardDesign, SchoolClass, Student, StudentEnrolment, db,
)

PRINT_LIMIT = 300


def _now():
    return datetime.now(timezone.utc).isoformat()


def _kind(value):
    return value if value in ic.KINDS else 'student'


def _design_row(kind):
    return db.session.scalars(select(IdCardDesign).where(IdCardDesign.kind == kind)).first()


def _layout(kind):
    row = _design_row(kind)
    if row:
        try:
            return ic.clean_layout(row.layout_json, kind)
        except ValueError:
            app.logger.exception('A stored ID card design could not be read; using the default')
    return ic.default_layout(kind)


def _static(path):
    return url_for('static', filename=path) if path else None


def _signature_url():
    path = (report_settings().get('head_signature') or '').strip()
    return _static(path)


# ---------------------------------------------------------------- the people
def _students(class_id=None, ids=None):
    current = _school_current_session()
    if not current:
        return []
    stmt = (select(Student.id, Student.first_name, Student.middle_name, Student.last_name, Student.admission_no,
                   Student.gender, Student.blood_group, Student.guardian_phone, Student.department, Student.photo_path,
                   SchoolClass.id.label('class_id'), SchoolClass.name.label('class_name'),
                   AcademicSession.name.label('session_name'))
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
    allowed = {}
    out = []
    for r in all_rows(stmt):
        if r['class_id'] not in allowed:
            allowed[r['class_id']] = _school_class_allowed(me['id'], r['class_id'])
        if allowed[r['class_id']]:
            out.append(dict(r))
    return out


def _staff(ids=None):
    stmt = (select(Admin.id, Admin.display_name, Admin.phone, Admin.email, Admin.photo_path, AdminType.name.label('role'))
            .outerjoin(AdminType, AdminType.id == Admin.admin_type_id)
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


def _card_view(kind, person, card, brand, photo_url=None):
    data = ic.card_data(kind, person, brand, card.serial if card else ic.serial_for(kind, 0), card.issued_at if card else _now())
    name = data.get('name') or ''
    verify = url_for('id_card_verify', token=card.token, _external=True) if card else 'SAMPLE'
    return {'data': data, 'photo_url': photo_url if photo_url is not None else _static(person.get('photo_path')),
            'qr': ic.qr_path(verify), 'initials': ''.join(p[:1] for p in name.split()[:2]).upper()}


# ---------------------------------------------------------------- the list, and printing
@app.route('/admin/id-cards')
@admin_required
def admin_id_cards():
    me = current_admin()
    kind = _kind(request.args.get('kind'))
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
                                           if _school_class_allowed(me['id'], c['id'])]
    design = _design_row(kind)
    return render_template('school_id_cards.html', kind=kind, people=people, cards=cards, classes=classes,
                           class_id=class_id, q=q, design=design, limit=PRINT_LIMIT,
                           can_manage=admin_has_permission(me['id'], 'id_cards.manage'))


@app.post('/admin/id-cards/print')
@admin_required
@csrf_protect
def admin_id_cards_print():
    me = current_admin()
    kind = _kind(request.form.get('kind'))
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
    layout = _layout(kind)
    views = [_card_view(kind, p, cards[p['id']], brand) for p in people]
    db.session.commit()
    response = make_response(render_template('school_id_card_sheet.html', layout=layout, views=views, kind=kind,
                                             backs=request.form.get('backs') == '1', logo_url=brand.get('logo_url'),
                                             signature_url=_signature_url(), brand=brand))
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
    people = _students(ids=[card.person_id]) if card.kind == 'student' else _staff(ids=[card.person_id])
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
    brand = school_brand()
    row = _design_row(kind)
    view = _card_view(kind, _sample(kind), None, brand)
    view['qr'] = ic.qr_path('https://example.com/id/sample')
    return render_template('school_id_card_designer.html', kind=kind, layout=_layout(kind), design=row, view=view,
                           fields=ic.fields_for(kind), brand=brand, logo_url=brand.get('logo_url'),
                           signature_url=_signature_url(), limits={'elements': ic.MAX_ELEMENTS, 'text': ic.MAX_TEXT})


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
        layout = ic.clean_layout(request.form.get('layout', ''), kind)
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    if row is None:
        row = IdCardDesign(kind=kind, layout_json='{}', updated_at=_now())
        db.session.add(row)
    row.layout_json, row.updated_at, row.updated_by = json.dumps(layout, separators=(',', ':')), _now(), me['id']
    db.session.commit()
    audit_log('id_card_design_saved', 'school', 'id_card_design', kind,
              {'front': len(layout['sides']['front']), 'back': len(layout['sides']['back']), 'orientation': layout['orientation']})
    return jsonify(ok=True, layout=layout, saved_at=row.updated_at)


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
    if row is None:
        row = IdCardDesign(kind=kind, layout_json='{}', updated_at=_now())
        db.session.add(row)
    row.layout_json, row.updated_at, row.updated_by = json.dumps(ic.default_layout(kind)), _now(), me['id']
    db.session.commit()
    audit_log('id_card_design_reset', 'school', 'id_card_design', kind, {})
    flash('The design is back to the standard one.', 'success')
    return redirect(url_for('admin_id_card_designer', kind=kind))


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
    return bool(admin and admin_has_permission(admin['id'], 'id_cards.print'))


app.jinja_env.globals.update(id_cards_shown=id_cards_shown)
