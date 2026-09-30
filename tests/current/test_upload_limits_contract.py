"""Contract: a person is always told how large an upload may be, and why one was refused.

The upload limit has one source (core/uploads.py). Every page with a file box must print it under the
box and carry it for static/upload-limit.js, every place that saves an uploaded picture must show a
refusal to the person on the form they were on, and a whole submission that is too large must be
answered with an explanation. It reads source only, and needs no database.
"""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"

FILE_INPUT = re.compile(r"""<input\b[^>]*\btype=["']?file["']?[^>]*>""", re.I | re.S)
# A file box that is not a picture or an attachment has its own, different rule and says so itself.
OWN_LIMIT = {"admin_bank_import.html": 'name="bank_file"'}


def _templates():
    return sorted(p for p in TEMPLATES.rglob("*.html"))


def test_every_file_box_carries_its_limit_and_says_it_out_loud():
    boxes = 0
    for path in _templates():
        source = path.read_text(encoding="utf-8")
        found = FILE_INPUT.findall(source)
        if not found:
            continue
        counted = 0
        for tag in found:
            if OWN_LIMIT.get(path.name) and OWN_LIMIT[path.name] in tag:
                continue
            counted += 1
            assert "upload_attrs(" in tag, f"{path.relative_to(ROOT)}: a file box without the limit attribute: {tag[:120]}"
        boxes += counted
        assert source.count("upload_hint(") >= counted, (
            f"{path.relative_to(ROOT)}: {counted} file box(es) but only {source.count('upload_hint(')} hint(s)")
    assert boxes >= 15, f"only {boxes} upload boxes were found; the search is looking in the wrong place"


def test_the_image_boxes_accept_exactly_what_the_server_accepts():
    """A box must not offer what the server then refuses (an "image/*" that lets a BMP through)."""
    for path in _templates():
        for tag in FILE_INPUT.findall(path.read_text(encoding="utf-8")):
            if "upload_attrs(" in tag and "attachment" not in tag and "chat-file-input" not in tag:
                assert "image/*" not in tag, f"{path.relative_to(ROOT)} accepts every image type: {tag[:120]}"


def test_no_page_spells_the_limit_out_by_hand():
    """One source of truth: a hand-written "5 MB" goes stale the day the setting changes."""
    for path in _templates():
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"\b\d+(\.\d+)?\s?MB\b", source), f"{path.relative_to(ROOT)} writes a size limit by hand"
        assert "follows the platform security policy" not in source


def test_the_limit_is_read_in_one_place():
    # core/uploads.py is the source of truth. settings_registry.py is a plain-data catalogue of
    # every setting's name for the console's Settings page (its own docstring: "nothing here
    # reads or writes a value") - it names this variable exactly like every other one it lists,
    # never reads or computes its value, so it is not a second place the limit is read from.
    exempt = {ROOT / "core" / "uploads.py", ROOT / "control_plane" / "settings_registry.py"}
    for path in list(ROOT.glob("*.py")) + [p for folder in ("blueprints", "core", "control_plane", "services", "models")
                                          for p in (ROOT / folder).rglob("*.py")]:
        if path in exempt:
            continue
        source = path.read_text(encoding="utf-8")
        assert "BRIGHTSTARS_MAX_UPLOAD_BYTES" not in source, f"{path.relative_to(ROOT)} reads the upload limit itself"
        assert not re.search(r"5\s*\*\s*1024\s*\*\s*1024", source), f"{path.relative_to(ROOT)} spells the limit out"


def _catches_value_error(node):
    """Whether an ``except`` clause is one that a ValueError from an upload would land in."""
    if node.type is None:
        return True
    names = [n.id for n in ast.walk(node.type) if isinstance(n, ast.Name)]
    return "ValueError" in names or "Exception" in names


