"""Admin library management: books, availability, and staff/student loans."""

from datetime import datetime, timedelta, timezone

from flask import abort, flash, redirect, render_template, request, url_for
from sqlalchemy import and_, func, or_, select, update as sa_update

from app import app
from models import Admin, LibraryBook, LibraryLoan, Student, db
from core.db_helpers import all_rows, obj, one_scalar, _flatten
from core.security import admin_required, audit_log, current_admin, csrf_protect


@app.route('/admin/library')
@admin_required
def admin_library():
    q=request.args.get('q','').strip(); status=request.args.get('status','all')
    stmt=select(LibraryBook).where(LibraryBook.active==1)
    if q:
        like=f'%{q}%'
        stmt=stmt.where(or_(LibraryBook.title.like(like),LibraryBook.author.like(like),
                            LibraryBook.isbn.like(like),LibraryBook.category.like(like)))
    if status=='available':
        stmt=stmt.where(LibraryBook.available_copies>0)
    books=db.session.scalars(stmt.order_by(LibraryBook.title)).all()
    borrowed=one_scalar(select(func.count()).select_from(LibraryLoan)
                        .where(LibraryLoan.status=='borrowed'),0)
    overdue=one_scalar(select(func.count()).select_from(LibraryLoan)
                       .where(LibraryLoan.status=='borrowed',
                              LibraryLoan.due_at.is_not(None),
                              LibraryLoan.due_at<func.date('now')),0)
    students=all_rows(select(Student.id,Student.admission_no,Student.first_name,
                             Student.middle_name,Student.last_name)
                      .where(Student.active==1)
                      .order_by(Student.last_name,Student.first_name))
    staff=all_rows(select(Admin.id,Admin.display_name,Admin.username)
                   .where(Admin.active==1).order_by(Admin.display_name))
    loans=[_flatten(r,'LibraryLoan','title','first_name','last_name','admission_no','staff_name')
           for r in all_rows(
        select(LibraryLoan,LibraryBook.title,Student.first_name,Student.last_name,
               Student.admission_no,Admin.display_name.label('staff_name'))
        .join(LibraryBook,LibraryBook.id==LibraryLoan.book_id)
        .outerjoin(Student,and_(LibraryLoan.member_type=='student',
                                Student.id==LibraryLoan.member_id))
        .outerjoin(Admin,and_(LibraryLoan.member_type=='staff',
                              Admin.id==LibraryLoan.member_id))
        .where(LibraryLoan.status=='borrowed')
        .order_by(LibraryLoan.due_at,LibraryLoan.id.desc()).limit(30))]
    return render_template('library_dashboard.html',books=books,borrowed=borrowed,overdue=overdue,q=q,status=status,students=students,staff=staff,loans=loans)

@app.post('/admin/library/books/new')
@admin_required
@csrf_protect
def admin_library_book_new():
    title=request.form.get('title','').strip(); author=request.form.get('author','').strip(); isbn=request.form.get('isbn','').strip(); publisher=request.form.get('publisher','').strip(); category=request.form.get('category','').strip(); shelf=request.form.get('shelf','').strip()
    try: year=int(request.form.get('publication_year','') or 0); copies=max(1,int(request.form.get('copies','1') or 1))
    except (TypeError,ValueError): year=0; copies=1
    if not title: flash('Book title is required.','error'); return redirect(url_for('admin_library'))
    db.session.add(LibraryBook(isbn=isbn,title=title,author=author,publisher=publisher,
        publication_year=year or None,category=category,shelf=shelf,
        total_copies=copies,available_copies=copies,active=1,
        created_at=datetime.now(timezone.utc).isoformat(),created_by=current_admin()['id']))
    db.session.commit()
    audit_log('library_book_created','library','book',title,{'copies':copies}); flash('Book added to the library.','success'); return redirect(url_for('admin_library'))

