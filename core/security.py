"""Administrator RBAC: the permission catalogue, role presets, the
endpoint-to-permission map, every permission/scope check, the
``admin_required`` decorator, and the audit log those checks write to.

These pieces are grouped in one module rather than split into separate
"security" and "audit" files because they're mutually dependent:
``admin_required`` calls ``audit_log`` on every denied or state-changing
request, and ``audit_log`` calls ``_notify_school_admins`` (also here, since
its only caller is ``audit_log``) — splitting them would just move the
circular reference into two files importing each other.
"""

import json
import secrets
from datetime import datetime, timezone
from functools import wraps

import sqlalchemy as sa
from sqlalchemy import select
from flask import abort, redirect, render_template, request, session, url_for

from models import (
    Admin, AdminNotification, AdminPermission, AdminRoleAssignment,
    AdminScope, AdminType, AdminTypePermission, Attempt, AuditLog, Candidate,
    Permission, db,
)
from core.db_helpers import one, one_scalar, tuples


# ---------------- administrator permission catalogue ----------------

ADMIN_PERMISSION_DEFS = [
    ('admin.access','Administration access','administration','Access the administration area.'),
    ('admins.view','View administrators','administration','View administrator accounts.'),
    ('admins.create','Create administrators','administration','Create ordinary administrator accounts.'),
    ('admins.edit','Edit administrators','administration','Edit administrator accounts and access.'),
    ('admins.deactivate','Deactivate administrators','administration','Activate or deactivate administrator accounts.'),
    ('roles.view','View admin types','administration','View administrator types/roles.'),
    ('roles.create','Create admin types','administration','Create administrator types and assign permissions.'),
    ('roles.edit','Edit admin types','administration','Edit administrator types and permissions.'),
    ('permissions.view','View permissions','administration','View the permission catalogue.'),
    ('scopes.view','View scopes','administration','View administrator scopes.'),
    ('scopes.assign','Assign scopes','administration','Assign class/subject/bank/session scopes.'),
    ('audit.view','View audit logs','administration','Review administrative audit records.'),
    ('dashboard.view','View dashboard','operations','View the administration dashboard.'),
    ('candidates.view','View candidates','candidates','View registered candidates.'),
    ('candidates.create','Register candidates','candidates','Register candidates.'),
    ('candidates.edit','Edit candidates','candidates','Edit candidate records.'),
    ('candidates.delete','Delete candidates','candidates','Delete candidate records.'),
    ('candidates.credentials_reset','Reset candidate credentials','candidates','Reset candidate passwords.'),
    ('candidates.credentials_print','Print candidate credentials','candidates','Print candidate credentials.'),
    ('candidates.admit','Admit candidates','candidates','Move a candidate from the admissions waitlist into a real, enrolled student.'),
    ('question_banks.view','View question banks','assessment','View question banks.'),
    ('question_banks.create','Create question banks','assessment','Create question banks.'),
    ('question_banks.edit','Edit question banks','assessment','Edit question-bank settings.'),
    ('questions.create','Add questions','assessment','Add questions to banks.'),
    ('questions.edit','Edit questions','assessment','Edit questions in banks.'),
    ('questions.delete','Delete questions','assessment','Delete questions from banks.'),
    ('questions.reorder','Reorder questions','assessment','Reorder questions.'),
    ('examinations.manage','Manage examinations','assessment','Activate or deactivate examinations.'),
    ('attempts.view','View attempts','results','View candidate attempts.'),
    ('results.view','View results','results','View examination results.'),
    ('results.print','Print results','results','Print candidate results.'),
    ('results.retake','Grant retakes','results','Grant one-time retake access.'),
    ('results.regrade','Regrade attempts','results','Regrade completed attempts.'),
    ('results.rankings','View rankings','results','View rankings.'),
    ('results.export','Export results','results','Export result data.'),

    ('school.view','View School Portal','school','Access the School Portal workspace.'),
    ('school.students.view','View students','school','View enrolled school students.'),
    ('school.students.create','Register students','school','Create enrolled student records.'),
    ('school.students.edit','Edit students','school','Edit enrolled student records.'),
    ('school.students.delete','Deactivate students','school','Deactivate school student records.'),
    ('school.classes.view','View classes','school','View school classes.'),
    ('school.classes.manage','Manage classes','school','Activate or configure school classes.'),
    ('school.subjects.view','View subjects','school','View class subjects.'),
    ('school.subjects.create','Create subjects','school','Create subjects and attach them to classes.'),
    ('school.subjects.edit','Edit subjects','school','Edit subject details.'),
    ('school.subjects.delete','Delete subjects','school','Delete unlocked subjects.'),
    ('school.subjects.lock','Lock subjects','school','Lock or unlock class subjects.'),
    ('school.assignments.view','View assignments','school','View school assignments.'),
    ('school.assignments.create','Create assignments','school','Create subject assignments for students.'),
    ('school.assignments.edit','Edit assignments','school','Edit school assignments.'),
    ('school.assignments.delete','Delete assignments','school','Delete school assignments.'),
    ('school.projects.view','View projects','school','View school projects.'),
    ('school.projects.create','Create projects','school','Create class projects and tasks.'),
    ('school.projects.edit','Edit projects','school','Edit school projects and student records.'),
    ('school.projects.delete','Delete projects','school','Delete school projects.'),
    ('school.tests.view','View tests','school','View school tests.'),
    ('school.tests.create','Create tests','school','Create school tests and load questions.'),
    ('school.tests.edit','Edit tests','school','Edit school tests and questions.'),
    ('school.tests.delete','Delete tests','school','Delete school tests.'),
    ('school.practice.view','View practice tests','school','View school practice resources.'),
    ('school.practice.create','Create practice tests','school','Create practice tests and load questions.'),
    ('school.practice.edit','Edit practice tests','school','Edit practice tests.'),
    ('school.practice.delete','Delete practice tests','school','Delete practice tests.'),
    ('school.examinations.view','View examinations','school','View school examinations.'),
    ('school.examinations.create','Create examinations','school','Create school examinations and load questions.'),
    ('school.examinations.edit','Edit examinations','school','Edit school examinations and questions.'),
    ('school.examinations.delete','Delete examinations','school','Delete school examinations.'),
    ('school.results.view','View student records','school','View student academic records.'),
    ('school.results.release','Release student results','school','Set or change the school-wide academic result release schedule.'),
    ('school.results.enter','Enter student results','school','Enter approved manual or offline student results.'),
    ('school.results.verify','Verify student results','school','Verify compiled student result components.'),
    ('school.results.approve','Approve student results','school','Approve student result components for release.'),
    ('school.attendance.view','View attendance','school','View student attendance records and term summaries for the classes you may access.'),
    ('school.attendance.mark','Mark attendance','school','Take the daily register for the classes you may access.'),
    ('school.timetable.view','View exam timetables','school','View exam and test timetables for the classes you may access.'),
    ('school.timetable.manage','Manage exam timetables','school','Create, edit and delete exam and test timetable entries.'),
    ('school.timetable.release','Release exam timetables','school','Release an exam/test timetable to students and parents, with a notification.'),
    ('parent.view','View parent accounts','school','View parent account relationships.'),
    ('parent.manage','Manage parent accounts','school','Create and manage parent accounts and relationships.'),
    ('parent.feedback.view','View parent feedback','school','View parent messages and feedback assigned to the school academic team.'),
    ('parent.feedback.manage','Manage parent feedback','school','Assign, reply to and resolve parent feedback.'),
    ('presence.view','View live presence','administration','View authorized online account presence.'),
    ('finance.view_own','View own finance collections','finance','View payments and collection totals recorded by the signed-in finance officer.'),
    ('finance.record','Record payments','finance','Record student payments and generate official receipts.'),
    ('finance.receipt.send','Send receipts','finance','Send receipts by email or WhatsApp and print/download receipts.'),
    ('finance.view_all','View all finance','finance','View school-wide collections, balances, transactions and finance reports.'),
    ('finance.manage','Manage finance','finance','Manage fee assessments, payment corrections and finance controls.'),
    ('finance.paystack.manage','Manage online payments','finance','Set up and test the school\'s own Paystack account for parents to pay online.'),
    ('library.view','View library','library','View library books, members, loans and availability.'),
    ('library.manage','Manage library','library','Add books, issue/return books and manage library records.'),
    ('student.history.manage','Manage enrollment history','school','Record and review a student historical enrollment including Daycare, Crèche and Nursery.'),
    ('branding.manage','Manage school profile and branding','school','Change the school name, contact details, colours, logo and sign-in photographs.'),
    ('report_cards.view','View report cards','school','See and download students\' report cards for the classes you may access.'),
    ('report_cards.comment','Write report card comments','school','Write the class teacher\'s comment on report cards, and keep your own signature for them.'),
    ('report_cards.manage','Manage report card settings','school','Set the head teacher\'s or proprietor\'s title, name and signature, and when the next term begins.'),
    ('delivery.manage','Manage email and WhatsApp delivery','school','Set up the mail server and WhatsApp account the school sends receipts, recovery emails and alerts from.'),
    ('entrance.config.view','View entrance configurations','assessment','View entrance bank academic-period configurations.'),
    ('entrance.config.create','Create entrance configurations','assessment','Create entrance bank configurations.'),
    ('entrance.config.edit','Edit entrance configurations','assessment','Edit entrance bank configuration settings.'),
    ('entrance.config.activate','Activate entrance configurations','assessment','Activate the applicable entrance configuration.'),
    ('entrance.practice.manage','Manage entrance practice eligibility','assessment','Enable historical entrance banks for practice when eligible.'),
]


