# Phase 3B — Student Assessment Corrections & UI Parity

Date: 31 August 2026

## Scope

This corrective build follows the live regression findings reported during Phase 3 testing.

### Fixed

1. **Student Test/Examination submission failure**
   - Corrected the result INSERT query to use `school_assessments.id` rather than the nonexistent `school_assessments.assessment_id`.
   - Added transaction rollback/finally-close handling so a grading failure cannot leave a SQLite write transaction open and cause a subsequent `database is locked` failure.

2. **Student assessment interaction parity**
   - Student Tests, Examinations and Practice assessments now use the same structural interaction model as the Entrance Examination:
     - one question at a time
     - question palette on the left on desktop
     - current/answered question states
     - Previous
     - Save & Next
     - Submit on the final question
     - server-created attempt
     - server-authoritative expiry
   - Opening an assessment still does **not** start the timer. The timer starts only after Start is pressed.

3. **Assessment question authoring parity**
   - School Portal questions now have an Edit workflow.
   - Edit supports question text, instruction, all four options, correct option, points and image replacement/removal.
   - Added question reordering with persisted order.
   - Existing Delete workflow remains protected by CSRF and permissions.

4. **Image uploads**
   - Corrected the binary signature validation bug introduced in the earlier hardening patch.
   - PNG, JPG, JPEG, GIF and WEBP are accepted when their actual file signatures match.
   - File size limits and server-side signature validation remain enabled.
   - File chooser accept attributes were updated consistently across candidate, student and question upload forms.

5. **Results & Analytics presentation**
   - Reworked the page to use the same dashboard visual hierarchy as:
     - Entrance Examination / Overview
     - Administration / People, access & governance
   - Metrics now use the shared dashboard metric-card pattern.
   - Filters, subject navigation and subject result sections are organized as dashboard sections.

## Regression safety

Current contract suite:

**20 passed, 0 failed**

Python syntax validation passes for the modified application and migration runner.

A SQLite validation also confirmed that the corrected result INSERT successfully records a completed student assessment result.

## Runtime note

The development validation environment used for this package does not have Flask installed, so live Flask HTTP integration tests were not executed in this environment. The actual school-machine runtime test should therefore be performed after replacing the current development folder with this build and installing the project's requirements.

## Important baseline rule

Do not modify the protected Phase 6K4/Phase 3 baseline directly. This package is the corrected working branch and should become the new candidate baseline only after the live regression checklist passes.
