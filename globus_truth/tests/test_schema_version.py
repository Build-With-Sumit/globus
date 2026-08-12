"""Schema versioning for the embedded Truth store.

The MySQL side versions its schema with files and a ledger table
(scripts/migrate.py). This store is embedded — created and owned by the library
rather than administered by an operator — so its steps live in code and its
version lives in `PRAGMA user_version`, inside the database file itself.

What these tests pin down:

  * a FRESH database is STAMPED at the current version, not migrated. The CREATE
    script already produces the current shape, so replaying steps over it would
    at best be a no-op and at worst fail on a column already present.
  * a database that PREDATES versioning (user_version = 0) is migrated, not
    assumed current,
  * a database that already received the old inline `fresh_until` patch migrates
    cleanly anyway — that patch left no record and ran on EVERY connect, so any
    real legacy database has its result and the step superseding it must
    tolerate finding it,
  * a genuine failure is NOT swallowed into a version bump,
  * migrating twice is a no-op, and existing rows survive.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from globus_truth.storage import TruthRepository


class SchemaVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.path = str(Path(self._dir.name) / "truth.sqlite3")

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_fresh_database_is_stamped_at_current_version(self) -> None:
        repo = TruthRepository(self.path)
        self.assertEqual(repo.schema_version(), TruthRepository.SCHEMA_VERSION)
        repo.close()

    def test_reopening_does_not_move_the_version(self) -> None:
        TruthRepository(self.path).close()
        repo = TruthRepository(self.path)
        self.assertEqual(repo.schema_version(), TruthRepository.SCHEMA_VERSION)
        repo.close()

    def test_memory_database_is_versioned_too(self) -> None:
        repo = TruthRepository(":memory:")
        self.assertEqual(repo.schema_version(), TruthRepository.SCHEMA_VERSION)
        repo.close()

    def _legacy_database(self) -> None:
        """A database as it looked BEFORE versioning existed.

        Built by letting the repository create the current shape and then
        winding `user_version` back to 0. It deliberately KEEPS `fresh_until`,
        because that is the realistic legacy state: the old patch added the
        column on every single connect, so any database opened even once had
        it. That also makes this the interesting arm — the step that supersedes
        the patch has to tolerate finding its own result.

        A legacy database genuinely LACKING the column cannot be constructed
        against the current schema at all: `DROP COLUMN` is refused because a
        trigger references `v.fresh_until`, and rebuilding the table breaks the
        same triggers. Modelling one would mean reconstructing a schema that no
        longer exists, which would test the fixture rather than the code.
        """
        TruthRepository(self.path).close()
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("PRAGMA user_version = 0")
            conn.commit()

    def test_database_predating_versioning_is_migrated_and_stamped(self) -> None:
        self._legacy_database()
        repo = TruthRepository(self.path)
        self.assertEqual(repo.schema_version(), TruthRepository.SCHEMA_VERSION)
        with closing(sqlite3.connect(self.path)) as conn:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(verdicts)").fetchall()
            }
        self.assertIn("fresh_until", columns)
        repo.close()

    def test_existing_rows_survive_the_migration(self) -> None:
        TruthRepository(self.path).close()
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(
                "INSERT INTO receipts (storage_id, receipt_id, agent_id, run_id,"
                " received_at, payload_json) VALUES (?,?,?,?,?,?)",
                ("s1", "r1", "agent", "run1", "2030-01-15T12:00:00Z", "{}"),
            )
            conn.execute("PRAGMA user_version = 0")
            conn.commit()
        repo = TruthRepository(self.path)
        self.assertEqual(repo.schema_version(), TruthRepository.SCHEMA_VERSION)
        with closing(sqlite3.connect(self.path)) as conn:
            kept = conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
        self.assertEqual(kept, 1)
        repo.close()

    def test_a_real_failure_does_not_bump_the_version(self) -> None:
        """Only "duplicate column name" is tolerated. Anything else must raise
        rather than silently record a step that did not happen."""
        self._legacy_database()
        broken = (
            (1, "deliberately broken", ("ALTER TABLE no_such_table ADD COLUMN x TEXT",)),
        )
        original = TruthRepository._MIGRATIONS
        try:
            TruthRepository._MIGRATIONS = broken
            with self.assertRaises(sqlite3.OperationalError):
                TruthRepository(self.path)
        finally:
            TruthRepository._MIGRATIONS = original
        with closing(sqlite3.connect(self.path)) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 0, "a failed step must leave the version alone")

    def test_migrations_are_ordered_and_uniquely_numbered(self) -> None:
        versions = [v for v, _, _ in TruthRepository._MIGRATIONS]
        self.assertEqual(versions, sorted(versions), "steps must be in order")
        self.assertEqual(len(versions), len(set(versions)), "versions must be unique")
        if versions:
            self.assertEqual(
                max(versions),
                TruthRepository.SCHEMA_VERSION,
                "SCHEMA_VERSION must match the highest step, or a fresh database "
                "is stamped at a version whose steps it never received",
            )


if __name__ == "__main__":
    unittest.main()
