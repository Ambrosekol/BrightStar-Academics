"""Human resources: staff records, their qualifications, documents and career, and leave. The rules are core/hr.py and
the tables models/hr.py.

Who may do what.
* hr.view: the staff list and each record (profile, qualifications, documents, career, leave).
* hr.manage: add and change records, upload and remove documents, record career entries (never edited or deleted,
  only struck out with a reason), set leave allowances and kinds of leave, and record leave for someone.
* hr.leave.approve: approve or decline leave. It shows only the requests and each person's balance, not the record.
* Everyone whose account is linked to a record asks for their own leave, and sees their own record, on My leave. The
  record's private HR notes are never shown to them.
Nobody decides their own leave. Every change, every download of a document and every opening of a record is in the
activity log; the details logged never include the personal information itself.
"""

import mimetypes
from datetime import date, datetime, timezone

from flask import abort, flash, g, make_response, redirect, render_template, request, url_for
from sqlalchemy import and_, func, or_, select

from app import app
from control_plane.config import badge_cache_seconds
from core import hr
from core.db_helpers import all_rows
from core.security import (
    admin_access_error, admin_has_permission, admin_required, audit_log, csrf_protect, current_admin,
)
from core.short_cache import remember
from core.storage import delete_upload, read_upload_bytes, save_upload_bytes
from core.uploads import _save_image_upload, attachment_limit_bytes
from models import (
    Admin, AdminNotification, AdminType, LeaveAllowance, LeaveRequest, LeaveType, StaffCareerEvent, StaffDocument,
    StaffQualification, StaffRecord, db,
)

LIST_LIMIT = 1000
HISTORY = 150


def _now():
    return datetime.now(timezone.utc).isoformat()


def _today():
    return date.today()


def _may(code, admin=None):
    admin = admin or current_admin()
    return bool(admin and admin_has_permission(admin['id'], code))


def _record_or_404(sid):
    record = db.session.get(StaffRecord, sid)
    if record is None:
        abort(404)
    return record


def _names(ids):
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {a.id: a.display_name for a in db.session.scalars(select(Admin).where(Admin.id.in_(ids))).all()}


def _photo(record, accounts=None):
    if record.photo_path:
        return record.photo_path
    if record.admin_id:
        account = (accounts or {}).get(record.admin_id) or db.session.get(Admin, record.admin_id)
        return account.photo_path if account else None
    return None


def _unlinked_accounts(keep=None):
    """Active staff accounts with no record yet (and ``keep``, the one a record is linked to now)."""
    linked = {a for (a,) in db.session.execute(select(StaffRecord.admin_id).where(StaffRecord.admin_id.is_not(None))).all()}
    rows = db.session.execute(select(Admin.id, Admin.display_name, AdminType.name.label('role'))
                              .outerjoin(AdminType, AdminType.id == Admin.admin_type_id)
                              .where(Admin.active == 1, ~Admin.username.startswith('platform@'))
                              .order_by(Admin.display_name)).all()
    return [r._asdict() for r in rows if r.id not in linked or r.id == keep]


def _leave_now(staff_ids=None):
    """{staff_id: last day} of everyone on approved leave today."""
    today = _today().isoformat()
    stmt = select(LeaveRequest.staff_id, func.max(LeaveRequest.end_date)).where(
        LeaveRequest.status == 'approved', LeaveRequest.start_date <= today, LeaveRequest.end_date >= today)
    if staff_ids is not None:
        stmt = stmt.where(LeaveRequest.staff_id.in_(list(staff_ids) or [0]))
    return dict(db.session.execute(stmt.group_by(LeaveRequest.staff_id)).all())


# ---------------------------------------------------------------- leave arithmetic
def _types(active_only=True):
    stmt = select(LeaveType).order_by(LeaveType.sort_order, LeaveType.name)
    if active_only:
        stmt = stmt.where(LeaveType.active == 1)
    return db.session.scalars(stmt).all()


def _days_taken(staff_id, type_id, year, status, exclude=None):
    stmt = select(func.coalesce(func.sum(LeaveRequest.days), 0)).where(
        LeaveRequest.staff_id == staff_id, LeaveRequest.leave_type_id == type_id, LeaveRequest.status == status,
        LeaveRequest.start_date >= f'{year:04d}-01-01', LeaveRequest.start_date <= f'{year:04d}-12-31')
    if exclude:
        stmt = stmt.where(LeaveRequest.id != exclude)
    return int(db.session.execute(stmt).scalar() or 0)


def _allowance(staff_id, type_id, year):
    row = db.session.scalars(select(LeaveAllowance).where(LeaveAllowance.staff_id == staff_id, LeaveAllowance.leave_type_id == type_id,
                                                          LeaveAllowance.year == year)).first()
    return row


def _balance(staff_id, leave_type, year, exclude=None):
    own = _allowance(staff_id, leave_type.id, year)
    out = hr.balance(leave_type, own.days if own else None, _days_taken(staff_id, leave_type.id, year, 'approved', exclude),
                     _days_taken(staff_id, leave_type.id, year, 'pending', exclude))
    out.update({'type': leave_type, 'own': own})
    return out


