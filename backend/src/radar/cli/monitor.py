"""`radar monitor ...`: configure monitored sources, run checks, and run the scheduler.

    radar monitor add https://api.example.com --every 60
    radar monitor list
    radar monitor check SOURCE_ID            # run now and print the result
    radar monitor check SOURCE_ID --queue    # queue for the runner, print the check id, return at once
    radar monitor status CHECK_ID
    radar monitor history SOURCE_ID
    radar monitor pause SOURCE_ID / resume SOURCE_ID
    radar monitor choose SOURCE_ID CONTRACT_URL
    radar monitor run                        # the runner; Ctrl+C to stop

This module parses, calls `radar.monitoring` and `radar.storage`, and prints. It holds no check logic.
Exit codes: 0 success (initial or unchanged), 1 changes found, 2 failure or the check did not produce a snapshot.
"""

import logging
import math
import signal
import sys
import threading
import time

from radar.monitoring.checks import CheckSkipped, request_check, run_check, run_request
from radar.monitoring.runner import DEFAULT_POLL_SECONDS, RunnerBusy, RunnerLock, run_forever
from radar.monitoring.schedule import INTERVAL_KEY, PAUSED_KEY, close_interrupted, interval_of, next_due
from radar.storage.errors import NotFoundError, StoreError
from radar.storage.records import CHANGED, INITIAL, MANUAL, UNCHANGED
from radar.storage.repository import Store

EXIT_OK, EXIT_CHANGES, EXIT_FAILURE = 0, 1, 2
WAIT_POLL_SECONDS = 1.0


def add_parser(commands):
    monitor = commands.add_parser("monitor", help="monitor published OpenAPI contracts over time")
    monitor.add_argument("--data-dir", metavar="DIR", default=None,
                         help="Radar data folder (default: RADAR_DATA_DIR or .radar)")
    sub = monitor.add_subparsers(dest="monitor_command", required=True, metavar="command")

    add = sub.add_parser("add", help="start monitoring an API URL or provider domain")
    add.add_argument("locator", help="API URL or provider domain, e.g. https://api.example.com")
    add.add_argument("--every", type=minutes, metavar="MINUTES", help="check on a schedule, every MINUTES")
    add.add_argument("--contract-url", metavar="URL", help="always capture this contract URL")

    sub.add_parser("list", help="list monitored sources")

    check = sub.add_parser("check", help="check a source now")
    check.add_argument("source_id")
    check.add_argument("--queue", action="store_true",
                       help="queue the check for the runner and print its id without waiting")

    status = sub.add_parser("status", help="show one check")
    status.add_argument("check_id")

    history = sub.add_parser("history", help="show a source's checks, newest first")
    history.add_argument("source_id")
    history.add_argument("--limit", type=int, default=10)

    for name, text in (("pause", "stop scheduled checks of a source"), ("resume", "restart scheduled checks")):
        sub.add_parser(name, help=text).add_argument("source_id")

    choose = sub.add_parser("choose", help="pin the contract URL later checks capture")
    choose.add_argument("source_id")
    choose.add_argument("contract_url")

    run = sub.add_parser("run", help="run queued and scheduled checks until stopped")
    run.add_argument("--poll", type=float, default=DEFAULT_POLL_SECONDS, metavar="SECONDS",
                     help=f"how often to look for work (default {DEFAULT_POLL_SECONDS})")


def main(args):
    handler = _COMMANDS[args.monitor_command]
    try:
        return handler(args)
    except NotFoundError as error:
        return _fail(error)
    except RunnerBusy as error:
        return _fail(error)
    except (StoreError, ValueError) as error:
        return _fail(error)


def _add(args):
    config = {}
    if args.every is not None:
        config[INTERVAL_KEY] = args.every
    if args.contract_url:
        config["contract_url"] = args.contract_url
    with Store(args.data_dir) as store:
        source = store.add_source(args.locator, config)
    schedule = f"every {args.every:g} min" if args.every is not None else "manual checks only"
    print(f"Added {source.id} for {source.locator} ({schedule}).")
    return EXIT_OK


def _list(args):
    with Store(args.data_dir) as store:
        sources = store.list_sources()
        if not sources:
            print("No sources yet. Add one with: radar monitor add URL --every 60")
        for source in sources:
            latest = store.list_checks(source.id, limit=1)
            last = f"{latest[0].status} at {_time(latest[0].started_at)}" if latest else "never checked"
            due = next_due(store, source)
            if source.config.get(PAUSED_KEY) is True:
                when = "paused"
            elif interval_of(source) is None:
                when = "manual only"
            elif due is None:
                when = "running now"
            else:
                when = f"next {_time(due)}"
            print(f"{source.id}  {source.locator}  [{when}]  last: {last}")
    return EXIT_OK


