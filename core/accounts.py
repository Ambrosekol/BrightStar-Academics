"""The shared /login form serves four account types (admin, candidate,
parent, student) from one identifier+password pair, so the identity
resolution, session teardown, and login-attempt rate limiting behind it live
here rather than in any single domain.
"""

from flask import session
from sqlalchemy import func, or_, select
from werkzeug.security import check_password_hash

from control_plane.ratelimit import allow
from models import Admin, AdminType, Candidate, ParentAccount, Student, db

def _rate_limit(key, limit=10, window=300):
    """True if this try is allowed: at most `limit` tries per `window` seconds for `key`.
    The count is kept in the registry database, so every worker process shares it
    (see control_plane/ratelimit.py). If that database cannot be reached this worker
    counts on its own for the moment, rather than locking everyone out.
    """
    return allow(key, limit, window)

def _clear_identity_sessions():
    for key in ('admin_id','admin_logged_in','admin_workspace','student_id','parent_id','candidate_id','attempt_id','_presence_token','pwv'):
        session.pop(key,None)

def _authenticate_unified(identifier, password):
    """Identify the account type from the supplied login identifier.

    Admin, candidate, parent and student credentials all arrive through one
    form, so each store is tried in turn. The Admin model exposes
    admin_type_name/admin_type_system as properties, which is what the join in
    the pre-ORM query was for.
    """
    raw=(identifier or '').strip()
    if not raw or not password: return None, None
    admin=db.session.scalars(
        select(Admin).join(AdminType,AdminType.id==Admin.admin_type_id)
        .where(func.lower(Admin.username)==raw.lower(),
               Admin.active==1,AdminType.active==1)).first()
    if admin and check_password_hash(admin.password_hash,password):
        return 'admin',admin
    candidate=db.session.scalars(select(Candidate).where(
        Candidate.candidate_code==raw.upper(),Candidate.active==1)).first()
    if candidate and check_password_hash(candidate.password_hash,password):
        return 'candidate',candidate
    parent=db.session.scalars(select(ParentAccount).where(
        or_(func.lower(ParentAccount.username)==raw.lower(),
            func.lower(func.coalesce(ParentAccount.email,''))==raw.lower()),
        ParentAccount.active==1)).first()
    if parent and check_password_hash(parent.password_hash,password):
        return 'parent',parent
    student=db.session.scalars(select(Student).where(
        func.lower(Student.login_username)==raw.lower(),
        Student.active==1,Student.account_active==1)).first()
    if student and student.login_password_hash and check_password_hash(student.login_password_hash,password):
        return 'student',student
    return None, None
