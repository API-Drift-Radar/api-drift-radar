"""Acceptance tests for issue #10: contract snapshot and check history storage.

One source is taken through a realistic history, one step per test class:

    initial capture -> unchanged -> failed checks -> changed -> restart

Each acceptance criterion from the issue has its own test, named after it.
"Restart" and "interrupted write" use real separate Python processes, and an
interrupted write really kills the process part-way, so these tests exercise
what happens on disk, not a simulation inside one process.
"""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import radar
from radar.domain.discovery import (
    CapturedDocument,
    ContractCandidate,
    MatchingEvidence,
    ValidatedContractPackage,
)
from radar.storage.identifiers import is_valid_id
from radar.storage.records import (
    ABSENT,
    CHANGED,
    FAILED,
    INITIAL,
    NON_BREAKING,
    POTENTIALLY_BREAKING,
    RUNNING,
    UNCHANGED,
    ChangeInput,
)
from radar.storage.repository import Store

PROVIDER = "https://api.example.com"
CONTRACT_URL = f"{PROVIDER}/openapi.yaml"
SCHEMA_URL = f"{PROVIDER}/schemas/user.yaml"
DAY_1 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
RULES = "canonical-json-1"

ROOT_V1 = b'openapi: 3.1.0\npaths:\n  /users:\n    get: {$ref: "schemas/user.yaml"}\n'
ROOT_V2 = b'openapi: 3.1.0\npaths:\n  /users:\n    post: {$ref: "schemas/user.yaml"}\n'
SCHEMA = b"type: object\nproperties:\n  id: {type: integer}\n"
NORMALIZED_V1 = b'{"paths":{"/users":{"get":{}}}}'
NORMALIZED_V2 = b'{"paths":{"/users":{"post":{}}}}'

GET_REMOVED = ChangeInput(
    change_type="operation_removed",
    location="GET /users",
    classification=POTENTIALLY_BREAKING,
    summary="GET /users was removed",
    before={"operationId": "listUsers"},
    after=ABSENT,
)
POST_ADDED = ChangeInput(
    change_type="operation_added",
    location="POST /users",
    classification=NON_BREAKING,
    summary="POST /users was added",
    before=ABSENT,
    after={"operationId": "createUser"},
)


def package(root, retrieved_at):
    """A validated contract package exactly as discovery produces it."""
    return ValidatedContractPackage(
        candidate=ContractCandidate(
            source_url=CONTRACT_URL,
            discovery_method="common_location",
            discovery_source=PROVIDER,
            evidence=(
                MatchingEvidence("server_host", "Servers match the API host", CONTRACT_URL, "match"),
            ),
        ),
        root_document=CapturedDocument(CONTRACT_URL, root, retrieved_at, "application/yaml"),
        referenced_documents=(
            CapturedDocument(SCHEMA_URL, SCHEMA, retrieved_at, "application/yaml"),
        ),
        parsed_contract={"openapi": "3.1.0"},
        openapi_version="3.1.0",
    )


# Run in a child process: start a check of the source, then try to complete it.
# argv: data directory, source id, crash point ("none", "mid_files" or
# "after_files"). A crash point kills the process with os._exit, which skips
# every cleanup handler, the closest a test can get to pulling the plug.
CHILD_SCRIPT = textwrap.dedent(
    """
    import os, sys
    from datetime import datetime, timezone
    from radar.domain.discovery import CapturedDocument, ContractCandidate, ValidatedContractPackage
    from radar.storage import contracts
    from radar.storage.repository import Store

    data, source_id, crash = sys.argv[1:4]
    now = datetime.now(timezone.utc)
    url = "https://api.example.com/openapi.yaml"
    pkg = ValidatedContractPackage(
        candidate=ContractCandidate(url, "common_location", "https://api.example.com"),
        root_document=CapturedDocument(url, b"openapi: 3.1.0\\npaths: {}\\n# crash test\\n", now),
        referenced_documents=(),
        parsed_contract={}, openapi_version="3.1.0",
    )

    if crash == "mid_files":
        # Die after the first file of the bundle is written, before the rest.
        original = contracts._write_synced
        def write_then_die(path, data):
            original(path, data)
            os._exit(9)
        contracts._write_synced = write_then_die
    elif crash == "after_files":
        # Die once the files are complete, before any record refers to them.
        original_put = contracts.ContractFiles.put
        def put_then_die(self, *args):
            original_put(self, *args)
            os._exit(9)
        contracts.ContractFiles.put = put_then_die

    store = Store(data)
    check = store.begin_check(source_id)
    print(check.id, flush=True)
    store.complete_check(
        check.id, outcome="changed", package=pkg, normalized=b'{"paths":{}}',
        normalization_version="canonical-json-1",
        before_snapshot_id=store.get_source(source_id).latest_snapshot_id,
    )
    store.close()
    """
)


