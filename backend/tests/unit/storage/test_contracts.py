import errno
import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from radar.storage import contracts
from radar.storage.contracts import (
    ContractFiles,
    build_manifest,
    check_document_path,
    check_document_set,
)
from radar.storage.errors import StoreError

ROOT = b'openapi: 3.1.0\ncomponents:\n  $ref: "schemas/user.yaml"\n'
USER = b"type: object\n"
NORMALIZED = b'{"openapi":"3.1.0"}'
VERSION = "openapi-norm-1"


class ContractFilesTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name) / "contracts"
        self.files = ContractFiles(self.dir)

    def put(self, documents=None, normalized=NORMALIZED, version=VERSION, entry="openapi.yaml"):
        if documents is None:
            documents = {"openapi.yaml": ROOT, "schemas/user.yaml": USER}
        return self.files.put(entry, documents, normalized, version)

    def entries(self):
        return sorted(p.name for p in self.dir.iterdir()) if self.dir.exists() else []


class StoreAndReadTests(ContractFilesTestCase):
    def test_round_trips_original_documents_and_normalized_form(self):
        fingerprint = self.put()
        stored = self.files.read(fingerprint)

        self.assertEqual(stored.fingerprint, fingerprint)
        self.assertEqual(stored.entry_point, "openapi.yaml")
        self.assertEqual(stored.original, ROOT)
        self.assertEqual(
            dict(stored.documents), {"openapi.yaml": ROOT, "schemas/user.yaml": USER}
        )
        self.assertEqual(stored.normalized, NORMALIZED)
        self.assertEqual(stored.normalization_version, VERSION)

    def test_keeps_bytes_exactly(self):
        raw = b"\xef\xbb\xbfopenapi: 3.0.0\r\n\x00\xff"
        fingerprint = self.put(documents={"openapi.yaml": raw})
        self.assertEqual(self.files.read(fingerprint).original, raw)

    def test_lays_out_bundle_on_disk(self):
        fingerprint = self.put()
        base = self.dir / fingerprint
        self.assertEqual((base / "original" / "openapi.yaml").read_bytes(), ROOT)
        self.assertEqual((base / "original" / "schemas" / "user.yaml").read_bytes(), USER)
        self.assertEqual((base / "normalized").read_bytes(), NORMALIZED)
        self.assertTrue((base / "manifest.json").is_file())
        self.assertEqual(self.entries(), [fingerprint])

    def test_survives_a_new_instance(self):
        fingerprint = self.put()
        self.assertEqual(ContractFiles(self.dir).read(fingerprint).original, ROOT)

    def test_fingerprint_is_the_manifest_hash(self):
        fingerprint = self.put()
        expected, _ = build_manifest(
            "openapi.yaml",
            {"openapi.yaml": ROOT, "schemas/user.yaml": USER},
            NORMALIZED,
            VERSION,
        )
        self.assertEqual(fingerprint, expected)
        self.assertRegex(fingerprint, r"^[0-9a-f]{64}$")


class ContentIdentityTests(ContractFilesTestCase):
    def test_identical_content_is_stored_once(self):
        first = self.put()
        second = self.put()
        self.assertEqual(first, second)
        self.assertEqual(self.entries(), [first])

    def test_document_order_does_not_change_identity(self):
        first = self.put(documents={"openapi.yaml": ROOT, "schemas/user.yaml": USER})
        second = self.put(documents={"schemas/user.yaml": USER, "openapi.yaml": ROOT})
        self.assertEqual(first, second)

    def test_any_difference_gives_a_new_bundle(self):
        base = self.put()
        variants = {
            self.put(documents={"openapi.yaml": ROOT + b"\n", "schemas/user.yaml": USER}),
            self.put(normalized=NORMALIZED + b" "),
            self.put(version="openapi-norm-2"),
            self.put(documents={"openapi.yaml": ROOT, "schemas/User.yaml": USER}),
            self.put(entry="schemas/user.yaml"),
        }
        self.assertNotIn(base, variants)
        self.assertEqual(len(variants), 5)
        self.assertEqual(len(self.entries()), 6)

    def test_reuse_does_not_rewrite_existing_files(self):
        fingerprint = self.put()
        manifest = self.dir / fingerprint / "manifest.json"
        before = manifest.stat().st_mtime_ns
        with patch.object(contracts, "_write_synced") as write:
            self.put()
        write.assert_not_called()
        self.assertEqual(manifest.stat().st_mtime_ns, before)


