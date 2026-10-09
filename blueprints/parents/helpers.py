"""Private helpers used only by the parent-portal and admin parent-management
routes in this package: none of these are called from any other domain.
"""

import secrets
from functools import wraps

from flask import redirect, request, session, url_for
from sqlalchemy import and_, case, func, or_, select

from models import (
    Admin, AcademicSession, AssignmentStudent, ParentAccount,
    ParentFeedbackReply, ParentStudentLink, ProjectStudent, SchoolAssessment,
    SchoolAssignment, SchoolClass, SchoolProject, SchoolStudentResult,
    SchoolSubject, Student, StudentEnrolment, db,
)
from core.accounts import _clear_identity_sessions
from core.db_helpers import _flatten, all_rows, one
from blueprints.school.helpers import _school_class_allowed


def parent_required(fn):
    @wraps(fn)
    def wrapper(*args,**kwargs):
        pid=session.get('parent_id')
        if not pid:
            return redirect(url_for('login',next=request.path))
        row=db.session.scalars(select(ParentAccount).where(
            ParentAccount.id==pid,ParentAccount.active==1)).first()
        if not row:
            _clear_identity_sessions(); return redirect(url_for('login'))
        if row['password_must_change'] and request.endpoint != 'parent_password_change':
            return redirect(url_for('parent_password_change'))
        return fn(*args,**kwargs)
    return wrapper

def _parent_owns_student(pid, student_id):
    return bool(one(select(ParentStudentLink.id).where(
        ParentStudentLink.parent_id==pid,ParentStudentLink.student_id==student_id,
        ParentStudentLink.active==1)))