ADMIN_ROLE_PRESETS = {
    'Entrance Examination Manager': {
        'description': 'Runs day-to-day entrance examination operations, candidate registration and assessment monitoring.',
        'permissions': ['dashboard.view','candidates.view','candidates.create','candidates.edit','candidates.credentials_reset','candidates.credentials_print','question_banks.view','attempts.view','results.view','results.print','results.rankings']
    },
    'Question Bank Manager': {
        'description': 'Creates and maintains examination question banks, questions and periodized examination configurations.',
        'permissions': ['dashboard.view','question_banks.view','question_banks.create','question_banks.edit','questions.create','questions.edit','questions.reorder','entrance.config.view','entrance.config.create','entrance.config.edit','entrance.config.activate','entrance.practice.manage']
    },
    'Student Records Officer': {
        'description': 'Manages candidate registration records and candidate credentials for entrance processing.',
        'permissions': ['dashboard.view','candidates.view','candidates.create','candidates.edit','candidates.credentials_reset','candidates.credentials_print']
    },
    'Results & Analytics Officer': {
        'description': 'Reviews completed examinations, results, rankings and approved reporting data.',
        'permissions': ['dashboard.view','attempts.view','results.view','results.print','results.rankings','results.export']
    },
    'Examination Supervisor': {
        'description': 'Supervises live examination operations and handles controlled assessment actions.',
        'permissions': ['dashboard.view','candidates.view','question_banks.view','examinations.manage','attempts.view','results.view','results.print','results.rankings']
    },
    'Read-Only Academic Viewer': {
        'description': 'View-only access to examination information without the ability to change records.',
        'permissions': ['dashboard.view','candidates.view','question_banks.view','attempts.view','results.view','results.print','results.rankings']
    },
    'Admissions Officer': {
        'description': 'Reviews the admissions waitlist and decides who moves from candidate to enrolled student.',
        'permissions': ['dashboard.view','candidates.view','candidates.admit','results.view','results.rankings']
    },
    'School Records Officer': {
        'description': 'Manages enrolled student records and class information within assigned school scopes.',
        'permissions': ['school.view','school.students.view','school.students.create','school.students.edit','school.classes.view','school.attendance.view','parent.feedback.view']
    },
    'Primary Class Teacher': {
        'description': 'Manages students, subjects and assessments for assigned primary classes.',
        'permissions': ['school.view','school.students.view','school.students.create','school.students.edit','school.classes.view','school.subjects.view','school.subjects.create','school.subjects.edit','school.subjects.lock','school.assignments.view','school.assignments.create','school.assignments.edit','school.projects.view','school.projects.create','school.projects.edit','school.tests.view','school.tests.create','school.tests.edit','school.practice.view','school.practice.create','school.practice.edit','school.examinations.view','school.examinations.create','school.examinations.edit','school.results.view','school.attendance.view','school.attendance.mark','school.timetable.view','school.timetable.manage','parent.feedback.view','report_cards.view','report_cards.comment']
    },
    'College Subject Teacher': {
        'description': 'Manages the assigned subject across permitted college classes.',
        'permissions': ['school.view','school.students.view','school.classes.view','school.subjects.view','school.subjects.create','school.subjects.edit','school.subjects.lock','school.assignments.view','school.assignments.create','school.assignments.edit','school.projects.view','school.projects.create','school.projects.edit','school.tests.view','school.tests.create','school.tests.edit','school.practice.view','school.practice.create','school.practice.edit','school.examinations.view','school.examinations.create','school.examinations.edit','school.results.view','school.attendance.view','school.attendance.mark','school.timetable.view','school.timetable.manage']
    },
    'School Academic Administrator': {
        'description': 'Full school-portal academic administration without access to entrance-examination operations.',
        'permissions': ['school.view','school.students.view','school.students.create','school.students.edit','school.students.delete','school.classes.view','school.classes.manage','school.subjects.view','school.subjects.create','school.subjects.edit','school.subjects.delete','school.subjects.lock','school.assignments.view','school.assignments.create','school.assignments.edit','school.assignments.delete','school.projects.view','school.projects.create','school.projects.edit','school.projects.delete','school.tests.view','school.tests.create','school.tests.edit','school.tests.delete','school.practice.view','school.practice.create','school.practice.edit','school.practice.delete','school.examinations.view','school.examinations.create','school.examinations.edit','school.examinations.delete','school.results.view','school.results.enter','school.results.verify','school.results.approve','school.results.release','school.attendance.view','school.attendance.mark','school.timetable.view','school.timetable.manage','school.timetable.release','parent.view','parent.manage','parent.feedback.view','parent.feedback.manage','presence.view','report_cards.view','report_cards.comment','report_cards.manage']
    },
    'Finance Records Officer': {
        'description': 'Records student payments, issues receipts and sees only collections recorded by the officer.',
        'permissions': ['dashboard.view','school.view','school.students.view','finance.view_own','finance.record','finance.receipt.send','library.view']
    },
    'Finance Manager': {
        'description': 'Oversees the school-wide financial ledger, collections, balances, receipts and finance reports.',
        'permissions': ['dashboard.view','school.view','school.students.view','finance.view_own','finance.record','finance.receipt.send','finance.view_all','finance.manage','finance.paystack.manage']
    },
    'Secretary / Records Officer': {
        'description': 'Handles school records, student registration history and library operations without school-wide finance visibility.',
        'permissions': ['dashboard.view','school.view','school.students.view','school.students.create','school.students.edit','student.history.manage','library.view','library.manage']
    },
    'Librarian': {
        'description': 'Manages books, loans, returns and library records.',
        'permissions': ['dashboard.view','school.view','school.students.view','library.view','library.manage']
    },
    'Report Card Officer': {
        'description': "Writes class teachers' comments and prepares report cards for the classes assigned, and sets the head's signature.",
        'permissions': ['school.view','school.students.view','school.classes.view','report_cards.view','report_cards.comment','report_cards.manage']
    },
    'School Profile Manager': {
        'description': "Maintains the school's name, contact details, colours, logo and sign-in photographs.",
        'permissions': ['school.view','branding.manage']
    },
    'Parents\' Feedback Officer': {
        'description': 'Receives and manages parent feedback submitted through the Parent Portal.',
        'permissions': ['school.view','parent.view','parent.feedback.view','parent.feedback.manage']
    },
}


