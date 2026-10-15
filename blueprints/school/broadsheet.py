"""The broadsheet: a class's master score sheet. One row per student, one column per subject, then the total, the average,
the position and the grade; under it each subject's average, highest and lowest.

For a term it shows each subject's term result (or its CA and Exam, side by side), worked out exactly as the report card
works it out (blueprints/school/helpers._term_reports_bulk, the level's grading scheme). For the session it shows each
subject's average over the terms the student has results in, as the Third Term card's "session so far" does.

Unlike a report card it is for checking before release too: it counts every mark recorded, released or not, and says
for each student how many of their results are still waiting to be released.

Who: the class teacher's (report_cards.view, and the class within their teaching duties; the class teacher of a
subdivision sees that subdivision), as the report cards themselves. It prints in landscape and downloads as CSV, every
cell defused against spreadsheet formulas.
"""

import csv
import io
import re

from flask import Response, render_template, request
from sqlalchemy import select

from app import ACADEMIC_TERMS, app
from blueprints.entrance.helpers import csv_safe
from blueprints.school.helpers import _term_reports_bulk
from blueprints.school.report_card_data import _class_students, _official_result_counts
from blueprints.school.report_cards import _choice
from core import grading
from core.db_helpers import tuples
from core.security import admin_required, audit_log
from models import SchoolSubject, db

PERIODS = (*ACADEMIC_TERMS, 'Session')


def _rank(values):
    """``{key: position}`` by value, highest first, equal values sharing a place (1, 2, 2, 4)."""
    places, last, place = {}, None, 0
    for i, (value, key) in enumerate(sorted(((v, k) for k, v in values.items() if v is not None), reverse=True), 1):
        if value != last:
            place, last = i, value
        places[key] = place
    return places


def sheet(class_id, session_id, period, detail=False):
    """Everything the broadsheet shows, for a class in a term (or the whole session)."""
    students = db.session.execute(_class_students(class_id, session_id)).all()
    ids = [s.id for s in students]
    scheme = grading.scheme_for(session_id, grading.class_levels([class_id]).get(class_id))
    terms = ACADEMIC_TERMS if period == 'Session' else [period]
    per_term = {t: _term_reports_bulk(ids, session_id, t) if ids else {} for t in terms}
    waiting = {}
    if ids:
        for (sid, _s, t), c in _official_result_counts(ids, session_id).items():
            if t in terms:
                waiting[sid] = waiting.get(sid, 0) + c['pending']
    subject_ids = sorted({subj for reports in per_term.values() for (_sid, subj), r in reports.items() if r['has_data']})
    names = dict(tuples(select(SchoolSubject.id, SchoolSubject.name).where(SchoolSubject.id.in_(subject_ids or [0]))))
    subject_ids.sort(key=lambda s: names.get(s, '').casefold())
    rows = []
    for s in students:
        cells, scored, available = {}, 0.0, 0.0
        for subj in subject_ids:
            found = [per_term[t].get((s.id, subj)) for t in terms]
            found = [r for r in found if r and r['has_data']]
            if not found:
                continue
            if period == 'Session':
                pcts = [r['total_score'] / r['total_max'] * 100 for r in found if r['total_max']]
                value = round(sum(pcts) / len(pcts), 1) if pcts else None
                cells[subj] = {'total': value}
                if value is not None:
                    scored += value; available += 100
            else:
                r = found[0]
                cells[subj] = {'total': r['total_score'], 'ca': r['ca_score'], 'exam': r['exam_score']}
                scored += r['total_score']; available += r['total_max']
        average = round(scored / available * 100, 2) if available else None
        rows.append({'id': s.id, 'name': ' '.join(p for p in (s.last_name, s.first_name, s.middle_name) if p),
                     'number': s.student_number or s.admission_no or '', 'cells': cells,
                     'total': round(scored, 1) if available else None, 'average': average,
                     'grade': grading.grade(average, scheme)[0] if average is not None else '',
                     'waiting': waiting.get(s.id, 0)})
    places = _rank({r['id']: r['average'] for r in rows})
    for r in rows:
        r['position'] = places.get(r['id'])
    rows.sort(key=lambda r: (r['position'] or 10 ** 6, r['name'].casefold()))
    footer = []
    for subj in subject_ids:
        values = [r['cells'][subj]['total'] for r in rows if subj in r['cells'] and r['cells'][subj]['total'] is not None]
        footer.append({'id': subj, 'average': round(sum(values) / len(values), 1) if values else None,
                       'highest': max(values) if values else None, 'lowest': min(values) if values else None})
    averages = [r['average'] for r in rows if r['average'] is not None]
    return {'subjects': [{'id': s, 'name': names.get(s, '—')} for s in subject_ids], 'rows': rows, 'footer': footer,
            'class_average': round(sum(averages) / len(averages), 2) if averages else None,
            'waiting': sum(r['waiting'] for r in rows), 'detail': detail and period != 'Session', 'scheme': scheme}


def _csv(data, class_name, session_name, period):
    out = io.StringIO()
    w = csv.writer(out)
    subjects = data['subjects']
    head = ['Position', 'Student', 'Number']
    for s in subjects:
        head += [f"{s['name']} CA", f"{s['name']} Exam", f"{s['name']} Total"] if data['detail'] else [s['name']]
    w.writerow([csv_safe(h) for h in head + ['Total', 'Average %', 'Grade', 'Results waiting']])
    for r in data['rows']:
        line = [r['position'] or '', r['name'], r['number']]
        for s in subjects:
            c = r['cells'].get(s['id']) or {}
            line += [c.get('ca', ''), c.get('exam', ''), c.get('total', '')] if data['detail'] else [c.get('total', '')]
        line += [r['total'] if r['total'] is not None else '', r['average'] if r['average'] is not None else '', r['grade'],
                 r['waiting']]
        w.writerow([csv_safe('' if v is None else v) for v in line])
    stem = re.sub(r'[^A-Za-z0-9._-]+', '-', f'Broadsheet-{class_name}-{period}-{session_name}').strip('-')[:120]
    response = Response(out.getvalue(), mimetype='text/csv')
    response.headers['Content-Disposition'] = f'attachment; filename="{stem}.csv"'
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/admin/school/report-cards/broadsheet')
@admin_required
def admin_school_broadsheet():
    classes, sessions, class_row, session_row, _term = _choice()
    period = request.args.get('period', ACADEMIC_TERMS[0])
    period = period if period in PERIODS else ACADEMIC_TERMS[0]
    detail = request.args.get('detail') == '1'
    data = sheet(class_row.id, session_row.id, period, detail) if class_row and session_row else None
    if data is not None and request.args.get('format') == 'csv':
        audit_log('broadsheet_downloaded', 'school', 'class', class_row.id, {'session_id': session_row.id, 'period': period})
        return _csv(data, class_row.name, session_row.name, period)
    return render_template('school_broadsheet.html', classes=classes, sessions=sessions, class_row=class_row,
                           session_row=session_row, period=period, periods=PERIODS, detail=detail, data=data)
