"""Rotating BRIGHTSTARS_DELIVERY_KEY safely.

Every school's own mail-server password, SMS API token, Paystack secret key and its staff's bank account numbers,
pension PINs and tax IDs (core/payroll.py) are encrypted
under this one key (core/delivery.py, core/payments.py's own domain-separated derivations of it).
Simply changing the environment variable and restarting, the way recommendations.html's Hardening
section frames "rotate the platform secrets on a calendar" for the session secret, would instead
silently break every school's mail, SMS and online payments at once: the next read of any of
them would decrypt to nothing under a key that no longer matches what encrypted it.

rotate_one_tenant() re-encrypts everything the CURRENT tenant holds, from the old key to the new
one; `python -m control_plane rotate-delivery-key OLD NEW [CODE]` (control_plane/cli.py) runs it
across every school (or one), so the environment variable itself is only ever changed once every
school's own secrets have already been moved to the new key - never the other way around.
"""

import os

from sqlalchemy import select

from models import SchoolDeliverySetting, SchoolPaymentSetting, StaffPay, db

_DELIVERY_KEYS = ('smtp_password', 'sms_api_token')
_PAYMENT_KEYS = ('paystack_secret_key',)
_PAYROLL_FIELDS = ('account_number_enc', 'rsa_pin_enc', 'tax_id_enc')


def rotate_one_tenant(old_key, new_key):
    """Re-encrypt this tenant's own secrets, from ``old_key`` to ``new_key``.

    Returns ``(rotated, skipped)``: how many values were moved, and the names of any that could
    not be read under ``old_key`` (left untouched, never blanked - a wrong old key must never
    silently destroy a working setting).
    """
    from core import delivery, payments, payroll  # deferred: both import core.security, a heavier import

    delivery_rows = db.session.scalars(
        select(SchoolDeliverySetting).where(SchoolDeliverySetting.setting_key.in_(_DELIVERY_KEYS))).all()
    payment_rows = db.session.scalars(
        select(SchoolPaymentSetting).where(SchoolPaymentSetting.setting_key.in_(_PAYMENT_KEYS))).all()

    previous = os.environ.get('BRIGHTSTARS_DELIVERY_KEY')
    try:
        os.environ['BRIGHTSTARS_DELIVERY_KEY'] = old_key
        plain = {}
        for row in delivery_rows:
            plain[('delivery', row.setting_key)] = delivery.decrypt(row.setting_value)
        for row in payment_rows:
            plain[('payment', row.setting_key)] = payments.decrypt(row.setting_value)
        pay_rows = db.session.scalars(select(StaffPay)).all()
        for row in pay_rows:
            for field in _PAYROLL_FIELDS:
                if getattr(row, field):
                    plain[('payroll', row.id, field)] = payroll.decrypt(getattr(row, field))

        os.environ['BRIGHTSTARS_DELIVERY_KEY'] = new_key
        rotated, skipped = 0, []
        for row in delivery_rows:
            value = plain[('delivery', row.setting_key)]
            if not value:  # decrypt() returns '' on a key that does not match - never re-encrypt that
                skipped.append(row.setting_key)
                continue
            row.setting_value = delivery.encrypt(value)
            rotated += 1
        for row in payment_rows:
            value = plain[('payment', row.setting_key)]
            if not value:
                skipped.append(row.setting_key)
                continue
            row.setting_value = payments.encrypt(value)
            rotated += 1
        for row in pay_rows:
            for field in _PAYROLL_FIELDS:
                if ('payroll', row.id, field) not in plain:
                    continue
                value = plain[('payroll', row.id, field)]
                if not value:
                    skipped.append(f'payroll_staff.{row.id}.{field}')
                    continue
                setattr(row, field, payroll.encrypt(value))
                rotated += 1
        db.session.commit()
        return rotated, skipped
    finally:
        if previous is None:
            os.environ.pop('BRIGHTSTARS_DELIVERY_KEY', None)
        else:
            os.environ['BRIGHTSTARS_DELIVERY_KEY'] = previous