@app.route('/admin/library/books/<int:book_id>/edit',methods=['GET','POST'])
@admin_required
@csrf_protect
def admin_library_book_edit(book_id):
    row=obj(LibraryBook,book_id)
    if not row: abort(404)
    errors=[]
    if request.method=='POST':
        title=request.form.get('title','').strip(); author=request.form.get('author','').strip(); isbn=request.form.get('isbn','').strip(); publisher=request.form.get('publisher','').strip(); category=request.form.get('category','').strip(); shelf=request.form.get('shelf','').strip()
        try: year=int(request.form.get('publication_year','') or 0); total=int(request.form.get('total_copies','1') or 1)
        except (TypeError,ValueError): year=0; total=0
        borrowed=int(row.total_copies)-int(row.available_copies)
        if not title: errors.append('Book title is required.')
        if total<borrowed: errors.append(f'Total copies cannot be below {borrowed}, because that many copies are currently on loan.')
        if total<0: errors.append('Total copies cannot be negative.')
        if not errors:
            available=total-borrowed
            row.title=title; row.author=author; row.isbn=isbn; row.publisher=publisher
            row.publication_year=year or None; row.category=category; row.shelf=shelf
            row.total_copies=total; row.available_copies=available
            db.session.commit()
            audit_log('library_book_updated','library','book',book_id,{'total_copies':total,'available_copies':available})
            flash('Library book details updated.','success'); return redirect(url_for('admin_library'))
    form={c.key:getattr(row,c.key) for c in row.__mapper__.column_attrs}
    if request.method=='POST': form.update(request.form)
    return render_template('library_book_form.html',book=form,errors=errors)

@app.post('/admin/library/books/<int:book_id>/toggle')
@admin_required
@csrf_protect
def admin_library_book_toggle(book_id):
    row=obj(LibraryBook,book_id)
    if not row: abort(404)
    new=0 if row.active else 1
    if not new and int(row.available_copies) != int(row.total_copies):
        flash('Return all copies before deactivating this book.','error'); return redirect(url_for('admin_library'))
    row.active=new; db.session.commit()
    audit_log('library_book_status_changed','library','book',book_id,{'active':new}); flash('Book '+('activated.' if new else 'archived.'),'success'); return redirect(url_for('admin_library'))

@app.post('/admin/library/issue')
@admin_required
@csrf_protect
def admin_library_issue():
    try: book_id=int(request.form.get('book_id','')); member_id=int(request.form.get('member_id','')); days=max(1,int(request.form.get('days','14') or 14))
    except (TypeError,ValueError): book_id=member_id=0; days=14
    member_type=request.form.get('member_type','student').strip()
    book=db.session.scalars(select(LibraryBook).where(
        LibraryBook.id==book_id,LibraryBook.active==1)).first()
    if not book or book.available_copies<1: flash('That book is not currently available.','error'); return redirect(url_for('admin_library'))
    if member_type=='student':
        member=one_scalar(select(Student.id).where(Student.id==member_id,Student.active==1))
    else:
        member=one_scalar(select(Admin.id).where(Admin.id==member_id,Admin.active==1))
    if not member: flash('Library member not found.','error'); return redirect(url_for('admin_library'))
    now=datetime.now(timezone.utc); due=(now+timedelta(days=days)).date().isoformat()
    loan=LibraryLoan(book_id=book_id,member_type=member_type,member_id=member_id,
                     borrowed_at=now.isoformat(),due_at=due,status='borrowed',
                     issued_by=current_admin()['id'])
    db.session.add(loan)
    book.available_copies=book.available_copies-1
    db.session.commit()
    audit_log('library_book_issued','library','loan',loan.id,{'book_id':book_id,'member_type':member_type,'member_id':member_id}); flash('Book issued successfully.','success'); return redirect(url_for('admin_library'))

@app.post('/admin/library/loans/<int:loan_id>/return')
@admin_required
@csrf_protect
def admin_library_return(loan_id):
    loan=db.session.scalars(select(LibraryLoan).where(
        LibraryLoan.id==loan_id,LibraryLoan.status=='borrowed')).first()
    if not loan: abort(404)
    now=datetime.now(timezone.utc).isoformat()
    loan.status='returned'; loan.returned_at=now; loan.received_by=current_admin()['id']
    db.session.execute(sa_update(LibraryBook).where(LibraryBook.id==loan.book_id)
        .values(available_copies=func.min(LibraryBook.total_copies,
                                          LibraryBook.available_copies+1)))
    db.session.commit()
    audit_log('library_book_returned','library','loan',loan_id,{'book_id':loan.book_id}); flash('Book returned successfully.','success'); return redirect(url_for('admin_library'))