def _balances(staff_id, year):
    return [_balance(staff_id, t, year) for t in _types()]


def _clashes(staff_id, start, end, exclude=None):
    stmt = select(LeaveRequest).where(LeaveRequest.staff_id == staff_id, LeaveRequest.status.in_(('pending', 'approved')),
                                      LeaveRequest.start_date <= end.isoformat(), LeaveRequest.end_date >= start.isoformat())
    if exclude:
        stmt = stmt.where(LeaveRequest.id != exclude)
    return db.session.scalars(stmt).first()


def _new_request(record, form, me, backdate=hr.MAX_BACKDATE):
    """A LeaveRequest from a form (not yet added), or an error sentence."""
    leave_type = db.session.get(LeaveType, form.get('leave_type_id', type=int) or 0)
    if leave_type is None or not leave_type.active:
        return None, 'Choose the kind of leave.'
    if record.status == 'left':
        return None, 'They have left the school.'
    start, end, days, error = hr.check_leave_dates(form.get('start_date'), form.get('end_date'), _today(), backdate)
    if error:
        return None, error
    if _clashes(record.id, start, end):
        return None, 'Those dates overlap leave that is already asked for or approved.'
    held = _balance(record.id, leave_type, start.year)
    if held['left'] is not None and days > held['left'] - held['waiting']:
        free = max(held['left'] - held['waiting'], 0)
        return None, (f'That is {days} working day{"s" if days != 1 else ""} of {leave_type.name.lower()}, '
                      f'but only {free} {"is" if free == 1 else "are"} left for {start.year}'
                      + (' (some is waiting for approval).' if held['waiting'] else '.'))
    return LeaveRequest(staff_id=record.id, leave_type_id=leave_type.id, start_date=start.isoformat(), end_date=end.isoformat(),
                        days=days, reason=hr.one_line(form.get('reason'), hr.MAX_REASON) or None,
                        cover=hr.one_line(form.get('cover'), 200) or None, status='pending', requested_by=me['id'],
                        requested_at=_now()), None


def _approvers(exclude):
    """The active accounts that may approve leave, less ``exclude`` (the account of the person on leave)."""
    ids = [a for (a,) in db.session.execute(select(Admin.id).where(Admin.active == 1, ~Admin.username.startswith('platform@'))).all()]
    return [a for a in ids if a != exclude and admin_has_permission(a, 'hr.leave.approve')]


def _notify(admin_ids, title, message, actor, endpoint):
    now = _now()
    for aid in {a for a in admin_ids if a and a != actor['id']}:
        db.session.add(AdminNotification(admin_id=aid, title=title, message=message, severity='info',
                                         action_url=url_for(endpoint), actor_admin_id=actor['id'],
                                         actor_username_snapshot=actor['username'],
                                         actor_display_name_snapshot=actor['display_name'], created_at=now))


def _may_decide(admin, leave, record):
    return (leave.status == 'pending' and _may('hr.leave.approve', admin)
            and not (record and record.admin_id == admin['id']))


def hr_menu(admin=None):
    """For the menu: {'records': may see staff records, 'leave': may see the leave page, 'waiting': requests waiting
    for this person's decision, 'mine': has a record of their own (My leave)}. Read once a request."""
    admin = admin or current_admin()
    empty = {'records': False, 'leave': False, 'waiting': 0, 'mine': False}
    if not admin:
        return empty
    cached = getattr(g, '_hr_menu', None)
    if cached is not None:
        return cached

    def compute(admin_id):
        approve = admin_has_permission(admin_id, 'hr.leave.approve')
        mine = db.session.execute(select(StaffRecord.id).where(StaffRecord.admin_id == admin_id)).first()
        waiting = 0
        if approve:
            waiting = db.session.execute(
                select(func.count(LeaveRequest.id)).join(StaffRecord, StaffRecord.id == LeaveRequest.staff_id)
                .where(LeaveRequest.status == 'pending', or_(StaffRecord.admin_id.is_(None), StaffRecord.admin_id != admin_id))).scalar() or 0
        return {'records': admin_has_permission(admin_id, 'hr.view'), 'leave': approve or admin_has_permission(admin_id, 'hr.view'),
                'waiting': int(waiting), 'mine': mine is not None}

    try:
        # Asked on every staff page (the menu), so kept for a few seconds like the other menu counts and dropped
        # whenever anything changes (app.py, forget_counts_after_a_change).
        g._hr_menu = remember('hr-menu', badge_cache_seconds(), compute, admin['id'])
    except Exception:
        db.session.rollback()
        g._hr_menu = empty
    return g._hr_menu


app.jinja_env.globals.update(hr_menu=hr_menu)


