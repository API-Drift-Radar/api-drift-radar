"""Compare two OpenAPI 3.x files and list what changed, for a client of the earlier one.

    python -m radar.comparison.openapi before.yaml after.yaml
    python -m radar.comparison.openapi before.json after.json --json

Both files are read from disk and treated as single-file contracts from the provider: a `$ref` to another file is
reported as a limitation, not followed. Nothing is fetched. Exit status: 0 no findings, 1 findings were produced,
2 unusable input. No findings does not mean the contracts are the same: read the coverage and limitations.
"""

import argparse
import json
from pathlib import Path
import sys

from radar.comparison.openapi.compare import compare_contracts
from radar.comparison.openapi.evaluation import build_package
from radar.comparison.openapi.serialization import group_findings, result_to_dict
from radar.discovery.validation import validate_document
from radar.domain.comparison import CATEGORIES


def _load(path: Path, side: str):
    try:
        content = path.read_bytes()
    except OSError as error:
        raise SystemExit(f"Cannot read the {side} file {path}: {error.strerror or error}") from None
    checked = validate_document(content)
    if not checked.ok:
        raise SystemExit(f"The {side} file {path} is not a supported OpenAPI 3.x document: "
                         f"{checked.rejection.code}: {checked.rejection.reason}")
    return build_package(json.loads(json.dumps(checked.document)), url=path.resolve().as_uri())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m radar.comparison.openapi", description=__doc__.split("\n")[0])
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    parser.add_argument("--limit", type=int, default=40, help="how many distinct changes to print (default 40)")
    args = parser.parse_args(argv)
    try:
        before, after = _load(args.before, "before"), _load(args.after, "after")
    except SystemExit as error:
        print(error, file=sys.stderr)
        return 2
    result = compare_contracts(before, after)
    if args.json:
        print(json.dumps(result_to_dict(result), indent=2))
        return 1 if result.findings else 0
    groups = group_findings(result.findings)
    if not result.findings:
        print("No findings in the parts of the contract that were compared.")
    else:
        counts = {category: sum(1 for g in groups if g["category"] == category) for category in CATEGORIES}
        print(f"{len(result.findings)} finding(s), {len(groups)} distinct change(s): "
              + ", ".join(f"{n} {c}" for c, n in counts.items() if n))
    for group in groups[:args.limit]:
        where = ", ".join(group["operations"][:3]) + (f" and {group['operation_count'] - 3} more" if group["operation_count"] > 3 else "")
        times = f" (in {group['operation_count']} operations: {where})" if group["operation_count"] > 1 else (f" ({where})" if where else "")
        print(f"  [{group['category']}] {group['rule_id']}{times}\n      {group['subject'] or ''}: {group['explanation']}")
    if len(groups) > args.limit:
        print(f"  ... and {len(groups) - args.limit} more distinct changes (use --json for all, or --limit).")
    print(f"\nCompared: {', '.join(result.coverage.assessed)}.\nNot compared: {', '.join(result.coverage.not_assessed)}.")
    by_code: dict = {}
    for limitation in result.limitations:
        if limitation.code != "scope_not_assessed":
            by_code.setdefault(limitation.code, []).append(limitation)
    for code, items in by_code.items():  # the same limitation can apply in hundreds of places: say it once, with a count
        more = f" (and {len(items) - 1} more like it; use --json for all)" if len(items) > 1 else ""
        print(f"Limitation ({code}, {len(items)}x): {items[0].explanation}{more}")
    return 1 if result.findings else 0


if __name__ == "__main__":
    sys.exit(main())
