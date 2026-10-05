"""The school's own user guide: plain-language, hand-written pages that explain what each part of
the portal is for and how to use it, for the staff who use it — not the platform's own documentation
(``/docs``, for platform operators, generated from the code) and not a school's public site (schools
have none: see the module docstring of ``control_plane/platform_pages.py``).

Any signed-in admin may read it, in either workspace: it carries no permission of its own, so an
unmapped endpoint falls back to ``admin.access`` (core/security.py), which every staff account holds.
The guide is the same for every school; it is not branded or generated per school, only its reader is
whoever happens to be signed in there.
"""

import html
import re
from functools import lru_cache

from flask import abort, render_template, request

from app import app
from core.security import admin_required

# (slug, title, one line). The order is the reading order and the order of the sidebar.
GUIDE_GROUPS = (
    ('Getting started', (
        ('welcome', 'Welcome to your guide', 'What this is, and the two workspaces'),
        ('dashboard', 'Your school dashboard', 'The home page: setup, Top of the Class and what is new'),
    )),
    ('Set up the school', (
        ('classes-subjects', 'Classes and subjects', 'The shape of a school year'),
        ('roles-permissions', 'Roles, permissions and scope', 'Who can do what, and how it is limited'),
        ('staff-admin', 'Staff, roles and branding', 'Accounts, audit and how the portal looks'),
    )),
    ('Students and families', (
        ('students', 'Students', 'Registering, importing, archiving'),
        ('families', 'Parents and messages', 'Parent accounts, feedback and notices'),
        ('attendance', 'Attendance', 'The daily register and the term summary'),
    )),
    ('Academics', (
        ('work', 'Assignments, projects and tests', 'Setting work, issuing it to a class, marking it'),
        ('timetable', 'Exam and test timetables', 'Building a draft, then releasing it'),
        ('results-report-cards', 'Results and report cards', 'From a mark to a report card a parent can see'),
    )),
    ('Money and library', (
        ('finance', 'Fees, payments and receipts', 'Charging, collecting and receipting'),
        ('library', 'Library', 'Books, members and loans'),
    )),
    ('Entrance workspace', (
        ('entrance', 'Entrance examinations', 'Banks, candidates, the exam and results'),
        ('admissions', 'Admissions: candidate to student', 'The waitlist, admitting and declining'),
    )),
)

GUIDE_PAGES = {slug: (title, blurb, group) for group, items in GUIDE_GROUPS for slug, title, blurb in items}
GUIDE_ORDER = [slug for _, items in GUIDE_GROUPS for slug, _, _ in items]


def _neighbours(slug):
    index = GUIDE_ORDER.index(slug)
    before = GUIDE_ORDER[index - 1] if index else None
    after = GUIDE_ORDER[index + 1] if index + 1 < len(GUIDE_ORDER) else None
    return ({'slug': before, 'title': GUIDE_PAGES[before][0]} if before else None,
            {'slug': after, 'title': GUIDE_PAGES[after][0]} if after else None)


def _render(slug):
    title, blurb, group = GUIDE_PAGES[slug]
    before, after = _neighbours(slug)
    return render_template(f'school_guide/{slug}.html', slug=slug, title=title, blurb=blurb, group=group,
                           groups=GUIDE_GROUPS, before=before, after=after)


@app.route('/admin/guide')
@admin_required
def admin_guide_home():
    return _render('welcome')


@app.route('/admin/guide/<slug>')
@admin_required
def admin_guide_page(slug):
    if slug not in GUIDE_PAGES or slug == 'welcome':
        abort(404)
    return _render(slug)


# ------------------------------------------------------------------------------- search
def _plain(markup):
    text = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', markup, flags=re.S)
    return ' '.join(html.unescape(re.sub(r'<[^>]+>', ' ', text)).split())


@lru_cache(maxsize=1)
def _index_pages_text(_marker):
    """The plain text of every guide page, rendered once (the key only lets a test reset it)."""
    found = []
    for slug in GUIDE_ORDER:
        found.append({'title': GUIDE_PAGES[slug][0], 'where': GUIDE_PAGES[slug][2],
                      'url': f'/admin/guide/{slug}' if slug != 'welcome' else '/admin/guide',
                      'text': _plain(_render(slug))})
    return found


def _search(query):
    words = [w.lower() for w in query.split()]
    scored = []
    for entry in _index_pages_text(0):
        title, text = entry['title'].lower(), entry['text'].lower()
        if not all(w in title or w in text for w in words):
            continue
        score = sum(10 for w in words if w in title) + sum(min(text.count(w), 5) for w in words)
        first = min((text.find(w) for w in words if w in text), default=-1)
        snippet = ''
        if first >= 0:
            raw = entry['text']
            snippet = ('…' if first > 60 else '') + raw[max(0, first - 60):first + 140].strip() + '…'
        scored.append((score, {**entry, 'snippet': snippet}))
    scored.sort(key=lambda pair: (-pair[0], pair[1]['title']))
    return [entry for _, entry in scored[:30]]


@app.route('/admin/guide/search')
@admin_required
def admin_guide_search():
    query = ' '.join(request.args.get('q', '').split())[:80]
    return render_template('school_guide/search.html', slug='search', title='Search the guide', blurb='', group='',
                           groups=GUIDE_GROUPS, before=None, after=None, query=query,
                           results=_search(query) if len(query) >= 2 else [])
