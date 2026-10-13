"""Staff attendance: clocking in and out (Clock in), the screen at the school that shows the changing code, the day's
register, the month, corrections, and the settings. The rules, and what is and is not trusted, are
core/staff_attendance.py; the tables models/staff_attendance.py.

Who may do what.
* Everyone whose account is linked to a staff record (models/hr.py) clocks in and out on Clock in, from their one
  registered phone, and asks for a correction of a day they could not clock.
* staff_attendance.view: the register of a day and the month, and its CSV.
* staff_attendance.manage: the settings, the screen's link, resetting someone's phone, closed days, marking someone in
  or out by hand (with a note, for staff without an account or a phone that failed), and deciding corrections.
The screen (/staff-attendance/display/<token>) needs no sign-in: it answers only for the current link's random token
and shows only the code. Every clock-in, refusal, hand-marking and decision is in the activity log, without positions.
"""

import csv
import hmac
import io
from datetime import date, datetime, timedelta, timezone

from flask import Response, abort, flash, g, jsonify, make_response, redirect, render_template, request, session, url_for
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import app
from blueprints.entrance.helpers import csv_safe
from control_plane.config import badge_cache_seconds
from control_plane.ratelimit import allow
from core import hr
from core import staff_attendance as sa
from core.id_cards import qr_path
from core.security import admin_has_permission, admin_required, audit_log, csrf_protect, current_admin
from core.short_cache import remember
from models import (
    Admin, AdminNotification, LeaveRequest, StaffAttendanceClosedDay, StaffAttendanceCorrection, StaffAttendanceDevice,
    StaffAttendanceRecord, StaffAttendanceSettings, StaffRecord, db,
)

GEO = 'camera=(), microphone=(), geolocation=(self)'
CORRECTION_DAYS = 31


def _now():
    return datetime.now(timezone.utc).isoformat()


def _may(code, admin=None):
    admin = admin or current_admin()
    return bool(admin and admin_has_permission(admin['id'], code))


def _settings(create=False):
    row = db.session.scalars(select(StaffAttendanceSettings).order_by(StaffAttendanceSettings.id)).first()
    if row is None and create:
        row = StaffAttendanceSettings(enabled=0, method='qr_gps', radius_m=150, max_accuracy_m=100, late_after='08:00',
                                      work_days='12345', code_secret=sa.new_secret())
        db.session.add(row)
        db.session.flush()
    return row


def _my_record(admin_id):
    return db.session.scalars(select(StaffRecord).where(StaffRecord.admin_id == admin_id)).first()


def _private(response, geo=False):
    response.headers['Cache-Control'] = 'no-store, private'
    if geo:
        response.headers['Permissions-Policy'] = GEO
    return response


def staff_clock_menu(admin=None):
    """For the menu: {'clock': may clock in (attendance is on and they have a record), 'register': may see the
    register, 'waiting': corrections waiting for this person's decision}. Read once a request."""
    admin = admin or current_admin()
    empty = {'clock': False, 'register': False, 'waiting': 0}
    if not admin:
        return empty
    cached = getattr(g, '_staff_clock_menu', None)
    if cached is not None:
        return cached

    def compute(admin_id):
        settings = _settings()
        on = bool(settings and settings.enabled)
        mine = db.session.execute(select(StaffRecord.id).where(StaffRecord.admin_id == admin_id, StaffRecord.status != 'left')).first()
        manage = admin_has_permission(admin_id, 'staff_attendance.manage')
        waiting = 0
        if manage:
            waiting = len([c for c in db.session.scalars(select(StaffAttendanceCorrection).where(StaffAttendanceCorrection.status == 'pending')).all()
                           if c.requested_by != admin_id])
        return {'clock': on and mine is not None, 'register': admin_has_permission(admin_id, 'staff_attendance.view'),
                'waiting': waiting}

    try:
        g._staff_clock_menu = remember('staff-clock-menu', badge_cache_seconds(), compute, admin['id'])
    except Exception:
        db.session.rollback()
        g._staff_clock_menu = empty
    return g._staff_clock_menu


app.jinja_env.globals.update(staff_clock_menu=staff_clock_menu)


