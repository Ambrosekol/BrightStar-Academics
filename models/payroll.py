"""Payroll: each member of staff's pay (basic salary, allowances, deductions, bank details), the school's statutory
settings (PAYE, pension, NHF), and each month's payroll run with its payslips. The rules are core/payroll.py and the
pages blueprints/school/payroll.py. See models/__init__.py for the column conventions.

All money is whole kobo (Integer), never a float, so nothing is lost to rounding. A payslip keeps a copy of every
line it was paid with, so changing someone's salary later never changes a payslip already approved. Bank account
numbers, pension PINs and tax IDs are encrypted (core/payroll.py); only the last digits are shown on a page.
"""

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint, text

from .base import db


class PayrollSettings(db.Model):
    """One row: the school's statutory deductions, as its accountant set them."""

    __tablename__ = 'payroll_settings'

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    paye_enabled = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    paye_bands_json = db.Column(Text)                  # [[upper limit in kobo or null, rate in basis points], ...]
    rent_relief_bp = db.Column(Integer, nullable=False, default=2000, server_default=text('2000'))       # 20% of rent
    rent_relief_cap = db.Column(Integer, nullable=False, default=50_000_000, server_default=text('50000000'))  # kobo a year
    pension_enabled = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    pension_employee_bp = db.Column(Integer, nullable=False, default=800, server_default=text('800'))
    pension_employer_bp = db.Column(Integer, nullable=False, default=1000, server_default=text('1000'))
    nhf_enabled = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    nhf_bp = db.Column(Integer, nullable=False, default=250, server_default=text('250'))                 # of basic
    deduct_unpaid_leave = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    checked_by = db.Column(Text)                       # the accountant who checked these rates, in their own words
    checked_on = db.Column(Text)
    updated_at = db.Column(Text)
    updated_by = db.Column(Integer, ForeignKey('admins.id'))


