import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from drift_engine import extract_structure

from baseline import baseline_path, load_baseline, save_baseline

URL = "https://api.example.com/users"
STRUCTURE = {("id",): "number", ("user",): "object", ("user", "name"): "string"}


class BaselineTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.path = self.dir / "users.json"

    def write_document(self, **overrides):
        document = {
            "version": 1,
            "url": URL,
            "captured_at": "2026-10-06T12:00:00+00:00",
            "structure": [{"path": ["id"], "type": "number"}],
        }
        document.update(overrides)
        self.path.write_text(json.dumps(document))

    def leftover_files(self):
        return [p.name for p in self.dir.iterdir() if p != self.path]


class SaveTests(BaselineTestCase):
    def test_stores_url_time_and_structure(self):
        before = datetime.now(timezone.utc).replace(microsecond=0)
        save_baseline(self.path, URL, STRUCTURE)
        document = json.loads(self.path.read_text())

        self.assertEqual(document["url"], URL)
        self.assertEqual(document["version"], 1)
        captured_at = datetime.fromisoformat(document["captured_at"])
        self.assertTrue(before <= captured_at <= datetime.now(timezone.utc))
        self.assertEqual(
            document["structure"],
            [
                {"path": ["id"], "type": "number"},
                {"path": ["user"], "type": "object"},
                {"path": ["user", "name"], "type": "string"},
            ],
        )

    def test_stores_no_response_values(self):
        data = {"name": "Alice", "email": "alice@example.com", "age": 31337}
        save_baseline(self.path, URL, extract_structure(data))
        text = self.path.read_text()
        for value in ("Alice", "alice@example.com", "31337"):
            self.assertNotIn(value, text)

    def test_rejects_value_like_structures(self):
        for structure in (
            [],
            {"id": "number"},
            {(): "number"},
            {("id",): "Alice"},
            {("id",): ["number"]},
            {(1,): "number"},
        ):
            with self.subTest(structure=structure), self.assertRaises(ValueError):
                save_baseline(self.path, URL, structure)
        self.assertFalse(self.path.exists())

    def test_rejects_invalid_url(self):
        for url in ("", "   ", None, 5):
            with self.subTest(url=url), self.assertRaises(ValueError):
                save_baseline(self.path, url, STRUCTURE)
        self.assertFalse(self.path.exists())

    def test_empty_structure_is_allowed(self):
        save_baseline(self.path, URL, {})
        self.assertEqual(load_baseline(self.path, URL).structure, {})

    def test_accepts_string_paths(self):
        save_baseline(str(self.path), URL, STRUCTURE)
        self.assertTrue(self.path.exists())


class ReplacementTests(BaselineTestCase):
    def test_refuses_replacement_by_default(self):
        save_baseline(self.path, URL, STRUCTURE)
        original = self.path.read_bytes()

        with self.assertRaises(FileExistsError):
            save_baseline(self.path, URL, {("other",): "string"})

        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.leftover_files(), [])

    def test_replace_true_overwrites(self):
        save_baseline(self.path, URL, STRUCTURE)
        save_baseline(self.path, URL, {("other",): "string"}, replace=True)
        self.assertEqual(load_baseline(self.path, URL).structure, {("other",): "string"})

    def test_replace_true_creates_missing_file(self):
        save_baseline(self.path, URL, STRUCTURE, replace=True)
        self.assertEqual(load_baseline(self.path, URL).structure, STRUCTURE)


class FailedSaveTests(BaselineTestCase):
    def test_failed_replace_preserves_existing_baseline(self):
        save_baseline(self.path, URL, STRUCTURE)
        original = self.path.read_bytes()

        with patch("atomic_write.os.replace", side_effect=OSError("denied")):
            with self.assertRaisesRegex(RuntimeError, "Could not write"):
                save_baseline(self.path, URL, {("other",): "string"}, replace=True)

        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.leftover_files(), [])
        self.assertEqual(load_baseline(self.path, URL).structure, STRUCTURE)

    def test_invalid_structure_preserves_existing_baseline(self):
        save_baseline(self.path, URL, STRUCTURE)
        original = self.path.read_bytes()

        with self.assertRaises(ValueError):
            save_baseline(self.path, URL, {("id",): "Alice"}, replace=True)

        self.assertEqual(self.path.read_bytes(), original)


