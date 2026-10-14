"""Builds the platform's built-in entrance practice banks (practice_banks/*.json).

    python tools/build_practice_banks.py

Six banks: Mathematics, English and General Knowledge, for entry into Year 7 / JSS 1 and into Year 10 / SSS 1. Every
school's public entrance practice adds them to its question pool (core/entrance.py, entrance_practice_paper), on top of
its own earlier entrance papers and its own practice bank, so a school can offer practice from its first day and a retake
rarely repeats. They are never an examination paper.

Mathematics is generated from patterns: each question's answer is worked out here, and the wrong options are the usual
slips around it, so every answer is right and there are hundreds of different questions. English and General Knowledge
come from the lists below, written for this purpose, keeping to facts that do not change and are not disputed. The
output is the same every time (a fixed seed); run it again after changing a list. Every bank is checked with the same
rules as a bank a school imports (core/banks.check_bank).
"""

import json
import math
import os
import random
import sys
from fractions import Fraction

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'practice_banks')
SUP = str.maketrans('0123456789-', '⁰¹²³⁴⁵⁶⁷⁸⁹⁻')


def sup(n):
    return str(n).translate(SUP)


def num(n):
    """A number as written on a paper: thousands separated, no trailing .0."""
    if isinstance(n, float) and n.is_integer():
        n = int(n)
    if isinstance(n, int):
        return f'{n:,}'
    return f'{n:,.2f}'.rstrip('0').rstrip('.')


class Bank:
    def __init__(self, seed):
        self.rng = random.Random(seed)
        self.items, self.seen = [], set()

    def add(self, text, answer, wrong, instruction=None):
        """One question: its text, the right option and wrong ones (the first three distinct ones are used)."""
        answer = str(answer)
        if text in self.seen:
            return False
        options = []
        for w in wrong:
            w = str(w)
            if w != answer and w not in options:
                options.append(w)
            if len(options) == 3:
                break
        if len(options) < 3:
            return False
        options.append(answer)
        self.rng.shuffle(options)
        self.seen.add(text)
        item = {'text': text, 'options': options, 'answer': options.index(answer), 'points': 1}
        if instruction:
            item['instruction'] = instruction
        self.items.append(item)
        return True

    def near(self, ans, extra=()):
        """Wrong numbers close to ``ans``: the slips a candidate makes, then neighbours."""
        out = [x for x in extra if x != ans and (not isinstance(x, (int, float)) or x > 0)]
        for d in (1, -1, 2, 10, -10, -2, 3, 5, -5, 100, 20):
            c = ans + d
            if c > 0 and c != ans:
                out.append(c)
        return out

    def many(self, count, make, tries=40):
        made = 0
        for _ in range(count * tries):
            if made >= count:
                break
            if make():
                made += 1


def numeric(bank, text, ans, extra=()):
    return bank.add(text, num(ans), [num(x) for x in bank.near(ans, extra)])


