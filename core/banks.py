"""Question banks a school starts with, and the checks for banks a school brings.

A school's question banks are JSON files in its own ``data/`` folder (``core/storage.py``),
so copying that one folder moves the school. This module is the one place that decides
what a well-formed bank looks like and how a bank file is safely written there:

* ``starter_banks/`` (next to ``app.py``) holds the platform's standard entrance banks.
  They are copied into each new school's own folder when it is created, never overwriting
  anything, and are the school's own to edit afterwards. Nothing in that folder names a school.
* ``parse_bank_bytes()`` is the strict check for a bank a school uploads. It refuses anything
  it does not fully understand, so a bad file can never reach the examination engine.
* ``save_bank_file()`` writes a bank only inside the current school's folder, all-or-nothing.

It has no routes and, at import time, needs no database or Flask application.
"""

import json
import os
import re
import tempfile

from core.storage import BASE, data_dir

STARTER_DIR = os.path.join(BASE, 'starter_banks')

# ---- what a bank may contain ----
MAX_BANK_BYTES = 2 * 1024 * 1024
MAX_QUESTIONS = 500
MAX_PROBLEMS_SHOWN = 12
MIN_DURATION_SECONDS = 60
MAX_DURATION_SECONDS = 6 * 60 * 60
# Every screen that shows or edits a question (the exam, the results, the question form) is
# built for options A to D, so a bank is held to exactly four.
OPTIONS_PER_QUESTION = 4
ENTRY_GROUPS = ('year7', 'year10')

# A bank's id becomes its file name (<id>.json), so it is limited to characters that can
# never mean a folder, a drive or a special file, on any operating system.
BANK_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
# 'manifest' is a file the engine skips; 'new' and 'import' are pages of the bank screens.
RESERVED_IDS = {'manifest', 'new', 'import'}
# Names Windows treats as devices, whatever the extension ("con.json" is the console).
WINDOWS_DEVICE_NAMES = {'con', 'prn', 'aux', 'nul'} | {f'com{i}' for i in range(1, 10)} | {f'lpt{i}' for i in range(1, 10)}


class BankError(ValueError):
    """A bank that cannot be accepted. ``problems`` are plain sentences safe to show."""

    def __init__(self, problems):
        if isinstance(problems, str):
            problems = [problems]
        self.problems = list(problems)
        super().__init__(' '.join(self.problems))


class _Problems:
    """Collects what is wrong, stopping the list at a readable length."""

    def __init__(self):
        self.items = []
        self.extra = 0

    def add(self, text):
        if len(self.items) < MAX_PROBLEMS_SHOWN:
            self.items.append(text)
        else:
            self.extra += 1

    def raise_if_any(self):
        if self.items:
            shown = list(self.items)
            if self.extra:
                shown.append(f'...and {self.extra} more problem{"s" if self.extra != 1 else ""}.')
            raise BankError(shown)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _clean_text(value, label, longest, problems, required=True):
    """A stripped string, or None with a problem recorded."""
    if value is None or value == '':
        if required:
            problems.add(f'{label} is required.')
        return None
    if not isinstance(value, str):
        problems.add(f'{label} must be text.')
        return None
    text = value.strip()
    if any(ord(ch) < 32 and ch not in '\n\t' for ch in text):
        problems.add(f'{label} contains a control character.')
        return None
    if not text:
        if required:
            problems.add(f'{label} is required.')
        return None
    if len(text) > longest:
        problems.add(f'{label} is too long (at most {longest} characters).')
        return None
    return text


def check_bank_id(bank_id):
    """The reason a bank id cannot be used, or None when it is fine."""
    if not isinstance(bank_id, str) or not BANK_ID_RE.match(bank_id):
        return ('The bank id may use only lower-case letters, digits, hyphens and underscores '
                '(up to 64 characters), and must start with a letter or digit.')
    if bank_id in RESERVED_IDS or bank_id in WINDOWS_DEVICE_NAMES:
        return f'"{bank_id}" cannot be used as a bank id. Choose another.'
    return None


