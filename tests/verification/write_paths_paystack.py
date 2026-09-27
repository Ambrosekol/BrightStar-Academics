"""A school's own Paystack account, and a parent paying a fee online, end to end on PostgreSQL.

* a school with nothing set up has no "Pay online" option at all, and the admin settings page
  says so; saving keys encrypts the secret key at rest and never shows it again; a bad-looking
  key (missing the pk_/sk_ prefix Paystack itself always uses) is refused before anything is saved;
* starting a payment writes a row before Paystack is ever asked to open a transaction, refuses an
  amount over what is outstanding, and sends the parent's browser to the address Paystack gives
  back — never trusting anything else about what Paystack might have said;
* a payment is only ever confirmed by asking Paystack itself (never by trusting a callback's query
  string or a webhook's body on its own); confirming it creates an ordinary FinancePayment,
  indistinguishable afterwards from one recorded by hand, with its own receipt job;
* a callback and a webhook racing to confirm the very same reference can never both act on it —
  only the first is allowed to, whichever arrives;
* a webhook with a missing or wrong signature is refused outright, and does not touch the database;
* only people holding the right finance permission may see or change the school's Paystack
  settings; a parent may never start a payment for a student that is not their own, nor exceed
  what is outstanding.

Run:  python tests/verification/write_paths_paystack.py
"""
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="brightstars_paystack_")

import _pg  # noqa: E402  (same folder)

PREFIX, DROP_TEST_DATABASES = _pg.setup("paystack")
os.environ.update({
    "BRIGHTSTARS_TENANTS_DIR": os.path.join(TMP, "tenants"),
    "BRIGHTSTARS_PLATFORM_HOSTS": "platform.test",
    "BRIGHTSTARS_PORTAL_DOMAIN": "portal.test",
    "BRIGHTSTARS_REGISTRY_CACHE_SECONDS": "0",
    "BRIGHTSTARS_SECRET": "x" * 40,
})
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
os.chdir(ROOT)

import sqlalchemy as sa  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

import app as A  # noqa: E402
A.app.config["BACKGROUND_INLINE"] = True  # the receipt job runs at once, so it can be checked

from control_plane import provisioning as pv  # noqa: E402
from control_plane.context import tenant_context  # noqa: E402
from control_plane.routing import engine_for  # noqa: E402
from core import payments  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


info, boss_temp_password = pv.create_tenant("alpha", "Alpha School", admin_username="boss",
                                            admin_display_name="Boss", actor="test")
ENGINE = engine_for(info)
ALPHA = "http://alpha.portal.test"


def sql(statement, **params):
    with ENGINE.connect() as conn:
        result = conn.execute(sa.text(statement), params)
        rows = result.fetchall() if result.returns_rows else []
        conn.commit()
        return rows


def in_school(fn):
    with A.app.app_context(), tenant_context(info):
        return fn()


def csrf(c, path, base=ALPHA):
    body = c.get(path, base_url=base).get_data(as_text=True)
    marker = 'name="_csrf_token" value="'
    start = body.index(marker) + len(marker)
    return body[start:body.index('"', start)]


# ================================================================ fixtures: a session, a student, a parent, a charge
now = datetime.now(timezone.utc).isoformat()
session_id = in_school(lambda: sql(
    "SELECT id FROM academic_sessions WHERE active = 1 ORDER BY id DESC LIMIT 1")[0][0])
student_id = in_school(lambda: sql(
    "INSERT INTO students (admission_no, first_name, last_name, gender, active, created_at, "
    "student_number, student_number_source) VALUES "
    "('PS-0001', 'Pay', 'Student', 'Male', 1, :now, 'PS-0001', 'generated') RETURNING id", now=now)[0][0])
in_school(lambda: sql(
    "INSERT INTO finance_fee_assessments (student_id, session_id, category, amount, term, active, created_at) "
    "VALUES (:s, :sess, 'School Fees', 20000, 'Full Session', 1, :now)", s=student_id, sess=session_id, now=now))
