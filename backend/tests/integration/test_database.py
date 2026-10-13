import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from radar.storage import database
from radar.storage.database import (
    MIGRATIONS_DIRECTORY,
    connect,
    load_migrations,
    schema_version,
    transaction,
)
from radar.storage.errors import StoreError

NOW = "2026-10-09T12:00:00.000000+00:00"
LATER = "2026-10-09T12:05:00.000000+00:00"
FINGERPRINT = "a" * 64


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.path = self.dir / "data" / "radar.db"

    def open(self, **kwargs):
        connection = connect(self.path, **kwargs)
        self.addCleanup(connection.close)
        return connection

    def migrations(self, *scripts):
        """Write a throwaway migrations directory for one test."""
        directory = self.dir / f"migrations-{len(list(self.dir.iterdir()))}"
        directory.mkdir()
        for number, sql in enumerate(scripts, start=1):
            (directory / f"{number:04d}_step.sql").write_text(sql, "utf-8")
        return directory

    def seed(self, db):
        """Insert one source with an initial snapshot, as storage will."""
        with transaction(db):
            db.execute(
                "INSERT INTO sources (id, locator, created_at) VALUES ('src_1', 'https://api.example.com', ?)",
                (NOW,),
            )
            db.execute(
                "INSERT INTO contracts (fingerprint, entry_point, normalization_version, "
                "normalized_sha256, document_count, total_size, created_at) "
                "VALUES (?, 'openapi.yaml', 'norm-1', ?, 1, 10, ?)",
                (FINGERPRINT, "b" * 64, NOW),
            )
            db.execute(
                "INSERT INTO snapshots (id, source_id, fingerprint, captured_at, "
                "normalization_version, openapi_version, content_fingerprint) "
                "VALUES ('snap_1', 'src_1', ?, ?, 'norm-1', '3.1.0', ?)",
                (FINGERPRINT, NOW, "d" * 64),
            )
            db.execute(
                "INSERT INTO snapshot_documents (snapshot_id, path, source_url, "
                "retrieved_at, media_type, is_root) VALUES "
                "('snap_1', 'root.yaml', 'https://api.example.com/openapi.yaml', ?, "
                "'application/yaml', 1)",
                (NOW,),
            )
            db.execute(
                "INSERT INTO check_runs (id, source_id, status, started_at, finished_at, "
                "snapshot_id) VALUES ('chk_1', 'src_1', 'initial', ?, ?, 'snap_1')",
                (NOW, LATER),
            )
            db.execute("UPDATE sources SET latest_snapshot_id = 'snap_1' WHERE id = 'src_1'")


class OpeningTests(DatabaseTestCase):
    def test_creates_database_and_parent_directory(self):
        db = self.open()
        self.assertTrue(self.path.is_file())
        self.assertEqual(schema_version(db), len(load_migrations(MIGRATIONS_DIRECTORY)))

    def test_creates_every_table(self):
        db = self.open()
        tables = {
            row["name"]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        self.assertLessEqual(
            {
                "sources", "contracts", "snapshots", "snapshot_documents",
                "check_runs", "changes",
            },
            tables,
        )

    def test_enables_safety_settings(self):
        db = self.open()
        self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 2)  # FULL

    def test_uses_configured_path_by_default(self):
        with patch.object(database.config, "DATABASE_PATH", self.path):
            with closing(connect()):
                pass
        self.assertTrue(self.path.is_file())


class RestartTests(DatabaseTestCase):
    def test_history_survives_reopening(self):
        db = self.open()
        self.seed(db)
        db.close()

        reopened = self.open()
        row = reopened.execute(
            "SELECT latest_snapshot_id FROM sources WHERE id = 'src_1'"
        ).fetchone()
        self.assertEqual(row["latest_snapshot_id"], "snap_1")
        self.assertEqual(
            reopened.execute("SELECT status FROM check_runs").fetchone()["status"], "initial"
        )

    def test_reopening_does_not_rerun_migrations(self):
        self.open().close()
        with patch.object(database, "_statements") as statements:
            self.open()
        statements.assert_not_called()


