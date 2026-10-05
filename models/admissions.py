"""Admissions profile detail and historical enrolment record."""

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint, text

from .base import db


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
    # How the student left this year: 'promoted', 'repeated', 'graduated' or 'withdrawn'. A
    # 'graduated' record is what archives the student (see blueprints/school/student_history_import.py).
    outcome = db.Column(Text)
    corrected_by = db.Column(Integer)
    correction_reason = db.Column(Text)
