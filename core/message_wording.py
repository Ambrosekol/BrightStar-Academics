"""The words of every email and text message the school sends, which a school may rewrite.

Each message has an entry in MESSAGES: when it is sent, its placeholders and the platform's own wording (the
default). A school's own wording is kept in ``school_settings`` under ``message_wording:<key>`` and used instead
whenever it is there; restoring the default deletes it. Senders call ``render(key, **values)``.

* Placeholders are the only thing filled in: ``{child}`` becomes the student's name. The school's wording is
  never treated as a format string, and a value is put in only once, so a name that itself contains
  ``{school}`` stays as typed.
* A message can require a placeholder: the password-reset email must keep its ``{link}``.
* A text message is always ONE page, so a school never pays for two: 160 characters in the plain SMS alphabet.
  A wording is checked against long sample values when it is saved, and ``one_page`` (called by
  core/delivery.send_sms for every text, whatever wrote it) makes sure at sending time: it swaps characters that
  would make the text Unicode (₦, curly quotes, accents) for plain ones, drops what has no plain form (an emoji),
  and shortens the text at a word if a long name still pushes it over.
"""

import json
import re
import unicodedata
from datetime import datetime, timezone

from sqlalchemy import delete, select

from core.db_helpers import tuples

SETTING_PREFIX = 'message_wording:'
SMS_PAGE = 160
SUBJECT_MAX = 150
EMAIL_BODY_MAX = 3000

# The GSM 03.38 alphabet: a text in these characters costs one message per 160; any other character makes the
# whole text Unicode, one per 70. The extension characters count twice.
GSM = set('@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !"#¤%&\'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿'
          'abcdefghijklmnopqrstuvwxyzäöñüà')
GSM_EXT = set('^{}\\[~]|€')
_PLAIN = {'₦': 'N', '‘': "'", '’': "'", '‚': "'", '“': '"', '”': '"', '„': '"', '–': '-', '—': '-', '−': '-',
          '…': '...', '×': 'x', '•': '-', ' ': ' ', ' ': ' ', ' ': ' '}

# Long but realistic values, for checking that a wording still fits when the names are long.
SAMPLES = {
    'school': None,  # the school's own name
    'child': 'Oluwaseyifunmi Adebayo-Okafor', 'first_name': 'Oluwaseyifunmi', 'name': 'Mrs Ngozi Okafor-Adebayo',
    'amount': 'N1,250,000.00', 'term': 'Second Term', 'session': '2026/2027', 'category': 'School Fees',
    'receipt_no': 'RCP-2026-000123', 'report': 'Second Term', 'period': 'the Second Term', 'fees': 'Tuition, Development Levy',
    'kind': 'assignment', 'title': 'Algebra worksheet 3', 'due': '2026-11-20', 'timetable': 'Second Term examination timetable',
    'quantity': '2', 'item': 'Mathematics textbook', 'collector': 'Mrs Ngozi Okafor', 'about': ' (Visiting day)',
    'subject': 'Visiting day', 'message': 'Please remember that visiting day is this Saturday.',
    'link': 'https://yourschool.portal.example/parent', 'details': 'Amount paid: N250,000.00\nDate: 14 Oct 2026\nPaid for: School Fees',
    'candidate': 'Chika Okafor', 'candidate_id': 'BSA-26-0042', 'target_class': 'JSS 1', 'result': '172/200 (86.0%)',
    'grade': 'A',
}

