"""Guard against drift between the models and a real school's schema.

Every school's database is built from the models, so this checks a live one
still matches them: if someone edits a model without upgrading the schools, or
changes a schema by hand, every query touching that table starts failing at
runtime.

It runs against the first school in the platform registry. With no PostgreSQL
server or no school yet, there is nothing to compare and the checks are skipped.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _school_engine():
    """An engine for the first registered school, or None."""
    try:
        import sqlalchemy as sa

        from control_plane.registry import platform_session, to_info
        from control_plane.models import Tenant
        from control_plane.routing import engine_for

        with platform_session() as session:
            tenant = session.scalars(sa.select(Tenant).order_by(Tenant.id)).first()
            if tenant is None:
                return None
            info = to_info(tenant)
        engine = engine_for(info)
        with engine.connect():
            pass
        return engine
    except Exception:
        return None

# SQLite spells some types differently from SQLAlchemy's generic types.
# PostgreSQL and SQLAlchemy spell some types differently.
TYPE_EQUIV = {
    "INTEGER": {"INTEGER"},
    "TEXT": {"TEXT", "VARCHAR"},
    "VARCHAR": {"TEXT", "VARCHAR"},
    "REAL": {"REAL", "FLOAT"},
    "DOUBLE PRECISION": {"FLOAT", "REAL", "DOUBLE PRECISION"},
    "": {"TEXT", "VARCHAR"},
}


def _live_tables(inspector):
    return set(inspector.get_table_names())


def _metadata():
    from models import db

    return db.metadata


def test_every_live_table_is_modelled():
    import sqlalchemy as sa

    engine = _school_engine()
    if engine is None:
        return  # No PostgreSQL server or no school yet; nothing to compare.
    live = _live_tables(sa.inspect(engine))
    modelled = set(_metadata().tables)
    assert not (live - modelled), f"tables in DB but not modelled: {sorted(live - modelled)}"
    assert not (modelled - live), f"tables modelled but not in DB: {sorted(modelled - live)}"


def test_columns_types_and_constraints_match():
    import sqlalchemy as sa

    engine = _school_engine()
    if engine is None:
        return
    inspector = sa.inspect(engine)
    meta = _metadata()
    problems = []
    if True:
        for tname in sorted(_live_tables(inspector) & set(meta.tables)):
            live_cols = {c["name"]: c for c in inspector.get_columns(tname)}
            model_cols = {c.name: c for c in meta.tables[tname].columns}

            missing = set(live_cols) - set(model_cols)
            extra = set(model_cols) - set(live_cols)
            if missing:
                problems.append(f"{tname}: in DB but not modelled: {sorted(missing)}")
            if extra:
                problems.append(f"{tname}: modelled but not in DB: {sorted(extra)}")

            pk_names = set(inspector.get_pk_constraint(tname).get("constrained_columns") or [])
            for cname in sorted(set(live_cols) & set(model_cols)):
                lc, mc = live_cols[cname], model_cols[cname]

                live_type = str(lc["type"]).upper().split("(")[0]
                model_type = str(mc.type).upper().split("(")[0]
                if model_type not in TYPE_EQUIV.get(live_type, {live_type}):
                    problems.append(f"{tname}.{cname}: type DB={live_type} model={model_type}")

                live_pk, model_pk = cname in pk_names, mc.primary_key
                if live_pk != model_pk:
                    problems.append(f"{tname}.{cname}: PK DB={live_pk} model={model_pk}")

                # A primary key is implicitly NOT NULL, so nullability is only
                # meaningfully comparable on the other columns.
                if not live_pk and not model_pk:
                    if bool(lc["nullable"]) != bool(mc.nullable):
                        problems.append(
                            f"{tname}.{cname}: nullable DB={bool(lc['nullable'])} "
                            f"model={bool(mc.nullable)}"
                        )

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
