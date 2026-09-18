from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[2]
app = (ROOT / "app.py").read_text(encoding="utf-8")
base = (ROOT / "templates" / "admin_base.html").read_text(encoding="utf-8")
messages = (ROOT / "templates" / "admin_messages.html").read_text(encoding="utf-8")
css = (ROOT / "static" / "admin.css").read_text(encoding="utf-8")

ast.parse(app)
assert "def _unread_admin_message_summaries" in app
assert "sender_name" in app
assert "admin_unread_message_summaries" in base
assert "New from {{ admin_unread_message_summaries[0].sender_name }}" in base
assert "New message from {{ admin_unread_message_summaries[0].sender_name }}" in base
assert "admin_unread_message_summaries" in messages
assert "nav-message-sender" in css
assert "6K-message-notification-sender" in base
print("Message notification sender regression: PASS")
