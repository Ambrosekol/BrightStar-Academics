"""SQLAlchemy models for one school's database, split by domain.

This package replaces the old single-file ``models.py``. Every model class
is re-exported here so existing code — ``from models import db, Student,
FinancePayment, ...`` — keeps working completely unchanged, whether that
import lives in app.py, a services/ module, or a test script.

These models are the single description of a school's schema: ``create_all()`` builds it and
a column-diffing helper adds anything an older database lacks.
They are deliberately NOT an idealised redesign:

* Timestamps stay ``Text``. The application stores and compares ISO-8601
  strings produced by ``datetime.now(timezone.utc).isoformat()``. Switching
  these to ``DateTime`` would silently change the stored format and break
  every existing row and every string comparison in the codebase.
* Boolean-ish flags stay ``Integer`` (0/1), matching the existing columns and
  the ``active=1`` style predicates used throughout the app.
* Column order, nullability, defaults and constraints reproduce the live
  schema so that ``metadata.create_all()`` on a fresh database yields the
  same shape.

Instances support ``sqlite3.Row``-style access (``row['col']``, ``'col' in
row.keys()``) as well as normal attribute access, so existing templates and
helper code that index rows by name keep working unchanged.

Import order below matters only for readability, not correctness: SQLAlchemy
resolves every ``ForeignKey('table_name.column')`` by table name once all
modules below have been imported and every model has registered itself on
the shared ``Base.metadata`` from :mod:`models.base` — not by Python import
order — so a model in ``school.py`` can freely reference a table declared in
``finance.py`` (or vice versa) without a circular-import problem.
"""

from .base import Base, db

from .entrance import (
    Answer,
    Attempt,
    AttemptQuestion,
    Candidate,
    CandidatePaper,
    EntranceBankConfig,
    EntrancePracticeSetting,
    Examination,
    RetakeGrant,
)

from .auth import (
    Admin,
    AdminControlItem,
    AdminMessage,
    AdminNotification,
    AdminPermission,
    AdminResourceLock,
    AdminRoleAssignment,
    AdminScope,
    AdminType,
    AdminTypePermission,
    AuditLog,
    Permission,
    SecurityEvent,
    _LegacyAttachmentColumns,
)

from .school import (
    AcademicPromotionItem,
    AcademicPromotionRun,
    AcademicSession,
    AssignmentQuestion,
    AssignmentStudent,
    AttendanceRecord,
    ClassSubject,
    ExamTimetableEntry,
    ProjectStudent,
    ReportCardComment, ReportCardTrait, ResultWorkflowEvent,
    SchoolAssessment,
    SchoolAssessmentAnswer,
    SchoolAssessmentAttempt,
    SchoolAssessmentAttemptQuestion,
    SchoolAssignment,
    SchoolAssignmentAnswer,
    SchoolAssignmentAttempt,
    SchoolAssignmentAttemptQuestion,
    SchoolClass,
    SchoolClassProgression,
    SchoolProject,
    SchoolQuestion,
    SchoolStudentResult,
    SchoolSubject,
    Student,
    StudentEnrolment,
)

from .admissions import (
    StudentAdmissionContact,
    StudentAdmissionProfile,
    StudentEnrollmentHistory,
)

from .finance import (
    FinanceDeliveryLog,
    FinanceFeeAssessment,
    FinanceFeeItem,
    FinanceFeeItemClass,
    FinanceOnlinePayment,
    FinancePayment,
    FinancePaymentAllocation,
)

from .library import LibraryBook, LibraryLoan

from .parents import (
    ParentAccount,
    ParentFeedback,
    ParentFeedbackReply,
    ParentStudentLink,
)

from .public import SchoolPublicSetting

from .tenancy import (
    School,
    SchoolDeliverySetting,
    SchoolNumberingPolicy,
    SchoolPaymentSetting,
    SchoolSetting,
    StudentNumberAllocation,
)

from .presence import NotificationDeliveryLog, PasswordResetToken, PresenceSession, SchoolNotification

from .resilience import BackgroundJob, IdempotencyKey

__all__ = [
    'Base', 'db',
    # entrance
    'Answer', 'Attempt', 'AttemptQuestion', 'Candidate', 'CandidatePaper',
    'EntranceBankConfig', 'EntrancePracticeSetting', 'Examination', 'RetakeGrant',
    # auth / governance
    'Admin', 'AdminControlItem', 'AdminMessage', 'AdminNotification',
    'AdminPermission', 'AdminResourceLock', 'AdminRoleAssignment',
    'AdminScope', 'AdminType', 'AdminTypePermission', 'AuditLog',
    'Permission', 'SecurityEvent', '_LegacyAttachmentColumns',
    # school
    'AcademicPromotionItem', 'AcademicPromotionRun', 'AcademicSession',
    'AssignmentQuestion', 'AssignmentStudent', 'AttendanceRecord', 'ClassSubject', 'ExamTimetableEntry',
    'ProjectStudent', 'ReportCardComment', 'ReportCardTrait', 'ResultWorkflowEvent', 'SchoolAssessment',
    'SchoolAssessmentAnswer', 'SchoolAssessmentAttempt',
    'SchoolAssessmentAttemptQuestion', 'SchoolAssignment',
    'SchoolAssignmentAnswer', 'SchoolAssignmentAttempt',
    'SchoolAssignmentAttemptQuestion', 'SchoolClass',
    'SchoolClassProgression', 'SchoolProject', 'SchoolQuestion',
    'SchoolStudentResult', 'SchoolSubject', 'Student', 'StudentEnrolment',
    # admissions
    'StudentAdmissionContact', 'StudentAdmissionProfile',
    'StudentEnrollmentHistory',
    # finance
    'FinanceDeliveryLog', 'FinanceFeeAssessment', 'FinanceFeeItem',
    'FinanceFeeItemClass', 'FinanceOnlinePayment', 'FinancePayment', 'FinancePaymentAllocation',
    # library
    'LibraryBook', 'LibraryLoan',
    # parents
    'ParentAccount', 'ParentFeedback', 'ParentFeedbackReply',
    'ParentStudentLink',
    # settings
    'SchoolPublicSetting',
    # tenancy
    'School', 'SchoolDeliverySetting', 'SchoolNumberingPolicy', 'SchoolPaymentSetting', 'SchoolSetting',
    'StudentNumberAllocation',
    # presence / notifications / password recovery
    'NotificationDeliveryLog', 'PasswordResetToken', 'PresenceSession', 'SchoolNotification',
    # resilience: durable background work, and forms that must never run twice
    'BackgroundJob', 'IdempotencyKey',
]
