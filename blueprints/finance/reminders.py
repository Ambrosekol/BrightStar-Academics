"""Fee reminders: telling the parents of students whose fees are overdue, by text and email, on a schedule.

Set up under Settings > Fees & payments > Fee reminders (finance.manage): on or off, how often (every 3, 7, 14 or 30
days), how much must be overdue before a family is reminded, and by text, email or both. The words of both messages
are edited on the same page (they are 'sms_fee_reminder' and 'email_fee_reminder' in core/message_wording.py, so they
also appear under Message wording).

*Who* is reminded: every active student owing at least the amount set, past its date. In the current session that is
what is overdue by each fee's due date, or by the student's instalment plan (blueprints/finance/instalments.py); a
balance left from an earlier session counts in full. Each parent linked to the student gets the text, the guardian's
email address on the student's record gets the email, and every linked parent sees it in the parent portal.

*When*: like the birthday texts, there is no scheduler to run. The first admin page opened on a day the reminders are
due starts them, between 9:00 and 18:00 school time; ``python -m control_plane sweep-jobs`` on a timer also starts them,
for a school nobody opens that day. The day is claimed in the database first, so two servers never send twice, and a
family already reminded today (by hand, say) is skipped. Every text and email is in the notice log, kind
'fee_reminder'.

A reminder can also be sent at once: to everyone due one (Send reminders now), or to one student's parents from their
fee account.
"""

from datetime import date, datetime, timedelta, timezone

from flask import abort, current_app, flash, redirect, render_template, request, url_for
from sqlalchemy import func, select

from app import _active_sessions, _school_current_session, app
from blueprints.finance import analytics
from blueprints.finance.helpers import _money, json_setting, save_json_setting
from core import delivery
from core import message_wording as mw
from core.db_helpers import all_rows, one, tuples
from core.jobs import enqueue, job_handler
from core.security import admin_required, audit_log, csrf_protect, current_admin
from core.staff_attendance import school_now, zone
from models import (
    FinanceFeeAssessment, FinancePayment, FinancePaymentAllocation, NotificationDeliveryLog, SchoolNotification,
    Student, db,
)

KEY = 'fee_reminders'
DEFAULT = {'enabled': False, 'every_days': 7, 'min_overdue': 1000.0, 'sms': True, 'email': True}
LAST_DAY_KEY = 'fee_reminders_last_day'     # the school date of the last scheduled run (the claim)
LAST_KEY = 'fee_reminders_last'             # what the last run sent, for the settings page
LAST_DEFAULT = {'day': '', 'students': 0, 'sms': 0, 'email': 0, 'manual': False}
EVERY = (3, 7, 14, 30)
SEND_FROM_HOUR, SEND_UNTIL_HOUR = 9, 18
KIND = 'fee_reminder'
WORDS = ('sms_fee_reminder', 'email_fee_reminder')


def settings():
    s = json_setting(KEY, DEFAULT)
    s['every_days'] = int(s['every_days']) if str(s['every_days']).isdigit() and int(s['every_days']) in EVERY else 7
    try:
        s['min_overdue'] = max(0.0, float(s['min_overdue']))
    except (TypeError, ValueError):
        s['min_overdue'] = DEFAULT['min_overdue']
    s['enabled'], s['sms'], s['email'] = bool(s['enabled']), bool(s['sms']), bool(s['email'])
    return s


def _naira(v):
    return f'₦{float(v or 0):,.2f}'


