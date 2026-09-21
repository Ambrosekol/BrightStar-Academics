# Tests

Two folders, each with a different job. Run `tests/current/` after every change; reach for
`tests/verification/` when a change touches something the contract tests cannot see from source.

## `tests/current/` — the contract suite

Fast, source-level checks of the architecture, security, assessment flow, roles and multi-tenancy
rules. It needs no database. Run it from the project folder:

```text
python tests/current/run_current.py
```

## `tests/verification/` — end-to-end checks against a real PostgreSQL server

Scripts that drive the real application over HTTP against throwaway databases (`bs_test_*`, dropped
afterwards) and check what actually happens, not just what the source says. Each is run by hand and
prints its own report. The project README's *Testing* section lists them and what each covers.
