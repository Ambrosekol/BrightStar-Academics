"""The generated half of /docs: a reference built from the code itself.

The narrative pages of the documentation are written by hand, and hand-written lists go out of date.
So everything that is a *list of what exists* is read straight from the code every time the process
starts, and cannot drift from it:

* ``modules()``      every Python file: its docstring and its top-level classes and functions, with lines
* ``routes()``       every URL the application answers: methods, who may call it, which file serves it
* ``tables()``       every database table of a school and of the platform registry: columns, keys, notes
* ``permissions()``  the permission catalogue and the role presets, and which URLs each permission guards
* ``templates()``    every page template: what it extends and which code renders it
* ``static_files()`` the shared stylesheets, scripts and images

Nothing here writes anything, opens a school's database, or runs any school's code. The results are
cached, because the code does not change while the process runs.
"""

import ast
import functools
import inspect
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Where the code lives, in the order a newcomer should meet it.
SOURCE_ROOTS = ('app.py', 'control_plane', 'models', 'core', 'blueprints', 'services', 'tests')
PACKAGE_BLURBS = {
    'app.py': 'The application object, request hooks, error pages and shared helpers',
    'control_plane': 'The platform itself: schools, addresses, admins, the console, this documentation',
    'models': "One school's database schema, one module per area",
    'core': 'Cross-cutting helpers with no routes: security, branding, banks, delivery, numbering',
    'blueprints': 'The routes, one package per part of the product',
    'services': 'Small dependency-free utilities',
    'tests': 'The contract suite and the end-to-end verification scripts',
}


def slug(text):
    return re.sub(r'[^a-z0-9]+', '-', str(text).lower()).strip('-')


def _summary(doc):
    """The first paragraph of a docstring on one line."""
    if not doc:
        return ''
    return ' '.join(doc.strip().split('\n\n')[0].split())


# ------------------------------------------------------------------------------- modules

def _python_files():
    for root in SOURCE_ROOTS:
        base = ROOT / root
        if base.is_file():
            yield base
        elif base.is_dir():
            for path in sorted(base.rglob('*.py')):
                if '__pycache__' in path.parts or path.name == '__init__.py' and path.stat().st_size == 0:
                    continue
                yield path


@functools.lru_cache(maxsize=1)
def modules():
    """``[{'group', 'blurb', 'files': [{'path', 'slug', 'summary', 'doc', 'lines', 'symbols'}]}]``."""
    groups = {root: [] for root in SOURCE_ROOTS}
    for path in _python_files():
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding='utf-8', errors='replace')
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        doc = ast.get_docstring(tree) or ''
        symbols = []
        if not rel.startswith('tests/'):
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    kind = 'class' if isinstance(node, ast.ClassDef) else 'def'
                    symbols.append({'kind': kind, 'name': node.name, 'line': node.lineno,
                                    'doc': _summary(ast.get_docstring(node))})
        top = rel.split('/')[0]
        groups[top].append({'path': rel, 'slug': slug(rel), 'summary': _summary(doc), 'doc': doc,
                            'lines': text.count('\n') + 1, 'symbols': symbols})
    return [{'group': root, 'blurb': PACKAGE_BLURBS[root], 'files': groups[root]}
            for root in SOURCE_ROOTS if groups[root]]


# ------------------------------------------------------------------------------- routes

_AUDIENCES = (
    ('/platform', 'Platform console'), ('/admin/finance', 'School admin · finance'),
    ('/admin/library', 'School admin · library'), ('/admin/administration', 'School admin · governance'),
    ('/admin/school', 'School admin · academics'), ('/admin/candidates', 'Entrance admin'),
    ('/admin/banks', 'Entrance admin'), ('/admin/entrance-config', 'Entrance admin'),
    ('/admin/practice-tests', 'Entrance admin'), ('/admin/examination', 'Entrance admin'),
    ('/admin/results', 'Entrance admin'), ('/admin/rankings', 'Entrance admin'),
    ('/admin/attempts', 'Entrance admin'), ('/admin', 'School admin'), ('/student', 'Student'),
    ('/parent', 'Parent'), ('/candidate', 'Entrance candidate'), ('/entrance-practice', 'Public'),
    ('/practice', 'Public'), ('/docs', 'Platform admins with access'), ('/marketing', 'Public'),
)


