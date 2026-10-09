"""A school's own email and SMS delivery, end to end.

Runs a real (tiny) SMTP server so messages actually travel, and checks what matters:

* a school's mail goes through the school's own server, not the platform's, and a school that has
  set up nothing falls back to the platform's account;
* a saved password or token is encrypted in the school's database, is never shown again, never
  reaches the audit log, and means nothing in another school or under another key;
* the server cannot be aimed at its own network: a school's mail host must be a public address,
  checked when saved and again at send time, on the standard ports only;
* a half-set-up school never quietly borrows the platform's account;
* only people with the delivery permission can change any of it.

Run:  python tests/verification/write_paths_delivery.py
"""
import base64
import io
import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_delivery_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup('delivery')
os.environ.update({
    "BRIGHTSTARS_TENANTS_DIR": os.path.join(TMP, "tenants"),
    "BRIGHTSTARS_PLATFORM_HOSTS": "platform.test",
    "BRIGHTSTARS_PORTAL_DOMAIN": "portal.test",
    "BRIGHTSTARS_REGISTRY_CACHE_SECONDS": "0",
    "BRIGHTSTARS_SECRET": "x" * 40,
    "BRIGHTSTARS_ENV": "development",
})
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
os.chdir(ROOT)

import sqlalchemy as sa  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

import app as A  # noqa: E402
from control_plane import provisioning as pv  # noqa: E402
from control_plane.context import tenant_context  # noqa: E402
from control_plane.registry import get_tenant, platform_session, to_info  # noqa: E402
from control_plane.routing import dispose_engines, engine_for  # noqa: E402
from core import delivery  # noqa: E402
from models import AuditLog  # noqa: E402

# app.py has just read the developer's .env, which may hold real mail credentials. A test of email
# must never be able to reach them, so start from a platform with no shared account at all.
for name in ("BRIGHTSTARS_SMTP_HOST", "BRIGHTSTARS_SMTP_USER", "BRIGHTSTARS_SMTP_FROM", "BRIGHTSTARS_SMTP_PASSWORD",
             "BRIGHTSTARS_SMS_API_TOKEN", "BRIGHTSTARS_SMS_SENDER_ID", "BRIGHTSTARS_SMS_GATEWAY", "BRIGHTSTARS_SMS_PAYER",
             "BRIGHTSTARS_DELIVERY_KEY",
             "BRIGHTSTARS_SMTP_PORT", "BRIGHTSTARS_SMTP_SSL", "BRIGHTSTARS_SMTP_STARTTLS", "BRIGHTSTARS_SMTP_STARTTLS"):
    os.environ.pop(name, None)

results = []
PL = "http://platform.test"


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


# ------------------------------------------------------------------ a tiny SMTP server
class Mailbox(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port=0):
        self.received = []
        super().__init__(("127.0.0.1", port), Mailbox.Session)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    class Session(socketserver.StreamRequestHandler):
        def handle(self):
            box, mail, to, body, data, auth = self.server, None, [], [], False, None
            self.wfile.write(b"220 fake ready\r\n")
            for raw in self.rfile:
                line = raw.decode(errors="replace").rstrip("\r\n")
                if data:
                    if line == ".":
                        box.received.append({"from": mail, "to": to, "auth": auth, "data": "\n".join(body)})
                        self.wfile.write(b"250 queued\r\n")
                        data, body = False, []
                    else:
                        body.append(line[1:] if line.startswith("..") else line)
                    continue
                cmd = line.upper()
                if cmd.startswith(("EHLO", "HELO")):
                    self.wfile.write(b"250-fake\r\n250 AUTH PLAIN\r\n")
                elif cmd.startswith("AUTH PLAIN"):
                    try:
                        _, user, password = base64.b64decode(line.split()[2]).decode().split("\0")
                        auth = (user, password)
                    except Exception:
                        auth = ("?", "?")
                    self.wfile.write(b"235 ok\r\n")
                elif cmd.startswith("MAIL FROM"):
                    mail = line.split(":", 1)[1].strip()
                    self.wfile.write(b"250 ok\r\n")
                elif cmd.startswith("RCPT TO"):
                    to.append(line.split(":", 1)[1].strip())
                    self.wfile.write(b"250 ok\r\n")
                elif cmd == "DATA":
                    data = True
                    self.wfile.write(b"354 go\r\n")
                elif cmd == "QUIT":
                    self.wfile.write(b"221 bye\r\n")
                    return
                else:
                    self.wfile.write(b"250 ok\r\n")