# =============================================================== Mathematics, Year 7 / JSS 1
def maths_year7():
    b = Bank(7101)
    r = b.rng
    b.many(28, lambda: (lambda x, y: numeric(b, f'Add: {num(x)} + {num(y)}', x + y, [x + y + 1000, x + y - 100]))(
        r.randint(1200, 8999), r.randint(1200, 8999)))
    b.many(28, lambda: (lambda x, y: numeric(b, f'Subtract: {num(max(x, y))} − {num(min(x, y))}', abs(x - y),
                                             [abs(x - y) + 100, abs(x - y) - 10]))(r.randint(2000, 9999), r.randint(1000, 8999)))
    b.many(28, lambda: (lambda x, y: numeric(b, f'Multiply: {x} × {y}', x * y, [x * y + x, x * y - y, x * (y + 1)]))(
        r.randint(12, 99), r.randint(11, 49)))
    b.many(24, lambda: (lambda q, d: numeric(b, f'Divide: {num(q * d)} ÷ {d}', q, [q + d, q * 2]))(r.randint(21, 199), r.randint(3, 9)))

    def fraction_of():
        d = r.choice([2, 3, 4, 5, 6, 8, 10]); n = r.randint(1, d - 1)
        if math.gcd(n, d) != 1:
            return False
        q = d * r.randint(3, 30)
        return numeric(b, f'What is {n}/{d} of {q}?', q * n // d, [q // d, q * d // n if n else q, q - q * n // d])
    b.many(26, fraction_of)

    def simplify():
        n, d = r.randint(1, 9), r.randint(2, 12)
        if n >= d or math.gcd(n, d) != 1:
            return False
        k = r.randint(2, 9)
        wrong = [f'{n * k // 2}/{d * k // 2}' if k % 2 == 0 else f'{n + 1}/{d}', f'{n}/{d + 1}', f'{d}/{n}', f'{n * 2}/{d}']
        return b.add(f'Write {n * k}/{d * k} in its lowest terms.', f'{n}/{d}', wrong)
    b.many(22, simplify)

    def add_fractions():
        d = r.choice([5, 6, 7, 8, 9, 10, 11, 12]); x, y = r.randint(1, d - 2), r.randint(1, d - 2)
        if x + y >= d:
            return False
        s = Fraction(x + y, d)
        return b.add(f'Add: {x}/{d} + {y}/{d}', f'{s.numerator}/{s.denominator}',
                     [f'{x + y}/{2 * d}', f'{x * y}/{d}', f'{x + y + 1}/{d}', f'{x + y - 1}/{d}'] if s.denominator == d else
                     [f'{x + y}/{2 * d}', f'{x + y}/{d + 1}', f'{x * y}/{d}', f'{x + y + 1}/{d}'])
    b.many(18, add_fractions)

    def percent_of():
        p = r.choice([5, 10, 15, 20, 25, 30, 40, 50, 60, 75]); n = r.choice(range(20, 2001, 20))
        if n * p % 100:
            return False
        return numeric(b, f'What is {p}% of {num(n)}?', n * p // 100, [n * p // 10, n - n * p // 100, p])
    b.many(26, percent_of)

    def lcm():
        x, y = r.randint(2, 15), r.randint(2, 15)
        if x == y:
            return False
        return numeric(b, f'Find the L.C.M. of {x} and {y}.', x * y // math.gcd(x, y), [x * y if math.gcd(x, y) > 1 else x + y, math.gcd(x, y) + 1])
    b.many(20, lcm)

    def hcf():
        g = r.randint(2, 12); x, y = g * r.randint(2, 9), g * r.randint(2, 9)
        if x == y or math.gcd(x, y) != g:
            return False
        return numeric(b, f'Find the H.C.F. of {x} and {y}.', g, [g * 2, x * y // g, g + 1])
    b.many(20, hcf)
    for n in range(4, 26):
        numeric(b, f'What is {n}²?', n * n, [2 * n, n * n + n, (n + 1) ** 2])
    for n in range(4, 21):
        numeric(b, f'What is the square root of {n * n}?', n, [n * 2, n * n // 2, n + 1])

    def rect():
        l, w = r.randint(4, 30), r.randint(2, 20)
        if l == w:
            return False
        if r.random() < .5:
            return numeric(b, f'A rectangle is {l} cm long and {w} cm wide. What is its perimeter?', 2 * (l + w), [l + w, l * w])
        return numeric(b, f'A rectangle is {l} m long and {w} m wide. What is its area?', l * w, [2 * (l + w), l + w])
    b.many(26, rect)

    def average():
        k = r.randint(3, 5); vals = [r.randint(5, 60) for _ in range(k)]
        if sum(vals) % k:
            return False
        return numeric(b, f'Find the average of {", ".join(map(str, vals[:-1]))} and {vals[-1]}.', sum(vals) // k, [sum(vals), max(vals)])
    b.many(20, average)

    def rounding():
        n = r.randint(1000, 99999); to = r.choice([10, 100, 1000])
        ans = int(math.floor(n / to + 0.5) * to)
        return numeric(b, f'Round {num(n)} to the nearest {num(to)}.', ans, [ans + to, ans - to if ans > to else ans + 2 * to, n // to * to if n // to * to != ans else ans + 3 * to])
    b.many(20, rounding)

    def money():
        k, p = r.randint(3, 15), r.choice(range(50, 2001, 50))
        one, many_ = r.choice([('exercise book', 'exercise books'), ('pen', 'pens'), ('loaf of bread', 'loaves of bread'),
                               ('tin of milk', 'tins of milk'), ('mango', 'mangoes'), ('orange', 'oranges')])
        return numeric(b, f'One {one} costs ₦{num(p)}. How much do {k} {many_} cost, in naira?', k * p, [k + p, k * p + p, (k - 1) * p])
    b.many(20, money)

    def change():
        p, paid = r.choice(range(150, 4001, 50)), r.choice([5000, 10000, 2000, 1000])
        if paid <= p:
            return False
        return numeric(b, f'A book costs ₦{num(p)}. How much change is given from ₦{num(paid)}?', paid - p, [paid + p, p])
    b.many(14, change)

    def angles():
        a, c = r.randint(25, 95), r.randint(25, 95)
        if a + c >= 170:
            return False
        return numeric(b, f'Two angles of a triangle are {a}° and {c}°. What is the third angle?', 180 - a - c, [360 - a - c, a + c, 90 - a if a < 90 else 100])
    b.many(20, angles)

    def decimals():
        x, y = round(r.uniform(1, 60), 2), round(r.uniform(1, 40), 2)
        return b.add(f'Add: {x} + {y}', num(round(x + y, 2)), [num(round(x + y + 1, 2)), num(round(x + y + 0.1, 2)), num(round(x + y - 0.01, 2))])
    b.many(18, decimals)

    def ratio():
        a, c, k = r.randint(1, 5), r.randint(2, 7), r.randint(5, 60)
        if a == c or math.gcd(a, c) != 1:
            return False
        total = (a + c) * k
        return numeric(b, f'Share ₦{num(total)} between Ada and Bayo in the ratio {a} : {c}. How much does Bayo get?', c * k, [a * k, total // 2, c * k + k])
    b.many(18, ratio)

    def place():
        n = r.randint(10000, 999999); digits = str(n); i = r.randint(0, len(digits) - 2)
        d = int(digits[i])
        if d == 0 or digits.count(digits[i]) > 1:
            return False
        value = d * 10 ** (len(digits) - 1 - i)
        return numeric(b, f'What is the value of the digit {d} in {num(n)}?', value, [d, value * 10, value // 10 if value >= 10 else d + 1])
    b.many(20, place)
    for h in range(2, 12):
        numeric(b, f'How many minutes are there in {h} hours?', 60 * h, [100 * h, 24 * h, 60 * h + 60])
    for d in range(2, 12):
        numeric(b, f'How many hours are there in {d} days?', 24 * d, [12 * d, 60 * d, 24 * d + 24])
    return b.items


# =============================================================== Mathematics, Year 10 / SSS 1
def maths_year10():
    b = Bank(10101)
    r = b.rng

    def like_terms():
        a, c, e = r.randint(2, 12), r.randint(2, 12), r.randint(1, 9)
        v = r.choice('xyabmn')
        ans = a + c - e
        if ans <= 1:
            return False
        return b.add(f'Simplify: {a}{v} + {c}{v} − {e}{v}', f'{ans}{v}', [f'{a + c + e}{v}', f'{ans}{v}²', f'{a * c - e}{v}', f'{ans + 1}{v}'])
    b.many(22, like_terms)

    def linear():
        x, a = r.randint(-9, 15), r.randint(2, 9); c = r.choice([n for n in range(-20, 21) if n])
        rhs = a * x + c
        sign = f'+ {c}' if c >= 0 else f'− {-c}'
        return b.add(f'Solve for x: {a}x {sign} = {rhs}', str(x), [str(x + 1), str(-x if x else 2), str(x - 2), str(rhs - c)])
    b.many(30, linear)

    def substitute():
        x, y = r.randint(-5, 9), r.randint(-5, 9); p, q = r.randint(2, 6), r.randint(2, 6)
        return b.add(f'If x = {x} and y = {y}, find the value of {p}x + {q}y.', str(p * x + q * y),
                     [str(p * x - q * y), str(p * y + q * x), str(p * x + q * y + 1), str(x + y)])
    b.many(22, substitute)

    def expand():
        a, c = r.randint(2, 9), r.randint(1, 12); v = r.choice('xyab')
        sign = r.choice(['+', '−'])
        return b.add(f'Expand: {a}({v} {sign} {c})', f'{a}{v} {sign} {a * c}',
                     [f'{a}{v} {sign} {c}', f'{v} {sign} {a * c}', f'{a}{v} {"−" if sign == "+" else "+"} {a * c}', f'{a + c}{v}'])
    b.many(20, expand)
    for base in (2, 3, 5, 10):
        for m, n in ((2, 3), (3, 4), (4, 5), (2, 6), (5, 3)):
            b.add(f'Simplify: {base}{sup(m)} × {base}{sup(n)}', f'{base}{sup(m + n)}',
                  [f'{base}{sup(m * n)}', f'{base * base}{sup(m + n)}', f'{base}{sup(abs(m - n))}', f'{base * 2}{sup(m + n)}'])
    for base, e in ((2, 5), (2, 6), (2, 7), (3, 3), (3, 4), (5, 3), (4, 3), (10, 4), (2, 8), (6, 2), (7, 2), (9, 2)):
        numeric(b, f'Evaluate {base}{sup(e)}.', base ** e, [base * e, base ** (e - 1), e ** base if e ** base != base ** e else base ** e + 1])

    def standard_form():
        m = r.randint(11, 99) / 10; k = r.randint(3, 7)
        if m == int(m):
            return False
        n = int(round(m * 10 ** k))
        return b.add(f'Write {num(n)} in standard form.', f'{m} × 10{sup(k)}',
                     [f'{m} × 10{sup(k + 1)}', f'{m} × 10{sup(k - 1)}', f'{int(m * 10)} × 10{sup(k - 1)}', f'{m} × 10{sup(-k)}'])
    b.many(20, standard_form)

    def interest():
        p, rate, t = r.choice(range(5000, 200001, 5000)), r.choice([2, 4, 5, 8, 10, 12, 15]), r.randint(2, 5)
        i = p * rate * t // 100
        if p * rate * t % 100:
            return False
        return numeric(b, f'Find the simple interest on ₦{num(p)} for {t} years at {rate}% per annum.', i, [p * rate // 100, i + p, i * 2])
    b.many(20, interest)

    def profit():
        cost = r.choice(range(1000, 50001, 1000)); pct = r.choice([5, 10, 20, 25, 40, 50])
        sold = cost * (100 + pct) // 100
        if cost * (100 + pct) % 100:
            return False
        return b.add(f'A trader buys an item for ₦{num(cost)} and sells it for ₦{num(sold)}. What is the profit per cent?',
                     f'{pct}%', [f'{pct * 2}%', f'{100 - pct}%', f'{round((sold - cost) / sold * 100, 1)}%', f'{pct + 5}%'])
    b.many(18, profit)
    for n in range(5, 13):
        numeric(b, f'What is the sum of the interior angles of a polygon with {n} sides?', (n - 2) * 180, [n * 180, (n - 1) * 180, 360])
    for n in (5, 6, 8, 9, 10, 12, 15, 18, 20):
        b.add(f'What is the size of each interior angle of a regular polygon with {n} sides?', f'{180 - 360 // n}°',
              [f'{360 // n}°', f'{180 - 360 // n + 10}°', f'{(n - 2) * 180}°', f'{180 - 360 // n - 12}°'])
    for (x, y, z) in ((3, 4, 5), (5, 12, 13), (8, 15, 17), (7, 24, 25)):
        for k in (1, 2, 3, 4):
            numeric(b, f'The two shorter sides of a right-angled triangle are {x * k} cm and {y * k} cm. How long is the hypotenuse?',
                    z * k, [x * k + y * k, z * k + 1, (x + y) * k // 2])
    for rad in (7, 14, 21, 28, 35, 3.5):
        area = Fraction(22, 7) * Fraction(rad) * Fraction(rad)
        circ = 2 * Fraction(22, 7) * Fraction(rad)
        b.add(f'Find the area of a circle of radius {num(rad)} cm. (Take π = 22/7)', f'{num(float(area))} cm²',
              [f'{num(float(circ))} cm²', f'{num(float(area * 2))} cm²', f'{num(float(area / 2))} cm²'])
        b.add(f'Find the circumference of a circle of radius {num(rad)} cm. (Take π = 22/7)', f'{num(float(circ))} cm',
              [f'{num(float(area))} cm', f'{num(float(circ / 2))} cm', f'{num(float(circ * 2))} cm'])

    def mean():
        k = r.randint(5, 7); vals = [r.randint(2, 30) for _ in range(k)]
        if sum(vals) % k:
            return False
        return numeric(b, f'Find the mean of: {", ".join(map(str, vals))}', sum(vals) // k, [sum(vals), sorted(vals)[k // 2] + (1 if sorted(vals)[k // 2] == sum(vals) // k else 0), max(vals)])
    b.many(16, mean)

    def median():
        vals = r.sample(range(2, 60), 7); s = sorted(vals)
        return numeric(b, f'Find the median of: {", ".join(map(str, vals))}', s[3], [vals[3] if vals[3] != s[3] else s[2], s[4], sum(vals) // 7])
    b.many(16, median)
    for n in r.sample(range(5, 64), 22):
        b.add(f'Convert {n} (base ten) to base two.', f'{n:b}₂', [f'{n + 1:b}₂', f'{n - 1:b}₂', f'{n * 2:b}₂', f'{n:b}'[::-1] + '₂'])
    for n in r.sample(range(5, 64), 22):
        numeric(b, f'Convert {n:b}₂ to base ten.', n, [int(f'{n:b}'[::-1], 2), n * 2])

    def sets():
        a, c = r.randint(10, 40), r.randint(10, 40); both = r.randint(2, min(a, c) - 1)
        return numeric(b, f'n(A) = {a}, n(B) = {c} and n(A ∩ B) = {both}. Find n(A ∪ B).', a + c - both, [a + c, a + c - 2 * both, a + both])
    b.many(20, sets)

    def probability():
        red, blue = r.randint(2, 9), r.randint(2, 9); green = r.randint(0, 5)
        total = red + blue + green
        p = Fraction(red, total)
        wrong = [(red, blue), (blue, total), (1, red), (red + 1, total), (red, total + 1)]
        wrong = [f'{x}/{y}' for x, y in wrong if Fraction(x, y) != p]      # never the right value written another way
        return b.add(f'A bag has {red} red, {blue} blue{f" and {green} green" if green else ""} balls. A ball is picked at random. What is the probability that it is red?',
                     f'{p.numerator}/{p.denominator}', wrong)
    b.many(20, probability)

    def factorise():
        p, q = r.randint(1, 9), r.randint(1, 9)
        if p == q:
            return False
        return b.add(f'Factorise: x² + {p + q}x + {p * q}', f'(x + {p})(x + {q})',
                     [f'(x + {p + q})(x + {p * q})', f'(x − {p})(x − {q})', f'(x + {p})(x − {q})', f'(x + {p * q})(x + 1)'])
    b.many(20, factorise)

    def increase():
        n = r.choice(range(200, 20001, 200)); pct = r.choice([5, 10, 15, 20, 25, 50])
        if n * pct % 100:
            return False
        return numeric(b, f'Increase {num(n)} by {pct}%.', n + n * pct // 100, [n * pct // 100, n - n * pct // 100, n + pct])
    b.many(16, increase)

    def workers():
        men, days = r.choice([4, 5, 6, 8, 10, 12]), r.choice([6, 8, 9, 10, 12, 15]); more = r.choice([2, 3, 4, 5, 6])
        total = men * days
        if total % (men + more):
            return False
        return numeric(b, f'{men} men can finish a job in {days} days. How many days will {men + more} men take, working at the same rate?',
                       total // (men + more), [days + more, days * (men + more) // men if days * (men + more) % men == 0 else days + 1])
    b.many(12, workers)

    def sig_figs():
        n = r.randint(10000, 999999)
        digits = len(str(n))
        to = lambda keep: int(math.floor(n / 10 ** (digits - keep) + 0.5)) * 10 ** (digits - keep)   # halves round up
        ans = to(2)
        if ans == n:
            return False
        return numeric(b, f'Write {num(n)} correct to 2 significant figures.', ans, [to(3), int(str(n)[:2]), to(1)])
    b.many(16, sig_figs)

    def simultaneous():
        x, y = r.randint(1, 15), r.randint(1, 15)
        if x == y:
            return False
        return b.add(f'Solve: x + y = {x + y} and x − y = {x - y}. Find x.', str(x), [str(y), str(x + y), str(x - y if x - y != x else x + 2), str(x + 1)])
    b.many(16, simultaneous)
    return b.items


# =============================================================== English
SYN_Y7 = [('big', 'large'), ('tiny', 'small'), ('glad', 'happy'), ('quick', 'fast'), ('begin', 'start'), ('finish', 'end'),
          ('brave', 'courageous'), ('shut', 'close'), ('wealthy', 'rich'), ('gift', 'present'), ('select', 'choose'),
          ('assist', 'help'), ('eager', 'keen'), ('mistake', 'error'), ('silent', 'quiet'), ('clever', 'intelligent'),
          ('purchase', 'buy'), ('journey', 'trip'), ('reply', 'answer'), ('centre', 'middle'), ('damp', 'moist'),
          ('angry', 'annoyed'), ('difficult', 'hard'), ('ill', 'sick'), ('aid', 'support'), ('stop', 'halt'), ('famous', 'well-known'),
          ('fix', 'repair'), ('route', 'way'), ('collect', 'gather'), ('loud', 'noisy'), ('neat', 'tidy'), ('sad', 'unhappy'),
          ('scared', 'afraid'), ('timid', 'shy'), ('false', 'untrue'), ('enormous', 'huge'), ('gentle', 'mild'), ('alter', 'change'),
          ('ancient', 'old'), ('beautiful', 'lovely'), ('ponder', 'think'), ('locate', 'find'), ('vanish', 'disappear')]
ANT_Y7 = [('hot', 'cold'), ('tall', 'short'), ('rich', 'poor'), ('early', 'late'), ('open', 'closed'), ('heavy', 'light'),
          ('strong', 'weak'), ('happy', 'sad'), ('clean', 'dirty'), ('young', 'old'), ('push', 'pull'), ('buy', 'sell'),
          ('arrive', 'depart'), ('inside', 'outside'), ('friend', 'enemy'), ('true', 'false'), ('give', 'take'), ('always', 'never'),
          ('increase', 'decrease'), ('accept', 'refuse'), ('wide', 'narrow'), ('deep', 'shallow'), ('loud', 'quiet'), ('day', 'night'),
          ('win', 'lose'), ('remember', 'forget'), ('enter', 'leave'), ('above', 'below'), ('borrow', 'lend'), ('sharp', 'blunt'),
          ('cheap', 'expensive'), ('fresh', 'stale'), ('polite', 'rude'), ('success', 'failure'), ('smooth', 'rough'), ('love', 'hate'),
          ('question', 'answer'), ('asleep', 'awake'), ('first', 'last'), ('empty', 'full')]
SYN_Y10 = [('abundant', 'plentiful'), ('candid', 'frank'), ('diligent', 'hardworking'), ('frugal', 'thrifty'), ('hostile', 'unfriendly'),
           ('lethargic', 'sluggish'), ('obstinate', 'stubborn'), ('ponder', 'consider'), ('scarce', 'rare'), ('tranquil', 'calm'),
           ('vacant', 'empty'), ('verbose', 'wordy'), ('wary', 'cautious'), ('eminent', 'distinguished'), ('benevolent', 'kind'),
           ('ambiguous', 'unclear'), ('affluent', 'wealthy'), ('commence', 'begin'), ('conceal', 'hide'), ('deceive', 'mislead'),
           ('diminish', 'reduce'), ('eloquent', 'articulate'), ('feeble', 'weak'), ('hazardous', 'dangerous'), ('imminent', 'impending'),
           ('jovial', 'cheerful'), ('meticulous', 'careful'), ('novice', 'beginner'), ('obsolete', 'outdated'), ('prudent', 'wise'),
           ('reluctant', 'unwilling'), ('serene', 'peaceful'), ('tedious', 'boring'), ('vigilant', 'watchful'), ('zealous', 'enthusiastic'),
           ('authentic', 'genuine'), ('lucid', 'clear'), ('mediocre', 'average'), ('ostentatious', 'showy'), ('placid', 'gentle'),
           ('resilient', 'tough'), ('trivial', 'unimportant')]
ANT_Y10 = [('generous', 'mean'), ('optimistic', 'pessimistic'), ('ancient', 'modern'), ('artificial', 'natural'),
           ('temporary', 'permanent'), ('victory', 'defeat'), ('guilty', 'innocent'), ('praise', 'criticise'), ('expand', 'contract'),
           ('maximum', 'minimum'), ('ascend', 'descend'), ('voluntary', 'compulsory'), ('import', 'export'), ('rural', 'urban'),
           ('transparent', 'opaque'), ('superior', 'inferior'), ('humble', 'arrogant'), ('reveal', 'conceal'), ('frequent', 'rare'),
           ('vertical', 'horizontal'), ('include', 'exclude'), ('majority', 'minority'), ('profit', 'loss'), ('attract', 'repel'),
           ('external', 'internal'), ('genuine', 'fake'), ('approve', 'reject'), ('cautious', 'reckless'), ('scarcity', 'abundance'),
           ('allow', 'forbid'), ('advance', 'retreat'), ('barren', 'fertile'), ('acquit', 'convict'), ('lenient', 'strict'),
           ('fragile', 'sturdy'), ('ignorance', 'knowledge'), ('literate', 'illiterate'), ('optional', 'mandatory'),
           ('affirm', 'deny'), ('loyal', 'treacherous')]
VERBS = [('go', 'went', 'gone'), ('see', 'saw', 'seen'), ('eat', 'ate', 'eaten'), ('write', 'wrote', 'written'), ('take', 'took', 'taken'),
         ('give', 'gave', 'given'), ('run', 'ran', 'run'), ('swim', 'swam', 'swum'), ('sing', 'sang', 'sung'), ('drink', 'drank', 'drunk'),
         ('begin', 'began', 'begun'), ('break', 'broke', 'broken'), ('bring', 'brought', 'brought'), ('buy', 'bought', 'bought'),
         ('catch', 'caught', 'caught'), ('teach', 'taught', 'taught'), ('think', 'thought', 'thought'), ('fly', 'flew', 'flown'),
         ('grow', 'grew', 'grown'), ('know', 'knew', 'known'), ('throw', 'threw', 'thrown'), ('draw', 'drew', 'drawn'),
         ('drive', 'drove', 'driven'), ('ride', 'rode', 'ridden'), ('speak', 'spoke', 'spoken'), ('steal', 'stole', 'stolen'),
         ('choose', 'chose', 'chosen'), ('freeze', 'froze', 'frozen'), ('forget', 'forgot', 'forgotten'), ('sit', 'sat', 'sat'),
         ('stand', 'stood', 'stood'), ('sleep', 'slept', 'slept'), ('keep', 'kept', 'kept'), ('feel', 'felt', 'felt'),
         ('leave', 'left', 'left'), ('lose', 'lost', 'lost'), ('meet', 'met', 'met'), ('send', 'sent', 'sent'), ('spend', 'spent', 'spent'),
         ('build', 'built', 'built'), ('hold', 'held', 'held'), ('tell', 'told', 'told'), ('sell', 'sold', 'sold'), ('find', 'found', 'found'),
         ('fight', 'fought', 'fought'), ('wear', 'wore', 'worn'), ('tear', 'tore', 'torn'), ('hide', 'hid', 'hidden'), ('shake', 'shook', 'shaken'),
         ('rise', 'rose', 'risen'), ('fall', 'fell', 'fallen'), ('blow', 'blew', 'blown'), ('bite', 'bit', 'bitten'), ('weep', 'wept', 'wept')]
ING = {'run': 'running', 'swim': 'swimming', 'sit': 'sitting', 'begin': 'beginning', 'forget': 'forgetting', 'write': 'writing',
       'take': 'taking', 'give': 'giving', 'drive': 'driving', 'ride': 'riding', 'choose': 'choosing', 'freeze': 'freezing',
       'leave': 'leaving', 'lose': 'losing', 'hide': 'hiding', 'shake': 'shaking', 'rise': 'rising', 'bite': 'biting'}
PLURAL_Y7 = [('child', 'children'), ('man', 'men'), ('woman', 'women'), ('tooth', 'teeth'), ('foot', 'feet'), ('mouse', 'mice'),
             ('goose', 'geese'), ('ox', 'oxen'), ('sheep', 'sheep'), ('box', 'boxes'), ('baby', 'babies'), ('knife', 'knives'),
             ('leaf', 'leaves'), ('wife', 'wives'), ('potato', 'potatoes'), ('tomato', 'tomatoes'), ('city', 'cities'),
             ('church', 'churches'), ('bus', 'buses'), ('lady', 'ladies'), ('half', 'halves'), ('wolf', 'wolves'), ('deer', 'deer'),
             ('loaf', 'loaves'), ('glass', 'glasses'), ('watch', 'watches'), ('story', 'stories'), ('hero', 'heroes')]
PLURAL_Y10 = [('crisis', 'crises'), ('analysis', 'analyses'), ('criterion', 'criteria'), ('phenomenon', 'phenomena'),
              ('stimulus', 'stimuli'), ('thesis', 'theses'), ('oasis', 'oases'), ('axis', 'axes'), ('basis', 'bases'),
              ('mother-in-law', 'mothers-in-law'), ('passer-by', 'passers-by'), ('commander-in-chief', 'commanders-in-chief'),
              ('nucleus', 'nuclei'), ('bacterium', 'bacteria'), ('diagnosis', 'diagnoses'),
              ('hypothesis', 'hypotheses'), ('parenthesis', 'parentheses'), ('alumnus', 'alumni'), ('son-in-law', 'sons-in-law')]
GENDER = [('king', 'queen'), ('prince', 'princess'), ('father', 'mother'), ('uncle', 'aunt'), ('nephew', 'niece'), ('husband', 'wife'),
          ('bull', 'cow'), ('cock', 'hen'), ('actor', 'actress'), ('waiter', 'waitress'), ('lion', 'lioness'), ('host', 'hostess'),
          ('son', 'daughter'), ('brother', 'sister'), ('gentleman', 'lady'), ('monk', 'nun'), ('ram', 'ewe'), ('stallion', 'mare'),
          ('bridegroom', 'bride'), ('landlord', 'landlady'), ('headmaster', 'headmistress'), ('emperor', 'empress'), ('wizard', 'witch'),
          ('drake', 'duck'), ('steward', 'stewardess'), ('god', 'goddess')]
COLLECTIVE = [('sheep', 'flock'), ('cattle', 'herd'), ('bees', 'swarm'), ('lions', 'pride'), ('fish', 'school'), ('ships', 'fleet'),
              ('wolves', 'pack'), ('thieves', 'gang'), ('ants', 'colony'), ('flowers', 'bouquet'), ('puppies', 'litter'),
              ('monkeys', 'troop'), ('musicians', 'band'), ('singers', 'choir'), ('players', 'team'), ('soldiers', 'regiment'),
              ('grapes', 'bunch'), ('stairs', 'flight'), ('mountains', 'range'), ('cards', 'pack')]
ALSO_FITS = {'flowers': {'bunch'}, 'soldiers': {'troop', 'band', 'team', 'gang'}, 'singers': {'band', 'team'},
             'musicians': {'troop', 'choir'}, 'bees': {'colony'}, 'ants': {'swarm'}, 'monkeys': {'band', 'gang', 'colony'},
             'wolves': {'gang'}, 'thieves': {'band', 'pack'}, 'cattle': {'flock'}, 'sheep': {'herd'}, 'fish': {'swarm'}}
COMPARE = [('good', 'better', 'best'), ('bad', 'worse', 'worst'), ('tall', 'taller', 'tallest'), ('big', 'bigger', 'biggest'),
           ('happy', 'happier', 'happiest'), ('beautiful', 'more beautiful', 'most beautiful'), ('careful', 'more careful', 'most careful'),
           ('many', 'more', 'most'), ('easy', 'easier', 'easiest'), ('thin', 'thinner', 'thinnest'),
           ('hot', 'hotter', 'hottest'), ('large', 'larger', 'largest'), ('dry', 'drier', 'driest'), ('early', 'earlier', 'earliest'),
           ('busy', 'busier', 'busiest'), ('famous', 'more famous', 'most famous'), ('important', 'more important', 'most important'),
           ('fat', 'fatter', 'fattest')]
PREPOSITIONS = [('He is very good ___ Mathematics.', 'at', ['on', 'for', 'to']), ('She was born ___ 2013.', 'in', ['on', 'at', 'by']),
                ('We shall meet ___ Monday.', 'on', ['in', 'at', 'to']), ('The class starts ___ eight o\'clock.', 'at', ['in', 'on', 'for']),
                ('My sister is afraid ___ dogs.', 'of', ['from', 'with', 'at']), ('He depends ___ his parents for everything.', 'on', ['of', 'from', 'at']),
                ('Share the sweets ___ the two children.', 'between', ['among', 'with', 'at']),
                ('She is interested ___ music.', 'in', ['on', 'at', 'for']), ('He apologised ___ coming late.', 'for', ['of', 'on', 'at']),
                ('We congratulated her ___ her success.', 'on', ['for', 'at', 'with']), ('Bola is fond ___ singing.', 'of', ['in', 'at', 'with']),
                ('Listen ___ your teacher.', 'to', ['at', 'on', 'for']), ('They have lived here ___ 2015.', 'since', ['for', 'from', 'at']),
                ('We have waited here ___ two hours.', 'for', ['since', 'from', 'at']), ('He was angry ___ his brother.', 'with', ['on', 'for', 'to']),
                ('She prefers tea ___ coffee.', 'to', ['than', 'from', 'for']), ('He divided the cake ___ four pieces.', 'into', ['with', 'on', 'by']),
                ('The pupils were proud ___ their school.', 'of', ['with', 'on', 'for']), ('She is married ___ a doctor.', 'to', ['with', 'by', 'for']),
                ('The man died ___ malaria.', 'of', ['by', 'with', 'on']), ('He insisted ___ paying for the meal.', 'on', ['for', 'to', 'at']),
                ('This bag is different ___ that one.', 'from', ['with', 'of', 'against'])]
SPELL = [('necessary', ['neccessary', 'necesary', 'neccesary']), ('receive', ['recieve', 'receeve', 'riceive']),
         ('separate', ['seperate', 'separete', 'seprate']), ('accommodation', ['accomodation', 'acommodation', 'accommadation']),
         ('embarrass', ['embarass', 'embarras', 'embbarrass']), ('occurrence', ['occurence', 'ocurrence', 'occurrance']),
         ('definitely', ['definately', 'definitly', 'defenitely']), ('government', ['goverment', 'governmant', 'govermant']),
         ('environment', ['enviroment', 'environmant', 'envirnoment']), ('February', ['Febuary', 'Febrary', 'Feburary']),
         ('Wednesday', ['Wensday', 'Wednsday', 'Wedensday']), ('library', ['libary', 'liberary', 'librery']),
         ('beautiful', ['beautifull', 'beutiful', 'beautyful']), ('business', ['buisness', 'busness', 'bussiness']),
         ('believe', ['belive', 'beleive', 'beleave']), ('calendar', ['calandar', 'callendar', 'calendre']),
         ('committee', ['commitee', 'comittee', 'committe']), ('conscience', ['concience', 'conscence', 'consciense']),
         ('guarantee', ['garantee', 'guarentee', 'guarrantee']), ('independent', ['independant', 'indipendent', 'independint']),
         ('knowledge', ['knowlege', 'knowladge', 'nowledge']), ('maintenance', ['maintainance', 'maintenence', 'maintanance']),
         ('millennium', ['millenium', 'milennium', 'millenniun']), ('occasion', ['occassion', 'ocasion', 'ocassion']),
         ('parliament', ['parliment', 'parlament', 'parliamant']), ('privilege', ['priviledge', 'privelege', 'privilage']),
         ('pronunciation', ['pronounciation', 'pronunciaton', 'prononciation']), ('recommend', ['reccommend', 'recomend', 'reccomend']),
         ('rhythm', ['rythm', 'rhythem', 'rythem']), ('schedule', ['schedual', 'shedule', 'scedule']), ('tomorrow', ['tommorow', 'tomorow', 'tommorrow']),
         ('vacuum', ['vaccum', 'vacum', 'vaccuum']), ('address', ['adress', 'addres', 'adres']), ('across', ['accross', 'acros', 'acrross']),
         ('until', ['untill', 'unttil', 'untl']), ('friend', ['freind', 'frend', 'friendd']), ('grammar', ['grammer', 'gramar', 'grammmar'])]
CONCORD_Y7 = [('Each of the boys ___ a book.', 'has', ['have', 'are having', 'were having']),
              ('My father and mother ___ at home.', 'are', ['is', 'was', 'has']),
              ('The girls ___ playing in the field.', 'are', ['is', 'was', 'has']),
              ('Everybody ___ present today.', 'is', ['are', 'were', 'have']),
              ('Neither Ade nor Tunde ___ here.', 'was', ['were', 'are', 'have been']),
              ('One of the pupils ___ absent.', 'is', ['are', 'were', 'have been']),
              ('The news ___ good.', 'is', ['are', 'were', 'have']),
              ('Mathematics ___ my favourite subject.', 'is', ['are', 'were', 'have']),
              ('The children ___ their lunch.', 'have eaten', ['has eaten', 'has ate', 'have ate']),
              ('She ___ to school every day.', 'walks', ['walk', 'walking', 'have walked']),
              ('They ___ football yesterday.', 'played', ['play', 'plays', 'playing']),
              ('The teacher, with his pupils, ___ coming.', 'is', ['are', 'were', 'have']),
              ('Ten kilometres ___ a long way to walk.', 'is', ['are', 'were', 'have']),
              ('Either the boys or their father ___ coming.', 'is', ['are', 'were', 'have']),
              ('The jury ___ given its verdict.', 'has', ['have', 'are', 'were'])]
CONCORD_Y10 = [('Neither the teacher nor the students ___ aware of the change.', 'were', ['was', 'is', 'has been']),
               ('The committee ___ reached its decision.', 'has', ['have', 'are', 'were']),
               ('A number of students ___ absent today.', 'are', ['is', 'was', 'has been']),
               ('The number of students ___ increasing.', 'is', ['are', 'were', 'have been']),
               ('Bread and butter ___ my favourite breakfast.', 'is', ['are', 'were', 'have been']),
               ('Every boy and girl ___ a uniform.', 'has', ['have', 'are having', 'were having']),
               ('Physics ___ a difficult subject.', 'is', ['are', 'were', 'have been']),
               ('The scissors ___ on the table.', 'are', ['is', 'was', 'has been']),
               ('Not only the principal but also the teachers ___ present.', 'were', ['was', 'is', 'has been']),
               ('Many a man ___ tried and failed.', 'has', ['have', 'are', 'were']),
               ('The cattle ___ grazing in the field.', 'are', ['is', 'was', 'has been']),
               ('Five hundred naira ___ too much for that pen.', 'is', ['are', 'were', 'have been']),
               ('He is one of those men who ___ always late.', 'are', ['is', 'was', 'has been']),
               ('The furniture in the house ___ new.', 'is', ['are', 'were', 'have been']),
               ('There ___ many reasons for his failure.', 'are', ['is', 'was', 'has been']),
               ('Either of the two answers ___ correct.', 'is', ['are', 'were', 'have been']),
               ('Politics ___ not my interest.', 'is', ['are', 'were', 'have been']),
               ('The police ___ arrested the thief.', 'have', ['has', 'is', 'was']),
               ('None of the food ___ eaten.', 'was', ['were', 'are', 'have been']),
               ('The poor ___ helped by the government.', 'are', ['is', 'was', 'has been'])]
IDIOMS = [('let the cat out of the bag', 'to reveal a secret'), ('bury the hatchet', 'to make peace'),
          ('a storm in a teacup', 'a lot of fuss about something small'), ('burn the midnight oil', 'to work or study late into the night'),
          ('beat about the bush', 'to avoid saying something directly'), ('a wet blanket', 'a person who spoils other people\'s fun'),
          ('once in a blue moon', 'very rarely'), ('hit the nail on the head', 'to describe something exactly right'),
          ('take the bull by the horns', 'to face a difficulty boldly'), ('a piece of cake', 'something very easy'),
          ('cry wolf', 'to raise a false alarm'), ('turn over a new leaf', 'to change one\'s behaviour for the better'),
          ('be in hot water', 'to be in trouble'), ('see eye to eye', 'to agree with someone'), ('pull someone\'s leg', 'to tease someone'),
          ('a white elephant', 'a costly possession that is of little use'), ('face the music', 'to accept the consequences of one\'s actions'),
          ('the apple of one\'s eye', 'a person who is greatly loved'), ('add fuel to the fire', 'to make a bad situation worse'),
          ('bite off more than one can chew', 'to take on more than one can manage'), ('call a spade a spade', 'to speak plainly'),
          ('Achilles\' heel', 'a weak point'), ('at the eleventh hour', 'at the last moment'), ('blow one\'s own trumpet', 'to boast'),
          ('break the ice', 'to start a conversation in an awkward situation'), ('under the weather', 'slightly unwell'),
          ('sit on the fence', 'to avoid taking sides'), ('a blessing in disguise', 'something good that seemed bad at first'),
          ('by the skin of one\'s teeth', 'only just; narrowly'), ('kill two birds with one stone', 'to achieve two things with one action'),
          ('get cold feet', 'to become nervous about something planned'), ('a fair-weather friend', 'a friend only in good times')]
FIGURES = [('The boy is as brave as a lion.', 'Simile'), ('Life is a journey.', 'Metaphor'), ('The wind whispered through the trees.', 'Personification'),
           ('I have told you a million times.', 'Hyperbole'), ('Peter Piper picked a peck of pickled peppers.', 'Alliteration'),
           ('The snake hissed at us.', 'Onomatopoeia'), ('It was an open secret.', 'Oxymoron'), ('Her grandfather passed away last year.', 'Euphemism'),
           ('She sings like a nightingale.', 'Simile'), ('The classroom was a zoo.', 'Metaphor'), ('The sun smiled down on us.', 'Personification'),
           ('This bag weighs a ton.', 'Hyperbole'), ('The clock went tick-tock.', 'Onomatopoeia'), ('A deafening silence filled the room.', 'Oxymoron'),
           ('His heart is made of stone.', 'Metaphor'), ('The leaves danced in the wind.', 'Personification'), ('He ran like the wind.', 'Simile'),
           ('Sally sells sea shells.', 'Alliteration'), ('The door creaked open.', 'Onomatopoeia'), ('I am so hungry I could eat a horse.', 'Hyperbole')]
FIGURE_NAMES = ['Simile', 'Metaphor', 'Personification', 'Hyperbole', 'Alliteration', 'Onomatopoeia', 'Oxymoron', 'Euphemism']
TAGS = [('She is your sister, ___?', 'isn\'t she', ['is she', 'doesn\'t she', 'wasn\'t she']),
        ('You can swim, ___?', 'can\'t you', ['can you', 'don\'t you', 'couldn\'t you']),
        ('They didn\'t come, ___?', 'did they', ['didn\'t they', 'do they', 'were they']),
        ('Let\'s go home, ___?', 'shall we', ['will we', 'don\'t we', 'let we']),
        ('I am right, ___?', 'aren\'t I', ['amn\'t I', 'isn\'t it', 'am I']),
        ('He has finished his work, ___?', 'hasn\'t he', ['has he', 'didn\'t he', 'isn\'t he']),
        ('Nobody called me, ___?', 'did they', ['didn\'t they', 'did he', 'didn\'t he']),
        ('Open the door, ___?', 'will you', ['do you', 'shall you', 'won\'t it']),
        ('There is a problem, ___?', 'isn\'t there', ['is there', 'isn\'t it', 'wasn\'t there']),
        ('She rarely sings, ___?', 'does she', ['doesn\'t she', 'is she', 'did she']),
        ('You will come, ___?', 'won\'t you', ['will you', 'don\'t you', 'shan\'t you']),
        ('We must leave now, ___?', 'mustn\'t we', ['must we', 'don\'t we', 'shouldn\'t we']),
        ('He used to live here, ___?', 'didn\'t he', ['usedn\'t he', 'did he', 'doesn\'t he']),
        ('Everyone was happy, ___?', 'weren\'t they', ['wasn\'t he', 'were they', 'wasn\'t it']),
        ('It rained yesterday, ___?', 'didn\'t it', ['did it', 'wasn\'t it', 'doesn\'t it']),
        ('They have never been here, ___?', 'have they', ['haven\'t they', 'did they', 'are they'])]


# Words close in meaning. A wrong option must share no group with the question's word or its answer, so it can never
# also be right.
RELATED = [{'big', 'large', 'enormous', 'huge', 'tall', 'heavy'}, {'tiny', 'small', 'little', 'short'},
           {'glad', 'happy', 'cheerful', 'jovial'}, {'quick', 'fast', 'early'}, {'begin', 'start', 'commence', 'enter', 'arrive', 'first'},
           {'finish', 'end', 'stop', 'halt', 'last', 'leave', 'depart'}, {'brave', 'courageous', 'bold', 'strong', 'sturdy', 'tough', 'resilient'},
           {'shut', 'close', 'closed', 'conceal', 'hide'}, {'wealthy', 'rich', 'affluent', 'expensive'}, {'gift', 'present', 'give', 'lend', 'sell'},
           {'select', 'choose', 'accept', 'approve', 'include'}, {'assist', 'help', 'aid', 'support'},
           {'eager', 'keen', 'zealous', 'enthusiastic'}, {'mistake', 'error', 'failure', 'false', 'untrue', 'fake', 'artificial'},
           {'silent', 'quiet', 'tranquil', 'calm', 'serene', 'peaceful', 'placid', 'gentle', 'mild', 'smooth'},
           {'clever', 'intelligent', 'sharp', 'wise', 'prudent', 'knowledge', 'literate'}, {'purchase', 'buy', 'take', 'borrow', 'import'},
           {'journey', 'trip', 'route', 'way'}, {'reply', 'answer'}, {'centre', 'middle', 'inside', 'internal'}, {'damp', 'moist'},
           {'angry', 'annoyed', 'hostile', 'unfriendly', 'rude', 'hate', 'enemy', 'treacherous'}, {'difficult', 'hard', 'rough', 'tedious', 'boring'},
           {'ill', 'sick', 'unwell', 'weak', 'feeble', 'fragile'}, {'famous', 'well-known', 'eminent', 'distinguished', 'superior'},
           {'fix', 'repair'}, {'collect', 'gather'}, {'loud', 'noisy'}, {'neat', 'tidy', 'clean', 'fresh'}, {'sad', 'unhappy'},
           {'scared', 'afraid', 'timid', 'shy', 'cautious', 'wary', 'careful', 'meticulous', 'diligent', 'hardworking', 'vigilant', 'watchful'},
           {'alter', 'change'}, {'ancient', 'old', 'obsolete', 'outdated'}, {'young', 'modern', 'new'}, {'beautiful', 'lovely'},
           {'ponder', 'think', 'consider'}, {'locate', 'find', 'reveal'}, {'vanish', 'disappear', 'outside', 'external'},
           {'abundant', 'plentiful', 'abundance', 'full', 'maximum', 'majority', 'frequent'},
           {'scarce', 'rare', 'scarcity', 'empty', 'vacant', 'minimum', 'minority', 'barren'}, {'candid', 'frank', 'genuine', 'authentic', 'true', 'natural'},
           {'frugal', 'thrifty', 'mean'}, {'benevolent', 'kind', 'generous', 'humble', 'polite', 'loyal', 'friend'},
           {'lethargic', 'sluggish', 'asleep'}, {'obstinate', 'stubborn'}, {'verbose', 'wordy'}, {'ambiguous', 'unclear', 'opaque'},
           {'deceive', 'mislead', 'deny'}, {'diminish', 'reduce', 'decrease', 'contract', 'descend', 'retreat', 'lose'},
           {'increase', 'expand', 'ascend', 'advance', 'win', 'victory', 'success', 'profit'}, {'eloquent', 'articulate'},
           {'hazardous', 'dangerous', 'reckless'}, {'imminent', 'impending'}, {'novice', 'beginner', 'ignorance', 'illiterate'},
           {'reluctant', 'unwilling', 'refuse', 'reject', 'forbid', 'repel'}, {'lucid', 'clear', 'transparent'},
           {'mediocre', 'average', 'inferior'}, {'ostentatious', 'showy', 'arrogant'}, {'trivial', 'unimportant', 'optional', 'voluntary'},
           {'compulsory', 'mandatory', 'permanent'}, {'allow', 'accept', 'approve', 'acquit'}, {'convict', 'guilty'},
           {'lenient', 'mild', 'gentle'}, {'cheap', 'poor'}]


def related(a, c):
    return any(a in g and c in g for g in RELATED)


def words_bank(b, pairs, ask, wrong_pool):
    pool = list(dict.fromkeys(wrong_pool))
    for word, answer in pairs:
        others = [w for w in pool if w not in (word, answer) and not related(w, word) and not related(w, answer)]
        b.rng.shuffle(others)
        b.add(ask(word), answer, others[:6])


def verbs(b, participle=False):
    for base, past, part in VERBS:
        ing = ING.get(base, base + 'ing')
        if participle:
            wrong = [past if past != part else base + 'ed', base + 'ed', ing, base]
            b.add(f'Which is the past participle of "{base}"? (They have ___.)', part, wrong)
        else:
            wrong = [part if part != past else base + 'ed', base + 'ed', base, ing]
            b.add(f'Fill in the gap: Yesterday, we ___ ({base}).', past, wrong)


def english_year7():
    b = Bank(7202)
    syn_words = [w for pair in SYN_Y7 for w in pair]
    words_bank(b, SYN_Y7, lambda w: f'Choose the word nearest in meaning to "{w}".', syn_words)
    ant_words = [w for pair in ANT_Y7 for w in pair]
    words_bank(b, ANT_Y7, lambda w: f'Choose the word opposite in meaning to "{w}".', ant_words)
    verbs(b)
    plural_wrong = lambda s, p: [s + 's', s + 'es', p + 's', s + 'en']
    for s, p in PLURAL_Y7:
        b.add(f'What is the plural of "{s}"?', p, [x for x in plural_wrong(s, p) if x != p] + [s + 'ies'])
    fem = [f for _, f in GENDER]
    words_bank(b, GENDER, lambda w: f'What is the feminine of "{w}"?', fem)
    names = [c for _, c in COLLECTIVE]
    for thing, name in COLLECTIVE:
        others = [n for n in names if n != name and n not in ALSO_FITS.get(thing, ())]
        b.rng.shuffle(others)
        b.add(f'Complete: a ___ of {thing}.', name, others)
    for base, comp, sup_ in COMPARE:
        b.add(f'What is the comparative form of "{base}"?', comp,
              [sup_, (base + 'er') if (base + 'er') != comp else 'more ' + base, 'more ' + comp if ' ' not in comp else comp.replace('more', 'most'), base])
        b.add(f'What is the superlative form of "{base}"?', sup_,
              [comp, (base + 'est') if (base + 'est') != sup_ else 'most ' + base, 'most ' + sup_ if ' ' not in sup_ else sup_.replace('most', 'more'), base])
    for sentence, prep, wrong in PREPOSITIONS:
        b.add(f'Choose the right word: {sentence}', prep, wrong)
    for word, wrong in SPELL[:22]:
        b.add(f'Which is the correct spelling? ({word[0].upper()}…, {len(word)} letters)', word, wrong)
    for sentence, right, wrong in CONCORD_Y7:
        b.add(f'Choose the right word: {sentence}', right, wrong)
    return b.items


def english_year10():
    b = Bank(10202)
    syn_words = [w for pair in SYN_Y10 for w in pair]
    words_bank(b, SYN_Y10, lambda w: f'Choose the word nearest in meaning to "{w}".', syn_words)
    ant_words = [w for pair in ANT_Y10 for w in pair]
    words_bank(b, ANT_Y10, lambda w: f'Choose the word opposite in meaning to "{w}".', ant_words)
    verbs(b, participle=True)
    for s, p in PLURAL_Y10:
        b.add(f'What is the plural of "{s}"?', p, [s + 's', s + 'es', p + 's', s.split(' ')[0] + 'es' if ' ' in s else s + 'i'])
    meanings = [m for _, m in IDIOMS]
    for idiom, meaning in IDIOMS:
        others = [m for m in meanings if m != meaning]
        b.rng.shuffle(others)
        b.add(f'What does the idiom "{idiom}" mean?', meaning, others)
    for sentence, figure in FIGURES:
        others = [f for f in FIGURE_NAMES if f != figure]
        b.rng.shuffle(others)
        b.add(f'Name the figure of speech: "{sentence}"', figure, others)
    for sentence, right, wrong in TAGS:
        b.add(f'Choose the correct question tag: {sentence}', right, wrong)
    for word, wrong in SPELL:
        b.add(f'Which is the correct spelling? ({word[0].upper()}…, {len(word)} letters)', word, wrong)
    for sentence, right, wrong in CONCORD_Y10:
        b.add(f'Choose the right word: {sentence}', right, wrong)
    for base, comp, sup_ in COMPARE:
        if base not in ('good', 'tall', 'happy', 'beautiful', 'careful', 'famous', 'thin', 'busy', 'bad', 'important'):
            continue
        b.add(f'Choose the right form: Of all the pupils, Ngozi is the ___ ({base}).', sup_,
              [comp, (base + 'est') if (base + 'est') != sup_ else 'most ' + base, base])
    return b.items


# =============================================================== General Knowledge
STATES = [('Abia', 'Umuahia', 'South East'), ('Adamawa', 'Yola', 'North East'), ('Akwa Ibom', 'Uyo', 'South South'),
          ('Anambra', 'Awka', 'South East'), ('Bauchi', 'Bauchi', 'North East'), ('Bayelsa', 'Yenagoa', 'South South'),
          ('Benue', 'Makurdi', 'North Central'), ('Borno', 'Maiduguri', 'North East'), ('Cross River', 'Calabar', 'South South'),
          ('Delta', 'Asaba', 'South South'), ('Ebonyi', 'Abakaliki', 'South East'), ('Edo', 'Benin City', 'South South'),
          ('Ekiti', 'Ado-Ekiti', 'South West'), ('Enugu', 'Enugu', 'South East'), ('Gombe', 'Gombe', 'North East'),
          ('Imo', 'Owerri', 'South East'), ('Jigawa', 'Dutse', 'North West'), ('Kaduna', 'Kaduna', 'North West'),
          ('Kano', 'Kano', 'North West'), ('Katsina', 'Katsina', 'North West'), ('Kebbi', 'Birnin Kebbi', 'North West'),
          ('Kogi', 'Lokoja', 'North Central'), ('Kwara', 'Ilorin', 'North Central'), ('Lagos', 'Ikeja', 'South West'),
          ('Nasarawa', 'Lafia', 'North Central'), ('Niger', 'Minna', 'North Central'), ('Ogun', 'Abeokuta', 'South West'),
          ('Ondo', 'Akure', 'South West'), ('Osun', 'Osogbo', 'South West'), ('Oyo', 'Ibadan', 'South West'),
          ('Plateau', 'Jos', 'North Central'), ('Rivers', 'Port Harcourt', 'South South'), ('Sokoto', 'Sokoto', 'North West'),
          ('Taraba', 'Jalingo', 'North East'), ('Yobe', 'Damaturu', 'North East'), ('Zamfara', 'Gusau', 'North West')]
ZONES = ['North Central', 'North East', 'North West', 'South East', 'South South', 'South West']
ELEMENTS = [('H', 'Hydrogen'), ('He', 'Helium'), ('Li', 'Lithium'), ('C', 'Carbon'), ('N', 'Nitrogen'), ('O', 'Oxygen'), ('F', 'Fluorine'),
            ('Ne', 'Neon'), ('Na', 'Sodium'), ('Mg', 'Magnesium'), ('Al', 'Aluminium'), ('Si', 'Silicon'), ('P', 'Phosphorus'),
            ('S', 'Sulphur'), ('Cl', 'Chlorine'), ('Ar', 'Argon'), ('K', 'Potassium'), ('Ca', 'Calcium'), ('Fe', 'Iron'),
            ('Cu', 'Copper'), ('Zn', 'Zinc'), ('Ag', 'Silver'), ('Au', 'Gold'), ('Hg', 'Mercury'), ('Pb', 'Lead'), ('Sn', 'Tin'),
            ('I', 'Iodine'), ('Mn', 'Manganese'), ('Ni', 'Nickel'), ('Br', 'Bromine')]
CAPITALS = [('Ghana', 'Accra'), ('Kenya', 'Nairobi'), ('Egypt', 'Cairo'), ('Cameroon', 'Yaoundé'), ('Togo', 'Lomé'),
            ('Niger Republic', 'Niamey'), ('Chad', 'N\'Djamena'), ('Senegal', 'Dakar'), ('Liberia', 'Monrovia'),
            ('Sierra Leone', 'Freetown'), ('The Gambia', 'Banjul'), ('Mali', 'Bamako'), ('Burkina Faso', 'Ouagadougou'),
            ('Ethiopia', 'Addis Ababa'), ('Uganda', 'Kampala'), ('Rwanda', 'Kigali'), ('Zambia', 'Lusaka'), ('Zimbabwe', 'Harare'),
            ('Angola', 'Luanda'), ('Morocco', 'Rabat'), ('Algeria', 'Algiers'), ('Tunisia', 'Tunis'), ('Sudan', 'Khartoum'),
            ('Gabon', 'Libreville'), ('Botswana', 'Gaborone'), ('Namibia', 'Windhoek'), ('Malawi', 'Lilongwe'), ('Mozambique', 'Maputo'),
            ('United Kingdom', 'London'), ('France', 'Paris'), ('Germany', 'Berlin'), ('Italy', 'Rome'), ('Spain', 'Madrid'),
            ('Portugal', 'Lisbon'), ('Russia', 'Moscow'), ('China', 'Beijing'), ('Japan', 'Tokyo'), ('India', 'New Delhi'),
            ('United States of America', 'Washington, D.C.'), ('Canada', 'Ottawa'), ('Brazil', 'Brasília'), ('Argentina', 'Buenos Aires'),
            ('Australia', 'Canberra'), ('Saudi Arabia', 'Riyadh'), ('Turkey', 'Ankara'), ('Greece', 'Athens'), ('Belgium', 'Brussels'),
            ('Sweden', 'Stockholm'), ('Norway', 'Oslo'), ('Mexico', 'Mexico City'), ('South Korea', 'Seoul'), ('Pakistan', 'Islamabad'),
            ('Ireland', 'Dublin'), ('Poland', 'Warsaw'), ('Austria', 'Vienna'), ('Switzerland', 'Bern'), ('Cuba', 'Havana'),
            ('Jamaica', 'Kingston'), ('Democratic Republic of the Congo', 'Kinshasa')]
FACTS_Y7 = [  # (question, answer, three wrong)
    ('Nigeria became independent in which year?', '1960', ['1963', '1914', '1970']),
    ('On what date does Nigeria celebrate its Independence Day?', '1 October', ['12 June', '29 May', '27 May']),
    ('What is the capital of Nigeria?', 'Abuja', ['Lagos', 'Kaduna', 'Ibadan']),
    ('What is the name of Nigeria\'s currency?', 'Naira', ['Cedi', 'Rand', 'Shilling']),
    ('What are the colours of the Nigerian flag?', 'Green, white, green', ['Green, yellow, green', 'Red, white, red', 'Green, white, red']),
    ('How many states are there in Nigeria?', '36', ['30', '37', '19']),
    ('Which is the longest river in Nigeria?', 'River Niger', ['River Benue', 'River Ogun', 'River Kaduna']),
    ('The Rivers Niger and Benue meet at which town?', 'Lokoja', ['Makurdi', 'Onitsha', 'Jebba']),
    ('What is the national motto of Nigeria?', 'Unity and Faith, Peace and Progress', ['One Nation, One Destiny', 'Peace and Unity', 'Faith and Hope']),
    ('Who designed the Nigerian flag?', 'Taiwo Akinkunmi', ['Herbert Macaulay', 'Nnamdi Azikiwe', 'Ahmadu Bello']),
    ('Water boils at what temperature at sea level?', '100 °C', ['0 °C', '50 °C', '212 °C']),
    ('Water freezes at what temperature?', '0 °C', ['100 °C', '10 °C', '−10 °C']),
    ('Which planet is nearest to the Sun?', 'Mercury', ['Venus', 'Earth', 'Mars']),
    ('Which is the largest planet in our solar system?', 'Jupiter', ['Saturn', 'Earth', 'Neptune']),
    ('Which planet is known as the Red Planet?', 'Mars', ['Venus', 'Jupiter', 'Mercury']),
    ('How many planets are in our solar system?', '8', ['9', '7', '10']),
    ('What do plants take in from the air to make their food?', 'Carbon dioxide', ['Oxygen', 'Nitrogen', 'Hydrogen']),
    ('Which gas do we need to breathe in to live?', 'Oxygen', ['Carbon dioxide', 'Nitrogen', 'Helium']),
    ('Which organ pumps blood round the body?', 'The heart', ['The lungs', 'The liver', 'The kidneys']),
    ('Which organ do we use to breathe?', 'The lungs', ['The heart', 'The stomach', 'The kidneys']),
    ('How many bones does an adult human have?', '206', ['300', '106', '212']),
    ('How many legs does an insect have?', 'Six', ['Eight', 'Four', 'Ten']),
    ('How many legs does a spider have?', 'Eight', ['Six', 'Ten', 'Four']),
    ('Which is the largest ocean in the world?', 'The Pacific Ocean', ['The Atlantic Ocean', 'The Indian Ocean', 'The Arctic Ocean']),
    ('Which is the largest continent?', 'Asia', ['Africa', 'Europe', 'Australia']),
    ('How many continents are there?', 'Seven', ['Five', 'Six', 'Eight']),
    ('Which animal is called the "ship of the desert"?', 'The camel', ['The horse', 'The donkey', 'The elephant']),
    ('Which is the largest land animal?', 'The African elephant', ['The giraffe', 'The hippopotamus', 'The rhinoceros']),
    ('Which is the tallest animal?', 'The giraffe', ['The elephant', 'The camel', 'The ostrich']),
    ('What do we call a baby cat?', 'A kitten', ['A puppy', 'A cub', 'A calf']),
    ('What do we call a baby dog?', 'A puppy', ['A kitten', 'A kid', 'A foal']),
    ('What do we call a baby goat?', 'A kid', ['A lamb', 'A calf', 'A cub']),
    ('How many days are in a leap year?', '366', ['365', '364', '360']),
    ('How many months have 30 days?', 'Four', ['Five', 'Six', 'Seven']),
    ('Which month is the shortest?', 'February', ['April', 'June', 'November']),
    ('How many centimetres make one metre?', '100', ['10', '1,000', '12']),
    ('How many grams make one kilogram?', '1,000', ['100', '10', '10,000']),
    ('How many minutes make one hour?', '60', ['100', '24', '30']),
    ('Which instrument is used to measure temperature?', 'Thermometer', ['Barometer', 'Ruler', 'Speedometer']),
    ('Which of these is a source of light?', 'The Sun', ['The Moon', 'A mirror', 'A window']),
    ('What is the main colour of chlorophyll in leaves?', 'Green', ['Yellow', 'Red', 'Blue']),
    ('What part of the plant takes in water from the soil?', 'The roots', ['The leaves', 'The flowers', 'The fruits']),
    ('Which vitamin do we get from sunlight?', 'Vitamin D', ['Vitamin C', 'Vitamin A', 'Vitamin K']),
    ('Oranges are a good source of which vitamin?', 'Vitamin C', ['Vitamin D', 'Vitamin B', 'Vitamin K']),
    ('Which disease is spread by mosquitoes?', 'Malaria', ['Cholera', 'Typhoid', 'Measles']),
    ('Which arm of government makes laws?', 'The legislature', ['The executive', 'The judiciary', 'The police']),
    ('Who heads a state government in Nigeria?', 'The governor', ['The president', 'The senator', 'The chairman']),
    ('How many local government areas are there in Nigeria?', '774', ['768', '36', '700']),
    ('What is the colour of the eagle on Nigeria\'s coat of arms?', 'Red', ['Black', 'Green', 'White']),
    ('What do the two horses on Nigeria\'s coat of arms stand for?', 'Dignity', ['Strength', 'Wealth', 'Peace']),
]
FACTS_Y10 = [
    ('Which arm of government interprets the law?', 'The judiciary', ['The legislature', 'The executive', 'The police']),
    ('Which arm of government carries out the law?', 'The executive', ['The judiciary', 'The legislature', 'The civil service']),
    ('Nigeria\'s National Assembly is made up of the Senate and the …', 'House of Representatives', ['House of Assembly', 'Council of State', 'House of Lords']),
    ('How many senators represent each state in the Senate?', 'Three', ['Two', 'Four', 'Five']),
    ('Nigeria became a republic in which year?', '1963', ['1960', '1966', '1979']),
    ('Nigeria\'s national anthem begins with the words …', 'Arise, O compatriots', ['Nigeria, we hail thee', 'I pledge to Nigeria', 'O God of creation']),
    ('In which state is Nigeria\'s highest point, Chappal Waddi?', 'Taraba', ['Plateau', 'Adamawa', 'Bauchi']),
    ('The Kainji Dam is on which river?', 'River Niger', ['River Benue', 'River Kaduna', 'River Ogun']),
    ('In which year did the Federal Government officially move from Lagos to Abuja?', '1991', ['1976', '1999', '1983']),
    ('The Amalgamation of the Northern and Southern Protectorates of Nigeria took place in …', '1914', ['1900', '1960', '1922']),
    ('What does CPU stand for?', 'Central Processing Unit', ['Computer Personal Unit', 'Central Program Utility', 'Control Processing Unit']),
    ('What does RAM stand for?', 'Random Access Memory', ['Read And Memorise', 'Rapid Access Machine', 'Read Access Memory']),
    ('Which of these is an input device?', 'Keyboard', ['Monitor', 'Printer', 'Speaker']),
    ('Which of these is an output device?', 'Printer', ['Mouse', 'Keyboard', 'Scanner']),
    ('How many bits make one byte?', '8', ['4', '16', '10']),
    ('Who is called the father of the computer?', 'Charles Babbage', ['Bill Gates', 'Alan Turing', 'Thomas Edison']),
    ('What does WWW stand for?', 'World Wide Web', ['World Web Wide', 'Wide World Web', 'Web World Wide']),
    ('Which memory keeps its contents when the computer is switched off?', 'ROM', ['RAM', 'Cache', 'Register']),
    ('The basic unit of life is the …', 'Cell', ['Tissue', 'Organ', 'Atom']),
    ('Which part of the cell controls its activities?', 'Nucleus', ['Cell membrane', 'Cytoplasm', 'Vacuole']),
    ('Which blood cells fight infection?', 'White blood cells', ['Red blood cells', 'Platelets', 'Plasma']),
    ('Which blood cells carry oxygen?', 'Red blood cells', ['White blood cells', 'Platelets', 'Lymph']),
    ('The process by which green plants make food is called …', 'Photosynthesis', ['Respiration', 'Transpiration', 'Digestion']),
    ('The loss of water from the leaves of plants is called …', 'Transpiration', ['Photosynthesis', 'Respiration', 'Excretion']),
    ('What is the chemical formula for water?', 'H₂O', ['CO₂', 'O₂', 'H₂O₂']),
    ('What is the chemical formula for carbon dioxide?', 'CO₂', ['CO', 'O₂', 'C₂O']),
    ('Common salt is the chemical called …', 'Sodium chloride', ['Calcium carbonate', 'Sodium hydroxide', 'Potassium chloride']),
    ('What is the SI unit of force?', 'Newton', ['Joule', 'Watt', 'Pascal']),
    ('What is the SI unit of energy?', 'Joule', ['Newton', 'Watt', 'Volt']),
    ('What is the SI unit of power?', 'Watt', ['Joule', 'Ampere', 'Newton']),
    ('What is the SI unit of electric current?', 'Ampere', ['Volt', 'Ohm', 'Watt']),
    ('What is the SI unit of length?', 'Metre', ['Kilometre', 'Centimetre', 'Foot']),
    ('What is the SI unit of mass?', 'Kilogram', ['Gram', 'Newton', 'Tonne']),
    ('Which gas makes up most of the air?', 'Nitrogen', ['Oxygen', 'Carbon dioxide', 'Argon']),
    ('Which organ removes urea from the blood?', 'The kidneys', ['The liver', 'The lungs', 'The heart']),
    ('Insulin is made by which organ?', 'The pancreas', ['The liver', 'The kidneys', 'The stomach']),
    ('Which planet is known for its rings?', 'Saturn', ['Mars', 'Venus', 'Mercury']),
    ('What is the closest star to the Earth?', 'The Sun', ['Sirius', 'The Moon', 'Polaris']),
    ('An instrument used to measure atmospheric pressure is the …', 'Barometer', ['Thermometer', 'Hygrometer', 'Anemometer']),
    ('An instrument used to measure wind speed is the …', 'Anemometer', ['Barometer', 'Rain gauge', 'Thermometer']),
    ('The study of living things is called …', 'Biology', ['Geology', 'Chemistry', 'Physics']),
    ('The study of the Earth\'s rocks is called …', 'Geology', ['Biology', 'Astronomy', 'Botany']),
    ('The study of plants is called …', 'Botany', ['Zoology', 'Ecology', 'Geology']),
    ('The study of animals is called …', 'Zoology', ['Botany', 'Geology', 'Ecology']),
    ('The headquarters of the African Union is in …', 'Addis Ababa', ['Abuja', 'Nairobi', 'Accra']),
    ('The headquarters of ECOWAS is in …', 'Abuja', ['Accra', 'Lagos', 'Dakar']),
    ('The headquarters of the United Nations is in …', 'New York', ['Geneva', 'London', 'Paris']),
    ('Which is the longest river in Africa?', 'The Nile', ['The Niger', 'The Congo', 'The Zambezi']),
    ('Which is the largest desert in Africa?', 'The Sahara', ['The Kalahari', 'The Namib', 'The Sahel']),
    ('Which is the highest mountain in Africa?', 'Mount Kilimanjaro', ['Mount Kenya', 'Mount Cameroon', 'Mount Everest']),
]


def gk_year7():
    b = Bank(7303)
    caps = [c for _, c, _ in STATES]
    for state, cap, _ in STATES:
        if state == cap:
            continue                                   # a capital that is the state's own name gives itself away
        others = [c for c in caps if c != cap]
        b.rng.shuffle(others)
        b.add(f'What is the capital of {state} State?', cap, others)
    names = [s for s, _, _ in STATES]
    for state, cap, _ in STATES:
        if state == cap:
            continue
        others = [s for s in names if s != state]
        b.rng.shuffle(others)
        b.add(f'{cap} is the capital of which state?', f'{state} State', [f'{o} State' for o in others])
    for state, _, zone in STATES:
        b.add(f'In which geopolitical zone is {state} State?', zone, [z for z in ZONES if z != zone])
    wcaps = [c for _, c in CAPITALS]
    for country, cap in CAPITALS:
        others = [c for c in wcaps if c != cap]
        b.rng.shuffle(others)
        b.add(f'What is the capital of {country}?', cap, others)
    for q, a, w in FACTS_Y7:
        b.add(q, a, w)
    return b.items


def gk_year10():
    b = Bank(10303)
    for state, cap, zone in STATES:
        b.add(f'In which geopolitical zone is {state} State?', zone, [z for z in ZONES if z != zone])
    caps = [c for _, c, _ in STATES]
    for state, cap, _ in STATES:
        others = [c for c in caps if c != cap]
        b.rng.shuffle(others)
        if state != cap:
            b.add(f'Which town is the capital of {state} State?', cap, others)
    names = [e for _, e in ELEMENTS]
    symbols = [s for s, _ in ELEMENTS]
    for sym, name in ELEMENTS:
        others = [n for n in names if n != name]
        b.rng.shuffle(others)
        b.add(f'Which element has the chemical symbol {sym}?', name, others)
        others = [s for s in symbols if s != sym] + [name[:2], name[:1] + name[2:3]]
        b.rng.shuffle(others)
        b.add(f'What is the chemical symbol for {name}?', sym, [o for o in others if o != sym])
    wcaps = [c for _, c in CAPITALS]
    for country, cap in CAPITALS:
        others = [c for c in wcaps if c != cap]
        b.rng.shuffle(others)
        b.add(f'What is the capital of {country}?', cap, others)
    for q, a, w in FACTS_Y10 + FACTS_Y7[:10]:
        b.add(q, a, w)
    return b.items


BANKS = [
    ('practice_year7_mathematics', 'year7', 'Mathematics', 'Year 7', maths_year7, 3600, 25),
    ('practice_year7_english', 'year7', 'English Language', 'Year 7', english_year7, 3600, 25),
    ('practice_year7_general_knowledge', 'year7', 'General Knowledge', 'Year 7', gk_year7, 3000, 25),
    ('practice_year10_mathematics', 'year10', 'Mathematics', 'Year 10', maths_year10, 3600, 25),
    ('practice_year10_english', 'year10', 'English Language', 'Year 10', english_year10, 3600, 25),
    ('practice_year10_general_knowledge', 'year10', 'General Knowledge', 'Year 10', gk_year10, 3000, 25),
]


def main():
    sys.path.insert(0, ROOT)
    from core.banks import check_bank
    os.makedirs(OUT, exist_ok=True)
    manifest = []
    for bid, group, subject, level, make, seconds, serve in BANKS:
        items = make()
        for i, q in enumerate(items, 1):
            q['id'] = i
        bank = {'id': bid, 'name': f'{subject} - entrance practice into {level}', 'level': level, 'entry_group': group,
                'subject': subject, 'duration_seconds': seconds, 'questions_to_serve': serve, 'version': '1.0',
                'source_status': 'platform_practice', 'questions': [dict(id=q['id'], **{k: v for k, v in q.items() if k != 'id'}) for q in items]}
        check_bank(dict(bank))                         # the same checks as a bank a school imports
        with open(os.path.join(OUT, bid + '.json'), 'w', encoding='utf-8', newline='\n') as fh:
            json.dump(bank, fh, ensure_ascii=False, indent=1)
            fh.write('\n')
        manifest.append({'file': bid + '.json', 'id': bid, 'entry_group': group, 'subject': subject, 'question_count': len(items)})
        print(f'{bid}: {len(items)} questions')
    with open(os.path.join(OUT, 'manifest.json'), 'w', encoding='utf-8', newline='\n') as fh:
        json.dump({'format': 1, 'name': 'Built-in entrance practice questions', 'version': '1.0',
                   'description': 'Added to every school\'s entrance practice pool, on top of its own earlier papers and practice '
                                  'bank (core/entrance.py). Platform content, read-only, never an examination paper. Built by '
                                  'tools/build_practice_banks.py.', 'banks': manifest}, fh, ensure_ascii=False, indent=1)
        fh.write('\n')


if __name__ == '__main__':
    main()