# ---------------------------------------------------------------- the staff list
@app.route('/admin/hr')
@admin_required
def admin_hr_staff():
    status = request.args.get('status', 'working')
    category = request.args.get('category', '')
    q = hr.one_line(request.args.get('q'), 80)
    stmt = select(StaffRecord).order_by(StaffRecord.last_name, StaffRecord.first_name).limit(LIST_LIMIT)
    if status == 'working':
        stmt = stmt.where(StaffRecord.status.in_(('active', 'suspended')))
    elif status in hr.STATUSES:
        stmt = stmt.where(StaffRecord.status == status)
    else:
        status = 'all'
    if category in hr.CATEGORIES:
        stmt = stmt.where(StaffRecord.category == category)
    records = db.session.scalars(stmt).all()
    if q:
        needle = q.casefold()
        records = [r for r in records if needle in ' '.join(str(v or '') for v in (
            r.first_name, r.middle_name, r.last_name, r.staff_no, r.job_title, r.department)).casefold()]
    accounts = {a.id: a for a in db.session.scalars(select(Admin).where(Admin.id.in_([r.admin_id for r in records if r.admin_id] or [0]))).all()}
    counts = dict(db.session.execute(select(StaffRecord.status, func.count(StaffRecord.id)).group_by(StaffRecord.status)).all())
    return render_template('school_hr_staff.html', records=records, accounts=accounts, photos={r.id: _photo(r, accounts) for r in records},
                           on_leave=_leave_now([r.id for r in records]), status=status, category=category, q=q, counts=counts,
                           unlinked=len(_unlinked_accounts()) if _may('hr.manage') else 0, can_manage=_may('hr.manage'), hr=hr)


@app.post('/admin/hr/from-accounts')
@admin_required
@csrf_protect
def admin_hr_from_accounts():
    """A record for every active staff account that has none, from what the account already holds."""
    me = current_admin()
    made = 0
    for account in _unlinked_accounts():
        row = db.session.get(Admin, account['id'])
        parts = (row.display_name or '').replace('.', ' ').split()
        title = parts.pop(0).capitalize() if len(parts) >= 2 and parts[0].capitalize() in hr.TITLES else None
        # One name left ("Mrs Bello") is the surname; the first name is then for HR to fill in.
        first = parts[0] if len(parts) > 1 else ''
        last = parts[-1] if parts else (row.username or 'Staff')
        middle = ' '.join(parts[1:-1]) or None
        db.session.add(StaffRecord(admin_id=row.id, title=title, first_name=first[:60], middle_name=(middle or '')[:60] or None,
                                   last_name=last[:60], phone=hr.one_line(row.phone, 30) or None, email=hr.one_line(row.email, 120) or None,
                                   job_title=(account['role'] or '')[:80] or None, created_at=_now(), created_by=me['id']))
        made += 1
    db.session.commit()
    audit_log('hr_records_from_accounts', 'staff_records', 'hr_staff', None, {'created': made})
    flash(f'{made} staff record{"s" if made != 1 else ""} added from staff accounts. Open each one to fill in the rest.'
          if made else 'Every staff account already has a record.', 'success')
    return redirect(url_for('admin_hr_staff'))


# ---------------------------------------------------------------- one record
def _form_page(record, values, errors, status=200):
    return render_template('school_hr_form.html', record=record, values=values, errors=errors,
                           accounts=_unlinked_accounts(keep=record.admin_id if record else None), hr=hr), status


def _apply(record, values, form, me):
    """Write the cleaned values (and the linked account and photograph) onto ``record``; an error sentence or None."""
    staff_no = values.get('staff_no')
    if staff_no:
        clash = db.session.execute(select(StaffRecord.id).where(func.lower(StaffRecord.staff_no) == staff_no.lower(),
                                                                StaffRecord.id != (record.id or 0))).first()
        if clash:
            return f'Staff number {staff_no} is already someone else\'s.'
    account_id = form.get('admin_id', type=int)
    if account_id and account_id != record.admin_id:
        if account_id not in {a['id'] for a in _unlinked_accounts()}:
            return 'That account is not one that can be linked: it has a record already, or it is switched off.'
    photo = request.files.get('photo')
    if photo and photo.filename:
        try:
            path = _save_image_upload(photo, 'admins', 'hr')
        except ValueError as exc:
            return str(exc)
        if path:
            record.photo_path = path
    for key, value in values.items():
        setattr(record, key, value)
    record.admin_id = account_id or None
    return None


@app.route('/admin/hr/new', methods=['GET', 'POST'])
@admin_required
@csrf_protect
def admin_hr_new():
    me = current_admin()
    if request.method == 'GET':
        account = request.args.get('account', type=int)
        values = {'category': 'teaching', 'employment_type': 'full_time', 'status': 'active'}
        if account and account in {a['id'] for a in _unlinked_accounts()}:
            row = db.session.get(Admin, account)
            parts = (row.display_name or '').split()
            values.update({'admin_id': account, 'first_name': parts[0] if parts else '', 'last_name': parts[-1] if len(parts) > 1 else '',
                           'phone': row.phone, 'email': row.email})
        return _form_page(None, values, [])
    values, errors = hr.clean_record(request.form, _today())
    shown = dict(values, admin_id=request.form.get('admin_id', type=int))
    if errors:
        return _form_page(None, shown, errors, 400)
    record = StaffRecord(first_name='-', last_name='-', created_at=_now(), created_by=me['id'])
    error = _apply(record, values, request.form, me)
    if error:
        return _form_page(None, shown, [error], 400)
    db.session.add(record)
    db.session.commit()
    audit_log('hr_record_created', 'staff_records', 'hr_staff', record.id, {'linked_account': record.admin_id})
    flash(f'{hr.full_name(record)} added.', 'success')
    return redirect(url_for('admin_hr_person', sid=record.id))


