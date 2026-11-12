"""One check of one source: discover, compare against the last valid snapshot, record the result.

Three entry points, one workflow:

* `run_check(store, source_id, trigger)` runs a check now and returns the finished `CheckRun`. The CLI uses it,
  and the runner uses it for scheduled checks.
* `request_check(store, source_id)` only queues a check and returns its `CheckRequest` at once; the request's
  `check_id` is the id the check will have. The runner (`radar monitor run`) starts it with `run_request`. This is
  how a caller such as the HTTP API (#13) returns a check id while the work continues in the background.
* `run_request(store, request)` runs a queued check.

When a check of the same source is already running, nothing new starts: `run_check` and `request_check` return
`CheckSkipped` naming the running check. Storage tests and starts in one transaction, so this holds across
threads and processes.

Every check that starts is closed exactly once, as `initial`, `unchanged`, `changed` or a failure with its stage;
a failure never moves the source's reference. Storage (#10) refuses inconsistent results, so this module only
has to choose the right call.

Source configuration read here (all optional):

* `contract_url`: the contract a person chose, for example from an ambiguous discovery result. Checks then
  capture that URL and fail, rather than switch, if it no longer yields a valid contract.
* `method`, `api_version`, `product`: matching hints passed to discovery.

Scheduling keys (`interval_minutes`, `paused`) are read by `radar.monitoring.schedule`.
"""

from dataclasses import dataclass
import logging
import time

from radar.comparison.openapi.stored import compare_stored, normalize_contract
from radar.discovery.evaluation import package_fingerprint
from radar.discovery.input import DiscoveryInputError, normalize_target
from radar.discovery.orchestrator import discover
from radar.discovery.serialization import outcome_to_dict
from radar.domain.discovery import DiscoveryRequest, DiscoveryStatus
from radar.monitoring.retry import RetryPolicy, classify
from radar.storage.errors import CheckInProgress, ConflictError, StoreError
from radar.storage.records import (AMBIGUOUS, CHANGED, INITIAL, MANUAL, SCHEDULED, UNCHANGED, UNCONFIRMED)

log = logging.getLogger(__name__)

TRIGGERS = (MANUAL, SCHEDULED)
CONTRACT_URL_KEY = "contract_url"
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
    """No check was started or queued because one of the same source is already running."""

    source_id: str
    trigger: str
    running_check_id: str
    reason: str = "already_running"


def discover_source(source):
    """Discovery for a stored source: its chosen contract URL if it has one, otherwise its locator,
    plus any matching hints in its configuration."""
    hints = {key: source.config[key] for key in _REQUEST_HINTS if source.config.get(key)}
    chosen = source.config.get(CONTRACT_URL_KEY)
    if chosen:
        # A chosen contract is fetched directly; the directory is not consulted for alternatives.
        return discover(DiscoveryRequest(target=chosen, **hints), use_directory=False)
    return discover(DiscoveryRequest(target=source.locator, **hints))


def run_check(store, source_id, trigger, **options):
    """Run one check of `source_id` now and record it. Returns the finished `CheckRun`, or `CheckSkipped`.

    A queued request for the source, if any, is fulfilled by this check (it gets the request's id).
    `options` (`discover`, `retry`, `normalize`, `compare`, `sleep`) replace the real components in tests.
    Raises `NotFoundError` for an unknown source and `ValueError` for an unknown trigger; nothing is recorded then.
    """
    _check_trigger(trigger)
    source = store.get_source(source_id)
    try:
        check = store.start_check(source_id)
    except CheckInProgress as busy:
        log.info("Skipped %s check of %s: %s is still running.", trigger, source_id, busy.check_id)
        return CheckSkipped(source_id, trigger, busy.check_id)
    return _execute(store, source, check, trigger, **options)


def request_check(store, source_id, trigger=MANUAL):
    """Queue a check of `source_id` for the runner. Returns the `CheckRequest` at once, or `CheckSkipped`.

    Asking again while a request waits returns that same request, so the check runs once.
    """
    _check_trigger(trigger)
    try:
        return store.request_check(source_id, trigger)
    except CheckInProgress as busy:
        return CheckSkipped(source_id, trigger, busy.check_id)


