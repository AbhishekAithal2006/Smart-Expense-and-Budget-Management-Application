"""Ordered, transactional PostgreSQL schema migrations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .v0001_initial_schema import apply as apply_initial_schema


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[object], None]


MIGRATION_LOCK_ID = 6_838_812_411_092_017_135
MIGRATIONS = (
    Migration(version=1, name="initial_schema", apply=apply_initial_schema),
)


def _ordered_migrations(migrations: tuple[Migration, ...]) -> tuple[Migration, ...]:
    versions = [migration.version for migration in migrations]
    if any(version < 1 for version in versions) or versions != sorted(set(versions)):
        raise RuntimeError("Schema migrations must have unique, increasing positive versions.")
    return migrations


def apply_pending_migrations(db_factory, migrations=None) -> None:
    """Apply each pending migration and its history row in one transaction."""
    selected = _ordered_migrations(tuple(MIGRATIONS if migrations is None else migrations))

    with db_factory() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        applied_versions = {
            row["version"]
            for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }

    known_versions = {migration.version for migration in selected}
    if applied_versions - known_versions:
        raise RuntimeError("Database schema contains a migration version unknown to this application.")

    for migration in selected:
        with db_factory() as conn:
            # Serialize concurrent application startups and re-check after acquiring
            # the transaction-scoped lock.
            conn.execute("SELECT pg_advisory_xact_lock(?)", (MIGRATION_LOCK_ID,))
            applied = conn.execute(
                "SELECT name FROM schema_migrations WHERE version=?",
                (migration.version,),
            ).fetchone()
            if applied:
                if applied["name"] != migration.name:
                    raise RuntimeError(
                        f"Schema migration {migration.version} does not match its recorded name."
                    )
                continue

            migration.apply(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version,name) VALUES(?,?)",
                (migration.version, migration.name),
            )
