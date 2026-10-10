"""The figures behind the finance pages: what was collected and when (the Collections dashboard), and what
each student, class and fee owes for a session (the dashboard's breakdowns and the Student accounts list).

"Paid" against a fee always means allocated to that fee, as everywhere else in finance
(``_finance_student_outstanding``): money not yet applied to a fee is shown separately as unallocated.
A cashier without ``finance.view_all`` only ever gets their own takings and no school-wide balances; the
callers decide that by passing ``own``.
"""
from datetime import date, datetime, timedelta

from sqlalchemy import func, select

from core.db_helpers import all_rows, one_scalar
from models import (
    Admin, FinanceFeeAssessment, FinancePayment, FinancePaymentAllocation, SchoolClass, Student,
    StudentEnrolment,
)

# The windows the Collections chart can show, and how each is bucketed.
PERIODS = {
    '7d': {'label': 'Last 7 days', 'days': 7, 'bucket': 'day'},
    '30d': {'label': 'Last 30 days', 'days': 30, 'bucket': 'day'},
    '90d': {'label': 'Last 90 days', 'days': 91, 'bucket': 'week'},
    '12m': {'label': 'Last 12 months', 'days': 365, 'bucket': 'month'},
}
DEFAULT_PERIOD = '30d'


def _r(v):
    return round(float(v or 0), 2)


def _payment_scope(own_admin_id):
    scope = [FinancePayment.status == 'posted']
    if own_admin_id:
        scope.append(FinancePayment.recorded_by == own_admin_id)
    return scope


def collections(period=DEFAULT_PERIOD, own_admin_id=None, today=None):
    """Posted payments over a window: the total against the window before it, a series of buckets for the
    chart, the split by method and (school-wide only) by the member of staff who took them."""
    period = period if period in PERIODS else DEFAULT_PERIOD
    spec = PERIODS[period]
    today = today or date.today()
    start = today - timedelta(days=spec['days'] - 1)
    prev_start = start - timedelta(days=spec['days'])
    scope = _payment_scope(own_admin_id)
    day = func.substr(FinancePayment.paid_at, 1, 10)

    by_day = {r['d']: (_r(r['total']), int(r['n'])) for r in all_rows(
        select(day.label('d'), func.sum(FinancePayment.amount).label('total'), func.count().label('n'))
        .where(*scope, day >= start.isoformat(), day <= today.isoformat()).group_by(day))}
    prev_total = _r(one_scalar(select(func.coalesce(func.sum(FinancePayment.amount), 0))
                               .where(*scope, day >= prev_start.isoformat(), day < start.isoformat()), 0))

    # Buckets: every day, week (starting Monday) or month in the window, empty ones included.
    buckets = []
    if spec['bucket'] == 'day':
        for i in range(spec['days']):
            d = start + timedelta(days=i)
            total, n = by_day.get(d.isoformat(), (0.0, 0))
            buckets.append({'key': d.isoformat(), 'label': d.strftime('%d %b').lstrip('0'), 'value': total, 'count': n,
                            'weekend': d.weekday() >= 5, 'tip': f"{d.strftime('%a %d %b')}"})
    elif spec['bucket'] == 'week':
        w = start - timedelta(days=start.weekday())
        while w <= today:
            total = n = 0
            for i in range(7):
                t, c = by_day.get((w + timedelta(days=i)).isoformat(), (0.0, 0))
                total += t; n += c
            buckets.append({'key': w.isoformat(), 'label': w.strftime('%d %b').lstrip('0'), 'value': _r(total), 'count': n,
                            'weekend': False, 'tip': f"Week of {w.strftime('%d %b')}"})
            w += timedelta(days=7)
    else:
        y, m = start.year, start.month
        while (y, m) <= (today.year, today.month):
            key = f'{y:04d}-{m:02d}'
            total = sum(v[0] for k, v in by_day.items() if k.startswith(key))
            n = sum(v[1] for k, v in by_day.items() if k.startswith(key))
            buckets.append({'key': key, 'label': date(y, m, 1).strftime('%b'), 'value': _r(total), 'count': n,
                            'weekend': False, 'tip': date(y, m, 1).strftime('%B %Y')})
            m += 1
            if m > 12:
                y, m = y + 1, 1
    for b in buckets:
        b['tip'] += f": ₦{b['value']:,.0f} from {b['count']} payment{'' if b['count'] == 1 else 's'}"

    total = _r(sum(b['value'] for b in buckets))
    count = sum(b['count'] for b in buckets)
    methods = [{'label': r['method'] or 'Other', 'value': _r(r['total']), 'count': int(r['n'])} for r in all_rows(
        select(FinancePayment.method, func.sum(FinancePayment.amount).label('total'), func.count().label('n'))
        .where(*scope, day >= start.isoformat(), day <= today.isoformat())
        .group_by(FinancePayment.method).order_by(func.sum(FinancePayment.amount).desc()))]
    collectors = []
    if not own_admin_id:
        collectors = [{'label': r['display_name'], 'value': _r(r['total']), 'count': int(r['n'])} for r in all_rows(
            select(Admin.display_name, func.sum(FinancePayment.amount).label('total'), func.count().label('n'))
            .join(Admin, Admin.id == FinancePayment.recorded_by)
            .where(*scope, day >= start.isoformat(), day <= today.isoformat())
            .group_by(Admin.display_name).order_by(func.sum(FinancePayment.amount).desc()).limit(8))]
    best = max(buckets, key=lambda b: b['value']) if buckets else None
    change = None
    if prev_total:
        change = round(100 * (total - prev_total) / prev_total, 1)
    return {'period': period, 'periods': PERIODS, 'label': spec['label'], 'bucket': spec['bucket'],
            'start': start, 'end': today, 'buckets': buckets, 'max': max([b['value'] for b in buckets] + [0]),
            'total': total, 'count': count, 'average': _r(total / count) if count else 0,
            'prev_total': prev_total, 'change': change, 'methods': methods, 'collectors': collectors,
            'best': best if best and best['value'] else None}


