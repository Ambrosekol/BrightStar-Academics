# Crainbow CBT — Phase 6J School Portal Functional Foundation

## Scope
This build turns the previously placeholder School Portal modules into working administrative foundations while preserving the existing entrance-examination candidate flow.

### Workspace boundary
- Administrators, including Super Admin, must select a workspace after login.
- The workspace chooser is intentionally sidebar-free.
- Entrance Examination navigation is not rendered inside School Portal.
- School Portal navigation is not rendered inside Entrance Examination.
- Exit Workspace returns to the neutral Workspace Home and clears the selected workspace.
- The global Back button uses browser history for one-step backward navigation, with a safe workspace fallback.

### School terminology
- Entrance examination participants remain **candidates**.
- Enrolled people in the School Portal are **students**.

### School modules now wired
- Students: register, edit, class enrolment, deactivate.
- Classes: supported class structure with optional Primary 5; Super Admin can activate/deactivate.
- Subjects: create, edit, attach to classes, deactivate, Super Admin lock/unlock per class.
- Assignments: create/edit/delete, class + subject + individual student targeting.
- Tests: create, edit, load/delete objective questions, activate/close.
- Practice Tests: separate school practice assessment management foundation.
- Examinations: create, edit, load/delete objective questions, activate/close.
- Results & Records: connected academic-record viewing foundation.

### Academic structure
Supported classes:
- Primary 1, 2, 3, 4, 5, 6
- JSS 1, 2, 3
- SSS 1, 2, 3

Primary 5 is included in the data model as an optional class and is inactive by default so the school can activate it if needed.

### Subject governance
- Subjects are not hard-coded.
- Teachers can create subjects for authorised classes.
- Primary class teachers can manage the subjects offered by their class.
- College subject teachers can be constrained by subject scope.
- A locked class-subject connection cannot be removed by an ordinary administrator.
- Lock/unlock is a Super Admin control.

### Assessment questions
School tests, practice tests and examinations have their own database-backed question records. Questions are loaded after the assessment is created, rather than pretending a module is functional while only displaying a placeholder.

## Important future phase
Student login remains a separate future build. The existing entrance-examination candidate portal remains untouched by this school foundation.
