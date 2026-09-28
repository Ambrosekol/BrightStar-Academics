"""Reads and writes the deployment's own ``.env`` file - the configuration mechanism this
application already uses (``control_plane/config.py``'s own ``load_dotenv()`` call). The platform
Settings page edits this file directly rather than a database, so a change made in the portal is
the exact same kind of change an operator would otherwise make by hand over SSH.

Preserves every comment, blank line and the existing order; only ever touches the one line (or adds
one new line) for the variable being changed. Refuses to write a name ``settings_registry.py`` does
not know about, so this can never be used to plant an arbitrary environment variable through the web.
"""

import os
import re
import shutil
from datetime import datetime, timezone

from . import config, settings_registry

ENV_PATH = os.path.join(config.BASE, '.env')
_LINE_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)$')


def _read_lines():
    if not os.path.exists(ENV_PATH):
        return []
    with open(ENV_PATH, 'r', encoding='utf-8') as f:
        return f.read().splitlines()


def read_values():
    """``{name: value}`` for every active (uncommented) ``NAME=value`` line in ``.env``, as
    currently saved on disk - not necessarily what the running process sees; see
    :func:`host_overridden`."""
    values = {}
    for line in _read_lines():
        match = _LINE_RE.match(line.strip())
        if match:
            values[match.group(1)] = match.group(2)
    return values


def effective_value(name):
    """What the running process actually uses right now, whatever it came from."""
    return os.environ.get(name, '')


def host_overridden(name):
    """True when the running process's value did not come from ``.env`` - it was already set in
    the real environment before this process started (systemd, Docker, a hosting panel's own
    environment settings...). ``python-dotenv`` never overrides an already-set variable, so editing
    ``.env`` here would have no effect until that other place is changed too; the Settings page must
    say so plainly rather than let an edit look like it already took effect.
    """
    if name not in os.environ:
        return False
    saved = read_values().get(name)
    return saved is None or saved != os.environ[name]


def write_value(name, value):
    """Set ``name`` to ``value`` in ``.env``, keeping every other line untouched.

    Writes a timestamped backup of the previous file alongside it before making any change, and
    writes the new file to a temporary path first, then swaps it into place atomically, so a
    crash or a concurrent read never sees a half-written file.
    """
    if settings_registry.get(name) is None:
        raise ValueError(f'"{name}" is not a recognised system-wide setting.')
    lines = _read_lines()
    new_line = f'{name}={value}'
    replaced = False
    for i, line in enumerate(lines):
        match = _LINE_RE.match(line.strip())
        if match and match.group(1) == name:
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        if lines and lines[-1].strip():
            lines.append('')
        lines.append(new_line)
    _write_atomically(lines)


def _write_atomically(lines):
    if os.path.exists(ENV_PATH):
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
        # Matches the existing .env.backup-* convention (.gitignore), so a backup this writes is
        # never at risk of being committed alongside the file it protects.
        shutil.copy2(ENV_PATH, f'{ENV_PATH}.backup-{stamp}')
    tmp_path = f'{ENV_PATH}.tmp-{os.getpid()}'
    with open(tmp_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(lines) + ('\n' if lines else ''))
    os.replace(tmp_path, ENV_PATH)
