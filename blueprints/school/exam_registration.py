"""Exam registration: the candidates' details for WAEC and NECO (senior secondary) and BECE (junior secondary),
checked and ready to carry into the examination body's own registration portal.

For a class, each candidate's name (surname first), sex, date of birth, state of origin, admission number, department
and the subjects they take (the class's subjects, those of their department in SSS) are listed, with anything missing
pointed out, so it can be put right on the student's record before registration. Then:

* **Candidates (CSV)**: one row per candidate, every cell defused against spreadsheet formulas;
* **Passport photographs (ZIP)**: each candidate's photograph as a JPEG of a standard size, named after the row it
  belongs to, so the two files go together.

The exam bodies' own portals change their formats from year to year; this gives the details they all ask for, clean
and checked, rather than imitating one portal's file.

Sharing a child's details with an exam body follows the family's consent (core/privacy.py, 'exam_sharing'), which is
given unless a parent has withdrawn it: a candidate whose family said no is listed with that missing and left out of
both downloads. A parent allows it again in their portal, or the school records a signed form here, for several
candidates at once.

Who: school.exam_registration (the head teacher, the academic administrator and the records officers; the School Admin
always). It is personal data: every download is in the activity log.
"""

import csv
import io
import re
import zipfile
from datetime import date, datetime

from flask import Response, flash, redirect, render_template, request, url_for
from sqlalchemy import and_, select

from app import _active_sessions, _school_current_session, app
from blueprints.entrance.helpers import csv_safe
from core import grading
from core.db_helpers import tuples
from core.school_structure import takes_subject
from core import privacy
from core.security import admin_required, audit_log, csrf_protect, current_admin
from core.storage import read_upload_bytes
from models import ClassSubject, SchoolClass, SchoolSubject, Student, StudentEnrolment, db

BODIES = {'waec': ('WAEC (WASSCE)', 'sss'), 'neco': ('NECO (SSCE)', 'sss'), 'bece': ('BECE (Junior WAEC)', 'jss')}
PHOTO_SIZE = (300, 400)            # a passport photograph, portrait
MIN_AGE, MAX_AGE = 9, 30


def _dob(text):
    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%Y/%m/%d', '%d.%m.%Y'):
        try:
            return datetime.strptime(str(text or '').strip()[:10], fmt).date()
        except ValueError:
            continue
    return None


def _classes(level):
    rows = tuples(select(SchoolClass.id, SchoolClass.name, SchoolClass.stage).where(SchoolClass.active == 1)
                  .order_by(SchoolClass.level_order))
    return [(cid, name) for cid, name, stage in rows if grading.level_of(stage, name) == level]


def candidates(class_id, session_id):
    """Each candidate of a class in a session, with their subjects and what is missing. Three queries."""
    students = db.session.execute(
        select(Student).join(StudentEnrolment, and_(StudentEnrolment.student_id == Student.id,
                                                    StudentEnrolment.class_id == class_id,
                                                    StudentEnrolment.session_id == session_id, StudentEnrolment.active == 1))
        .where(Student.active == 1).order_by(Student.last_name, Student.first_name)).scalars().all()
    subjects = tuples(select(SchoolSubject.name, SchoolSubject.departments)
                      .join(ClassSubject, ClassSubject.subject_id == SchoolSubject.id)
                      .where(ClassSubject.class_id == class_id, SchoolSubject.active == 1).order_by(SchoolSubject.name))
    today = date.today()
    consented = privacy.allowed([s.id for s in students], 'exam_sharing')
    out = []
    for i, s in enumerate(students, 1):
        born = _dob(s.date_of_birth)
        age = today.year - born.year - ((today.month, today.day) < (born.month, born.day)) if born else None
        taken = [name for name, depts in subjects if takes_subject(s.department, depts)]
        sex = (s.gender or '').strip()[:1].upper()
        missing = []
        if sex not in ('M', 'F'):
            missing.append('sex')
        if not born:
            missing.append('date of birth')
        elif not MIN_AGE <= age <= MAX_AGE:
            missing.append(f'date of birth (age {age}?)')
        if not (s.state_of_origin or '').strip():
            missing.append('state of origin')
        if not s.photo_path:
            missing.append('photograph')
        if not taken:
            missing.append('subjects')
        if s.id not in consented:
            missing.append('consent to share')
        out.append({'n': i, 'id': s.id, 'surname': (s.last_name or '').strip(), 'first': (s.first_name or '').strip(),
                    'middle': (s.middle_name or '').strip(), 'sex': sex if sex in ('M', 'F') else '',
                    'dob': born.strftime('%d/%m/%Y') if born else '', 'state': (s.state_of_origin or '').strip(),
                    'number': s.student_number or s.admission_no or '', 'department': s.department or '',
                    'subjects': taken, 'photo': s.photo_path, 'missing': missing, 'consent': s.id in consented})
    return out


def _photo_name(c):
    stem = re.sub(r'[^A-Za-z0-9]+', '-', f"{c['n']:03d}-{c['surname']}-{c['number']}").strip('-')
    return f'{stem[:60]}.jpg'


