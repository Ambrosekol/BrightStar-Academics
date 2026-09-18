# Crainbow CBT — Phase 6H Hotfix Notes

## Fixed in this build

1. **CSRF form navigation regression**
   - `csrf_protect` previously validated the CSRF token even on GET requests.
   - This caused normal links such as Register Candidate, New Question Bank and Edit Question Bank to return `403 Forbidden — Invalid or missing CSRF token.` before their forms could render.
   - CSRF validation now runs only for state-changing methods (`POST`, `PUT`, `PATCH`, `DELETE`).
   - CSRF protection itself remains enabled; it has not been disabled.

2. **Admin dashboard metric animation**
   - The dashboard metric elements used `.metric-number`, while `admin.js` animates `.metric`.
   - Dashboard metrics now carry both classes so the existing count-up animation runs without changing the Results & Analytics implementation.

3. **Entrance Examination terminology**
   - Entrance-examination records and users are consistently presented as **Candidates**.
   - **Students** is reserved for enrolled learners in the School Portal.

4. **Workspace boundary**
   - The sidebar now shows only the active workspace's operational navigation.
   - Entrance Examination navigation disappears while working inside School Portal.
   - School Portal navigation disappears while working inside Entrance Examination.
   - Each workspace provides a direct switch back to the other workspace.

5. **School Portal foundation navigation**
   - Added visible School Portal areas for:
     - Students
     - Classes
     - Subjects
     - Assignments
     - Tests
     - Practice Tests
     - Examinations
     - Results & Records
   - These pages are intentionally foundation shells in this build; no fake school records or incomplete CRUD has been introduced.
   - The future **Student Login Portal** is explicitly reserved for a later build.

6. **Practice Tests visibility**
   - Added a visible Practice Tests entry in the administration navigation and a separate School Portal practice-test foundation page.
   - The existing entrance-examination Candidate Portal remains the currently functioning candidate-facing portal.

## Validation

- `python -m py_compile app.py` — PASS
- Jinja parsing of all HTML templates — PASS
- `verify_phase6h_admin_ux.py` — PASS
- JavaScript syntax check with Node — PASS

The packaging environment did not contain Flask and had no network access for installing it, so a live Flask/browser runtime test was not possible in this environment. The CSRF failure itself was confirmed statically from the decorator: the previous implementation rejected GET requests before protected forms could render.
