"""Recording successful captures, using discovery's real package types.

Each acceptance criterion of issue #10 that concerns successful checks has a
test class here.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from radar.domain.discovery import (
    CapturedDocument,
    ContractCandidate,
    MatchingEvidence,
    ValidatedContractPackage,
)
from radar.storage import contracts
from radar.storage.errors import ConflictError, NotFoundError, StoreError
from radar.storage.identifiers import is_valid_id, new_id
from radar.storage.records import (
    ABSENT,
    CHANGED,
    INITIAL,
    POTENTIALLY_BREAKING,
    RESPONSE,
    RUNNING,
    UNCHANGED,
    ChangeInput,
)
from radar.storage.repository import Store

URL = "https://api.example.com"
T0 = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
ROOT_V1 = b'openapi: 3.1.0\npaths:\n  /users:\n    get: {$ref: "schemas/user.yaml"}\n'
ROOT_V2 = b'openapi: 3.1.0\npaths:\n  /users:\n    post: {$ref: "schemas/user.yaml"}\n'
USER = b"type: object\nproperties:\n  id: {type: integer}\n"
NORM_V1 = b'{"paths":{"/users":{"get":{}}}}'
NORM_V2 = b'{"paths":{"/users":{"post":{}}}}'
RULES = "canonical-json-1"


def make_package(root=ROOT_V1, retrieved_at=T0, source_kind="provider", refs=None):
    if refs is None:
        refs = (CapturedDocument(f"{URL}/schemas/user.yaml", USER, retrieved_at, "application/yaml"),)
    candidate = ContractCandidate(
        source_url=f"{URL}/openapi.yaml",
        discovery_method="common_location",
        discovery_source=URL,
        evidence=(MatchingEvidence("server_host", "Servers declare api.example.com", f"{URL}/openapi.yaml", "match"),),
        limitations=("search cut short",),
        source_kind=source_kind,
    )
    return ValidatedContractPackage(
        candidate=candidate,
        root_document=CapturedDocument(f"{URL}/openapi.yaml", root, retrieved_at, "application/yaml"),
        referenced_documents=refs,
        parsed_contract={"openapi": "3.1.0"},
        openapi_version="3.1.0",
        limitations=("operationRef not followed",),
    )


REMOVED_GET = ChangeInput(
    change_type="operation_removed",
    location="GET /users",
    classification=POTENTIALLY_BREAKING,
    summary="GET /users was removed",
    direction=None,
    rationale="Clients calling it will fail.",
    before={"operationId": "listUsers"},
    after=ABSENT,
)
ADDED_POST = ChangeInput(
    change_type="operation_added",
    location="POST /users",
    classification="non_breaking",
    summary="POST /users was added",
    before=ABSENT,
    after={"operationId": "createUser"},
)
DEFAULT_CHANGED = ChangeInput(
    change_type="default_changed",
    location="GET /users > response 200 > body.limit",
    classification="uncertain",
    summary="Default changed from null to 0",
    direction=RESPONSE,
    before=None,
    after=0,
    details={"rule": "defaults"},
)


class CompleteCheckTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name) / ".radar"
        self.store = self.open_store()
        self.source = self.store.add_source(URL)

    def open_store(self):
        store = Store(self.data)
        self.addCleanup(store.close)
        return store

    def capture(self, outcome, package=None, normalized=NORM_V1, before=None, **kwargs):
        check = self.store.begin_check(self.source.id)
        return self.store.complete_check(
            check.id,
            outcome=outcome,
            package=package or make_package(),
            normalized=normalized,
            normalization_version=kwargs.pop("normalization_version", RULES),
            before_snapshot_id=before,
            **kwargs,
        )

    def latest(self):
        return self.store.get_source(self.source.id).latest_snapshot_id

    def bundle_dirs(self):
        return [p for p in (self.data / "contracts").iterdir() if not p.name.startswith(".")]


class InitialCaptureTests(CompleteCheckTestCase):
    """Acceptance: snapshots retain source, capture time, content identity and normalization version."""

    def test_records_snapshot_and_sets_reference(self):
        check = self.capture(INITIAL, content_fingerprint="d" * 64)

        self.assertEqual(check.status, INITIAL)
        self.assertTrue(check.succeeded)
        self.assertIsNone(check.before_snapshot_id)
        self.assertEqual(self.latest(), check.snapshot_id)

        snapshot = self.store.get_snapshot(check.snapshot_id)
        self.assertTrue(is_valid_id(snapshot.id, "snap"))
        self.assertEqual(snapshot.source_id, self.source.id)
        self.assertEqual(snapshot.captured_at, T0)
        self.assertEqual(snapshot.normalization_version, RULES)
        self.assertEqual(snapshot.openapi_version, "3.1.0")
        self.assertEqual(snapshot.content_fingerprint, "d" * 64)
        self.assertEqual(snapshot.media_type, "application/yaml")
        self.assertRegex(snapshot.fingerprint, r"^[0-9a-f]{64}$")

    def test_keeps_provenance_from_discovery(self):
        check = self.capture(INITIAL, package=make_package(source_kind="directory_copy"))
        provenance = self.store.get_snapshot(check.snapshot_id).provenance
        self.assertEqual(provenance["source_url"], f"{URL}/openapi.yaml")
        self.assertEqual(provenance["discovery_method"], "common_location")
        self.assertEqual(provenance["source_kind"], "directory_copy")
        self.assertEqual(provenance["evidence"][0]["criterion"], "server_host")
        self.assertEqual(provenance["evidence"][0]["outcome"], "match")
        self.assertEqual(provenance["candidate_limitations"], ["search cut short"])
        self.assertEqual(provenance["package_limitations"], ["operationRef not followed"])

    def test_original_and_normalized_content_are_retrievable(self):
        """Acceptance: original and normalized contract content can be stored and retrieved."""
        check = self.capture(INITIAL)
        contract = self.store.read_contract(check.snapshot_id)
        self.assertEqual(contract.original, ROOT_V1)
        self.assertIn(USER, contract.documents.values())
        self.assertEqual(contract.normalized, NORM_V1)
        self.assertEqual(contract.normalization_version, RULES)

    def test_records_where_each_document_came_from(self):
        check = self.capture(INITIAL)
        documents = self.store.snapshot_documents(check.snapshot_id)
        self.assertEqual([d.source_url for d in documents], [f"{URL}/openapi.yaml", f"{URL}/schemas/user.yaml"])
        self.assertTrue(documents[0].is_root)
        self.assertFalse(documents[1].is_root)
        self.assertEqual(documents[0].path, "root.yaml")
        self.assertTrue(documents[1].path.startswith("refs/"))
        self.assertEqual({d.retrieved_at for d in documents}, {T0})

    def test_capture_time_is_when_the_last_document_arrived(self):
        later = T0 + timedelta(seconds=4)
        refs = (CapturedDocument(f"{URL}/schemas/user.yaml", USER, later),)
        check = self.capture(INITIAL, package=make_package(refs=refs))
        self.assertEqual(self.store.get_snapshot(check.snapshot_id).captured_at, later)

    def test_initial_capture_cannot_replace_an_existing_reference(self):
        self.capture(INITIAL)
        with self.assertRaises(ConflictError):
            self.capture(INITIAL)


class UnchangedCaptureTests(CompleteCheckTestCase):
    """Acceptance: identical content reuses stored content while preserving each check's history."""

    def test_identical_content_is_stored_once_but_every_check_is_kept(self):
        first = self.capture(INITIAL)
        second = self.capture(UNCHANGED, before=first.snapshot_id)
        third = self.capture(UNCHANGED, before=second.snapshot_id)

        snapshots = self.store.list_snapshots(self.source.id)
        self.assertEqual(len(snapshots), 3)
        self.assertEqual(len({s.fingerprint for s in snapshots}), 1)
        self.assertEqual(len(self.bundle_dirs()), 1)
        self.assertEqual(len(self.store.list_checks(self.source.id)), 3)
        self.assertEqual(self.latest(), third.snapshot_id)
        self.assertEqual(self.store.list_changes(source_id=self.source.id), [])

    def test_content_from_moved_urls_is_still_reused(self):
        first = self.capture(INITIAL)
        moved = make_package(refs=(CapturedDocument("https://cdn.example.com/u.yaml", USER, T0),))
        second = self.capture(UNCHANGED, package=moved, before=first.snapshot_id)
        self.assertEqual(
            self.store.get_snapshot(first.snapshot_id).fingerprint,
            self.store.get_snapshot(second.snapshot_id).fingerprint,
        )
        urls = [d.source_url for d in self.store.snapshot_documents(second.snapshot_id)]
        self.assertIn("https://cdn.example.com/u.yaml", urls)

    def test_refuses_unchanged_when_content_differs(self):
        first = self.capture(INITIAL)
        with self.assertRaisesRegex(ValueError, "differs"):
            self.capture(UNCHANGED, package=make_package(ROOT_V2), normalized=NORM_V2, before=first.snapshot_id)
        self.assertEqual(self.latest(), first.snapshot_id)

    def test_refuses_unchanged_across_normalization_versions(self):
        first = self.capture(INITIAL)
        with self.assertRaisesRegex(ValueError, "normalization versions"):
            self.capture(UNCHANGED, before=first.snapshot_id, normalization_version="canonical-json-2")


