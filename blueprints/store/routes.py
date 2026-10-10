"""The school store: the inventory, purchases waiting to be collected, payments taken in person, and the parents' Store.

Staff (store.view to look, store.manage to run it) keep the inventory: items with a price, a count in stock and up to
three pictures. Each sale takes from the stock; an item can be sold again and again while its count is above nought.

A sale is made one of two ways:

* online, by a parent in their portal, when the school has its own Paystack account. The checkout is the very one a
  fee payment uses (a FinanceOnlinePayment whose ``items`` name store items instead of fees, see
  blueprints/finance/paystack.py): the callback, the webhook and the self-healing reconcile all settle it, exactly
  once, and settling it calls settle_online_purchase() here.
* in person (cash, bank transfer, POS), recorded by staff on "Record a payment". This is also how a school without
  online payments sells.

Either way the purchase then waits on "Purchase claims" until the student or the parent collects it; staff mark it
collected (by whom), and the parent gets a text. Stock is taken with a conditional UPDATE (``stock >= quantity``), so
two sales at once can never sell the last one twice; an online payment that arrives after the last one has gone is
still recorded, marked short, for the office to settle.
"""

import json
from datetime import datetime, timedelta, timezone

from flask import abort, current_app, flash, redirect, render_template, request, session, url_for
from sqlalchemy import func, or_, select, update as sa_update

from app import app, _school_current_session
from core import payments
from core.db_helpers import all_rows, obj, one, one_scalar
from core.short_cache import forget_here, remember
from core.security import admin_has_permission, admin_required, audit_log, csrf_protect, current_admin
from core.uploads import _save_image_upload
from blueprints.parents.helpers import _parent_children, _parent_owns_student, parent_required
from models import (
    FinanceOnlinePayment, ParentAccount, ParentStudentLink, SchoolClass, SchoolNotification, Student,
    StudentEnrolment, StoreItem, StorePurchase, db,
)

METHODS = ('Cash', 'Bank transfer', 'POS')
LOW_STOCK = 5
PER_PAGE = 50


def _now():
    return datetime.now(timezone.utc).isoformat()


def receipt_label(purchase_id):
    return f'STR-{int(purchase_id):06d}'


app.jinja_env.globals.update(store_receipt=receipt_label)


def _money(text):
    try:
        value = round(float(str(text).replace(',', '').strip()), 2)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _whole(text, default=0):
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return default


def _photos(item):
    return [p for p in (item.photo_1, item.photo_2, item.photo_3) if p] if item else []


app.jinja_env.globals.update(store_photos=_photos)


def _student_rows():
    """Every active student with their current class, for the pickers (one query)."""
    current = _school_current_session()
    stmt = (select(Student.id, Student.admission_no, Student.first_name, Student.last_name, SchoolClass.name.label('class_name'))
            .outerjoin(StudentEnrolment, (StudentEnrolment.student_id == Student.id) & (StudentEnrolment.active == 1)
                       & (StudentEnrolment.session_id == (current['id'] if current else -1)))
            .outerjoin(SchoolClass, SchoolClass.id == StudentEnrolment.class_id)
            .where(Student.active == 1).order_by(Student.last_name, Student.first_name))
    seen, out = set(), []
    for r in all_rows(stmt):
        if r['id'] not in seen:
            seen.add(r['id']); out.append(r)
    return out


def _take_stock(item_id, quantity):
    """Take ``quantity`` from an item's stock if, and only if, that many are left. True when taken."""
    taken = db.session.execute(sa_update(StoreItem)
                               .where(StoreItem.id == item_id, StoreItem.stock >= quantity)
                               .values(stock=StoreItem.stock - quantity)).rowcount == 1
    return taken


def _notify_parents(student_id, title, message, url_endpoint='parent_store', admin_id=None):
    """An in-app notice to every parent linked to the student (best effort, never stops a sale)."""
    try:
        for (pid,) in db.session.execute(select(ParentStudentLink.parent_id).where(
                ParentStudentLink.student_id == student_id, ParentStudentLink.active == 1)).all():
            db.session.add(SchoolNotification(
                recipient_type='parent', recipient_id=pid, student_id=student_id, category='store',
                title=title, message=message, action_url=url_for(url_endpoint), created_at=_now(), created_by=admin_id))
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Store notice to parents failed for student %s', student_id)


