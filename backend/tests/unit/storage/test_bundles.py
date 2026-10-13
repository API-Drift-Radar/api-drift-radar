import hashlib
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from radar.storage.bundles import DocumentOrigin, plan_bundle
from radar.storage.contracts import ContractFiles

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
ROOT_YAML = b'openapi: 3.1.0\ncomponents:\n  schemas:\n    User:\n      $ref: "schemas/user.yaml"\n'
ROOT_JSON = b'{"openapi": "3.1.0"}'
USER = b"type: object\n"
ERROR = b'{"type": "object"}'


@dataclass(frozen=True)
class Captured:
    """Same attribute names as discovery's CapturedDocument."""

    source_url: str
    content: bytes
    retrieved_at: datetime
    media_type: Optional[str] = None


def ref_path(content, extension):
    return f"refs/{hashlib.sha256(content).hexdigest()[:32]}{extension}"


class PlanTests(unittest.TestCase):
    def test_names_root_and_references(self):
        root = Captured("https://api.example.com/openapi.yaml", ROOT_YAML, NOW, "application/yaml")
        user = Captured("https://api.example.com/schemas/user.yaml", USER, NOW)
        error = Captured("https://api.example.com/schemas/error.json", ERROR, NOW)

        plan = plan_bundle(root, [user, error])

        self.assertEqual(plan.entry_point, "root.yaml")
        self.assertEqual(
            dict(plan.documents),
            {
                "root.yaml": ROOT_YAML,
                ref_path(USER, ".yaml"): USER,
                ref_path(ERROR, ".json"): ERROR,
            },
        )
        self.assertEqual(
            plan.origins,
            (
                DocumentOrigin("root.yaml", root.source_url, NOW, "application/yaml", True),
                DocumentOrigin(ref_path(USER, ".yaml"), user.source_url, NOW, None, False),
                DocumentOrigin(ref_path(ERROR, ".json"), error.source_url, NOW, None, False),
            ),
        )

    def test_root_alone(self):
        plan = plan_bundle(Captured("https://api.example.com/openapi.json", ROOT_JSON, NOW))
        self.assertEqual(plan.entry_point, "root.json")
        self.assertEqual(dict(plan.documents), {"root.json": ROOT_JSON})
        self.assertEqual(len(plan.origins), 1)

    def test_extension_comes_from_content_not_url_or_header(self):
        cases = [
            (b'{"a": 1}', "https://x.example/spec.yaml", "text/yaml", ".json"),
            (b'\xef\xbb\xbf\r\n  [1, 2]', "https://x.example/spec", None, ".json"),
            (b"openapi: 3.0.3", "https://x.example/spec.json", "application/json", ".yaml"),
            (b"", "https://x.example/empty", None, ".yaml"),
        ]
        for content, url, media_type, extension in cases:
            with self.subTest(url=url):
                plan = plan_bundle(Captured(url, content, NOW, media_type))
                self.assertEqual(plan.entry_point, "root" + extension)

    def test_keeps_exact_bytes(self):
        raw = b"\xef\xbb\xbfopenapi: 3.0.0\r\n\x00\xff"
        plan = plan_bundle(Captured("https://x.example/a", bytearray(raw), NOW))
        self.assertEqual(plan.documents[plan.entry_point], raw)
        self.assertIsInstance(plan.documents[plan.entry_point], bytes)

    def test_identical_reference_content_at_two_urls_is_stored_once(self):
        root = Captured("https://x.example/openapi.yaml", ROOT_YAML, NOW)
        first = Captured("https://x.example/a/user.yaml", USER, NOW)
        second = Captured("https://x.example/b/user.yaml", USER, NOW)

        plan = plan_bundle(root, [first, second])

        self.assertEqual(len(plan.documents), 2)
        self.assertEqual(plan.origins[1].path, plan.origins[2].path)
        self.assertEqual(
            {o.source_url for o in plan.origins[1:]}, {first.source_url, second.source_url}
        )

    def test_keeps_each_documents_own_retrieval_time(self):
        later = NOW + timedelta(seconds=3)
        plan = plan_bundle(
            Captured("https://x.example/openapi.yaml", ROOT_YAML, NOW),
            [Captured("https://x.example/user.yaml", USER, later)],
        )
        self.assertEqual([o.retrieved_at for o in plan.origins], [NOW, later])

    def test_rejects_the_same_url_twice(self):
        root = Captured("https://x.example/openapi.yaml", ROOT_YAML, NOW)
        with self.assertRaisesRegex(ValueError, "more than once"):
            plan_bundle(root, [Captured(root.source_url, USER, NOW)])
        user = Captured("https://x.example/user.yaml", USER, NOW)
        with self.assertRaisesRegex(ValueError, "more than once"):
            plan_bundle(root, [user, user])

    def test_rejects_unusable_documents(self):
        cases = [
            (Captured("", ROOT_YAML, NOW), ValueError),
            (Captured("   ", ROOT_YAML, NOW), ValueError),
            (Captured("https://x.example/\nspec", ROOT_YAML, NOW), ValueError),
            (Captured("https://x.example/" + "a" * 9000, ROOT_YAML, NOW), ValueError),
            (Captured("https://x.example/spec", "text", NOW), TypeError),
            (Captured("https://x.example/spec", ROOT_YAML, datetime(2026, 1, 1)), ValueError),
            (Captured("https://x.example/spec", ROOT_YAML, "2026-01-01"), ValueError),
            (Captured("https://x.example/spec", ROOT_YAML, NOW, 7), TypeError),
            (Captured("https://x.example/\ud800", ROOT_YAML, NOW), ValueError),
            (Captured("https://x.example/spec", ROOT_YAML, NOW, "text/\udfff"), ValueError),
            (object(), ValueError),
        ]
        for document, error in cases:
            with self.subTest(document=document), self.assertRaises(error):
                plan_bundle(document)


