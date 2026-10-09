"""The new-school setup checklist shown on the School workspace home page.

A school that signs up and then stalls on setup — never adding a subject, never enrolling a
student — is a school that quietly churns without ever raising a support ticket. This checklist
gives the things a school must do before it can really run, and the smaller ones worth doing after
(profile, session, classes, subjects, staff, students, parents, fees, billing, payments, report cards,
messaging and more) as one ordered, grouped, visible list, so nobody has to already know where each of those lives.

Nothing here is stored as "done" or "not done": every step's state is read live from the school's
own data (how many active classes/subjects/students/fee items it has), so the checklist can never
disagree with what the school has actually done. The only thing that is stored is whether the
school has chosen to hide it once — a normal key/value row in ``school_settings``, exactly like the
report card settings, so no new table is needed for one flag.
"""

from datetime import datetime, timezone

from flask import flash, redirect, url_for

from app import app
from models import FinanceFeeItem, SchoolClass, SchoolPublicSetting, SchoolSetting, SchoolSubject, Student, db
from core.db_helpers import one_scalar
from core.security import admin_access_error, admin_has_permission, admin_required, current_admin, csrf_protect
from sqlalchemy import func, select
from blueprints.finance.helpers import _primary_school_id

DISMISSED_KEY = 'onboarding_checklist_dismissed'


def _count(model, *where):
    return one_scalar(select(func.count()).select_from(model).where(*where), 0)


def _dismissed():
    school_id = _primary_school_id()
    if not school_id:
        return False
    return one_scalar(select(SchoolSetting.setting_value).where(
        SchoolSetting.school_id == school_id, SchoolSetting.setting_key == DISMISSED_KEY)) == '1'


def _set_dismissed(value):
    school_id = _primary_school_id()
    if not school_id:
        return
    now = datetime.now(timezone.utc).isoformat()
    row = db.session.scalars(select(SchoolSetting).where(
        SchoolSetting.school_id == school_id, SchoolSetting.setting_key == DISMISSED_KEY)).first()
    if row:
        row.setting_value = '1' if value else ''
        row.updated_at = now
    else:
        db.session.add(SchoolSetting(school_id=school_id, setting_key=DISMISSED_KEY,
                                     setting_value='1' if value else '', updated_at=now))
    db.session.commit()


def _has_setting(key):
    school_id = _primary_school_id()
    return bool(school_id and (one_scalar(select(SchoolSetting.setting_value).where(
        SchoolSetting.school_id == school_id, SchoolSetting.setting_key == key)) or '').strip())


def _public(keys):
    """True when the school has saved a value for any of these public-profile settings."""
    return _count(SchoolPublicSetting, SchoolPublicSetting.setting_key.in_(keys),
                  func.length(func.trim(SchoolPublicSetting.setting_value)) > 0) > 0


def _safe(check):
    """A step's state must never take the whole page down: a check that cannot be answered counts as
    not done."""
    try:
        return bool(check())
    except Exception:
        db.session.rollback()
        return False


