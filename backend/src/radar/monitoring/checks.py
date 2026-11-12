"""One check of one source: discover, compare against the last valid snapshot, record the result.

Manual and scheduled triggers both call `run_check`; only the `trigger` recorded with the check differs.

    with Store() as store:
        result = run_check(store, source_id, "manual")

The result is the finished `CheckRun`, or `CheckSkipped` when a check of that source was already running.
Every check that starts is closed exactly once, as `initial`, `unchanged`, `changed` or a failure with its stage;
a failure never moves the source's reference. Storage (#10) refuses inconsistent results, so this module only
has to choose the right call.
"""

from dataclasses import dataclass
import logging
import threading
import time

from radar.comparison.openapi.stored import compare_stored, normalize_contract
from radar.discovery.evaluation import package_fingerprint
from radar.discovery.input import DiscoveryInputError
from radar.discovery.orchestrator import discover
from radar.discovery.serialization import outcome_to_dict
from radar.domain.discovery import DiscoveryRequest, DiscoveryStatus
from radar.monitoring.retry import RetryPolicy, classify
from radar.storage.errors import ConflictError, StoreError
from radar.storage.records import AMBIGUOUS, CHANGED, INITIAL, UNCHANGED, UNCONFIRMED

log = logging.getLogger(__name__)

MANUAL, SCHEDULED = "manual", "scheduled"
TRIGGERS = (MANUAL, SCHEDULED)
# How often to compare again when another check moved the reference while this one was comparing.
MAX_REFERENCE_ATTEMPTS = 3
# Where a discovery outcome that produced no contract stopped, and the reason recorded with it.
_DISCOVERY_FAILURES = {
    DiscoveryStatus.INACCESSIBLE: ("fetch", "inaccessible"),
    DiscoveryStatus.REJECTED: ("validation", "rejected"),
    DiscoveryStatus.NOT_FOUND: ("discovery", "not_found"),
}
# Source configuration keys passed to discovery as matching hints.
_REQUEST_HINTS = ("method", "api_version", "product")


@dataclass(frozen=True)
class CheckSkipped:
    """No check was started because one of the same source was already running."""

    source_id: str
    trigger: str
    running_check_id: str | None  # None when the other check is in this process and has not been saved yet
    reason: str = "already_running"


def discover_source(source):
    """Discovery for a stored source: its locator, plus any matching hints in its configuration."""
    hints = {key: source.config[key] for key in _REQUEST_HINTS if source.config.get(key)}
    return discover(DiscoveryRequest(target=source.locator, **hints))


# One lock per source for checks in this process. Checks in other processes are seen through storage.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(source_id):
    with _locks_guard:
        return _locks.setdefault(source_id, threading.Lock())


def _running_check(store, source_id):
    return next((c for c in store.list_running_checks() if c.source_id == source_id), None)


def run_check(store, source_id, trigger, *, discover=discover_source, retry=RetryPolicy(),
              normalize=normalize_contract, compare=compare_stored, sleep=time.sleep):
    """Run one check of `source_id` and record it. Returns the finished `CheckRun`, or `CheckSkipped`.

    `discover`, `normalize`, `compare` and `sleep` are replaceable so tests can run without a network.
    Raises `NotFoundError` for an unknown source and `ValueError` for an unknown trigger; nothing is recorded then.
    """
    if trigger not in TRIGGERS:
        raise ValueError(f"trigger must be one of {TRIGGERS}.")
    source = store.get_source(source_id)
    lock = _lock_for(source_id)
    if not lock.acquire(blocking=False):
        log.info("Skipped %s check of %s: a check is already running in this process.", trigger, source_id)
        return CheckSkipped(source_id, trigger, None)
    try:
        running = _running_check(store, source_id)
        if running is not None:
            log.info("Skipped %s check of %s: %s is still running.", trigger, source_id, running.id)
            return CheckSkipped(source_id, trigger, running.id)
        check = store.begin_check(source_id)
        progress = {"stage": "discovery"}
        try:
            return _run(store, source, check, trigger, progress, discover, retry, normalize, compare, sleep)
        except BaseException as error:
            # Whatever happened, do not leave the check 'running' until the next restart.
            if not store.get_check(check.id).finished:
                store.fail_check(check.id, stage="storage" if isinstance(error, StoreError) else progress["stage"],
                                 reason="unexpected_error", error=f"{type(error).__name__}: {error}",
                                 detail={"trigger": trigger})
            raise
    finally:
        lock.release()


