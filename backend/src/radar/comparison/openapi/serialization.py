"""JSON-serializable form of a comparison result, for the API and for storage to persist alongside snapshot links."""

from radar.domain.comparison import ComparisonResult, ContractFinding, SourceLocation


def _location(location: SourceLocation | None):
    return None if location is None else {"document_url": location.document_url, "pointer": location.pointer}


def finding_to_dict(finding: ContractFinding) -> dict:
    return {
        "key": finding.key, "rule_id": finding.rule_id, "category": finding.category,
        "change_kind": finding.change_kind, "direction": finding.direction, "method": finding.method,
        "path": finding.path, "before_present": finding.before_present, "after_present": finding.after_present,
        "before_value": finding.before_value, "after_value": finding.after_value,
        "before_location": _location(finding.before_location), "after_location": _location(finding.after_location),
        "explanation": finding.explanation,
    }


def result_to_dict(result: ComparisonResult) -> dict:
    return {
        "ruleset_version": result.ruleset_version,
        "findings": [finding_to_dict(f) for f in result.findings],
        "limitations": [{"code": l.code, "scope": l.scope, "blocks_classification": l.blocks_classification,
                         "explanation": l.explanation} for l in result.limitations],
        "coverage": {"complete": result.coverage.complete, "assessed": list(result.coverage.assessed),
                     "not_assessed": list(result.coverage.not_assessed)},
    }