def _child_name(student_id):
    row = one(select(Student.first_name, Student.last_name, Student.guardian_phone).where(Student.id == student_id))
    return (f"{row['first_name']} {row['last_name']}".strip(), row['guardian_phone']) if row else ('', None)


def _waiting_count():
    return one_scalar(select(func.count()).select_from(StorePurchase).where(StorePurchase.status == 'paid'), 0)


@app.context_processor
def inject_store_waiting():
    """How many purchases wait to be collected, for the badge on Purchase claims. Kept for half a minute and
    dropped whenever a sale, a collection or a cancellation changes it, so the menu costs no query on most pages."""
    if not session.get('admin_id') or not request.path.startswith('/admin') or request.headers.get('X-Fragment') == '1':
        return {'store_waiting': 0}
    try:
        return {'store_waiting': remember('store_waiting', 30, _waiting_count)}
    except Exception:
        db.session.rollback()
        return {'store_waiting': 0}


def _changed():
    forget_here('store_waiting')


# =================================================================== staff: the inventory

@app.route('/admin/store')
@admin_required
def admin_store():
    q = request.args.get('q', '').strip()[:80]
    show = request.args.get('show', 'all')
    if show not in ('all', 'in', 'low', 'out', 'hidden'):
        show = 'all'
    stmt = select(StoreItem).where(StoreItem.active == (0 if show == 'hidden' else 1))
    if q:
        stmt = stmt.where(or_(StoreItem.name.icontains(q, autoescape=True), StoreItem.category.icontains(q, autoescape=True),
                              StoreItem.description.icontains(q, autoescape=True)))
    if show == 'in':
        stmt = stmt.where(StoreItem.stock > 0)
    elif show == 'low':
        stmt = stmt.where(StoreItem.stock > 0, StoreItem.stock <= LOW_STOCK)
    elif show == 'out':
        stmt = stmt.where(StoreItem.stock <= 0)
    items = db.session.scalars(stmt.order_by(StoreItem.name)).all()
    # The figures: one grouped query over the live items, one over this month's sales.
    k = one(select(func.count(StoreItem.id).label('n_items'), func.coalesce(func.sum(StoreItem.stock), 0).label('units'),
                   func.count().filter(StoreItem.stock <= 0).label('out'),
                   func.count().filter(StoreItem.stock > 0, StoreItem.stock <= LOW_STOCK).label('low'))
            .where(StoreItem.active == 1)) or {}
    month = datetime.now(timezone.utc).strftime('%Y-%m')
    sales = one(select(func.count(StorePurchase.id).label('n'), func.coalesce(func.sum(StorePurchase.amount), 0).label('total'))
                .where(StorePurchase.status != 'cancelled', StorePurchase.created_at >= month)) or {}
    waiting = one_scalar(select(func.count()).select_from(StorePurchase).where(StorePurchase.status == 'paid'), 0)
    sold = dict(db.session.execute(select(StorePurchase.item_id, func.coalesce(func.sum(StorePurchase.quantity), 0))
                                   .where(StorePurchase.status != 'cancelled').group_by(StorePurchase.item_id)).all())
    return render_template('store_inventory.html', items=items, q=q, show=show, k=k, sales=sales, waiting=waiting,
                           sold=sold, low=LOW_STOCK, online=payments.payment_settings() is not None)


def _item_form(item=None, errors=None, form=None):
    return render_template('store_item_form.html', item=item, errors=errors or [], form=form or {})


