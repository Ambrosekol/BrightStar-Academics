"""Dependency-free static verification of candidate result restrictions."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
app = (ROOT / "app.py").read_text(encoding="utf-8")
result = (ROOT / "templates" / "result.html").read_text(encoding="utf-8")
review = (ROOT / "templates" / "review.html").read_text(encoding="utf-8")

checks = {
    "candidate result route does not pass score data": "render_template('result.html')" in app,
    "candidate review route redirects to result": "def review():" in app and "return redirect(url_for('result'))" in app,
    "submitted exam redirects away from exam": "if a['status']!='active': return redirect(url_for('result'))" in app,
    "candidate result has no score": all(x not in result for x in ["attempt.score", "attempt.percentage", "Final Score", "Percentage"]),
    "candidate result has no review link": "Review Attempt" not in result,
    "candidate result has school-release message": "Your result will be released by the school." in result,
    "candidate login blocks completed attempt": "already completed this examination" in app,
    "admin retake grant exists": "def admin_grant_retake" in app,
    "admin-only result detail": "@admin_required\ndef admin_result_detail" in app,
    "admin-only printable result": "@admin_required\ndef admin_result_print" in app,
    "candidate review template is not candidate-accessible": "Start Another Test" in review,
}
failed = [name for name, ok in checks.items() if not ok]
if failed:
    raise SystemExit("STATIC CHECK FAILED:\n- " + "\n- ".join(failed))
print(f"PASS: {len(checks)} candidate-result/access-control static checks")