class PayItem(db.Model):
    """An allowance or a deduction the school pays or takes (Housing, Transport, Cooperative, Salary advance...)."""

    __tablename__ = 'payroll_items'
    __table_args__ = (UniqueConstraint('name'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    name = db.Column(Text, nullable=False)
    kind = db.Column(Text, nullable=False)             # 'earning' or 'deduction'
    calc = db.Column(Text, nullable=False, default='fixed', server_default=text("'fixed'"))  # 'fixed' or 'percent_basic'
    amount = db.Column(Integer, nullable=False, default=0, server_default=text('0'))          # kobo, when fixed
    rate_bp = db.Column(Integer, nullable=False, default=0, server_default=text('0'))         # when a percentage of basic
    taxable = db.Column(Integer, nullable=False, default=1, server_default=text('1'))         # an earning counted for PAYE
    pensionable = db.Column(Integer, nullable=False, default=0, server_default=text('0'))     # an earning counted for pension
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    sort_order = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    created_at = db.Column(Text, nullable=False)


class StaffPay(db.Model):
    """What one member of staff is paid, and where."""

    __tablename__ = 'payroll_staff'
    __table_args__ = (UniqueConstraint('staff_id'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    staff_id = db.Column(Integer, ForeignKey('hr_staff.id', ondelete='CASCADE'), nullable=False)
    on_payroll = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    basic = db.Column(Integer, nullable=False, default=0, server_default=text('0'))           # kobo a month
    bank_name = db.Column(Text)
    account_name = db.Column(Text)
    account_number_enc = db.Column(Text)               # encrypted
    account_last4 = db.Column(Text)                    # for pages
    pfa = db.Column(Text)                              # pension fund administrator
    rsa_pin_enc = db.Column(Text)                      # encrypted
    tax_id_enc = db.Column(Text)                       # encrypted
    annual_rent = db.Column(Integer, nullable=False, default=0, server_default=text('0'))     # kobo, for rent relief
    paye_exempt = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    pension_exempt = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    nhf_exempt = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    updated_at = db.Column(Text)
    updated_by = db.Column(Integer, ForeignKey('admins.id'))


class StaffPayItem(db.Model):
    """An allowance or deduction given to one member of staff, at the item's own amount or their own."""

    __tablename__ = 'payroll_staff_items'
    __table_args__ = (UniqueConstraint('staff_id', 'item_id'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    staff_id = db.Column(Integer, ForeignKey('hr_staff.id', ondelete='CASCADE'), nullable=False)
    item_id = db.Column(Integer, ForeignKey('payroll_items.id', ondelete='CASCADE'), nullable=False)
    amount = db.Column(Integer)                        # kobo: their own fixed amount (None: the item's)
    rate_bp = db.Column(Integer)                       # their own percentage (None: the item's)


class PayrollRun(db.Model):
    __tablename__ = 'payroll_runs'
    __table_args__ = (Index('idx_payroll_runs_period', 'period', 'status'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    period = db.Column(Text, nullable=False)           # YYYY-MM
    status = db.Column(Text, nullable=False, default='draft', server_default=text("'draft'"))  # draft, approved, paid, cancelled
    gross = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    deductions = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    net = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    employer_pension = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    headcount = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    settings_json = db.Column(Text)                    # the statutory settings it was worked out with
    note = db.Column(Text)
    prepared_by = db.Column(Integer, ForeignKey('admins.id'))
    prepared_at = db.Column(Text, nullable=False)
    approved_by = db.Column(Integer, ForeignKey('admins.id'))
    approved_at = db.Column(Text)
    paid_by = db.Column(Integer, ForeignKey('admins.id'))
    paid_at = db.Column(Text)
    cancelled_by = db.Column(Integer, ForeignKey('admins.id'))
    cancelled_at = db.Column(Text)


class Payslip(db.Model):
    __tablename__ = 'payroll_payslips'
    __table_args__ = (UniqueConstraint('run_id', 'staff_id'), Index('idx_payslips_staff', 'staff_id'))

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    run_id = db.Column(Integer, ForeignKey('payroll_runs.id', ondelete='CASCADE'), nullable=False)
    staff_id = db.Column(Integer, ForeignKey('hr_staff.id'), nullable=False)
    name = db.Column(Text, nullable=False)             # as it was when paid
    job_title = db.Column(Text)
    staff_no = db.Column(Text)
    bank_name = db.Column(Text)
    account_name = db.Column(Text)
    account_last4 = db.Column(Text)
    lines_json = db.Column(Text, nullable=False)       # [{"kind", "label", "amount"}...]
    basic = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    gross = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    deductions = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    net = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    paye = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    pension_employee = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    pension_employer = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    nhf = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    pfa = db.Column(Text)
    warnings = db.Column(Text)                         # what HR should look at (no bank details, net below zero...)
    # Paid through the school's Paystack (core/payroll_transfers.py): None until sent, then pending, success, failed or
    # reversed. A pending or successful one is never sent again.
    transfer_status = db.Column(Text)
    transfer_reference = db.Column(Text, index=True)
    transfer_code = db.Column(Text)
    transfer_error = db.Column(Text)
    transfer_sent_at = db.Column(Text)
    transfer_sent_by = db.Column(Integer, ForeignKey('admins.id'))
    transfer_done_at = db.Column(Text)


class PayrollAdjustment(db.Model):
    """A one-off line on one payslip of a draft run: an arrears payment, a bonus, a salary advance taken back."""

    __tablename__ = 'payroll_adjustments'
    __table_args__ = (Index('idx_payroll_adj_run', 'run_id', 'staff_id'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    run_id = db.Column(Integer, ForeignKey('payroll_runs.id', ondelete='CASCADE'), nullable=False)
    staff_id = db.Column(Integer, ForeignKey('hr_staff.id', ondelete='CASCADE'), nullable=False)
    kind = db.Column(Text, nullable=False)             # 'earning' or 'deduction'
    label = db.Column(Text, nullable=False)
    amount = db.Column(Integer, nullable=False)
    taxable = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    created_at = db.Column(Text, nullable=False)
    created_by = db.Column(Integer, ForeignKey('admins.id'))
