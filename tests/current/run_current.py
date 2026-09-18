"""Minimal current regression runner; uses only the Python standard library."""
import importlib.util
from pathlib import Path
import traceback

ROOT = Path(__file__).resolve().parents[2]
TEST_DIR = ROOT / "tests" / "current"
failures = []

for path in sorted(TEST_DIR.glob("test_*.py")):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        for name in dir(mod):
            if name.startswith("test_"):
                fn = getattr(mod, name)
                if callable(fn):
                    fn()
        print(f"PASS {path.name}")
    except Exception as exc:
        failures.append((path.name, str(exc)))
        print(f"FAIL {path.name}: {exc}")
        traceback.print_exc()

print(f"Current contract tests: {len(list(TEST_DIR.glob('test_*.py'))) - len(failures)} passed, {len(failures)} failed")
raise SystemExit(1 if failures else 0)
