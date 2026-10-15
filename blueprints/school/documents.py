"""School documents: issuing testimonials, transcripts and leaving certificates, and the register of every one issued.

Issuing one works out what it says from the student's own record (their name, date of birth, state of origin, their
classes and sessions from their enrolments, when they joined and left) and what the person issuing it adds (conduct,
the head's words on their character, positions held, the reason for leaving). A transcript adds the student's results:
one row per subject, one column per session, each the average of that session's terms whose report cards are ready
(released results only), graded by that session's grading scheme (core/grading.py).

Everything a document says is kept with it (``details_json``) when it is issued, with a serial number of its own, so a
copy printed later says exactly what the first did; it prints from the school's current design of that kind (the
documents studio). A document is never deleted: one issued by mistake is revoked, with a reason, and prints marked
REVOKED from then on.

Who: documents.issue (the head teacher, the academic administrator and the records officers; the School Admin always).
Archived students (leavers, graduates) can be given one: they are who most often need it.
"""

import json
import re
from datetime import date, datetime, timezone

from flask import Response, abort, flash, redirect, render_template, request, url_for
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app import app
from blueprints.school.report_card_data import _date_text, _is_ready, _official_result_counts, report_settings
from core import document_design as dd
from core import grading, theme
from core.branding import receipt_prefix, school_brand
from core.db_helpers import tuples
from core.idempotency import idempotent_write
from core.security import admin_required, audit_log, csrf_protect, current_admin
from models import (
    AcademicSession, Admin, IssuedDocument, SchoolClass, SchoolSubject, Student, StudentEnrolment, db,
)

CODES = {'testimonial': 'TST', 'transcript': 'TRN', 'leaving': 'LVC'}
CONDUCT = ('Excellent', 'Very good', 'Good', 'Fair')
REASONS = ('Completed studies', 'Transferred to another school', 'Family relocated', 'Withdrawn by parent', 'Other')
LIMITS = {'character': 600, 'positions_held': 200, 'reason_for_leaving': 120, 'conduct': 40}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _name(s):
    return ' '.join(p for p in (s.first_name, s.middle_name, s.last_name) if p and str(p).strip())


def _history(student_id):
    """``[(session id, session name, class name, enrolled_at)]`` of the student's classes, oldest first."""
    return [tuple(r) for r in tuples(
        select(AcademicSession.id, AcademicSession.name, SchoolClass.name, StudentEnrolment.enrolled_at)
        .join(AcademicSession, AcademicSession.id == StudentEnrolment.session_id)
        .join(SchoolClass, SchoolClass.id == StudentEnrolment.class_id)
        .where(StudentEnrolment.student_id == student_id, StudentEnrolment.active == 1)
        .order_by(AcademicSession.id))]


