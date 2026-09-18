from __future__ import annotations

from datetime import datetime, timezone
import re


CORE_FIELDS = (
    "surname",
    "first_name",
    "middle_name",
    "date_of_birth",
    "state_of_origin",
    "gender",
    "school_attended",
)

PROFILE_FIELDS = (
    "previous_school",
    "reason_for_leaving",
    "religion",
    "denomination",
    "blood_group",
    "genotype",
    "convulsion_history",
    "asthma_history",
    "medical_frequency",
    "medical_treatment",
    "immunization",
    "food_allergies",
    "drug_allergies",
    "other_health_challenges",
    "disability",
    "disability_indication",
    "parent_signature",
    "parent_signature_date",
)

CONTACT_FIELDS = (
    "name",
    "address",
    "office_phone",
    "mobile",
    "email",
    "occupation",
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _clean(value):
    if value is None:
        return ""
    return str(value).strip()


def _norm(value):
    return re.sub(r"\s+", " ", _clean(value)).strip().casefold()


def _norm_contact(value):
    return re.sub(r"[^0-9+]", "", _clean(value))


def table_columns(con, table):
    return {
        row["name"]
        for row in con.execute(f"PRAGMA table_info([{table}])")
    }


def _required_student_fields(con):
    cols = table_columns(con, "students")
    required = {
        "school_id",
        "surname",
        "first_name",
        "date_of_birth",
        "gender",
    }
    missing = required - cols
    if missing:
        raise RuntimeError(
            "students table is missing required fields: "
            + ", ".join(sorted(missing))
        )
    return cols


def find_duplicate_student(con, school_id, payload):
    """
    Actual-schema-safe duplicate detection.

    The school student table is the authoritative legacy schema.
    This function discovers the available name/DOB/gender/contact columns
    instead of assuming a surname/first_name schema that may not exist.
    """
    import sqlite3

    def cols(table):
        return {
            r[1]
            for r in con.execute(
                f"PRAGMA table_info({table})"
            )
        }

    student_cols = cols("students")

    # Candidate aliases used by the existing system.
    surname_col = next(
        (
            c for c in (
                "surname",
                "last_name",
                "student_surname",
                "family_name",
            )
            if c in student_cols
        ),
        None,
    )

    first_col = next(
        (
            c for c in (
                "first_name",
                "firstname",
                "student_first_name",
                "given_name",
            )
            if c in student_cols
        ),
        None,
    )

    middle_col = next(
        (
            c for c in (
                "middle_name",
                "middlename",
                "other_name",
            )
            if c in student_cols
        ),
        None,
    )

    dob_col = next(
        (
            c for c in (
                "date_of_birth",
                "dob",
                "birth_date",
            )
            if c in student_cols
        ),
        None,
    )

    gender_col = next(
        (
            c for c in (
                "gender",
                "sex",
            )
            if c in student_cols
        ),
        None,
    )

    school_col = "school_id" if "school_id" in student_cols else None

    if not surname_col and not first_col:
        raise sqlite3.OperationalError(
            "Admissions duplicate engine cannot find a student name column "
            f"in actual students schema: {sorted(student_cols)}"
        )

    conditions = []
    params = []

    def clean(v):
        return str(v or "").strip().lower()

    surname = clean(
        payload.get("surname")
        or payload.get("last_name")
        or payload.get("student_surname")
    )

    first_name = clean(
        payload.get("first_name")
        or payload.get("firstname")
        or payload.get("student_first_name")
        or payload.get("given_name")
    )

    middle_name = clean(
        payload.get("middle_name")
        or payload.get("middlename")
        or payload.get("other_name")
    )

    dob = str(
        payload.get("date_of_birth")
        or payload.get("dob")
        or ""
    ).strip()

    gender = clean(
        payload.get("gender")
        or payload.get("sex")
    )

    if school_col and school_id is not None:
        conditions.append("school_id=?")
        params.append(school_id)

    # Use only columns that genuinely exist.
    if surname_col and surname:
        conditions.append(
            f"LOWER(COALESCE({surname_col},''))=?"
        )
        params.append(surname)

    if first_col and first_name:
        conditions.append(
            f"LOWER(COALESCE({first_col},''))=?"
        )
        params.append(first_name)

    if middle_col and middle_name:
        conditions.append(
            f"LOWER(COALESCE({middle_col},''))=?"
        )
        params.append(middle_name)

    if dob_col and dob:
        conditions.append(
            f"COALESCE({dob_col},'')=?"
        )
        params.append(dob)

    if gender_col and gender:
        conditions.append(
            f"LOWER(COALESCE({gender_col},''))=?"
        )
        params.append(gender)

    # Name + DOB is the strongest safe legacy match available here.
    # Do not auto-merge on name alone.
    if not conditions:
        return None

    sql = (
        "SELECT * FROM students WHERE "
        + " AND ".join(conditions)
        + " LIMIT 1"
    )

    return con.execute(sql, params).fetchone()

def find_or_create_parent(con, payload, explicit_id=None):
    row = _find_parent(
        con,
        payload,
        explicit_id=explicit_id,
    )

    if row:
        return row, False

    return _create_parent(con, payload), True


def _write_contact(
    con,
    student_id,
    parent_id,
    role,
    payload,
    updated_by=None,
):
    existing = con.execute(
        """
        SELECT id
        FROM student_admission_contacts
        WHERE student_id=? AND role=?
        """,
        (student_id, role),
    ).fetchone()

    values = {
        field: _clean(payload.get(field))
        for field in CONTACT_FIELDS
    }

    if existing:
        con.execute(
            """
            UPDATE student_admission_contacts
            SET parent_id=?,
                name=?,
                address=?,
                office_phone=?,
                mobile=?,
                email=?,
                occupation=?,
                updated_at=?,
                updated_by=?
            WHERE id=?
            """,
            (
                parent_id,
                values["name"],
                values["address"],
                values["office_phone"],
                values["mobile"],
                values["email"],
                values["occupation"],
                _now(),
                updated_by,
                existing["id"],
            ),
        )
    else:
        con.execute(
            """
            INSERT INTO student_admission_contacts
            (
                student_id,
                parent_id,
                role,
                name,
                address,
                office_phone,
                mobile,
                email,
                occupation,
                created_at,
                updated_at,
                updated_by
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                student_id,
                parent_id,
                role,
                values["name"],
                values["address"],
                values["office_phone"],
                values["mobile"],
                values["email"],
                values["occupation"],
                _now(),
                _now(),
                updated_by,
            ),
        )

    if parent_id:
        link_cols = table_columns(con, "parent_student_links")

        existing_link = con.execute(
            """
            SELECT id
            FROM parent_student_links
            WHERE parent_id=? AND student_id=?
            LIMIT 1
            """,
            (parent_id, student_id),
        ).fetchone()

        if not existing_link:
            fields = ["parent_id", "student_id"]
            vals = [parent_id, student_id]

            for col in (
                "relationship",
                "role",
                "relationship_type",
            ):
                if col in link_cols:
                    fields.append(col)
                    vals.append(role)
                    break

            placeholders = ",".join("?" for _ in fields)

            con.execute(
                f"""
                INSERT INTO parent_student_links
                ({",".join(fields)})
                VALUES ({placeholders})
                """,
                vals,
            )


def save_admission(
    con,
    school_id,
    payload,
    student_id=None,
    updated_by=None,
):
    student_cols = _required_student_fields(con)

    surname = _clean(payload.get("surname"))
    first_name = _clean(payload.get("first_name"))
    dob = _clean(payload.get("date_of_birth"))
    gender = _clean(payload.get("gender"))

    if not surname or not first_name or not dob or not gender:
        raise ValueError(
            "Surname, first name, date of birth and gender are required."
        )

    duplicate = find_duplicate_student(
        con,
        school_id,
        payload,
        exclude_id=student_id,
    )

    if duplicate:
        number = (
            duplicate["student_number"]
            or duplicate["admission_no"]
            or "not assigned"
        )

        raise ValueError(
            "A student with the same surname, first name and date "
            f"of birth already exists "
            f"(ID {duplicate['id']}, Registration {number})."
        )

    student_values = {
        "school_id": school_id,
        "surname": surname,
        "first_name": first_name,
        "middle_name": _clean(payload.get("middle_name")),
        "date_of_birth": dob,
        "state_of_origin": _clean(payload.get("state_of_origin")),
        "gender": gender,
        "school_attended": _clean(
            payload.get("school_attended")
            or payload.get("previous_school")
        ),
    }

    if "reason_for_leaving" in student_cols:
        student_values["reason_for_leaving"] = _clean(
            payload.get("reason_for_leaving")
        )

    if "previous_school" in student_cols:
        student_values["previous_school"] = _clean(
            payload.get("previous_school")
        )

    if "parent_guardian_name" in student_cols:
        student_values["parent_guardian_name"] = _clean(
            payload.get("father_guardian_name")
        )

    if "parent_guardian_relationship" in student_cols:
        student_values["parent_guardian_relationship"] = (
            "Father/Guardian"
        )

    if "primary_mobile" in student_cols:
        student_values["primary_mobile"] = _clean(
            payload.get("father_guardian_mobile")
        )

    if "alternative_mobile" in student_cols:
        student_values["alternative_mobile"] = _clean(
            payload.get("mother_office_phone")
        )

    if "parent_guardian_email" in student_cols:
        student_values["parent_guardian_email"] = _clean(
            payload.get("father_guardian_email")
        )

    if student_id is None:
        # admission_no is transitional legacy compatibility.
        if "admission_no" in student_cols:
            info = {
                r["name"]: r
                for r in con.execute("PRAGMA table_info(students)")
            }

            admission_info = info["admission_no"]

            if (
                admission_info["notnull"]
                and admission_info["dflt_value"] is None
            ):
                import uuid

                student_values["admission_no"] = (
                    "__PENDING_ADMISSION__" + uuid.uuid4().hex
                )

        fields = [
            field
            for field in student_values
            if field in student_cols
        ]

        placeholders = ",".join("?" for _ in fields)

        cur = con.execute(
            f"""
            INSERT INTO students ({",".join(fields)})
            VALUES ({placeholders})
            """,
            [student_values[field] for field in fields],
        )

        student_id = cur.lastrowid

        from services.student_number_generator import (
            allocate_student_number
        )

        allocation = allocate_student_number(
            con,
            school_id,
            student_id=student_id,
            allocated_by=updated_by,
        )

        generated_number = allocation["student_number"]

        if "student_number" in student_cols:
            con.execute(
                """
                UPDATE students
                SET student_number=?
                WHERE id=?
                """,
                (generated_number, student_id),
            )

        if "admission_no" in student_cols:
            con.execute(
                """
                UPDATE students
                SET admission_no=?
                WHERE id=?
                """,
                (generated_number, student_id),
            )

    else:
        fields = [
            field
            for field in student_values
            if field in student_cols
            and field != "school_id"
        ]

        if fields:
            assignments = ", ".join(
                f"{field}=?" for field in fields
            )

            con.execute(
                f"""
                UPDATE students
                SET {assignments}
                WHERE id=? AND school_id=?
                """,
                [student_values[field] for field in fields]
                + [student_id, school_id],
            )

    _write_profile(
        con,
        student_id,
        payload,
        updated_by=updated_by,
    )

    father = {
        "name": _clean(payload.get("father_guardian_name")),
        "address": _clean(payload.get("father_guardian_address")),
        "office_phone": _clean(
            payload.get("father_guardian_office_phone")
        ),
        "mobile": _clean(payload.get("father_guardian_mobile")),
        "email": _clean(payload.get("father_guardian_email")),
        "occupation": "",
    }

    mother = {
        "name": _clean(payload.get("mother_name")),
        "address": _clean(payload.get("mother_address")),
        "office_phone": _clean(
            payload.get("mother_office_phone")
        ),
        "mobile": "",
        "email": _clean(payload.get("mother_email")),
        "occupation": _clean(payload.get("mother_occupation")),
    }

    parent_results = {}

    if any(father.values()):
        parent, created = find_or_create_parent(
            con,
            father,
            explicit_id=payload.get("father_parent_id") or None,
        )

        _write_contact(
            con,
            student_id,
            parent["id"],
            "father_guardian",
            father,
            updated_by,
        )

        parent_results["father_guardian"] = {
            "id": parent["id"],
            "created": created,
        }

    if any(mother.values()):
        parent, created = find_or_create_parent(
            con,
            mother,
            explicit_id=payload.get("mother_parent_id") or None,
        )

        _write_contact(
            con,
            student_id,
            parent["id"],
            "mother",
            mother,
            updated_by,
        )

        parent_results["mother"] = {
            "id": parent["id"],
            "created": created,
        }

    row = con.execute(
        """
        SELECT student_number, admission_no
        FROM students
        WHERE id=?
        """,
        (student_id,),
    ).fetchone()

    return {
        "student_id": student_id,
        "student_number": row["student_number"],
        "admission_no": row["admission_no"],
        "parents": parent_results,
    }