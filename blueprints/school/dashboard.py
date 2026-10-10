"""The staff Overview: a dense, at-a-glance report made for the person looking at it.

Nobody sees a general page. The Overview is built from panels, and each panel is only worked out
(and shown) when the signed-in person's role and teaching duties call for it:

    academic       proprietor and head teacher: the whole school - scores, attendance, results, enrolment
    my_classes     a class teacher: each class they are in charge of
    my_teaching    a subject teacher: each subject they teach, in each class
    finance        bursar and finance officers (a cashier sees only their own takings)
    library        librarian
    admissions     admissions officer
    parents        whoever answers parents
    records        secretary and records officers

Every headline number carries a comparison (last term, last session, yesterday, last month) so the page
reads as a report, not a tally. Charts are plain data here; templates/includes/_dash.html draws them.
"""

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

from flask import request, url_for
from sqlalchemy import and_, case, func, or_, select

from app import ACADEMIC_TERMS, _school_current_session
from core.db_helpers import one_scalar, tuples
from core.school_structure import LEVEL_LABELS, LEVEL_RANK, TOP_LEVEL_LABEL, is_early_years, is_senior
from core.security import (
    admin_covers_whole_school, admin_has_permission, admin_rank, is_school_admin, teaching_duties,
)
from models import (
    AcademicSession, Admin, AttendanceRecord, LibraryBook, LibraryLoan, ParentFeedback, ParentStudentLink,
    ReportCardComment, SchoolAssessment, SchoolAssignment, SchoolClass, SchoolProject, SchoolStudentResult,
    SchoolSubject, Student, StudentEnrolment, TeachingDuty, db,
)

WAITING = ('draft', 'entered', 'verified')   # recorded, not yet sent on by the subject teacher
ATTENDED = ('present', 'late')


def _can(me, code):
    return admin_has_permission(me['id'], code)


def _official():
    return or_(SchoolAssessment.assessment_type.is_(None), SchoolAssessment.assessment_type != 'practice')


def _term_of():
    return func.coalesce(SchoolStudentResult.term, 'Full Session')


def _pct(part, whole):
    return round(100.0 * part / whole, 1) if whole else None


def delta(now, before, unit='', good_up=True, label='', digits=1):
    """A comparison for a stat: the signed change, its direction, and whether that direction is good.
    None when there is nothing to compare with, so the template simply leaves it out."""
    if now is None or before is None:
        return None
    diff = round(float(now) - float(before), digits)
    if unit == '%':
        text = f'{diff:+.{digits}f} pts'
    elif unit == '₦':
        text = ('+' if diff >= 0 else '−') + '₦' + f'{abs(diff):,.0f}'
    else:
        text = f'{diff:+,.0f}' if digits == 0 or float(diff).is_integer() else f'{diff:+,.{digits}f}'
    direction = 'up' if diff > 0 else ('down' if diff < 0 else 'flat')
    tone = 'flat' if direction == 'flat' else ('good' if (direction == 'up') == good_up else 'bad')
    return {'text': text.replace('-', '−'), 'direction': direction, 'tone': tone, 'label': label}


# ------------------------------------------------------------------------------------------ periods

def dashboard_term(session_id):
    """The term the Overview speaks about: the one asked for, else the latest term of the current session
    that has any result or attendance recorded, else First Term."""
    asked = request.args.get('term', '').strip()
    if asked in ACADEMIC_TERMS:
        return asked
    if session_id:
        used = {t for (t,) in tuples(select(_term_of()).where(SchoolStudentResult.session_id == session_id).distinct())}
        used |= {t for (t,) in tuples(select(AttendanceRecord.term).where(AttendanceRecord.session_id == session_id).distinct())}
        for term in reversed(ACADEMIC_TERMS):
            if term in used:
                return term
    return ACADEMIC_TERMS[0]


def previous_period(session, term):
    """The term before this one: an earlier term of the same session, or the last term of the session
    before. (session_id, term, label) or None."""
    if not session:
        return None
    i = ACADEMIC_TERMS.index(term) if term in ACADEMIC_TERMS else 0
    if i > 0:
        return session.id, ACADEMIC_TERMS[i - 1], ACADEMIC_TERMS[i - 1]
    prior = db.session.scalars(select(AcademicSession).where(AcademicSession.id < session.id, AcademicSession.active == 1)
                               .order_by(AcademicSession.id.desc()).limit(1)).first()
    return (prior.id, ACADEMIC_TERMS[-1], f'{ACADEMIC_TERMS[-1]}, {prior.name}') if prior else None


def _previous_session_id(session):
    if not session:
        return None
    return one_scalar(select(AcademicSession.id).where(AcademicSession.id < session.id, AcademicSession.active == 1)
                      .order_by(AcademicSession.id.desc()).limit(1))


# ------------------------------------------------------------------------------------------ measures

def _enrolled(session_id, class_ids=None):
    """{class_id: active students enrolled that session}."""
    if not session_id:
        return {}
    stmt = (select(StudentEnrolment.class_id, func.count())
            .join(Student, Student.id == StudentEnrolment.student_id)
            .where(StudentEnrolment.session_id == session_id, StudentEnrolment.active == 1, Student.active == 1)
            .group_by(StudentEnrolment.class_id))
    if class_ids is not None:
        stmt = stmt.where(StudentEnrolment.class_id.in_(list(class_ids) or [0]))
    return dict(tuples(stmt))


def _scores(session_id, term, by, class_ids=None, pairs=None):
    """Average score (percent of marks available) of a term's official results, grouped by ``by``:
    'class', 'subject', 'pair' (class, subject) or 'student'. Every recorded mark counts, released or not,
    because staff read this to see how teaching is going, not what parents can see yet."""
    if not session_id:
        return {}
    key = {'class': (StudentEnrolment.class_id,), 'subject': (SchoolStudentResult.subject_id,),
           'pair': (StudentEnrolment.class_id, SchoolStudentResult.subject_id),
           'student': (SchoolStudentResult.student_id,)}[by]
    stmt = (select(*key, func.sum(SchoolStudentResult.score), func.sum(SchoolStudentResult.max_score))
            .select_from(SchoolStudentResult)
            .join(StudentEnrolment, and_(StudentEnrolment.student_id == SchoolStudentResult.student_id,
                                         StudentEnrolment.session_id == session_id, StudentEnrolment.active == 1))
            .outerjoin(SchoolAssessment, SchoolAssessment.id == SchoolStudentResult.assessment_id)
            .where(SchoolStudentResult.session_id == session_id, _term_of() == term, _official(),
                   SchoolStudentResult.score.is_not(None), SchoolStudentResult.max_score > 0)
            .group_by(*key))
    if class_ids is not None:
        stmt = stmt.where(StudentEnrolment.class_id.in_(list(class_ids) or [0]))
    out = {}
    for row in tuples(stmt):
        k = tuple(row[:-2]) if len(key) > 1 else row[0]
        if pairs is not None and k not in pairs:
            continue
        out[k] = _pct(float(row[-2] or 0), float(row[-1] or 0))
    return out