@app.route('/admin/hr/<int:sid>/edit', methods=['GET', 'POST'])
@admin_required
@csrf_protect
def admin_hr_edit(sid):
    me = current_admin()
    record = _record_or_404(sid)
    if request.method == 'GET':
        values = {c.name: getattr(record, c.name) for c in StaffRecord.__table__.columns}
        return _form_page(record, values, [])
    values, errors = hr.clean_record(request.form, _today())
    shown = dict(values, admin_id=request.form.get('admin_id', type=int))
    if errors:
        return _form_page(record, shown, errors, 400)
    before = {k: getattr(record, k) for k in values} | {'admin_id': record.admin_id}
    error = _apply(record, values, request.form, me)
    if error:
        db.session.rollback()
        return _form_page(record, shown, [error], 400)
    record.updated_at, record.updated_by = _now(), me['id']
    changed = sorted(k for k in before if before[k] != getattr(record, k))
    db.session.commit()
    # Which fields changed, never what they were or became: the log is read by more people than HR.
    audit_log('hr_record_updated', 'staff_records', 'hr_staff', record.id, {'fields': changed})
    if 'status' in changed and record.status == 'left' and record.admin_id:
        account = db.session.get(Admin, record.admin_id)
        if account and account.active:
            flash(f'{hr.full_name(record)} is marked as having left. Their portal account is still on: switch it off in Staff '
                  'accounts if they should no longer sign in.', 'warning')
    flash('Record saved.', 'success')
    return redirect(url_for('admin_hr_person', sid=record.id))


@app.route('/admin/hr/<int:sid>')
@admin_required
def admin_hr_person(sid):
    me = current_admin()
    record = _record_or_404(sid)
    tab = request.args.get('tab', 'profile')
    if tab not in ('profile', 'files', 'career', 'leave'):
        tab = 'profile'
    year = request.args.get('year', type=int) or _today().year
    year = min(max(year, 2000), _today().year + 1)
    account = db.session.get(Admin, record.admin_id) if record.admin_id else None
    context = {'record': record, 'account': account, 'photo': _photo(record), 'tab': tab, 'hr': hr, 'this_year': _today().year, 'today': _today().isoformat(),
               'can_manage': _may('hr.manage'), 'on_leave': _leave_now([record.id]).get(record.id)}
    who = set()
    if tab == 'files':
        context['qualifications'] = db.session.scalars(select(StaffQualification).where(StaffQualification.staff_id == sid)
                                                       .order_by(StaffQualification.year.desc().nulls_last(), StaffQualification.id)).all()
        context['documents'] = db.session.scalars(select(StaffDocument).where(StaffDocument.staff_id == sid)
                                                  .order_by(StaffDocument.id.desc())).all()
        who |= {d.uploaded_by for d in context['documents']}
        context['upload_limit'] = attachment_limit_bytes()
    elif tab == 'career':
        context['events'] = db.session.scalars(select(StaffCareerEvent).where(StaffCareerEvent.staff_id == sid)
                                               .order_by(StaffCareerEvent.effective_date.desc(), StaffCareerEvent.id.desc())).all()
        who |= {e.recorded_by for e in context['events']} | {e.struck_by for e in context['events']}
    elif tab == 'leave':
        context['year'] = year
        context['balances'] = _balances(sid, year)
        context['requests'] = db.session.scalars(select(LeaveRequest).where(LeaveRequest.staff_id == sid)
                                                 .order_by(LeaveRequest.start_date.desc()).limit(HISTORY)).all()
        context['types'] = {t.id: t for t in _types(active_only=False)}
        context['active_types'] = _types()
        context['can_approve'] = _may('hr.leave.approve') and record.admin_id != me['id']
        who |= {r.requested_by for r in context['requests']} | {r.decided_by for r in context['requests']}
    context['names'] = _names(who | {record.created_by, record.updated_by})
    audit_log('hr_record_viewed', 'staff_records', 'hr_staff', sid, {'tab': tab})
    response = make_response(render_template('school_hr_person.html', **context))
    response.headers['Cache-Control'] = 'no-store, private'
    return response


# ---------------------------------------------------------------- qualifications and documents
@app.post('/admin/hr/<int:sid>/qualifications')
@admin_required
@csrf_protect
def admin_hr_qualification_add(sid):
    me = current_admin()
    record = _record_or_404(sid)
    name = hr.one_line(request.form.get('qualification'), 120)
    year = request.form.get('year', type=int)
    if not name:
        flash('Enter the qualification.', 'error')
        return redirect(url_for('admin_hr_person', sid=sid, tab='files'))
    if year is not None and not (1950 <= year <= _today().year + 1):
        year = None
    db.session.add(StaffQualification(staff_id=record.id, qualification=name, institution=hr.one_line(request.form.get('institution'), 120) or None,
                                      year=year, created_at=_now(), created_by=me['id']))
    db.session.commit()
    audit_log('hr_qualification_added', 'staff_records', 'hr_staff', sid, {})
    flash('Qualification added.', 'success')
    return redirect(url_for('admin_hr_person', sid=sid, tab='files'))