def transcript_for(student_id):
    """The transcript table: ``{'sessions': [{'key', 'name', 'class'}], 'rows': [{'subject', s0.., 'average', 'grade'}],
    'average'}``. Only terms whose report card is ready count (released results). Five queries for a whole school career:
    the ready terms, the marks (three, every period at once), the classes."""
    from app import _release_due_school_results  # deferred: app imports this module
    from blueprints.school.helpers import _assemble_term_report, _term_raw_sums
    _release_due_school_results()               # a release date that has passed counts, as on the student's own cards
    ready ={(sess, term) for (_sid, sess, term), c in _official_result_counts([student_id]).items() if _is_ready(c)}
    if not ready:
        return {'sessions': [], 'rows': [], 'average': ''}
    raw = _term_raw_sums([student_id], None, None, by_period=True)
    history = {sid: cls for sid, _n, cls, _a in _history(student_id)}
    class_ids = dict(tuples(select(StudentEnrolment.session_id, StudentEnrolment.class_id).where(
        StudentEnrolment.student_id == student_id, StudentEnrolment.active == 1)))
    levels = grading.class_levels(class_ids.values())
    marks = {}                                   # (session, subject) -> [percentages of each ready term]
    for (_sid, subject_id, session_id, term), sums in raw.items():
        if (session_id, term) not in ready:
            continue
        scheme = grading.scheme_for(session_id, levels.get(class_ids.get(session_id)))
        report = _assemble_term_report(scheme, sums)
        if report['has_data'] and report['total_max']:
            marks.setdefault((session_id, subject_id), []).append(report['total_score'] / report['total_max'] * 100)
    session_ids = sorted({s for s, _ in marks})
    names = dict(tuples(select(AcademicSession.id, AcademicSession.name).where(AcademicSession.id.in_(session_ids or [0]))))
    subjects = dict(tuples(select(SchoolSubject.id, SchoolSubject.name).where(
        SchoolSubject.id.in_({subj for _, subj in marks} or {0}))))
    keys = {sid: f's{i}' for i, sid in enumerate(session_ids)}
    last = session_ids[-1] if session_ids else None
    scheme = grading.scheme_for(last, levels.get(class_ids.get(last))) if last else grading.legacy()
    rows, every = [], []
    for subject_id in sorted(subjects, key=lambda s: subjects[s].casefold()):
        row, got = {'subject': subjects[subject_id]}, []
        for sid in session_ids:
            vals = marks.get((sid, subject_id))
            if vals:
                avg = round(sum(vals) / len(vals), 1)
                row[keys[sid]] = f'{avg:g}'
                got.append(avg)
            else:
                row[keys[sid]] = '-'
        overall = round(sum(got) / len(got), 1) if got else None
        row['average'] = f'{overall:g}' if overall is not None else ''
        row['grade'] = grading.grade(overall, scheme)[0] if overall is not None else ''
        if overall is not None:
            every.append(overall)
        rows.append(row)
    average = round(sum(every) / len(every), 1) if every else None
    return {'sessions': [{'key': keys[s], 'name': names.get(s, ''), 'class': history.get(s, '')} for s in session_ids],
            'rows': rows, 'average': f'{average:g}' if average is not None else '',
            'grade': grading.grade(average, scheme)[0] if average is not None else ''}


def details_for(kind, student, extra):
    """Everything a document says, from the student's record and what the person issuing it added."""
    history = _history(student.id)
    brand = school_brand()
    settings = report_settings()
    gender = (student.gender or '').strip().lower()
    pronoun, possessive = ('She', 'Her') if gender.startswith('f') else (('He', 'His') if gender.startswith('m') else ('They', 'Their'))
    joined = extra.get('date_admitted') or (history[0][3] if history and history[0][3] else student.created_at)
    left = extra.get('date_left') or student.archived_at or date.today().isoformat()
    doc = {
        'document_title': dd.KINDS[kind].upper(), 'date_issued': _date_text(date.today().isoformat()),
        'student_name': _name(student), 'admission_no': student.student_number or student.admission_no or '',
        'gender': student.gender or '', 'pronoun': pronoun, 'possessive': possessive,
        'date_of_birth': _date_text(student.date_of_birth) if student.date_of_birth else '',
        'state_of_origin': student.state_of_origin or '', 'religion': student.religion or '',
        'guardian_name': student.guardian_name or '', 'department': student.department or '',
        'first_class': history[0][2] if history else '', 'last_class': history[-1][2] if history else '',
        'sessions_attended': (f'{history[0][1]} to {history[-1][1]}' if len(history) > 1 else (history[0][1] if history else '')),
        'date_admitted': _date_text(joined), 'date_left': _date_text(left),
        'reason_for_leaving': extra.get('reason_for_leaving', ''), 'conduct': extra.get('conduct', ''),
        'character': extra.get('character', ''), 'positions_held': extra.get('positions_held', '') or 'None',
        'head_name': settings['head_name'], 'head_title': settings['head_title'] or 'Head of School',
        'school_name': brand.get('name') or '', 'school_motto': brand.get('motto') or '',
        'school_address': brand.get('address') or '', 'school_phone': brand.get('phone') or '',
        'school_email': brand.get('email') or '',
    }
    if kind == 'transcript':
        doc['transcript'] = transcript_for(student.id)
        doc['final_average'] = (doc['transcript']['average'] + '%') if doc['transcript']['average'] else ''
        doc['final_grade'] = doc['transcript'].get('grade', '')
    return doc


