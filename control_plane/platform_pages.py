"""The pages the platform serves besides its console: ``/marketing``, ``/docs`` and ``/privacy``.

* ``/marketing`` is public: the page a school (or anyone) is shown to understand what Brightstars
  Academics is. It is on the platform hostnames only, like the console, because a school's own address
  is a portal and never a website.
* ``/docs`` is the platform's own documentation: how it works, how to run it, how to change it, and a
  reference generated from the code (``control_plane/docsgen.py``). It is not public. It needs a signed-in
  platform admin who has been given access: the super admin always has it, and grants it to others one at
  a time on the Team page. Anyone else gets the same 404 as for any address that does not exist, so the
  page cannot even be found by someone who should not read it. Signing in first is the only way in.
* ``/privacy`` is the platform's data-protection statement. Unlike the other two, it is registered as
  a plain route with no host guard at all, so it is reachable both on a platform hostname (a school
  considering the platform reads it before signing anything) and on every school's own address (that
  school's own parents and staff can read it too, from their own sign-in page). ``resolver.py`` still
  has to let it through explicitly on a platform hostname (``PLATFORM_SITE_PATHS``); on a school's own
  address nothing needs to, because only the console's own paths are restricted there.
"""

import html
import os
import re
from functools import lru_cache, wraps

from flask import abort, g, render_template, request

from app import app

from . import docsgen
from .console import platform_host_only, platform_required

# (slug, title, one line). The order is the reading order and the order of the sidebar.
DOC_GROUPS = (
    ('Start here', (
        ('welcome', 'Welcome', 'What this is, and how to read these pages'),
        ('getting-started', 'Getting started', 'Run it on your own machine in ten minutes'),
        ('tour', 'A tour of the code', 'Where everything lives, folder by folder'),
    )),
    ('How it works', (
        ('architecture', 'Architecture', 'One deployment, many schools: how a request finds its school'),
        ('platform-console', 'The platform console', 'Schools, addresses, the team, entering a school'),
        ('school-portal', 'The school portal', 'Staff, roles, permissions, scope, audit'),
        ('academics', 'Academics', 'Sessions, classes, tests, results, report cards, practice'),
        ('entrance', 'Entrance examinations', 'Question banks, candidates, the exam engine, practice'),
        ('finance', 'Finance and receipts', 'Fees, payments, allocation, the receipt'),
        ('families', 'Students, parents and messages', 'Their portals, and how parents are told things'),
        ('branding-numbering', 'Identity and numbering', "A school's look, its files, its number patterns"),
    )),
    ('Run it', (
        ('deployment', 'Deployment', 'Servers, environment, proxy, TLS, first production launch'),
        ('operations', 'Operating it', 'Backups, upgrades, restarts, limits, monitoring'),
        ('security', 'Security model', 'What protects what, and why'),
        ('recommendations', 'Recommendations', 'What we would do next, and what to watch'),
    )),
    ('Build on it', (
        ('contributing', 'Contributing', 'Conventions, and the mistakes worth not repeating'),
        ('recipes', 'Recipes', 'Step-by-step: add a page, a permission, a column, a school setting'),
        ('testing', 'Testing', 'The contract suite, the end-to-end scripts, and the guards'),
    )),
    ('Reference', (
        ('reference-modules', 'Modules', 'Every Python file, what it is for, and its functions'),
        ('reference-routes', 'Routes', 'Every URL, who may call it, and where it is served'),
        ('reference-data', 'Data model', 'Every table and column of a school and of the registry'),
        ('reference-permissions', 'Permissions and roles', 'What each permission allows and guards'),
        ('reference-templates', 'Templates and assets', 'Every page template and where it is rendered'),
        ('reference-config', 'Configuration and commands', 'Environment variables and the command line'),
        ('glossary', 'Glossary', 'The words this codebase uses'),
    )),
)
app.add_template_filter(docsgen.slug, 'docs_slug')
PAGES = {slug: (title, blurb, group) for group, items in DOC_GROUPS for slug, title, blurb in items}
ORDER = [slug for _, items in DOC_GROUPS for slug, _, _ in items]


def docs_required(fn):
    """A signed-in platform admin who may read the documentation, and nobody else.

    Not signed in: sent to the console's sign-in page and brought back afterwards. Signed in without
    access: a plain 404, so the page is indistinguishable from one that does not exist.
    """
    @wraps(fn)
    @platform_host_only
    @platform_required
    def wrapper(*args, **kwargs):
        if not g.platform_admin.get('docs_access'):
            abort(404)
        return fn(*args, **kwargs)
    return wrapper


def _neighbours(slug):
    index = ORDER.index(slug)
    before = ORDER[index - 1] if index else None
    after = ORDER[index + 1] if index + 1 < len(ORDER) else None
    return ({'slug': before, 'title': PAGES[before][0]} if before else None,
            {'slug': after, 'title': PAGES[after][0]} if after else None)


