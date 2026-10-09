"""Report cards: the staff side.

Staff see which cards are ready and which are waiting on a result, write the class teacher's
comments, keep their own signature, and (with permission) set the head's title, name and signature.
Students and parents get their own cards from blueprints/student_portal/report_cards.py and
blueprints/parents/report_cards.py; all three draw the same card from blueprints/school/report_card_data.py.

Who may do what is the permission catalogue's job (core/security.py): ``report_cards.view`` to see and
download, ``report_cards.comment`` to write comments, rate affective/psychomotor traits and keep a
signature, ``report_cards.manage`` for the head's details. A staff member who is limited to some
classes only ever sees those classes.
"""

import json
import re
from datetime import datetime, timezone

from flask import Response, abort, flash, jsonify, redirect, render_template, request, url_for
from sqlalchemy import select

from app import ACADEMIC_TERMS, _release_due_school_results, _school_current_session, app
from blueprints.school.helpers import _school_class_allowed
from blueprints.school.report_card_data import (
    HEAD_TITLES, MAX_COMMENT_LENGTH, SETTING_HEAD_NAME, SETTING_HEAD_SIGNATURE, SETTING_HEAD_TITLE,
    SETTING_NEXT_TERM, TRAIT_GROUPS, TRAIT_SCALE, _parse_ratings, build_card, build_cards, card_for_web, class_overview,
    report_settings, save_report_settings, term_from_slug, term_slug, traits_overview,
)
from blueprints.finance.helpers import _save_signature_data_url
from core.report_card_pdf import render_report_cards_pdf
from core.security import admin_required, audit_log, csrf_protect, current_admin
from core.storage import delete_upload, upload_exists
from core.uploads import _save_image_upload
from models import (
    AcademicSession, Admin, ReportCardComment, ReportCardTrait, SchoolClass, Student, StudentEnrolment, db,
)

TERMS = (*ACADEMIC_TERMS, 'Full Session')


# ---------------------------------------------------------------------------------------------
# Small helpers
def _my_classes():
    """The classes the signed-in staff member may work with, in class order."""
    me = current_admin()
    classes = db.session.scalars(select(SchoolClass).where(SchoolClass.active == 1).order_by(SchoolClass.level_order)).all()
    return [c for c in classes if _school_class_allowed(me['id'], c.id)]


def _sessions():
    return db.session.scalars(select(AcademicSession).where(AcademicSession.active == 1).order_by(AcademicSession.id.desc())).all()


def _choice():
    """The class, session and term picked on the page (query string), each checked against what exists
    and what this staff member may see. Returns ``(classes, sessions, class_row, session_row, term)``."""
    classes, sessions = _my_classes(), _sessions()
    class_row = next((c for c in classes if c.id == request.values.get('class_id', type=int)), None)
    current = _school_current_session()
    wanted = request.values.get('session_id', type=int) or (current['id'] if current else None)
    session_row = next((s for s in sessions if s.id == wanted), sessions[0] if sessions else None)
    term = request.values.get('term', '').strip()
    term = term if term in TERMS else ACADEMIC_TERMS[0]
    return classes, sessions, class_row, session_row, term


def _class_of(student_id, session_id):
    """The class a student was in during a session, or None."""
    return db.session.scalar(select(StudentEnrolment.class_id).where(
        StudentEnrolment.student_id == student_id, StudentEnrolment.session_id == session_id, StudentEnrolment.active == 1))


def _may_see(student_id, session_id):
    """Whether the signed-in staff member may open this student's card: the class scope decides.

    A student or session this school does not have is never visible (so a number from another school is a 404)."""
    if db.session.get(Student, student_id) is None or db.session.get(AcademicSession, session_id) is None:
        return False
    me = current_admin()
    class_id = _class_of(student_id, session_id)
    if class_id is None:
        return bool(me['admin_type_system'])
    return _school_class_allowed(me['id'], class_id)


def _file_name(card):
    stem = f"Report-Card-{card['student']['name']}-{card['term']}-{card['session']}"
    return re.sub(r'[^A-Za-z0-9._-]+', '-', stem).strip('-')[:120] + '.pdf'


def _pdf_response(cards, name):
    response = Response(render_report_cards_pdf(cards), mimetype='application/pdf')
    response.headers['Content-Disposition'] = f'inline; filename="{name}"'
    response.headers['Cache-Control'] = 'no-store'
    return response


def _remove_file(stored):
    delete_upload(stored or '')