def _run(store, source, check, trigger, progress, discover, retry, normalize, compare, sleep):
    detail = {"trigger": trigger, "discovery_attempts": 0}

    def failed(stage, reason, error, outcome="failed", extra=None):
        return store.fail_check(check.id, stage=stage, reason=reason, outcome=outcome,
                                error=str(error) or type(error).__name__, detail={**detail, **(extra or {})})

    # 1. Discovery, retried while the failure may clear by itself.
    retries = []
    for attempt in range(1, retry.max_attempts + 1):
        detail["discovery_attempts"] = attempt
        try:
            outcome = discover(source)
        except DiscoveryInputError as error:
            return failed("discovery", "invalid_target", error)
        retryable, rate_limited = classify(outcome)
        if not retryable or attempt == retry.max_attempts:
            break
        wait = retry.delay(attempt, rate_limited)
        retries.append({"after_attempt": attempt, "waited_seconds": wait, "rate_limited": rate_limited})
        log.info("Check %s: discovery was %s; retrying in %ss.", check.id, outcome.status.value, wait)
        sleep(wait)
    if retries:
        detail["retries"] = retries

    if outcome.status in (DiscoveryStatus.AMBIGUOUS, DiscoveryStatus.UNCONFIRMED):
        # Kept for a person to choose or confirm; never a snapshot and never a change of reference.
        kind = AMBIGUOUS if outcome.status is DiscoveryStatus.AMBIGUOUS else UNCONFIRMED
        return failed("discovery", outcome.status.value, "Discovery could not settle on one contract for this API.",
                      outcome=kind, extra={"discovery": outcome_to_dict(outcome)})
    if outcome.status is not DiscoveryStatus.VALIDATED:
        stage, reason = _DISCOVERY_FAILURES.get(outcome.status, ("discovery", outcome.status.value))
        if classify(outcome)[1]:
            reason = "rate_limited"
        return failed(stage, reason, f"Discovery ended with {outcome.status.value}.")

    # 2. Normalize the new contract once; it does not depend on the reference.
    package = outcome.package
    progress["stage"] = "normalization"
    try:
        normalized, version = normalize(package)
    except Exception as error:
        return failed("normalization", "normalization_failed", error)

    # 3. Compare with the current reference and save. If another check moved the reference meanwhile,
    #    storage refuses the save (ConflictError) and the comparison is repeated against the new one.
    progress["stage"] = "comparison"
    for _ in range(MAX_REFERENCE_ATTEMPTS):
        before = store.latest_snapshot(source.id)
        if before is None:
            result, changes, limitations = INITIAL, [], []
        else:
            try:
                reference = store.read_contract(before.id)
                changes, limitations = compare(
                    reference, normalized, version,
                    snapshot_documents=store.snapshot_documents(before.id),
                    before_source_kind=before.provenance.get("source_kind", "provider"),
                    after_source_kind=package.candidate.source_kind)
            except StoreError as error:
                return failed("storage", "reference_unreadable", error)
            except Exception as error:
                return failed("comparison", "comparison_failed", error)
            same = reference.normalized == normalized and before.normalization_version == version
            result = UNCHANGED if same else CHANGED
            if same:
                changes = []
        try:
            return store.complete_check(
                check.id, outcome=result, package=package, normalized=normalized, normalization_version=version,
                before_snapshot_id=before.id if before else None, changes=changes, limitations=limitations,
                content_fingerprint=package_fingerprint(package), detail=detail)
        except ConflictError:
            if store.get_check(check.id).finished:
                raise  # closed elsewhere (for example as interrupted by a second runner): nothing more to do
            log.info("Check %s: the reference moved while comparing; comparing again.", check.id)
        except StoreError as error:
            return failed("storage", "storage_failed", error)  # for example a full disk; nothing was saved
    return failed("comparison", "reference_kept_moving",
                  f"The reference changed {MAX_REFERENCE_ATTEMPTS} times while comparing.")
