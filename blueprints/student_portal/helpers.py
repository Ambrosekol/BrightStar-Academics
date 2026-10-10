"""Private helpers used only by the student-portal routes in this package."""

from datetime import datetime, timezone
from functools import wraps

import sqlalchemy as sa
from flask import redirect, request, session, url_for
from sqlalchemy import select, update as sa_update

from app import app, _student_with_enrolment
from models import (
    AcademicSession, SchoolAssessment, SchoolAssessmentAnswer,
    SchoolAssessmentAttempt, SchoolAssessmentAttemptQuestion, SchoolClass,
    SchoolQuestion, SchoolStudentResult, SchoolSubject, Student, db,
)
from core.accounts import _clear_identity_sessions
from core.db_helpers import all_rows, obj, one, one_scalar, tuples, _flatten
from core.school_structure import class_is_senior, student_is_early_years, takes_subject


def student_required(fn):
    @wraps(fn)
    def wrapper(*args,**kwargs):
        sid=session.get('student_id')
        if not sid:
            return redirect(url_for('login',next=request.path))
        row=one(select(Student.id,Student.active,Student.account_active,
                       Student.password_must_change).where(Student.id==sid))
        if not row or not row['active'] or not row['account_active'] or student_is_early_years(sid):
            _clear_identity_sessions(); return redirect(url_for('login'))
        if row['password_must_change'] and request.endpoint != 'student_password_change':
            return redirect(url_for('student_password_change'))
        return fn(*args,**kwargs)
    return wrapper

def _student_assessment_context(assessment_id, sid):
    student=_student_with_enrolment(sid)
    a=_flatten(one(select(SchoolAssessment,SchoolClass.name.label('class_name'),
                          SchoolSubject.name.label('subject_name'),
                          AcademicSession.name.label('session_name'))
        .join(SchoolClass,SchoolClass.id==SchoolAssessment.class_id)
        .join(SchoolSubject,SchoolSubject.id==SchoolAssessment.subject_id)
        .outerjoin(AcademicSession,AcademicSession.id==SchoolAssessment.session_id)
        .where(SchoolAssessment.id==assessment_id,SchoolAssessment.active==1,
               SchoolAssessment.question_count>0)),
        'SchoolAssessment','class_name','subject_name','session_name')
    if not student or not a or not student['class_id'] or a['class_id']!=student['class_id']:
        return None,None,[]
    if a['session_id'] is not None and a['session_id']!=student['session_id']:
        return None,None,[]
    # In SSS, a test in a subject the student's department does not take is not theirs.
    if class_is_senior(student['class_id']) and not takes_subject(
            student.get('department'),one_scalar(select(SchoolSubject.departments).where(SchoolSubject.id==a['subject_id']))):
        return None,None,[]
    questions=db.session.scalars(select(SchoolQuestion)
        .where(SchoolQuestion.assessment_id==assessment_id)
        .order_by(SchoolQuestion.sort_order,SchoolQuestion.id)).all()
    return student,a,questions

def _student_practice_context(assessment_id, sid):
    """A practice test a student may take, or ``(None, None, [])``.

    Practice is a self-study bank, so unlike a test or examination it is open to the student's
    class in every session, and nothing about taking it is ever written to the database.
    """
    student=_student_with_enrolment(sid)
    if not student or not student['class_id']:
        return None,None,[]
    a=_flatten(one(select(SchoolAssessment,SchoolClass.name.label('class_name'),
                          SchoolSubject.name.label('subject_name'))
        .join(SchoolClass,SchoolClass.id==SchoolAssessment.class_id)
        .join(SchoolSubject,SchoolSubject.id==SchoolAssessment.subject_id)
        .where(SchoolAssessment.id==assessment_id,SchoolAssessment.assessment_type=='practice',
               SchoolAssessment.active==1,SchoolAssessment.question_count>0,
               SchoolAssessment.class_id==student['class_id'])),
        'SchoolAssessment','class_name','subject_name')
    if not a:
        return None,None,[]
    questions=db.session.scalars(select(SchoolQuestion)
        .where(SchoolQuestion.assessment_id==assessment_id)
        .order_by(SchoolQuestion.sort_order,SchoolQuestion.id)).all()
    return student,a,questions

def student_assessment_grade(attempt_id,auto=False):
    """Grade a frozen student assessment attempt safely and idempotently."""
    try:
        attempt=obj(SchoolAssessmentAttempt,attempt_id)
        if not attempt:
            return None
        if attempt.status in ('submitted','expired'):
            return attempt

        # Grade from the frozen snapshot, never from the live question bank.
        questions=all_rows(
            select(SchoolAssessmentAttemptQuestion.question_id,
                   SchoolAssessmentAttemptQuestion.correct_option,
                   SchoolAssessmentAttemptQuestion.points)
            .where(SchoolAssessmentAttemptQuestion.attempt_id==attempt_id)
            .order_by(SchoolAssessmentAttemptQuestion.question_order))
        answers={qid:opt for qid,opt in tuples(
            select(SchoolAssessmentAnswer.question_id,SchoolAssessmentAnswer.option_index)
            .where(SchoolAssessmentAnswer.attempt_id==attempt_id))}

        score=sum(int(q['points'] or 1) for q in questions
                  if answers.get(q['question_id'])==q['correct_option'])
        max_score=sum(int(q['points'] or 1) for q in questions)
        pct=(score/max_score*100) if max_score else 0
        status='expired' if auto else 'submitted'
        submitted=datetime.now(timezone.utc).isoformat()

        assessment_meta=one(select(SchoolAssessment.assessment_type,
                                   SchoolAssessment.session_id,
                                   SchoolAssessment.subject_id,
                                   SchoolAssessment.term)
                            .where(SchoolAssessment.id==attempt.assessment_id))
        # Practice results are visible immediately; graded work waits for the
        # session's release schedule.
        result_status='released' if assessment_meta and assessment_meta['assessment_type']=='practice' else 'entered'
        if assessment_meta and assessment_meta['assessment_type']!='practice':
            release=one_scalar(select(AcademicSession.result_release_at)
                               .where(AcademicSession.id==assessment_meta['session_id']))
            if release and release <= submitted:
                result_status='approved'

        # Conditional on status='active' so two concurrent submissions cannot
        # both post a result.
        updated=db.session.execute(sa_update(SchoolAssessmentAttempt)
            .where(SchoolAssessmentAttempt.id==attempt_id,
                   SchoolAssessmentAttempt.status=='active')
            .values(submitted_at=submitted,score=score,max_score=max_score,
                    percentage=pct,status=status)).rowcount
        if not updated:
            db.session.rollback()
            return obj(SchoolAssessmentAttempt,attempt_id)

        # Never overwrite a result that has already been released.
        already_released=one_scalar(select(SchoolStudentResult.id).where(
            SchoolStudentResult.student_id==attempt.student_id,
            SchoolStudentResult.assessment_id==attempt.assessment_id,
            SchoolStudentResult.status=='released'))
        if not already_released and assessment_meta:
            db.session.add(SchoolStudentResult(
                student_id=attempt.student_id,
                assessment_id=attempt.assessment_id,
                subject_id=assessment_meta['subject_id'],
                score=score,max_score=max_score,
                term=assessment_meta['term'] or 'Full Session',
                session_id=assessment_meta['session_id'],
                status=result_status,created_at=submitted))
        db.session.commit()
        db.session.refresh(attempt)
        return attempt
    except sa.exc.SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Failed to grade student assessment attempt %s", attempt_id)
        raise
