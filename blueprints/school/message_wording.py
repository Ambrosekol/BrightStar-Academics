"""Settings > Message wording: the words of every email and text message the school sends.

Each message (core/message_wording.py MESSAGES) shows the words in use, the platform's own by default; the School
Admin edits them in a pop-up, with the placeholders it may use and a preview, or restores the standard words. A text
message must fit in one text (checked here with long names, and enforced again when it is sent).

School Admin only, like Email & SMS: these are words parents read as coming from the school.
"""

from flask import abort, flash, redirect, render_template, request, session, url_for

from app import app
from core import message_wording as mw
from core.security import admin_access_error, admin_required, audit_log, csrf_protect, current_admin, is_school_admin

_DRAFT = 'message_wording_draft'


def _back(key=None):
    return redirect(url_for('admin_message_wording', edit=key) + (f'#{key}' if key else ''))


@app.route('/admin/settings/message-wording')
@admin_required
def admin_message_wording():
    if not is_school_admin(): return admin_access_error('Message wording')
    groups = mw.catalogue()
    draft = session.pop(_DRAFT, None)
    edit = request.args.get('edit', '')
    if draft and draft.get('key') == edit and edit in mw.MESSAGES:
        for _, _, rows in groups:
            for row in rows:
                if row['key'] == edit:
                    row['subject'], row['body'], row['draft'] = draft.get('subject', ''), draft.get('body', ''), True
    changed = sum(1 for _, _, rows in groups for r in rows if r['custom'])
    return render_template('admin_message_wording.html', groups=groups, changed=changed,
                           edit=edit if edit in mw.MESSAGES else '', sms_page=mw.SMS_PAGE)


@app.post('/admin/settings/message-wording/<key>')
@admin_required
@csrf_protect
def admin_message_wording_save(key):
    if not is_school_admin(): return admin_access_error('Message wording')
    if key not in mw.MESSAGES: abort(404)
    subject, body = request.form.get('subject', ''), request.form.get('body', '')
    try:
        mw.save(key, subject, body, current_admin()['id'])
    except ValueError as exc:
        session[_DRAFT] = {'key': key, 'subject': subject[:mw.SUBJECT_MAX * 2], 'body': body[:mw.EMAIL_BODY_MAX + 500]}
        flash(str(exc), 'error')
        return _back(key)
    custom = mw.wording(key)['custom']
    audit_log('message_wording_updated', 'school', 'message_wording', key, {'custom': custom})
    flash(f'"{mw.MESSAGES[key]["label"]}" ' + ('now uses your words.' if custom else 'uses the standard words.'), 'success')
    return _back()


@app.post('/admin/settings/message-wording/<key>/reset')
@admin_required
@csrf_protect
def admin_message_wording_reset(key):
    if not is_school_admin(): return admin_access_error('Message wording')
    if key not in mw.MESSAGES: abort(404)
    mw.reset(key)
    audit_log('message_wording_reset', 'school', 'message_wording', key)
    flash(f'"{mw.MESSAGES[key]["label"]}" uses the standard words again.', 'success')
    return _back()
