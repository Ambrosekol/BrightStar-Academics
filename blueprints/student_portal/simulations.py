"""A student's learning simulations (core/simulations.py): the ones for their class, and in SSS their department,
with a topic filter and a search; and one simulation, embedded from PhET.

Both pages carry their own Content-Security-Policy, which lets in phet.colorado.edu and nothing else, so they are
deliberately not in the portal's in-place list (blueprints/portal_shell.py): a page loaded in place would keep the
policy of the page it was loaded into, and the browser would refuse the simulation.
"""

from flask import abort, make_response, render_template, request, session

from app import _student_with_enrolment, app, csp_nonce
from blueprints.student_portal.helpers import student_required
from core import simulations as sims


def _page(template, **values):
    response = make_response(render_template(template, credit=sims.CREDIT, licence_url=sims.LICENCE_URL, **values))
    response.headers['Content-Security-Policy'] = sims.csp(csp_nonce())
    return response


@app.route('/student/simulations')
@student_required
def student_simulations():
    student = _student_with_enrolment(session['student_id'])
    s = sims.settings()
    available = sims.for_student(student, s)
    topic = request.args.get('topic', '')
    topic = topic if topic in sims.TOPICS else ''
    query = ' '.join(request.args.get('q', '').split())[:60]
    shown = [e for e in available if (not topic or e['topic'] == topic)
             and (not query or query.casefold() in e['title'].casefold())]
    counts = {key: sum(1 for e in available if e['topic'] == key) for key in sims.TOPICS}
    return _page('student_simulations.html', student=student, simulations=shown, total=len(available),
                 department=sims.student_department(student),
                 topics=[(key, label, counts[key]) for key, label in sims.TOPICS.items() if counts[key]],
                 topic=topic, query=query, picture_url=sims.picture_url,
                 switched_off=not sims.open_for_class(student['class_id'], s))


@app.route('/student/simulations/<slug>')
@student_required
def student_simulation(slug):
    student = _student_with_enrolment(session['student_id'])
    found = next((e for e in sims.for_student(student) if e['slug'] == slug), None)
    if found is None:
        abort(404)
    return _page('student_simulation.html', student=student, sim=found, run_url=sims.run_url(slug),
                 about_url=sims.about_url(slug))