def _overall_score(session_id, term, class_ids=None):
    if not session_id:
        return None
    stmt = (select(func.sum(SchoolStudentResult.score), func.sum(SchoolStudentResult.max_score))
            .select_from(SchoolStudentResult)
            .join(StudentEnrolment, and_(StudentEnrolment.student_id == SchoolStudentResult.student_id,
                                         StudentEnrolment.session_id == session_id, StudentEnrolment.active == 1))
            .outerjoin(SchoolAssessment, SchoolAssessment.id == SchoolStudentResult.assessment_id)
            .where(SchoolStudentResult.session_id == session_id, _term_of() == term, _official(),
                   SchoolStudentResult.score.is_not(None), SchoolStudentResult.max_score > 0))
    if class_ids is not None:
        stmt = stmt.where(StudentEnrolment.class_id.in_(list(class_ids) or [0]))
    got, out_of = db.session.execute(stmt).one()
    return _pct(float(got or 0), float(out_of or 0))


def _statuses(session_id, term, class_ids=None, pairs=None):
    """{(class_id, subject_id, status): count} of a term's official results."""
    if not session_id:
        return {}
    stmt = (select(StudentEnrolment.class_id, SchoolStudentResult.subject_id, SchoolStudentResult.status, func.count())
            .select_from(SchoolStudentResult)
            .join(StudentEnrolment, and_(StudentEnrolment.student_id == SchoolStudentResult.student_id,
                                         StudentEnrolment.session_id == session_id, StudentEnrolment.active == 1))
            .outerjoin(SchoolAssessment, SchoolAssessment.id == SchoolStudentResult.assessment_id)
            .where(SchoolStudentResult.session_id == session_id, _term_of() == term, _official())
            .group_by(StudentEnrolment.class_id, SchoolStudentResult.subject_id, SchoolStudentResult.status))
    if class_ids is not None:
        stmt = stmt.where(StudentEnrolment.class_id.in_(list(class_ids) or [0]))
    rows = {(c, s, st): n for c, s, st, n in tuples(stmt)}
    if pairs is not None:
        rows = {k: v for k, v in rows.items() if (k[0], k[1]) in pairs}
    return rows


def _count(counts, statuses, class_id=None, subject_id=None):
    return sum(n for (c, s, st), n in counts.items()
               if st in statuses and (class_id is None or c == class_id) and (subject_id is None or s == subject_id))


def _pipeline(counts, class_id=None, subject_id=None):
    """The ordered result pipeline for a stacked bar: not sent -> ready -> released."""
    return [{'key': 'waiting', 'label': 'Not sent yet', 'value': _count(counts, WAITING, class_id, subject_id)},
            {'key': 'ready', 'label': 'Ready to release', 'value': _count(counts, ('approved',), class_id, subject_id)},
            {'key': 'released', 'label': 'Released', 'value': _count(counts, ('released',), class_id, subject_id)}]


def _attendance(session_id, term, class_ids=None, by_class=False):
    """Attendance rate (present or late, of every day recorded) for a term: overall, or {class_id: rate}."""
    if not session_id:
        return {} if by_class else None
    attended = func.sum(case((AttendanceRecord.status.in_(ATTENDED), 1), else_=0))
    cols = ((AttendanceRecord.class_id,) if by_class else ()) + (attended, func.count())
    stmt = select(*cols).where(AttendanceRecord.session_id == session_id, AttendanceRecord.term == term)
    if class_ids is not None:
        stmt = stmt.where(AttendanceRecord.class_id.in_(list(class_ids) or [0]))
    if by_class:
        return {cid: _pct(float(a or 0), n) for cid, a, n in tuples(stmt.group_by(AttendanceRecord.class_id))}
    a, n = db.session.execute(stmt).one()
    return _pct(float(a or 0), n)


def _attendance_days(class_ids=None, days=14):
    """The attendance rate of each of the last ``days`` days a register was taken, oldest first."""
    attended = func.sum(case((AttendanceRecord.status.in_(ATTENDED), 1), else_=0))
    stmt = select(AttendanceRecord.date, attended, func.count()).group_by(AttendanceRecord.date)
    if class_ids is not None:
        stmt = stmt.where(AttendanceRecord.class_id.in_(list(class_ids) or [0]))
    rows = tuples(stmt.order_by(AttendanceRecord.date.desc()).limit(days))
    return [{'label': _short_date(d), 'value': _pct(float(a or 0), n), 'tip': f'{_short_date(d)}: {_pct(float(a or 0), n)}% present ({a} of {n})'}
            for d, a, n in reversed(rows)]


def _short_date(iso):
    try:
        return datetime.fromisoformat(str(iso)[:10]).strftime('%d %b')
    except ValueError:
        return str(iso)


def _classes(ids=None):
    stmt = select(SchoolClass).where(SchoolClass.active == 1).order_by(SchoolClass.level_order)
    rows = db.session.scalars(stmt).all()
    return [c for c in rows if ids is None or c.id in ids]


def _section(cls):
    if is_early_years(cls.stage, cls.name):
        return 'Crèche & Nursery'
    if is_senior(cls.stage, cls.name):
        return 'SSS'
    stage = (cls.stage or '').upper()
    return 'JSS' if 'JSS' in stage or cls.name.upper().startswith('JSS') else 'Primary'


def _student_names(ids):
    return {sid: ' '.join(p for p in (f, l) if p) for sid, f, l in tuples(
        select(Student.id, Student.first_name, Student.last_name).where(Student.id.in_(list(ids) or [0])))}


def _top_students(session_id, term, class_ids, limit=1, lowest=False):
    """The best (or weakest) students of each class this term by average score: {class_id: [rows]}."""
    if not session_id:
        return {}
    got = func.sum(SchoolStudentResult.score)
    out_of = func.sum(SchoolStudentResult.max_score)
    rows = tuples(select(StudentEnrolment.class_id, SchoolStudentResult.student_id, got, out_of)
                  .select_from(SchoolStudentResult)
                  .join(StudentEnrolment, and_(StudentEnrolment.student_id == SchoolStudentResult.student_id,
                                               StudentEnrolment.session_id == session_id, StudentEnrolment.active == 1))
                  .outerjoin(SchoolAssessment, SchoolAssessment.id == SchoolStudentResult.assessment_id)
                  .where(SchoolStudentResult.session_id == session_id, _term_of() == term, _official(),
                         SchoolStudentResult.score.is_not(None), SchoolStudentResult.max_score > 0,
                         StudentEnrolment.class_id.in_(list(class_ids) or [0]))
                  .group_by(StudentEnrolment.class_id, SchoolStudentResult.student_id))
    by_class = defaultdict(list)
    for cid, sid, g, o in rows:
        by_class[cid].append((sid, _pct(float(g or 0), float(o or 0))))
    names = _student_names({sid for v in by_class.values() for sid, _ in v})
    from blueprints.school.report_card_data import grade_for
    out = {}
    for cid, items in by_class.items():
        items.sort(key=lambda x: x[1], reverse=not lowest)
        out[cid] = [{'name': names.get(sid, ''), 'percentage': pct, 'grade': grade_for(pct)[0], 'id': sid} for sid, pct in items[:limit]]
    return out


