"""Contract: the newer areas keep the database's load flat as the platform grows to tens of thousands of schools.

* the per-process cache is held school by school, bounded, and never emptied all at once; dropping one school's
  counts looks only at that school;
* a student's or candidate's own write (an exam autosave) does not throw away every staff member's cached counts and
  permissions; the newer menus' counts are dropped only by a change in their own area;
* who to tell (approvers, managers) is one query, not a permission check per account;
* leave balances, unpaid leave and a payroll's payslips are read in bulk, never a query per person;
* the "To mark" count looks at the current session (and older scripts still waiting), not the school's whole history,
  and checks students together;
* an exam-mode report reads both of its counts in one query;
* pages that are logged are drawn before the log commits (a commit would make every row on the page be read again).
The behavioural proof, with real query counts, is tests/verification/write_paths_query_budget.py. It reads source
only, and needs no database.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE = (ROOT / "core" / "short_cache.py").read_text(encoding="utf-8")
APP = (ROOT / "app.py").read_text(encoding="utf-8")
HR = (ROOT / "blueprints" / "school" / "hr.py").read_text(encoding="utf-8")
ATT = (ROOT / "blueprints" / "school" / "staff_attendance.py").read_text(encoding="utf-8")
PAY = (ROOT / "blueprints" / "school" / "payroll.py").read_text(encoding="utf-8")
MARK = (ROOT / "blueprints" / "school" / "marking.py").read_text(encoding="utf-8")
THEORY = (ROOT / "core" / "theory_marking.py").read_text(encoding="utf-8")
INTEGRITY = (ROOT / "core" / "exam_integrity.py").read_text(encoding="utf-8")


def _body(source, name):
    start = source.index(f"def {name}(")
    following = source.find("\ndef ", start + 1)
    return source[start:following if following != -1 else len(source)]


def test_the_cache_is_held_by_school_and_never_emptied_at_once():
    assert "_scopes = OrderedDict()" in CACHE and "_MAX_ENTRIES = 50_000" in CACHE
    room = _body(CACHE, "_make_room")
    assert "popitem(last=False)" in room and ".clear()" not in room
    assert "_scopes.get(scope)" in _body(CACHE, "forget_here")
    assert ".clear()" not in _body(CACHE, "remember")


def test_learner_writes_keep_staff_caches_and_menus_drop_only_on_their_own_changes():
    forget = APP[APP.index("def forget_counts_after_a_change("):APP.index("def forget_counts_after_a_change(") + 1500]
    assert "learner = (session.get('student_id') or session.get('candidate_id')) and not session.get('admin_id')" in forget
    assert "and not learner" in forget
    for name in ("hr-menu", "staff-clock-menu", "payroll-menu"):
        assert name not in forget, name
    for source, cache in ((HR, "'hr-menu'"), (ATT, "'staff-clock-menu'"), (PAY, "'payroll-menu'")):
        assert f"forget_here({cache}" in source


def test_approvers_are_found_in_one_query():
    assert "admins_holding('hr.leave.approve')" in HR and "admins_holding('staff_attendance.manage')" in ATT
    for source in (HR, ATT):
        assert "admin_has_permission(a," not in source


def test_balances_unpaid_leave_and_payslips_are_read_in_bulk():
    assert "_ledger(" in _body(HR, "admin_hr_leave") and "_ledger(" in _body(HR, "_balances")
    work = _body(PAY, "_work_out")
    assert "sa_delete(Payslip)" in work and "_unpaid_days(first, last)" in work and "db.session.delete(" not in work


def test_to_mark_looks_at_the_current_session_and_checks_students_together():
    assert "papers_with_theory(current['id'] if current else None)" in MARK
    assert "_students_for_subject(" in _body(MARK, "_students_i_mark")
    assert "SchoolAssessment.session_id == session_id" in _body(THEORY, "papers_with_theory")


def test_an_exam_report_reads_its_counts_in_one_query():
    record = _body(INTEGRITY, "record")
    assert record.count("db.session.execute(") + record.count("db.session.scalar(") == 1
    assert "func.count().filter(ExamIntegrityEvent.kind == 'left_page')" in record


def test_logged_pages_are_drawn_before_the_log_commits():
    person = _body(HR, "admin_hr_person")
    assert person.index("render_template(") < person.index("audit_log(")
    for name in ("admin_payroll", "admin_payroll_run", "admin_payroll_staff", "admin_payroll_payslip"):
        body = _body(PAY, name)
        assert "_logged(" in body and "audit_log(" not in body, name
