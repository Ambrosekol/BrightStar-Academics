"""School tenancy, per-school settings and student-number issuance."""

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint, text

from .base import db


class SchoolDeliverySetting(db.Model):
    """A school's own email and SMS account: host, sender, and the secrets for them.

    Deliberately not in the general settings store (school_public_settings), which is read
    freely to render pages. Only core/delivery.py reads this table, and it encrypts the secrets.
    """

    __tablename__ = 'school_delivery_settings'
    __table_args__ = (UniqueConstraint('setting_key'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    setting_key = db.Column(Text, nullable=False)
    setting_value = db.Column(Text)
    updated_at = db.Column(Text, nullable=False)
    updated_by = db.Column(Integer, ForeignKey('admins.id'))


class SchoolPaymentSetting(db.Model):
    """A school's own Paystack account: its public key, and its secret key, encrypted.

    The same shape as SchoolDeliverySetting, and for the same reason: kept apart from the general
    settings store, which is read freely to render pages. Only core/payments.py reads this table.
    """

    __tablename__ = 'school_payment_settings'
    __table_args__ = (UniqueConstraint('setting_key'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    setting_key = db.Column(Text, nullable=False)
    setting_value = db.Column(Text)
    updated_at = db.Column(Text, nullable=False)
    updated_by = db.Column(Integer, ForeignKey('admins.id'))


class School(db.Model):
    __tablename__ = 'schools'
    __table_args__ = (UniqueConstraint('code'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    code = db.Column(Text, nullable=False)
    name = db.Column(Text, nullable=False)
    motto = db.Column(Text)
    tagline = db.Column(Text)
    address = db.Column(Text)
    phone = db.Column(Text)
    email = db.Column(Text)
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    created_at = db.Column(Text, nullable=False)
    updated_at = db.Column(Text)


class SchoolSetting(db.Model):
    __tablename__ = 'school_settings'
    __table_args__ = (
        Index('ix_school_settings_key', 'setting_key'),
        Index('ix_school_settings_school', 'school_id'),
        Index('uq_school_settings_school_key', 'school_id', 'setting_key', unique=True),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    school_id = db.Column(Integer, ForeignKey('schools.id'), nullable=False)
    setting_key = db.Column(Text, nullable=False)
    setting_value = db.Column(Text)
    updated_at = db.Column(Text)
    updated_by = db.Column(Integer)


class SchoolNumberingPolicy(db.Model):
    """How a school builds its student numbers: prefix, year, padding and the
    next sequence value to hand out."""

    __tablename__ = 'school_numbering_policies'
    __table_args__ = (UniqueConstraint('school_id'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    school_id = db.Column(Integer, ForeignKey('schools.id'), nullable=False)
    label = db.Column(Text, nullable=False, default='Registration Number', server_default=text("'Registration Number'"))
    prefix = db.Column(Text)
    include_year = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    sequence_start = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    next_sequence = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    padding = db.Column(Integer, nullable=False, default=4, server_default=text('4'))
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    created_at = db.Column(Text, nullable=False)
    updated_at = db.Column(Text)


class StudentNumberAllocation(db.Model):
    """Every student number ever issued, so numbers are never silently reused."""

    __tablename__ = 'student_number_allocations'
    __table_args__ = (
        Index('idx_student_number_allocations_sequence', 'school_id', 'sequence_number'),
        Index('idx_student_number_allocations_student', 'student_id'),
        Index('idx_student_number_allocations_school', 'school_id'),
        Index('uq_student_number_allocations_school_number', 'school_id', 'student_number', unique=True,
              sqlite_where=text("TRIM(student_number) <> ''"),
              postgresql_where=text("TRIM(student_number) <> ''")),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    school_id = db.Column(Integer, ForeignKey('schools.id', ondelete='RESTRICT'), nullable=False)
    student_number = db.Column(Text, nullable=False)
    sequence_number = db.Column(Integer)
    allocation_year = db.Column(Integer)
    source = db.Column(Text, nullable=False, default='generated', server_default=text("'generated'"))
    student_id = db.Column(Integer, ForeignKey('students.id', ondelete='SET NULL'))
    allocated_at = db.Column(Text, nullable=False)
    allocated_by = db.Column(Text)
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