class ChangedCaptureTests(CompleteCheckTestCase):
    """Acceptance: both snapshots remain; change records link both and have stable identifiers."""

    def setUp(self):
        super().setUp()
        self.first = self.capture(INITIAL)
        self.second = self.capture(
            CHANGED,
            package=make_package(ROOT_V2, T0 + timedelta(hours=1)),
            normalized=NORM_V2,
            before=self.first.snapshot_id,
            changes=[REMOVED_GET, ADDED_POST, DEFAULT_CHANGED],
            limitations=[{"location": "x-webhooks", "reason": "unsupported"}],
        )

    def test_both_snapshots_remain_available(self):
        self.assertEqual(self.store.read_contract(self.first.snapshot_id).original, ROOT_V1)
        self.assertEqual(self.store.read_contract(self.second.snapshot_id).original, ROOT_V2)
        self.assertEqual(self.latest(), self.second.snapshot_id)
        self.assertEqual(self.second.before_snapshot_id, self.first.snapshot_id)

    def test_changes_link_both_snapshots_with_stable_ids(self):
        changes = self.store.list_changes(check_id=self.second.id)
        self.assertEqual([c.location for c in changes], ["GET /users", "POST /users", DEFAULT_CHANGED.location])
        for change in changes:
            self.assertTrue(is_valid_id(change.id, "chg"))
            self.assertEqual(change.before_snapshot_id, self.first.snapshot_id)
            self.assertEqual(change.after_snapshot_id, self.second.snapshot_id)
            self.assertEqual(change.source_id, self.source.id)
            self.assertEqual(self.store.get_change(change.id), change)
        self.assertEqual(len({c.id for c in changes}), 3)

    def test_change_fields_round_trip(self):
        removed, added, default = self.store.list_changes(check_id=self.second.id)
        self.assertEqual(removed.classification, POTENTIALLY_BREAKING)
        self.assertEqual(removed.rationale, "Clients calling it will fail.")
        self.assertEqual(removed.before_value, {"operationId": "listUsers"})
        self.assertIs(removed.after_value, ABSENT)
        self.assertIs(added.before_value, ABSENT)
        self.assertEqual(default.direction, RESPONSE)
        self.assertEqual(default.details, {"rule": "defaults"})

    def test_absent_and_null_stay_distinct(self):
        _, added, default = self.store.list_changes(check_id=self.second.id)
        self.assertIs(added.before_value, ABSENT)
        self.assertIsNone(default.before_value)
        self.assertIsNot(default.before_value, ABSENT)

    def test_limitations_are_kept_on_the_check(self):
        self.assertEqual(
            self.store.get_check(self.second.id).limitations,
            [{"location": "x-webhooks", "reason": "unsupported"}],
        )

    def test_history_lists_changes_newest_first(self):
        third = self.capture(
            CHANGED, package=make_package(ROOT_V1), normalized=NORM_V1,
            before=self.second.snapshot_id, changes=[ADDED_POST],
        )
        history = self.store.list_changes(source_id=self.source.id)
        self.assertEqual(history[0].check_id, third.id)
        self.assertEqual(len(history), 4)

    def test_changed_with_no_findings_is_allowed(self):
        third = self.capture(CHANGED, package=make_package(ROOT_V1), normalized=NORM_V1, before=self.second.snapshot_id)
        self.assertEqual(third.status, CHANGED)
        self.assertEqual(self.store.list_changes(check_id=third.id), [])

    def test_refuses_changed_when_content_is_identical(self):
        with self.assertRaisesRegex(ValueError, "identical"):
            self.capture(CHANGED, package=make_package(ROOT_V2), normalized=NORM_V2, before=self.second.snapshot_id)