def check_bank(data):
    """Validate a decoded bank and return a clean copy of it.

    Only the fields the examination engine uses are kept; anything else in the file
    (including picture paths, which would point outside what was uploaded) is left out.
    Raises :class:`BankError` listing what is wrong.
    """
    if not isinstance(data, dict):
        raise BankError('The file must hold one question bank: a JSON object that starts with { '
                        'and has an "id" and a list of "questions".')
    problems = _Problems()

    bank_id = data.get('id')
    if bank_id is None or bank_id == '':
        problems.add('The bank needs an "id".')
        bank_id = None
    else:
        reason = check_bank_id(bank_id)
        if reason:
            problems.add(reason)
            bank_id = None

    name = _clean_text(data.get('name'), 'The bank "name"', 200, problems)
    level = _clean_text(data.get('level'), 'The "level"', 60, problems, required=False)
    subject = _clean_text(data.get('subject'), 'The "subject"', 80, problems, required=False)
    raw_version = data.get('version')
    if isinstance(raw_version, (int, float)) and not isinstance(raw_version, bool):
        raw_version = str(raw_version)  # "version": 2 is a natural thing to write
    version = _clean_text(raw_version, 'The "version"', 30, problems, required=False) or '1.0'
    source = _clean_text(data.get('source_status'), 'The "source_status"', 60, problems, required=False)

    entry_group = data.get('entry_group')
    if entry_group in (None, ''):
        entry_group = None
    elif entry_group not in ENTRY_GROUPS:
        problems.add('The "entry_group" must be "year7" (JSS 1) or "year10" (SSS 1), or left out.')
        entry_group = None

    if 'duration_seconds' in data:
        duration = data.get('duration_seconds')
    elif 'duration_minutes' in data and _is_int(data.get('duration_minutes')):
        duration = data['duration_minutes'] * 60
    elif 'duration_minutes' in data:
        duration = None
    else:
        duration = 3600
    if not _is_int(duration) or not MIN_DURATION_SECONDS <= duration <= MAX_DURATION_SECONDS:
        problems.add('The duration must be a whole number of seconds ("duration_seconds") between '
                     f'{MIN_DURATION_SECONDS} (1 minute) and {MAX_DURATION_SECONDS} (6 hours).')
        duration = None

    raw_questions = data.get('questions')
    questions = []
    if not isinstance(raw_questions, list):
        problems.add('The bank needs a "questions" list.')
    elif not raw_questions:
        problems.add('The bank has no questions in it.')
    elif len(raw_questions) > MAX_QUESTIONS:
        problems.add(f'The bank has {len(raw_questions)} questions; the most one bank may hold is {MAX_QUESTIONS}.')
    else:
        seen_ids = set()
        for position, raw in enumerate(raw_questions, 1):
            label = f'Question {position}'
            if not isinstance(raw, dict):
                problems.add(f'{label} must be an object with an id, text, options and answer.')
                continue
            qid = raw.get('id')
            if not _is_int(qid) or not 1 <= qid <= 1_000_000:
                problems.add(f'{label} needs a whole-number "id" of 1 or more.')
            elif qid in seen_ids:
                problems.add(f'{label} repeats the id {qid}; every question needs its own id.')
            else:
                seen_ids.add(qid)
            text = _clean_text(raw.get('text'), f'{label} "text"', 2000, problems)
            instruction = _clean_text(raw.get('instruction'), f'{label} "instruction"', 1000, problems, required=False)

            options = raw.get('options')
            clean_options = None
            if not isinstance(options, list) or len(options) != OPTIONS_PER_QUESTION:
                problems.add(f'{label} needs a list of exactly {OPTIONS_PER_QUESTION} "options" (A to D).')
            else:
                clean_options = [_clean_text(o, f'{label} option {i}', 500, problems)
                                 for i, o in enumerate(options, 1)]
                if None in clean_options:
                    clean_options = None
                elif len({o.casefold() for o in clean_options}) != len(clean_options):
                    problems.add(f'{label} has two options that are the same.')
                    clean_options = None

            answer = raw.get('answer')
            if not _is_int(answer):
                problems.add(f'{label} needs an "answer": the position of the right option, counting from 0.')
            elif clean_options is not None and not 0 <= answer < len(clean_options):
                problems.add(f'{label} has the answer {answer}, but it has only {len(clean_options)} options '
                             f'(the answer counts from 0, so it must be 0 to {len(clean_options) - 1}).')
            points = raw.get('points', 1)
            if not _is_int(points) or not 1 <= points <= 100:
                problems.add(f'{label} "points" must be a whole number from 1 to 100.')

            item = {'id': qid, 'text': text, 'options': clean_options, 'answer': answer, 'points': points}
            if instruction:
                item['instruction'] = instruction
            questions.append(item)

    problems.raise_if_any()
    bank = {'id': bank_id, 'name': name}
    if level:
        bank['level'] = level
    if entry_group:
        bank['entry_group'] = entry_group
    if subject:
        bank['subject'] = subject
    bank.update(duration_seconds=duration, version=version, source_status=source or 'imported',
                questions=questions)
    return bank


