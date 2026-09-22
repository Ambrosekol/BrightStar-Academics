"""Each school's own rules for numbering its people.

Schools do not all number their candidates and students the same way, so the rule is not built
into the platform. It is a short **pattern** for each school, such as ``{school}-{year}-{seq:4}``,
kept in the school's own folder as ``tenants/<code>/numbering.json`` and set by the platform
team on the platform console (when the school is created, and later on the school's page).

A pattern is *read*, never run: ``core/numbering_pattern.py`` splits it into pieces, checks each
against a fixed list and fills them in. Nothing typed is executed, so being able to set a school's
numbering does not let anyone run code on the server. This module adds what the language does
not need to know about: where the rules are kept, how a running number is found in a school's
own database, and the two functions the rest of the platform calls:

* ``new_candidate_code(candidate_name, target_class)``
* ``student_number(prefix, include_year, year, sequence, padding)``: how a student's number is
  *written*. It returns ``None`` when the school has no student pattern, and then the school's
  numbering policy decides, exactly as before. The running number, and the ledger that stops a
  number being issued twice, stay with ``services/student_number_generator.py``.

The file is small, checked on every read (a file that is wrong is refused with a plain message,
never quietly replaced by another rule), written whole and atomically, and only ever through the
checks in ``save_rules``. A missing file means the default pattern.
"""

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from control_plane.context import current_tenant
from core.numbering_pattern import (  # noqa: F401  (some are re-exported for callers and tests)
    CANDIDATE, CANDIDATE_CODE, DEFAULT_CANDIDATE_PATTERN, DEFAULT_FIRST_NUMBER, DEFAULT_STUDENT_PATTERN,
    MAX_CODE_LENGTH, MAX_FIRST_NUMBER, MAX_PATTERN_LENGTH, PALETTE, STUDENT, STUDENT_NUMBER, Facts,
    NumberingRuleError, Pattern, PatternError, check_pattern, explain_bad_code, parse, sample_facts,
)
from core.storage import tenant_root

logger = logging.getLogger(__name__)

RULES_FILE = 'numbering.json'
SCHEMA_VERSION = 1
MAX_FILE_BYTES = 4096
MAX_ATTEMPTS = 50
SAMPLES = 3

_KEYS = {'version', 'candidate_pattern', 'student_pattern', 'first_number'}


class RulesFileError(NumberingRuleError):
    """The school's numbering.json is there but cannot be used. It is never replaced silently."""


@dataclass(frozen=True)
class Rules:
    """One school's numbering rules."""

    candidate_pattern: str = DEFAULT_CANDIDATE_PATTERN
    student_pattern: str = DEFAULT_STUDENT_PATTERN
    first_number: int = DEFAULT_FIRST_NUMBER

    def as_file(self):
        return {'version': SCHEMA_VERSION, 'candidate_pattern': self.candidate_pattern,
                'student_pattern': self.student_pattern, 'first_number': self.first_number}


DEFAULT_RULES = Rules()


def is_default(rules):
    return rules == DEFAULT_RULES


# ---------------------------------------------------------------------------------- checking


def read_first_number(value):
    """A first number from what a form or a file holds, or a PatternError saying what is wrong."""
    if isinstance(value, bool):
        raise PatternError('The first number must be a whole number.')
    if isinstance(value, str):
        value = value.strip()
        if not value.isascii() or not value.isdigit():
            raise PatternError('The first number must be a whole number, 0 or more (for example 1).')
        value = int(value)
    if not isinstance(value, int) or not 0 <= value <= MAX_FIRST_NUMBER:
        raise PatternError(f'The first number must be a whole number from 0 to {MAX_FIRST_NUMBER}.')
    return value