def _audience(path):
    for prefix, label in _AUDIENCES:
        if path == prefix or path.startswith(prefix + '/') or path.startswith(prefix + '<'):
            return label
    return 'Everyone signed in, or public'


def _decorators(func):
    try:
        lines, _ = inspect.getsourcelines(func)
    except (OSError, TypeError):
        return []
    found = []
    for line in lines:
        text = line.strip()
        if text.startswith(('def ', 'async def ')):
            break
        if text.startswith('@') and not text.startswith(('@app.route', '@app.get', '@app.post')):
            found.append(text)
    return found


@functools.lru_cache(maxsize=1)
def routes():
    """Every URL the application answers, sorted by path."""
    from app import app
    from core.security import ADMIN_ENDPOINT_PERMISSIONS

    out = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint == 'static':
            continue
        view = app.view_functions.get(rule.endpoint)
        original = inspect.unwrap(view) if view else None
        try:
            source = Path(inspect.getsourcefile(original)).resolve().relative_to(ROOT).as_posix()
            line = inspect.getsourcelines(original)[1]
        except (OSError, TypeError, ValueError):
            source, line = '', 0
        methods = sorted(m for m in (rule.methods or ()) if m not in ('HEAD', 'OPTIONS'))
        out.append({'path': rule.rule, 'endpoint': rule.endpoint, 'methods': methods,
                    'file': source, 'line': line, 'doc': _summary(inspect.getdoc(original)) if original else '',
                    'guards': _decorators(original) if original else [],
                    'permission': ADMIN_ENDPOINT_PERMISSIONS.get(rule.endpoint, ''),
                    'audience': _audience(rule.rule)})
    out.sort(key=lambda r: (r['path'], r['methods']))
    return out


# ------------------------------------------------------------------------------- tables

def _table_info(table, owners):
    owner = owners.get(table.name)
    columns = []
    for column in table.columns:
        columns.append({'name': column.name, 'type': str(column.type), 'pk': column.primary_key,
                        'nullable': column.nullable, 'default': column.server_default is not None,
                        'refs': sorted(fk.target_fullname for fk in column.foreign_keys)})
    uniques = [', '.join(c.name for c in constraint.columns)
               for constraint in table.constraints if constraint.__class__.__name__ == 'UniqueConstraint']
    return {'name': table.name, 'slug': slug(table.name), 'columns': columns, 'uniques': uniques,
            'indexes': sorted(index.name for index in table.indexes if index.name),
            'doc': _summary(inspect.getdoc(owner)) if owner else '',
            'model': owner.__name__ if owner else '',
            'file': (owner.__module__.replace('.', '/') + '.py') if owner else ''}


@functools.lru_cache(maxsize=1)
def tables():
    """``[{'title', 'blurb', 'tables': [...]}]``: each school's tables by area, then the registry's."""
    from models import db
    from .models import PlatformBase

    def owners(base):
        return {mapper.local_table.name: mapper.class_ for mapper in base.registry.mappers}

    school_owners = owners(db.Model)
    areas = {}
    for table in db.metadata.sorted_tables:
        info = _table_info(table, school_owners)
        area = info['file'].rsplit('/', 1)[-1].removesuffix('.py') or 'other'
        areas.setdefault(area, []).append(info)
    blurbs = {'school': 'Sessions, classes, students, assignments, assessments, results, promotion',
              'entrance': 'Question banks in use, candidates, papers and attempts', 'auth': 'Staff accounts, roles, permissions, audit, messages, locks',
              'finance': 'Fee items, assessments, payments, allocations, delivery log', 'parents': 'Parent accounts, links and feedback',
              'library': 'Books and loans', 'public': "The school's settings (name, logo, colours)", 'admissions': 'Admission profiles and enrolment history',
              'tenancy': "The school's own row, its settings, delivery secrets and number allocations", 'presence': 'Who is online, notifications, password-reset tokens',
              'other': 'Everything else'}
    groups = [{'title': f'School database · {area}', 'blurb': blurbs.get(area, ''), 'tables': items}
              for area, items in sorted(areas.items())]
    registry = [_table_info(t, owners(PlatformBase)) for t in PlatformBase.metadata.sorted_tables]
    groups.append({'title': 'Platform registry database', 'blurb': 'Which schools exist, their addresses, the platform team, sign-in tickets, limits',
                   'tables': registry})
    return groups