@app.post('/admin/hr/qualifications/<int:qid>/remove')
@admin_required
@csrf_protect
def admin_hr_qualification_remove(qid):
    row = db.session.get(StaffQualification, qid)
    if row is None:
        abort(404)
    sid = row.staff_id
    db.session.delete(row)
    db.session.commit()
    audit_log('hr_qualification_removed', 'staff_records', 'hr_staff', sid, {'qualification_id': qid})
    flash('Qualification removed.', 'success')
    return redirect(url_for('admin_hr_person', sid=sid, tab='files'))


@app.post('/admin/hr/<int:sid>/documents')
@admin_required
@csrf_protect
def admin_hr_document_add(sid):
    me = current_admin()
    record = _record_or_404(sid)
    back = redirect(url_for('admin_hr_person', sid=sid, tab='files'))
    upload = request.files.get('document')
    label = hr.one_line(request.form.get('label'), 120)
    category = request.form.get('category', 'other')
    category = category if category in hr.DOCUMENT_CATEGORIES else 'other'
    if not upload or not upload.filename:
        flash('Choose the file to upload.', 'error')
        return back
    try:
        ext, data, content_type, original = hr.check_document(upload, attachment_limit_bytes())
    except ValueError as exc:
        flash(str(exc), 'error')
        return back
    path = save_upload_bytes('hr', hr.document_name(ext), data, content_type)
    try:
        db.session.add(StaffDocument(staff_id=record.id, label=label or hr.DOCUMENT_CATEGORIES[category], category=category,
                                     file_path=path, original_name=original[:120], content_type=content_type, size_bytes=len(data),
                                     uploaded_at=_now(), uploaded_by=me['id']))
        db.session.commit()
    except Exception:
        db.session.rollback()
        delete_upload(path)
        raise
    audit_log('hr_document_added', 'staff_records', 'hr_staff', sid, {'category': category, 'bytes': len(data)})
    flash('Document uploaded.', 'success')
    return back


@app.route('/admin/hr/documents/<int:did>')
@admin_required
def admin_hr_document(did):
    row = db.session.get(StaffDocument, did)
    if row is None:
        abort(404)
    data = read_upload_bytes(row.file_path)
    if data is None:
        abort(404)
    audit_log('hr_document_opened', 'staff_records', 'hr_staff', row.staff_id, {'document_id': did})
    ext = row.file_path.rsplit('.', 1)[-1].lower()
    content_type = hr.DOCUMENT_TYPES.get(ext, ('application/octet-stream',))[0]
    response = make_response(data)
    response.headers['Content-Type'] = content_type
    # PDFs and pictures open in the browser; anything else downloads. The name is the stored, safe one.
    inline = ext in ('pdf', 'png', 'jpg', 'jpeg') and request.args.get('download') != '1'
    filename = (row.original_name or f'document.{ext}').replace('"', '')
    response.headers['Content-Disposition'] = f'{"inline" if inline else "attachment"}; filename="{filename}"'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Cache-Control'] = 'no-store, private'
    return response


@app.post('/admin/hr/documents/<int:did>/remove')
@admin_required
@csrf_protect
def admin_hr_document_remove(did):
    row = db.session.get(StaffDocument, did)
    if row is None:
        abort(404)
    sid, path = row.staff_id, row.file_path
    db.session.delete(row)
    db.session.commit()
    delete_upload(path)
    audit_log('hr_document_removed', 'staff_records', 'hr_staff', sid, {'document_id': did})
    flash('Document removed.', 'success')
    return redirect(url_for('admin_hr_person', sid=sid, tab='files'))


# ---------------------------------------------------------------- career
@app.post('/admin/hr/<int:sid>/career')
@admin_required
@csrf_protect
def admin_hr_career_add(sid):
    me = current_admin()
    record = _record_or_404(sid)
    back = redirect(url_for('admin_hr_person', sid=sid, tab='career'))
    kind = request.form.get('kind', '')
    when = hr.parse_date(request.form.get('effective_date'))
    summary = hr.one_line(request.form.get('summary'), 160)
    if kind not in hr.CAREER_KINDS:
        flash('Choose what kind of entry this is.', 'error')
        return back
    if not when or not (date(1950, 1, 1) <= when <= date(_today().year + 1, 12, 31)):
        flash('Enter the date it took effect.', 'error')
        return back
    new_value = hr.one_line(request.form.get('to_value'), 80) or None
    if not summary:
        summary = hr.CAREER_KINDS[kind] + (f': {new_value}' if new_value else '')
    event = StaffCareerEvent(staff_id=sid, kind=kind, effective_date=when.isoformat(), summary=summary,
                             detail=str(request.form.get('detail', '')).strip()[:2000] or None, recorded_at=_now(), recorded_by=me['id'])
    # A promotion changes the job title, a change of department the department, when a new one is given.
    if kind == 'promotion' and new_value:
        event.from_value, event.to_value, record.job_title = record.job_title, new_value, new_value
    elif kind == 'transfer' and new_value:
        event.from_value, event.to_value, record.department = record.department, new_value, new_value
    elif kind == 'confirmation' and not record.confirmation_date:
        record.confirmation_date = when.isoformat()
    record.updated_at, record.updated_by = _now(), me['id']
    db.session.add(event)
    db.session.commit()
    audit_log('hr_career_recorded', 'staff_records', 'hr_staff', sid, {'kind': kind, 'event_id': event.id})
    flash(f'{hr.CAREER_KINDS[kind]} recorded.', 'success')
    return back


