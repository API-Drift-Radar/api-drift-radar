"""Issue #12: manual and scheduled checks, run against a real Store with discovery replaced by test doubles.

Every result is read back through a newly opened store, as the API would.
"""

from datetime import timedelta
import threading

import pytest

from radar.comparison.openapi.evaluation import build_package
from radar.comparison.openapi.stored import compare_stored
from radar.domain.discovery import DiscoveryAttempt, DiscoveryOutcome, DiscoveryStatus
from radar.monitoring.checks import MANUAL, SCHEDULED, CheckSkipped, run_check
from radar.monitoring.retry import NO_RETRY, RetryPolicy
from radar.monitoring.schedule import close_interrupted, due_sources, next_due
from radar.storage.identifiers import utc_now
from radar.storage.records import (AMBIGUOUS, CHANGED, FAILED, INITIAL, RUNNING, UNCHANGED, UNCONFIRMED)
from radar.storage.repository import Store

LOCATOR = "https://api.acme.com"
URL = "https://api.acme.com/openapi.json"


def pets(version=1):
    contract = {
        "openapi": "3.0.3", "info": {"title": "Acme", "version": "1"},
        "paths": {"/pets": {"get": {"responses": {"200": {"description": "ok"}}}},
                  "/pets/{id}": {"delete": {"responses": {"204": {"description": "gone"}}}}}}
    if version == 2:
        del contract["paths"]["/pets/{id}"]
    return contract


def validated(version=1):
    return DiscoveryOutcome(DiscoveryStatus.VALIDATED, package=build_package(pets(version)))


def inaccessible(*reasons):
    attempts = tuple(DiscoveryAttempt(URL, "common_location", "inaccessible", r) for r in reasons)
    return DiscoveryOutcome(DiscoveryStatus.INACCESSIBLE, attempts=attempts)


TIMEOUT = "timeout: Connection or read timed out."


def sequence(*outcomes):
    """A discovery double returning the given outcomes in turn (the last one repeats)."""
    remaining = list(outcomes)

    def fake(source):
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]
    return fake


class Sleeps(list):
    def __call__(self, seconds):
        self.append(seconds)


@pytest.fixture
def data(tmp_path):
    return tmp_path / "radar-data"


@pytest.fixture
def source(data):
    with Store(data) as store:
        return store.add_source(LOCATOR, {"interval_minutes": 60}).id


def check(data, source, trigger=MANUAL, **options):
    options.setdefault("sleep", Sleeps())
    with Store(data) as store:
        return run_check(store, source, trigger, **options)


def read(data, check_id):
    with Store(data) as store:
        return store.get_check(check_id)


def reference(data, source):
    with Store(data) as store:
        latest = store.latest_snapshot(source)
        return latest.id if latest else None


# Acceptance: manual and scheduled triggers invoke the same check workflow.

def test_manual_and_scheduled_triggers_run_the_same_workflow_and_record_their_trigger(data, source):
    first = check(data, source, MANUAL, discover=sequence(validated(1)))
    second = check(data, source, SCHEDULED, discover=sequence(validated(1)))
    assert (read(data, first.id).status, read(data, second.id).status) == (INITIAL, UNCHANGED)
    assert read(data, first.id).detail["trigger"] == MANUAL
    assert read(data, second.id).detail["trigger"] == SCHEDULED


def test_an_unknown_trigger_is_refused_before_anything_is_recorded(data, source):
    with pytest.raises(ValueError):
        check(data, source, "cron", discover=sequence(validated(1)))
    with Store(data) as store:
        assert store.list_checks(source) == []


# Acceptance: initial snapshot, later comparisons, changed and unchanged captures.

def test_the_first_valid_capture_establishes_the_initial_snapshot(data, source):
    result = check(data, source, discover=sequence(validated(1)))
    stored = read(data, result.id)
    assert stored.status == INITIAL and stored.before_snapshot_id is None
    assert reference(data, source) == stored.snapshot_id


def test_a_changed_capture_persists_findings_and_advances_the_reference(data, source):
    first = check(data, source, discover=sequence(validated(1)))
    second = check(data, source, discover=sequence(validated(2)))
    stored = read(data, second.id)
    assert stored.status == CHANGED and stored.before_snapshot_id == first.snapshot_id
    assert reference(data, source) == stored.snapshot_id
    with Store(data) as store:
        changes = store.list_changes(check_id=second.id)
    assert [c.change_type for c in changes] == ["operation_removed"]
    assert changes[0].before_snapshot_id == first.snapshot_id and changes[0].after_snapshot_id == stored.snapshot_id


