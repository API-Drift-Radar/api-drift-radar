"""Measure the comparison engine against labeled fixtures.

    python -m radar.comparison.openapi.evaluation            # prints the reports; exit status 1 on any mismatch
    python -m radar.comparison.openapi.evaluation --json     # the machine-readable report

A case is a directory holding `before.json`, `after.json`, `expected.json` and optionally `meta.json`. There are two
sets. `backend/tests/fixtures/comparison/` is the development set: the rules were built alongside it, so it is a
regression check. `backend/tests/fixtures/comparison_holdout/` was labeled before the engine was run on it, and any
disagreement was investigated rather than relabeled (the one label changed was a spelling of a header name's key). Both
sets have the same author as the rules, so the held-out set is a blind check by one person, not independent validation.
Labels are written from the rule specification (the module docstring of `compare`), not generated from the
comparator's output. `expected.json` lists the findings (`rule_id`, `method`, `path` as the
normalized identity, `target` for rules about a parameter, body, response, schema or documentation field, and `category`) and the limitation codes that must appear.

Detection: a finding is found when its (rule, method, normalized path, target) identity is present; precision and recall are
computed over those identities. Classification: of the findings that were detected, the share with the expected
category. A zero denominator is reported as None, never as 1.0.
"""

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from radar.comparison.openapi.compare import compare_contracts
from radar.comparison.openapi.operations import normalize_path
from radar.domain.comparison import CATEGORIES, ComparisonResult
from radar.domain.discovery import (
    CapturedDocument, ContractCandidate, MatchingEvidence, ValidatedContractPackage,
)

FIXTURES = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "comparison"
HOLDOUT = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "comparison_holdout"
RETRIEVED = datetime(2026, 10, 13, tzinfo=timezone.utc)
DEFAULT_URL = "https://api.acme.com/openapi.json"


def build_package(contract: dict, *, url=DEFAULT_URL, source_kind="provider", confirmed=True,
                  referenced=None) -> ValidatedContractPackage:
    """A package as discovery would return it. `referenced` maps document URL -> parsed JSON content."""
    evidence = (MatchingEvidence("provenance", "served by the provider", url, "match"),) if confirmed else ()
    candidate = ContractCandidate(url, "common_location", url, evidence=evidence, source_kind=source_kind)
    documents = tuple(CapturedDocument(u, json.dumps(c).encode(), RETRIEVED, "application/json")
                      for u, c in (referenced or {}).items())
    return ValidatedContractPackage(
        candidate, CapturedDocument(url, json.dumps(contract).encode(), RETRIEVED, "application/json"), documents,
        contract, str(contract.get("openapi", "3.0.3")))


@dataclass(frozen=True)
class Case:
    name: str
    before: ValidatedContractPackage
    after: ValidatedContractPackage
    expected_findings: dict  # (rule_id, METHOD or "", normalized path or "", target or "") -> category
    expected_limitations: frozenset
    rationale: str


def _side(directory: Path, side: str, meta: dict) -> ValidatedContractPackage:
    options = meta.get(side, {})
    referenced = {u: json.loads((directory / name).read_text()) for u, name in options.get("referenced", {}).items()}
    return build_package(json.loads((directory / f"{side}.json").read_text()), url=options.get("url", DEFAULT_URL),
                         source_kind=options.get("source_kind", "provider"), confirmed=options.get("confirmed", True),
                         referenced=referenced)


def load_cases(root: Path = FIXTURES) -> list[Case]:
    cases = []
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        meta_file = directory / "meta.json"
        meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
        expected = json.loads((directory / "expected.json").read_text())
        findings = {(f["rule_id"], f.get("method", ""), normalize_path(f.get("path", "")), f.get("target", f.get("parameter", ""))):
                    f["category"] for f in expected["findings"]}
        cases.append(Case(directory.name, _side(directory, "before", meta), _side(directory, "after", meta), findings,
                          frozenset(expected.get("limitations", [])), meta.get("rationale", "")))
    return cases


def identity(finding) -> tuple:
    """(rule, method, normalized path, target): what the finding is about, taken from the rest of its key.

    A parameter is `in:name` (`path:N` for the Nth path placeholder). A schema change is `scope|schema path[|keyword]`,
    such as `request-body:application/json|/owner/age`; documentation is `scope|field`, such as `operation|summary`.
    """
    parts = finding.key.split("|", 3)
    target = parts[3] if len(parts) > 3 else ""  # operation and server rules have no target
    return finding.rule_id, finding.method or "", normalize_path(finding.path or ""), target


CONFUSION_ROWS = (*CATEGORIES, "unexpected")  # what the label says; "unexpected": a finding nobody labeled
CONFUSION_COLUMNS = (*CATEGORIES, "missed")  # what the engine said; "missed": a labeled finding that was not produced


def _ratio(numerator, denominator):
    return round(numerator / denominator, 3) if denominator else None  # a zero denominator is N/A, never 1.0


