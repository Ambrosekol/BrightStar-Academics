"""Public marketing website: pages, news, settings and contact enquiries."""

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint, text

from .base import db


class SchoolPublicPage(db.Model):
    __tablename__ = 'school_public_pages'
    __table_args__ = (UniqueConstraint('slug'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    slug = db.Column(Text, nullable=False)
    title = db.Column(Text, nullable=False)
    content = db.Column(Text)
    published = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    updated_at = db.Column(Text, nullable=False)
    updated_by = db.Column(Integer, ForeignKey('admins.id'))


class SchoolPublicNews(db.Model):
    __tablename__ = 'school_public_news'
    __table_args__ = (
        Index('idx_school_public_news_published', 'published', 'published_at'),
        UniqueConstraint('slug'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    slug = db.Column(Text, nullable=False)
    title = db.Column(Text, nullable=False)
    excerpt = db.Column(Text)
    body = db.Column(Text)
    published = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    published_at = db.Column(Text)
    created_at = db.Column(Text, nullable=False)
    updated_at = db.Column(Text, nullable=False)
    created_by = db.Column(Integer, ForeignKey('admins.id'))
    updated_by = db.Column(Integer, ForeignKey('admins.id'))
    image_url = db.Column(Text)


class SchoolPublicSetting(db.Model):
    __tablename__ = 'school_public_settings'
    __table_args__ = (UniqueConstraint('setting_key'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    setting_key = db.Column(Text, nullable=False)
    setting_value = db.Column(Text)
    updated_at = db.Column(Text, nullable=False)
    updated_by = db.Column(Integer, ForeignKey('admins.id'))


class SchoolPublicEnquiry(db.Model):
    """Contact-form submission from the public website."""

    __tablename__ = 'school_public_enquiries'
    __table_args__ = (Index('idx_school_public_enquiries_status', 'status', 'created_at'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    name = db.Column(Text, nullable=False)
    email = db.Column(Text)
    phone = db.Column(Text)
    subject = db.Column(Text)
    message = db.Column(Text, nullable=False)
    status = db.Column(Text, nullable=False, default='new', server_default=text("'new'"))
    created_at = db.Column(Text, nullable=False)
    handled_by = db.Column(Integer, ForeignKey('admins.id'))
    handled_at = db.Column(Text)