def test_later_captures_compare_against_the_previous_valid_snapshot_and_unchanged_adds_no_findings(data, source):
    check(data, source, discover=sequence(validated(1)))
    changed = check(data, source, discover=sequence(validated(2)))
    unchanged = check(data, source, discover=sequence(validated(2)))
    stored = read(data, unchanged.id)
    assert stored.status == UNCHANGED and stored.before_snapshot_id == changed.snapshot_id
    with Store(data) as store:
        assert store.list_changes(check_id=unchanged.id) == []
        assert len(store.list_changes(source_id=source)) == 1  # the finding was recorded once, by the changed check


# Acceptance: failures preserve the previous valid snapshot and record the stage.

@pytest.mark.parametrize("outcome, stage, reason", [
    (inaccessible("http_error: HTTP status 403."), "fetch", "inaccessible"),
    (DiscoveryOutcome(DiscoveryStatus.REJECTED), "validation", "rejected"),
    (DiscoveryOutcome(DiscoveryStatus.NOT_FOUND), "discovery", "not_found"),
])
def test_discovery_failures_keep_the_reference_and_record_their_stage(data, source, outcome, stage, reason):
    first = check(data, source, discover=sequence(validated(1)))
    failed = read(data, check(data, source, discover=sequence(outcome)).id)
    assert (failed.status, failed.failure_stage, failed.failure_reason) == (FAILED, stage, reason)
    assert failed.before_snapshot_id == first.snapshot_id and reference(data, source) == first.snapshot_id


def test_a_comparison_failure_keeps_the_reference(data, source):
    first = check(data, source, discover=sequence(validated(1)))

    def broken(*args, **kwargs):
        raise ValueError("cannot read the reference")
    failed = read(data, check(data, source, discover=sequence(validated(2)), compare=broken).id)
    assert (failed.status, failed.failure_stage) == (FAILED, "comparison")
    assert reference(data, source) == first.snapshot_id


def test_a_normalization_failure_keeps_the_reference(data, source):
    first = check(data, source, discover=sequence(validated(1)))

    def broken(package):
        raise ValueError("not normalizable")
    failed = read(data, check(data, source, discover=sequence(validated(2)), normalize=broken).id)
    assert (failed.status, failed.failure_stage) == (FAILED, "normalization")
    assert reference(data, source) == first.snapshot_id


@pytest.mark.parametrize("status, kind", [(DiscoveryStatus.AMBIGUOUS, AMBIGUOUS),
                                          (DiscoveryStatus.UNCONFIRMED, UNCONFIRMED)])
def test_ambiguous_and_unconfirmed_discovery_are_kept_for_review_without_a_snapshot(data, source, status, kind):
    if status is DiscoveryStatus.AMBIGUOUS:
        outcome = DiscoveryOutcome(status, packages=(build_package(pets(1)), build_package(pets(2))))
    else:
        outcome = DiscoveryOutcome(status, package=build_package(pets(1), confirmed=False))
    stored = read(data, check(data, source, discover=sequence(outcome)).id)
    assert (stored.status, stored.failure_stage, stored.snapshot_id) == (kind, "discovery", None)
    assert "discovery" in stored.detail and reference(data, source) is None


def test_an_unexpected_error_closes_the_check_before_it_is_raised(data, source):
    def crash(source):
        raise RuntimeError("bug")
    with pytest.raises(RuntimeError):
        check(data, source, discover=crash)
    with Store(data) as store:
        (stored,) = store.list_checks(source)
        assert (stored.status, stored.failure_reason) == (FAILED, "unexpected_error")
        assert store.list_running_checks() == []


# Acceptance: timeouts, bounded retries and provider rate limits have defined behavior.

def test_a_transient_failure_is_retried_with_backoff_and_then_succeeds(data, source):
    sleeps = Sleeps()
    result = check(data, source, discover=sequence(inaccessible(TIMEOUT), inaccessible("http_error: HTTP status 503."),
                                                   validated(1)), sleep=sleeps)
    stored = read(data, result.id)
    assert stored.status == INITIAL and stored.detail["discovery_attempts"] == 3
    assert sleeps == [5.0, 10.0] and len(stored.detail["retries"]) == 2


def test_retries_are_bounded_and_the_last_failure_is_recorded(data, source):
    sleeps = Sleeps()
    stored = read(data, check(data, source, discover=sequence(inaccessible(TIMEOUT)), sleep=sleeps).id)
    assert (stored.status, stored.failure_stage, stored.failure_reason) == (FAILED, "fetch", "inaccessible")
    assert stored.detail["discovery_attempts"] == 3 and len(sleeps) == 2


def test_a_rate_limited_source_waits_longer_and_is_recorded_as_rate_limited(data, source):
    sleeps = Sleeps()
    stored = read(data, check(data, source, discover=sequence(inaccessible("http_error: HTTP status 429.")),
                              sleep=sleeps).id)
    assert sleeps == [60.0, 60.0] and stored.failure_reason == "rate_limited"


