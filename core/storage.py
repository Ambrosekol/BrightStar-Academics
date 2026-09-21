"""Where the current school keeps its files.

Each school gets its own folder under ``BRIGHTSTARS_TENANTS_DIR``, so one
school's question banks and uploaded photos are neither readable nor
overwritable from another school.

Files live on the application server's disk. A deployment with more than one
application server must mount ``BRIGHTSTARS_TENANTS_DIR`` on shared storage (or
swap these two functions for an object-store backend); the database moving to
PostgreSQL does not change that.
"""

import os

from control_plane import config
from control_plane.context import current_tenant
from control_plane.registry import SLUG_RE

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def tenant_root(tenant):
    if not SLUG_RE.match(tenant.storage_key or ''):
        raise ValueError('Unsafe storage key.')
    return os.path.join(config.tenants_dir(), tenant.storage_key)


def _folder(kind):
    # Resolved on every call rather than cached, so this works with or without a
    # Flask application context (scripts load question banks outside one), and
    # always for whichever school the caller has selected.
    path = os.path.join(tenant_root(current_tenant()), kind)
    os.makedirs(path, exist_ok=True)
    return path


def stored_upload_path(relative):
    """Filesystem path of a stored ``uploads/...`` value, or None.

    Database columns hold upload locations as ``uploads/<folder>/<file>``. The
    result is confined to the current school's uploads folder, so a value that
    tries to climb out of it (or names another folder entirely) resolves to
    None rather than to some other file.
    """
    relative = (relative or '').replace('\\', '/').lstrip('/')
    if relative.startswith('static/'):
        relative = relative[len('static/'):]
    if not relative.startswith('uploads/'):
        return None
    root = os.path.realpath(uploads_dir())
    path = os.path.realpath(os.path.join(root, relative[len('uploads/'):]))
    try:
        inside = os.path.commonpath([root, path]) == root
    except ValueError:
        # Windows: the stored value named another drive (e.g. "uploads/D:/x"),
        # so the two paths have no common prefix at all.
        return None
    return path if inside else None


def data_dir():
    """Folder of the current school's JSON question banks."""
    return _folder('data')


def generated_dir():
    """Folder of the current school's generated files (result images). Not served to anyone: a file
    here is read by the route that made it, which sends it and deletes it."""
    return _folder('generated')


def uploads_dir():
    """Folder of the current school's uploaded files; served at /static/uploads/."""
    return _folder('uploads')
