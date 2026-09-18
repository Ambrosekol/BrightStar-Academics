# Crainbow CBT — Phase 6K.2 Frontend Analytics & Administration Fixes

## Purpose

This additive pass keeps the Phase 6K.1 information architecture and working backend while refining the administrative views requested after visual QA.

## Changes

### Candidate profiles
- Candidate detail and performance pages now identify Paper 1, Paper 2 and Paper 3 by **subject** (Mathematics, English, General Knowledge) instead of repeating the examination title.
- Existing cumulative result, individual result, credential and parent-result actions remain available.

### Rankings
- Rankings are now **one row per candidate**, not one row per examination paper.
- Each row contains Mathematics, English, General Knowledge, cumulative score/percentage and completion status.
- Only candidates who have completed all three assigned papers receive an official rank.
- Class/entry-level filtering and candidate search are available.
- The previous duplicate-rank display caused by ranking individual papers is removed.

### Results & Analytics
- Results are now organised into three subject sections: Mathematics, English and General Knowledge.
- Each subject contains the registered candidates once, with score, percentage, status and grade.
- Subject-level average, pass count and pass rate are displayed.
- Entry-level and candidate search filters remain available.

### Entrance Examination dashboard
- The five headline totals are preserved: question banks, questions, attempts, completed and candidates.
- A new Performance Overview adds:
  - Subject performance chart
  - Class/entry-level performance comparison
  - Top completed candidates
- The dashboard therefore functions as an overview rather than another long record list.

### Navigation / school portal
- The duplicate **SCHOOL PORTAL** sidebar label was removed.
- Student navigation remains under the School Portal workspace and the existing class-first student register is preserved.
- The gateway remains exactly two public choices: Take a Practice Test and Log in to My Account.
- Question Banks remain class-first with JSS 1 and SSS 1 segmentation before the bank library opens.

## Preservation

- Entrance examination engine, candidate registration, paper assignment, answer storage and grading logic were not intentionally rewritten.
- Question-bank JSON files were not modified.
- Existing school portal data structures and workflows remain in place.

## Validation

- `app.py` passes Python syntax compilation.
- All Jinja templates parse successfully.
- URL endpoint references were checked statically against Flask view-function names.
- The packaged project should be runtime-tested on the Windows/Python environment used by the school because Flask is not installed in the analysis container.