# ---------------------------------------------------------------- the people and their days
def _people(day_from, day_to):
    """Staff records that were with the school at some point between the two days."""
    records = db.session.scalars(select(StaffRecord).order_by(StaffRecord.last_name, StaffRecord.first_name)).all()
    return [r for r in records if not (r.status == 'left' and r.exit_date and r.exit_date < day_from.isoformat())
            and not (r.employment_date and r.employment_date > day_to.isoformat())]


def _leave_days(staff_ids, day_from, day_to):
    """{(staff_id, 'YYYY-MM-DD')} of approved leave."""
    rows = db.session.scalars(select(LeaveRequest).where(
        LeaveRequest.status == 'approved', LeaveRequest.staff_id.in_(list(staff_ids) or [0]),
        LeaveRequest.start_date <= day_to.isoformat(), LeaveRequest.end_date >= day_from.isoformat())).all()
    out = set()
    for x in rows:
        d = max(date.fromisoformat(x.start_date), day_from)
        end = min(date.fromisoformat(x.end_date), day_to)
        while d <= end:
            out.add((x.staff_id, d.isoformat()))
            d += timedelta(days=1)
    return out


def _records(staff_ids, day_from, day_to):
    rows = db.session.scalars(select(StaffAttendanceRecord).where(
        StaffAttendanceRecord.staff_id.in_(list(staff_ids) or [0]), StaffAttendanceRecord.day >= day_from.isoformat(),
        StaffAttendanceRecord.day <= day_to.isoformat())).all()
    return {(r.staff_id, r.day): r for r in rows}


def _closed(day_from, day_to):
    return {c.day: c.label for c in db.session.scalars(select(StaffAttendanceClosedDay).where(
        StaffAttendanceClosedDay.day >= day_from.isoformat(), StaffAttendanceClosedDay.day <= day_to.isoformat())).all()}


def _status(settings, person, day, today, record, on_leave, closed):
    return sa.day_status(settings, day, today, None, record, on_leave, closed, person.employment_date,
                         person.exit_date if person.status == 'left' else None)


def _notify(admin_ids, title, message, actor, endpoint):
    now = _now()
    for aid in {a for a in admin_ids if a and a != actor['id']}:
        db.session.add(AdminNotification(admin_id=aid, title=title, message=message, severity='info', action_url=url_for(endpoint),
                                         actor_admin_id=actor['id'], actor_username_snapshot=actor['username'],
                                         actor_display_name_snapshot=actor['display_name'], created_at=now))


def _managers(exclude):
    ids = [a for (a,) in db.session.execute(select(Admin.id).where(Admin.active == 1, ~Admin.username.startswith('platform@'))).all()]
    return [a for a in ids if a != exclude and admin_has_permission(a, 'staff_attendance.manage')]


# ---------------------------------------------------------------- clock in (everyone with a record)
@app.route('/admin/clock')
@admin_required
def admin_clock():
    me = current_admin()
    settings = _settings()
    record = _my_record(me['id'])
    now = sa.school_now()
    today = now.date()
    context = {'settings': settings, 'record': record, 'sa': sa, 'code': (request.args.get('c') or '')[:40],
               'today': today.isoformat(), 'now_hhmm': now.strftime('%H:%M')}
    if settings and record:
        context['day'] = db.session.scalars(select(StaffAttendanceRecord).where(
            StaffAttendanceRecord.staff_id == record.id, StaffAttendanceRecord.day == today.isoformat())).first()
        start = today - timedelta(days=CORRECTION_DAYS)
        days = [start + timedelta(days=i) for i in range((today - start).days + 1)]
        records = _records([record.id], start, today)
        leave = _leave_days([record.id], start, today)
        closed = _closed(start, today)
        context['history'] = [{'day': d, 'record': records.get((record.id, d.isoformat())),
                               'status': _status(settings, record, d, today, records.get((record.id, d.isoformat())),
                                                 (record.id, d.isoformat()) in leave, d.isoformat() in closed)}
                              for d in reversed(days)]
        context['corrections'] = db.session.scalars(select(StaffAttendanceCorrection).where(StaffAttendanceCorrection.staff_id == record.id)
                                                    .order_by(StaffAttendanceCorrection.id.desc()).limit(20)).all()
        device = db.session.scalars(select(StaffAttendanceDevice).where(StaffAttendanceDevice.admin_id == me['id'],
                                                                        StaffAttendanceDevice.active == 1)).first()
        context['device'] = device
        context['this_device'] = bool(device and hmac.compare_digest(sa.token_hash(request.cookies.get(sa.COOKIE)), device.tag_hash))
        context['earliest'] = (today - timedelta(days=CORRECTION_DAYS)).isoformat()
    return _private(make_response(render_template('school_clock.html', **context)), geo=True)