def port_free(port):
    import socket
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


SCHOOL_PORT = next((p for p in (2525, 25, 587, 465) if port_free(p)), None)
school_mail = Mailbox(SCHOOL_PORT) if SCHOOL_PORT else None
platform_mail = Mailbox(0)
check("a fake mail server is listening on one of the standard ports", school_mail is not None, str(SCHOOL_PORT))


# ------------------------------------------------------------------ two schools, and operators
def csrf(c, path, base):
    body = c.get(path, base_url=base).get_data(as_text=True)
    marker = 'name="_csrf_token" value="'
    start = body.index(marker) + len(marker)
    return body[start:body.index('"', start)]


def info_for(slug):
    with platform_session() as s:
        return to_info(get_tenant(s, slug))


pv.create_platform_admin("ops", "Ops", "a-long-platform-password")
console = A.app.test_client()
console.post("/platform/login", data={"username": "ops", "password": "a-long-platform-password",
                                      "_csrf_token": csrf(console, "/platform/login", PL)}, base_url=PL)
for code in ("alpha", "beta"):
    console.post("/platform/schools/new", data={
        "_csrf_token": csrf(console, "/platform/schools/new", PL), "name": code.title() + " School", "code": code,
        "school_brand_primary": "#0d2b52", "school_brand_accent": "#1674b9"}, base_url=PL,
        content_type="multipart/form-data")


def operator_in(code):
    url = f"http://{code}.portal.test"
    r = console.post(f"/platform/schools/{code}/enter", data={"_csrf_token": csrf(console, f"/platform/schools/{code}", PL)},
                     base_url=PL)
    c = A.app.test_client()
    c.get(r.headers["Location"][len(url):], base_url=url)
    c.get("/admin/workspace/school", base_url=url)
    return c, url


alpha, ALPHA = operator_in("alpha")
beta, BETA = operator_in("beta")
info_alpha, info_beta = info_for("alpha"), info_for("beta")


def in_school(info, fn):
    with A.app.app_context(), tenant_context(info):
        return fn()


def stored(info, key):
    with engine_for(info).connect() as conn:
        row = conn.execute(sa.text("SELECT setting_value FROM school_delivery_settings WHERE setting_key = :k"),
                           {"k": key}).first()
        return row[0] if row else None


def post(client, base, path, **fields):
    fields["_csrf_token"] = csrf(client, "/admin/school/delivery", base)
    return client.post(path, data=fields, base_url=base, follow_redirects=True)


def page(client, base):
    return client.get("/admin/school/delivery", base_url=base).get_data(as_text=True)


# ================================================================ nothing set up anywhere
html = page(alpha, ALPHA)
settings_page = alpha.get("/admin/settings", base_url=ALPHA).get_data(as_text=True)
check("the Settings page offers Email & SMS", "/admin/school/delivery" in settings_page and "Email &amp; SMS" in settings_page)
check("with no account anywhere, the page says email and SMS are not set up",
      "Email is not set up" in html and "SMS is not set up" in html)
check("…and nothing can be sent", in_school(info_alpha, delivery.email_settings) is None
      and in_school(info_alpha, delivery.sms_settings) is None)

# ================================================================ the platform's shared account
os.environ.update({"BRIGHTSTARS_SMTP_HOST": "127.0.0.1", "BRIGHTSTARS_SMTP_PORT": str(platform_mail.server_address[1]),
                   "BRIGHTSTARS_SMTP_USER": "platform-user", "BRIGHTSTARS_SMTP_PASSWORD": "platform-secret-pw",
                   "BRIGHTSTARS_SMTP_FROM": "noreply@platform.example", "BRIGHTSTARS_SMTP_STARTTLS": "0",
                   "BRIGHTSTARS_SMS_API_TOKEN": "platform-sms-token", "BRIGHTSTARS_SMS_SENDER_ID": "PLATFORM",
                   "BRIGHTSTARS_SMS_PAYER": "school"})
