"""The receipt studio's rules that must not quietly change: who may use it, what a design may contain, that a voided
receipt is always marked VOID, and that nothing is drawn as HTML.

The behaviour itself is proven end to end by tests/verification/write_paths_receipt_studio.py.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def read(path):
    return (ROOT / path).read_text(encoding='utf-8')


def test_every_receipt_studio_route_needs_finance_manage():
    security = read('core/security.py')
    routes = read('blueprints/school/receipt_studio.py')
    endpoints = re.findall(r"^def (admin_receipt_\w+)\(", routes, re.M)
    assert len(endpoints) == 8, endpoints
    for endpoint in endpoints:
        assert f"'{endpoint}':'finance.manage'" in security, endpoint
    for block in re.split(r"\n(?=@app\.)", routes):
        if block.startswith('@app.'):
            assert '@admin_required' in block, block[:80]
            if block.startswith('@app.post'):
                assert '@csrf_protect' in block, block[:80]


def test_a_receipt_design_is_rebuilt_from_known_keys_only():
    core = read('core/receipt_design.py')
    for rule in ("_clean_element(", "SUMMARY_ITEMS if c in asked", "len(raw) > 400_000", "MAX_ELEMENTS",
                 "image if image in assets else None", "raw.get('page') if raw.get('page') in ('A5', 'A4') else 'A5'",
                 "_text(raw.get('text'), 16, 1)"):
        assert rule in core, rule
    studio = read('blueprints/school/receipt_studio.py')
    assert "rd.clean_layout(request.form.get('layout', ''), {a.id for a in _asset_rows()})" in studio
    assert "row.locked" in studio and '409' in studio


def test_a_voided_receipt_is_always_marked_void():
    pdf = read('core/receipt_design_pdf.py')
    assert "if sheet.get('voided'):\n        _void(c, width, height)" in pdf
    assert "if rep.get('voided'):\n        return" in pdf                     # and never shows the PAID stamp
    js = read('static/receipt-render.js')
    assert 'if (report(view).voided) return;' in js
    page = read('templates/includes/_receipt_designed.html')
    assert 'if (data.voided) R.voidMark(page);' in page


def test_drawn_with_text_never_html():
    js = read('static/receipt-render.js')
    assert 'innerHTML' not in js and 'insertAdjacentHTML' not in js and 'document.write' not in js
    page = read('templates/includes/_receipt_designed.html')
    assert 'id="rcdReceipt" nonce="{{ csp_nonce() }}">{{ designed|tojson }}' in page and '|safe' not in page


def test_every_receipt_pdf_follows_the_school_design():
    helpers = read('blueprints/finance/helpers.py')
    assert 'design=school_receipt_design()' in helpers and 'render_designed_receipt(sheet,*design)' in helpers
    assert helpers.count('render_receipt_pdf(') == 1                    # only as the standard receipt, in _receipt_pdf