def _class_of_students(session_id):
    """{student_id: (class_id, class_name, level_order)} for the session's active enrolments."""
    return {r['student_id']: (r['class_id'], r['name'], r['level_order'] or 0) for r in all_rows(
        select(StudentEnrolment.student_id, StudentEnrolment.class_id, SchoolClass.name, SchoolClass.level_order)
        .join(SchoolClass, SchoolClass.id == StudentEnrolment.class_id)
        .where(StudentEnrolment.session_id == session_id, StudentEnrolment.active == 1))}


def accounts(session_id):
    """One row per active student enrolled in the session or charged in it: their class, what they were
    charged, what has been applied to those charges, the balance, overdue part and last payment."""
    if not session_id:
        return []
    today = date.today().isoformat()
    classes = _class_of_students(session_id)
    charged = {}
    for r in all_rows(select(FinanceFeeAssessment.student_id, FinanceFeeAssessment.amount, FinanceFeeAssessment.due_date,
                             FinanceFeeAssessment.id)
                      .where(FinanceFeeAssessment.session_id == session_id, FinanceFeeAssessment.active == 1)):
        charged.setdefault(r['student_id'], []).append(r)
    paid_by_fee = {r['assessment_id']: _r(r['total']) for r in all_rows(
        select(FinancePaymentAllocation.assessment_id, func.sum(FinancePaymentAllocation.amount).label('total'))
        .join(FinancePayment, FinancePayment.id == FinancePaymentAllocation.payment_id)
        .join(FinanceFeeAssessment, FinanceFeeAssessment.id == FinancePaymentAllocation.assessment_id)
        .where(FinanceFeeAssessment.session_id == session_id, FinanceFeeAssessment.active == 1,
               FinancePaymentAllocation.voided_at.is_(None), FinancePayment.status == 'posted')
        .group_by(FinancePaymentAllocation.assessment_id))}
    last_paid = {r['student_id']: r['last'] for r in all_rows(
        select(FinancePayment.student_id, func.max(FinancePayment.paid_at).label('last'))
        .where(FinancePayment.status == 'posted', FinancePayment.session_id == session_id)
        .group_by(FinancePayment.student_id))}
    ids = set(classes) | set(charged)
    if not ids:
        return []
    students = {r['id']: r for r in all_rows(
        select(Student.id, Student.first_name, Student.middle_name, Student.last_name, Student.admission_no)
        .where(Student.active == 1, Student.id.in_(ids)))}
    rows = []
    for sid, st in students.items():
        assessed = paid = overdue = 0.0
        for fee in charged.get(sid, []):
            a = _r(fee['amount']); p = min(a, paid_by_fee.get(fee['id'], 0.0))
            assessed += a; paid += p
            if fee['due_date'] and str(fee['due_date'])[:10] < today:
                overdue += a - p
        assessed, paid = _r(assessed), _r(paid)
        balance = _r(max(0.0, assessed - paid))
        cid, cname, order = classes.get(sid, (None, '', 9999))
        status = 'none' if not assessed else ('paid' if balance <= 0.004 else ('part' if paid > 0 else 'unpaid'))
        rows.append({'id': sid, 'name': ' '.join(x for x in (st['last_name'] + ',', st['first_name'], st['middle_name'] or '') if x).strip(),
                     'admission_no': st['admission_no'], 'class_id': cid, 'class_name': cname, 'class_order': order,
                     'assessed': assessed, 'paid': paid, 'balance': balance, 'overdue': _r(overdue),
                     'rate': round(100 * paid / assessed) if assessed else None, 'status': status,
                     'last_paid': (last_paid.get(sid) or '')[:10]})
    return rows