def positions(today=None, student_ids=None):
    """``{student_id: (overdue, balance)}`` for every active student who owes anything, across every active session.

    Earlier sessions' balances count as overdue in full; the current and later sessions by each fee's due date, or the
    student's instalment plan. Five queries whatever the number of students or sessions: the sessions, the current
    one, every live bill, what has been applied to them, and the plans (``student_ids`` narrows all of it)."""
    from blueprints.finance.instalments import overdue_and_next, plans_for_students
    today = today or school_now().date()
    sessions = [s.id for s in _active_sessions()]
    if not sessions:
        return {}
    current = _school_current_session()
    current_id = current['id'] if current else None
    scope = [FinanceFeeAssessment.active == 1, FinanceFeeAssessment.voided_at.is_(None),
             FinanceFeeAssessment.session_id.in_(sessions), Student.active == 1]
    if student_ids is not None:
        scope.append(FinanceFeeAssessment.student_id.in_(list(student_ids) or [0]))
    fees = all_rows(select(FinanceFeeAssessment.id, FinanceFeeAssessment.student_id, FinanceFeeAssessment.session_id,
                           FinanceFeeAssessment.amount, FinanceFeeAssessment.due_date, FinanceFeeAssessment.term)
                    .join(Student, Student.id == FinanceFeeAssessment.student_id).where(*scope))
    if not fees:
        return {}
    paid = {aid: float(v or 0) for aid, v in tuples(
        select(FinancePaymentAllocation.assessment_id, func.sum(FinancePaymentAllocation.amount))
        .join(FinancePayment, FinancePayment.id == FinancePaymentAllocation.payment_id)
        .join(FinanceFeeAssessment, FinanceFeeAssessment.id == FinancePaymentAllocation.assessment_id)
        .join(Student, Student.id == FinanceFeeAssessment.student_id)
        .where(*scope, FinancePaymentAllocation.voided_at.is_(None), FinancePayment.status == 'posted')
        .group_by(FinancePaymentAllocation.assessment_id))}
    plans = plans_for_students(student_ids)
    grouped = {}
    for f in fees:
        grouped.setdefault((f['student_id'], f['session_id']), []).append(f)
    out = {}
    for (sid, session_id), items in grouped.items():
        owed = sum(max(0.0, float(f['amount'] or 0) - paid.get(f['id'], 0.0)) for f in items)
        if owed <= 0.004:
            continue
        if current_id is not None and session_id < current_id:
            late = owed
        else:
            late = overdue_and_next(items, paid, plans.get(sid, {}).get(session_id, {}), today)[0]
        o, b = out.get(sid, (0.0, 0.0))
        out[sid] = (o + late, b + owed)
    return {sid: (round(o, 2), round(b, 2)) for sid, (o, b) in out.items()}


def targets(today=None, floor=None, basis='overdue'):
    """``[{'id', 'name', 'class_name', 'overdue', 'balance'}]`` of every active student owing past its date (see
    ``positions``), largest first. ``floor`` is the least that counts (the reminders' own minimum when None); with
    ``basis`` 'any' the whole balance is measured against it instead of what is overdue (the report card hold)."""
    floor = max(settings()['min_overdue'] if floor is None else floor, 0.01)
    measure = 1 if basis == 'any' else 0
    found = {sid: p for sid, p in positions(today).items() if p[measure] >= floor}
    if not found:
        return []
    current = _school_current_session()
    classes = analytics._class_of_students(current['id']) if current else {}
    out = []
    for sid, first, middle, last in tuples(select(Student.id, Student.first_name, Student.middle_name, Student.last_name)
                                           .where(Student.id.in_(list(found)))):
        overdue, balance = found[sid]
        out.append({'id': sid, 'name': ' '.join(x for x in (f'{last},', first, middle or '') if x).strip(),
                    'class_name': classes.get(sid, (None, ''))[1], 'overdue': overdue, 'balance': balance})
    key = 'balance' if measure else 'overdue'
    return sorted(out, key=lambda r: (-r[key], r['name']))


def _day_start_utc(day):
    return datetime.combine(day, datetime.min.time(), tzinfo=zone()).astimezone(timezone.utc).isoformat()


def reminded_since(day, student_ids=None):
    """The students whose parents were sent a fee reminder on or after this school date."""
    scope = [NotificationDeliveryLog.kind == KIND, NotificationDeliveryLog.status == 'sent',
             NotificationDeliveryLog.created_at >= _day_start_utc(day)]
    if student_ids is not None:
        scope.append(NotificationDeliveryLog.student_id.in_(list(student_ids) or [0]))
    return {int(s) for (s,) in tuples(select(NotificationDeliveryLog.student_id).where(*scope).distinct())}


def _finance_link(student_id):
    try:
        return url_for('parent_child_finance', student_id=student_id)
    except RuntimeError:      # a command-line run has no address to build one from
        return f'/parent/children/{student_id}/finance'


