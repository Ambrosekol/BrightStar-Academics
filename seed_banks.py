import json, os
BASE=os.path.dirname(os.path.abspath(__file__))
DATA=os.path.join(BASE,'data')
os.makedirs(DATA,exist_ok=True)

english=[
("The professor gave a lucid explanation of the theory.",["obscure","clear","lengthy","doubtful"],1),
("His decision was considered arbitrary by many.",["random","fair","deliberate","wise"],0),
("The student gave a plausible excuse for his lateness.",["believable","false","weak","doubtful"],0),
("Her response was rather evasive.",["direct","avoiding","rude","polite"],1),
("The student was reluctant to answer the question.",["eager","unwilling","ready","bold"],1),
("The manager tried to mitigate the problem.",["worsen","control","reduce","ignore"],2),
("The situation became critical within minutes.",["dangerous","calm","simple","funny"],0),
("The boy's behaviour was erratic.",["steady","unpredictable","calm","polite"],1),
("The teacher was adamant about the rules.",["flexible","stubborn","unsure","gentle"],1),
("The man gave a vague explanation of the incident.",["clear","detailed","unclear","honest"],2),
("The judge's decision was impartial.",["equitable","prejudiced","objective","neutral"],1),
("The hall was spacious enough to accommodate everyone.",["extensive","confined","airy","broad"],1),
("His argument was highly convincing.",["persuasive","credible","dubious","logical"],2),
("She remained optimistic despite the setback.",["enthusiastic","hopeful","despondent","confident"],2),
("The glass was completely transparent.",["visible","translucent","obscure","opaque"],3),
("The child was obedient to all instructions.",["compliant","defiant","respectful","attentive"],1),
("The lecture was rather tedious.",["engaging","lengthy","exhausting","repetitive"],0),
("He is widely known for being generous.",["charitable","benevolent","miserly","kind"],2),
("Her answer was accurate.",["precise","exact","erroneous","correct"],2),
("The environment was extremely hostile.",["aggressive","amicable","unfriendly","tense"],1),
("Neither the teacher nor the students _____ present at the meeting.",["was","were","is","be"],1),
("Hardly had she arrived _____ the rain started.",["than","when","then","that"],1),
("If I _____ you, I would apologize immediately.",["am","was","were","be"],2),
("He insisted that she _____ the truth.",["tells","told","tell","telling"],2),
("The book, along with the pens, _____ missing.",["are","were","is","be"],2),
("By this time next year, she _____ her degree.",["completes","will complete","would have completed","completed"],2),
("No sooner had he finished speaking _____ the audience applauded.",["when","than","then","that"],1),
("She is one of the students who _____ always punctual.",["is","was","are","be"],2),
("The news _____ shocking to everyone.",["were","are","is","be"],2),
("I would rather you _____ the truth.",["tell","told","telling","tells"],1),
("Which word has the same consonant sound as /ʃ/?",["Measure","Ship","Vision","Garage"],1),
("Which word has the same vowel sound as /eɪ/?",["Bed","Make","Sit","Pot"],1),
("Which word has the same vowel sound as /ɔː/?",["Cut","Saw","Cat","Pen"],1),
("Which word has the same vowel sound as /ɪ/?",["Seat","Sit","Cite","Suit"],1),
("Which word has the same consonant sound as /tʃ/?",["Church","Judge","Pleasure","Vision"],0),
("A poem of six lines is called a/an _____.",["couplet","triplet","sestet","octave"],2),
("A figure of speech that compares two things using 'like' or 'as' is a/an _____.",["metaphor","simile","irony","hyperbole"],1),
("A story with a moral lesson is called a _____.",["fable","novel","play","poem"],0),
("The main character in a literary work is the _____.",["antagonist","protagonist","narrator","poet"],1),
("A humorous play is known as a/an _____.",["tragedy","comedy","epic","satire"],1),
]

def make_bank(bank_id,name,level,duration,qs):
    return {"id":bank_id,"name":name,"level":level,"duration_seconds":duration,"version":"6C.1","source_status":"verified_from_supplied_question_sheet","questions":[{"id":i+1,"text":q,"options":opts,"answer":ans,"points":1} for i,(q,opts,ans) in enumerate(qs)]}

banks=[make_bank("jss3_english_2026","JSS 3 → SS 1 Entrance — English Studies (Objective)","Year 10 / SS 1",3600,english),
       make_bank("phase6b_test","Phase 6B Engine Test Bank","TEST",300,[("What is 12 + 8?",["18","20","22","24"],1),("Which number is a prime number?",["9","15","17","21"],2),("Choose the word closest in meaning to 'rapid'.",["slow","quick","weak","late"],1),("If 5 tins cost ₦250, what is the cost of 10 tins at the same rate?",["₦300","₦400","₦500","₦750"],2),("Neither the teacher nor the students ___ present.",["was","were","is","be"],1),("What is 101₂ in base ten?",["3","4","5","6"],2),("The opposite of 'generous' is:",["kind","charitable","miserly","benevolent"],2),("A figure of speech using 'like' or 'as' is called a:",["metaphor","simile","irony","hyperbole"],1),("If x + 7 = 15, find x.",["6","7","8","9"],2),("Which is the correct spelling?",["acurate","accurate","acurrate","accuratte"],1)])]
for b in banks:
    with open(os.path.join(DATA,b['id']+'.json'),'w',encoding='utf-8') as f: json.dump(b,f,ensure_ascii=False,indent=2)
with open(os.path.join(DATA,'manifest.json'),'w',encoding='utf-8') as f:
    json.dump({"phase":"6C","banks":[{"id":b['id'],"name":b['name'],"question_count":len(b['questions']),"source_status":b['source_status']} for b in banks],"pending_banks":["Year 7 Mathematics","Year 10 Mathematics","Year 7 English","General Knowledge"],"pending_reason":"The available source material for these banks is incomplete/contains image placeholders in the parsed text; they are not silently reconstructed in Phase 6C."},f,ensure_ascii=False,indent=2)
