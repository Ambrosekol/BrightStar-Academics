"""Birthdays: reminders for class teachers, and a greeting texted to the parents.

Two days before a student's birthday, the staff who teach that student's class get a notification in
their bell in the admin portal ("Ada Obi (JSS 1) turns 12 on Fri 11 Oct").

On the birthday itself, if the school has switched it on (Email & SMS, Birthday texts), each of the
student's parents is texted the school's greeting ("Happy birthday, Ada! Everyone at Bright Star Academy
wishes you..."), in the school's own words if it has rewritten them (Settings, Message wording). It goes to the same numbers as every other text about the student (core/notifications.py)
and is written to the notice log like them.

*Who* is told: every active staff member who has access to the student's class (a class scope on
their account) and may write report card comments - the same people the school already treats as that
class's teachers. A student who is not in a class, or a class nobody is assigned to, notifies no one.
Greetings go to the active students enrolled in the current session.

*When* it runs: there is no separate scheduler to set up. The first time anyone opens an admin page
on a given day, the school checks for birthdays coming up and notifies the teachers, once. If nobody
opens the portal for a day or two, the next visit still covers the birthdays that are now today,
tomorrow or two days away, so none is lost. What was already sent is remembered, so nobody is told
twice about the same birthday. The greetings go from 7 in the morning to 8 in the evening, school time,
on the day itself only (never a day late, never at night); ``python -m control_plane sweep-jobs`` on a
timer also sends them, for a weekend or holiday when nobody opens the portal. The day is claimed in the
database before anything is sent, so two servers or two people opening a page at once never text a
parent twice.

A student's date of birth is free text on their record, so it is read as day/month/year or
year-month-day; one that cannot be read is skipped.
"""

import json
from datetime import date, datetime, time, timedelta, timezone

from flask import current_app, url_for
from sqlalchemy import and_, distinct, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from blueprints.finance.helpers import _primary_school_id
from core.message_wording import render
from core.db_helpers import all_rows, one_scalar, tuples
from core.jobs import enqueue, job_handler
from core.security import admin_has_permission
from core.staff_attendance import school_now, zone
from models import (
    AcademicSession, Admin, AdminNotification, AdminScope, NotificationDeliveryLog, SchoolClass, SchoolSetting, Student,
    StudentEnrolment, db,
)

WARN_DAYS = 2
CHECKED_KEY = 'birthday_checked_on'
NOTIFIED_KEY = 'birthday_notified'
_FORMATS = ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%Y/%m/%d', '%d.%m.%Y', '%d %B %Y', '%d %b %Y')

# ---- the parents' birthday text (its words: 'sms_birthday' in core/message_wording.py)
TEXT_ON_KEY = 'birthday_sms_enabled'        # '1' when the school sends them
TEXT_DAY_KEY = 'birthday_sms_sent_on'       # the school date whose greetings have been started (the claim)
NOTICE_KIND = 'birthday'
SEND_FROM_HOUR, SEND_UNTIL_HOUR = 7, 20     # school time


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


def _save_setting(key, value, admin_id=None):
    school_id = _primary_school_id()
    if not school_id:
        return
    now = datetime.now(timezone.utc).isoformat()
    row = db.session.scalars(select(SchoolSetting).where(
        SchoolSetting.school_id == school_id, SchoolSetting.setting_key == key)).first()
    if row:
        row.setting_value, row.updated_at = value, now
        if admin_id:
            row.updated_by = admin_id
    else:
        db.session.add(SchoolSetting(school_id=school_id, setting_key=key, setting_value=value, updated_at=now,
                                     updated_by=admin_id))


def _claim(key, value):
    """Set a setting to ``value`` only if it is not that already, in one statement: True for the one caller that
    changed it. What makes "once a day" hold across threads, processes and servers."""
    school_id = _primary_school_id()
    if not school_id:
        return False
    now = datetime.now(timezone.utc).isoformat()
    if one_scalar(select(SchoolSetting.id).where(SchoolSetting.school_id == school_id,
                                                 SchoolSetting.setting_key == key)) is None:
        try:
            db.session.add(SchoolSetting(school_id=school_id, setting_key=key, setting_value='', updated_at=now))
            db.session.commit()
        except IntegrityError:                   # someone else made it a moment ago
            db.session.rollback()
    result = db.session.execute(
        update(SchoolSetting)
        .where(SchoolSetting.school_id == school_id, SchoolSetting.setting_key == key,
               or_(SchoolSetting.setting_value.is_(None), SchoolSetting.setting_value != value))
        .values(setting_value=value, updated_at=now))
    db.session.commit()
    return result.rowcount == 1


def _teachers(class_name):
    """The active staff with access to this class who may write its report card comments."""
    ids = [r['admin_id'] for r in all_rows(
        select(AdminScope.admin_id).join(Admin, Admin.id == AdminScope.admin_id)
        .where(AdminScope.scope_type == 'class', AdminScope.scope_value == class_name, Admin.active == 1).distinct())]
    return [a for a in ids if admin_has_permission(a, 'report_cards.comment')]


def _when(days):
    return 'today' if days == 0 else 'tomorrow' if days == 1 else f'in {days} days'


