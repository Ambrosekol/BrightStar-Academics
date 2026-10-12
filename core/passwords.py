"""What a new password must be, in one place, for every account that chooses its own: staff, parents and students
(their own change, and the reset link), and platform operators.

Long enough (8 characters for a school account, 10 for a platform operator, as before), and not one of the passwords
people try first: a common password, one character over and over, a straight run of letters or numbers, or one built
from the person's own username. These are the passwords a guesser tries against every account; refusing them is what
makes the sign-in limits enough.
"""

import re

SCHOOL_MINIMUM = 8
OPERATOR_MINIMUM = 10

# The passwords tried first everywhere, and the ones a Nigerian school's people are likely to choose. Compared without
# case, and with digits and symbols at the end ignored ("Password1!" is "password").
COMMON = frozenset('''
password passw0rd p@ssword p@ssw0rd password1 password12 password123 passcode letmein welcome welcome1 admin
administrator admin123 root qwerty qwertyuiop qwerty123 asdfghjkl zxcvbnm iloveyou iloveu princess sunshine
football baseball dragon monkey master shadow superman batman trustno1 starwars freedom whatever secret
abc123 abcd1234 abcdef abcdefgh changeme default guest login user test testing hello hello123 goodluck
nigeria naija lagos abuja jesus jesus123 godisgood godislove blessed blessing praise mummy daddy family
school school123 student student123 teacher teacher123 parent parent123 bright brightstars brightstar academy
classroom exam exams result results principal headteacher
'''.split())

_SEQUENCES = ('0123456789012345678901234567890', 'abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz',
              'qwertyuiopasdfghjklzxcvbnm', '9876543210987654321098765432109', 'zyxwvutsrqponmlkjihgfedcba')


def _core(password):
    """The password without case, and without the digits and symbols people add at the end."""
    return re.sub(r'[\d\W_]+$', '', password.lower())


def password_problem(password, minimum=SCHOOL_MINIMUM, usernames=()):
    """Why this new password will not do, in a sentence for the person, or None when it is fine."""
    password = password or ''
    if len(password) < minimum:
        return f'Your new password must be at least {minimum} characters long.'
    lowered = password.lower()
    if lowered in COMMON or _core(password) in COMMON:
        return 'That password is one of the first that people try. Choose one that is harder to guess.'
    if len(set(lowered)) <= 2:
        return 'Your new password needs more than one or two different characters.'
    if any(lowered in run for run in _SEQUENCES):
        return 'A straight run of letters or numbers is easy to guess. Choose a different password.'
    for name in usernames:
        name = str(name or '').strip().lower()
        if len(name) >= 3 and (name in lowered or lowered in name):
            return 'Your new password must not contain your username or ID.'
    return None
