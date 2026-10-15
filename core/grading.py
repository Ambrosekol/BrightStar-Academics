"""Grading schemes: how a term result is made up and graded, per level of the school, per session.

A scheme says, for one level (Nursery, Primary, JSS or SSS) in one session:

* the score split: the Exam's share of the 100 marks, and up to four parts of continuous assessment (CA), each with a
  name, a share and where its marks come from: the class's tests, its assignments, its projects, or marks typed in by
  the teacher for that part (CA1, CA2, CA3...). Exam and parts always add up to 100;
* the grade scale: bands from the top down, each with the lowest percentage for it, the grade (A, B3, "Very good") and
  a remark. The last band starts at 0.

Stored in school_settings as ``grading:<session id>`` = ``{level: scheme}``. A session with nothing stored uses the
school's original split (its CA weights, Settings > Academic sessions) and the A-F scale, so a school that never opens
Grading sees exactly what it always saw. Saving one session's schemes first writes down what every other session is
using at that moment, so changing this session never changes a report card of another one.

Read on every report card, so the stored schemes are kept for a short while in each process (core/short_cache.py)
and dropped at once when they are saved (forget()).
"""

import json
from datetime import datetime, timezone

from sqlalchemy import select

from core.db_helpers import tuples

LEVELS = (('early', 'Nursery'), ('primary', 'Primary'), ('jss', 'JSS'), ('sss', 'SSS'))
LEVEL_NAMES = dict(LEVELS)
SOURCES = {'test': 'Tests', 'assignment': 'Assignments', 'project': 'Projects', 'entered': 'Typed in by the teacher'}
ENTERED_KEYS = ('ca1', 'ca2', 'ca3', 'ca4')
MAX_PARTS = 4
MAX_BANDS = 12
KEY_PREFIX = 'grading:'
LATEST_KEY = 'grading:latest'       # the session whose grading the school saved last: a session without its own uses it
CACHE = 'grading-schemes'

STANDARD_SCALE = [[70, 'A', 'Excellent'], [60, 'B', 'Very Good'], [50, 'C', 'Good'], [45, 'D', 'Fair'], [40, 'E', 'Pass'],
                  [0, 'F', 'Fail']]
PRESETS = {
    'standard': ('A to F', STANDARD_SCALE),
    'waec': ('WAEC (A1 to F9)', [[75, 'A1', 'Excellent'], [70, 'B2', 'Very Good'], [65, 'B3', 'Good'], [60, 'C4', 'Credit'],
                                 [55, 'C5', 'Credit'], [50, 'C6', 'Credit'], [45, 'D7', 'Pass'], [40, 'E8', 'Pass'],
                                 [0, 'F9', 'Fail']]),
    'descriptive': ('Descriptive (for nursery)', [[80, 'Excellent', ''], [65, 'Very good', ''], [50, 'Good', ''],
                                                 [40, 'Fair', ''], [0, 'Needs improvement', '']]),
}


def level_of(stage, name=''):
    """The level a class belongs to, from its stage (and name, for older classes)."""
    stage, name = (stage or '').strip().lower(), (name or '').strip().lower()
    if stage == 'jss' or name.startswith('jss'):
        return 'jss'
    if stage in ('sss', 'ss') or name.startswith(('sss', 'ss ')):
        return 'sss'
    if stage == 'primary' or name.startswith('primary'):
        return 'primary'
    return 'early'


def _legacy():
    """The split and scale a school used before grading schemes: its CA weights and A-F."""
    from app import CA_DEFAULT_WEIGHTS, EXAM_MAX_SCORE
    try:
        from blueprints.school.helpers import _ca_weights
        weights = _ca_weights()
    except Exception:
        weights = dict(CA_DEFAULT_WEIGHTS)
    parts = [{'key': k, 'label': k.capitalize(), 'max': float(weights.get(k) or 0), 'from': k}
             for k in ('test', 'assignment', 'project') if float(weights.get(k) or 0) > 0]
    return {'exam_max': float(EXAM_MAX_SCORE), 'parts': parts, 'scale': [list(b) for b in STANDARD_SCALE]}


def _num(value, low, high):
    try:
        n = round(float(value), 2)
    except (TypeError, ValueError):
        raise ValueError('Every mark and percentage must be a number.')
    if n != n or not low <= n <= high:
        raise ValueError(f'Every mark and percentage must be from {low:g} to {high:g}.')
    return n


def _words(value, limit):
    return ' '.join(str(value or '').split())[:limit]


