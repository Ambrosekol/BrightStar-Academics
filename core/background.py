"""Best-effort work that must not keep a person waiting: telling parents something has happened.

Sending an email or a WhatsApp message can take many seconds (a slow mail server, a timeout). The
bursar recording a payment, or an administrator releasing a term's results, should not sit and wait
for that, and a message that cannot be sent must never undo what was just saved. So the work is
handed to a thread of its own, run *as the same school* (its database, its files, its address) and
inside a request-like context, so anything that builds a link or reads the school's branding works
exactly as it does during a request.

Call it only after the change it reports has been committed: the thread opens its own database
session and would otherwise not see the change.

Setting ``BACKGROUND_INLINE`` in the application's config runs the work in the calling thread
instead, which is what makes the behaviour testable.
"""

import threading

from flask import current_app, has_request_context, request

from control_plane.context import current_tenant, tenant_context


def run_in_background(work, *args, **kwargs):
    """Run ``work(*args, **kwargs)`` for the current school without blocking the caller.

    Never raises: a failure is logged and dropped, because this is only ever used for
    notifications that are a courtesy on top of something already saved.
    """
    app = current_app._get_current_object()
    tenant = current_tenant(required=False)
    base_url = request.host_url if has_request_context() else None

    def job():
        try:
            with app.test_request_context(base_url=base_url or 'http://localhost/'):
                if tenant is not None:
                    with tenant_context(tenant):
                        work(*args, **kwargs)
                else:
                    work(*args, **kwargs)
        except Exception:
            app.logger.exception('Background task %s failed', getattr(work, '__name__', work))

    if app.config.get('BACKGROUND_INLINE'):
        # Same thread, same session: nothing to switch, and the caller's own objects stay usable.
        try:
            work(*args, **kwargs)
        except Exception:
            app.logger.exception('Background task %s failed', getattr(work, '__name__', work))
        return
    threading.Thread(target=job, name='brightstars-notify', daemon=True).start()