def _next_serial(kind):
    """The next serial number of a kind this year: BSA/TST/2026/00001."""
    year = date.today().year
    prefix = f'{receipt_prefix()}/{CODES[kind]}/{year}/'
    last = db.session.scalar(select(IssuedDocument.serial_no).where(IssuedDocument.serial_no.startswith(prefix, autoescape=True))
                             .order_by(IssuedDocument.id.desc()).limit(1))
    try:
        n = int(str(last).rsplit('/', 1)[1]) + 1 if last else 1
    except (IndexError, ValueError):
        n = 1
    return prefix, n


def _students():
    """Every student, current and archived, for the issue form."""
    return [{'id': sid, 'label': f'{last} {first} · {adm or ""}' + (' · left' if archived else '')}
            for sid, first, last, adm, archived in tuples(
                select(Student.id, Student.first_name, Student.last_name, Student.admission_no, Student.archived_at)
                .order_by(Student.last_name, Student.first_name))]


def _pictures(student_id):
    from blueprints.finance.helpers import _school_logo_stored
    brand = school_brand()
    settings = report_settings()
    return {'primary': brand.get('primary') or theme.DEFAULT_PRIMARY, 'accent': brand.get('accent') or theme.DEFAULT_ACCENT,
            'logo_path': _school_logo_stored(), 'signature_path': settings['head_signature'] or None,
            'photo_path': db.session.scalar(select(Student.photo_path).where(Student.id == student_id))}


# ---------------------------------------------------------------- the register
@app.route('/admin/school/documents')
@admin_required
def admin_school_documents():
    kind = request.args.get('kind', '')
    kind = kind if kind in dd.KINDS else ''
    q = request.args.get('q', '').strip()
    stmt = (select(IssuedDocument, Student.first_name, Student.middle_name, Student.last_name, Student.admission_no, Admin.display_name)
            .join(Student, Student.id == IssuedDocument.student_id)
            .outerjoin(Admin, Admin.id == IssuedDocument.issued_by)
            .order_by(IssuedDocument.id.desc()).limit(500))
    if kind:
        stmt = stmt.where(IssuedDocument.kind == kind)
    if q:
        like = f'%{q.replace("%", "").replace("_", "")[:60]}%'
        stmt = stmt.where((Student.first_name.ilike(like)) | (Student.last_name.ilike(like)) | (Student.admission_no.ilike(like))
                          | (IssuedDocument.serial_no.ilike(like)))
    rows = [{'doc': d, 'name': ' '.join(p for p in (f, m, l) if p), 'admission_no': adm, 'by': by or ''}
            for d, f, m, l, adm, by in db.session.execute(stmt).all()]
    counts = dict(tuples(select(IssuedDocument.kind, func.count()).group_by(IssuedDocument.kind)))
    return render_template('school_documents.html', rows=rows, kind=kind, kinds=dd.KINDS, q=q, counts=counts)


