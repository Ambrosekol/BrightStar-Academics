import json, os, ast
BASE=os.path.dirname(os.path.abspath(__file__))
app=open(os.path.join(BASE,'app.py'),encoding='utf-8').read(); ast.parse(app)
required=['/admin/login','/admin/logout','/admin','/admin/banks/new','/admin/banks/<bid>','/admin/banks/<bid>/edit','/admin/banks/<bid>/questions/new','/admin/banks/<bid>/questions/<int:qid>/edit','/admin/banks/<bid>/questions/<int:qid>/delete','/admin/attempts']
for route in required:
    assert route in app, f'missing route marker: {route}'
manifest=json.load(open(os.path.join(BASE,'data','manifest.json'),encoding='utf-8'))
assert manifest['phase']=='6D'
for fn in os.listdir(os.path.join(BASE,'data')):
    if fn.endswith('.json') and fn!='manifest.json':
        b=json.load(open(os.path.join(BASE,'data',fn),encoding='utf-8'))
        assert b['id'] and isinstance(b['questions'],list)
        ids=[]
        for q in b['questions']:
            assert len(q['options'])==4
            assert q['answer'] in range(4)
            ids.append(q['id'])
        assert len(ids)==len(set(ids)), f'duplicate question ids in {fn}'
print('Phase 6D static validation passed')
