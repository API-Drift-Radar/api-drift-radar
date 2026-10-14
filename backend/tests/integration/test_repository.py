import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from radar.storage import database, repository
from radar.storage.contracts import ContractFiles
from radar.storage.errors import ConflictError, NotFoundError, StoreError
from radar.storage.identifiers import format_timestamp, is_valid_id, new_id
from radar.storage.records import AMBIGUOUS, FAILED, RUNNING, UNCONFIRMED
from radar.storage.repository import MAX_ERROR_LENGTH, Store

URL = "https://api.example.com"
T0 = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
ROOT = b"openapi: 3.1.0\n"
NORMALIZED = b'{"openapi":"3.1.0"}'


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name) / ".radar"
        self.store = self.open_store()

    def open_store(self):
        store = Store(self.data)
        self.addCleanup(store.close)
        return store

    def add_snapshot(self, source_id, captured_at=T0, content=ROOT):
        """Record an initial capture directly, as complete_check will.

        Uses its own connection, the way another process would.
        """
        fingerprint = ContractFiles(self.data / "contracts").put(
            "root.yaml", {"root.yaml": content}, NORMALIZED, "norm-1"
        )
        snapshot_id = new_id("snap")
        check_id = new_id("chk")
        when = format_timestamp(captured_at)
        db = database.connect(self.data / "radar.db")
        try:
            with database.transaction(db):
                db.execute(
                    "INSERT OR IGNORE INTO contracts (fingerprint, entry_point, "
                    "normalization_version, normalized_sha256, document_count, "
                    "total_size, created_at) VALUES (?, 'root.yaml', 'norm-1', ?, 1, ?, ?)",
                    (fingerprint, "b" * 64, len(content), when),
                )
                db.execute(
                    "INSERT INTO snapshots (id, source_id, fingerprint, captured_at, "
                    "normalization_version, openapi_version) "
                    "VALUES (?, ?, ?, ?, 'norm-1', '3.1.0')",
                    (snapshot_id, source_id, fingerprint, when),
                )
                db.execute(
                    "INSERT INTO snapshot_documents (snapshot_id, source_url, path, "
                    "retrieved_at, media_type, is_root) VALUES (?, ?, 'root.yaml', ?, "
                    "'application/yaml', 1)",
                    (snapshot_id, URL + "/openapi.yaml", when),
                )
                db.execute(
                    "INSERT INTO check_runs (id, source_id, status, started_at, "
                    "finished_at, snapshot_id) VALUES (?, ?, 'initial', ?, ?, ?)",
                    (check_id, source_id, when, when, snapshot_id),
                )
                db.execute(
                    "UPDATE sources SET latest_snapshot_id = ? WHERE id = ?",
                    (snapshot_id, source_id),
                )
        finally:
            db.close()
        return snapshot_id, check_id


class OpeningTests(StoreTestCase):
    def test_creates_database_in_data_directory(self):
        self.assertTrue((self.data / "radar.db").is_file())

    def test_uses_configured_paths_by_default(self):
        other = self.data.parent / "configured"
        with patch.object(repository.settings, "DATABASE_PATH", other / "radar.db"), \
                patch.object(repository.settings, "CONTRACTS_DIRECTORY", other / "contracts"):
            with Store() as store:
                store.add_source(URL)
        self.assertTrue((other / "radar.db").is_file())

    def test_refuses_use_from_another_thread(self):
        errors = []

        def use_store():
            try:
                self.store.list_sources()
            except Exception as error:  # noqa: BLE001 - recording whatever happens
                errors.append(error)

        thread = threading.Thread(target=use_store)
        thread.start()
        thread.join()
        self.assertEqual(len(errors), 1)