html = page(alpha, ALPHA)
check("a school with none of its own is told it uses the platform's shared account, and from which address",
      "Using the platform's shared account" in html and "noreply@platform.example" in html)
check("…without being shown the platform's server, username or password",
      "platform-secret-pw" not in html and "platform-user" not in html and 'value="127.0.0.1"' not in html)
settings = in_school(info_alpha, delivery.email_settings)
check("the school sends through the platform's account", settings and settings.source == "platform"
      and settings.sender == "noreply@platform.example")

# ================================================================ setting up its own
school_form = {"smtp_host": "127.0.0.1", "smtp_port": str(SCHOOL_PORT), "smtp_security": "none",
               "smtp_user": "alpha-mailer", "smtp_password": "alpha-super-secret", "smtp_from": "office@alpha.example"}
r = post(alpha, ALPHA, "/admin/school/delivery/email/save", **school_form)
text = r.get_data(as_text=True)
check("a school saves its own mail server", "email settings have been saved" in text, text[text.find("flash"):][:200])
check("…and is told it now sends from its own account", "Sending from your school&#39;s own account" in text
      or "Sending from your school's own account" in text)
check("the password is stored encrypted, not as typed",
      stored(info_alpha, "smtp_password").startswith("enc:v1:") and "alpha-super-secret" not in stored(info_alpha, "smtp_password"))
check("the page never shows the password again, only that one is saved",
      "alpha-super-secret" not in text and "A password is saved" in text)
with engine_for(info_alpha).connect() as conn:
    everything = " ".join(str(v) for row in conn.execute(sa.text("SELECT setting_value FROM school_delivery_settings")).all() for v in row)
    trail = " ".join(str(v) for row in conn.execute(sa.text("SELECT action, details FROM audit_logs")).all() for v in row)
check("…and it appears nowhere in the school's database in the clear, nor in its audit log",
      "alpha-super-secret" not in everything and "alpha-super-secret" not in trail)
check("the audit log records that email settings changed, naming the fields but not their values",
      "school_delivery_updated" in trail and "smtp_password" in trail and "alpha-mailer" not in trail)

status = in_school(info_alpha, delivery.status)
check("the settings summary contains no secret", "alpha-super-secret" not in str(status))

# ---- mail really goes through the school's server, with the school's credentials and sender
r = post(alpha, ALPHA, "/admin/school/delivery/email/test", to="parent@example.org")
text = r.get_data(as_text=True)
check("a test message can be sent", "A test message was sent to parent@example.org" in text, text[:0])
check("…and arrives at the school's own server, not the platform's",
      len(school_mail.received) == 1 and len(platform_mail.received) == 0)
got = school_mail.received[0] if school_mail.received else {}
check("…signed in with the school's credentials, from the school's address, to the recipient",
      got.get("auth") == ("alpha-mailer", "alpha-super-secret") and "office@alpha.example" in got.get("from", "")
      and "parent@example.org" in "".join(got.get("to", [])))
check("…and its subject names the school", "Alpha School" in got.get("data", ""))

# ---- the real senders use it
from email.message import EmailMessage  # noqa: E402

school_mail.received.clear()
ok, detail = in_school(info_alpha, lambda: A.sys.modules["core.notifications"]._notify_guardian_email("mum@example.org", "Hello", "Body"))
check("an alert to a guardian goes through the school's own server", ok and len(school_mail.received) == 1
      and len(platform_mail.received) == 0, str(detail))
auth_module = A.sys.modules["blueprints.auth.routes"]
with A.app.test_request_context("/", base_url=ALPHA):
    pass
in_school(info_alpha, lambda: auth_module._send_recovery_email("dad@example.org", "Dad", "http://x/reset"))
check("a password-recovery email does too", len(school_mail.received) == 2 and len(platform_mail.received) == 0)
check("a school with no account of its own still reaches the platform's",
      in_school(info_beta, lambda: A.sys.modules["core.notifications"]._notify_guardian_email("a@b.org", "s", "b"))[0]
      and len(platform_mail.received) == 1 and len(school_mail.received) == 2)

