"""The frame the student and parent portals share (templates/portal_base.html): the staff portal's own shell - a
navigation rail, a header with the bell and the account menu, and pages that load in place (static/inplace.js) -
with a tab bar along the bottom on a phone, where the installed app (blueprints/pwa.py) is used most.

``portal`` is worked out once per request for a signed-in student or parent: who they are, their menu, the tab bar,
and which addresses load in place. Pages a person takes a timed test on, pays through Paystack from, or chats in are
deliberately left out of the in-place list: they always load as whole pages (with the loading bar of
static/page-loading.js), so nothing interrupts a timer, a payment or a live conversation.
"""

from flask import g, request, session, url_for

from app import app


def _active(*prefixes, exact=()):
    path = request.path
    return path in exact or any(path == p or path.startswith(p + '/') for p in prefixes)


def _initials(name):
    words = [w for w in (name or '').split() if w]
    return ((words[0][:1] if words else '?') + (words[-1][:1] if len(words) > 1 else '')).upper()


def _student():
    from app import _student_with_enrolment
    from core.names import full_name
    sid = session.get('student_id')
    if not sid:
        return None
    st = _student_with_enrolment(sid)
    if not st:
        return None
    name = full_name(st['first_name'], st['last_name'])
    I = lambda label, url, icon, active, **kw: dict(label=label, url=url, icon=icon, active=active, **kw)
    sections = [
        {'label': '', 'items': [
            I('Home', url_for('student_dashboard'), 'nav-overview', _active(exact=('/student/dashboard',))),
        ]},
        {'label': 'My work', 'items': [
            I('Tests', url_for('student_assessment_list', kind='tests'), 'nav-academics', _active('/student/tests')),
            I('Examinations', url_for('student_assessment_list', kind='examinations'), 'nav-reportcard', _active('/student/examinations')),
            I('Practice', url_for('student_assessment_list', kind='practice'), 'nav-practice', _active('/student/practice')),
        ]},
        {'label': 'My school', 'items': [
            I('Timetable', url_for('student_timetable'), 'nav-timetable', _active('/student/timetable')),
            I('Attendance', url_for('student_attendance'), 'nav-attendance', _active('/student/attendance')),
        ]},
    ]
    tabs = [sections[0]['items'][0], sections[1]['items'][0], sections[1]['items'][2], sections[2]['items'][0]]
    return {
        'kind': 'student', 'label': 'Student Portal', 'home_url': url_for('student_dashboard'),
        'person': {'name': name, 'sub': ' · '.join(x for x in (st['class_name'], st['admission_no']) if x),
                   'initials': _initials(name), 'photo': st['photo_path']},
        'sections': sections, 'tabs': tabs,
        'password_url': url_for('student_password_change'),
        # Exact addresses: a test, a practice run or an assignment being taken always loads as a whole page.
        'scope': ['=/student/dashboard', '=/student/tests', '=/student/examinations', '=/student/practice',
                  '/student/timetable', '/student/attendance'],
    }


def _parent():
    from blueprints.parents.helpers import _parent_children
    from core.db_helpers import one
    from core.names import full_name
    from models import ParentAccount
    from sqlalchemy import select
    pid = session.get('parent_id')
    if not pid:
        return None
    row = one(select(ParentAccount.display_name, ParentAccount.username, ParentAccount.email, ParentAccount.phone)
              .where(ParentAccount.id == pid))
    if not row:
        return None
    name = row['display_name'] or row['username']
    children = _parent_children(pid)
    I = lambda label, url, icon, active, **kw: dict(label=label, url=url, icon=icon, active=active, **kw)
    kids = [I(full_name(c['first_name'], c['last_name']), url_for('parent_child_detail', student_id=c['id']), 'nav-students',
              _active(f"/parent/children/{c['id']}"), photo=c.get('photo_path'), initials=_initials(full_name(c['first_name'], c['last_name'])))
            for c in children]
    sections = [
        {'label': '', 'items': [I('Home', url_for('parent_dashboard'), 'nav-overview', _active(exact=('/parent/dashboard',)))]},
        {'label': 'Your children' if len(kids) != 1 else 'Your child', 'items': kids},
        {'label': 'The school', 'items': [
            I('Messages', url_for('parent_feedback'), 'nav-messages', _active('/parent/feedback')),
            I('Store', url_for('parent_store'), 'nav-store', _active('/parent/store')),
        ]},
    ]
    first_child = kids[0] if kids else None
    tabs = [sections[0]['items'][0]]
    if first_child:
        tabs.append(dict(first_child, label='Children' if len(kids) > 1 else first_child['label'].split()[0], icon='nav-students',
                         active=_active('/parent/children')))
    tabs += sections[2]['items']
    return {
        'kind': 'parent', 'label': 'Parent Portal', 'home_url': url_for('parent_dashboard'),
        'person': {'name': name, 'sub': row['email'] or row['phone'] or 'Parent account', 'initials': _initials(name), 'photo': None},
        'sections': sections, 'tabs': tabs,
        'password_url': url_for('parent_password_change'),
        # Paying, buying and the live chat always load as whole pages (Paystack takes over the browser; the chat has
        # its own live updates). A child's pages, the dashboard and the store load in place.
        'scope': ['=/parent/dashboard', '/parent/children', '=/parent/store'],
    }


def portal():
    """The frame's data for this request's student or parent, or None."""
    if 'portal' not in g:
        value = None
        try:
            if request.path.startswith('/student'):
                value = _student()
            elif request.path.startswith('/parent'):
                value = _parent()
        except Exception:
            app.logger.exception('Could not build the portal frame')
            value = None
        g.portal = value
    return g.portal


app.jinja_env.globals.update(portal_frame=portal)
