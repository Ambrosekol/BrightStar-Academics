import os, sqlite3
from app import app, init_db, DB
init_db()
assert os.path.exists(DB)
client=app.test_client()
r=client.get('/health'); assert r.status_code==200 and r.json['status']=='ok'
r=client.post('/login', data={'candidate':'Phase 6B Test Candidate'}, follow_redirects=False); assert r.status_code==302
r=client.get('/exam?q=1'); assert r.status_code==200 and b'What is 12 + 8?' in r.data
r=client.post('/answer', data={'question_id':'1','option_index':'1'}); assert r.status_code==200 and r.json['ok']
r=client.get('/exam?q=1'); assert b'value="1" checked' in r.data
r=client.post('/submit', follow_redirects=False); assert r.status_code==302
r=client.get('/result'); assert r.status_code==200 and b'1 / 10' in r.data
print('PHASE 6B VERIFICATION: PASS')