def _save_item(item):
    """Validate and apply the item form to ``item``. Returns a list of what is wrong, or [] when saved."""
    f = request.form
    errors = []
    name = f.get('name', '').strip()[:120]
    price = _money(f.get('price', ''))
    if not name:
        errors.append('Give the item a name.')
    if price is None:
        errors.append('Enter a price above nought, in naira.')
    new = item.id is None
    stock = _whole(f.get('stock', '0'), -1)
    if new and stock < 0:
        errors.append('Enter how many are in stock (0 or more).')
    # Up to three pictures: a new file replaces that slot; "remove" empties it.
    uploads = {}
    for n in (1, 2, 3):
        file = request.files.get(f'photo_{n}')
        if file and file.filename:
            try:
                uploads[n] = _save_image_upload(file, 'store', f'store_{secure_slug(name) or "item"}')
            except ValueError as exc:
                errors.append(f'Picture {n}: {exc}')
    if errors:
        return errors
    item.name, item.price = name, price
    item.category = f.get('category', '').strip()[:60] or None
    item.description = f.get('description', '').strip()[:2000] or None
    if new:
        item.stock = stock
    for n in (1, 2, 3):
        if n in uploads:
            setattr(item, f'photo_{n}', uploads[n])
        elif f.get(f'remove_{n}'):
            setattr(item, f'photo_{n}', None)
    # Keep the pictures in order, so the first one is always the card's.
    kept = [p for p in (item.photo_1, item.photo_2, item.photo_3) if p] + [None, None, None]
    item.photo_1, item.photo_2, item.photo_3 = kept[:3]
    item.updated_at = _now()
    return []


def secure_slug(text):
    from werkzeug.utils import secure_filename
    return secure_filename(text.lower().replace(' ', '_'))[:40]


@app.route('/admin/store/items/new', methods=['GET', 'POST'])
@admin_required
@csrf_protect
def admin_store_item_new():
    if request.method == 'POST':
        item = StoreItem(created_at=_now(), created_by=current_admin()['id'], active=1)
        errors = _save_item(item)
        if errors:
            db.session.rollback()
            return _item_form(None, errors, request.form)
        db.session.add(item); db.session.commit()
        audit_log('store_item_created', 'store', 'item', item.id, {'name': item.name, 'price': item.price, 'stock': item.stock})
        flash(f'{item.name} is in the store, with {item.stock} in stock.', 'success')
        return redirect(url_for('admin_store'))
    return _item_form()


@app.route('/admin/store/items/<int:item_id>/edit', methods=['GET', 'POST'])
@admin_required
@csrf_protect
def admin_store_item_edit(item_id):
    item = obj(StoreItem, item_id)
    if not item:
        abort(404)
    if request.method == 'POST':
        errors = _save_item(item)
        if errors:
            db.session.rollback()
            return _item_form(obj(StoreItem, item_id), errors, request.form)
        db.session.commit()
        audit_log('store_item_updated', 'store', 'item', item.id, {'name': item.name, 'price': item.price})
        flash(f'{item.name} saved.', 'success')
        return redirect(url_for('admin_store'))
    return _item_form(item)


@app.post('/admin/store/items/<int:item_id>/restock')
@admin_required
@csrf_protect
def admin_store_item_restock(item_id):
    """Add to (or, with a minus, take from) an item's stock; it never goes below nought."""
    item = obj(StoreItem, item_id)
    if not item:
        abort(404)
    change = _whole(request.form.get('change', '0'))
    if not change:
        flash('Enter how many to add (or, for a correction, a minus number to take away).', 'error')
        return redirect(url_for('admin_store'))
    before = item.stock
    item.stock = max(0, before + change)
    item.updated_at = _now()
    db.session.commit()
    audit_log('store_stock_changed', 'store', 'item', item.id, {'name': item.name, 'from': before, 'to': item.stock})
    flash(f'{item.name}: {before} → {item.stock} in stock.', 'success')
    return redirect(url_for('admin_store', **{k: v for k, v in request.args.items() if k in ('q', 'show')}))


@app.post('/admin/store/items/<int:item_id>/toggle')
@admin_required
@csrf_protect
def admin_store_item_toggle(item_id):
    item = obj(StoreItem, item_id)
    if not item:
        abort(404)
    item.active = 0 if item.active else 1
    item.updated_at = _now()
    db.session.commit()
    audit_log('store_item_hidden' if not item.active else 'store_item_shown', 'store', 'item', item.id, {'name': item.name})
    flash(f'{item.name} is {"back in" if item.active else "hidden from"} the store.', 'success')
    return redirect(url_for('admin_store', show='hidden' if item.active else None))


# =================================================================== staff: selling in person