ADMIN_ENDPOINT_PERMISSIONS = {
    'admin_school_branding':'branding.manage','admin_school_branding_save':'branding.manage',
    'admin_school_report_cards':'report_cards.view','admin_school_report_card_view':'report_cards.view',
    'admin_school_report_card_pdf':'report_cards.view','admin_school_report_cards_class_pdf':'report_cards.view',
    'admin_school_report_card_comments':'report_cards.comment','admin_school_report_card_comments_save':'report_cards.comment',
    'admin_school_report_card_traits':'report_cards.comment','admin_school_report_card_traits_save':'report_cards.comment',
    'admin_my_signature':'report_cards.comment','admin_my_signature_save':'report_cards.comment',
    'admin_school_report_card_settings':'report_cards.manage','admin_school_report_card_settings_save':'report_cards.manage',
    'admin_school_attendance':'school.attendance.mark','admin_school_attendance_save':'school.attendance.mark',
    'admin_school_attendance_summary':'school.attendance.view',
    'admin_school_timetable':'school.timetable.view','admin_school_timetable_pdf':'school.timetable.view',
    'admin_school_timetable_new':'school.timetable.manage','admin_school_timetable_edit':'school.timetable.manage',
    'admin_school_timetable_delete':'school.timetable.manage','admin_school_timetable_release':'school.timetable.release',
    'admin_school_delivery':'delivery.manage','admin_school_delivery_email_save':'delivery.manage',
    'admin_school_delivery_email_clear':'delivery.manage','admin_school_delivery_email_test':'delivery.manage',
    'admin_school_delivery_whatsapp_save':'delivery.manage','admin_school_delivery_whatsapp_clear':'delivery.manage',
    'admin_school_delivery_whatsapp_check':'delivery.manage',
    # Bringing a bank in is creating one (replacing an existing one also needs question_banks.edit,
    # checked in the route); setting up the standard papers creates and activates configurations.
    'admin_bank_import':'question_banks.create','admin_entrance_config_standard':'entrance.config.create',
    'admin_dashboard':'dashboard.view','admin_candidates':'candidates.view','admin_new_candidate':'candidates.create',
    'admin_candidate_detail':'candidates.view','admin_candidate_delete':'candidates.delete',
    'admin_candidate_result_print':'results.print','admin_candidate_credentials_reset':'candidates.credentials_reset',
    'admin_candidate_credentials_print':'candidates.credentials_print','admin_new_bank':'question_banks.create',
    'admin_candidate_admissions':'candidates.admit','admin_candidate_admit':'candidates.admit',
    'admin_candidate_decline':'candidates.admit','admin_candidate_admission_reset':'candidates.admit',
    'admin_question_banks':'question_banks.view','admin_bank':'question_banks.view','admin_edit_bank':'question_banks.edit','admin_new_question':'questions.create',
    'admin_edit_question':'questions.edit','admin_delete_question':'questions.delete','admin_reorder':'questions.reorder',
    'admin_attempts':'attempts.view','admin_export_bank':'question_banks.view','admin_results':'results.view',
    'admin_results_summary':'results.view','admin_results_summary_print':'results.print','admin_result_detail':'results.view',
    'admin_result_print':'results.print','admin_rankings':'results.rankings','export_results_csv':'results.export',
    'export_rankings_csv':'results.export','export_results_json':'results.export','toggle_exam':'examinations.manage',
    'admin_entrance_config':'entrance.config.view','admin_entrance_config_save':'entrance.config.view','admin_entrance_config_activate':'entrance.config.activate','admin_practice_tests':'entrance.config.view','admin_practice_tests_save':'entrance.practice.manage',
    'admin_grant_retake':'results.retake','admin_regrade':'results.regrade',
    'admin_workspace_home':'admin.access','admin_controls':'audit.view',
    'admin_notification_read':'admin.access','admin_lock_resource':'audit.view','admin_unlock_resource':'audit.view','admin_control_resolve':'audit.view',
    'admin_notification_open':'admin.access','admin_notifications':'admin.access','admin_school_parent_edit':'parent.manage','admin_school_parent_credentials_reset':'parent.manage','admin_school_parent_feedback_detail':'parent.feedback.view',
    
    'admin_account_edit':'admins.edit',
    'admin_school_student_history_add':'student.history.manage','admin_school_student_history_edit':'student.history.manage',
    'admin_finance_dashboard':'finance.view_own',
'admin_finance_fee_items':'finance.manage','admin_finance_fee_item_new':'finance.manage','admin_finance_fee_item_edit':'finance.manage','admin_finance_fee_item_toggle':'finance.manage','admin_finance_assessment_new':'finance.manage','admin_finance_payment_void':'finance.manage','admin_finance_student_assessed_items':'finance.manage',
    'admin_finance_record':'finance.record',
    'admin_finance_receipt':'finance.view_own',
    'admin_finance_receipt_print':'finance.view_own',
    'admin_finance_receipt_pdf':'finance.view_own',
    'admin_finance_receipt_email':'finance.receipt.send',
    'admin_finance_receipt_whatsapp':'finance.receipt.send',
    'admin_finance_receipt_settings':'finance.manage',
    'admin_finance_student_account':'finance.view_own',
    'admin_finance_payment_allocate':'finance.record',
    'admin_finance_paystack_settings':'finance.paystack.manage','admin_finance_paystack_save':'finance.paystack.manage',
    'admin_finance_paystack_clear':'finance.paystack.manage','admin_finance_paystack_test':'finance.paystack.manage',
    'admin_library':'library.view',
    'admin_library_book_new':'library.manage',
'admin_library_book_edit':'library.manage','admin_library_book_toggle':'library.manage',
    'admin_library_issue':'library.manage',
    'admin_library_return':'library.manage',
    'admin_school_home':'school.view','admin_school_students':'school.students.view','admin_school_student_new':'school.students.create','admin_school_student_edit':'school.students.edit','admin_school_student_toggle':'school.students.delete','admin_school_student_account_reset':'school.students.edit','admin_school_student_account_toggle':'school.students.edit','admin_school_student_account_print':'school.students.view',
    'admin_school_students_import':'school.students.create','admin_school_students_import_template':'school.students.create','admin_school_students_import_run':'school.students.create',
    'admin_school_classes':'school.classes.view','admin_school_class_edit':'school.classes.manage','admin_school_class_toggle':'school.classes.manage',
    'admin_school_subjects':'school.subjects.view','admin_school_subject_new':'school.subjects.create','admin_school_subject_edit':'school.subjects.edit','admin_school_subject_delete':'school.subjects.delete','admin_school_subject_lock':'school.subjects.lock','admin_school_subject_final_lock':'school.subjects.lock','admin_school_subject_quick_create':'school.subjects.create',
    'admin_school_assignments':'school.assignments.view','admin_school_assignment_new':'school.assignments.create','admin_school_assignment_edit':'school.assignments.edit','admin_school_assignment_delete':'school.assignments.delete','admin_school_assignment_detail':'school.assignments.view','admin_school_assignment_question_new':'school.assignments.edit','admin_school_assignment_student_update':'school.assignments.edit',
    'admin_school_projects':'school.projects.view','admin_school_project_new':'school.projects.create','admin_school_project_edit':'school.projects.edit','admin_school_project_delete':'school.projects.delete','admin_school_project_detail':'school.projects.view','admin_school_project_student_update':'school.projects.edit',
    'admin_school_tests':'school.tests.view','admin_school_test_new':'school.tests.create','admin_school_test_edit':'school.tests.edit','admin_school_test_delete':'school.tests.delete',
    'admin_school_practice_tests':'school.practice.view','admin_school_practice_new':'school.practice.create','admin_school_practice_edit':'school.practice.edit','admin_school_practice_delete':'school.practice.delete',
    'admin_school_examinations':'school.examinations.view','admin_school_examination_new':'school.examinations.create','admin_school_examination_edit':'school.examinations.edit','admin_school_examination_delete':'school.examinations.delete',
    'admin_school_results':'school.results.view','admin_school_results_release':'school.results.release','admin_school_results_release_term':'school.results.release',
    'admin_school_onboarding_dismiss':'school.view','admin_school_onboarding_show':'school.view',
    'admin_school_parents':'parent.view','admin_school_parent_new':'parent.manage','admin_school_parent_feedback':'parent.feedback.view','admin_school_parent_feedback_reply':'parent.feedback.manage','admin_school_parent_feedback_status':'parent.feedback.manage','admin_school_result_manual_new':'school.results.enter','admin_school_result_edit':'school.results.enter','admin_school_result_workflow':'school.results.verify',
    'admin_school_assessment_detail':'school.view','admin_school_assessment_edit':'school.view','admin_school_assessment_toggle':'school.view','admin_school_assessment_question_new':'school.view','admin_school_assessment_question_delete':'school.view','admin_school_assessment_delete':'school.view',
}


