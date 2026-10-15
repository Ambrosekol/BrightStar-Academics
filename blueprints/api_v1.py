"""The school's read-only API, version 1, on the school's own address: ``https://<school>/api/v1/...``.

Every call needs ``Authorization: Bearer <key>`` (core/api_keys.py): a key the School Admin created, limited to the
areas it was given. Nothing here writes. Lists are paged by id: ``?limit=`` (up to 200, 50 by default) and
``?after=<the last id you have>``; each answer carries ``next_after`` until there is no more. Filters are listed per
endpoint in the documentation (``/api/v1/docs``) and the machine-readable description (``/api/v1/openapi.json``),
both open to anyone, since they hold nothing of the school's.

Only what a partner needs: never health details, religion, passwords, staff pay or messages; only released results;
a child whose data was erased appears under the placeholder name with ``erased: true``.
"""

from datetime import date, timedelta
from functools import wraps

from flask import g, jsonify, render_template, request, url_for
from sqlalchemy import and_, select

from app import app
from control_plane.ratelimit import allow
from core import api_keys, webhooks
from core.db_helpers import tuples
from models import (
    AcademicSession, AttendanceRecord, ClassSubject, FinanceFeeAssessment, FinancePayment, ParentAccount,
    ParentStudentLink, SchoolAssessment, SchoolClass, SchoolStudentResult, SchoolSubject, Student, StudentEnrolment, db,
)

MAX_LIMIT, DEFAULT_LIMIT = 200, 50
MAX_ATTENDANCE_DAYS = 93


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _error(status, code, message):
    response = jsonify(error={'code': code, 'message': message})
    response.status_code = status
    response.headers['Cache-Control'] = 'no-store'
    return response


