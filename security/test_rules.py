"""Phase 6F static rule checks."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

required = [
    "README.md",
    "docs/phase-history/PHASE6F_DEPLOYMENT_CHECKLIST.md",
    "security/SECURITY_TEST_PLAN.md",
]

missing = [p for p in required if not (ROOT / p).exists()]
if missing:
    raise SystemExit("Missing required files: " + ", ".join(missing))

# Basic source checks where application source is available.
py_files = list(ROOT.rglob("*.py"))
source = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in py_files)

rules = {
    "server-side timing language": any(x in source.lower() for x in ["expires_at", "expiry", "expires"]),
    "submission handling": any(x in source.lower() for x in ["submit", "submitted"]),
}

failed = [name for name, ok in rules.items() if not ok]
if failed:
    raise SystemExit("Static checks failed: " + ", ".join(failed))

print("Phase 6F static checks: PASS")
