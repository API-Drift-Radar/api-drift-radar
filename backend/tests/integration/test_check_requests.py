"""Storage for issue #12: queued check requests, and starting a check atomically."""

import sqlite3
import threading

import pytest

from radar.storage.errors import CheckInProgress, ConflictError, NotFoundError
from radar.storage.records import MANUAL, RUNNING, SCHEDULED
from radar.storage.repository import Store


@pytest.fixture
def data(tmp_path):
    return tmp_path / "radar-data"


@pytest.fixture
def source(data):
    with Store(data) as store:
        return store.add_source("https://api.acme.com").id


def test_a_request_reserves_the_id_its_check_will_have(data, source):
    with Store(data) as store:
        request = store.request_check(source, MANUAL)
        assert request.pending and request.kind == MANUAL
        with pytest.raises(NotFoundError):
            store.get_check(request.check_id)  # not started yet
        check = store.start_check(source, check_id=request.check_id)
    with Store(data) as store:
        assert check.id == request.check_id and store.get_check(check.id).status == RUNNING
        started = store.get_check_request(request.check_id)
        assert not started.pending and started.started_at == check.started_at
        assert store.list_pending_requests() == []


def test_asking_again_while_a_request_waits_returns_the_same_request(data, source):
    with Store(data) as store:
        first = store.request_check(source, SCHEDULED)
        second = store.request_check(source, MANUAL)
        assert second == first and [r.check_id for r in store.list_pending_requests()] == [first.check_id]


def test_a_request_while_a_check_runs_is_refused_with_the_running_check(data, source):
    with Store(data) as store:
        running = store.start_check(source)
        with pytest.raises(CheckInProgress) as refused:
            store.request_check(source)
        assert refused.value.check_id == running.id and store.list_pending_requests() == []


def test_start_check_refuses_a_second_running_check_of_the_same_source(data, source):
    with Store(data) as store:
        first = store.start_check(source)
        with pytest.raises(CheckInProgress) as refused:
            store.start_check(source)
        assert refused.value.check_id == first.id
        other = store.add_source("https://api.other.com").id
        assert store.start_check(other).status == RUNNING  # other sources are independent


def test_concurrent_starts_from_separate_stores_start_exactly_one(data, source):
    barrier, outcomes = threading.Barrier(8), []

    def start():
        with Store(data) as store:
            barrier.wait()
            try:
                outcomes.append(store.start_check(source).id)
            except CheckInProgress as busy:
                outcomes.append(("busy", busy.check_id))
    threads = [threading.Thread(target=start) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    started = [o for o in outcomes if isinstance(o, str)]
    assert len(started) == 1 and all(o == ("busy", started[0]) for o in outcomes if o != started[0])


def test_a_direct_start_fulfils_a_waiting_request(data, source):
    with Store(data) as store:
        request = store.request_check(source)
        check = store.start_check(source)
        assert check.id == request.check_id and store.list_pending_requests() == []


def test_a_request_starts_once_and_only_for_its_source(data, source):
    with Store(data) as store:
        other = store.add_source("https://api.other.com").id
        request = store.request_check(source)
        with pytest.raises(ValueError):
            store.start_check(other, check_id=request.check_id)
        check = store.start_check(source, check_id=request.check_id)
        store.fail_check(check.id, stage="fetch", reason="inaccessible", error="down")
        with pytest.raises(ConflictError):
            store.start_check(source, check_id=request.check_id)


def test_requests_are_validated_and_unknown_ids_are_not_found(data, source):
    with Store(data) as store:
        with pytest.raises(ValueError):
            store.request_check(source, "cron")
        with pytest.raises(NotFoundError):
            store.request_check("src_" + "0" * 32)
        with pytest.raises(NotFoundError):
            store.get_check_request("not-an-id")


def test_requests_are_append_only_in_the_database(data, source):
    with Store(data) as store:
        request = store.request_check(source)
    db = sqlite3.connect(data / "radar.db")
    with pytest.raises(sqlite3.DatabaseError, match="cannot be deleted"):
        db.execute("DELETE FROM check_requests")
    with pytest.raises(sqlite3.DatabaseError, match="keeps its id"):
        db.execute("UPDATE check_requests SET kind = 'scheduled'")
    with pytest.raises(sqlite3.DatabaseError, match="must have its check run"):
        db.execute("UPDATE check_requests SET started_at = requested_at WHERE check_id = ?", (request.check_id,))
    db.close()
