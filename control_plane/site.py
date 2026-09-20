"""The platform's own public website.

On a multi-tenant deployment this is the only public website there is: a
school's address is a portal and has no marketing pages of its own. Served at
``/`` on the platform hostnames, by ``index()`` in app.py.
"""

from flask import render_template, session

from . import config


def platform_home():
    return render_template('platform/site.html',
                           portal_domain=config.portal_domain(),
                           signed_in=bool(session.get('platform_admin_id')))
