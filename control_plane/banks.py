"""Give a school its starting question banks when the platform creates it.

The work is done by ``core/banks.py``; this is only the step that selects the new school,
copies the platform's standard entrance banks into the school's own ``data/`` folder and fills
the school's examinations table from them. It is safe to run again.
"""

from .context import tenant_context


def add_starter_banks(info):
    """Copy the standard entrance banks into a school's own folder and register them.

    Returns ``{'created': [bank ids], 'skipped': [bank ids]}``: a bank the school already
    had is skipped, and nothing in the school's folder is ever overwritten.
    """
    import app as A  # deferred: the app imports this package at start-up
    from core import banks

    with A.app.app_context(), tenant_context(info):
        result = banks.install_starter_banks()
        banks.sync_new_banks(result['created'])
    return result