# ---------------- identity and permission checks ----------------

def _active_admin(admin_id):
    """Load an active administrator whose role is also active, else None."""
    return db.session.scalars(
        select(Admin).join(AdminType,AdminType.id==Admin.admin_type_id)
                     .where(Admin.id==admin_id,Admin.active==1,AdminType.active==1)
    ).first()


def current_admin():
    aid=session.get('admin_id')
    if not aid: return None
    return db.session.scalars(
        select(Admin).join(AdminType,AdminType.id==Admin.admin_type_id)
                     .where(Admin.id==aid,Admin.active==1,AdminType.active==1)
    ).first()


# A school's top-level role: unrestricted inside its own school and the only one that can
# manage other roles. It was called "Super Admin" until the platform gained a super admin
# of its own (control_plane/team.py); an existing school's role is renamed in place at start-up.
SCHOOL_ADMIN_ROLE = 'School Admin'
LEGACY_TOP_ROLE_NAMES = ('Super Admin',)

# The public-website permissions went with the website editor. What they let a person edit — the
# school's name and contact details — now lives on the Branding page, so anyone who held
# website.manage keeps that ability as branding.manage; website.view had nothing left to view.
RETIRED_PERMISSIONS = {'website.manage': 'branding.manage', 'website.view': None}
# A preset role that was renamed, so an existing school's copy is renamed in place rather than
# left behind next to a new one.
RENAMED_PRESET_ROLES = {'Website & Content Manager': 'School Profile Manager'}