def _parent_children(pid, with_session=False):
    """Active children linked to a parent, each once, with their current class.

    A student who has been enrolled in more than one session (every promoted student)
    has more than one active enrolment row. The child is listed once, in the class of
    the current session or else their latest one: listed once per enrolment, the
    dashboard showed the child twice and added their unpaid fees up twice.
    """
    cols=[Student,ParentStudentLink.relationship,SchoolClass.name.label('class_name'),
          AcademicSession.name.label('session_name')]
    if with_session:
        cols.append(StudentEnrolment.session_id.label('session_id'))
    stmt=(select(*cols)
          .select_from(ParentStudentLink)
          .join(Student,and_(Student.id==ParentStudentLink.student_id,or_(Student.active==1,Student.archived_at.isnot(None))))
          .outerjoin(StudentEnrolment,and_(StudentEnrolment.student_id==Student.id,
                                           StudentEnrolment.active==1))
          .outerjoin(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
          .outerjoin(AcademicSession,AcademicSession.id==StudentEnrolment.session_id))
    stmt=stmt.where(ParentStudentLink.parent_id==pid,ParentStudentLink.active==1)
    stmt=stmt.order_by(Student.first_name,Student.last_name,Student.id,
                       case((AcademicSession.is_current==1,0),else_=1),StudentEnrolment.id.desc())
    extra=('relationship','class_name')+(('session_name','session_id') if with_session else ())
    children,seen=[],set()
    for r in all_rows(stmt):
        child=_flatten(r,'Student',*extra)
        if child['id'] in seen: continue
        seen.add(child['id']); children.append(child)
    return children


def _released_results(student_id, limit=None, session_id=None):
    stmt=(select(SchoolStudentResult.score,SchoolStudentResult.max_score,
                 SchoolStudentResult.term,SchoolStudentResult.status,
                 SchoolSubject.name.label('subject_name'),
                 func.coalesce(SchoolStudentResult.component_name,SchoolAssessment.title,
                               'Academic Result').label('component_name'))
          .join(SchoolSubject,SchoolSubject.id==SchoolStudentResult.subject_id)
          .outerjoin(SchoolAssessment,SchoolAssessment.id==SchoolStudentResult.assessment_id)
          .where(SchoolStudentResult.student_id==student_id,
                 SchoolStudentResult.status=='released')
          .order_by(SchoolStudentResult.id.desc()))
    if session_id:
        stmt=stmt.where(SchoolStudentResult.session_id==session_id)
    if limit:
        stmt=stmt.limit(limit)
    return all_rows(stmt)


def _student_assignments(student_id, limit=None, with_id=True, session_id=None):
    cols=([SchoolAssignment.id] if with_id else [])+[
        SchoolAssignment.title,SchoolAssignment.date_given,SchoolAssignment.due_date,
        SchoolAssignment.assignment_type,AssignmentStudent.status,
        AssignmentStudent.score,AssignmentStudent.max_score,AssignmentStudent.remark,
        SchoolSubject.name.label('subject_name')]
    stmt=(select(*cols).select_from(AssignmentStudent)
          .join(SchoolAssignment,SchoolAssignment.id==AssignmentStudent.assignment_id)
          .join(SchoolSubject,SchoolSubject.id==SchoolAssignment.subject_id)
          .where(AssignmentStudent.student_id==student_id,SchoolAssignment.active==1)
          .order_by(SchoolAssignment.date_given.desc(),SchoolAssignment.id.desc()))
    if session_id:
        stmt=stmt.where(SchoolAssignment.session_id==session_id)
    if limit:
        stmt=stmt.limit(limit)
    return all_rows(stmt)


def _student_projects(student_id, limit=None, with_id=True, session_id=None):
    cols=([SchoolProject.id] if with_id else [])+[
        SchoolProject.title,SchoolProject.date_given,SchoolProject.due_date,
        SchoolProject.max_score,ProjectStudent.status,ProjectStudent.score,
        ProjectStudent.remark,SchoolSubject.name.label('subject_name')]
    stmt=(select(*cols).select_from(ProjectStudent)
          .join(SchoolProject,SchoolProject.id==ProjectStudent.project_id)
          .join(SchoolSubject,SchoolSubject.id==SchoolProject.subject_id)
          .where(ProjectStudent.student_id==student_id,SchoolProject.active==1)
          .order_by(SchoolProject.date_given.desc(),SchoolProject.id.desc()))
    if session_id:
        stmt=stmt.where(SchoolProject.session_id==session_id)
    if limit:
        stmt=stmt.limit(limit)
    return all_rows(stmt)


def _child_sessions(student_id):
    """The academic sessions a child has anything to show for - a place in a class, or a released result -
    newest first, and the one to open on: the current session if the child is in it, else the latest.

    Returns ``(sessions, default_id)``; ``sessions`` is a list of ``AcademicSession``.
    """
    ids=set(db.session.scalars(select(StudentEnrolment.session_id).where(
        StudentEnrolment.student_id==student_id,StudentEnrolment.active==1)).all())
    ids|=set(db.session.scalars(select(SchoolStudentResult.session_id).where(
        SchoolStudentResult.student_id==student_id,SchoolStudentResult.status=='released').distinct()).all())
    ids.discard(None)
    sessions=db.session.scalars(select(AcademicSession).where(
        AcademicSession.id.in_(ids or {0}),AcademicSession.active==1).order_by(AcademicSession.id.desc())).all()
    default=next((x.id for x in sessions if x.is_current),sessions[0].id if sessions else None)
    return sessions,default


def _feedback_replies(feedback_ids):
    """Replies for a set of feedback threads, grouped by thread.

    One query for every thread rather than one per thread.
    """
    replies={}
    if not feedback_ids:
        return replies
    rows=all_rows(select(ParentFeedbackReply,Admin.display_name.label('admin_name'))
                  .outerjoin(Admin,Admin.id==ParentFeedbackReply.admin_id)
                  .where(ParentFeedbackReply.feedback_id.in_(list(feedback_ids)))
                  .order_by(ParentFeedbackReply.created_at))
    for row in rows:
        flat=_flatten(row,'ParentFeedbackReply','admin_name')
        flat['is_parent']=flat['admin_id'] is None
        replies.setdefault(flat['feedback_id'],[]).append(flat)
    return replies


def _assignment_metrics(assignments):
    """Average score, completion rate and direction of travel."""
    graded=[float(x['score'])/float(x['max_score'])*100 for x in assignments
            if x['score'] is not None and x['max_score'] and float(x['max_score'])>0]
    avg=round(sum(graded)/len(graded),1) if graded else None
    completed=sum(1 for x in assignments if x['status']=='done')
    completion=round(completed/len(assignments)*100,1) if assignments else None
    trend='Not enough data'
    if len(graded)>=3:
        recent=sum(graded[:3])/3; older=sum(graded[-3:])/3
        trend='Improving' if recent>older+3 else ('Needs attention' if recent<older-3 else 'Stable')
    return avg,completion,trend


def _parent_form_context(me):
    """Classes and enrolled students this administrator may link."""
    classes=db.session.scalars(select(SchoolClass).where(SchoolClass.active==1)
        .order_by(SchoolClass.level_order)).all()
    students=all_rows(
        select(Student.id,Student.admission_no,Student.first_name,Student.last_name,
               SchoolClass.id.label('class_id'),SchoolClass.name.label('class_name'))
        .join(StudentEnrolment,and_(StudentEnrolment.student_id==Student.id,
                                    StudentEnrolment.active==1))
        .join(SchoolClass,SchoolClass.id==StudentEnrolment.class_id)
        .where(Student.active==1)
        .order_by(SchoolClass.level_order,Student.first_name,Student.last_name))
    if not me['admin_type_system']:
        classes=[c for c in classes if _school_class_allowed(me['id'],c.id)]
        students=[st for st in students if _school_class_allowed(me['id'],st['class_id'])]
    return classes,students


def _new_parent_password():
    return 'PAR-'+secrets.token_urlsafe(8).replace('-','').replace('_','')[:8].upper()
