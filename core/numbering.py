"""Each school's own rules for numbering its people.

Schools do not all number their candidates and students the same way, so the rule is not
built into the platform. It is a small Python file that lives in the school's own folder,
``tenants/<code>/numbering.py``. This module finds that file, runs it, and checks what
comes back. Nothing here knows what a good code looks like for any particular school.

* A new school is given a copy of ``tenant_starter/numbering.py`` when its folder is made
  (``install_rules_file``). A school that predates this gets the same copy the first time
  it needs a number, so it goes on numbering exactly as it did.
* The file is read again whenever it changes, so an edit takes effect on the next number.
* A rule that fails is refused with a plain message. It never falls back to another rule:
  a school's codes must follow that school's rule or none.

The file is program code, so only the people who run the platform may edit it (it is on
the server's disk, and nothing in the portal writes to it). The checks below are there to
keep a slip from damaging data, not to make untrusted code safe.
"""

import importlib.util
import logging
import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from control_plane.context import current_tenant
from core.storage import tenant_root

logger = logging.getLogger(__name__)

RULES_FILE = 'numbering.py'
STARTER_FILE = Path(__file__).resolve().parent.parent / 'tenant_starter' / RULES_FILE

# The code is typed at sign-in, printed on papers and used in file names, so it is kept to
# characters that are safe everywhere. A candidate signs in with the code in capitals.
CANDIDATE_CODE = re.compile(r'^[A-Z0-9][A-Z0-9._-]{2,39}$')
# A student number may also use "/" (ABC/2026/0001), which is what the policy has always made.
STUDENT_NUMBER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/-]{0,39}$')
MAX_ATTEMPTS = 50


class NumberingRuleError(Exception):
    """The school's numbering rule could not produce a usable number. The message is safe to
    show to school staff."""


_lock = threading.RLock()
_loaded = {}  # school code -> (fingerprint of the file, module)


def rules_path(tenant=None):
    return Path(tenant_root(tenant or current_tenant())) / RULES_FILE


def install_rules_file(root):
    """Put the starter rules in a school's folder if it has none. Never overwrites.

    Returns True if a file was written. Written with "exclusive create", so two workers
    starting together cannot overwrite each other, nor a file someone has just edited.
    """
    target = Path(root) / RULES_FILE
    if target.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(target, 'x', encoding='utf-8', newline='\n') as handle:
            handle.write(STARTER_FILE.read_text(encoding='utf-8'))
    except FileExistsError:
        return False
    return True


def _module_name(tenant):
    return 'brightstars_school_rules_' + re.sub(r'[^a-z0-9]', '_', tenant.slug.lower())


def _fingerprint(path):
    info = path.stat()
    return (info.st_mtime_ns, info.st_size)


