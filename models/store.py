"""The school store: items parents can buy for their children, and every purchase until it is collected.

An item (StoreItem) has a price and a count in stock; it can be bought again and again while the count is above
nought. A purchase (StorePurchase) is one parent's (or the office's, for cash) purchase of some of an item for one
child. It is made either online, through the school's own Paystack account (it then arrives through the same
FinanceOnlinePayment checkout a fee payment uses, see blueprints/store/routes.py), or recorded by staff for money
taken in person. From then on it waits to be collected: staff mark it claimed, by the student or the parent, and the
parent is told by SMS.

Store money is kept apart from school fees: a purchase is not a FinancePayment, so it never shows as a fee payment
"not yet applied to a fee". Its own receipt number (STR-000001) comes from ``StorePurchase.id``.
"""

from sqlalchemy import Float, ForeignKey, Index, Integer, Text, text

from .base import db


class StoreItem(db.Model):
    __tablename__ = 'store_items'
    __table_args__ = (Index('idx_store_items_active', 'active', 'name'),)

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    name = db.Column(Text, nullable=False)
    description = db.Column(Text)
    category = db.Column(Text)
    price = db.Column(Float, nullable=False)
    stock = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    # Up to three pictures: uploads/store/<name>. The first is the one shown on the item's card.
    photo_1 = db.Column(Text)
    photo_2 = db.Column(Text)
    photo_3 = db.Column(Text)
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    created_at = db.Column(Text, nullable=False)
    created_by = db.Column(Integer, ForeignKey('admins.id'))
    updated_at = db.Column(Text)


class StorePurchase(db.Model):
    __tablename__ = 'store_purchases'
    __table_args__ = (
        Index('idx_store_purchases_status', 'status', 'created_at'),
        Index('idx_store_purchases_student', 'student_id'),
        Index('idx_store_purchases_item', 'item_id'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    item_id = db.Column(Integer, ForeignKey('store_items.id'), nullable=False)
    # The item's name and price as they were when it was bought, so a later edit never rewrites history.
    item_name = db.Column(Text, nullable=False)
    unit_price = db.Column(Float, nullable=False)
    quantity = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    amount = db.Column(Float, nullable=False)
    student_id = db.Column(Integer, ForeignKey('students.id', ondelete='CASCADE'), nullable=False)
    parent_id = db.Column(Integer, ForeignKey('parent_accounts.id', ondelete='SET NULL'))
    # 'Paystack' for an online purchase; otherwise how the office was paid (Cash, Bank transfer, POS).
    method = db.Column(Text, nullable=False)
    reference = db.Column(Text)
    online_payment_id = db.Column(Integer, ForeignKey('finance_online_payments.id', ondelete='SET NULL'))
    recorded_by = db.Column(Integer, ForeignKey('admins.id'))
    # paid -> claimed, or paid -> cancelled (stock returned). ``short`` is set when an online payment arrived for
    # more than was left in stock: the money is in, the item is owed, and the office decides what to do.
    status = db.Column(Text, nullable=False, default='paid', server_default=text("'paid'"))
    short = db.Column(Integer, nullable=False, default=0, server_default=text('0'))
    notes = db.Column(Text)
    created_at = db.Column(Text, nullable=False)
    claimed_at = db.Column(Text)
    claimed_by_type = db.Column(Text)      # 'student' or 'parent'
    claimed_by_name = db.Column(Text)
    claimed_marked_by = db.Column(Integer, ForeignKey('admins.id'))
    cancelled_at = db.Column(Text)
    cancelled_by = db.Column(Integer, ForeignKey('admins.id'))
    cancel_reason = db.Column(Text)
