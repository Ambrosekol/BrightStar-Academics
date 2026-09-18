# Phase 6K.4 — Targeted Portal & Assessment Fixes

This package is based on the uploaded Phase 6K.4 Student Account Layer and keeps the existing Phase6K2/6K4 architecture and candidate authentication boundary intact.

## Included targeted fixes

1. **Entrance Examination Candidate Dashboard**
   - Candidate paper cards now display human-readable subjects, e.g. `Paper 1: Mathematics`.
   - Internal question-bank IDs are no longer the primary label shown to candidates.

2. **Candidate Administration Profile Layout**
   - Corrected the unclosed profile-hero container that caused the candidate detail page sections to collapse into the wrong flex layout and produce the narrow/stacked presentation shown in the screenshot.

3. **Student Assignments**
   - Dashboard assignments are now compact clickable notifications/previews.
   - Clicking an assignment opens a dedicated full assignment view with complete instructions, subject, class and due date.

4. **Student Tests / Practice / Examinations**
   - Added authenticated, class/session-scoped student assessment listings.
   - Added a student assessment-taking experience for active assessments containing questions.
   - Added countdown timing based on the configured assessment duration.
   - Added assessment-level instructions and per-question `What to do` guidance.
   - Added question-image display using the existing `school_questions.image_path` field.
   - Submitted scores are recorded in `school_student_results` and shown to the student as released results.
   - Existing public Practice Test flow remains separate and unchanged.

5. **Student Dashboard Navigation**
   - Practice Tests now open the student's school assessment area instead of the public Practice Test page.
   - Tests and Examinations now have functional student-facing destinations.
   - Results/Performance links return to the student's own released-result area.

6. **School Portal Admin Dashboard**
   - The School Portal summary metrics now use the same polished metric-card visual language as the Entrance Examination Admin Dashboard, including colour accents and silhouettes.
   - Added Tests and Examinations metrics to make the school assessment workspace immediately visible.

## Intentionally preserved

- Candidate authentication and `candidate_id` session handling.
- Student authentication and `student_id` session handling.
- Existing database schema and Phase6K4 student-account columns.
- Entrance examination engine, candidate attempts, answers, scoring, rankings and result withholding.
- Existing question-bank architecture.
- Existing admin workspace boundaries and permissions.
- Existing student registration and photograph handling.
- Existing school assessment authoring, activation, question images and instruction fields.

## Important test-data observation

In the uploaded `cbt.db` at packaging time:

- Hannatu Attairu is enrolled in **JSS 2**, session **2026/2027**.
- The English Language school **Test** has 5 questions and is **Active**, so it is eligible to appear in the new student Test area.
- The school **Practice** assessment has 5 questions but is **inactive**.
- The school **Examination** has 5 questions but is **inactive / Draft/Closed**.

Therefore, after this fix, the Test should appear for Hannatu immediately. The Practice Test and Examination will appear after their respective assessment records are activated by the administrator.


## Phase 6K4 targeted follow-up — human-facing labels and governance cards
- Entrance-examination paper labels are consistently presented as `Paper N: Mathematics`, `Paper N: English`, or `Paper N: General Knowledge`; internal bank IDs remain implementation data.
- Candidate registration, candidate activity, result detail/print and entrance activity views no longer expose internal bank IDs as the paper/examination label.
- The Entrance Examination dashboard's metric-card visual language is reused on the School Portal and Administration governance overview without changing the underlying architecture.
- Activity & Security Log details are now rendered as plain-language administrative summaries; raw JSON payloads and technical permission/endpoint arguments are intentionally hidden from the UI.