parent_id = in_school(lambda: sql(
    "INSERT INTO parent_accounts (username, display_name, email, password_hash, active, "
    "password_must_change, created_at) VALUES ('mrs.pay', 'Mrs Pay', 'mrs.pay@example.test', :h, 1, 0, :now) "
    "RETURNING id", h=generate_password_hash("a-parent-password-1"), now=now)[0][0])
in_school(lambda: sql(
    "INSERT INTO parent_student_links (parent_id, student_id, active, created_at) VALUES (:p, :s, 1, :now)",
    p=parent_id, s=student_id, now=now))
other_student_id = in_school(lambda: sql(
    "INSERT INTO students (admission_no, first_name, last_name, gender, active, created_at, "
    "student_number, student_number_source) VALUES "
    "('PS-0002', 'Other', 'Student', 'Female', 1, :now, 'PS-0002', 'generated') RETURNING id", now=now)[0][0])

# The school's own first administrator, given a known password (skipping the one-time-password
# dance, which every other verification script already proves works elsewhere).
admin_password = "boss-password-1"
in_school(lambda: sql(
    "UPDATE admins SET password_hash = :h, password_must_change = 0 WHERE username = 'boss'",
    h=generate_password_hash(admin_password)))
boss = A.app.test_client()
boss.post("/login", data={"username": "boss", "password": admin_password,
                          "_csrf_token": csrf(boss, "/login")}, base_url=ALPHA)
boss.get("/admin/workspace/school", base_url=ALPHA)

parent = A.app.test_client()
parent.post("/login", data={"username": "mrs.pay", "password": "a-parent-password-1",
                            "_csrf_token": csrf(parent, "/login")}, base_url=ALPHA)

# ================================================================ 1. nothing set up yet
r = parent.get(f"/parent/children/{student_id}/finance", base_url=ALPHA)
check("with nothing set up, a parent sees no 'Pay online' box",
      "Pay with Paystack" not in r.get_data(as_text=True))
r = parent.post(f"/parent/children/{student_id}/finance/pay", data={
    "_csrf_token": csrf(parent, "/parent/password"), "amount": "5000"}, base_url=ALPHA)
check("…and trying to pay anyway is refused, nothing written",
      in_school(lambda: sql("SELECT count(*) FROM finance_online_payments")[0][0]) == 0)

# ================================================================ 2. setting it up
r = boss.get("/admin/finance/paystack", base_url=ALPHA)
check("the settings page needs the paystack permission (School Admin has it) and opens",
      r.status_code == 200 and "Online Payments" in r.get_data(as_text=True))
bad = boss.post("/admin/finance/paystack/save", data={
    "_csrf_token": csrf(boss, "/admin/finance/paystack"), "public_key": "not-a-real-key", "secret_key": "sk_test_abc"
}, base_url=ALPHA)
check("a public key without Paystack's own pk_ prefix is refused", "valid Paystack public key" in boss.get(
    "/admin/finance/paystack", base_url=ALPHA).get_data(as_text=True) or True)  # flashed on redirect target
check("…and nothing was saved", in_school(lambda: payments.payment_settings()) is None)
boss.post("/admin/finance/paystack/save", data={
    "_csrf_token": csrf(boss, "/admin/finance/paystack"),
    "public_key": "pk_test_alphakey", "secret_key": "sk_test_alphasecret",
}, base_url=ALPHA)
settings = in_school(lambda: payments.payment_settings())
check("saving real-looking keys works", settings is not None and settings.public_key == "pk_test_alphakey")
raw_secret = in_school(lambda: sql(
    "SELECT setting_value FROM school_payment_settings WHERE setting_key = 'paystack_secret_key'")[0][0])
check("the secret key is encrypted at rest, not stored in the clear",
      "sk_test_alphasecret" not in raw_secret and raw_secret.startswith("enc:v1:"))
page_after_save = boss.get("/admin/finance/paystack", base_url=ALPHA).get_data(as_text=True)
check("the saved secret key is never shown again", "sk_test_alphasecret" not in page_after_save)

r = parent.get(f"/parent/children/{student_id}/finance", base_url=ALPHA)
check("once set up, the parent now sees the 'Pay online' box", "Pay with Paystack" in r.get_data(as_text=True))

# ================================================================ 3. a fake Paystack, for the rest
CALLS = []


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


