"""Contract: a button with its own formaction / formmethod sends the form there, also inside a pop-up.

static/inplace.js submits pop-up forms itself. It once always used the form's own address, so the staff pop-up's
Reset password and Suspend buttons (each a formaction on the staff form) simply saved the staff form: no new
password was made and nobody was suspended, while a success message said otherwise.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INPLACE = (ROOT / "static" / "inplace.js").read_text(encoding="utf-8")
FORM = (ROOT / "templates" / "admin_account_form.html").read_text(encoding="utf-8")


def test_the_pressed_button_s_own_address_and_method_are_used():
    submit = INPLACE[INPLACE.index('document.addEventListener("submit"'):]
    assert 'getAttribute("formaction")' in submit
    assert 'getAttribute("formmethod")' in submit
    assert 'form.getAttribute("method")' in submit
    assert 'if (method === "get")' in submit


def test_the_staff_pop_up_s_reset_and_suspend_buttons_carry_their_own_address():
    assert 'formaction="{{ reset_url }}"' in FORM
    assert "formaction=\"{{ url_for('admin_account_toggle'" in FORM


def test_new_sign_in_details_have_one_page_in_the_portal_s_frame():
    page = (ROOT / "templates" / "admin_credentials.html").read_text(encoding="utf-8")
    assert "{% extends 'admin_base.html' %}" in page
    assert "history.replaceState" in page
    assert not (ROOT / "templates" / "admin_staff_credentials.html").exists()
