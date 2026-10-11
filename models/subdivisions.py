"""Class subdivisions (JSS 1A, JSS 1 Gold, SS 1A Science) and the class transfers between them.

A large JSS or SSS class is split into subdivisions, each with its own class teacher. The class itself stays one
class for everything else - its subjects and subject teachers, timetable, fees and results - and a subdivision only
groups its students for their class teacher: the register, report card comments and releasing results reach the
students of that subdivision. A student's subdivision is on their enrolment for the session
(StudentEnrolment.subdivision_id), placed automatically (core/subdivisions.py). See models/__init__.py for the
column conventions.
"""

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint, text

from .base import db


class ClassSubdivision(db.Model):
    """One subdivision of a JSS or SSS class. ``department`` (Science, Art or Commercial; senior classes only) keeps
    it to students of that department; without one it takes anyone. Removing a subdivision makes it inactive, so the
    history of who was in it stays readable."""

    __tablename__ = 'class_subdivisions'
    __table_args__ = (
        Index('idx_class_subdivisions_class', 'class_id', 'active'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    class_id = db.Column(Integer, ForeignKey('school_classes.id', ondelete='CASCADE'), nullable=False)
    name = db.Column(Text, nullable=False)
    department = db.Column(Text)
    position = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    created_at = db.Column(Text, nullable=False)
    created_by = db.Column(Integer, ForeignKey('admins.id'))


class ClassTransfer(db.Model):
    """A request to move a student to another subdivision of their class, or to another department, or both.

    Asked for by the class teacher of the student's subdivision; nothing changes until the class teacher of the
    subdivision they would move to approves it (or the School Admin or a head teacher does). Status: 'pending',
    'approved', 'declined' or 'cancelled'. The from/to columns record the move as it was asked, so the history stays
    true after later moves."""

    __tablename__ = 'class_transfers'
    __table_args__ = (
        Index('idx_class_transfers_status', 'status', 'class_id'),
        Index('idx_class_transfers_student', 'student_id', 'status'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    student_id = db.Column(Integer, ForeignKey('students.id', ondelete='CASCADE'), nullable=False)
    session_id = db.Column(Integer, ForeignKey('academic_sessions.id'), nullable=False)
    class_id = db.Column(Integer, ForeignKey('school_classes.id'), nullable=False)
    from_subdivision_id = db.Column(Integer, ForeignKey('class_subdivisions.id'))
    to_subdivision_id = db.Column(Integer, ForeignKey('class_subdivisions.id'))
    from_department = db.Column(Text)
    to_department = db.Column(Text)
    reason = db.Column(Text)
    status = db.Column(Text, nullable=False, default='pending', server_default=text("'pending'"))
    requested_by = db.Column(Integer, ForeignKey('admins.id'), nullable=False)
    requested_at = db.Column(Text, nullable=False)
    decided_by = db.Column(Integer, ForeignKey('admins.id'))
    decided_at = db.Column(Text)
    decision_note = db.Column(Text)