@app.route('/admin/school/documents/issue', methods=['GET', 'POST'])
@admin_required
@csrf_protect
@idempotent_write('school.issue_document')
def admin_school_document_issue():
    me = current_admin()
    form = request.form if request.method == 'POST' else {}
    errors = []
    kind = (form.get('kind') or request.args.get('kind') or 'testimonial')
    kind = kind if kind in dd.KINDS else 'testimonial'
    if request.method == 'POST':
        student = db.session.get(Student, request.form.get('student_id', type=int) or 0)
        if not student:
            errors.append('Choose the student.')
        extra = {k: ' '.join(str(form.get(k, '')).split())[:LIMITS.get(k, 80)] for k in ('conduct', 'character', 'positions_held')}
        reason = form.get('reason_for_leaving', '')
        extra['reason_for_leaving'] = (' '.join(form.get('reason_other', '').split())[:120] if reason == 'Other'
                                       else reason if reason in REASONS else '')
        for key in ('date_admitted', 'date_left'):
            raw = form.get(key, '').strip()
            if raw:
                try:
                    extra[key] = date.fromisoformat(raw).isoformat()
                except ValueError:
                    errors.append('Enter the dates as they appear in the date boxes.')
        if kind in ('testimonial', 'leaving') and not extra['conduct']:
            errors.append('Say what the student\'s conduct was.')
        if kind == 'leaving' and not extra['reason_for_leaving']:
            errors.append('Give the reason for leaving.')
        if kind == 'testimonial' and not extra['character']:
            errors.append('Write a sentence or two about the student\'s character.')
        if not errors:
            doc = details_for(kind, student, extra)
            if kind == 'transcript' and not doc['transcript']['rows']:
                errors.append('This student has no released results yet, so there is nothing to put on a transcript.')
        if not errors:
            prefix, n = _next_serial(kind)
            for _ in range(5):          # two people issuing at once: the unique serial number decides, then the next
                serial = f'{prefix}{n:05d}'
                doc['serial_no'] = serial
                row = IssuedDocument(kind=kind, serial_no=serial, student_id=student.id, details_json=json.dumps(doc),
                                     issued_by=me['id'], issued_at=_now())
                db.session.add(row)
                try:
                    db.session.flush()
                    break
                except IntegrityError:
                    db.session.rollback()
                    n += 1
            else:
                errors.append('The document could not be numbered. Please try again.')
        if not errors:
            audit_log('document_issued', 'school', 'issued_document', row.id,
                      {'kind': kind, 'serial_no': serial, 'student_id': student.id}, commit=False)
            db.session.commit()
            flash(f'{dd.KINDS[kind]} {serial} issued to {doc["student_name"]}.', 'success')
            return redirect(url_for('admin_school_documents', kind=kind, opened=row.id))
    return render_template('school_document_issue.html', kind=kind, kinds=dd.KINDS, form=form, errors=errors,
                           students=_students(), conduct=CONDUCT, reasons=REASONS,
                           chosen=form.get('student_id') or request.args.get('student_id', ''))


@app.route('/admin/school/documents/<int:doc_id>/pdf')
@admin_required
def admin_school_document_pdf(doc_id):
    row = db.session.get(IssuedDocument, doc_id)
    if row is None:
        abort(404)
    from blueprints.school.document_studio import layout_for
    from core.document_design_pdf import render_document
    from models import IdCardAsset
    layout = layout_for(row.kind)
    used = dd.assets_used(layout)
    assets = dict(tuples(select(IdCardAsset.id, IdCardAsset.file_path).where(IdCardAsset.id.in_(list(used))))) if used else {}
    try:
        doc = json.loads(row.details_json)
    except ValueError:
        abort(500)
    doc.update(_pictures(row.student_id))
    pdf = render_document(doc, layout, assets, revoked=bool(row.revoked_at))
    stem = re.sub(r'[^A-Za-z0-9._-]+', '-', f"{dd.KINDS[row.kind]}-{doc.get('student_name', '')}-{row.serial_no}").strip('-')[:120]
    response = Response(pdf, mimetype='application/pdf')
    response.headers['Content-Disposition'] = f'inline; filename="{stem}.pdf"'
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.post('/admin/school/documents/<int:doc_id>/revoke')
@admin_required
@csrf_protect
def admin_school_document_revoke(doc_id):
    me = current_admin()
    row = db.session.get(IssuedDocument, doc_id)
    if row is None:
        abort(404)
    reason = ' '.join(request.form.get('reason', '').split())[:300]
    if row.revoked_at:
        flash('That document is already revoked.', 'error')
    elif not reason:
        flash('Give the reason for revoking it.', 'error')
    else:
        row.revoked_at, row.revoked_by, row.revoke_reason = _now(), me['id'], reason
        audit_log('document_revoked', 'school', 'issued_document', row.id, {'serial_no': row.serial_no, 'reason': reason},
                  commit=False)
        db.session.commit()
        flash(f'{row.serial_no} revoked. It now prints marked REVOKED.', 'success')
    return redirect(url_for('admin_school_documents', kind=row.kind))
