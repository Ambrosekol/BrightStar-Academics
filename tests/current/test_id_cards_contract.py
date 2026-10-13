"""Contract: ID cards stay safe.

* a design from the designer is always rebuilt by core/id_cards.clean_layout before it is stored, and a locked design
  refuses to be saved;
* a card's token is random (secrets), never a number to guess;
* the public check page and its photograph are never cached, indexed or referred, and the photograph is served only
  for an active card of a current person;
* the card and designer templates never print anything raw, and the designer sets text with textContent only.
It reads source only, and needs no database.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROUTES = (ROOT / "blueprints" / "school" / "id_cards.py").read_text(encoding="utf-8")
CORE = (ROOT / "core" / "id_cards.py").read_text(encoding="utf-8")
JS = (ROOT / "static" / "id-card-designer.js").read_text(encoding="utf-8")


def _route(name):
    start = ROUTES.index(f"def {name}(")
    following = ROUTES.find("\n@app.", start)
    return ROUTES[start:following if following != -1 else len(ROUTES)]


def test_a_design_is_rebuilt_before_it_is_stored_and_a_lock_holds():
    save = _route("admin_id_card_design_save")
    assert "ic.clean_layout(request.form.get('layout', ''), kind)" in save
    assert "row.locked" in save and "409" in save
    assert "row.locked" in _route("admin_id_card_design_reset")
    for rule in ("ELEMENT_TYPES", "COLOUR = re.compile(r'^#[0-9a-fA-F]{6}$')", "MAX_ELEMENTS", "fields_for(kind)"):
        assert rule in CORE, rule


def test_tokens_are_random():
    assert "secrets.token_urlsafe(16)" in CORE


def test_the_check_page_is_private_and_shows_only_a_current_card():
    assert "'no-store, private'" in ROUTES and "'noindex, nofollow'" in ROUTES and "'no-referrer'" in ROUTES
    photo = _route("id_card_photo")
    assert "card.status == 'active'" in photo and "holder['current']" in photo and "_private(response)" in photo
    assert "_private(response)" in _route("id_card_verify")


def test_printing_needs_every_person_to_be_within_reach():
    printing = _route("admin_id_cards_print")
    assert "len(people) != len(ids)" in printing and "Nothing was printed" in printing
    assert "'no-store, private'" in printing


def test_nothing_is_printed_raw():
    for name in ("includes/_id_card.html", "school_id_card_sheet.html", "school_id_card_designer.html", "school_id_cards.html",
                 "id_card_verify.html"):
        source = (ROOT / "templates" / name).read_text(encoding="utf-8")
        assert "|safe" not in source.replace(" ", ""), name
    assert "innerHTML" not in JS and "textContent" in JS