def _refuse(me, reason, message, status=400):
    audit_log('staff_clock_refused', 'staff_attendance', 'admin', me['id'], {'reason': reason}, False)
    return jsonify(ok=False, error=message), status


@app.post('/admin/clock')
@admin_required
@csrf_protect
def admin_clock_punch():
    me = current_admin()
    settings = _settings()
    if not settings or not settings.enabled:
        return jsonify(ok=False, error='The school is not taking staff attendance on the portal.'), 400
    record = _my_record(me['id'])
    if record is None or record.status == 'left':
        return jsonify(ok=False, error='You have no staff record, so you cannot clock in here. Ask the school office.'), 400
    tenant = g.get('tenant')
    if not allow(f'staff-clock:{tenant.id if tenant else 0}:{me["id"]}', 12, 600):
        return _refuse(me, 'too_many', 'Too many tries. Wait ten minutes and try again.', 429)
    action = request.form.get('action')
    if action not in ('in', 'out'):
        abort(400)
    if settings.method in ('qr_gps', 'qr') and not sa.code_is_current(settings.code_secret, request.form.get('code')):
        return _refuse(me, 'code', 'That code is not the one on the school\'s screen now (it changes every 30 seconds). '
                                   'Scan the screen again, or type the six digits it shows.')
    distance = accuracy = None
    if settings.method in ('qr_gps', 'gps'):
        distance, accuracy, error = sa.check_position(settings, sa.read_position(request.form))
        if error:
            return _refuse(me, 'location', error)
    # The phone: theirs, or the first one they ever clock in with.
    device = db.session.scalars(select(StaffAttendanceDevice).where(StaffAttendanceDevice.admin_id == me['id'],
                                                                    StaffAttendanceDevice.active == 1)).first()
    new_tag = None
    if device is None:
        new_tag = sa.new_secret()
        device = StaffAttendanceDevice(admin_id=me['id'], tag_hash=sa.token_hash(new_tag), label=sa.device_label(request.user_agent.string),
                                       registered_at=_now(), active=1)
        db.session.add(device)
    elif not hmac.compare_digest(sa.token_hash(request.cookies.get(sa.COOKIE)), device.tag_hash):
        return _refuse(me, 'device', f'This is not the phone you clock in with ({device.label}, from {device.registered_at[:10]}). '
                                     'If you have changed phones, ask the attendance manager to reset it.', 403)
    now = sa.school_now()
    hhmm, today = now.strftime('%H:%M'), now.date().isoformat()
    day = db.session.scalars(select(StaffAttendanceRecord).where(StaffAttendanceRecord.staff_id == record.id,
                                                                 StaffAttendanceRecord.day == today).with_for_update()).first()
    if action == 'in':
        if day and day.clock_in:
            db.session.rollback()
            return jsonify(ok=False, error=f'You clocked in already today, at {day.clock_in}.'), 400
        if day is None:
            day = StaffAttendanceRecord(staff_id=record.id, day=today, created_at=_now())
            db.session.add(day)
        day.clock_in, day.late, day.in_method = hhmm, 1 if sa.is_late(settings, hhmm) else 0, 'phone'
        day.in_distance_m, day.in_accuracy_m, day.updated_at = distance, accuracy, _now()
        message = f'Clocked in at {hhmm}' + (' (late).' if day.late else '.')
    else:
        if not day or not day.clock_in:
            db.session.rollback()
            return jsonify(ok=False, error='You have not clocked in today, so you cannot clock out.'), 400
        if day.clock_out:
            db.session.rollback()
            return jsonify(ok=False, error=f'You clocked out already today, at {day.clock_out}.'), 400
        day.clock_out, day.out_method, day.out_distance_m, day.out_accuracy_m, day.updated_at = hhmm, 'phone', distance, accuracy, _now()
        message = f'Clocked out at {hhmm}.'
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify(ok=False, error='That was sent twice. Look at today\'s record below.'), 409
    audit_log('staff_clock_' + action, 'staff_attendance', 'hr_staff', record.id,
              {'day': today, 'time': hhmm, 'distance_m': distance, 'accuracy_m': accuracy, 'new_device': bool(new_tag)})
    response = jsonify(ok=True, message=message + (' This phone is now the one you clock in with.' if new_tag else ''))
    if new_tag:
        response.set_cookie(sa.COOKIE, new_tag, max_age=5 * 365 * 24 * 3600, httponly=True, samesite='Lax', secure=request.is_secure)
    return response


