"""The school's side of data protection, the full data export, and the API (Settings).

* **Privacy & data requests** (privacy.manage: the School Admin, the head teacher, the records officers): families'
  requests, oldest due first, each approved or declined; what families have allowed, child by child, as a spreadsheet;
  and, on a student's page, a consent recorded from a signed paper form. Erasing a child is the School Admin's alone,
  and is confirmed by typing the child's admission number: it cannot be undone (core/privacy.erase_student).
* **Export all the school's data** (the School Admin's alone): a ZIP built in the background (core/data_export.py).
* **API & webhooks** (the School Admin's alone, since a key reads across the whole school): keys, shown once; and the
  addresses told when something happens (core/webhooks.py), each with its recent deliveries.
"""

import csv
import io
import json

from flask import Response, abort, flash, redirect, render_template, request, url_for
from sqlalchemy import func, select

from app import app
from core import api_keys, data_export, privacy, webhooks
from core.db_helpers import tuples
from core.idempotency import idempotent_write
from core.security import (admin_access_error, admin_has_permission, admin_required, audit_log, csrf_protect,
                           current_admin, is_school_admin)
from models import (
    Admin, ApiKey, DataExport, DataRequest, ParentAccount, Student, WebhookDelivery, WebhookEndpoint, db,
)


def _can_manage():
    me = current_admin()
    return bool(me and (is_school_admin(me) or admin_has_permission(me['id'], 'privacy.manage')))


def _names(student_ids):
    return {sid: (f'{first} {last}', adm) for sid, first, last, adm in tuples(
        select(Student.id, Student.first_name, Student.last_name, Student.admission_no)
        .where(Student.id.in_(student_ids or [0])))}


# ---------------------------------------------------------------- requests and consents
@app.route('/admin/school/privacy')
@admin_required
def admin_school_privacy():
    if not _can_manage():
        return admin_access_error('privacy.manage')
    data_export.remove_expired()
    open_rows = privacy.open_requests()
    closed = db.session.scalars(select(DataRequest).where(DataRequest.status.not_in(privacy.OPEN))
                                .order_by(DataRequest.id.desc()).limit(30)).all()
    rows = list(open_rows) + list(closed)
    names = _names([r.student_id for r in rows])
    parents = dict(tuples(select(ParentAccount.id, ParentAccount.display_name)
                          .where(ParentAccount.id.in_([r.parent_id for r in rows] or [0]))))
    exports = {e.id: e for e in db.session.scalars(select(DataExport).where(
        DataExport.id.in_([r.export_id for r in rows if r.export_id] or [0]))).all()}
    active = [sid for (sid,) in tuples(select(Student.id).where(Student.active == 1))]
    state = privacy.consents_for(active)
    summary = {p: {'allowed': sum(1 for s in state.values() if s[p]['granted']),
                   'decided': sum(1 for s in state.values() if s[p]['decided'])} for p in privacy.CHILD_PURPOSES}
    return render_template('admin_school_privacy.html', open_rows=open_rows, closed=closed, names=names,
                           parents=parents, exports=exports, kinds=privacy.KINDS, purposes=privacy.PURPOSES,
                           child_purposes=privacy.CHILD_PURPOSES, summary=summary, total=len(active),
                           school_admin=is_school_admin(), now=privacy._iso(), due_days=privacy.DUE_DAYS)


