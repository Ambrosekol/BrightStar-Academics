"""Bringing question banks into a school: importing a bank from a JSON file, and setting up
the standard entrance papers from the banks the school started with.

Everything is written inside the current school's own ``data/`` folder (core/storage.py); the
rules for what a bank may contain, and for writing its file safely, are in core/banks.py.

Importing is guarded by ``question_banks.create`` (and ``question_banks.edit`` as well when it
replaces a bank that is already there); setting up the standard papers by
``entrance.config.create`` and ``entrance.config.activate``.
"""

from flask import flash, redirect, render_template, request, url_for
from sqlalchemy import func, select
from werkzeug.utils import secure_filename

from app import _create_control_item, _resource_locked, _school_current_session, app
from core import banks as bank_files
from core.entrance import ENTRANCE_SUBJECT_LABELS, bank, bank_entry_group, bank_subject, load_banks
from core.security import (
    _notify_school_admins, admin_access_error, admin_has_permission, admin_required,
    admin_scope_allows, audit_log, csrf_protect, current_admin,
)
from models import EntranceBankConfig, db


def _usable_for_entrance(new_bank):
    """Whether the entrance-paper setup can use this bank: it must say which subject it is,
    and (except General Knowledge, shared by both levels) which entry class."""
    subject = bank_subject(new_bank)
    if subject is None:
        return False
    return subject == 'general_knowledge' or bank_entry_group(new_bank) is not None


def _import_page(errors=None, status=200, **extra):
    banks = load_banks()
    return render_template('admin_bank_import.html', errors=errors or [], bank_count=len(banks),
                           limits={'megabytes': bank_files.MAX_BANK_BYTES // (1024 * 1024),
                                   'questions': bank_files.MAX_QUESTIONS}, **extra), status


@app.route('/admin/banks/import', methods=['GET', 'POST'])
@admin_required
@csrf_protect
def admin_bank_import():
    if request.method == 'GET':
        return _import_page()
    me = current_admin()
    replace = request.form.get('replace') == '1'
    try:
        new_bank = bank_files.read_bank_upload(request.files.get('bank_file'))
        existing = bank(new_bank['id'])
        if existing and not replace:
            raise bank_files.BankError(
                f'A bank with the id "{new_bank["id"]}" is already here: "{existing.get("name", new_bank["id"])}", '
                f'{len(existing.get("questions", []))} questions. Nothing was changed. To overwrite it with this '
                'file, tick "Replace it" and choose the file again; otherwise change the "id" in the file.')
        if existing:
            if not admin_has_permission(me['id'], 'question_banks.edit'):
                return admin_access_error('question_banks.edit')
            if not admin_scope_allows(me['id'], 'bank', new_bank['id']):
                return admin_access_error('question_banks.edit')
            if _resource_locked('bank', new_bank['id']):
                raise bank_files.BankError('This question bank is locked by School Admin, so it cannot be replaced. '
                                           'Unlock it from Administration > Controls first.')
            needed = db.session.scalar(select(func.max(EntranceBankConfig.questions_to_serve)).where(
                EntranceBankConfig.bank_id == new_bank['id'], EntranceBankConfig.active == 1))
            if needed and needed > len(new_bank['questions']):
                raise bank_files.BankError(
                    f'The live entrance examination that uses this bank asks for {needed} questions, but the new '
                    f'file has only {len(new_bank["questions"])}. Nothing was changed. Add questions to the file, '
                    'or change the entrance configuration first.')
        outcome = bank_files.save_bank_file(new_bank, replace=bool(existing))
    except bank_files.BankError as exc:
        return _import_page(errors=exc.problems, status=400)

    replaced = outcome == 'replaced'
    bank_files.sync_new_banks([] if replaced else [new_bank['id']])
    upload = request.files.get('bank_file')
    audit_log('question_bank_replaced' if replaced else 'question_bank_imported', 'assessment', 'bank',
              new_bank['id'], {'name': new_bank['name'], 'questions': len(new_bank['questions']),
                               'file': secure_filename(getattr(upload, 'filename', '') or '')[:80]})
    _create_control_item(
        'Question bank replaced by an import' if replaced else 'New question bank requires review',
        f'{new_bank["name"]} was {"replaced" if replaced else "imported"} from a file by a staff administrator. '
        'Review it before it is used for an important examination.',
        'question_bank_review', 'bank', new_bank['id'], me['id'])
    _notify_school_admins('Question bank imported', f'{new_bank["name"]} was {"replaced" if replaced else "imported"} '
                          f'from a file ({len(new_bank["questions"])} questions).', 'warning', url_for('admin_controls'))
    message = (f'{"Replaced" if replaced else "Imported"} "{new_bank["name"]}" with {len(new_bank["questions"])} '
               'questions. School Admin has been notified.')
    if not _usable_for_entrance(new_bank):
        message += (' It does not say which subject (Mathematics, English or General Knowledge) and entry class '
                    '(Year 7/JSS 1 or Year 10/SSS 1) it is for, so it cannot be used as an entrance paper until it does.')
    flash(message, 'success')
    return redirect(url_for('admin_bank', bid=new_bank['id']))


@app.post('/admin/entrance-config/standard')
@admin_required
@csrf_protect
def admin_entrance_config_standard():
    """One click: make the standard banks the live entrance papers for the current session."""
    me = current_admin()
    if not admin_has_permission(me['id'], 'entrance.config.activate'):
        return admin_access_error('entrance.config.activate')
    current = _school_current_session()
    if not current:
        flash('There is no academic session yet. Create one under Academic Sessions first.', 'error')
        return redirect(url_for('admin_entrance_config'))
    result = bank_files.set_up_standard_papers(current['id'], me['id'])
    audit_log('entrance_standard_papers_set_up', 'assessment', 'entrance_config', None,
              {'session_id': current['id'], 'made': [s['bank_id'] for s in result['made']],
               'kept': [s['bank_id'] for s in result['kept']], 'missing': [s['bank_id'] for s in result['missing']]})
    if result['made']:
        message = f'{len(result["made"])} standard entrance paper{"s" if len(result["made"]) != 1 else ""} set up and live for {current["name"]}.'
        if result['kept']:
            message += f' {len(result["kept"])} already had a configuration of your own and were left as they are.'
        flash(message, 'success')
    elif result['kept']:
        flash('Nothing changed: every standard paper already has a configuration for this session.', 'success')
    else:
        flash('This school has none of the standard question banks. Import a bank, or ask the platform team '
              'to add the standard ones.', 'error')
    return redirect(url_for('admin_entrance_config'))


@app.context_processor
def _standard_paper_helpers():
    """Lets the Exam Configuration page say whether the standard papers still need setting up."""

    def standard_paper_status():
        current = _school_current_session()
        have = load_banks()
        slots = [s for s in bank_files.standard_slots() if s['bank_id'] in have]
        todo = 0
        if slots and current:
            done = {(g, s) for g, s in db.session.execute(select(
                EntranceBankConfig.entry_group, EntranceBankConfig.subject).where(
                EntranceBankConfig.session_id == current['id'], EntranceBankConfig.term == 'Full Session'))}
            todo = sum(1 for s in slots if (s['entry_group'], s['subject']) not in done)
        return {'available': len(slots), 'todo': todo, 'session': current,
                'subjects': ENTRANCE_SUBJECT_LABELS}

    return {'standard_paper_status': standard_paper_status}