class MigrationTests(DatabaseTestCase):
    def test_applies_new_migrations_in_order(self):
        first = self.migrations("CREATE TABLE a (x);")
        self.open(migrations_directory=first).close()

        both = self.migrations("CREATE TABLE a (x);", "ALTER TABLE a ADD COLUMN y;")
        db = self.open(migrations_directory=both)
        self.assertEqual(schema_version(db), 2)
        columns = [row["name"] for row in db.execute("PRAGMA table_info(a)")]
        self.assertEqual(columns, ["x", "y"])

    def test_failed_migration_changes_nothing(self):
        broken = self.migrations("CREATE TABLE a (x);", "CREATE TABLE b (x); NOT SQL;")
        with self.assertRaisesRegex(StoreError, "0002"):
            connect(self.path, migrations_directory=broken)

        db = self.open(migrations_directory=self.migrations("CREATE TABLE a (x);"))
        self.assertEqual(schema_version(db), 1)
        tables = {r["name"] for r in db.execute("SELECT name FROM sqlite_master")}
        self.assertNotIn("b", tables)

    def test_refuses_a_database_from_a_newer_radar(self):
        db = self.open()
        db.execute("PRAGMA user_version = 99")
        db.close()
        with self.assertRaisesRegex(StoreError, "newer"):
            connect(self.path)

    def test_rejects_badly_numbered_migrations(self):
        directory = self.dir / "gappy"
        directory.mkdir()
        (directory / "0001_a.sql").write_text("CREATE TABLE a (x);")
        (directory / "0003_c.sql").write_text("CREATE TABLE c (x);")
        with self.assertRaisesRegex(StoreError, "sequence"):
            load_migrations(directory)

        (directory / "0003_c.sql").rename(directory / "3-c.sql")
        with self.assertRaisesRegex(StoreError, "named"):
            load_migrations(directory)

    def test_rejects_missing_migrations(self):
        with self.assertRaises(StoreError):
            load_migrations(self.dir / "nowhere")
        empty = self.dir / "empty"
        empty.mkdir()
        with self.assertRaises(StoreError):
            load_migrations(empty)

    def test_splits_trigger_bodies_correctly(self):
        sql = (
            "-- comment; with a semicolon\n"
            "CREATE TABLE t (x);\n\n"
            "CREATE TRIGGER t_ro BEFORE UPDATE ON t\n"
            "BEGIN SELECT RAISE(ABORT, 'no; really'); END;\n"
        )
        self.assertEqual(len(list(database._statements(sql))), 2)

    def test_splits_statements_sharing_a_line(self):
        sql = "CREATE TABLE a (x); CREATE TABLE b (y);\nINSERT INTO a VALUES ('1;2');"
        self.assertEqual(
            list(database._statements(sql)),
            ["CREATE TABLE a (x);", "CREATE TABLE b (y);", "INSERT INTO a VALUES ('1;2');"],
        )

    def test_ignores_trailing_comments_but_not_trailing_sql(self):
        self.assertEqual(len(list(database._statements("CREATE TABLE a (x);\n-- done\n"))), 1)
        with self.assertRaises(StoreError):
            list(database._statements("CREATE TABLE a (x);\nCREATE TABLE b (y)"))

    def test_migration_with_statements_on_one_line(self):
        directory = self.migrations("CREATE TABLE a (x); CREATE TABLE b (y);")
        db = self.open(migrations_directory=directory)
        tables = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertLessEqual({"a", "b"}, tables)


