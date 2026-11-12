"""The runner: the one process per data folder that executes checks.

    radar monitor run       # until Ctrl+C

On start it takes the data folder's runner lock and closes checks a previous process left running
(`interrupted`). Then, every `poll_seconds`, it runs queued requests (oldest first) and then scheduled sources
that are due. Ctrl+C stops it after the check in progress finishes; a second Ctrl+C stops at once, and that
check is recorded as `interrupted`.

The runner lock is an operating-system lock on `<data folder>/runner.lock`, released automatically when the
process ends, even if it crashes. Anything that executes checks holds it, so a check found `running` at startup
belongs to a process that is gone. `radar monitor check` takes the lock itself when no runner is active, and
otherwise queues the check for the runner and waits for it.
"""

from contextlib import contextmanager
import logging
import os
from pathlib import Path
import threading

from radar import config
from radar.monitoring.checks import run_check, run_request
from radar.monitoring.schedule import close_interrupted, due_sources
from radar.storage.errors import StoreError
from radar.storage.identifiers import utc_now
from radar.storage.records import SCHEDULED
from radar.storage.repository import Store

log = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 30
LOCK_NAME = "runner.lock"


class RunnerBusy(RuntimeError):
    """Another process holds the data folder's runner lock."""


class RunnerLock:
    """An exclusive, non-blocking lock on `<data folder>/runner.lock`."""

    def __init__(self, data_directory=None):
        directory = Path(data_directory) if data_directory is not None else config.DATA_DIRECTORY
        self.path = directory / LOCK_NAME
        self._file = None

    def acquire(self):
        """Take the lock; return False (without waiting) if another process holds it."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            _lock(handle)
        except OSError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode())
        handle.flush()
        self._file = handle
        return True

    def release(self):
        if self._file is not None:
            try:
                _unlock(self._file)
            finally:
                self._file.close()
                self._file = None

    def __enter__(self):
        if not self.acquire():
            raise RunnerBusy(f"Another Radar runner is using {self.path.parent}.")
        return self

    def __exit__(self, *exc_info):
        self.release()


if os.name == "nt":
    import msvcrt

    # Windows locks are mandatory for the bytes they cover, so lock one byte far past the process id written at
    # the start of the file; that keeps the id readable by other processes.
    _LOCK_OFFSET = 1 << 30

    def _lock(handle):
        handle.seek(_LOCK_OFFSET)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle):
        handle.seek(_LOCK_OFFSET)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(handle):
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle):
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def executing(data_directory=None):
    """Hold the runner lock and an open Store, after closing checks a dead process left running.

    Raises RunnerBusy if another process is executing checks in this data folder.
    """
    with RunnerLock(data_directory), Store(data_directory) as store:
        for check in close_interrupted(store):
            log.warning("Closed %s of %s: it was still running when Radar last stopped.", check.id, check.source_id)
        yield store


def tick(store, now=None, *, should_stop=lambda: False, **options):
    """Run queued requests, then due scheduled sources, once. Returns the finished checks (and skips).

    A check that crashes is already recorded as failed by the workflow; the error is logged and the next one runs.
    """
    results = []

    def attempt(run, *args):
        if should_stop():
            return False
        try:
            result = run(store, *args, **options)
        except Exception:
            log.exception("A check stopped with an unexpected error; it is recorded as failed if it had started.")
            return True
        if result is not None:
            results.append(result)
        return True

    for request in store.list_pending_requests():
        if not attempt(run_request, request):
            return results
    for source in due_sources(store, now or utc_now()):
        if not attempt(run_check, source.id, SCHEDULED):
            return results
    return results


def run_forever(data_directory=None, *, poll_seconds=DEFAULT_POLL_SECONDS, stop=None, on_result=None, **options):
    """Run checks until `stop` (a threading.Event) is set. Raises RunnerBusy if another runner is active."""
    stop = stop or threading.Event()
    with executing(data_directory) as store:
        log.info("Radar runner started (every %ss).", poll_seconds)
        while not stop.is_set():
            try:
                for result in tick(store, should_stop=stop.is_set, **options):
                    if on_result is not None:
                        on_result(result)
            except StoreError:
                log.exception("Could not read the store; trying again next round.")
            stop.wait(poll_seconds)
        log.info("Radar runner stopped.")