@app.post('/admin/school/privacy/requests/<int:request_id>')
@admin_required
@csrf_protect
def admin_school_privacy_decide(request_id):
    if not _can_manage():
        return admin_access_error('privacy.manage')
    me = current_admin()
    row = db.session.get(DataRequest, request_id)
    if row is None:
        abort(404)
    back = redirect(url_for('admin_school_privacy'))
    if row.status != 'pending':
        flash('That request has already been answered.', 'error')
        return back
    action = request.form.get('action')
    note = ' '.join(request.form.get('note', '').split())[:300]
    if action == 'decline':
        if not note:
            flash('Say why the request is declined: the family sees it.', 'error')
            return back
        row.status, row.decided_by, row.decided_at, row.decision_note = 'declined', me['id'], privacy._iso(), note
        audit_log('data_request_declined', 'privacy', 'data_request', row.id, {'kind': row.kind, 'note': note},
                  commit=False)
        db.session.commit()
        flash('Declined. The family sees your reason.', 'success')
        return back
    if action != 'approve':
        abort(400)
    if row.kind == 'export':
        row.status, row.decided_by, row.decided_at, row.decision_note = 'approved', me['id'], privacy._iso(), note or None
        audit_log('data_request_approved', 'privacy', 'data_request', row.id, {'kind': 'export'}, commit=False)
        db.session.commit()
        data_export.start('family', admin_id=me['id'], student_id=row.student_id, request_id=row.id)
        flash('Approved. The copy is being prepared; the family downloads it from their portal.', 'success')
        return back
    # Erasure: the School Admin's alone, confirmed by the child's admission number.
    if not is_school_admin(me):
        flash('Only the School Admin can erase a child\'s data.', 'error')
        return back
    student = db.session.get(Student, row.student_id) if row.student_id else None
    if student is None:
        abort(404)
    if (request.form.get('confirm') or '').strip().lower() != (student.admission_no or '').strip().lower():
        flash(f'Type the admission number ({student.admission_no}) to confirm the erasure.', 'error')
        return back
    try:
        result = privacy.erase_student(student.id, me['id'], requesting_parent_id=row.parent_id)
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'error')
        return back
    row.status, row.decided_by, row.decided_at = 'done', me['id'], privacy._iso()
    row.completed_at, row.decision_note = row.decided_at, note or None
    audit_log('student_data_erased', 'privacy', 'student', student.id,
              {'request_id': row.id, 'removed': result['removed'], 'parents_anonymised': result['parents']}, commit=False)
    db.session.commit()
    privacy.remove_files(result['files'])
    webhooks.emit('student.archived', {'student_id': student.id, 'reason': 'erased'})
    flash(f"Erased. {student.admission_no} is now kept as \"{student.first_name} {student.last_name}\" for the results "
          f"and fee records the school must keep.", 'success')
    return back


@app.route('/admin/school/privacy/consents.csv')
@admin_required
def admin_school_privacy_consents_csv():
    if not _can_manage():
        return admin_access_error('privacy.manage')
    from blueprints.entrance.helpers import csv_safe
    students = tuples(select(Student.id, Student.admission_no, Student.last_name, Student.first_name)
                      .where(Student.active == 1).order_by(Student.last_name, Student.first_name))
    state = privacy.consents_for([s[0] for s in students])
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['Admission no', 'Surname', 'First name'] + [privacy.PURPOSES[p]['title'] for p in privacy.CHILD_PURPOSES])
    for sid, adm, last, first in students:
        w.writerow([csv_safe(v) for v in (adm, last, first)] + [
            ('allowed' if state[sid][p]['granted'] else 'not allowed') + ('' if state[sid][p]['decided'] else ' (default)')
            for p in privacy.CHILD_PURPOSES])
    audit_log('consents_downloaded', 'privacy', 'school', None, {'students': len(students)})
    response = Response(out.getvalue().encode('utf-8-sig'), mimetype='text/csv')
    response.headers['Content-Disposition'] = 'attachment; filename="consents.csv"'
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.post('/admin/school/students/<int:student_id>/consent')
@admin_required
@csrf_protect
def admin_school_student_consent(student_id):
    if not _can_manage():
        return admin_access_error('privacy.manage')
    me = current_admin()
    student = db.session.get(Student, student_id)
    if student is None or student.erased_at:
        abort(404)
    purpose = request.form.get('purpose', '')
    if purpose not in privacy.CHILD_PURPOSES:
        abort(400)
    granted = request.form.get('granted') == '1'
    note = request.form.get('note', '')
    if not ' '.join(note.split()):
        flash('Say where the decision came from, for example "Signed form, 12 September".', 'error')
    elif privacy.record(purpose, granted, student_id=student_id, source='staff', admin_id=me['id'], note=note,
                        ip=request.remote_addr):
        audit_log('consent_changed', 'privacy', 'student', student_id,
                  {'purpose': purpose, 'granted': granted, 'by': 'staff', 'note': note[:120]}, commit=False)
        db.session.commit()
        flash(f"{privacy.PURPOSES[purpose]['title']}: recorded as {'given' if granted else 'withdrawn'}.", 'success')
    else:
        flash('That is already what is recorded.', 'success')
    return redirect(url_for('admin_school_student_detail', sid=student_id) + '#consents')


