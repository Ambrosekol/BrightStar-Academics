"""School portal: classes, students, subjects, assignments, assessments,
projects, results and end-of-session promotion."""

from sqlalchemy import Float, ForeignKey, Index, Integer, Text, UniqueConstraint, text

from .base import db


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
    # Who created the session and why, when it was not made by hand from the session page: a past
    # session created during a bulk history import records the importing administrator and the reason.
    created_by_admin_id = db.Column(Integer, ForeignKey('admins.id'))
    creation_reason = db.Column(Text)
    created_via = db.Column(Text)


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
            postgresql_where=text('login_username IS NOT NULL'),
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

    # Archiving keeps a student's record and academic history but takes them out of every current
    # list, count and login. It also sets ``active`` to 0, so the existing "active" filters do the
    # exclusion; ``archived_at`` is what tells an archived student apart from a merely deactivated one.
    archived_at = db.Column(Text)
    archived_by_admin_id = db.Column(Integer, ForeignKey('admins.id'))
    archive_reason = db.Column(Text)

    # Senior secondary (SSS 1-3) only: 'Science', 'Art' or 'Commercial'. It decides which
    # department-specific subjects the student takes and which subject teachers reach them.
    department = db.Column(Text)


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
    # In senior secondary classes only: the departments that take this subject, comma-separated
    # (for example 'Science' for Physics, 'Art,Commercial' for Literature). Empty means every
    # department takes it, as with English and Mathematics. Junior classes ignore it.
    departments = db.Column(Text)


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
    # Connectivity-grace bookkeeping (core/entrance.py:apply_connectivity_grace) - last_seen_at is
    # bumped by a periodic client heartbeat while the paper is open; grace_extended_seconds tracks
    # how much of the capped allowance has already been granted to this attempt.
    last_seen_at = db.Column(Text)
    grace_extended_seconds = db.Column(Integer, nullable=False, default=0, server_default=text('0'))


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


class ReportCardComment(db.Model):
    """The class teacher's comment on one student's report card for one term.

    One row per student, session and term. The author is the staff member who wrote it: their
    name and their own signature are what the report card shows beside the comment.
    """

    __tablename__ = 'report_card_comments'
    __table_args__ = (
        UniqueConstraint('student_id', 'session_id', 'term'),
        Index('idx_report_card_comments_session', 'session_id', 'term'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    student_id = db.Column(Integer, ForeignKey('students.id'), nullable=False)
    session_id = db.Column(Integer, ForeignKey('academic_sessions.id'), nullable=False)
    term = db.Column(Text, nullable=False)
    comment = db.Column(Text, nullable=False)
    author_admin_id = db.Column(Integer, ForeignKey('admins.id'))
    created_at = db.Column(Text, nullable=False)
    updated_at = db.Column(Text)


class AttendanceRecord(db.Model):
    """One student's attendance status for one calendar day.

    Recorded once per student per day (not per subject): whichever class teacher takes the
    register for that class marks it. ``status`` is one of 'present', 'late', 'absent' or 'excused'
    (blueprints/school/attendance_data.py, ``STATUSES``). A day nobody has taken the register for
    simply has no row, so a summary only ever counts days the school actually recorded.
    """

    __tablename__ = 'attendance_records'
    __table_args__ = (
        UniqueConstraint('student_id', 'date'),
        Index('idx_attendance_class_date', 'class_id', 'date'),
        Index('idx_attendance_student_session_term', 'student_id', 'session_id', 'term'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    student_id = db.Column(Integer, ForeignKey('students.id'), nullable=False)
    class_id = db.Column(Integer, ForeignKey('school_classes.id'), nullable=False)
    session_id = db.Column(Integer, ForeignKey('academic_sessions.id'), nullable=False)
    term = db.Column(Text, nullable=False)
    date = db.Column(Text, nullable=False)
    status = db.Column(Text, nullable=False)
    marked_by_admin_id = db.Column(Integer, ForeignKey('admins.id'))
    created_at = db.Column(Text, nullable=False)
    updated_at = db.Column(Text)


class ExamTimetableEntry(db.Model):
    """One row of an exam or test timetable: one subject's paper for one class, on one date and time.

    Entries are created freely as a draft and only take effect for students and parents once
    released (``released_at`` set) — see blueprints/school/timetable.py. Releasing notifies every
    student and parent of every class the release covers, once, the same day it happens.
    """

    __tablename__ = 'exam_timetable_entries'
    __table_args__ = (
        Index('idx_timetable_session_term', 'session_id', 'term'),
        Index('idx_timetable_class', 'class_id'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    session_id = db.Column(Integer, ForeignKey('academic_sessions.id'), nullable=False)
    term = db.Column(Text, nullable=False)
    class_id = db.Column(Integer, ForeignKey('school_classes.id'), nullable=False)
    subject_id = db.Column(Integer, ForeignKey('school_subjects.id'), nullable=False)
    exam_type = db.Column(Text, nullable=False, server_default=text("'examination'"))
    date = db.Column(Text, nullable=False)
    start_time = db.Column(Text, nullable=False)
    end_time = db.Column(Text)
    venue = db.Column(Text)
    released_at = db.Column(Text)
    created_by = db.Column(Integer, ForeignKey('admins.id'))
    created_at = db.Column(Text, nullable=False)
    updated_at = db.Column(Text)


class ReportCardTrait(db.Model):
    """A student's affective and psychomotor trait ratings for one term, alongside the class teacher's comment.

    One row per student, session and term, written by the same class teacher on the same page as the
    comment. ``ratings`` is a small JSON object mapping a trait key (the catalogue lives in
    blueprints/school/report_card_data.py, ``TRAIT_GROUPS``) to a rating from 1 (Poor) to 5 (Excellent);
    a trait the teacher has not rated is simply missing from the object, never stored as a null.
    """

    __tablename__ = 'report_card_traits'
    __table_args__ = (
        UniqueConstraint('student_id', 'session_id', 'term'),
        Index('idx_report_card_traits_session', 'session_id', 'term'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    student_id = db.Column(Integer, ForeignKey('students.id'), nullable=False)
    session_id = db.Column(Integer, ForeignKey('academic_sessions.id'), nullable=False)
    term = db.Column(Text, nullable=False)
    ratings = db.Column(Text, nullable=False)
    author_admin_id = db.Column(Integer, ForeignKey('admins.id'))
    created_at = db.Column(Text, nullable=False)
    updated_at = db.Column(Text)