def _bars(rows, compare=None, unit='%', limit=None, href=None):
    """Rows for a horizontal bar chart: [{'label','value','compare','tip','href'}], largest first."""
    out = []
    for label, value, key in rows:
        if value is None:
            continue
        before = compare.get(key) if compare else None
        tip = f'{label}: {value:,.1f}{unit}' if unit == '%' else f'{label}: {unit}{value:,.0f}'
        if before is not None:
            tip += f' (was {before:,.1f}{unit})' if unit == '%' else f' (was {unit}{before:,.0f})'
        out.append({'label': label, 'value': value, 'compare': before, 'tip': tip,
                    'href': href(key) if href else None})
    out.sort(key=lambda r: r['value'], reverse=True)
    return out[:limit] if limit else out


# ------------------------------------------------------------------------------------------ panels

def _whole_school(me):
    return is_school_admin(me) or (_can(me, 'school.results.view') and admin_covers_whole_school(me['id'])
                                   and admin_rank(me['id']) <= LEVEL_RANK['head_teacher'])


def _academic(me, session, term, prev):
    """The whole school: for the proprietor and a head teacher only."""
    if not _whole_school(me):
        return None
    sid = session.id if session else None
    classes = _classes()
    ids = [c.id for c in classes]
    names = {c.id: c.name for c in classes}
    prev_sid, prev_term = (prev[0], prev[1]) if prev else (None, None)
    last_session = _previous_session_id(session)

    enrolled, enrolled_before = _enrolled(sid), _enrolled(last_session)
    students = sum(enrolled.values())
    score, score_before = _overall_score(sid, term), _overall_score(prev_sid, prev_term)
    att, att_before = _attendance(sid, term), _attendance(prev_sid, prev_term)
    counts = _statuses(sid, term)
    total = sum(counts.values())
    released = _count(counts, ('released',))
    ready = _count(counts, ('approved',))
    with_ct = {cid for (cid,) in tuples(select(TeachingDuty.class_id).where(TeachingDuty.subject_id.is_(None)).distinct())}
    work_now = one_scalar(select(func.count()).select_from(SchoolAssignment).where(
        SchoolAssignment.active == 1, SchoolAssignment.session_id == sid, SchoolAssignment.term == term), 0) if sid else 0
    work_before = one_scalar(select(func.count()).select_from(SchoolAssignment).where(
        SchoolAssignment.active == 1, SchoolAssignment.session_id == prev_sid, SchoolAssignment.term == prev_term), 0) if prev_sid else None

    kpis = [
        {'label': 'Students', 'value': f'{students:,}',
         'delta': delta(students, sum(enrolled_before.values()) if last_session else None, label='last session', digits=0)},
        {'label': 'Average score', 'value': f'{score:.1f}%' if score is not None else '—',
         'delta': delta(score, score_before, '%', label=prev[2] if prev else '')},
        {'label': 'Attendance', 'value': f'{att:.1f}%' if att is not None else '—',
         'delta': delta(att, att_before, '%', label=prev[2] if prev else '')},
        {'label': 'Marks released', 'value': f'{_pct(released, total):.0f}%' if total else '—',
         'note': f'{released:,} of {total:,}'},
        {'label': 'Ready to release', 'value': f'{ready:,}', 'alert': bool(ready), 'note': 'with class teachers'},
        {'label': 'Assignments set', 'value': f'{work_now:,}', 'delta': delta(work_now, work_before, label=prev[2] if prev else '', digits=0)},
        {'label': 'No class teacher', 'value': str(len([c for c in ids if c not in with_ct])),
         'alert': any(c not in with_ct for c in ids), 'href': url_for('admin_school_teaching') if (is_school_admin(me) or _can(me, 'school.staff.assign')) else None},
    ]

    class_scores = _scores(sid, term, 'class')
    class_scores_before = _scores(prev_sid, prev_term, 'class') if prev_sid else {}
    class_att = _attendance(sid, term, by_class=True)
    subject_names = {s.id: s.name for s in db.session.scalars(select(SchoolSubject).where(SchoolSubject.active == 1)).all()}
    subj = _scores(sid, term, 'subject')
    subj_before = _scores(prev_sid, prev_term, 'subject') if prev_sid else {}
    top = _top_students(sid, term, ids)

    rows = []
    for c in classes:
        pipe = _pipeline(counts, c.id)
        rows.append({'name': c.name, 'href': url_for('admin_school_results', **{'class': c.name}),
                     'students': enrolled.get(c.id, 0), 'students_before': enrolled_before.get(c.id),
                     'score': class_scores.get(c.id), 'score_delta': delta(class_scores.get(c.id), class_scores_before.get(c.id), '%'),
                     'attendance': class_att.get(c.id), 'pipeline': pipe, 'pipeline_total': sum(p['value'] for p in pipe),
                     'top': (top.get(c.id) or [None])[0], 'class_teacher': c.id in with_ct})

    sections = defaultdict(lambda: [0, 0])
    for c in classes:
        sections[_section(c)][0] += enrolled.get(c.id, 0)
        sections[_section(c)][1] += enrolled_before.get(c.id, 0)
    order = ['Crèche & Nursery', 'Primary', 'JSS', 'SSS']
    gender = Counter((g or '').strip().title() or 'Not recorded' for (g,) in tuples(
        select(Student.gender).join(StudentEnrolment, and_(StudentEnrolment.student_id == Student.id,
                                                           StudentEnrolment.session_id == sid, StudentEnrolment.active == 1))
        .where(Student.active == 1))) if sid else Counter()

    return {
        'kpis': kpis,
        'classes': rows,
        'class_scores': _bars([(names[c], v, c) for c, v in class_scores.items() if c in names], class_scores_before),
        'subjects': _bars([(subject_names.get(s, '?'), v, s) for s, v in subj.items()], subj_before),
        'attendance_days': _attendance_days(),
        'sections': [{'label': k, 'value': sections[k][0], 'compare': sections[k][1] if last_session else None,
                      'tip': f'{k}: {sections[k][0]} students' + (f' (last session {sections[k][1]})' if last_session else '')}
                     for k in order if sections[k][0] or sections[k][1]],
        'gender': _gender_segments(gender),
    }


def _gender_segments(counter):
    order = [('Male', 'series-1'), ('Female', 'series-2')]
    segs = [{'label': k, 'value': counter.get(k, 0), 'cls': cls} for k, cls in order]
    other = sum(v for k, v in counter.items() if k not in ('Male', 'Female'))
    if other:
        segs.append({'label': 'Not recorded', 'value': other, 'cls': 'other'})
    return [s for s in segs if s['value']]


