"""Attendance: the staff side.

A class teacher (or anyone with ``school.attendance.mark``) takes the register for one class on one
date, choosing present, late, absent or excused for each enrolled student; a status left blank is
simply not recorded, so nothing assumes a student was present by default. ``school.attendance.view``
sees the term summary without being able to change it. A staff member limited to some classes only
ever sees those classes, the same scope check every other school-portal page makes.
"""

from datetime import date as _date, datetime, timedelta, timezone

from flask import flash, redirect, render_template, request, url_for
from sqlalchemy import func, select

from app import ACADEMIC_TERMS, _school_current_session, app
from blueprints.school.attendance_data import (
    STATUSES, class_roster, class_summary, day_register, save_day_register,
)
from blueprints.school.helpers import _class_teacher_allowed, _school_class_allowed
from core.security import admin_required, audit_log, csrf_protect, current_admin
from models import AcademicSession, AttendanceRecord, SchoolClass, db

TERMS = ACADEMIC_TERMS


def _my_classes(register=False):
    """The classes the signed-in staff member may work with, in class order. ``register`` narrows it to
    the classes whose register they take: a teacher's own class-teacher classes, never a class they only
    teach a subject in. The term summary (read only) covers every class in reach."""
    me = current_admin()
    allowed = _class_teacher_allowed if register else _school_class_allowed
    classes = db.session.scalars(select(SchoolClass).where(SchoolClass.active == 1).order_by(SchoolClass.level_order)).all()
    return [c for c in classes if allowed(me['id'], c.id)]


def _sessions():
    return db.session.scalars(select(AcademicSession).where(AcademicSession.active == 1).order_by(AcademicSession.id.desc())).all()


def _choice(register=False):
    """The class, session and term picked on the page (query string), each checked against what exists
    and what this staff member may see. Returns ``(classes, sessions, class_row, session_row, term)``."""
    classes, sessions = _my_classes(register), _sessions()
    class_row = next((c for c in classes if c.id == request.values.get('class_id', type=int)), None)
    current = _school_current_session()
    wanted = request.values.get('session_id', type=int) or (current['id'] if current else None)
    session_row = next((s for s in sessions if s.id == wanted), sessions[0] if sessions else None)
    term = request.values.get('term', '').strip()
    term = term if term in TERMS else ACADEMIC_TERMS[0]
    return classes, sessions, class_row, session_row, term


def _today():
    return _date.today().isoformat()


def _valid_date(value):
    try:
        datetime.strptime(value, '%Y-%m-%d')
        return True
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------------------------
# The daily register
@app.route('/admin/school/attendance')
@admin_required
def admin_school_attendance():
    classes, sessions, class_row, session_row, term = _choice(register=True)
    date = request.values.get('date', '').strip()
    if not _valid_date(date):
        # No day asked for: today, or on a Saturday or Sunday the Friday before, the last day there was a register.
        today = _date.today()
        date = today.isoformat() if today.weekday() < 5 else _school_day(today, -1)
    rows = day_register(class_row.id, session_row.id, date) if class_row and session_row else []
    day = datetime.strptime(date, '%Y-%m-%d').date()
    return render_template('admin_school_attendance.html', classes=classes, sessions=sessions, terms=TERMS,
                           class_row=class_row, session_row=session_row, term=term, date=date, today=_today(),
                           rows=rows, statuses=STATUSES,
                           marked=sum(1 for r in rows if r['status']),
                           week=_week_strip(class_row, session_row, day),
                           prev_day=_school_day(day, -1), next_day=_school_day(day, 1) if date < _today() else None,
                           day_label=day.strftime('%A %d %B %Y').replace(' 0', ' '))


def _school_day(day, step):
    """The weekday before or after ``day`` (Saturday and Sunday are skipped), as YYYY-MM-DD."""
    nxt = day + timedelta(days=step)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=step)
    return nxt.isoformat()


def _week_strip(class_row, session_row, day):
    """The school week around ``day`` (Monday to Friday), each day with how many of the class were marked,
    so a day the register was not taken stands out."""
    monday = day - timedelta(days=day.weekday())
    days = [monday + timedelta(days=i) for i in range(5)]
    taken = {}
    if class_row and session_row:
        ids = [r['student_id'] for r in class_roster(class_row.id, session_row.id)]
        if ids:
            for d, n in db.session.execute(
                    select(AttendanceRecord.date, func.count())
                    .where(AttendanceRecord.student_id.in_(ids), AttendanceRecord.session_id == session_row.id,
                           AttendanceRecord.date >= days[0].isoformat(), AttendanceRecord.date <= days[-1].isoformat())
                    .group_by(AttendanceRecord.date)).all():
                taken[d] = n
    today = _today()
    return [{'date': d.isoformat(), 'label': d.strftime('%a'), 'day': d.day, 'marked': taken.get(d.isoformat(), 0),
             'future': d.isoformat() > today} for d in days]


@app.post('/admin/school/attendance')
@admin_required
@csrf_protect
def admin_school_attendance_save():
    me = current_admin()
    classes, sessions, class_row, session_row, term = _choice(register=True)
    date = request.values.get('date', '').strip()
    if not class_row or not session_row or not _valid_date(date):
        return redirect(url_for('admin_school_attendance'))
    # Only a student whose radio group was actually submitted is touched, the same way the report
    # card comments page only touches a textarea that was submitted: a partial or scripted
    # submission can never silently blank out students it did not mean to change.
    statuses = {}
    for r in class_roster(class_row.id, session_row.id):
        field = f"status_{r['student_id']}"
        if field in request.form:
            statuses[r['student_id']] = request.form.get(field, '').strip()
    saved, cleared = save_day_register(class_row.id, session_row.id, term, date, statuses, me['id'])
    if saved or cleared:
        db.session.commit()
        audit_log('attendance_saved', 'school', 'class', class_row.id,
                  {'date': date, 'session_id': session_row.id, 'saved': saved, 'cleared': cleared})
        flash(f'{saved} status{"" if saved == 1 else "es"} saved' + (f', {cleared} cleared' if cleared else '') + '.', 'success')
    else:
        db.session.rollback()
        flash('No status was changed.', 'success')
    return redirect(url_for('admin_school_attendance', class_id=class_row.id, session_id=session_row.id, term=term, date=date))


# ---------------------------------------------------------------------------------------------
# The term summary
@app.route('/admin/school/attendance/summary')
@admin_required
def admin_school_attendance_summary():
    classes, sessions, class_row, session_row, term = _choice()
    rows = class_summary(class_row.id, session_row.id, term) if class_row and session_row else []
    recorded = [r for r in rows if r['total']]
    totals = {k: sum(r[k] for r in rows) for k in ('present', 'late', 'absent', 'excused', 'total')}
    totals['percentage'] = round((totals['present'] + totals['late']) / totals['total'] * 100, 1) if totals['total'] else None
    totals['days'] = max((r['total'] for r in rows), default=0)
    totals['below'] = sum(1 for r in recorded if r['percentage'] is not None and r['percentage'] < 75)
    return render_template('admin_school_attendance_summary.html', classes=classes, sessions=sessions, terms=TERMS,
                           class_row=class_row, session_row=session_row, term=term, rows=rows, totals=totals,
                           can_mark=bool(_my_classes(register=True)))