def parse_bank_bytes(raw):
    """Decode and validate the bytes of an uploaded bank file. Returns the clean bank."""
    if len(raw) > MAX_BANK_BYTES:
        raise BankError(f'The file is too big: a bank file may be at most {MAX_BANK_BYTES // (1024 * 1024)} MB.')
    if not raw.strip():
        raise BankError('The file is empty.')

    def refuse(constant):
        raise ValueError(constant)

    try:
        data = json.loads(raw.decode('utf-8-sig'), parse_constant=refuse)
    except UnicodeDecodeError:
        raise BankError('The file is not plain text in UTF-8, so it cannot be a question bank.') from None
    except (ValueError, RecursionError):
        raise BankError('The file is not valid JSON. Check it for a missing comma, quote or bracket.') from None
    return check_bank(data)


def read_bank_upload(file_storage):
    """The clean bank in an uploaded file, reading at most one byte over the size limit."""
    stream = getattr(file_storage, 'stream', None)
    if stream is None or not getattr(file_storage, 'filename', ''):
        raise BankError('Choose a bank file (.json) to import.')
    return parse_bank_bytes(stream.read(MAX_BANK_BYTES + 1))


# ---- the school's own folder ----

def _bank_files():
    """(file name, decoded bank or None) for each .json file in this school's folder."""
    folder = data_dir()
    for name in sorted(os.listdir(folder)):
        if not name.endswith('.json') or name == 'manifest.json':
            continue
        try:
            with open(os.path.join(folder, name), encoding='utf-8') as f:
                loaded = json.load(f)
        except (OSError, ValueError):
            loaded = None
        yield name, loaded if isinstance(loaded, dict) else None


def existing_bank_ids():
    """The ids of the banks already in this school's folder."""
    return {str(b['id']) for _, b in _bank_files() if b and b.get('id') and isinstance(b.get('questions'), list)}


def find_bank_files(bank_id):
    """Names of this school's files holding exactly this bank id."""
    return [name for name, b in _bank_files() if b and b.get('id') == bank_id]


