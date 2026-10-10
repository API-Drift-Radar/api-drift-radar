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
class DiscoveryOutcome:
    status: DiscoveryStatus
    candidates: tuple[ContractCandidate, ...] = ()
    attempts: tuple[DiscoveryAttempt, ...] = ()
    limitations: tuple[str, ...] = ()
    package: ValidatedContractPackage | None = None
    packages: tuple[ValidatedContractPackage, ...] = ()  # the accepted alternatives of an ambiguous outcome

    def __post_init__(self):
        if not isinstance(self.status, DiscoveryStatus):
            raise ValueError("status must be a DiscoveryStatus.")
        if (self.status is DiscoveryStatus.VALIDATED) != (self.package is not None):
            raise ValueError("Only a validated outcome must contain a contract package.")
        if self.packages and self.status is not DiscoveryStatus.AMBIGUOUS:
            raise ValueError("Only an ambiguous outcome carries alternative packages.")
