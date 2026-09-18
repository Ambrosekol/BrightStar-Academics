"""The shared /login form serves four account types (admin, candidate,
parent, student) from one identifier+password pair, so the identity
resolution, session teardown, and login-attempt rate limiting behind it live
here rather than in any single domain.
"""

import time

from flask import session
from sqlalchemy import func, or_, select
from werkzeug.security import check_password_hash

from models import Admin, AdminType, Candidate, ParentAccount, Student, db

_RATE_BUCKETS={}

def _rate_limit(key, limit=10, window=300):
    """Small single-process guard for development/single-worker deployments.
    Production must place rate limiting at the reverse proxy/shared store layer.
    """
    now=time.monotonic()
    bucket=_RATE_BUCKETS.get(key,[])
    bucket=[t for t in bucket if now-t < window]
    if len(bucket) >= limit:
        _RATE_BUCKETS[key]=bucket
        return False
    bucket.append(now); _RATE_BUCKETS[key]=bucket
    return True

def _clear_identity_sessions():
    for key in ('admin_id','admin_logged_in','admin_workspace','student_id','parent_id','candidate_id','attempt_id','_presence_token'):
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
