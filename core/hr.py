"""The rules of human resources (models/hr.py; the pages are blueprints/school/hr.py).

* A staff record's fields are read from a form by ``clean_record``: every value trimmed and cut to its length, every
  choice checked against its list, every date a real YYYY-MM-DD in a sensible range. Nothing else on the form is read.
* Leave is counted in working days, Monday to Friday (``working_days``); public holidays are not taken off. A request
  counts against the year its first day falls in. A kind of leave with no days a year has no set allowance (unpaid
  leave, say), so it is never refused for want of days.
* A document is checked by what the file is, not only by its name (``check_document``): a PDF must begin like one,
  a Word file like one, a picture like one. It is stored under a random name in uploads/hr/, a folder no page opens
  directly (core/upload_access.py).
"""

import os
import re
import secrets
from datetime import date, timedelta

from werkzeug.utils import secure_filename

TITLES = ('Mr', 'Mrs', 'Miss', 'Ms', 'Dr', 'Prof', 'Rev', 'Pastor', 'Imam', 'Engr', 'Barr')
GENDERS = ('Female', 'Male')
MARITAL = ('Single', 'Married', 'Divorced', 'Widowed', 'Separated')
CATEGORIES = {'teaching': 'Teaching', 'non_teaching': 'Non-teaching'}
EMPLOYMENT_TYPES = {'full_time': 'Full-time', 'part_time': 'Part-time', 'contract': 'Contract',
                    'temporary': 'Temporary', 'volunteer': 'Volunteer', 'nysc': 'NYSC corps member'}
STATUSES = {'active': 'Working', 'suspended': 'Suspended', 'left': 'Left the school'}
EXIT_REASONS = {'resigned': 'Resigned', 'retired': 'Retired', 'contract_ended': 'Contract ended',
                'dismissed': 'Dismissed', 'deceased': 'Deceased', 'other': 'Other'}
CAREER_KINDS = {'appointment': 'Appointment', 'confirmation': 'Confirmation', 'promotion': 'Promotion',
                'transfer': 'Change of department', 'commendation': 'Commendation', 'query': 'Query',
                'warning': 'Warning', 'suspension': 'Suspension', 'other': 'Other'}
DOCUMENT_CATEGORIES = {'cv': 'CV', 'certificate': 'Certificate', 'appointment': 'Appointment letter',
                       'identity': 'Identity', 'medical': 'Medical', 'other': 'Other'}
LEAVE_STATUSES = {'pending': 'Waiting', 'approved': 'Approved', 'declined': 'Declined', 'cancelled': 'Cancelled'}

# The kinds of leave a school starts with; it changes their days, adds its own, or switches them off.
DEFAULT_LEAVE_TYPES = (('Annual leave', 10, 1), ('Casual leave', 5, 1), ('Sick leave', 10, 1),
                       ('Maternity leave', 60, 1), ('Paternity leave', 5, 1), ('Study leave', 0, 0),
                       ('Unpaid leave', 0, 0))

TEXT_LIMITS = {'staff_no': 30, 'title': 12, 'first_name': 60, 'middle_name': 60, 'last_name': 60, 'phone': 30,
               'email': 120, 'address': 300, 'state_of_origin': 60, 'job_title': 80, 'department': 80,
               'nok_name': 120, 'nok_relationship': 40, 'nok_phone': 30, 'nok_address': 300, 'notes': 2000}
DATE_FIELDS = ('date_of_birth', 'employment_date', 'confirmation_date', 'exit_date')
MAX_REASON = 500
MAX_LEAVE_SPAN = 366        # calendar days in one request
MAX_BACKDATE = 60           # how far back a member of staff may ask for leave (sick leave, after the event)
MAX_AHEAD = 400             # and how far ahead

EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
PHONE = re.compile(r'^[0-9+()\- ]{7,30}$')