@dataclass
class Report:
    cases: int = 0
    expected: int = 0
    returned: int = 0
    detected: int = 0
    classified_correctly: int = 0
    failures: list = field(default_factory=list)
    confusion: dict = field(default_factory=lambda: {r: {c: 0 for c in CONFUSION_COLUMNS} for r in CONFUSION_ROWS})
    per_rule: dict = field(default_factory=dict)  # rule id -> {expected, returned, detected, correct}
    uncertain_returned: int = 0
    limitations: dict = field(default_factory=dict)  # limitation code -> number of cases that produced it

    @property
    def precision(self):
        return _ratio(self.detected, self.returned)

    @property
    def recall(self):
        return _ratio(self.detected, self.expected)

    @property
    def classification_accuracy(self):
        return _ratio(self.classified_correctly, self.detected)

    def as_dict(self) -> dict:
        """The machine-readable report. Ratios are None when their denominator is zero."""
        rules = {rule: {**counts, "recall": _ratio(counts["detected"], counts["expected"]),
                        "precision": _ratio(counts["detected"], counts["returned"]),
                        "classification_accuracy": _ratio(counts["correct"], counts["detected"])}
                 for rule, counts in sorted(self.per_rule.items())}
        return {"cases": self.cases, "expected_findings": self.expected, "returned_findings": self.returned,
                "detected": self.detected, "classified_correctly": self.classified_correctly,
                "precision": self.precision, "recall": self.recall,
                "classification_accuracy": self.classification_accuracy,
                "uncertain_returned": self.uncertain_returned, "confusion_matrix": self.confusion,
                "per_rule": rules, "limitations_by_code": dict(sorted(self.limitations.items())),
                "failures": self.failures}


def evaluate(cases: list[Case], compare=compare_contracts) -> Report:
    report = Report(cases=len(cases))
    rule = lambda name: report.per_rule.setdefault(  # noqa: E731
        name, {"expected": 0, "returned": 0, "detected": 0, "correct": 0})
    for case in cases:
        result: ComparisonResult = compare(case.before, case.after)
        got = {identity(f): f for f in result.findings}
        report.expected += len(case.expected_findings)
        report.returned += len(got)
        report.uncertain_returned += sum(1 for f in got.values() if f.category == "uncertain")
        for code in {l.code for l in result.limitations}:
            report.limitations[code] = report.limitations.get(code, 0) + 1
        for key in got:
            rule(key[0])["returned"] += 1
        for key, category in case.expected_findings.items():
            counts = rule(key[0])
            counts["expected"] += 1
            if key in got:
                report.detected += 1
                counts["detected"] += 1
                report.confusion[category][got[key].category] += 1
                if got[key].category == category:
                    report.classified_correctly += 1
                    counts["correct"] += 1
                else:
                    report.failures.append(f"{case.name}: {key} classified {got[key].category}, expected {category}")
            else:
                report.confusion[category]["missed"] += 1
                report.failures.append(f"{case.name}: missed {key}")
        for key, found in got.items():
            if key not in case.expected_findings:
                report.confusion["unexpected"][found.category] += 1
                report.failures.append(f"{case.name}: unexpected {key} ({found.category})")
        codes = {l.code for l in result.limitations}
        report.failures.extend(f"{case.name}: missing limitation {code}" for code in sorted(case.expected_limitations - codes))
    return report


def render(report: Report, name: str) -> str:
    width = max(len(c) for c in CONFUSION_COLUMNS) + 2
    lines = [f"[{name}] cases: {report.cases} | expected findings: {report.expected} | returned: {report.returned} "
             f"(uncertain: {report.uncertain_returned})",
             f"precision: {report.precision} | recall: {report.recall} | "
             f"classification accuracy: {report.classification_accuracy}", "",
             "confusion matrix (rows: what the label says, columns: what the engine said)",
             " " * 28 + "".join(c.rjust(width) for c in CONFUSION_COLUMNS)]
    for row in CONFUSION_ROWS:
        lines.append(f"{row:28}" + "".join(str(report.confusion[row][c]).rjust(width) for c in CONFUSION_COLUMNS))
    lines += ["", "limitations produced (cases): " + (", ".join(f"{k} {v}" for k, v in sorted(report.limitations.items())) or "none")]
    lines += [f"  FAIL {failure}" for failure in report.failures]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m radar.comparison.openapi.evaluation", description=__doc__.split("\n")[0])
    parser.add_argument("--fixtures", type=Path, action="append",
                        help="a fixtures directory; repeat for several (default: the development fixtures and the held-out set)")
    parser.add_argument("--json", action="store_true", help="print the machine-readable report")
    args = parser.parse_args(argv)
    sets = args.fixtures or [FIXTURES, HOLDOUT]
    reports = {path.name: evaluate(load_cases(path)) for path in sets}
    if args.json:
        print(json.dumps({name: report.as_dict() for name, report in reports.items()}, indent=2))
    else:
        print("\n\n".join(render(report, name) for name, report in reports.items()))
    return 1 if any(report.failures for report in reports.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
