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
import os
import re
from datetime import datetime, timezone

from flask import Response, abort, flash, redirect, render_template, request, url_for
from sqlalchemy import select

from app import ACADEMIC_TERMS, _release_due_school_results, _school_current_session, app
from blueprints.school.helpers import _school_class_allowed
from blueprints.school.report_card_data import (
    HEAD_TITLES, MAX_COMMENT_LENGTH, SETTING_HEAD_NAME, SETTING_HEAD_SIGNATURE, SETTING_HEAD_TITLE,
    SETTING_NEXT_TERM, TRAIT_GROUPS, _parse_ratings, build_card, build_cards, card_for_web, class_overview,
    report_settings, save_report_settings, term_from_slug, term_slug, traits_overview,
)
from blueprints.finance.helpers import _save_signature_data_url
from core.report_card_pdf import render_report_cards_pdf
from core.security import admin_required, audit_log, csrf_protect, current_admin
from core.storage import stored_upload_path
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
    found = stored_upload_path(stored or '')
    if found and os.path.isfile(found):
        try:
            os.remove(found)
        except OSError:
            pass


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
    _release_due_school_results()
    db.session.commit()
    classes, sessions, class_row, session_row, term = _choice()
    rows = class_overview(class_row.id, session_row.id, term) if class_row and session_row else []
    settings = report_settings()
    return render_template(
        'admin_school_report_cards.html', classes=classes, sessions=sessions, terms=TERMS, class_row=class_row,
        session_row=session_row, term=term, rows=rows, slug=term_slug(term), settings=settings,
        ready=sum(1 for r in rows if r['state'] == 'ready'),
        missing_comments=sum(1 for r in rows if r['state'] != 'none' and not r['comment'].strip()))


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
# The class teacher's comments
@app.route('/admin/school/report-cards/comments')
@admin_required
def admin_school_report_card_comments():
    classes, sessions, class_row, session_row, term = _choice()
    rows = class_overview(class_row.id, session_row.id, term) if class_row and session_row else []
    me = current_admin()
    mine = db.session.scalar(select(Admin.signature_path).where(Admin.id == me['id']))
    return render_template('admin_school_report_card_comments.html', classes=classes, sessions=sessions, terms=TERMS,
                           class_row=class_row, session_row=session_row, term=term, rows=rows,
                           max_length=MAX_COMMENT_LENGTH, has_signature=bool(stored_upload_path(mine or '')))


@app.post('/admin/school/report-cards/comments')
@admin_required
@csrf_protect
def admin_school_report_card_comments_save():
    me = current_admin()
    classes, sessions, class_row, session_row, term = _choice()
    if not class_row or not session_row:
        flash('Choose a class, a session and a term first.', 'error')
        return redirect(url_for('admin_school_report_card_comments'))
    enrolled = {r['student_id']: r for r in class_overview(class_row.id, session_row.id, term)}
    existing = {c.student_id: c for c in db.session.scalars(select(ReportCardComment).where(
        ReportCardComment.session_id == session_row.id, ReportCardComment.term == term,
        ReportCardComment.student_id.in_(list(enrolled) or [0])))}
    now = datetime.now(timezone.utc).isoformat()
    saved = cleared = 0
    too_long = []
    for student_id, row in enrolled.items():
        field = f'comment_{student_id}'
        if field not in request.form:
            continue
        # A browser sends a line break as CRLF; keep them all as LF so the same comment always compares equal.
        text = re.sub(r'[ \t]+\n', '\n', request.form.get(field, '').replace('\r\n', '\n').replace('\r', '\n').strip())
        old = existing.get(student_id)
        if text == (old.comment.replace('\r\n', '\n').strip() if old else ''):
            continue        # nothing changed: saving the page never takes over someone else's comment
        if len(text) > MAX_COMMENT_LENGTH:
            too_long.append(row['name'])
            continue
        if not text:
            if old is not None:
                db.session.delete(old)
                cleared += 1
        elif old is None:
            db.session.add(ReportCardComment(student_id=student_id, session_id=session_row.id, term=term, comment=text,
                                             author_admin_id=me['id'], created_at=now, updated_at=now))
            saved += 1
        else:
            old.comment, old.author_admin_id, old.updated_at = text, me['id'], now
            saved += 1
    if too_long:
        db.session.rollback()
        flash(f'A comment can be at most {MAX_COMMENT_LENGTH} characters. Nothing was saved: shorten the comment for '
              + ', '.join(too_long[:5]) + ('…' if len(too_long) > 5 else '') + '.', 'error')
    else:
        db.session.commit()
        if saved or cleared:
            audit_log('report_card_comments_saved', 'school', 'class', class_row.id,
                      {'term': term, 'session_id': session_row.id, 'saved': saved, 'cleared': cleared})
        flash(f'{saved} comment{"" if saved == 1 else "s"} saved' + (f', {cleared} cleared' if cleared else '') + '.'
              if (saved or cleared) else 'No comment was changed.', 'success')
    return redirect(url_for('admin_school_report_card_comments', class_id=class_row.id, session_id=session_row.id, term=term))