def _csv(rows, body, class_name, session_name):
    out = io.StringIO()
    w = csv.writer(out)
    most = max((len(c['subjects']) for c in rows), default=0)
    w.writerow(['S/N', 'Surname', 'First name', 'Middle name', 'Sex', 'Date of birth', 'State of origin', 'Admission no',
                'Department', 'Number of subjects'] + [f'Subject {i}' for i in range(1, most + 1)] + ['Photograph file'])
    for c in rows:
        line = [c['n'], c['surname'], c['first'], c['middle'], c['sex'], c['dob'], c['state'], c['number'], c['department'],
                len(c['subjects'])] + c['subjects'] + [''] * (most - len(c['subjects'])) + [_photo_name(c) if c['photo'] else '']
        w.writerow([csv_safe(v) for v in line])
    return _download(out.getvalue().encode('utf-8-sig'), 'text/csv', f'{body}-candidates-{class_name}-{session_name}.csv')


def _photos(rows, body, class_name, session_name):
    from PIL import Image, ImageOps
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as z:
        for c in rows:
            raw = read_upload_bytes(c['photo']) if c['photo'] else None
            if not raw:
                continue
            try:
                with Image.open(io.BytesIO(raw)) as img:
                    img = ImageOps.exif_transpose(img).convert('RGB')
                    img = ImageOps.fit(img, PHOTO_SIZE, method=Image.LANCZOS, centering=(0.5, 0.35))
                    out = io.BytesIO()
                    img.save(out, 'JPEG', quality=85, optimize=True)
                z.writestr(_photo_name(c), out.getvalue())
            except Exception:
                app.logger.exception('A candidate photograph could not be converted (student %s)', c['id'])
    return _download(buffer.getvalue(), 'application/zip', f'{body}-photographs-{class_name}-{session_name}.zip')


def _download(data, mimetype, name):
    name = re.sub(r'[^A-Za-z0-9._-]+', '-', name).strip('-')[:120]
    response = Response(data, mimetype=mimetype)
    response.headers['Content-Disposition'] = f'attachment; filename="{name}"'
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/admin/school/exam-registration')
@admin_required
def admin_school_exam_registration():
    body = request.args.get('body', 'waec')
    body = body if body in BODIES else 'waec'
    classes = _classes(BODIES[body][1])
    sessions = _active_sessions()
    current = _school_current_session()
    session_id = request.args.get('session_id', type=int) or (current['id'] if current else None)
    session_row = next((s for s in sessions if s.id == session_id), None)
    wanted = request.args.get('class_id', type=int)
    class_row = next((c for c in classes if c[0] == wanted), None) or (classes[-1] if classes else None)
    rows = candidates(class_row[0], session_row.id) if class_row and session_row else []
    fmt = request.args.get('format')
    shared = [c for c in rows if c['consent']]
    if rows and fmt in ('csv', 'photos'):
        if not shared:
            flash('No candidate in this class has consent to share their details yet, so there is nothing to download.',
                  'error')
            return redirect(url_for('admin_school_exam_registration', body=body, class_id=class_row[0],
                                    session_id=session_row.id))
        audit_log('exam_registration_downloaded', 'school', 'class', class_row[0],
                  {'body': body, 'what': fmt, 'session_id': session_row.id, 'candidates': len(shared),
                   'left_out_without_consent': len(rows) - len(shared)})
        args = (shared, body, class_row[1], session_row.name)
        return _csv(*args) if fmt == 'csv' else _photos(*args)
    return render_template('school_exam_registration.html', bodies=BODIES, body=body, classes=classes, class_row=class_row,
                           sessions=sessions, session_row=session_row, rows=rows,
                           ready=sum(1 for c in rows if not c['missing']), without_consent=[c for c in rows if not c['consent']])


@app.post('/admin/school/exam-registration/consent')
@admin_required
@csrf_protect
def admin_school_exam_registration_consent():
    """Record signed consent forms for several candidates at once: the forms are kept by the school."""
    me = current_admin()
    note = ' '.join(request.form.get('note', '').split())[:200]
    ids = [int(v) for v in request.form.getlist('student_id') if v.isdigit()][:500]
    back = redirect(request.form.get('back') if (request.form.get('back') or '').startswith('/admin/school/exam-registration')
                    else url_for('admin_school_exam_registration'))
    if not note:
        flash('Say what the school holds, for example "Signed forms collected 14 October".', 'error')
        return back
    known = {sid for (sid,) in tuples(select(Student.id).where(Student.id.in_(ids or [0]), Student.active == 1))}
    recorded = 0
    for sid in sorted(known):
        if privacy.record('exam_sharing', True, student_id=sid, source='staff', admin_id=me['id'], note=note,
                          ip=request.remote_addr):
            recorded += 1
    audit_log('exam_consent_recorded', 'privacy', 'school', None, {'students': sorted(known), 'note': note}, commit=False)
    db.session.commit()
    flash(f'Consent to share recorded for {recorded} candidate{"" if recorded == 1 else "s"}.', 'success')
    return back
