"""JSON-serializable form of a comparison result, for the API and for storage to persist alongside snapshot links."""

import json

from radar.domain.comparison import CATEGORIES, ComparisonResult, ContractFinding, SourceLocation

MAX_LISTED_OPERATIONS = 10
KEYWORD_RULES = frozenset({"schema.constraint_changed", "schema.unsupported_changed"})  # one node, several keywords


def _location(location: SourceLocation | None):
    return None if location is None else {"document_url": location.document_url, "pointer": location.pointer}


def finding_to_dict(finding: ContractFinding) -> dict:
    return {
        "key": finding.key, "rule_id": finding.rule_id, "category": finding.category,
        "change_kind": finding.change_kind, "direction": finding.direction, "method": finding.method,
        "path": finding.path, "before_present": finding.before_present, "after_present": finding.after_present,
        "before_value": finding.before_value, "after_value": finding.after_value,
        "before_location": _location(finding.before_location), "after_location": _location(finding.after_location),
        "explanation": finding.explanation, "subject": finding.subject,
    }


def group_findings(findings) -> list[dict]:
    """Collapse findings that are the same change to the same definition into one group.

    A schema shared by many operations is reported once per use, which is right for the operation a person is looking
    at but would show one removed property 75 times. Findings are grouped when their rule, category, direction,
    definition locations and values are identical. Each group says how many findings it holds and which operations
    they are in; `example_key` is the key of its first finding.
    """
    groups: dict = {}
    for f in findings:
        keyword = f.key.rsplit("|", 1)[-1] if f.rule_id in KEYWORD_RULES else None
        identity = (f.rule_id, f.category, f.direction, f.before_location, f.after_location,
                    json.dumps(f.before_value, sort_keys=True, default=str),
                    json.dumps(f.after_value, sort_keys=True, default=str), keyword)
        groups.setdefault(identity, []).append(f)
    rank = {category: index for index, category in enumerate(CATEGORIES)}
    listed = []
    for members in groups.values():
        first = members[0]
        operations = sorted({f"{f.method} {f.path}" for f in members if f.method})
        listed.append({
            "rule_id": first.rule_id, "category": first.category, "direction": first.direction,
            "count": len(members), "operation_count": len(operations), "operations": operations[:MAX_LISTED_OPERATIONS],
            "before_location": _location(first.before_location), "after_location": _location(first.after_location),
            "subject": first.subject, "example_key": first.key, "explanation": first.explanation})
    return sorted(listed, key=lambda g: (rank[g["category"]], g["rule_id"], g["example_key"]))


def result_to_dict(result: ComparisonResult) -> dict:
    return {
        "ruleset_version": result.ruleset_version,
        "findings": [finding_to_dict(f) for f in result.findings],
        "finding_groups": group_findings(result.findings),
        "limitations": [{"code": l.code, "scope": l.scope, "blocks_classification": l.blocks_classification,
                         "explanation": l.explanation} for l in result.limitations],
        "coverage": {"complete": result.coverage.complete, "assessed": list(result.coverage.assessed),
                     "not_assessed": list(result.coverage.not_assessed)},
    }
