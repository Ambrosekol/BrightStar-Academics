"""The public marketing site: /school and its about/academics/school-life/
admissions/news/contact pages. No login required for any route here.
"""

from datetime import datetime, timezone

from flask import abort, flash, redirect, render_template, request, url_for
from sqlalchemy import func, select

from app import app
from models import Admin, AdminNotification, AdminType, SchoolPublicEnquiry, SchoolPublicNews, db
from core.db_helpers import tuples
from core.public_settings import _public_page, _public_settings
from core.security import admin_has_permission


@app.route('/school')
def public_school_home():
    # / is the public school's front door; /school is a stable alias for the same environment.
    return redirect(url_for('index'))

@app.route('/school/about')
def public_school_about():
    return render_template('public_page.html',settings=_public_settings(),page=_public_page('about'))

@app.route('/school/academics')
def public_school_academics():
    return render_template(
        'public_academics.html',
        settings=_public_settings(),
        public_settings=_public_settings()
    )

@app.route('/school/school-life')
def public_school_life():
    return render_template(
        'public_school_life.html',
        settings=_public_settings(),
        public_settings=_public_settings()
    )

@app.route('/school/admissions')
def public_school_admissions():
    return render_template(
        'public_admissions.html',
        settings=_public_settings(),
        public_settings=_public_settings()
    )

@app.route('/school/news')
def public_school_news():
    news=db.session.scalars(select(SchoolPublicNews).where(SchoolPublicNews.published==1)
        .order_by(func.coalesce(SchoolPublicNews.published_at,SchoolPublicNews.created_at).desc(),
                  SchoolPublicNews.id.desc())).all()
    return render_template('public_news.html',settings=_public_settings(),news=news)

@app.route('/school/news/<slug>')
def public_school_news_detail(slug):
    news=db.session.scalars(select(SchoolPublicNews).where(
        SchoolPublicNews.slug==slug, SchoolPublicNews.published==1)).first()
    if not news: abort(404)
    return render_template('public_news_detail.html',settings=_public_settings(),news=news)

@app.route('/school/contact',methods=['GET','POST'])
def public_school_contact():
    errors=[]
    if request.method=='POST':
        name=request.form.get('name','').strip(); email=request.form.get('email','').strip().lower(); phone=request.form.get('phone','').strip(); subject=request.form.get('subject','').strip(); message=request.form.get('message','').strip()
        if not name: errors.append('Your name is required.')
        if email and ('@' not in email or '.' not in email.rsplit('@',1)[-1]): errors.append('Enter a valid email address.')
        if not message: errors.append('Please enter your message.')
        if len(message)>5000: errors.append('Please keep your message under 5,000 characters.')
        if not errors:
            now=datetime.now(timezone.utc).isoformat()
            enquiry=SchoolPublicEnquiry(name=name,email=email or None,phone=phone or None,
                                        subject=subject or None,message=message,created_at=now)
            db.session.add(enquiry); db.session.flush()
            admin_ids=[aid for (aid,) in tuples(
                select(Admin.id).join(AdminType,AdminType.id==Admin.admin_type_id)
                .where(Admin.active==1,AdminType.active==1))]
            for aid in admin_ids:
                if admin_has_permission(aid,'website.view'):
                    db.session.add(AdminNotification(
                        admin_id=aid,title='New website enquiry',
                        message=f'{name} sent a message{(" about " + subject) if subject else ""}.',
                        severity='info',
                        action_url=url_for('admin_school_enquiry_detail',eid=enquiry.id),
                        created_at=now))
            db.session.commit(); flash('Thank you. Your message has been sent to the school.','success'); return redirect(url_for('public_school_contact'))
    return render_template('public_contact.html',settings=_public_settings(),errors=errors,form=request.form)
