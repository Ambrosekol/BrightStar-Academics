"""Current architecture contract tests.
These tests intentionally avoid importing Flask so the baseline can be checked in a minimal environment.
"""
from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app.py"
MODELS = ROOT / "models.py"


def test_app_parses():
    ast.parse(APP.read_text(encoding="utf-8"))


def test_baseline_documents_exist():
    for rel in (
        "docs/BASELINE_SHA256.txt",
        "docs/architecture/CURRENT_ARCHITECTURE_MAP.md",
        "docs/architecture/TARGET_ARCHITECTURE.md",
        "docs/architecture/ASSESSMENT_UX_REQUIREMENTS.md",
        "docs/architecture/TYPEAHEAD_REQUIREMENT.md",
    ):
        assert (ROOT / rel).exists(), rel


def test_core_security_and_domain_markers_exist():
    text = APP.read_text(encoding="utf-8")
    required = [
        "def student_required",
        "def _student_assessment_context",
        "def student_assessment_take",
        "@csrf_protect",
        "correct_option",
    ]
    for marker in required:
        assert marker in text, marker


def test_core_domain_tables_are_declared_and_used():
    """The domain tables still exist, now as SQLAlchemy models.

    Since the SQLAlchemy migration, table names live in models.py as
    __tablename__ and app.py refers to the mapped classes instead, so this
    checks both halves rather than grepping app.py for raw SQL table names.
    """
    app_text = APP.read_text(encoding="utf-8")
    models_text = MODELS.read_text(encoding="utf-8")
    for table, model in (
        ("school_assessments", "SchoolAssessment"),
        ("school_questions", "SchoolQuestion"),
        ("school_student_results", "SchoolStudentResult"),
        ("attempts", "Attempt"),
        ("answers", "Answer"),
        ("audit_logs", "AuditLog"),
    ):
        assert f"__tablename__ = '{table}'" in models_text, table
        assert model in app_text, model


def test_student_assessment_current_flow_is_explicitly_identified_for_refactor():
    text = APP.read_text(encoding="utf-8")
    start = text.index("def student_assessment_take")
    end = text.index("@app.route('/practice'", start)
    route = text[start:end]
    assert "SchoolAssessmentAttempt" in text
    assert "SchoolStudentResult" in text
    assert "student_assessment_start" in text
    # The take route reads the attempt rather than re-deriving it from the bank.
    assert "SchoolAssessmentAttempt" in route