def clean(raw):
    """One level's scheme from a form or JSON, checked. Raises ValueError saying what to fix."""
    if not isinstance(raw, dict):
        raise ValueError('The scheme could not be read.')
    exam = _num(raw.get('exam_max'), 0, 100)
    parts, seen, entered = [], set(), iter(ENTERED_KEYS)
    for p in (raw.get('parts') or [])[:MAX_PARTS + 4]:
        if not isinstance(p, dict) or (p.get('max') in (None, '') and not _words(p.get('label'), 20)):
            continue
        source = p.get('from') if p.get('from') in SOURCES else 'entered'
        if source != 'entered' and source in seen:
            raise ValueError(f'Only one part can come from {SOURCES[source].lower()}.')
        seen.add(source)
        key = next(entered, None) if source == 'entered' else source
        if key is None:
            raise ValueError(f'At most {len(ENTERED_KEYS)} parts can be typed in.')
        label = _words(p.get('label'), 20) or (key.upper() if source == 'entered' else SOURCES[source][:-1])
        mark = _num(p.get('max'), 0, 100)
        if mark <= 0:
            raise ValueError(f'"{label}" needs a share above 0, or remove it.')
        parts.append({'key': key, 'label': label, 'max': mark, 'from': source})
    if len(parts) > MAX_PARTS:
        raise ValueError(f'A scheme can have at most {MAX_PARTS} CA parts.')
    total = round(exam + sum(p['max'] for p in parts), 2)
    if abs(total - 100) > 0.01:
        raise ValueError(f'The Exam and the CA parts add up to {total:g}. They must add up to 100.')
    if len({p['label'].casefold() for p in parts}) != len(parts):
        raise ValueError('Give each CA part its own name.')
    bands = []
    for b in (raw.get('scale') or [])[:MAX_BANDS + 4]:
        if isinstance(b, dict):
            b = [b.get('min'), b.get('grade'), b.get('remark')]
        if not isinstance(b, (list, tuple)) or len(b) < 2 or (b[0] in (None, '') and not _words(b[1], 20)):
            continue
        grade = _words(b[1], 20)
        if not grade:
            raise ValueError('Every grade band needs a grade.')
        bands.append([_num(b[0] if b[0] not in (None, '') else 0, 0, 100), grade, _words(b[2] if len(b) > 2 else '', 30)])
    if len(bands) < 2:
        raise ValueError('The grade scale needs at least two bands.')
    if len(bands) > MAX_BANDS:
        raise ValueError(f'The grade scale can have at most {MAX_BANDS} bands.')
    bands.sort(key=lambda b: -b[0])
    if len({b[0] for b in bands}) != len(bands):
        raise ValueError('Two grade bands start at the same percentage.')
    if len({b[1].casefold() for b in bands}) != len(bands):
        raise ValueError('Two grade bands have the same grade.')
    bands[-1][0] = 0.0          # the lowest band always starts at 0, so every mark has a grade
    return {'exam_max': exam, 'parts': parts, 'scale': bands}


# ---------------------------------------------------------------- stored schemes
def _school_id():
    from blueprints.finance.helpers import _primary_school_id
    return _primary_school_id()


def _load():
    from models import SchoolSetting
    school_id = _school_id()
    if not school_id:
        return {}
    out = {}
    for key, value in tuples(select(SchoolSetting.setting_key, SchoolSetting.setting_value).where(
            SchoolSetting.school_id == school_id, SchoolSetting.setting_key.startswith(KEY_PREFIX))):
        if key == LATEST_KEY:
            try:
                out['latest'] = int(value)
            except (TypeError, ValueError):
                pass
            continue
        try:
            session_id = int(key[len(KEY_PREFIX):])
            data = json.loads(value or '{}')
        except (TypeError, ValueError):
            continue
        schemes = {}
        for level, raw in (data.items() if isinstance(data, dict) else []):
            if level in LEVEL_NAMES:
                try:
                    schemes[level] = clean(raw)
                except ValueError:
                    continue
        out[session_id] = schemes
    return out


def stored():
    """``{session id: {level: scheme}}`` as saved: once a minute or so per process, not once per report card."""
    from control_plane.config import design_cache_seconds
    from core.short_cache import remember
    return remember(CACHE, design_cache_seconds(), _load)


def forget():
    from core.short_cache import forget_here
    forget_here(CACHE)


def legacy():
    """The school's original split and the A-F scale, worked out once per request."""
    from core.speed import request_memo
    return request_memo('grading-legacy', _legacy)


def _in_use(session_id, data):
    """The schemes a session uses: its own, or else those the school saved last (a session created after the school
    set its grading carries it on), or else none (the original split and scale)."""
    sid = int(session_id) if session_id else 0
    return data.get(sid) or data.get(data.get('latest')) or {}