class LoadTests(BaselineTestCase):
    def test_round_trip(self):
        save_baseline(self.path, URL, STRUCTURE)
        baseline = load_baseline(self.path, URL)

        self.assertEqual(baseline.url, URL)
        self.assertEqual(baseline.structure, STRUCTURE)
        self.assertEqual(baseline.captured_at.utcoffset(), timedelta(0))

    def test_round_trip_with_dotted_keys(self):
        structure = {("a.b",): "number", ("a",): "object", ("a", "b"): "string"}
        save_baseline(self.path, URL, structure)
        self.assertEqual(load_baseline(self.path, URL).structure, structure)

    def test_loaded_structure_matches_a_fresh_extraction(self):
        data = {"id": 1, "user": {"name": "Ana", "email": None}}
        save_baseline(self.path, URL, extract_structure(data))
        baseline = load_baseline(self.path, URL)
        self.assertEqual(baseline.structure, extract_structure(data))

    def test_url_mismatch(self):
        save_baseline(self.path, URL, STRUCTURE)
        with self.assertRaisesRegex(ValueError, "not https://api.example.com/orders"):
            load_baseline(self.path, "https://api.example.com/orders")

    def test_invalid_expected_url(self):
        save_baseline(self.path, URL, STRUCTURE)
        for url in ("", None):
            with self.subTest(url=url), self.assertRaises(ValueError):
                load_baseline(self.path, url)

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            load_baseline(self.path, URL)

    def test_directory_is_not_a_baseline(self):
        with self.assertRaises(RuntimeError):
            load_baseline(self.dir, URL)

    def test_not_json(self):
        self.path.write_text("{ not json")
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            load_baseline(self.path, URL)

    def test_not_utf8(self):
        self.path.write_bytes(b"\xff\xfe\x00")
        with self.assertRaises(RuntimeError):
            load_baseline(self.path, URL)

    def test_empty_file(self):
        self.path.write_text("")
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            load_baseline(self.path, URL)

    def test_top_level_must_be_an_object(self):
        for text in ("[]", '"x"', "null", "3"):
            self.path.write_text(text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                load_baseline(self.path, URL)

    def test_valid_document_loads(self):
        self.write_document()
        self.assertEqual(load_baseline(self.path, URL).structure, {("id",): "number"})

    def test_invalid_contents(self):
        cases = {
            "wrong version": {"version": 2},
            "missing version": {"version": None},
            "url not a string": {"url": 5},
            "missing timestamp": {"captured_at": None},
            "bad timestamp": {"captured_at": "yesterday"},
            "naive timestamp": {"captured_at": "2026-10-06T12:00:00"},
            "structure not a list": {"structure": {"id": "number"}},
            "entry not an object": {"structure": ["id"]},
            "path not a list": {"structure": [{"path": "id", "type": "number"}]},
            "empty path": {"structure": [{"path": [], "type": "number"}]},
            "non-string path part": {"structure": [{"path": [1], "type": "number"}]},
            "unknown type": {"structure": [{"path": ["id"], "type": "Alice"}]},
            "unhashable type": {"structure": [{"path": ["id"], "type": ["number"]}]},
            "duplicate path": {
                "structure": [
                    {"path": ["id"], "type": "number"},
                    {"path": ["id"], "type": "string"},
                ]
            },
        }
        for name, overrides in cases.items():
            self.write_document(**overrides)
            with self.subTest(name), self.assertRaises(ValueError):
                load_baseline(self.path, URL)

    def test_missing_keys(self):
        for key in ("version", "url", "captured_at", "structure"):
            self.write_document()
            document = json.loads(self.path.read_text())
            del document[key]
            self.path.write_text(json.dumps(document))
            with self.subTest(key=key), self.assertRaises(ValueError):
                load_baseline(self.path, URL)


class BaselinePathTests(BaselineTestCase):
    def test_same_url_same_path(self):
        self.assertEqual(baseline_path(URL, self.dir), baseline_path(URL, self.dir))

    def test_different_urls_get_different_paths(self):
        urls = [URL, URL + "/", URL + "?page=2", "http://api.example.com/users",
                "https://api.example.com/orders", "https://other.example.com/users"]
        paths = {baseline_path(url, self.dir) for url in urls}
        self.assertEqual(len(paths), len(urls))

    def test_name_is_readable_and_safe(self):
        name = Path(baseline_path("https://API.Example.com:8443/a/b", self.dir)).name
        self.assertRegex(name, r"^api\.example\.com-[0-9a-f]{12}\.json$")

    def test_hostile_urls_stay_inside_the_directory(self):
        for url in ("https://../../etc/passwd", "../../x", "https://[::1]/a",
                    "///", "https://exa mple.com/../..", "\u202e", "x" * 5000):
            with self.subTest(url=url):
                path = Path(baseline_path(url, self.dir))
                self.assertEqual(path.parent, self.dir)
                self.assertRegex(path.name, r"^[a-z0-9.-]+-[0-9a-f]{12}\.json$")
                self.assertLessEqual(len(path.name), 80)

    def test_default_directory(self):
        path = Path(baseline_path(URL))
        self.assertEqual(path.parent, Path(".radar") / "baselines")

    def test_invalid_url(self):
        for url in ("", "  ", None):
            with self.subTest(url=url), self.assertRaises(ValueError):
                baseline_path(url)

    def test_one_baseline_per_url(self):
        orders = "https://api.example.com/orders"
        save_baseline(baseline_path(URL, self.dir), URL, STRUCTURE)
        save_baseline(baseline_path(orders, self.dir), orders, {("id",): "string"})

        self.assertEqual(load_baseline(baseline_path(URL, self.dir), URL).structure, STRUCTURE)
        self.assertEqual(
            load_baseline(baseline_path(orders, self.dir), orders).structure,
            {("id",): "string"},
        )


class DirectoryCreationTests(BaselineTestCase):
    def test_creates_missing_directory(self):
        path = self.dir / ".radar" / "baselines" / "users.json"
        save_baseline(path, URL, STRUCTURE)
        self.assertEqual(load_baseline(path, URL).structure, STRUCTURE)

    def test_directory_is_owner_only(self):
        if os.name == "nt":
            self.skipTest("POSIX permissions")
        path = self.dir / "store" / "users.json"
        save_baseline(path, URL, STRUCTURE)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_directory_blocked_by_a_file(self):
        (self.dir / "store").write_text("not a directory")
        with self.assertRaisesRegex(RuntimeError, "baseline directory"):
            save_baseline(self.dir / "store" / "users.json", URL, STRUCTURE)


if __name__ == "__main__":
    unittest.main()
