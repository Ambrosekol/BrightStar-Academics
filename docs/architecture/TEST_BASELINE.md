# Crainbow Test Baseline

## Authoritative direction
`tests/current/` is the new home for current, architecture-aware regression contracts.

`tests/legacy/` contains historical Phase6K-era verification scripts retained for reference. They are not treated as production gates until reviewed and either migrated or retired.

## Current checks established
- Python syntax parses.
- Core student/security/domain markers remain present.
- Student assessment route remains identified as a deliberate refactor target.
- CSRF protection on the student assessment route is recorded.
- Assessment UX/timing requirements are recorded as locked acceptance criteria.
- Answer-key non-disclosure is recorded as a production security contract.

## Known baseline legacy-test issues
The original Phase6K4 archive includes phase-specific verification scripts. Some are historical and fail because they assert earlier phase manifests or schemas. One bank validator also encounters a current bank schema/content mismatch. These are recorded findings, not reasons to modify the approved baseline blindly.

## Required future gates
Every architectural phase must run the current suite before and after the change. Production additionally requires functional, authorization, examination-integrity, database, load, recovery and deployment tests.
