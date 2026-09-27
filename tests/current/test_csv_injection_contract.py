"""Contract: nothing exported to CSV can be read as a spreadsheet formula.

Excel, Sheets and LibreOffice all treat a cell that opens with =, +, - or @ (or a tab or a
carriage return) as the start of a formula, not as text. A candidate's or a bulk-imported
student's name is typed by a person and then lands in three CSV outputs; a name deliberately
made to look like ``=HYPERLINK(...)`` or ``=cmd|'/C calc'!A1`` must come out defused (led with a
straight quote) in every one of them, without changing what is stored or shown on screen. This
reads source only, and needs no database - the behavioural proof (a real import, a real export)
is tests/verification/write_paths_student_import.py and write_paths_question_banks.py.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HELPERS = (ROOT / "blueprints" / "entrance" / "helpers.py").read_text(encoding="utf-8")
ROUTES = (ROOT / "blueprints" / "entrance" / "routes.py").read_text(encoding="utf-8")
REPORT_TEMPLATE = (ROOT / "templates" / "admin_school_students_import_report.html").read_text(encoding="utf-8")


def _load_csv_safe():
    """Executes just the ``csv_safe`` function's own source, in isolation - the module it lives
    in imports the whole Flask app, which this fast contract test does not want to boot."""
    constant = re.search(r"^_CSV_FORMULA_TRIGGERS\s*=.*$", HELPERS, re.M)
    function = re.search(r"^def csv_safe\(.*?(?=\n\S)", HELPERS, re.S | re.M)
    assert constant and function, "blueprints/entrance/helpers.py lost its csv_safe() helper"
    namespace = {}
    exec(constant.group(0) + "\n" + function.group(0), namespace)  # noqa: S102 - trusted, own source
    return namespace["csv_safe"]


def test_a_formula_looking_name_is_defused_in_a_cell():
    csv_safe = _load_csv_safe()
    for hostile in ("=cmd|'/C calc'!A1", "+1+1", "-2+3", "@SUM(1,1)", "\t=1", "\r=1",
                    "=HYPERLINK(\"http://evil\",\"click\")"):
        safe = csv_safe(hostile)
        assert safe.startswith("'"), f"{hostile!r} was not defused: {safe!r}"
        assert safe[1:] == hostile, "the original text must survive after the leading quote"


def test_an_ordinary_name_is_left_exactly_as_it_is():
    csv_safe = _load_csv_safe()
    for ordinary in ("Ada Nkem Obi", "O'Brien-Smith", "", None, 42):
        original = "" if ordinary is None else str(ordinary)
        assert csv_safe(ordinary) == original


def test_every_server_side_csv_export_of_a_typed_name_uses_it():
    # _csv_response (results.csv) and export_rankings_csv (rankings.csv) both write a
    # candidate's own name into a cell; both must run it through csv_safe first.
    assert "csv_safe(r['candidate'])" in HELPERS, "helpers.py's CSV export no longer defuses the candidate name"
    assert "csv_safe(exam_name or r['bank_id'])" in HELPERS, "helpers.py's CSV export no longer defuses the exam name"
    assert "csv_safe" in ROUTES and "csv_safe(r['candidate'])" in ROUTES, (
        "routes.py's rankings CSV export no longer defuses the candidate name")


def test_the_bulk_import_credentials_download_defuses_a_name_too():
    # The "Download credentials (CSV)" button (templates/admin_school_students_import_report.html)
    # builds its file client-side from a name that came straight out of the uploaded file, so the
    # same defusing has to happen in its own JavaScript, not only on the server.
    assert re.search(r"FORMULA_START\s*=\s*/\^\[=\+\\-@\\t\\r\]/", REPORT_TEMPLATE), (
        "the credentials-download script lost its formula-trigger check")
    assert "csvSafe(v)" in REPORT_TEMPLATE and "csvSafe(v).replace" in REPORT_TEMPLATE, (
        "the credentials-download script no longer defuses each cell before quoting it")
