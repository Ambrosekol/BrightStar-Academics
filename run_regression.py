from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parent
result = subprocess.run([sys.executable, str(root / "tests" / "current" / "run_current.py")], cwd=root)
raise SystemExit(result.returncode)
