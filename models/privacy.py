"""Data protection and the outside world: consents and their history, families' requests to see or erase their data,
data exports, and the school's API keys and webhooks (core/privacy.py, core/data_export.py, core/api_keys.py,
core/webhooks.py). See models/__init__.py for the column conventions."""

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint, text

from .base import db


class ConsentRecord(db.Model):
    """One decision about one purpose, never changed afterwards: the latest row for a student (or, for the privacy
    notice, a parent) and a purpose is the consent in force, and the rows before it are its history.

    ``source`` is 'parent' (given or withdrawn in the parent portal) or 'staff' (recorded from a signed paper form,
    with who recorded it)."""

    __tablename__ = 'consent_records'
    __table_args__ = (Index('idx_consent_student', 'student_id', 'purpose', 'id'),
                      Index('idx_consent_parent', 'parent_id', 'purpose', 'id'))

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    purpose = db.Column(Text, nullable=False)
    student_id = db.Column(Integer, ForeignKey('students.id', ondelete='CASCADE'))
    parent_id = db.Column(Integer, ForeignKey('parent_accounts.id', ondelete='SET NULL'))
    granted = db.Column(Integer, nullable=False)
    notice_version = db.Column(Text)
    source = db.Column(Text, nullable=False)
    recorded_by = db.Column(Integer, ForeignKey('admins.id'))
    note = db.Column(Text)
    ip_address = db.Column(Text)
    created_at = db.Column(Text, nullable=False)


class DataRequest(db.Model):
    """A family's request to receive a copy of a child's data ('export') or to have it erased ('erasure'). The school
    decides within ``due_at`` (thirty days, the NDPA's period); an export, once approved, is built in the background
    and the family downloads it from the portal until it expires."""

    __tablename__ = 'data_requests'
    __table_args__ = (Index('idx_data_requests_status', 'status', 'id'), Index('idx_data_requests_parent', 'parent_id', 'id'))

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    kind = db.Column(Text, nullable=False)
    student_id = db.Column(Integer, ForeignKey('students.id', ondelete='SET NULL'))
    parent_id = db.Column(Integer, ForeignKey('parent_accounts.id', ondelete='SET NULL'))
    status = db.Column(Text, nullable=False, default='pending', server_default=text("'pending'"))
    reason = db.Column(Text)
    requested_at = db.Column(Text, nullable=False)
    due_at = db.Column(Text, nullable=False)
    decided_by = db.Column(Integer, ForeignKey('admins.id'))
    decided_at = db.Column(Text)
    decision_note = db.Column(Text)
    completed_at = db.Column(Text)
    export_id = db.Column(Integer, ForeignKey('data_exports.id', ondelete='SET NULL'))


class DataExport(db.Model):
    """A file of data built in the background: the whole school's ('school') or one child's for a request ('family').
    The file is kept outside the public uploads and removed when it expires."""

    __tablename__ = 'data_exports'
    __table_args__ = (Index('idx_data_exports_kind', 'kind', 'id'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    kind = db.Column(Text, nullable=False)
    student_id = db.Column(Integer, ForeignKey('students.id', ondelete='SET NULL'))
    status = db.Column(Text, nullable=False, default='queued', server_default=text("'queued'"))
    file_key = db.Column(Text)
    size_bytes = db.Column(Integer)
    summary_json = db.Column(Text)
    error = db.Column(Text)
    requested_by = db.Column(Integer, ForeignKey('admins.id'))
    created_at = db.Column(Text, nullable=False)
    started_at = db.Column(Text)
    heartbeat_at = db.Column(Text)
    completed_at = db.Column(Text)
    expires_at = db.Column(Text)
    downloads = db.Column(Integer, nullable=False, default=0, server_default=text('0'))


class ApiKey(db.Model):
    """A key the School Admin created for the school's own developers or a partner: read-only, limited to the areas
    (scopes) chosen. Only a hash of the secret is kept; the key is shown once, when it is made."""

    __tablename__ = 'api_keys'
    __table_args__ = (UniqueConstraint('prefix'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    name = db.Column(Text, nullable=False)
    prefix = db.Column(Text, nullable=False)
    secret_hash = db.Column(Text, nullable=False)
    scopes = db.Column(Text, nullable=False)
    created_by = db.Column(Integer, ForeignKey('admins.id'))
    created_at = db.Column(Text, nullable=False)
    last_used_at = db.Column(Text)
    last_used_ip = db.Column(Text)
    revoked_at = db.Column(Text)
    revoked_by = db.Column(Integer, ForeignKey('admins.id'))


class WebhookEndpoint(db.Model):
    """An address the school's system listens on, the events it wants, and the secret its notices are signed with
    (kept encrypted: it must be read back to sign)."""

    __tablename__ = 'webhook_endpoints'

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    url = db.Column(Text, nullable=False)
    description = db.Column(Text)
    events = db.Column(Text, nullable=False)
    secret_encrypted = db.Column(Text, nullable=False)
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    failures_in_a_row = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    disabled_reason = db.Column(Text)
    created_by = db.Column(Integer, ForeignKey('admins.id'))
    created_at = db.Column(Text, nullable=False)
    updated_at = db.Column(Text)


class WebhookDelivery(db.Model):
    """One event sent (or to be sent) to one endpoint, with how it went. Kept for thirty days."""

    __tablename__ = 'webhook_deliveries'
    __table_args__ = (Index('idx_webhook_deliveries_endpoint', 'endpoint_id', 'id'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    endpoint_id = db.Column(Integer, ForeignKey('webhook_endpoints.id', ondelete='CASCADE'), nullable=False)
    event_id = db.Column(Text, nullable=False)
    event = db.Column(Text, nullable=False)
    payload_json = db.Column(Text, nullable=False)
    status = db.Column(Text, nullable=False, default='pending', server_default=text("'pending'"))
    attempts = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    response_code = db.Column(Integer)
    error = db.Column(Text)
    created_at = db.Column(Text, nullable=False)
    delivered_at = db.Column(Text)