def _write_file(folder, name, payload, replace):
    """Write ``payload`` to ``folder/name`` all at once: a reader sees the old file or the
    complete new one, never half of it. Without ``replace`` an existing file is never touched
    (FileExistsError)."""
    dest = os.path.join(folder, name)
    if os.path.islink(dest):
        raise BankError(f'{name} is not an ordinary file, so it will not be written to.')
    fd, tmp = tempfile.mkstemp(prefix='.bank-', suffix='.tmp', dir=folder)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        if replace:
            os.replace(tmp, dest)
        else:
            try:
                os.link(tmp, dest)  # fails if dest exists, and never overwrites
            except FileExistsError:
                raise
            except OSError:
                # A file system without hard links: fall back to a plain check-then-move.
                if os.path.lexists(dest):
                    raise FileExistsError(dest) from None
                os.replace(tmp, dest)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _payload(bank):
    return (json.dumps(bank, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def save_bank_file(bank, replace=False):
    """Write a validated bank into the current school's data folder.

    Returns ``'created'`` or ``'replaced'``. A bank whose id is already there is refused
    unless ``replace`` is true; then the file(s) that hold it are overwritten. The file name
    is always built from the validated id, so nothing can be written outside the folder.
    """
    reason = check_bank_id(bank.get('id'))
    if reason:
        raise BankError(reason)
    folder = data_dir()
    ids = existing_bank_ids()
    bank_id = bank['id']
    clash = [i for i in ids if i.casefold() == bank_id.casefold() and i != bank_id]
    if clash:
        raise BankError(f'A bank called "{clash[0]}" is already here, and bank ids are not case-sensitive. '
                        'Choose a different id.')
    payload = _payload(bank)
    if bank_id in ids:
        if not replace:
            raise BankError(f'A bank with the id "{bank_id}" already exists here. '
                            'It is only replaced when you confirm that.')
        for name in find_bank_files(bank_id):
            _write_file(folder, name, payload, replace=True)
        return 'replaced'
    name = bank_id + '.json'
    if any(existing.casefold() == name.casefold() for existing in os.listdir(folder)):
        raise BankError(f'A file called {name} is already in this school\'s folder, so this bank was not saved.')
    try:
        _write_file(folder, name, payload, replace=False)
    except FileExistsError:
        raise BankError(f'A file called {name} is already in this school\'s folder, so this bank was not saved.') from None
    return 'created'


def sync_new_banks(bank_ids=()):
    """Mirror the school's banks into its examinations table.

    A bank that has just been added is not live until an administrator activates it, the
    same rule as a bank created by hand; the ids of those banks are passed as ``bank_ids``.
    """
    import sqlalchemy as sa

    from core.entrance import sync_examinations
    from models import Examination, db

    sync_examinations()
    if bank_ids:
        db.session.execute(sa.update(Examination).where(Examination.bank_id.in_(list(bank_ids))).values(active=0))
        db.session.commit()


# ---- the platform's standard entrance banks ----

_STARTER_FILE_RE = re.compile(r'^[a-z0-9_]{1,64}\.json$')
ENTRANCE_SUBJECTS = ('mathematics', 'english', 'general_knowledge')


def starter_entries():
    """The standard banks listed in ``starter_banks/manifest.json`` (empty if there is none)."""
    try:
        with open(os.path.join(STARTER_DIR, 'manifest.json'), encoding='utf-8') as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        return []
    entries = []
    for item in manifest.get('banks', []) if isinstance(manifest, dict) else []:
        if (isinstance(item, dict) and _STARTER_FILE_RE.match(str(item.get('file', '')))
                and not check_bank_id(item.get('id')) and item.get('entry_group') in ENTRY_GROUPS
                and item.get('subject') in ENTRANCE_SUBJECTS):
            entries.append(item)
    return entries


def read_starter_bank(entry):
    """One standard bank, run through exactly the same checks as a school's upload."""
    with open(os.path.join(STARTER_DIR, entry['file']), 'rb') as f:
        bank = parse_bank_bytes(f.read(MAX_BANK_BYTES + 1))
    if bank['id'] != entry['id']:
        raise BankError(f'{entry["file"]} holds the bank "{bank["id"]}", not "{entry["id"]}".')
    return bank


def install_starter_banks():
    """Copy the standard banks into the current school's own data folder.

    Safe to run again: a bank the school already has (by id or by file name) is left exactly
    as it is, and nothing is ever overwritten. Returns ``{'created': [ids], 'skipped': [ids]}``.
    """
    folder = data_dir()
    have = {i.casefold() for i in existing_bank_ids()}
    created, skipped = [], []
    for entry in starter_entries():
        bank = read_starter_bank(entry)
        name = bank['id'] + '.json'
        if bank['id'].casefold() in have or any(n.casefold() == name.casefold() for n in os.listdir(folder)):
            skipped.append(bank['id'])
            continue
        try:
            _write_file(folder, name, _payload(bank), replace=False)
        except FileExistsError:
            skipped.append(bank['id'])
            continue
        created.append(bank['id'])
    return {'created': created, 'skipped': skipped}


def standard_slots():
    """The papers the standard set covers: one bank for each entry class and subject."""
    return [{'bank_id': e['id'], 'entry_group': e['entry_group'], 'subject': e['subject'],
             'questions_to_serve': e.get('questions_to_serve')} for e in starter_entries()]


def set_up_standard_papers(session_id, admin_id=None):
    """Make the standard banks the live entrance papers for one academic session.

    A paper is only set up where the school has that standard bank and has not already
    configured that entry class and subject for the session: anything the school chose
    itself is left exactly as it is. Safe to run again. Returns
    ``{'made': [...], 'kept': [...], 'missing': [...]}``, each a list of slots.
    """
    from datetime import datetime, timezone

    import sqlalchemy as sa

    from core.entrance import _valid_question_configuration, load_banks
    from models import EntranceBankConfig, Examination, db

    banks = load_banks()
    now = datetime.now(timezone.utc).isoformat()
    made, kept, missing = [], [], []
    for slot in standard_slots():
        bank = banks.get(slot['bank_id'])
        if not bank:
            missing.append(slot)
            continue
        taken = db.session.scalars(sa.select(EntranceBankConfig.id).where(
            EntranceBankConfig.entry_group == slot['entry_group'],
            EntranceBankConfig.subject == slot['subject'],
            EntranceBankConfig.session_id == session_id,
            EntranceBankConfig.term == 'Full Session')).first()
        if taken:
            kept.append(slot)
            continue
        available = len(bank['questions'])
        count = slot['questions_to_serve'] if _is_int(slot['questions_to_serve']) else available
        count = min(count, available)
        # Every question must be worth a clean share of 100 marks.
        while count > 1 and not _valid_question_configuration(count)[0]:
            count -= 1
        valid, marks = _valid_question_configuration(count)
        if not valid:
            missing.append(slot)
            continue
        db.session.add(EntranceBankConfig(
            bank_id=bank['id'], entry_group=slot['entry_group'], subject=slot['subject'],
            session_id=session_id, term='Full Session', questions_to_serve=count,
            marks_per_question=marks, active=1, practice_enabled=0,
            created_at=now, created_by=admin_id, updated_at=now, updated_by=admin_id))
        db.session.execute(sa.update(Examination).where(Examination.bank_id == bank['id']).values(active=1))
        made.append(slot)
    db.session.commit()
    return {'made': made, 'kept': kept, 'missing': missing}