@app.route('/admin/store/sell', methods=['GET', 'POST'])
@admin_required
@csrf_protect
def admin_store_sell():
    """Record a payment taken in person (cash, transfer, POS): the item, how many, for which student. Works the same
    for a school with or without online payments; the stock goes down at once."""
    items = db.session.scalars(select(StoreItem).where(StoreItem.active == 1).order_by(StoreItem.name)).all()
    errors, form = [], request.form if request.method == 'POST' else {}
    if request.method == 'POST':
        item = obj(StoreItem, _whole(request.form.get('item_id')))
        quantity = _whole(request.form.get('quantity', '1'), 0)
        student = obj(Student, _whole(request.form.get('student_id')))
        method = request.form.get('method', '')
        reference = request.form.get('reference', '').strip()[:80]
        if not item or not item.active:
            errors.append('Choose the item sold.')
        if quantity < 1:
            errors.append('Enter how many were bought (1 or more).')
        if not student or not student.active:
            errors.append('Choose the student it is for.')
        if method not in METHODS:
            errors.append('Choose how it was paid.')
        if method in ('Bank transfer', 'POS') and not reference:
            errors.append('Enter the transfer or POS reference, so the payment can be traced.')
        if not errors:
            if not _take_stock(item.id, quantity):
                db.session.rollback()
                errors.append(f'Only {obj(StoreItem, item.id).stock} of {item.name} left in stock.')
        if not errors:
            me = current_admin()
            purchase = StorePurchase(item_id=item.id, item_name=item.name, unit_price=item.price, quantity=quantity,
                                     amount=round(item.price * quantity, 2), student_id=student.id, method=method,
                                     reference=reference or None, recorded_by=me['id'], status='paid',
                                     notes=request.form.get('notes', '').strip()[:500] or None, created_at=_now())
            db.session.add(purchase); db.session.commit(); _changed()
            audit_log('store_payment_recorded', 'store', 'purchase', purchase.id,
                      {'item': item.name, 'quantity': quantity, 'amount': purchase.amount, 'method': method, 'student_id': student.id})
            child = f'{student.first_name} {student.last_name}'.strip()
            _notify_parents(student.id, f'Store: {item.name} for {child}',
                            f'{quantity} × {item.name} paid ({method}), ₦{purchase.amount:,.2f}. Collect it from the school. '
                            f'Receipt {receipt_label(purchase.id)}.', admin_id=me['id'])
            flash(f'Payment recorded: {quantity} × {item.name} for {child}. Receipt {receipt_label(purchase.id)}.', 'success')
            return redirect(url_for('admin_store_purchase', purchase_id=purchase.id))
    return render_template('store_sell.html', items=items, students=_student_rows(), errors=errors, form=form,
                           methods=METHODS, item_id=_whole(request.args.get('item_id')) or _whole(form.get('item_id')))


# =================================================================== staff: purchases and claims

