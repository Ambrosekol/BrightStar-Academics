"""Human resources: each member of staff's employment record, their qualifications and documents, the record of
their career (appointments, promotions, queries...), and their leave.

A staff record is not a portal account. A driver or a cleaner has a record and no sign-in; a teacher's record is
linked to their account (``admin_id``), which lets them ask for leave themselves. The rules are core/hr.py and the
pages blueprints/school/hr.py. See models/__init__.py for the column conventions.
"""

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint, text

from .base import db


class StaffRecord(db.Model):
    __tablename__ = 'hr_staff'
    __table_args__ = (UniqueConstraint('admin_id'), UniqueConstraint('staff_no'), Index('idx_hr_staff_status', 'status'))

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    admin_id = db.Column(Integer, ForeignKey('admins.id'))     # their portal account, if they have one
    staff_no = db.Column(Text)                                  # the school's own staff number, if it uses one
    title = db.Column(Text)                                     # Mr, Mrs, Miss, Dr...
    first_name = db.Column(Text, nullable=False)
    middle_name = db.Column(Text)
    last_name = db.Column(Text, nullable=False)
    gender = db.Column(Text)
    date_of_birth = db.Column(Text)                             # YYYY-MM-DD
    marital_status = db.Column(Text)
    phone = db.Column(Text)
    email = db.Column(Text)
    address = db.Column(Text)
    state_of_origin = db.Column(Text)
    photo_path = db.Column(Text)                                # uploads/admins/..., for staff without an account
    job_title = db.Column(Text)
    department = db.Column(Text)
    category = db.Column(Text, nullable=False, default='teaching', server_default=text("'teaching'"))  # or 'non_teaching'
    employment_type = db.Column(Text, nullable=False, default='full_time', server_default=text("'full_time'"))
    employment_date = db.Column(Text)
    confirmation_date = db.Column(Text)
    status = db.Column(Text, nullable=False, default='active', server_default=text("'active'"))  # active, suspended, left
    exit_date = db.Column(Text)
    exit_reason = db.Column(Text)
    nok_name = db.Column(Text)                                  # next of kin
    nok_relationship = db.Column(Text)
    nok_phone = db.Column(Text)
    nok_address = db.Column(Text)
    notes = db.Column(Text)                                     # private to HR: never shown to the person
    created_at = db.Column(Text, nullable=False)
    created_by = db.Column(Integer, ForeignKey('admins.id'))
    updated_at = db.Column(Text)
    updated_by = db.Column(Integer, ForeignKey('admins.id'))


class StaffQualification(db.Model):
    __tablename__ = 'hr_qualifications'
    __table_args__ = (Index('idx_hr_qualifications_staff', 'staff_id'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    staff_id = db.Column(Integer, ForeignKey('hr_staff.id', ondelete='CASCADE'), nullable=False)
    qualification = db.Column(Text, nullable=False)             # B.Ed. Mathematics, NCE, TRCN registration...
    institution = db.Column(Text)
    year = db.Column(Integer)
    created_at = db.Column(Text, nullable=False)
    created_by = db.Column(Integer, ForeignKey('admins.id'))


class StaffDocument(db.Model):
    """A file kept on a member of staff (a CV, a certificate, an appointment letter). Stored under uploads/hr/,
    which no page links to directly: only the HR download route serves it, after checking the permission."""

    __tablename__ = 'hr_documents'
    __table_args__ = (Index('idx_hr_documents_staff', 'staff_id'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    staff_id = db.Column(Integer, ForeignKey('hr_staff.id', ondelete='CASCADE'), nullable=False)
    label = db.Column(Text, nullable=False)
    category = db.Column(Text, nullable=False, default='other', server_default=text("'other'"))
    file_path = db.Column(Text, nullable=False)
    original_name = db.Column(Text)
    content_type = db.Column(Text)
    size_bytes = db.Column(Integer)
    uploaded_at = db.Column(Text, nullable=False)
    uploaded_by = db.Column(Integer, ForeignKey('admins.id'))


class StaffCareerEvent(db.Model):
    """One entry in a member of staff's career: never edited or deleted, only struck out with a reason, so the record
    of a query or a warning cannot quietly disappear."""

    __tablename__ = 'hr_career_events'
    __table_args__ = (Index('idx_hr_career_staff', 'staff_id'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    staff_id = db.Column(Integer, ForeignKey('hr_staff.id', ondelete='CASCADE'), nullable=False)
    kind = db.Column(Text, nullable=False)                      # core/hr.py, CAREER_KINDS
    effective_date = db.Column(Text, nullable=False)
    summary = db.Column(Text, nullable=False)
    detail = db.Column(Text)
    from_value = db.Column(Text)                                # a promotion's or a transfer's old job title or department
    to_value = db.Column(Text)
    recorded_at = db.Column(Text, nullable=False)
    recorded_by = db.Column(Integer, ForeignKey('admins.id'))
    struck_at = db.Column(Text)
    struck_by = db.Column(Integer, ForeignKey('admins.id'))
    struck_reason = db.Column(Text)


class LeaveType(db.Model):
    __tablename__ = 'hr_leave_types'
    __table_args__ = (UniqueConstraint('name'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    name = db.Column(Text, nullable=False)
    days_per_year = db.Column(Integer, nullable=False, default=0, server_default=text('0'))  # 0: no set allowance
    paid = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    sort_order = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    created_at = db.Column(Text, nullable=False)


class LeaveAllowance(db.Model):
    """A member of staff's own allowance of one kind of leave for one year, where it differs from the kind's usual
    days (days carried over, a longer service allowance)."""

    __tablename__ = 'hr_leave_allowances'
    __table_args__ = (UniqueConstraint('staff_id', 'leave_type_id', 'year'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    staff_id = db.Column(Integer, ForeignKey('hr_staff.id', ondelete='CASCADE'), nullable=False)
    leave_type_id = db.Column(Integer, ForeignKey('hr_leave_types.id'), nullable=False)
    year = db.Column(Integer, nullable=False)
    days = db.Column(Integer, nullable=False)
    note = db.Column(Text)
    set_at = db.Column(Text, nullable=False)
    set_by = db.Column(Integer, ForeignKey('admins.id'))


class LeaveRequest(db.Model):
    __tablename__ = 'hr_leave_requests'
    __table_args__ = (Index('idx_hr_leave_staff', 'staff_id', 'status'), Index('idx_hr_leave_status', 'status'))

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    staff_id = db.Column(Integer, ForeignKey('hr_staff.id', ondelete='CASCADE'), nullable=False)
    leave_type_id = db.Column(Integer, ForeignKey('hr_leave_types.id'), nullable=False)
    start_date = db.Column(Text, nullable=False)
    end_date = db.Column(Text, nullable=False)
    days = db.Column(Integer, nullable=False)                   # working days, Monday to Friday
    reason = db.Column(Text)
    cover = db.Column(Text)                                     # who covers their classes or duties
    status = db.Column(Text, nullable=False, default='pending', server_default=text("'pending'"))
    requested_by = db.Column(Integer, ForeignKey('admins.id'))
    requested_at = db.Column(Text, nullable=False)
    decided_by = db.Column(Integer, ForeignKey('admins.id'))
    decided_at = db.Column(Text)
    decision_note = db.Column(Text)