# What a document may be, by extension, and how each kind of file must begin.
DOCUMENT_TYPES = {
    'pdf': ('application/pdf', (b'%PDF-',)),
    'png': ('image/png', (b'\x89PNG\r\n\x1a\n',)),
    'jpg': ('image/jpeg', (b'\xff\xd8\xff',)),
    'jpeg': ('image/jpeg', (b'\xff\xd8\xff',)),
    'docx': ('application/vnd.openxmlformats-officedocument.wordprocessingml.document', (b'PK\x03\x04',)),
    'doc': ('application/msword', (b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1',)),
}
DOCUMENT_EXTENSIONS = {'.' + ext for ext in DOCUMENT_TYPES}
DOCUMENT_TYPES_LABEL = 'a PDF, Word document, JPG or PNG'


def one_line(value, limit):
    return ' '.join(str(value or '').split())[:limit]


def parse_date(value):
    """A YYYY-MM-DD string as a date, or None."""
    value = str(value or '').strip()
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def full_name(record, title=True):
    parts = [record.title if title else None, record.first_name, record.middle_name, record.last_name]
    return ' '.join(p for p in parts if p and str(p).strip())


def clean_record(form, today=None):
    """``(values, errors)`` for a staff record from a submitted form. Only the known fields are read."""
    today = today or date.today()
    values, errors = {}, []
    for name, limit in TEXT_LIMITS.items():
        raw = form.get(name, '')
        values[name] = (str(raw).strip()[:limit] if name in ('address', 'nok_address', 'notes') else one_line(raw, limit)) or None
    if not values['first_name'] or not values['last_name']:
        errors.append('Enter the first name and the surname.')
    if values['title'] and values['title'] not in TITLES:
        values['title'] = None
    for name, allowed in (('gender', GENDERS), ('marital_status', MARITAL)):
        value = one_line(form.get(name), 20)
        values[name] = value if value in allowed else None
    for name, allowed, default in (('category', CATEGORIES, 'teaching'), ('employment_type', EMPLOYMENT_TYPES, 'full_time'),
                                   ('status', STATUSES, 'active')):
        value = one_line(form.get(name), 30)
        values[name] = value if value in allowed else default
    reason = one_line(form.get('exit_reason'), 30)
    values['exit_reason'] = reason if reason in EXIT_REASONS else None
    for name in DATE_FIELDS:
        raw = str(form.get(name, '') or '').strip()
        parsed = parse_date(raw)
        if raw and parsed is None:
            errors.append(f'{name.replace("_", " ").capitalize()} is not a date.')
        values[name] = parsed.isoformat() if parsed else None
    born = parse_date(values['date_of_birth'])
    if born and not (date(today.year - 90, 1, 1) <= born <= date(today.year - 14, 12, 31)):
        errors.append('The date of birth does not look right.')
    for name in ('employment_date', 'confirmation_date', 'exit_date'):
        when = parse_date(values[name])
        if when and not (date(1950, 1, 1) <= when <= today + timedelta(days=366)):
            errors.append(f'The {name.replace("_", " ")} does not look right.')
    started = parse_date(values['employment_date'])
    for name in ('confirmation_date', 'exit_date'):
        when = parse_date(values[name])
        if started and when and when < started:
            errors.append(f'The {name.replace("_", " ")} is before the employment date.')
    for name in ('email',):
        if values[name] and not EMAIL.match(values[name]):
            errors.append('The email address does not look right.')
    for name in ('phone', 'nok_phone'):
        if values[name] and not PHONE.match(values[name]):
            errors.append(('The phone number' if name == 'phone' else "The next of kin's phone number") + ' does not look right.')
    if values['status'] == 'left':
        if not values['exit_date']:
            errors.append('Enter the date they left.')
        if not values['exit_reason']:
            errors.append('Choose why they left.')
    else:
        values['exit_date'] = values['exit_reason'] = None
    return values, errors


# ---------------------------------------------------------------- leave
def working_days(start, end):
    """Monday to Friday from ``start`` to ``end``, both included."""
    if not start or not end or end < start:
        return 0
    total = (end - start).days + 1
    weeks, extra = divmod(total, 7)
    days = weeks * 5
    for offset in range(extra):
        if (start + timedelta(days=weeks * 7 + offset)).weekday() < 5:
            days += 1
    return days


def check_leave_dates(start_raw, end_raw, today=None, backdate=MAX_BACKDATE):
    """``(start, end, days, error)``: the dates of a request, and the working days it takes."""
    today = today or date.today()
    start, end = parse_date(start_raw), parse_date(end_raw)
    if not start or not end:
        return None, None, 0, 'Enter the first and the last day of the leave.'
    if end < start:
        return None, None, 0, 'The last day is before the first.'
    if (end - start).days + 1 > MAX_LEAVE_SPAN:
        return None, None, 0, 'Ask for at most a year of leave at a time.'
    if start < today - timedelta(days=backdate):
        return None, None, 0, f'Leave can be asked for at most {backdate} days after it started.'
    if start > today + timedelta(days=MAX_AHEAD):
        return None, None, 0, 'That is too far ahead to ask for leave.'
    days = working_days(start, end)
    if not days:
        return None, None, 0, 'Those dates are a weekend: there are no working days to take off.'
    return start, end, days, None


def overlaps(a_start, a_end, b_start, b_end):
    return a_start <= b_end and b_start <= a_end


def balance(leave_type, allowance_days, approved_days, pending_days):
    """One kind of leave for one year: {'allowance', 'used', 'waiting', 'left'}; 'allowance' and 'left' are None
    when the kind has no set allowance."""
    allowance = allowance_days if allowance_days is not None else (leave_type.days_per_year or None)
    left = None if allowance is None else allowance - approved_days
    return {'allowance': allowance, 'used': approved_days, 'waiting': pending_days, 'left': left}


# ---------------------------------------------------------------- documents
def check_document(file_obj, limit):
    """``(ext, data, content_type, shown_name)`` for an uploaded document, or ValueError with a sentence to show."""
    original = secure_filename(getattr(file_obj, 'filename', '') or '')
    shown = os.path.basename(str(getattr(file_obj, 'filename', '') or ''))[:120] or 'The file'
    ext = original.rsplit('.', 1)[-1].lower() if '.' in original else ''
    if ext not in DOCUMENT_TYPES:
        raise ValueError(f'{shown} is not {DOCUMENT_TYPES_LABEL}.')
    data = file_obj.stream.read(limit + 1)
    if not data:
        raise ValueError(f'{shown} is empty.')
    if len(data) > limit:
        raise ValueError(f'{shown} is too large.')
    content_type, signatures = DOCUMENT_TYPES[ext]
    if not data.startswith(signatures):
        raise ValueError(f'{shown} is not really {DOCUMENT_TYPES_LABEL}: its contents do not match its name.')
    return ext, data, content_type, original or f'document.{ext}'


def document_name(ext):
    return f'hr_{secrets.token_hex(12)}.{ext}'