@app.route('/admin/store/claims')
@admin_required
def admin_store_claims():
    status = request.args.get('status', 'paid')
    if status not in ('paid', 'claimed', 'cancelled', 'all'):
        status = 'paid'
    q = request.args.get('q', '').strip()[:80]
    page = max(1, _whole(request.args.get('page', '1'), 1))
    base = (select(StorePurchase, Student.first_name, Student.last_name, Student.admission_no)
            .join(Student, Student.id == StorePurchase.student_id))
    if status != 'all':
        base = base.where(StorePurchase.status == status)
    if q:
        like = [Student.first_name.icontains(q, autoescape=True), Student.last_name.icontains(q, autoescape=True),
                Student.admission_no.icontains(q, autoescape=True), StorePurchase.item_name.icontains(q, autoescape=True),
                StorePurchase.reference.icontains(q, autoescape=True)]
        digits = q.upper().replace('STR-', '').lstrip('0')
        if digits.isdigit():
            like.append(StorePurchase.id == int(digits))
        base = base.where(or_(*like))
    total = one_scalar(select(func.count()).select_from(base.subquery()), 0)
    pages = max(1, -(-total // PER_PAGE))
    page = min(page, pages)
    rows = db.session.execute(base.order_by(StorePurchase.id.desc()).limit(PER_PAGE).offset((page - 1) * PER_PAGE)).all()
    purchases = [{'p': r[0], 'first_name': r[1], 'last_name': r[2], 'admission_no': r[3]} for r in rows]
    counts = dict(db.session.execute(select(StorePurchase.status, func.count()).group_by(StorePurchase.status)).all())
    short = one_scalar(select(func.count()).select_from(StorePurchase).where(StorePurchase.status == 'paid', StorePurchase.short == 1), 0)
    week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    collected_week = one_scalar(select(func.count()).select_from(StorePurchase)
                                .where(StorePurchase.status == 'claimed', StorePurchase.claimed_at >= week), 0)
    return render_template('store_claims.html', purchases=purchases, status=status, q=q, page=page, pages=pages,
                           total=total, counts=counts, short=short, collected_week=collected_week)


def _purchase_context(purchase):
    student = one(select(Student.id, Student.first_name, Student.last_name, Student.admission_no).where(Student.id == purchase.student_id))
    parents = all_rows(select(ParentAccount.id, ParentAccount.display_name, ParentStudentLink.relationship)
                       .join(ParentStudentLink, ParentStudentLink.parent_id == ParentAccount.id)
                       .where(ParentStudentLink.student_id == purchase.student_id, ParentStudentLink.active == 1))
    names = {}
    for aid in {purchase.recorded_by, purchase.claimed_marked_by, purchase.cancelled_by} - {None}:
        from models import Admin
        names[aid] = one_scalar(select(Admin.display_name).where(Admin.id == aid))
    item = obj(StoreItem, purchase.item_id)
    buyer = one_scalar(select(ParentAccount.display_name).where(ParentAccount.id == purchase.parent_id)) if purchase.parent_id else None
    return dict(purchase=purchase, student=student, parents=parents, staff=names, item=item, buyer=buyer)


@app.route('/admin/store/purchases/<int:purchase_id>')
@admin_required
def admin_store_purchase(purchase_id):
    purchase = obj(StorePurchase, purchase_id)
    if not purchase:
        abort(404)
    return render_template('store_purchase.html', **_purchase_context(purchase),
                           can_manage=admin_has_permission(current_admin()['id'], 'store.manage'))


@app.post('/admin/store/purchases/<int:purchase_id>/claim')
@admin_required
@csrf_protect
def admin_store_purchase_claim(purchase_id):
    """Hand over a purchase: who collected it (the student, or a parent), and the parent is told by text."""
    purchase = obj(StorePurchase, purchase_id)
    if not purchase:
        abort(404)
    who = request.form.get('claimed_by_type', '')
    name = request.form.get('claimed_by_name', '').strip()[:120]
    if who not in ('student', 'parent'):
        flash('Choose who collected it: the student or a parent.', 'error')
        return redirect(url_for('admin_store_purchase', purchase_id=purchase_id))
    child, phone = _child_name(purchase.student_id)
    if who == 'student':
        name = child
    if not name:
        flash('Enter the name of the parent who collected it.', 'error')
        return redirect(url_for('admin_store_purchase', purchase_id=purchase_id))
    me = current_admin()
    done = db.session.execute(sa_update(StorePurchase).where(StorePurchase.id == purchase_id, StorePurchase.status == 'paid')
                              .values(status='claimed', claimed_at=_now(), claimed_by_type=who, claimed_by_name=name,
                                      claimed_marked_by=me['id'])).rowcount == 1
    db.session.commit()
    if not done:
        flash('This purchase is not waiting to be collected any more.', 'error')
        return redirect(url_for('admin_store_purchase', purchase_id=purchase_id))
    _changed()
    audit_log('store_purchase_claimed', 'store', 'purchase', purchase_id, {'by': who, 'name': name, 'item': purchase.item_name})
    collector = f'{child}' if who == 'student' else name
    _notify_parents(purchase.student_id, f'Collected: {purchase.item_name}',
                    f'{purchase.quantity} × {purchase.item_name} for {child} was collected by {collector}.', admin_id=me['id'])
    from core.branding import school_name
    from core.notifications import _notify_parents_sms
    text = (f'{school_name()}: {purchase.quantity} x {purchase.item_name} bought for {child} was collected by '
            f'{collector} ({"the student" if who == "student" else "parent"}). Receipt {receipt_label(purchase_id)}.')
    try:
        _notify_parents_sms(purchase.student_id, phone, text, kind='store_claimed')
    except Exception:
        current_app.logger.exception('Store collection SMS failed for purchase %s', purchase_id)
    flash(f'Marked collected by {collector}. The parent has been sent a text.', 'success')
    return redirect(url_for('admin_store_purchase', purchase_id=purchase_id))


@app.post('/admin/store/purchases/<int:purchase_id>/cancel')
@admin_required
@csrf_protect
def admin_store_purchase_cancel(purchase_id):
    """Undo a purchase not yet collected (recorded by mistake, or refunded by hand): what it took goes back on the shelf."""
    purchase = obj(StorePurchase, purchase_id)
    if not purchase:
        abort(404)
    reason = request.form.get('reason', '').strip()[:300]
    if not reason:
        flash('Give a reason for cancelling.', 'error')
        return redirect(url_for('admin_store_purchase', purchase_id=purchase_id))
    me = current_admin()
    done = db.session.execute(sa_update(StorePurchase).where(StorePurchase.id == purchase_id, StorePurchase.status == 'paid')
                              .values(status='cancelled', cancelled_at=_now(), cancelled_by=me['id'], cancel_reason=reason)).rowcount == 1
    if done and not purchase.short:
        db.session.execute(sa_update(StoreItem).where(StoreItem.id == purchase.item_id)
                           .values(stock=StoreItem.stock + purchase.quantity))
    db.session.commit()
    if not done:
        flash('Only a purchase still waiting to be collected can be cancelled.', 'error')
    else:
        _changed()
        audit_log('store_purchase_cancelled', 'store', 'purchase', purchase_id, {'reason': reason, 'item': purchase.item_name})
        flash('Cancelled' + ('' if purchase.short else f', and {purchase.quantity} put back in stock') + '.'
              + (' Refund the parent through Paystack or by hand.' if purchase.method == 'Paystack' else ''), 'success')
    return redirect(url_for('admin_store_purchase', purchase_id=purchase_id))


# =================================================================== parents: the Store

def _parent_child_rows(pid):
    """The parent's children still at the school (an archived child is not bought for)."""
    return [c for c in _parent_children(pid) if c.get('active', 1)]


@app.route('/parent/store')
@parent_required
def parent_store():
    pid = session['parent_id']
    settings = payments.payment_settings()
    if settings is not None:
        from blueprints.finance.paystack import reconcile_pending
        try:
            reconcile_pending()
        except Exception:
            current_app.logger.exception('Online-payment reconciliation failed while a parent opened the store')
    children = _parent_child_rows(pid)
    items = db.session.scalars(select(StoreItem).where(StoreItem.active == 1).order_by(StoreItem.name)).all()
    ids = [c['id'] for c in children]
    bought = []
    if ids:
        bought = [{'p': r[0], 'first_name': r[1]} for r in db.session.execute(
            select(StorePurchase, Student.first_name).join(Student, Student.id == StorePurchase.student_id)
            .where(StorePurchase.student_id.in_(ids), StorePurchase.status != 'cancelled')
            .order_by(StorePurchase.id.desc()).limit(30)).all()]
    return render_template('parent_store.html', items=items, children=children, bought=bought,
                           online=settings is not None)


@app.post('/parent/store/buy')
@parent_required
@csrf_protect
def parent_store_buy():
    pid = session['parent_id']
    settings = payments.payment_settings()
    if settings is None:
        flash('The school takes payment for the store at the office. You can see what is available here.', 'error')
        return redirect(url_for('parent_store'))
    item = obj(StoreItem, _whole(request.form.get('item_id')))
    if not item or not item.active:
        flash('That item is no longer in the store.', 'error')
        return redirect(url_for('parent_store'))
    lines = []
    for key, value in request.form.items():
        if key.startswith('qty_'):
            sid, qty = _whole(key[4:]), _whole(value)
            if qty > 0:
                if qty > 50 or not _parent_owns_student(pid, sid):
                    abort(400)
                lines.append({'store_item_id': item.id, 'student_id': sid, 'qty': qty})
    if not lines:
        flash('Choose how many for at least one child.', 'error')
        return redirect(url_for('parent_store'))
    wanted = sum(line['qty'] for line in lines)
    if wanted > item.stock:
        flash(f'Only {item.stock} of {item.name} left. Choose fewer.' if item.stock else f'{item.name} is sold out.', 'error')
        return redirect(url_for('parent_store'))
    current = _school_current_session()
    if not current:
        flash('The store is not open yet: the school has no current session.', 'error')
        return redirect(url_for('parent_store'))
    amount = round(item.price * wanted, 2)
    parent = obj(ParentAccount, pid)
    email = (parent.email or '').strip()
    if not email:
        host = request.host.split(':')[0]
        email = f'guardian{pid}@{host if "." in host else "example.com"}'
    reference = payments.new_reference()
    db.session.add(FinanceOnlinePayment(reference=reference, student_id=lines[0]['student_id'], parent_id=pid,
                                        session_id=current['id'], amount=amount, status='pending', created_at=_now(),
                                        items=json.dumps(lines)))
    db.session.commit()
    try:
        authorization_url = payments.initialize_transaction(settings, email, amount, reference,
                                                            url_for('parent_store_pay_callback', _external=True))
    except payments.PaystackError as exc:
        flash(f'Could not start the payment: {exc.detail}', 'error')
        return redirect(url_for('parent_store'))
    return redirect(authorization_url)


@app.route('/parent/store/pay/callback')
@parent_required
def parent_store_pay_callback():
    from blueprints.finance.paystack import _finalize
    pid = session['parent_id']
    reference = request.args.get('reference', '').strip()
    row = db.session.scalars(select(FinanceOnlinePayment).where(FinanceOnlinePayment.reference == reference,
                                                                FinanceOnlinePayment.parent_id == pid)).first()
    if not row:
        abort(404)
    settings = payments.payment_settings()
    if settings is not None and row.status == 'pending':
        _finalize(settings, row)
    if row.status == 'success':
        flash('Payment received. Collect your purchase from the school: it is listed under Your purchases.', 'success')
    elif row.status == 'failed':
        flash(f'This payment was not successful{": " + row.failure_reason if row.failure_reason else ""}.', 'error')
    else:
        flash('This payment is still being confirmed. Refresh in a moment.', 'error')
    return redirect(url_for('parent_store'))


def store_lines(row):
    """The store items an online payment was started for (empty for a fee payment)."""
    try:
        items = json.loads(row.items) if row.items else []
    except ValueError:
        return []
    return [i for i in items if isinstance(i, dict) and i.get('store_item_id')]


def settle_online_purchase(row, lines, paid_naira, now):
    """A confirmed online store payment becomes one purchase per child, each taking from the stock.

    Called once per payment by paystack._finalize, after Paystack has confirmed it. If the stock ran out between
    checkout and payment, the purchase is still recorded (the money is in) and marked short for the office."""
    created = []
    for line in lines:
        item = obj(StoreItem, line['store_item_id'])
        qty = int(line['qty'])
        if not item:
            continue
        short = not _take_stock(item.id, qty)
        purchase = StorePurchase(item_id=item.id, item_name=item.name, unit_price=item.price, quantity=qty,
                                 amount=round(item.price * qty, 2), student_id=int(line['student_id']), parent_id=row.parent_id,
                                 method='Paystack', reference=row.reference, online_payment_id=row.id, status='paid',
                                 short=1 if short else 0, created_at=now,
                                 notes='Paid online by the parent through Paystack.')
        db.session.add(purchase)
        created.append(purchase)
    row.status = 'success'
    db.session.commit()
    _changed()
    for p in created:
        audit_log('store_payment_recorded', 'store', 'purchase', p.id,
                  {'item': p.item_name, 'quantity': p.quantity, 'amount': p.amount, 'method': 'Paystack',
                   'student_id': p.student_id, 'online_payment_id': row.id, 'short': p.short})
        child, _ = _child_name(p.student_id)
        _notify_parents(p.student_id, f'Store: {p.item_name} for {child}',
                        f'{p.quantity} × {p.item_name} paid online, ₦{p.amount:,.2f}. Collect it from the school. '
                        f'Receipt {receipt_label(p.id)}.')
    return created
