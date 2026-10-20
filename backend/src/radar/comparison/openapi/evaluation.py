"""Measure the comparison engine against labeled fixtures.

    python -m radar.comparison.openapi.evaluation            # prints the report; exit status 1 on any mismatch

A case is a directory under `backend/tests/fixtures/comparison/` holding `before.json`, `after.json`, `expected.json`
and optionally `meta.json`. Labels are written from the rule specification (the module docstring of `compare`), not
generated from the comparator's output. `expected.json` lists the findings (`rule_id`, `method`, `path` as the
normalized identity, `target` for rules about a parameter, body, response, schema or documentation field, and `category`) and the limitation codes that must appear.

Detection: a finding is found when its (rule, method, normalized path, target) identity is present; precision and recall are
computed over those identities. Classification: of the findings that were detected, the share with the expected
category. A zero denominator is reported as None, never as 1.0.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from radar.comparison.openapi.compare import compare_contracts
from radar.comparison.openapi.operations import normalize_path
from radar.domain.comparison import ComparisonResult
from radar.domain.discovery import (
    CapturedDocument, ContractCandidate, MatchingEvidence, ValidatedContractPackage,
)

FIXTURES = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "comparison"
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


@dataclass
class Report:
    cases: int = 0
    expected: int = 0
    returned: int = 0
    detected: int = 0
    classified_correctly: int = 0
    failures: list = field(default_factory=list)

    @property
    def precision(self):
        return round(self.detected / self.returned, 3) if self.returned else None

    @property
    def recall(self):
        return round(self.detected / self.expected, 3) if self.expected else None

    @property
    def classification_accuracy(self):
        return round(self.classified_correctly / self.detected, 3) if self.detected else None


def evaluate(cases: list[Case], compare=compare_contracts) -> Report:
    report = Report(cases=len(cases))
    for case in cases:
        result: ComparisonResult = compare(case.before, case.after)
        got = {identity(f): f.category for f in result.findings}
        report.expected += len(case.expected_findings)
        report.returned += len(got)
        for key, category in case.expected_findings.items():
            if key in got:
                report.detected += 1
                report.classified_correctly += got[key] == category
                if got[key] != category:
                    report.failures.append(f"{case.name}: {key} classified {got[key]}, expected {category}")
            else:
                report.failures.append(f"{case.name}: missed {key}")
        report.failures.extend(f"{case.name}: unexpected {key} ({got[key]})" for key in got if key not in case.expected_findings)
        codes = {l.code for l in result.limitations}
        report.failures.extend(f"{case.name}: missing limitation {code}" for code in sorted(case.expected_limitations - codes))
    return report


def main() -> int:
    report = evaluate(load_cases())
    print(f"cases: {report.cases} | expected findings: {report.expected} | returned: {report.returned}")
    print(f"precision: {report.precision} | recall: {report.recall} | classification accuracy: {report.classification_accuracy}")
    for failure in report.failures:
        print("  FAIL", failure)
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