def _step_definitions():
    """Every setup step, grouped, in the order a new school should do them.

    ``major`` steps are what the school cannot really run without; the rest are minor - worth doing,
    but nothing stops without them. Each ``check`` is a live look at the school's own data.
    """
    from core import delivery, payments, theme
    from blueprints.finance.helpers import _receipt_signature_relpath
    from blueprints.school.report_card_data import SETTING_HEAD_NAME
    from models import (AcademicSession, Admin, AttendanceRecord, ClassSubject, ExamTimetableEntry,
                        FinanceFeeAssessment, FinancePayment, LibraryBook, ParentStudentLink, ReportCardTrait)

    def S(group, key, label, text, endpoint, permission, check, major=True, admin_only=False, warning=''):
        return {'group': group, 'key': key, 'label': label, 'text': text, 'endpoint': endpoint,
                'permission': permission, 'major': major, 'admin_only': admin_only, 'check': check,
                'warning': warning}

    SCHOOL, YEAR, PEOPLE, MONEY, RESULTS, EXTRAS = (
        'Your school', 'Academic year', 'People', 'Fees & money', 'Results & report cards',
        'Communication & extras')
    return [
        S(SCHOOL, 'profile', 'Complete your school profile',
          'Add the motto and your phone, email and address. They appear on the sign-in page, receipts and report cards.',
          'admin_school_branding', 'school.view',
          lambda: _public(('school_motto', 'school_tagline', 'school_phone', 'school_email', 'school_address')),
          admin_only=True),
        S(SCHOOL, 'look', 'Add your logo and school colours',
          'Upload the logo and choose your colours so every page, receipt and report card looks like your school.',
          'admin_school_branding', 'school.view',
          lambda: _public(('school_logo', theme.PRIMARY_KEY, theme.ACCENT_KEY)),
          major=False, admin_only=True),
        S(YEAR, 'sessions', 'Review the academic session',
          'Set the real session (for example 2026/2027): its name, start and end dates, and which one is current. Fees, results, attendance and report cards are all filed against it.',
          'admin_school_sessions', 'school.view',
          # A new school is given a placeholder session with no dates; it counts as reviewed once the
          # current session has been given its dates.
          lambda: _count(AcademicSession, AcademicSession.active == 1, AcademicSession.is_current == 1,
                         func.length(func.trim(func.coalesce(AcademicSession.start_date, ''))) > 0,
                         func.length(func.trim(func.coalesce(AcademicSession.end_date, ''))) > 0) > 0,
          admin_only=True,
          warning='Your school starts with a placeholder session. It is probably not the right one, so check its name and dates before you bill fees or enter results.'),
        S(YEAR, 'classes', 'Review your classes',
          'Every school starts with the standard class list (Primary, JSS, SSS). Switch off any your school does not use, or add the ones it does.',
          'admin_school_classes', 'school.classes.view',
          lambda: _count(SchoolClass, SchoolClass.active == 1) > 0),
        S(YEAR, 'subjects', 'Add your subjects',
          'Create the subjects taught at your school.',
          'admin_school_subjects', 'school.subjects.view',
          lambda: _count(SchoolSubject, SchoolSubject.active == 1) > 0),
        S(YEAR, 'class_subjects', 'Offer subjects to classes',
          'Choose which subjects each class takes. Assignments, tests, results and report cards only list a subject for the classes it is offered to.',
          'admin_school_subjects', 'school.subjects.view',
          lambda: _count(ClassSubject) > 0),
        S(PEOPLE, 'staff', 'Add your teachers and staff',
          'Create an account for each member of staff and give them a role, so they sign in with their own login rather than sharing yours.',
          'admin_accounts', 'admins.view',
          lambda: _count(Admin, Admin.active == 1) > 1, admin_only=True),
        S(PEOPLE, 'students', 'Enrol your students',
          'Register students and place each one in a class, so results, assignments and fees have someone to belong to. Many at once can be imported from a spreadsheet.',
          'admin_school_students', 'school.students.view',
          lambda: _count(Student, Student.active == 1) > 0),
        S(PEOPLE, 'parents', 'Link parents to their children',
          'Give each parent a portal account linked to their child, so they can see fees, results and notices and pay online.',
          'admin_school_parents', 'parent.view',
          lambda: _count(ParentStudentLink) > 0),
        S(MONEY, 'fees', 'Set up your fee items',
          'Add the fee items your school charges (tuition, uniform, transport…) and the classes each applies to.',
          'admin_finance_fee_items', 'finance.manage',
          lambda: _count(FinanceFeeItem, FinanceFeeItem.active == 1) > 0),
        S(MONEY, 'billing', 'Bill your students',
          'Charge the fees to a student, or to a whole class at once, for the session and term. Parents see what they owe only after this.',
          'admin_finance_fee_items', 'finance.manage',
          lambda: _count(FinanceFeeAssessment) > 0),
        S(MONEY, 'online_payments', 'Connect online payments',
          "Add your school's own Paystack keys so parents can pay fees from their portal, one fee or several at a time.",
          'admin_finance_paystack_settings', 'school.view',
          lambda: payments.payment_settings() is not None, major=False, admin_only=True),
        S(MONEY, 'receipt', 'Add the receipt signature',
          'Draw or upload the authorised signature that is printed on every receipt.',
          'admin_finance_receipt_settings', 'finance.manage',
          lambda: bool(_receipt_signature_relpath()), major=False),
        S(MONEY, 'first_payment', 'Record your first payment',
          'Try recording a payment and allocating it to a fee, so you know the whole money flow works before the term starts.',
          'admin_finance_record', 'finance.manage',
          lambda: _count(FinancePayment) > 0, major=False),
        S(RESULTS, 'report_settings', 'Set up your report cards',
          "Enter the head teacher's name, title and signature and the date the next term begins. They are printed on every report card.",
          'admin_school_report_card_settings', 'report_cards.view',
          lambda: _has_setting(SETTING_HEAD_NAME)),
        S(RESULTS, 'traits', 'Set the report card traits',
          'Review the behaviour and skills traits teachers rate on each report card.',
          'admin_school_report_cards', 'report_cards.view',
          lambda: _count(ReportCardTrait) > 0, major=False),
        S(RESULTS, 'attendance', 'Start taking attendance',
          'Mark the register for a class. Attendance feeds the summaries and the report cards.',
          'admin_school_attendance', 'school.attendance.view',
          lambda: _count(AttendanceRecord) > 0, major=False),
        S(RESULTS, 'timetable', 'Build the exam timetable',
          'Schedule the examinations by class and subject so students and parents know when each paper is.',
          'admin_school_timetable', 'school.timetable.view',
          lambda: _count(ExamTimetableEntry) > 0, major=False),
        S(EXTRAS, 'delivery', 'Connect email and SMS',
          "Add the school's own email (and SMS) account so receipts, results and notices reach parents from your school, not from the platform's shared address.",
          'admin_school_delivery', 'school.view',
          # Only the school's own account counts: the platform's shared sender is a fallback, not a setup.
          lambda: any(s is not None and s.source == 'school'
                      for s in (delivery.email_settings(), delivery.sms_settings())),
          admin_only=True,
          warning="Until you add your own account, parents receive mail from the platform's shared address instead of your school's."),
        S(EXTRAS, 'library', 'Stock the library',
          'Add your books so students can be lent them and returns are tracked.',
          'admin_library', 'library.view',
          lambda: _count(LibraryBook) > 0, major=False),
    ]