@app.post('/admin/clock/correction')
@admin_required
@csrf_protect
def admin_clock_correction():
    me = current_admin()
    record = _my_record(me['id'])
    back = redirect(url_for('admin_clock'))
    if record is None:
        flash('You have no staff record. Ask the school office.', 'error')
        return back
    today = sa.school_now().date()
    day = sa.parse_day(request.form.get('day'))
    kind, when = request.form.get('kind'), str(request.form.get('time', '')).strip()
    reason = hr.one_line(request.form.get('reason'), 300)
    if not day or day > today or day < today - timedelta(days=CORRECTION_DAYS):
        flash(f'Choose a day in the last {CORRECTION_DAYS} days.', 'error')
        return back
    if kind not in ('in', 'out') or not sa.is_hhmm(when) or not reason:
        flash('Say whether it is your arrival or your leaving, the time, and why.', 'error')
        return back
    existing = db.session.scalars(select(StaffAttendanceRecord).where(StaffAttendanceRecord.staff_id == record.id,
                                                                      StaffAttendanceRecord.day == day.isoformat())).first()
    if kind == 'out' and not (existing and existing.clock_in):
        flash('That day has no arrival yet. Ask for the arrival first.', 'error')
        return back
    if kind == 'out' and when <= existing.clock_in:
        flash(f'You arrived at {existing.clock_in} that day: leaving must be later.', 'error')
        return back
    if db.session.execute(select(StaffAttendanceCorrection.id).where(
            StaffAttendanceCorrection.staff_id == record.id, StaffAttendanceCorrection.day == day.isoformat(),
            StaffAttendanceCorrection.kind == kind, StaffAttendanceCorrection.status == 'pending')).first():
        flash('You have already asked for that. It is waiting for a decision.', 'error')
        return back
    db.session.add(StaffAttendanceCorrection(staff_id=record.id, day=day.isoformat(), kind=kind, time=when, reason=reason,
                                             status='pending', requested_by=me['id'], requested_at=_now()))
    _notify(_managers(me['id']), 'Attendance correction to decide',
            f'{me["display_name"]} asks to record {"an arrival" if kind == "in" else "leaving"} at {when} on {day.isoformat()}.', me,
            'admin_staff_attendance')
    db.session.commit()
    audit_log('staff_attendance_correction_requested', 'staff_attendance', 'hr_staff', record.id, {'day': day.isoformat(), 'kind': kind})
    flash('Correction asked for. You will be told when it is decided.', 'success')
    return back


# ---------------------------------------------------------------- the register
@app.route('/admin/staff-attendance')
@admin_required
def admin_staff_attendance():
    settings = _settings()
    today = sa.school_now().date()
    day = sa.parse_day(request.args.get('day')) or today
    people = _people(day, day)
    ids = [p.id for p in people]
    records, leave, closed = _records(ids, day, day), _leave_days(ids, day, day), _closed(day, day)
    rows = []
    counts = {}
    for p in people:
        rec = records.get((p.id, day.isoformat()))
        status = _status(settings, p, day, today, rec, (p.id, day.isoformat()) in leave, day.isoformat() in closed) if settings else 'not_counted'
        counts[status] = counts.get(status, 0) + 1
        rows.append({'person': p, 'record': rec, 'status': status})
    corrections = []
    if _may('staff_attendance.manage'):
        pending = db.session.scalars(select(StaffAttendanceCorrection).where(StaffAttendanceCorrection.status == 'pending')
                                     .order_by(StaffAttendanceCorrection.day)).all()
        names = {r.id: r for r in db.session.scalars(select(StaffRecord).where(StaffRecord.id.in_([c.staff_id for c in pending] or [0]))).all()}
        corrections = [{'c': c, 'person': names.get(c.staff_id), 'mine': c.requested_by == current_admin()['id']} for c in pending]
    response = make_response(render_template(
        'school_staff_attendance.html', settings=settings, day=day, today=today, rows=rows, counts=counts,
        closed_label=closed.get(day.isoformat()), corrections=corrections, can_manage=_may('staff_attendance.manage'),
        prev_day=(day - timedelta(days=1)).isoformat(), next_day=(day + timedelta(days=1)).isoformat(), sa=sa, hr=hr))
    return _private(response)


