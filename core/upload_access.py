"""Who may open which of a school's uploaded files.

A school's uploads folder holds pictures of children and of staff, signatures and question images.
Every file name ends in twenty random characters, so a name cannot be guessed, but "any signed-in
account of the school may open anything" is still looser than it needs to be. Here each folder has
its own rule, and the rule is decided on the normalised path (``app.uploaded_file`` cleans the
path first, so ``students/../signatures/x`` is judged as ``signatures/x``).

===========  ===========================================================================  =============
Folder       Who may open a file in it                                                    Written by
===========  ===========================================================================  =============
branding     anyone (the sign-in page needs the logo and photographs)                     school branding
messages     nobody here; only the permission-checked download route serves them          staff messages
admins       staff                                                                        staff accounts
signatures   staff (report cards and receipts embed signatures in the file they build)    receipts, report cards
candidates   staff, and the candidate the photograph belongs to                           candidate register
students     staff, the student it belongs to, and a parent linked to that student        student records
questions    staff, students and candidates (they take the exams the pictures belong to)  question banks, tests
assignments  staff and students (quiz pictures)                                           assignments
store        staff and parents (pictures of what the school store sells)                  store items
anything     staff only                                                                   (nothing writes here)
else
===========  ===========================================================================  =============

"Belongs to" is settled by the database row that owns the file: a student's photograph is theirs
when ``students.photo_path`` is exactly ``uploads/students/<name>``. A parent is matched through
their active link to that student. Every account must also still be active. A sign-in that has
ended (the server was restarted, the password was changed) opens nothing, exactly like being signed
out, and so does a sign-in that belongs to another school.

Questions and assignments stay open to every student and candidate rather than only those whose
exam contains the picture: a wrong refusal there would take a diagram away in the middle of an
exam, and a name cannot be guessed. Parents have no exam, so they are not let in.

The one exception outside a public folder is the school's own chosen logo (``school_logo``), which the
sign-in page shows to anyone; it matters only to a school whose logo predates the branding folder.

A refusal is always a plain 404, so it does not confirm that a file exists.
"""

import sqlalchemy as sa
from flask import g, session

from core.session_guard import usable_for_files
from models import (
    Admin, AdminType, Candidate, ParentAccount, ParentStudentLink, SchoolPublicSetting, Student, db,
)

PUBLIC = 'public'
NEVER = 'never'

# The kinds of person a folder is open to. 'admin' is any active member of staff; 'student' and
# 'candidate' any active account of that kind; 'own_student', 'own_candidate' and
# 'parent_of_student' only the account the file's owning row points at.
FOLDER_RULES = {
    'branding': PUBLIC,
    'messages': NEVER,
    'admins': frozenset({'admin'}),
    'signatures': frozenset({'admin'}),
    'candidates': frozenset({'admin', 'own_candidate'}),
    'students': frozenset({'admin', 'own_student', 'parent_of_student'}),
    'questions': frozenset({'admin', 'student', 'candidate'}),
    'assignments': frozenset({'admin', 'student'}),
    'store': frozenset({'admin', 'parent'}),
}
# A folder with no rule (and a file directly in the uploads folder) is for staff.
DEFAULT_RULE = frozenset({'admin'})

_SESSION_KEYS = (('admin', 'admin_id'), ('student', 'student_id'),
                 ('parent', 'parent_id'), ('candidate', 'candidate_id'))


def rule_for(folder):
    return FOLDER_RULES.get(folder, DEFAULT_RULE)


def _who():
    """``(kind, account_id)`` of the active account signed in to *this* school, else ``(None, None)``."""
    tenant = g.get('tenant')
    if tenant is None or session.get('tenant_id') != tenant.id:
        return None, None
    for kind, key in _SESSION_KEYS:
        value = session.get(key)
        if not value:
            continue
        try:
            account_id = int(value)
        except (TypeError, ValueError):
            return None, None
        if not usable_for_files():
            return None, None
        return (kind, account_id) if _is_active(kind, account_id) else (None, None)
    return None, None


def _is_active(kind, account_id):
    if kind == 'admin':
        return db.session.execute(
            sa.select(Admin.id).join(AdminType, AdminType.id == Admin.admin_type_id)
            .where(Admin.id == account_id, Admin.active == 1, AdminType.active == 1)).first() is not None
    if kind == 'student':
        query = sa.select(Student.id).where(Student.id == account_id, Student.active == 1,
                                            Student.account_active == 1)
    elif kind == 'parent':
        query = sa.select(ParentAccount.id).where(ParentAccount.id == account_id, ParentAccount.active == 1)
    else:
        query = sa.select(Candidate.id).where(Candidate.id == account_id, Candidate.active == 1)
    return db.session.execute(query).first() is not None


def _is_the_school_logo(clean):
    """Whether this file is the logo the school has chosen. The sign-in page shows it to people who are not
    signed in, so it is open to anyone - including a school whose logo was saved before the branding folder
    existed and so sits outside it. Only that one file; nothing else outside a folder with a public rule."""
    stored = db.session.execute(sa.select(SchoolPublicSetting.setting_value)
                                .where(SchoolPublicSetting.setting_key == 'school_logo')).scalar()
    return bool(stored) and stored == 'uploads/' + clean


def _owns_student_photo(kind, account_id, stored):
    if kind == 'student':
        return db.session.execute(sa.select(Student.id).where(
            Student.id == account_id, Student.photo_path == stored)).first() is not None
    if kind == 'parent':
        return db.session.execute(
            sa.select(ParentStudentLink.id)
            .join(Student, Student.id == ParentStudentLink.student_id)
            .where(ParentStudentLink.parent_id == account_id, ParentStudentLink.active == 1,
                   Student.photo_path == stored)).first() is not None
    return False


def _owns_candidate_photo(account_id, stored):
    return db.session.execute(sa.select(Candidate.id).where(
        Candidate.id == account_id, Candidate.photo_path == stored)).first() is not None


def may_open(folder, clean):
    """Whether this request's person may open ``uploads/<clean>``, which is in ``folder``.

    ``clean`` is the normalised path below the uploads folder; ``folder`` its first part, in lower
    case. Never raises for a refusal: it answers False.
    """
    rule = rule_for(folder)
    if rule == PUBLIC:
        return True
    if rule == NEVER:
        return False
    if _is_the_school_logo(clean):
        return True
    kind, account_id = _who()
    if kind is None:
        return False
    if kind in rule:                       # staff always; students and candidates where the folder says
        return True
    # Ownership is judged on the folder as it was cleaned (lower case), the file name as it is.
    stored = 'uploads/' + folder + '/' + clean.split('/', 1)[1] if '/' in clean else None
    if stored is None:
        return False
    if kind == 'student' and 'own_student' in rule:
        return _owns_student_photo('student', account_id, stored)
    if kind == 'parent' and 'parent_of_student' in rule:
        return _owns_student_photo('parent', account_id, stored)
    if kind == 'candidate' and 'own_candidate' in rule:
        return _owns_candidate_photo(account_id, stored)
    return False
