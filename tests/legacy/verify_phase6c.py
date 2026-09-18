import json, os, ast
BASE=os.path.dirname(os.path.abspath(__file__)); DATA=os.path.join(BASE,'data')
for fn in os.listdir(DATA):
    if fn.endswith('.json'):
        with open(os.path.join(DATA,fn),encoding='utf-8') as f: b=json.load(f)
        if fn!='manifest.json':
            assert b['questions']
            for q in b['questions']:
                assert len(q['options'])==4
                assert 0 <= q['answer'] < 4
        print('OK',fn)
print('Phase 6C bank validation passed')