def check_rules(candidate_pattern, student_pattern, first_number, facts=None):
    """Prove a set of rules is acceptable and return it as :class:`Rules`, or raise
    :class:`PatternError`. ``facts`` (the school's real code and name) lets the longest code
    the pattern can make be checked against the limit."""
    first = read_first_number(first_number)
    if not isinstance(candidate_pattern, str) or not isinstance(student_pattern, str):
        raise PatternError('A pattern must be text.')
    if candidate_pattern == '':
        raise PatternError('The candidate code pattern cannot be empty. Use "Reset to default" for '
                           f'{DEFAULT_CANDIDATE_PATTERN}.')
    for label, kind, text in (('Candidate code pattern', CANDIDATE, candidate_pattern),
                              ('Student number pattern', STUDENT, student_pattern)):
        if kind == STUDENT and text == '':
            continue
        try:
            check_pattern(text, kind, facts, first)
        except PatternError as exc:
            raise PatternError(f'{label}: {exc}', exc.position, exc.length) from None
    return Rules(candidate_pattern, student_pattern, first)


# ---------------------------------------------------------------------------------- the file


def _rules_error(why):
    return RulesFileError(
        f"This school's numbering rules ({RULES_FILE}) could not be used: {why}. Nothing has been "
        "changed. The platform team can set the rules again from the school's page in the console.")


def _inside(root, path):
    try:
        return os.path.commonpath([root, path]) == root
    except ValueError:  # Windows: another drive has no common path at all
        return False


def read_rules(root):
    """The rules kept in a school's folder. The default if there is no file; a plain refusal
    (:class:`RulesFileError`) if there is one that is wrong."""
    root = Path(root)
    path = root / RULES_FILE
    if not path.exists() and not path.is_symlink():
        return DEFAULT_RULES
    # A link that leads out of the school's folder is never followed.
    if not _inside(os.path.realpath(root), os.path.realpath(path)):
        raise _rules_error("the file must be a file inside the school's own folder")
    try:
        if not path.is_file():
            raise _rules_error('it is not a file')
        if path.stat().st_size > MAX_FILE_BYTES:
            raise _rules_error('the file is far larger than a numbering file can be')
        data = json.loads(path.read_text(encoding='utf-8'))
    except RulesFileError:
        raise
    except (OSError, ValueError) as exc:  # JSON and text-decoding errors are both ValueErrors
        raise _rules_error(f'it is not readable ({type(exc).__name__})') from None
    if not isinstance(data, dict) or set(data) != _KEYS:
        raise _rules_error('it does not have the expected entries')
    if data['version'] != SCHEMA_VERSION or isinstance(data['version'], bool):
        raise _rules_error('it is from a newer or unknown version of the platform')
    try:
        return check_rules(data['candidate_pattern'], data['student_pattern'], data['first_number'])
    except PatternError as exc:
        raise _rules_error(str(exc).rstrip('.')) from None


def _write(root, rules):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    text = json.dumps(rules.as_file(), indent=2) + '\n'
    handle, temporary = tempfile.mkstemp(prefix='.numbering-', suffix='.tmp', dir=root)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8', newline='\n') as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, root / RULES_FILE)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise NumberingRuleError("The numbering rules could not be saved: the school's folder cannot be "
                                 f'written to ({type(exc).__name__}).') from None


def write_rules(root, rules, facts=None):
    """Save rules into a school's folder, whole and atomically. The rules are checked again
    here, so nothing that has not passed the checks can reach the file."""
    checked = check_rules(rules.candidate_pattern, rules.student_pattern, rules.first_number, facts)
    _write(root, checked)
    return checked


def save_rules(root, rules, facts=None):
    """Replace a school's rules. Returns ``(old, unreadable)``: the rules that were there (the
    default if there was no file) and, if the old file could not be read, why not.

    A file that cannot be read is refused everywhere else, but the platform team must be able to
    put a school right without touching the server, so an explicit, checked save may replace it;
    a copy of the unreadable file is kept beside it, so nothing is lost.
    """
    unreadable = None
    try:
        old = read_rules(root)
    except RulesFileError as exc:
        old, unreadable = None, str(exc)
    checked = check_rules(rules.candidate_pattern, rules.student_pattern, rules.first_number, facts)
    path = Path(root) / RULES_FILE
    if unreadable and path.is_file() and not path.is_symlink():
        try:
            path.replace(path.with_name(f'numbering.unreadable-{datetime.now():%Y%m%d-%H%M%S}.json'))
        except OSError:
            pass
    elif unreadable and path.is_symlink():
        try:
            path.unlink()  # only the link goes; whatever it pointed at is left alone
        except OSError:
            pass
    _write(root, checked)
    return old, unreadable


