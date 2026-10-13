"""The school's own report card design (the report card studio): the design itself, whether report cards use it, its
lock, and the saved versions of it. The rules are core/report_card_design.py; see models/__init__.py for the column
conventions.
"""

from sqlalchemy import ForeignKey, Index, Integer, Text, text

from .base import db


class ReportCardDesign(db.Model):
    """One row: the school's report card design. Report cards use it only while ``in_use`` is 1; otherwise they keep
    the standard card."""

    __tablename__ = 'report_card_designs'

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    layout_json = db.Column(Text, nullable=False)
    in_use = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    locked = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    locked_by = db.Column(Integer, ForeignKey('admins.id'))
    locked_at = db.Column(Text)
    updated_at = db.Column(Text, nullable=False)
    updated_by = db.Column(Integer, ForeignKey('admins.id'))


class ReportCardDesignVersion(db.Model):
    __tablename__ = 'report_card_design_versions'

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    layout_json = db.Column(Text, nullable=False)
    saved_at = db.Column(Text, nullable=False)
    saved_by = db.Column(Integer, ForeignKey('admins.id'))
