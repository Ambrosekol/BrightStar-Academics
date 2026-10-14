"""Finance > Discounts: scholarships, staff children's discounts, any other discount given to a student, and the
school-wide sibling discount.

A discount is taken off a fee at the moment it is charged: ``Pricer`` works out what each student owes for each fee,
and both ways of billing (a student, a class) charge that. The bill keeps the fee's full amount and what was taken off
(``gross_amount``, ``discount_amount``, ``discount_note`` on FinanceFeeAssessment), while ``amount`` stays what the
student owes, so every balance, receipt and report in finance needs no change.

* A student's own discount is a percentage or a number of naira off each fee it covers (every fee, or one category such
  as Tuition), for one session or for every session until it is ended.
* The sibling discount is one rule for the whole school: from the second (or third) child of a family, a percentage off.
  Children are of one family when a parent account is linked to each of them; the first child is the one who joined the
  school first. Percentages add up (never past 100%), then fixed amounts come off.
* Giving or ending a discount can also re-work the fees already charged this session (``reprice``). A fee is never set
  below what has already been paid against it; what could not be taken off is reported, to be refunded or moved.
"""

from datetime import datetime, timezone

from flask import abort, flash, redirect, render_template, request, url_for
from sqlalchemy import func, or_, select

from app import FINANCE_FEE_CATEGORIES, _school_current_session, app
from blueprints.finance import analytics
from blueprints.finance.helpers import _money, json_setting, save_json_setting
from core.db_helpers import all_rows, obj, tuples
from core.idempotency import idempotent_write
from core.security import admin_has_permission, admin_required, audit_log, csrf_protect, current_admin, is_school_admin
from models import (
    AcademicSession, Admin, FinanceDiscount, FinanceFeeAssessment, FinancePayment, FinancePaymentAllocation,
    ParentAccount, ParentStudentLink, Student, db,
)

KINDS = {'scholarship': 'Scholarship', 'staff_child': 'Staff child', 'discount': 'Discount'}
SIBLING_KEY = 'sibling_discount'
SIBLING_DEFAULT = {'enabled': False, 'percent': 10.0, 'from_child': 2, 'category': 'All'}


def _naira(v):
    return f'₦{float(v or 0):,.2f}'


def _pct(v):
    v = float(v or 0)
    return f'{v:g}%'


def describe(basis, value, category):
    """'50% off Tuition', '₦20,000.00 off every fee'."""
    off = _pct(value) if basis == 'percent' else _naira(value)
    return f"{off} off {'every fee' if category == 'All' else category}" + (' (each charge)' if basis == 'amount' else '')


def sibling_rule():
    rule = json_setting(SIBLING_KEY, SIBLING_DEFAULT)
    try:
        rule['percent'] = min(100.0, max(0.0, float(rule['percent'])))
        rule['from_child'] = 3 if int(rule['from_child']) >= 3 else 2
    except (TypeError, ValueError):
        rule.update(percent=SIBLING_DEFAULT['percent'], from_child=2)
    rule['enabled'] = bool(rule['enabled'])
    if rule['category'] != 'All' and rule['category'] not in FINANCE_FEE_CATEGORIES:
        rule['category'] = 'All'
    return rule