def is_school_admin(admin=None):
    admin=admin or current_admin()
    return bool(admin and admin['admin_type_system'])


def _notify_school_admins(title, message, severity='info', action_url=None, exclude_admin_id=None):
    try:
        # Resolve the actor before applying the exclusion rule. The previous
        # implementation only populated `me` when no exclusion was supplied,
        # causing every notification call that passed exclude_admin_id to fail
        # silently before a notification row was written.
        me=current_admin()
        if exclude_admin_id is None and me and me['admin_type_system']:
            exclude_admin_id=me['id']
        now=datetime.now(timezone.utc).isoformat()
        supers=db.session.scalars(
            select(Admin.id).join(AdminType,AdminType.id==Admin.admin_type_id)
                            .where(Admin.active==1,AdminType.active==1,AdminType.is_system==1)
        ).all()
        for sid in supers:
            if exclude_admin_id and sid==exclude_admin_id: continue
            db.session.add(AdminNotification(
                admin_id=sid,title=title,message=message,severity=severity,action_url=action_url,
                actor_admin_id=me['id'] if me else None,
                actor_username_snapshot=me['username'] if me else None,
                actor_display_name_snapshot=me['display_name'] if me else None,
                created_at=now))
        db.session.commit()
    except Exception:
        db.session.rollback()


