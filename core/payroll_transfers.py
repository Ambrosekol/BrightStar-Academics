"""Paying an approved payroll through the school's own Paystack account (Paystack Transfers).

Only the School Admin (the proprietor or proprietress) sends the money (blueprints/school/payroll_paystack.py); this
module is the talking to Paystack, and the rules that keep a salary from going to the wrong place or going twice.

* **The account is the approved one.** A person is paid only into the bank account their approved payslip was worked
  out with (its bank and last four digits). If their bank details changed after approval, they are not paid from here:
  reopen the payroll and approve it again. Nobody can redirect a salary after the proprietor signed it off.
* **The bank says whose account it is.** Every account is looked up at Paystack (``/bank/resolve``) before it is paid,
  and the name the bank holds is shown beside the payroll's name; a name that shares nothing with the payroll's is not
  paid unless the proprietor ticks it.
* **Never twice.** Each payslip carries its own transfer reference and state. One that is being paid or paid is never
  sent again; one whose sending could not be confirmed (Paystack unreachable mid-request) stays "pending" and is only
  settled by asking Paystack about its reference, never by sending it again.
* **Nothing is believed without asking.** A webhook only says "look again": its signature is checked
  (blueprints/finance/paystack.py) and then the transfer is verified at Paystack (``/transfer/verify``).
* Account numbers are decrypted only to send them to Paystack, never logged or shown in full.

Money is whole kobo, as everywhere in payroll, which is also what Paystack takes.
"""

import re
import secrets
import time

from core.payments import PaystackError, _request

BULK_LIMIT = 100                     # transfers in one /transfer/bulk request (Paystack's limit)
NOT_FOUND_AFTER_SECONDS = 15 * 60    # a pending transfer Paystack still has no record of after this is treated as not sent
LIVE = ('pending', 'success')        # a payslip in one of these states is never sent again

# Our bank list (core/payroll.py NIGERIAN_BANKS) against Paystack's names: what each one's Paystack name contains,
# letters and digits only. Among several matches the shortest Paystack name wins (Access Bank, not Access Bank
# (Diamond)). The account lookup that follows proves the bank is right.
BANK_KEYS = {
    'Access Bank': 'accessbank', 'Citibank': 'citibank', 'Ecobank': 'ecobank', 'Fidelity Bank': 'fidelity',
    'First Bank': 'firstbank', 'FCMB': 'firstcitymonument', 'Globus Bank': 'globus', 'GTBank': 'guarantytrust',
    'Heritage Bank': 'heritage', 'Jaiz Bank': 'jaiz', 'Keystone Bank': 'keystone', 'Kuda': 'kuda', 'Lotus Bank': 'lotus',
    'Moniepoint': 'moniepoint', 'OPay': 'opay', 'Optimus Bank': 'optimus', 'PalmPay': 'palmpay', 'Parallex Bank': 'parallex',
    'Polaris Bank': 'polaris', 'Premium Trust Bank': 'premiumtrust', 'Providus Bank': 'providus', 'Signature Bank': 'signature',
    'Stanbic IBTC': 'stanbic', 'Standard Chartered': 'standardchartered', 'Sterling Bank': 'sterling', 'SunTrust Bank': 'suntrust',
    'Taj Bank': 'taj', 'Titan Trust Bank': 'titantrust', 'Union Bank': 'unionbank', 'UBA': 'unitedbankforafrica',
    'Unity Bank': 'unitybank', 'Wema Bank': 'wema', 'Zenith Bank': 'zenith',
}
_BANK_CACHE = {}                     # {secret key's tail: (fetched at, [banks])}, an hour


def _plain(text):
    return re.sub(r'[^a-z0-9]', '', str(text or '').lower())


def banks(settings):
    """Paystack's Nigerian banks: ``[{'name', 'code'}]``, kept for an hour."""
    key = settings.secret_key[-12:]
    hit = _BANK_CACHE.get(key)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    found = []
    for page in range(1, 11):
        status, body = _request(settings.secret_key, 'GET', f'/bank?country=nigeria&currency=NGN&perPage=100&page={page}')
        if status != 200 or not body.get('status'):
            raise PaystackError(body.get('message') or 'Paystack did not give its list of banks.')
        rows = body.get('data') or []
        found += [{'name': b.get('name', ''), 'code': str(b.get('code', ''))} for b in rows
                  if b.get('code') and b.get('active', True) is not False]
        if len(rows) < 100:
            break
    _BANK_CACHE[key] = (time.time(), found)
    return found