@app.post('/admin/staff-attendance/mark')
@admin_required
@csrf_protect
def admin_staff_attendance_mark():
    """Marking someone in or out by hand: staff without an account, or a phone that would not work. A time made on a
    phone is never overwritten here; that is what a correction is for."""
    me = current_admin()
    settings = _settings(create=True)
    person = db.session.get(StaffRecord, request.form.get('staff_id', type=int) or 0)
    day = sa.parse_day(request.form.get('day'))
    kind, when = request.form.get('kind'), str(request.form.get('time', '')).strip()
    note = hr.one_line(request.form.get('note'), 200)
    today = sa.school_now().date()
    back = redirect(url_for('admin_staff_attendance', day=day.isoformat() if day else None))
    if person is None or not day or day > today or kind not in ('in', 'out') or not sa.is_hhmm(when):
        flash('Choose the person, the time, and whether they arrived or left.', 'error')
        return back
    if not note:
        flash('Add a note saying why this is marked by hand.', 'error')
        return back
    rec = db.session.scalars(select(StaffAttendanceRecord).where(StaffAttendanceRecord.staff_id == person.id,
                                                                 StaffAttendanceRecord.day == day.isoformat()).with_for_update()).first()
    if kind == 'in':
        if rec and rec.clock_in:
            db.session.rollback()
            flash(f'{hr.full_name(person, False)} already arrived at {rec.clock_in} that day.', 'error')
            return back
        if rec is None:
            rec = StaffAttendanceRecord(staff_id=person.id, day=day.isoformat(), created_at=_now())
            db.session.add(rec)
        rec.clock_in, rec.late, rec.in_method = when, 1 if sa.is_late(settings, when) else 0, 'register'
    else:
        if not rec or not rec.clock_in or rec.clock_out or when <= rec.clock_in:
            db.session.rollback()
            flash('Leaving can be marked only after an arrival, once, and later than it.', 'error')
            return back
        rec.clock_out, rec.out_method = when, 'register'
    rec.note, rec.marked_by, rec.updated_at = note, me['id'], _now()
    db.session.commit()
    audit_log('staff_attendance_marked', 'staff_attendance', 'hr_staff', person.id, {'day': day.isoformat(), 'kind': kind, 'time': when})
    flash(f'{hr.full_name(person, False)}: {"arrival" if kind == "in" else "leaving"} at {when} marked.', 'success')
    return back