_P = {  # what each placeholder means, shown on the settings page
    'school': "the school's name", 'child': "the student's full name", 'first_name': "the student's first name",
    'name': "the person's name", 'amount': 'the amount, as N25,000.00', 'term': 'the term', 'session': 'the session, as 2026/2027',
    'category': 'what it was paid for', 'receipt_no': 'the receipt number', 'report': 'First Term, Second Term... or Annual',
    'period': 'the First Term... or the annual', 'fees': 'the fees charged', 'kind': 'assignment or project',
    'title': 'the title of the work', 'due': 'the due date', 'timetable': 'which timetable', 'quantity': 'how many',
    'item': 'the item', 'collector': 'who collected it', 'about': "the message's subject in brackets, if it has one",
    'subject': "the message's subject", 'message': 'the message itself', 'link': 'the link to open',
    'details': 'the payment lines: amount, date, what for, balance', 'candidate': "the candidate's name",
    'candidate_id': 'the candidate ID', 'target_class': 'the class applied for', 'result': 'the cumulative result',
    'grade': 'the overall grade',
}


def _m(channel, group, label, when, placeholders, body, subject=None, required=()):
    return {'channel': channel, 'group': group, 'label': label, 'when': when, 'placeholders': placeholders,
            'body': body, 'subject': subject, 'required': tuple(required)}