def signature_action(current_stored):
    """Carry out a "draw", "upload" or "remove" request from a signature form.

    Returns ``(new stored path, message)`` where the path is '' when the signature was removed. The
    file it replaces is deleted only after the new one is safely saved. Raises ValueError, with a
    message safe to show, for anything it cannot use.
    """
    action = request.form.get('action', '').strip()
    if action == 'remove':
        _remove_file(current_stored)
        return '', 'Signature removed.'
    if action == 'draw':
        stored = _save_signature_data_url(request.form.get('signature_data_url', ''))
    elif action == 'upload':
        stored = _save_image_upload(request.files.get('signature_file'), 'signatures', 'signature')
        if not stored:
            raise ValueError('Choose an image file to upload.')
    else:
        raise ValueError('Unrecognised action.')
    _remove_file(current_stored)
    return stored, 'Signature saved.'


# ---------------------------------------------------------------------------------------------
# Which cards are ready, and downloading them
@app.route('/admin/school/report-cards')
@admin_required
def admin_school_report_cards():
    """The page itself. The class, session and term pickers sit across the top; the students are
    fetched in-page (``admin_school_report_card_students``) so changing a picker never reloads it."""
    classes, sessions, class_row, session_row, term = _choice()
    me = current_admin()
    mine = db.session.scalar(select(Admin.signature_path).where(Admin.id == me['id']))
    return render_template(
        'admin_school_report_cards.html', classes=classes, sessions=sessions, terms=TERMS, class_row=class_row,
        session_row=session_row, term=term, settings=report_settings(), max_length=MAX_COMMENT_LENGTH,
        trait_groups=[{'name': name, 'items': [{'key': key, 'label': label} for key, label in items]}
                      for name, items in TRAIT_GROUPS],
        trait_scale=[{'value': value, 'label': label} for value, label in TRAIT_SCALE],
        signature_path=mine if upload_exists(mine or '') else '', open_signature=request.args.get('signature') == '1')


@app.get('/admin/school/report-cards/students')
@admin_required
def admin_school_report_card_students():
    """One class's students for a session and term, as JSON for the report cards page: the state of
    each card, the class teacher's comment and the trait ratings, all in one round trip."""
    _release_due_school_results()
    db.session.commit()
    classes, sessions, class_row, session_row, term = _choice()
    if not class_row or not session_row:
        return jsonify({'error': 'Choose a class, a session and a term.'}), 400
    slug = term_slug(term)
    overview = class_overview(class_row.id, session_row.id, term)
    traits = {t['student_id']: t for t in traits_overview(class_row.id, session_row.id, term)}
    students = []
    for r in overview:
        t = traits.get(r['student_id'], {})
        ratings = t.get('ratings', {})
        ready = r['state'] == 'ready'
        students.append({
            **r, 'ratings': ratings, 'rated_by': t.get('rated_by', ''),
            'view_url': url_for('admin_school_report_card_view', student_id=r['student_id'], session_id=session_row.id, slug=slug) if ready else '',
            'pdf_url': url_for('admin_school_report_card_pdf', student_id=r['student_id'], session_id=session_row.id, slug=slug) if ready else ''})
    ready_count = sum(1 for r in overview if r['state'] == 'ready')
    return jsonify({
        'class': {'id': class_row.id, 'name': class_row.name}, 'session': {'id': session_row.id, 'name': session_row.name},
        'term': term, 'total': len(students), 'ready': ready_count,
        'trait_total': sum(len(items) for _, items in TRAIT_GROUPS),
        'class_pdf_url': url_for('admin_school_report_cards_class_pdf', class_id=class_row.id, session_id=session_row.id, term=term)
        if ready_count else '',
        'students': students})


@app.route('/admin/school/report-cards/<int:student_id>/<int:session_id>/<slug>')
@admin_required
def admin_school_report_card_view(student_id, session_id, slug):
    term = term_from_slug(slug)
    if term is None or not _may_see(student_id, session_id):
        abort(404)
    _release_due_school_results()
    db.session.commit()
    card = build_card(student_id, session_id, term)
    if card is None:
        flash("That report card is not ready: every one of the student's results for the term has to be released first.", 'error')
        return redirect(url_for('admin_school_report_cards', session_id=session_id, term=term,
                                class_id=_class_of(student_id, session_id) or ''))
    return render_template('report_card_page.html', card=card_for_web(card), back_url=url_for(
        'admin_school_report_cards', session_id=session_id, term=term, class_id=_class_of(student_id, session_id) or ''),
        back_label='Back to report cards',
        pdf_url=url_for('admin_school_report_card_pdf', student_id=student_id, session_id=session_id, slug=slug))


@app.route('/admin/school/report-cards/<int:student_id>/<int:session_id>/<slug>/pdf')
@admin_required
def admin_school_report_card_pdf(student_id, session_id, slug):
    term = term_from_slug(slug)
    if term is None or not _may_see(student_id, session_id):
        abort(404)
    _release_due_school_results()
    db.session.commit()
    card = build_card(student_id, session_id, term)
    if card is None:
        abort(404)
    audit_log('report_card_downloaded', 'school', 'student', student_id, {'term': term, 'session_id': session_id})
    return _pdf_response([card], _file_name(card))


