"""Where the current school keeps its files.

Each school gets its own folder under ``BRIGHTSTARS_TENANTS_DIR`` (or, in
``BRIGHTSTARS_STORAGE_BACKEND=s3`` mode, its own key prefix in an S3-compatible bucket -
see ``core/object_store.py``), so one school's question banks and uploaded photos are
neither readable nor overwritable from another school.

``generated_dir()`` (result images shelled out to Chrome) is always local, whichever
backend is configured: nothing in it is ever served or kept - the route that makes a file
there reads it, sends it, and deletes it in the same request - so there is nothing to move
between application servers and no need to keep a copy anywhere else.
"""

import os

from control_plane import config
from control_plane.context import current_tenant
from control_plane.registry import SLUG_RE

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def tenant_root(tenant):
    """Local backend: the school's folder on disk. S3 backend: the key prefix its objects
    live under in the bucket. Only a caller that knows which backend is configured should do
    anything with the result - use the backend-aware helpers below instead where possible."""
    if not SLUG_RE.match(tenant.storage_key or ''):
        raise ValueError('Unsafe storage key.')
    if config.storage_backend() == 's3':
        return f'{config.s3_key_prefix()}{tenant.storage_key}'
    return os.path.join(config.tenants_dir(), tenant.storage_key)


def _folder(kind):
    # Local backend only. Resolved on every call rather than cached, so this works with or
    # without a Flask application context (scripts load question banks outside one), and
    # always for whichever school the caller has selected.
    path = os.path.join(tenant_root(current_tenant()), kind)
    os.makedirs(path, exist_ok=True)
    return path


def _tenant_key(kind):
    """S3 backend only: the key prefix (ending in '/') for one kind of the current
    school's files."""
    return f'{tenant_root(current_tenant())}/{kind}/'


def data_dir():
    """Folder of the current school's JSON question banks. Local backend only."""
    return _folder('data')


def generated_dir():
    """Folder of the current school's generated files (result images). Always local - see
    the module docstring - regardless of BRIGHTSTARS_STORAGE_BACKEND."""
    return _folder('generated')


def uploads_dir():
    """Folder of the current school's uploaded files; served at /static/uploads/. Local
    backend only."""
    return _folder('uploads')


# --------------------------------------------------------------------------------------------
# Uploads: backend-aware. Every caller that needs to read, check, save or remove an upload
# should use these instead of the filesystem directly, so it works under either backend.

def _sanitize_upload_relative(relative):
    """The ``uploads/...``-relative part of a stored value, normalised and confined to the
    uploads folder - or None for anything that tries to leave it (or names another folder
    entirely). Pure string logic, with no filesystem or network call, so both backends apply
    exactly the same rule for what a stored value may point at.
    """
    relative = (relative or '').replace('\\', '/').lstrip('/')
    if relative.startswith('static/'):
        relative = relative[len('static/'):]
    if not relative.startswith('uploads/'):
        return None
    relative = relative[len('uploads/'):]
    parts = relative.split('/')
    if not relative or any(part in ('', '.', '..') for part in parts):
        return None
    return relative


def stored_upload_path(relative):
    """Filesystem path of a stored ``uploads/...`` value, or None. Local backend only - use
    ``read_upload_bytes`` / ``upload_exists`` / ``delete_upload`` for code that must work
    under either backend.

    Database columns hold upload locations as ``uploads/<folder>/<file>``. The
    result is confined to the current school's uploads folder, so a value that
    tries to climb out of it (or names another folder entirely) resolves to
    None rather than to some other file.
    """
    sanitized = _sanitize_upload_relative(relative)
    if sanitized is None:
        return None
    root = os.path.realpath(uploads_dir())
    path = os.path.realpath(os.path.join(root, sanitized))
    try:
        inside = os.path.commonpath([root, path]) == root
    except ValueError:
        # Windows: the stored value named another drive (e.g. "uploads/D:/x"),
        # so the two paths have no common prefix at all.
        return None
    return path if inside else None


def upload_exists(relative):
    """Whether a stored ``uploads/...`` value points at a real file, under either backend."""
    if config.storage_backend() == 's3':
        sanitized = _sanitize_upload_relative(relative)
        if sanitized is None:
            return False
        from core import object_store
        return object_store.exists(_tenant_key('uploads') + sanitized)
    path = stored_upload_path(relative)
    return bool(path and os.path.isfile(path))


def read_upload_bytes(relative):
    """The bytes of a stored ``uploads/...`` value, or None if there is nothing there (or
    the value does not point inside the current school's uploads folder)."""
    if config.storage_backend() == 's3':
        sanitized = _sanitize_upload_relative(relative)
        if sanitized is None:
            return None
        from core import object_store
        return object_store.get_bytes(_tenant_key('uploads') + sanitized)
    path = stored_upload_path(relative)
    if not path or not os.path.isfile(path):
        return None
    with open(path, 'rb') as fh:
        return fh.read()


def delete_upload(relative):
    """Remove a stored ``uploads/...`` value if it is there. Never an error if it is not."""
    if config.storage_backend() == 's3':
        sanitized = _sanitize_upload_relative(relative)
        if sanitized is None:
            return
        from core import object_store
        object_store.delete(_tenant_key('uploads') + sanitized)
        return
    path = stored_upload_path(relative)
    if path and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass


def save_upload_bytes(subdir, filename, data, content_type=None):
    """Write a new upload under ``uploads/<subdir>/<filename>`` and return that stored
    value, under either backend."""
    if config.storage_backend() == 's3':
        from core import object_store
        object_store.put_bytes(f'{_tenant_key("uploads")}{subdir}/{filename}', data, content_type)
    else:
        folder = os.path.join(uploads_dir(), subdir)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, filename), 'wb') as fh:
            fh.write(data)
    return f'uploads/{subdir}/{filename}'


# --------------------------------------------------------------------------------------------
# Question bank data: backend-aware directory-like access, used by core/banks.py and
# core/entrance.py so neither has to know which backend is configured.

def list_data_names():
    """Every file name in the current school's question-bank folder."""
    if config.storage_backend() == 's3':
        from core import object_store
        return sorted(object_store.list_names(_tenant_key('data')))
    return sorted(os.listdir(data_dir()))


def read_data_text(name, encoding='utf-8'):
    """The text of one file in the current school's question-bank folder, or None."""
    if config.storage_backend() == 's3':
        from core import object_store
        raw = object_store.get_bytes(_tenant_key('data') + name)
        return raw.decode(encoding) if raw is not None else None
    try:
        with open(os.path.join(data_dir(), name), encoding=encoding) as f:
            return f.read()
    except OSError:
        return None


def write_data_bytes(name, data, overwrite):
    """Write ``data`` to ``name`` in the current school's question-bank folder. S3 backend
    only (core/banks.py keeps its own atomic, symlink-safe local write for the local
    backend). Returns True if written; with ``overwrite=False``, False without writing
    anything if ``name`` is already there."""
    from core import object_store
    key = _tenant_key('data') + name
    if overwrite:
        object_store.put_bytes(key, data)
        return True
    return object_store.put_if_absent(key, data)