MESSAGES = {
    # ---------------------------------------------------------------- text messages (one page each)
    'sms_fee_assessed': _m('sms', 'Fees', 'New fee charged', 'A fee is charged to a student.',
                           ('school', 'child', 'amount', 'term'),
                           '{school}: a fee of {amount} has been charged for {child} ({term}). See the parent portal.'),
    'sms_payment_recorded': _m('sms', 'Fees', 'Payment received', 'A payment is recorded.',
                               ('school', 'child', 'amount', 'category', 'receipt_no'),
                               '{school}: {amount} received for {child} ({category}). Receipt {receipt_no}. Thank you.'),
    'sms_report_card_ready': _m('sms', 'Results', 'Report card ready', 'Results are released and the report card is ready.',
                                ('school', 'child', 'report', 'session'),
                                "{school}: {child}'s {report} report card ({session}) is ready in the parent portal."),
    'sms_school_message': _m('sms', 'Messages', 'New message from the school', 'The school starts a conversation with a parent.',
                             ('school', 'about'),
                             '{school}: you have a new message from the school{about}. Sign in to the parent portal to read it.'),
    'sms_store_collected': _m('sms', 'Store', 'Store purchase collected', 'A purchase from the school store is collected.',
                              ('school', 'quantity', 'item', 'child', 'collector', 'receipt_no'),
                              '{school}: {quantity} x {item} for {child} was collected by {collector}. Receipt {receipt_no}.'),
    'sms_birthday': _m('sms', 'Birthdays', 'Birthday greeting', "A student's birthday, if birthday texts are on (Email & SMS).",
                       ('first_name', 'school'),
                       'Happy birthday, {first_name}! Everyone at {school} wishes you a wonderful day and a great year ahead.'),
    # ---------------------------------------------------------------- emails
    'email_work': _m('email', 'School work', 'New assignment or project', 'Work is given to a class.',
                     ('child', 'kind', 'title', 'due', 'school'),
                     'Dear Parent/Guardian,\n\n{child} has been given a new {kind}: "{title}".\nDue: {due}.\n\n'
                     'Please check the student/parent portal for details.\n\n{school}',
                     subject='New {kind} for {child}'),
    'email_fee_assessed': _m('email', 'Fees', 'New fee charged', 'A fee is charged to a student.',
                             ('child', 'fees', 'amount', 'term', 'session', 'school'),
                             'Dear Parent/Guardian,\n\n{child} has been charged a new fee: {fees}.\n'
                             'Amount: {amount} - {term} ({session}).\n\n'
                             'Please check the parent portal for your full fee account and outstanding balance.\n\n{school}',
                             subject='New fee charged - {child}'),
    'email_payment_recorded': _m('email', 'Fees', 'Payment received', 'A payment is recorded (when no receipt is emailed).',
                                 ('child', 'amount', 'category', 'receipt_no', 'school'),
                                 'Dear Parent/Guardian,\n\nWe have received a payment of {amount} for {category} on behalf of '
                                 '{child}. Receipt number: {receipt_no}.\n\nPlease check the parent portal for your full fee '
                                 'account and outstanding balance.\n\nThank you,\n{school}',
                                 subject='Payment received - {child}'),
    'email_payment_receipt': _m('email', 'Fees', 'Payment receipt', 'A payment is recorded; the PDF receipt is attached.',
                                ('child', 'receipt_no', 'details', 'school'),
                                'Dear Parent/Guardian,\n\nThank you. We have received a payment for {child}, and the official '
                                'receipt {receipt_no} is attached.\n\n{details}\n\n{school}',
                                subject='{school} Payment Receipt {receipt_no}'),
    'email_report_card_ready': _m('email', 'Results', 'Report card ready', 'Results are released and the report card is ready.',
                                  ('child', 'report', 'period', 'session', 'link', 'school'),
                                  "Dear Parent/Guardian,\n\n{child}'s results for {period} report card ({session}) have been "
                                  'released, and the report card is now ready.\n\nSign in to the parent portal to view and '
                                  'download it:\n{link}\n\nThank you,\n{school}',
                                  subject='{report} report card ready - {child}'),
    'email_timetable_released': _m('email', 'Results', 'Timetable released', 'A test or examination timetable is released.',
                                   ('child', 'timetable', 'school'),
                                   'Dear Parent/Guardian,\n\nThe {timetable} has been released.\n\nSign in to the parent '
                                   'portal to view and download it.\n\nThank you,\n{school}',
                                   subject='{timetable} released - {child}'),
    'email_school_message': _m('email', 'Messages', 'New message from the school', 'The school starts a conversation with a parent.',
                               ('name', 'about', 'subject', 'message', 'school'),
                               'Dear {name},\n\nThe school has sent you a message{about}:\n\n{message}\n\n'
                               'Sign in to the parent portal to continue the conversation.\n\n{school}',
                               subject='Message from the school: {subject}'),
    'email_school_reply': _m('email', 'Messages', "The school's reply", 'The school replies to a parent.',
                             ('name', 'about', 'subject', 'message', 'school'),
                             'Dear {name},\n\nThe school has replied to your message{about}:\n\n{message}\n\n'
                             'Sign in to the parent portal to continue the conversation.\n\n{school}',
                             subject='School replied: {subject}'),
    'email_password_reset': _m('email', 'Accounts', 'Password reset', 'Someone asks to reset their password.',
                               ('name', 'school', 'link'),
                               'Dear {name},\n\nA password reset was requested for your {school} account. Use this link '
                               'within 30 minutes:\n\n{link}\n\nIf you did not request this, you can ignore this message.\n\n{school}',
                               subject='{school} - Password reset', required=('link',)),
    'email_entrance_result': _m('email', 'Entrance', 'Entrance examination result', "Staff email a candidate's result (it opens in their own email app).",
                                ('candidate', 'candidate_id', 'target_class', 'result', 'grade', 'school'),
                                '{school} - Entrance Examination Result\n\nCandidate: {candidate}\nCandidate ID: {candidate_id}\n'
                                'Target class: {target_class}\nCumulative result: {result}\nOverall grade: {grade}\n\n'
                                'This result was prepared by {school}.',
                                subject='Entrance Examination Result - {candidate}'),
}

_TOKEN = re.compile(r'\{([a-z_]+)\}')


def describe(name):
    return _P.get(name, name)


def _primary_school_id():
    from blueprints.finance.helpers import _primary_school_id as school_id
    return school_id()


def _school():
    from core.branding import school_name
    return school_name()


def stored(keys=None):
    """``{key: {'subject', 'body'}}`` for the messages this school has rewritten."""
    from models import SchoolSetting
    school_id = _primary_school_id()
    if not school_id:
        return {}
    names = [SETTING_PREFIX + k for k in (keys or MESSAGES)]
    found = {}
    for name, value in tuples(select(SchoolSetting.setting_key, SchoolSetting.setting_value).where(
            SchoolSetting.school_id == school_id, SchoolSetting.setting_key.in_(names))):
        try:
            data = json.loads(value or '{}')
        except ValueError:
            continue
        if isinstance(data, dict) and data.get('body'):
            found[name[len(SETTING_PREFIX):]] = {'subject': str(data.get('subject') or ''), 'body': str(data['body'])}
    return found