@app.post('/admin/staff-attendance/corrections/<int:cid>/decide')
@admin_required
@csrf_protect
def admin_staff_attendance_correction_decide(cid):
    me = current_admin()
    c = db.session.scalars(select(StaffAttendanceCorrection).where(StaffAttendanceCorrection.id == cid).with_for_update()).first()
    if c is None:
        abort(404)
    back = redirect(url_for('admin_staff_attendance'))
    if c.status != 'pending' or c.requested_by == me['id']:
        db.session.rollback()
        flash('That correction is no longer waiting, or it is your own (someone else decides it).', 'error')
        return back
    approve = request.form.get('decision') == 'approve'
    settings = _settings(create=True)
    if approve:
        rec = db.session.scalars(select(StaffAttendanceRecord).where(StaffAttendanceRecord.staff_id == c.staff_id,
                                                                     StaffAttendanceRecord.day == c.day).with_for_update()).first()
        if c.kind == 'in':
            if rec is None:
                rec = StaffAttendanceRecord(staff_id=c.staff_id, day=c.day, created_at=_now())
                db.session.add(rec)
            if rec.clock_out and c.time >= rec.clock_out:
                db.session.rollback()
                flash(f'They left at {rec.clock_out} that day, so they cannot have arrived at {c.time}.', 'error')
                return back
            rec.clock_in, rec.late, rec.in_method = c.time, 1 if sa.is_late(settings, c.time) else 0, 'correction'
        else:
            if not rec or not rec.clock_in or c.time <= rec.clock_in:
                db.session.rollback()
                flash('That day has no arrival before that time, so leaving cannot be recorded.', 'error')
                return back
            rec.clock_out, rec.out_method = c.time, 'correction'
        rec.note, rec.marked_by, rec.updated_at = f'Correction: {c.reason}'[:200], me['id'], _now()
    c.status, c.decided_by, c.decided_at = ('approved' if approve else 'declined'), me['id'], _now()
    c.decision_note = hr.one_line(request.form.get('note'), 200) or None
    _notify([c.requested_by], 'Attendance correction ' + ('approved' if approve else 'declined'),
            f'{c.day}, {"arrival" if c.kind == "in" else "leaving"} at {c.time}: {"approved" if approve else "declined"} by {me["display_name"]}.',
            me, 'admin_clock')
    db.session.commit()
    audit_log('staff_attendance_correction_' + ('approved' if approve else 'declined'), 'staff_attendance', 'hr_staff', c.staff_id,
              {'day': c.day, 'kind': c.kind})
    flash('Correction approved and recorded.' if approve else 'Correction declined.', 'success')
    return back


# ---------------------------------------------------------------- the month
@app.route('/admin/staff-attendance/month')
@admin_required
def admin_staff_attendance_month():
    settings = _settings()
    today = sa.school_now().date()
    try:
        year, month = (int(p) for p in str(request.args.get('month', '')).split('-'))
        date(year, month, 1)
    except (ValueError, TypeError):
        year, month = today.year, today.month
    days = sa.month_days(year, month)
    people = _people(days[0], days[-1])
    ids = [p.id for p in people]
    records, leave, closed = _records(ids, days[0], days[-1]), _leave_days(ids, days[0], days[-1]), _closed(days[0], days[-1])
    rows = []
    for p in people:
        cells, totals = [], {'present': 0, 'late': 0, 'absent': 0, 'leave': 0}
        for d in days:
            rec = records.get((p.id, d.isoformat()))
            status = _status(settings, p, d, today, rec, (p.id, d.isoformat()) in leave, d.isoformat() in closed) if settings else 'not_counted'
            if status in totals:
                totals[status] += 1
            if status == 'late':
                totals['present'] += 1
            cells.append({'day': d, 'status': status, 'record': rec})
        rows.append({'person': p, 'cells': cells, 'totals': totals})
    label = days[0].strftime('%B %Y')
    if request.args.get('format') == 'csv':
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(['Name', 'Staff number', 'Job', 'Days in', 'Of which late', 'Absent', 'On leave']
                        + [d.strftime('%d %a') for d in days])
        for r in rows:
            p, t = r['person'], r['totals']
            writer.writerow([csv_safe(hr.full_name(p, False)), csv_safe(p.staff_no or ''), csv_safe(p.job_title or ''),
                             t['present'], t['late'], t['absent'], t['leave']]
                            + [(c['record'].clock_in + (f'-{c["record"].clock_out}' if c['record'].clock_out else '')) if c['record'] and c['record'].clock_in
                               else sa.STATUS_LABELS.get(c['status'], '') for c in r['cells']])
        audit_log('staff_attendance_exported', 'staff_attendance', 'month', f'{year:04d}-{month:02d}', {'people': len(rows)})
        response = Response(out.getvalue(), mimetype='text/csv',
                            headers={'Content-Disposition': f'attachment; filename=staff-attendance-{year:04d}-{month:02d}.csv'})
        return _private(response)
    prev_month = (days[0] - timedelta(days=1)).strftime('%Y-%m')
    next_month = (days[-1] + timedelta(days=1)).strftime('%Y-%m')
    return _private(make_response(render_template('school_staff_attendance_month.html', settings=settings, rows=rows, days=days,
                                                  label=label, month=f'{year:04d}-{month:02d}', prev_month=prev_month,
                                                  next_month=next_month, today=today, closed=closed, sa=sa, hr=hr)))