class InterruptedWriteTests(ContractFilesTestCase):
    def test_failure_mid_write_leaves_nothing_behind(self):
        real_write = contracts._write_synced

        def fail_on_manifest(path, data):
            if path.endswith(contracts.MANIFEST_NAME):
                raise OSError("disk full")
            real_write(path, data)

        with patch.object(contracts, "_write_synced", side_effect=fail_on_manifest):
            with self.assertRaises(StoreError):
                self.put()

        self.assertEqual(self.entries(), [])

    def test_failure_on_rename_leaves_nothing_behind(self):
        with patch.object(contracts.os, "rename", side_effect=OSError("interrupted")):
            with self.assertRaises(StoreError):
                self.put()
        self.assertEqual(self.entries(), [])

    def test_leftover_incoming_directory_is_not_content(self):
        # As if a previous process died before its rename.
        self.dir.mkdir()
        (self.dir / ".incoming-dead").mkdir()
        fingerprint = self.put()
        self.assertEqual(self.files.read(fingerprint).original, ROOT)
        self.assertFalse(self.files.exists(fingerprint[::-1]))

    def test_concurrent_writer_of_same_content_wins_cleanly(self):
        other = ContractFiles(self.dir)
        real_rename = os.rename

        def rename_after_competitor(source, target):
            # Another process finishes storing identical content first.
            with patch.object(contracts.os, "rename", real_rename):
                other.put(
                    "openapi.yaml",
                    {"openapi.yaml": ROOT, "schemas/user.yaml": USER},
                    NORMALIZED,
                    VERSION,
                )
            raise FileExistsError(target)

        with patch.object(contracts.os, "rename", side_effect=rename_after_competitor):
            fingerprint = self.put()

        self.assertEqual(self.entries(), [fingerprint])
        self.assertEqual(self.files.read(fingerprint).original, ROOT)