# ---------------------------------------------------------------------------------- the database


def _candidate_codes(prefix):
    """Every existing candidate code that starts with ``prefix`` (all of them if it is empty)."""
    from models import Candidate, db

    query = select(Candidate.candidate_code)
    if prefix:
        query = query.where(Candidate.candidate_code.startswith(prefix, autoescape=True))
    return [code for (code,) in db.session.execute(query)]


def _candidate_taken(code):
    from models import Candidate, db

    return db.session.scalars(select(Candidate.id).where(Candidate.candidate_code == code).limit(1)).first() is not None


def _student_number_taken(number):
    from models import Student, StudentNumberAllocation, db

    return (db.session.scalars(select(Student.id).where(Student.student_number == number).limit(1)).first()
            is not None
            or db.session.scalars(select(StudentNumberAllocation.id).where(
                StudentNumberAllocation.student_number == number).limit(1)).first() is not None)


def _runtime_facts(**details):
    from core.branding import code_prefix, school_name  # deferred: core.branding imports the app

    return Facts(school_code=code_prefix(), school_name=school_name(), now=datetime.now(), **details)


def _refusal(exc):
    """A rule's refusal, with the way out for the person reading it."""
    if isinstance(exc, RulesFileError):
        return exc
    return NumberingRuleError(f"{exc} Ask the platform team to correct this school's numbering.")


def new_candidate_code(candidate_name='', target_class=''):
    """The next candidate code, made by this school's own pattern.

    The number after the pattern's other parts goes on from the biggest one already used by a
    code that is the same in all of them; a code that is already taken moves on to the next.
    ``candidate_name`` is accepted so callers need not change, but no placeholder uses it.
    """
    tenant = current_tenant()
    rules = read_rules(tenant_root(tenant))
    pattern = parse(rules.candidate_pattern, CANDIDATE)
    facts = _runtime_facts(target_class=target_class or '')
    try:
        start = None
        if pattern.has_seq:
            start = pattern.next_number(facts, rules.first_number,
                                        _candidate_codes(pattern.leading_text(facts)))
        code = ''
        for attempt in range(MAX_ATTEMPTS):
            code = pattern.render(facts, None if start is None else start + attempt)
            if not _candidate_taken(code):
                return code
    except NumberingRuleError as exc:
        raise _refusal(exc) from None
    raise NumberingRuleError(
        f"This school's candidate code pattern kept making codes that are already in use (last: {code}). "
        "Ask the platform team to correct this school's numbering.")


def student_number(prefix, include_year, year, sequence, padding):
    """The written form of a student number if the school has its own pattern, else ``None``
    (and the school's numbering policy decides).

    The running number, and the record that it was issued, stay with the platform
    (services/student_number_generator.py): the pattern only chooses how the number is written,
    so ``include_year`` and ``padding`` (which are for the policy's own form) are not used here.
    """
    tenant = current_tenant()
    rules = read_rules(tenant_root(tenant))
    if not rules.student_pattern:
        return None
    pattern = parse(rules.student_pattern, STUDENT)
    facts = _runtime_facts(year=int(year), prefix=str(prefix or ''))
    try:
        number = ''
        for _ in range(MAX_ATTEMPTS if pattern.has_random else 1):
            number = pattern.render(facts, int(sequence))
            # Only a number with a random part can be made again differently; a running number
            # that is already used is the platform's own duplicate check to report.
            if not pattern.has_random or not _student_number_taken(number):
                return number
    except NumberingRuleError as exc:
        raise _refusal(exc) from None
    raise NumberingRuleError(
        f"This school's student number pattern kept making numbers that are already in use (last: {number}). "
        "Ask the platform team to correct this school's numbering.")