def _my_classes(me, session, term, prev, duties):
    """Each class this person is class teacher of."""
    class_ids = list(dict.fromkeys(d['class_id'] for d in duties if d['subject_id'] is None))
    if not class_ids:
        return None
    sid = session.id if session else None
    prev_sid, prev_term = (prev[0], prev[1]) if prev else (None, None)
    names = {d['class_id']: d['class_name'] for d in duties}
    enrolled = _enrolled(sid, class_ids)
    counts = _statuses(sid, term, class_ids=class_ids)
    scores = _scores(sid, term, 'class', class_ids=class_ids)
    scores_before = _scores(prev_sid, prev_term, 'class', class_ids=class_ids) if prev_sid else {}
    school_score = _overall_score(sid, term)
    att = _attendance(sid, term, class_ids, by_class=True)
    att_before = _attendance(prev_sid, prev_term, class_ids, by_class=True) if prev_sid else {}
    pair_scores = _scores(sid, term, 'pair', class_ids=class_ids)
    school_subjects = _scores(sid, term, 'subject')
    subject_names = {s.id: s.name for s in db.session.scalars(select(SchoolSubject)).all()}
    best = _top_students(sid, term, class_ids, limit=3)
    weakest = _top_students(sid, term, class_ids, limit=3, lowest=True)
    today = datetime.now(timezone.utc).date().isoformat()
    marked = dict(tuples(select(AttendanceRecord.class_id, func.count())
                         .where(AttendanceRecord.class_id.in_(class_ids), AttendanceRecord.date == today)
                         .group_by(AttendanceRecord.class_id)))
    absences = Counter()
    if sid:
        for cid, stid, n in tuples(select(AttendanceRecord.class_id, AttendanceRecord.student_id, func.count())
                                   .where(AttendanceRecord.class_id.in_(class_ids), AttendanceRecord.session_id == sid,
                                          AttendanceRecord.term == term, AttendanceRecord.status == 'absent')
                                   .group_by(AttendanceRecord.class_id, AttendanceRecord.student_id)):
            absences[(cid, stid)] = n
    absent_names = _student_names({stid for _c, stid in absences})
    commented = {}
    if sid:
        commented = dict(tuples(
            select(StudentEnrolment.class_id, func.count())
            .join(ReportCardComment, and_(ReportCardComment.student_id == StudentEnrolment.student_id,
                                          ReportCardComment.session_id == sid, ReportCardComment.term == term))
            .where(StudentEnrolment.class_id.in_(class_ids), StudentEnrolment.session_id == sid, StudentEnrolment.active == 1)
            .group_by(StudentEnrolment.class_id)))
    out = []
    for cid in class_ids:
        n = enrolled.get(cid, 0)
        ready, waiting = _count(counts, ('approved',), cid), _count(counts, WAITING, cid)
        out.append({
            'id': cid, 'name': names[cid],
            'kpis': [
                {'label': 'Students', 'value': str(n)},
                {'label': "Today's register", 'value': 'Taken' if marked.get(cid) else 'Not yet', 'alert': not marked.get(cid),
                 'href': url_for('admin_school_attendance', class_id=cid) if _can(me, 'school.attendance.mark') else None},
                {'label': 'Attendance', 'value': f"{att[cid]:.1f}%" if att.get(cid) is not None else '—',
                 'delta': delta(att.get(cid), att_before.get(cid), '%', label=prev[2] if prev else '')},
                {'label': 'Class average', 'value': f'{scores[cid]:.1f}%' if scores.get(cid) is not None else '—',
                 'delta': delta(scores.get(cid), scores_before.get(cid), '%', label=prev[2] if prev else '')},
                {'label': 'vs school', 'value': f'{scores[cid] - school_score:+.1f} pts' if scores.get(cid) is not None and school_score is not None else '—',
                 'note': f'school {school_score:.1f}%' if school_score is not None else ''},
                {'label': 'Ready to release', 'value': str(ready), 'alert': bool(ready), 'note': f'{waiting} not sent yet'},
                {'label': 'Comments', 'value': f'{commented.get(cid, 0)}/{n}', 'alert': commented.get(cid, 0) < n},
            ],
            'subjects': _bars([(subject_names.get(s, '?'), v, s) for (c, s), v in pair_scores.items() if c == cid], school_subjects),
            'pipeline': _pipeline(counts, cid),
            'best': best.get(cid, []), 'weakest': weakest.get(cid, []),
            'absent': [{'name': absent_names.get(stid, ''), 'days': k} for (c, stid), k in absences.most_common() if c == cid][:3],
            'attendance_days': _attendance_days([cid]),
            'can_release': _can(me, 'school.results.release'), 'can_comment': _can(me, 'report_cards.comment'),
            'can_mark': _can(me, 'school.attendance.mark'),
        })
    return out


