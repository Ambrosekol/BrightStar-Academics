"""End-to-end candidate submission/result/access-control test.

Run on a machine with the project's requirements installed:
    python security/test_candidate_result_access.py

The test uses a temporary SQLite database and never modifies the production cbt.db.
"""
from __future__ import annotations

import os
import tempfile

import app as cbt


BANK_ID = "jss3_english_2026"
CANDIDATE = "Access Control Test Candidate"


def main():
    if BANK_ID not in cbt.load_banks():
        raise SystemExit(f"Missing test bank: {BANK_ID}")

    fd, db_path = tempfile.mkstemp(prefix="crainbow_access_test_", suffix=".db")
    os.close(fd)
    os.unlink(db_path)
    cbt.DB = db_path
    cbt.init_db()

    candidate = cbt.app.test_client()
    admin = cbt.app.test_client()

    # 1. Candidate starts and submits an exam.
    r = candidate.post("/login", data={"candidate": CANDIDATE, "bank_id": BANK_ID})
    assert r.status_code == 302 and "/exam" in r.headers["Location"]
    r = candidate.post("/submit")
    assert r.status_code == 302 and "/result" in r.headers["Location"]

    # 2. Candidate result page is strictly score-free.
    r = candidate.get("/result")
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    required = [
        "Examination Submitted Successfully",
        "Your examination has been submitted successfully.",
        "Your responses have been securely recorded.",
        "Your result will be released by the school.",
    ]
    forbidden = [
        "Final Score", "Percentage", "Review Attempt", "Start Another Test",
        "Correct answer", "Incorrect", "%", "score", "percentage",
    ]
    for text in required:
        assert text in body, f"Missing candidate message: {text}"
    for text in forbidden:
        assert text not in body, f"Candidate result leaked restricted content: {text}"

    # 3. Direct candidate access to review/exam after submission is blocked.
    r = candidate.get("/review", follow_redirects=False)
    assert r.status_code == 302 and "/result" in r.headers["Location"]
    r = candidate.get("/exam?q=1", follow_redirects=False)
    assert r.status_code == 302 and "/result" in r.headers["Location"]

    # 4. Same candidate cannot retake without admin permission.
    r = candidate.post("/login", data={"candidate": CANDIDATE, "bank_id": BANK_ID})
    assert r.status_code == 200
    assert "already completed this examination" in r.get_data(as_text=True)

    with candidate.session_transaction() as sess:
        attempt_id = sess["attempt_id"]

    # 5. Admin can see the score and print the detailed script.
    r = admin.post("/admin/login", data={"password": "admin123"})
    assert r.status_code == 302
    r = admin.get(f"/admin/results/{attempt_id}")
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and "Correct Answer" in body and "Score" in body
    r = admin.get(f"/admin/results/{attempt_id}/print")
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and "Candidate Answer" in body and "Correct Answer" in body

    # 6. Admin grants one retake; it is consumed and cannot be reused.
    r = admin.post(f"/admin/results/{attempt_id}/grant-retake", follow_redirects=True)
    assert r.status_code == 200 and "One-time retake access granted" in r.get_data(as_text=True)
    r = candidate.post("/login", data={"candidate": CANDIDATE, "bank_id": BANK_ID})
    assert r.status_code == 302 and "/exam" in r.headers["Location"]
    r = candidate.post("/submit")
    assert r.status_code == 302 and "/result" in r.headers["Location"]
    r = candidate.post("/login", data={"candidate": CANDIDATE, "bank_id": BANK_ID})
    assert r.status_code == 200 and "already completed this examination" in r.get_data(as_text=True)

    os.remove(db_path)
    print("PASS: candidate submission confirmation is score-free")
    print("PASS: candidate cannot open review/exam after submission")
    print("PASS: completed examination cannot be retaken without admin grant")
    print("PASS: admin can view and print the candidate script/result")
    print("PASS: one-time admin retake grant is consumed")


if __name__ == "__main__":
    main()
