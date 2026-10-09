"""Throwaway PostgreSQL databases for the verification scripts.

PostgreSQL is the only database this application supports, so the verification
scripts run against a real server rather than a stand-in. Each run gets its own
registry database and its own per-school databases, all named with a unique
prefix, and drops them again on the way out — the real ``brightstars_*``
databases are never touched.

Connection details come from ``BRIGHTSTARS_PLATFORM_DB`` in your .env, so
whatever works for the application works here.
"""
import os
import re
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / '.env')

# The developer's .env may hold the real mail account and the real SMS token. A test must never be able to reach
# them: a verification run that releases report cards, charges fees or resets passwords would otherwise send real
# emails and real text messages (to test addresses and numbers, and at the cost of real credit). Blanked here, before
# the application reads the file, so nothing in a run can use them; a script that tests delivery sets its own.
for _name in ('BRIGHTSTARS_SMS_API_TOKEN', 'BRIGHTSTARS_SMS_SENDER_ID', 'BRIGHTSTARS_SMTP_HOST', 'BRIGHTSTARS_SMTP_USER',
              'BRIGHTSTARS_SMTP_PASSWORD', 'BRIGHTSTARS_SMTP_FROM'):
    os.environ[_name] = ''

import sqlalchemy as sa  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402


def _configured_url():
    url = os.environ.get('BRIGHTSTARS_PLATFORM_DB', '').strip()
    if not url:
        print('SKIPPED: BRIGHTSTARS_PLATFORM_DB is not set, so there is no PostgreSQL server '
              'to test against. Set it in .env (see README > Getting started).')
        raise SystemExit(2)
    if make_url(url).get_backend_name() != 'postgresql':
        print(f'SKIPPED: BRIGHTSTARS_PLATFORM_DB is not a PostgreSQL URL ({url.split("://")[0]}).')
        raise SystemExit(2)
    return make_url(url)


def _admin_engine(url):
    # The server's maintenance database: "postgres" on a stock install, but e.g. "defaultdb" on Aiven.
    from control_plane import config
    return sa.create_engine(url.set(database=config.pg_maintenance_db()), isolation_level='AUTOCOMMIT')


# A run's databases are named ``bs_test_<tag>_<minutes since 1970, hex>_<random>``. The time
# lets a later run tell a crashed run's leftovers from a run that is still going.
STALE_AFTER_MINUTES = 60


def _drop_stale(url):
    """Remove test databases left behind by a run that crashed before teardown.

    Several verification scripts may run at once, so a database is only removed when
    nobody is connected to it AND it is old enough that its own run cannot still be
    starting up (a run creates its databases before it opens its first connection).
    """
    try:
        engine = _admin_engine(url)
    except Exception:
        return
    now_minutes = int(time.time() // 60)
    try:
        with engine.connect() as conn:
            for name in [r[0] for r in conn.execute(sa.text(
                    "SELECT datname FROM pg_database WHERE datname LIKE 'bs\\_test\\_%' "
                    "AND NOT EXISTS (SELECT 1 FROM pg_stat_activity a WHERE a.datname = pg_database.datname)"))]:
                stamp = re.match(r'bs_test_.+_([0-9a-f]{6,8})_[0-9a-f]{8}(_|$)', name)
                if stamp and now_minutes - int(stamp.group(1), 16) < STALE_AFTER_MINUTES:
                    continue
                conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
    except Exception:
        pass
    finally:
        engine.dispose()


def _build_schools_inline():
    """A school is built on a background thread, and the page follows it with a progress poll. A script
    that submits the new-school form and goes straight on to use the school needs it finished, so the
    thread named ``create-school-<code>`` is run to completion right where it starts."""
    import threading

    real_start = threading.Thread.start

    def start(self, *args, **kwargs):
        if (self.name or '').startswith('create-school-'):
            self.run()
            return
        return real_start(self, *args, **kwargs)

    threading.Thread.start = start


def setup(tag):
    """Point this process at a fresh, uniquely named registry database.

    Returns ``(prefix, teardown)``: every database this run creates starts with
    ``prefix``, and ``teardown()`` drops all of them.
    """
    url = _configured_url()
    _drop_stale(url)
    prefix = f'bs_test_{tag}_{int(time.time() // 60):x}_{uuid.uuid4().hex[:8]}'
    try:
        engine = _admin_engine(url)
        with engine.connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{prefix}_platform"'))
        engine.dispose()
    except sa.exc.OperationalError as exc:
        print(f'SKIPPED: could not reach PostgreSQL at {url.host}:{url.port or 5432} — {exc}')
        raise SystemExit(2)

    os.environ['BRIGHTSTARS_PLATFORM_DB'] = url.set(
        database=f'{prefix}_platform').render_as_string(hide_password=False)
    # Each school's database is named from this template, so they all carry the
    # run's prefix and can be found and dropped again afterwards.
    os.environ['BRIGHTSTARS_SCHOOL_DB_TEMPLATE'] = url.set(
        database=f'{prefix}_{{slug}}').render_as_string(hide_password=False).replace('%7Bslug%7D', '{slug}')  # SQLAlchemy percent-encodes the braces; the template needs them literal

    _build_schools_inline()

    def teardown():
        from control_plane.routing import dispose_engines
        from control_plane.registry import dispose_platform_engine
        try:
            dispose_engines()
            dispose_platform_engine()
        except Exception:
            pass
        engine = _admin_engine(url)
        try:
            with engine.connect() as conn:
                names = [r[0] for r in conn.execute(sa.text(
                    'SELECT datname FROM pg_database WHERE datname LIKE :p'), {'p': prefix + '%'})]
                for name in names:
                    conn.execute(sa.text(
                        'SELECT pg_terminate_backend(pid) FROM pg_stat_activity '
                        'WHERE datname = :n AND pid <> pg_backend_pid()'), {'n': name})
                    conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        finally:
            engine.dispose()

    return prefix, teardown
