"""Contract: school exports find a pg_dump that is the same major version as the server or newer.

The lookup is: BRIGHTSTARS_PG_DUMP if set, then pg_dump on the PATH, then the standard install
places (the newest version first). These checks use temporary folders and monkeypatching, so they
need no PostgreSQL install.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from control_plane import provisioning  # noqa: E402


def _fake_install(root, version):
    binary = root / version / 'bin' / 'pg_dump'
    binary.parent.mkdir(parents=True)
    binary.write_text('')
    return binary


def test_the_override_names_the_program_and_must_be_a_file():
    with tempfile.TemporaryDirectory() as tmp:
        program = Path(tmp) / 'pg_dump'
        program.write_text('')
        os.environ['BRIGHTSTARS_PG_DUMP'] = str(program)
        try:
            assert provisioning.find_pg_dump() == program
            os.environ['BRIGHTSTARS_PG_DUMP'] = str(Path(tmp) / 'missing')
            assert provisioning.find_pg_dump() is None
        finally:
            os.environ.pop('BRIGHTSTARS_PG_DUMP', None)


def test_installs_are_listed_newest_version_first():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for version in ('9.6', '18', '17'):
            _fake_install(root, version)
        order = [p.parts[-3] for p in provisioning._installs_newest_first(root, 'bin/pg_dump')]
        assert order == ['18', '17', '9.6'], order


def test_the_path_is_tried_before_the_install_folders():
    with tempfile.TemporaryDirectory() as tmp:
        on_path = Path(tmp) / 'pg_dump'
        on_path.write_text('')
        original = shutil.which
        shutil.which = lambda name, *a, **k: str(on_path) if name == 'pg_dump' else original(name, *a, **k)
        os.environ.pop('BRIGHTSTARS_PG_DUMP', None)
        try:
            assert provisioning.find_pg_dump() == on_path
        finally:
            shutil.which = original


def test_nothing_found_gives_none_rather_than_a_guess():
    original_which = shutil.which
    original_program_files = os.environ.get('ProgramFiles')
    shutil.which = lambda name, *a, **k: None
    os.environ.pop('BRIGHTSTARS_PG_DUMP', None)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            assert provisioning._installs_newest_first(Path(tmp) / 'absent', 'bin/pg_dump') == []
            # On Windows, point the search at an empty folder, so an install on this machine cannot answer.
            os.environ['ProgramFiles'] = tmp
            if os.name == 'nt':
                assert provisioning.find_pg_dump() is None
    finally:
        shutil.which = original_which
        if original_program_files is None:
            os.environ.pop('ProgramFiles', None)
        else:
            os.environ['ProgramFiles'] = original_program_files