# ---------------------------------------------------------------- the whole school's data
@app.route('/admin/school/data-export', methods=['GET', 'POST'])
@admin_required
@csrf_protect
def admin_school_data_export():
    if not is_school_admin():
        return admin_access_error("Export all the school's data")
    me = current_admin()
    data_export.remove_expired()
    if request.method == 'POST':
        busy = db.session.scalar(select(DataExport.id).where(DataExport.kind == 'school',
                                                             DataExport.status.in_(('queued', 'building'))))
        if busy:
            flash('An export is already being prepared. It shows here when it is ready.', 'error')
        else:
            data_export.start('school', admin_id=me['id'])
            audit_log('school_export_started', 'privacy', 'school', None, {})
            flash('The export is being prepared. A large school can take a few minutes; refresh this page to see it.',
                  'success')
        return redirect(url_for('admin_school_data_export'))
    rows = db.session.execute(select(DataExport, Admin.display_name).outerjoin(Admin, Admin.id == DataExport.requested_by)
                              .where(DataExport.kind == 'school').order_by(DataExport.id.desc()).limit(10)).all()
    return render_template('admin_school_data_export.html', rows=rows, keep_days=data_export.KEEP_DAYS,
                           now=privacy._iso(), loads=json.loads)


@app.route('/admin/school/data-export/<int:export_id>/download')
@admin_required
def admin_school_data_export_download(export_id):
    if not is_school_admin():
        return admin_access_error("Export all the school's data")
    row = db.session.get(DataExport, export_id)
    if row is None or row.kind != 'school':
        abort(404)
    from core.branding import school_brand
    import re
    stem = re.sub(r'[^A-Za-z0-9]+', '-', school_brand().get('name') or 'school').strip('-')[:50] or 'school'
    response = data_export.download(row, f'{stem}-data-{(row.completed_at or "")[:10]}.zip')
    if response is None:
        flash('That export is no longer available. Prepare a new one.', 'error')
        return redirect(url_for('admin_school_data_export'))
    audit_log('school_export_downloaded', 'privacy', 'data_export', export_id, {'size': row.size_bytes})
    return response


# ---------------------------------------------------------------- API and webhooks
def _api_page(new_key=None, new_secret=None, form=None):
    keys = db.session.execute(select(ApiKey, Admin.display_name).outerjoin(Admin, Admin.id == ApiKey.created_by)
                              .order_by(ApiKey.revoked_at.is_not(None), ApiKey.id.desc())).all()
    endpoints = db.session.scalars(select(WebhookEndpoint).order_by(WebhookEndpoint.id)).all()
    recent = {}
    for d in db.session.scalars(select(WebhookDelivery).where(
            WebhookDelivery.endpoint_id.in_([e.id for e in endpoints] or [0])).order_by(WebhookDelivery.id.desc()).limit(60)):
        recent.setdefault(d.endpoint_id, [])
        if len(recent[d.endpoint_id]) < 8:
            recent[d.endpoint_id].append(d)
    return render_template('admin_school_api.html', keys=keys, endpoints=endpoints, recent=recent,
                           scopes=api_keys.SCOPES, events=webhooks.EVENTS, new_key=new_key, new_secret=new_secret,
                           form=form or {}, loads=json.loads, docs_url=url_for('api_docs', _external=True),
                           max_endpoints=webhooks.MAX_PER_SCHOOL)


@app.route('/admin/school/api')
@admin_required
def admin_school_api():
    if not is_school_admin():
        return admin_access_error('API & webhooks')
    return _api_page()


@app.post('/admin/school/api/keys')
@admin_required
@csrf_protect
@idempotent_write('school.api_key')
def admin_school_api_key_new():
    if not is_school_admin():
        return admin_access_error('API & webhooks')
    me = current_admin()
    try:
        row, key = api_keys.create(request.form.get('name'), request.form.getlist('scopes'), me['id'])
    except ValueError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('admin_school_api'))
    db.session.flush()
    audit_log('api_key_created', 'privacy', 'api_key', row.id, {'name': row.name, 'scopes': json.loads(row.scopes)},
              commit=False)
    db.session.commit()
    return _api_page(new_key={'name': row.name, 'key': key})


