import os
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


if __name__ == "__main__":
    unittest.main()