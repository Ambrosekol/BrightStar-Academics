"""Staff attendance: clocking in and out at school, by a code that changes every 30 seconds on a screen at the school
and the phone's location, and the register the school keeps from it. The rules are core/staff_attendance.py and the
pages blueprints/school/staff_attendance.py. A day's record belongs to a staff record (models/hr.py), so staff
without a portal account are marked on the register by hand. See models/__init__.py for the column conventions.

No location is stored: only how far from the school the phone said it was, and how sure it was.
"""

from sqlalchemy import Float, ForeignKey, Index, Integer, Text, UniqueConstraint, text

from .base import db


class StaffAttendanceSettings(db.Model):
    """One row: how the school takes staff attendance."""

    __tablename__ = 'staff_attendance_settings'

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    enabled = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    method = db.Column(Text, nullable=False, default='qr_gps', server_default=text("'qr_gps'"))  # qr_gps, qr, gps
    latitude = db.Column(Float)
    longitude = db.Column(Float)
    radius_m = db.Column(Integer, nullable=False, default=150, server_default=text('150'))
    max_accuracy_m = db.Column(Integer, nullable=False, default=100, server_default=text('100'))
    late_after = db.Column(Text, nullable=False, default='08:00', server_default=text("'08:00'"))  # HH:MM, school time
    work_days = db.Column(Text, nullable=False, default='12345', server_default=text("'12345'"))  # ISO weekdays
    started_on = db.Column(Text)                       # the first day absences are counted from
    code_secret = db.Column(Text)                      # the key the changing codes are made with
    display_token_hash = db.Column(Text)               # the screen's link, hashed; a new link ends the old one
    display_set_at = db.Column(Text)
    updated_at = db.Column(Text)
    updated_by = db.Column(Integer, ForeignKey('admins.id'))


class StaffAttendanceDevice(db.Model):
    """The one phone (browser) a member of staff clocks in with. Only a hash of its random tag is kept."""

    __tablename__ = 'staff_attendance_devices'
    __table_args__ = (Index('idx_staff_att_devices_admin', 'admin_id', 'active'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    admin_id = db.Column(Integer, ForeignKey('admins.id'), nullable=False)
    tag_hash = db.Column(Text, nullable=False)
    label = db.Column(Text)                            # what the browser said it was, for the manager
    registered_at = db.Column(Text, nullable=False)
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    revoked_at = db.Column(Text)
    revoked_by = db.Column(Integer, ForeignKey('admins.id'))


class StaffAttendanceRecord(db.Model):
    __tablename__ = 'staff_attendance_records'
    __table_args__ = (UniqueConstraint('staff_id', 'day'), Index('idx_staff_att_day', 'day'))

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    staff_id = db.Column(Integer, ForeignKey('hr_staff.id', ondelete='CASCADE'), nullable=False)
    day = db.Column(Text, nullable=False)              # YYYY-MM-DD, school time
    clock_in = db.Column(Text)                         # HH:MM, school time
    clock_out = db.Column(Text)
    late = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    in_method = db.Column(Text)                        # 'phone', 'register' (by hand) or 'correction'
    out_method = db.Column(Text)
    in_distance_m = db.Column(Integer)
    in_accuracy_m = db.Column(Integer)
    out_distance_m = db.Column(Integer)
    out_accuracy_m = db.Column(Integer)
    note = db.Column(Text)
    marked_by = db.Column(Integer, ForeignKey('admins.id'))
    created_at = db.Column(Text, nullable=False)
    updated_at = db.Column(Text)


class StaffAttendanceCorrection(db.Model):
    """A member of staff's request to put right a day: a clock-in or a clock-out they could not make."""

    __tablename__ = 'staff_attendance_corrections'
    __table_args__ = (Index('idx_staff_att_corr_status', 'status'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    staff_id = db.Column(Integer, ForeignKey('hr_staff.id', ondelete='CASCADE'), nullable=False)
    day = db.Column(Text, nullable=False)
    kind = db.Column(Text, nullable=False)             # 'in' or 'out'
    time = db.Column(Text, nullable=False)             # HH:MM
    reason = db.Column(Text, nullable=False)
    status = db.Column(Text, nullable=False, default='pending', server_default=text("'pending'"))
    requested_by = db.Column(Integer, ForeignKey('admins.id'))
    requested_at = db.Column(Text, nullable=False)
    decided_by = db.Column(Integer, ForeignKey('admins.id'))
    decided_at = db.Column(Text)
    decision_note = db.Column(Text)


class StaffAttendanceClosedDay(db.Model):
    """A day the school is closed (a public holiday, mid-term break): nobody is absent on it."""

    __tablename__ = 'staff_attendance_closed_days'
    __table_args__ = (UniqueConstraint('day'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    day = db.Column(Text, nullable=False)
    label = db.Column(Text, nullable=False)
    created_at = db.Column(Text, nullable=False)
    created_by = db.Column(Integer, ForeignKey('admins.id'))