@app.post('/admin/hr/career/<int:eid>/strike')
@admin_required
@csrf_protect
def admin_hr_career_strike(eid):
    me = current_admin()
    event = db.session.get(StaffCareerEvent, eid)
    if event is None:
        abort(404)
    reason = hr.one_line(request.form.get('reason'), 300)
    if not reason:
        flash('Say why the entry is being struck out.', 'error')
    elif not event.struck_at:
        event.struck_at, event.struck_by, event.struck_reason = _now(), me['id'], reason
        db.session.commit()
        audit_log('hr_career_struck', 'staff_records', 'hr_staff', event.staff_id, {'event_id': eid})
        flash('Entry struck out. It stays on the record, crossed through, with your reason.', 'success')
    return redirect(url_for('admin_hr_person', sid=event.staff_id, tab='career'))


# ---------------------------------------------------------------- leave, for HR
@app.post('/admin/hr/<int:sid>/allowance')
@admin_required
@csrf_protect
def admin_hr_allowance(sid):
    me = current_admin()
    _record_or_404(sid)
    leave_type = db.session.get(LeaveType, request.form.get('leave_type_id', type=int) or 0)
    year = request.form.get('year', type=int)
    raw = str(request.form.get('days', '')).strip()
    back = redirect(url_for('admin_hr_person', sid=sid, tab='leave', year=year))
    if leave_type is None or not year or not (2000 <= year <= _today().year + 1):
        abort(400)
    row = _allowance(sid, leave_type.id, year)
    if raw == '':
        if row:
            db.session.delete(row)
            db.session.commit()
            audit_log('hr_allowance_reset', 'staff_records', 'hr_staff', sid, {'leave_type_id': leave_type.id, 'year': year})
        flash(f'{leave_type.name} for {year} is back to the usual {leave_type.days_per_year} days.', 'success')
        return back
    if not raw.isdigit() or int(raw) > 366:
        flash('Enter a number of days from 0 to 366, or leave it empty for the usual allowance.', 'error')
        return back
    if row is None:
        row = LeaveAllowance(staff_id=sid, leave_type_id=leave_type.id, year=year, days=0, set_at=_now())
        db.session.add(row)
    row.days, row.note, row.set_at, row.set_by = int(raw), hr.one_line(request.form.get('note'), 200) or None, _now(), me['id']
    db.session.commit()
    audit_log('hr_allowance_set', 'staff_records', 'hr_staff', sid, {'leave_type_id': leave_type.id, 'year': year, 'days': row.days})
    flash(f'{leave_type.name} for {year}: {row.days} days.', 'success')
    return back


@app.post('/admin/hr/<int:sid>/leave')
@admin_required
@csrf_protect
def admin_hr_leave_record(sid):
    """Leave recorded by HR for someone (one without an account, or on their behalf). It is approved at once when
    the person recording it may approve leave and it is not their own; otherwise it waits like any request."""
    me = current_admin()
    record = _record_or_404(sid)
    back = redirect(url_for('admin_hr_person', sid=sid, tab='leave'))
    leave, error = _new_request(record, request.form, me, backdate=366)
    if error:
        flash(error, 'error')
        return back
    approve = _may('hr.leave.approve') and record.admin_id != me['id']
    if approve:
        leave.status, leave.decided_by, leave.decided_at, leave.decision_note = 'approved', me['id'], _now(), 'Recorded by HR'
    db.session.add(leave)
    db.session.flush()
    if not approve:
        _notify(_approvers(record.admin_id), 'Leave to approve',
                f'{me["display_name"]} asks for {leave.days} day(s) of leave for {hr.full_name(record, False)}.', me, 'admin_hr_leave')
    db.session.commit()
    audit_log('hr_leave_recorded', 'staff_records', 'hr_leave', leave.id, {'staff_id': sid, 'days': leave.days, 'status': leave.status})
    flash('Leave recorded and approved.' if approve else 'Leave recorded. It waits for someone who may approve leave.', 'success')
    return back