# ---------------------------------------------------------------------------------------------
# Affective and psychomotor traits (same class teacher, same class/session/term)
@app.route('/admin/school/report-cards/traits')
@admin_required
def admin_school_report_card_traits():
    classes, sessions, class_row, session_row, term = _choice()
    rows = traits_overview(class_row.id, session_row.id, term) if class_row and session_row else []
    return render_template('admin_school_report_card_traits.html', classes=classes, sessions=sessions, terms=TERMS,
                           class_row=class_row, session_row=session_row, term=term, rows=rows, trait_groups=TRAIT_GROUPS)


@app.post('/admin/school/report-cards/traits')
@admin_required
@csrf_protect
def admin_school_report_card_traits_save():
    me = current_admin()
    classes, sessions, class_row, session_row, term = _choice()
    if not class_row or not session_row:
        flash('Choose a class, a session and a term first.', 'error')
        return redirect(url_for('admin_school_report_card_traits'))
    enrolled = {r['student_id'] for r in traits_overview(class_row.id, session_row.id, term)}
    existing = {t.student_id: t for t in db.session.scalars(select(ReportCardTrait).where(
        ReportCardTrait.session_id == session_row.id, ReportCardTrait.term == term,
        ReportCardTrait.student_id.in_(list(enrolled) or [0])))}
    trait_keys = {key for _, items in TRAIT_GROUPS for key, _ in items}
    now = datetime.now(timezone.utc).isoformat()
    saved = cleared = 0
    for student_id in enrolled:
        ratings = {}
        for key in trait_keys:
            value = request.form.get(f'trait_{student_id}_{key}', '').strip()
            if value.isdigit() and 1 <= int(value) <= 5:
                ratings[key] = int(value)
        new_json = json.dumps(ratings, sort_keys=True)
        old = existing.get(student_id)
        if old is not None and json.dumps(_parse_ratings(old.ratings), sort_keys=True) == new_json:
            continue      # nothing changed: saving the page never takes over someone else's ratings
        if not ratings:
            if old is not None:
                db.session.delete(old)
                cleared += 1
        elif old is None:
            db.session.add(ReportCardTrait(student_id=student_id, session_id=session_row.id, term=term,
                                           ratings=new_json, author_admin_id=me['id'], created_at=now, updated_at=now))
            saved += 1
        else:
            old.ratings, old.author_admin_id, old.updated_at = new_json, me['id'], now
            saved += 1
    db.session.commit()
    if saved or cleared:
        audit_log('report_card_traits_saved', 'school', 'class', class_row.id,
                  {'term': term, 'session_id': session_row.id, 'saved': saved, 'cleared': cleared})
    flash(f'{saved} student{"" if saved == 1 else "s"} updated' + (f', {cleared} cleared' if cleared else '') + '.'
          if (saved or cleared) else 'No rating was changed.', 'success')
    return redirect(url_for('admin_school_report_card_traits', class_id=class_row.id, session_id=session_row.id, term=term))


# ---------------------------------------------------------------------------------------------
# A staff member's own signature
@app.route('/admin/school/report-cards/my-signature')
@admin_required
def admin_my_signature():
    me = current_admin()
    stored = db.session.scalar(select(Admin.signature_path).where(Admin.id == me['id']))
    current = stored if stored_upload_path(stored or '') else ''
    return render_template('admin_report_card_signature.html', signature_path=current, errors=[])


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
        return render_template('admin_report_card_signature.html', signature_path=admin.signature_path or '',
                               errors=[str(exc)]), 400
    db.session.commit()
    audit_log('report_card_signature_updated', 'school', 'admin', me['id'], {'action': request.form.get('action')})
    flash(message, 'success')
    return redirect(url_for('admin_my_signature'))


# ---------------------------------------------------------------------------------------------
# The head's title, name and signature, and when the next term begins
@app.route('/admin/school/report-cards/settings')
@admin_required
def admin_school_report_card_settings():
    settings = report_settings()
    signature = settings['head_signature'] if stored_upload_path(settings['head_signature']) else ''
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
                               signature_path=settings['head_signature'] if stored_upload_path(settings['head_signature']) else '',
                               titles=HEAD_TITLES, errors=errors), 400
    db.session.commit()
    audit_log('report_card_settings_updated', 'school', 'school_setting', 'report_cards', {'action': request.form.get('action') or 'details'})
    flash(message, 'success')
    return redirect(url_for('admin_school_report_card_settings'))