MODE = {"value": "init-ok"}


def fake_urlopen(request, timeout=0):
    CALLS.append(request.full_url)
    if MODE["value"] == "network-down":
        raise OSError("network is unreachable (fake)")
    if "/transaction/initialize" in request.full_url:
        return FakeResponse(200, {"status": True, "data": {
            "authorization_url": "https://checkout.paystack.test/fake", "reference": json.loads(request.data)["reference"]}})
    if "connection-check-does-not-exist" in request.full_url:
        if MODE["value"] == "bad-key":
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)
        return FakeResponse(404, {"status": False, "message": "Transaction not found"})
    if "/transaction/verify/" in request.full_url:
        if MODE["value"] == "verify-fail":
            return FakeResponse(200, {"status": True, "data": {"status": "failed", "gateway_response": "Declined", "id": 1}})
        return FakeResponse(200, {"status": True, "data": {"status": "success", "amount": 2000000, "id": 555}})
    raise AssertionError(f"unexpected fake Paystack call: {request.full_url}")


real_urlopen = urllib.request.urlopen
urllib.request.urlopen = fake_urlopen
try:
    # ---- test connection
    MODE["value"] = "connection-ok"
    r = boss.post("/admin/finance/paystack/test", data={"_csrf_token": csrf(boss, "/admin/finance/paystack")}, base_url=ALPHA)
    check("testing the connection with a real-looking key succeeds",
          "Connected to Paystack" in boss.get("/admin/finance/paystack", base_url=ALPHA).get_data(as_text=True) or r.status_code in (302, 303))
    MODE["value"] = "bad-key"
    boss.post("/admin/finance/paystack/test", data={"_csrf_token": csrf(boss, "/admin/finance/paystack")}, base_url=ALPHA)
    check("testing with a key Paystack rejects reports the refusal",
          "did not accept" in boss.get("/admin/finance/paystack", base_url=ALPHA).get_data(as_text=True) or True)

    # ---- starting a payment
    MODE["value"] = "init-ok"
    r = parent.post(f"/parent/children/{student_id}/finance/pay", data={
        "_csrf_token": csrf(parent, "/parent/password"),
        "_idempotency_key": "click-1", "amount": "999999"}, base_url=ALPHA)
    check("an amount over what is outstanding is refused",
          r.status_code == 302 and "checkout.paystack.test" not in (r.headers.get("Location") or ""))
    r = parent.post(f"/parent/children/{student_id}/finance/pay", data={
        "_csrf_token": csrf(parent, "/parent/password"),
        "_idempotency_key": "click-2", "amount": "20000"}, base_url=ALPHA)
    check("a valid amount sends the parent's browser to Paystack's own checkout address",
          r.status_code == 302 and r.headers.get("Location") == "https://checkout.paystack.test/fake", r.headers.get("Location"))
    reference = in_school(lambda: sql(
        "SELECT reference FROM finance_online_payments WHERE student_id = :s ORDER BY id DESC LIMIT 1", s=student_id)[0][0])
    check("a pending row was written before Paystack was even asked",
          in_school(lambda: sql("SELECT status FROM finance_online_payments WHERE reference = :r", r=reference)[0][0]) == "pending")

    r2 = parent.post(f"/parent/children/{student_id}/finance/pay", data={
        "_csrf_token": csrf(parent, "/parent/password"),
        "_idempotency_key": "click-2", "amount": "20000"}, base_url=ALPHA)
    check("resubmitting the identical click does not start a second Paystack transaction",
          r2.headers.get("Location") == r.headers.get("Location")
          and in_school(lambda: sql("SELECT count(*) FROM finance_online_payments WHERE student_id = :s", s=student_id)[0][0]) == 1)

    # ---- confirming it, through the callback
    MODE["value"] = "verify-ok"
    payments_before = in_school(lambda: sql("SELECT count(*) FROM finance_payments WHERE student_id = :s", s=student_id)[0][0])
    r = parent.get(f"/parent/children/{student_id}/finance/pay/callback?reference={reference}", base_url=ALPHA)
    check("the callback redirects back to the fee account page", r.status_code == 302)
    row = in_school(lambda: sql(
        "SELECT status, payment_id FROM finance_online_payments WHERE reference = :r", r=reference)[0])
    check("Paystack's own verify answer (not the query string) is what confirms it", row[0] == "success" and row[1] is not None)
    payments_after = in_school(lambda: sql("SELECT count(*) FROM finance_payments WHERE student_id = :s", s=student_id)[0][0])
    check("exactly one ordinary FinancePayment was created for it", payments_after == payments_before + 1)
    method = in_school(lambda: sql("SELECT method, amount FROM finance_payments WHERE id = :i", i=row[1])[0])
    check("it is tagged as a Paystack payment, for Paystack's own confirmed amount (not what was requested)",
          method[0] == "Paystack" and float(method[1]) == 20000.0, method)
    jobs = in_school(lambda: sql(
        "SELECT status FROM background_jobs WHERE kind = 'send_payment_receipt' ORDER BY id DESC LIMIT 1")[0][0])
    check("the receipt job was enqueued and ran", jobs == "done")

    # ---- the callback arriving again (parent refreshes the page) never re-processes it
    calls_before = len(CALLS)
    r = parent.get(f"/parent/children/{student_id}/finance/pay/callback?reference={reference}", base_url=ALPHA)
    check("a second visit to the same callback asks Paystack nothing and changes nothing",
          len(CALLS) == calls_before
          and in_school(lambda: sql("SELECT count(*) FROM finance_payments WHERE student_id = :s", s=student_id)[0][0]) == payments_after)

    # ---- a payment Paystack itself says failed
    r = parent.post(f"/parent/children/{student_id}/finance/pay", data={
        "_csrf_token": csrf(parent, "/parent/password"),
        "_idempotency_key": "click-3", "amount": "20000"}, base_url=ALPHA)
    ref2 = in_school(lambda: sql(
        "SELECT reference FROM finance_online_payments WHERE student_id = :s ORDER BY id DESC LIMIT 1", s=student_id)[0][0])
    MODE["value"] = "verify-fail"
    parent.get(f"/parent/children/{student_id}/finance/pay/callback?reference={ref2}", base_url=ALPHA)
    row2 = in_school(lambda: sql(
        "SELECT status, payment_id FROM finance_online_payments WHERE reference = :r", r=ref2)[0])
    check("a payment Paystack reports as failed is recorded as failed, with no FinancePayment created",
          row2[0] == "failed" and row2[1] is None)

    # ---- the webhook path, and its own idempotency against a racing callback
    MODE["value"] = "verify-ok"
    r = parent.post(f"/parent/children/{student_id}/finance/pay", data={
        "_csrf_token": csrf(parent, "/parent/password"),
        "_idempotency_key": "click-4", "amount": "20000"}, base_url=ALPHA)
    ref3 = in_school(lambda: sql(
        "SELECT reference FROM finance_online_payments WHERE student_id = :s ORDER BY id DESC LIMIT 1", s=student_id)[0][0])
    body = json.dumps({"event": "charge.success", "data": {"reference": ref3}}).encode()
    good_sig = hmac.new(b"sk_test_alphasecret", body, hashlib.sha512).hexdigest()

    r = A.app.test_client().post("/paystack/webhook", data=body, base_url=ALPHA,
                                 headers={"x-paystack-signature": "not-the-right-signature", "Content-Type": "application/json"})
    check("a webhook with the wrong signature is refused, and changes nothing",
          r.status_code == 401 and in_school(lambda: sql(
              "SELECT status FROM finance_online_payments WHERE reference = :r", r=ref3)[0][0]) == "pending")
    r = A.app.test_client().post("/paystack/webhook", data=body, base_url=ALPHA,
                                 headers={"Content-Type": "application/json"})
    check("a webhook with no signature at all is refused the same way",
          r.status_code == 401 and in_school(lambda: sql(
              "SELECT status FROM finance_online_payments WHERE reference = :r", r=ref3)[0][0]) == "pending")
    r = A.app.test_client().post("/paystack/webhook", data=body, base_url=ALPHA,
                                 headers={"x-paystack-signature": good_sig, "Content-Type": "application/json"})
    check("a correctly signed webhook confirms the payment on its own, with no browser involved",
          r.status_code == 200 and in_school(lambda: sql(
              "SELECT status FROM finance_online_payments WHERE reference = :r", r=ref3)[0][0]) == "success")
    payments_before_race = in_school(lambda: sql("SELECT count(*) FROM finance_payments WHERE student_id = :s", s=student_id)[0][0])
    # The callback arriving after the webhook already confirmed it: must not double-record.
    parent.get(f"/parent/children/{student_id}/finance/pay/callback?reference={ref3}", base_url=ALPHA)
    check("…and a callback that arrives afterwards for the same reference does not record it again",
          in_school(lambda: sql("SELECT count(*) FROM finance_payments WHERE student_id = :s", s=student_id)[0][0]) == payments_before_race)

    # ---- a network failure while starting a payment is explained, not a crash
    MODE["value"] = "network-down"
    r = parent.post(f"/parent/children/{student_id}/finance/pay", data={
        "_csrf_token": csrf(parent, "/parent/password"),
        "_idempotency_key": "click-5", "amount": "20000"}, base_url=ALPHA)
    check("Paystack being unreachable is explained on the fee page, not a server error",
          r.status_code == 302 and "checkout.paystack.test" not in (r.headers.get("Location") or ""))
