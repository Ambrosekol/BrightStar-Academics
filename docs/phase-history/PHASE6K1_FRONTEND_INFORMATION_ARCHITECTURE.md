# Crainbow CBT Phase 6K.1 — Frontend Information Architecture

This build preserves the working backend and reorganizes the frontend around a person-first and class-first information architecture.

## Gateway
- `/` now presents exactly two actions:
  - Take a Practice Test
  - Log in to My Account
- Entrance Examination is no longer a third landing-page destination.
- Existing authenticated users see a context-aware Continue to My Account action.

## Unified login
- `/login` remains the single account login surface.
- Admin, candidate and student credentials are resolved by the backend and redirected to the appropriate destination.

## Entrance Examination
### Candidate Register
- First screen presents two entry-level segments:
  - JSS 1
  - SSS 1
- Selecting a level opens only that level's candidate register.
- Search supports candidate name, candidate ID, school and parent/guardian.
- The list is person-centric rather than paper-centric.
- Individual paper progress is moved into the person's profile/performance area.

### Candidate Profile
- View opens a complete person record.
- Sections include candidate information, parent/guardian contact, examination overview and recent activity.
- Actions include Issue New Password, Print Credentials, Print Cumulative Result, Check Performance and Send Result to Parent.
- Send Result to Parent opens a WhatsApp message addressed to the stored primary/alternative mobile number.

### Candidate Performance
- Dedicated performance view contains the cumulative metrics, all three papers, paper status, scores, percentages and links to individual result details.
- Candidate information remains visible beside the academic record.

### Question Banks
- First screen presents JSS 1 and SSS 1 bank segments.
- Selecting a level opens only the relevant banks.
- Search remains available inside the selected segment.

## School Portal
- Students are class-first: select a class before opening the student register.
- Student View opens a complete student profile with personal information, guardian information, enrolment history, academic results and assignments.
- Assignments, Tests, Practice Tests, Examinations and Results & Records now use the same class-first separation pattern.

## Validation performed
- `app.py` passes Python syntax compilation.
- All modified Jinja templates parse successfully.
- URL endpoint references introduced by this build resolve to defined Flask view functions.