def test_every_place_that_saves_a_picture_can_show_the_person_a_refusal():
    """``_save_image_upload`` raises ValueError with a sentence made for the person. Each caller must
    catch it (to show it on the form) or be a helper documented to pass it up to one that does."""
    passes_it_up = {"signature_action", "store_branding", "_save_image_upload", "create_tenant", "update_branding"}
    checked = 0
    for folder in ("blueprints", "control_plane", "core"):
        for path in (ROOT / folder).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            parents = {}
            for parent in ast.walk(tree):
                for child in ast.iter_child_nodes(parent):
                    parents[child] = parent
            for call in ast.walk(tree):
                if not (isinstance(call, ast.Call) and getattr(call.func, "id", getattr(call.func, "attr", "")) in
                        ("_save_image_upload", "validate_image_upload")):
                    continue
                checked += 1
                node, safe, function = call, False, None
                while node in parents:
                    child, node = node, parents[node]
                    if isinstance(node, ast.Try) and child in node.body and any(_catches_value_error(h) for h in node.handlers):
                        safe = True
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        function = node.name
                        break
                assert safe or function in passes_it_up, (
                    f"{path.relative_to(ROOT)}:{call.lineno} saves an upload without showing a refusal to the person")
    assert checked >= 15, f"only {checked} upload calls were found"


def test_a_submission_that_is_too_large_is_explained_not_crashed():
    source = (ROOT / "core" / "request_errors.py").read_text(encoding="utf-8")
    assert "errorhandler(RequestEntityTooLarge)" in source
    assert "request_too_large_message" in source
    # It only ever sends the person back to a page of this same site, that they can open, and that shows a message.
    assert "referer.netloc != request.host" in source and "url_map.bind_to_environ" in source
    assert "_PAGES_THAT_SHOW_A_MESSAGE" in source
    # The helpers reach every template from the same module.
    assert "add_template_global(uploads.upload_attrs, 'upload_attrs')" in source
    assert "add_template_global(uploads.upload_hint, 'upload_hint')" in source
    # The numbers the application is built on were not changed.
    assert "8 * 1024 * 1024" in (ROOT / "app.py").read_text(encoding="utf-8")
    from core import uploads
    assert uploads.DEFAULT_IMAGE_LIMIT_BYTES == 5 * 1024 * 1024 and uploads.DEFAULT_REQUEST_LIMIT_BYTES == 8 * 1024 * 1024


def test_the_upload_words_are_specific():
    from core import uploads
    mb = 1024 * 1024
    assert uploads.too_large_message('"photo.jpg"', int(7.2 * mb), 5 * mb) == (
        '"photo.jpg" is 7.2 MB. The limit is 5 MB. Choose a smaller picture, or reduce this one first.')
    assert "does not appear to be a valid image" in uploads.not_an_image_message('"a.jpg"')
    assert "not a picture we can use" in uploads.wrong_type_message('"a.pdf"')
    assert "larger than the limit of 8 MB" in uploads.request_too_large_message(9 * mb, 8 * mb)
    # A file a little over the limit must never read as equal to it.
    assert uploads.format_size_over(5 * mb + 1, 5 * mb) != uploads.format_limit(5 * mb)


def test_the_upload_script_is_plain_and_content_security_policy_safe():
    script = (ROOT / "static" / "upload-limit.js").read_text(encoding="utf-8")
    for banned in ("eval(", "new Function", "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                   "XMLHttpRequest", "fetch(", "import(", "http://", "https://"):
        assert banned not in script, f"static/upload-limit.js uses {banned}"
    for needed in ("role', bad ? 'alert' : 'status'", "aria-live", "input.value = ''", "data-upload-limit"):
        assert needed in script, f"static/upload-limit.js lost {needed!r}"
    # It is loaded from the site's own address, which the policy allows.
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "script-src 'self'" in app
    uploads = (ROOT / "core" / "uploads.py").read_text(encoding="utf-8")
    assert "url_for('static', filename='upload-limit.js'" in uploads