@app.route('/admin/school/report-cards/class.pdf')
@admin_required
def admin_school_report_cards_class_pdf():
    _release_due_school_results()
    db.session.commit()
    classes, sessions, class_row, session_row, term = _choice()
    if not class_row or not session_row:
        abort(404)
    ids = [r['student_id'] for r in class_overview(class_row.id, session_row.id, term) if r['state'] == 'ready']
    cards = build_cards(ids, session_row.id, term)
    if not cards:
        flash('No report card in this class is ready for this term yet.', 'error')
        return redirect(url_for('admin_school_report_cards', class_id=class_row.id, session_id=session_row.id, term=term))
    audit_log('report_cards_class_downloaded', 'school', 'class', class_row.id,
              {'term': term, 'session_id': session_row.id, 'cards': len(cards)})
    name = re.sub(r'[^A-Za-z0-9._-]+', '-', f'Report-Cards-{class_row.name}-{term}-{session_row.name}').strip('-') + '.pdf'
    return _pdf_response(cards, name)


# ---------------------------------------------------------------------------------------------
# The class teacher's comment and trait ratings: one student at a time, from a small window on the
# report cards page. The old stand-alone pages now simply lead back to that page.
def _display_name(admin_id):
    return (db.session.scalar(select(Admin.display_name).where(Admin.id == admin_id)) or '') if admin_id else ''


def _student_target():
    """The student, session and term a comment or rating is being saved for, as
    ``((student_id, session_id, term, class_id), '')`` or ``(None, reason)``.

    The same rule as opening the card: the student must be in a class this staff member may work
    with for that session, so a number from another class (or another school) is refused."""
    student_id = request.form.get('student_id', type=int)
    session_id = request.form.get('session_id', type=int)
    term = request.form.get('term', '').strip()
    if not student_id or not session_id or term not in TERMS:
        return None, 'Choose a class, a session and a term first.'
    class_id = _class_of(student_id, session_id)
    if class_id is None or not _may_see(student_id, session_id):
        return None, 'That student is not in one of your classes for this session.'
    return (student_id, session_id, term, class_id), ''


def _kept_filters():
    return {k: v for k, v in request.args.items() if k in ('class_id', 'session_id', 'term')}


@app.route('/admin/school/report-cards/comments')
@admin_required
def admin_school_report_card_comments():
    return redirect(url_for('admin_school_report_cards', **_kept_filters()))


@app.post('/admin/school/report-cards/comments')
@admin_required
@csrf_protect
def admin_school_report_card_comments_save():
    """Save (or clear) one student's class teacher comment. Answers with JSON for the page's window."""
    me = current_admin()
    target, error = _student_target()
    if target is None:
        return jsonify({'error': error}), 400
    student_id, session_id, term, class_id = target
    # A browser sends a line break as CRLF; keep them all as LF so the same comment always compares equal.
    text = re.sub(r'[ \t]+\n', '\n', request.form.get('comment', '').replace('\r\n', '\n').replace('\r', '\n').strip())
    if len(text) > MAX_COMMENT_LENGTH:
        return jsonify({'error': f'A comment can be at most {MAX_COMMENT_LENGTH} characters.'}), 400
    old = db.session.scalars(select(ReportCardComment).where(
        ReportCardComment.student_id == student_id, ReportCardComment.session_id == session_id,
        ReportCardComment.term == term)).first()
    now = datetime.now(timezone.utc).isoformat()
    if old is not None and text == old.comment.replace('\r\n', '\n').strip():
        # nothing changed: saving never takes over someone else's comment
        return jsonify({'ok': True, 'comment': old.comment, 'comment_by': _display_name(old.author_admin_id)})
    if not text:
        if old is not None:
            db.session.delete(old)
    elif old is None:
        db.session.add(ReportCardComment(student_id=student_id, session_id=session_id, term=term, comment=text,
                                         author_admin_id=me['id'], created_at=now, updated_at=now))
    else:
        old.comment, old.author_admin_id, old.updated_at = text, me['id'], now
    db.session.commit()
    audit_log('report_card_comments_saved', 'school', 'class', class_id,
              {'term': term, 'session_id': session_id, 'student_id': student_id, 'saved': int(bool(text)), 'cleared': int(not text)})
    return jsonify({'ok': True, 'comment': text, 'comment_by': me['display_name'] if text else ''})


@app.route('/admin/school/report-cards/traits')
@admin_required
def admin_school_report_card_traits():
    return redirect(url_for('admin_school_report_cards', **_kept_filters()))


