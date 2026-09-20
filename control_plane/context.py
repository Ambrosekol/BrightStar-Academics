"""Which school the current request (or provisioning job) is acting for.

Held in a ``ContextVar`` so it is isolated per request thread and also works
outside a request, e.g. while provisioning a school from the command line.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional


class NoTenantError(RuntimeError):
    """A school database was requested but no school is selected.

    Raised instead of falling back to a default database: guessing a school
    could show one school's data to another.
    """


@dataclass(frozen=True)
class TenantInfo:
    """Immutable snapshot of a school's registry row.

    A plain value rather than an ORM instance, so it stays valid after the
    registry session that loaded it is closed and can be cached safely.
    """

    id: int
    slug: str
    name: str
    status: str
    db_url: str
    db_schema: Optional[str]
    storage_key: str

    @property
    def is_active(self):
        return self.status == 'active'


_current = ContextVar('brightstars_current_tenant', default=None)


def current_tenant(required=True):
    tenant = _current.get()
    if tenant is None and required:
        raise NoTenantError('No school is selected for this request.')
    return tenant


def set_current_tenant(tenant):
    """Select a school; returns a token for :func:`reset_current_tenant`."""
    return _current.set(tenant)


def reset_current_tenant(token):
    _current.reset(token)


@contextmanager
def tenant_context(tenant):
    """Run a block against one school's database and files.

    The SQLAlchemy session is discarded on the way in and out so no object
    loaded for one school can be reused for another.
    """
    from models.base import db  # deferred: models.base imports this package

    token = set_current_tenant(tenant)
    db.session.remove()
    try:
        yield tenant
    finally:
        db.session.remove()
        reset_current_tenant(token)
