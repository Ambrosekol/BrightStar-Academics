"""How a school's portal looks: its two brand colours and its photographs.

Deliberately free of Flask and the database, so the platform console can check a
school's choices before anything is created, and the portal can apply them on
every page, with the same rules.

A colour is only ever accepted as ``#rrggbb`` and only ever written into a page
as that normalised value. It goes inside a ``<style>`` block, so anything looser
would let a school's settings inject CSS into its own users' pages.
"""

import json
import re

PRIMARY_KEY = 'school_brand_primary'
ACCENT_KEY = 'school_brand_accent'
GALLERY_KEY = 'school_gallery'

# The portal's own colours, used until a school chooses its own.
DEFAULT_PRIMARY = '#0d2b52'
DEFAULT_ACCENT = '#1674b9'

# Both colours sit behind white text (the sidebar, header and buttons), so they
# must be dark enough to read it. 4.5 is the WCAG AA ratio for normal text.
MIN_CONTRAST_WITH_WHITE = 4.5

MAX_GALLERY_IMAGES = 8
GALLERY_FOLDER = 'uploads/branding/'

_HEX = re.compile(r'^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$')


def normalise_hex(value):
    """``'#ABC'`` / ``'aabbcc'`` -> ``'#aabbcc'``; ``''`` for blank. Raises
    ValueError for anything that is not a colour."""
    value = (value or '').strip()
    if not value:
        return ''
    match = _HEX.match(value)
    if not match:
        raise ValueError(f'"{value[:20]}" is not a colour. Use a hex colour such as #0d2b52.')
    digits = match.group(1).lower()
    if len(digits) == 3:
        digits = ''.join(c * 2 for c in digits)
    return '#' + digits


def _channels(colour):
    return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))


def _luminance(colour):
    def linear(channel):
        c = channel / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (linear(c) for c in _channels(colour))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_with_white(colour):
    return 1.05 / (_luminance(colour) + 0.05)


def check_colour(colour, label):
    """Normalise one brand colour and make sure white text is readable on it.
    Blank means "use the portal's own colour". Raises ValueError."""
    colour = normalise_hex(colour)
    if colour and contrast_with_white(colour) < MIN_CONTRAST_WITH_WHITE:
        raise ValueError(f'The {label} colour is too light: white text on it would be hard to '
                         f'read. Choose a darker shade.')
    return colour


def shade(colour, amount):
    """Mix a colour toward black (negative ``amount``) or white (positive)."""
    target = 255 if amount > 0 else 0
    mixed = [round(c + (target - c) * abs(amount)) for c in _channels(colour)]
    return '#' + ''.join(f'{c:02x}' for c in mixed)


def theme_css(primary, accent):
    """The ``<style>`` body that re-colours the portal, or '' when the school has
    chosen nothing. Inputs must already have passed :func:`check_colour`."""
    primary = normalise_hex(primary)
    accent = normalise_hex(accent)
    if not primary and not accent:
        return ''
    rules = []
    if primary:
        rules.append(f'--navy:{primary};--navy-2:{shade(primary, 0.14)};--navy-deep:{shade(primary, -0.3)}')
    if accent:
        rules.append(f'--blue:{accent};--blue-2:{shade(accent, -0.18)}')
    css = ':root{' + ';'.join(rules) + '}'
    # Places the stylesheets fix a colour instead of reading the variables above. (The navigation
    # rail, headers and sign-in pages of static/school-ui.css read the variables directly.)
    if accent:
        css += '.btn-primary{background:var(--blue)}.btn-primary:hover{background:var(--blue-2)}'
    return css


def parse_gallery(value):
    """The stored gallery setting -> a list of upload paths. Anything malformed,
    or pointing outside the branding folder, is dropped rather than trusted."""
    try:
        items = json.loads(value or '[]')
    except (TypeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    return [p for p in items
            if isinstance(p, str) and p.startswith(GALLERY_FOLDER) and '..' not in p and '\\' not in p]


def dump_gallery(paths):
    return json.dumps(list(paths))
