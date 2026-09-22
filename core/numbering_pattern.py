"""The small pattern language a school's numbers are written in.

A pattern is plain text with ``{placeholders}`` in it, for example ``{school}-{year}-{seq:4}``.
This module READS a pattern: it splits it into pieces, checks every piece against the fixed
list below, and fills the pieces in. **Nothing typed is ever run.** There is no eval, exec,
compile, import, template engine or ``format`` call on typed text anywhere in this file; a
piece that is not on the list is refused with a message that names it and says where it is.
That is what makes it safe to let the platform's operators type a school's numbering rule on a
web page: the worst a pattern can do is make an odd-looking number, and even that is refused
unless it passes the same safety checks every code has always had.

The language (see ``PALETTE`` for the same list with examples, which the console shows):

  literal text     letters, digits and - _ .   (and / in student numbers only)
  {school}         the school's code in capitals            ABC
  {initials}       initials of the school's name            Bright Future Academy -> BFA
  {year} {yy}      4-digit / 2-digit year                   2026 / 26
  {month} {mon}    2-digit month / 3-letter month           09 / SEP
  {day}            2-digit day                              05
  {class}          the class applied for, compacted         JSS 1 -> JSS1  (candidate codes only)
  {seq}            the running number                       7
  {seq:4}          padded with zeros to exactly 4 digits    0007
  {seq:4-5}        at least 4 digits, growing to at most 5  0007 ... 99999
  {random:N}       N random digits          {letters:N} N random capital letters (no I or O)
  {alnum:N}        N random letters and digits
  {prefix}         the numbering policy's prefix            (student numbers only)
  {name|lower}     an optional case filter, ``|upper`` or ``|lower``, on placeholders that are
                   words (student numbers only for ``|lower``: candidate codes are always
                   capitals, because that is how a candidate signs in)

A pattern must contain ``{seq}`` or a random placeholder, or every code would be the same, and
at most one ``{seq}``.
"""

import difflib
import re
import secrets
import string
from dataclasses import dataclass, replace
from datetime import datetime

CANDIDATE = 'candidate'
STUDENT = 'student'
KINDS = (CANDIDATE, STUDENT)

# The code is typed at sign-in, printed on papers and used in file names, so it is kept to
# characters that are safe everywhere. A candidate signs in with the code in capitals.
CANDIDATE_CODE = re.compile(r'^[A-Z0-9][A-Z0-9._-]{2,39}$')
# A student number may also use "/" (ABC/2026/0001), which is what the policy has always made.
STUDENT_NUMBER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/-]{0,39}$')

MAX_PATTERN_LENGTH = 80
MAX_CODE_LENGTH = 40
MAX_DIGITS = 12                 # the most digits one {seq}, {random}, {letters} or {alnum} may have
NOMINAL_CLASS_LENGTH = 8        # what {class} is assumed to need when the longest code is worked out

MONTHS = ('JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC')
DIGITS = string.digits
LETTERS = ''.join(c for c in string.ascii_uppercase if c not in 'IO')   # no look-alikes for 1 and 0
ALNUM = LETTERS + DIGITS
# Small words that are not part of a school's initials ("Bright Stars of the Future" -> BSF).
CONNECTING_WORDS = frozenset({'of', 'and', 'the', 'for', 'in', 'at', 'de', 'la', 'a', 'an'})
MAX_INITIALS = 8


class NumberingRuleError(Exception):
    """A numbering rule could not produce a usable number, or a pattern is not acceptable.
    The message is safe to show to staff."""


class PatternError(NumberingRuleError):
    """A pattern that is not acceptable. ``position`` (counting from 1) is where the problem
    starts, ``length`` how many characters it covers; both are None when it is about the
    pattern as a whole."""

    def __init__(self, message, position=None, length=1):
        super().__init__(message)
        self.position = position
        self.length = length


# ---------------------------------------------------------------------------------- the list


@dataclass(frozen=True)
class Spec:
    name: str
    meaning: str
    kinds: tuple = KINDS
    takes: str = ''        # '' nothing, 'width' for {seq}, 'count' for the random ones
    word: bool = False     # text rather than a number, so it can take |upper / |lower
    random: bool = False


