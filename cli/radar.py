"""Command-line entry point: ``radar baseline URL`` and ``radar check URL``.

This module only parses commands, coordinates the other components, and
prints results. Fetching, extraction, comparison, and file storage live in
``fetch``, ``drift_engine``, and ``baseline``.
"""

import argparse
import sys

from drift_engine import ChangeKind, compare_extracted, extract_structure

from baseline import load_baseline, save_baseline
from fetch import fetch_json

DEFAULT_BASELINE_FILE = "baseline.json"

EXIT_OK = 0
EXIT_DIFFERENCES = 1
EXIT_FAILURE = 2

def build_parser():
    parser = argparse.ArgumentParser(
        prog="radar",
        description="Detect structural drift in a JSON API response.",
        epilog="Exit codes: 0 no differences, 1 differences found, 2 failure.",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    baseline = commands.add_parser(
        "baseline", help="save the current response structure as the baseline"
    )
    baseline.add_argument("url", help="HTTP or HTTPS URL that returns a JSON object")
    baseline.add_argument(
        "--overwrite",
        action="store_true",
        help="replace the baseline file if it already exists",
    )

    check = commands.add_parser(
        "check", help="compare the current response structure with the baseline"
    )
    check.add_argument("url", help="HTTP or HTTPS URL that returns a JSON object")

    for command in (baseline, check):
        command.add_argument(
            "--baseline",
            default=DEFAULT_BASELINE_FILE,
            metavar="FILE",
            help=f"baseline file to use (default: {DEFAULT_BASELINE_FILE})",
        )
    return parser


def main(argv=None):
    """Run the CLI and return the exit code."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "baseline":
            return run_baseline(args)
        return run_check(args)
    except FileNotFoundError as error:
        return _fail(f"{error} Run 'radar baseline {args.url}' first.")
    except FileExistsError:
        return _fail(
            f"{args.baseline} already exists. Use --overwrite to replace it, "
            "or --baseline FILE to choose another file."
        )
    except (ValueError, RuntimeError) as error:
        return _fail(error)


def run_baseline(args):
    structure = extract_structure(fetch_json(args.url))
    save_baseline(args.baseline, args.url, structure, replace=args.overwrite)
    print(f"Saved baseline for {args.url} to {args.baseline} ({len(structure)} fields).")
    return EXIT_OK


def run_check(args):
    # Load first so a missing or mismatched baseline fails before any request.
    saved = load_baseline(args.baseline, args.url)
    findings = compare_extracted(
        saved.structure, extract_structure(fetch_json(args.url))
    )

    if not findings:
        print(f"No changes: {args.url} matches {args.baseline}.")
        return EXIT_OK

    count = len(findings)
    print(
        f"Found {count} {'change' if count == 1 else 'changes'} in {args.url} "
        f"since the baseline was captured at {saved.captured_at.isoformat()}:"
    )
    for finding in findings:
        print(f"  {_describe(finding)}")
    return EXIT_DIFFERENCES


def _format_path(path):
    """Join a path with dots, writing array elements as ``users[]``."""
    return ".".join(path).replace(".[]", "[]")


def _describe(finding):
    field = _format_path(finding.path)
    if finding.kind is ChangeKind.ADDED:
        return f"+ {field} (added, {finding.new_type})"
    if finding.kind is ChangeKind.ABSENT:
        return f"- {field} (absent, was {finding.old_type})"
    return f"~ {field} (type changed: {finding.old_type} -> {finding.new_type})"


def _fail(message):
    print(f"error: {message}", file=sys.stderr)
    return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