# ================================================================ keeping and replacing the password
post(alpha, ALPHA, "/admin/school/delivery/email/save", **{**school_form, "smtp_password": ""})
school_mail.received.clear()
post(alpha, ALPHA, "/admin/school/delivery/email/test", to="parent@example.org")
check("saving with the password left blank keeps the one already saved",
      school_mail.received and school_mail.received[0]["auth"] == ("alpha-mailer", "alpha-super-secret"))
post(alpha, ALPHA, "/admin/school/delivery/email/save", **{**school_form, "smtp_password": "a-new-password"})
school_mail.received.clear()
post(alpha, ALPHA, "/admin/school/delivery/email/test", to="parent@example.org")
check("typing a new one replaces it", school_mail.received and school_mail.received[0]["auth"][1] == "a-new-password")

# ================================================================ secrets belong to one school under one key
cipher_text = stored(info_alpha, "smtp_password")
check("a stored secret means nothing in another school",
      in_school(info_beta, lambda: delivery.decrypt(cipher_text)) == ""
      and in_school(info_alpha, lambda: delivery.decrypt(cipher_text)) == "a-new-password")
os.environ["BRIGHTSTARS_DELIVERY_KEY"] = "a-different-key-entirely"
check("nor under a different key", in_school(info_alpha, lambda: delivery.decrypt(cipher_text)) == "")
after_rotation = page(alpha, ALPHA)
check("an unreadable password is reported as not saved, so the admin is prompted to re-enter it",
      "A password is saved" not in after_rotation)
os.environ.pop("BRIGHTSTARS_DELIVERY_KEY")
check("…and it works again once the key is back", in_school(info_alpha, lambda: delivery.decrypt(cipher_text)) == "a-new-password")
check("a value that was never encrypted is never accepted as a secret", in_school(info_alpha, lambda: delivery.decrypt("plain-text")) == "")

# ================================================================ what may be saved
bad = {
    "an empty mail server": {**school_form, "smtp_host": ""},
    "a mail server with a path": {**school_form, "smtp_host": "mail.example.org/../x"},
    "a port that is not a mail port": {**school_form, "smtp_port": "5432"},
    "a port that is not a number": {**school_form, "smtp_port": "abc"},
    "an unknown security choice": {**school_form, "smtp_security": "magic"},
    "a sender that is not an address": {**school_form, "smtp_from": "not an address"},
}
for label, form in bad.items():
    r = post(alpha, ALPHA, "/admin/school/delivery/email/save", **form)
    check(f"{label} is refused", "saved" not in r.get_data(as_text=True).lower().split("delivery-state")[0]
          and stored(info_alpha, "smtp_host") == "127.0.0.1" and stored(info_alpha, "smtp_port") == str(SCHOOL_PORT),
          label)

# ================================================================ the server cannot be aimed at its own network
import socket  # noqa: E402

