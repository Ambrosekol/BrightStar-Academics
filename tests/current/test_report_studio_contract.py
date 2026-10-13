"""The report card studio's rules that must not quietly change: who may use it, what a design may contain, what
reaches a parent's or student's page, and that nothing is ever drawn as HTML.

The behaviour itself is proven end to end by tests/verification/write_paths_report_studio.py.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def read(path):
    return (ROOT / path).read_text(encoding='utf-8')


def test_every_studio_route_needs_report_cards_manage():
    security = read('core/security.py')
    routes = read('blueprints/school/report_card_studio.py')
    endpoints = re.findall(r"^def (admin_report_card_\w+)\(", routes, re.M)
    assert len(endpoints) == 8, endpoints
    for endpoint in endpoints:
        assert f"'{endpoint}':'report_cards.manage'" in security, endpoint
    # every route signs in, and every write checks the CSRF token
    for block in re.split(r"\n(?=@app\.)", routes):
        if block.startswith('@app.'):
            assert '@admin_required' in block, block[:80]
            if block.startswith('@app.post'):
                assert '@csrf_protect' in block, block[:80]


def test_a_design_is_rebuilt_from_known_keys_only():
    core = read('core/report_card_design.py')
    for rule in ("_clean_element(", "_choice(raw.get('font'), FONTS", "RESULT_COLUMNS if c in asked", "SUMMARY_ITEMS if c in asked",
                 "len(raw) > 600_000", "MAX_ELEMENTS", "image if image in assets else None"):
        assert rule in core, rule
    studio = read('blueprints/school/report_card_studio.py')
    assert "rcd.clean_layout(request.form.get('layout', ''), {a.id for a in _asset_rows()})" in studio
    assert "row.locked" in studio and '409' in studio


def test_no_health_details_and_only_shown_details_reach_the_page():
    core = read('core/report_card_design.py')
    data = read('blueprints/school/report_card_data.py')
    for secret in ('allerg', 'convulsion', 'asthma', 'disability', 'immunization', 'health_challenges', 'password'):
        assert secret not in core.lower(), secret
        assert f"student.{secret}" not in data.lower(), secret
    assert "view['data'] = {k: v for k, v in view['data'].items() if k in shown}" in data


def test_drawn_with_text_never_html():
    for path in ('static/report-card-render.js', 'static/id-card-render.js', 'static/id-card-designer.js'):
        source = read(path)
        assert 'innerHTML' not in source and 'insertAdjacentHTML' not in source and 'document.write' not in source, path
    page = read('templates/report_card_page.html')
    assert "id=\"rcdData\" nonce=\"{{ csp_nonce() }}\">{{ {'layout': card.design.layout, 'view': card.design.view}|tojson }}" in page
    studio = read('templates/school_report_card_studio.html')
    assert 'id="iddData" nonce="{{ csp_nonce() }}">{{ data|tojson }}' in studio and '|safe' not in studio


def test_every_report_card_pdf_follows_the_school_design():
    for path in ('blueprints/school/report_cards.py', 'blueprints/parents/report_cards.py', 'blueprints/student_portal/report_cards.py'):
        source = read(path)
        assert 'render_report_cards_pdf' not in source and 'report_cards_pdf(' in source, path
