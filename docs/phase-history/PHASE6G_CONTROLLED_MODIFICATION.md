# Phase 6G — Controlled Candidate Session Modification

## Scope
This build adds only the candidate-session layer around the existing CBT engine.

### Added
- Administrator candidate registration.
- Automatic candidate ID and one-time generated password.
- Entry-level selection: Primary 6 → JSS 1 or JSS 3 → SS 1.
- Automatic assignment of exactly three papers for the selected entry level:
  - Mathematics
  - English
  - General Knowledge (shared by both entry levels)
- Candidate login and dashboard.
- Paper-by-paper sessions: the candidate may start the next paper whenever ready.
- Server-side one-attempt-per-paper enforcement.
- Administrator-only individual paper results.
- Administrator-only cumulative three-paper result.
- Printable cumulative result sheet and credential sheet.
- Existing one-time administrator retake permission remains in force.

### Deliberately preserved
- Existing JSON question-bank format.
- Existing examination activation controls.
- Existing timer/expiry model (`started_at` / `expires_at`).
- Existing answer saving.
- Existing grading and answer-key calculation.
- Existing individual result detail/printing.
- Existing regrade function.
- Existing admin authentication and question-bank CRUD.
- Candidate result withholding: the candidate dashboard never receives score, percentage, grade or answer-key data.

### Controlled fixes included in this session
- Candidate paper rows now carry the real `candidate_papers.id`, so the dashboard's Start/Resume action targets the correct assigned paper.
- Registration no longer permits arbitrary paper combinations. The selected entry level determines the required three-paper set.
- `/exam?q=` now safely handles a malformed/non-numeric question parameter instead of throwing a server error.

## Important deployment note
The build does **not** invent missing examination banks. Registration is blocked until the complete verified three-paper set for the selected entry level exists in `data/`.

For JSS 1, the required set is Year 7 Mathematics + Year 7 English + shared General Knowledge.
For SS 1, the required set is Year 10 Mathematics + Year 10 English + shared General Knowledge.
