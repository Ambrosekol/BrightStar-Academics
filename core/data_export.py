"""Data exports: a whole school's data, or a family's copy of one child's, as a ZIP built in the background.

* **The school's** (Settings > Export all the school's data, the School Admin's alone): one spreadsheet (CSV) for every
  table of the school's database, readable in Excel or loaded into another system, and every uploaded file
  (photographs, signatures, attachments), with a README saying what each is. Nothing that would let someone sign in or
  reach an outside account is included: no password or secret hashes, no tokens, no stored credentials (anything kept
  encrypted is left blank).
* **A family's** (core/privacy.family_files), when the school approves a parent's request.

Both are written to a temporary file row by row and file by file, so a school of thousands with all their photographs
never sits in memory, then stored outside the public uploads (the 'exports' folder is never served, see
core/upload_access.py) and downloaded through a route that checks who may have it. A file is kept ``KEEP_DAYS`` and
then removed. A build that dies (a restart) is picked up again by the next try once its heartbeat is old.
"""

import csv
import io
import json
import os
import re
import secrets
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from core.jobs import enqueue, job_handler
from models import DataExport, DataRequest, db

KEEP_DAYS = 7
HEARTBEAT_STALE_SECONDS = 180
FOLDER = 'exports'
# Never in an export: what would let someone sign in, or reach an outside account.
SECRET_COLUMN = re.compile(r'(password_hash|secret|token|released_password|api_key)', re.I)
SKIP_TABLES = {'background_jobs', 'data_exports', 'webhook_deliveries', 'alembic_version'}


def _now():
    return datetime.now(timezone.utc)


def _iso(dt=None):
    return (dt or _now()).isoformat()


def _encrypted_marker():
    from core import delivery
    return getattr(delivery, '_ENCRYPTED', '\x00never')


def start(kind, admin_id=None, student_id=None, request_id=None):
    """Queue an export and start building it. Returns the DataExport row (committed)."""
    row = DataExport(kind=kind, student_id=student_id, status='queued', requested_by=admin_id, created_at=_iso())
    db.session.add(row)
    db.session.flush()
    if request_id:
        req = db.session.get(DataRequest, request_id)
        req.export_id = row.id
    db.session.commit()
    enqueue('data_export_build', export_id=row.id, request_id=request_id)
    return row


def _claim(export_id):
    stale = _iso(_now() - timedelta(seconds=HEARTBEAT_STALE_SECONDS))
    claimed = db.session.execute(sa.text(
        "UPDATE data_exports SET status = 'building', started_at = :now, heartbeat_at = :now WHERE id = :i AND "
        "(status = 'queued' OR (status = 'building' AND (heartbeat_at IS NULL OR heartbeat_at < :stale)))"),
        {'now': _iso(), 'i': export_id, 'stale': stale}).rowcount
    db.session.commit()
    return claimed == 1


def _beat(export_id):
    db.session.execute(sa.text('UPDATE data_exports SET heartbeat_at = :now WHERE id = :i'), {'now': _iso(), 'i': export_id})
    db.session.commit()


def _school_tables():
    return [t for t in sorted(db.metadata.sorted_tables, key=lambda t: t.name) if t.name not in SKIP_TABLES]


def _write_table(zf, conn, table, marker):
    from blueprints.entrance.helpers import csv_safe
    columns = [c for c in table.columns if not SECRET_COLUMN.search(c.name)]
    order = list(table.primary_key.columns) or columns[:1]
    count = 0
    with zf.open(f'data/{table.name}.csv', 'w', force_zip64=True) as raw:
        out = io.TextIOWrapper(raw, encoding='utf-8-sig', newline='')
        w = csv.writer(out)
        w.writerow([c.name for c in columns])
        result = conn.execution_options(stream_results=True, yield_per=2000).execute(
            sa.select(*columns).order_by(*order))
        for row in result:
            w.writerow(['' if isinstance(v, str) and v.startswith(marker) else csv_safe(v) for v in row])
            count += 1
        out.flush()
        out.detach()
    return count


