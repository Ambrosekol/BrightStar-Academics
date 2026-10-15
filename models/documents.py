"""School documents: testimonials, transcripts and leaving certificates. Each kind has the school's own design (the
documents studio, core/document_design.py) with saved versions, and every document issued is kept in a register with
its serial number and exactly what it said (blueprints/school/documents.py). See models/__init__.py for the column
conventions."""

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint, text

from .base import db


class DocumentDesign(db.Model):
    """One row per kind ('testimonial', 'transcript', 'leaving'): the school's design for it, and its lock."""

    __tablename__ = 'document_designs'
    __table_args__ = (UniqueConstraint('kind'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    kind = db.Column(Text, nullable=False)
    layout_json = db.Column(Text, nullable=False)
    locked = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    locked_by = db.Column(Integer, ForeignKey('admins.id'))
    locked_at = db.Column(Text)
    updated_at = db.Column(Text, nullable=False)
    updated_by = db.Column(Integer, ForeignKey('admins.id'))


class DocumentDesignVersion(db.Model):
    __tablename__ = 'document_design_versions'
    __table_args__ = (Index('idx_document_design_versions_kind', 'kind', 'id'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    kind = db.Column(Text, nullable=False)
    layout_json = db.Column(Text, nullable=False)
    saved_at = db.Column(Text, nullable=False)
    saved_by = db.Column(Integer, ForeignKey('admins.id'))


class IssuedDocument(db.Model):
    """A testimonial, transcript or leaving certificate given to a student. ``details_json`` is everything it says, as
    it said it when issued (so a reprint is the same document); ``serial_no`` is printed on it. Never deleted: one issued
    by mistake is revoked, with a reason, and prints marked REVOKED from then on."""

    __tablename__ = 'issued_documents'
    __table_args__ = (UniqueConstraint('serial_no'), Index('idx_issued_documents_student', 'student_id'),
                      Index('idx_issued_documents_kind', 'kind', 'id'))

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    kind = db.Column(Text, nullable=False)
    serial_no = db.Column(Text, nullable=False)
    student_id = db.Column(Integer, ForeignKey('students.id', ondelete='CASCADE'), nullable=False)
    details_json = db.Column(Text, nullable=False)
    issued_by = db.Column(Integer, ForeignKey('admins.id'), nullable=False)
    issued_at = db.Column(Text, nullable=False)
    revoked_at = db.Column(Text)
    revoked_by = db.Column(Integer, ForeignKey('admins.id'))
    revoke_reason = db.Column(Text)