def bank_code(bank_name, bank_list):
    """Paystack's code for one of our banks, or None if Paystack does not list it."""
    want = BANK_KEYS.get(bank_name)
    if not want:
        return None
    matches = [b for b in bank_list if want in _plain(b['name'])]
    return min(matches, key=lambda b: len(_plain(b['name'])))['code'] if matches else None


def resolve(settings, account_number, code):
    """``(account name the bank holds, None)``, or ``(None, why it could not be found)``."""
    try:
        status, body = _request(settings.secret_key, 'GET', f'/bank/resolve?account_number={account_number}&bank_code={code}')
    except PaystackError as exc:
        return None, exc.detail
    if status == 200 and body.get('status') and (body.get('data') or {}).get('account_name'):
        return body['data']['account_name'], None
    return None, body.get('message') or 'The bank could not find this account.'


def names_match(bank_name_on_account, *ours):
    """Whether the name the bank holds shares a word (three letters or more) with the payroll's name for the person."""
    words = {w for w in re.findall(r'[a-z]+', str(bank_name_on_account or '').lower()) if len(w) >= 3}
    mine = {w for text in ours for w in re.findall(r'[a-z]+', str(text or '').lower()) if len(w) >= 3}
    return bool(words & mine)


def balance(settings):
    """The school's Paystack balance in naira, in kobo."""
    status, body = _request(settings.secret_key, 'GET', '/balance')
    if status != 200 or not body.get('status'):
        raise PaystackError(body.get('message') or 'Paystack did not give the balance.')
    return sum(int(b.get('balance') or 0) for b in (body.get('data') or []) if (b.get('currency') or 'NGN') == 'NGN')


def recipient(settings, name, account_number, code):
    """A Paystack transfer recipient for this account; returns its code."""
    status, body = _request(settings.secret_key, 'POST', '/transferrecipient', {
        'type': 'nuban', 'name': name[:100], 'account_number': account_number, 'bank_code': code, 'currency': 'NGN'})
    if status not in (200, 201) or not body.get('status'):
        raise PaystackError(body.get('message') or 'Paystack would not accept this account for transfers.')
    return body['data']['recipient_code']


def new_reference(run_id, payslip_id):
    """A transfer reference: Paystack takes 16 to 50 lower-case letters, digits, - and _."""
    return f'bsp-{run_id}-{payslip_id}-{secrets.token_hex(6)}'


def send_bulk(settings, transfers):
    """Send up to BULK_LIMIT transfers ``[{'amount', 'recipient', 'reference', 'reason'}]`` from the balance.

    Returns ``{reference: transfer_code}``. Raises PaystackError when Paystack refuses the request (nothing was sent);
    raises ConnectionError when Paystack could not be reached or answered something unreadable, when it is not known
    whether anything was sent, so the caller leaves those transfers pending and asks Paystack about them later."""
    try:
        status, body = _request(settings.secret_key, 'POST', '/transfer/bulk',
                                {'currency': 'NGN', 'source': 'balance', 'transfers': transfers}, timeout=30)
    except PaystackError as exc:
        raise ConnectionError(exc.detail) from exc
    if status >= 500 or not isinstance(body, dict) or ('status' not in body):
        raise ConnectionError(f'Paystack answered unexpectedly (status {status}).')
    if not body.get('status'):
        message = body.get('message') or 'Paystack refused these transfers.'
        if 'otp' in message.lower():
            message += (' Paying several people at once needs the one-time code (OTP) for transfers turned off: in your '
                        'Paystack dashboard, Settings, Preferences, Transfers (or ask Paystack support to turn it off).')
        raise PaystackError(message)
    return {str(t.get('reference')): t.get('transfer_code') for t in (body.get('data') or []) if t.get('reference')}


def verify(settings, reference):
    """What became of a transfer: ``'success'``, ``'failed'``, ``'reversed'``, ``'pending'`` or ``'unknown'`` (Paystack
    has no record of it), and Paystack's reason when it did not go through."""
    status, body = _request(settings.secret_key, 'GET', f'/transfer/verify/{reference}')
    if status == 404 or (status == 400 and not body.get('status')):
        return 'unknown', body.get('message') or 'Paystack has no record of this transfer.'
    if status != 200 or not body.get('status'):
        raise PaystackError(body.get('message') or 'Paystack could not check this transfer.')
    data = body.get('data') or {}
    state = str(data.get('status') or '').lower()
    if state == 'success':
        return 'success', None
    if state == 'reversed':
        return 'reversed', data.get('reason') or 'The bank sent the money back.'
    if state in ('failed', 'abandoned', 'blocked', 'rejected'):
        return 'failed', data.get('reason') or data.get('gateway_response') or f'Paystack: {state}.'
    return 'pending', None
