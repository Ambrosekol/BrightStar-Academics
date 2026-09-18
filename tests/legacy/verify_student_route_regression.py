from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
app = (ROOT / "app.py").read_text(encoding="utf-8")
tpl = (ROOT / "templates" / "school_students.html").read_text(encoding="utf-8")

assert not re.search(r"render_template\([^\n]*session=session_row", app), "A school route still shadows Flask session."
assert "school_session=" in app, "school_session context is missing."
assert "{{school_session.name if school_session else 'Not configured'}}" in tpl, "Student list template is not using school_session."
print("PASS: student route session-boundary regression checks")