def remind(student_id, overdue, balance, actor_id=None):
    """Remind one student's parents: an alert in the parent portal, and the text and email the school sends (Fee
    reminders settings). Returns ``(texts sent, emails sent)``. Never raises."""
    from core.notifications import _notify_guardian_email, _notify_parents_sms, _parent_ids_for_student
    s = settings()
    student = one(select(Student.first_name, Student.last_name, Student.guardian_email, Student.guardian_phone)
                  .where(Student.id == student_id))
    if not student:
        return 0, 0
    child = f"{student['first_name']} {student['last_name']}".strip()
    values = {'child': child, 'amount': _naira(overdue), 'balance': _naira(balance)}
    try:
        now = datetime.now(timezone.utc).isoformat()
        for pid in _parent_ids_for_student(student_id):
            db.session.add(SchoolNotification(
                recipient_type='parent', recipient_id=pid, student_id=student_id, category='finance',
                title=f'Fees overdue for {child}',
                message=f"{_naira(overdue)} is overdue, of {_naira(balance)} still owed. Pay online or at the school's finance office.",
                action_url=_finance_link(student_id), created_at=now, created_by=actor_id))
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception('In-app fee reminder failed for student %s', student_id)
    texts = emails = 0
    if s['sms']:
        try:
            texts, _ = _notify_parents_sms(student_id, student['guardian_phone'], mw.render('sms_fee_reminder', **values)[1],
                                           kind=KIND)
        except Exception:
            current_app.logger.exception('Fee reminder text failed for student %s', student_id)
    if s['email'] and (student['guardian_email'] or '').strip():
        try:
            subject, body = mw.render('email_fee_reminder', **values)
            ok, _ = _notify_guardian_email(student['guardian_email'], subject, body, student_id=student_id, kind=KIND)
            emails = 1 if ok else 0
        except Exception:
            current_app.logger.exception('Fee reminder email failed for student %s', student_id)
    return texts, emails


@job_handler('fee_reminders')
def send_reminders(day, manual=False, actor_id=None):
    """Remind every family due a reminder (``targets``), skipping any already reminded on ``day``. Each student is on
    its own: one that fails is logged and the rest still go. Records what was sent for the settings page."""
    when = date.fromisoformat(day)
    due = targets(when)
    done = reminded_since(when, [r['id'] for r in due])
    sent = {'day': day, 'students': 0, 'sms': 0, 'email': 0, 'manual': bool(manual)}
    for r in due:
        if r['id'] in done:
            continue
        texts, emails = remind(r['id'], r['overdue'], r['balance'], actor_id)
        if texts or emails:
            sent['students'] += 1
        sent['sms'] += texts
        sent['email'] += emails
    try:
        save_json_setting(LAST_KEY, sent, actor_id)
        db.session.commit()
    except Exception:
        db.session.rollback()
    return sent


def student_overdue(student_id, today=None):
    """``(overdue, balance)`` for one student, worked out as ``positions`` does for everyone; once per request, since
    a page (a fee account, a dashboard) can ask more than once."""
    from core.speed import request_memo
    sid = int(student_id)
    return request_memo(f'student-overdue-{sid}-{today or ""}',
                        lambda: positions(today, [sid]).get(sid, (0.0, 0.0)))


@job_handler('fee_reminder_one')
def send_one(student_id, actor_id=None):
    overdue, balance = student_overdue(student_id)
    return remind(student_id, overdue, balance, actor_id) if overdue > 0.004 else (0, 0)


def next_run(s=None, today=None):
    """The school date the next scheduled reminders go, or None when they are off."""
    s = s or settings()
    if not s['enabled']:
        return None
    today = today or school_now().date()
    from blueprints.school.birthdays import _setting
    last = _setting(LAST_DAY_KEY)
    try:
        nxt = date.fromisoformat(last) + timedelta(days=s['every_days'])
    except (TypeError, ValueError):
        return today
    return max(nxt, today)


def start_reminders(now=None, inline=False):
    """Start the scheduled reminders if they are due today: switched on, within the sending hours, and not run in the
    last ``every_days`` days. Returns True when this call started them."""
    s = settings()
    if not s['enabled']:
        return False
    now = now or school_now()
    if not (SEND_FROM_HOUR <= now.hour < SEND_UNTIL_HOUR):
        return False
    today = now.date()
    due_on = next_run(s, today)
    if due_on is None or due_on > today:
        return False
    from blueprints.school.birthdays import _claim
    if not _claim(LAST_DAY_KEY, today.isoformat()):
        return False
    if inline:
        send_reminders(today.isoformat())
    else:
        enqueue('fee_reminders', day=today.isoformat())
    return True


def run_reminder_check_safely():
    """The page-load hook: a failure here must never take an admin page down."""
    try:
        return start_reminders()
    except Exception:
        db.session.rollback()
        current_app.logger.exception('The fee reminder check failed')
        return False