# ---------------------------------------------------------------- settings
@app.route('/admin/staff-attendance/settings', methods=['GET', 'POST'])
@admin_required
@csrf_protect
def admin_staff_attendance_settings():
    me = current_admin()
    settings = _settings(create=True)
    if request.method == 'POST':
        errors = []
        method = request.form.get('method')
        settings.method = method if method in sa.METHODS else 'qr_gps'
        lat, lng = str(request.form.get('latitude', '')).strip(), str(request.form.get('longitude', '')).strip()
        if lat or lng:
            position = sa.read_position({'lat': lat, 'lng': lng, 'acc': '0'})
            if position is None:
                errors.append('The school\'s location must be a latitude and a longitude, such as 6.5244 and 3.3792.')
            else:
                settings.latitude, settings.longitude = round(position[0], 6), round(position[1], 6)
        for name, low, high, label in (('radius_m', 30, 2000, 'The distance allowed'), ('max_accuracy_m', 10, 500, 'The accuracy asked for')):
            raw = str(request.form.get(name, '')).strip()
            if not raw.isdigit() or not (low <= int(raw) <= high):
                errors.append(f'{label} must be from {low} to {high} metres.')
            else:
                setattr(settings, name, int(raw))
        late = str(request.form.get('late_after', '')).strip()
        if not sa.is_hhmm(late):
            errors.append('Late after must be a time, such as 08:00.')
        else:
            settings.late_after = late
        work = ''.join(d for d in '1234567' if request.form.get(f'day_{d}'))
        if not work:
            errors.append('Tick at least one work day.')
        else:
            settings.work_days = work
        enabled = request.form.get('enabled') == '1'
        if enabled and settings.method != 'qr' and (settings.latitude is None or settings.longitude is None):
            errors.append('Set the school\'s location before turning attendance on, or choose the screen\'s code only.')
        if errors:
            db.session.rollback()
            for e in errors:
                flash(e, 'error')
            return redirect(url_for('admin_staff_attendance_settings'))
        if enabled and not settings.enabled and not settings.started_on:
            settings.started_on = sa.school_now().date().isoformat()
        settings.enabled = 1 if enabled else 0
        settings.code_secret = settings.code_secret or sa.new_secret()
        settings.updated_at, settings.updated_by = _now(), me['id']
        db.session.commit()
        audit_log('staff_attendance_settings_saved', 'staff_attendance', 'settings', None,
                  {'enabled': settings.enabled, 'method': settings.method, 'radius_m': settings.radius_m})
        flash('Staff attendance settings saved.', 'success')
        return redirect(url_for('admin_staff_attendance_settings'))
    db.session.commit()
    devices = db.session.scalars(select(StaffAttendanceDevice).where(StaffAttendanceDevice.active == 1)
                                 .order_by(StaffAttendanceDevice.registered_at.desc())).all()
    owners = {a.id: a.display_name for a in db.session.scalars(select(Admin).where(Admin.id.in_([d.admin_id for d in devices] or [0]))).all()}
    closed = db.session.scalars(select(StaffAttendanceClosedDay).where(StaffAttendanceClosedDay.day >= (sa.school_now().date() - timedelta(days=60)).isoformat())
                                .order_by(StaffAttendanceClosedDay.day)).all()
    # The screen's new link is shown once, straight after it is made; only its hash is kept.
    raw = session.pop('staff_display_link', None)
    link = url_for('staff_attendance_display', token=raw, _external=True) if raw and settings.display_token_hash and hmac.compare_digest(
        sa.token_hash(raw), settings.display_token_hash) else None
    return _private(make_response(render_template('school_staff_attendance_settings.html', settings=settings, devices=devices, owners=owners,
                                                  closed=closed, link=link, sa=sa)), geo=True)


@app.post('/admin/staff-attendance/display')
@admin_required
@csrf_protect
def admin_staff_attendance_display():
    """A new link for the screen at the school. The old link stops working at once, and so do the codes it showed."""
    me = current_admin()
    settings = _settings(create=True)
    raw = sa.new_secret()
    settings.display_token_hash, settings.display_set_at = sa.token_hash(raw), _now()
    settings.code_secret = sa.new_secret()
    db.session.commit()
    audit_log('staff_attendance_display_link', 'staff_attendance', 'settings', None, {})
    flash('A new link for the screen is ready. Open it on the screen at the school; the old link no longer works.', 'success')
    session['staff_display_link'] = raw
    return redirect(url_for('admin_staff_attendance_settings', _anchor='display'))