SPECS = {spec.name: spec for spec in (
    Spec('school', "the school's code in capitals", word=True),
    Spec('initials', "the initials of the school's name", word=True),
    Spec('year', 'the 4-digit year'),
    Spec('yy', 'the 2-digit year'),
    Spec('month', 'the 2-digit month'),
    Spec('mon', 'the 3-letter month', word=True),
    Spec('day', 'the 2-digit day of the month'),
    Spec('class', 'the class applied for, without spaces and in capitals', kinds=(CANDIDATE,), word=True),
    Spec('seq', 'the running number', takes='width'),
    Spec('random', 'random digits', takes='count', random=True),
    Spec('letters', 'random capital letters', takes='count', word=True, random=True),
    Spec('alnum', 'random capital letters and digits', takes='count', word=True, random=True),
    Spec('prefix', "the numbering policy's prefix", kinds=(STUDENT,), word=True),
)}

# The chips the console offers, with what each means and an example. The reference table on the
# page is drawn from this same list, so the documentation cannot drift from what is accepted.
# (token, meaning, example, kinds)
PALETTE = (
    ('{school}', "The school's code, in capitals.", 'ABC', KINDS),
    ('{initials}', "The initials of the school's name.", 'Bright Future Academy gives BFA', KINDS),
    ('{year}', 'The 4-digit year.', '2026', KINDS),
    ('{yy}', 'The 2-digit year.', '26', KINDS),
    ('{month}', 'The 2-digit month.', '09', KINDS),
    ('{mon}', 'The 3-letter month, in capitals.', 'SEP', KINDS),
    ('{day}', 'The 2-digit day of the month.', '05', KINDS),
    ('{class}', 'The class applied for, without spaces and in capitals. Empty if it is not known.',
     'JSS 1 gives JSS1', (CANDIDATE,)),
    ('{seq}', 'The running number, not padded. It goes on from the biggest number already used '
              'by a code that is the same in every other part.', '7', KINDS),
    ('{seq:4}', 'The running number, padded with zeros to exactly 4 digits.', '0007', KINDS),
    ('{seq:4-5}', 'Padded to at least 4 digits; it may grow to 5 digits, and no further.', '0007, later 12345', KINDS),
    ('{random:4}', '4 random digits.', '4827', KINDS),
    ('{letters:4}', '4 random capital letters (never I or O, which look like 1 and 0).', 'KWTB', KINDS),
    ('{alnum:4}', '4 random capital letters and digits (never I or O).', 'K7WB', KINDS),
    ('{prefix}', "The prefix of the school's numbering policy.", 'ABC', (STUDENT,)),
)

DEFAULT_CANDIDATE_PATTERN = '{school}-{year}-{seq:4}'
DEFAULT_STUDENT_PATTERN = ''
DEFAULT_FIRST_NUMBER = 1
MAX_FIRST_NUMBER = 999_999_999


# ---------------------------------------------------------------------------------- the facts


@dataclass
class Facts:
    """What a pattern can look at when a number is made."""

    school_code: str = 'ABC'
    school_name: str = 'Bright Future Academy'
    now: datetime = None
    year: int = None
    target_class: str = ''
    prefix: str = ''

    def __post_init__(self):
        if self.now is None:
            self.now = datetime.now()
        if self.year is None:
            self.year = self.now.year


def sample_facts(school_code='ABC', school_name='Bright Future Academy', prefix='ABC', target_class='JSS 1'):
    return Facts(school_code=school_code, school_name=school_name, now=datetime.now(),
                 target_class=target_class, prefix=prefix)


def initials_of(name):
    """"Bright Future Academy" -> "BFA". Small connecting words are left out."""
    words = re.findall(r"[A-Za-z0-9]+", re.sub(r"['’]", '', name or ''))  # "Mary's" is one word
    kept = [w for w in words if w.lower() not in CONNECTING_WORDS] or words
    return ''.join(w[0] for w in kept[:MAX_INITIALS]).upper()


def compact_class(text):
    """"JSS 1" -> "JSS1"."""
    return re.sub(r'[^A-Za-z0-9]', '', text or '').upper()


# ---------------------------------------------------------------------------------- reading


