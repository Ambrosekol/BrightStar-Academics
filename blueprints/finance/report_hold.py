"""Holding report cards back from families who owe fees.

Switched on by the School Admin under Settings > Fees & payments > Report card hold, it decides whether a student's
report cards can be opened in the parent portal and the student portal. Results are still released and staff still see
and print every card: only the family's copy waits. It holds when the family owes more than the amount set, either:

* anything at all (every unpaid fee, in any session), or
* only what is overdue: past each fee's due date or the student's instalment plan (blueprints/finance/instalments.py),
  so a family paying on time by a plan is never held back.

The bursar (finance.manage) can let one student's cards through, with a reason (a promise to pay, a hardship case),
from the student's fee account; the list of those is on the settings page. A held card shows the family why, and the
parent a link to their fees; the moment the balance is paid, the cards open, with nothing for anyone to do.
"""

from datetime import datetime, timezone

from flask import abort, flash, redirect, render_template, request, url_for
from sqlalchemy import delete, select

from app import app
from blueprints.finance.helpers import _money, json_setting, save_json_setting
from core.db_helpers import tuples
from core.security import admin_access_error, admin_required, audit_log, csrf_protect, current_admin, is_school_admin
from core.speed import request_memo
from models import Admin, FinanceReportHoldExemption, Student, db

KEY = 'report_card_hold'
DEFAULT = {'enabled': False, 'basis': 'overdue', 'min_owed': 0.0}
BASES = {'overdue': 'Only what is overdue', 'any': 'Any unpaid balance'}


def settings():
    s = json_setting(KEY, DEFAULT)
    s['enabled'] = bool(s['enabled'])
    s['basis'] = s['basis'] if s['basis'] in BASES else 'overdue'
    try:
        s['min_owed'] = max(0.0, float(s['min_owed']))
    except (TypeError, ValueError):
        s['min_owed'] = 0.0
    return s


def exempt(student_id):
    return db.session.scalar(select(FinanceReportHoldExemption).where(FinanceReportHoldExemption.student_id == student_id))


def held(student_id):
    """``{'owed': naira}`` when this student's report cards are held from the family, else None. Worked out once per
    request (a dashboard lists several cards)."""
    def work():
        s = settings()
        if not s['enabled'] or exempt(student_id):
            return None
        from blueprints.finance.reminders import student_overdue
        overdue, balance = student_overdue(student_id)
        owed = overdue if s['basis'] == 'overdue' else balance
        return {'owed': owed} if owed > max(s['min_owed'], 0.004) else None
    return request_memo(f'report-hold-{int(student_id)}', work)


def held_students():
    """Every active student whose cards are held now, for the settings page."""
    from blueprints.finance.reminders import targets
    s = settings()
    floor = max(s['min_owed'], 0.004) + 0.001
    rows = targets(floor=floor, basis=s['basis'])
    gone = {sid for (sid,) in tuples(select(FinanceReportHoldExemption.student_id))}
    return [r for r in rows if r['id'] not in gone]


@app.route('/admin/finance/report-hold', methods=['GET', 'POST'])
@admin_required
@csrf_protect
def admin_finance_report_hold():
    if not is_school_admin():
        return admin_access_error('Report card hold')
    me = current_admin()
    if request.method == 'POST':
        floor = _money(request.form.get('min_owed', '0'))
        new = {'enabled': request.form.get('enabled') == '1',
               'basis': request.form.get('basis') if request.form.get('basis') in BASES else 'overdue',
               'min_owed': floor if floor is not None and floor >= 0 else 0.0}
        save_json_setting(KEY, new, me['id'])
        audit_log('finance_report_hold_saved', 'finance', 'school_setting', KEY, new, commit=False)
        db.session.commit()
        flash('Report cards are now held from families who owe fees.' if new['enabled']
              else 'Report cards are no longer held for fees.', 'success')
        return redirect(url_for('admin_finance_report_hold'))
    names = dict(tuples(select(Admin.id, Admin.display_name)))
    exemptions = [{'row': e, 'name': f'{last}, {first}', 'admission_no': adm, 'by': names.get(e.granted_by, '')}
                  for e, first, last, adm in db.session.execute(
                      select(FinanceReportHoldExemption, Student.first_name, Student.last_name, Student.admission_no)
                      .join(Student, Student.id == FinanceReportHoldExemption.student_id)
                      .order_by(Student.last_name, Student.first_name)).all()]
    s = settings()
    return render_template('finance_report_hold.html', s=s, bases=BASES, exemptions=exemptions,
                           held=held_students() if s['enabled'] else [])


@app.post('/admin/finance/students/<int:student_id>/report-hold')
@admin_required
@csrf_protect
def admin_finance_student_report_hold(student_id):
    """Let one student's report cards through while fees are owed, or stop doing so."""
    me = current_admin()
    if not db.session.get(Student, student_id):
        abort(404)
    if request.form.get('action') == 'release':
        note = ' '.join(request.form.get('note', '').split())[:300]
        if not note:
            flash("Say why this student's report cards should reach the family, for example a promise to pay by a date.",
                  'error')
        elif not exempt(student_id):
            db.session.add(FinanceReportHoldExemption(student_id=student_id, note=note, granted_by=me['id'],
                                                      granted_at=datetime.now(timezone.utc).isoformat()))
            audit_log('finance_report_hold_exempted', 'finance', 'student', student_id, {'note': note}, commit=False)
            db.session.commit()
            flash("This student's report cards now reach the family, whatever is owed.", 'success')
    else:
        db.session.execute(delete(FinanceReportHoldExemption).where(FinanceReportHoldExemption.student_id == student_id))
        audit_log('finance_report_hold_exemption_removed', 'finance', 'student', student_id, {}, commit=False)
        db.session.commit()
        flash("This student's report cards are held again while fees are owed.", 'success')
    if request.form.get('back') == 'settings':
        return redirect(url_for('admin_finance_report_hold'))
    return redirect(url_for('admin_finance_student_account', student_id=student_id))
