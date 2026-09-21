from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
APP=(ROOT/"app.py").read_text(encoding="utf-8")
UPLOADS=(ROOT/"core"/"uploads.py").read_text(encoding="utf-8")
SCHOOL_ROUTES=(ROOT/"blueprints"/"school"/"routes.py").read_text(encoding="utf-8")
STUDENT_PORTAL_ROUTES=(ROOT/"blueprints"/"student_portal"/"routes.py").read_text(encoding="utf-8")
STUDENT_PORTAL_HELPERS=(ROOT/"blueprints"/"student_portal"/"helpers.py").read_text(encoding="utf-8")
CANDIDATE_PORTAL_ROUTES=(ROOT/"blueprints"/"candidate_portal"/"routes.py").read_text(encoding="utf-8")

def test_production_secret_has_no_known_fallback():
    assert "BRIGHTSTARS_SECRET" in APP
    assert "phase6d-change-me" not in APP

def test_default_admin_password_is_removed():
    assert "admin123" not in APP

def test_student_assessment_does_not_render_correct_option():
    start=STUDENT_PORTAL_ROUTES.index("def student_assessment_take")
    end=STUDENT_PORTAL_ROUTES.index("def student_assessment_answer",start)
    route=STUDENT_PORTAL_ROUTES[start:end]
    assert "public_q.pop('correct_option',None)" in route
    assert "def student_assessment_grade" in STUDENT_PORTAL_HELPERS

def test_exam_client_uses_frozen_snapshot():
    """The exam is served from the per-attempt snapshot, not the live bank.

    The snapshot table is declared under models/ (a package split by domain),
    so the route is checked for the mapped class rather than the table name.
    """
    MODELS="\n".join(p.read_text(encoding="utf-8") for p in sorted((ROOT/"models").glob("*.py")))
    assert "__tablename__ = 'attempt_questions'" in MODELS
    assert "AttemptQuestion" in CANDIDATE_PORTAL_ROUTES
    start=CANDIDATE_PORTAL_ROUTES.index("def exam():")
    end=CANDIDATE_PORTAL_ROUTES.index("@app.post('/answer')",start)
    route=CANDIDATE_PORTAL_ROUTES[start:end]
    assert "AttemptQuestion" in route

def test_csrf_is_applied_to_candidate_state_changes():
    assert "@app.post('/answer')\n@csrf_protect" in CANDIDATE_PORTAL_ROUTES
    assert "@app.post('/submit')\n@csrf_protect" in CANDIDATE_PORTAL_ROUTES
    assert "@app.post('/candidate/papers/<int:paper_id>/start')\n@csrf_protect" in CANDIDATE_PORTAL_ROUTES

def test_security_headers_and_upload_limits_exist():
    assert "apply_security_headers" in APP
    assert "MAX_CONTENT_LENGTH" in APP
    assert "BRIGHTSTARS_MAX_UPLOAD_BYTES" in UPLOADS

def test_login_rate_limiting_exists():
    AUTH_ROUTES=(ROOT/"blueprints"/"auth"/"routes.py").read_text(encoding="utf-8")
    assert "_rate_limit(rate_key" in AUTH_ROUTES


def test_upload_signature_validation_accepts_common_image_formats():
    route=UPLOADS[UPLOADS.index("def validate_image_upload"):UPLOADS.index("def _save_image_upload")]
    # Saving an upload must never bypass that validation.
    assert "validate_image_upload(file_obj)" in UPLOADS[UPLOADS.index("def _save_image_upload"):]
    assert "'jpg': header.startswith(b'\\xff\\xd8\\xff')" in route
    assert "'jpeg': header.startswith(b'\\xff\\xd8\\xff')" in route
    assert "'gif': header.startswith((b'GIF87a',b'GIF89a'))" in route
    assert "'webp': header.startswith(b'RIFF')" in route

def test_school_assessment_questions_are_editable():
    assert "admin_school_assessment_question_edit" in SCHOOL_ROUTES
    assert "/admin/school/assessments/<int:assessment_id>/questions/<int:question_id>/edit" in SCHOOL_ROUTES
    template=(ROOT/"templates/school_assessment_detail.html").read_text(encoding="utf-8")
    assert "admin_school_assessment_question_edit" in template


def test_a_candidate_never_sees_their_own_score_or_answers():
    """After an exam a candidate is told the school will release the result. The score, the
    percentage and a way to review the attempt exist only for staff."""
    result = (ROOT / "templates" / "result.html").read_text(encoding="utf-8")
    review = (ROOT / "templates" / "review.html").read_text(encoding="utf-8")
    code = "\n".join(p.read_text(encoding="utf-8") for folder in ("blueprints", "core")
                     for p in (ROOT / folder).rglob("*.py") if "__pycache__" not in str(p))
    code += (ROOT / "app.py").read_text(encoding="utf-8")
    # The candidate's result page is rendered with no score data at all.
    assert "Response(render_template('result.html'))" in code
    for word in ("attempt.score", "attempt.percentage", "Final Score", "Percentage", "Review Attempt"):
        assert word not in result, word
    assert "Your result will be released by the school." in result
    # Reviewing an attempt sends a candidate back to that page; only staff have the detail views.
    assert "def review():" in code and "redirect(url_for('result'))" in code
    assert "Start Another Test" in review
    for view in ("admin_result_detail", "admin_result_print"):
        assert "@admin_required\ndef " + view in code, view
    assert "def admin_grant_retake" in code
