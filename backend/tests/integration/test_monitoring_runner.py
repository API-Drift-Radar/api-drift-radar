"""Issue #12: the runner (queued and scheduled checks, the runner lock, restarts) and `radar monitor` commands.

Discovery is replaced by a test double, so nothing here uses the network. The restart test uses a real second
process that is killed in the middle of a check.
"""

from datetime import timedelta
import os
import subprocess
import sys
import textwrap
import threading

import pytest

from radar.cli.main import main as radar
from radar.comparison.openapi.evaluation import build_package
from radar.domain.discovery import DiscoveryOutcome, DiscoveryStatus
from radar.monitoring import checks
from radar.monitoring.checks import request_check, run_check
from radar.monitoring.runner import RunnerBusy, RunnerLock, executing, run_forever, tick
from radar.storage.identifiers import utc_now
from radar.storage.records import CHANGED, FAILED, INITIAL, SCHEDULED, UNCHANGED
from radar.storage.repository import Store


def contract(paths=("/pets",)):
    return {"openapi": "3.0.3", "info": {"title": "Acme", "version": "1"},
            "paths": {p: {"get": {"responses": {"200": {"description": "ok"}}}} for p in paths}}


class FakeDiscovery:
    """Returns the current contract for every source; set `.paths` to change what the API publishes."""

    def __init__(self):
        self.paths = ("/pets",)
        self.calls = []

    def __call__(self, source):
        self.calls.append(source.id)
        return DiscoveryOutcome(DiscoveryStatus.VALIDATED, package=build_package(contract(self.paths)))


@pytest.fixture
def data(tmp_path):
    return tmp_path / "radar-data"


@pytest.fixture
def api(monkeypatch):
    fake = FakeDiscovery()
    monkeypatch.setattr(checks, "discover_source", fake)
    return fake


def add(data, locator="https://api.acme.com", **config):
    with Store(data) as store:
        return store.add_source(locator, config).id


# The runner lock

def test_only_one_process_or_object_holds_the_runner_lock(data):
    first, second = RunnerLock(data), RunnerLock(data)
    assert first.acquire() and not second.acquire()
    with pytest.raises(RunnerBusy):
        with executing(data, lock_wait_seconds=0):
            pass
    first.release()
    assert second.acquire()
    second.release()


# One round of the runner

def test_a_round_runs_queued_requests_first_then_due_scheduled_sources(data, api):
    scheduled = add(data, interval_minutes=60)
    manual_only = add(data, "https://api.manual.com")
    with Store(data) as store:
        queued = request_check(store, manual_only)
    with executing(data) as store:
        results = tick(store)
    assert [r.source_id for r in results] == [manual_only, scheduled]
    with Store(data) as store:
        assert store.get_check(queued.check_id).status == INITIAL
        assert store.list_checks(scheduled)[0].detail["trigger"] == SCHEDULED
        assert tick(store) == []  # nothing queued, nothing due until the interval passes
        later = utc_now() + timedelta(minutes=61)
        assert [r.source_id for r in tick(store, later)] == [scheduled]


def test_a_crashing_check_is_recorded_and_the_round_continues(data, api, monkeypatch):
    broken, healthy = add(data, interval_minutes=5), add(data, "https://api.ok.com", interval_minutes=5)

    def discover(source):
        if source.id == broken:
            raise RuntimeError("bug in discovery")
        return api(source)
    monkeypatch.setattr(checks, "discover_source", discover)
    with executing(data) as store:
        results = tick(store)
        assert [r.source_id for r in results] == [healthy]
        assert store.list_checks(broken)[0].failure_reason == "unexpected_error"


def test_the_runner_stops_between_checks_when_asked(data, api):
    add(data, interval_minutes=5)
    stop = threading.Event()
    seen = []
    worker = threading.Thread(target=run_forever, args=(data,),
                              kwargs={"poll_seconds": 0.05, "stop": stop, "on_result": seen.append})
    worker.start()
    for _ in range(100):
        if seen:
            break
        stop.wait(0.05)
    stop.set()
    worker.join(5)
    assert not worker.is_alive() and [r.status for r in seen] == [INITIAL]
    assert RunnerLock(data).acquire()  # released on exit


# Restart: a real process dies mid-check

CRASHING_RUNNER = textwrap.dedent("""
    import os, sys
    from radar.monitoring.runner import RunnerLock
    from radar.storage.repository import Store
    data, source = sys.argv[1], sys.argv[2]
    lock = RunnerLock(data)
    assert lock.acquire()
    with Store(data) as store:
        print(store.start_check(source).id, flush=True)
    sys.stdin.readline()   # stays in the middle of the check until told to crash
    os._exit(1)            # dies without any cleanup: the check stays running, the lock is not released by code
""")