def _load(path, name, label):
    """Run one rules file as a module of its own. Not shared with any other school."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(name, None)
        logger.exception('The numbering rules for %s could not be loaded', label)
        raise NumberingRuleError(
            f"This school's numbering rules ({RULES_FILE}) could not be read: "
            f'{type(exc).__name__}: {exc}. Ask the platform team to correct that file.') from exc
    return module


def _inside(root, path):
    try:
        return os.path.commonpath([root, path]) == root
    except ValueError:  # Windows: another drive has no common path at all
        return False


def _rules(tenant):
    """The rules module for a school, read again if the file has changed."""
    path = rules_path(tenant)
    root = os.path.realpath(path.parent)
    if not path.exists():
        try:
            install_rules_file(path.parent)
        except OSError:
            # A folder that cannot be written to still numbers by the platform's own starter
            # rules, which are the same rules a new school begins with.
            logger.warning('Could not write %s for %s; using the starter rules.', RULES_FILE, tenant.slug)
            path = STARTER_FILE
    # A link that leads out of the school's folder is never followed.
    if path != STARTER_FILE and not _inside(root, os.path.realpath(path)):
        raise NumberingRuleError(f"{RULES_FILE} must be a file inside the school's own folder.")

    with _lock:
        fingerprint = _fingerprint(path)
        cached = _loaded.get(tenant.slug)
        if cached and cached[0] == (str(path), fingerprint):
            return cached[1]
        module = _load(path, _module_name(tenant), tenant.slug)
        _loaded[tenant.slug] = ((str(path), fingerprint), module)
        return module


def _rule(name, tenant, required=True):
    module = _rules(tenant)
    function = getattr(module, name, None)
    if function is None:
        if required:
            raise NumberingRuleError(f"This school's {RULES_FILE} has no {name}() function.")
        return None
    if not callable(function):
        raise NumberingRuleError(f'{name} in {RULES_FILE} is not a function.')
    return function


def _run(function, name, ctx):
    try:
        return function(ctx)
    except NumberingRuleError:
        raise
    except Exception as exc:
        logger.exception('The %s rule for %s failed', name, ctx.school_slug)
        raise NumberingRuleError(
            f"This school's {name}() rule failed: {type(exc).__name__}: {exc}. "
            'Ask the platform team to correct its numbering.py.') from exc


def _text(value, name, pattern, upper=False):
    if not isinstance(value, str):
        raise NumberingRuleError(f'{name}() must return text, not {type(value).__name__}.')
    value = value.strip()
    if upper:
        value = value.upper()
    if not pattern.match(value):
        allowed = 'letters, digits and . _ - /' if pattern is STUDENT_NUMBER else 'letters, digits and . _ -'
        length = 'at most 40 characters' if pattern is STUDENT_NUMBER else '3 to 40 characters'
        raise NumberingRuleError(
            f'{name}() returned {value[:60]!r}, which is not allowed: use {allowed}, starting with '
            f'a letter or digit, {length}.')
    return value


class _Context:
    """What a rule is offered to look at: facts about the school and a few questions it can ask."""

    def __init__(self, tenant, **details):
        from core.branding import code_prefix, school_name  # deferred: core.branding imports the app

        self.school_slug = tenant.slug
        self.school_code = code_prefix()
        self.school_name = school_name()
        self.now = datetime.now()
        self.year = self.now.year
        self.attempt = 0
        for key, value in details.items():
            setattr(self, key, value)

    # ---- helpers for candidate codes -------------------------------------------------------
    def last_number(self, prefix):
        """The biggest whole number already used after ``prefix`` in a candidate code."""
        from models import Candidate, db

        biggest = 0
        for (code,) in db.session.execute(
                select(Candidate.candidate_code)
                .where(Candidate.candidate_code.startswith(str(prefix).upper(), autoescape=True))):
            tail = code[len(str(prefix)):]
            if tail.isdigit():
                biggest = max(biggest, int(tail))
        return biggest

    def next_in_sequence(self, prefix, digits=4):
        number = self.last_number(prefix) + 1 + self.attempt
        return f'{prefix}{number:0{int(digits)}d}'

    def taken(self, code):
        from models import Candidate, db

        return db.session.scalars(select(Candidate.id).where(
            Candidate.candidate_code == str(code).strip().upper()).limit(1)).first() is not None


def new_candidate_code(candidate_name='', target_class=''):
    """The next candidate code, made by this school's own rule."""
    tenant = current_tenant()
    function = _rule('candidate_code', tenant)
    ctx = _Context(tenant, candidate_name=candidate_name or '', target_class=target_class or '')
    for attempt in range(MAX_ATTEMPTS):
        ctx.attempt = attempt
        code = _text(_run(function, 'candidate_code', ctx), 'candidate_code', CANDIDATE_CODE, upper=True)
        if not ctx.taken(code):
            return code
    raise NumberingRuleError(
        f"This school's candidate_code() rule kept returning codes that are already in use "
        f'(last: {code}). Ask the platform team to correct its numbering.py.')


def student_number(prefix, include_year, year, sequence, padding):
    """The written form of a student number if the school has its own rule, else ``None``
    (and the school's numbering policy decides).

    The running number itself, and the record that it was issued, stay with the platform
    (services/student_number_generator.py): the rule only chooses how the number is written.
    """
    tenant = current_tenant()
    function = _rule('student_number', tenant, required=False)
    if function is None:
        return None
    ctx = _Context(tenant, prefix=prefix, include_year=bool(include_year), year=int(year),
                   sequence=int(sequence), padding=int(padding))
    return _text(_run(function, 'student_number', ctx), 'student_number', STUDENT_NUMBER)
