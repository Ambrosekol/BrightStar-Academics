"""The one line a page adds to say out loud when the connection drops or comes back
(static/connectivity.js does the rest). A template global, the same idea as upload_hint()."""

from markupsafe import Markup


def connectivity_banner():
    """The banner element the script writes into, and the script itself (once per page)."""
    from flask import g, url_for
    try:
        if getattr(g, '_connectivity_script', False):
            return Markup('<div class="connectivity-banner" data-connectivity-banner aria-live="assertive"></div>')
        g._connectivity_script = True
        src = url_for('static', filename='connectivity.js')
    except RuntimeError:
        return Markup('')
    return Markup(
        '<div class="connectivity-banner" data-connectivity-banner aria-live="assertive"></div>'
        f'<script src="{src}" defer></script>'
    )