def _school_zip(zf, export_id):
    from control_plane.routing import current_engine
    from core import storage
    marker = _encrypted_marker()
    tables = {}
    engine = current_engine()
    with engine.connect() as conn:
        for i, table in enumerate(_school_tables()):
            try:
                tables[table.name] = _write_table(zf, conn, table, marker)
            except sa.exc.ProgrammingError:
                conn.rollback()             # a table the model knows that this school's database does not have yet
            if i % 10 == 0:
                _beat(export_id)
    files, size = 0, 0
    for stored, length in storage.iter_uploads(skip_folders=(FOLDER,)):
        with zf.open('files/' + stored[len('uploads/'):], 'w', force_zip64=True) as dst:
            if storage.copy_upload_to(stored, dst):
                files += 1
                size += length or 0
        if files % 200 == 0:
            _beat(export_id)
    zf.writestr('README.txt', _README.format(when=_now().strftime('%d %B %Y %H:%M UTC'), tables=len(tables),
                                             rows=sum(tables.values()), files=files))
    return {'tables': tables, 'files': files, 'file_bytes': size}


_README = """Everything this school keeps in Brightstars Academics, as it was on {when}.

data/   one spreadsheet (CSV, UTF-8) for each of the {tables} tables of the school's database: {rows} rows in all.
        Open them in Excel or Google Sheets, or load them into another system. Columns that link tables are named after
        what they point to (student_id is the id column of data/students.csv, class_id of data/school_classes.csv...).
files/  every uploaded file ({files}): photographs, signatures, logos, attachments, as they are named in the data.

Left out on purpose: password and other secret hashes, sign-in tokens, and the school's stored keys and passwords for
outside services (email, SMS, Paystack). Those cannot be moved anyway; set them up again wherever the data goes.

This is personal data about children and families. Keep it as safely as the school keeps its paper records, and delete
copies that are no longer needed.
"""


def _family_zip(zf, export_id, request_id):
    from core.privacy import family_files
    req = db.session.get(DataRequest, request_id)
    for name, data in family_files(req.student_id, req.parent_id):
        zf.writestr(name, data)
    return {'files': 'family'}


@job_handler('data_export_build')
def build(export_id, request_id=None):
    if not _claim(export_id):
        return
    export = db.session.get(DataExport, export_id)
    fd, temp_path = tempfile.mkstemp(prefix='brightstars-export-', suffix='.zip')
    os.close(fd)
    try:
        with zipfile.ZipFile(temp_path, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            summary = _family_zip(zf, export_id, request_id) if export.kind == 'family' else _school_zip(zf, export_id)
        size = os.path.getsize(temp_path)
        from core import storage
        stored = storage.save_upload_file(FOLDER, f'{export.kind}-{export_id}-{secrets.token_hex(16)}.zip', temp_path,
                                          'application/zip')
        export = db.session.get(DataExport, export_id)
        export.status, export.file_key, export.size_bytes = 'ready', stored, size
        export.summary_json = json.dumps(summary)
        export.completed_at = _iso()
        export.expires_at = _iso(_now() + timedelta(days=KEEP_DAYS))
        if request_id:
            req = db.session.get(DataRequest, request_id)
            if req and req.status == 'approved':
                req.status, req.completed_at = 'done', _iso()
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        export = db.session.get(DataExport, export_id)
        export.status, export.error = 'failed', f'{type(exc).__name__}: {str(exc)[:500]}'
        db.session.commit()
        from flask import current_app
        current_app.logger.exception('Data export %s failed', export_id)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def remove_expired():
    """Delete the files of exports past their date. One indexed query when there is nothing to do."""
    from core.storage import delete_upload
    now = _iso()
    rows = db.session.scalars(sa.select(DataExport).where(DataExport.status == 'ready', DataExport.expires_at < now)).all()
    for row in rows:
        if row.file_key:
            try:
                delete_upload(row.file_key)
            except Exception:
                pass
        row.status, row.file_key = 'expired', None
    if rows:
        db.session.commit()
    return len(rows)


def download(export, filename):
    """The download response for a ready export (the caller has checked who may have it), or None."""
    from core.storage import download_upload
    if export is None or export.status != 'ready' or not export.file_key:
        return None
    if export.expires_at and export.expires_at < _iso():
        return None
    response = download_upload(export.file_key, filename)
    if response is not None:
        export.downloads = (export.downloads or 0) + 1
        db.session.commit()
    return response