@dataclass(frozen=True)
class Token:
    kind: str            # 'text' (literal) or 'field' (a {placeholder})
    text: str            # the literal text, or the whole {placeholder} as typed
    pos: int             # where it starts, counting from 1
    name: str = ''
    low: int = 0         # {seq}: least digits (0 = no padding); random ones: how many
    high: int = 0        # {seq}: most digits (0 = no limit); random ones: how many
    case: str = ''       # '', 'upper' or 'lower'

    @property
    def spec(self):
        return SPECS[self.name]


_LITERAL = re.compile(r'[A-Za-z0-9._-]')
_WIDTH = re.compile(r'^(\d{1,2})(?:-(\d{1,2}))?$')
_COUNT = re.compile(r'^\d{1,2}$')


def _shown(text, limit=24):
    text = text if len(text) <= limit else text[:limit] + '...'
    return text


def _names_for(kind):
    return ' '.join('{' + n + '}' for n, s in SPECS.items() if kind in s.kinds)


def _char_name(char):
    if char == ' ':
        return 'a space'
    if char == '\t':
        return 'a tab'
    return repr(char) if char.isprintable() else 'an unprintable character'


def _read_field(body, pos, kind):
    """One ``{...}`` (``body`` is what is between the braces) as a Token."""
    length = len(body) + 2
    typed = '{' + _shown(body) + '}'
    if body == '':
        raise PatternError(f'Empty braces at position {pos}: put a placeholder name between them, '
                           f'for example {{year}}.', pos, 2)
    spec_part, bar, case = body.partition('|')
    name, colon, arg = spec_part.partition(':')
    if '|' in case:
        raise PatternError(f'{typed} at position {pos} has more than one case filter; use one, '
                           f'|upper or |lower.', pos, length)
    if ':' in arg:
        raise PatternError(f'{typed} at position {pos} has more than one option after the name.', pos, length)
    if name not in SPECS:
        if name.lower() in SPECS:
            raise PatternError(f'Placeholders are written in lower case: use {{{name.lower()}}} instead '
                               f'of {typed} at position {pos}.', pos, length)
        near = difflib.get_close_matches(name.lower(), list(SPECS), n=1)
        hint = f' Did you mean {{{near[0]}}}?' if near else ''
        raise PatternError(f'Unknown placeholder {typed} at position {pos}.{hint} '
                           f'You can use: {_names_for(kind)}.', pos, length)
    spec = SPECS[name]
    if kind not in spec.kinds:
        if kind == CANDIDATE:
            raise PatternError(f'{{{name}}} at position {pos} is only available in student numbers.', pos, length)
        raise PatternError(f'{{{name}}} at position {pos} is only available in candidate codes; '
                           f'it is not known when a student number is made.', pos, length)

    low = high = 0
    if spec.takes == '':
        if colon:
            raise PatternError(f'{{{name}}} at position {pos} takes no options, so nothing may follow '
                               f'the name but a case filter.', pos, length)
    elif spec.takes == 'width':
        if colon:
            found = _WIDTH.match(arg)
            if not found:
                raise PatternError(f'{typed} at position {pos}: after {{seq: put the number of digits, '
                                   f'like {{seq:4}}, or a range like {{seq:4-5}}.', pos, length)
            low = int(found.group(1))
            high = int(found.group(2)) if found.group(2) else low
            if not 1 <= low <= MAX_DIGITS or not low <= high <= MAX_DIGITS:
                raise PatternError(f'{typed} at position {pos}: the digits must be from 1 to {MAX_DIGITS}, '
                                   f'and the second number of a range must not be smaller than the first.',
                                   pos, length)
    else:  # count
        if not colon or not _COUNT.match(arg) or not 1 <= int(arg) <= MAX_DIGITS:
            raise PatternError(f'{{{name}}} at position {pos} needs how many to make, from 1 to {MAX_DIGITS}, '
                               f'like {{{name}:4}}.', pos, length)
        low = high = int(arg)

    if bar:
        if case not in ('upper', 'lower'):
            raise PatternError(f'Unknown case filter |{_shown(case)} at position {pos}: use |upper or |lower.',
                               pos, length)
        if not spec.word:
            raise PatternError(f'{{{name}}} is a number, so a case filter cannot be used with it '
                               f'(position {pos}).', pos, length)
        if kind == CANDIDATE and case == 'lower':
            raise PatternError(f'Candidate codes are always made capitals (that is how a candidate signs in), '
                               f'so |lower at position {pos} could have no effect.', pos, length)
    return Token('field', '{' + body + '}', pos, name, low, high, case)


