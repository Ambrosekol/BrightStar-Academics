"""Contract: exam mode and the one-page papers stay safe.

* the page's reports are accepted only for the person's own open paper, only with the form token, and only for kinds
  the page may send ('new_device' is the server's alone);
* the device tag is stored only as a hash, in an HttpOnly cookie;
* the whole paper is sent at once, so every question is copied without its correct answer, and no paper page ever
  carries a marking guide;
* ending a paper for leaving it too often happens on the server, never only in the browser;
* every paper page uses the shared layout and the school's own dialog (static/paper.js), not the browser's.
It reads source only, and needs no database.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STUDENT = (ROOT / "blueprints" / "student_portal" / "routes.py").read_text(encoding="utf-8")
CANDIDATE = (ROOT / "blueprints" / "candidate_portal" / "routes.py").read_text(encoding="utf-8")
INTEGRITY = (ROOT / "core" / "exam_integrity.py").read_text(encoding="utf-8")
PAPER_JS = (ROOT / "static" / "paper.js").read_text(encoding="utf-8")
PAGES = ("student_assessment_take.html", "exam.html", "student_practice_take.html", "entrance_practice_take.html", "practice_take.html")


def _route(source, name, after):
    start = source.index(f"def {name}(")
    return source[start:source.index(after, start)]


def test_reports_are_checked_before_they_are_recorded():
    school = _route(STUDENT, "student_assessment_integrity", "@app.post('/student/assessments/<int:assessment_id>/heartbeat')")
    assert "SchoolAssessmentAttempt.student_id==sid" in school and "attempt['status']!='active'" in school
    assert "exam_integrity.PAGE_KINDS" in school
    entrance = _route(CANDIDATE, "exam_integrity_report", "@app.post('/exam/heartbeat')")
    assert "a['candidate_id'] != session.get('candidate_id')" in entrance and "a['status']!='active'" in entrance
    assert "exam_integrity.PAGE_KINDS" in entrance
    assert "@app.post('/student/assessments/<int:assessment_id>/integrity')\n@student_required\n@csrf_protect" in STUDENT
    assert "@app.post('/exam/integrity')\n@csrf_protect" in CANDIDATE
    assert "PAGE_KINDS = frozenset(KINDS) - {'new_device'}" in INTEGRITY
    assert "EVENT_CAP" in INTEGRITY


def test_the_device_tag_is_kept_as_a_hash_in_an_httponly_cookie():
    assert "hashlib.sha256" in INTEGRITY and "hmac.compare_digest" in INTEGRITY
    assert "httponly=True" in INTEGRITY
    start = _route(STUDENT, "student_assessment_start", "@app.route('/student/assessments/<int:assessment_id>',methods=['GET'])")
    assert "device_tag=device_hash" in start and "device_raw" in start


def test_the_whole_paper_never_carries_an_answer_or_a_guide():
    take = _route(STUDENT, "student_assessment_take", "@app.post('/student/assessments/<int:assessment_id>/answer')")
    assert "public_q.pop('correct_option',None)" in take and "for q in rows:" in take
    for name in PAGES:
        source = (ROOT / "templates" / name).read_text(encoding="utf-8")
        assert "correct_option" not in source and "marking_guide" not in source, name
        assert "|safe" not in source.replace(" ", ""), name


def test_ending_a_paper_for_leaving_it_is_decided_by_the_server():
    school = _route(STUDENT, "student_assessment_integrity", "@app.post('/student/assessments/<int:assessment_id>/heartbeat')")
    assert "left>limit" in school and "student_assessment_grade(attempt['id']" in school
    assert "'school_assessment_ended_by_exam_mode'" in school


def test_every_paper_page_uses_the_shared_layout_and_the_schools_dialog():
    for name in PAGES:
        source = (ROOT / "templates" / name).read_text(encoding="utf-8")
        assert "{% import 'includes/_paper.html' as paper with context %}" in source, name
        assert "paper.js" in source and "interactions.js" in source, name
        assert "window.paperHooks" in source, name
    assert "window.brandDialog.confirm(" in PAPER_JS and "window.confirm(" not in PAPER_JS
