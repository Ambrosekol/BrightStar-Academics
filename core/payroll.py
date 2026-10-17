"""The rules of payroll (models/payroll.py; the pages are blueprints/school/payroll.py).

Money is whole kobo (int) everywhere. A percentage is held in basis points (800 = 8%), and every percentage of an
amount is rounded half up to the kobo (``pct``), so a payslip always adds up exactly.

Statutory deductions are the school's settings, not this code's. The defaults are a starting point, taken from the
Nigeria Tax Act 2025 (PAYE bands from January 2026, rent relief instead of the old consolidated relief) and the
Pension Reform Act 2014 (8% from the employee, 10% from the employer), with NHF (2.5% of basic) off until the school
turns it on. Tax law changes, and a school's own circumstances matter: the settings page asks for the name of the
accountant who checked them, and nothing here is tax advice.

PAYE is worked out on the month's pay as if it were paid all year (annualised): taxable pay less pension, NHF and rent
relief, through the bands, divided by twelve. A member of staff whose pay changes in the year can end the year a little
over or under; the accountant settles that at the year's end, as with any annualised payroll.

Bank account numbers, pension PINs and tax IDs are encrypted at rest under a key derived, for this school alone, from
BEACON_DELIVERY_KEY (the same key the school's other secrets use, so core/secrets_rotation.py moves them too).
"""

import base64
import json
import os
import re

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_ENCRYPTED = 'enc:v1:'

# Nigeria Tax Act 2025, from 1 January 2026: [upper limit of the band in kobo a year (None: no limit), rate in basis points]
DEFAULT_PAYE_BANDS = [[80_000_000, 0], [300_000_000, 1500], [1_200_000_000, 1800], [2_500_000_000, 2100],
                      [5_000_000_000, 2300], [None, 2500]]
DEFAULT_ITEMS = (('Housing allowance', 'earning', 1, 1), ('Transport allowance', 'earning', 1, 1),
                 ('Responsibility allowance', 'earning', 1, 0), ('Cooperative', 'deduction', 0, 0),
                 ('Salary advance', 'deduction', 0, 0))
STATUSES = {'draft': 'Draft', 'approved': 'Approved', 'paid': 'Paid', 'cancelled': 'Cancelled'}
MAX_MONEY = 10_000_000_000      # kobo: ₦100m, more than any one line of a school's payroll
NIGERIAN_BANKS = ('Access Bank', 'Citibank', 'Ecobank', 'Fidelity Bank', 'First Bank', 'FCMB', 'Globus Bank', 'GTBank',
                  'Heritage Bank', 'Jaiz Bank', 'Keystone Bank', 'Kuda', 'Lotus Bank', 'Moniepoint', 'OPay', 'Optimus Bank',
                  'PalmPay', 'Parallex Bank', 'Polaris Bank', 'Premium Trust Bank', 'Providus Bank', 'Signature Bank',
                  'Stanbic IBTC', 'Standard Chartered', 'Sterling Bank', 'SunTrust Bank', 'Taj Bank', 'Titan Trust Bank',
                  'Union Bank', 'UBA', 'Unity Bank', 'Wema Bank', 'Zenith Bank')


# ---------------------------------------------------------------- money
def pct(amount, bp):
    """``bp`` basis points of ``amount`` kobo, rounded half up to the kobo."""
    if amount <= 0 or bp <= 0:
        return 0
    return (amount * bp + 5000) // 10000


def parse_naira(value):
    """Kobo from what a person typed ("150,000", "₦150000.50"), or None if it is not an amount in range."""
    text = str(value or '').strip().replace(',', '').replace('₦', '').replace(' ', '')
    if not re.fullmatch(r'\d{1,9}(\.\d{1,2})?', text):
        return None
    whole, _, frac = text.partition('.')
    kobo = int(whole) * 100 + int((frac + '00')[:2])
    return kobo if kobo <= MAX_MONEY else None


def parse_percent(value):
    """Basis points from "8", "2.5" or "17.25" (at most 100%), or None."""
    text = str(value or '').strip().rstrip('%')
    if not re.fullmatch(r'\d{1,3}(\.\d{1,2})?', text):
        return None
    whole, _, frac = text.partition('.')
    bp = int(whole) * 100 + int((frac + '00')[:2])
    return bp if bp <= 10000 else None


