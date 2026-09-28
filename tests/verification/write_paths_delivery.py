"""A school's own email and WhatsApp delivery, end to end.

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
             "BRIGHTSTARS_WHATSAPP_TOKEN", "BRIGHTSTARS_WHATSAPP_PHONE_NUMBER_ID", "BRIGHTSTARS_DELIVERY_KEY",
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
check("the school's menu offers Email & WhatsApp", "/admin/school/delivery" in alpha.get("/admin/school", base_url=ALPHA).get_data(as_text=True))
check("with no account anywhere, the page says email and WhatsApp are not set up",
      "Email is not set up" in html and "WhatsApp is not set up" in html)
check("…and nothing can be sent", in_school(info_alpha, delivery.email_settings) is None
      and in_school(info_alpha, delivery.whatsapp_settings) is None)

# ================================================================ the platform's shared account
os.environ.update({"BRIGHTSTARS_SMTP_HOST": "127.0.0.1", "BRIGHTSTARS_SMTP_PORT": str(platform_mail.server_address[1]),
                   "BRIGHTSTARS_SMTP_USER": "platform-user", "BRIGHTSTARS_SMTP_PASSWORD": "platform-secret-pw",
                   "BRIGHTSTARS_SMTP_FROM": "noreply@platform.example", "BRIGHTSTARS_SMTP_STARTTLS": "0",
                   "BRIGHTSTARS_WHATSAPP_TOKEN": "platform-wa-token", "BRIGHTSTARS_WHATSAPP_PHONE_NUMBER_ID": "111222333"})
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

# ================================================================ WhatsApp
wa_form = {"whatsapp_phone_number_id": "987654321", "whatsapp_graph_version": "v23.0", "whatsapp_token": "alpha-wa-token"}
r = post(alpha, ALPHA, "/admin/school/delivery/whatsapp/save", **wa_form)
text = r.get_data(as_text=True)
check("a school saves its own WhatsApp account", "WhatsApp settings have been saved" in text)
check("…its token is encrypted, and never shown again",
      stored(info_alpha, "whatsapp_token").startswith("enc:v1:") and "alpha-wa-token" not in text
      and "A token is saved" in text)
r = post(alpha, ALPHA, "/admin/school/delivery/whatsapp/save", **{**wa_form, "whatsapp_phone_number_id": "12ab"})
check("a phone number ID that is not digits is refused", "digits only" in r.get_data(as_text=True))
r = post(alpha, ALPHA, "/admin/school/delivery/whatsapp/save", **{**wa_form, "whatsapp_graph_version": "latest"})
check("an API version that is not a version is refused", "looks like v23.0" in r.get_data(as_text=True))
post(alpha, ALPHA, "/admin/school/delivery/whatsapp/save", **{**wa_form, "whatsapp_token": ""})
check("saving with the token left blank keeps it", in_school(info_alpha, delivery.whatsapp_settings).token == "alpha-wa-token")
r = post(beta, BETA, "/admin/school/delivery/whatsapp/save", whatsapp_phone_number_id="555666777",
         whatsapp_graph_version="v23.0", whatsapp_token="")
check("a first save with no token is refused", "Enter the access token" in r.get_data(as_text=True))
check("…and leaves the school on the platform's account", in_school(info_beta, delivery.whatsapp_settings).source == "platform")

seen = {}


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(request, timeout=0):
    seen["url"], seen["auth"] = request.full_url, request.headers.get("Authorization")
    if seen.get("fail"):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)
    return FakeResponse(b'{"verified_name": "Alpha School", "display_phone_number": "+234 800 000 0000", "messages": [{"id": "wamid.1"}]}')


real_urlopen = urllib.request.urlopen
urllib.request.urlopen = fake_urlopen
try:
    r = post(alpha, ALPHA, "/admin/school/delivery/whatsapp/check")
    check("the connection check asks WhatsApp who the credentials belong to, with the school's token",
          seen["auth"] == "Bearer alpha-wa-token" and "987654321" in seen["url"]
          and "Connected to WhatsApp: Alpha School" in r.get_data(as_text=True), str(seen))
    seen["fail"] = True
    r = post(alpha, ALPHA, "/admin/school/delivery/whatsapp/check")
    body = r.get_data(as_text=True)
    check("a rejected token is reported without echoing it", "did not accept these details" in body and "alpha-wa-token" not in body)
    seen["fail"] = False
    ok, detail = in_school(info_alpha, lambda: A.sys.modules["core.notifications"]._notify_guardian_whatsapp("08030000001", "hello"))
    check("a WhatsApp alert is sent with the school's own token, not the platform's",
          ok and seen["auth"] == "Bearer alpha-wa-token" and "987654321" in seen["url"], str(seen))
    ok, detail = in_school(info_beta, lambda: A.sys.modules["core.notifications"]._notify_guardian_whatsapp("08030000001", "hello"))
    check("a school with no account of its own uses the platform's",
          ok and seen["auth"] == "Bearer platform-wa-token" and "111222333" in seen["url"], str(seen))
finally:
    urllib.request.urlopen = real_urlopen
r = post(alpha, ALPHA, "/admin/school/delivery/whatsapp/clear")
check("removing WhatsApp settings returns the school to the platform's", in_school(info_alpha, delivery.whatsapp_settings).source == "platform")

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
                   ("/admin/school/delivery/email/test", {"to": "a@b.org"}), ("/admin/school/delivery/whatsapp/save", wa_form),
                   ("/admin/school/delivery/whatsapp/clear", {}), ("/admin/school/delivery/whatsapp/check", {})):
    r = clerk.post(path, data={"_csrf_token": "t" * 32, **data}, base_url=ALPHA)
    denied.append(r.status_code)
check("…nor change, clear or test anything, even with a valid form token", all(s != 302 for s in denied), str(denied))
check("…and nothing was changed", stored(info_alpha, "smtp_host") is None and stored(info_alpha, "whatsapp_token") is None)
check("the menu does not offer it to them", "Email &amp; WhatsApp" not in clerk.get("/admin/school", base_url=ALPHA).get_data(as_text=True))
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
check("rotating with the real old key moves the school's own secret (WhatsApp's own was cleared above)",
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