@app.route('/admin/hr/leave')
@admin_required
def admin_hr_leave():
    me = current_admin()
    can_approve, can_view = _may('hr.leave.approve'), _may('hr.view')
    if not (can_approve or can_view):
        return admin_access_error('hr.leave.approve')
    today = _today().isoformat()
    pending = db.session.scalars(select(LeaveRequest).where(LeaveRequest.status == 'pending').order_by(LeaveRequest.start_date)).all()
    current = db.session.scalars(select(LeaveRequest).where(LeaveRequest.status == 'approved', LeaveRequest.end_date >= today)
                                 .order_by(LeaveRequest.start_date)).all()
    history = db.session.scalars(select(LeaveRequest).where(or_(LeaveRequest.status.in_(('declined', 'cancelled')),
                                                                and_(LeaveRequest.status == 'approved', LeaveRequest.end_date < today)))
                                 .order_by(LeaveRequest.decided_at.desc().nulls_last(), LeaveRequest.id.desc()).limit(HISTORY)).all()
    everyone = pending + current + history
    records = {r.id: r for r in db.session.scalars(select(StaffRecord).where(StaffRecord.id.in_({x.staff_id for x in everyone} or {0}))).all()}
    types = {t.id: t for t in _types(active_only=False)}
    rows = []
    for x in pending:
        record = records.get(x.staff_id)
        held = _balance(x.staff_id, types[x.leave_type_id], int(x.start_date[:4]), exclude=x.id) if record else None
        rows.append({'leave': x, 'record': record, 'balance': held, 'can_decide': _may_decide(me, x, record)})
    response = make_response(render_template(
        'school_hr_leave.html', waiting=rows, current=current, history=history, records=records, types=types, today=today,
        names=_names({x.requested_by for x in everyone} | {x.decided_by for x in everyone}), can_view=can_view,
        can_manage=_may('hr.manage'), hr=hr))
    response.headers['Cache-Control'] = 'no-store, private'
    return response


@app.post('/admin/hr/leave/<int:rid>/decide')
@admin_required
@csrf_protect
def admin_hr_leave_decide(rid):
    me = current_admin()
    leave = db.session.scalars(select(LeaveRequest).where(LeaveRequest.id == rid).with_for_update()).first()
    if leave is None:
        abort(404)
    record = db.session.get(StaffRecord, leave.staff_id)
    back = redirect(request.form.get('back') == 'person' and url_for('admin_hr_person', sid=leave.staff_id, tab='leave')
                    or url_for('admin_hr_leave'))
    if not _may_decide(me, leave, record):
        db.session.rollback()
        flash('That request is no longer waiting, or it is not one you may decide (nobody decides their own leave).', 'error')
        return back
    approve = request.form.get('decision') == 'approve'
    note = hr.one_line(request.form.get('note'), 300) or None
    leave_type = db.session.get(LeaveType, leave.leave_type_id)
    if approve:
        held = _balance(leave.staff_id, leave_type, int(leave.start_date[:4]), exclude=leave.id)
        if held['left'] is not None and leave.days > held['left']:
            db.session.rollback()
            flash(f'Only {max(held["left"], 0)} day(s) of {leave_type.name.lower()} are left for {leave.start_date[:4]}. Decline it, '
                  'or raise their allowance on their record first.', 'error')
            return back
    leave.status = 'approved' if approve else 'declined'
    leave.decided_by, leave.decided_at, leave.decision_note = me['id'], _now(), note
    if record and record.admin_id:
        _notify([record.admin_id], 'Leave approved' if approve else 'Leave declined',
                f'{leave_type.name}, {leave.start_date} to {leave.end_date}: {"approved" if approve else "declined"} by '
                f'{me["display_name"]}.' + (f' "{note}"' if note else ''), me, 'admin_my_leave')
    db.session.commit()
    audit_log('hr_leave_approved' if approve else 'hr_leave_declined', 'staff_records', 'hr_leave', rid, {'staff_id': leave.staff_id, 'days': leave.days})
    flash('Leave approved.' if approve else 'Leave declined.', 'success')
    return back


@app.post('/admin/hr/leave/<int:rid>/cancel')
@admin_required
@csrf_protect
def admin_hr_leave_cancel(rid):
    """The person cancels their own leave (waiting, or approved and not yet started); HR cancels any that has not
    ended (someone came back early)."""
    me = current_admin()
    leave = db.session.scalars(select(LeaveRequest).where(LeaveRequest.id == rid).with_for_update()).first()
    if leave is None:
        abort(404)
    record = db.session.get(StaffRecord, leave.staff_id)
    mine = bool(record and record.admin_id == me['id'])
    today = _today().isoformat()
    manager = _may('hr.manage')
    allowed = leave.status in ('pending', 'approved') and (
        (mine and (leave.status == 'pending' or leave.start_date > today)) or (manager and leave.end_date >= today) or
        (manager and leave.status == 'pending'))
    back = redirect(url_for('admin_my_leave') if mine and request.form.get('back') != 'hr' else
                    url_for('admin_hr_person', sid=leave.staff_id, tab='leave') if request.form.get('back') == 'person' else
                    url_for('admin_hr_leave'))
    if not allowed:
        db.session.rollback()
        if not (mine or manager):
            return admin_access_error('hr.manage')
        flash('That leave can no longer be cancelled.', 'error')
        return back
    was = leave.status
    leave.status = 'cancelled'
    leave.decided_by, leave.decided_at = me['id'], _now()
    leave.decision_note = hr.one_line(request.form.get('note'), 300) or ('Cancelled by them' if mine else 'Cancelled by HR')
    if mine and was == 'approved':
        _notify(_approvers(me['id']), 'Leave cancelled', f'{me["display_name"]} cancelled their approved leave from {leave.start_date}.',
                me, 'admin_hr_leave')
    elif not mine and record and record.admin_id:
        _notify([record.admin_id], 'Leave cancelled', f'Your leave from {leave.start_date} was cancelled by {me["display_name"]}.',
                me, 'admin_my_leave')
    db.session.commit()
    audit_log('hr_leave_cancelled', 'staff_records', 'hr_leave', rid, {'staff_id': leave.staff_id, 'was': was})
    flash('Leave cancelled.', 'success')
    return back


