"""Learning simulations: PhET Interactive Simulations (University of Colorado Boulder) in the student portal.

Each simulation is one of PhET's HTML5 ones, embedded from phet.colorado.edu (nothing is copied here, so students
need the internet while they use one). CATALOGUE (core/simulations_catalogue.py, data only) says which classes
each one suits, Primary 1 to SSS 3 (a class's ``level_order``: 1-6 Primary, 7-9 JSS, 10-12 SSS), its topic, and -
for SSS, where every student belongs to a department (core/school_structure.py) - which departments it is for. A
senior student whose department has not been set yet sees every SSS one, so nobody is hidden by missing data (as
``takes_subject`` does for subjects).

Where to look when something is wrong:

* a student sees the wrong simulations: ``for_student`` below (class from ``class_level``, then the school's
  choices from ``settings``, then the department);
* the student pages: blueprints/student_portal/simulations.py, templates/student_simulations.html and
  student_simulation.html; the menu entry is in blueprints/portal_shell.py;
* the settings page: blueprints/school/simulations.py and templates/admin_school_simulations.html;
* a simulation that will not load: ``csp`` below (the page's Content-Security-Policy must let PhET in).

On by default. The School Admin or a head teacher (Settings > Your school > Learning simulations,
blueprints/school/simulations.py) can switch it off for the whole school or for single classes, hide a simulation,
and change the classes and departments a simulation is shown to. All of that is one JSON setting in
school_settings; a school that has never saved it reads as DEFAULT, with every simulation as suggested here.

PhET's simulations are licensed CC BY 4.0: every page that shows one credits PhET and links to the licence.
"""

from sqlalchemy import select

from core.school_structure import DEPARTMENTS, clean_department
from core.simulations_catalogue import CATALOGUE, TOPICS

KEY = 'learning_simulations'
DEFAULT = {'enabled': True, 'classes_off': [], 'hidden': [], 'ranges': {}, 'departments': {}}

PHET = 'https://phet.colorado.edu'
CREDIT = 'PhET Interactive Simulations, University of Colorado Boulder'
LICENCE_URL = 'https://creativecommons.org/licenses/by/4.0/'

# A class's level_order, Primary 1 to SSS 3.
LEVELS = {1: 'Primary 1', 2: 'Primary 2', 3: 'Primary 3', 4: 'Primary 4', 5: 'Primary 5', 6: 'Primary 6',
          7: 'JSS 1', 8: 'JSS 2', 9: 'JSS 3', 10: 'SSS 1', 11: 'SSS 2', 12: 'SSS 3'}
FIRST_SENIOR = 10

BY_SLUG = {row[0]: row for row in CATALOGUE}


def run_url(slug):
    return f'{PHET}/sims/html/{slug}/latest/{slug}_en.html'


def picture_url(slug):
    return f'{PHET}/sims/html/{slug}/latest/{slug}-600.png'


def about_url(slug):
    return f'{PHET}/en/simulations/{slug}'


# ---------------------------------------------------------------- the school's choices
def settings():
    """The school's choices, cleaned: anything unknown or out of range is dropped, so a bad value never breaks
    the student portal."""
    from blueprints.finance.helpers import json_setting
    s = json_setting(KEY, DEFAULT)
    s['enabled'] = bool(s['enabled'])
    s['classes_off'] = sorted({int(c) for c in (s['classes_off'] or []) if str(c).isdigit()})
    s['hidden'] = sorted({slug for slug in (s['hidden'] or []) if slug in BY_SLUG})
    ranges = {}
    for slug, pair in (s['ranges'] or {}).items() if isinstance(s['ranges'], dict) else ():
        try:
            lo, hi = int(pair[0]), int(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        if slug in BY_SLUG and lo in LEVELS and hi in LEVELS and lo <= hi:
            ranges[slug] = [lo, hi]
    s['ranges'] = ranges
    departments = {}
    for slug, values in (s['departments'] or {}).items() if isinstance(s['departments'], dict) else ():
        if slug in BY_SLUG and isinstance(values, list):
            departments[slug] = [d for d in DEPARTMENTS if d in values]
    s['departments'] = departments
    return s


def save(new, admin_id):
    """Save the school's choices. Does not commit."""
    from blueprints.finance.helpers import save_json_setting
    save_json_setting(KEY, {k: new[k] for k in DEFAULT}, admin_id)


def entry(slug, s=None):
    """One simulation as the school has it: {'slug', 'title', 'topic', 'topic_label', 'lo', 'hi', 'departments',
    'hidden', 'changed', 'suggested': (lo, hi, departments)}."""
    s = s if s is not None else settings()
    _slug, title, topic, lo, hi, departments = BY_SLUG[slug]
    own_range = s['ranges'].get(slug)
    own_departments = s['departments'].get(slug)
    return {'slug': slug, 'title': title, 'topic': topic, 'topic_label': TOPICS[topic],
            'lo': own_range[0] if own_range else lo, 'hi': own_range[1] if own_range else hi,
            'departments': list(own_departments if own_departments is not None else departments),
            'hidden': slug in s['hidden'], 'changed': bool(own_range or own_departments is not None),
            'suggested': (lo, hi, list(departments))}


def entries(s=None):
    s = s if s is not None else settings()
    return [entry(row[0], s) for row in CATALOGUE]


def class_level(class_id):
    """The class's place from Primary 1 (1) to SSS 3 (12), or None for any other class (Crèche, Nursery)."""
    from models import SchoolClass, db
    if not class_id:
        return None
    level = db.session.scalar(select(SchoolClass.level_order).where(SchoolClass.id == int(class_id)))
    return level if level in LEVELS else None


def student_department(student):
    """An SSS student's department (Science, Art or Commercial), or None: outside SSS, or not set yet."""
    level = class_level(student.get('class_id')) if student else None
    return clean_department(student.get('department')) if level and level >= FIRST_SENIOR else None


def open_for_class(class_id, s=None):
    """Whether this class's students see simulations at all."""
    s = s if s is not None else settings()
    return bool(s['enabled'] and class_id and int(class_id) not in s['classes_off'] and class_level(class_id))


def for_student(student, s=None):
    """The simulations this student sees, in catalogue order: their class's, and in SSS their department's.
    Empty when the school or the class has them switched off. ``student`` has 'class_id' and 'department'."""
    s = s if s is not None else settings()
    class_id = student.get('class_id') if student else None
    if not open_for_class(class_id, s):
        return []
    level = class_level(class_id)
    department = student_department(student)
    out = []
    for e in entries(s):
        if e['hidden'] or not (e['lo'] <= level <= e['hi']):
            continue
        if department and department not in e['departments']:
            continue
        out.append(e)
    return out


def senior_departments(e):
    """How the departments read on the settings page: '' outside SSS, else 'All departments' or the list."""
    if e['hi'] < FIRST_SENIOR:
        return ''
    if not e['departments']:
        return 'No department'
    return 'All departments' if len(e['departments']) == len(DEPARTMENTS) else ', '.join(e['departments'])


def csp(nonce):
    """This page's Content-Security-Policy: the portal's own (app.py's apply_security_headers), plus PhET's site
    for the embedded simulation and its pictures - and for nothing else."""
    return ("default-src 'self'; img-src 'self' data: " + PHET + "; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'nonce-" + nonce + "'; frame-src " + PHET + "; object-src 'none'; "
            "base-uri 'self'; frame-ancestors 'none'")