class SourceTests(StoreTestCase):
    def test_add_and_get(self):
        source = self.store.add_source(URL, {"api_version": "2024-01-01"})
        self.assertTrue(is_valid_id(source.id, "src"))
        self.assertEqual(source.locator, URL)
        self.assertEqual(source.config, {"api_version": "2024-01-01"})
        self.assertIsNone(source.latest_snapshot_id)
        self.assertEqual(source.created_at.utcoffset(), timedelta(0))
        self.assertEqual(self.store.get_source(source.id), source)

    def test_config_defaults_to_empty(self):
        self.assertEqual(self.store.add_source(URL).config, {})

    def test_locator_is_trimmed(self):
        self.assertEqual(self.store.add_source(f"  {URL}\t").locator, URL)

    def test_same_locator_can_be_monitored_twice(self):
        first = self.store.add_source(URL, {"api_version": "v1"})
        second = self.store.add_source(URL, {"api_version": "v2"})
        self.assertNotEqual(first.id, second.id)
        self.assertEqual([s.id for s in self.store.find_sources(URL)], [first.id, second.id])
        self.assertEqual(self.store.find_sources(" " + URL), self.store.find_sources(URL))
        self.assertEqual(self.store.find_sources("https://nowhere.example"), [])
        self.assertEqual(self.store.find_sources(None), [])

    def test_list_sources_oldest_first_with_limit(self):
        ids = [self.store.add_source(f"{URL}/{n}").id for n in range(3)]
        self.assertEqual([s.id for s in self.store.list_sources()], ids)
        self.assertEqual([s.id for s in self.store.list_sources(limit=2)], ids[:2])

    def test_update_config_replaces_it(self):
        source = self.store.add_source(URL, {"api_version": "v1", "paused": False})
        updated = self.store.update_source_config(source.id, {"paused": True})
        self.assertEqual(updated.config, {"paused": True})
        self.assertEqual(updated.locator, source.locator)
        self.assertEqual(updated.created_at, source.created_at)

    def test_rejects_bad_locators(self):
        for bad in (None, 7, "", "   ", "https://x.example/\nnext", "x" * 2049):
            with self.subTest(locator=bad), self.assertRaises(ValueError):
                self.store.add_source(bad)
        self.assertEqual(self.store.list_sources(), [])

    def test_rejects_bad_config(self):
        source = self.store.add_source(URL)
        for bad in ([], "text", {"x": float("nan")}, {"x": object()}, {"x": "a" * (5 * 1024 * 1024)}):
            with self.subTest(config=str(bad)[:40]):
                with self.assertRaises(ValueError):
                    self.store.add_source(URL, bad)
                with self.assertRaises(ValueError):
                    self.store.update_source_config(source.id, bad)
        self.assertEqual(self.store.get_source(source.id).config, {})

    def test_unknown_or_malformed_ids_are_not_found(self):
        for bad in (new_id("src"), new_id("chk"), "src_1", "", None, "'; DROP TABLE sources; --"):
            with self.subTest(id=bad):
                with self.assertRaises(NotFoundError):
                    self.store.get_source(bad)
                with self.assertRaises(NotFoundError):
                    self.store.update_source_config(bad, {})

    def test_returned_config_is_a_fresh_copy(self):
        source = self.store.add_source(URL, {"hints": ["a"]})
        source.config["hints"].append("b")
        self.assertEqual(self.store.get_source(source.id).config, {"hints": ["a"]})


class CheckTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.source = self.store.add_source(URL)

    def test_begin_check(self):
        check = self.store.begin_check(self.source.id, started_at=T0)
        self.assertTrue(is_valid_id(check.id, "chk"))
        self.assertEqual(check.status, RUNNING)
        self.assertEqual(check.started_at, T0)
        self.assertIsNone(check.finished_at)
        self.assertFalse(check.finished)
        self.assertFalse(check.succeeded)

    def test_begin_check_requires_an_existing_source(self):
        with self.assertRaises(NotFoundError):
            self.store.begin_check(new_id("src"))

    def test_begin_check_rejects_naive_start_time(self):
        with self.assertRaises(ValueError):
            self.store.begin_check(self.source.id, started_at=datetime(2026, 1, 1))

    def test_fail_check_records_everything(self):
        check = self.store.begin_check(self.source.id, started_at=T0)
        failed = self.store.fail_check(
            check.id,
            stage="fetch",
            reason="inaccessible",
            error="Connection timed out after 30 s",
            detail={"status": 503},
            finished_at=T0 + timedelta(seconds=31),
        )
        self.assertEqual(failed.status, FAILED)
        self.assertTrue(failed.finished)
        self.assertFalse(failed.succeeded)
        self.assertEqual(failed.failure_stage, "fetch")
        self.assertEqual(failed.failure_reason, "inaccessible")
        self.assertEqual(failed.error, "Connection timed out after 30 s")
        self.assertEqual(failed.detail, {"status": 503})
        self.assertEqual(failed.finished_at, T0 + timedelta(seconds=31))
        self.assertIsNone(failed.snapshot_id)

    def test_ambiguous_discovery_keeps_candidates(self):
        check = self.store.begin_check(self.source.id)
        candidates = {"candidates": [{"source_url": f"{URL}/v1.json"}, {"source_url": f"{URL}/v2.json"}]}
        result = self.store.fail_check(
            check.id, stage="discovery", reason="ambiguous",
            error="Two plausible contracts", outcome=AMBIGUOUS, detail=candidates,
        )
        self.assertEqual(result.status, AMBIGUOUS)
        self.assertEqual(result.detail, candidates)

    def test_check_finishes_only_once(self):
        check = self.store.begin_check(self.source.id)
        self.store.fail_check(check.id, stage="fetch", reason="inaccessible", error="down")
        with self.assertRaises(ConflictError):
            self.store.fail_check(check.id, stage="fetch", reason="inaccessible", error="again")
        self.assertEqual(self.store.get_check(check.id).error, "down")

    def test_rejects_bad_failure_details(self):
        check = self.store.begin_check(self.source.id, started_at=T0)
        bad_calls = [
            dict(stage="nowhere", reason="x", error="e"),
            dict(stage="fetch", reason="Not A Code", error="e"),
            dict(stage="fetch", reason="", error="e"),
            dict(stage="fetch", reason="x" * 65, error="e"),
            dict(stage="fetch", reason="x", error="   "),
            dict(stage="fetch", reason="x", error=None),
            dict(stage="fetch", reason="x", error="e", outcome="changed"),
            dict(stage="fetch", reason="x", error="e", detail=["not", "a", "dict"]),
            dict(stage="fetch", reason="x", error="e", finished_at=T0 - timedelta(seconds=1)),
            dict(stage="fetch", reason="x", error="e", finished_at=datetime(2026, 1, 1)),
        ]
        for kwargs in bad_calls:
            with self.subTest(**{k: str(v)[:30] for k, v in kwargs.items()}):
                with self.assertRaises(ValueError):
                    self.store.fail_check(check.id, **kwargs)
        self.assertEqual(self.store.get_check(check.id).status, RUNNING)

    def test_long_errors_are_truncated_not_rejected(self):
        check = self.store.begin_check(self.source.id)
        failed = self.store.fail_check(
            check.id, stage="comparison", reason="internal_error", error="x" * 10_000
        )
        self.assertEqual(len(failed.error), MAX_ERROR_LENGTH)
        self.assertTrue(failed.error.endswith("[truncated]"))

    def test_unknown_check_is_not_found(self):
        for call in (
            lambda: self.store.get_check(new_id("chk")),
            lambda: self.store.fail_check(new_id("chk"), stage="fetch", reason="x", error="e"),
            lambda: self.store.get_check(self.source.id),
        ):
            with self.assertRaises(NotFoundError):
                call()

    def test_list_checks_newest_first_with_limit(self):
        ids = [
            self.store.begin_check(self.source.id, started_at=T0 + timedelta(minutes=n)).id
            for n in range(3)
        ]
        self.assertEqual([c.id for c in self.store.list_checks(self.source.id)], ids[::-1])
        self.assertEqual([c.id for c in self.store.list_checks(self.source.id, limit=1)], [ids[-1]])
        for bad in (0, -1, True, "2"):
            with self.subTest(limit=bad), self.assertRaises(ValueError):
                self.store.list_checks(self.source.id, limit=bad)

    def test_list_checks_only_for_that_source(self):
        other = self.store.add_source("https://other.example")
        self.store.begin_check(other.id)
        self.assertEqual(self.store.list_checks(self.source.id), [])

    def test_list_running_checks(self):
        first = self.store.begin_check(self.source.id, started_at=T0)
        done = self.store.begin_check(self.source.id, started_at=T0 + timedelta(minutes=1))
        second = self.store.begin_check(self.source.id, started_at=T0 + timedelta(minutes=2))
        self.store.fail_check(done.id, stage="fetch", reason="inaccessible", error="down")
        self.assertEqual([c.id for c in self.store.list_running_checks()], [first.id, second.id])


