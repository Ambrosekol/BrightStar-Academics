"""Design studios: one menu entry for the school's two design tools, and a page to choose between them - the ID card
studio (blueprints/school/id_cards.py, needs id_cards.manage) and the report card studio
(blueprints/school/report_card_studio.py, needs report_cards.manage) and the receipt studio
(blueprints/school/receipt_studio.py, needs finance.manage). Each card on the page is shown only to someone
who may open that studio; the studios check the permission again themselves. Someone with neither is refused.

Designing needs a computer. A phone (told apart by its browser's own description of itself) is answered with a page
asking for one instead of a studio (phone_notice); a small screen of any other kind sees the same message drawn over
the studio by its stylesheet. Saving is not refused this way: the guard is about a usable screen, not about access.
"""

import re

from flask import render_template, request
from sqlalchemy import select

from app import app
from core.security import admin_access_error, admin_has_permission, admin_required, current_admin, may_use_studios
from models import IdCardDesign, ReceiptDesign, ReportCardDesign, db


PHONE = re.compile(r'iPhone|iPod|Android.+Mobile|Windows Phone|IEMobile|BlackBerry|BB10|Opera Mini|Mobile Safari(?!.*iPad)|webOS', re.I)


def phone_notice():
    """The "please use a computer" page when the request comes from a phone, else None."""
    if PHONE.search(request.headers.get('User-Agent', '')[:400]):
        return render_template('school_studio_phone.html')
    return None


def _may():
    """(ID cards, report cards, receipts): which studios the person signed in may open."""
    admin = current_admin()
    if not admin:
        return False, False, False
    return (may_use_studios(admin['id'], 'id_cards.manage'), may_use_studios(admin['id'], 'report_cards.manage'),
            may_use_studios(admin['id'], 'finance.manage'))


@app.route('/admin/design-studios')
@admin_required
def admin_design_studios():
    id_cards, report_cards, receipts = _may()
    if not (id_cards or report_cards or receipts):
        return admin_access_error('design studios')
    notice = phone_notice()
    if notice:
        return notice
    cards = {kind: (locked, bool(saved)) for kind, locked, saved in
             db.session.execute(select(IdCardDesign.kind, IdCardDesign.locked, IdCardDesign.updated_at)).all()} if id_cards else {}
    report = db.session.execute(select(ReportCardDesign.in_use, ReportCardDesign.locked)
                                .order_by(ReportCardDesign.id).limit(1)).first() if report_cards else None
    receipt = db.session.execute(select(ReceiptDesign.in_use, ReceiptDesign.locked)
                                 .order_by(ReceiptDesign.id).limit(1)).first() if receipts else None
    return render_template('school_design_studios.html', id_cards=id_cards, report_cards=report_cards, receipts=receipts,
                           cards=cards, report=report, receipt=receipt)


def design_studios_shown():
    return any(_may())


def studio_allowed(permission):
    """For a page's link into a studio: the studio's permission, and not teaching staff (core/security.py)."""
    admin = current_admin()
    return bool(admin and may_use_studios(admin['id'], permission))


app.jinja_env.globals.update(design_studios_shown=design_studios_shown, studio_allowed=studio_allowed)