def wording(key):
    """``{'subject', 'body', 'custom'}`` in use for one message: the school's own, or the default."""
    spec = MESSAGES[key]
    try:
        own = stored([key]).get(key)
    except Exception:                     # never let a missing setting stop a message going out
        from models import db
        db.session.rollback()
        own = None
    if own:
        return {'subject': own['subject'] or (spec['subject'] or ''), 'body': own['body'], 'custom': True}
    return {'subject': spec['subject'] or '', 'body': spec['body'], 'custom': False}


def fill(template, values):
    """Put the values in, once each; an unknown placeholder is left as written."""
    return _TOKEN.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), template)


def render(key, **values):
    """``(subject, body)`` of one message, in the school's own words if it has them. ``school`` is filled in
    automatically. A text message's body is made one page here as well as in send_sms."""
    values.setdefault('school', _school())
    current = wording(key)
    subject = ' '.join(fill(current['subject'], values).split())[:SUBJECT_MAX] if current['subject'] else ''
    body = fill(current['body'], values)
    if MESSAGES[key]['channel'] == 'sms':
        body = one_page(body)
    return subject, body


def plain(text):
    """The text in the plain SMS alphabet where it can be: ₦ as N, curly quotes straight, accents off
    (Ọ̀lá as Ola); a character with no plain form (an emoji) is dropped. Whitespace runs become one space."""
    out = []
    for ch in str(text or ''):
        ch = _PLAIN.get(ch, ch)
        if all(c in GSM or c in GSM_EXT for c in ch):
            out.append(ch)
            continue
        base = ''.join(c for c in unicodedata.normalize('NFKD', ch) if not unicodedata.combining(c))
        out.append(''.join(c for c in base if c in GSM or c in GSM_EXT))
    return ' '.join(''.join(out).split())


def _has_plain_form(ch):
    """Whether a character can go in a one-page text: it is in the SMS alphabet, or plain() has a plain form for it."""
    if ch in GSM or ch in GSM_EXT or ch in _PLAIN or ch.isspace():
        return True
    base = ''.join(c for c in unicodedata.normalize('NFKD', ch) if not unicodedata.combining(c))
    return bool(base) and all(c in GSM or c in GSM_EXT for c in base)


def sms_length(text):
    """Characters a text counts as in the plain SMS alphabet (extension characters count twice)."""
    return sum(2 if ch in GSM_EXT else 1 for ch in text)


def one_page(text):
    """``text`` as ONE plain-alphabet text message: at most 160 characters, cut at a word with '...' if needed."""
    text = plain(text)
    if sms_length(text) <= SMS_PAGE:
        return text
    cut = []
    for word in text.split(' '):
        if sms_length(' '.join(cut + [word])) > SMS_PAGE - 3:
            break
        cut.append(word)
    short = ' '.join(cut) or text[:SMS_PAGE - 3]
    return short.rstrip(' ,;:-') + '...'


def _clean(raw, multiline):
    text = str(raw or '').replace('\r\n', '\n').replace('\r', '\n')
    text = ''.join(ch for ch in text if ch.isprintable() or ch == '\n')
    if multiline:
        return '\n'.join(line.rstrip() for line in text.strip().split('\n'))
    return ' '.join(text.split())