def api_endpoint(scope):
    """Check the key and its scope, the rate, then run; every answer is JSON."""
    def decorate(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if g.get('tenant') is None:
                return _error(404, 'not_found', 'This address has no school.')
            key, why = api_keys.authenticate(request.headers.get('Authorization'))
            if key is None:
                response = _error(401, 'unauthorized', why)
                response.headers['WWW-Authenticate'] = 'Bearer'
                return response
            if scope not in api_keys.scopes_of(key):
                return _error(403, 'forbidden', f'This key may not read {scope}. The School Admin can give it that '
                                                f'area under Settings, API & webhooks.')
            if not allow(f'api:{g.tenant.id}:{key.id}', api_keys.RATE_PER_MINUTE, 60):
                response = _error(429, 'rate_limited', f'At most {api_keys.RATE_PER_MINUTE} calls a minute per key.')
                response.headers['Retry-After'] = '60'
                return response
            api_keys.touch(key, request.remote_addr)
            try:
                response = jsonify(fn(*args, **kwargs))
            except ApiError as exc:
                return _error(exc.status, exc.code, exc.message)
            response.headers['Cache-Control'] = 'no-store'
            return response
        return wrapper
    return decorate


def _int(name, required=False):
    raw = request.args.get(name, '').strip()
    if not raw:
        if required:
            raise ApiError(400, 'bad_request', f'{name} is required.')
        return None
    if not raw.isdigit() or len(raw) > 12:
        raise ApiError(400, 'bad_request', f'{name} must be a whole number.')
    return int(raw)


def _page(stmt, id_column):
    limit = _int('limit') or DEFAULT_LIMIT
    if limit < 1 or limit > MAX_LIMIT:
        raise ApiError(400, 'bad_request', f'limit must be from 1 to {MAX_LIMIT}.')
    after = _int('after')
    if after is not None:
        stmt = stmt.where(id_column > after)
    return stmt.order_by(id_column).limit(limit + 1), limit


def _listed(rows, limit, shape):
    more = len(rows) > limit
    rows = rows[:limit]
    return {'data': [shape(r) for r in rows], 'next_after': (rows[-1][0] if more and rows else None)}


def _current_session_id():
    return db.session.scalar(select(AcademicSession.id).where(AcademicSession.is_current == 1).limit(1))


def _date(name):
    raw = request.args.get(name, '').strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        raise ApiError(400, 'bad_request', f'{name} must be a date like 2026-09-14.') from None


# ---------------------------------------------------------------- students
_STUDENT = (Student.id, Student.admission_no, Student.student_number, Student.first_name, Student.middle_name,
            Student.last_name, Student.gender, Student.date_of_birth, Student.department, Student.active,
            Student.archived_at, Student.erased_at, Student.created_at)


def _student_shape(classes):
    def shape(r):
        sid = r[0]
        cls = classes.get(sid)
        return {'id': sid, 'admission_no': r[1], 'student_number': r[2], 'first_name': r[3], 'middle_name': r[4],
                'last_name': r[5], 'gender': r[6], 'date_of_birth': r[7], 'department': r[8], 'active': bool(r[9]),
                'archived_at': r[10], 'erased': bool(r[11]), 'created_at': r[12],
                'class': {'id': cls[0], 'name': cls[1]} if cls else None}
    return shape


def _classes_of(student_ids, session_id):
    if not student_ids or not session_id:
        return {}
    return {sid: (cid, name) for sid, cid, name in tuples(
        select(StudentEnrolment.student_id, SchoolClass.id, SchoolClass.name)
        .join(SchoolClass, SchoolClass.id == StudentEnrolment.class_id)
        .where(StudentEnrolment.student_id.in_(student_ids), StudentEnrolment.session_id == session_id,
               StudentEnrolment.active == 1))}


@app.route('/api/v1/students')
@api_endpoint('students')
def api_students():
    stmt = select(*_STUDENT)
    status = request.args.get('status', 'active')
    if status == 'active':
        stmt = stmt.where(Student.active == 1)
    elif status == 'archived':
        stmt = stmt.where(Student.archived_at.is_not(None))
    elif status != 'all':
        raise ApiError(400, 'bad_request', 'status must be active, archived or all.')
    class_id = _int('class_id')
    session_id = _current_session_id()
    if class_id:
        stmt = stmt.join(StudentEnrolment, and_(StudentEnrolment.student_id == Student.id,
                                                StudentEnrolment.class_id == class_id,
                                                StudentEnrolment.session_id == session_id, StudentEnrolment.active == 1))
    stmt, limit = _page(stmt, Student.id)
    rows = tuples(stmt)
    return _listed(rows, limit, _student_shape(_classes_of([r[0] for r in rows[:limit]], session_id)))


@app.route('/api/v1/students/<int:student_id>')
@api_endpoint('students')
def api_student(student_id):
    rows = tuples(select(*_STUDENT).where(Student.id == student_id))
    if not rows:
        raise ApiError(404, 'not_found', 'No such student.')
    return {'data': _student_shape(_classes_of([student_id], _current_session_id()))(rows[0])}


# ---------------------------------------------------------------- parents
@app.route('/api/v1/parents')
@api_endpoint('parents')
def api_parents():
    stmt, limit = _page(select(ParentAccount.id, ParentAccount.display_name, ParentAccount.email, ParentAccount.phone,
                               ParentAccount.active, ParentAccount.created_at), ParentAccount.id)
    rows = tuples(stmt)
    ids = [r[0] for r in rows[:limit]]
    children = {}
    for pid, sid, relationship in tuples(select(ParentStudentLink.parent_id, ParentStudentLink.student_id,
                                                ParentStudentLink.relationship)
                                         .where(ParentStudentLink.parent_id.in_(ids or [0]), ParentStudentLink.active == 1)):
        children.setdefault(pid, []).append({'student_id': sid, 'relationship': relationship})
    return _listed(rows, limit, lambda r: {'id': r[0], 'name': r[1], 'email': r[2], 'phone': r[3], 'active': bool(r[4]),
                                           'created_at': r[5], 'children': children.get(r[0], [])})


# ---------------------------------------------------------------- classes
@app.route('/api/v1/classes')
@api_endpoint('classes')
def api_classes():
    stmt, limit = _page(select(SchoolClass.id, SchoolClass.name, SchoolClass.stage, SchoolClass.level_order,
                               SchoolClass.active), SchoolClass.id)
    rows = tuples(stmt)
    subjects = {}
    for cid, sid, name in tuples(select(ClassSubject.class_id, SchoolSubject.id, SchoolSubject.name)
                                 .join(SchoolSubject, SchoolSubject.id == ClassSubject.subject_id)
                                 .where(ClassSubject.class_id.in_([r[0] for r in rows[:limit]] or [0]))
                                 .order_by(SchoolSubject.name)):
        subjects.setdefault(cid, []).append({'id': sid, 'name': name})
    return _listed(rows, limit, lambda r: {'id': r[0], 'name': r[1], 'stage': r[2], 'order': r[3], 'active': bool(r[4]),
                                           'subjects': subjects.get(r[0], [])})


# ---------------------------------------------------------------- attendance
@app.route('/api/v1/attendance')
@api_endpoint('attendance')
def api_attendance():
    start, end = _date('from'), _date('to')
    end = end or date.today()
    start = start or end - timedelta(days=30)
    if start > end or (end - start).days > MAX_ATTENDANCE_DAYS:
        raise ApiError(400, 'bad_request', f'from must be before to, at most {MAX_ATTENDANCE_DAYS} days apart.')
    stmt = select(AttendanceRecord.id, AttendanceRecord.student_id, AttendanceRecord.class_id, AttendanceRecord.date,
                  AttendanceRecord.status, AttendanceRecord.session_id, AttendanceRecord.term).where(
        AttendanceRecord.date >= start.isoformat(), AttendanceRecord.date <= end.isoformat())
    for name, column in (('class_id', AttendanceRecord.class_id), ('student_id', AttendanceRecord.student_id)):
        value = _int(name)
        if value:
            stmt = stmt.where(column == value)
    stmt, limit = _page(stmt, AttendanceRecord.id)
    return _listed(tuples(stmt), limit, lambda r: {'id': r[0], 'student_id': r[1], 'class_id': r[2], 'date': r[3],
                                                   'status': r[4], 'session_id': r[5], 'term': r[6]})


# ---------------------------------------------------------------- results
@app.route('/api/v1/results')
@api_endpoint('results')
def api_results():
    from sqlalchemy import func
    part = func.coalesce(SchoolStudentResult.component_name, SchoolAssessment.title)
    stmt = (select(SchoolStudentResult.id, SchoolStudentResult.student_id, SchoolStudentResult.subject_id,
                   SchoolSubject.name, SchoolStudentResult.session_id, SchoolStudentResult.term, part,
                   SchoolStudentResult.score, SchoolStudentResult.max_score, SchoolStudentResult.released_at)
            .join(SchoolSubject, SchoolSubject.id == SchoolStudentResult.subject_id)
            .outerjoin(SchoolAssessment, SchoolAssessment.id == SchoolStudentResult.assessment_id)
            .where(SchoolStudentResult.status == 'released'))
    for name, column in (('session_id', SchoolStudentResult.session_id), ('student_id', SchoolStudentResult.student_id),
                         ('subject_id', SchoolStudentResult.subject_id)):
        value = _int(name)
        if value:
            stmt = stmt.where(column == value)
    term = request.args.get('term', '').strip()
    if term:
        stmt = stmt.where(SchoolStudentResult.term == term[:20])
    stmt, limit = _page(stmt, SchoolStudentResult.id)
    return _listed(tuples(stmt), limit, lambda r: {
        'id': r[0], 'student_id': r[1], 'subject': {'id': r[2], 'name': r[3]}, 'session_id': r[4], 'term': r[5],
        'part': r[6], 'score': r[7], 'max_score': r[8], 'released_at': r[9]})


# ---------------------------------------------------------------- fees and payments
@app.route('/api/v1/fees')
@api_endpoint('fees')
def api_fees():
    stmt = select(FinanceFeeAssessment.id, FinanceFeeAssessment.student_id, FinanceFeeAssessment.session_id,
                  FinanceFeeAssessment.term, FinanceFeeAssessment.category, FinanceFeeAssessment.gross_amount,
                  FinanceFeeAssessment.discount_amount, FinanceFeeAssessment.amount, FinanceFeeAssessment.due_date,
                  FinanceFeeAssessment.created_at).where(FinanceFeeAssessment.voided_at.is_(None))
    for name, column in (('session_id', FinanceFeeAssessment.session_id), ('student_id', FinanceFeeAssessment.student_id)):
        value = _int(name)
        if value:
            stmt = stmt.where(column == value)
    stmt, limit = _page(stmt, FinanceFeeAssessment.id)
    return _listed(tuples(stmt), limit, lambda r: {
        'id': r[0], 'student_id': r[1], 'session_id': r[2], 'term': r[3], 'category': r[4],
        'full_amount': r[5] if r[5] is not None else r[7], 'discount': r[6] or 0, 'amount': r[7], 'due_date': r[8],
        'created_at': r[9], 'currency': 'NGN'})


@app.route('/api/v1/payments')
@api_endpoint('payments')
def api_payments():
    stmt = select(FinancePayment.id, FinancePayment.receipt_no, FinancePayment.student_id, FinancePayment.session_id,
                  FinancePayment.amount, FinancePayment.category, FinancePayment.method, FinancePayment.reference,
                  FinancePayment.paid_at, FinancePayment.status, FinancePayment.voided_at)
    for name, column in (('session_id', FinancePayment.session_id), ('student_id', FinancePayment.student_id)):
        value = _int(name)
        if value:
            stmt = stmt.where(column == value)
    since = _date('paid_from')
    if since:
        stmt = stmt.where(FinancePayment.paid_at >= since.isoformat())
    stmt, limit = _page(stmt, FinancePayment.id)
    return _listed(tuples(stmt), limit, lambda r: {
        'id': r[0], 'receipt_no': r[1], 'student_id': r[2], 'session_id': r[3], 'amount': r[4], 'category': r[5],
        'method': r[6], 'reference': r[7], 'paid_at': r[8], 'status': r[9], 'voided_at': r[10], 'currency': 'NGN'})


# ---------------------------------------------------------------- the description, for people and for tools
ENDPOINTS = [
    ('/students', 'students', 'Students (active by default)',
     [('status', 'active, archived or all'), ('class_id', "only this class's students, this session")]),
    ('/students/{id}', 'students', 'One student', []),
    ('/parents', 'parents', 'Parents, with the children each is linked to', []),
    ('/classes', 'classes', 'Classes, with their subjects', []),
    ('/attendance', 'attendance', 'Attendance marks, one per student per day',
     [('from', 'first day, 2026-09-14 (30 days before to by default)'), ('to', 'last day (today by default)'),
      ('class_id', 'one class'), ('student_id', 'one student')]),
    ('/results', 'results', 'Released results: each part (Test, CA1, Exam...) of each subject',
     [('session_id', 'one session'), ('term', 'First Term, Second Term or Third Term'), ('student_id', 'one student'),
      ('subject_id', 'one subject')]),
    ('/fees', 'fees', 'Fees charged', [('session_id', 'one session'), ('student_id', 'one student')]),
    ('/payments', 'payments', 'Payments, including voided ones (status says)',
     [('session_id', 'one session'), ('student_id', 'one student'), ('paid_from', 'paid on or after this date')]),
]


def openapi(base):
    paths = {}
    for path, scope, summary, params in ENDPOINTS:
        parameters = [{'name': n, 'in': 'query', 'required': False, 'description': d, 'schema': {'type': 'string'}}
                      for n, d in params]
        if '{id}' in path:
            parameters.append({'name': 'id', 'in': 'path', 'required': True, 'schema': {'type': 'integer'}})
        else:
            parameters += [{'name': 'limit', 'in': 'query', 'schema': {'type': 'integer', 'maximum': MAX_LIMIT},
                            'description': f'how many (1 to {MAX_LIMIT}, {DEFAULT_LIMIT} by default)'},
                           {'name': 'after', 'in': 'query', 'schema': {'type': 'integer'},
                            'description': 'the next_after of the previous page'}]
        paths[path] = {'get': {'summary': summary, 'description': f'Needs a key with the "{scope}" area.',
                               'parameters': parameters, 'security': [{'bearer': []}],
                               'responses': {'200': {'description': 'OK'}, '401': {'description': 'No valid key'},
                                             '403': {'description': 'The key may not read this'},
                                             '429': {'description': 'Too many calls'}}}}
    return {'openapi': '3.0.3', 'info': {'title': 'Brightstars Academics school API', 'version': '1',
                                         'description': 'Read-only. Each school has its own address and its own keys.'},
            'servers': [{'url': base}], 'paths': paths,
            'components': {'securitySchemes': {'bearer': {'type': 'http', 'scheme': 'bearer'}}}}


@app.route('/api/v1/<path:anything>')
def api_unknown(anything):
    """Anything else under the API answers in the API's own way, not with a web page."""
    return _error(404, 'not_found', 'There is no such endpoint. See /api/v1/docs.')


@app.route('/api/v1/openapi.json')
def api_openapi():
    response = jsonify(openapi(request.host_url.rstrip('/') + '/api/v1'))
    response.headers['Cache-Control'] = 'public, max-age=300'
    return response


@app.route('/api/v1/docs')
def api_docs():
    return render_template('api_docs.html', base=request.host_url.rstrip('/') + '/api/v1', endpoints=ENDPOINTS,
                           scopes=api_keys.SCOPES, events=webhooks.EVENTS, max_limit=MAX_LIMIT,
                           default_limit=DEFAULT_LIMIT, rate=api_keys.RATE_PER_MINUTE,
                           school=g.get('tenant').name if g.get('tenant') else None)
