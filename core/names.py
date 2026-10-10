"""People's names: an optional part (a middle name) that holds nothing real is nothing.

A middle name is optional, and some records hold a word that only means "none" where nothing was entered - "None",
"null", "-", "N/A" - left by an older import or a form that wrote an empty value back as text. Such a value is never
shown and never saved: ``clean_part`` turns it into None on the way in, ``full_name`` leaves it out on the way out, and
app.py's schema upgrade clears any already stored.
"""

PLACEHOLDERS = frozenset({'none', 'null', 'nil', 'n/a', 'na', '-', '--', 'undefined'})


def clean_part(value):
    """An optional name part as it should be stored: trimmed, or None when it holds nothing real."""
    value = (value or '').strip()
    return None if not value or value.lower() in PLACEHOLDERS else value


def full_name(*parts):
    """The name from its parts, leaving out any that are empty or only a placeholder."""
    return ' '.join(p for p in (clean_part(str(x)) if x is not None else None for x in parts) if p)
