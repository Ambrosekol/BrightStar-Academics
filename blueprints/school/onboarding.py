"""The new-school setup checklist shown on the School workspace home page.

A school that signs up and then stalls on setup — never adding a subject, never enrolling a
student — is a school that quietly churns without ever raising a support ticket. This checklist
gives the four things a school must do before it can really run (classes, subjects, students, fee
items) as one ordered, visible list, so nobody has to already know where each of those lives.

Nothing here is stored as "done" or "not done": every step's state is read live from the school's
own data (how many active classes/subjects/students/fee items it has), so the checklist can never
disagree with what the school has actually done. The only thing that is stored is whether the
school has chosen to hide it once — a normal key/value row in ``school_settings``, exactly like the
report card settings, so no new table is needed for one flag.
"""

from datetime import datetime, timezone

from flask import flash, redirect, url_for

from app import app
from models import FinanceFeeItem, SchoolClass, SchoolSetting, SchoolSubject, Student, db
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


def onboarding_status():
    """The four setup steps, in the order a new school should do them, plus whether the whole
    checklist should be shown at all.

    Each step's ``done`` is a plain count from the school's own data, never a flag anyone can forget
    to set. ``show`` is false once every step is done (nothing left to prompt) or once the school has
    dismissed it; the caller still gets ``dismissed`` on its own, so a hidden-but-incomplete checklist
    can offer a small way back in.
    """
    steps = [
        {'key': 'classes', 'label': 'Review your classes',
         'text': 'Every school starts with the standard class list (Primary, JSS, SSS). Switch off any your school does not use, or add the ones it does.',
         'endpoint': 'admin_school_classes', 'permission': 'school.classes.view',
         'done': _count(SchoolClass, SchoolClass.active == 1) > 0},
        {'key': 'subjects', 'label': 'Add your subjects',
         'text': 'Create the subjects taught at your school and the classes each one is offered to.',
         'endpoint': 'admin_school_subjects', 'permission': 'school.subjects.view',
         'done': _count(SchoolSubject, SchoolSubject.active == 1) > 0},
        {'key': 'students', 'label': 'Enrol your students',
         'text': 'Register students and place each one in a class, so results, assignments and fees have someone to belong to.',
         'endpoint': 'admin_school_students', 'permission': 'school.students.view',
         'done': _count(Student, Student.active == 1) > 0},
        {'key': 'fees', 'label': 'Set up your fees',
         'text': 'Add the fee items your school charges (tuition, uniform, transport…) so you can start billing and collecting.',
         'endpoint': 'admin_finance_fee_items', 'permission': 'finance.manage',
         'done': _count(FinanceFeeItem, FinanceFeeItem.active == 1) > 0},
    ]
    done_count = sum(1 for s in steps if s['done'])
    dismissed = _dismissed()
    return {'steps': steps, 'done_count': done_count, 'total': len(steps),
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
