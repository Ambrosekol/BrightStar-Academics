"""Allocation of official student numbers.

Numbers are issued from a per-school numbering policy and recorded in the
student_number_allocations ledger, which is what makes a number traceable and
guarantees it is never silently reused.

Every function here runs inside the caller's SQLAlchemy session, so an
allocation and the student record it belongs to commit or roll back together.
"""
from datetime import datetime, timezone
import re

from sqlalchemy import select, update as sa_update

from core import numbering
from models import (
    SchoolNumberingPolicy,
    Student,
    StudentNumberAllocation,
    db,
)


class StudentNumberAllocationError(RuntimeError):
    """Raised when a student number cannot be safely allocated."""


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _clean(value):
    if value is None:
        return ""
    return str(value).strip()


def _positive_int(value, field_name):
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise StudentNumberAllocationError(
            f"Invalid {field_name}: {value!r}"
        )

    if value < 0:
        raise StudentNumberAllocationError(
            f"{field_name} cannot be negative."
        )

    return value


def _build_student_number(
    prefix,
    include_year,
    allocation_year,
    sequence_number,
    padding,
):
    prefix = _clean(prefix)

    if not prefix:
        raise StudentNumberAllocationError(
            "Numbering policy prefix is empty."
        )

    if not re.fullmatch(r"[A-Za-z0-9_-]+", prefix):
        raise StudentNumberAllocationError(
            "Numbering policy prefix contains unsupported characters."
        )

    padding = max(1, _positive_int(padding, "padding"))
    sequence_number = _positive_int(
        sequence_number,
        "sequence_number",
    )

    parts = [prefix]

    if int(include_year or 0) == 1:
        if allocation_year is None:
            raise StudentNumberAllocationError(
                "allocation_year is required when include_year is enabled."
            )

        year_text = str(allocation_year).strip()

        if not re.fullmatch(r"\d{4}", year_text):
            raise StudentNumberAllocationError(
                "allocation_year must be a four-digit year."
            )

        parts.append(year_text)

    parts.append(str(sequence_number).zfill(padding))

    return "/".join(parts)


def _policy(school_id):
    row = db.session.scalars(
        select(SchoolNumberingPolicy).where(
            SchoolNumberingPolicy.school_id == school_id
        )
    ).first()

    if row is None:
        raise StudentNumberAllocationError(
            f"No numbering policy exists for school_id={school_id}."
        )

    if int(row.active or 0) != 1:
        raise StudentNumberAllocationError(
            f"Numbering policy is inactive for school_id={school_id}."
        )

    return row


def allocate_student_number(
    school_id,
    student_id=None,
    allocation_year=None,
    allocated_by=None,
):
    """Allocate the next student number for a school.

    Runs inside the caller's session transaction; it never commits, so the
    caller decides whether the whole registration succeeds.

    An existing student number is never replaced.

    Returns a dict containing student_number and allocation metadata.
    """

    school_id = _positive_int(school_id, "school_id")

    if student_id is not None:
        student_id = _positive_int(student_id, "student_id")

    policy = _policy(school_id)

    if student_id is not None:
        existing = db.session.scalars(
            select(Student).where(Student.id == student_id)
        ).first()

        if existing is None:
            raise StudentNumberAllocationError(
                f"Student id={student_id} does not exist."
            )

        if int(existing.school_id) != school_id:
            raise StudentNumberAllocationError(
                "Student does not belong to requested school."
            )

        if _clean(existing.student_number):
            raise StudentNumberAllocationError(
                "Student already has a student number; "
                "existing identity will not be overwritten."
            )

    sequence = policy.next_sequence

    if sequence is None:
        sequence = policy.sequence_start

    sequence = _positive_int(sequence, "next_sequence")

    if sequence < 1:
        sequence = 1

    if allocation_year is None:
        allocation_year = datetime.now(timezone.utc).year

    # A school may write its numbers its own way (a student-number pattern in tenants/<code>/numbering.json,
    # set on the platform console; a pattern is read, never run). The running
    # number, the ledger and the collision checks below stay the platform's, whatever the rule.
    try:
        candidate = numbering.student_number(
            prefix=policy.prefix,
            include_year=policy.include_year,
            year=allocation_year,
            sequence=sequence,
            padding=policy.padding,
        )
    except numbering.NumberingRuleError as exc:
        raise StudentNumberAllocationError(str(exc)) from exc

    if candidate is None:
        candidate = _build_student_number(
            prefix=policy.prefix,
            include_year=policy.include_year,
            allocation_year=allocation_year,
            sequence_number=sequence,
            padding=policy.padding,
        )

    # Collision check #1: the allocation ledger.
    ledger_collision = db.session.scalars(
        select(StudentNumberAllocation).where(
            StudentNumberAllocation.school_id == school_id,
            StudentNumberAllocation.student_number == candidate,
        ).limit(1)
    ).first()

    if ledger_collision is not None:
        raise StudentNumberAllocationError(
            "Student-number collision in allocation ledger: " + candidate
        )

    # Collision check #2: student records.
    student_collision = db.session.scalars(
        select(Student).where(
            Student.school_id == school_id,
            Student.student_number == candidate,
        ).limit(1)
    ).first()

    if student_collision is not None:
        raise StudentNumberAllocationError(
            "Student-number collision in students table: " + candidate
        )

    db.session.add(
        StudentNumberAllocation(
            school_id=school_id,
            student_number=candidate,
            sequence_number=sequence,
            allocation_year=allocation_year,
            source="generated",
            student_id=student_id,
            allocated_at=_utc_now(),
            allocated_by=_clean(allocated_by) or None,
            active=1,
        )
    )

    # Advance the sequence only after the allocation record exists, and only
    # if it still holds the value this allocation was built from. A concurrent
    # allocation that moved it on makes this update match no rows, which aborts
    # the allocation rather than issuing a duplicate number.
    updated = db.session.execute(
        sa_update(SchoolNumberingPolicy)
        .where(
            SchoolNumberingPolicy.school_id == school_id,
            SchoolNumberingPolicy.active == 1,
            SchoolNumberingPolicy.next_sequence == sequence,
        )
        .values(next_sequence=sequence + 1)
    ).rowcount

    if updated != 1:
        raise StudentNumberAllocationError(
            "Numbering policy sequence changed unexpectedly; "
            "allocation aborted."
        )

    db.session.flush()

    return {
        "school_id": school_id,
        "student_id": student_id,
        "student_number": candidate,
        "sequence_number": sequence,
        "allocation_year": allocation_year,
        "source": "generated",
    }