def tokenize(text, kind):
    """Split a pattern into literal text and placeholders. Refuses anything not allowed."""
    if kind not in KINDS:
        raise ValueError(kind)
    if not isinstance(text, str):
        raise PatternError('A pattern must be text.')
    if len(text) > MAX_PATTERN_LENGTH:
        raise PatternError(f'This pattern is {len(text)} characters long; the most allowed is '
                           f'{MAX_PATTERN_LENGTH}.', MAX_PATTERN_LENGTH + 1, len(text) - MAX_PATTERN_LENGTH)
    tokens, literal, i = [], '', 0
    literal_at = 1
    allowed = ('letters, digits, "-", "_", "." and "/"' if kind == STUDENT
               else 'letters, digits, "-", "_" and "."')

    def flush():
        nonlocal literal
        if literal:
            tokens.append(Token('text', literal, literal_at))
            literal = ''

    while i < len(text):
        char = text[i]
        if char == '{':
            flush()
            j = i + 1
            while j < len(text) and text[j] not in '{}':
                j += 1
            if j >= len(text):
                raise PatternError(f"The '{{' at position {i + 1} is never closed: add a '}}' after the "
                                   f'placeholder name.', i + 1)
            if text[j] == '{':
                raise PatternError(f"Unbalanced braces: the '{{' at position {i + 1} is not closed before "
                                   f"the next '{{' at position {j + 1}.", i + 1, j - i)
            tokens.append(_read_field(text[i + 1:j], i + 1, kind))
            i = j + 1
            literal_at = i + 1
            continue
        if char == '}':
            raise PatternError(f"Unbalanced braces: the '}}' at position {i + 1} has no '{{' before it.", i + 1)
        if char == '/' and kind == CANDIDATE:
            raise PatternError(f"A '/' at position {i + 1} is only allowed in student numbers: a candidate "
                               f'signs in with the code, so it uses {allowed}.', i + 1)
        if not (_LITERAL.match(char) or (char == '/' and kind == STUDENT)):
            raise PatternError(f'{_char_name(char).capitalize()} at position {i + 1} is not allowed in a '
                               f'pattern. Plain text may use {allowed}; anything else goes inside a '
                               f'{{placeholder}}.', i + 1)
        if not literal:
            literal_at = i + 1
        literal += char
        i += 1
    flush()
    return tokens


def explain_bad_code(code, kind):
    """Why a finished code is not acceptable, in words; None if it is."""
    pattern = CANDIDATE_CODE if kind == CANDIDATE else STUDENT_NUMBER
    if pattern.fullmatch(code):
        return None
    minimum = 3 if kind == CANDIDATE else 1
    if len(code) < minimum:
        return f'it is shorter than {minimum} characters'
    if len(code) > MAX_CODE_LENGTH:
        return f'it is longer than {MAX_CODE_LENGTH} characters'
    if not code[:1].isascii() or not code[:1].isalnum():
        return 'it must start with a letter or a digit'
    return 'it may use only ' + ('letters, digits and . _ - /' if kind == STUDENT else 'letters, digits and . _ -')


# ---------------------------------------------------------------------------------- a pattern


def _draw(alphabet, count, pick):
    return ''.join(pick(alphabet) for _ in range(count))