def _check(args):
    if args.queue:
        with Store(args.data_dir) as store:
            result = request_check(store, args.source_id, MANUAL)
        if isinstance(result, CheckSkipped):
            print(f"Not queued: {result.running_check_id} is already running for {args.source_id}.")
        else:
            print(f"Queued {result.check_id}. Start 'radar monitor run' if no runner is active.")
        return EXIT_OK

    lock = RunnerLock(args.data_dir)
    if lock.acquire():  # no runner: run the check in this process
        try:
            with Store(args.data_dir) as store:
                close_interrupted(store)
                result = run_check(store, args.source_id, MANUAL)
                if isinstance(result, CheckSkipped):
                    print(f"Skipped: {result.running_check_id} is already running.")
                    return EXIT_OK
                return _report(store, result)
        finally:
            lock.release()

    # A runner is active: it executes checks, so queue this one and wait for it.
    with Store(args.data_dir) as store:
        result = request_check(store, args.source_id, MANUAL)
        if isinstance(result, CheckSkipped):
            print(f"Skipped: {result.running_check_id} is already running.")
            return EXIT_OK
        print(f"Queued {result.check_id}; waiting for the runner...", file=sys.stderr)
        while True:
            try:
                check = store.get_check(result.check_id)
                if check.finished:
                    return _report(store, check)
            except NotFoundError:
                pass  # not started yet
            if lock.acquire():  # the runner stopped while we waited: run the check here instead
                try:
                    close_interrupted(store)
                    request = store.get_check_request(result.check_id)
                    if request.pending:
                        run_request(store, request)
                finally:
                    lock.release()
                continue
            time.sleep(WAIT_POLL_SECONDS)


def _status(args):
    with Store(args.data_dir) as store:
        try:
            check = store.get_check(args.check_id)
        except NotFoundError:
            request = store.get_check_request(args.check_id)
            print(f"{request.check_id}  queued ({request.kind}) since {_time(request.requested_at)}")
            return EXIT_OK
        return _report(store, check)


def _history(args):
    with Store(args.data_dir) as store:
        checks = store.list_checks(args.source_id, limit=args.limit)
        if not checks:
            print("No checks yet.")
        for check in checks:
            print(_one_line(store, check))
    return EXIT_OK


def _set_paused(paused):
    def handler(args):
        with Store(args.data_dir) as store:
            source = store.get_source(args.source_id)
            config = dict(source.config)
            if paused:
                config[PAUSED_KEY] = True
            else:
                config.pop(PAUSED_KEY, None)
            store.update_source_config(source.id, config)
        print(f"{'Paused' if paused else 'Resumed'} scheduled checks of {args.source_id}.")
        return EXIT_OK
    return handler


def _choose(args):
    with Store(args.data_dir) as store:
        source = store.get_source(args.source_id)
        store.update_source_config(source.id, {**source.config, "contract_url": args.contract_url})
    print(f"Checks of {args.source_id} will capture {args.contract_url}.")
    return EXIT_OK


def _run(args):
    if not math.isfinite(args.poll) or args.poll <= 0:
        raise ValueError("--poll must be a positive number of seconds.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = threading.Event()

    def first_interrupt(signum, frame):
        print("\nStopping after the current check. Press Ctrl+C again to stop now.", file=sys.stderr)
        stop.set()
        signal.signal(signal.SIGINT, signal.default_int_handler)

    previous = signal.signal(signal.SIGINT, first_interrupt)
    try:
        with Store(args.data_dir) as store_for_printing:
            run_forever(args.data_dir, poll_seconds=args.poll, stop=stop,
                        on_result=lambda result: print(_one_line(store_for_printing, result)
                                                       if not isinstance(result, CheckSkipped) else
                                                       f"skipped {result.source_id}: {result.running_check_id} running"))
    except KeyboardInterrupt:
        print("Stopped. A check that was in progress is recorded as interrupted.", file=sys.stderr)
    finally:
        signal.signal(signal.SIGINT, previous)
    return EXIT_OK


_COMMANDS = {
    "add": _add, "list": _list, "check": _check, "status": _status, "history": _history,
    "pause": _set_paused(True), "resume": _set_paused(False), "choose": _choose, "run": _run,
}


def _report(store, check):
    print(_one_line(store, check))
    if check.status == CHANGED:
        for change in store.list_changes(check_id=check.id):
            print(f"  [{change.classification}] {change.location}: {change.summary}")
    for limitation in check.limitations:
        print(f"  note: {limitation if isinstance(limitation, str) else limitation.get('summary', limitation)}")
    if check.status in (INITIAL, UNCHANGED):
        return EXIT_OK
    return EXIT_CHANGES if check.status == CHANGED else EXIT_FAILURE


def _one_line(store, check):
    text = f"{check.id}  {check.source_id}  {check.status}  {_time(check.started_at)}"
    trigger = check.detail.get("trigger")
    if trigger:
        text += f"  ({trigger})"
    if check.status == CHANGED:
        text += f"  {len(store.list_changes(check_id=check.id))} change(s)"
    if check.failure_stage:
        text += f"  at {check.failure_stage}: {check.failure_reason}"
    return text


def _time(value):
    return value.strftime("%Y-%m-%d %H:%M:%S UTC")


def minutes(text):
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("must be a positive number of minutes")
    return value


def _fail(message):
    print(f"error: {message}", file=sys.stderr)
    return EXIT_FAILURE
