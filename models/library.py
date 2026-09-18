"""Library: books and loans."""

from sqlalchemy import ForeignKey, Index, Integer, Text, text

from .base import db


class LibraryBook(db.Model):
    __tablename__ = 'library_books'

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    isbn = db.Column(Text)
    title = db.Column(Text, nullable=False)
    author = db.Column(Text)
    publisher = db.Column(Text)
    publication_year = db.Column(Integer)
    category = db.Column(Text)
    shelf = db.Column(Text)
    total_copies = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    available_copies = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    active = db.Column(Integer, nullable=False, default=1, server_default=text('1'))
    created_at = db.Column(Text, nullable=False)
    created_by = db.Column(Integer, ForeignKey('admins.id'))


class LibraryLoan(db.Model):
    __tablename__ = 'library_loans'
    __table_args__ = (
        Index('idx_library_loans_book', 'book_id', 'status'),
        Index('idx_library_loans_member', 'member_type', 'member_id', 'status'),
    )

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    book_id = db.Column(Integer, ForeignKey('library_books.id'), nullable=False)
    member_type = db.Column(Text, nullable=False)
    member_id = db.Column(Integer, nullable=False)
    borrowed_at = db.Column(Text, nullable=False)
    due_at = db.Column(Text)
    returned_at = db.Column(Text)
    status = db.Column(Text, nullable=False, server_default=text('"borrowed"'))
    notes = db.Column(Text)
    issued_by = db.Column(Integer, ForeignKey('admins.id'))
    received_by = db.Column(Integer, ForeignKey('admins.id'))
