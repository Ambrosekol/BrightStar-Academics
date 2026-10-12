"""Reading a bulk-import file - a CSV, or an Excel workbook (.xlsx) - into a header and rows of text.

Used by the three bulk imports (students, enrolment history, past results), which then check every row exactly as
before: an Excel file is turned into the same rows a CSV would give, so nothing after this knows which it was.

An uploaded spreadsheet is someone else's file, so an Excel workbook is read defensively:

* **Only .xlsx**, recognised by its contents (a zip archive holding a workbook), not by its name. A macro-enabled
  workbook (.xlsm, or any workbook carrying a vbaProject) is refused: macros are never kept or run.
* **No zip bomb.** Before anything is opened, the archive's entries are checked: at most ``MAX_UNPACKED_BYTES``
  unpacked in all, no entry unpacking to more than ``MAX_RATIO`` times its packed size, no encrypted entries.
* **No hostile XML.** The workbook's XML is parsed with defusedxml (entity expansion and external entities refused);
  without defusedxml installed an Excel file is refused rather than parsed unsafely.
* **No formulas run.** Cells are read with ``data_only``: the value Excel last saved, never the formula. A cell's text
  is only ever text here (a CSV export still neutralises anything that looks like a formula: csv_safe in blueprints/entrance/helpers.py).
* **Bounded.** The first sheet only, at most ``max_rows`` rows and ``MAX_COLUMNS`` columns; the upload's own size
  limit has already been applied by the caller.
"""

import csv
import datetime as dt
import io
import zipfile

MAX_UNPACKED_BYTES = 40 * 1024 * 1024
MAX_RATIO = 120
MAX_COLUMNS = 60
EXTENSIONS = ('.csv', '.xlsx')
LABEL = 'a CSV or Excel (.xlsx) file'


class ImportFileError(ValueError):
    """The file cannot be read as an import; the message says why, for the person."""


def _is_zip(raw):
    return raw[:4] == b'PK\x03\x04'


def _check_archive(raw):
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ImportFileError('That Excel file is damaged or is not really an Excel workbook.') from None
    names = set()
    total = 0
    for info in archive.infolist():
        names.add(info.filename.lower())
        if info.flag_bits & 0x1:
            raise ImportFileError('That Excel file is password-protected. Remove the password and try again.')
        total += info.file_size
        packed = max(info.compress_size, 1)
        if total > MAX_UNPACKED_BYTES or info.file_size / packed > MAX_RATIO:
            raise ImportFileError('That Excel file unpacks to far more than an import needs. Save the sheet again '
                                  'as a plain .xlsx (or CSV) and try again.')
    if '[content_types].xml' not in names or not any(n.startswith('xl/workbook') for n in names):
        raise ImportFileError('That file is not an Excel workbook (.xlsx). Save it as .xlsx or CSV and try again.')
    if any('vbaproject' in n for n in names):
        raise ImportFileError('That workbook contains macros. Save it as a plain Excel workbook (.xlsx) or CSV.')


def _cell_text(value):
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, dt.datetime):
        return value.date().isoformat() if (value.hour, value.minute, value.second) == (0, 0, 0) else value.isoformat(sep=' ')
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _read_xlsx(raw, max_rows):
    _check_archive(raw)
    try:
        import openpyxl
        from openpyxl import xml as openpyxl_xml
    except ImportError:
        raise ImportFileError('Excel files cannot be read on this server yet. Save the sheet as CSV and try again.') from None
    if not getattr(openpyxl_xml, 'DEFUSEDXML', False):
        raise ImportFileError('Excel files cannot be read safely on this server yet. Save the sheet as CSV and try again.')
    try:
        book = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True, keep_links=False)
    except Exception:
        raise ImportFileError('That Excel file could not be read. Save it again as .xlsx (or CSV) and try again.') from None
    try:
        sheet = book.worksheets[0] if book.worksheets else None
        if sheet is None:
            raise ImportFileError('That Excel file has no sheets.')
        rows = sheet.iter_rows(max_col=MAX_COLUMNS, values_only=True)
        header = next(rows, None)
        if not header or not any(_cell_text(v) for v in header):
            return [], []
        names = [_cell_text(v) for v in header]
        while names and not names[-1]:
            names.pop()
        out = []
        for values in rows:
            texts = [_cell_text(v) for v in values[:len(names)]]
            if not any(texts):
                continue                      # Excel keeps empty, formatted rows: they are not rows of data
            out.append(dict(zip(names, texts + [''] * (len(names) - len(texts)))))
            if len(out) > max_rows:
                break                         # the caller reports that only the first rows were read
        return names, out
    finally:
        book.close()


def _read_csv(raw):
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ImportFileError('That file is not readable as plain-text CSV. Save it as CSV (UTF-8) or as an Excel '
                              'workbook (.xlsx) and try again.') from None
    reader = csv.DictReader(io.StringIO(text))
    return list(reader.fieldnames or []), reader


def read_table(filename, raw, max_rows):
    """``(column names, rows)`` of an import file, each row a dict of column name to text.

    Raises ImportFileError with a sentence for the person when the file cannot be used."""
    name = (filename or '').lower()
    if name.endswith(('.xlsm', '.xltm', '.xlsb', '.xls')):
        raise ImportFileError('Only plain Excel workbooks (.xlsx) and CSV files can be imported. Save the sheet as '
                              '.xlsx or CSV and try again.')
    if _is_zip(raw):
        return _read_xlsx(raw, max_rows)
    if name.endswith('.xlsx'):
        raise ImportFileError('That file is not an Excel workbook (.xlsx). Save it as .xlsx or CSV and try again.')
    return _read_csv(raw)
