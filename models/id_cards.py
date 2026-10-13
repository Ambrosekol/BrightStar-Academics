"""Student and staff ID cards: the school's card design, and every card issued.

The design (one per kind) is a small layout - each element's type, position and size on the card, in millimetres -
checked strictly by core/id_cards.py before it is saved, and locked by someone with the right to manage cards so it
cannot change by accident. A card is issued to a person the first time it is printed, with a random token for the QR
code on it; the token opens a public verification page. A lost card is cancelled (its QR then says so) and a new one
issued. See models/__init__.py for the column conventions.
"""

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint, text

from .base import db


class IdCardDesign(db.Model):
    __tablename__ = 'id_card_designs'
    __table_args__ = (UniqueConstraint('kind'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    kind = db.Column(Text, nullable=False)              # 'student' or 'staff'
    layout_json = db.Column(Text, nullable=False)
    locked = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    locked_by = db.Column(Integer, ForeignKey('admins.id'))
    locked_at = db.Column(Text)
    updated_at = db.Column(Text, nullable=False)
    updated_by = db.Column(Integer, ForeignKey('admins.id'))


class IdCard(db.Model):
    __tablename__ = 'id_cards'
    __table_args__ = (UniqueConstraint('token'), Index('idx_id_cards_person', 'kind', 'person_id', 'status'))

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    kind = db.Column(Text, nullable=False)              # 'student' (students.id) or 'staff' (admins.id)
    person_id = db.Column(Integer, nullable=False)
    token = db.Column(Text, nullable=False)             # random, for the QR code; never a number someone could guess
    serial = db.Column(Text, nullable=False)            # printed on the card, e.g. STU-000042
    status = db.Column(Text, nullable=False, default='active', server_default=text("'active'"))
    issued_at = db.Column(Text, nullable=False)
    issued_by = db.Column(Integer, ForeignKey('admins.id'))
    printed_count = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    cancelled_at = db.Column(Text)
    cancelled_by = db.Column(Integer, ForeignKey('admins.id'))
    cancel_reason = db.Column(Text)