def admin_has_permission(admin_id, code):
    admin=_active_admin(admin_id)
    if not admin: return False
    # Every authenticated active administrator may enter the workspace shell;
    # job-role capabilities govern the actual operational areas.
    if code == 'admin.access': return True
    if admin['admin_type_system']: return True
    # A permission may be granted three ways: by the account's own admin type, by
    # any additionally assigned role, or as a direct per-administrator override.
    via_own_type=(select(sa.literal(1))
        .select_from(AdminTypePermission)
        .join(Permission,Permission.id==AdminTypePermission.permission_id)
        .where(AdminTypePermission.admin_type_id==admin.admin_type_id,Permission.code==code))
    via_assigned_role=(select(sa.literal(1))
        .select_from(AdminRoleAssignment)
        .join(AdminTypePermission,AdminTypePermission.admin_type_id==AdminRoleAssignment.admin_type_id)
        .join(Permission,Permission.id==AdminTypePermission.permission_id)
        .where(AdminRoleAssignment.admin_id==admin.id,Permission.code==code))
    via_direct_grant=(select(sa.literal(1))
        .select_from(AdminPermission)
        .join(Permission,Permission.id==AdminPermission.permission_id)
        .where(AdminPermission.admin_id==admin.id,Permission.code==code))
    return one(via_own_type.union(via_assigned_role,via_direct_grant).limit(1)) is not None


def admin_permission_codes(admin_id):
    via_own_type=(select(Permission.code)
        .select_from(Permission)
        .join(AdminTypePermission,AdminTypePermission.permission_id==Permission.id)
        .join(Admin,Admin.admin_type_id==AdminTypePermission.admin_type_id)
        .where(Admin.id==admin_id))
    via_assigned_role=(select(Permission.code)
        .select_from(Permission)
        .join(AdminTypePermission,AdminTypePermission.permission_id==Permission.id)
        .join(AdminRoleAssignment,AdminRoleAssignment.admin_type_id==AdminTypePermission.admin_type_id)
        .where(AdminRoleAssignment.admin_id==admin_id))
    via_direct_grant=(select(Permission.code)
        .select_from(Permission)
        .join(AdminPermission,AdminPermission.permission_id==Permission.id)
        .where(AdminPermission.admin_id==admin_id))
    return {code for (code,) in tuples(via_own_type.union(via_assigned_role,via_direct_grant))}


def admin_scope_allows(admin_id, scope_type=None, scope_value=None):
    if not scope_type: return True
    admin=_active_admin(admin_id)
    if not admin: return False
    if admin['admin_type_system']: return True
    scopes=tuples(select(AdminScope.scope_type,AdminScope.scope_value)
                    .where(AdminScope.admin_id==admin.id))
    # An administrator with no explicit boundary works across the whole permitted area.
    # A boundary only narrows the dimension it names (class, subject, bank, etc.).
    if not scopes: return True
    if any(s_type=='global' and s_value=='*' for s_type,s_value in scopes): return True
    typed=[s_value for s_type,s_value in scopes if s_type==scope_type]
    if not typed: return True
    value=str(scope_value or '')
    return any(s_value=='*' or s_value==value for s_value in typed)