class FailedCheckReferenceTests(StoreTestCase):
    """Acceptance: failed checks are recorded without advancing the latest valid snapshot."""

    def test_failure_keeps_the_latest_snapshot(self):
        source = self.store.add_source(URL)
        snapshot_id, _ = self.add_snapshot(source.id)

        for stage, reason in (("fetch", "inaccessible"), ("validation", "invalid"),
                              ("capture", "incomplete"), ("comparison", "internal_error")):
            check = self.store.begin_check(source.id)
            failed = self.store.fail_check(check.id, stage=stage, reason=reason, error="boom")
            self.assertEqual(failed.before_snapshot_id, snapshot_id)
            self.assertEqual(self.store.get_source(source.id).latest_snapshot_id, snapshot_id)

        self.assertEqual(self.store.latest_snapshot(source.id).id, snapshot_id)
        self.assertEqual(len(self.store.list_snapshots(source.id)), 1)

    def test_failure_before_any_capture_has_no_reference(self):
        source = self.store.add_source(URL)
        check = self.store.begin_check(source.id)
        failed = self.store.fail_check(check.id, stage="discovery", reason="not_found", error="none")
        self.assertIsNone(failed.before_snapshot_id)
        self.assertIsNone(self.store.latest_snapshot(source.id))


class UnconfirmedDiscoveryTests(StoreTestCase):
    """An unconfirmed contract is recorded, but never becomes the baseline."""

    CANDIDATE = {
        "candidate": {
            "source_url": f"{URL}/openapi.json",
            "discovery_method": "directory",
            "source_kind": "directory_copy",
            "evidence": [{"criterion": "server_host", "outcome": "match"}],
        }
    }

    def record_unconfirmed(self, source_id):
        check = self.store.begin_check(source_id)
        return self.store.fail_check(
            check.id, stage="discovery", reason="unconfirmed",
            error="Found a contract on this host, but nothing confirmed it is this API.",
            outcome=UNCONFIRMED, detail=self.CANDIDATE,
        )

    def test_recorded_with_its_candidate(self):
        source = self.store.add_source(URL)
        check = self.record_unconfirmed(source.id)
        self.assertEqual(check.status, UNCONFIRMED)
        self.assertTrue(check.finished)
        self.assertFalse(check.succeeded)
        self.assertIsNone(check.snapshot_id)
        self.assertEqual(check.detail, self.CANDIDATE)
        self.assertEqual(self.store.list_snapshots(source.id), [])
        self.assertIsNone(self.store.latest_snapshot(source.id))

    def test_does_not_replace_an_existing_baseline(self):
        source = self.store.add_source(URL)
        snapshot_id, _ = self.add_snapshot(source.id)
        check = self.record_unconfirmed(source.id)
        self.assertEqual(check.before_snapshot_id, snapshot_id)
        self.assertEqual(self.store.latest_snapshot(source.id).id, snapshot_id)
        self.assertEqual(len(self.store.list_snapshots(source.id)), 1)


class SnapshotReadTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.source = self.store.add_source(URL)
        self.snapshot_id, self.check_id = self.add_snapshot(self.source.id)

    def test_get_and_latest_snapshot(self):
        snapshot = self.store.get_snapshot(self.snapshot_id)
        self.assertEqual(snapshot.source_id, self.source.id)
        self.assertEqual(snapshot.captured_at, T0)
        self.assertEqual(snapshot.normalization_version, "norm-1")
        self.assertEqual(snapshot.openapi_version, "3.1.0")
        self.assertEqual(snapshot.provenance, {})
        self.assertEqual(self.store.latest_snapshot(self.source.id), snapshot)

    def test_list_snapshots_newest_first(self):
        later_id, _ = self.add_snapshot(self.source.id, T0 + timedelta(hours=1), ROOT + b"# v2\n")
        self.assertEqual(
            [s.id for s in self.store.list_snapshots(self.source.id)], [later_id, self.snapshot_id]
        )
        self.assertEqual(len(self.store.list_snapshots(self.source.id, limit=1)), 1)

    def test_snapshot_documents(self):
        (document,) = self.store.snapshot_documents(self.snapshot_id)
        self.assertEqual(document.source_url, URL + "/openapi.yaml")
        self.assertEqual(document.path, "root.yaml")
        self.assertEqual(document.retrieved_at, T0)
        self.assertTrue(document.is_root)

    def test_read_contract(self):
        contract = self.store.read_contract(self.snapshot_id)
        self.assertEqual(contract.original, ROOT)
        self.assertEqual(contract.normalized, NORMALIZED)
        self.assertEqual(contract.normalization_version, "norm-1")

    def test_read_contract_reports_damaged_content(self):
        fingerprint = self.store.get_snapshot(self.snapshot_id).fingerprint
        (self.data / "contracts" / fingerprint / "normalized").write_bytes(b"tampered")
        with self.assertRaises(StoreError):
            self.store.read_contract(self.snapshot_id)

    def test_unknown_snapshot_is_not_found(self):
        for call in (
            lambda: self.store.get_snapshot(new_id("snap")),
            lambda: self.store.snapshot_documents(new_id("snap")),
            lambda: self.store.read_contract(new_id("snap")),
            lambda: self.store.list_snapshots(new_id("src")),
            lambda: self.store.latest_snapshot(new_id("src")),
        ):
            with self.assertRaises(NotFoundError):
                call()


class ChangeReadTests(StoreTestCase):
    def test_no_changes_yet(self):
        source = self.store.add_source(URL)
        _, check_id = self.add_snapshot(source.id)
        self.assertEqual(self.store.list_changes(check_id=check_id), [])
        self.assertEqual(self.store.list_changes(source_id=source.id), [])

    def test_requires_exactly_one_filter(self):
        source = self.store.add_source(URL)
        with self.assertRaises(ValueError):
            self.store.list_changes()
        with self.assertRaises(ValueError):
            self.store.list_changes(check_id=new_id("chk"), source_id=source.id)

    def test_unknown_ids_are_not_found(self):
        for call in (
            lambda: self.store.get_change(new_id("chg")),
            lambda: self.store.list_changes(check_id=new_id("chk")),
            lambda: self.store.list_changes(source_id=new_id("src")),
        ):
            with self.assertRaises(NotFoundError):
                call()


class InvalidTextTests(StoreTestCase):
    """Text from a JSON request may hold lone surrogates, which cannot be stored."""

    BROKEN = "bad \ud800 text"

    def test_rejected_as_value_errors(self):
        source = self.store.add_source(URL)
        check = self.store.begin_check(source.id)
        calls = [
            lambda: self.store.add_source(URL + self.BROKEN),
            lambda: self.store.add_source(URL, {"hint": self.BROKEN}),
            lambda: self.store.add_source(URL, {self.BROKEN: 1}),
            lambda: self.store.update_source_config(source.id, {"hint": self.BROKEN}),
            lambda: self.store.fail_check(check.id, stage="fetch", reason="x", error=self.BROKEN),
            lambda: self.store.fail_check(
                check.id, stage="fetch", reason="x", error="e", detail={"why": self.BROKEN}
            ),
        ]
        for index, call in enumerate(calls):
            with self.subTest(call=index), self.assertRaises(ValueError) as caught:
                call()
            self.assertNotIsInstance(caught.exception, UnicodeError)

        self.assertEqual(len(self.store.list_sources()), 1)
        self.assertEqual(self.store.get_source(source.id).config, {})
        self.assertEqual(self.store.get_check(check.id).status, RUNNING)