@app.post('/admin/school/api/keys/<int:key_id>/revoke')
@admin_required
@csrf_protect
def admin_school_api_key_revoke(key_id):
    if not is_school_admin():
        return admin_access_error('API & webhooks')
    row = db.session.get(ApiKey, key_id)
    if row is None:
        abort(404)
    if not row.revoked_at:
        row.revoked_at, row.revoked_by = privacy._iso(), current_admin()['id']
        audit_log('api_key_revoked', 'privacy', 'api_key', row.id, {'name': row.name}, commit=False)
        db.session.commit()
        flash(f'"{row.name}" revoked. Anything using it stops working at once.', 'success')
    return redirect(url_for('admin_school_api'))


@app.post('/admin/school/api/webhooks')
@admin_required
@csrf_protect
@idempotent_write('school.webhook')
def admin_school_webhook_new():
    if not is_school_admin():
        return admin_access_error('API & webhooks')
    from core.delivery import encrypt
    me = current_admin()
    events = sorted({e for e in request.form.getlist('events') if e in webhooks.EVENTS})
    try:
        url = webhooks.check_url(request.form.get('url'))
        if not events:
            raise ValueError('Choose at least one event.')
        if db.session.scalar(select(func.count()).select_from(WebhookEndpoint)) >= webhooks.MAX_PER_SCHOOL:
            raise ValueError(f'A school can have at most {webhooks.MAX_PER_SCHOOL} addresses.')
    except ValueError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('admin_school_api'))
    secret = webhooks.new_secret()
    row = WebhookEndpoint(url=url, description=' '.join(request.form.get('description', '').split())[:120] or None,
                          events=json.dumps(events), secret_encrypted=encrypt(secret), active=1, created_by=me['id'],
                          created_at=privacy._iso())
    db.session.add(row)
    db.session.flush()
    audit_log('webhook_added', 'privacy', 'webhook', row.id, {'url': url, 'events': events}, commit=False)
    db.session.commit()
    return _api_page(new_secret={'url': url, 'secret': secret})


@app.post('/admin/school/api/webhooks/<int:endpoint_id>')
@admin_required
@csrf_protect
def admin_school_webhook_action(endpoint_id):
    if not is_school_admin():
        return admin_access_error('API & webhooks')
    row = db.session.get(WebhookEndpoint, endpoint_id)
    if row is None:
        abort(404)
    action = request.form.get('action')
    if action == 'test':
        webhooks.send_test(row)
        flash('A test was sent. Its result shows under the address in a moment.', 'success')
    elif action in ('off', 'on'):
        row.active = 1 if action == 'on' else 0
        row.failures_in_a_row = 0 if action == 'on' else row.failures_in_a_row
        row.disabled_reason = None if action == 'on' else 'Switched off by the School Admin.'
        row.updated_at = privacy._iso()
        audit_log('webhook_switched', 'privacy', 'webhook', row.id, {'on': action == 'on'}, commit=False)
        db.session.commit()
        flash('Switched on.' if action == 'on' else 'Switched off: nothing is sent to it until it is switched on.', 'success')
    elif action == 'delete':
        audit_log('webhook_removed', 'privacy', 'webhook', row.id, {'url': row.url}, commit=False)
        db.session.delete(row)
        db.session.commit()
        flash('Address removed.', 'success')
    else:
        abort(400)
    return redirect(url_for('admin_school_api'))


@app.post('/admin/school/api/deliveries/<int:delivery_id>/resend')
@admin_required
@csrf_protect
def admin_school_webhook_resend(delivery_id):
    if not is_school_admin():
        return admin_access_error('API & webhooks')
    row = db.session.get(WebhookDelivery, delivery_id)
    if row is None:
        abort(404)
    webhooks.resend(row)
    flash('Sent again.', 'success')
    return redirect(url_for('admin_school_api'))