def _my_teaching(me, session, term, prev, duties):
    """Each subject this person teaches, in each class."""
    subject_duties = [d for d in duties if d['subject_id'] is not None]
    if not subject_duties:
        return None
    sid = session.id if session else None
    prev_sid, prev_term = (prev[0], prev[1]) if prev else (None, None)
    pairs = {(d['class_id'], d['subject_id']) for d in subject_duties}
    counts = _statuses(sid, term, pairs=pairs)
    scores = _scores(sid, term, 'pair', pairs=pairs)
    before = _scores(prev_sid, prev_term, 'pair', pairs=pairs) if prev_sid else {}
    school = _scores(sid, term, 'subject')
    enrolled = _enrolled(sid, {c for c, _s in pairs})
    # Students with at least one mark, and the spread of their averages, per class and subject.
    spread = defaultdict(list)
    if sid:
        for cid, subj, stid, g, o in tuples(
                select(StudentEnrolment.class_id, SchoolStudentResult.subject_id, SchoolStudentResult.student_id,
                       func.sum(SchoolStudentResult.score), func.sum(SchoolStudentResult.max_score))
                .select_from(SchoolStudentResult)
                .join(StudentEnrolment, and_(StudentEnrolment.student_id == SchoolStudentResult.student_id,
                                             StudentEnrolment.session_id == sid, StudentEnrolment.active == 1))
                .outerjoin(SchoolAssessment, SchoolAssessment.id == SchoolStudentResult.assessment_id)
                .where(SchoolStudentResult.session_id == sid, _term_of() == term, _official(),
                       SchoolStudentResult.score.is_not(None), SchoolStudentResult.max_score > 0,
                       StudentEnrolment.class_id.in_({c for c, _s in pairs}))
                .group_by(StudentEnrolment.class_id, SchoolStudentResult.subject_id, SchoolStudentResult.student_id)):
            if (cid, subj) in pairs:
                spread[(cid, subj)].append((stid, _pct(float(g or 0), float(o or 0))))
    rows = []
    for d in subject_duties:
        key = (d['class_id'], d['subject_id'])
        marks = [pct for _stid, pct in spread.get(key, [])]
        students = enrolled.get(d['class_id'], 0)
        rows.append({'class_name': d['class_name'], 'subject_name': d['subject_name'], 'department': d['department'],
                     'href': url_for('admin_school_results', **{'class': d['class_name']}),
                     'students': students, 'marked': len(marks),
                     'coverage': _pct(len(marks), students) or 0,
                     'pipeline': _pipeline(counts, *key),
                     'to_send': _count(counts, WAITING, *key),
                     'score': scores.get(key), 'score_delta': delta(scores.get(key), before.get(key), '%'),
                     'school': school.get(d['subject_id']),
                     'low': min(marks) if marks else None, 'high': max(marks) if marks else None})
    to_send = sum(r['to_send'] for r in rows)
    marked = sum(r['marked'] for r in rows)
    places = sum(r['students'] for r in rows)
    scored = [r['score'] for r in rows if r['score'] is not None]
    from blueprints.school.report_card_data import GRADE_SCALE, grade_for
    from models import AssignmentStudent
    every = [(key, stid, pct) for key, items in spread.items() for stid, pct in items]
    bands = Counter(grade_for(pct)[0] for _k, _s, pct in every)
    upper = 100
    grades = []
    for low, letter, remark in GRADE_SCALE:
        grades.append({'label': f'{letter} · {low}–{upper}', 'value': bands.get(letter, 0), 'compare': None,
                       'tip': f'{letter} ({remark}, {low}–{upper}%): {bands.get(letter, 0)} of {len(every)} student marks'})
        upper = low - 1
    labels = {(d['class_id'], d['subject_id']): f"{d['subject_name']} · {d['class_name']}" for d in subject_duties}
    weakest = sorted(every, key=lambda x: x[2])[:5]
    names = _student_names({stid for _k, stid, _p in weakest})
    to_grade = one_scalar(select(func.count()).select_from(AssignmentStudent)
                          .join(SchoolAssignment, SchoolAssignment.id == AssignmentStudent.assignment_id)
                          .where(SchoolAssignment.created_by == me['id'], SchoolAssignment.active == 1,
                                 AssignmentStudent.submitted_at.is_not(None), AssignmentStudent.graded_at.is_(None)), 0)
    set_now = one_scalar(select(func.count()).select_from(SchoolAssignment).where(
        SchoolAssignment.created_by == me['id'], SchoolAssignment.active == 1,
        SchoolAssignment.session_id == sid, SchoolAssignment.term == term), 0) if sid else 0
    set_before = one_scalar(select(func.count()).select_from(SchoolAssignment).where(
        SchoolAssignment.created_by == me['id'], SchoolAssignment.active == 1,
        SchoolAssignment.session_id == prev_sid, SchoolAssignment.term == prev_term), 0) if prev_sid else None
    kpis = [
        {'label': 'Classes & subjects', 'value': str(len(rows))},
        {'label': 'Marks to send', 'value': str(to_send), 'alert': bool(to_send), 'note': 'to class teachers'},
        {'label': 'Students marked', 'value': f'{_pct(marked, places):.0f}%' if places else '—', 'note': f'{marked} of {places}'},
        {'label': 'My average', 'value': f'{sum(scored) / len(scored):.1f}%' if scored else '—'},
        {'label': 'Passing (C or better)', 'value': f"{_pct(sum(1 for _k, _s, p in every if p >= 50), len(every)):.0f}%" if every else '—',
         'note': f'{len(every)} student marks'},
        {'label': 'Work to grade', 'value': str(to_grade), 'alert': bool(to_grade), 'note': 'submitted, not marked'},
        {'label': 'Assignments set', 'value': str(set_now), 'delta': delta(set_now, set_before, label=prev[2] if prev else '', digits=0)},
    ]
    return {'kpis': kpis, 'rows': rows, 'grades': grades,
            'vs_school': _bars([(labels[k], v, k) for k, v in scores.items()], {k: school.get(k[1]) for k in scores}),
            'support': [{'name': names.get(stid, ''), 'where': labels[k], 'percentage': pct} for k, stid, pct in weakest]}


def _method_group(method):
    m = (method or '').lower()
    if 'cash' in m:
        return 'Cash'
    if any(w in m for w in ('online', 'paystack', 'card', 'web')):
        return 'Online'
    return 'Bank, transfer & POS'


