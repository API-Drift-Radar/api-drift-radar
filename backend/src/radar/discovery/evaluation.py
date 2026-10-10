"""Evaluate one retrieved candidate and, if it passes, assemble the contract package.

Stages, cheapest first, each rejection recording its stage:
  validation        is it a valid, supported OpenAPI document?          (pure)
  matching          does it belong to the requested API?                (pure)
  reference_capture can every document it depends on be captured?       (network)
Matching runs before capture so a contract that does not fit never spends request budget on
its references. Nothing here chooses between candidates or sets a final discovery status.
"""

from dataclasses import dataclass, replace
import hashlib
import json

from radar.discovery.candidates import RetrievedCandidate
from radar.discovery.capture import CaptureLimits, capture_references
from radar.discovery.limits import DiscoveryBudget
from radar.discovery.matching import MatchContext, MatchResult, assess_match
from radar.discovery.providers import ProviderMapping
from radar.discovery.validation import ValidationLimits, ValidationRejection, parse_document, validate_document
from radar.domain.discovery import (
    CapturedDocument, ContractCandidate, MatchingEvidence, NormalizedTarget, ValidatedContractPackage,
)


@dataclass(frozen=True)
class CandidateEvaluation:
    candidate: ContractCandidate  # evidence and, if rejected, reasons recorded on it
    package: ValidatedContractPackage | None = None
    rejection: ValidationRejection | None = None  # stage: validation, matching or reference_capture
    match: MatchResult | None = None  # present once matching ran, whatever its outcome
    capture_failure: object | None = None  # CaptureFailure when capture was what failed

    def __post_init__(self):
        if (self.package is None) == (self.rejection is None):
            raise ValueError('An evaluation either produced a package or has a rejection.')

    @property
    def accepted(self):
        return self.package is not None


def _canonical(value) -> bytes:
    """Serialization-independent bytes for a parsed document.

    Object keys are sorted so key order never matters; list order is kept because it can be
    meaningful. JSON text keeps `true` and `1`, and `1` and `1.0`, apart (Python's `==` would
    not), so two different contracts are never merged: when in doubt they stay distinct.
    """
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('ascii')


def _document_digest(parsed, raw: bytes) -> bytes:
    try:
        return hashlib.sha256(_canonical(parsed)).digest()
    except (TypeError, ValueError, RecursionError):  # not JSON-representable: fall back to the bytes
        return hashlib.sha256(raw).digest()


def package_fingerprint(package: ValidatedContractPackage) -> str:
    """Identity of a contract's content: the parsed root plus the parsed referenced files.

    The same contract served as JSON and as YAML, with different formatting or key order, has
    one fingerprint. It ignores where the contract was found, file names, and the order of
    referenced documents. A candidate-level key for recognising duplicates, not storage's
    content address: the original bytes are still what gets stored.
    """
    digest = hashlib.sha256()
    digest.update(_document_digest(package.parsed_contract, package.root_document.content))
    parts = []
    for document in package.referenced_documents:
        parsed = parse_document(document.content)
        parts.append(_document_digest(parsed.value, document.content) if parsed.ok
                     else hashlib.sha256(document.content).digest())
    for part in sorted(parts):
        digest.update(part)
    return digest.hexdigest()


def _evidence(checks, source_url):
    return tuple(MatchingEvidence(c.criterion, c.description, c.source_url or source_url, c.outcome) for c in checks)


def _unique(*groups):
    return tuple(dict.fromkeys(item for group in groups for item in group))


def _reject(candidate, rejection, evidence=(), match=None, failure=None):
    reason = f'{rejection.stage}:{rejection.code}: {rejection.reason}'
    updated = replace(candidate, evidence=_unique(candidate.evidence, evidence),
                      rejection_reasons=_unique(candidate.rejection_reasons, (reason,)))
    return CandidateEvaluation(updated, rejection=rejection, match=match, capture_failure=failure)


def evaluate_candidate(
    retrieved: RetrievedCandidate,
    target: NormalizedTarget,
    budget: DiscoveryBudget,
    *,
    mapping: ProviderMapping | None = None,
    validation_limits: ValidationLimits | None = None,
    capture_limits: CaptureLimits | None = None,
    allow_loopback: bool = False,
) -> CandidateEvaluation:
    """Validate, match, capture; return a package or the single rejection that stopped it.

    `mapping` supplies the provider mapping's version and product hints when the candidate was
    found through one. The budget is shared with the rest of discovery.
    """
    fetch, candidate = retrieved.retrieval, retrieved.candidate
    if not fetch.ok or fetch.content is None:
        raise ValueError('Only a successfully retrieved candidate can be evaluated.')
    validation = validate_document(fetch.content, validation_limits)
    if not validation.ok:
        return _reject(candidate, validation.rejection)

    match = assess_match(MatchContext(
        target, fetch.final_url, candidate.discovery_method, candidate.discovery_source,
        mapping.api_version if mapping else None, mapping.product if mapping else None), validation)
    evidence = _evidence(match.checks, fetch.final_url)
    if not match.accepted:
        return _reject(candidate, match.rejection, evidence, match)

    capture = capture_references(fetch.final_url, validation.document, budget, limits=capture_limits,
                                 allow_loopback=allow_loopback)
    if not capture.ok:
        return _reject(candidate, capture.rejection, evidence, match, capture.failure)

    limitations = _unique(validation.limitations, match.limitations, capture.limitations)
    final = replace(candidate, source_url=fetch.final_url, evidence=_unique(candidate.evidence, evidence),
                    rejection_reasons=(), limitations=limitations)
    package = ValidatedContractPackage(
        candidate=final,
        root_document=CapturedDocument(fetch.final_url, fetch.content, fetch.retrieved_at, fetch.content_type),
        referenced_documents=capture.documents,
        parsed_contract=validation.document,
        openapi_version=validation.summary.openapi_version,
        limitations=limitations)
    return CandidateEvaluation(final, package=package, match=match)