def session_breakdown(session_id, rows=None):
    """For a session: totals, and the same figures by class and by fee category."""
    rows = accounts(session_id) if rows is None else rows
    assessed = _r(sum(r['assessed'] for r in rows)); paid = _r(sum(r['paid'] for r in rows))
    by_class = {}
    for r in rows:
        if not r['class_id'] and not r['assessed']:
            continue
        key = r['class_id'] or 0
        c = by_class.setdefault(key, {'id': r['class_id'], 'label': r['class_name'] or 'No class this session', 'order': r['class_order'],
                                      'assessed': 0.0, 'paid': 0.0, 'students': 0, 'owing': 0, 'cleared': 0})
        c['assessed'] += r['assessed']; c['paid'] += r['paid']; c['students'] += 1
        c['owing'] += 1 if r['balance'] > 0.004 else 0
        c['cleared'] += 1 if r['status'] == 'paid' else 0
    classes = sorted(by_class.values(), key=lambda c: (c['order'], c['label']))
    for c in classes:
        c['assessed'], c['paid'] = _r(c['assessed']), _r(c['paid'])
        c['balance'] = _r(max(0, c['assessed'] - c['paid']))
        c['rate'] = round(100 * c['paid'] / c['assessed']) if c['assessed'] else None
    paid_sq = (select(func.coalesce(func.sum(FinancePaymentAllocation.amount), 0))
               .select_from(FinancePaymentAllocation)
               .join(FinancePayment, FinancePayment.id == FinancePaymentAllocation.payment_id)
               .where(FinancePaymentAllocation.assessment_id == FinanceFeeAssessment.id,
                      FinancePaymentAllocation.voided_at.is_(None), FinancePayment.status == 'posted')
               .correlate(FinanceFeeAssessment).scalar_subquery())
    categories = {}
    for r in all_rows(select(FinanceFeeAssessment.category, FinanceFeeAssessment.amount, paid_sq.label('paid'))
                      .where(FinanceFeeAssessment.session_id == session_id, FinanceFeeAssessment.active == 1)):
        c = categories.setdefault(r['category'] or 'Other', {'label': r['category'] or 'Other', 'assessed': 0.0, 'paid': 0.0, 'fees': 0})
        a = _r(r['amount']); c['assessed'] += a; c['paid'] += min(a, _r(r['paid'])); c['fees'] += 1
    cats = sorted(categories.values(), key=lambda c: -c['assessed'])
    for c in cats:
        c['assessed'], c['paid'] = _r(c['assessed']), _r(c['paid'])
        c['balance'] = _r(max(0, c['assessed'] - c['paid']))
        c['rate'] = round(100 * c['paid'] / c['assessed']) if c['assessed'] else None
    billed = [r for r in rows if r['assessed']]
    status = {k: sum(1 for r in rows if r['status'] == k) for k in ('paid', 'part', 'unpaid', 'none')}
    return {'assessed': assessed, 'paid': paid, 'balance': _r(max(0, assessed - paid)),
            'overdue': _r(sum(r['overdue'] for r in rows)),
            'rate': round(100 * paid / assessed, 1) if assessed else None,
            'students': len(rows), 'billed': len(billed), 'status': status,
            'classes': classes, 'categories': cats,
            'debtors': sorted([r for r in rows if r['balance'] > 0.004], key=lambda r: -r['balance'])[:8]}


def today_summary(own_admin_id=None, today=None):
    today = (today or date.today()).isoformat()
    scope = _payment_scope(own_admin_id)
    day = func.substr(FinancePayment.paid_at, 1, 10)
    row = all_rows(select(func.coalesce(func.sum(FinancePayment.amount), 0).label('total'), func.count().label('n'))
                   .where(*scope, day == today))[0]
    return {'total': _r(row['total']), 'count': int(row['n'])}


def recent_payments(own_admin_id=None, limit=10):
    return all_rows(select(FinancePayment.id, FinancePayment.receipt_no, FinancePayment.amount, FinancePayment.method,
                           FinancePayment.paid_at, FinancePayment.student_id, FinancePayment.session_id,
                           FinancePayment.category, Student.first_name, Student.last_name, Student.admission_no)
                    .join(Student, Student.id == FinancePayment.student_id)
                    .where(*_payment_scope(own_admin_id))
                    .order_by(FinancePayment.paid_at.desc(), FinancePayment.id.desc()).limit(limit))


PAYMENT_VIEWS = {'all': 'All payments', 'unallocated': 'Not yet applied', 'applied': 'Fully applied', 'voided': 'Voided'}


