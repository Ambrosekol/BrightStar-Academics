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
RENDER = (ROOT / "static" / "id-card-render.js").read_text(encoding="utf-8")


def _route(name):
    start = ROUTES.index(f"def {name}(")
    following = ROUTES.find("\n@app.", start)
    return ROUTES[start:following if following != -1 else len(ROUTES)]


def test_a_design_is_rebuilt_before_it_is_stored_and_a_lock_holds():
    save = _route("admin_id_card_design_save")
    assert "ic.clean_layout(request.form.get('layout', ''), kind, {a.id for a in _asset_rows()})" in save
    assert "row.locked" in save and "409" in save
    assert "row.locked" in _route("admin_id_card_design_reset")
    for rule in ("ELEMENT_TYPES", "COLOUR = re.compile(r'^#[0-9a-fA-F]{6}$')", "MAX_ELEMENTS", "fields_for(kind)",
                 "_choice(raw.get('font'), spec['fonts']","_choice(raw.get('shape'), SHAPES", "_choice(raw.get('clip'), CLIPS",
                 "if assets is None or asset not in assets", "re.sub(r'[\\x00-\\x08\\x0b-\\x1f\\x7f]'"):
        assert rule in CORE, rule


def test_school_images_are_checked_and_kept_where_only_staff_can_open_them():
    upload = _route("admin_id_card_asset_upload")
    assert "_save_image_upload(upload, 'idcards', 'card')" in upload and "ic.MAX_ASSETS" in upload
    delete = _route("admin_id_card_asset_delete")
    assert "IdCardDesign.locked == 1" in delete and "409" in delete and "delete_upload(path)" in delete
    access = (ROOT / "core" / "upload_access.py").read_text(encoding="utf-8")
    assert "'idcards': frozenset({'admin'})" in access    # staff only: never students, parents or the public


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
    for name in ("school_id_card_sheet.html", "school_id_card_designer.html", "school_id_cards.html", "id_card_verify.html"):
        source = (ROOT / "templates" / name).read_text(encoding="utf-8")
        assert "|safe" not in source.replace(" ", ""), name
    for source in (JS, RENDER):
        assert "innerHTML" not in source and "insertAdjacentHTML" not in source and "outerHTML" not in source
        assert "eval(" not in source and "new Function" not in source
        assert "textContent" in source
    # The data the sheet and the designer draw from is JSON in a script block, which tojson escapes ("<" as \u003c).
    for name in ("school_id_card_sheet.html", "school_id_card_designer.html"):
        source = (ROOT / "templates" / name).read_text(encoding="utf-8")
        assert 'type="application/json"' in source and "|tojson" in source
