"""Live presence tracking, in-app notifications, and password reset tokens."""

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint, text

from .base import db


class PresenceSession(db.Model):
    __tablename__ = 'presence_sessions'
    __table_args__ = (
        Index('idx_presence_account', 'account_type', 'account_id', 'last_seen'),
        Index('idx_presence_online', 'account_type', 'active', 'last_seen'),
        UniqueConstraint('session_key_hash'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    account_type = db.Column(Text, nullable=False)
    account_id = db.Column(Integer, nullable=False)
    session_key_hash = db.Column(Text, nullable=False)
    first_seen = db.Column(Text, nullable=False)
    last_seen = db.Column(Text, nullable=False)
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    user_agent = db.Column(Text)


class SchoolNotification(db.Model):
    __tablename__ = 'school_notifications'
    __table_args__ = (Index('idx_school_notifications_recipient', 'recipient_type', 'recipient_id', 'read_at', 'created_at'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    recipient_type = db.Column(Text, nullable=False)
    recipient_id = db.Column(Integer, nullable=False)
    student_id = db.Column(Integer, ForeignKey('students.id', ondelete='CASCADE'))
    category = db.Column(Text, nullable=False)
    title = db.Column(Text, nullable=False)
    message = db.Column(Text, nullable=False)
    action_url = db.Column(Text)
    created_at = db.Column(Text, nullable=False)
    read_at = db.Column(Text)
    created_by = db.Column(Integer, ForeignKey('admins.id'))


class PasswordResetToken(db.Model):
    __tablename__ = 'password_reset_tokens'
    __table_args__ = (
        Index('idx_password_reset_lookup', 'account_type', 'account_id', 'expires_at', 'used_at'),
        UniqueConstraint('token_hash'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    account_type = db.Column(Text, nullable=False)
    account_id = db.Column(Integer, nullable=False)
    token_hash = db.Column(Text, nullable=False)
    expires_at = db.Column(Text, nullable=False)
    used_at = db.Column(Text)
    created_at = db.Column(Text, nullable=False)
    requested_ip = db.Column(Text)
