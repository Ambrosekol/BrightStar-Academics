"""Adds Part Two (Features) and Part Three (Security) to guide.html. Run on the Part One file:
    python build.py base.html out.html
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from content_features import FEATURES
from content_security import SECURITY, SEC_FAQ
from content_facts import FACTS
from content_steps import STEPS
GROW = 26  # extra mm for every screenshot window, unless set below
HOVR = {
 'Signing in, workspaces and the app': 70, 'The Overview: a dashboard for every role': 72, 'Students: every record in one place': 72,
 'Parents and families': 88, 'Teachers and teaching duties': 90, 'Attendance': 98, 'Report cards': 78, 'Promotion to the next session': 88,
 'Exam and test timetables': 84, 'Fee structure and billing': 78, 'Recording payments and receipts': 88, 'Student accounts and collections': 98,
 'The school store': 98, 'The library': 98, 'Staff accounts': 98, 'Roles, permissions and limits': 84, 'Settings, branding and connections': 90,
 'Activity and notice logs': 88, 'A record of everything': 86,
}

BLUE, NAVY, RED, TEAL, INK, BODY, MUTED, LINE, SOFT, PAPER = '#0a1a8f', '#0b1d4d', '#d8261d', '#0f766e', '#12163a', '#3d4266', '#6b6f87', '#ded8c8', '#e9ecfb', '#f5f1e8'
F = 'font-family="Public Sans"'
M = 'font-family="IBM Plex Mono"'
S = 'font-family="Newsreader"'

# ---------------------------------------------------------------- svg helpers
def box(x, y, w, h, title, sub='', fill='#fff', stroke=LINE, tc=INK, sc=MUTED, r=10, ts=12, anchor='middle'):
    tx = x + w / 2 if anchor == 'middle' else x + 12
    out = f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" stroke="{stroke}"/>'
    if sub:
        out += f'<text x="{tx}" y="{y + h/2 - 3}" text-anchor="{anchor}" {F} font-weight="700" font-size="{ts}" fill="{tc}">{title}</text>'
        out += f'<text x="{tx}" y="{y + h/2 + 13}" text-anchor="{anchor}" {F} font-size="{ts-2.5}" fill="{sc}">{sub}</text>'
    else:
        out += f'<text x="{tx}" y="{y + h/2 + 4}" text-anchor="{anchor}" {F} font-weight="700" font-size="{ts}" fill="{tc}">{title}</text>'
    return out

def arrow(x1, y1, x2, y2, c=BLUE, w=2, dash=''):
    d = f' stroke-dasharray="{dash}"' if dash else ''
    return f'<path d="M{x1} {y1} L{x2} {y2}" stroke="{c}" stroke-width="{w}" fill="none" marker-end="url(#ar-{"r" if c == RED else "b"})"{d}/>'

def chip(x, y, w, text, fill=SOFT, color=BLUE, h=24, fs=10.5):
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{h/2}" fill="{fill}"/>'
            f'<text x="{x + w/2}" y="{y + h/2 + 4}" text-anchor="middle" {F} font-weight="600" font-size="{fs}" fill="{color}">{text}</text>')

def label(x, y, text, c='#8a8469', anchor='start', fs=9):
    return f'<text x="{x}" y="{y}" text-anchor="{anchor}" {M} font-size="{fs}" letter-spacing="1.4" fill="{c}">{text}</text>'

def t(x, y, text, fs=11, c=INK, w=400, anchor='start', fam=F):
    return f'<text x="{x}" y="{y}" text-anchor="{anchor}" {fam} font-size="{fs}" font-weight="{w}" fill="{c}">{text}</text>'

def phone(x, y, rows, title='Your School', sub='Portal', h=158):
    o = (f'<rect x="{x}" y="{y}" width="90" height="{h}" rx="14" fill="{INK}"/>'
         f'<rect x="{x+5}" y="{y+8}" width="80" height="{h-16}" rx="8" fill="{PAPER}"/>'
         f'<rect x="{x+5}" y="{y+8}" width="80" height="26" rx="8" fill="{NAVY}"/><rect x="{x+5}" y="{y+26}" width="80" height="8" fill="{NAVY}"/>'
         f'<rect x="{x+11}" y="{y+13}" width="15" height="15" rx="3" fill="#fff"/>'
         f'<text x="{x+31}" y="{y+21}" {S} font-weight="600" font-size="7" fill="#fff">{title}</text>'
         f'<text x="{x+31}" y="{y+30}" {F} font-size="5" fill="#c9cff0">{sub}</text>')
    yy = y + 40
    for (txt, col) in rows:
        o += f'<rect x="{x+10}" y="{yy}" width="70" height="15" rx="3" fill="{col}"/>'
        o += f'<text x="{x+14}" y="{yy+10}" {F} font-size="6" font-weight="600" fill="{BLUE if col != NAVY and col != RED else "#fff"}">{txt}</text>'
        yy += 19
    return o

def svg(inner, vb='0 0 600 150'):
    return (f'<svg viewBox="{vb}" width="100%"><defs>'
            f'<marker id="ar-b" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0 L10 5 L0 10 z" fill="{BLUE}"/></marker>'
            f'<marker id="ar-r" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0 L10 5 L0 10 z" fill="{RED}"/></marker>'
            f'</defs>{inner}</svg>')

def flow(items, y=45, h=60, gap=26, x0=0, total=600, colors=None):
    n = len(items); w = (total - gap * (n - 1)) / n; o = ''
    for i, (a, b) in enumerate(items):
        x = x0 + i * (w + gap)
        fill, stroke, tc, sc = ('#fff', LINE, INK, MUTED)
        if colors and colors[i]:
            fill, stroke, tc, sc = colors[i]
        o += box(x, y, w, h, a, b, fill, stroke, tc, sc)
        if i < n - 1:
            o += arrow(x + w + 3, y + h / 2, x + w + gap - 3, y + h / 2)
    return o

DARK = (NAVY, NAVY, '#fff', '#c9cff0')
REDC = (RED, RED, '#fff', '#ffe1df')
BLUC = (SOFT, '#c9d1f3', BLUE, BODY)

ART = {}
ART['import'] = svg(
    label(0, 16, 'THREE GUIDED STEPS') +
    flow([('1 · Students', 'records, classes, logins'), ('2 · History', 'past classes and years'), ('3 · Past results', 'earlier terms, released')], y=26, h=56, colors=[BLUC, BLUC, BLUC]) +
    box(0, 98, 600, 44, '412 imported  ·  1 skipped: line 88, guardian phone is missing  ·  0 duplicates', '', '#e3f0e8', '#bcd9c8', '#1d7a4b', ts=11.5))
ART['classes'] = svg(
    box(0, 40, 120, 70, 'JSS 1', '72 students', NAVY, NAVY, '#fff', '#c9cff0', ts=15) +
    arrow(124, 75, 168, 45) + arrow(124, 75, 168, 75) + arrow(124, 75, 168, 105) +
    box(172, 30, 110, 30, 'JSS 1A · 24', '', SOFT, '#c9d1f3', BLUE) + box(172, 62, 110, 30, 'JSS 1B · 24', '', SOFT, '#c9d1f3', BLUE) + box(172, 94, 110, 30, 'JSS 1C · 24', '', SOFT, '#c9d1f3', BLUE) +
    label(320, 22, 'A CLASS TRANSFER') +
    flow([('Request', 'by teacher'), ('Approve', 'new teacher'), ('Moved', 'new register')], y=40, h=70, gap=18, x0=320, total=280, colors=[None, BLUC, DARK]))
ART['subjects'] = svg(
    label(0, 14, 'SUBJECTS × CLASSES') +
    ''.join(t(0, 40 + i * 22, s, 10.5, INK, 600) for i, s in enumerate(['Mathematics', 'English', 'Physics', 'Literature'])) +
    ''.join(t(110 + j * 46, 22, c, 9, MUTED, 600, 'middle') for j, c in enumerate(['JSS1', 'JSS2', 'SSS1', 'SSS2'])) +
    ''.join(f'<circle cx="{110 + j*46}" cy="{36 + i*22}" r="6" fill="{c}"/>' for i, row in enumerate([[1,1,1,1],[1,1,1,1],[0,0,2,2],[0,0,3,3]]) for j, v in enumerate(row) for c in [[LINE, BLUE, TEAL, RED][v]]) +
    t(110, 132, '● all  ● Science  ● Art &amp; Commercial', 9, MUTED) +
    label(330, 14, 'THE SCHOOL YEAR') +
    box(330, 26, 130, 40, '2025/2026', 'kept as it was', '#fff', LINE) + box(470, 26, 130, 40, '2026/2027', 'current session', NAVY, NAVY, '#fff', '#c9cff0') +
    chip(330, 86, 84, 'First Term') + chip(423, 86, 92, 'Second Term', RED, '#fff') + chip(524, 86, 76, 'Third Term') +
    t(330, 134, 'Results, fees and registers file under the current session.', 9.5, MUTED))
ART['work'] = svg(flow([('Set work', 'whole class or chosen'), ('Students answer', 'on phone or computer'), ('Marked', 'by hand or automatically'), ('Term result', 'CA adds itself up')], y=30, h=64, colors=[None, None, BLUC, DARK]) +
    t(300, 128, 'Parents are alerted when work is set, and again when it is marked.', 10.5, MUTED, 400, 'middle'))
ART['cbt'] = svg(
    box(0, 10, 330, 130, '', '', '#fff', LINE) + label(14, 32, 'SECOND TERM EXAMINATION · SSS 1 MATHS') +
    t(14, 58, 'Q12. Solve 3x + 5 = 20', 13, INK, 600, fam=S) +
    ''.join(box(14 + i * 76, 72, 68, 26, f'{"ABCD"[i]}. {v}', '', SOFT if i == 1 else '#fff', BLUE if i == 1 else LINE, BLUE if i == 1 else INK, ts=10.5) for i, v in enumerate(['3', '5', '6', '15'])) +
    chip(14, 108, 80, 'Draft', '#fbf1dc', '#9a6400') + chip(102, 108, 120, 'Open to students', '#e3f0e8', '#1d7a4b') +
    box(350, 10, 120, 60, '30', 'questions', SOFT, '#c9d1f3', BLUE, ts=20) + box(480, 10, 120, 60, '60', 'marks', SOFT, '#c9d1f3', BLUE, ts=20) +
    box(350, 80, 120, 60, '45 min', 'time allowed', '#fff', LINE, INK, ts=16) + box(480, 80, 120, 60, '1.5 min', 'per question', '#fff', LINE, INK, ts=16))
ART['results'] = svg(
    label(0, 14, 'ENTER OFFLINE SCORES · SSS 2 CHEMISTRY') +
    f'<rect x="0" y="24" width="250" height="10" rx="5" fill="{LINE}"/><rect x="0" y="24" width="187" height="10" rx="5" fill="{BLUE}"/>' + t(258, 33, '18 of 24 done', 10, BLUE, 700) +
    ''.join(box(0, 44 + i * 32, 250, 26, f'{n}', '', '#fff', LINE, INK, ts=10.5, anchor='start') + t(240, 61 + i * 32, s, 10.5, TEAL if s != '—' else MUTED, 700, 'end') for i, (n, s) in enumerate([('Adaeze O.', '74%'), ('Bayo K.', '81%'), ('Chinedu A.', '—')])) +
    flow([('Subject teacher', 'records'), ('Class teacher', 'checks'), ('Released', 'family sees it')], y=52, h=56, gap=22, x0=340, total=260, colors=[None, BLUC, REDC]) +
    f'<path d="M490 112 C490 140 380 140 380 112" stroke="{RED}" stroke-width="1.6" fill="none" stroke-dasharray="4 4" marker-end="url(#ar-r)"/>' + t(435, 146, 'sent back to correct', 9, RED, 600, 'middle'))
ART['paystack'] = svg(flow([('Parent taps Pay', 'any amount owed'), ('Paystack checkout', 'card · transfer · USSD'), ('Confirmed', 'checked with Paystack'), ('Recorded', 'receipt sent, fee applied')], y=26, h=64, colors=[None, None, BLUC, DARK]) +
    t(300, 124, 'Money goes straight to your school\'s own Paystack account.', 11, BLUE, 700, 'middle') +
    t(300, 142, 'Refunds are only marked done once Paystack confirms the money went back.', 9.5, MUTED, 400, 'middle'))
ev = ['Work set or marked', 'Fee charged', 'Payment recorded', 'Report card ready', 'Timetable released', 'Store item collected']
ART['notify'] = svg(''.join(chip(0, 6 + i * 24, 180, e, '#fff', INK, h=20, fs=9.5) for i, e in enumerate(ev)) +
    ''.join(f'<path d="M184 {16 + i*24} C250 {16 + i*24} 250 75 300 75" stroke="#c9d1f3" stroke-width="1.4" fill="none"/>' for i in range(6)) +
    box(300, 50, 120, 50, 'Your school', 'sends for you', NAVY, NAVY, '#fff', '#c9cff0') +
    arrow(424, 75, 470, 30) + arrow(424, 75, 470, 75) + arrow(424, 75, 470, 120) +
    chip(474, 18, 126, 'In-app alert') + chip(474, 63, 126, 'Email') + chip(474, 108, 126, 'SMS', RED, '#fff'))
ART['candidate'] = svg(''.join(
    f'<g transform="translate({i*205} 8)"><rect width="190" height="130" rx="8" fill="#fff" stroke="{LINE}" stroke-dasharray="5 4"/>'
    f'<rect x="12" y="12" width="26" height="26" rx="5" fill="{NAVY}"/>' + t(46, 24, 'YOUR SCHOOL', 9, INK, 700) + t(46, 36, 'Entrance examination', 8, MUTED) +
    label(12, 62, 'CANDIDATE CODE', fs=7.5) + t(12, 80, code, 15, BLUE, 700, fam=M) +
    label(12, 102, 'PASSWORD', fs=7.5) + t(12, 120, 'K7WB-29Q', 12, INK, 600, fam=M) + '</g>'
    for i, code in enumerate(['ABC-2026-0141', 'ABC-2026-0142', 'ABC-2026-0143'])))
ART['banks'] = svg(''.join(box(i * 14, 10 + i * 12, 250, 70, f'{n}', f'{s}', '#fff', LINE, INK, anchor='start') for i, (n, s) in enumerate([('English · SSS 1', '42 questions'), ('General Knowledge · Year 7', '44 questions · department tagged'), ('Mathematics · Year 7', '45 questions · locked during exam')])) +
    f'<g transform="translate(250 104)"><rect width="22" height="18" rx="3" fill="{RED}"/><path d="M5 0 v-5 a6 6 0 0 1 12 0 v5" stroke="{RED}" stroke-width="2.4" fill="none"/></g>' +
    box(330, 20, 270, 110, '', '', NAVY, NAVY) + label(348, 44, 'PUBLIC PRACTICE PAGE', '#ffb3ae') +
    t(348, 70, 'yourschool…/entrance-practice', 12, '#fff', 600, fam=M) + t(348, 94, 'No sign-up. Last year\'s paper', 10.5, '#c9cff0') + t(348, 110, 'or a practice bank, never the live one.', 10.5, '#c9cff0'))
ART['results4'] = svg(''.join(chip(i * 128, 0, 120, n, NAVY if i == 2 else SOFT, '#fff' if i == 2 else BLUE) for i, n in enumerate(['By subject', 'Summary', 'Rankings', 'Activity'])) +
    t(560, 17, 'Export CSV', 10, TEAL, 700, 'end') +
    ''.join(f'<rect x="0" y="{34 + i*23}" width="600" height="20" rx="4" fill="{SOFT if i == 0 else "#fff"}" stroke="{LINE}"/>' + t(12, 48 + i * 23, str(i + 1), 10, BLUE, 700) + t(40, 48 + i * 23, n, 10, INK, 600) + t(330, 48 + i * 23, sc, 10, MUTED) + t(588, 48 + i * 23, p, 10, BLUE, 700, 'end')
            for i, (n, sc, p) in enumerate([('Candidate A', 'Maths 94 · English 90 · GK 93', '92.4%'), ('Candidate B', 'Maths 86 · English 91 · GK 87', '88.0%'), ('Candidate C', 'Maths 80 · English 84 · GK 80', '81.5%'), ('Candidate D', 'Maths 75 · English 79 · GK 77', '77.2%'), ('Candidate E', 'Maths 70 · English 77 · GK 72', '73.0%')])))
ART['admit'] = svg(label(0, 14, 'BISI · CHOSE ART · SSS 1') +
    ''.join(t(0, 44 + i * 30, d, 11, INK, 600) + f'<rect x="100" y="{32 + i*30}" width="260" height="16" rx="4" fill="#ede8dc"/><rect x="100" y="{32 + i*30}" width="{v*2.6}" height="16" rx="4" fill="{c}"/>' + t(108 + v * 2.6, 45 + i * 30, f'{v}%', 10.5, INK, 700)
            for i, (d, v, c) in enumerate([('Science', 81, BLUE), ('Art', 58, '#9b968a'), ('Commercial', 66, '#9b968a')])) +
    box(420, 26, 180, 52, 'Suggest Science', 'best by 15 points', RED, RED, '#fff', '#ffe1df', ts=14) +
    box(420, 88, 180, 52, 'Admit into SS 1A', 'record · login · parent', NAVY, NAVY, '#fff', '#c9cff0'))
ART['studentphone'] = svg(phone(0, 0, [('Tests · 2 open', SOFT), ('Practice', SOFT), ('Timetable', SOFT), ('Attendance 96%', SOFT), ('Report card ready', NAVY)], 'Your School', 'Student portal') +
    ''.join(chip(120 + (i % 2) * 245, 14 + (i // 2) * 34, 235, s) for i, s in enumerate(['Assignments with due dates', 'Tests and examinations', 'Unlimited practice', 'Exam timetable (PDF)', 'Attendance history', 'Results and report cards', 'Menu bar on phones', 'What\'s new for students'])), '0 0 600 160')
ART['parentphone'] = svg(phone(0, 0, [('3 children', SOFT), ('Owed ₦70,000', RED), ('Report card ready', SOFT), ('Messages · 1', SOFT), ('Store', SOFT)], 'Your School', 'Parent portal') +
    ''.join(chip(120 + (i % 2) * 245, 14 + (i // 2) * 34, 235, s) for i, s in enumerate(['Every child in one table', 'Fee account and receipts', 'Pay online, any amount', 'Results and report cards', 'Attendance and timetable', 'Messages with the school', 'The school store', 'Kept after a child leaves'])), '0 0 600 160')
ART['candphone'] = svg(phone(0, 0, [('Mathematics ✓', SOFT), ('English · Resume', SOFT), ('General Knowledge', SOFT), ('Retake granted', '#fbf1dc'), ('Admitted!', NAVY)], 'Your School', 'Candidate portal') +
    flow([('Sign in', 'candidate code'), ('Sit 3 papers', 'any order, timed'), ('Await release', 'result withheld'), ('Admitted', 'new student login')], y=46, h=62, gap=18, x0=120, total=480, colors=[None, None, BLUC, DARK]), '0 0 600 160')
ART['help'] = svg(box(0, 0, 600, 150, '', '', '#fff', LINE) + f'<rect x="0" y="0" width="600" height="26" rx="10" fill="#e9e5da"/>' +
    f'<rect x="14" y="38" width="150" height="24" rx="6" fill="{PAPER}" stroke="{LINE}"/>' + t(24, 54, '🔍 receipt', 10, MUTED) +
    ''.join(t(18, 82 + i * 16, s, 9.5, BLUE if i == 3 else BODY, 700 if i == 3 else 400) for i, s in enumerate(['Getting started', 'Academics', 'Report cards', 'Finance', 'Families', 'Entrance'])) +
    t(190, 56, 'Finance', 16, INK, 600, fam=S) + t(190, 76, 'Recording a payment: + Record payment opens a short form…', 10, BODY) +
    f'<rect x="190" y="88" width="250" height="52" rx="6" fill="{SOFT}"/>' + t(315, 118, '[ picture of the screen ]', 10, BLUE, 600, 'middle') +
    box(456, 88, 130, 52, 'What\'s new', '1 update for you', NAVY, NAVY, '#fff', '#c9cff0', ts=11))

ART['reportcard'] = svg(
    box(0, 0, 300, 150, '', '', '#fff', '#b9b2a0', r=6) + f'<rect x="0" y="0" width="300" height="5" fill="{NAVY}"/>' +
    f'<rect x="12" y="14" width="26" height="26" rx="5" fill="{NAVY}"/>' + t(25, 32, 'YS', 10, '#fff', 600, 'middle', fam=S) +
    t(46, 26, "Your School's Name", 12, INK, 600, fam=S) + t(46, 38, 'Second Term · 2026/2027 · JSS 3B', 8.5, MUTED) +
    f'<rect x="12" y="48" width="276" height="14" fill="{NAVY}"/>' + t(18, 58, 'SUBJECT', 7.5, '#fff', 700) + t(170, 58, 'CA', 7.5, '#fff', 700) + t(205, 58, 'EXAM', 7.5, '#fff', 700) + t(245, 58, 'TOTAL', 7.5, '#fff', 700) +
    ''.join(t(18, 76 + i * 15, n, 9, INK) + t(172, 76 + i * 15, c, 9, INK) + t(210, 76 + i * 15, e, 9, INK) + t(250, 76 + i * 15, tt, 9, BLUE, 700) for i, (n, c, e, tt) in enumerate([('Mathematics', '34', '52', '86 A'), ('English', '31', '47', '78 A'), ('Basic Science', '27', '37', '64 B')])) +
    t(18, 132, 'Overall 71.4% · A    Class average 62.9%', 9, BLUE, 700) +
    f'<path d="M190 140 c8 -10 14 4 22 -4 s10 -4 18 2" stroke="{BLUE}" fill="none" stroke-width="1.2"/>' +
    chip(330, 4, 130, 'Logo and colours') + chip(470, 4, 130, 'Class averages') +
    chip(330, 40, 130, 'Teacher comment') + chip(470, 40, 130, 'Trait ratings') +
    chip(330, 76, 130, 'Both signatures') + chip(470, 76, 130, 'Next-term date') +
    box(330, 112, 270, 36, 'Released → card ready → parents alerted', '', NAVY, NAVY, '#fff', ts=10.5))

# security art
def house(x, name, col, addr):
    return (f'<g transform="translate({x} 0)"><path d="M0 60 L80 10 L160 60 Z" fill="{col}"/><rect x="12" y="60" width="136" height="80" fill="#fff" stroke="{col}" stroke-width="2"/>'
            f'<rect x="66" y="100" width="28" height="40" fill="{col}"/><circle cx="88" cy="121" r="2.5" fill="#fff"/>'
            f'<rect x="24" y="74" width="30" height="22" fill="{SOFT}"/><rect x="106" y="74" width="30" height="22" fill="{SOFT}"/>'
            + t(80, 48, name, 11, '#fff', 700, 'middle') + t(80, 156, addr, 9, col, 600, 'middle', fam=M) + '</g>')
ART['houses'] = svg(house(0, 'Your School', BLUE, 'yourschool.edu.ng') + house(220, 'School B', RED, 'school-b.org') + house(440, 'School C', TEAL, 'school-c.sch.ng'), '0 0 600 162')
ART['lock'] = svg(box(0, 40, 150, 60, 'MyDog2014!', 'what you type', '#fff', LINE, INK) + arrow(154, 70, 210, 70) +
    f'<g transform="translate(220 22)"><path d="M10 0 h80 l-12 70 h-56 z" fill="{SOFT}" stroke="{BLUE}" stroke-width="2"/><rect x="26" y="70" width="48" height="22" rx="4" fill="{BLUE}"/>' + t(50, 46, 'one-way', 10, BLUE, 700, 'middle') + t(50, 116, 'scrambler', 10, MUTED, 600, 'middle') + '</g>' +
    arrow(330, 70, 386, 70) + box(390, 40, 210, 60, 'scrypt$9f2c…e81a7', 'what we keep: can\'t be reversed', NAVY, NAVY, '#fff', '#c9cff0'))
ART['keys'] = svg(f'<circle cx="70" cy="75" r="40" fill="none" stroke="{MUTED}" stroke-width="5"/>' + t(70, 80, 'Subject', 10, INK, 700, 'middle') + t(70, 93, 'Teacher', 10, INK, 700, 'middle') +
    ''.join(f'<path d="M110 75 L170 {y}" stroke="{MUTED}" stroke-width="2"/>' + box(174, y - 15, 190, 30, txt, '', fill, stroke, tc, ts=10.5) for y, txt, fill, stroke, tc in [(25, '✓ JSS 2 Maths marks', '#e3f0e8', '#bcd9c8', '#1d7a4b'), (75, '✓ Set Maths work', '#e3f0e8', '#bcd9c8', '#1d7a4b'), (125, '✓ JSS 2 register (view)', '#e3f0e8', '#bcd9c8', '#1d7a4b')]) +
    ''.join(box(400, y - 15, 200, 30, txt, '', '#f3f0e8', LINE, MUTED, ts=10.5) for y, txt in [(25, '✗ Fees and payments'), (75, '✗ Admissions waitlist'), (125, '✗ Other classes\' results')]))
ART['exam'] = svg(box(0, 20, 240, 110, '', '', '#fff', LINE) + label(14, 42, "CANDIDATE'S COMPUTER") + t(14, 70, 'Questions and options', 12, INK, 700) + t(14, 90, 'their own chosen answers', 10.5, MUTED) + t(14, 112, 'no answer key', 12, RED, 700) +
    f'<rect x="276" y="10" width="14" height="130" fill="{INK}"/>' + t(283, 156, 'the wall', 9, MUTED, 600, 'middle') +
    box(326, 20, 274, 110, '', '', NAVY, NAVY) + label(340, 42, 'BRIGHTSTARS (OUR SIDE)', '#ffb3ae') + t(340, 70, 'Correct answers', 12, '#fff', 700) + t(340, 90, 'the clock', 12, '#fff', 700) + t(340, 110, 'marking', 12, '#fff', 700), '0 0 600 160')
ART['money'] = svg(flow([('Parent pays', 'phone says "done"'), ('Brightstars asks', '"did this really pay?"'), ('Paystack replies', '"yes, ₦120,000"'), ('Then recorded', 'once, with a receipt')], y=30, h=66, colors=[None, BLUC, BLUC, DARK]) +
    t(300, 128, 'A double tap or a retried request goes back to the same checkout and never charges twice.', 10.5, MUTED, 400, 'middle'))
ART['files'] = svg(f'<path d="M0 30 h60 l12 14 h110 v100 h-182 z" fill="{SOFT}" stroke="{BLUE}" stroke-width="2"/>' + t(91, 100, 'students/', 13, BLUE, 700, 'middle', fam=M) +
    box(220, 10, 380, 140, '', '', '#fff', LINE) + label(236, 34, 'GUEST LIST · A STUDENT PHOTO') +
    ''.join(t(236, 62 + i * 22, s, 11.5, c, 600) for i, (s, c) in enumerate([('✓ Staff of the school', '#1d7a4b'), ('✓ The student it belongs to', '#1d7a4b'), ('✓ Parents linked to that student', '#1d7a4b'), ('✗ Everyone else, checked on every visit', RED)])))
ART['envelope'] = svg(''.join(f'<g transform="translate({i*205} 20)"><rect width="190" height="110" rx="6" fill="#fff" stroke="{LINE}"/><path d="M0 0 L95 60 L190 0" fill="none" stroke="{LINE}" stroke-width="2"/>'
    f'<circle cx="95" cy="62" r="16" fill="{RED}"/>' + t(95, 67, '🔒', 13, '#fff', 400, 'middle') + t(95, 100, n, 10.5, INK, 700, 'middle') + '</g>' for i, n in enumerate(['Email password', 'SMS token', 'Paystack secret key'])), '0 0 600 150')
ART['browser'] = svg(box(0, 0, 600, 150, '', '', '#fff', LINE) + f'<rect x="0" y="0" width="600" height="30" rx="10" fill="#e9e5da"/>' +
    f'<rect x="60" y="7" width="300" height="16" rx="8" fill="#fff"/>' + t(70, 19, '🔒 https://portal.yourschool.edu.ng', 9.5, '#1d7a4b', 600, fam=M) +
    ''.join(chip(20 + (i % 3) * 190, 48 + (i // 3) * 46, 180, s, f, c, 32, 10.5) for i, (s, f, c) in enumerate([('Encrypted (HTTPS)', '#e3f0e8', '#1d7a4b'), ('One cookie, no trackers', SOFT, BLUE), ('No outside scripts', SOFT, BLUE), ('Can\'t be embedded', SOFT, BLUE), ('Forged clicks blocked', SOFT, BLUE), ('Nothing private stored', '#fdeceb', RED)])))
ART['visitor'] = svg(box(0, 0, 600, 150, '', '', '#fff', LINE) + label(16, 24, 'YOUR ACTIVITY LOG · SEARCH "PLATFORM"') +
    ''.join(f'<line x1="16" y1="{36 + i*26}" x2="584" y2="{36 + i*26}" stroke="{LINE}"/>' + t(16, 54 + i * 26, d, 10.5, MUTED, 400, fam=M) + t(130, 54 + i * 26, 'platform@ops', 10.5, BLUE, 700, fam=M) + t(270, 54 + i * 26, a, 10.5, INK) for i, (d, a) in enumerate([('12 Mar 10:02', 'Entered the school (one-time pass)'), ('12 Mar 10:05', 'Updated school branding, as you asked'), ('12 Mar 10:07', 'Signed out')])))
ART['safe'] = svg(flow([('Action saved', 'payment recorded'), ('Written to the logbook', 'send the receipt'), ('Sent', 'email · SMS · in-app')], y=24, h=62, colors=[None, BLUC, DARK]) +
    f'<path d="M470 92 C470 130 300 130 300 92" stroke="{RED}" stroke-width="1.6" fill="none" stroke-dasharray="4 4" marker-end="url(#ar-r)"/>' + t(385, 142, 'power cut? retried, never lost', 10, RED, 700, 'middle'))

ICONS = ['i-shield', 'i-db', 'i-key', 'i-users', 'i-log', 'i-exam', 'i-pay', 'i-upload', 'i-msg', 'i-globe', 'i-admit', 'i-clock']

# ---------------------------------------------------------------- page builders
def run(right):
    return f'<div class="run"><span class="l"><img src="assets/brightstars-mark.png" alt="">Brightstars Academics</span><span>{right}</span></div>'

def foot(n):
    return f'<div class="foot"><span>BRIGHTSTARS ACADEMICS · SCHOOL PLATFORM GUIDE</span><span class="pn">{n:02d}</span></div>'

def shot_html(spec, caption, title=''):
    f, h, off = spec[:3]
    zoom = spec[3] if len(spec) > 3 else True
    h = HOVR.get(title, h + GROW)
    return (f'<div class="shot {"zoom" if zoom else "plain"}" style="height:{h + 5}mm"><div class="bar"><i></i><i></i><i></i><b>yourschool · demo data</b></div>'
            f'<div class="zwin" style="height:{h}mm"><img src="assets/shots/{f}" alt="" style="margin-top:-{off}%"></div></div>'
            f'<div class="caption">{caption}</div>')

def steps_html(title):
    st = STEPS.get(title)
    if not st: return ''
    return ('<div class="steps"><div class="kicker">How it works, step by step</div><div class="srow">' +
            ''.join(f'<div class="st"><b>{k}</b><span>{x}</span></div>' for k, x in enumerate(st, 1)) + '</div></div>')

def facts_html(title):
    fs = FACTS.get(title)
    if not fs: return ''
    return '<div class="facts">' + ''.join(f'<div><div class="fb">{a}</div><div class="fs">{b}</div></div>' for a, b in fs) + '</div>'

def stars():
    return ('<svg class="stars" viewBox="0 0 210 297" preserveAspectRatio="none"><g fill="none" stroke="#1b3277" stroke-width="0.5">'
            '<use href="#i-star" x="98" y="-18" width="150" height="150"/><use href="#i-star" x="120" y="4" width="105" height="105" stroke="#223c8a"/>'
            '<use href="#i-star" x="140" y="24" width="64" height="64" stroke="#2a479a"/></g>'
            '<g fill="#d8261d"><use href="#i-star" x="186" y="140" width="6" height="6"/><use href="#i-star" x="150" y="12" width="4" height="4"/></g>'
            '<g fill="#4c66d9"><use href="#i-star" x="196" y="118" width="4" height="4"/></g></svg>')

def section_cover(part, title, title_em, sub, items, start):
    rows = ''.join(f'<div class="ix"><b>{i+1:02d}</b><span>{name}</span><i>{start + i}</i></div>' for i, name in enumerate(items))
    return f'''<section class="page cover sec-cover">{stars()}
  <div class="plate"><img src="assets/brightstars-logo.png" alt="Brightstars"></div>
  <div class="title" style="top:66mm"><div class="ey">{part}</div><h1>{title} <em>{title_em}</em></h1><p class="sub">{sub}</p></div>
  <div class="index">{rows}</div>
</section>'''

def feature_page(i, f, n):
    media = shot_html(f['shot'], 'A real screen from the demonstration school. All names and figures are sample data.', f['title']) if f.get('shot') else f'<div class="card art">{ART[f["art"]]}</div>'
    who = ''.join(f'<span class="pill">{w}</span>' for w in f['who'])
    does = ''.join(f'<li>{d}</li>' for d in f['does'])
    return f'''<section class="page fpage">{run(f"Features · {i:02d}")}
  <div class="eyebrow">Feature {i:02d} · {f["group"]}</div>
  <h2 class="fh">{f["title"]}</h2>
  <p class="flead">{f["lead"]}</p>
  <div class="fbody"><div>{media}</div>
  {facts_html(f['title'])}
  {steps_html(f['title'])}
  <div class="fgrid">
    <div><div class="kicker">What you can do</div><ul class="ticks fl">{does}</ul></div>
    <div>
      <div class="ex"><div class="exl">Example</div><p>{f["example"]}</p></div>
      <div class="kicker" style="margin-top:3.6mm">Who uses it</div><div>{who}</div>
      <div class="why"><div class="kicker" style="color:#ffb3ae">Why it matters</div><p>{f["why"]}</p></div>
    </div>
  </div></div>
  {foot(n)}
</section>'''

def security_page(i, s, n):
    media = shot_html(s['shot'], 'A real activity log from the demonstration school. Platform support visits appear as platform@ops.', s['title']) if s.get('shot') else f'<div class="card art">{ART[s["art"]]}</div>'
    pts = ''.join(f'<li>{p}</li>' for p in s['points'])
    an_t, an_b = s['analogy']
    return f'''<section class="page fpage">{run(f"Security · {i:02d}")}
  <div class="eyebrow">Security {i:02d}</div>
  <h2 class="fh">{s["title"]}</h2>
  <p class="flead">{s["lead"]}</p>
  <div class="fbody"><div class="analogy"><div class="anl">Think of it like this: {an_t}</div><p>{an_b}</p></div>
  <div>{media}</div>
  {facts_html(s['title'])}
  <div class="fgrid">
    <div><div class="kicker">How it protects your school</div><ul class="ticks fl">{pts}</ul></div>
    <div><div class="ex"><div class="exl">Example</div><p>{s["example"]}</p></div></div>
  </div></div>
  {foot(n)}
</section>'''

def glance_page(n):
    tiles = [
        ('i-db', 'Own database per school', 'Your records are never stored alongside another school\'s.'),
        ('i-globe', 'Own web address', 'Sign-ins from one school are rejected at another.'),
        ('i-key', 'Scrambled passwords', 'Nobody can read a password, only reset it.'),
        ('i-clock', 'Guessing slowed', 'Paused after 8 wrong tries in 5 minutes.'),
        ('i-users', '100+ permissions', 'Each job sees only what it needs.'),
        ('i-shield', 'Admin-only areas', 'Staff, roles, payments and branding stay with the School Admin.'),
        ('i-log', 'Activity log', 'Who, what, when and from where, refusals included.'),
        ('i-exam', 'Fair exams', 'Answers never reach the candidate\'s device.'),
        ('i-pay', 'Verified payments', 'Every online payment confirmed with Paystack.'),
        ('i-receipt', 'Void, never delete', 'Corrections always leave a trail.'),
        ('i-upload', 'Guarded photos', 'Children\'s pictures only for staff, the child and their parents.'),
        ('i-msg', 'Encrypted secrets', 'Mail, SMS and Paystack keys sealed per school.'),
        ('i-phone', 'Nothing left on phones', 'The app never stores private pages.'),
        ('i-check', 'No trackers', 'No ads, analytics or outside scripts.'),
        ('i-admit', 'Recorded support visits', 'Our team\'s visits appear in your log.'),
        ('i-book', 'NDPR-aligned', 'Written to align with Nigeria\'s Data Protection Act 2023.'),
    ]
    cells = ''.join(f'<div class="gt"><div class="ico {["", "red", "teal"][k % 3]}"><svg><use href="#{ic}"/></svg></div><h4>{h}</h4><p class="small" style="margin:0">{p}</p></div>' for k, (ic, h, p) in enumerate(tiles))
    return f'''<section class="page">{run("Security · at a glance")}
  <div class="eyebrow">Security 01 · At a glance</div>
  <h2>Sixteen ways your school is protected.</h2>
  <p class="lead" style="margin-bottom:6mm">The pages that follow explain each protection in everyday language, with an example from school life. Here they are on one page, for the busy proprietor.</p>
  <div class="glance">{cells}</div>
  <div class="band-cta" style="margin-top:6mm"><div><h3>Security isn't an extra. It's how the platform is built.</h3><span class="small">None of this costs extra or needs switching on. Every school gets every protection from the moment it's created.</span></div></div>
  {foot(n)}
</section>'''

def sec_faq_page(n):
    qs = ''.join(f'<div class="faq"><h4>{q}</h4><p>{a}</p></div>' for q, a in SEC_FAQ)
    return f'''<section class="page">{run("Security · questions")}
  <div class="eyebrow">Security {len(SECURITY) + 2:02d} · Questions</div>
  <h2>What proprietors ask us about safety.</h2>
  <div class="sfaq" style="columns:2; column-gap:9mm; margin-top:6mm">{qs}</div>
  <div class="band-cta" style="position:absolute; left:17mm; right:17mm; bottom:22mm"><div><h3>Want to go deeper?</h3><span class="small">Our full privacy and data-protection statement is linked from every school's sign-in page, and we're happy to walk your board or IT adviser through it.</span></div><div class="go">Ask us →</div></div>
  {foot(n)}
</section>'''

CSS = '''
/* ---------- Parts two and three ---------- */
.sec-cover .title h1 { font-size: 52pt; }
.sec-cover .index { position:absolute; left:17mm; right:17mm; bottom:16mm; columns:2; column-gap:8mm; background: rgba(255,255,255,.04); border-top:1.4mm solid var(--red); padding:5mm 0 0; }
.sec-cover .ix { display:flex; gap:2.6mm; break-inside:avoid; padding:1.25mm 0; border-bottom:1px dotted #2a479a; font-size:8.4pt; color:#e3e7fb; }
.sec-cover .ix b { font: 400 7.6pt/1.5 var(--mono); color:#ff8f88; width:6mm; }
.sec-cover .ix span { flex:1; }
.sec-cover .ix i { font-style:normal; font-family:var(--mono); font-size:7.6pt; color:#9aa5dc; }
.fpage { display:flex; flex-direction:column; }
.fbody { flex:1; display:flex; flex-direction:column; justify-content:space-evenly; min-height:0; }
.fbody > * { margin-top:0 !important; margin-bottom:0 !important; }
.steps .srow { display:grid; grid-template-columns: repeat(4, 1fr); gap: 3mm; }
.steps .st { background: var(--surface); border:1px solid var(--line); border-radius: 2.4mm; padding: 3mm; display:flex; gap:2.4mm; align-items:flex-start; }
.steps .st b { flex:none; width:7mm; height:7mm; border-radius:50%; background: var(--red); color:#fff; display:grid; place-items:center; font: 600 10pt/1 var(--serif); }
.steps .st span { font-size: 8.5pt; line-height: 1.38; color: var(--ink); }
.fpage h2.fh { font-size: 22pt; margin-bottom: 2.6mm; }
.fpage .flead { font-size: 11pt; line-height: 1.48; color: var(--body); margin-bottom: 4mm; }
.shot.zoom .zwin { overflow:hidden; position:relative; }
.shot.zoom .zwin img { width:121.5%; margin-left:-17.6%; display:block; max-width:none; }
.shot.plain .zwin img { width:100%; display:block; }
.facts { display:grid; grid-template-columns: repeat(3, 1fr); gap: 3.5mm; margin-top: 4.5mm; }
.facts > div { background: var(--surface); border:1px solid var(--line); border-top:1.2mm solid var(--blue); border-radius: 0 0 2.4mm 2.4mm; padding: 3mm 3.6mm; }
.facts > div:nth-child(2) { border-top-color: var(--red); } .facts > div:nth-child(3) { border-top-color: var(--teal); }
.facts .fb { font: 600 17pt/1.05 var(--serif); color: var(--blue); }
.facts > div:nth-child(2) .fb { color: var(--red); } .facts > div:nth-child(3) .fb { color: var(--teal); }
.facts .fs { font-size: 8.2pt; line-height: 1.35; color: var(--body); margin-top: 1.2mm; }
.card.art { padding: 4mm 5mm; }
.fgrid { display:grid; grid-template-columns: 1.42fr 1fr; gap: 6mm; margin-top: 4.5mm; align-items:start; }
ul.fl li { font-size: 9.1pt; line-height: 1.46; margin-bottom: 1.9mm; }
ul.fl li::before { top: 0.9mm; }
.ex { background: var(--blue-tint); border:1px solid #d5daf5; border-left: 1.2mm solid var(--blue); border-radius: 2.4mm; padding: 3.4mm 4mm; }
.ex .exl { font: 400 7pt/1 var(--mono); letter-spacing: .16em; text-transform: uppercase; color: var(--blue); margin-bottom: 1.8mm; }
.ex p { font-family: var(--serif); font-size: 10.8pt; line-height: 1.42; color: var(--ink); margin: 0; }
.why { background: var(--navy); color:#d9def5; border-radius: 2.4mm; padding: 3.4mm 4mm; margin-top: 3.6mm; }
.why p { margin:0; font-size: 9.2pt; line-height:1.45; }
.analogy { border: 1px solid #f0c9c6; background: #fdf4f3; border-radius: 2.4mm; padding: 3.2mm 4.2mm; margin-bottom: 4mm; }
.analogy .anl { font: 600 10.6pt/1.3 var(--serif); color: var(--red-deep); margin-bottom: 1.2mm; }
.analogy p { margin:0; font-size: 9pt; line-height: 1.5; color: var(--ink); }
.glance { display:grid; grid-template-columns: repeat(4, 1fr); gap: 3.4mm; }
.sfaq .faq { margin-bottom: 5.4mm; }
.sfaq .faq h4 { font-size: 12.6pt; margin-bottom: 1.6mm; }
.sfaq .faq p { font-size: 9.8pt; line-height: 1.5; }
.gt { background: var(--surface); border:1px solid var(--line); border-radius: 2.6mm; padding: 4.6mm 4mm; }
.gt h4 { font-size: 10pt; }
.gt p { font-size: 8.8pt !important; }
'''

CHECK = '''<div id="ovf" style="display:none"></div><script>
window.addEventListener('load',function(){setTimeout(function(){var bad=[];document.querySelectorAll('.page').forEach(function(p,i){var f=p.querySelector('.foot');if(!f)return;var ft=f.getBoundingClientRect().top;var pr=p.getBoundingClientRect();var mx=0;p.querySelectorAll(':scope > *').forEach(function(c){if(c.classList.contains('foot')||c.classList.contains('run')||getComputedStyle(c).position==='absolute')return;var b=c.getBoundingClientRect().bottom;if(b>mx)mx=b;});if(mx>ft-2)bad.push((i+1)+':'+Math.round(mx-ft));});document.getElementById('ovf').textContent='OVF['+bad.join(',')+']';},500);});
</script>'''


def main(src, dst):
    s = open(src, encoding='utf-8').read()
    s = s.replace('</style>', CSS + '</style>', 1)
    # factual corrections to Part One
    s = s.replace('<li><strong>Nothing lost mid-exam.</strong> Every answer is saved the moment it is chosen. If power or the network drops, the candidate carries on exactly where they were.</li>',
                  '<li><strong>Nothing lost mid-exam.</strong> Every answer is saved as the candidate presses Save &amp; Next. If power or the network drops, the candidate carries on where they were, and up to five minutes of lost time is given back.</li>')
    s = s.replace('<p>Every answer is saved the moment it\'s chosen, and the clock is kept by the server. A candidate mid-paper carries on where they were, even after a restart. Staff can also grant a retake.</p>',
                  '<p>Each answer is saved as the candidate presses Save &amp; Next, and the clock is kept by the server. A candidate mid-paper carries on where they were, even after a restart, and a retake can be granted.</p>')
    s = s.replace('A focused and diligent term. Keep it up.', 'A focused and diligent term. Keep it up.')
    start_f = 23
    n_f = len(FEATURES)
    sec_cover_n = start_f + n_f
    glance_n = sec_cover_n + 1
    start_s = glance_n + 1
    faq_n = start_s + len(SECURITY)
    # table of contents
    s = s.replace('<div><b>19</b><span>Questions schools ask</span><i>21</i></div>',
                  '<div><b>19</b><span>Questions schools ask</span><i>21</i></div>\n'
                  f'    <div><b style="color:var(--blue)">II</b><span><strong>Part Two · {n_f} features in depth</strong></span><i>22</i></div>\n'
                  f'    <div><b style="color:var(--blue)">III</b><span><strong>Part Three · Security in depth</strong></span><i>{sec_cover_n}</i></div>')
    pages = [section_cover('Part two · In depth', 'Features', 'in full.',
                           f'Every part of a Brightstars school portal, one page each: what it does, a real example from school life, who uses it and why it matters. Written for school leaders, not engineers.',
                           [f['title'] for f in FEATURES], start_f)]
    for i, f in enumerate(FEATURES, 1):
        pages.append(feature_page(i, f, start_f + i - 1))
    pages.append(section_cover('Part three · In depth', 'Security', 'made simple.',
                               'How Brightstars protects your school, your children\'s information and your money, explained without jargon, with an everyday comparison and an example for every safeguard.',
                               ['Security at a glance'] + [x['title'] for x in SECURITY] + ['Security questions'], glance_n))
    pages.append(glance_page(glance_n))
    for i, x in enumerate(SECURITY, 1):
        pages.append(security_page(i + 1, x, start_s + i - 1))
    pages.append(sec_faq_page(faq_n))
    marker = '<!-- =============================== 22. BACK COVER =============================== -->'
    assert marker in s
    s = s.replace(marker, '\n'.join(pages) + '\n' + marker)
    if '--check' in sys.argv:
        s = s.replace('</body>', CHECK + '</body>')
    open(dst, 'w', encoding='utf-8').write(s)
    print('pages added:', len(pages), 'features', n_f, 'security', len(SECURITY), 'last numbered', faq_n)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
