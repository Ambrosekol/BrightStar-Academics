"""Birthday reminders for class teachers.

Two days before a student's birthday, the staff who teach that student's class get a notification in
their bell in the admin portal ("Ada Obi (JSS 1) turns 12 on Fri 11 Oct").

*Who* is told: every active staff member who has access to the student's class (a class scope on
their account) and may write report card comments - the same people the school already treats as that
class's teachers. A student who is not in a class, or a class nobody is assigned to, notifies no one.

*When* it runs: there is no separate scheduler to set up. The first time anyone opens an admin page
on a given day, the school checks for birthdays coming up and notifies the teachers, once. If nobody
opens the portal for a day or two, the next visit still covers the birthdays that are now today,
tomorrow or two days away, so none is lost. What was already sent is remembered, so nobody is told
twice about the same birthday.

A student's date of birth is free text on their record, so it is read as day/month/year or
year-month-day; one that cannot be read is skipped.
"""

import json
from datetime import date, datetime, timedelta, timezone

from flask import current_app, url_for
from sqlalchemy import and_, select

from blueprints.finance.helpers import _primary_school_id
from core.db_helpers import all_rows, one_scalar
from core.security import admin_has_permission
from models import (
    AcademicSession, Admin, AdminNotification, AdminScope, SchoolClass, SchoolSetting, Student, StudentEnrolment, db,
)

WARN_DAYS = 2
CHECKED_KEY = 'birthday_checked_on'
NOTIFIED_KEY = 'birthday_notified'
_FORMATS = ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%Y/%m/%d', '%d.%m.%Y', '%d %B %Y', '%d %b %Y')


def parse_dob(text):
    """A date from what was typed on the student's record, or None when it cannot be read."""
    text = (text or '').strip()
    for candidate in (text, text[:10]):          # a stored timestamp keeps only its date
        for fmt in _FORMATS:
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


def next_birthday(dob, today):
    """The first date on or after ``today`` that is this birthday. A 29 February birthday falls on
    28 February in a year that has no 29th."""
    for year in (today.year, today.year + 1):
        try:
            candidate = dob.replace(year=year)
        except ValueError:
            candidate = date(year, 2, 28)
        if candidate >= today:
            return candidate
    return None


def _setting(key):
    school_id = _primary_school_id()
    return one_scalar(select(SchoolSetting.setting_value).where(
        SchoolSetting.school_id == school_id, SchoolSetting.setting_key == key)) if school_id else None


def _save_setting(key, value):
    school_id = _primary_school_id()
    if not school_id:
        return
    now = datetime.now(timezone.utc).isoformat()
    row = db.session.scalars(select(SchoolSetting).where(
        SchoolSetting.school_id == school_id, SchoolSetting.setting_key == key)).first()
    if row:
        row.setting_value, row.updated_at = value, now
    else:
        db.session.add(SchoolSetting(school_id=school_id, setting_key=key, setting_value=value, updated_at=now))


def _teachers(class_name):
    """The active staff with access to this class who may write its report card comments."""
    ids = [r['admin_id'] for r in all_rows(
        select(AdminScope.admin_id).join(Admin, Admin.id == AdminScope.admin_id)
        .where(AdminScope.scope_type == 'class', AdminScope.scope_value == class_name, Admin.active == 1).distinct())]
    return [a for a in ids if admin_has_permission(a, 'report_cards.comment')]


def _when(days):
    return 'today' if days == 0 else 'tomorrow' if days == 1 else f'in {days} days'


def run_birthday_check(today=None):
    """Notify the teachers of every birthday from today to two days away that has not been announced
    yet. Does nothing if it has already run today. Returns how many notifications were made."""
    today = today or date.today()
    if _setting(CHECKED_KEY) == today.isoformat():
        return 0
    # Mark the day as done first, so two people opening a page at once do not both send.
    _save_setting(CHECKED_KEY, today.isoformat())
    db.session.commit()

    try:
        sent = set(json.loads(_setting(NOTIFIED_KEY) or '[]'))
    except ValueError:
        sent = set()
    # Remembered birthdays only matter for the year they were announced in.
    sent = {k for k in sent if k.rsplit(':', 1)[-1] in (str(today.year), str(today.year + 1))}

    rows = all_rows(
        select(Student.id, Student.first_name, Student.last_name, Student.date_of_birth, SchoolClass.name.label('class_name'))
        .join(StudentEnrolment, and_(StudentEnrolment.student_id == Student.id, StudentEnrolment.active == 1))
        .join(SchoolClass, SchoolClass.id == StudentEnrolment.class_id)
        .join(AcademicSession, AcademicSession.id == StudentEnrolment.session_id)
        .where(Student.active == 1, AcademicSession.is_current == 1, Student.date_of_birth.is_not(None)))
    made, teachers = 0, {}
    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        dob = parse_dob(r['date_of_birth'])
        if dob is None:
            continue
        day = next_birthday(dob, today)
        if day is None or (day - today).days > WARN_DAYS:
            continue
        key = f"{r['id']}:{day.year}"
        if key in sent:
            continue
        sent.add(key)
        if r['class_name'] not in teachers:
            teachers[r['class_name']] = _teachers(r['class_name'])
        name = f"{r['first_name']} {r['last_name']}".strip()
        age = day.year - dob.year
        for admin_id in teachers[r['class_name']]:
            db.session.add(AdminNotification(
                admin_id=admin_id, title=f'Birthday {_when((day - today).days)}: {name}',
                message=f"{name} ({r['class_name']}) turns {age} on {day.strftime('%a')} {day.day} {day.strftime('%b')}.",
                severity='info', action_url=url_for('admin_school_student_detail', sid=r['id']), created_at=now))
            made += 1
    _save_setting(NOTIFIED_KEY, json.dumps(sorted(sent)))
    db.session.commit()
    return made


def run_birthday_check_safely():
    """The page-load hook: a failure here must never take an admin page down."""
    try:
        return run_birthday_check()
    except Exception:
        db.session.rollback()
        current_app.logger.exception('The daily birthday check failed')
        return 0