def families():
    """``{student_id: (family key, place in the family)}`` for every active student who shares a parent account with
    another active student. Place 1 is the child who joined the school first (the lowest student id)."""
    links = tuples(select(ParentStudentLink.parent_id, ParentStudentLink.student_id)
                   .join(Student, Student.id == ParentStudentLink.student_id)
                   .join(ParentAccount, ParentAccount.id == ParentStudentLink.parent_id)
                   .where(ParentStudentLink.active == 1, Student.active == 1, Student.archived_at.is_(None),
                          ParentAccount.active == 1))
    parent = {}

    def root(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for pid, sid in links:
        a, b = root(('p', pid)), root(('s', sid))
        if a != b:
            parent[b] = a
    groups = {}
    for _, sid in links:
        groups.setdefault(root(('s', sid)), set()).add(sid)
    out = {}
    for key, members in groups.items():
        if len(members) < 2:
            continue
        for place, sid in enumerate(sorted(members), start=1):
            out[sid] = (min(members), place)
    return out


class Pricer:
    """What each of a set of students owes for a fee in one session, once their discounts are taken off. Loads every
    discount it needs in two queries, so billing a class of hundreds costs no more than billing one student."""

    def __init__(self, student_ids, session_id):
        ids = list({int(s) for s in student_ids})
        self.awards = {}
        if ids:
            for d in db.session.scalars(select(FinanceDiscount).where(
                    FinanceDiscount.student_id.in_(ids), FinanceDiscount.active == 1,
                    or_(FinanceDiscount.session_id.is_(None), FinanceDiscount.session_id == session_id))
                    .order_by(FinanceDiscount.id)):
                self.awards.setdefault(int(d.student_id), []).append(d)
        self.rule = sibling_rule()
        self.places = families() if self.rule['enabled'] and ids else {}

    def price(self, student_id, category, gross):
        """``(owed, taken off, why)`` for one fee of ``gross`` naira in ``category``."""
        gross = round(float(gross or 0), 2)
        pct, fixed, why = 0.0, 0.0, []
        for d in self.awards.get(int(student_id), []):
            if d.category not in ('All', category):
                continue
            name = d.label or KINDS.get(d.kind, 'Discount')
            if d.basis == 'percent':
                pct += float(d.value or 0); why.append(f'{name} {_pct(d.value)}')
            else:
                fixed += float(d.value or 0); why.append(f'{name} {_naira(d.value)}')
        rule, place = self.rule, self.places.get(int(student_id))
        if rule['enabled'] and place and place[1] >= rule['from_child'] and rule['category'] in ('All', category):
            pct += rule['percent']; why.append(f"Sibling {_pct(rule['percent'])}")
        off = min(gross, round(gross * min(pct, 100.0) / 100 + fixed, 2))
        if off <= 0:
            return gross, 0.0, ''
        return round(gross - off, 2), off, '; '.join(why)

    def charge(self, assessment, gross):
        """Set a new bill's amount (and its discount) from the fee's full amount."""
        owed, off, why = self.price(assessment.student_id, assessment.category, gross)
        assessment.amount = owed
        assessment.gross_amount, assessment.discount_amount, assessment.discount_note = (
            (round(float(gross), 2), off, why) if off else (None, None, None))
        return assessment


def reprice(student_ids, session_id):
    """Work out again the session's bills of these students (None: everyone billed in it) from their full amounts and
    today's discounts. A bill is never set below what was already applied to it. Does not commit. Returns
    ``{'changed', 'less', 'more', 'paid_already'}``: how many bills changed, how much was taken off and put back, and
    how much could not be taken off because it had been paid."""
    scope = [FinanceFeeAssessment.session_id == session_id, FinanceFeeAssessment.active == 1,
             FinanceFeeAssessment.voided_at.is_(None)]
    if student_ids is not None:
        if not student_ids:
            return {'changed': 0, 'less': 0.0, 'more': 0.0, 'paid_already': 0.0}
        scope.append(FinanceFeeAssessment.student_id.in_(list(student_ids)))
    bills = db.session.scalars(select(FinanceFeeAssessment).where(*scope)).all()
    paid = dict(tuples(select(FinancePaymentAllocation.assessment_id, func.sum(FinancePaymentAllocation.amount))
                       .join(FinancePayment, FinancePayment.id == FinancePaymentAllocation.payment_id)
                       .where(FinancePaymentAllocation.assessment_id.in_([b.id for b in bills] or [0]),
                              FinancePaymentAllocation.voided_at.is_(None), FinancePayment.status == 'posted')
                       .group_by(FinancePaymentAllocation.assessment_id)))
    pricer = Pricer({b.student_id for b in bills}, session_id)
    out = {'changed': 0, 'less': 0.0, 'more': 0.0, 'paid_already': 0.0}
    for b in bills:
        gross = round(float(b.gross_amount if b.gross_amount is not None else b.amount or 0), 2)
        owed, off, why = pricer.price(b.student_id, b.category, gross)
        floor = round(float(paid.get(b.id) or 0), 2)
        if owed < floor:
            out['paid_already'] += floor - owed
            owed, off = floor, round(gross - floor, 2)
        old = round(float(b.amount or 0), 2)
        if abs(owed - old) < 0.005 and (b.discount_note or '') == (why if off else ''):
            continue
        b.amount = owed
        b.gross_amount, b.discount_amount, b.discount_note = (gross, off, why) if off > 0 else (None, None, None)
        out['changed'] += 1
        if owed < old:
            out['less'] += old - owed
        else:
            out['more'] += owed - old
    for k in ('less', 'more', 'paid_already'):
        out[k] = round(out[k], 2)
    return out


def _reprice_message(result):
    if not result['changed']:
        return 'No fee already charged this session needed to change.'
    parts = [f"{result['changed']} fee{'' if result['changed'] == 1 else 's'} already charged this session changed"]
    if result['less']:
        parts.append(f"{_naira(result['less'])} taken off")
    if result['more']:
        parts.append(f"{_naira(result['more'])} put back")
    text = ', '.join(parts) + '.'
    if result['paid_already']:
        text += (f" {_naira(result['paid_already'])} could not be taken off because it had already been paid: refund it,"
                 ' or apply it to another fee.')
    return text


def student_discounts(student_id):
    """Every discount ever given to a student, the ones in force first."""
    sessions = dict(tuples(select(AcademicSession.id, AcademicSession.name)))
    rows = db.session.scalars(select(FinanceDiscount).where(FinanceDiscount.student_id == student_id)
                              .order_by(FinanceDiscount.active.desc(), FinanceDiscount.id.desc())).all()
    return [{'row': d, 'kind': KINDS.get(d.kind, 'Discount'), 'what': describe(d.basis, d.value, d.category),
             'session': sessions.get(d.session_id, 'Every session') if d.session_id else 'Every session'} for d in rows]


def student_sibling_place(student_id):
    """``(place, family size)`` when the student is one of a family of children at the school, else None."""
    fam = families()
    mine = fam.get(int(student_id))
    if not mine:
        return None
    return mine[1], sum(1 for v in fam.values() if v[0] == mine[0])


def _students_for_picker():
    current = _school_current_session()
    classes = analytics._class_of_students(current['id']) if current else {}
    return [{'id': r['id'], 'label': f"{r['last_name']} {r['first_name']} · {r['admission_no']}"
             + (f" · {classes[r['id']][1]}" if r['id'] in classes else '')}
            for r in all_rows(select(Student.id, Student.first_name, Student.last_name, Student.admission_no)
                              .where(Student.active == 1).order_by(Student.last_name, Student.first_name))]


# ---------------------------------------------------------------- the Discounts tab
@app.route('/admin/finance/discounts')
@admin_required
def admin_finance_discounts():
    view = request.args.get('view', 'active')
    view = view if view in ('active', 'ended') else 'active'
    q = request.args.get('q', '').strip()
    current = _school_current_session()
    classes = analytics._class_of_students(current['id']) if current else {}
    sessions = dict(tuples(select(AcademicSession.id, AcademicSession.name)))
    names = dict(tuples(select(Admin.id, Admin.display_name)))
    rows = []
    for d, first, last, adm in db.session.execute(
            select(FinanceDiscount, Student.first_name, Student.last_name, Student.admission_no)
            .join(Student, Student.id == FinanceDiscount.student_id)
            .order_by(Student.last_name, Student.first_name, FinanceDiscount.id.desc())).all():
        rows.append({'row': d, 'name': f'{last}, {first}', 'admission_no': adm,
                     'class_name': classes.get(d.student_id, (None, ''))[1], 'kind': KINDS.get(d.kind, 'Discount'),
                     'what': describe(d.basis, d.value, d.category),
                     'session': sessions.get(d.session_id) if d.session_id else 'Every session',
                     'by': names.get(d.created_by, ''), 'ended_by': names.get(d.ended_by, '')})
    counts = {'active': sum(1 for r in rows if r['row'].active), 'ended': sum(1 for r in rows if not r['row'].active)}
    shown = [r for r in rows if bool(r['row'].active) == (view == 'active')]
    if q:
        needle = q.lower()
        shown = [r for r in shown if needle in f"{r['name']} {r['admission_no']} {r['row'].label or ''} {r['kind']}".lower()]
    given = 0.0
    if current:
        given = round(float(db.session.scalar(select(func.coalesce(func.sum(FinanceFeeAssessment.discount_amount), 0))
                                              .where(FinanceFeeAssessment.session_id == current['id'],
                                                     FinanceFeeAssessment.active == 1)) or 0), 2)
    rule = sibling_rule()
    fam = families()
    family_count = len({v[0] for v in fam.values()})
    eligible = sum(1 for v in fam.values() if v[1] >= rule['from_child'])
    return render_template('finance_discounts.html', rows=shown, view=view, q=q, counts=counts, given=given,
                           current_session=current, rule=rule, family_count=family_count, eligible=eligible,
                           students_with=len({r['row'].student_id for r in rows if r['row'].active}),
                           categories=FINANCE_FEE_CATEGORIES)


@app.route('/admin/finance/discounts/new', methods=['GET', 'POST'])
@admin_required
@csrf_protect
@idempotent_write('finance.give_discount')
def admin_finance_discount_new():
    me = current_admin()
    current = _school_current_session()
    errors, form = [], request.form if request.method == 'POST' else {}
    if request.method == 'POST':
        try:
            student_id = int(form.get('student_id', ''))
        except (TypeError, ValueError):
            student_id = 0
        kind = form.get('kind', 'discount')
        basis = form.get('basis', 'percent')
        value = _money(form.get('value', ''))
        category = form.get('category', 'All')
        label = ' '.join(form.get('label', '').split())[:80]
        note = form.get('note', '').strip()[:300]
        whole = form.get('scope') == 'every'
        student = db.session.scalars(select(Student).where(Student.id == student_id, Student.active == 1)).first()
        if not student:
            errors.append('Choose the student.')
        if kind not in KINDS:
            errors.append('Choose what kind of discount it is.')
        if basis not in ('percent', 'amount'):
            errors.append('Choose a percentage or an amount.')
        if value is None or value <= 0:
            errors.append('Enter how much is taken off, more than nothing.')
        elif basis == 'percent' and value > 100:
            errors.append('A percentage cannot be more than 100.')
        if category != 'All' and category not in FINANCE_FEE_CATEGORIES:
            errors.append('Choose which fees it covers.')
        if not whole and not current:
            errors.append('There is no current session: choose "Every session" or set a current session first.')
        # The same discount twice would be taken off twice (two 50% scholarships make 100%).
        if not errors and db.session.scalar(select(FinanceDiscount.id).where(
                FinanceDiscount.student_id == student_id, FinanceDiscount.active == 1, FinanceDiscount.kind == kind,
                FinanceDiscount.basis == basis, FinanceDiscount.value == value, FinanceDiscount.category == category,
                FinanceDiscount.session_id.is_(None) if whole else FinanceDiscount.session_id == current['id'])):
            errors.append('This student already has exactly this discount. End it first, or give a different one.')
        if not errors:
            d = FinanceDiscount(student_id=student_id, kind=kind, label=label or None, basis=basis, value=value,
                                category=category, session_id=None if whole else current['id'], active=1,
                                note=note or None, created_by=me['id'], created_at=datetime.now(timezone.utc).isoformat())
            db.session.add(d)
            db.session.flush()
            message = f"{KINDS[kind]} given: {describe(basis, value, category)}."
            if current and form.get('reprice') == '1':
                message += ' ' + _reprice_message(reprice([student_id], current['id']))
            audit_log('finance_discount_given', 'finance', 'discount', d.id,
                      {'student_id': student_id, 'kind': kind, 'basis': basis, 'value': value, 'category': category,
                       'session_id': d.session_id}, commit=False)
            db.session.commit()
            flash(message, 'success')
            back = request.args.get('back')
            if back == 'account':
                return redirect(url_for('admin_finance_student_account', student_id=student_id))
            return redirect(url_for('admin_finance_discounts'))
    return render_template('finance_discount_form.html', errors=errors, form=form, kinds=KINDS,
                           categories=FINANCE_FEE_CATEGORIES, students=_students_for_picker(), current_session=current,
                           chosen=form.get('student_id') or request.args.get('student_id', ''))


@app.post('/admin/finance/discounts/<int:discount_id>/end')
@admin_required
@csrf_protect
def admin_finance_discount_end(discount_id):
    me = current_admin()
    d = obj(FinanceDiscount, discount_id)
    if not d:
        abort(404)
    if not d.active:
        flash('That discount has already ended.', 'error')
        return redirect(url_for('admin_finance_discounts', view='ended'))
    d.active, d.ended_at, d.ended_by = 0, datetime.now(timezone.utc).isoformat(), me['id']
    db.session.flush()
    message = 'Discount ended. New fees are charged in full.'
    current = _school_current_session()
    if current and request.form.get('reprice') == '1':
        message += ' ' + _reprice_message(reprice([d.student_id], current['id']))
    audit_log('finance_discount_ended', 'finance', 'discount', d.id, {'student_id': d.student_id,
                                                                     'repriced': request.form.get('reprice') == '1'},
              commit=False)
    db.session.commit()
    flash(message, 'success')
    if request.form.get('back') == 'account':
        return redirect(url_for('admin_finance_student_account', student_id=d.student_id))
    return redirect(url_for('admin_finance_discounts'))


@app.post('/admin/finance/discounts/sibling')
@admin_required
@csrf_protect
def admin_finance_sibling_rule():
    me = current_admin()
    form = request.form
    percent = _money(form.get('percent', ''))
    category = form.get('category', 'All')
    if percent is None or not 0 < percent <= 100:
        flash('Enter the sibling discount as a percentage from 1 to 100.', 'error')
        return redirect(url_for('admin_finance_discounts'))
    if category != 'All' and category not in FINANCE_FEE_CATEGORIES:
        category = 'All'
    rule = {'enabled': form.get('enabled') == '1', 'percent': percent,
            'from_child': 3 if form.get('from_child') == '3' else 2, 'category': category}
    save_json_setting(SIBLING_KEY, rule, me['id'])
    db.session.flush()
    message = 'Sibling discount ' + ('saved. It is taken off fees charged from now on.' if rule['enabled'] else 'switched off.')
    current = _school_current_session()
    if current and form.get('reprice') == '1':
        message += ' ' + _reprice_message(reprice(None, current['id']))
    audit_log('finance_sibling_discount_saved', 'finance', 'school_setting', SIBLING_KEY,
              dict(rule, repriced=form.get('reprice') == '1'), commit=False)
    db.session.commit()
    flash(message, 'success')
    return redirect(url_for('admin_finance_discounts'))


def discounts_for_account(student_id):
    """What the student account pop-up shows about discounts: the student's own, and their place among siblings."""
    me = current_admin()
    return {'list': student_discounts(student_id), 'sibling': student_sibling_place(student_id), 'rule': sibling_rule(),
            'can_manage': bool(me) and (is_school_admin(me) or admin_has_permission(me['id'], 'finance.manage'))}