def run_child(data, source_id, crash):
    """Run CHILD_SCRIPT in a new Python process; return (exit code, check id)."""
    env = dict(os.environ)
    src = str(Path(radar.__file__).resolve().parents[1])
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c", CHILD_SCRIPT, str(data), source_id, crash],
        capture_output=True, text=True, timeout=60, env=env,
    )
    lines = result.stdout.split()
    return result.returncode, (lines[0] if lines else None), result.stderr


class SourceHistory(unittest.TestCase):
    """Builds the shared history once: initial, unchanged, two failures, changed."""

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.data = Path(cls._directory.name) / ".radar"
        store = Store(cls.data)
        try:
            cls.source = store.add_source(PROVIDER)

            check = store.begin_check(cls.source.id, started_at=DAY_1)
            cls.initial = store.complete_check(
                check.id, outcome=INITIAL, package=package(ROOT_V1, DAY_1),
                normalized=NORMALIZED_V1, normalization_version=RULES,
            )

            day_2 = DAY_1 + timedelta(days=1)
            check = store.begin_check(cls.source.id, started_at=day_2)
            cls.unchanged = store.complete_check(
                check.id, outcome=UNCHANGED, package=package(ROOT_V1, day_2),
                normalized=NORMALIZED_V1, normalization_version=RULES,
                before_snapshot_id=cls.initial.snapshot_id,
            )

            cls.failures = []
            for day, stage, reason in ((3, "fetch", "inaccessible"), (4, "validation", "invalid_contract")):
                check = store.begin_check(cls.source.id, started_at=DAY_1 + timedelta(days=day))
                cls.failures.append(
                    store.fail_check(check.id, stage=stage, reason=reason, error="Provider returned 503.")
                )
            cls.reference_after_failures = store.get_source(cls.source.id).latest_snapshot_id

            day_5 = DAY_1 + timedelta(days=5)
            check = store.begin_check(cls.source.id, started_at=day_5)
            cls.changed = store.complete_check(
                check.id, outcome=CHANGED, package=package(ROOT_V2, day_5),
                normalized=NORMALIZED_V2, normalization_version=RULES,
                before_snapshot_id=cls.unchanged.snapshot_id,
                changes=[GET_REMOVED, POST_ADDED],
            )
        finally:
            store.close()

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def setUp(self):
        # Every test reads through a freshly opened store, as after a restart.
        self.store = Store(self.data)
        self.addCleanup(self.store.close)


class OriginalAndNormalizedContentTests(SourceHistory):
    def test_original_and_normalized_contract_content_can_be_stored_and_retrieved(self):
        contract = self.store.read_contract(self.initial.snapshot_id)
        self.assertEqual(contract.original, ROOT_V1)
        self.assertIn(SCHEMA, contract.documents.values())
        self.assertEqual(contract.normalized, NORMALIZED_V1)

        urls = {d.source_url: d for d in self.store.snapshot_documents(self.initial.snapshot_id)}
        self.assertTrue(urls[CONTRACT_URL].is_root)
        self.assertEqual(contract.documents[urls[SCHEMA_URL].path], SCHEMA)


class SnapshotMetadataTests(SourceHistory):
    def test_snapshots_retain_source_capture_time_content_identity_and_normalization_version(self):
        snapshot = self.store.get_snapshot(self.changed.snapshot_id)
        self.assertEqual(snapshot.source_id, self.source.id)
        self.assertEqual(snapshot.captured_at, DAY_1 + timedelta(days=5))
        self.assertRegex(snapshot.fingerprint, r"^[0-9a-f]{64}$")
        self.assertEqual(snapshot.normalization_version, RULES)
        self.assertEqual(snapshot.provenance["source_url"], CONTRACT_URL)
        self.assertEqual(snapshot.provenance["evidence"][0]["outcome"], "match")


class BeforeAndAfterTests(SourceHistory):
    def test_before_and_after_snapshots_both_remain_available(self):
        self.assertEqual(self.changed.before_snapshot_id, self.unchanged.snapshot_id)
        before = self.store.read_contract(self.changed.before_snapshot_id)
        after = self.store.read_contract(self.changed.snapshot_id)
        self.assertEqual(before.original, ROOT_V1)
        self.assertEqual(after.original, ROOT_V2)
        self.assertEqual(before.normalized, NORMALIZED_V1)
        self.assertEqual(after.normalized, NORMALIZED_V2)


class ReuseTests(SourceHistory):
    def test_identical_content_reuses_stored_content_while_preserving_each_checks_history(self):
        initial = self.store.get_snapshot(self.initial.snapshot_id)
        unchanged = self.store.get_snapshot(self.unchanged.snapshot_id)
        self.assertNotEqual(initial.id, unchanged.id)
        self.assertEqual(initial.fingerprint, unchanged.fingerprint)
        self.assertNotEqual(initial.captured_at, unchanged.captured_at)

        # Two different contents were captured, so exactly two bundles exist.
        bundles = [p for p in (self.data / "contracts").iterdir() if not p.name.startswith(".")]
        self.assertEqual(len(bundles), 2)

        statuses = [c.status for c in reversed(self.store.list_checks(self.source.id))]
        self.assertEqual(statuses, [INITIAL, UNCHANGED, FAILED, FAILED, CHANGED])


