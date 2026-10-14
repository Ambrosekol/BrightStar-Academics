"""Finance: fee catalogue, per-student assessments, payments and receipts."""

from sqlalchemy import Float, ForeignKey, Index, Integer, Text, UniqueConstraint, text

from .base import db


class FinanceFeeItem(db.Model):
    __tablename__ = 'finance_fee_items'
    __table_args__ = (Index('idx_finance_fee_items_stage_active', 'stage', 'active', 'effective_from'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    name = db.Column(Text, nullable=False)
    category = db.Column(Text, nullable=False, default='School Fees', server_default=text("'School Fees'"))
    stage = db.Column(Text, nullable=False, default='All', server_default=text("'All'"))
    amount = db.Column(Float, nullable=False)
    applicability = db.Column(Text, nullable=False, default='Annual', server_default=text("'Annual'"))
    required = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    optional = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    effective_from = db.Column(Text)
    effective_to = db.Column(Text)
    notes = db.Column(Text)
    created_at = db.Column(Text, nullable=False)
    updated_at = db.Column(Text, nullable=False)
    created_by = db.Column(Integer, ForeignKey('admins.id'))
    updated_by = db.Column(Integer, ForeignKey('admins.id'))


class FinanceFeeItemClass(db.Model):
    __tablename__ = 'finance_fee_item_classes'
    __table_args__ = (
        Index('idx_finance_fee_item_classes_item_active', 'fee_item_id', 'active'),
        Index('idx_finance_fee_item_classes_class_active', 'class_id', 'active'),
        UniqueConstraint('fee_item_id', 'class_id'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    fee_item_id = db.Column(Integer, ForeignKey('finance_fee_items.id', ondelete='CASCADE'), nullable=False)
    class_id = db.Column(Integer, ForeignKey('school_classes.id', ondelete='RESTRICT'), nullable=False)
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    created_at = db.Column(Text, nullable=False)
    created_by = db.Column(Integer)


class FinanceFeeAssessment(db.Model):
    """A fee charged to one student for one session.

    ``amount`` is what the student owes for it, after any discount (blueprints/finance/discounts.py), so every
    balance in finance stays ``amount`` minus what was applied. ``gross_amount`` is the fee before the discount and
    ``discount_amount`` / ``discount_note`` what was taken off and why; all three are None for a fee charged in full.
    """

    __tablename__ = 'finance_fee_assessments'
    # Every balance reads a session's bills or a student's bills; the reminders and the report card hold read them all.
    __table_args__ = (Index('idx_finance_assessments_session', 'session_id', 'active'),
                      Index('idx_finance_assessments_student', 'student_id', 'active'))

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    student_id = db.Column(Integer, ForeignKey('students.id', ondelete='CASCADE'), nullable=False)
    session_id = db.Column(Integer, ForeignKey('academic_sessions.id'), nullable=False)
    category = db.Column(Text, nullable=False)
    amount = db.Column(Float, nullable=False)
    due_date = db.Column(Text)
    notes = db.Column(Text)
    created_by = db.Column(Integer, ForeignKey('admins.id'))
    created_at = db.Column(Text, nullable=False)
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    fee_item_id = db.Column(Integer)
    term = db.Column(Text, default='Full Session', server_default=text("'Full Session'"))
    voided_at = db.Column(Text)
    gross_amount = db.Column(Float)
    discount_amount = db.Column(Float)
    discount_note = db.Column(Text)


class FinancePayment(db.Model):
    __tablename__ = 'finance_payments'
    __table_args__ = (
        Index('idx_finance_payments_payer', 'payer_name'),
        Index('idx_finance_payments_correction', 'correction_of_payment_id'),
        Index('idx_finance_payments_status', 'status', 'paid_at'),
        Index('idx_finance_payments_student', 'student_id', 'session_id', 'paid_at'),
        Index('idx_finance_payments_recorded_by', 'recorded_by', 'paid_at'),
        UniqueConstraint('receipt_no'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    receipt_no = db.Column(Text, nullable=False)
    student_id = db.Column(Integer, ForeignKey('students.id', ondelete='CASCADE'), nullable=False)
    session_id = db.Column(Integer, ForeignKey('academic_sessions.id'), nullable=False)
    amount = db.Column(Float, nullable=False)
    category = db.Column(Text, nullable=False)
    method = db.Column(Text, nullable=False)
    reference = db.Column(Text)
    paid_at = db.Column(Text, nullable=False)
    recorded_by = db.Column(Integer, ForeignKey('admins.id'), nullable=False)
    status = db.Column(Text, nullable=False, default='posted', server_default=text("'posted'"))
    notes = db.Column(Text)
    created_at = db.Column(Text, nullable=False)
    voided_at = db.Column(Text)
    voided_by = db.Column(Integer)
    void_reason = db.Column(Text)
    correction_of_payment_id = db.Column(Integer)
    payer_name = db.Column(Text)
    receipt_notes = db.Column(Text)


class FinancePaymentAllocation(db.Model):
    """Applies part of a payment to a specific fee assessment."""

    __tablename__ = 'finance_payment_allocations'
    __table_args__ = (
        Index('idx_finance_alloc_assessment', 'assessment_id'),
        Index('idx_finance_alloc_payment', 'payment_id'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    payment_id = db.Column(Integer, ForeignKey('finance_payments.id'), nullable=False)
    assessment_id = db.Column(Integer, ForeignKey('finance_fee_assessments.id'), nullable=False)
    amount = db.Column(Float, nullable=False)
    created_by = db.Column(Integer)
    created_at = db.Column(Text, nullable=False)
    voided_at = db.Column(Text)
    voided_by = db.Column(Integer)
    void_reason = db.Column(Text)


class FinanceDeliveryLog(db.Model):
    """Record of each attempt to deliver a receipt by email or SMS."""

    __tablename__ = 'finance_delivery_logs'

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    payment_id = db.Column(Integer, ForeignKey('finance_payments.id', ondelete='CASCADE'), nullable=False)
    channel = db.Column(Text, nullable=False)
    recipient = db.Column(Text)
    status = db.Column(Text, nullable=False)
    provider_reference = db.Column(Text)
    error_message = db.Column(Text)
    sent_by = db.Column(Integer, ForeignKey('admins.id'))
    sent_at = db.Column(Text, nullable=False)


class FinanceOnlinePayment(db.Model):
    """One attempt by a parent to pay online through Paystack, from the moment it is started to
    however it ends.

    ``reference`` is generated here and sent to Paystack as the transaction's own reference, so it
    is what ties a webhook, a callback and this row together; it is unique regardless of how many
    times a parent tries. ``status`` moves pending -> success or failed (Paystack itself decides
    which) or abandoned (the parent never completed checkout); only a conditional UPDATE that
    still finds it 'pending' is allowed to move it out of that state, so a callback and a webhook
    racing to confirm the same payment can never both act on it (core/payments.py).

    A payment confirmed this way still becomes an ordinary FinancePayment (``payment_id``), made
    through the exact same code a member of staff's manually recorded payment is - the receipt,
    the notification, the audit trail are all identical either way.
    """

    __tablename__ = 'finance_online_payments'
    __table_args__ = (
        UniqueConstraint('reference'),
        Index('idx_finance_online_payments_status', 'status', 'created_at'),
        Index('idx_finance_online_payments_student', 'student_id'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    reference = db.Column(Text, nullable=False)
    student_id = db.Column(Integer, ForeignKey('students.id', ondelete='CASCADE'), nullable=False)
    parent_id = db.Column(Integer, ForeignKey('parent_accounts.id'))
    session_id = db.Column(Integer, ForeignKey('academic_sessions.id'), nullable=False)
    amount = db.Column(Float, nullable=False)
    status = db.Column(Text, nullable=False, default='pending', server_default=text("'pending'"))
    paystack_transaction_id = db.Column(Text)
    payment_id = db.Column(Integer, ForeignKey('finance_payments.id'))
    failure_reason = db.Column(Text)
    created_at = db.Column(Text, nullable=False)
    verified_at = db.Column(Text)
    # JSON list of {assessment_id, amount}: the specific fees this payment was started for, so
    # that once Paystack confirms it the money is applied to exactly those fees. Null = a lump sum.
    items = db.Column(Text)


class FinanceRefund(db.Model):
    """One attempt to refund a payment that was made online, through Paystack, back to the payer.

    Started here as 'pending' the moment Paystack *accepts the request to refund* - which is not
    the same as the money having actually moved back yet, the same distinction core/payments.py
    already draws for a payment itself. Only the ``refund.processed`` webhook (paystack_webhook,
    blueprints/finance/paystack.py) moves this to 'processed' and, at that same moment, voids the
    original FinancePayment - so a payment never shows as both posted and refunded, and never
    shows as refunded before Paystack has actually confirmed it. ``refund.failed`` moves it to
    'failed' and leaves the original payment exactly as it was, untouched.
    """

    __tablename__ = 'finance_refunds'
    __table_args__ = (
        Index('idx_finance_refunds_payment', 'payment_id'),
        Index('idx_finance_refunds_status', 'status'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    payment_id = db.Column(Integer, ForeignKey('finance_payments.id'), nullable=False)
    online_payment_id = db.Column(Integer, ForeignKey('finance_online_payments.id'), nullable=False)
    amount = db.Column(Float, nullable=False)
    reason = db.Column(Text)
    status = db.Column(Text, nullable=False, default='pending', server_default=text("'pending'"))
    paystack_refund_id = db.Column(Text)
    requested_by = db.Column(Integer, ForeignKey('admins.id'), nullable=False)
    requested_at = db.Column(Text, nullable=False)
    resolved_at = db.Column(Text)
    failure_reason = db.Column(Text)


class FinanceNewStudent(db.Model):
    """A student who joined the school (admitted from the entrance exam, registered by hand or imported) and so
    still has to be billed: the bursar's list of new students to bill (blueprints/finance/new_students.py).

    Written once, when the student is created. It stays open while the student has nothing billed, or has a bill not
    yet paid in full; once everything billed is paid it is closed with ``cleared_reason`` 'paid'. Someone with
    finance.manage may also close it by hand ('removed', with a note), for a student the school does not bill. Rows
    are kept once closed, so the list of who was billed and paid stays readable.

    ``source``: 'admitted', 'registered' or 'imported'."""

    __tablename__ = 'finance_new_students'
    __table_args__ = (
        UniqueConstraint('student_id'),
        Index('idx_finance_new_students_open', 'cleared_at', 'added_at'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    student_id = db.Column(Integer, ForeignKey('students.id', ondelete='CASCADE'), nullable=False)
    source = db.Column(Text, nullable=False, default='registered', server_default=text("'registered'"))
    added_at = db.Column(Text, nullable=False)
    cleared_at = db.Column(Text)
    cleared_by = db.Column(Integer, ForeignKey('admins.id'))
    cleared_reason = db.Column(Text)
    note = db.Column(Text)


class FinanceDiscount(db.Model):
    """A discount given to one student: a scholarship, a staff child's discount, or any other reduction.

    It is taken off each fee charged to the student that it covers (``category``, or every fee when it is 'All'),
    either as a percentage (``basis`` 'percent', ``value`` 0-100) or as a fixed number of naira off each such fee
    (``basis`` 'amount'). ``session_id`` None means every session until it is ended. Ending one keeps the row, with who
    ended it, so the school can always see who was given what. The sibling discount is not a row here but a school-wide
    rule (blueprints/finance/discounts.py).
    """

    __tablename__ = 'finance_discounts'
    __table_args__ = (Index('idx_finance_discounts_student', 'student_id', 'active'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    student_id = db.Column(Integer, ForeignKey('students.id', ondelete='CASCADE'), nullable=False)
    kind = db.Column(Text, nullable=False, default='discount', server_default=text("'discount'"))
    label = db.Column(Text)
    basis = db.Column(Text, nullable=False, default='percent', server_default=text("'percent'"))
    value = db.Column(Float, nullable=False)
    category = db.Column(Text, nullable=False, default='All', server_default=text("'All'"))
    session_id = db.Column(Integer, ForeignKey('academic_sessions.id'))
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    note = db.Column(Text)
    created_by = db.Column(Integer, ForeignKey('admins.id'))
    created_at = db.Column(Text, nullable=False)
    ended_by = db.Column(Integer, ForeignKey('admins.id'))
    ended_at = db.Column(Text)


class FinanceInstalmentPlan(db.Model):
    """How the fees of one billing period may be paid in parts: 60% by 15 September, 40% by 15 October.

    ``parts_json`` is ``[[percent, 'YYYY-MM-DD'], ...]`` in date order, the percents adding up to 100. A student on the
    plan (FinanceStudentPlan) owes, by each date, that share of everything charged to them for ``term`` of
    ``session_id``; until a date passes, its part is not overdue (blueprints/finance/instalments.py)."""

    __tablename__ = 'finance_instalment_plans'
    __table_args__ = (Index('idx_finance_plans_session', 'session_id', 'term', 'active'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    name = db.Column(Text, nullable=False)
    session_id = db.Column(Integer, ForeignKey('academic_sessions.id'), nullable=False)
    term = db.Column(Text, nullable=False, default='Full Session', server_default=text("'Full Session'"))
    parts_json = db.Column(Text, nullable=False)
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    created_by = db.Column(Integer, ForeignKey('admins.id'))
    created_at = db.Column(Text, nullable=False)
    updated_at = db.Column(Text)


class FinanceStudentPlan(db.Model):
    """A student on an instalment plan. One plan per student per billing period (kept so by the code that assigns)."""

    __tablename__ = 'finance_student_plans'
    __table_args__ = (UniqueConstraint('student_id', 'plan_id'), Index('idx_finance_student_plans_plan', 'plan_id'))

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    student_id = db.Column(Integer, ForeignKey('students.id', ondelete='CASCADE'), nullable=False)
    plan_id = db.Column(Integer, ForeignKey('finance_instalment_plans.id', ondelete='CASCADE'), nullable=False)
    assigned_by = db.Column(Integer, ForeignKey('admins.id'))
    assigned_at = db.Column(Text, nullable=False)


class FinanceReportHoldExemption(db.Model):
    """A student whose report cards reach the family even though fees are owed, while report cards are held for
    unpaid fees (blueprints/finance/report_hold.py): a promise to pay, a hardship case. Removed when no longer given."""

    __tablename__ = 'finance_report_hold_exemptions'
    __table_args__ = (UniqueConstraint('student_id'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    student_id = db.Column(Integer, ForeignKey('students.id', ondelete='CASCADE'), nullable=False)
    note = db.Column(Text)
    granted_by = db.Column(Integer, ForeignKey('admins.id'))
    granted_at = db.Column(Text, nullable=False)


class FinanceExpense(db.Model):
    """Money the school spent: diesel, repairs, books, a vendor's bill. Staff salaries are not entered here; the
    profit and loss takes them from the payroll. Never deleted: a mistake is voided with a reason, like a payment.
    ``receipt_path`` is a photograph of the vendor's receipt, in the private 'expenses' uploads folder."""

    __tablename__ = 'finance_expenses'
    __table_args__ = (Index('idx_finance_expenses_spent', 'status', 'spent_on'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    spent_on = db.Column(Text, nullable=False)          # YYYY-MM-DD
    category = db.Column(Text, nullable=False)
    amount = db.Column(Float, nullable=False)
    paid_to = db.Column(Text)
    description = db.Column(Text)
    method = db.Column(Text)
    reference = db.Column(Text)
    receipt_path = db.Column(Text)
    status = db.Column(Text, nullable=False, default='posted', server_default=text("'posted'"))
    recorded_by = db.Column(Integer, ForeignKey('admins.id'), nullable=False)
    created_at = db.Column(Text, nullable=False)
    voided_at = db.Column(Text)
    voided_by = db.Column(Integer, ForeignKey('admins.id'))
    void_reason = db.Column(Text)
