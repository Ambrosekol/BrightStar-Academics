"""Parent portal accounts, their linked children, and feedback threads."""

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint, text

from .auth import _LegacyAttachmentColumns
from .base import db


class ParentAccount(_LegacyAttachmentColumns, db.Model):
    __tablename__ = 'parent_accounts'
    __table_args__ = (
        Index('idx_parent_email', 'email', unique=True,
              sqlite_where=text("email IS NOT NULL AND email <> ''"),
              postgresql_where=text("email IS NOT NULL AND email <> ''")),
        UniqueConstraint('username'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    username = db.Column(Text, nullable=False)
    display_name = db.Column(Text, nullable=False)
    email = db.Column(Text)
    phone = db.Column(Text)
    password_hash = db.Column(Text, nullable=False)
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    password_must_change = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    created_at = db.Column(Text, nullable=False)
    last_login_at = db.Column(Text)
    address = db.Column(Text)
    office_phone = db.Column(Text)
    mobile = db.Column(Text)
    occupation = db.Column(Text)
    relationship = db.Column(Text)
    school_id = db.Column(Integer)


class ParentStudentLink(_LegacyAttachmentColumns, db.Model):
    __tablename__ = 'parent_student_links'
    __table_args__ = (
        Index('idx_parent_links_student', 'student_id', 'active'),
        Index('idx_parent_links_parent', 'parent_id', 'active'),
        UniqueConstraint('parent_id', 'student_id'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    parent_id = db.Column(Integer, ForeignKey('parent_accounts.id', ondelete='CASCADE'), nullable=False)
    student_id = db.Column(Integer, ForeignKey('students.id', ondelete='CASCADE'), nullable=False)
    relationship = db.Column(Text)
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    created_at = db.Column(Text, nullable=False)
    created_by = db.Column(Integer, ForeignKey('admins.id'))
    school_id = db.Column(Integer)


class ParentFeedback(_LegacyAttachmentColumns, db.Model):
    __tablename__ = 'parent_feedback'
    __table_args__ = (
        Index('idx_parent_feedback_parent', 'parent_id', 'created_at'),
        Index('idx_parent_feedback_status', 'status', 'created_at'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    parent_id = db.Column(Integer, ForeignKey('parent_accounts.id', ondelete='CASCADE'), nullable=False)
    student_id = db.Column(Integer, ForeignKey('students.id', ondelete='SET NULL'))
    subject = db.Column(Text, nullable=False)
    body = db.Column(Text, nullable=False)
    status = db.Column(Text, nullable=False, default='open', server_default=text("'open'"))
    assigned_admin_id = db.Column(Integer, ForeignKey('admins.id'))
    created_at = db.Column(Text, nullable=False)
    updated_at = db.Column(Text, nullable=False)
    closed_at = db.Column(Text)
    file_type = db.Column(Text)


class ParentFeedbackReply(_LegacyAttachmentColumns, db.Model):
    __tablename__ = 'parent_feedback_replies'
    __table_args__ = (Index('idx_parent_feedback_replies_feedback', 'feedback_id', 'created_at'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    feedback_id = db.Column(Integer, ForeignKey('parent_feedback.id', ondelete='CASCADE'), nullable=False)
    # NULL means this message was written by the parent who owns the thread
    # (ParentFeedback.parent_id) rather than by staff - each thread belongs
    # to exactly one parent, so that's unambiguous without a separate
    # sender-type column.
    admin_id = db.Column(Integer, ForeignKey('admins.id'))
    body = db.Column(Text, nullable=False)
    created_at = db.Column(Text, nullable=False)
    file_type = db.Column(Text)
