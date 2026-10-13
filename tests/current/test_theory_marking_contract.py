"""Contract: theory questions and their marking stay safe.

* every To mark route checks the paper's own permission and the teacher's scope (they are not in
  ADMIN_ENDPOINT_PERMISSIONS, so nothing else would), and every one that writes is CSRF-protected;
* a student never receives a marking guide or a correct answer: the frozen copy made at the start of a paper never
  copies the guide, and the take route still drops correct_option;
* a written answer, a marking guide and a teacher's comment are only ever printed escaped (no |safe);
* a result with no score (theory still unmarked) can never be sent on or moved through the workflow;
* practice tests refuse theory questions.
It reads source only, and needs no database.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARKING = (ROOT / "blueprints" / "school" / "marking.py").read_text(encoding="utf-8")
SCHOOL_ROUTES = (ROOT / "blueprints" / "school" / "routes.py").read_text(encoding="utf-8")
STUDENT_ROUTES = (ROOT / "blueprints" / "student_portal" / "routes.py").read_text(encoding="utf-8")
RECORDS = (ROOT / "blueprints" / "school" / "results_records.py").read_text(encoding="utf-8")
SECURITY = (ROOT / "core" / "security.py").read_text(encoding="utf-8")


def _routes(source):
    """(decorators, body) of each route function in a module."""
    out = []
    for match in re.finditer(r"((?:@[^\n]+\n)+)def (\w+)\(", source):
        decorators, name = match.group(1), match.group(2)
        if "@app." not in decorators:
            continue
        body_end = source.find("\n@app.", match.end())
        out.append((name, decorators, source[match.end(): body_end if body_end != -1 else len(source)]))
    return out


def test_every_marking_route_checks_permission_and_scope_itself():
    routes = _routes(MARKING)
    assert len(routes) == 4, [r[0] for r in routes]
    for name, decorators, body in routes:
        assert "@admin_required" in decorators, name
        assert "admin_school_marking" in name
        assert f"'{name}'" not in SECURITY, f"{name} must check the paper's own permission, not a fixed one"
        if name == "admin_school_marking":
            assert "_may_mark_any(" in body and "_my_papers(" in body
        else:
            assert "_open_paper(" in body and "if refused is not None" in body, name
        if "@app.post" in decorators:
            assert "@csrf_protect" in decorators, name
    assert "_school_pair_allowed(" in MARKING and "_student_subject_allowed(" in MARKING
    save = next(body for name, _, body in routes if name == "admin_school_marking_save")
    assert "_students_i_mark(me['id'], paper, [student_id])" in save, "each student must be checked on their own"
    assert "'released'" in save, "a released result's marks must be locked"
    split = next(body for name, _, body in routes if name == "admin_school_marking_split")
    assert "split_locked(" in split


def test_a_student_never_receives_a_guide_or_a_correct_answer():
    start = STUDENT_ROUTES[STUDENT_ROUTES.index("def student_assessment_start"):STUDENT_ROUTES.index("def student_assessment_take")]
    assert "marking_guide" not in start
    take = STUDENT_ROUTES[STUDENT_ROUTES.index("def student_assessment_take"):STUDENT_ROUTES.index("def student_assessment_answer")]
    assert "public_q.pop('correct_option',None)" in take
    template = (ROOT / "templates" / "student_assessment_take.html").read_text(encoding="utf-8")
    assert "marking_guide" not in template and "correct_option" not in template


def test_answers_guides_and_comments_are_never_printed_raw():
    for name in ("school_marking_paper.html", "student_assessment_take.html", "school_assessment_detail.html",
                 "includes/_question_fields.html"):
        source = (ROOT / "templates" / name).read_text(encoding="utf-8")
        assert "|safe" not in source.replace(" ", ""), name


def test_an_unscored_result_cannot_move_on():
    workflow = SCHOOL_ROUTES[SCHOOL_ROUTES.index("def admin_school_result_workflow"):SCHOOL_ROUTES.index("def admin_school_results_release")]
    assert "row['score'] is None and row['assessment_id']" in workflow
    assert "or_(SchoolStudentResult.score.is_not(None), SchoolStudentResult.assessment_id.is_(None))" in RECORDS[RECORDS.index("def _submittable"):]
    edit = SCHOOL_ROUTES[SCHOOL_ROUTES.index("def admin_school_result_edit"):SCHOOL_ROUTES.index("def admin_school_result_workflow")]
    assert "theory_marking.has_theory(" in edit


def test_practice_tests_refuse_theory_and_answers_are_limited():
    new = SCHOOL_ROUTES[SCHOOL_ROUTES.index("def admin_school_assessment_question_new"):SCHOOL_ROUTES.index("def admin_school_assessment_question_edit")]
    assert "a['assessment_type']=='practice'" in new and "theory_marking.THEORY" in new
    answer = STUDENT_ROUTES[STUDENT_ROUTES.index("def student_assessment_answer"):STUDENT_ROUTES.index("def student_assessment_heartbeat")]
    assert "THEORY_ANSWER_LIMIT" in answer and "abort(413)" in answer
