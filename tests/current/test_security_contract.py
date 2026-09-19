from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
APP=(ROOT/"app.py").read_text(encoding="utf-8")
UPLOADS=(ROOT/"core"/"uploads.py").read_text(encoding="utf-8")

def test_production_secret_has_no_known_fallback():
    assert "CRAINBOW_SECRET" in APP
    assert "phase6d-change-me" not in APP

def test_default_admin_password_is_removed():
    assert "admin123" not in APP

def test_student_assessment_does_not_render_correct_option():
    start=APP.index("def student_assessment_take")
    end=APP.index("def student_assessment_grade",start)
    route=APP[start:end]
    assert "public_q.pop('correct_option',None)" in route

def test_exam_client_uses_frozen_snapshot():
    """The exam is served from the per-attempt snapshot, not the live bank.

    The snapshot table is declared under models/ (a package split by domain),
    so the route is checked for the mapped class rather than the table name.
    """
    MODELS="\n".join(p.read_text(encoding="utf-8") for p in sorted((ROOT/"models").glob("*.py")))
    assert "__tablename__ = 'attempt_questions'" in MODELS
    assert "AttemptQuestion" in APP
    start=APP.index("def exam():")
    end=APP.index("@app.post('/answer')",start)
    route=APP[start:end]
    assert "AttemptQuestion" in route

def test_csrf_is_applied_to_candidate_state_changes():
    assert "@app.post('/answer')\n@csrf_protect" in APP
    assert "@app.post('/submit')\n@csrf_protect" in APP
    assert "@app.post('/candidate/papers/<int:paper_id>/start')\n@csrf_protect" in APP

def test_security_headers_and_upload_limits_exist():
    assert "apply_security_headers" in APP
    assert "MAX_CONTENT_LENGTH" in APP
    assert "CRAINBOW_MAX_UPLOAD_BYTES" in APP

def test_sqlite_foreign_keys_are_enabled_per_connection():
    assert "PRAGMA foreign_keys=ON" in APP

def test_login_rate_limiting_exists():
    AUTH_ROUTES=(ROOT/"blueprints"/"auth"/"routes.py").read_text(encoding="utf-8")
    assert "_rate_limit(rate_key" in AUTH_ROUTES


def test_upload_signature_validation_accepts_common_image_formats():
    route=UPLOADS[UPLOADS.index("def _save_image_upload"):]
    assert "'jpg': header.startswith(b'\\xff\\xd8\\xff')" in route
    assert "'jpeg': header.startswith(b'\\xff\\xd8\\xff')" in route
    assert "'gif': header.startswith((b'GIF87a',b'GIF89a'))" in route
    assert "'webp': header.startswith(b'RIFF')" in route

def test_school_assessment_questions_are_editable():
    assert "admin_school_assessment_question_edit" in APP
    assert "/admin/school/assessments/<int:assessment_id>/questions/<int:question_id>/edit" in APP
    template=(ROOT/"templates/school_assessment_detail.html").read_text(encoding="utf-8")
    assert "admin_school_assessment_question_edit" in template
