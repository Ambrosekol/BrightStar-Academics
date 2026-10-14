"""A student's own report cards.

They are listed on the student's dashboard as soon as a term's results have all been released, and
open here as a page or a PDF. A student only ever reaches their own: the account in the session is the
only one asked about, never a number in the address.
"""

import re

from flask import Response, abort, render_template, session, url_for

from app import _release_due_school_results, app
from blueprints.finance.report_hold import held
from blueprints.school.report_card_data import build_card, card_for_web, report_cards_pdf, term_from_slug
from blueprints.student_portal.helpers import student_required
from models import db


def _card_or_404(session_id, slug):
    term = term_from_slug(slug)
    if term is None:
        abort(404)
    # Held while fees are owed, if the school holds report cards for unpaid fees (blueprints/finance/report_hold.py).
    if held(session['student_id']):
        abort(Response(render_template('report_card_held.html', who='student', owed=None, fees_url=None,
                                       back_url=url_for('student_dashboard') + '#report-cards'), 403))
    _release_due_school_results()      # a release date that has passed releases the approved results
    db.session.commit()
    card = build_card(session['student_id'], session_id, term)
    if card is None:
        abort(404)
    return card


@app.route('/student/report-cards/<int:session_id>/<slug>')
@student_required
def student_report_card_view(session_id, slug):
    card = _card_or_404(session_id, slug)
    return render_template('report_card_page.html', card=card_for_web(card), back_url=url_for('student_dashboard') + '#report-cards',
                           back_label='Back to my dashboard',
                           pdf_url=url_for('student_report_card_pdf', session_id=session_id, slug=slug))


@app.route('/student/report-cards/<int:session_id>/<slug>/pdf')
@student_required
def student_report_card_pdf(session_id, slug):
    card = _card_or_404(session_id, slug)
    name = re.sub(r'[^A-Za-z0-9._-]+', '-', f"Report-Card-{card['term']}-{card['session']}").strip('-')
    response = Response(report_cards_pdf([card]), mimetype='application/pdf')
    response.headers['Content-Disposition'] = f'inline; filename="{name}.pdf"'
    response.headers['Cache-Control'] = 'no-store'
    return response