@app.post('/admin/staff-attendance/devices/<int:did>/reset')
@admin_required
@csrf_protect
def admin_staff_attendance_device_reset(did):
    me = current_admin()
    device = db.session.get(StaffAttendanceDevice, did)
    if device is None:
        abort(404)
    if device.active:
        device.active, device.revoked_at, device.revoked_by = 0, _now(), me['id']
        _notify([device.admin_id], 'Clock-in phone reset', 'The next phone you clock in with becomes your clock-in phone.', me, 'admin_clock')
        db.session.commit()
        audit_log('staff_attendance_device_reset', 'staff_attendance', 'admin', device.admin_id, {})
        flash('Phone reset. The next phone they clock in with becomes theirs.', 'success')
    return redirect(url_for('admin_staff_attendance_settings', _anchor='phones'))


@app.post('/admin/staff-attendance/closed')
@admin_required
@csrf_protect
def admin_staff_attendance_closed():
    me = current_admin()
    back = redirect(url_for('admin_staff_attendance_settings', _anchor='closed'))
    remove = request.form.get('remove', type=int)
    if remove:
        row = db.session.get(StaffAttendanceClosedDay, remove)
        if row:
            db.session.delete(row)
            db.session.commit()
            audit_log('staff_attendance_closed_day_removed', 'staff_attendance', 'closed_day', remove, {'day': row.day})
        return back
    first, last = sa.parse_day(request.form.get('day')), sa.parse_day(request.form.get('until') or request.form.get('day'))
    label = hr.one_line(request.form.get('label'), 80)
    if not first or not last or last < first or (last - first).days > 120 or not label:
        flash('Give the day (or the first and last day, at most four months apart) and what it is.', 'error')
        return back
    have = {d for (d,) in db.session.execute(select(StaffAttendanceClosedDay.day).where(
        StaffAttendanceClosedDay.day >= first.isoformat(), StaffAttendanceClosedDay.day <= last.isoformat())).all()}
    d, added = first, 0
    while d <= last:
        if d.isoformat() not in have:
            db.session.add(StaffAttendanceClosedDay(day=d.isoformat(), label=label, created_at=_now(), created_by=me['id']))
            added += 1
        d += timedelta(days=1)
    db.session.commit()
    audit_log('staff_attendance_closed_days_added', 'staff_attendance', 'closed_day', None, {'from': first.isoformat(), 'to': last.isoformat(), 'added': added})
    flash(f'{added} day{"s" if added != 1 else ""} marked as closed: {label}.', 'success')
    return back


# ---------------------------------------------------------------- the screen at the school (no sign-in)
def _display_settings(token):
    settings = _settings()
    if (not settings or not settings.display_token_hash or not token or len(token) > 100
            or not hmac.compare_digest(sa.token_hash(token), settings.display_token_hash)):
        return None
    return settings


def _screen(response):
    response.headers['Cache-Control'] = 'no-store, private'
    response.headers['X-Robots-Tag'] = 'noindex, nofollow'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


@app.route('/staff-attendance/display/<token>')
def staff_attendance_display(token):
    settings = _display_settings(token)
    if settings is None:
        return _screen(make_response(render_template('staff_attendance_display.html', settings=None), 404))
    return _screen(make_response(render_template('staff_attendance_display.html', settings=settings, token=token, sa=sa)))


@app.route('/staff-attendance/display/<token>/code')
def staff_attendance_display_code(token):
    settings = _display_settings(token)
    if settings is None or not settings.enabled:
        return _screen(make_response(jsonify(ok=False), 404))
    qr, digits = sa.code_for(settings.code_secret, sa.window_of())
    path, size = qr_path(url_for('admin_clock', c=qr, _external=True))
    return _screen(make_response(jsonify(ok=True, path=path, size=size, digits=digits, left=sa.seconds_left(),
                                         time=sa.school_now().strftime('%H:%M'))))
