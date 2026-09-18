"""Declarative base and the shared SQLAlchemy instance.

Every model module in this package imports ``db`` from here so they all
register against the same ``Base.metadata`` — required for
``db.create_all()`` and for cross-module ``ForeignKey('table_name.column')``
references (which SQLAlchemy resolves by table name at mapper-configuration
time, not by Python import order) to see every table.
"""

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base that keeps ``sqlite3.Row``-compatible access.

    The application and its Jinja templates were written against ``sqlite3.Row``
    and use ``row['column']`` and ``'column' in row.keys()`` in a number of
    places. Supporting the mapping protocol here means the ORM migration does
    not have to rewrite every template and every defensive column check.
    """

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return getattr(self, key)
            except AttributeError as exc:  # pragma: no cover - defensive
                raise KeyError(key) from exc
        raise TypeError('Model rows are indexed by column name.')

    def keys(self):
        return [c.key for c in self.__mapper__.column_attrs]

    def __contains__(self, key):
        return key in self.keys()

    def get(self, key, default=None):
        return getattr(self, key, default)


db = SQLAlchemy(model_class=Base)