def check(key, subject, body):
    """The school's wording for one message, checked. Returns ``(subject, body)`` cleaned, or raises ValueError
    with what to fix."""
    spec = MESSAGES[key]
    sms = spec['channel'] == 'sms'
    body = _clean(body, multiline=not sms)
    subject = '' if sms else _clean(subject, multiline=False)
    if not body:
        raise ValueError('Write the message, or restore the standard wording.')
    if not sms and not subject:
        raise ValueError('Write the subject line of the email.')
    if len(body) > (SMS_PAGE * 2 if sms else EMAIL_BODY_MAX) or len(subject) > SUBJECT_MAX:
        raise ValueError('That message is too long.')
    allowed = set(spec['placeholders'])
    for text in (subject, body):
        unknown = sorted({m for m in re.findall(r'\{([^{}]*)\}', text)} - allowed)
        if unknown or text.count('{') != text.count('}'):
            listed = ', '.join('{' + p + '}' for p in spec['placeholders'])
            raise ValueError(f'This message can use only these in curly brackets: {listed}.')
    for need in spec['required']:
        if '{' + need + '}' not in body:
            raise ValueError(f'This message must keep {{{need}}}: without it the message does not work.')
    if sms:
        if not all(_has_plain_form(ch) for ch in body):
            raise ValueError('Text messages can use only plain letters, numbers and common punctuation (no emoji).')
        sample = dict(SAMPLES, school=_school())
        filled = plain(fill(body, sample))
        if sms_length(filled) > SMS_PAGE:
            raise ValueError(f'That text would be {sms_length(filled)} characters with a long name, over the '
                             f'{SMS_PAGE} of one text message. Shorten it so it costs one text.')
    return subject, body


def save(key, subject, body, admin_id):
    """Save the school's own wording for one message (checked first). Commits."""
    from models import SchoolSetting, db
    subject, body = check(key, subject, body)
    spec = MESSAGES[key]
    if body == spec['body'] and subject == (spec['subject'] or ''):
        return reset(key)
    school_id = _primary_school_id()
    if not school_id:
        raise ValueError('No active school is configured.')
    now = datetime.now(timezone.utc).isoformat()
    name, value = SETTING_PREFIX + key, json.dumps({'subject': subject, 'body': body}, ensure_ascii=False)
    row = db.session.scalars(select(SchoolSetting).where(SchoolSetting.school_id == school_id,
                                                         SchoolSetting.setting_key == name)).first()
    if row:
        row.setting_value, row.updated_at, row.updated_by = value, now, admin_id
    else:
        db.session.add(SchoolSetting(school_id=school_id, setting_key=name, setting_value=value, updated_at=now,
                                     updated_by=admin_id))
    db.session.commit()


def reset(key):
    """Go back to the platform's wording for one message. Commits."""
    from models import SchoolSetting, db
    school_id = _primary_school_id()
    if school_id:
        db.session.execute(delete(SchoolSetting).where(SchoolSetting.school_id == school_id,
                                                       SchoolSetting.setting_key == SETTING_PREFIX + key))
        db.session.commit()


def catalogue():
    """Every message for the settings page, grouped: ``[(channel label, [entry])]``."""
    own = stored()
    sample = dict(SAMPLES, school=_school())
    short = dict(sample, child='Ada Obi', first_name='Ada', name='Mrs Obi', amount='N25,000.00', collector='Mrs Obi')
    out = []
    for channel, title in (('sms', 'Text messages'), ('email', 'Emails')):
        rows = []
        for key, spec in MESSAGES.items():
            if spec['channel'] != channel:
                continue
            mine = own.get(key)
            rows.append({'key': key, 'channel': channel, 'label': spec['label'], 'group': spec['group'], 'when': spec['when'],
                         'custom': bool(mine), 'subject': (mine or {}).get('subject') or spec['subject'] or '',
                         'body': (mine or {}).get('body') or spec['body'],
                         'default_subject': spec['subject'] or '', 'default_body': spec['body'],
                         'placeholders': [{'name': p, 'means': describe(p)} for p in spec['placeholders']],
                         'required': list(spec['required']), 'sample': short, 'long': sample})
        out.append((channel, title, rows))
    return out