os.environ["BRIGHTSTARS_ENV"] = "production"
try:
    for host in ("127.0.0.1", "localhost", "10.0.0.5", "192.168.1.10", "169.254.169.254", "172.16.0.1"):
        try:
            in_school(info_alpha, lambda h=host: delivery.save_email({**school_form, "smtp_host": h}, None))
            refused = False
        except ValueError as exc:
            refused = "not a public internet address" in str(exc) or "could not be found" in str(exc)
        check(f"in production a mail server of {host} is refused", refused)

    real = socket.getaddrinfo
    socket.getaddrinfo = lambda *a, **k: [(socket.AF_INET6, 0, 0, "", ("::ffff:127.0.0.1", 0, 0, 0))]
    try:
        try:
            delivery.resolve_public("mapped.example")
            mapped = False
        except ValueError:
            mapped = True
    finally:
        socket.getaddrinfo = real
    check("…including a private address dressed up as an IPv6 one", mapped)

    socket.getaddrinfo = lambda *a, **k: [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0)), (socket.AF_INET, 0, 0, "", ("10.1.1.1", 0))]
    try:
        try:
            delivery.resolve_public("split.example")
            split = False
        except ValueError:
            split = True
    finally:
        socket.getaddrinfo = real
    check("…and a name that also points somewhere private", split)

    # A public name is accepted, and the connection is made to the address that was checked.
    connected = {}

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def connect(self, host, port):
            connected["to"] = (host, port)
            raise OSError("stop here")

        def close(self):
            pass

    socket.getaddrinfo = lambda *a, **k: [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))]
    real_smtp = delivery.smtplib.SMTP
    delivery.smtplib.SMTP = FakeSMTP
    try:
        settings = delivery.EmailSettings("mail.example.org", 587, "u", "p", "s@example.org", "starttls", "school")
        try:
            delivery._open_smtp(settings)
        except OSError:
            pass
    finally:
        delivery.smtplib.SMTP = real_smtp
        socket.getaddrinfo = real
    check("a school's mail is sent to the very address that was checked, not to a name looked up again",
          connected.get("to") == ("93.184.216.34", 587), str(connected))

    # A private host that got into the database anyway is still refused when the message is sent.
    with engine_for(info_alpha).begin() as conn:
        conn.execute(sa.text("UPDATE school_delivery_settings SET setting_value = '10.9.9.9' WHERE setting_key = 'smtp_host'"))
    blocked = in_school(info_alpha, lambda: delivery.check_email(delivery.email_settings(), "a@b.org", "Alpha"))
    check("…and a private host already in the database is refused at send time too", blocked[0] is False
          and "not a public internet address" in blocked[1], str(blocked))
    with engine_for(info_alpha).begin() as conn:
        conn.execute(sa.text("UPDATE school_delivery_settings SET setting_value = '127.0.0.1' WHERE setting_key = 'smtp_host'"))
finally:
    os.environ["BRIGHTSTARS_ENV"] = "development"

# ================================================================ never borrowing the platform's account
with engine_for(info_alpha).begin() as conn:
    conn.execute(sa.text("DELETE FROM school_delivery_settings WHERE setting_key IN ('smtp_user', 'smtp_from')"))
check("a school that entered a host but no sender does not quietly use the platform's account instead",
      in_school(info_alpha, delivery.email_settings) is None)
page_incomplete = page(alpha, ALPHA)
check("…and its page says the settings are incomplete", "Your settings are incomplete" in page_incomplete)

# ================================================================ removing its settings
post(alpha, ALPHA, "/admin/school/delivery/email/save", **school_form)
r = post(alpha, ALPHA, "/admin/school/delivery/email/clear")
check("a school can remove its own email settings", "Your own email settings were removed" in r.get_data(as_text=True)
      and stored(info_alpha, "smtp_host") is None and stored(info_alpha, "smtp_password") is None)
check("…and goes back to the platform's shared account",
      in_school(info_alpha, delivery.email_settings).source == "platform")
check("…which is recorded", "school_delivery_cleared" in " ".join(
    str(v) for row in engine_for(info_alpha).connect().execute(sa.text("SELECT action FROM audit_logs")).all() for v in row))

# ================================================================ SMS (BulkSMS Nigeria)
sms_form = {"sms_sender_id": "ALPHASCH", "sms_gateway": "direct-refund", "sms_api_token": "alpha-sms-token"}
check("payer 'school': a school with no account of its own cannot send, even though the platform has one set up",
      in_school(info_beta, delivery.sms_settings) is None and "SMS is not set up" in page(beta, BETA))
r = post(alpha, ALPHA, "/admin/school/delivery/sms/save", **sms_form)
text = r.get_data(as_text=True)
check("a school saves its own SMS account", "SMS settings have been saved" in text)
check("…its token is encrypted, and never shown again",
      stored(info_alpha, "sms_api_token").startswith("enc:v1:") and "alpha-sms-token" not in text and "A token is saved" in text)
check("…and it is told it texts from its own account, under its own sender name",
      ("Texting from your school&#39;s own account" in text or "Texting from your school's own account" in text) and "ALPHASCH" in text)