def payments(view='all', own_admin_id=None, session_id=None, q='', method=''):
    """Payments for the Payments page, newest first, each with how much of it has been applied to fees
    and how much is still free. ``view`` narrows to those not yet (fully) applied, fully applied, or voided;
    ``counts`` says how many fall under each, for the same session, search and method."""
    from blueprints.finance.helpers import _payment_allocated_sq
    scope = []
    if own_admin_id:
        scope.append(FinancePayment.recorded_by == own_admin_id)
    if session_id:
        scope.append(FinancePayment.session_id == session_id)
    if method:
        scope.append(FinancePayment.method == method)
    rows = []
    for r in all_rows(select(FinancePayment.id, FinancePayment.receipt_no, FinancePayment.amount, FinancePayment.method,
                             FinancePayment.paid_at, FinancePayment.status, FinancePayment.category,
                             FinancePayment.student_id, FinancePayment.session_id, FinancePayment.payer_name,
                             Student.first_name, Student.middle_name, Student.last_name, Student.admission_no,
                             _payment_allocated_sq().label('allocated'))
                      .join(Student, Student.id == FinancePayment.student_id)
                      .where(*scope)
                      .order_by(FinancePayment.paid_at.desc(), FinancePayment.id.desc())):
        row = dict(r)
        amount = _r(row['amount']); applied = min(amount, _r(row['allocated']))
        row.update(amount=amount, applied=applied, free=_r(max(0.0, amount - applied)) if row['status'] == 'posted' else 0.0,
                   name=' '.join(x for x in (row['first_name'], row['middle_name'], row['last_name']) if x))
        row['state'] = 'voided' if row['status'] != 'posted' else ('unallocated' if row['free'] > 0.005 else 'applied')
        rows.append(row)
    if q:
        needle = q.lower()
        rows = [r for r in rows if needle in ' '.join(str(x or '') for x in (r['name'], r['admission_no'], r['receipt_no'], r['payer_name'])).lower()]
    counts = {'all': len(rows), **{k: sum(1 for r in rows if r['state'] == k) for k in ('unallocated', 'applied', 'voided')}}
    totals = {'amount': _r(sum(r['amount'] for r in rows if r['state'] != 'voided')),
              'free': _r(sum(r['free'] for r in rows)),
              'voided': _r(sum(r['amount'] for r in rows if r['state'] == 'voided'))}
    if view in ('unallocated', 'applied', 'voided'):
        rows = [r for r in rows if r['state'] == view]
    return rows, counts, totals


def payment_methods(own_admin_id=None):
    scope = [FinancePayment.recorded_by == own_admin_id] if own_admin_id else []
    return [r['method'] for r in all_rows(select(FinancePayment.method).where(*scope).group_by(FinancePayment.method)
                                          .order_by(FinancePayment.method)) if r['method']]


def account_payments(student_id, session_id):
    """A student's payments for one session, each with the fees it was applied to (the fee, its term or
    billing period, and how much), newest first."""
    from models import FinanceFeeItem
    pays = all_rows(select(FinancePayment.id, FinancePayment.receipt_no, FinancePayment.amount, FinancePayment.method,
                           FinancePayment.paid_at, FinancePayment.status, FinancePayment.category, FinancePayment.reference,
                           FinancePayment.payer_name)
                    .where(FinancePayment.student_id == student_id, FinancePayment.session_id == session_id)
                    .order_by(FinancePayment.paid_at.desc(), FinancePayment.id.desc()))
    if not pays:
        return []
    applied = {}
    for a in all_rows(select(FinancePaymentAllocation.payment_id, FinancePaymentAllocation.amount,
                             FinanceFeeAssessment.category, FinanceFeeAssessment.term, FinanceFeeItem.applicability)
                      .join(FinanceFeeAssessment, FinanceFeeAssessment.id == FinancePaymentAllocation.assessment_id)
                      .outerjoin(FinanceFeeItem, FinanceFeeItem.id == FinanceFeeAssessment.fee_item_id)
                      .where(FinancePaymentAllocation.payment_id.in_([p['id'] for p in pays]),
                             FinancePaymentAllocation.voided_at.is_(None))
                      .order_by(FinancePaymentAllocation.id)):
        applied.setdefault(a['payment_id'], []).append(
            {'fee': a['category'], 'type': fee_type(a['term'], a['applicability']), 'amount': _r(a['amount'])})
    out = []
    for p in pays:
        row = dict(p)
        row['amount'] = _r(row['amount'])
        row['applied'] = applied.get(p['id'], [])
        used = _r(sum(x['amount'] for x in row['applied']))
        row['free'] = _r(max(0.0, row['amount'] - used)) if row['status'] == 'posted' else 0.0
        out.append(row)
    return out


def fee_type(term, applicability=None):
    """How a charge is billed, in the words a school uses: One-time, Full Session, or the term it is for."""
    if (applicability or '').strip().lower() in ('one-time', 'one time', 'once'):
        return 'One-time'
    return term or 'Full Session'
