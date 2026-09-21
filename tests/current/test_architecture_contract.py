"""Current architecture contract tests.
These tests intentionally avoid importing Flask so the baseline can be checked in a minimal environment.
"""
from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app.py"
MODELS_DIR = ROOT / "models"
CORE_DIR = ROOT / "core"
BLUEPRINTS_DIR = ROOT / "blueprints"
STUDENT_PORTAL_ROUTES = BLUEPRINTS_DIR / "student_portal" / "routes.py"
STUDENT_PORTAL_HELPERS = BLUEPRINTS_DIR / "student_portal" / "helpers.py"


def _models_text():
    """Concatenated source of every module in the models/ package.

    models.py used to be one file; it's now a package split by domain
    (models/school.py, models/finance.py, ...), so a single-file read no
    longer sees every model. Reading every .py under models/ keeps this
    check correct regardless of how the package is further subdivided.
    """
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(MODELS_DIR.glob("*.py")))


def _blueprints_text():
    """Concatenated source of every route/helper module under blueprints/.

    Routes and their private helpers used to live in app.py; they're now
    split one package per domain, so markers that used to be found in app.py
    directly may now live in a blueprint module instead.
    """
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(BLUEPRINTS_DIR.rglob("*.py")))


def _core_text():
    """Concatenated source of every module in the core/ package.

    Cross-cutting helpers (and the model imports they need) moved out of
    app.py into core/*.py, so a marker once only found in app.py's own text
    may now live here instead.
    """
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(CORE_DIR.glob("*.py")))


def test_app_parses():
    ast.parse(APP.read_text(encoding="utf-8"))


def test_requirement_documents_exist():
    for rel in (
        "docs/architecture/MULTI_TENANCY.md",
        "docs/architecture/ASSESSMENT_UX_REQUIREMENTS.md",
        "docs/architecture/TYPEAHEAD_REQUIREMENT.md",
    ):
        assert (ROOT / rel).exists(), rel


def test_core_security_and_domain_markers_exist():
    text = APP.read_text(encoding="utf-8") + _blueprints_text()
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
    # Not just app.py: a model referenced only by a blueprint/core module
    # after the reorganization is just as "used" as one app.py still imports.
    everywhere = APP.read_text(encoding="utf-8") + _blueprints_text() + _core_text()
    models_text = _models_text()
    for table, model in (
        ("school_assessments", "SchoolAssessment"),
        ("school_questions", "SchoolQuestion"),
        ("school_student_results", "SchoolStudentResult"),
        ("attempts", "Attempt"),
        ("answers", "Answer"),
        ("audit_logs", "AuditLog"),
    ):
        assert f"__tablename__ = '{table}'" in models_text, table
        assert model in everywhere, model


def test_student_assessment_current_flow_is_explicitly_identified_for_refactor():
    routes_text = STUDENT_PORTAL_ROUTES.read_text(encoding="utf-8")
    helpers_text = STUDENT_PORTAL_HELPERS.read_text(encoding="utf-8")
    start = routes_text.index("def student_assessment_take")
    end = routes_text.index("def student_assessment_answer", start)
    route = routes_text[start:end]
    assert "SchoolAssessmentAttempt" in routes_text
    assert "SchoolStudentResult" in helpers_text
    assert "student_assessment_start" in routes_text
    # The take route reads the attempt rather than re-deriving it from the bank.
    assert "SchoolAssessmentAttempt" in route