def test_after_a_crash_the_lock_is_free_the_check_is_closed_and_monitoring_resumes(data, api):
    source = add(data, interval_minutes=60)
    with Store(data) as store:
        first = run_check(store, source, SCHEDULED)
    process = subprocess.Popen([sys.executable, "-c", CRASHING_RUNNER, str(data), source],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        interrupted_id = process.stdout.readline().strip()
        assert not RunnerLock(data).acquire()  # the runner is alive and holds the lock
        with Store(data) as store:
            assert request_check(store, source).running_check_id == interrupted_id
    finally:
        # The crash. Closing stdin makes the child call os._exit; kill() alone is not enough on Windows, where a
        # virtual environment's python.exe is a launcher that runs the real interpreter as a separate process.
        process.stdin.close()
        try:
            process.wait(20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(10)
        process.stdout.close()

    with executing(data) as store:  # Radar starts again
        closed = store.get_check(interrupted_id)
        assert (closed.status, closed.failure_stage, closed.failure_reason) == (FAILED, "interrupted", "restarted")
        assert store.latest_snapshot(source).id == first.snapshot_id  # the reference survived
        later = utc_now() + timedelta(minutes=61)
        (resumed,) = tick(store, later)
        assert resumed.status == UNCHANGED and resumed.before_snapshot_id == first.snapshot_id


# radar monitor ...

def monitor(data, *arguments):
    return radar(["monitor", "--data-dir", str(data), *arguments])


def test_monitor_commands_add_check_detect_a_change_and_show_history(data, api, capsys):
    assert monitor(data, "add", "https://api.acme.com", "--every", "60") == 0
    source = capsys.readouterr().out.split()[1]
    assert monitor(data, "check", source) == 0 and " initial " in capsys.readouterr().out
    api.paths = ("/pets", "/owners")
    assert monitor(data, "check", source) == 1  # exit code 1: changes found
    output = capsys.readouterr().out
    assert " changed " in output and "[non_breaking] GET /owners" in output
    assert monitor(data, "history", source) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[2] for line in lines] == [CHANGED, INITIAL]


def test_monitor_check_queue_returns_an_id_and_status_follows_it(data, api, capsys):
    source = add(data)
    assert monitor(data, "check", source, "--queue") == 0
    check_id = capsys.readouterr().out.split()[1].rstrip(".")
    assert monitor(data, "status", check_id) == 0 and "queued (manual)" in capsys.readouterr().out
    with executing(data) as store:
        tick(store)
    assert monitor(data, "status", check_id) == 0 and " initial " in capsys.readouterr().out


def test_monitor_check_waits_for_an_active_runner_instead_of_running_beside_it(data, api, capsys):
    source = add(data)
    stop = threading.Event()
    runner = threading.Thread(target=run_forever, args=(data,), kwargs={"poll_seconds": 0.05, "stop": stop})
    runner.start()
    try:
        lock_file = RunnerLock(data).path
        for _ in range(200):  # wait until the runner holds the lock (it writes this process's id into the file)
            if lock_file.exists() and lock_file.read_text() == str(os.getpid()):
                break
            stop.wait(0.02)
        assert monitor(data, "check", source) == 0
    finally:
        stop.set()
        runner.join(5)
    captured = capsys.readouterr()
    assert "waiting for the runner" in captured.err and " initial " in captured.out


def test_monitor_pause_resume_and_choose_update_the_source(data, capsys):
    source = add(data, interval_minutes=30)
    assert monitor(data, "pause", source) == 0
    assert monitor(data, "list") == 0 and "[paused]" in capsys.readouterr().out
    assert monitor(data, "resume", source) == 0
    assert monitor(data, "choose", source, "https://api.acme.com/openapi.json") == 0
    with Store(data) as store:
        assert store.get_source(source).config == {"interval_minutes": 30,
                                                   "contract_url": "https://api.acme.com/openapi.json"}


def test_monitor_reports_unknown_records_as_errors(data, capsys):
    assert monitor(data, "check", "src_" + "0" * 32) == 2
    assert monitor(data, "status", "chk_" + "0" * 32) == 2
    assert "error:" in capsys.readouterr().err


def test_monitor_check_takes_over_when_the_runner_stops_while_it_waits(data, api, capsys, monkeypatch):
    from radar.cli import monitor as commands

    source = add(data)
    held = RunnerLock(data)
    assert held.acquire()  # a runner that will stop without running the queued check
    real_sleep = commands.time.sleep

    def runner_goes_away(seconds):
        held.release()
        real_sleep(0)
    monkeypatch.setattr(commands.time, "sleep", runner_goes_away)
    assert monitor(data, "check", source) == 0
    assert " initial " in capsys.readouterr().out


def test_a_starting_runner_waits_briefly_for_a_lock_that_is_being_released(data):
    # Windows frees a dead process's lock asynchronously; a runner restarted right away must not give up.
    held = RunnerLock(data)
    assert held.acquire()
    threading.Timer(0.3, held.release).start()
    with executing(data, lock_wait_seconds=5) as store:
        assert store.list_running_checks() == []
