"""The unified sign-in surface shared by every account type (admin, parent,
student, candidate): login, logout, and self-service password recovery.
Account-type-specific mandatory password *change* flows (admin/parent/
student) stay with their own domain for now.
"""

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from flask import abort, flash, redirect, render_template, request, session, url_for
from sqlalchemy import func, or_, select, update as sa_update
from werkzeug.security import generate_password_hash

from app import app, csrf_check_request
from control_plane.context import current_tenant
from models import Admin, ParentAccount, PasswordResetToken, Student, db
from core.accounts import _authenticate_unified, _clear_identity_sessions, _rate_limit
from core.branding import school_name
from core.db_helpers import one
from core.delivery import email_settings, send_email
from core.presence import _presence_identity, end_presence
from core.public_settings import _public_settings
from core.security import audit_log, current_admin
from core.session_guard import stamp_sign_in


def _rate_limit_scope():
    """The school a login attempt belongs to, for keying the rate limiter."""
    tenant=current_tenant(required=False)
    return tenant.slug if tenant else '-'


def _account_recovery_target(raw):
    value=(raw or '').strip().lower()
    if not value: return None
    admin=one(select(Admin.id,Admin.email,Admin.display_name).where(
        Admin.active==1,
        or_(func.lower(Admin.username)==value,
            func.lower(func.coalesce(Admin.email,''))==value)))
    if admin: return ('admin',admin['id'],admin['email'],admin['display_name'])
    parent=one(select(ParentAccount.id,ParentAccount.email,ParentAccount.display_name).where(
        ParentAccount.active==1,
        or_(func.lower(ParentAccount.username)==value,
            func.lower(func.coalesce(ParentAccount.email,''))==value)))
    if parent: return ('parent',parent['id'],parent['email'],parent['display_name'])
    # Student recovery is keyed to the student's own login ID/admission username.
    # Guardian email is deliberately not used here because one guardian can have multiple children.
    student=one(select(Student.id,Student.guardian_email,Student.first_name,Student.last_name).where(
        Student.active==1, func.lower(func.coalesce(Student.login_username,''))==value))
    if student: return ('student',student['id'],student['guardian_email'],f"{student['first_name']} {student['last_name']}")
    return None

def _send_recovery_email(recipient,name,reset_url):
    settings=email_settings()
    if settings is None: return False,'Email recovery is not configured by the school yet.'
    from email.message import EmailMessage
    school=school_name()
    msg=EmailMessage(); msg['Subject']=f'{school} — Password reset'; msg['From']=settings.sender; msg['To']=recipient; msg.set_content(f'Dear {name},\n\nA password reset was requested for your {school} account. Use this link within 30 minutes:\n\n{reset_url}\n\nIf you did not request this, you can ignore this message.\n\n{school}')
    try:
        send_email(settings,msg)
        return True,'sent'
    except Exception as exc: return False,f'Password recovery email could not be sent: {exc}'

@app.route('/forgot-password',methods=['GET','POST'])
def forgot_password():
    if request.method=='POST':
        if not _rate_limit(f'forgot-password:{_rate_limit_scope()}:{request.remote_addr or "unknown"}', limit=5, window=900):
            flash('Too many password-recovery requests. Please wait a few minutes and try again.','error')
            return redirect(url_for('forgot_password'))
        raw=request.form.get('identifier','').strip(); target=_account_recovery_target(raw)
        generic='If the account exists and has a registered recovery email, instructions will be sent. If no message arrives, contact the school administrator.'
        if target:
            account_type,account_id,email,name=target
            if email:
                token=secrets.token_urlsafe(32); token_hash=hashlib.sha256(token.encode()).hexdigest(); now=datetime.now(timezone.utc); expires=(now+timedelta(minutes=30)).isoformat()
                db.session.execute(sa_update(PasswordResetToken)
                    .where(PasswordResetToken.account_type==account_type,
                           PasswordResetToken.account_id==account_id,
                           PasswordResetToken.used_at.is_(None))
                    .values(used_at=now.isoformat()))
                db.session.add(PasswordResetToken(
                    account_type=account_type,account_id=account_id,token_hash=token_hash,
                    expires_at=expires,created_at=now.isoformat(),requested_ip=request.remote_addr))
                db.session.commit()
                ok,_=_send_recovery_email(email,name,url_for('password_reset',token=token,_external=True))
                # Do not reveal whether an account exists or whether its email is configured.
                # The same response is used for every identifier.
                _ = ok
        flash(generic,'success'); return redirect(url_for('forgot_password'))
    return render_template('forgot_password.html')