class StorageCompatibilityTests(unittest.TestCase):
    """Plans must always be storable, and identity must ignore URLs and order."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.files = ContractFiles(Path(directory.name) / "contracts")

    def store(self, plan):
        return self.files.put(plan.entry_point, plan.documents, b"{}", "norm-1")

    def test_plans_are_accepted_by_contract_storage(self):
        plan = plan_bundle(
            Captured("https://x.example/openapi.yaml", ROOT_YAML, NOW),
            [Captured("https://x.example/user.yaml", USER, NOW),
             Captured("https://x.example/error.json", ERROR, NOW)],
        )
        stored = self.files.read(self.store(plan))
        self.assertEqual(dict(stored.documents), dict(plan.documents))
        self.assertEqual(stored.original, ROOT_YAML)

    def test_same_content_from_new_urls_and_order_reuses_the_bundle(self):
        first = plan_bundle(
            Captured("https://x.example/openapi.yaml", ROOT_YAML, NOW),
            [Captured("https://x.example/user.yaml", USER, NOW),
             Captured("https://x.example/error.json", ERROR, NOW)],
        )
        moved = plan_bundle(
            Captured("https://cdn.x.example/v2/spec.yml", ROOT_YAML, NOW + timedelta(days=1)),
            [Captured("https://cdn.x.example/v2/err.json", ERROR, NOW),
             Captured("https://cdn.x.example/v2/u.yaml", USER, NOW)],
        )
        self.assertEqual(self.store(first), self.store(moved))

    def test_changed_content_gives_a_new_bundle(self):
        before = plan_bundle(
            Captured("https://x.example/openapi.yaml", ROOT_YAML, NOW),
            [Captured("https://x.example/user.yaml", USER, NOW)],
        )
        after = plan_bundle(
            Captured("https://x.example/openapi.yaml", ROOT_YAML, NOW),
            [Captured("https://x.example/user.yaml", USER + b"required: [id]\n", NOW)],
        )
        self.assertNotEqual(self.store(before), self.store(after))


if __name__ == "__main__":
    unittest.main()