def _render(slug):
    title, blurb, group = PAGES[slug]
    before, after = _neighbours(slug)
    return render_template(f'platform/docs/{slug}.html', slug=slug, title=title, blurb=blurb, group=group,
                           groups=DOC_GROUPS, before=before, after=after, ref=docsgen)


@app.route('/docs')
@docs_required
def docs_home():
    return _render('welcome')


@app.route('/docs/search')
@docs_required
def docs_search():
    query = ' '.join(request.args.get('q', '').split())[:80]
    return render_template('platform/docs/search.html', slug='search', title='Search', blurb='', group='',
                           groups=DOC_GROUPS, before=None, after=None, query=query,
                           results=_search(query) if len(query) >= 2 else [])


@app.route('/docs/<slug>')
@docs_required
def docs_page(slug):
    if slug not in PAGES or slug == 'welcome':
        abort(404)
    return _render(slug)


# ------------------------------------------------------------------------------- search

def _plain(markup):
    text = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', markup, flags=re.S)
    return ' '.join(html.unescape(re.sub(r'<[^>]+>', ' ', text)).split())


@lru_cache(maxsize=1)
def _index_pages_text(_marker):
    """The plain text of every hand-written page, rendered once (the key only lets a test reset it)."""
    found = []
    for slug in ORDER:
        if slug.startswith('reference-'):
            continue
        found.append({'kind': 'Page', 'title': PAGES[slug][0], 'where': PAGES[slug][2],
                      'url': f'/docs/{slug}' if slug != 'welcome' else '/docs', 'text': _plain(_render(slug))})
    return found


def _entries():
    entries = list(_index_pages_text(0))
    for group in docsgen.modules():
        for module in group['files']:
            symbols = ' '.join(s['name'] for s in module['symbols'])
            entries.append({'kind': 'Module', 'title': module['path'], 'where': module['summary'],
                            'url': f"/docs/reference-modules#{module['slug']}", 'text': f"{module['path']} {module['doc']} {symbols}"})
    for route in docsgen.routes():
        entries.append({'kind': 'Route', 'title': f"{' '.join(route['methods'])} {route['path']}",
                        'where': route['audience'], 'url': f"/docs/reference-routes#{docsgen.slug(route['endpoint'])}",
                        'text': f"{route['path']} {route['endpoint']} {route['doc']} {route['file']} {route['permission']}"})
    for group in docsgen.tables():
        for table in group['tables']:
            names = ' '.join(c['name'] for c in table['columns'])
            entries.append({'kind': 'Table', 'title': table['name'], 'where': group['title'],
                            'url': f"/docs/reference-data#{table['slug']}", 'text': f"{table['name']} {table['doc']} {names}"})
    for area in docsgen.permissions()['groups']:
        for item in area['permissions']:
            entries.append({'kind': 'Permission', 'title': item['key'], 'where': item['label'],
                            'url': '/docs/reference-permissions', 'text': f"{item['key']} {item['label']} {item['description']}"})
    for template in docsgen.templates():
        entries.append({'kind': 'Template', 'title': template['name'], 'where': ', '.join(template['renders'][:2]),
                        'url': f"/docs/reference-templates#{template['slug']}", 'text': template['name']})
    return entries


def _search(query):
    words = [w.lower() for w in query.split()]
    scored = []
    for entry in _entries():
        title, text = entry['title'].lower(), entry['text'].lower()
        if not all(w in title or w in text for w in words):
            continue
        score = sum(10 for w in words if w in title) + sum(min(text.count(w), 5) for w in words)
        if entry['kind'] == 'Page':
            score += 5
        first = min((text.find(w) for w in words if w in text), default=-1)
        snippet = ''
        if first >= 0:
            raw = entry['text']
            snippet = ('…' if first > 60 else '') + raw[max(0, first - 60):first + 140].strip() + '…'
        scored.append((score, {**entry, 'snippet': snippet}))
    scored.sort(key=lambda pair: (-pair[0], pair[1]['kind'], pair[1]['title']))
    return [entry for _, entry in scored[:60]]


# ------------------------------------------------------------------------------- marketing

@app.route('/marketing')
@platform_host_only
def marketing():
    """The public marketing page. ``BRIGHTSTARS_CONTACT_EMAIL``, when set, is where its buttons write to."""
    return render_template('marketing.html', contact_email=os.environ.get('BRIGHTSTARS_CONTACT_EMAIL', '').strip())


@app.route('/privacy')
def privacy():
    """The platform's data-protection statement. Deliberately unguarded (see the module docstring):
    reachable on a platform hostname and on every school's own address alike, signed in or not."""
    return render_template('privacy.html', contact_email=os.environ.get('BRIGHTSTARS_CONTACT_EMAIL', '').strip())