@app.route('/reset-password/<token>',methods=['GET','POST'])
def password_reset(token):
    token_hash=hashlib.sha256((token or '').encode()).hexdigest()
    row=db.session.scalars(select(PasswordResetToken).where(
        PasswordResetToken.token_hash==token_hash,
        PasswordResetToken.used_at.is_(None),
        PasswordResetToken.expires_at>datetime.now(timezone.utc).isoformat())).first()
    if not row: return render_template('password_reset.html',errors=['This password reset link is invalid or has expired.'],valid=False)
    errors=[]
    if request.method=='POST':
        new=request.form.get('new_password',''); confirm=request.form.get('confirm_password','')
        if len(new)<8: errors.append('Your new password must be at least 8 characters long.')
        if new!=confirm: errors.append('The new password and confirmation do not match.')
        if not errors:
            now=datetime.now(timezone.utc).isoformat(); pw=generate_password_hash(new)
            if row['account_type']=='admin':
                db.session.execute(sa_update(Admin).where(Admin.id==row['account_id'])
                                   .values(password_hash=pw,password_must_change=0))
            elif row['account_type']=='parent':
                db.session.execute(sa_update(ParentAccount).where(ParentAccount.id==row['account_id'])
                                   .values(password_hash=pw,password_must_change=0))
            elif row['account_type']=='student':
                db.session.execute(sa_update(Student).where(Student.id==row['account_id'])
                                   .values(login_password_hash=pw,password_must_change=0))
            else: abort(400)
            row.used_at=now
            db.session.commit(); flash('Your password has been reset successfully. You can now sign in.','success'); return redirect(url_for('login'))
    return render_template('password_reset.html',errors=errors,valid=True)

@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        identifier=request.form.get('username','').strip()
        password=request.form.get('password','')
        # The bucket is per school as well as per IP/identifier: the counter is
        # process-wide, so without the school in the key, failed sign-ins at one
        # school would lock the same username out at every other school.
        rate_key=f"login:{_rate_limit_scope()}:{request.remote_addr or 'unknown'}:{identifier.lower()[:120]}"
        if not _rate_limit(rate_key, limit=8, window=300):
            return render_template('login.html',error='Too many sign-in attempts. Please wait a few minutes and try again.',identifier=identifier), 429
        if not identifier or not password:
            return render_template('login.html',error='Enter your username, Student ID or Candidate ID and password.',identifier=identifier)
        kind, account=_authenticate_unified(identifier,password)
        if not account:
            return render_template('login.html',error='We could not verify those login details. Please check your ID/username and password.',identifier=identifier)
        _clear_identity_sessions()
        stamp_sign_in(kind,account)   # this launch of the server, and this password (core/session_guard.py)
        if kind=='admin':
            session['admin_id']=account['id']; session['admin_logged_in']=True
            db.session.execute(sa_update(Admin).where(Admin.id==account['id'])
                .values(last_login_at=datetime.now(timezone.utc).isoformat()))
            db.session.commit()
            audit_log('admin_login','authentication','admin',account['id'],{'username':account['username']},True,account)
            if account['password_must_change']:
                return redirect(url_for('admin_password_change'))
            return redirect(url_for('admin_workspace_home'))
        if kind=='candidate':
            session['candidate_id']=account['id']
            audit_log('candidate_login','authentication','candidate',account['id'])
            return redirect(url_for('candidate_dashboard'))
        if kind=='parent':
            session['parent_id']=account['id']
            db.session.execute(sa_update(ParentAccount).where(ParentAccount.id==account['id'])
                .values(last_login_at=datetime.now(timezone.utc).isoformat()))
            db.session.commit()
            audit_log('parent_login','authentication','parent',account['id'])
            if account['password_must_change']:
                return redirect(url_for('parent_password_change'))
            return redirect(url_for('parent_dashboard'))
        session['student_id']=account['id']
        db.session.execute(sa_update(Student).where(Student.id==account['id'])
            .values(last_login_at=datetime.now(timezone.utc).isoformat()))
        db.session.commit()
        audit_log('student_login','authentication','student',account['id'])
        return redirect(url_for('student_dashboard'))
    # Login is intentionally a child environment of the public school site.
    return render_template('login.html', public_settings=_public_settings())

@app.route('/admin/login',methods=['GET','POST'])
def admin_login():
    # Compatibility URL: all account types now use the single login surface.
    if request.method=='GET':
        return redirect(url_for('login',next=request.args.get('next','')))
    return login()

@app.route('/logout',methods=['GET','POST'])
def logout():
    if request.method=='POST' and not csrf_check_request():
        abort(403, description='Invalid or missing CSRF token.')
    kind,account_id=_presence_identity()
    if kind:
        if kind=='admin':
            me=current_admin(); audit_log('admin_logout','authentication','admin',account_id,{'username':me['username'] if me else None},True,me)
        elif kind=='student':
            audit_log('student_logout','authentication','student',account_id)
        elif kind=='parent':
            audit_log('parent_logout','authentication','parent',account_id)
        elif kind=='candidate':
            audit_log('candidate_logout','authentication','candidate',account_id)
    end_presence()
    _clear_identity_sessions(); return redirect(url_for('index'))

@app.route('/admin/logout')
def admin_logout():
    return redirect(url_for('logout'))

@app.route('/candidate/logout')
def candidate_logout():
    return redirect(url_for('logout'))

# One answer, for the whole application, to a form value the database cannot hold (registers a handler).
import core.request_errors  # noqa: F401,E402