class StaleReferenceTests(CompleteCheckTestCase):
    def test_refuses_a_comparison_against_an_old_reference(self):
        first = self.capture(INITIAL)
        racing = self.store.begin_check(self.source.id)
        second = self.capture(UNCHANGED, before=first.snapshot_id)

        with self.assertRaises(ConflictError):
            self.store.complete_check(
                racing.id, outcome=UNCHANGED, package=make_package(), normalized=NORM_V1,
                normalization_version=RULES, before_snapshot_id=first.snapshot_id,
            )
        self.assertEqual(self.store.get_check(racing.id).status, RUNNING)
        self.assertEqual(self.latest(), second.snapshot_id)

    def test_check_finishes_only_once(self):
        check = self.store.begin_check(self.source.id)
        kwargs = dict(outcome=INITIAL, package=make_package(), normalized=NORM_V1, normalization_version=RULES)
        self.store.complete_check(check.id, **kwargs)
        with self.assertRaises(ConflictError):
            self.store.complete_check(check.id, **kwargs)
        self.assertEqual(len(self.store.list_snapshots(self.source.id)), 1)

    def test_failed_check_cannot_then_complete(self):
        check = self.store.begin_check(self.source.id)
        self.store.fail_check(check.id, stage="fetch", reason="inaccessible", error="down")
        with self.assertRaises(ConflictError):
            self.store.complete_check(
                check.id, outcome=INITIAL, package=make_package(), normalized=NORM_V1, normalization_version=RULES,
            )