r = post(alpha, ALPHA, "/admin/school/delivery/sms/save", **{**sms_form, "sms_sender_id": "x"})
check("a sender name that is too short is refused", "3 to 11 letters and digits" in r.get_data(as_text=True))
r = post(alpha, ALPHA, "/admin/school/delivery/sms/save", **{**sms_form, "sms_sender_id": "Way Too Long Name"})
check("a sender name over 11 characters is refused", "3 to 11 letters and digits" in r.get_data(as_text=True))
r = post(alpha, ALPHA, "/admin/school/delivery/sms/save", **{**sms_form, "sms_gateway": "fast-lane"})
check("a route that does not exist is refused", "how the messages are routed" in r.get_data(as_text=True))
post(alpha, ALPHA, "/admin/school/delivery/sms/save", **{**sms_form, "sms_api_token": ""})
check("saving with the token left blank keeps it", in_school(info_alpha, delivery.sms_settings).token == "alpha-sms-token")
r = post(beta, BETA, "/admin/school/delivery/sms/save", sms_sender_id="BETASCH", sms_gateway="direct-refund", sms_api_token="")
check("a first save with no token is refused", "Enter the API token" in r.get_data(as_text=True))
check("…and leaves the school unable to send (the platform's account is not borrowed)", in_school(info_beta, delivery.sms_settings) is None)
post(beta, BETA, "/admin/school/delivery/sms/save", sms_sender_id="BETASCH", sms_gateway="direct-refund", sms_api_token="beta-sms-token")
post(beta, BETA, "/admin/school/delivery/sms/clear")
os.environ["BRIGHTSTARS_SMS_PAYER"] = "school"

seen = {}


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(request, timeout=0):
    seen["url"], seen["auth"], seen["method"] = request.full_url, request.headers.get("Authorization"), request.get_method()
    seen["body"] = json.loads(request.data) if request.data else None
    seen["calls"] = seen.get("calls", 0) + 1
    if seen.get("fail"):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b"{}"))
    if request.full_url.endswith("/balance"):
        return FakeResponse(b'{"status": "success", "data": {"universal_wallet": 5400.5, "sms_wallet": 1250}}')
    return FakeResponse(b'{"status": "success", "data": {"id": "MSG1", "cost": 2.5}}')


