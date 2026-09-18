import os, json, ast

BASE=os.path.dirname(os.path.abspath(__file__))
app=open(os.path.join(BASE,'app.py'),encoding='utf-8').read()
ast.parse(app)
required_routes=[
    '/login','/candidate/logout','/candidate/dashboard',
    '/candidate/papers/<int:paper_id>/start',
    '/admin/candidates','/admin/candidates/new',
    '/admin/candidates/<int:cid>','/admin/candidates/<int:cid>/result/print',
    '/admin/candidates/<int:cid>/credentials/reset',
    '/admin/results/<int:aid>/grant-retake'
]
for route in required_routes:
    assert route in app, f'missing route marker: {route}'
for table in ['candidates','candidate_papers','retake_grants']:
    assert f'CREATE TABLE IF NOT EXISTS {table}' in app, f'missing table: {table}'
assert 'ALTER TABLE attempts ADD COLUMN candidate_id INTEGER' in app
assert 'candidate_id INTEGER' in app
assert "phase='6G'" in app
assert "SELECT cp.id AS paper_id" in app, 'candidate paper primary key not exposed to dashboard'
assert "'id':r['paper_id']" in app, 'dashboard paper start id not wired'
assert 'required_papers_for_target' in app, 'entry-level paper assignment missing'
assert "Mathematics, English and General Knowledge" in app, 'three-paper rule missing'
assert 'render_template(\'candidate_form.html\'' in app
assert 'if session.get(\'candidate_id\') and a[\'candidate_id\'] != session.get(\'candidate_id\')' in app
assert "if session.get('candidate_id'): return redirect(url_for('candidate_dashboard'))" in app
assert "score, max_score or percentage" in app
assert 'requested_q=int(request.args.get' in app
manifest=json.load(open(os.path.join(BASE,'data','manifest.json'),encoding='utf-8'))
assert manifest['phase']=='6G'
assert 'entry_level_based_three_paper_assignment' in manifest['candidate_features']
for template in ['login.html','candidate_dashboard.html','candidate_form.html','candidate_registered.html','admin_candidates.html','admin_candidate_detail.html','admin_candidate_result_print.html']:
    assert os.path.exists(os.path.join(BASE,'templates',template)), f'missing template: {template}'
assert os.path.exists(os.path.join(BASE,'PHASE6G_CONTROLLED_MODIFICATION.md'))
print('PHASE 6G STATIC VERIFICATION: PASS')