def admin_scope_for_request(kwargs):
    if request.endpoint and request.endpoint.startswith('admin_account_'): return None,None
    # Legacy collection pages currently expose mixed resources, so they require
    # an explicit global scope until resource-aware filtering is added to those pages.
    if request.endpoint in {'admin_candidates','admin_results','admin_results_summary','admin_results_summary_print','admin_rankings','admin_attempts','export_results_csv','export_rankings_csv','export_results_json','admin_candidate_admissions'}:
        return 'global','*'
    if 'bid' in kwargs: return 'bank',kwargs.get('bid')
    if 'cid' in kwargs:
        target_class=one_scalar(select(Candidate.target_class).where(Candidate.id==kwargs['cid']))
        return ('class',target_class) if target_class is not None else (None,None)
    if 'aid' in kwargs:
        bank_id=one_scalar(select(Attempt.bank_id).where(Attempt.id==kwargs['aid']))
        return ('bank',bank_id) if bank_id is not None else (None,None)
    return None,None


def admin_access_error(item):
    friendly={
        'admin.access':'Administration access',
        'dashboard.view':'View the examination overview',
        'report_cards.view':'View report cards',
        'report_cards.comment':'Write report card comments',
        'report_cards.manage':'Manage report card settings',
        'candidates.view':'View candidates',
        'candidates.create':'Register candidates',
        'question_banks.view':'View question banks',
        'question_banks.create':'Create question banks',
        'entrance.config.create':'Create entrance configurations',
        'entrance.config.activate':'Activate entrance configurations',
        'question_banks.edit':'Edit question banks',
        'questions.create':'Add questions',
        'questions.edit':'Edit questions',
        'questions.delete':'Delete questions',
        'results.view':'View results',
        'results.rankings':'View rankings',
        'attempts.view':'View examination activity',
        'audit.view':'View controls and alerts',
        'admins.view':'Manage administrators',
        'admins.create':'Create administrators',
        'admins.edit':'Edit administrator access',
        'School Admin control':'School Admin control',
        'School Admin role management':'School Admin role management',
        'School Admin approval':'School Admin approval',
        'Protected School Admin account':'Protected School Admin account',
    }
    return render_template('admin_forbidden.html',item=friendly.get(item,item.replace('_',' ').replace('.',' — ').title() if isinstance(item,str) else item),),403


# ---------------- audit log ----------------

def audit_display_detail(log):
    """Return a plain-language audit detail for the UI, never raw JSON/technical IDs."""
    action = str(log['action'] or '')
    details = log['details'] or ''
    try:
        payload = json.loads(details) if details else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}

    friendly = {
        'admin_login': 'Successful administrator sign-in.',
        'authorization_denied': 'Access was denied for an administrative action.',
        'scope_denied': 'The requested action was outside the administrator’s access boundary.',
        'state_change_request': 'An administrative state change was requested.',
        'report_card_comments_saved': 'Class teacher comments on report cards were saved.',
        'report_card_settings_updated': 'The report card settings (head signature and title) were updated.',
        'report_card_signature_updated': 'A staff signature for report cards was changed.',
        'school_student_created': 'A student record and school account were created.',
        'school_student_updated': 'Student record details were updated.',
        'school_student_account_reset': 'Student account credentials were reset.',
        'school_student_account_status_changed': 'Student account access status was changed.',
        'school_student_status_changed': 'Student record status was changed.',
        'school_class_status_changed': 'Class status was changed.',
        'school_subject_created': 'A school subject was created and assigned to classes.',
        'school_subject_updated': 'School subject details were updated.',
        'school_subject_deactivated': 'A school subject was deactivated.',
        'school_subject_lock_changed': 'A class subject was locked or unlocked.',
        'school_assignment_created': 'An assignment was created and assigned to students.',
        'school_assignment_updated': 'Assignment details were updated.',
        'school_assignment_deleted': 'An assignment was removed.',
        'school_assessment_created': 'A school assessment was created.',
        'school_assessment_updated': 'School assessment details were updated.',
        'school_assessment_status_changed': 'School assessment availability was changed.',
        'school_assessment_question_added': f"Question {payload.get('question_number')} was added to the assessment." if payload.get('question_number') else 'A question was added to the assessment.',
        'school_assessment_question_deleted': 'A question was removed from the assessment.',
        'school_assessment_deleted': 'A school assessment was deleted.',
        'control_item_resolved': 'A governance review item was marked as resolved.',
        'resource_locked': 'An examination resource was locked for review.',
        'resource_unlocked': 'An examination resource was unlocked.',
        'admin_created': 'An administrator account was created.',
        'admin_access_updated': 'Administrator access and boundary settings were updated.',
        'admin_status_changed': 'Administrator account status was changed.',
        'role_created': 'A staff role was created.',
        'role_updated': 'A staff role was updated.',
        'question_bank_created': 'A question bank was created.',
        'question_bank_updated': 'Question bank settings were updated.',
        'question_added': 'A question was added to a question bank.',
        'question_updated': 'A question was updated.',
        'question_deleted': 'A question was removed.',
        'questions_reordered': 'Question order was changed.',
        'admin_logout': 'Administrator signed out.',
        'student_login': 'Student signed in.',
        'student_logout': 'Student signed out.',
        'candidate_login': 'Entrance candidate signed in.',
        'candidate_logout': 'Entrance candidate signed out.',
        'parent_login': 'Parent signed in.',
        'parent_logout': 'Parent signed out.',
        'parent_password_changed': 'Parent password was changed.',
        'parent_account_created': 'A parent account was created and linked to students.',
        'entrance_config_created': 'An entrance examination configuration was created.',
        'entrance_config_updated': 'An entrance examination configuration was updated.',
        'entrance_config_activated': 'An entrance examination configuration was activated.',
        'question_bank_imported': 'A question bank was imported from a file.',
        'question_bank_replaced': 'A question bank was replaced by an imported file.',
        'entrance_standard_papers_set_up': 'The standard entrance papers were set up for an academic session.',
        'entrance_practice_source_changed': 'The source of a subject\'s entrance practice questions was changed.',
        'school_manual_result_entered': 'A manual/offline result was entered.',
        'school_result_edited': 'A school result was edited and returned for verification.',
        'school_result_verify': 'A school result was verified.',
        'school_result_approve': 'A school result was approved.',
        'school_result_release': 'A school result was released.',
        'school_results_term_released': 'All approved results of a student for a term were released.',
        'school_subject_final_locked': 'A school class subject was permanently locked.',
        'school_assignment_question_added': 'A CBT-style assignment question was added.',
        'school_assignment_student_updated': 'A student assignment record was updated.',
        'school_project_created': 'A school project was created.',
        'school_project_updated': 'A school project was updated.',
        'school_project_deleted': 'A school project was removed.',
        'school_project_student_updated': 'A student project record was updated.',
        'parent_feedback_replied': 'A parent feedback message received a school reply.',
        'parent_feedback_status_changed': 'A parent feedback status was changed.',

    }
    return friendly.get(action, 'Administrative activity recorded.')