real_urlopen = urllib.request.urlopen
urllib.request.urlopen = fake_urlopen
try:
    r = post(alpha, ALPHA, "/admin/school/delivery/sms/check")
    check("the connection check calls BulkSMS Nigeria v2 with the school's own token",
          seen["auth"] == "Bearer alpha-sms-token" and seen["url"] == "https://www.bulksmsnigeria.com/api/v2/balance"
          and "Connected to BulkSMS Nigeria" in r.get_data(as_text=True), str(seen))
    seen["fail"] = True
    r = post(alpha, ALPHA, "/admin/school/delivery/sms/check")
    body = r.get_data(as_text=True)
    check("a rejected token is reported without echoing it", "token was not accepted" in body and "alpha-sms-token" not in body)
    seen["fail"] = False

    r = post(alpha, ALPHA, "/admin/school/delivery/sms/balance")
    body = r.get_data(as_text=True)
    check("a school that pays for its own SMS can see its balance (every wallet, under its own name)",
          "Balance:" in body and "5,400.50" in body and "1,250.00" in body, body[body.find("Balance"):][:200])
    check("…the balance button is offered only on a school's own account",
          "Check my SMS balance" in page(alpha, ALPHA) and "Check my SMS balance" not in page(beta, BETA))
    r = post(beta, BETA, "/admin/school/delivery/sms/balance")
    check("…and a school with no account of its own is told to connect one first", "not connected to your school" in r.get_data(as_text=True).replace("&#39;", "'"))

    ok, detail = in_school(info_alpha, lambda: A.sys.modules["core.notifications"]._notify_guardian_sms("08030000001", "hello"))
    check("a text is sent to BulkSMS Nigeria v2 with the school's own token, sender name and route, to a +234 number",
          ok and seen["auth"] == "Bearer alpha-sms-token" and seen["url"] == "https://www.bulksmsnigeria.com/api/v2/sms"
          and seen["method"] == "POST" and seen["body"] == {"from": "ALPHASCH", "to": "+2348030000001", "body": "hello", "gateway": "direct-refund"},
          str(seen))
    before = seen["calls"]
    ok, detail = in_school(info_alpha, lambda: A.sys.modules["core.notifications"]._notify_guardian_sms("123", "hello"))
    check("a phone number that is not a real number is not texted at all", not ok and seen["calls"] == before)

    # who pays: 'either' lets a school without its own account use the platform's
    os.environ["BRIGHTSTARS_SMS_PAYER"] = "either"
    ok, detail = in_school(info_beta, lambda: A.sys.modules["core.notifications"]._notify_guardian_sms("08030000001", "hello"))
    check("payer 'either': a school with no account of its own texts through the platform's (its token and sender name)",
          ok and seen["auth"] == "Bearer platform-sms-token" and seen["body"]["from"] == "PLATFORM", str(seen))
    ok, detail = in_school(info_alpha, lambda: A.sys.modules["core.notifications"]._notify_guardian_sms("08030000001", "hello"))
    check("…while a school that has its own still uses its own", ok and seen["auth"] == "Bearer alpha-sms-token")

    # who pays: 'platform' means the platform's account for everyone, and schools have nothing to set up
    os.environ["BRIGHTSTARS_SMS_PAYER"] = "platform"
    ok, detail = in_school(info_alpha, lambda: A.sys.modules["core.notifications"]._notify_guardian_sms("08030000001", "hello"))
    check("payer 'platform': even a school with its own saved account texts through the platform's",
          ok and seen["auth"] == "Bearer platform-sms-token" and seen["body"]["from"] == "PLATFORM", str(seen))
    html = page(alpha, ALPHA)
    check("…its page says SMS is provided by the platform, with nothing to enter and no balance to check",
          "SMS is provided by the platform" in html and "sms_api_token" not in html and "Check my SMS balance" not in html
          and "platform-sms-token" not in html)
    r = post(alpha, ALPHA, "/admin/school/delivery/sms/save", **sms_form)
    check("…saving SMS settings is refused", "provided by the platform" in r.get_data(as_text=True))
    calls = seen["calls"]
    r = post(alpha, ALPHA, "/admin/school/delivery/sms/balance")
    check("…and the school cannot see a balance (the platform's is the platform's business), and nothing was asked of BulkSMS",
          "no balance for the school to check" in r.get_data(as_text=True) and seen["calls"] == calls)
    status = in_school(info_alpha, delivery.status)["sms"]
    check("…the page is handed no token at all", "token" not in {k for k in status if k != "has_token"} and status["payer"] == "platform"
          and status["can_configure"] is False)
    os.environ["BRIGHTSTARS_SMS_PAYER"] = "school"
finally:
    urllib.request.urlopen = real_urlopen
r = post(alpha, ALPHA, "/admin/school/delivery/sms/clear")
check("removing SMS settings leaves the school unable to send under payer 'school'", in_school(info_alpha, delivery.sms_settings) is None
      and stored(info_alpha, "sms_api_token") is None)

# ================================================================ who may change any of it
with A.app.app_context(), tenant_context(info_alpha):
    ordinary = A.db.session.scalars(sa.select(A.AdminType).where(A.AdminType.name == "Ordinary Admin")).first()
    A.db.session.add(A.Admin(username="clerk", display_name="Clerk", password_hash=generate_password_hash("clerk-password-123"),
                             admin_type_id=ordinary.id, active=1, password_must_change=0, created_at="2026-01-01T00:00:00+00:00"))
    A.db.session.commit()
clerk = A.app.test_client()
# A real GET first lets the tenant boundary stamp this brand-new client's session with this
# school's own tenant_id; only then does a token planted straight into the session (rather than
# scraped from a page) survive to the next request instead of being wiped as a foreign cookie.
clerk.get("/login", base_url=ALPHA)
with clerk.session_transaction(base_url=ALPHA) as sess:
    sess["_csrf_token"] = "t" * 32
clerk.post("/login", data={"username": "clerk", "password": "clerk-password-123", "_csrf_token": "t" * 32}, base_url=ALPHA)
clerk.get("/admin/workspace/school", base_url=ALPHA)
with clerk.session_transaction(base_url=ALPHA) as sess:
    sess["_csrf_token"] = "t" * 32
check("an administrator without the permission cannot open the page",
      clerk.get("/admin/school/delivery", base_url=ALPHA).status_code != 200)
