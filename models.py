"""SQLAlchemy models for Crainbow CBT.

These models mirror the schema that already exists in ``cbt.db`` exactly, as it
was accreted by ``init_db()``, ``init_admin_security()`` and the ``migrations``
package. They are deliberately NOT an idealised redesign:

* Timestamps stay ``Text``. The application stores and compares ISO-8601 strings
  produced by ``datetime.now(timezone.utc).isoformat()``. Switching these to
  ``DateTime`` would silently change the stored format and break every existing
  row and every string comparison in the codebase.
* Boolean-ish flags stay ``Integer`` (0/1), matching the existing columns and the
  ``active=1`` style predicates used throughout the app.
* Column order, nullability, defaults and constraints reproduce the live schema
  so that ``metadata.create_all()`` on a fresh database yields the same shape.

Instances support ``sqlite3.Row``-style access (``row['col']``, ``'col' in
row.keys()``) as well as normal attribute access, so existing templates and
helper code that index rows by name keep working unchanged.
"""

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import (
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base that keeps ``sqlite3.Row``-compatible access.

    The application and its Jinja templates were written against ``sqlite3.Row``
    and use ``row['column']`` and ``'column' in row.keys()`` in a number of
    places. Supporting the mapping protocol here means the ORM migration does
    not have to rewrite every template and every defensive column check.
    """

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return getattr(self, key)
            except AttributeError as exc:  # pragma: no cover - defensive
                raise KeyError(key) from exc
        raise TypeError('Model rows are indexed by column name.')

    def keys(self):
        return [c.key for c in self.__mapper__.column_attrs]

    def __contains__(self, key):
        return key in self.keys()

    def get(self, key, default=None):
        return getattr(self, key, default)


db = SQLAlchemy(model_class=Base)


# ---------------------------------------------------------------- entrance exam


class Examination(db.Model):
    __tablename__ = 'examinations'

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    bank_id = db.Column(Text, nullable=False, unique=True)
    name = db.Column(Text, nullable=False)
    duration_seconds = db.Column(Integer, nullable=False)
    version = db.Column(Text, nullable=False)
    question_count = db.Column(Integer, nullable=False)
    created_at = db.Column(Text, nullable=False)
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))


class Attempt(db.Model):
    __tablename__ = 'attempts'
    __table_args__ = (
        Index('idx_attempts_bank_status', 'bank_id', 'status'),
        Index('idx_attempts_candidate', 'candidate_id', 'status'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    candidate = db.Column(Text, nullable=False)
    exam_id = db.Column(Integer, nullable=False)
    bank_id = db.Column(Text, nullable=False)
    started_at = db.Column(Text, nullable=False)
    expires_at = db.Column(Text, nullable=False)
    submitted_at = db.Column(Text)
    score = db.Column(Integer)
    max_score = db.Column(Integer)
    percentage = db.Column(Float)
    status = db.Column(Text, nullable=False)
    candidate_id = db.Column(Integer)


class Answer(db.Model):
    __tablename__ = 'answers'
    __table_args__ = (Index('idx_answers_attempt', 'attempt_id'),)

    attempt_id = db.Column(Integer, primary_key=True, nullable=False)
    question_id = db.Column(Integer, primary_key=True, nullable=False)
    option_index = db.Column(Integer)
    answered_at = db.Column(Text, nullable=False)


class AttemptQuestion(db.Model):
    """Frozen per-attempt question snapshot that guarantees result integrity."""

    __tablename__ = 'attempt_questions'
    __table_args__ = (
        UniqueConstraint('attempt_id', 'question_id'),
        UniqueConstraint('attempt_id', 'question_order'),
        Index('idx_attempt_questions_attempt', 'attempt_id', 'question_order'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    attempt_id = db.Column(Integer, ForeignKey('attempts.id', ondelete='CASCADE'), nullable=False)
    question_id = db.Column(Integer, nullable=False)
    question_order = db.Column(Integer, nullable=False)
    question_text = db.Column(Text, nullable=False)
    options_json = db.Column(Text, nullable=False)
    instruction = db.Column(Text)
    image_path = db.Column(Text)
    correct_option = db.Column(Integer, nullable=False)
    points = db.Column(Integer, nullable=False, default=1, server_default=text('1'))


class RetakeGrant(db.Model):
    __tablename__ = 'retake_grants'
    __table_args__ = (
        Index('idx_retake_grants_lookup', 'candidate_key', 'bank_id', 'used_at'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    candidate_key = db.Column(Text, nullable=False)
    candidate_name = db.Column(Text, nullable=False)
    candidate_id = db.Column(Integer)
    bank_id = db.Column(Text, nullable=False)
    granted_at = db.Column(Text, nullable=False)
    granted_by = db.Column(Text, nullable=False, default='admin', server_default=text("'admin'"))
    used_at = db.Column(Text)


class Candidate(db.Model):
    __tablename__ = 'candidates'

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    candidate_code = db.Column(Text, nullable=False, unique=True)
    candidate_name = db.Column(Text, nullable=False)
    target_class = db.Column(Text, nullable=False)
    password_hash = db.Column(Text, nullable=False)
    created_at = db.Column(Text, nullable=False)
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    school_attended = db.Column(Text)
    parent_guardian_name = db.Column(Text)
    parent_guardian_relationship = db.Column(Text)
    primary_mobile = db.Column(Text)
    alternative_mobile = db.Column(Text)
    parent_guardian_email = db.Column(Text)
    photo_path = db.Column(Text)


class CandidatePaper(db.Model):
    __tablename__ = 'candidate_papers'
    __table_args__ = (
        UniqueConstraint('candidate_id', 'slot'),
        UniqueConstraint('candidate_id', 'bank_id'),
        Index('idx_candidate_papers_candidate', 'candidate_id'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    candidate_id = db.Column(Integer, ForeignKey('candidates.id'), nullable=False)
    slot = db.Column(Integer, nullable=False)
    bank_id = db.Column(Text, nullable=False)
    assigned_at = db.Column(Text, nullable=False)
    config_id = db.Column(Integer, ForeignKey('entrance_bank_configs.id'))


# ------------------------------------------------------- administrator security


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


# --------------------------------------------------------------- school portal


class AcademicSession(db.Model):
    __tablename__ = 'academic_sessions'

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    name = db.Column(Text, nullable=False, unique=True)
    start_date = db.Column(Text)
    end_date = db.Column(Text)
    is_current = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    created_at = db.Column(Text, nullable=False)
    result_release_at = db.Column(Text)
    school_id = db.Column(Integer, ForeignKey('schools.id'))


class SchoolClass(db.Model):
    __tablename__ = 'school_classes'

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    name = db.Column(Text, nullable=False, unique=True)
    stage = db.Column(Text, nullable=False)
    level_order = db.Column(Integer, nullable=False)
    optional = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    created_at = db.Column(Text, nullable=False)
    school_id = db.Column(Integer, ForeignKey('schools.id'))


class Student(db.Model):
    __tablename__ = 'students'
    __table_args__ = (
        Index(
            'idx_students_login_username',
            'login_username',
            unique=True,
            sqlite_where=text('login_username IS NOT NULL'),
        ),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    admission_no = db.Column(Text, nullable=False, unique=True)
    first_name = db.Column(Text, nullable=False)
    middle_name = db.Column(Text)
    last_name = db.Column(Text, nullable=False)
    gender = db.Column(Text)
    date_of_birth = db.Column(Text)
    guardian_name = db.Column(Text)
    guardian_phone = db.Column(Text)
    guardian_email = db.Column(Text)
    photo_path = db.Column(Text)
    created_at = db.Column(Text, nullable=False)
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    login_username = db.Column(Text)
    login_password_hash = db.Column(Text)
    account_active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    password_must_change = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    last_login_at = db.Column(Text)

    # Admissions profile captured on the full student registration form.
    blood_group = db.Column(Text)
    genotype = db.Column(Text)
    state_of_origin = db.Column(Text)
    previous_school = db.Column(Text)
    reason_for_leaving = db.Column(Text)
    religion = db.Column(Text)
    denomination = db.Column(Text)
    convulsion_history = db.Column(Text)
    convulsion_frequency = db.Column(Text)
    convulsion_treatment = db.Column(Text)
    asthma_history = db.Column(Text)
    asthma_frequency = db.Column(Text)
    asthma_treatment = db.Column(Text)
    immunization_status = db.Column(Text)
    food_allergies = db.Column(Text)
    drug_allergies = db.Column(Text)
    other_health_challenges = db.Column(Text)
    disability = db.Column(Text)
    disability_indication = db.Column(Text)
    parent_signature = db.Column(Text)
    parent_signature_date = db.Column(Text)

    # Multi-school tenancy and the generated student-number identity.
    school_id = db.Column(Integer, ForeignKey('schools.id'))
    legacy_student_id = db.Column(Text)
    student_number_source = db.Column(Text, nullable=False, default='existing', server_default=text("'existing'"))
    student_number = db.Column(Text)


class StudentEnrolment(db.Model):
    __tablename__ = 'student_enrolments'
    __table_args__ = (
        UniqueConstraint('student_id', 'session_id'),
        Index('idx_student_enrolments_class', 'class_id', 'session_id', 'active'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    student_id = db.Column(Integer, ForeignKey('students.id'), nullable=False)
    class_id = db.Column(Integer, ForeignKey('school_classes.id'), nullable=False)
    session_id = db.Column(Integer, ForeignKey('academic_sessions.id'), nullable=False)
    enrolled_at = db.Column(Text, nullable=False)
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    school_id = db.Column(Integer, ForeignKey('schools.id'))


class SchoolSubject(db.Model):
    __tablename__ = 'school_subjects'

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    name = db.Column(Text, nullable=False, unique=True)
    code = db.Column(Text)
    created_at = db.Column(Text, nullable=False)
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))


class ClassSubject(db.Model):
    __tablename__ = 'class_subjects'
    __table_args__ = (
        UniqueConstraint('class_id', 'subject_id'),
        Index('idx_class_subjects_class', 'class_id'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    class_id = db.Column(Integer, ForeignKey('school_classes.id'), nullable=False)
    subject_id = db.Column(Integer, ForeignKey('school_subjects.id'), nullable=False)
    locked = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    created_by = db.Column(Integer, ForeignKey('admins.id'))
    created_at = db.Column(Text, nullable=False)
    final_locked = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    final_locked_by = db.Column(Integer, ForeignKey('admins.id'))
    final_locked_at = db.Column(Text)


class SchoolAssignment(db.Model):
    __tablename__ = 'school_assignments'
    __table_args__ = (
        Index('idx_assignments_class_subject', 'class_id', 'subject_id', 'active'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    title = db.Column(Text, nullable=False)
    instructions = db.Column(Text)
    class_id = db.Column(Integer, ForeignKey('school_classes.id'), nullable=False)
    subject_id = db.Column(Integer, ForeignKey('school_subjects.id'), nullable=False)
    due_date = db.Column(Text)
    created_by = db.Column(Integer, ForeignKey('admins.id'), nullable=False)
    created_at = db.Column(Text, nullable=False)
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    assignment_type = db.Column(Text, nullable=False, default='written', server_default=text("'written'"))
    date_given = db.Column(Text)
    timing_mode = db.Column(Text, nullable=False, default='untimed', server_default=text("'untimed'"))
    time_limit_seconds = db.Column(Integer)
    per_question_seconds = db.Column(Integer)
    max_score = db.Column(Float)
    session_id = db.Column(Integer, ForeignKey('academic_sessions.id'))
    term = db.Column(Text)


class AssignmentStudent(db.Model):
    __tablename__ = 'assignment_students'

    assignment_id = db.Column(
        Integer, ForeignKey('school_assignments.id', ondelete='CASCADE'), primary_key=True, nullable=False
    )
    student_id = db.Column(Integer, ForeignKey('students.id'), primary_key=True, nullable=False)
    status = db.Column(Text, nullable=False, default='undone', server_default=text("'undone'"))
    score = db.Column(Float)
    max_score = db.Column(Float)
    remark = db.Column(Text)
    started_at = db.Column(Text)
    submitted_at = db.Column(Text)
    graded_at = db.Column(Text)
    graded_by = db.Column(Integer, ForeignKey('admins.id'))


class SchoolAssessment(db.Model):
    __tablename__ = 'school_assessments'
    __table_args__ = (
        Index('idx_assessments_type_class', 'assessment_type', 'class_id', 'subject_id'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    assessment_type = db.Column(Text, nullable=False)
    title = db.Column(Text, nullable=False)
    instructions = db.Column(Text)
    class_id = db.Column(Integer, ForeignKey('school_classes.id'), nullable=False)
    subject_id = db.Column(Integer, ForeignKey('school_subjects.id'), nullable=False)
    session_id = db.Column(Integer, ForeignKey('academic_sessions.id'))
    duration_minutes = db.Column(Integer, nullable=False, default=30, server_default=text('30'))
    question_count = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    active = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    created_by = db.Column(Integer, ForeignKey('admins.id'), nullable=False)
    created_at = db.Column(Text, nullable=False)
    term = db.Column(Text)


class SchoolQuestion(db.Model):
    __tablename__ = 'school_questions'
    __table_args__ = (
        Index('idx_school_questions_assessment', 'assessment_id', 'sort_order'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    assessment_id = db.Column(
        Integer, ForeignKey('school_assessments.id', ondelete='CASCADE'), nullable=False
    )
    question_text = db.Column(Text, nullable=False)
    instruction = db.Column(Text)
    image_path = db.Column(Text)
    option_a = db.Column(Text, nullable=False)
    option_b = db.Column(Text, nullable=False)
    option_c = db.Column(Text, nullable=False)
    option_d = db.Column(Text, nullable=False)
    correct_option = db.Column(Integer, nullable=False)
    points = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    sort_order = db.Column(Integer, nullable=False, default=1, server_default=text('1'))


class SchoolAssessmentAttempt(db.Model):
    __tablename__ = 'school_assessment_attempts'
    __table_args__ = (
        UniqueConstraint('student_id', 'assessment_id'),
        Index('idx_school_assessment_attempt_student', 'student_id', 'status'),
        Index('idx_school_assessment_attempt_assessment', 'assessment_id', 'status'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    student_id = db.Column(Integer, ForeignKey('students.id', ondelete='CASCADE'), nullable=False)
    assessment_id = db.Column(
        Integer, ForeignKey('school_assessments.id', ondelete='CASCADE'), nullable=False
    )
    started_at = db.Column(Text, nullable=False)
    expires_at = db.Column(Text, nullable=False)
    submitted_at = db.Column(Text)
    status = db.Column(Text, nullable=False, default='active', server_default=text("'active'"))
    score = db.Column(Integer)
    max_score = db.Column(Integer)
    percentage = db.Column(Float)


class SchoolAssessmentAttemptQuestion(db.Model):
    __tablename__ = 'school_assessment_attempt_questions'
    __table_args__ = (
        UniqueConstraint('attempt_id', 'question_id'),
        UniqueConstraint('attempt_id', 'question_order'),
        Index('idx_school_assessment_attempt_questions', 'attempt_id', 'question_order'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    attempt_id = db.Column(
        Integer, ForeignKey('school_assessment_attempts.id', ondelete='CASCADE'), nullable=False
    )
    question_id = db.Column(Integer, nullable=False)
    question_order = db.Column(Integer, nullable=False)
    question_text = db.Column(Text, nullable=False)
    option_a = db.Column(Text)
    option_b = db.Column(Text)
    option_c = db.Column(Text)
    option_d = db.Column(Text)
    instruction = db.Column(Text)
    image_path = db.Column(Text)
    correct_option = db.Column(Integer, nullable=False)
    points = db.Column(Integer, nullable=False, default=1, server_default=text('1'))


class SchoolAssessmentAnswer(db.Model):
    __tablename__ = 'school_assessment_answers'

    attempt_id = db.Column(
        Integer, ForeignKey('school_assessment_attempts.id', ondelete='CASCADE'),
        primary_key=True, nullable=False,
    )
    question_id = db.Column(Integer, primary_key=True, nullable=False)
    option_index = db.Column(Integer)
    answered_at = db.Column(Text, nullable=False)


class SchoolStudentResult(db.Model):
    __tablename__ = 'school_student_results'
    __table_args__ = (Index('idx_school_results_student', 'student_id', 'session_id'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    student_id = db.Column(Integer, ForeignKey('students.id'), nullable=False)
    assessment_id = db.Column(Integer, ForeignKey('school_assessments.id'))
    assignment_id = db.Column(Integer, ForeignKey('school_assignments.id'))
    subject_id = db.Column(Integer, ForeignKey('school_subjects.id'), nullable=False)
    score = db.Column(Float)
    max_score = db.Column(Float)
    term = db.Column(Text)
    session_id = db.Column(Integer, ForeignKey('academic_sessions.id'))
    status = db.Column(Text, nullable=False, default='draft', server_default=text("'draft'"))
    created_at = db.Column(Text, nullable=False)

    # Result workflow: where the mark came from and who moved it through
    # entry -> verification -> approval -> release.
    source_type = db.Column(Text, nullable=False, default='cbt', server_default=text("'cbt'"))
    component_name = db.Column(Text)
    entered_by = db.Column(Integer, ForeignKey('admins.id'))
    verified_by = db.Column(Integer, ForeignKey('admins.id'))
    approved_by = db.Column(Integer, ForeignKey('admins.id'))
    released_at = db.Column(Text)
    override_reason = db.Column(Text)
    updated_at = db.Column(Text)
    updated_by = db.Column(Integer, ForeignKey('admins.id'))



# ------------------------------------------ entrance examination configuration

class EntranceBankConfig(db.Model):
    """Periodised entrance paper configuration: which bank serves which entry
    group and subject, for a given session and term."""

    __tablename__ = 'entrance_bank_configs'
    __table_args__ = (
        Index('idx_entrance_config_practice', 'session_id', 'practice_enabled', 'active'),
        Index('idx_entrance_config_scope', 'entry_group', 'subject', 'session_id', 'term', 'active'),
        UniqueConstraint('bank_id', 'entry_group', 'subject', 'session_id', 'term'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    bank_id = db.Column(Text, nullable=False)
    entry_group = db.Column(Text, nullable=False)
    subject = db.Column(Text, nullable=False)
    session_id = db.Column(Integer, ForeignKey('academic_sessions.id'), nullable=False)
    term = db.Column(Text, nullable=False, default='Full Session', server_default=text("'Full Session'"))
    questions_to_serve = db.Column(Integer, nullable=False)
    marks_per_question = db.Column(Float, nullable=False)
    active = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    practice_enabled = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    created_at = db.Column(Text, nullable=False)
    created_by = db.Column(Integer, ForeignKey('admins.id'))
    updated_at = db.Column(Text, nullable=False)
    updated_by = db.Column(Integer, ForeignKey('admins.id'))


# ------------------------------ school academic work: assignments and projects

class AssignmentQuestion(db.Model):
    __tablename__ = 'assignment_questions'
    __table_args__ = (Index('idx_assignment_questions_assignment', 'assignment_id', 'sort_order'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    assignment_id = db.Column(Integer, ForeignKey('school_assignments.id', ondelete='CASCADE'), nullable=False)
    question_text = db.Column(Text, nullable=False)
    instruction = db.Column(Text)
    image_path = db.Column(Text)
    option_a = db.Column(Text, nullable=False)
    option_b = db.Column(Text, nullable=False)
    option_c = db.Column(Text, nullable=False)
    option_d = db.Column(Text, nullable=False)
    correct_option = db.Column(Integer, nullable=False)
    points = db.Column(Float, nullable=False, default=1, server_default=text('1'))
    sort_order = db.Column(Integer, nullable=False, default=1, server_default=text('1'))

class SchoolAssignmentAttempt(db.Model):
    __tablename__ = 'school_assignment_attempts'
    __table_args__ = (UniqueConstraint('assignment_id', 'student_id'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    assignment_id = db.Column(Integer, ForeignKey('school_assignments.id', ondelete='CASCADE'), nullable=False)
    student_id = db.Column(Integer, ForeignKey('students.id', ondelete='CASCADE'), nullable=False)
    started_at = db.Column(Text, nullable=False)
    expires_at = db.Column(Text)
    submitted_at = db.Column(Text)
    status = db.Column(Text, nullable=False, default='active', server_default=text("'active'"))
    score = db.Column(Float)
    max_score = db.Column(Float)
    percentage = db.Column(Float)

class SchoolAssignmentAttemptQuestion(db.Model):
    """Frozen per-attempt question snapshot for CBT-style assignments."""

    __tablename__ = 'school_assignment_attempt_questions'
    __table_args__ = (
        UniqueConstraint('attempt_id', 'question_order'),
        UniqueConstraint('attempt_id', 'question_id'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    attempt_id = db.Column(Integer, ForeignKey('school_assignment_attempts.id', ondelete='CASCADE'), nullable=False)
    question_id = db.Column(Integer, nullable=False)
    question_order = db.Column(Integer, nullable=False)
    question_text = db.Column(Text, nullable=False)
    option_a = db.Column(Text, nullable=False)
    option_b = db.Column(Text, nullable=False)
    option_c = db.Column(Text, nullable=False)
    option_d = db.Column(Text, nullable=False)
    instruction = db.Column(Text)
    image_path = db.Column(Text)
    correct_option = db.Column(Integer, nullable=False)
    points = db.Column(Float, nullable=False, default=1, server_default=text('1'))

class SchoolAssignmentAnswer(db.Model):
    __tablename__ = 'school_assignment_answers'

    attempt_id = db.Column(Integer, ForeignKey('school_assignment_attempts.id', ondelete='CASCADE'), primary_key=True)
    question_id = db.Column(Integer, primary_key=True)
    option_index = db.Column(Integer)
    answered_at = db.Column(Text, nullable=False)

class SchoolProject(db.Model):
    __tablename__ = 'school_projects'
    __table_args__ = (Index('idx_school_projects_class_subject', 'class_id', 'subject_id', 'active'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    title = db.Column(Text, nullable=False)
    instructions = db.Column(Text)
    class_id = db.Column(Integer, ForeignKey('school_classes.id'), nullable=False)
    subject_id = db.Column(Integer, ForeignKey('school_subjects.id'), nullable=False)
    date_given = db.Column(Text, nullable=False)
    due_date = db.Column(Text)
    max_score = db.Column(Float)
    created_by = db.Column(Integer, ForeignKey('admins.id'), nullable=False)
    created_at = db.Column(Text, nullable=False)
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    session_id = db.Column(Integer, ForeignKey('academic_sessions.id'))
    term = db.Column(Text)

class ProjectStudent(db.Model):
    __tablename__ = 'project_students'
    __table_args__ = (Index('idx_project_students_student', 'student_id', 'status'),)

    project_id = db.Column(Integer, ForeignKey('school_projects.id', ondelete='CASCADE'), primary_key=True)
    student_id = db.Column(Integer, ForeignKey('students.id', ondelete='CASCADE'), primary_key=True)
    status = db.Column(Text, nullable=False, default='not_done', server_default=text("'not_done'"))
    score = db.Column(Float)
    remark = db.Column(Text)
    graded_by = db.Column(Integer, ForeignKey('admins.id'))
    graded_at = db.Column(Text)


# ---------------------------------------------------- end-of-session promotion

class SchoolClassProgression(db.Model):
    """The default next class for each class, used to seed a promotion run."""

    __tablename__ = 'school_class_progressions'
    __table_args__ = (
        Index('idx_school_class_progressions_to_active', 'to_class_id', 'active'),
        Index('idx_school_class_progressions_from_active', 'from_class_id', 'active'),
        Index('idx_school_class_progressions_to', 'to_class_id'),
        Index('uq_school_class_progression_from_active', 'from_class_id', unique=True),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    from_class_id = db.Column(Integer, ForeignKey('school_classes.id'), nullable=False)
    to_class_id = db.Column(Integer, ForeignKey('school_classes.id'))
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    created_by = db.Column(Integer)
    created_at = db.Column(Text, nullable=False)
    updated_at = db.Column(Text)
    notes = db.Column(Text)
    updated_by = db.Column(Integer)

class AcademicPromotionRun(db.Model):
    """One end-of-session promotion exercise, from draft through to commit."""

    __tablename__ = 'academic_promotion_runs'
    __table_args__ = (
        Index('idx_promotion_runs_sessions_status', 'from_session_id', 'to_session_id', 'status'),
        Index('idx_promotion_runs_status', 'status'),
        Index('idx_promotion_runs_sessions', 'from_session_id', 'to_session_id'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    from_session_id = db.Column(Integer, ForeignKey('academic_sessions.id'), nullable=False)
    to_session_id = db.Column(Integer, ForeignKey('academic_sessions.id'), nullable=False)
    status = db.Column(Text, nullable=False, default='draft', server_default=text("'draft'"))
    created_by = db.Column(Integer)
    created_at = db.Column(Text, nullable=False)
    approved_by = db.Column(Integer)
    approved_at = db.Column(Text)
    committed_by = db.Column(Integer)
    committed_at = db.Column(Text)
    cancelled_by = db.Column(Integer)
    cancelled_at = db.Column(Text)
    notes = db.Column(Text)

class AcademicPromotionItem(db.Model):
    """A single student's proposed and final placement within a promotion run."""

    __tablename__ = 'academic_promotion_items'
    __table_args__ = (
        Index('idx_promotion_items_run_decision', 'run_id', 'decision'),
        Index('idx_promotion_items_student', 'student_id'),
        Index('idx_promotion_items_run', 'run_id'),
        Index('uq_promotion_item_student_run', 'run_id', 'student_id', unique=True),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    run_id = db.Column(Integer, ForeignKey('academic_promotion_runs.id'), nullable=False)
    student_id = db.Column(Integer, ForeignKey('students.id'), nullable=False)
    source_enrolment_id = db.Column(Integer, ForeignKey('student_enrolments.id'))
    source_class_id = db.Column(Integer, ForeignKey('school_classes.id'))
    proposed_class_id = db.Column(Integer, ForeignKey('school_classes.id'))
    final_class_id = db.Column(Integer, ForeignKey('school_classes.id'))
    action = db.Column(Text, nullable=False, default='promote', server_default=text("'promote'"))
    reason = db.Column(Text)
    committed_enrolment_id = db.Column(Integer, ForeignKey('student_enrolments.id'))
    created_at = db.Column(Text, nullable=False)
    updated_at = db.Column(Text)
    decision = db.Column(Text, default='promote', server_default=text("'promote'"))


# ------------------------------------------------------------- result workflow

class ResultWorkflowEvent(db.Model):
    """Audit trail of a result moving between draft, verified, approved and released."""

    __tablename__ = 'result_workflow_events'
    __table_args__ = (Index('idx_result_workflow_result', 'result_id', 'created_at'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    result_id = db.Column(Integer, ForeignKey('school_student_results.id', ondelete='CASCADE'), nullable=False)
    from_status = db.Column(Text)
    to_status = db.Column(Text, nullable=False)
    actor_admin_id = db.Column(Integer, ForeignKey('admins.id'))
    reason = db.Column(Text)
    created_at = db.Column(Text, nullable=False)


# ---------------------------------------- finance: fees, payments and receipts

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


# --------------------------------------------------------------------- library

class LibraryBook(db.Model):
    __tablename__ = 'library_books'

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    isbn = db.Column(Text)
    title = db.Column(Text, nullable=False)
    author = db.Column(Text)
    publisher = db.Column(Text)
    publication_year = db.Column(Integer)
    category = db.Column(Text)
    shelf = db.Column(Text)
    total_copies = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    available_copies = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    created_at = db.Column(Text, nullable=False)
    created_by = db.Column(Integer, ForeignKey('admins.id'))

class LibraryLoan(db.Model):
    __tablename__ = 'library_loans'
    __table_args__ = (
        Index('idx_library_loans_book', 'book_id', 'status'),
        Index('idx_library_loans_member', 'member_type', 'member_id', 'status'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    book_id = db.Column(Integer, ForeignKey('library_books.id'), nullable=False)
    member_type = db.Column(Text, nullable=False)
    member_id = db.Column(Integer, nullable=False)
    borrowed_at = db.Column(Text, nullable=False)
    due_at = db.Column(Text)
    returned_at = db.Column(Text)
    status = db.Column(Text, nullable=False, server_default=text('"borrowed"'))
    notes = db.Column(Text)
    issued_by = db.Column(Integer, ForeignKey('admins.id'))
    received_by = db.Column(Integer, ForeignKey('admins.id'))


# --------------------------------------------------------------- parent portal

class ParentAccount(_LegacyAttachmentColumns, db.Model):
    __tablename__ = 'parent_accounts'
    __table_args__ = (
        Index('idx_parent_email', 'email', unique=True, sqlite_where=text("email IS NOT NULL AND email <> ''")),
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


# -------------------------------------------------------------- public website

class SchoolPublicPage(db.Model):
    __tablename__ = 'school_public_pages'
    __table_args__ = (UniqueConstraint('slug'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    slug = db.Column(Text, nullable=False)
    title = db.Column(Text, nullable=False)
    content = db.Column(Text)
    published = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    updated_at = db.Column(Text, nullable=False)
    updated_by = db.Column(Integer, ForeignKey('admins.id'))

class SchoolPublicNews(db.Model):
    __tablename__ = 'school_public_news'
    __table_args__ = (
        Index('idx_school_public_news_published', 'published', 'published_at'),
        UniqueConstraint('slug'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    slug = db.Column(Text, nullable=False)
    title = db.Column(Text, nullable=False)
    excerpt = db.Column(Text)
    body = db.Column(Text)
    published = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    published_at = db.Column(Text)
    created_at = db.Column(Text, nullable=False)
    updated_at = db.Column(Text, nullable=False)
    created_by = db.Column(Integer, ForeignKey('admins.id'))
    updated_by = db.Column(Integer, ForeignKey('admins.id'))
    image_url = db.Column(Text)

class SchoolPublicSetting(db.Model):
    __tablename__ = 'school_public_settings'
    __table_args__ = (UniqueConstraint('setting_key'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    setting_key = db.Column(Text, nullable=False)
    setting_value = db.Column(Text)
    updated_at = db.Column(Text, nullable=False)
    updated_by = db.Column(Integer, ForeignKey('admins.id'))

class SchoolPublicEnquiry(db.Model):
    """Contact-form submission from the public website."""

    __tablename__ = 'school_public_enquiries'
    __table_args__ = (Index('idx_school_public_enquiries_status', 'status', 'created_at'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    name = db.Column(Text, nullable=False)
    email = db.Column(Text)
    phone = db.Column(Text)
    subject = db.Column(Text)
    message = db.Column(Text, nullable=False)
    status = db.Column(Text, nullable=False, default='new', server_default=text("'new'"))
    created_at = db.Column(Text, nullable=False)
    handled_by = db.Column(Integer, ForeignKey('admins.id'))
    handled_at = db.Column(Text)


# ------------------------------ school tenancy, settings and student numbering

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
        Index('uq_student_number_allocations_school_number', 'school_id', 'student_number', unique=True, sqlite_where=text("TRIM(student_number) <> ''")),
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


# ------------------------------------ admissions profile and enrolment history

class StudentAdmissionProfile(db.Model):
    __tablename__ = 'student_admission_profiles'
    __table_args__ = (
        Index('idx_student_admission_profiles_student', 'student_id'),
        UniqueConstraint('student_id'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    student_id = db.Column(Integer, ForeignKey('students.id'), nullable=False)
    previous_school = db.Column(Text)
    reason_for_leaving = db.Column(Text)
    religion = db.Column(Text)
    denomination = db.Column(Text)
    blood_group = db.Column(Text)
    genotype = db.Column(Text)
    convulsion_history = db.Column(Text)
    asthma_history = db.Column(Text)
    medical_frequency = db.Column(Text)
    medical_treatment = db.Column(Text)
    immunization = db.Column(Text)
    food_allergies = db.Column(Text)
    drug_allergies = db.Column(Text)
    other_health_challenges = db.Column(Text)
    disability = db.Column(Text)
    disability_indication = db.Column(Text)
    parent_signature = db.Column(Text)
    parent_signature_date = db.Column(Text)
    updated_at = db.Column(Text)
    updated_by = db.Column(Integer)

class StudentAdmissionContact(db.Model):
    __tablename__ = 'student_admission_contacts'
    __table_args__ = (
        Index('idx_student_admission_contacts_parent', 'parent_id'),
        Index('idx_student_admission_contacts_student', 'student_id'),
        UniqueConstraint('student_id', 'role'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    student_id = db.Column(Integer, ForeignKey('students.id'), nullable=False)
    parent_id = db.Column(Integer, ForeignKey('parent_accounts.id'))
    role = db.Column(Text, nullable=False)
    name = db.Column(Text)
    address = db.Column(Text)
    office_phone = db.Column(Text)
    mobile = db.Column(Text)
    email = db.Column(Text)
    occupation = db.Column(Text)
    created_at = db.Column(Text)
    updated_at = db.Column(Text)
    updated_by = db.Column(Integer)

class StudentEnrollmentHistory(db.Model):
    __tablename__ = 'student_enrollment_history'
    __table_args__ = (Index('idx_student_history_student', 'student_id', 'session_id'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    student_id = db.Column(Integer, ForeignKey('students.id', ondelete='CASCADE'), nullable=False)
    session_id = db.Column(Integer, ForeignKey('academic_sessions.id'))
    level_name = db.Column(Text, nullable=False)
    class_id = db.Column(Integer, ForeignKey('school_classes.id'))
    enrolled_at = db.Column(Text)
    completed_at = db.Column(Text)
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    notes = db.Column(Text)
    created_at = db.Column(Text, nullable=False)
    corrected_at = db.Column(Text)
    corrected_by = db.Column(Integer)
    correction_reason = db.Column(Text)


# ------------------------------- presence, notifications and password recovery

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

class SchemaMigration(db.Model):
    __tablename__ = 'schema_migrations'

    version = db.Column(Text, primary_key=True)
    applied_at = db.Column(Text, nullable=False)