class ChangeRecordTests(SourceHistory):
    def test_change_records_link_both_snapshots_and_have_stable_identifiers(self):
        changes = self.store.list_changes(check_id=self.changed.id)
        self.assertEqual([c.location for c in changes], ["GET /users", "POST /users"])
        for change in changes:
            self.assertTrue(is_valid_id(change.id, "chg"))
            self.assertEqual(change.before_snapshot_id, self.unchanged.snapshot_id)
            self.assertEqual(change.after_snapshot_id, self.changed.snapshot_id)
            # Stable: the same ID finds the same record in a new session.
            self.assertEqual(self.store.get_change(change.id), change)
        self.assertIs(changes[0].after_value, ABSENT)
        self.assertEqual(changes[1].after_value, {"operationId": "createUser"})


class FailedCheckTests(SourceHistory):
    def test_failed_checks_are_recorded_without_advancing_the_latest_valid_snapshot(self):
        self.assertEqual(self.reference_after_failures, self.unchanged.snapshot_id)
        for failure in self.failures:
            stored = self.store.get_check(failure.id)
            self.assertEqual(stored.status, FAILED)
            self.assertIsNone(stored.snapshot_id)
            self.assertEqual(stored.before_snapshot_id, self.unchanged.snapshot_id)
            self.assertEqual(stored.error, "Provider returned 503.")
        self.assertEqual(
            [f.failure_stage for f in self.failures], ["fetch", "validation"]
        )


class RestartTests(SourceHistory):
    def test_stored_history_remains_available_after_restarting_radar(self):
        # A separate Python process reads everything back from disk.
        script = textwrap.dedent(
            """
            import sys
            from radar.storage.repository import Store
            store = Store(sys.argv[1])
            source = store.get_source(sys.argv[2])
            checks = store.list_checks(source.id)
            for snapshot in store.list_snapshots(source.id):
                store.read_contract(snapshot.id)
            print(source.latest_snapshot_id, len(checks),
                  len(store.list_snapshots(source.id)),
                  len(store.list_changes(source_id=source.id)))
            """
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(radar.__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.data), self.source.id],
            capture_output=True, text=True, timeout=60, env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), [self.changed.snapshot_id, "5", "3", "2"])


class InterruptedWriteTests(unittest.TestCase):
    """Each test kills a real process part-way through recording a capture."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name) / ".radar"
        store = Store(self.data)
        self.source = store.add_source(PROVIDER)
        check = store.begin_check(self.source.id)
        self.reference = store.complete_check(
            check.id, outcome=INITIAL, package=package(ROOT_V1, DAY_1),
            normalized=NORMALIZED_V1, normalization_version=RULES,
        ).snapshot_id
        store.close()

    def assert_nothing_recorded(self, crash):
        code, check_id, stderr = run_child(self.data, self.source.id, crash)
        self.assertEqual(code, 9, stderr)
        # Prove the crash happened where intended, by what it left on disk.
        entries = list((self.data / "contracts").iterdir())
        incomplete = [p for p in entries if p.name.startswith(".incoming-")]
        bundles = [p for p in entries if not p.name.startswith(".")]
        if crash == "mid_files":
            self.assertTrue(incomplete, "expected a half-written temporary folder")
        else:
            self.assertEqual(len(bundles), 2, "expected a complete but unreferenced bundle")

        store = Store(self.data)
        self.addCleanup(store.close)
        # The reference did not move and no new snapshot or change exists.
        self.assertEqual(store.get_source(self.source.id).latest_snapshot_id, self.reference)
        self.assertEqual(len(store.list_snapshots(self.source.id)), 1)
        self.assertEqual(store.list_changes(source_id=self.source.id), [])
        # The killed check is still running, so it can be found and closed.
        self.assertEqual([c.id for c in store.list_running_checks()], [check_id])
        closed = store.fail_check(check_id, stage="interrupted", reason="restarted", error="Radar stopped.")
        self.assertEqual(closed.status, FAILED)
        # Every successful record still points at complete, intact content.
        for snapshot in store.list_snapshots(self.source.id):
            store.read_contract(snapshot.id)
        return store

    def test_interrupted_writes_do_not_leave_successful_records_pointing_to_missing_or_incomplete_content(self):
        with self.subTest(crash="while writing contract files"):
            self.assert_nothing_recorded("mid_files")
        with self.subTest(crash="after files, before records"):
            self.assert_nothing_recorded("after_files")

    def test_the_next_check_succeeds_after_an_interrupted_one(self):
        store = self.assert_nothing_recorded("mid_files")
        store.close()
        code, check_id, stderr = run_child(self.data, self.source.id, "none")
        self.assertEqual(code, 0, stderr)
        store = Store(self.data)
        self.addCleanup(store.close)
        self.assertEqual(store.get_check(check_id).status, CHANGED)
        self.assertNotEqual(store.get_source(self.source.id).latest_snapshot_id, self.reference)
        self.assertNotIn(RUNNING, [c.status for c in store.list_checks(self.source.id)])


if __name__ == "__main__":
    unittest.main()