def onboarding_status():
    """Every setup step, grouped and in the order a new school should do them, plus whether the whole
    checklist should be shown at all.

    Each step's ``done`` is a live check on the school's own data, never a flag anyone can forget to set.
    ``major`` marks the steps a school cannot really run without; the others are minor. ``can_open`` says
    whether the person looking can use the step's page. ``show`` is false once every step is done
    (nothing left to prompt) or once the school has dismissed it; the caller still gets ``dismissed`` on
    its own, so a hidden-but-incomplete checklist can offer a small way back in.
    """
    from core.security import is_school_admin

    me = current_admin()
    steps, last_group = [], None
    for definition in _step_definitions():
        step = {k: v for k, v in definition.items() if k != 'check'}
        step['done'] = _safe(definition['check'])
        step['can_open'] = bool(me) and bool(admin_has_permission(me['id'], step['permission'])) \
            and (not step['admin_only'] or is_school_admin(me))
        step['group_start'] = step['group'] != last_group
        last_group = step['group']
        steps.append(step)
    done_count = sum(1 for s in steps if s['done'])
    major = [s for s in steps if s['major']]
    dismissed = _dismissed()
    return {'steps': steps, 'done_count': done_count, 'total': len(steps),
            'major_done': sum(1 for s in major if s['done']), 'major_total': len(major),
            'complete': done_count == len(steps), 'dismissed': dismissed,
            'show': dismissed is False and done_count < len(steps)}


@app.post('/admin/school/onboarding/dismiss')
@admin_required
@csrf_protect
def admin_school_onboarding_dismiss():
    me = current_admin()
    if not admin_has_permission(me['id'], 'school.view'):
        return admin_access_error('school.view')
    _set_dismissed(True)
    flash('The setup checklist is hidden. You can bring it back from the link at the bottom of this page.', 'success')
    return redirect(url_for('admin_school_home'))


@app.post('/admin/school/onboarding/show')
@admin_required
@csrf_protect
def admin_school_onboarding_show():
    me = current_admin()
    if not admin_has_permission(me['id'], 'school.view'):
        return admin_access_error('school.view')
    _set_dismissed(False)
    return redirect(url_for('admin_school_home'))
