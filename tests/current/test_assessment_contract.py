import re
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
APP=(ROOT/"app.py").read_text(encoding="utf-8")
CONTRACT=(ROOT/"docs/architecture/ASSESSMENT_UX_REQUIREMENTS.md").read_text(encoding="utf-8")

def test_student_test_and_exam_share_required_interaction_contract():
    for phrase in ("Timer does not start merely because the student opens the assessment page.",
                   "Student presses **Start Test/Start Examination**.",
                   "one question at a time","Save & Next","server-side",
                   "automatically finalizes/submits"):
        assert phrase in CONTRACT

def test_persistent_student_attempt_model_exists():
    # The attempt tables are declared in models.py and referred to in app.py by
    # their mapped classes since the SQLAlchemy migration.
    MODELS=(ROOT/"models.py").read_text(encoding="utf-8")
    for table,model in (("school_assessment_attempts","SchoolAssessmentAttempt"),
                        ("school_assessment_attempt_questions","SchoolAssessmentAttemptQuestion"),
                        ("school_assessment_answers","SchoolAssessmentAnswer")):
        assert f"__tablename__ = '{table}'" in MODELS, table
        assert model in APP, model
    assert "student_assessment_start" in APP
    assert "student_assessment_grade" in APP

def test_student_assessment_server_enforces_expiry():
    start=APP.index("def student_assessment_answer")
    end=APP.index("def student_assessment_result",start)
    route=APP[start:end]
    assert "remaining(attempt)<=0" in route
    assert "student_assessment_grade" in route


def test_student_assessment_uses_entrance_style_left_palette():
    template=(ROOT/"templates/student_assessment_take.html").read_text(encoding="utf-8")
    assert 'student-exam-layout' in template
    assert '<aside' in template
    assert 'class="palette"' in template
    assert 'Save &amp; Next' in template
    assert 'Submit {{assessment.assessment_type' in template

def test_student_assessment_timer_is_started_by_server_attempt_creation():
    start=APP.index("def student_assessment_start")
    end=APP.index("def student_assessment_take",start)
    route=APP[start:end]
    assert "started_at" in route
    assert "expires_at" in route
    assert "timedelta(minutes=int(a['duration_minutes']))" in route

def test_student_result_insert_uses_assessment_primary_key():
    """school_assessments exposes its key as `id`, never as `assessment_id`.

    Reading the assessment by the wrong column previously produced a silent
    failure to record the student's result, so the lookup is pinned here.
    """
    start=APP.index("def student_assessment_grade")
    end=APP.index("@app.post('/student/assessments/",start)
    route=APP[start:end]
    # The assessment is looked up by its primary key, keyed on the attempt's
    # assessment_id. Either attribute or mapping access on the attempt is fine;
    # what matters is which column of school_assessments is matched.
    assert re.search(r"SchoolAssessment\.id\s*==\s*attempt(\.assessment_id|\['assessment_id'\])", route)
    assert "SchoolAssessment.assessment_id" not in route
    # The stored result takes its subject and session from that assessment row.
    assert "subject_id=assessment_meta['subject_id']" in route