class Pattern:
    """A pattern that has been read and checked. Fill it in with :meth:`render`."""

    def __init__(self, kind, source, tokens):
        self.kind = kind
        self.source = source
        self.tokens = tokens
        fields = [t for t in tokens if t.kind == 'field']
        self.seq = next((t for t in fields if t.name == 'seq'), None)
        self.has_seq = self.seq is not None
        self.has_random = any(t.spec.random for t in fields)
        self.uses_class = any(t.name == 'class' for t in fields)

    def __repr__(self):
        return f'Pattern({self.kind}, {self.source!r})'

    # ---- filling in
    def _value(self, token, facts, sequence, pick):
        name = token.name
        if name == 'school':
            value = (facts.school_code or '').upper()
        elif name == 'initials':
            value = initials_of(facts.school_name) or (facts.school_code or '').upper()
        elif name == 'year':
            value = f'{int(facts.year):04d}'
        elif name == 'yy':
            value = f'{int(facts.year) % 100:02d}'
        elif name == 'month':
            value = f'{facts.now.month:02d}'
        elif name == 'mon':
            value = MONTHS[facts.now.month - 1]
        elif name == 'day':
            value = f'{facts.now.day:02d}'
        elif name == 'class':
            value = compact_class(facts.target_class)
        elif name == 'prefix':
            value = str(facts.prefix or '')
        elif name == 'seq':
            value = self._sequence_text(token, sequence)
        elif name == 'random':
            value = _draw(DIGITS, token.low, pick)
        elif name == 'letters':
            value = _draw(LETTERS, token.low, pick)
        else:  # alnum
            value = _draw(ALNUM, token.low, pick)
        if token.case == 'upper':
            value = value.upper()
        elif token.case == 'lower':
            value = value.lower()
        return value

    def _sequence_text(self, token, sequence):
        if sequence is None:
            raise NumberingRuleError('A running number is needed to fill in {seq}.')
        sequence = int(sequence)
        digits = len(str(sequence))
        if token.high and digits > token.high:
            widen = '{seq:%d-%d}' % (token.low, token.high + 1)
            if self.kind == CANDIDATE:
                raise NumberingRuleError(
                    f'Every number that {token.text} can hold is already used (the biggest is '
                    f"{'9' * token.high}). Ask the platform team to widen the pattern on this school's page "
                    f'in the console, for example to {widen}.')
            raise NumberingRuleError(
                f'The running number {sequence} has more digits than {token.text} can hold. Ask the '
                f"platform team to widen the pattern on this school's page in the console, for example "
                f'to {widen}.')
        return str(sequence).zfill(token.low)

    def render(self, facts, sequence=None, pick=None):
        """The finished number. Raises :class:`NumberingRuleError` (with a plain message) if what
        the pattern makes is not an acceptable code."""
        pick = pick or secrets.choice
        parts = [t.text if t.kind == 'text' else self._value(t, facts, sequence, pick) for t in self.tokens]
        code = ''.join(parts)
        if self.kind == CANDIDATE:
            code = code.upper()
        problem = explain_bad_code(code, self.kind)
        if problem:
            what = 'candidate code' if self.kind == CANDIDATE else 'student number'
            raise NumberingRuleError(
                f'This pattern made the {what} {code[:60]!r}, which is not allowed: {problem}.')
        return code

    # ---- the counter
    def leading_text(self, facts):
        """The fixed text every code of this pattern begins with, up to the first part that varies."""
        text = ''
        for token in self.tokens:
            if token.kind == 'field' and (token.name == 'seq' or token.spec.random):
                break
            text += token.text if token.kind == 'text' else self._value(token, facts, None, None)
        return text.upper()

    def matcher(self, facts):
        """A regular expression that matches the codes of this pattern which have the SAME text as
        a new code in every part except the running number (one group: that number). Built from
        the pattern's own pieces with ``re.escape``; the typed text is never used as an expression."""
        rx = []
        for token in self.tokens:
            if token.kind == 'text':
                rx.append(re.escape(token.text.upper()))
            elif token.name == 'seq':
                rx.append('(\\d{%d,})' % token.low if token.low else '(\\d+)')
            elif token.name == 'random':
                rx.append('\\d{%d}' % token.low)
            elif token.name == 'letters':
                rx.append('[%s]{%d}' % (LETTERS, token.low))
            elif token.name == 'alnum':
                rx.append('[%s]{%d}' % (ALNUM, token.low))
            else:
                rx.append(re.escape(self._value(token, facts, None, None).upper()))
        return re.compile(''.join(rx))

    def next_number(self, facts, first_number, codes):
        """1 + the biggest number in ``codes`` that has the same text in all the other parts, but
        never less than ``first_number``. So the count restarts by itself whenever a part that
        changes ({year}, {month}, {class}...) changes, and never if none of them is in the pattern."""
        expression = self.matcher(facts)
        biggest = int(first_number) - 1
        for code in codes:
            code = (code or '').upper()
            if len(code) > 2 * MAX_CODE_LENGTH:
                continue
            found = expression.fullmatch(code)
            if found:
                biggest = max(biggest, int(found.group(1)))
        return biggest + 1

    # ---- limits
    def longest(self, facts):
        """The most characters a code of this pattern can have."""
        total = 0
        for token in self.tokens:
            if token.kind == 'text':
                total += len(token.text)
            elif token.name == 'seq':
                total += token.high or len(str(MAX_FIRST_NUMBER))
            elif token.spec.random:
                total += token.low
            elif token.name == 'school':
                total += len(facts.school_code or '')
            elif token.name == 'initials':
                total += MAX_INITIALS
            elif token.name in ('year',):
                total += 4
            elif token.name in ('yy', 'month', 'day'):
                total += 2
            elif token.name == 'mon':
                total += 3
            elif token.name == 'class':
                total += NOMINAL_CLASS_LENGTH
            elif token.name == 'prefix':
                total += len(facts.prefix or '') or 20
        return total