def _current_students():
    """Active students enrolled in the current session who have a date of birth, with their class."""
    return all_rows(
        select(Student.id, Student.first_name, Student.last_name, Student.date_of_birth, Student.guardian_phone,
               SchoolClass.name.label('class_name'))
        .join(StudentEnrolment, and_(StudentEnrolment.student_id == Student.id, StudentEnrolment.active == 1))
        .join(SchoolClass, SchoolClass.id == StudentEnrolment.class_id)
        .join(AcademicSession, AcademicSession.id == StudentEnrolment.session_id)
        .where(Student.active == 1, AcademicSession.is_current == 1, Student.date_of_birth.is_not(None)))


def _remind_teachers(today):
    """Notify the teachers of every birthday from today to two days away that has not been announced
    yet. Does nothing if it has already run today. Returns how many notifications were made."""
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

    made, teachers = 0, {}
    now = datetime.now(timezone.utc).isoformat()
    for r in _current_students():
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


# ---------------------------------------------------------------- the parents' birthday text
def text_settings():
    """``{'enabled': bool}``: whether the school sends the parents' birthday text. Its words are the message
    'sms_birthday' in core/message_wording.py, which the school may rewrite (Settings, Message wording)."""
    return {'enabled': _setting(TEXT_ON_KEY) == '1'}


def save_text_settings(enabled, admin_id):
    """Switch the parents' birthday text on or off. Commits."""
    _save_setting(TEXT_ON_KEY, '1' if enabled else '', admin_id)
    db.session.commit()


def preview_text(first_name='Ada'):
    """The birthday text as a parent would receive it."""
    return render('sms_birthday', first_name=first_name)[1]


def birthdays_on(day):
    """``[student row]`` of the current students whose birthday is ``day``, each once."""
    found, seen = [], set()
    for r in _current_students():
        dob = parse_dob(r['date_of_birth'])
        if dob is None or r['id'] in seen:
            continue
        seen.add(r['id'])
        if next_birthday(dob, day) == day:
            found.append(r)
    return found


def text_overview(now=None):
    """What the settings card shows: birthdays today and in the next seven days, and today's greetings."""
    today = (now or school_now()).date()
    upcoming, seen = 0, set()
    for r in _current_students():
        dob = parse_dob(r['date_of_birth'])
        if dob is None or r['id'] in seen:
            continue
        seen.add(r['id'])
        nxt = next_birthday(dob, today)
        if nxt is not None and 0 < (nxt - today).days <= 7:
            upcoming += 1
    start = _day_start_utc(today)
    sent = one_scalar(select(func.count(distinct(NotificationDeliveryLog.student_id))).where(
        NotificationDeliveryLog.kind == NOTICE_KIND, NotificationDeliveryLog.status == 'sent',
        NotificationDeliveryLog.created_at >= start)) or 0
    return {'today': len(birthdays_on(today)), 'week': upcoming, 'sent_today': int(sent),
            'window': f'{SEND_FROM_HOUR}:00 to {SEND_UNTIL_HOUR}:00'}


def _day_start_utc(day):
    return datetime.combine(day, time(0), tzinfo=zone()).astimezone(timezone.utc).isoformat()


def start_birthday_texts(now=None, inline=False):
    """Start today's greetings if they are due: switched on, within the sending hours, and not started yet today.
    Returns True when this call started them. ``inline`` sends them before returning (a command-line run, whose
    process would end before a background thread finished)."""
    now = now or school_now()
    if not (SEND_FROM_HOUR <= now.hour < SEND_UNTIL_HOUR):
        return False
    day = now.date().isoformat()
    if _setting(TEXT_DAY_KEY) == day or _setting(TEXT_ON_KEY) != '1':
        return False
    if not _claim(TEXT_DAY_KEY, day):
        return False
    if inline:
        send_birthday_texts(day)
    else:
        enqueue('birthday_texts', day=day)
    return True


@job_handler('birthday_texts')
def send_birthday_texts(day):
    """Text the parents of everyone whose birthday is ``day`` (an ISO school date). A retry skips the students
    already greeted today, and a retry that comes after the day is over sends nothing: no greeting is ever late.
    Each student is on its own: one that fails is logged and the rest still go. Returns how many were greeted."""
    when = date.fromisoformat(day)
    if school_now().date() != when or not text_settings()['enabled']:
        return 0
    done = {sid for (sid,) in tuples(select(NotificationDeliveryLog.student_id).where(
        NotificationDeliveryLog.kind == NOTICE_KIND, NotificationDeliveryLog.created_at >= _day_start_utc(when)))}
    from core.notifications import _notify_parents_sms
    greeted = 0
    for r in birthdays_on(when):
        if r['id'] in done:
            continue
        try:
            first_name = ' '.join(str(r['first_name'] or '').split()) or 'there'
            sent, _ = _notify_parents_sms(r['id'], r['guardian_phone'], render('sms_birthday', first_name=first_name)[1],
                                          kind=NOTICE_KIND)
            greeted += 1 if sent else 0
        except Exception:
            db.session.rollback()
            current_app.logger.exception('The birthday text failed for student %s', r['id'])
    return greeted


def run_birthday_check(today=None, now=None):
    """The daily birthday work: the teachers' reminders, then the parents' greetings if they are due.
    Returns how many teacher notifications were made."""
    now = now or school_now()
    made = _remind_teachers(today or now.date())
    start_birthday_texts(now)
    return made


def run_birthday_check_safely():
    """The page-load hook: a failure here must never take an admin page down."""
    try:
        return run_birthday_check()
    except Exception:
        db.session.rollback()
        current_app.logger.exception('The daily birthday check failed')
        return 0
