import contextlib
import io
import os
import tempfile
import unittest
from unittest.mock import patch

from radar.comparison import ChangeKind, Finding

from radar.cli import main as radar

URL = "https://api.example.com/data"


def run(*argv):
    """Run the CLI and return (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = radar.main(list(argv))
        except SystemExit as exit_:
            code = exit_.code
    return code, out.getvalue(), err.getvalue()


class ParsingTests(unittest.TestCase):
    def test_help_exits_zero_and_lists_options(self):
        for argv in (["--help"], ["baseline", "--help"], ["check", "--help"]):
            code, out, _ = run(*argv)
            self.assertEqual(code, 0)
            self.assertIn("usage:", out)
        self.assertIn("--overwrite", run("baseline", "--help")[1])
        self.assertIn("--baseline", run("check", "--help")[1])

    def test_bad_usage_exits_two(self):
        for argv in ([], ["bogus"], ["baseline"], ["check", URL, "--overwrite"]):
            code, _, err = run(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("usage:", err)

    def test_default_baseline_file(self):
        args = radar.build_parser().parse_args(["check", URL])
        self.assertEqual(args.baseline, "baseline.json")


@patch("radar.cli.main.fetch_json")
@patch("radar.cli.main.save_baseline")
@patch("radar.cli.main.extract_structure")
class BaselineCommandWithStubs(unittest.TestCase):
    def test_coordinates_components_and_confirms(self, extract, save, fetch):
        fetch.return_value = {"id": 1}
        extract.return_value = {("id",): "number"}

        code, out, _ = run("baseline", URL)

        self.assertEqual(code, 0)
        fetch.assert_called_once_with(URL)
        extract.assert_called_once_with({"id": 1})
        save.assert_called_once_with(
            "baseline.json", URL, {("id",): "number"}, replace=False
        )
        self.assertIn("Saved baseline", out)
        self.assertIn("baseline.json", out)

    def test_flags_are_passed_through(self, extract, save, fetch):
        extract.return_value = {}
        run("baseline", URL, "--baseline", "other.json", "--overwrite")
        save.assert_called_once_with("other.json", URL, {}, replace=True)

    def test_existing_file_error_suggests_overwrite(self, extract, save, fetch):
        save.side_effect = FileExistsError("exists")
        code, out, err = run("baseline", URL)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--overwrite", err)

    def test_failures_exit_two_with_message(self, extract, save, fetch):
        for error in (RuntimeError("Request timed out."), ValueError("Bad URL.")):
            fetch.side_effect = error
            code, out, err = run("baseline", URL)
            self.assertEqual(code, 2)
            self.assertEqual(out, "")
            self.assertEqual(err, f"error: {error}\n")
        save.assert_not_called()


@patch("radar.cli.main.fetch_json")
@patch("radar.cli.main.load_baseline")
@patch("radar.cli.main.extract_structure")
@patch("radar.cli.main.compare_extracted")
class CheckCommandWithStubs(unittest.TestCase):
    def saved(self):
        from radar.storage.baseline import Baseline
        from datetime import datetime, timezone

        return Baseline(
            URL, datetime(2026, 1, 2, tzinfo=timezone.utc), {("id",): "number"}
        )

    def test_no_changes_exits_zero(self, compare, extract, load, fetch):
        load.return_value = self.saved()
        fetch.return_value = {"id": 2}
        compare.return_value = []

        code, out, _ = run("check", URL)

        self.assertEqual(code, 0)
        load.assert_called_once_with("baseline.json", URL)
        fetch.assert_called_once_with(URL)
        extract.assert_called_once_with({"id": 2})
        compare.assert_called_once_with({("id",): "number"}, extract.return_value)
        self.assertIn("No changes", out)

    def test_findings_exit_one_and_are_listed(self, compare, extract, load, fetch):
        load.return_value = self.saved()
        compare.return_value = [
            Finding(ChangeKind.ADDED, ("user", "email"), None, "string"),
            Finding(ChangeKind.ABSENT, ("id",), "number", None),
            Finding(ChangeKind.TYPE_CHANGED, ("age",), "number", "string"),
        ]

        code, out, _ = run("check", URL)

        self.assertEqual(code, 1)
        self.assertIn("Found 3 changes", out)
        self.assertIn("2026-01-02T00:00:00+00:00", out)
        self.assertIn("+ user.email (added, string)", out)
        self.assertIn("- id (absent, was number)", out)
        self.assertIn("~ age (type changed: number -> string)", out)

    def test_single_finding_is_singular(self, compare, extract, load, fetch):
        load.return_value = self.saved()
        compare.return_value = [Finding(ChangeKind.ABSENT, ("id",), "number", None)]
        self.assertIn("Found 1 change ", run("check", URL)[1])

    def test_missing_baseline_exits_two_before_fetching(self, compare, extract, load, fetch):
        load.side_effect = FileNotFoundError("No baseline found at baseline.json.")
        code, out, err = run("check", URL)
        self.assertEqual(code, 2)
        self.assertIn("No baseline found", err)
        self.assertIn(f"radar baseline {URL}", err)
        fetch.assert_not_called()

    def test_failures_exit_two(self, compare, extract, load, fetch):
        load.return_value = self.saved()
        for error in (RuntimeError("down"), ValueError("not json")):
            fetch.side_effect = error
            code, out, err = run("check", URL)
            self.assertEqual((code, out), (2, ""))
            self.assertEqual(err, f"error: {error}\n")

    def test_baseline_for_another_url_exits_two(self, compare, extract, load, fetch):
        load.side_effect = ValueError("Baseline is for https://other, not x.")
        self.assertEqual(run("check", URL)[0], 2)


@patch("radar.cli.main.fetch_json")
class FullWorkflow(unittest.TestCase):
    """Real engine and real baseline files; only the network is stubbed."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.file = os.path.join(directory.name, "baseline.json")

    def test_baseline_then_check(self, fetch):
        fetch.return_value = {"id": 1, "user": {"name": "Ana"}}
        self.assertEqual(run("baseline", URL, "--baseline", self.file)[0], 0)

        # Same structure, different values: no drift.
        fetch.return_value = {"id": 99, "user": {"name": "Bo"}}
        code, out, _ = run("check", URL, "--baseline", self.file)
        self.assertEqual(code, 0)
        self.assertIn("No changes", out)

        # Field removed, one added, one retyped.
        fetch.return_value = {"id": "99", "user": {"email": "a@b.c"}}
        code, out, _ = run("check", URL, "--baseline", self.file)
        self.assertEqual(code, 1)
        self.assertIn("+ user.email (added, string)", out)
        self.assertIn("- user.name (absent, was string)", out)
        self.assertIn("~ id (type changed: number -> string)", out)

    def test_baseline_refuses_overwrite_without_flag(self, fetch):
        fetch.return_value = {"id": 1}
        run("baseline", URL, "--baseline", self.file)

        fetch.return_value = {"id": 1, "extra": True}
        self.assertEqual(run("baseline", URL, "--baseline", self.file)[0], 2)
        self.assertEqual(run("check", URL, "--baseline", self.file)[0], 1)

        self.assertEqual(
            run("baseline", URL, "--baseline", self.file, "--overwrite")[0], 0
        )
        self.assertEqual(run("check", URL, "--baseline", self.file)[0], 0)

    def test_arrays_are_baselined_and_checked(self, fetch):
        fetch.return_value = {"users": [{"id": 1}], "tags": ["a"]}
        self.assertEqual(run("baseline", URL, "--baseline", self.file)[0], 0)

        # More elements, different values and order: no drift.
        fetch.return_value = {"users": [{"id": 5}, {"id": 6}], "tags": ["b", "c"]}
        self.assertEqual(run("check", URL, "--baseline", self.file)[0], 0)

        fetch.return_value = {"users": [{"id": "1", "email": "a@b.c"}], "tags": [1]}
        code, out, _ = run("check", URL, "--baseline", self.file)
        self.assertEqual(code, 1)
        self.assertIn("~ users[].id (type changed: number -> string)", out)
        self.assertIn("+ users[].email (added, string)", out)
        self.assertIn("~ tags[] (type changed: string -> number)", out)

    def test_empty_array_is_not_reported_as_drift(self, fetch):
        fetch.return_value = {"users": [{"id": 1}]}
        run("baseline", URL, "--baseline", self.file)
        fetch.return_value = {"users": []}
        self.assertEqual(run("check", URL, "--baseline", self.file)[0], 0)

    def test_top_level_array(self, fetch):
        fetch.return_value = [{"id": 1}]
        self.assertEqual(run("baseline", URL, "--baseline", self.file)[0], 0)
        fetch.return_value = [{"id": 2}, {"id": 3}]
        self.assertEqual(run("check", URL, "--baseline", self.file)[0], 0)
        fetch.return_value = [{"id": "x"}]
        code, out, _ = run("check", URL, "--baseline", self.file)
        self.assertEqual(code, 1)
        self.assertIn("~ [].id (type changed: number -> string)", out)

    def test_scalar_response_is_a_failure(self, fetch):
        fetch.return_value = "just text"
        code, _, err = run("baseline", URL, "--baseline", self.file)
        self.assertEqual(code, 2)
        self.assertIn("error:", err)
        self.assertFalse(os.path.exists(self.file))

    def test_check_without_baseline_is_a_failure(self, fetch):
        code, _, err = run("check", URL, "--baseline", self.file)
        self.assertEqual(code, 2)
        self.assertIn("radar baseline", err)
        fetch.assert_not_called()

    def test_default_file_is_baseline_json_in_working_directory(self, fetch):
        fetch.return_value = {"id": 1}
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            os.chdir(directory)
            try:
                self.assertEqual(run("baseline", URL)[0], 0)
                self.assertTrue(os.path.exists("baseline.json"))
                self.assertEqual(run("check", URL)[0], 0)
            finally:
                os.chdir(original)


if __name__ == "__main__":
    unittest.main()