@app.route('/admin/hr/leave/types', methods=['GET', 'POST'])
@admin_required
@csrf_protect
def admin_hr_leave_types():
    if request.method == 'POST':
        changed = 0
        for t in _types(active_only=False):
            raw = str(request.form.get(f'days_{t.id}', '')).strip()
            if not raw.isdigit() or int(raw) > 366:
                flash(f'{t.name}: enter a number of days from 0 to 366.', 'error')
                return redirect(url_for('admin_hr_leave_types'))
            new = (int(raw), 1 if request.form.get(f'paid_{t.id}') else 0, 1 if request.form.get(f'active_{t.id}') else 0)
            if new != (t.days_per_year, t.paid, t.active):
                t.days_per_year, t.paid, t.active = new
                changed += 1
        name = hr.one_line(request.form.get('new_name'), 60)
        if name:
            if db.session.execute(select(LeaveType.id).where(func.lower(LeaveType.name) == name.lower())).first():
                flash(f'There is already a kind of leave called {name}.', 'error')
                return redirect(url_for('admin_hr_leave_types'))
            raw = str(request.form.get('new_days', '0')).strip() or '0'
            days = int(raw) if raw.isdigit() and int(raw) <= 366 else 0
            top = db.session.execute(select(func.max(LeaveType.sort_order))).scalar() or 0
            db.session.add(LeaveType(name=name, days_per_year=days, paid=1 if request.form.get('new_paid') else 0, active=1,
                                     sort_order=top + 1, created_at=_now()))
            changed += 1
        db.session.commit()
        audit_log('hr_leave_types_saved', 'staff_records', 'hr_leave_types', None, {'changed': changed})
        flash('Kinds of leave saved.' if changed else 'Nothing changed.', 'success')
        return redirect(url_for('admin_hr_leave_types'))
    return render_template('school_hr_leave_types.html', types=_types(active_only=False))


# ---------------------------------------------------------------- my leave (every member of staff with a record)
@app.route('/admin/my-leave')
@admin_required
def admin_my_leave():
    me = current_admin()
    record = db.session.scalars(select(StaffRecord).where(StaffRecord.admin_id == me['id'])).first()
    context = {'record': record, 'hr': hr}
    if record:
        year = _today().year
        context.update({
            'year': year, 'balances': _balances(record.id, year), 'types': {t.id: t for t in _types(active_only=False)},
            'active_types': _types(), 'today': _today().isoformat(),
            'requests': db.session.scalars(select(LeaveRequest).where(LeaveRequest.staff_id == record.id)
                                           .order_by(LeaveRequest.start_date.desc()).limit(HISTORY)).all(),
            'qualifications': db.session.scalars(select(StaffQualification).where(StaffQualification.staff_id == record.id)
                                                 .order_by(StaffQualification.year.desc().nulls_last())).all(),
        })
        context['names'] = _names({r.decided_by for r in context['requests']})
    response = make_response(render_template('school_my_leave.html', **context))
    response.headers['Cache-Control'] = 'no-store, private'
    return response


@app.post('/admin/my-leave')
@admin_required
@csrf_protect
def admin_my_leave_request():
    me = current_admin()
    record = db.session.scalars(select(StaffRecord).where(StaffRecord.admin_id == me['id'])).first()
    if record is None:
        flash('You have no staff record yet, so leave cannot be asked for here. Ask the school office.', 'error')
        return redirect(url_for('admin_my_leave'))
    leave, error = _new_request(record, request.form, me)
    if error:
        flash(error, 'error')
        return redirect(url_for('admin_my_leave'))
    db.session.add(leave)
    db.session.flush()
    leave_type = db.session.get(LeaveType, leave.leave_type_id)
    approvers = _approvers(me['id'])
    _notify(approvers, 'Leave to approve', f'{me["display_name"]} asks for {leave_type.name.lower()}, {leave.start_date} to '
            f'{leave.end_date} ({leave.days} working day{"s" if leave.days != 1 else ""}).', me, 'admin_hr_leave')
    db.session.commit()
    audit_log('hr_leave_requested', 'staff_records', 'hr_leave', leave.id, {'staff_id': record.id, 'days': leave.days})
    flash(f'Leave asked for: {leave.days} working day{"s" if leave.days != 1 else ""}. You will be told when it is decided.'
          + ('' if approvers else ' Nobody at the school may approve leave yet, so tell the school office.'), 'success')
    return redirect(url_for('admin_my_leave'))
