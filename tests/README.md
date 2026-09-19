# Crainbow Test Structure

There are three subdirectories here, each with a different job. Run `tests/current/`
after every change; reach for `tests/verification/` when a change touches something
`tests/current/` can't see from source alone; leave `tests/legacy/` alone.

## `tests/current/` — the authoritative contract suite

Fast, source-level architecture/security/assessment/governance contract checks. These are
the tests that evolve with the production system, and the ones to run after any change.

Run from the `crainbow` directory:

```text
python tests/current/run_current.py
```

## `tests/verification/` — end-to-end checks against a real running app

Deeper scripts that actually drive the application (a real Flask test client or, for
`smoke_entrypoint.py`, a real `python app.py` process) against a throwaway copy of the
database, rather than reading source text. Each answers a question `tests/current/` can't:
does this route actually behave correctly when called, not just does the right code exist.
Each script is run by hand and prints its own report. See `tests/verification/README.md`
for what each one covers and why it exists.

## `tests/legacy/` — retained historical scripts

Historical phase-specific verification scripts kept for forensic/reference purposes. They
are **not** part of the authoritative suite and are not run as part of normal development —
leave them as-is unless you are specifically investigating project history.