class InputValidationTests(CompleteCheckTestCase):
    def assertRefused(self, error, **overrides):
        check = self.store.begin_check(self.source.id, started_at=T0)
        kwargs = dict(outcome=INITIAL, package=make_package(), normalized=NORM_V1, normalization_version=RULES)
        kwargs.update(overrides)
        with self.assertRaises(error):
            self.store.complete_check(check.id, **kwargs)
        self.assertEqual(self.store.get_check(check.id).status, RUNNING)
        self.assertIsNone(self.latest())
        self.assertFalse((self.data / "contracts").exists() and self.bundle_dirs())

    def test_rejects_bad_outcomes(self):
        for outcome in ("failed", "ambiguous", "running", "done"):
            with self.subTest(outcome=outcome):
                self.assertRefused(ValueError, outcome=outcome)

    def test_initial_cannot_name_a_before_snapshot(self):
        self.assertRefused(ValueError, before_snapshot_id=new_id("snap"))

    def test_unchanged_needs_a_before_snapshot(self):
        self.assertRefused(ValueError, outcome=UNCHANGED)

    def test_unknown_before_snapshot(self):
        self.assertRefused(NotFoundError, outcome=UNCHANGED, before_snapshot_id="snap_bogus")

    def test_changes_only_for_changed(self):
        self.assertRefused(ValueError, changes=[ADDED_POST])

    def test_rejects_malformed_changes(self):
        bad = [
            "not a change",
            ChangeInput("t", "l", "breaking", "s"),
            ChangeInput("", "l", POTENTIALLY_BREAKING, "s"),
            ChangeInput("t", "l", POTENTIALLY_BREAKING, "s", direction="sideways"),
            ChangeInput("t", "l", POTENTIALLY_BREAKING, "s", before=object()),
            ChangeInput("t", "l", POTENTIALLY_BREAKING, "s", details=["x"]),
            ChangeInput("t", "bad \ud800", POTENTIALLY_BREAKING, "s"),
        ]
        first = self.capture(INITIAL)
        for change in bad:
            with self.subTest(change=repr(change)[:50]):
                check = self.store.begin_check(self.source.id)
                with self.assertRaises((ValueError, TypeError)):
                    self.store.complete_check(
                        check.id, outcome=CHANGED, package=make_package(ROOT_V2), normalized=NORM_V2,
                        normalization_version=RULES, before_snapshot_id=first.snapshot_id, changes=[change],
                    )
                self.assertEqual(self.store.get_check(check.id).status, RUNNING)
        self.assertEqual(self.latest(), first.snapshot_id)

    def test_rejects_bad_contract_input(self):
        cases = [
            dict(package="not a package"),
            dict(normalized="text"),
            dict(normalization_version=""),
            dict(content_fingerprint=7),
            dict(limitations="not a list"),
            dict(finished_at=T0 - timedelta(seconds=1)),
        ]
        for case in cases:
            with self.subTest(case=list(case)[0]):
                self.assertRefused((ValueError, TypeError), **case)


