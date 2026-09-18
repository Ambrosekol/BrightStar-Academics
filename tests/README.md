# Crainbow Test Structure

## Current authoritative tests
`tests/current/` contains the current architecture/security/assessment contract checks. These are the tests that should evolve with the production system.

Run from the `crainbow` directory:

```text
python tests/current/run_current.py
```

## Legacy tests
`tests/legacy/` contains historical phase-specific verification scripts copied from the approved Phase6K4 baseline. They are retained for forensic/reference purposes but are **not** the authoritative production test suite.

Before production, each useful legacy check must either be rewritten into `tests/current/` or explicitly retired with a documented reason.
