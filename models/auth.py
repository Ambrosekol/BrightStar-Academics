"""Administrator identity, RBAC, audit trail and internal messaging."""

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint, text

from .base import db


class _LegacyAttachmentColumns:
    """Columns a blanket migration added to every ``admin*`` table.

    Only :class:`AdminMessage` ever reads or writes an attachment; it needs the
    pair plus ``attachment_type``/``attachment_name``. The copies on the other
    administrator tables are never referenced by the application and exist only
    because the migration that introduced message attachments matched tables by
    name prefix.

    They are modelled anyway so that ``create_all()`` on a fresh database yields
    the same shape as a database upgraded through the migration chain. Dropping
    them here would make new and upgraded installations diverge.
    """

    attachment_path = db.Column(Text)
    file_type = db.Column(Text)


class AdminType(_LegacyAttachmentColumns, db.Model):
    __tablename__ = 'admin_types'

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    name = db.Column(Text, nullable=False, unique=True)
    description = db.Column(Text)
    is_system = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    created_at = db.Column(Text, nullable=False)


class Permission(db.Model):
    __tablename__ = 'permissions'

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    code = db.Column(Text, nullable=False, unique=True)
    name = db.Column(Text, nullable=False)
    module = db.Column(Text, nullable=False)
    description = db.Column(Text)


class Admin(_LegacyAttachmentColumns, db.Model):
    __tablename__ = 'admins'

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    username = db.Column(Text, nullable=False, unique=True)
    display_name = db.Column(Text, nullable=False)
    password_hash = db.Column(Text, nullable=False)
    admin_type_id = db.Column(Integer, ForeignKey('admin_types.id'), nullable=False)
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    created_at = db.Column(Text, nullable=False)
    created_by = db.Column(Integer, ForeignKey('admins.id'))
    last_login_at = db.Column(Text)
    email = db.Column(Text)
    phone = db.Column(Text)
    whatsapp = db.Column(Text)
    photo_path = db.Column(Text)
    # The staff member's own signature (a file under uploads/signatures/), drawn or uploaded by them.
    # It goes on a report card beside the comment they wrote.
    signature_path = db.Column(Text)
    password_must_change = db.Column(Integer, nullable=False, default=0, server_default=text('0'))

    admin_type = db.relationship('AdminType', lazy='joined')

    # The pre-ORM code selected these two columns via a join onto admin_types and
    # consumers (routes, decorators and templates) read them straight off the row.
    # Exposing them as properties keeps every one of those call sites working.
    @property
    def admin_type_name(self):
        return self.admin_type.name if self.admin_type else None

    @property
    def admin_type_system(self):
        return self.admin_type.is_system if self.admin_type else 0


class AdminTypePermission(_LegacyAttachmentColumns, db.Model):
    __tablename__ = 'admin_type_permissions'

    admin_type_id = db.Column(Integer, ForeignKey('admin_types.id'), primary_key=True, nullable=False)
    permission_id = db.Column(Integer, ForeignKey('permissions.id'), primary_key=True, nullable=False)
    granted_at = db.Column(Text, nullable=False)


class AdminPermission(_LegacyAttachmentColumns, db.Model):
    """Per-administrator permission override, independent of their role."""

    __tablename__ = 'admin_permissions'
    __table_args__ = (Index('idx_admin_permissions_admin', 'admin_id'),)

    admin_id = db.Column(Integer, ForeignKey('admins.id'), primary_key=True, nullable=False)
    permission_id = db.Column(Integer, ForeignKey('permissions.id'), primary_key=True, nullable=False)
    granted_at = db.Column(Text, nullable=False)
    granted_by = db.Column(Integer, ForeignKey('admins.id'))


class AdminRoleAssignment(_LegacyAttachmentColumns, db.Model):
    __tablename__ = 'admin_role_assignments'
    __table_args__ = (Index('idx_admin_role_assignments_admin', 'admin_id'),)

    admin_id = db.Column(Integer, ForeignKey('admins.id', ondelete='CASCADE'), primary_key=True, nullable=False)
    admin_type_id = db.Column(Integer, ForeignKey('admin_types.id', ondelete='CASCADE'), primary_key=True, nullable=False)
    assigned_at = db.Column(Text, nullable=False)
    assigned_by = db.Column(Integer, ForeignKey('admins.id'))