# ------------------------------------------------------------------------------- permissions

@functools.lru_cache(maxsize=1)
def permissions():
    """``{'groups': [{'name', 'permissions': [...]}], 'roles': [...]}`` from core/security.py."""
    from core.security import ADMIN_ENDPOINT_PERMISSIONS, ADMIN_PERMISSION_DEFS, ADMIN_ROLE_PRESETS

    guarded = {}
    for endpoint, key in ADMIN_ENDPOINT_PERMISSIONS.items():
        guarded.setdefault(key, []).append(endpoint)
    groups = {}
    for key, label, group, description in ADMIN_PERMISSION_DEFS:
        groups.setdefault(group, []).append({'key': key, 'label': label, 'description': description,
                                             'endpoints': sorted(guarded.get(key, []))})
    roles = [{'name': name, 'description': spec.get('description', ''),
              'permissions': spec.get('permissions', [])} for name, spec in ADMIN_ROLE_PRESETS.items()]
    return {'groups': [{'name': name, 'permissions': items} for name, items in groups.items()], 'roles': roles,
            'total': sum(len(items) for items in groups.values())}


# ------------------------------------------------------------------------------- templates and static

_RENDER = re.compile(r"render_template\(\s*['\"]([^'\"]+)['\"]")
_TEMPLATE_REF = re.compile(r"{%-?\s*(extends|include|from|import)\s+['\"]([^'\"]+)['\"]")


@functools.lru_cache(maxsize=1)
def templates():
    """``[{'name', 'slug', 'renders': [file:line], 'links': [(kind, name)]}]`` for every template."""
    used = {}
    for path in _python_files():
        if path.relative_to(ROOT).parts[0] == 'tests':
            continue
        rel = path.relative_to(ROOT).as_posix()
        for number, line in enumerate(path.read_text(encoding='utf-8', errors='replace').splitlines(), 1):
            for name in _RENDER.findall(line):
                used.setdefault(name, []).append(f'{rel}:{number}')
    out = []
    base = ROOT / 'templates'
    for path in sorted(base.rglob('*.html')):
        name = path.relative_to(base).as_posix()
        text = path.read_text(encoding='utf-8', errors='replace')
        out.append({'name': name, 'slug': slug(name), 'renders': used.get(name, []),
                    'links': [(kind, target) for kind, target in _TEMPLATE_REF.findall(text)],
                    'lines': text.count('\n') + 1})
    return out


@functools.lru_cache(maxsize=1)
def static_files():
    base = ROOT / 'static'
    out = []
    for path in sorted(base.rglob('*')):
        if path.is_file() and 'uploads' not in path.relative_to(base).parts and 'fonts' not in path.relative_to(base).parts[:1]:
            out.append({'path': path.relative_to(ROOT).as_posix(), 'kb': max(1, round(path.stat().st_size / 1024))})
    return out


def known_paths():
    """Every source path the reference lists, for the test that keeps the narrative honest."""
    files = {m['path'] for group in modules() for m in group['files']}
    files |= {t['name'] for t in templates()}
    files |= {s['path'] for s in static_files()}
    return files