def naira(kobo, sign=True):
    kobo = int(kobo or 0)
    text = f'{abs(kobo) // 100:,}.{abs(kobo) % 100:02d}'
    return ('-' if kobo < 0 else '') + ('₦' if sign else '') + text


def percent(bp):
    bp = int(bp or 0)
    return f'{bp // 100}' + (f'.{bp % 100:02d}'.rstrip('0') if bp % 100 else '') + '%'


def naira_input(kobo):
    """Kobo as a person would type it back into a form (no symbol, no commas)."""
    kobo = int(kobo or 0)
    return f'{kobo // 100}' + (f'.{kobo % 100:02d}' if kobo % 100 else '')


# ---------------------------------------------------------------- secrets at rest
def _cipher():
    from flask import current_app
    from control_plane.context import current_tenant
    material = os.environ.get('BEACON_DELIVERY_KEY', '').strip() or current_app.secret_key
    if isinstance(material, str):
        material = material.encode()
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=b'brightstars-payroll-v1:' + current_tenant().slug.encode()).derive(material)
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(text):
    return _ENCRYPTED + _cipher().encrypt(text.encode()).decode() if text else None


def decrypt(stored):
    if not stored or not stored.startswith(_ENCRYPTED):
        return ''
    try:
        return _cipher().decrypt(stored[len(_ENCRYPTED):].encode()).decode()
    except (InvalidToken, ValueError):
        return ''


def clean_account_number(value):
    """A Nigerian bank account number (NUBAN, ten digits), or None."""
    digits = re.sub(r'[\s-]', '', str(value or ''))
    return digits if re.fullmatch(r'\d{10}', digits) else None


# ---------------------------------------------------------------- statutory
def bands_of(settings):
    try:
        bands = json.loads(settings.paye_bands_json or '')
        if _bands_ok(bands):
            return bands
    except (TypeError, ValueError):
        pass
    return [list(b) for b in DEFAULT_PAYE_BANDS]


def _bands_ok(bands):
    if not isinstance(bands, list) or not 1 <= len(bands) <= 12:
        return False
    last = 0
    for i, band in enumerate(bands):
        if not (isinstance(band, list) and len(band) == 2 and isinstance(band[1], int) and 0 <= band[1] <= 10000):
            return False
        upper = band[0]
        if upper is None:
            if i != len(bands) - 1:
                return False
        elif not (isinstance(upper, int) and upper > last):
            return False
        else:
            last = upper
    return bands[-1][0] is None


def parse_bands(form):
    """PAYE bands from the settings form (``band_upper_N`` in naira, empty for the last; ``band_rate_N`` in percent),
    or (None, error)."""
    bands = []
    for i in range(12):
        upper_raw, rate_raw = str(form.get(f'band_upper_{i}', '')).strip(), str(form.get(f'band_rate_{i}', '')).strip()
        if not upper_raw and not rate_raw:
            continue
        rate = parse_percent(rate_raw)
        if rate is None:
            return None, f'Band {len(bands) + 1}: the rate must be a percentage from 0 to 100.'
        upper = None
        if upper_raw:
            upper = parse_naira(upper_raw)
            if upper is None or upper == 0:
                return None, f'Band {len(bands) + 1}: the limit must be an amount in naira.'
        bands.append([upper, rate])
    if not _bands_ok(bands):
        return None, 'Each band\'s limit must be above the one before, and only the last band has no limit.'
    return bands, None


def annual_tax(taxable, bands):
    """Tax on ``taxable`` kobo a year, through the bands."""
    tax, lower = 0, 0
    for upper, rate in bands:
        if taxable <= lower:
            break
        top = taxable if upper is None else min(taxable, upper)
        tax += pct(top - lower, rate)
        if upper is None:
            break
        lower = upper
    return tax