# ---------------------------------------------------------------- Settings > Fees & payments > Fee reminders
@app.route('/admin/finance/reminders', methods=['GET', 'POST'])
@admin_required
@csrf_protect
def admin_finance_reminders():
    me = current_admin()
    if request.method == 'POST':
        every = request.form.get('every_days', type=int)
        floor = _money(request.form.get('min_overdue', '0'))
        new = {'enabled': request.form.get('enabled') == '1', 'every_days': every if every in EVERY else 7,
               'min_overdue': floor if floor is not None and floor >= 0 else DEFAULT['min_overdue'],
               'sms': request.form.get('sms') == '1', 'email': request.form.get('email') == '1'}
        if new['enabled'] and not (new['sms'] or new['email']):
            flash('Choose text, email or both, or switch the reminders off.', 'error')
            return redirect(url_for('admin_finance_reminders'))
        save_json_setting(KEY, new, me['id'])
        audit_log('finance_reminders_saved', 'finance', 'school_setting', KEY, new, commit=False)
        db.session.commit()
        flash('Fee reminders ' + ('saved.' if new['enabled'] else 'switched off.'), 'success')
        return redirect(url_for('admin_finance_reminders'))
    s = settings()
    due = targets()
    state = delivery.status()
    words = {key: mw.wording(key) for key in WORDS}
    return render_template('finance_reminders.html', s=s, due=due, due_total=round(sum(r['overdue'] for r in due), 2),
                           already=reminded_since(school_now().date(), [r['id'] for r in due]),
                           last=json_setting(LAST_KEY, LAST_DEFAULT), next_run=next_run(s), every=EVERY,
                           window=f'{SEND_FROM_HOUR}:00 and {SEND_UNTIL_HOUR}:00',
                           sms_ok=state['sms']['mode'] in ('school', 'platform'),
                           email_ok=state['email']['mode'] in ('school', 'platform'),
                           words=words, specs={k: mw.MESSAGES[k] for k in WORDS},
                           describe=mw.describe, sms_page=mw.SMS_PAGE,
                           sample=dict(mw.SAMPLES, school=mw._school(), child='Ada Obi', amount='N45,000.00',
                                       balance='N120,000.00'))


@app.post('/admin/finance/reminders/words/<key>')
@admin_required
@csrf_protect
def admin_finance_reminders_words(key):
    if key not in WORDS:
        abort(404)
    try:
        if request.form.get('reset') == '1':
            mw.reset(key)
            flash('The standard words are back.', 'success')
        else:
            mw.save(key, request.form.get('subject', ''), request.form.get('body', ''), current_admin()['id'])
            flash('The reminder ' + ('text' if key.startswith('sms') else 'email') + ' now uses your words.', 'success')
        audit_log('message_wording_updated', 'school', 'message_wording', key, {'custom': mw.wording(key)['custom']})
    except ValueError as exc:
        flash(str(exc), 'error')
    return redirect(url_for('admin_finance_reminders') + f'#{key}')


@app.post('/admin/finance/reminders/send')
@admin_required
@csrf_protect
def admin_finance_reminders_send():
    me = current_admin()
    s = settings()
    if not (s['sms'] or s['email']):
        flash('Choose text, email or both first.', 'error')
        return redirect(url_for('admin_finance_reminders'))
    due = targets()
    if not due:
        flash('No family owes enough past its date to be reminded.', 'success')
        return redirect(url_for('admin_finance_reminders'))
    enqueue('fee_reminders', day=school_now().date().isoformat(), manual=True, actor_id=me['id'])
    audit_log('finance_reminders_sent_now', 'finance', 'school_setting', KEY, {'students': len(due)})
    flash(f'Reminders are going to the families of {len(due)} student{"" if len(due) == 1 else "s"}. '
          'Anyone already reminded today is skipped. Each one is in the notice log.', 'success')
    return redirect(url_for('admin_finance_reminders'))


@app.post('/admin/finance/students/<int:student_id>/remind')
@admin_required
@csrf_protect
def admin_finance_student_remind(student_id):
    me = current_admin()
    overdue, _ = student_overdue(student_id)
    if overdue <= 0.004:
        flash('Nothing is overdue for this student, so no reminder was sent.', 'error')
    elif not (settings()['sms'] or settings()['email']):
        flash('Fee reminders are set to send neither texts nor emails. Choose one under Settings, Fee reminders.', 'error')
    else:
        enqueue('fee_reminder_one', student_id=student_id, actor_id=me['id'])
        audit_log('finance_reminder_sent', 'finance', 'student', student_id, {'overdue': overdue})
        flash(f'Reminder sent to the family: {_naira(overdue)} overdue. It is in the notice log.', 'success')
    return redirect(url_for('admin_finance_student_account', student_id=student_id,
                            session_id=request.form.get('session_id', type=int)))


def last_reminded(student_id):
    """When this student's family was last sent a fee reminder (an ISO timestamp), or None."""
    return db.session.scalar(select(NotificationDeliveryLog.created_at).where(
        NotificationDeliveryLog.kind == KIND, NotificationDeliveryLog.status == 'sent',
        NotificationDeliveryLog.student_id == student_id).order_by(NotificationDeliveryLog.id.desc()).limit(1))
