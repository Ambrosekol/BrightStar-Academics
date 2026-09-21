"""A school's settings: the key/value store its name, contact details, colours, logo and
sign-in photographs are kept in.

This module also held a public website's pages, news and contact enquiries. A school's address
is a portal with no public site, so those were removed; a database that predates the removal
still has the tables, which are simply no longer read or written.
"""

from sqlalchemy import ForeignKey, Integer, Text, UniqueConstraint

from .base import db


class SchoolPublicSetting(db.Model):
    __tablename__ = 'school_public_settings'
    __table_args__ = (UniqueConstraint('setting_key'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    setting_key = db.Column(Text, nullable=False)
    setting_value = db.Column(Text)
    updated_at = db.Column(Text, nullable=False)
    updated_by = db.Column(Integer, ForeignKey('admins.id'))