class AdminScope(_LegacyAttachmentColumns, db.Model):
    __tablename__ = 'admin_scopes'
    __table_args__ = (
        UniqueConstraint('admin_id', 'scope_type', 'scope_value'),
        Index('idx_admin_scopes_lookup', 'admin_id', 'scope_type', 'scope_value'),
        Index('idx_admin_scopes_admin', 'admin_id'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    admin_id = db.Column(Integer, ForeignKey('admins.id'), nullable=False)
    scope_type = db.Column(Text, nullable=False)
    scope_value = db.Column(Text, nullable=False)
    created_at = db.Column(Text, nullable=False)
    granted_by = db.Column(Integer, ForeignKey('admins.id'))


class AuditLog(db.Model):
    __tablename__ = 'audit_logs'
    __table_args__ = (
        Index('idx_audit_logs_admin', 'admin_id', 'created_at'),
        Index('idx_audit_logs_created', 'created_at'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    admin_id = db.Column(Integer, ForeignKey('admins.id'))
    username_snapshot = db.Column(Text)
    action = db.Column(Text, nullable=False)
    module = db.Column(Text, nullable=False)
    target_type = db.Column(Text)
    target_id = db.Column(Text)
    details = db.Column(Text)
    ip_address = db.Column(Text)
    user_agent = db.Column(Text)
    success = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    created_at = db.Column(Text, nullable=False)


class AdminNotification(_LegacyAttachmentColumns, db.Model):
    __tablename__ = 'admin_notifications'
    __table_args__ = (
        Index('idx_admin_notifications_admin', 'admin_id', 'read_at', 'created_at'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    admin_id = db.Column(Integer, ForeignKey('admins.id'), nullable=False)
    title = db.Column(Text, nullable=False)
    message = db.Column(Text, nullable=False)
    severity = db.Column(Text, nullable=False, default='info', server_default=text("'info'"))
    action_url = db.Column(Text)
    read_at = db.Column(Text)
    created_at = db.Column(Text, nullable=False)
    actor_admin_id = db.Column(Integer)
    actor_username_snapshot = db.Column(Text)
    actor_display_name_snapshot = db.Column(Text)


class AdminControlItem(_LegacyAttachmentColumns, db.Model):
    __tablename__ = 'admin_control_items'
    __table_args__ = (Index('idx_admin_controls_status', 'status', 'created_at'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    title = db.Column(Text, nullable=False)
    description = db.Column(Text, nullable=False)
    category = db.Column(Text, nullable=False)
    target_type = db.Column(Text)
    target_id = db.Column(Text)
    status = db.Column(Text, nullable=False, default='open', server_default=text("'open'"))
    requested_by = db.Column(Integer, ForeignKey('admins.id'))
    created_at = db.Column(Text, nullable=False)
    resolved_by = db.Column(Integer, ForeignKey('admins.id'))
    resolved_at = db.Column(Text)


class AdminResourceLock(_LegacyAttachmentColumns, db.Model):
    __tablename__ = 'admin_resource_locks'
    __table_args__ = (
        UniqueConstraint('resource_type', 'resource_id'),
        Index('idx_admin_locks_resource', 'resource_type', 'resource_id'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    resource_type = db.Column(Text, nullable=False)
    resource_id = db.Column(Text, nullable=False)
    reason = db.Column(Text)
    locked_by = db.Column(Integer, ForeignKey('admins.id'), nullable=False)
    locked_at = db.Column(Text, nullable=False)
    unlocked_by = db.Column(Integer, ForeignKey('admins.id'))
    unlocked_at = db.Column(Text)


class AdminMessage(_LegacyAttachmentColumns, db.Model):
    __tablename__ = 'admin_messages'
    __table_args__ = (
        Index('idx_admin_messages_thread', 'sender_admin_id', 'recipient_admin_id', 'sent_at'),
        Index('idx_admin_messages_recipient', 'recipient_admin_id', 'read_at', 'sent_at'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    sender_admin_id = db.Column(Integer, ForeignKey('admins.id', ondelete='CASCADE'), nullable=False)
    recipient_admin_id = db.Column(Integer, ForeignKey('admins.id', ondelete='CASCADE'), nullable=False)
    body = db.Column(Text, nullable=False)
    sent_at = db.Column(Text, nullable=False)
    read_at = db.Column(Text)
    attachment_type = db.Column(Text)
    attachment_name = db.Column(Text)

    sender = db.relationship('Admin', foreign_keys=[sender_admin_id], lazy='joined')
    recipient = db.relationship('Admin', foreign_keys=[recipient_admin_id], lazy='select')


class SecurityEvent(db.Model):
    __tablename__ = 'security_events'
    __table_args__ = (Index('idx_security_events_type_time', 'event_type', 'created_at'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    event_type = db.Column(Text, nullable=False)
    identifier_hash = db.Column(Text)
    ip_address = db.Column(Text)
    user_agent = db.Column(Text)
    details = db.Column(Text)
    created_at = db.Column(Text, nullable=False)
