"""A child's report cards, for the parent.

A parent reaches a child's card only through the link between their account and that child (the same
check every other parent page makes), and only once the child's results for the term have all been
released, the same moment the student sees it.
"""

import re

from flask import Response, abort, render_template, session, url_for

from app import _release_due_school_results, app
from blueprints.parents.helpers import _parent_owns_student, parent_required
from blueprints.school.report_card_data import build_card, card_for_web, term_from_slug
from core.report_card_pdf import render_report_cards_pdf
from models import db


def _card_or_404(student_id, session_id, slug):
    term = term_from_slug(slug)
    if term is None or not _parent_owns_student(session['parent_id'], student_id):
        abort(404)
    _release_due_school_results()
    db.session.commit()
    card = build_card(student_id, session_id, term)
    if card is None:
        abort(404)
    return card


@app.route('/parent/children/<int:student_id>/report-cards/<int:session_id>/<slug>')
@parent_required
def parent_report_card_view(student_id, session_id, slug):
    card = _card_or_404(student_id, session_id, slug)
    return render_template('report_card_page.html', card=card_for_web(card),
                           back_url=url_for('parent_child_detail', student_id=student_id) + '#report-cards',
                           back_label='Back to my child',
                           pdf_url=url_for('parent_report_card_pdf', student_id=student_id, session_id=session_id, slug=slug))


@app.route('/parent/children/<int:student_id>/report-cards/<int:session_id>/<slug>/pdf')
@parent_required
def parent_report_card_pdf(student_id, session_id, slug):
    card = _card_or_404(student_id, session_id, slug)
    # Plain ASCII only: a header cannot carry letters like the dotted O of a Yoruba name.
    name = re.sub(r'[^A-Za-z0-9._-]+', '-', f"Report-Card-{card['student']['name']}-{card['term']}-{card['session']}").strip('-')
    response = Response(render_report_cards_pdf([card]), mimetype='application/pdf')
    response.headers['Content-Disposition'] = f'inline; filename="{name[:120]}.pdf"'
    response.headers['Cache-Control'] = 'no-store'
    return response