def _finance(me, session):
    """Money: collections with their comparisons, how they were paid, and (for the bursar) who owes what."""
    if not (_can(me, 'finance.view_own') or _can(me, 'finance.view_all')):
        return None
    from blueprints.finance.helpers import finance_snapshot
    from models import FinanceFeeAssessment, FinancePayment, FinancePaymentAllocation
    snap = finance_snapshot(me)
    own = not snap['view_all']
    scope = [FinancePayment.status == 'posted']
    if own:
        scope.append(FinancePayment.recorded_by == me['id'])
    day = func.substr(FinancePayment.paid_at, 1, 10)
    now = datetime.now()
    today = now.date()
    start = today - timedelta(days=29)
    daily = dict(tuples(select(day, func.sum(FinancePayment.amount)).where(*scope, day >= start.isoformat())
                        .group_by(day)))
    days = [start + timedelta(days=i) for i in range(30)]
    columns = [{'label': d.strftime('%d %b'), 'value': float(daily.get(d.isoformat(), 0) or 0),
                'tip': f"{d.strftime('%a %d %b')}: ₦{float(daily.get(d.isoformat(), 0) or 0):,.0f}"} for d in days]
    yesterday = float(daily.get((today - timedelta(days=1)).isoformat(), 0) or 0)
    # Month to date against the same days of last month.
    first = today.replace(day=1)
    last_month_end = first - timedelta(days=1)
    last_first = last_month_end.replace(day=1)
    last_same = last_first + timedelta(days=min(today.day, last_month_end.day) - 1)

    def total(*extra):
        return float(one_scalar(select(func.coalesce(func.sum(FinancePayment.amount), 0)).where(*scope, *extra), 0) or 0)
    month_before = total(day >= last_first.isoformat(), day <= last_same.isoformat())
    session_total = total(FinancePayment.session_id == session.id) if session else None
    methods = Counter()
    for m, amt in tuples(select(FinancePayment.method, func.sum(FinancePayment.amount))
                         .where(*scope, day >= first.isoformat()).group_by(FinancePayment.method)):
        methods[_method_group(m)] += float(amt or 0)
    method_order = [('Cash', 'series-1'), ('Bank, transfer & POS', 'series-2'), ('Online', 'series-3')]
    kpis = [
        {'label': 'Today', 'value': _naira(snap['today_total']), 'note': f"{snap['count_today']} payment{'s' if snap['count_today'] != 1 else ''}",
         'delta': delta(float(snap['today_total']), yesterday, '₦', label='yesterday')},
        {'label': 'Month to date', 'value': _naira(snap['month_total']),
         'delta': delta(float(snap['month_total']), month_before, '₦', label='same days last month')},
    ]
    if session_total is not None:
        kpis.append({'label': 'This session', 'value': _naira(session_total), 'note': session.name})
    # The last six months, oldest first, for a month-on-month comparison.
    month_key = func.substr(FinancePayment.paid_at, 1, 7)
    months = []
    m = first
    for _ in range(6):
        months.insert(0, m)
        m = (m - timedelta(days=1)).replace(day=1)
    by_month = dict(tuples(select(month_key, func.sum(FinancePayment.amount)).where(*scope, month_key >= months[0].strftime('%Y-%m'))
                           .group_by(month_key)))
    monthly = [{'label': mm.strftime('%b'), 'value': float(by_month.get(mm.strftime('%Y-%m'), 0) or 0),
                'tip': f"{mm.strftime('%B %Y')}: ₦{float(by_month.get(mm.strftime('%Y-%m'), 0) or 0):,.0f}" + (' (so far)' if mm == first else '')}
               for mm in months]
    out = {'own': own, 'kpis': kpis, 'columns': columns, 'column_max': max([c['value'] for c in columns] + [1]),
           'monthly': monthly, 'monthly_max': max([c['value'] for c in monthly] + [1]),
           'methods': [{'label': k, 'value': methods.get(k, 0), 'cls': cls} for k, cls in method_order if methods.get(k)],
           'can_record': _can(me, 'finance.record')}
    unalloc = snap['unallocated'] or {}
    if not own:
        assessed = float(one_scalar(select(func.coalesce(func.sum(FinanceFeeAssessment.amount), 0))
                                    .where(FinanceFeeAssessment.active == 1), 0) or 0)
        kpis.append({'label': 'Fees owed', 'value': _naira(snap['outstanding'] or 0), 'alert': (snap['outstanding'] or 0) > 0})
        kpis.append({'label': 'Collection rate', 'value': f"{_pct(assessed - (snap['outstanding'] or 0), assessed):.0f}%" if assessed else '—',
                     'note': f'of {_naira(assessed)} billed'})
        # What each student still owes: billed minus what has been allocated to their bills.
        billed = dict(tuples(select(FinanceFeeAssessment.student_id, func.sum(FinanceFeeAssessment.amount))
                             .where(FinanceFeeAssessment.active == 1).group_by(FinanceFeeAssessment.student_id)))
        paid = dict(tuples(select(FinanceFeeAssessment.student_id, func.sum(FinancePaymentAllocation.amount))
                           .select_from(FinancePaymentAllocation)
                           .join(FinancePayment, FinancePayment.id == FinancePaymentAllocation.payment_id)
                           .join(FinanceFeeAssessment, FinanceFeeAssessment.id == FinancePaymentAllocation.assessment_id)
                           .where(FinancePayment.status == 'posted', FinanceFeeAssessment.active == 1,
                                  FinancePaymentAllocation.voided_at.is_(None))
                           .group_by(FinanceFeeAssessment.student_id)))
        owing = {stid: float(b or 0) - float(paid.get(stid, 0) or 0) for stid, b in billed.items()}
        owing = {k: v for k, v in owing.items() if v > 0.5}
        class_of = dict(tuples(select(StudentEnrolment.student_id, SchoolClass.name)
                               .join(SchoolClass, SchoolClass.id == StudentEnrolment.class_id)
                               .where(StudentEnrolment.active == 1, StudentEnrolment.session_id == (session.id if session else 0),
                                      StudentEnrolment.student_id.in_(list(owing) or [0]))))
        by_class = Counter()
        for stid, amt in owing.items():
            by_class[class_of.get(stid, 'No current class')] += amt
        out['owed_by_class'] = _bars([(k, v, k) for k, v in by_class.items()], unit='₦', limit=8)
        # Collection rate by class: of what each class was billed, how much has been paid.
        class_all = dict(tuples(select(StudentEnrolment.student_id, SchoolClass.name)
                                .join(SchoolClass, SchoolClass.id == StudentEnrolment.class_id)
                                .where(StudentEnrolment.active == 1, StudentEnrolment.session_id == (session.id if session else 0))))
        billed_c, paid_c = Counter(), Counter()
        for stid, b in billed.items():
            name = class_all.get(stid)
            if name:
                billed_c[name] += float(b or 0)
                paid_c[name] += float(paid.get(stid, 0) or 0)
        out['rate_by_class'] = _bars([(k, _pct(paid_c[k], v), k) for k, v in billed_c.items() if v], unit='%')
        # Each fee item (category): billed, collected, still owed.
        cat_billed = dict(tuples(select(FinanceFeeAssessment.category, func.sum(FinanceFeeAssessment.amount))
                                 .where(FinanceFeeAssessment.active == 1).group_by(FinanceFeeAssessment.category)))
        cat_paid = dict(tuples(select(FinanceFeeAssessment.category, func.sum(FinancePaymentAllocation.amount))
                               .select_from(FinancePaymentAllocation)
                               .join(FinancePayment, FinancePayment.id == FinancePaymentAllocation.payment_id)
                               .join(FinanceFeeAssessment, FinanceFeeAssessment.id == FinancePaymentAllocation.assessment_id)
                               .where(FinancePayment.status == 'posted', FinanceFeeAssessment.active == 1,
                                      FinancePaymentAllocation.voided_at.is_(None))
                               .group_by(FinanceFeeAssessment.category)))
        out['by_item'] = sorted(({'name': k, 'billed': float(v or 0), 'paid': float(cat_paid.get(k, 0) or 0),
                                  'owed': max(0.0, float(v or 0) - float(cat_paid.get(k, 0) or 0)),
                                  'rate': _pct(float(cat_paid.get(k, 0) or 0), float(v or 0))}
                                 for k, v in cat_billed.items()), key=lambda r: r['owed'], reverse=True)
        names = _student_names(owing)
        out['debtors'] = [{'name': names.get(stid, ''), 'class': class_of.get(stid, '—'), 'amount': amt,
                           'href': url_for('admin_finance_student_account', student_id=stid)}
                          for stid, amt in sorted(owing.items(), key=lambda kv: kv[1], reverse=True)[:6]]
        out['owing_students'] = len(owing)
    if unalloc.get('count'):
        kpis.append({'label': 'Not allocated', 'value': str(unalloc.get('count')), 'alert': True,
                     'href': url_for('admin_finance_unallocated')})
    return out


def _naira(v):
    v = float(v or 0)
    if v >= 1_000_000:
        return f'₦{v / 1_000_000:,.1f}M'
    if v >= 10_000:
        return f'₦{v / 1_000:,.0f}K'
    return f'₦{v:,.0f}'


