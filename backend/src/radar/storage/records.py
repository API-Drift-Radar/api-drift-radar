"""Records returned by the storage component, and the vocabulary they use.

Every record is a frozen dataclass built fresh from the database on each
call, so callers can hold or pass them around without affecting stored
state. Mapping and list fields (``config``, ``detail`` and so on) are new
objects each time for the same reason.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

# Check outcomes. A check is RUNNING until it is finished exactly once.
RUNNING = "running"
INITIAL = "initial"  # First successful capture: establishes the reference.
UNCHANGED = "unchanged"  # Same normalized content as the reference.
CHANGED = "changed"  # Different content; changes were recorded.
FAILED = "failed"  # The check could not produce a valid snapshot.
AMBIGUOUS = "ambiguous"  # Discovery found several contracts and none was chosen.

SUCCESS_OUTCOMES = frozenset({INITIAL, UNCHANGED, CHANGED})
FAILURE_OUTCOMES = frozenset({FAILED, AMBIGUOUS})

# Where a failed check stopped. INTERRUPTED is for a check found still
# running after Radar restarted, which the execution component may close.
FAILURE_STAGES = frozenset(
    {
        "discovery",
        "fetch",
        "capture",
        "validation",
        "normalization",
        "comparison",
        "storage",
        "interrupted",
    }
)

# The four agreed change classifications.
POTENTIALLY_BREAKING = "potentially_breaking"
NON_BREAKING = "non_breaking"
DOCUMENTATION_ONLY = "documentation_only"
UNCERTAIN = "uncertain"
CLASSIFICATIONS = frozenset(
    {POTENTIALLY_BREAKING, NON_BREAKING, DOCUMENTATION_ONLY, UNCERTAIN}
)


@dataclass(frozen=True)
class Source:
    """An API or published contract that Radar monitors."""

    id: str
    locator: str
    config: Mapping[str, Any]
    created_at: datetime
    latest_snapshot_id: Optional[str]


@dataclass(frozen=True)
class CheckRun:
    """One attempt to check a source, whatever its outcome."""

    id: str
    source_id: str
    status: str
    started_at: datetime
    finished_at: Optional[datetime]
    before_snapshot_id: Optional[str]  # the reference when the check finished
    snapshot_id: Optional[str]  # the snapshot this check captured, if any
    failure_stage: Optional[str]
    failure_reason: Optional[str]
    error: Optional[str]
    detail: Mapping[str, Any]
    limitations: Sequence[Any]

    @property
    def finished(self):
        return self.status != RUNNING

    @property
    def succeeded(self):
        return self.status in SUCCESS_OUTCOMES


@dataclass(frozen=True)
class Snapshot:
    """One successful capture of a source's contract."""

    id: str
    source_id: str
    fingerprint: str  # identifies the stored contract bundle
    captured_at: datetime
    normalization_version: str
    openapi_version: Optional[str]
    content_fingerprint: Optional[str]
    media_type: Optional[str]
    provenance: Mapping[str, Any]


@dataclass(frozen=True)
class SnapshotDocument:
    """Where one document of a snapshot came from, and where it is stored."""

    snapshot_id: str
    source_url: str
    path: str
    retrieved_at: datetime
    media_type: Optional[str]
    is_root: bool


@dataclass(frozen=True)
class ChangeRecord:
    """One difference found by a changed check, linked to both snapshots."""

    id: str
    check_id: str
    source_id: str
    before_snapshot_id: str
    after_snapshot_id: str
    ordinal: int
    change_type: str
    location: str
    direction: Optional[str]
    classification: str
    summary: str
    rationale: Optional[str]
    before_value: Any
    after_value: Any
    details: Mapping[str, Any]
