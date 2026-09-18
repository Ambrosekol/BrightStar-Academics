# Crainbow CBT — Phase 6K.3 Visual, Media & Result Sharing Fixes

This is an additive follow-up to Phase 6K.2. The existing Phase 6K2 information architecture, entrance examination engine, ranking logic, results analytics and school-portal structure are preserved.

## Included

- Dashboard upgraded as a true Entrance Examination Overview with colour-coded subject, entry-level and leading-candidate performance visuals.
- Performance colours use the agreed interpretation: green = performing well (70%+), yellow = fair/not bad (50–69%), red = below 50%.
- Dashboard headline metrics remain: Question Banks, Questions, Attempts, Completed and Candidates.
- Candidate performance paper labels are subject-first (for example: `Paper 1 · Mathematics`) rather than repeating the examination title.
- Rankings remains one row per fully completed candidate with Mathematics, English, General Knowledge, overall score/percentage and status; spacing and visual hierarchy were improved.
- Results & Analytics remains subject-separated, with one registered candidate row per subject and subject averages/pass metrics.
- Candidate photos can be uploaded during entrance registration and are shown in candidate records/analytics where available.
- School student photos can be uploaded during student registration and shown in the student register/profile.
- Parent/guardian email was added to entrance-candidate registration for result sharing.
- Result sharing now offers WhatsApp and Email actions from the candidate record/performance views. WhatsApp opens the recipient conversation with a prepared result message; Email opens a pre-addressed, pre-filled email using the registered guardian email.
- Entrance question authoring now supports an optional per-question instruction and image upload.
- School Portal assessment questions now support optional per-question instructions and images.
- Candidate examination view now exposes a clear Instructions panel and optional question-specific instruction/image.
- Public landing page, login, sidebar, candidate portal, student portal and examination header now use the existing Crainbow school logo where appropriate.
- The duplicate School Portal label was not present in the current Phase 6K2 navigation source; the sidebar remains a single School Portal section label.

## Data preservation

Existing question-bank JSON structures remain compatible. New entrance question metadata is additive (`instruction`, optional `image_path`). Existing records are not rewritten merely to add the feature.

Existing student/candidate databases are migrated at application initialisation with nullable columns, so previous records remain valid.

## Validation performed in the build environment

- `app.py` passes Python syntax compilation.
- All 58 Jinja templates parse successfully.
- New routes, fields and media helpers were checked statically.

The analysis container does not have Flask installed and cannot access PyPI, so a live Flask/browser runtime test could not be performed here. The packaged project should therefore be started on the same Windows/Python environment used by the school and the listed flows should be smoke-tested before production use.
