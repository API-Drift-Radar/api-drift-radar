"""Results of comparing two validated OpenAPI contracts.

Findings describe potential risk to a client that calls the API under Radar's documented rules. They do not prove
that the provider changed runtime behavior or that a particular application will break. These records perform no
I/O and know nothing about storage: storage attaches findings to snapshot identifiers.
"""

from dataclasses import dataclass
from typing import Any

POTENTIALLY_BREAKING = "potentially_breaking"
NON_BREAKING = "non_breaking_under_rules"
DOCUMENTATION_ONLY = "documentation_only"
UNCERTAIN = "uncertain"
CATEGORIES = (POTENTIALLY_BREAKING, NON_BREAKING, DOCUMENTATION_ONLY, UNCERTAIN)

# Whether a change affects what a client sends, what it receives, or neither (for example a whole operation).
REQUEST, RESPONSE, NO_DIRECTION = "request", "response", "none"
DIRECTIONS = (REQUEST, RESPONSE, NO_DIRECTION)

ADDED, REMOVED, CHANGED = "added", "removed", "changed"
CHANGE_KINDS = (ADDED, REMOVED, CHANGED)


@dataclass(frozen=True)
class SourceLocation:
    """Where a value sits: the document it was read from and an RFC 6901 JSON Pointer inside it."""

    document_url: str
    pointer: str


@dataclass(frozen=True)
class ContractFinding:
    """One difference between the contracts. Absence is `*_present=False`, never a null value."""

    key: str  # deterministic and free of timestamps, so repeated checks can recognise the same finding
    rule_id: str
    category: str
    change_kind: str
    direction: str
    method: str | None
    path: str | None  # as declared on the side where it exists (the after side if on both)
    before_present: bool
    after_present: bool
    explanation: str
    before_value: Any = None
    after_value: Any = None
    before_location: SourceLocation | None = None
    after_location: SourceLocation | None = None
    subject: str | None = None  # a short human label for what changed, such as "request body (application/json) > age"

    def __post_init__(self):
        if self.category not in CATEGORIES:
            raise ValueError(f"Unknown category {self.category!r}.")
        if self.change_kind not in CHANGE_KINDS:
            raise ValueError(f"Unknown change kind {self.change_kind!r}.")
        if self.direction not in DIRECTIONS:
            raise ValueError(f"Unknown direction {self.direction!r}.")
        if not (self.before_present or self.after_present):
            raise ValueError("A finding must be present on at least one side.")


@dataclass(frozen=True)
class ComparisonLimitation:
    """Something the comparison could not assess or could not trust. `blocks_classification` marks a scope that
    produced no confident finding because of it."""

    code: str
    explanation: str
    scope: str | None = None
    blocks_classification: bool = False


@dataclass(frozen=True)
class ComparisonCoverage:
    """What was assessed. `complete` is False whenever any part of a contract was not compared, so an empty
    finding list must never be read as "unchanged"."""

    assessed: tuple[str, ...]
    not_assessed: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.not_assessed


@dataclass(frozen=True)
class ComparisonResult:
    findings: tuple[ContractFinding, ...]
    limitations: tuple[ComparisonLimitation, ...]
    coverage: ComparisonCoverage
    ruleset_version: str
