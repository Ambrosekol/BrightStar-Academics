"""Schema-migration bookkeeping.

See migrations/runner.py: this table only ever records which migration
version identifiers are considered "already applied" (backfilled once at
startup); the migration SQL bodies themselves never execute. models.py plus
db.create_all() is the single source of truth for the live schema.
"""

from sqlalchemy import Text

from .base import db


class SchemaMigration(db.Model):
    __tablename__ = 'schema_migrations'

    version = db.Column(Text, primary_key=True)
    applied_at = db.Column(Text, nullable=False)