def _library(me):
    if not _can(me, 'library.view'):
        return None
    today = datetime.now(timezone.utc).date()
    month = today.strftime('%Y-%m')
    last_month = (today.replace(day=1) - timedelta(days=1)).strftime('%Y-%m')
    borrowed_day = func.substr(LibraryLoan.borrowed_at, 1, 10)
    open_loan = LibraryLoan.returned_at.is_(None)
    overdue_cond = and_(open_loan, LibraryLoan.due_at.is_not(None), LibraryLoan.due_at != '',
                        func.substr(LibraryLoan.due_at, 1, 10) < today.isoformat())
    def count(model, *where):
        return one_scalar(select(func.count()).select_from(model).where(*where), 0)
    this_month = count(LibraryLoan, func.substr(LibraryLoan.borrowed_at, 1, 7) == month)
    before = count(LibraryLoan, func.substr(LibraryLoan.borrowed_at, 1, 7) == last_month)
    copies = one_scalar(select(func.coalesce(func.sum(LibraryBook.total_copies), 0)).where(LibraryBook.active == 1), 0)
    on_loan = count(LibraryLoan, open_loan)
    kpis = [
        {'label': 'Titles', 'value': f"{count(LibraryBook, LibraryBook.active == 1):,}", 'note': f'{copies:,} copies'},
        {'label': 'On the shelf', 'value': f"{one_scalar(select(func.coalesce(func.sum(LibraryBook.available_copies), 0)).where(LibraryBook.active == 1), 0):,}"},
        {'label': 'On loan', 'value': f'{on_loan:,}', 'note': f'{_pct(on_loan, copies) or 0:.0f}% of copies'},
        {'label': 'Overdue', 'value': f'{count(LibraryLoan, overdue_cond):,}', 'alert': bool(count(LibraryLoan, overdue_cond))},
        {'label': 'Loans this month', 'value': f'{this_month:,}', 'delta': delta(this_month, before, label='last month', digits=0)},
    ]
    start = today - timedelta(weeks=7, days=today.weekday())
    weekly = Counter()
    for (d,) in tuples(select(borrowed_day).where(borrowed_day >= start.isoformat())):
        try:
            weekly[(date.fromisoformat(d) - start).days // 7] += 1
        except ValueError:
            pass
    columns = [{'label': (start + timedelta(weeks=i)).strftime('%d %b'), 'value': weekly.get(i, 0),
                'tip': f"Week of {(start + timedelta(weeks=i)).strftime('%d %b')}: {weekly.get(i, 0)} loans"} for i in range(8)]
    popular = tuples(select(LibraryBook.title, func.count()).join(LibraryLoan, LibraryLoan.book_id == LibraryBook.id)
                     .group_by(LibraryBook.title).order_by(func.count().desc()).limit(5))
    overdue = tuples(select(LibraryBook.title, LibraryLoan.member_type, LibraryLoan.member_id, LibraryLoan.due_at)
                     .join(LibraryBook, LibraryBook.id == LibraryLoan.book_id).where(overdue_cond)
                     .order_by(LibraryLoan.due_at).limit(5))
    students = _student_names({m for _t, kind, m, _d in overdue if kind == 'student'})
    staff = dict(tuples(select(Admin.id, Admin.display_name).where(Admin.id.in_([m for _t, kind, m, _d in overdue if kind != 'student'] or [0]))))
    return {'kpis': kpis, 'columns': columns, 'column_max': max([c['value'] for c in columns] + [1]),
            'popular': _bars([(t, float(n), t) for t, n in popular], unit=''),
            'overdue': [{'title': t, 'who': (students if kind == 'student' else staff).get(m, '—'),
                         'days': (today - date.fromisoformat(str(d)[:10])).days} for t, kind, m, d in overdue]}


def _store(me):
    """The school store (blueprints/store/routes.py), for the School Admin and anyone who may see the store: what is
    for sale and in stock, this month's sales against last month's, what waits to be collected, sales by week, the
    best sellers and what is running low. Six small grouped queries."""
    if not _can(me, 'store.view'):
        return None
    from models import StoreItem, StorePurchase
    today = datetime.now(timezone.utc).date()
    month = today.strftime('%Y-%m')
    last_month = (today.replace(day=1) - timedelta(days=1)).strftime('%Y-%m')
    sold = StorePurchase.status != 'cancelled'
    items = tuples(select(func.count(StoreItem.id), func.coalesce(func.sum(StoreItem.stock), 0),
                          func.count().filter(StoreItem.stock <= 0),
                          func.count().filter(StoreItem.stock > 0, StoreItem.stock <= 5))
                   .where(StoreItem.active == 1))[0]
    n_items, units, out, low = (int(v or 0) for v in items)
    in_month = func.substr(StorePurchase.created_at, 1, 7)
    sales = tuples(select(func.coalesce(func.sum(case((in_month == month, StorePurchase.amount), else_=0)), 0),
                          func.coalesce(func.sum(case((in_month == last_month, StorePurchase.amount), else_=0)), 0),
                          func.count().filter(in_month == month))
                   .where(sold))[0]
    this_month, before, n_month = float(sales[0] or 0), float(sales[1] or 0), int(sales[2] or 0)
    waiting = tuples(select(func.count(), func.count().filter(StorePurchase.short == 1))
                     .where(StorePurchase.status == 'paid'))[0]
    if not n_items and not this_month and not waiting[0] and not one_scalar(select(func.count()).select_from(StorePurchase), 0):
        return None   # a school that has not opened its store yet: nothing to show
    kpis = [
        {'label': 'For sale', 'value': f'{n_items:,}', 'note': f'{units:,} in stock'},
        {'label': 'Running low', 'value': f'{low:,}', 'note': '5 or fewer left', 'alert': bool(low)},
        {'label': 'Sold out', 'value': f'{out:,}', 'alert': bool(out)},
        {'label': 'To be collected', 'value': f'{int(waiting[0] or 0):,}',
         'note': f'{int(waiting[1] or 0)} paid but out of stock' if waiting[1] else 'paid, not handed over', 'alert': bool(waiting[1])},
        {'label': 'Sales this month', 'value': f'₦{this_month:,.0f}', 'note': f'{n_month} purchase{"" if n_month == 1 else "s"}',
         'delta': delta(this_month, before, unit='₦', label='last month', digits=0)},
    ]
    start = today - timedelta(weeks=7, days=today.weekday())
    weekly = Counter()
    for day, amount in tuples(select(func.substr(StorePurchase.created_at, 1, 10), StorePurchase.amount)
                              .where(sold, StorePurchase.created_at >= start.isoformat())):
        try:
            weekly[(date.fromisoformat(day) - start).days // 7] += float(amount or 0)
        except ValueError:
            pass
    columns = [{'label': (start + timedelta(weeks=i)).strftime('%d %b'), 'value': round(weekly.get(i, 0)),
                'tip': f"Week of {(start + timedelta(weeks=i)).strftime('%d %b')}: ₦{weekly.get(i, 0):,.0f}"} for i in range(8)]
    best = tuples(select(StorePurchase.item_name, func.sum(StorePurchase.quantity)).where(sold)
                  .group_by(StorePurchase.item_name).order_by(func.sum(StorePurchase.quantity).desc()).limit(5))
    running_low = tuples(select(StoreItem.name, StoreItem.stock).where(StoreItem.active == 1, StoreItem.stock <= 5)
                         .order_by(StoreItem.stock, StoreItem.name).limit(5))
    return {'kpis': kpis, 'columns': columns, 'column_max': max([c['value'] for c in columns] + [1]),
            'best': _bars([(name, float(n or 0), name) for name, n in best], unit=''),
            'low': [{'name': name, 'stock': stock} for name, stock in running_low]}


def _admissions(me):
    if not _can(me, 'candidates.admit'):
        return None
    from blueprints.entrance.admissions import _waitlist_rows
    rows = _waitlist_rows()
    status = Counter(r['status'] for r in rows)
    groups = Counter(r['entry_group_label'] for r in rows if r['status'] == 'pending')
    return {'kpis': [{'label': 'Waiting for a decision', 'value': str(status.get('pending', 0)), 'alert': bool(status.get('pending'))},
                     {'label': 'Admitted', 'value': str(status.get('admitted', 0))},
                     {'label': 'Declined', 'value': str(status.get('declined', 0))},
                     {'label': 'Admission rate', 'value': f"{_pct(status.get('admitted', 0), status.get('admitted', 0) + status.get('declined', 0)) or 0:.0f}%"
                      if status.get('admitted', 0) + status.get('declined', 0) else '—', 'note': 'of decisions made'}],
            'groups': _bars([(g or 'Other', float(n), g) for g, n in groups.items()], unit='')}


def _parents(me):
    if not _can(me, 'parent.feedback.view'):
        return None
    def count(*where):
        return one_scalar(select(func.count()).select_from(ParentFeedback).where(*where), 0)
    today = datetime.now(timezone.utc).date()
    created = func.substr(ParentFeedback.created_at, 1, 10)
    week = count(created > (today - timedelta(days=7)).isoformat())
    week_before = count(created > (today - timedelta(days=14)).isoformat(), created <= (today - timedelta(days=7)).isoformat())
    daily = dict(tuples(select(created, func.count()).where(created > (today - timedelta(days=14)).isoformat()).group_by(created)))
    days = [today - timedelta(days=13 - i) for i in range(14)]
    columns = [{'label': d.strftime('%d %b'), 'value': daily.get(d.isoformat(), 0),
                'tip': f"{d.strftime('%a %d %b')}: {daily.get(d.isoformat(), 0)} new"} for d in days]
    return {'kpis': [{'label': 'New, unanswered', 'value': str(count(ParentFeedback.status == 'open', ParentFeedback.assigned_admin_id.is_(None))),
                      'alert': bool(count(ParentFeedback.status == 'open', ParentFeedback.assigned_admin_id.is_(None)))},
                     {'label': 'Open', 'value': str(count(ParentFeedback.status == 'open'))},
                     {'label': 'In progress', 'value': str(count(ParentFeedback.status == 'in_progress'))},
                     {'label': 'New this week', 'value': str(week), 'delta': delta(week, week_before, good_up=False, label='the week before', digits=0)}],
            'columns': columns, 'column_max': max([c['value'] for c in columns] + [1])}


def _records(me, session):
    """For the secretary and records officers."""
    if not (_can(me, 'school.students.create') and not teaching_duties(me['id']) and not _whole_school(me)):
        return None
    month = datetime.now(timezone.utc).strftime('%Y-%m')
    last_month = (datetime.now(timezone.utc).date().replace(day=1) - timedelta(days=1)).strftime('%Y-%m')
    sid = session.id if session else None
    classes = _classes()
    senior = [c.id for c in classes if is_senior(c.stage, c.name)]
    enrolled = _enrolled(sid)
    before = _enrolled(_previous_session_id(session))
    def count(*where):
        return one_scalar(select(func.count()).select_from(Student).where(Student.active == 1, *where), 0)
    no_department = one_scalar(
        select(func.count(func.distinct(Student.id))).select_from(Student)
        .join(StudentEnrolment, and_(StudentEnrolment.student_id == Student.id, StudentEnrolment.active == 1,
                                     StudentEnrolment.session_id == sid))
        .where(Student.active == 1, StudentEnrolment.class_id.in_(senior or [0]),
               or_(Student.department.is_(None), Student.department == '')), 0) if sid else 0
    linked = select(ParentStudentLink.student_id).where(ParentStudentLink.active == 1)
    this_month = count(func.substr(Student.created_at, 1, 7) == month)
    kpis = [{'label': 'Students', 'value': f'{sum(enrolled.values()):,}', 'delta': delta(sum(enrolled.values()), sum(before.values()) if before else None, label='last session', digits=0)},
            {'label': 'Registered this month', 'value': str(this_month), 'delta': delta(this_month, count(func.substr(Student.created_at, 1, 7) == last_month), label='last month', digits=0)},
            {'label': 'SSS without department', 'value': str(no_department), 'alert': bool(no_department)},
            {'label': 'No guardian phone', 'value': str(count(or_(Student.guardian_phone.is_(None), Student.guardian_phone == ''))),
             'alert': bool(count(or_(Student.guardian_phone.is_(None), Student.guardian_phone == '')))}]
    if _can(me, 'parent.view'):
        n = count(Student.id.not_in(linked))
        kpis.append({'label': 'No parent account', 'value': str(n), 'alert': bool(n)})
    gender = Counter((g or '').strip().title() or 'Not recorded' for (g,) in tuples(select(Student.gender).where(Student.active == 1)))
    return {'kpis': kpis,
            'by_class': [{'label': c.name, 'value': enrolled.get(c.id, 0), 'compare': before.get(c.id) if before else None,
                          'tip': f'{c.name}: {enrolled.get(c.id, 0)}' + (f' (last session {before.get(c.id, 0)})' if before else '')}
                         for c in classes if enrolled.get(c.id) or before.get(c.id)],
            'gender': _gender_segments(gender)}


# Every page the Overview may point to: (label, endpoint, the permission that opens it, url arguments).
SHORTCUTS = [
    ('Students', 'admin_school_students', 'school.students.view', {}),
    ('Register a student', 'admin_school_student_new', 'school.students.create', {}),
    ('Take the register', 'admin_school_attendance', 'school.attendance.mark', {}),
    ('Results & Records', 'admin_school_results', 'school.results.view', {}),
    ('Enter offline scores', 'admin_school_result_manual_new', 'school.results.enter', {}),
    ('Report cards', 'admin_school_report_cards', 'report_cards.view', {}),
    ('Assignments', 'admin_school_assignments', 'school.assignments.view', {}),
    ('Tests', 'admin_school_tests', 'school.tests.view', {}),
    ('Exam timetable', 'admin_school_timetable', 'school.timetable.view', {}),
    ('Teachers', 'admin_school_teaching', 'school.staff.assign', {}),
    ('Record a payment', 'admin_finance_record', 'finance.record', {}),
    ('Finance', 'admin_finance_dashboard', 'finance.view_own', {}),
    ('Fee structure', 'admin_finance_fee_items', 'finance.manage', {}),
    ('Library', 'admin_library', 'library.view', {}),
    ('Admissions', 'admin_candidate_admissions', 'candidates.admit', {}),
    ('Parents', 'admin_school_parents', 'parent.view', {}),
    ('Messages', 'admin_messages', 'admin.access', {}),
]


def _shortcuts(me):
    return [{'label': label, 'url': url_for(endpoint, **kwargs)} for label, endpoint, code, kwargs in SHORTCUTS
            if _can(me, code)]


def build_dashboard(me):
    """Everything the Overview shows for this person, and nothing it does not."""
    session = _school_current_session()
    term = dashboard_term(session.id if session else None)
    prev = previous_period(session, term)
    duties = list(teaching_duties(me['id']))
    if is_school_admin(me):
        role_label = TOP_LEVEL_LABEL
    else:
        rank = admin_rank(me['id'])
        role_label = next((LEVEL_LABELS[c] for c, r in LEVEL_RANK.items() if r == rank), '')
    panels = {
        'academic': _academic(me, session, term, prev),
        'my_classes': _my_classes(me, session, term, prev, duties),
        'my_teaching': _my_teaching(me, session, term, prev, duties),
        'finance': _finance(me, session),
        'library': _library(me),
        'store': _store(me),
        'admissions': _admissions(me),
        'parents': _parents(me),
        'records': _records(me, session),
    }
    academic_panels = any(panels[k] for k in ('academic', 'my_classes', 'my_teaching'))
    return {'panels': panels, 'shortcuts': _shortcuts(me), 'term': term, 'terms': ACADEMIC_TERMS,
            'previous_label': prev[2] if prev else '', 'session_name': session.name if session else '',
            'role_label': role_label, 'show_term_picker': academic_panels,
            'has_panels': any(v for v in panels.values())}
