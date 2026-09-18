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
    """A fee charged to one student for one session."""

    __tablename__ = 'finance_fee_assessments'

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
    status = db.Column(Text, nullable=False, server_default=text('"posted"'))
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
    """Record of each attempt to deliver a receipt by email or WhatsApp."""

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