def test_a_permanent_failure_is_not_retried(data, source):
    sleeps = Sleeps()
    # One source timed out but another refused outright: waiting will not help the refusal.
    stored = read(data, check(data, source, discover=sequence(inaccessible(TIMEOUT, "tls_error: TLS failed.")),
                              sleep=sleeps).id)
    assert stored.detail["discovery_attempts"] == 1 and sleeps == []


def test_retries_can_be_turned_off(data, source):
    stored = read(data, check(data, source, discover=sequence(inaccessible(TIMEOUT)), retry=NO_RETRY).id)
    assert stored.detail["discovery_attempts"] == 1


def test_retry_policy_rejects_nonsense():
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(base_delay=-1)


# Acceptance: overlapping checks cannot corrupt history or advance the reference inconsistently.

def test_a_trigger_while_a_check_of_the_same_source_is_running_is_skipped(data, source):
    with Store(data) as store:
        running = store.begin_check(source)  # another process's check, still in progress
    result = check(data, source, SCHEDULED, discover=sequence(validated(1)))
    assert result == CheckSkipped(source, SCHEDULED, running.id)
    with Store(data) as store:
        assert [c.id for c in store.list_checks(source)] == [running.id]


def test_concurrent_triggers_in_one_process_run_one_check(data, source):
    started, release = threading.Event(), threading.Event()

    def slow(source):
        started.set()
        release.wait(5)
        return validated(1)
    results = {}
    worker = threading.Thread(target=lambda: results.setdefault("first", check(data, source, discover=slow)))
    worker.start()
    assert started.wait(5)
    results["second"] = check(data, source, SCHEDULED, discover=sequence(validated(1)))
    release.set()
    worker.join(5)
    assert isinstance(results["second"], CheckSkipped)
    assert read(data, results["first"].id).status == INITIAL
    with Store(data) as store:
        assert len(store.list_checks(source)) == 1


def test_a_reference_moved_during_comparison_is_compared_again(data, source):
    first = check(data, source, discover=sequence(validated(1)))
    moved = {}

    def compare_while_another_check_saves(reference, normalized, version, **kwargs):
        if not moved:  # another check of this source saves version 2 while we compare
            with Store(data) as other:
                running = other.begin_check(source)
                from radar.comparison.openapi.stored import normalize_contract
                package = build_package(pets(2))
                body, ver = normalize_contract(package)
                changes, limits = compare_stored(other.read_contract(first.snapshot_id), body, ver)
                moved["check"] = other.complete_check(
                    running.id, outcome=CHANGED, package=package, normalized=body, normalization_version=ver,
                    before_snapshot_id=first.snapshot_id, changes=changes, limitations=limits)
        return compare_stored(reference, normalized, version, **kwargs)

    result = check(data, source, discover=sequence(validated(2)), compare=compare_while_another_check_saves)
    stored = read(data, result.id)
    # Compared again against the other check's snapshot, which already has version 2: unchanged, no duplicates.
    assert stored.status == UNCHANGED and stored.before_snapshot_id == moved["check"].snapshot_id
    assert reference(data, source) == stored.snapshot_id
    with Store(data) as store:
        assert len(store.list_changes(source_id=source)) == 1


# Acceptance: restart and missed-schedule behavior.

def test_after_a_restart_interrupted_checks_are_closed_and_the_source_can_be_checked_again(data, source):
    first = check(data, source, discover=sequence(validated(1)))
    with Store(data) as store:
        store.begin_check(source)  # the process died during this check
    with Store(data) as store:  # Radar starts again
        (closed,) = close_interrupted(store)
    assert (closed.status, closed.failure_stage) == (FAILED, "interrupted")
    assert reference(data, source) == first.snapshot_id
    assert read(data, check(data, source, discover=sequence(validated(1))).id).status == UNCHANGED


def test_a_source_that_missed_several_runs_is_due_once_at_startup(data, source):
    check(data, source, SCHEDULED, discover=sequence(validated(1)))
    later = utc_now() + timedelta(hours=5)  # Radar was down for five intervals
    with Store(data) as store:
        assert [s.id for s in due_sources(store, later)] == [source]
    catch_up = check(data, source, SCHEDULED, discover=sequence(validated(1)))  # the one catch-up run at startup
    with Store(data) as store:
        assert due_sources(store, utc_now()) == []  # not once more per missed slot
        # The next run is one interval after the catch-up, not on the old grid.
        assert next_due(store, store.get_source(source)) == read(data, catch_up.id).started_at + timedelta(minutes=60)


def test_only_scheduled_idle_sources_are_due(data, source):
    with Store(data) as store:
        manual_only = store.add_source("https://api.other.com").id
        never_checked = store.add_source("https://api.third.com", {"interval_minutes": 30}).id
        store.begin_check(source)  # running: not due again while it runs
        due = {s.id for s in due_sources(store, utc_now() + timedelta(days=1))}
    assert due == {never_checked} and manual_only not in due
