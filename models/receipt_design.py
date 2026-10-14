"""The school's own receipt design (the receipt studio): the design itself, whether receipts use it, its lock, and the
saved versions of it. The rules are core/receipt_design.py; see models/__init__.py for the column conventions.
"""

from sqlalchemy import ForeignKey, Integer, Text, text

from .base import db


class ReceiptDesign(db.Model):
    """One row: the school's receipt design. Receipts use it only while ``in_use`` is 1; otherwise they keep the
    standard receipt."""

    __tablename__ = 'receipt_designs'

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    layout_json = db.Column(Text, nullable=False)
    in_use = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    locked = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    locked_by = db.Column(Integer, ForeignKey('admins.id'))
    locked_at = db.Column(Text)
    updated_at = db.Column(Text, nullable=False)
    updated_by = db.Column(Integer, ForeignKey('admins.id'))


class ReceiptDesignVersion(db.Model):
    __tablename__ = 'receipt_design_versions'

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    layout_json = db.Column(Text, nullable=False)
    saved_at = db.Column(Text, nullable=False)
    saved_by = db.Column(Integer, ForeignKey('admins.id'))