@app.post('/admin/school/report-cards/traits')
@admin_required
@csrf_protect
def admin_school_report_card_traits_save():
    """Save (or clear) one student's trait ratings. Answers with JSON for the page's window."""
    me = current_admin()
    target, error = _student_target()
    if target is None:
        return jsonify({'error': error}), 400
    student_id, session_id, term, class_id = target
    ratings = {}
    for _, items in TRAIT_GROUPS:
        for key, _label in items:
            value = request.form.get(f'trait_{key}', '').strip()
            if value.isdigit() and 1 <= int(value) <= 5:
                ratings[key] = int(value)
    new_json = json.dumps(ratings, sort_keys=True)
    old = db.session.scalars(select(ReportCardTrait).where(
        ReportCardTrait.student_id == student_id, ReportCardTrait.session_id == session_id,
        ReportCardTrait.term == term)).first()
    now = datetime.now(timezone.utc).isoformat()
    if old is not None and json.dumps(_parse_ratings(old.ratings), sort_keys=True) == new_json:
        # nothing changed: saving never takes over someone else's ratings
        return jsonify({'ok': True, 'ratings': ratings, 'rated_by': _display_name(old.author_admin_id)})
    if not ratings:
        if old is not None:
            db.session.delete(old)
    elif old is None:
        db.session.add(ReportCardTrait(student_id=student_id, session_id=session_id, term=term, ratings=new_json,
                                       author_admin_id=me['id'], created_at=now, updated_at=now))
    else:
        old.ratings, old.author_admin_id, old.updated_at = new_json, me['id'], now
    db.session.commit()
    audit_log('report_card_traits_saved', 'school', 'class', class_id,
              {'term': term, 'session_id': session_id, 'student_id': student_id, 'saved': int(bool(ratings)), 'cleared': int(not ratings)})
    return jsonify({'ok': True, 'ratings': ratings, 'rated_by': me['display_name'] if ratings else ''})


# ---------------------------------------------------------------------------------------------
# A staff member's own signature: set from a window on the report cards page
@app.route('/admin/school/report-cards/my-signature')
@admin_required
def admin_my_signature():
    return redirect(url_for('admin_school_report_cards', signature=1))


@app.post('/admin/school/report-cards/my-signature')
@admin_required
@csrf_protect
def admin_my_signature_save():
    me = current_admin()
    admin = db.session.get(Admin, me['id'])
    try:
        admin.signature_path, message = signature_action(admin.signature_path)
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'error')
        return redirect(url_for('admin_school_report_cards', signature=1))
    db.session.commit()
    audit_log('report_card_signature_updated', 'school', 'admin', me['id'], {'action': request.form.get('action')})
    flash(message, 'success')
    return redirect(url_for('admin_school_report_cards'))


# ---------------------------------------------------------------------------------------------
# The head's title, name and signature, and when the next term begins
@app.route('/admin/school/report-cards/settings')
@admin_required
def admin_school_report_card_settings():
    settings = report_settings()
    signature = settings['head_signature'] if upload_exists(settings['head_signature']) else ''
    return render_template('admin_school_report_card_settings.html', settings=settings, signature_path=signature,
                           titles=HEAD_TITLES, errors=[])


@app.post('/admin/school/report-cards/settings')
@admin_required
@csrf_protect
def admin_school_report_card_settings_save():
    me = current_admin()
    settings = report_settings()
    errors = []
    try:
        if request.form.get('action') in ('draw', 'upload', 'remove'):
            stored, message = signature_action(settings['head_signature'])
            save_report_settings({SETTING_HEAD_SIGNATURE: stored}, me['id'])
        else:
            title = request.form.get('head_title', '').strip()
            custom = ' '.join(request.form.get('head_title_custom', '').split())
            title = custom if title == '__other__' else title
            name = ' '.join(request.form.get('head_name', '').split())
            next_term = ' '.join(request.form.get('next_term_begins', '').split())
            if len(title) > 40:
                errors.append('The title can be at most 40 characters.')
            if len(name) > 80:
                errors.append('The name can be at most 80 characters.')
            if len(next_term) > 80:
                errors.append('"Next term begins" can be at most 80 characters.')
            if not errors:
                save_report_settings({SETTING_HEAD_TITLE: title, SETTING_HEAD_NAME: name, SETTING_NEXT_TERM: next_term}, me['id'])
            message = 'Report card settings saved.'
    except ValueError as exc:
        errors.append(str(exc))
    if errors:
        db.session.rollback()
        return render_template('admin_school_report_card_settings.html', settings=settings,
                               signature_path=settings['head_signature'] if upload_exists(settings['head_signature']) else '',
                               titles=HEAD_TITLES, errors=errors), 400
    db.session.commit()
    audit_log('report_card_settings_updated', 'school', 'school_setting', 'report_cards', {'action': request.form.get('action') or 'details'})
    flash(message, 'success')
    return redirect(url_for('admin_school_report_card_settings'))
