"""Optional local-only demo data for testing the Phase 6G candidate workflow.

This creates three clearly marked DEMO banks with two questions each.
It is NOT examination content and must NOT be used for the real school exam.
Run from the project folder with: py seed_demo_banks.py
"""
import json, os
BASE=os.path.dirname(os.path.abspath(__file__))
DATA=os.path.join(BASE,'data')
os.makedirs(DATA,exist_ok=True)

banks=[
 {
  'id':'demo_year7_mathematics','name':'DEMO Year 7 Mathematics','level':'Year 7','entry_group':'year7','subject':'Mathematics','duration_seconds':300,'version':'demo','source_status':'DEMO ONLY',
  'questions':[
   {'id':1,'text':'DEMO: What is 2 + 3?','options':['4','5','6','7'],'answer':1,'points':1},
   {'id':2,'text':'DEMO: What is 10 - 4?','options':['4','5','6','7'],'answer':2,'points':1}
  ]
 },
 {
  'id':'demo_year7_english','name':'DEMO Year 7 English','level':'Year 7','entry_group':'year7','subject':'English','duration_seconds':300,'version':'demo','source_status':'DEMO ONLY',
  'questions':[
   {'id':1,'text':'DEMO: Choose the noun.','options':['run','beautiful','teacher','quickly'],'answer':2,'points':1},
   {'id':2,'text':'DEMO: Choose the correct spelling.','options':['becouse','because','becaus','beacause'],'answer':1,'points':1}
  ]
 },
 {
  'id':'demo_general_knowledge','name':'DEMO General Knowledge','level':'Shared','entry_group':'year7','subject':'General Knowledge','duration_seconds':300,'version':'demo','source_status':'DEMO ONLY',
  'questions':[
   {'id':1,'text':'DEMO: How many days are in a week?','options':['5','6','7','8'],'answer':2,'points':1},
   {'id':2,'text':'DEMO: Which planet is known as the Red Planet?','options':['Earth','Mars','Venus','Jupiter'],'answer':1,'points':1}
  ]
 },
 {
  'id':'demo_year10_mathematics','name':'DEMO Year 10 Mathematics','level':'Year 10','entry_group':'year10','subject':'Mathematics','duration_seconds':300,'version':'demo','source_status':'DEMO ONLY',
  'questions':[
   {'id':1,'text':'DEMO: Solve 3x = 12.','options':['2','3','4','5'],'answer':2,'points':1},
   {'id':2,'text':'DEMO: What is 5 squared?','options':['10','15','20','25'],'answer':3,'points':1}
  ]
 },
 {
  'id':'demo_year10_english','name':'DEMO Year 10 English','level':'Year 10','entry_group':'year10','subject':'English','duration_seconds':300,'version':'demo','source_status':'DEMO ONLY',
  'questions':[
   {'id':1,'text':'DEMO: Choose the synonym of "rapid".','options':['slow','quick','weak','late'],'answer':1,'points':1},
   {'id':2,'text':'DEMO: Choose the correctly punctuated sentence.','options':['He said "come here".','He said, "Come here."','He said Come here.','He said, Come here.'],'answer':1,'points':1}
  ]
 }
]
for b in banks:
    with open(os.path.join(DATA,b['id']+'.json'),'w',encoding='utf-8') as f:
        json.dump(b,f,ensure_ascii=False,indent=2)
print('DEMO banks created. These are for local testing only, not the real examination.')