def audit_log(action,module,target_type=None,target_id=None,details=None,success=True,admin=None):
    try:
        admin=admin or current_admin()
        db.session.add(AuditLog(
            admin_id=admin['id'] if admin else None,
            username_snapshot=admin['username'] if admin else None,
            action=action,module=module,target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            details=json.dumps(details,ensure_ascii=False,sort_keys=True) if isinstance(details,(dict,list)) else (str(details) if details else None),
            # The address the connection came from, never a header the visitor could write: behind a
            # proxy we trust, BRIGHTSTARS_TRUSTED_PROXIES makes remote_addr the visitor's real one.
            ip_address=(request.remote_addr or '')[:64],
            user_agent=request.headers.get('User-Agent','')[:500],
            success=1 if success else 0,
            created_at=datetime.now(timezone.utc).isoformat()))
        db.session.commit()
        if success and action in {'admin_created','admin_status_changed','role_created'}:
            labels={'admin_created':'A new administrator account was created.','admin_status_changed':'An administrator account status changed.','role_created':'A new staff role was created.'}
            _notify_school_admins('Administrative change', labels.get(action,'A significant administrative change was recorded.'), 'warning', url_for('admin_controls') if request else None, admin['id'] if admin else None)
    except Exception:
        db.session.rollback()


# ---------------- the admin_required decorator ----------------

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args,**kwargs):
        admin=current_admin()
        if not admin:
            session.pop('admin_logged_in',None); session.pop('admin_id',None)
            return redirect(url_for('admin_login',next=request.path))
        session['admin_logged_in']=True
        if admin['password_must_change'] and request.endpoint not in {'admin_password_change','admin_logout','logout'}:
            return redirect(url_for('admin_password_change'))
        permission=ADMIN_ENDPOINT_PERMISSIONS.get(request.endpoint,'admin.access')
        if not admin_has_permission(admin['id'],permission):
            audit_log('authorization_denied','administration','endpoint',request.endpoint,{'permission':permission},False,admin)
            return admin_access_error(permission)
        scope_type,scope_value=admin_scope_for_request(kwargs)
        if scope_type and not admin_scope_allows(admin['id'],scope_type,scope_value):
            audit_log('scope_denied','administration',scope_type,scope_value,{'endpoint':request.endpoint},False,admin)
            return admin_access_error(f'{scope_type}:{scope_value}')
        if request.method in ('POST','PUT','PATCH','DELETE'):
            audit_log('state_change_request','administration','endpoint',request.endpoint,{'method':request.method,'args':kwargs},True,admin)
        return fn(*args,**kwargs)
    return wrapper


def csrf_protect(fn):
    """Require a valid session-bound CSRF token only for state-changing requests."""
    @wraps(fn)
    def wrapper(*args,**kwargs):
        # GET/HEAD/OPTIONS are safe navigation requests and must be allowed to
        # render protected forms.  The previous Phase 6H implementation
        # validated the token on every request, which meant clicking a normal
        # GET link such as "Edit bank" or "Register candidate" produced a
        # 403 before the form could even be displayed.
        if request.method in ('POST','PUT','PATCH','DELETE'):
            token=request.form.get('_csrf_token','') or request.headers.get('X-CSRF-Token','')
            expected=session.get('_csrf_token')
            if not expected or not token or not secrets.compare_digest(token,expected):
                abort(403, description='Invalid or missing CSRF token.')
        return fn(*args,**kwargs)
    return wrapper