denied = []
for path, data in (("/admin/school/delivery/email/save", school_form), ("/admin/school/delivery/email/clear", {}),
                   ("/admin/school/delivery/email/test", {"to": "a@b.org"}), ("/admin/school/delivery/sms/save", sms_form),
                   ("/admin/school/delivery/sms/clear", {}), ("/admin/school/delivery/sms/check", {}),
                   ("/admin/school/delivery/sms/balance", {})):
    r = clerk.post(path, data={"_csrf_token": "t" * 32, **data}, base_url=ALPHA)
    denied.append(r.status_code)
check("…nor change, clear or test anything, even with a valid form token", all(s != 302 for s in denied), str(denied))
check("…and nothing was changed", stored(info_alpha, "smtp_host") is None and stored(info_alpha, "sms_api_token") is None)
check("the Settings page does not offer it to them", "/admin/school/delivery" not in clerk.get("/admin/settings", base_url=ALPHA).get_data(as_text=True))
check("nobody signed out can reach it", A.app.test_client().get("/admin/school/delivery", base_url=ALPHA).status_code == 302)
check("it does not exist on the platform host", console.get("/admin/school/delivery", base_url=PL).status_code == 404)
check("every change needs a form token",
      alpha.post("/admin/school/delivery/email/save", data=school_form, base_url=ALPHA).status_code == 403
      and stored(info_alpha, "smtp_host") is None)

# ================================================================ tests cannot be used to hammer other servers
post(alpha, ALPHA, "/admin/school/delivery/email/save", **school_form)
last = ""
for i in range(7):
    last = post(alpha, ALPHA, "/admin/school/delivery/email/test", to=f"p{i}@example.org").get_data(as_text=True)
check("test messages are rate-limited", "Too many tests just now" in last)

# ================================================================ one school's settings never reach another
check("another school is unaffected by all of this",
      stored(info_beta, "smtp_host") is None and in_school(info_beta, delivery.email_settings).source == "platform")

# ================================================================ rotating BRIGHTSTARS_DELIVERY_KEY (Hardening)
from core.secrets_rotation import rotate_one_tenant  # noqa: E402

before_password = in_school(info_alpha, delivery.email_settings).password
old_key = A.app.secret_key  # BRIGHTSTARS_DELIVERY_KEY was stripped from the environment at setup,
                            # so everything so far was actually encrypted under this fallback.
new_key = "a-brand-new-delivery-key-" + "z" * 20

with A.app.app_context(), tenant_context(info_alpha):
    rotated, skipped = rotate_one_tenant(old_key, new_key)
check("rotating with the real old key moves the school's own secret (the SMS token was cleared above)",
      rotated == 1 and skipped == [], (rotated, skipped))

os.environ["BRIGHTSTARS_DELIVERY_KEY"] = new_key
try:
    check("after rotation, the same plaintext still comes back - under the new key",
          in_school(info_alpha, delivery.email_settings).password == before_password, before_password)
finally:
    os.environ.pop("BRIGHTSTARS_DELIVERY_KEY", None)

os.environ["BRIGHTSTARS_DELIVERY_KEY"] = old_key
try:
    check("…and the old key can no longer read it at all",
          in_school(info_alpha, delivery.email_settings).password != before_password)
finally:
    os.environ.pop("BRIGHTSTARS_DELIVERY_KEY", None)

# A wrong old key must never blank a setting it cannot actually read.
with A.app.app_context(), tenant_context(info_alpha):
    rotated2, skipped2 = rotate_one_tenant("definitely-the-wrong-old-key", "yet-another-new-key")
check("a wrong old key rotates nothing and reports exactly what it could not read",
      rotated2 == 0 and skipped2 == ["smtp_password"], (rotated2, skipped2))
os.environ["BRIGHTSTARS_DELIVERY_KEY"] = new_key
try:
    check("…and the real setting, under its real key, is untouched by that failed attempt",
          in_school(info_alpha, delivery.email_settings).password == before_password)
finally:
    os.environ.pop("BRIGHTSTARS_DELIVERY_KEY", None)

school_mail and school_mail.shutdown()
platform_mail.shutdown()
dispose_engines()
DROP_TEST_DATABASES()
shutil.rmtree(TMP, ignore_errors=True)
print()
failed = [x for x in results if not x[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