class CorruptedRecordTests(StoreTestCase):
    """Damage inside radar.db must surface as StoreError, never as a raw parsing error."""

    def setUp(self):
        super().setUp()
        self.source = self.store.add_source(URL)
        self.snapshot_id, self.check_id = self.add_snapshot(self.source.id)
        self.raw = sqlite3.connect(self.data / "radar.db")
        self.addCleanup(self.raw.close)

    def damage(self, sql, *triggers):
        for trigger in triggers:
            self.raw.execute(f"DROP TRIGGER {trigger}")
        self.raw.execute(sql)
        self.raw.commit()

    def assertCorrupted(self, label, read):
        with self.assertRaisesRegex(StoreError, f"Stored {label} .* is corrupted"):
            read()

    def test_damaged_source(self):
        self.damage("UPDATE sources SET config_json = '{oops'")
        self.assertCorrupted("source", lambda: self.store.get_source(self.source.id))

    def test_damaged_check(self):
        self.damage(
            "UPDATE check_runs SET started_at = 'yesterday'",
            "check_runs_final", "check_runs_identity",
        )
        self.assertCorrupted("check", lambda: self.store.get_check(self.check_id))

    def test_damaged_snapshot(self):
        self.damage("UPDATE snapshots SET provenance_json = 'not json'", "snapshots_immutable")
        self.assertCorrupted("snapshot", lambda: self.store.latest_snapshot(self.source.id))

    def test_damaged_snapshot_document(self):
        self.damage(
            "UPDATE snapshot_documents SET retrieved_at = '2026'",
            "snapshot_documents_immutable",
        )
        self.assertCorrupted(
            "snapshot document", lambda: self.store.snapshot_documents(self.snapshot_id)
        )


class RestartTests(StoreTestCase):
    """Acceptance: stored history remains available after restarting Radar."""

    def test_history_survives_reopening(self):
        source = self.store.add_source(URL, {"api_version": "v1"})
        snapshot_id, _ = self.add_snapshot(source.id)
        check = self.store.begin_check(source.id)
        self.store.fail_check(check.id, stage="fetch", reason="inaccessible", error="down")
        running = self.store.begin_check(source.id)
        self.store.close()

        reopened = self.open_store()
        self.assertEqual(reopened.get_source(source.id).config, {"api_version": "v1"})
        self.assertEqual(reopened.latest_snapshot(source.id).id, snapshot_id)
        self.assertEqual(reopened.read_contract(snapshot_id).original, ROOT)
        self.assertEqual(reopened.get_check(check.id).failure_reason, "inaccessible")
        self.assertEqual([c.id for c in reopened.list_running_checks()], [running.id])
        self.assertEqual(len(reopened.list_checks(source.id)), 3)


class ConcurrencyTests(StoreTestCase):
    def test_two_stores_share_one_history(self):
        other = self.open_store()
        source = self.store.add_source(URL)
        check = other.begin_check(source.id)
        self.store.fail_check(check.id, stage="fetch", reason="inaccessible", error="down")
        with self.assertRaises(ConflictError):
            other.fail_check(check.id, stage="fetch", reason="inaccessible", error="again")
        self.assertEqual(other.get_check(check.id).status, FAILED)

    def test_failed_write_leaves_nothing_behind(self):
        source = self.store.add_source(URL)
        check = self.store.begin_check(source.id)
        # Make the database refuse the update partway through the write.
        self.store._db.execute(
            "CREATE TEMP TRIGGER fail_update BEFORE UPDATE ON check_runs "
            "BEGIN SELECT RAISE(ABORT, 'simulated disk failure'); END"
        )
        with self.assertRaises(StoreError):
            self.store.fail_check(check.id, stage="fetch", reason="x", error="e")
        self.assertFalse(self.store._db.in_transaction)
        self.store._db.execute("DROP TRIGGER temp.fail_update")

        self.assertEqual(self.store.get_check(check.id).status, RUNNING)
        failed = self.store.fail_check(check.id, stage="fetch", reason="x", error="e")
        self.assertEqual(failed.status, FAILED)


if __name__ == "__main__":
    unittest.main()
