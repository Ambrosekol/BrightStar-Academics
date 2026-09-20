"""Live "who is online" tracking: a heartbeat row per active session in
PresenceSession, and the aggregate counts/roster the admin dashboard reads.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy import and_, func, select, update as sa_update
from flask import request, session

from models import (
    Admin, ParentAccount, PresenceSession, SchoolClass, Student,
    StudentEnrolment, db,
)
from core.db_helpers import all_rows, insert_stmt, one_scalar

PRESENCE_TIMEOUT_SECONDS=90

def _presence_identity():
    if session.get('admin_id'):
        return 'admin', int(session['admin_id'])
    if session.get('student_id'):
        return 'student', int(session['student_id'])
    if session.get('parent_id'):
        return 'parent', int(session['parent_id'])
    if session.get('candidate_id'):
        return 'candidate', int(session['candidate_id'])
    return None,None

def _presence_token():
    token=session.get('_presence_token')
    if not token:
        token=secrets.token_urlsafe(32)
        session['_presence_token']=token
    return token

def touch_presence():
    account_type,account_id=_presence_identity()
    if not account_type or not account_id:
        return
    now=datetime.now(timezone.utc).isoformat()
    token_hash=hashlib.sha256(_presence_token().encode()).hexdigest()
    stmt=insert_stmt(PresenceSession).values(
        account_type=account_type,account_id=account_id,session_key_hash=token_hash,
        first_seen=now,last_seen=now,active=1,
        user_agent=request.headers.get('User-Agent','')[:500])
    db.session.execute(stmt.on_conflict_do_update(
        index_elements=['session_key_hash'],
        set_={'last_seen':stmt.excluded.last_seen,'active':1,
              'user_agent':stmt.excluded.user_agent}))
    db.session.execute(sa_update(PresenceSession)
        .where(PresenceSession.last_seen <
               (datetime.now(timezone.utc)-timedelta(seconds=PRESENCE_TIMEOUT_SECONDS)).isoformat())
        .values(active=0))
    db.session.commit()

def end_presence():
    token=session.get('_presence_token')
    if not token:
        return
    token_hash=hashlib.sha256(token.encode()).hexdigest()
    db.session.execute(sa_update(PresenceSession)
        .where(PresenceSession.session_key_hash==token_hash)
        .values(active=0,last_seen=datetime.now(timezone.utc).isoformat()))
    db.session.commit()
    session.pop('_presence_token',None)

def online_presence():
    """Live counts and the roster of who is currently signed in."""
    cutoff=(datetime.now(timezone.utc)-timedelta(seconds=PRESENCE_TIMEOUT_SECONDS)).isoformat()
    db.session.execute(sa_update(PresenceSession)
        .where(PresenceSession.last_seen < cutoff).values(active=0))

    def live(kind):
        return one_scalar(
            select(func.count(func.distinct(PresenceSession.account_id)))
            .where(PresenceSession.account_type==kind,PresenceSession.active==1,
                   PresenceSession.last_seen>=cutoff), 0)

    counts={'admins':live('admin'),'students':live('student'),'parents':live('parent')}
    display=sa.case(
        (PresenceSession.account_type=='admin', Admin.display_name),
        (PresenceSession.account_type=='student',
         func.trim(Student.first_name+' '+func.coalesce(Student.middle_name,'')+' '+Student.last_name)),
        (PresenceSession.account_type=='parent', ParentAccount.display_name))
    identifier=sa.case(
        (PresenceSession.account_type=='admin', Admin.username),
        (PresenceSession.account_type=='student', Student.admission_no),
        (PresenceSession.account_type=='parent', ParentAccount.username))
    stmt=(select(PresenceSession.account_type,PresenceSession.account_id,
                 func.max(PresenceSession.last_seen).label('last_seen'),
                 display.label('display_name'),identifier.label('identifier'),
                 SchoolClass.name.label('class_name'))
          .select_from(PresenceSession)
          .outerjoin(Admin,and_(Admin.id==PresenceSession.account_id,
                                PresenceSession.account_type=='admin'))
          .outerjoin(Student,and_(Student.id==PresenceSession.account_id,
                                  PresenceSession.account_type=='student'))
          .outerjoin(ParentAccount,and_(ParentAccount.id==PresenceSession.account_id,
                                        PresenceSession.account_type=='parent'))
          .outerjoin(StudentEnrolment,and_(StudentEnrolment.student_id==Student.id,
                                           StudentEnrolment.active==1,
                                           PresenceSession.account_type=='student'))
          .outerjoin(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
          .where(PresenceSession.active==1,PresenceSession.last_seen>=cutoff)
          .group_by(PresenceSession.account_type,PresenceSession.account_id)
          .order_by(PresenceSession.account_type,func.max(PresenceSession.last_seen).desc()))
    rows=all_rows(stmt)
    db.session.commit()
    return counts,rows