def run_request(store, request, **options):
    """Run a queued check. Returns the finished `CheckRun`, or None if a check of the source is running
    right now (the request keeps waiting) or it was already started elsewhere."""
    source = store.get_source(request.source_id)
    try:
        check = store.start_check(request.source_id, check_id=request.check_id)
    except CheckInProgress:
        return None
    except ConflictError:
        return None  # another runner or a direct check already started it
    return _execute(store, source, check, request.kind, **options)


def _check_trigger(trigger):
    if trigger not in TRIGGERS:
        raise ValueError(f"trigger must be one of {TRIGGERS}.")


def _execute(store, source, check, trigger, *, discover=None, retry=None,
             normalize=normalize_contract, compare=compare_stored, sleep=time.sleep):
    discover = discover or discover_source  # looked up per call, so it can be replaced as a whole
    retry = retry or RetryPolicy()
    progress = {"stage": "discovery"}
    try:
        return _run(store, source, check, trigger, progress, discover, retry, normalize, compare, sleep)
    except BaseException as error:
        # Whatever happened, do not leave the check 'running' until the next restart.
        if not store.get_check(check.id).finished:
            if isinstance(error, KeyboardInterrupt):
                stage, reason = "interrupted", "stopped"
            elif isinstance(error, StoreError):
                stage, reason = "storage", "unexpected_error"
            else:
                stage, reason = progress["stage"], "unexpected_error"
            store.fail_check(check.id, stage=stage, reason=reason, error=f"{type(error).__name__}: {error}",
                             detail={"trigger": trigger})
        raise


def _chosen_package(outcome, chosen_url):
    """The package for the chosen contract URL, or None if discovery did not return that contract.

    A chosen contract counts as confirmed: a person selected it, which is the confirmation an unconfirmed
    result waits for.
    """
    try:
        wanted = normalize_target(DiscoveryRequest(target=chosen_url)).normalized_url
    except DiscoveryInputError:
        return None
    if outcome.status in (DiscoveryStatus.VALIDATED, DiscoveryStatus.UNCONFIRMED) and outcome.package is not None:
        packages = (outcome.package,)
    else:
        packages = outcome.packages or ()
    for package in packages:
        if wanted in (package.candidate.source_url, package.root_document.source_url):
            return package
    return None


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

    chosen_url = source.config.get(CONTRACT_URL_KEY)
    if chosen_url:
        package = _chosen_package(outcome, chosen_url)
        if package is None and outcome.status in (DiscoveryStatus.VALIDATED, DiscoveryStatus.UNCONFIRMED,
                                                  DiscoveryStatus.AMBIGUOUS):
            # Discovery found something, but not the chosen contract: never switch to it silently.
            return failed("fetch", "chosen_contract_unavailable",
                          f"The chosen contract {chosen_url} was not returned; discovery ended with "
                          f"{outcome.status.value}.", extra={"discovery": outcome_to_dict(outcome)})
    else:
        package = outcome.package if outcome.status is DiscoveryStatus.VALIDATED else None
        if outcome.status in (DiscoveryStatus.AMBIGUOUS, DiscoveryStatus.UNCONFIRMED):
            # Kept for a person to choose or confirm; never a snapshot and never a change of reference.
            kind = AMBIGUOUS if outcome.status is DiscoveryStatus.AMBIGUOUS else UNCONFIRMED
            return failed("discovery", outcome.status.value,
                          "Discovery could not settle on one contract for this API.",
                          outcome=kind, extra={"discovery": outcome_to_dict(outcome)})
    if package is None:
        stage, reason = _DISCOVERY_FAILURES.get(outcome.status, ("discovery", outcome.status.value))
        if classify(outcome)[1]:
            reason = "rate_limited"
        return failed(stage, reason, f"Discovery ended with {outcome.status.value}.")

    # 2. Normalize the new contract once; it does not depend on the reference.
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