# ---------------------------------------------------------------------------------- previews


def _problem(exc):
    return {'ok': False, 'error': str(exc), 'position': getattr(exc, 'position', None),
            'length': getattr(exc, 'length', 1)}


def preview(candidate_pattern, student_pattern, first_number, facts, *, candidate_codes=None,
            policy=None, count=SAMPLES):
    """What a set of rules would make, for the console. Reads nothing and writes nothing itself:
    the school's existing codes come in through ``candidate_codes`` (a function from a prefix to
    the codes that start with it; None when there is no school yet) and its numbering policy
    through ``policy`` (prefix, include_year, padding, next_sequence), so the page can show the
    real next numbers.

    Returns ``{'candidate': ..., 'student': ..., 'first_number': ...}``; each part is
    ``{'ok': True, 'samples': [...]}`` or ``{'ok': False, 'error': message, 'position': n}``.
    """
    out = {}
    try:
        first = read_first_number(first_number)
        out['first_number'] = {'ok': True, 'value': first}
    except PatternError as exc:
        first = DEFAULT_FIRST_NUMBER
        out['first_number'] = _problem(exc)

    policy = policy or {}
    try:
        if not isinstance(candidate_pattern, str) or candidate_pattern == '':
            raise PatternError('The candidate code pattern cannot be empty.')
        pattern = check_pattern(candidate_pattern, CANDIDATE, facts, first)
        samples, note = [], ''
        if pattern.has_seq:
            start = first
            if candidate_codes is not None:
                start = pattern.next_number(facts, first, candidate_codes(pattern.leading_text(facts)))
                note = "Next numbers, going on from this school's own candidates."
            else:
                note = 'Example numbers, as they would begin for a new school.'
            for offset in range(count):
                try:
                    samples.append(pattern.render(facts, start + offset))
                except NumberingRuleError as exc:
                    samples.append(None)
                    note = str(exc)
                    break
        else:
            samples = [pattern.render(facts) for _ in range(count)]
            note = 'Random example codes; each real code is made when the candidate is registered.'
        out['candidate'] = {'ok': True, 'samples': [s for s in samples if s], 'note': note,
                            'uses_class': pattern.uses_class}
    except NumberingRuleError as exc:
        out['candidate'] = _problem(exc)

    try:
        if student_pattern == '':
            out['student'] = {'ok': True, 'empty': True, 'samples': _policy_samples(policy, facts, count),
                              'note': "Left empty: the school's own numbering policy writes each number."}
        else:
            pattern = check_pattern(student_pattern, STUDENT, facts, first)
            sequence = int(policy.get('next_sequence') or 1)
            student_facts = Facts(facts.school_code, facts.school_name, facts.now, facts.year, '',
                                  str(policy.get('prefix') or facts.school_code))
            samples = []
            for offset in range(count):
                try:
                    samples.append(pattern.render(student_facts, sequence + offset))
                except NumberingRuleError as exc:
                    out['student'] = {'ok': False, 'error': str(exc), 'position': None, 'length': 1}
                    break
            else:
                out['student'] = {'ok': True, 'samples': samples,
                                  'note': "Written from the platform's own running number for students."}
    except NumberingRuleError as exc:
        out['student'] = _problem(exc)
    return out


def _policy_samples(policy, facts, count):
    """Three numbers as the school's numbering policy would write them."""
    from services.student_number_generator import _build_student_number  # deferred: imports the models

    prefix = policy.get('prefix') or facts.school_code
    start = int(policy.get('next_sequence') or 1)
    try:
        return [_build_student_number(prefix, int(policy.get('include_year', 1)), facts.year, start + i,
                                      int(policy.get('padding') or 4)) for i in range(count)]
    except Exception:  # a policy that cannot be written is the generator's to report, not the preview's
        return []
