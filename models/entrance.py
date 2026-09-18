"""Entrance examination: banks, attempts, candidates and their papers.

These models mirror the schema that already exists in ``cbt.db`` exactly, as
it was accreted by ``init_db()`` and the (now-inert) ``migrations`` package —
deliberately NOT an idealised redesign. See ``models/__init__.py`` for the
column-convention notes that apply across every model in this package.
"""

from sqlalchemy import Float, ForeignKey, Index, Integer, Text, UniqueConstraint, text

from .base import db


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
