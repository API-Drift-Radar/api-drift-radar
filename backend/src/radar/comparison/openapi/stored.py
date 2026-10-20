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
import posixpath
from typing import Any
from urllib.parse import urljoin, urlsplit

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


_ROOT = "@root"  # the root document's key: its own name (openapi.json or openapi.yaml) is not part of the contract
_BASE = "https://stored.invalid/contract/"  # a scheme whose relative references resolve
_EXTERNAL = "https://stored.invalid/external/"


def _key(url: str, root_url: str) -> str:
    """A document's place relative to the root, so where the contract was fetched from does not matter."""
    root, this = urlsplit(root_url), urlsplit(url)
    if (this.scheme, this.netloc) != (root.scheme, root.netloc):
        return f"//{this.netloc}{this.path}" + (f"?{this.query}" if this.query else "")
    key = posixpath.relpath(this.path or "/", posixpath.dirname(root.path) or "/")
    return key + (f"?{this.query}" if this.query else "")


def _rebuilt_url(key: str, depth: int) -> str:
    """The address a document has once rebuilt: relative references between documents still resolve."""
    root_directory = _BASE + "d/" * depth
    if key == _ROOT:
        return root_directory + "root"
    if key.startswith("//"):
        return _EXTERNAL + key[2:]
    return urljoin(root_directory, key)


def normalize_contract(package: ValidatedContractPackage) -> tuple[bytes, str]:
    """Canonical bytes and a version for a package. Deterministic; the package is not modified.

    Documents are keyed by their place relative to the root and the root's own address is left out, so only the
    contract's content decides the bytes: the same contract fetched from a CDN, or as JSON rather than YAML, is the
    same. (A reference back to the root document by file name is therefore not followed when rebuilt.) A referenced
    document that cannot be parsed is left out, exactly as the comparison treats an uncaptured one.
    """
    root_url = package.root_document.source_url
    documents = {_ROOT: package.parsed_contract}
    for document in package.referenced_documents:
        parsed = parse_document(document.content)
        if parsed.ok:
            documents[_key(document.source_url, root_url)] = parsed.value
    payload = {"format": FORMAT, "normalization_version": NORMALIZATION_VERSION,
               "openapi": package.openapi_version, "documents": documents}
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
    documents = payload.get("documents")
    if not isinstance(documents, dict) or not isinstance(documents.get(_ROOT), dict):
        raise ValueError("The normalized contract has no readable root document.")
    depth = max((key.count("../") for key in documents if not key.startswith("//") and key != _ROOT), default=0)
    urls = {key: _rebuilt_url(key, depth) for key in documents}
    root = urls[_ROOT]
    candidate = ContractCandidate(
        root, "stored_snapshot", root, source_kind=source_kind,
        evidence=(MatchingEvidence("provenance", "A stored snapshot, which is only ever a confirmed contract.", root, "match"),))
    captured = lambda key: CapturedDocument(urls[key], _canonical(documents[key]), _EPOCH, "application/json")  # noqa: E731
    return ValidatedContractPackage(
        candidate, captured(_ROOT), tuple(captured(key) for key in sorted(documents) if key != _ROOT), documents[_ROOT],
        str(payload.get("openapi") or documents[_ROOT].get("openapi", "")))


def package_from_originals(reference, snapshot_documents, *, source_kind: str = "provider") -> ValidatedContractPackage:
    """A package rebuilt from a stored contract's original documents, as discovery would have captured it.

    `snapshot_documents` is `store.snapshot_documents(snapshot_id)`: it says where each stored file came from.
    Raises ValueError if the originals cannot be read.
    """
    by_path = {d.path: d for d in snapshot_documents}
    root = next((d for d in snapshot_documents if d.is_root), None)
    if root is None or root.path not in reference.documents:
        raise ValueError("The stored contract has no readable original root document.")
    parsed = parse_document(reference.documents[root.path])
    if not parsed.ok or not isinstance(parsed.value, dict):
        raise ValueError("The stored original root document cannot be parsed.")
    candidate = ContractCandidate(
        root.source_url, "stored_snapshot", root.source_url, source_kind=source_kind,
        evidence=(MatchingEvidence("provenance", "A stored snapshot, which is only ever a confirmed contract.",
                                   root.source_url, "match"),))
    referenced = tuple(
        CapturedDocument(d.source_url, reference.documents[d.path], d.retrieved_at, d.media_type)
        for d in snapshot_documents if not d.is_root and d.path in reference.documents and d.path in by_path)
    return ValidatedContractPackage(
        candidate, CapturedDocument(root.source_url, reference.documents[root.path], root.retrieved_at, root.media_type),
        referenced, parsed.value, str(parsed.value.get("openapi", "")))


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


def compare_stored(reference, normalized: bytes, normalization_version: str, *, snapshot_documents=None,
                   before_source_kind: str = "provider",
                   after_source_kind: str = "provider") -> tuple[list[ChangeInput], list[dict]]:
    """Compare a stored reference (a `StoredContract`) with a newly normalized contract.

    The reference is read from its normalized form while that version is still supported. After a normalization
    upgrade it is rebuilt from the stored originals and normalized again with the current rules, so both sides always
    use the same version; that needs `snapshot_documents=store.snapshot_documents(reference_snapshot.id)`.

    Returns `(changes, limitations)` in the shapes `Store.complete_check` takes. Raises ValueError if either side
    cannot be read, which the runner records as a failed comparison.
    """
    if normalization_version not in SUPPORTED_VERSIONS:
        raise ValueError(f"Normalization version {normalization_version!r} is not supported.")
    if reference.normalization_version in SUPPORTED_VERSIONS:
        before = package_from_normalized(reference.normalized, source_kind=before_source_kind)
    else:
        if snapshot_documents is None:
            raise ValueError(f"Normalization version {reference.normalization_version!r} is not supported, and the "
                             "stored originals were not given to rebuild it.")
        original = package_from_originals(reference, snapshot_documents, source_kind=before_source_kind)
        before = package_from_normalized(normalize_contract(original)[0], source_kind=before_source_kind)
    after = package_from_normalized(normalized, source_kind=after_source_kind)
    result = compare_contracts(before, after)
    return [to_change_input(f) for f in result.findings], [limitation_to_entry(l) for l in result.limitations]