class TransactionTests(DatabaseTestCase):
    def test_commits_on_success(self):
        db = self.open()
        self.seed(db)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 1)

    def test_rolls_back_everything_on_error(self):
        db = self.open()
        with self.assertRaises(RuntimeError):
            with transaction(db):
                db.execute(
                    "INSERT INTO sources (id, locator, created_at) VALUES ('src_x', 'u', ?)",
                    (NOW,),
                )
                raise RuntimeError("crash before commit")
        self.assertEqual(db.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 0)
        self.assertFalse(db.in_transaction)

    def test_wraps_sqlite_errors(self):
        db = self.open()
        with self.assertRaises(StoreError):
            with transaction(db):
                db.execute("INSERT INTO nowhere VALUES (1)")

    def test_refuses_nested_transactions(self):
        db = self.open()
        with transaction(db):
            with self.assertRaises(StoreError):
                with transaction(db):
                    pass

    def test_uncommitted_writes_are_invisible_to_other_connections(self):
        writer = self.open()
        reader = self.open()
        with transaction(writer):
            writer.execute(
                "INSERT INTO sources (id, locator, created_at) VALUES ('src_x', 'u', ?)",
                (NOW,),
            )
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 0)
        self.assertEqual(reader.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 1)


class SchemaRuleTests(DatabaseTestCase):
    """The schema itself enforces the history rules, whatever code writes to it."""

    def setUp(self):
        super().setUp()
        self.db = self.open()
        self.seed(self.db)

    def assertRejected(self, sql, *params):
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.execute(sql, params)

    def test_history_cannot_be_edited(self):
        self.assertRejected("UPDATE snapshots SET captured_at = ?", LATER)
        self.assertRejected("UPDATE contracts SET entry_point = 'x'")
        self.assertRejected("UPDATE check_runs SET error = 'rewritten'")

    def test_history_cannot_be_deleted(self):
        for table in ("snapshot_documents", "snapshots", "contracts", "check_runs"):
            with self.subTest(table=table):
                self.assertRejected(f"DELETE FROM {table}")

    def test_document_origins_are_permanent(self):
        self.assertRejected("UPDATE snapshot_documents SET source_url = 'https://evil.example'")

    def test_snapshot_has_at_most_one_root_document(self):
        self.db.execute(
            "INSERT INTO snapshot_documents (snapshot_id, path, source_url, retrieved_at) "
            "VALUES ('snap_1', 'refs/user.yaml', 'https://api.example.com/user.yaml', ?)",
            (NOW,),
        )
        self.assertRejected(
            "INSERT INTO snapshot_documents (snapshot_id, path, source_url, retrieved_at, "
            "is_root) VALUES ('snap_1', 'other.yaml', 'https://api.example.com/x', ?, 1)",
            NOW,
        )

    def test_each_url_appears_once_per_snapshot(self):
        self.assertRejected(
            "INSERT INTO snapshot_documents (snapshot_id, path, source_url, retrieved_at) "
            "VALUES ('snap_1', 'refs/x.yaml', 'https://api.example.com/openapi.yaml', ?)",
            NOW,
        )

    def test_identical_content_from_two_urls_may_share_a_path(self):
        for url in ("https://api.example.com/a/user.yaml", "https://api.example.com/b/user.yaml"):
            self.db.execute(
                "INSERT INTO snapshot_documents (snapshot_id, path, source_url, retrieved_at) "
                "VALUES ('snap_1', 'refs/same.yaml', ?, ?)",
                (url, NOW),
            )
        count = self.db.execute(
            "SELECT COUNT(*) FROM snapshot_documents WHERE path = 'refs/same.yaml'"
        ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_document_must_belong_to_a_snapshot(self):
        self.assertRejected(
            "INSERT INTO snapshot_documents (snapshot_id, path, source_url, retrieved_at) "
            "VALUES ('snap_missing', 'root.yaml', 'https://api.example.com/x', ?)",
            NOW,
        )

    def test_failed_discovery_records_its_reason(self):
        self.insert_running_check("chk_2")
        self.db.execute(
            "UPDATE check_runs SET status = 'failed', finished_at = ?, "
            "failure_stage = 'discovery', failure_reason = 'not_found', "
            "error = 'No published contract found' WHERE id = 'chk_2'",
            (LATER,),
        )
        row = self.db.execute(
            "SELECT status, failure_stage, failure_reason FROM check_runs WHERE id = 'chk_2'"
        ).fetchone()
        self.assertEqual(tuple(row), ("failed", "discovery", "not_found"))

    def test_changes_are_permanent(self):
        self.finish_as_changed("chk_2")
        self.insert_change("chg_1", "chk_2", "src_1", "snap_1", "snap_2")
        self.assertRejected("UPDATE changes SET summary = 'x'")
        self.assertRejected("DELETE FROM changes")

    def test_change_must_match_its_check(self):
        self.finish_as_changed("chk_2")
        self.insert_running_check("chk_3")
        self.add_second_source()
        bad_changes = [
            ("running check", "chk_3", "src_1", "snap_1", "snap_2"),
            ("initial check", "chk_1", "src_1", "snap_1", "snap_2"),
            ("missing check", "chk_missing", "src_1", "snap_1", "snap_2"),
            ("wrong before", "chk_2", "src_1", "snap_2", "snap_2"),
            ("wrong after", "chk_2", "src_1", "snap_1", "snap_1"),
            ("wrong source", "chk_2", "src_2", "snap_1", "snap_2"),
        ]
        for label, check_id, source_id, before, after in bad_changes:
            with self.subTest(case=label):
                with self.assertRaises(sqlite3.DatabaseError):
                    self.insert_change("chg_x", check_id, source_id, before, after)

    def test_source_keeps_its_identity(self):
        self.assertRejected("UPDATE sources SET locator = 'https://other.example'")
        self.assertRejected("UPDATE sources SET created_at = ?", LATER)
        self.assertRejected("UPDATE sources SET id = 'src_9'")

    def test_source_config_can_change(self):
        self.db.execute("UPDATE sources SET config_json = '{\"paused\": true}' WHERE id = 'src_1'")

    def test_latest_snapshot_must_be_the_sources_own(self):
        self.add_second_source()
        self.assertRejected(
            "UPDATE sources SET latest_snapshot_id = 'snap_other' WHERE id = 'src_1'"
        )
        self.add_snapshot("snap_2", "src_1")
        self.db.execute("UPDATE sources SET latest_snapshot_id = 'snap_2' WHERE id = 'src_1'")

    def test_latest_snapshot_cannot_be_cleared(self):
        self.assertRejected("UPDATE sources SET latest_snapshot_id = NULL WHERE id = 'src_1'")

    def test_check_cannot_refer_to_another_sources_snapshot(self):
        self.add_second_source()
        self.assertRejected(
            "INSERT INTO check_runs (id, source_id, status, started_at, finished_at, "
            "snapshot_id) VALUES ('chk_x', 'src_1', 'initial', ?, ?, 'snap_other')",
            NOW, LATER,
        )
        self.add_snapshot("snap_2", "src_1")
        self.insert_running_check("chk_y")
        self.assertRejected(
            "UPDATE check_runs SET status = 'changed', finished_at = ?, "
            "before_snapshot_id = 'snap_other', snapshot_id = 'snap_2' WHERE id = 'chk_y'",
            LATER,
        )

    def test_running_check_can_finish_once(self):
        self.insert_running_check("chk_2")
        self.db.execute(
            "UPDATE check_runs SET status = 'failed', finished_at = ?, "
            "failure_stage = 'fetch', failure_reason = 'inaccessible', error = 'timeout' "
            "WHERE id = 'chk_2'",
            (LATER,),
        )
        self.assertRejected("UPDATE check_runs SET status = 'running' WHERE id = 'chk_2'")

    def test_running_check_keeps_its_identity(self):
        self.insert_running_check("chk_2")
        self.assertRejected("UPDATE check_runs SET started_at = ? WHERE id = 'chk_2'", LATER)

    def test_check_outcomes_must_be_consistent(self):
        bad_rows = [
            # Successful outcome without a snapshot.
            ("chk_a", "initial", LATER, None, None, None),
            # Failure without a stage.
            ("chk_b", "failed", LATER, None, None, None),
            # Finished status without a finish time.
            ("chk_c", "unchanged", None, "snap_1", "snap_1", None),
            # Unchanged with nothing to compare against.
            ("chk_d", "unchanged", LATER, None, "snap_1", None),
            # Initial capture that claims a previous reference.
            ("chk_e", "initial", LATER, "snap_1", "snap_1", None),
            # Unknown status.
            ("chk_f", "done", LATER, None, None, None),
        ]
        for check_id, status, finished, before, snapshot, stage in bad_rows:
            with self.subTest(check=check_id, status=status):
                self.assertRejected(
                    "INSERT INTO check_runs (id, source_id, status, started_at, "
                    "finished_at, before_snapshot_id, snapshot_id, failure_stage) "
                    "VALUES (?, 'src_1', ?, ?, ?, ?, ?, ?)",
                    check_id, status, NOW, finished, before, snapshot, stage,
                )

    def test_classification_must_be_agreed_category(self):
        self.finish_as_changed("chk_2")
        self.assertRejected(
            "INSERT INTO changes (id, check_run_id, source_id, before_snapshot_id, "
            "after_snapshot_id, ordinal, change_type, location, classification, summary) "
            "VALUES ('chg_x', 'chk_2', 'src_1', 'snap_1', 'snap_2', 0, 't', 'l', 'breaking', 's')"
        )

    def test_references_must_exist(self):
        self.assertRejected(
            "INSERT INTO snapshots (id, source_id, fingerprint, captured_at, "
            "normalization_version) VALUES ('snap_x', 'src_1', ?, ?, 'norm-1')",
            "c" * 64, NOW,
        )
        self.assertRejected(
            "UPDATE sources SET latest_snapshot_id = 'snap_missing' WHERE id = 'src_1'"
        )

    def add_snapshot(self, snapshot_id, source_id):
        self.db.execute(
            "INSERT INTO snapshots (id, source_id, fingerprint, captured_at, "
            "normalization_version) VALUES (?, ?, ?, ?, 'norm-1')",
            (snapshot_id, source_id, FINGERPRINT, LATER),
        )

    def add_second_source(self):
        self.db.execute(
            "INSERT INTO sources (id, locator, created_at) "
            "VALUES ('src_2', 'https://other.example', ?)",
            (NOW,),
        )
        self.add_snapshot("snap_other", "src_2")

    def finish_as_changed(self, check_id):
        """A 'changed' check from snap_1 to a new snap_2."""
        self.add_snapshot("snap_2", "src_1")
        self.insert_running_check(check_id)
        self.db.execute(
            "UPDATE check_runs SET status = 'changed', finished_at = ?, "
            "before_snapshot_id = 'snap_1', snapshot_id = 'snap_2' WHERE id = ?",
            (LATER, check_id),
        )

    def insert_change(self, change_id, check_id, source_id, before, after):
        self.db.execute(
            "INSERT INTO changes (id, check_run_id, source_id, before_snapshot_id, "
            "after_snapshot_id, ordinal, change_type, location, classification, summary) "
            "VALUES (?, ?, ?, ?, ?, 0, 'operation_removed', 'GET /users', "
            "'potentially_breaking', 'GET /users was removed')",
            (change_id, check_id, source_id, before, after),
        )

    def insert_running_check(self, check_id):
        self.db.execute(
            "INSERT INTO check_runs (id, source_id, status, started_at) "
            "VALUES (?, 'src_1', 'running', ?)",
            (check_id, NOW),
        )


if __name__ == "__main__":
    unittest.main()
