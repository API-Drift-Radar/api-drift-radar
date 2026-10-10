"""Discovery inputs and evidence exchanged with future monitoring/storage code.

These records describe results; they do not perform OpenAPI validation or I/O.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping


@dataclass(frozen=True)
class DiscoveryRequest:
    target: str
    method: str | None = None
    api_version: str | None = None
    product: str | None = None


@dataclass(frozen=True)
class NormalizedTarget:
    original_target: str
    normalized_url: str
    hostname: str
    path: str
    method: str | None = None
    api_version: str | None = None
    product: str | None = None


class DiscoveryStatus(str, Enum):
    VALIDATED = "validated"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"
    INACCESSIBLE = "inaccessible"
    REJECTED = "rejected"


@dataclass(frozen=True)
class MatchingEvidence:
    criterion: str
    description: str
    source_url: str
    outcome: str | None = None  # match, mismatch, indeterminate or not_requested; None for discovery hints


@dataclass(frozen=True)
class DiscoveryAttempt:
    url: str
    stage: str
    outcome: str
    reason: str | None = None


@dataclass(frozen=True)
class ContractCandidate:
    source_url: str
    discovery_method: str
    discovery_source: str
    evidence: tuple[MatchingEvidence, ...] = ()
    rejection_reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapturedDocument:
    source_url: str
    content: bytes
    retrieved_at: datetime
    media_type: str | None = None


@dataclass(frozen=True)
class ValidatedContractPackage:
    """Created by the future validator only after required references resolve."""

    candidate: ContractCandidate
    root_document: CapturedDocument
    referenced_documents: tuple[CapturedDocument, ...]
    parsed_contract: Mapping[str, Any]
    openapi_version: str
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArtifactFinding:
    """Something discovery found or hit that is not a supported contract, described structurally.

    category is `unsupported_description` (a formal description we do not support, e.g. Swagger 2.0),
    `documentation_only` (documentation data or a collection, e.g. apiDoc), `authentication_required` (a
    source answered 401/403: its contents are unknown) or `unsupported_dynamic_configuration` (a documentation
    viewer whose configuration cannot be read statically).
    """

    category: str
    kind: str
    url: str
    detail: str
    discovery_method: str | None = None
    parent_url: str | None = None
    status: int | None = None


@dataclass(frozen=True)
class LeadRecord:
    """One step of the search: where it was found, how, and what became of it."""

    url: str
    parent_url: str | None
    mechanism: str
    kind: str
    depth: int
    outcome: str  # fetched, failed, not_examined, skipped_depth, skipped_limit


@dataclass(frozen=True)
class Coverage:
    """How much of the bounded search was completed. `complete` is False when a limit cut it short."""

    complete: bool = True
    limits_reached: tuple[str, ...] = ()
    leads_examined: int = 0
    leads_unexamined: int = 0
    unexamined: tuple[LeadRecord, ...] = ()  # the first few leads left unexamined
    requests_used: int = 0
    requests_limit: int = 0
    reserved_for_references: int = 0
    hosts_contacted: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiscoveryOutcome:
    status: DiscoveryStatus
    candidates: tuple[ContractCandidate, ...] = ()
    attempts: tuple[DiscoveryAttempt, ...] = ()
    limitations: tuple[str, ...] = ()
    package: ValidatedContractPackage | None = None
    packages: tuple[ValidatedContractPackage, ...] = ()  # the accepted alternatives of an ambiguous outcome
    artifacts: tuple[ArtifactFinding, ...] = ()  # optional detail: unsupported or documentation-only findings, barriers
    trail: tuple[LeadRecord, ...] = ()  # optional detail: the navigation steps, each with its parent and mechanism
    coverage: Coverage | None = None  # optional detail: how complete the bounded search was

    def __post_init__(self):
        if not isinstance(self.status, DiscoveryStatus):
            raise ValueError("status must be a DiscoveryStatus.")
        if (self.status is DiscoveryStatus.VALIDATED) != (self.package is not None):
            raise ValueError("Only a validated outcome must contain a contract package.")
        if self.packages and self.status is not DiscoveryStatus.AMBIGUOUS:
            raise ValueError("Only an ambiguous outcome carries alternative packages.")