class IntegrityTests(ContractFilesTestCase):
    def test_detects_a_changed_document(self):
        fingerprint = self.put()
        (self.dir / fingerprint / "original" / "schemas" / "user.yaml").write_bytes(b"x")
        with self.assertRaisesRegex(StoreError, "corrupted"):
            self.files.read(fingerprint)

    def test_detects_a_changed_normalized_form(self):
        fingerprint = self.put()
        (self.dir / fingerprint / "normalized").write_bytes(b"{}")
        with self.assertRaisesRegex(StoreError, "corrupted"):
            self.files.read(fingerprint)

    def test_detects_a_changed_manifest(self):
        fingerprint = self.put()
        manifest = self.dir / fingerprint / "manifest.json"
        manifest.write_bytes(manifest.read_bytes().replace(b"norm-1", b"norm-9"))
        with self.assertRaisesRegex(StoreError, "manifest"):
            self.files.read(fingerprint)

    def test_detects_a_missing_file(self):
        fingerprint = self.put()
        (self.dir / fingerprint / "normalized").unlink()
        with self.assertRaisesRegex(StoreError, "missing"):
            self.files.read(fingerprint)

    def test_reports_missing_bundle(self):
        with self.assertRaisesRegex(StoreError, "missing"):
            self.files.read("0" * 64)

    def test_refuses_to_reuse_corrupted_content(self):
        fingerprint = self.put()
        (self.dir / fingerprint / "normalized").write_bytes(b"{}")
        with self.assertRaises(StoreError):
            self.put()

    def plant_bundle(self, manifest_bytes, files=()):
        """Create a bundle by hand whose name matches its manifest, as an
        attacker with write access to the data folder could."""
        fingerprint = hashlib.sha256(manifest_bytes).hexdigest()
        base = self.dir / fingerprint
        base.mkdir(parents=True)
        (base / "manifest.json").write_bytes(manifest_bytes)
        for relative, data in files:
            target = base / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return fingerprint

    def test_rejects_manifest_pointing_outside_its_bundle(self):
        secret = self.dir / "secret.txt"
        self.dir.mkdir(parents=True, exist_ok=True)
        secret.write_bytes(b"do not read")
        digest = hashlib.sha256(b"do not read").hexdigest()
        manifest = {
            "format": 1, "entry_point": "../../secret.txt", "normalization_version": "n",
            "documents": [{"path": "../../secret.txt", "sha256": digest, "size": 11}],
            "normalized": {"sha256": digest, "size": 11},
        }
        fingerprint = self.plant_bundle(json.dumps(manifest).encode())
        with self.assertRaisesRegex(StoreError, "invalid manifest"):
            self.files.read(fingerprint)

    def test_rejects_malformed_manifests(self):
        good_digest = "0" * 64
        cases = {
            "not json": b"\xff\xfe not json",
            "not an object": b"[1, 2]",
            "no documents": json.dumps({"format": 1, "documents": [], "normalized": {"sha256": good_digest}}).encode(),
            "bad digest": json.dumps({
                "format": 1, "entry_point": "a.yaml", "normalization_version": "n",
                "documents": [{"path": "a.yaml", "sha256": "xyz"}], "normalized": {"sha256": good_digest},
            }).encode(),
            "missing entry point": json.dumps({
                "format": 1, "entry_point": "b.yaml", "normalization_version": "n",
                "documents": [{"path": "a.yaml", "sha256": good_digest}], "normalized": {"sha256": good_digest},
            }).encode(),
        }
        for label, raw in cases.items():
            with self.subTest(case=label):
                fingerprint = self.plant_bundle(raw)
                with self.assertRaises(StoreError):
                    self.files.read(fingerprint)

    def test_read_opens_each_file_once(self):
        fingerprint = self.put()
        real_read = contracts._read
        opened = []

        def counting_read(path, fp):
            opened.append(os.path.basename(path))
            return real_read(path, fp)

        with patch.object(contracts, "_read", side_effect=counting_read):
            self.files.read(fingerprint)
        self.assertEqual(sorted(opened), sorted(["manifest.json", "openapi.yaml", "user.yaml", "normalized"]))

    def test_rejects_malformed_fingerprints(self):
        for bad in ("", "../etc", "A" * 64, "0" * 63, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.files.read(bad)


class InputValidationTests(ContractFilesTestCase):
    def test_rejects_unsafe_paths(self):
        for bad in (
            "", "/etc/passwd", "../x.yaml", "a/../b", "a//b", "./a", "a\\b",
            "C:x", "a/b.", "a /b", "a\x00b", "a?b", 7,
        ):
            with self.subTest(path=bad), self.assertRaises(ValueError):
                check_document_path(bad)

    def test_accepts_ordinary_paths(self):
        for good in ("openapi.yaml", "specs/v1/openapi.json", "a b/c-d_e.yaml", ".well-known/x"):
            with self.subTest(path=good):
                check_document_path(good)

    def test_rejects_windows_device_names(self):
        for bad in (
            "con.yaml", "CON", "refs/nul", "aux.tar.gz", "COM1.json", "lpt9",
            "Prn.yaml", "CON .yaml", "conin$", "refs/com¹.yaml",
        ):
            with self.subTest(path=bad), self.assertRaises(ValueError):
                check_document_path(bad)

    def test_accepts_names_that_only_resemble_devices(self):
        for good in ("console.yaml", "com10.json", "nul_spec.yaml", "refs/conx", "auxiliary"):
            with self.subTest(path=good):
                check_document_path(good)

    def test_rejects_paths_that_collide(self):
        with self.assertRaisesRegex(ValueError, "case"):
            check_document_set(["a.yaml", "A.yaml"])
        with self.assertRaisesRegex(ValueError, "directory"):
            check_document_set(["schemas", "schemas/user.yaml"])

    def test_rejects_bad_bundles_before_writing(self):
        cases = [
            dict(documents={}),
            dict(documents={"openapi.yaml": "text"}),
            dict(entry="missing.yaml"),
            dict(normalized="text"),
            dict(version=""),
            dict(documents={"../escape.yaml": ROOT}, entry="../escape.yaml"),
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises((ValueError, TypeError)):
                self.put(**case)
        self.assertEqual(self.entries(), [])


class RemoveAbandonedTests(ContractFilesTestCase):
    """Leftovers of crashed writes are deleted once no live writer can own them."""

    HOUR = contracts.ABANDONED_AFTER_SECONDS

    def leftover(self, name=None, age=2 * 60 * 60, files=("original/openapi.yaml",)):
        """Create a half-written incoming directory last modified ``age`` seconds ago."""
        path = self.dir / (name or contracts.INCOMING_PREFIX + os.urandom(16).hex())
        for relative in files:
            target = path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"partial")
        path.mkdir(parents=True, exist_ok=True)
        stamp = os.stat(path).st_mtime - age
        os.utime(path, (stamp, stamp))
        return path

    def test_removes_old_leftovers_and_their_contents(self):
        self.dir.mkdir()
        old = [self.leftover(), self.leftover(files=())]
        self.assertEqual(self.files.remove_abandoned(), 2)
        for path in old:
            self.assertFalse(path.exists())

    def test_keeps_a_write_that_may_still_be_in_progress(self):
        self.dir.mkdir()
        recent = self.leftover(age=60)
        self.assertEqual(self.files.remove_abandoned(), 0)
        self.assertTrue(recent.exists())

    def test_age_is_measured_against_the_threshold(self):
        self.dir.mkdir()
        path = self.leftover(age=0)
        mtime = os.stat(path).st_mtime
        self.assertEqual(self.files.remove_abandoned(now=mtime + self.HOUR - 1), 0)
        self.assertEqual(self.files.remove_abandoned(now=mtime + self.HOUR + 1), 1)

    def test_a_directory_modified_in_the_future_is_kept(self):
        self.dir.mkdir()
        path = self.leftover(age=-self.HOUR)  # e.g. the clock was set back
        self.assertEqual(self.files.remove_abandoned(), 0)
        self.assertTrue(path.exists())

    def test_never_touches_stored_bundles(self):
        fingerprint = self.put()
        bundle = self.dir / fingerprint
        stamp = os.stat(bundle).st_mtime - 10 * self.HOUR
        os.utime(bundle, (stamp, stamp))
        self.leftover()
        self.files.remove_abandoned()
        self.assertEqual(self.entries(), [fingerprint])
        self.files.verify(fingerprint)

    def test_only_removes_names_put_would_create(self):
        self.dir.mkdir()
        others = [
            self.leftover(name=".incoming-notes"),
            self.leftover(name=".incoming-" + "A" * 32),
            self.leftover(name=".incoming-" + "a" * 32 + "x"),
            self.leftover(name="incoming-" + "a" * 32),
        ]
        stray_file = self.dir / (contracts.INCOMING_PREFIX + "b" * 32)
        stray_file.write_bytes(b"not a directory")
        os.utime(stray_file, (0, 0))
        self.assertEqual(self.files.remove_abandoned(), 0)
        for path in others + [stray_file]:
            self.assertTrue(path.exists(), path.name)

    @unittest.skipUnless(hasattr(os, "symlink"), "needs symbolic links")
    def test_does_not_follow_a_symbolic_link(self):
        self.dir.mkdir()
        outside = self.dir.parent / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_bytes(b"precious")
        link = self.dir / (contracts.INCOMING_PREFIX + "c" * 32)
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links not permitted here")
        self.assertEqual(self.files.remove_abandoned(now=10 ** 12), 0)
        self.assertEqual((outside / "keep.txt").read_bytes(), b"precious")

    def test_missing_directory_is_not_an_error(self):
        self.assertEqual(self.files.remove_abandoned(), 0)
        self.assertFalse(self.dir.exists())

    def test_a_leftover_that_cannot_be_removed_is_skipped_quietly(self):
        self.dir.mkdir()
        stuck, removable = self.leftover(), self.leftover()
        real_rmtree = contracts.shutil.rmtree

        def rmtree(path, ignore_errors=False):
            if os.path.basename(path) != stuck.name:
                real_rmtree(path, ignore_errors=ignore_errors)

        with patch.object(contracts.shutil, "rmtree", side_effect=rmtree):
            self.assertEqual(self.files.remove_abandoned(), 1)
        self.assertTrue(stuck.exists())
        self.assertFalse(removable.exists())


def failing_directory_sync(code, only=None):
    """An os.fsync that fails with ``code`` for directories (or only for ``only``)."""
    real_fsync = os.fsync

    def fsync(descriptor):
        info = os.fstat(descriptor)
        if stat.S_ISDIR(info.st_mode) and (
            only is None or (info.st_dev, info.st_ino) == only
        ):
            raise OSError(code, os.strerror(code))
        return real_fsync(descriptor)

    return fsync


@unittest.skipIf(os.name == "nt", "Windows cannot sync directories; storage skips it there")
class DirectorySyncTests(ContractFilesTestCase):
    """Regression tests for PR #18 review: directory syncs must not fail silently."""

    def test_a_failed_directory_sync_is_an_error(self):
        with patch.object(contracts.os, "fsync", failing_directory_sync(errno.EIO)):
            with self.assertRaises(StoreError):
                self.put()
        self.assertFalse([name for name in self.entries() if not name.startswith(".")])

    def test_a_failed_sync_after_the_rename_is_an_error_and_a_retry_succeeds(self):
        self.dir.mkdir()
        info = os.stat(self.dir)
        with patch.object(
            contracts.os, "fsync",
            failing_directory_sync(errno.EIO, only=(info.st_dev, info.st_ino)),
        ):
            with self.assertRaises(StoreError):
                self.put()
        # The bundle is complete in place, but was not reported as saved.
        # The next attempt reuses it and syncs its name before returning.
        fingerprint = self.put()
        self.assertEqual(self.files.read(fingerprint).original, ROOT)

    def test_a_file_system_without_directory_sync_is_tolerated(self):
        for code in sorted(contracts._SYNC_NOT_SUPPORTED):
            with self.subTest(errno=errno.errorcode.get(code, code)):
                with patch.object(contracts.os, "fsync", failing_directory_sync(code)):
                    fingerprint = self.put(normalized=NORMALIZED + str(code).encode())
                self.assertTrue(self.files.exists(fingerprint))

    def record_syncs(self):
        synced = []
        real = contracts._sync_directory

        def record(directory):
            synced.append(os.path.abspath(directory))
            real(directory)

        patcher = patch.object(contracts, "_sync_directory", side_effect=record)
        patcher.start()
        self.addCleanup(patcher.stop)
        return synced

    def test_new_directories_are_synced_in_their_parents(self):
        data = self.dir.parent / "new-data"
        files = ContractFiles(data / "contracts")
        synced = self.record_syncs()
        files.put("openapi.yaml", {"openapi.yaml": ROOT}, NORMALIZED, VERSION)
        self.assertIn(os.path.abspath(self.dir.parent), synced)  # holds new-data/
        self.assertIn(os.path.abspath(data), synced)             # holds contracts/

    def test_the_directory_is_synced_in_its_parent_on_every_new_bundle(self):
        self.put()
        synced = self.record_syncs()
        self.put(normalized=b"{}")
        self.assertIn(os.path.abspath(self.dir.parent), synced)

    def test_a_retry_completes_a_parent_sync_that_failed_after_creation(self):
        """Regression (PR #18 review): creation, then parent-sync failure, then retry."""
        self.dir.parent.mkdir(parents=True, exist_ok=True)
        parent = os.stat(self.dir.parent)
        with patch.object(
            contracts.os, "fsync",
            failing_directory_sync(errno.EIO, only=(parent.st_dev, parent.st_ino)),
        ):
            with self.assertRaisesRegex(StoreError, "Could not create"):
                self.put()
        self.assertTrue(self.dir.is_dir())  # created, but its entry was never synced
        self.assertEqual(self.entries(), [])

        synced = self.record_syncs()
        fingerprint = self.put()  # the retry: the directory already exists
        self.assertIn(os.path.abspath(self.dir.parent), synced)
        # The parent is synced before anything is written into the directory.
        self.assertEqual(synced[0], os.path.abspath(self.dir.parent))
        self.assertEqual(self.files.read(fingerprint).original, ROOT)

    def test_a_retry_after_a_restart_also_completes_the_parent_sync(self):
        self.dir.mkdir(parents=True)  # left behind by a process that crashed before syncing
        synced = self.record_syncs()
        ContractFiles(self.dir).put("openapi.yaml", {"openapi.yaml": ROOT}, NORMALIZED, VERSION)
        self.assertIn(os.path.abspath(self.dir.parent), synced)

    def test_a_reused_bundle_does_not_resync_the_parent(self):
        self.put()
        synced = self.record_syncs()
        self.put()
        self.assertNotIn(os.path.abspath(self.dir.parent), synced)

    def test_a_reused_bundle_is_synced_before_it_is_returned(self):
        self.put()
        synced = self.record_syncs()
        self.put()
        self.assertEqual(synced, [os.path.abspath(self.dir)])

    def test_a_directory_that_cannot_be_created_durably_is_an_error(self):
        files = ContractFiles(self.dir.parent / "fresh" / "contracts")
        with patch.object(contracts.os, "fsync", failing_directory_sync(errno.EIO)):
            with self.assertRaisesRegex(StoreError, "Could not create"):
                files.put("openapi.yaml", {"openapi.yaml": ROOT}, NORMALIZED, VERSION)


if __name__ == "__main__":
    unittest.main()