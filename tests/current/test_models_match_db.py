"""Guard against drift between models.py and the real cbt.db schema.

The SQLAlchemy migration keeps the models as a faithful mirror of the database
that ``init_db()`` and the ``migrations`` package produced. If someone adds a
column to one side only, every query touching that table starts failing at
runtime, so this contract is checked explicitly rather than discovered in
production.
"""

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "cbt.db"

# SQLite spells some types differently from SQLAlchemy's generic types.
TYPE_EQUIV = {
    "INTEGER": {"INTEGER"},
    "TEXT": {"TEXT", "VARCHAR"},
    "REAL": {"REAL", "FLOAT"},
    "": {"TEXT", "VARCHAR"},
}


def _live_tables(con):
    return {
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _metadata():
    from models import db

    return db.metadata


def test_every_live_table_is_modelled():
    if not DB_PATH.exists():
        return  # A fresh checkout has no database yet; create_all() defines it.
    con = sqlite3.connect(DB_PATH)
    try:
        live = _live_tables(con)
    finally:
        con.close()
    modelled = set(_metadata().tables)
    assert not (live - modelled), f"tables in DB but not modelled: {sorted(live - modelled)}"
    assert not (modelled - live), f"tables modelled but not in DB: {sorted(modelled - live)}"


def test_columns_types_and_constraints_match():
    if not DB_PATH.exists():
        return
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    meta = _metadata()
    problems = []
    try:
        for tname in sorted(_live_tables(con) & set(meta.tables)):
            live_cols = {c["name"]: c for c in con.execute(f'PRAGMA table_info("{tname}")')}
            model_cols = {c.name: c for c in meta.tables[tname].columns}

            missing = set(live_cols) - set(model_cols)
            extra = set(model_cols) - set(live_cols)
            if missing:
                problems.append(f"{tname}: in DB but not modelled: {sorted(missing)}")
            if extra:
                problems.append(f"{tname}: modelled but not in DB: {sorted(extra)}")

            for cname in sorted(set(live_cols) & set(model_cols)):
                lc, mc = live_cols[cname], model_cols[cname]

                live_type = (lc["type"] or "").upper()
                model_type = str(mc.type).upper().split("(")[0]
                if model_type not in TYPE_EQUIV.get(live_type, {live_type}):
                    problems.append(f"{tname}.{cname}: type DB={live_type} model={model_type}")

                live_pk, model_pk = bool(lc["pk"]), mc.primary_key
                if live_pk != model_pk:
                    problems.append(f"{tname}.{cname}: PK DB={live_pk} model={model_pk}")

                # SQLite reports PRIMARY KEY columns as notnull=0 (an INTEGER
                # PRIMARY KEY is a rowid alias and implicitly NOT NULL), so
                # nullability is only comparable on non-PK columns.
                if not live_pk and not model_pk:
                    if bool(lc["notnull"]) != (not mc.nullable):
                        problems.append(
                            f"{tname}.{cname}: NOT NULL DB={bool(lc['notnull'])} "
                            f"model={not mc.nullable}"
                        )
    finally:
        con.close()

    assert not problems, "model/schema drift:\n  " + "\n  ".join(problems)


def test_timestamps_are_stored_as_text():
    """The app stores ISO-8601 strings; a DateTime column would corrupt them."""
    meta = _metadata()
    for tname, table in meta.tables.items():
        for col in table.columns:
            if col.name.endswith(("_at", "_date")) or col.name in ("date_of_birth",):
                assert str(col.type).upper().startswith(("TEXT", "VARCHAR")), (
                    f"{tname}.{col.name} must stay TEXT to match stored ISO strings, "
                    f"got {col.type}"
                )
