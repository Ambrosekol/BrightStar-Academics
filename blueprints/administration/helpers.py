"""Private helpers used only by admin account creation/editing in this
package.
"""

from sqlalchemy import delete as sa_delete

from models import AdminRoleAssignment, db
from core.db_helpers import _ignore_insert


def _sync_admin_roles(admin_id, role_ids, granted_by, now):
    ids=[]
    for rid in role_ids:
        try: rid=int(rid)
        except (TypeError,ValueError): continue
        if rid not in ids: ids.append(rid)
    db.session.execute(sa_delete(AdminRoleAssignment).where(AdminRoleAssignment.admin_id==admin_id))
    _ignore_insert(AdminRoleAssignment, [
        {'admin_id':admin_id,'admin_type_id':rid,'assigned_at':now,'assigned_by':granted_by}
        for rid in ids
    ])
    return ids

def _admin_contact_fields(form):
    return {
        'email': form.get('email','').strip().lower(),
        'phone': form.get('phone','').strip(),
        'whatsapp': form.get('whatsapp','').strip(),
    }

def _validate_admin_contact_fields(contact):
    errors=[]
    email=contact['email']
    if email and (len(email)>254 or '@' not in email or '.' not in email.rsplit('@',1)[-1]): errors.append('Enter a valid email address.')
    for label,key in (('Phone','phone'),('WhatsApp','whatsapp')):
        value=contact[key]
        if value and len(''.join(ch for ch in value if ch.isdigit())) < 7: errors.append(f'Enter a valid {label} number or leave it blank.')
    return errors