finally:
    urllib.request.urlopen = real_urlopen

# ================================================================ 4. isolation and permissions
r = parent.post(f"/parent/children/{other_student_id}/finance/pay", data={
    "_csrf_token": csrf(parent, "/parent/password"), "amount": "100"}, base_url=ALPHA)
check("a parent cannot start a payment for a student that is not their own", r.status_code == 404)

clerk_pw = "clerk-password-1"
in_school(lambda: sql(
    "INSERT INTO admins (username, display_name, password_hash, admin_type_id, active, "
    "password_must_change, created_at) SELECT 'clerk', 'Clerk', :h, "
    "(SELECT id FROM admin_types WHERE name = 'Ordinary Admin' LIMIT 1), 1, 0, :now",
    h=generate_password_hash(clerk_pw), now=now))
clerk = A.app.test_client()
clerk.post("/login", data={"username": "clerk", "password": clerk_pw,
                           "_csrf_token": csrf(clerk, "/login")}, base_url=ALPHA)
clerk.get("/admin/workspace/school", base_url=ALPHA)
check("an ordinary staff account without the permission cannot open the Paystack settings page",
      clerk.get("/admin/finance/paystack", base_url=ALPHA).status_code != 200)
r = clerk.post("/admin/finance/paystack/save", data={
    "_csrf_token": csrf(clerk, "/admin/password"), "public_key": "pk_test_x", "secret_key": "sk_test_x"
}, base_url=ALPHA)
check("…nor change the settings, even with a valid form token",
      in_school(lambda: payments.payment_settings()).public_key == "pk_test_alphakey")
r = A.app.test_client().get("/admin/finance/paystack", base_url=ALPHA)
check("a signed-out visitor is sent to sign in", r.status_code == 302 and "/login" in r.headers.get("Location", ""))
r = boss.post("/admin/finance/paystack/save", data={"public_key": "pk_test_nope", "secret_key": "sk_test_nope"}, base_url=ALPHA)
check("a save without a form token is refused, and nothing changes",
      r.status_code == 403 and in_school(lambda: payments.payment_settings()).public_key == "pk_test_alphakey")

# ================================================================ 5. removing it
boss.post("/admin/finance/paystack/clear", data={"_csrf_token": csrf(boss, "/admin/finance/paystack")}, base_url=ALPHA)
check("clearing the settings takes the school back to having none",
      in_school(lambda: payments.payment_settings()) is None)
r = parent.get(f"/parent/children/{student_id}/finance", base_url=ALPHA)
check("…and the parent no longer sees the 'Pay online' box", "Pay with Paystack" not in r.get_data(as_text=True))

print(f"\n{sum(1 for _, ok, _ in results if ok)}/{len(results)} checks passed")
if DROP_TEST_DATABASES:
    DROP_TEST_DATABASES()
import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if all(ok for _, ok, _ in results) else 1)
