# Crainbow CBT — Phase 6H Administration Experience & Governance

## Purpose

This is an additive administration UX/governance layer built on the stabilized Phase 6G + Phase A security foundation. The entrance-examination engine, candidate session engine, grading, answer storage, question-bank JSON format and existing database structures are preserved.

## What was redesigned

### 1. Workspace-first sign-in
Administrators now land on **Workspace Home** instead of being dropped directly into a mixed dashboard.

They choose:
- **Entrance Examination** — examination operations
- **School Portal** — a separate school-administration boundary reserved for school-wide modules

The direct `/admin` dashboard remains available for compatibility.

### 2. Professional navigation
The sidebar is now organised by purpose:
- Workspace
- Entrance Examination
- School Portal
- Administration

Operational labels are human-facing. Examples:
- Candidates → **Students**
- Admin Types → **Staff Roles**
- Audit Logs → **Activity & Security Log**
- Scopes → **Access Boundaries**
- Controls & Alerts → governance centre

### 3. Job-based staff roles
The account-creation experience no longer asks staff to understand a 30+ item permission catalogue.

The build seeds practical role bundles:
- Entrance Examination Manager
- Question Bank Manager
- Student Records Officer
- Results & Analytics Officer
- Examination Supervisor
- Read-Only Academic Viewer

The underlying permission catalogue remains in the database as a security mechanism. It is not the primary staff workflow.

### 4. Optional access boundaries
The previous **Scope Type / Scope Value** controls were technically valid but exposed too early and in backend language.

They are now presented as an optional advanced restriction:
> Restrict this person to a specific work area

The default is the whole entrance-examination workspace. A boundary is only needed when a staff member must be restricted to a particular class, subject, session or question bank.

### 5. Super Admin notifications
A new notification system records important administrator activity and sends alerts to active Super Admin accounts.

Examples include:
- administrator creation/status changes
- role changes
- question-bank creation/updates
- important question changes
- examination activation/deactivation
- retake grants
- result regrades
- credential resets

### 6. Super Admin governance controls
A new **Controls & Alerts** centre provides:
- important activity notifications
- review queue
- protected question-bank controls
- lock/unlock capability for examination resources

When a question bank is locked, staff may still view it where their role permits, but mutation routes are blocked until a Super Admin unlocks it.

### 7. Reduced backend leakage
The primary staff interface no longer exposes technical identifiers, raw permission codes, scope terminology or implementation details as normal workflow labels.

The security reference pages remain available for authorised technical review, but they are no longer the main navigation path.

### 8. Safer state changes
Administrative state-changing forms now use the existing session-bound CSRF mechanism where applicable, including newly protected account, bank, question, examination and result actions.

## Preservation rules

No question-bank JSON files were intentionally changed.

No candidate-session logic was intentionally rewritten.

No grading algorithm was intentionally rewritten.

No existing entrance-examination tables were replaced.

Database changes are additive and use `CREATE TABLE IF NOT EXISTS` / targeted migration behaviour.

## Validation performed in the build environment

- Python AST compilation of `app.py`
- Jinja parsing of all active HTML templates
- Existing `verify_phase6g.py` static verification
- New Phase 6H static UX/governance verification

## Important deployment note

The local analysis environment used for this packaging pass did not contain Flask, so the complete Flask runtime could not be booted here. Runtime verification should therefore be performed on the same Windows/Python environment used to run Crainbow CBT before replacing the live Phase 6G folder.