class InterruptedWriteTests(CompleteCheckTestCase):
    """Acceptance: interrupted writes do not leave a successful record pointing to missing or incomplete content."""

    def test_failure_while_writing_files_records_nothing(self):
        check = self.store.begin_check(self.source.id)
        with patch.object(contracts, "_write_synced", side_effect=OSError("disk full")):
            with self.assertRaises(StoreError):
                self.store.complete_check(
                    check.id, outcome=INITIAL, package=make_package(), normalized=NORM_V1, normalization_version=RULES,
                )
        self.assertEqual(self.store.get_check(check.id).status, RUNNING)
        self.assertEqual(self.store.list_snapshots(self.source.id), [])
        self.assertEqual(self.bundle_dirs(), [])

    def test_failure_after_files_before_records_leaves_no_record(self):
        check = self.store.begin_check(self.source.id)
        # The database refuses the snapshot insert, after the files are written.
        self.store._db.execute(
            "CREATE TEMP TRIGGER crash BEFORE INSERT ON snapshot_documents "
            "BEGIN SELECT RAISE(ABORT, 'simulated crash'); END"
        )
        with self.assertRaises(StoreError):
            self.store.complete_check(
                check.id, outcome=INITIAL, package=make_package(), normalized=NORM_V1, normalization_version=RULES,
            )
        self.store._db.execute("DROP TRIGGER temp.crash")

        self.assertEqual(self.store.get_check(check.id).status, RUNNING)
        self.assertEqual(self.store.list_snapshots(self.source.id), [])
        self.assertIsNone(self.latest())
        # The files exist but nothing refers to them; the retry reuses them.
        self.assertEqual(len(self.bundle_dirs()), 1)
        done = self.store.complete_check(
            check.id, outcome=INITIAL, package=make_package(), normalized=NORM_V1, normalization_version=RULES,
        )
        self.assertEqual(self.store.read_contract(done.snapshot_id).original, ROOT_V1)
        self.assertEqual(len(self.bundle_dirs()), 1)

    def test_every_recorded_snapshot_has_intact_content(self):
        first = self.capture(INITIAL)
        self.capture(CHANGED, package=make_package(ROOT_V2), normalized=NORM_V2, before=first.snapshot_id,
                     changes=[ADDED_POST])
        for snapshot in self.store.list_snapshots(self.source.id):
            with self.subTest(snapshot=snapshot.id):
                self.store.read_contract(snapshot.id)


class FailureAfterSuccessTests(CompleteCheckTestCase):
    """Acceptance: failed checks are recorded without advancing the latest valid snapshot."""

    def test_reference_survives_later_failures(self):
        first = self.capture(INITIAL)
        for stage in ("fetch", "validation", "comparison"):
            check = self.store.begin_check(self.source.id)
            self.store.fail_check(check.id, stage=stage, reason="boom", error="failed")
        self.assertEqual(self.latest(), first.snapshot_id)
        second = self.capture(UNCHANGED, before=first.snapshot_id)
        self.assertEqual(self.latest(), second.snapshot_id)


class RestartTests(CompleteCheckTestCase):
    """Acceptance: stored history remains available after restarting Radar."""

    def test_full_history_survives_reopening(self):
        first = self.capture(INITIAL)
        second = self.capture(CHANGED, package=make_package(ROOT_V2), normalized=NORM_V2,
                              before=first.snapshot_id, changes=[REMOVED_GET, ADDED_POST])
        self.store.close()

        reopened = self.open_store()
        self.assertEqual(reopened.get_source(self.source.id).latest_snapshot_id, second.snapshot_id)
        self.assertEqual(reopened.read_contract(first.snapshot_id).original, ROOT_V1)
        self.assertEqual(reopened.read_contract(second.snapshot_id).normalized, NORM_V2)
        changes = reopened.list_changes(check_id=second.id)
        self.assertEqual([c.summary for c in changes], [REMOVED_GET.summary, ADDED_POST.summary])
        self.assertIs(changes[0].after_value, ABSENT)
        self.assertEqual(len(reopened.list_checks(self.source.id)), 2)


if __name__ == "__main__":
    unittest.main()
