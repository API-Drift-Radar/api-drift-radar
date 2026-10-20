"""The comparison as storage (#10) uses it: a normalized form to keep, and a comparison against a stored reference.

Storage keeps, with each snapshot, opaque `normalized` bytes and a `normalization_version`, and decides `unchanged`
or `changed` by whether those are identical to the reference's. This module provides the two functions its runner
needs:

    normalized, version = normalize_contract(package)
    changes, limitations = compare_stored(reference, normalized, version)

`normalize_contract` produces canonical JSON of the root document and every captured referenced document that
parses (sorted keys, no timestamps, no retrieval details). Two captures of the same contract that differ only in file
formatting or key order give identical bytes, so storage records them as `unchanged`. A change the comparison does not
report, such as reordering a `required` list, still changes the bytes: that check is `changed` with no change records.

`compare_stored` rebuilds both sides from their normalized form alone, so the comparison never needs the original
bytes, and maps each finding to storage's `ChangeInput`. Everything in a snapshot is a confirmed contract (an
unconfirmed one never becomes a snapshot), so the rebuilt packages count as confirmed; pass `before_source_kind` and
`after_source_kind` (a snapshot's `provenance["source_kind"]`) to have a directory copy noted as a limitation.
"""

from datetime import datetime, timezone
import json
from typing import Any

from radar.comparison.openapi.compare import RULESET_VERSION, compare_contracts
from radar.discovery.validation import parse_document
from radar.domain.comparison import ComparisonLimitation, ContractFinding
from radar.domain.discovery import (
    CapturedDocument, ContractCandidate, MatchingEvidence, ValidatedContractPackage,
)
from radar.storage.records import (
    ABSENT, DOCUMENTATION_ONLY, NON_BREAKING, POTENTIALLY_BREAKING, REQUEST, RESPONSE, UNCERTAIN, ChangeInput,
)

NORMALIZATION_VERSION = "openapi-canonical-1"
SUPPORTED_VERSIONS = frozenset({NORMALIZATION_VERSION})
FORMAT = "radar.openapi.normalized"
MAX_TEXT = 4000  # storage's limit for the text fields of a change
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)  # rebuilt documents have no retrieval time; none is compared
_CLASSIFICATION = {  # our category names, as storage spells them
    "potentially_breaking": POTENTIALLY_BREAKING, "non_breaking_under_rules": NON_BREAKING,
    "documentation_only": DOCUMENTATION_ONLY, "uncertain": UNCERTAIN,
}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def normalize_contract(package: ValidatedContractPackage) -> tuple[bytes, str]:
    """Canonical bytes and a version for a package. Deterministic; the package is not modified.

    A referenced document that cannot be parsed is left out, exactly as the comparison treats an uncaptured one.
    """
    documents = {package.root_document.source_url: package.parsed_contract}
    for document in package.referenced_documents:
        parsed = parse_document(document.content)
        if parsed.ok:
            documents[document.source_url] = parsed.value
    payload = {"format": FORMAT, "normalization_version": NORMALIZATION_VERSION,
               "openapi": package.openapi_version, "root": package.root_document.source_url, "documents": documents}
    return _canonical(payload), NORMALIZATION_VERSION


def package_from_normalized(normalized: bytes, *, source_kind: str = "provider") -> ValidatedContractPackage:
    """A package the comparison can read, rebuilt from normalized bytes. Raises ValueError if they are not ours."""
    try:
        payload = json.loads(normalized)
    except (ValueError, TypeError):
        raise ValueError("The normalized contract is not valid JSON.") from None
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        raise ValueError("The normalized contract was not produced by normalize_contract.")
    if payload.get("normalization_version") not in SUPPORTED_VERSIONS:
        raise ValueError(f"Normalization version {payload.get('normalization_version')!r} is not supported.")
    root, documents = payload.get("root"), payload.get("documents")
    if not isinstance(root, str) or not isinstance(documents, dict) or not isinstance(documents.get(root), dict):
        raise ValueError("The normalized contract has no readable root document.")
    candidate = ContractCandidate(
        root, "stored_snapshot", root, source_kind=source_kind,
        evidence=(MatchingEvidence("provenance", "A stored snapshot, which is only ever a confirmed contract.", root, "match"),))
    captured = lambda url: CapturedDocument(url, _canonical(documents[url]), _EPOCH, "application/json")  # noqa: E731
    return ValidatedContractPackage(
        candidate, captured(root), tuple(captured(url) for url in sorted(documents) if url != root), documents[root],
        str(payload.get("openapi") or documents[root].get("openapi", "")))


def _split(explanation: str) -> tuple[str, str | None]:
    """The first sentence as the summary and the rest as the rationale."""
    text = " ".join(explanation.split())
    cut = text.find(". ")
    if cut == -1:
        return text[:MAX_TEXT], None
    return text[:cut + 1][:MAX_TEXT], text[cut + 2:][:MAX_TEXT] or None


def location_text(finding: ContractFinding) -> str:
    """A readable place, such as `GET /pets > response 200 (application/json) > [].name`."""
    base = f"{finding.method} {finding.path}" if finding.method else (finding.path or "contract")
    subject = finding.subject or ""
    text = subject if subject.startswith(base) else (f"{base} > {subject}" if subject else base)
    return text[:MAX_TEXT]


def _location_dict(location):
    return None if location is None else {"document_url": location.document_url, "pointer": location.pointer}


def to_change_input(finding: ContractFinding) -> ChangeInput:
    """Storage's record of one finding. A side where the thing does not exist is ABSENT, never null."""
    summary, rationale = _split(finding.explanation)
    return ChangeInput(
        change_type=finding.rule_id.replace(".", "_"),
        location=location_text(finding),
        classification=_CLASSIFICATION[finding.category],
        summary=summary or finding.rule_id,
        direction={"request": REQUEST, "response": RESPONSE}.get(finding.direction),
        rationale=rationale,
        before=finding.before_value if finding.before_present else ABSENT,
        after=finding.after_value if finding.after_present else ABSENT,
        details={"rule": finding.rule_id, "key": finding.key, "ruleset_version": RULESET_VERSION,
                 "subject": finding.subject, "method": finding.method, "path": finding.path,
                 "before_location": _location_dict(finding.before_location),
                 "after_location": _location_dict(finding.after_location)})


def limitation_to_entry(limitation: ComparisonLimitation) -> dict:
    return {"code": limitation.code, "scope": limitation.scope, "explanation": limitation.explanation,
            "blocks_classification": limitation.blocks_classification}


def compare_stored(reference, normalized: bytes, normalization_version: str, *, before_source_kind: str = "provider",
                   after_source_kind: str = "provider") -> tuple[list[ChangeInput], list[dict]]:
    """Compare a stored reference (a `StoredContract`) with a newly normalized contract.

    Returns `(changes, limitations)` in the shapes `Store.complete_check` takes. Raises ValueError if either side
    cannot be read, which the runner records as a failed comparison.
    """
    before = package_from_normalized(reference.normalized, source_kind=before_source_kind)
    after = package_from_normalized(normalized, source_kind=after_source_kind)
    if normalization_version not in SUPPORTED_VERSIONS:
        raise ValueError(f"Normalization version {normalization_version!r} is not supported.")
    result = compare_contracts(before, after)
    return [to_change_input(f) for f in result.findings], [limitation_to_entry(l) for l in result.limitations]