def payslip(pay, items, adjustments, settings, unpaid_days=0, working_days=0):
    """One member of staff's month.

    ``pay`` is their StaffPay; ``items`` [(PayItem, StaffPayItem or None)]; ``adjustments`` this run's one-off lines.
    Returns {'lines': [{'kind', 'label', 'amount'}], 'basic', 'gross', 'deductions', 'net', 'paye', 'pension_employee',
    'pension_employer', 'nhf', 'warnings'}.
    """
    basic = max(int(pay.basic or 0), 0)
    lines = [{'kind': 'earning', 'label': 'Basic salary', 'amount': basic}]
    taxable, pension_base = basic, basic
    regular = basic
    for item, own in items:
        if item.kind != 'earning' or not item.active:
            continue
        amount = _item_amount(item, own, basic)
        if amount:
            lines.append({'kind': 'earning', 'label': item.name, 'amount': amount})
            regular += amount
            taxable += amount if item.taxable else 0
            pension_base += amount if item.pensionable else 0
    for adj in adjustments:
        if adj.kind == 'earning':
            lines.append({'kind': 'earning', 'label': adj.label, 'amount': adj.amount})
            taxable += adj.amount if adj.taxable else 0
    if settings.deduct_unpaid_leave and unpaid_days and working_days:
        cut = min(regular * unpaid_days // working_days, regular)
        if cut:
            lines.append({'kind': 'earning', 'label': f'Unpaid leave ({unpaid_days} day{"s" if unpaid_days != 1 else ""})', 'amount': -cut})
            taxable = max(taxable - cut, 0)
            pension_base = max(pension_base - min(pension_base * unpaid_days // working_days, pension_base), 0)
    gross = sum(line['amount'] for line in lines)

    pension_employee = pension_employer = nhf = paye = 0
    if settings.pension_enabled and not pay.pension_exempt:
        pension_employee = pct(pension_base, settings.pension_employee_bp)
        pension_employer = pct(pension_base, settings.pension_employer_bp)
    if settings.nhf_enabled and not pay.nhf_exempt:
        nhf = pct(basic, settings.nhf_bp)
    if settings.paye_enabled and not pay.paye_exempt:
        relief = min(pct(int(pay.annual_rent or 0), settings.rent_relief_bp), settings.rent_relief_cap)
        annual = max(taxable * 12 - pension_employee * 12 - nhf * 12 - relief, 0)
        paye = (annual_tax(annual, bands_of(settings)) + 6) // 12
    for label, amount in (('PAYE tax', paye), ('Pension (employee)', pension_employee), ('NHF', nhf)):
        if amount:
            lines.append({'kind': 'deduction', 'label': label, 'amount': amount})
    for item, own in items:
        if item.kind != 'deduction' or not item.active:
            continue
        amount = _item_amount(item, own, basic)
        if amount:
            lines.append({'kind': 'deduction', 'label': item.name, 'amount': amount})
    for adj in adjustments:
        if adj.kind == 'deduction':
            lines.append({'kind': 'deduction', 'label': adj.label, 'amount': adj.amount})
    deductions = sum(line['amount'] for line in lines if line['kind'] == 'deduction')
    net = gross - deductions
    warnings = []
    if basic == 0:
        warnings.append('No basic salary')
    if not pay.account_number_enc:
        warnings.append('No bank account')
    if net < 0:
        warnings.append('Deductions are more than the pay')
    if pension_employee and not pay.pfa:
        warnings.append('No pension administrator')
    return {'lines': lines, 'basic': basic, 'gross': gross, 'deductions': deductions, 'net': net, 'paye': paye,
            'pension_employee': pension_employee, 'pension_employer': pension_employer, 'nhf': nhf, 'warnings': warnings}


def _item_amount(item, own, basic):
    if item.calc == 'percent_basic':
        rate = own.rate_bp if own is not None and own.rate_bp is not None else item.rate_bp
        return pct(basic, rate or 0)
    return own.amount if own is not None and own.amount is not None else int(item.amount or 0)


def settings_snapshot(settings):
    return json.dumps({'paye_enabled': settings.paye_enabled, 'bands': bands_of(settings), 'rent_relief_bp': settings.rent_relief_bp,
                       'rent_relief_cap': settings.rent_relief_cap, 'pension_enabled': settings.pension_enabled,
                       'pension_employee_bp': settings.pension_employee_bp, 'pension_employer_bp': settings.pension_employer_bp,
                       'nhf_enabled': settings.nhf_enabled, 'nhf_bp': settings.nhf_bp, 'deduct_unpaid_leave': settings.deduct_unpaid_leave,
                       'checked_by': settings.checked_by, 'checked_on': settings.checked_on}, separators=(',', ':'))
