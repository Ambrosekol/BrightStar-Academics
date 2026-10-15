"""Settings > Results & report cards > Grading and scores: each level's grading scheme for a session (core/grading.py).

One form holds the four levels (Nursery, Primary, JSS, SSS), each with its score split (the Exam and up to four CA
parts, adding up to 100) and its grade scale. Saving checks every level and saves all four together, for the chosen
session only: every other session keeps the schemes it has (core/grading.save writes them down first), so a report
card of an earlier session never changes.

Needs school.grading.manage (the Head Teacher and the School Academic Administrator; the School Admin always).
"""

from flask import flash, redirect, render_template, request, url_for
from sqlalchemy import select

from app import _active_sessions, _school_current_session, app
from core import grading
from core.security import admin_required, audit_log, csrf_protect, current_admin
from models import AcademicSession, db


def _from_form(form, level):
    parts = [{'label': label, 'from': source, 'max': mark} for label, source, mark in zip(
        form.getlist(f'{level}-part-label'), form.getlist(f'{level}-part-from'), form.getlist(f'{level}-part-max'))]
    scale = [[low, g, remark] for low, g, remark in zip(
        form.getlist(f'{level}-band-min'), form.getlist(f'{level}-band-grade'), form.getlist(f'{level}-band-remark'))]
    return {'exam_max': form.get(f'{level}-exam', ''), 'parts': parts, 'scale': scale}


@app.route('/admin/school/grading', methods=['GET', 'POST'])
@admin_required
@csrf_protect
def admin_school_grading():
    me = current_admin()
    sessions = _active_sessions()
    wanted = request.values.get('session_id', type=int)
    chosen = next((s for s in sessions if s.id == wanted), None)
    if chosen is None:
        current = _school_current_session()
        chosen = next((s for s in sessions if current and s.id == current['id']), None) or (sessions[0] if sessions else None)
    if chosen is None:
        flash('Create an academic session first.', 'error')
        return redirect(url_for('admin_school_sessions'))
    errors, form_schemes = [], None
    if request.method == 'POST':
        raw = {level: _from_form(request.form, level) for level, _ in grading.LEVELS}
        for level, name in grading.LEVELS:
            try:
                grading.clean(raw[level])
            except ValueError as exc:
                errors.append(f'{name}: {exc}')
        if not errors:
            saved = grading.save(chosen.id, raw, me['id'])
            audit_log('grading_schemes_saved', 'school', 'academic_session', chosen.id,
                      {level: {'exam': s['exam_max'], 'parts': [(p['label'], p['max'], p['from']) for p in s['parts']],
                               'scale': [b[1] for b in s['scale']]} for level, s in saved.items()}, commit=False)
            db.session.commit()
            grading.forget()
            flash(f'Grading for {chosen.name} saved. Report cards of {chosen.name} now follow it; other sessions keep theirs.',
                  'success')
            return redirect(url_for('admin_school_grading', session_id=chosen.id))
        form_schemes = raw
    schemes, own = grading.schemes_for(chosen.id)
    source = grading.inherited_from(chosen.id)
    inherited = next((s.name for s in sessions if s.id == source), None) if source else None
    if source and not inherited:
        inherited = db.session.scalar(select(AcademicSession.name).where(AcademicSession.id == source))
    return render_template('admin_school_grading.html', sessions=sessions, chosen=chosen, own=own, errors=errors,
                           schemes=form_schemes or schemes, inherited=inherited, levels=grading.LEVELS, sources=grading.SOURCES,
                           presets={k: {'name': v[0], 'scale': v[1]} for k, v in grading.PRESETS.items()},
                           max_parts=grading.MAX_PARTS, max_bands=grading.MAX_BANDS,
                           tab=request.values.get('level') if request.values.get('level') in grading.LEVEL_NAMES else 'jss')