def scheme_for(session_id, level):
    """The scheme one level uses in one session."""
    own = _in_use(session_id, stored())
    return own.get(level or 'jss') or own.get('jss') or legacy()


def schemes_for(session_id):
    """``{level: scheme}`` for every level in a session, and whether the session has its own."""
    data = stored()
    own = _in_use(session_id, data)
    return {level: own.get(level) or legacy() for level, _ in LEVELS}, bool(data.get(int(session_id) if session_id else 0))


def inherited_from(session_id):
    """The id of the earlier session whose grading a session carries on, or None (its own, or the original)."""
    data, sid = stored(), int(session_id) if session_id else 0
    latest = data.get('latest')
    return latest if not data.get(sid) and latest != sid and data.get(latest) else None


def save(session_id, schemes, admin_id):
    """Save a session's schemes (``{level: raw scheme}``, each checked). Every other session that has none of its own
    first gets what it is using now written down, so its report cards never change. Does not commit."""
    from models import AcademicSession, SchoolSetting, db
    clean_schemes = {level: clean(schemes[level]) for level in LEVEL_NAMES if level in schemes}
    if set(clean_schemes) != set(LEVEL_NAMES):
        raise ValueError('Every level needs a scheme.')
    school_id = _school_id()
    if not school_id:
        raise ValueError('No active school is configured.')
    now = datetime.now(timezone.utc).isoformat()
    rows = {r.setting_key: r for r in db.session.scalars(select(SchoolSetting).where(
        SchoolSetting.school_id == school_id, SchoolSetting.setting_key.startswith(KEY_PREFIX)))}
    have = set(rows)
    data = _load()                  # fresh, not the per-process copy: what each session uses right now
    for (sid,) in tuples(select(AcademicSession.id)):
        key = f'{KEY_PREFIX}{sid}'
        if sid != int(session_id) and key not in have:
            using = _in_use(sid, data)
            value = json.dumps({level: using.get(level) or legacy() for level in LEVEL_NAMES}, sort_keys=True)
            db.session.add(SchoolSetting(school_id=school_id, setting_key=key, setting_value=value, updated_at=now,
                                         updated_by=admin_id))
    for key, value in ((f'{KEY_PREFIX}{int(session_id)}', json.dumps(clean_schemes, sort_keys=True)),
                       (LATEST_KEY, str(int(session_id)))):
        row = rows.get(key)
        if row:
            row.setting_value, row.updated_at, row.updated_by = value, now, admin_id
        else:
            db.session.add(SchoolSetting(school_id=school_id, setting_key=key, setting_value=value, updated_at=now,
                                         updated_by=admin_id))
    return clean_schemes


# ---------------------------------------------------------------- using a scheme
def grade(percentage, scheme):
    """``(grade, remark)`` for a percentage in a scheme's scale."""
    pct = float(percentage or 0)
    for low, letter, remark in scheme['scale']:
        if pct >= low:
            return letter, remark
    return scheme['scale'][-1][1], scheme['scale'][-1][2]


def key_of(scheme):
    """The scale as it is printed on a card: ``[{'range': '70 - 100', 'grade', 'remark'}]``."""
    out, upper = [], 100
    for low, letter, remark in scheme['scale']:
        lo = int(low) if float(low).is_integer() else low
        out.append({'range': f'{lo:g} - {upper:g}', 'grade': letter, 'remark': remark})
        upper = round(low - 1 if float(low).is_integer() else low - 0.01, 2)
    return out


def class_levels(class_ids):
    """``{class id: level}`` in one query."""
    from models import SchoolClass
    ids = [c for c in set(class_ids) if c]
    if not ids:
        return {}
    return {cid: level_of(stage, name) for cid, stage, name in tuples(
        select(SchoolClass.id, SchoolClass.stage, SchoolClass.name).where(SchoolClass.id.in_(ids)))}


def student_levels(student_ids, session_id):
    """``{student id: level}`` from each student's class in the session, in one query."""
    from models import SchoolClass, StudentEnrolment
    ids = list(set(student_ids))
    if not ids:
        return {}
    return {sid: level_of(stage, name) for sid, stage, name in tuples(
        select(StudentEnrolment.student_id, SchoolClass.stage, SchoolClass.name)
        .join(SchoolClass, SchoolClass.id == StudentEnrolment.class_id)
        .where(StudentEnrolment.student_id.in_(ids), StudentEnrolment.session_id == session_id,
               StudentEnrolment.active == 1))}


def typed_parts(scheme):
    """The parts a teacher types marks for on the score sheet: the Test part (if any) and each typed-in part."""
    return [p for p in scheme['parts'] if p['from'] in ('test', 'entered')]
