"""Compare two validated OpenAPI contract packages: operations, servers, request parameters, request bodies and responses. Pure: no network,
storage or model calls, and the inputs are never modified.

Perspective: a client that calls the API. Rules (ruleset `schemas-1`):

- `operation.removed`  potentially breaking: existing clients may still call it.
- `operation.added`    non-breaking under our rules: it removes nothing a client uses.
- `servers.changed`    uncertain: effective URLs may have moved even where declared paths match.
- `parameter.*`        request parameters of operations present in both contracts; see `parameters.py`.
- `request_body.*`, `response.*`, `schema.*`  request bodies, responses, and the schemas inside them and inside
                       parameters; see `bodies.py`, `responses.py` and `schemas.py`.

A renamed path is a removal plus an addition; renames are never guessed. Response headers, links and
documentation are not assessed yet, and the result says so, so an empty finding list never means that the contracts
are unchanged.
"""

from radar.comparison.openapi.bodies import compare_request_bodies
from radar.comparison.openapi.operations import ContractView, normalize_path, read_contract
from radar.comparison.openapi.parameters import compare_parameters
from radar.comparison.openapi.responses import compare_responses
from radar.discovery.validation import HTTP_METHODS
from radar.domain.comparison import (
    ADDED, CHANGED, NO_DIRECTION, NON_BREAKING, POTENTIALLY_BREAKING, REMOVED, UNCERTAIN, ComparisonCoverage,
    ComparisonLimitation, ComparisonResult, ContractFinding,
)
from radar.domain.discovery import ValidatedContractPackage, is_confirmed

RULESET_VERSION = "schemas-1"
ASSESSED = ("operations", "servers", "parameters", "request bodies", "responses")
NOT_ASSESSED = ("response headers and links", "media-type encoding", "documentation (descriptions, examples, deprecation)",
                "path-level and operation-level servers", "callbacks")
_METHOD_ORDER = {method.upper(): index for index, method in enumerate(HTTP_METHODS)}


def _input_limitations(package: ValidatedContractPackage, side: str):
    if package.candidate.source_kind == "directory_copy":
        yield ComparisonLimitation(
            "directory_copy_source",
            f"The {side} contract is a copy kept by a third-party directory, which may be older than what the "
            "provider currently publishes. Differences may reflect the copy, not the provider.", side)
    if not is_confirmed(package):
        yield ComparisonLimitation(
            "unconfirmed_contract",
            f"The {side} contract is unconfirmed: nothing established that it is the complete, current contract "
            "of this API. Differences from it are not evidence of drift.", side)


def _removed(operation) -> ContractFinding:
    return ContractFinding(
        key=f"operation.removed|{operation.method}|{normalize_path(operation.declared_path)}", rule_id="operation.removed",
        category=POTENTIALLY_BREAKING, change_kind=REMOVED, direction=NO_DIRECTION, method=operation.method,
        path=operation.declared_path, before_present=True, after_present=False,
        before_location=operation.location,
        explanation=f"{operation.method} {operation.declared_path} is no longer declared. Existing clients calling "
                    "this operation may be affected.")


def _added(operation) -> ContractFinding:
    return ContractFinding(
        key=f"operation.added|{operation.method}|{normalize_path(operation.declared_path)}", rule_id="operation.added",
        category=NON_BREAKING, change_kind=ADDED, direction=NO_DIRECTION, method=operation.method,
        path=operation.declared_path, before_present=False, after_present=True,
        after_location=operation.location,
        explanation=f"{operation.method} {operation.declared_path} is newly declared. Existing clients do not "
                    "depend on it, so it does not affect them.")


def _compare_operations(before: ContractView, after: ContractView) -> list[ContractFinding]:
    skipped = set(before.unresolved) | set(after.unresolved)  # unknown scope: neither added nor removed
    findings = []
    for key in before.operations.keys() - after.operations.keys():
        if key[1] not in skipped:
            findings.append(_removed(before.operations[key]))
    for key in after.operations.keys() - before.operations.keys():
        if key[1] not in skipped:
            findings.append(_added(after.operations[key]))
    return findings


def _compare_parameters(before: ContractView, after: ContractView, limitations: list) -> list[ContractFinding]:
    """Parameters, request bodies and responses of operations present on both sides; an added or removed operation is reported once, as itself."""
    skipped = set(before.unresolved) | set(after.unresolved)
    findings = []
    for key in sorted(before.operations.keys() & after.operations.keys()):
        if key[1] not in skipped:
            findings.extend(compare_parameters(before.operations[key], after.operations[key], limitations))
            findings.extend(compare_request_bodies(before.operations[key], after.operations[key], limitations))
            findings.extend(compare_responses(before.operations[key], after.operations[key], limitations))
    return findings


def _compare_servers(before: ContractView, after: ContractView, limitations: list):
    if before.servers is None or after.servers is None:
        limitations.append(ComparisonLimitation(
            "servers_unreadable", "The servers of one contract could not be read, so they were not compared.",
            "servers", True))
        return []
    if before.servers == after.servers:
        return []
    urls = lambda view: [server["url"] for server in view.servers]  # noqa: E731
    return [ContractFinding(
        key="servers.changed", rule_id="servers.changed", category=UNCERTAIN, change_kind=CHANGED,
        direction=NO_DIRECTION, method=None, path=None, before_present=True, after_present=True,
        before_value=urls(before), after_value=urls(after),
        before_location=before.servers_location, after_location=after.servers_location,
        explanation="The declared servers changed. Effective request URLs may have moved even where the declared "
                    "paths are the same; this comparison does not resolve effective URLs.")]


def _order(finding: ContractFinding):
    return (finding.path or "", _METHOD_ORDER.get(finding.method or "", 99), finding.rule_id, finding.key)


def compare_contracts(before: ValidatedContractPackage, after: ValidatedContractPackage) -> ComparisonResult:
    """Compare two packages. Deterministic: the same inputs always give the same result."""
    before_view, after_view = read_contract(before, "before"), read_contract(after, "after")
    limitations: list[ComparisonLimitation] = [*before_view.limitations, *after_view.limitations]
    for package, side in ((before, "before"), (after, "after")):
        limitations.extend(_input_limitations(package, side))
    if before_view.has_webhooks or after_view.has_webhooks:
        limitations.append(ComparisonLimitation(
            "webhooks_not_compared", "Webhooks are declared and were not compared.", "webhooks"))
    findings = (_compare_operations(before_view, after_view) + _compare_parameters(before_view, after_view, limitations)
                + _compare_servers(before_view, after_view, limitations))
    limitations.append(ComparisonLimitation(
        "scope_not_assessed", "Not assessed yet: " + ", ".join(NOT_ASSESSED) + ". No findings in this result does "
        "not mean the rest of the contract is unchanged.", "contract"))
    unique = {(l.code, l.scope, l.explanation): l for l in limitations}
    return ComparisonResult(
        findings=tuple(sorted(findings, key=_order)), limitations=tuple(unique.values()),
        coverage=ComparisonCoverage(ASSESSED, NOT_ASSESSED), ruleset_version=RULESET_VERSION)
