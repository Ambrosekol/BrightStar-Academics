"""Privacy & my data, in the parent portal: the privacy notice, each child's consents, and requests to the school for a
copy of a child's data or for it to be erased (core/privacy.py).

A parent decides only for their own linked children. A consent changed here is a new record, with the time and the
address it came from; the school sees the history. A request goes to the school, which has thirty days to answer; a
copy, once the school approves it, is downloaded here until it expires.
"""

from flask import abort, flash, redirect, render_template, request, session, url_for

from app import app
from blueprints.parents.helpers import _parent_children, parent_required
from core import data_export, privacy
from core.security import audit_log, csrf_protect
from models import DataExport, DataRequest, db


def _pid():
    return session.get('parent_id')


@app.route('/parent/privacy')
@parent_required
def parent_privacy():
    pid = _pid()
    children = [c for c in _parent_children(pid) if not c.get('erased_at')]
    consents = privacy.consents_for([c['id'] for c in children])
    requests_ = db.session.query(DataRequest).filter(DataRequest.parent_id == pid).order_by(DataRequest.id.desc()).limit(30).all()
    exports = {e.id: e for e in db.session.query(DataExport).filter(
        DataExport.id.in_([r.export_id for r in requests_ if r.export_id] or [0])).all()}
    names = {c['id']: f"{c['first_name']} {c['last_name']}" for c in _parent_children(pid)}
    return render_template('parent_privacy.html', children=children, consents=consents, purposes=privacy.PURPOSES,
                           child_purposes=privacy.CHILD_PURPOSES, confirmed=privacy.notice_confirmed(pid),
                           version=privacy.NOTICE_VERSION, requests=requests_, exports=exports, names=names,
                           kinds=privacy.KINDS, due_days=privacy.DUE_DAYS, open_states=privacy.OPEN,
                           now=privacy._iso())


@app.post('/parent/privacy/notice')
@parent_required
@csrf_protect
def parent_privacy_notice():
    privacy.record('privacy_notice', True, parent_id=_pid(), source='parent', ip=request.remote_addr)
    db.session.commit()
    flash('Thank you. You can read the privacy notice again at any time from this page.', 'success')
    return redirect(request.form.get('next') if request.form.get('next', '').startswith('/parent/') else url_for('parent_privacy'))


@app.post('/parent/privacy/consent')
@parent_required
@csrf_protect
def parent_privacy_consent():
    pid = _pid()
    student_id = request.form.get('student_id', type=int)
    purpose = request.form.get('purpose', '')
    if purpose not in privacy.CHILD_PURPOSES or student_id not in {c['id'] for c in _parent_children(pid)}:
        abort(404)
    granted = request.form.get('granted') == '1'
    if privacy.record(purpose, granted, student_id=student_id, parent_id=pid, source='parent', ip=request.remote_addr):
        audit_log('consent_changed', 'privacy', 'student', student_id,
                  {'purpose': purpose, 'granted': granted, 'by': 'parent', 'parent_id': pid}, commit=False)
        db.session.commit()
        flash(f"{privacy.PURPOSES[purpose]['title']}: {'given' if granted else 'withdrawn'}. The school sees this at once.",
              'success')
    return redirect(url_for('parent_privacy') + f'#child-{student_id}')


@app.post('/parent/privacy/request')
@parent_required
@csrf_protect
def parent_privacy_request():
    pid = _pid()
    if request.form.get('kind') == 'erasure' and request.form.get('understood') != '1':
        flash('Tick the box to say you understand what erasing means.', 'error')
        return redirect(url_for('parent_privacy') + '#requests')
    try:
        row = privacy.ask(pid, request.form.get('student_id', type=int) or 0, request.form.get('kind', ''),
                          request.form.get('reason', ''))
    except ValueError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('parent_privacy') + '#requests')
    db.session.flush()
    audit_log('data_request_made', 'privacy', 'data_request', row.id,
              {'kind': row.kind, 'student_id': row.student_id, 'parent_id': pid}, commit=False)
    db.session.commit()
    try:
        from core.security import _notify_school_admins
        _notify_school_admins('A family asked about their data',
                              f"{privacy.KINDS[row.kind]}: a parent's request, to be answered by {row.due_at[:10]}.",
                              'warning', url_for('admin_school_privacy'))
    except Exception:
        app.logger.exception('Could not tell the School Admin about data request %s', row.id)
    flash(f'Your request has gone to the school. It must answer within {privacy.DUE_DAYS} days.', 'success')
    return redirect(url_for('parent_privacy') + '#requests')


@app.route('/parent/privacy/requests/<int:request_id>/download')
@parent_required
def parent_privacy_download(request_id):
    row = db.session.get(DataRequest, request_id)
    if row is None or row.parent_id != _pid() or row.kind != 'export' or not row.export_id:
        abort(404)
    data_export.remove_expired()
    export = db.session.get(DataExport, row.export_id)
    response = data_export.download(export, f'my-data-{request_id}.zip')
    if response is None:
        flash('That copy is no longer available. Ask the school for a new one.', 'error')
        return redirect(url_for('parent_privacy') + '#requests')
    audit_log('data_export_downloaded', 'privacy', 'data_request', request_id, {'by': 'parent'})
    return response


def privacy_prompt():
    """For the parent dashboard: whether to ask the signed-in parent to read the current notice."""
    pid = _pid()
    try:
        return bool(pid) and not privacy.notice_confirmed(pid)
    except Exception:
        return False


app.jinja_env.globals.update(parent_privacy_prompt=privacy_prompt)