def parse(text, kind):
    """Read a pattern and apply the rules about the pattern as a whole."""
    tokens = tokenize(text, kind)
    fields = [t for t in tokens if t.kind == 'field']
    seqs = [t for t in fields if t.name == 'seq']
    if len(seqs) > 1:
        raise PatternError(f'{{seq}} appears more than once (the second is at position {seqs[1].pos}); '
                           f'use it once.', seqs[1].pos, len(seqs[1].text))
    if not seqs and not any(t.spec.random for t in fields):
        raise PatternError('The pattern needs {seq} (a running number) or a random part such as {random:4}; '
                           'without one, every code would be the same.')
    if seqs:
        for before, after in zip(tokens, tokens[1:]):
            pair = {before.name, after.name} if before.kind == after.kind == 'field' else set()
            if 'seq' in pair and pair & {'random', 'alnum'}:
                other = before if before.name != 'seq' else after
                raise PatternError(f'Put a letter, - or . between {{seq}} and {other.text} (position '
                                   f'{other.pos}); side by side, the running number cannot be told from '
                                   f'the random digits.', other.pos, len(other.text))
    return Pattern(kind, text, tokens)


def _first_number_fits(pattern, first_number):
    seq = pattern.seq
    if seq and seq.high and len(str(first_number)) > seq.high:
        raise PatternError(f'The first number {first_number} has more digits than {seq.text} can hold '
                           f'(position {seq.pos}); use a smaller first number or a wider {{seq}}.',
                           seq.pos, len(seq.text))


def check_pattern(text, kind, facts=None, first_number=DEFAULT_FIRST_NUMBER):
    """Read a pattern and prove it makes acceptable codes, or raise :class:`PatternError`.

    Beyond reading it, a few example numbers are made (with the class known and unknown, since a
    pattern that only breaks when the class is missing would fail some day at the front desk),
    and the longest possible code is worked out against the limit.
    """
    pattern = parse(text, kind)
    real = facts is not None
    facts = facts or sample_facts()
    if not facts.prefix:
        # The policy's prefix is checked where it is set (it must be letters, digits, - and _); here
        # a school's own code stands in for it, which is what a new school's policy begins with.
        facts = replace(facts, prefix=facts.school_code or 'ABC')
    _first_number_fits(pattern, first_number)
    if real and pattern.longest(facts) > MAX_CODE_LENGTH:
        raise PatternError(f'With this school it could make codes up to {pattern.longest(facts)} characters '
                           f'long; the most allowed is {MAX_CODE_LENGTH}. Use a shorter pattern.')
    contexts = [facts]
    if pattern.uses_class:
        contexts.append(Facts(facts.school_code, facts.school_name, facts.now, facts.year, '', facts.prefix))
    tries = [max(first_number, 0)]
    for context in contexts:
        for sequence in tries if pattern.has_seq else [None]:
            try:
                pattern.render(context, sequence)
            except NumberingRuleError as exc:
                if context.target_class == '' and pattern.uses_class:
                    raise PatternError(
                        f'When the class is not known {{class}} is empty, and then the pattern makes a code '
                        f'that is not allowed. {exc}') from None
                raise PatternError(str(exc)) from None
    return pattern
