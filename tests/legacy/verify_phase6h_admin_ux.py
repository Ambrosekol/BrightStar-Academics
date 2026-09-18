"""Static verification for the Phase 6H administration UX/governance layer."""
from pathlib import Path
import ast

BASE=Path(__file__).resolve().parent
app=(BASE/'app.py').read_text(encoding='utf-8')
ast.parse(app)
required_routes=[
    "@app.route('/admin/home')",
    "@app.route('/admin/school')",
    "@app.route('/admin/administration/controls')",
    "@app.post('/admin/administration/notifications/<int:nid>/read')",
    "@app.post('/admin/administration/resources/<resource_type>/<path:resource_id>/lock')",
    "@app.post('/admin/administration/resources/<resource_type>/<path:resource_id>/unlock')",
    "@app.route('/admin/administration/admins/<int:aid>/edit',methods=['GET','POST'])",
    "@app.route('/admin/banks')",
    "@app.route('/admin/school/students')",
    "@app.route('/admin/school/classes')",
    "@app.route('/admin/school/subjects')",
    "@app.route('/admin/school/assignments')",
    "@app.route('/admin/school/tests')",
    "@app.route('/admin/school/practice-tests')",
    "@app.route('/admin/school/examinations')",
    "@app.route('/admin/school/results')",
]
for marker in required_routes:
    assert marker in app, f'missing route: {marker}'
for token in ['ADMIN_ROLE_PRESETS','admin_notifications','admin_control_items','admin_resource_locks','_notify_super_admins','_lock_resource','_unlock_resource']:
    assert token in app, f'missing governance component: {token}'
for fn in ['admin_base.html','admin_workspace_home.html','admin_school_home.html','admin_controls.html','admin_accounts.html','admin_account_form.html','admin_roles.html','admin_role_form.html','admin_question_banks.html']:
    assert (BASE/'templates'/fn).exists(), f'missing template: {fn}'
for phrase in ['scope type','scope value','direct permissions']:
    # Backend terminology may remain in advanced/internal pages, but must not be
    # the normal account creation labels.
    form=(BASE/'templates'/'admin_account_form.html').read_text(encoding='utf-8').lower()
    assert phrase not in form, f'backend wording leaked into account form: {phrase}'
for phrase in ["session.pop('admin_workspace', None)", 'workspace/entrance', 'workspace/school', 'workspace-selection-shell', 'Exit Workspace']:
    haystack = app + '\n' + (BASE/'templates'/'admin_base.html').read_text(encoding='utf-8') + '\n' + (BASE/'templates'/'admin_workspace_home.html').read_text(encoding='utf-8') + '\n' + (BASE/'static'/'admin.css').read_text(encoding='utf-8')
    assert phrase in haystack, f'missing strict workspace boundary component: {phrase}'
print('PHASE 6H ADMIN UX/GOVERNANCE STATIC VERIFICATION: PASS')
