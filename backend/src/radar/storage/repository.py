"""The storage component's interface: sources, checks, snapshots and changes.

Other components (execution, the HTTP API) use ``Store`` and nothing else
in this package. A ``Store`` owns one SQLite connection plus the contract
files directory of a Radar data directory::

    with Store() as store:                     # .radar/ from configuration
        source = store.add_source("https://api.example.com")
        check = store.begin_check(source.id)
        ...
        store.fail_check(check.id, stage="fetch", reason="inaccessible",
                         error="Connection timed out")

Rules every operation follows:

* Unknown or malformed identifiers raise ``NotFoundError``; they are
  checked before any query runs.
* Each write is one transaction: it is saved completely or not at all.
* History is never edited. A check can be finished once; finishing it again
  raises ``ConflictError``. Snapshots, checks and changes are never deleted.
* A failed check never moves a source's latest valid snapshot.
* Database problems are reported as ``StoreError``.

A ``Store`` must be used from the thread that created it, as SQLite
connections are not safe to share between threads. Open one ``Store`` per
thread or process; several may use the same data directory at once.

Recording a successful capture (``complete_check``) will be added once the
shared contract package type from discovery is available on ``main``.
"""

import json
import re
import sqlite3
from pathlib import Path

from radar import config as settings
from radar.storage import database
from radar.storage.contracts import ContractFiles
from radar.storage.errors import ConflictError, NotFoundError, StoreError
from radar.storage.identifiers import (
    CHANGE,
    CHECK,
    SNAPSHOT,
    SOURCE,
    format_timestamp,
    is_valid_id,
    new_id,
    parse_timestamp,
    utc_now,
)
from radar.storage.records import (
    FAILED,
    FAILURE_OUTCOMES,
    FAILURE_STAGES,
    RUNNING,
    ChangeRecord,
    CheckRun,
    Snapshot,
    SnapshotDocument,
    Source,
)

MAX_LOCATOR_LENGTH = 2048
MAX_ERROR_LENGTH = 4000
MAX_JSON_BYTES = 4 * 1024 * 1024
_REASON_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
_TRUNCATION_MARK = " [truncated]"


class Store:
    """Radar's persistent history. Use as a context manager, or call ``close``."""

    def __init__(self, data_directory=None, *, migrations_directory=None):
        if data_directory is None:
            database_path = settings.DATABASE_PATH
            contracts_directory = settings.CONTRACTS_DIRECTORY
        else:
            database_path = Path(data_directory) / "radar.db"
            contracts_directory = Path(data_directory) / "contracts"
        self._db = database.connect(database_path, migrations_directory)
        self._files = ContractFiles(contracts_directory)

    def close(self):
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    # Sources

    def add_source(self, locator, config=None):
        """Register an API URL or provider domain to monitor; return the new source.

        Each call creates a new source, so the same locator can be monitored
        more than once with different configuration (for example two API
        versions). Use ``find_sources`` to look for existing ones first.
        """
        locator = _check_locator(locator)
        config_json = _to_json(config if config is not None else {}, "config", dict)
        source_id = new_id(SOURCE)
        with database.transaction(self._db):
            self._db.execute(
                "INSERT INTO sources (id, locator, config_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (source_id, locator, config_json, format_timestamp(utc_now())),
            )
        return self.get_source(source_id)

    def get_source(self, source_id):
        return _source(self._require("sources", source_id, SOURCE))

    def find_sources(self, locator):
        """Sources registered for exactly this locator, oldest first."""
        if not isinstance(locator, str):
            return []
        locator = locator.strip()
        return [
            _source(row)
            for row in self._query(
                "SELECT * FROM sources WHERE locator = ? ORDER BY created_at, rowid",
                (locator,),
            )
        ]

    def list_sources(self, limit=None):
        """All sources, oldest first."""
        return [
            _source(row)
            for row in self._query(
                "SELECT * FROM sources ORDER BY created_at, rowid" + _limit(limit), ()
            )
        ]

    def update_source_config(self, source_id, config):
        """Replace a source's configuration. Its locator and history never change."""
        config_json = _to_json(config, "config", dict)
        self.get_source(source_id)
        with database.transaction(self._db):
            self._db.execute(
                "UPDATE sources SET config_json = ? WHERE id = ?", (config_json, source_id)
            )
        return self.get_source(source_id)

    # Checks

    def begin_check(self, source_id, started_at=None):
        """Record that a check of ``source_id`` has started; return it as ``running``."""
        started = format_timestamp(started_at if started_at is not None else utc_now())
        check_id = new_id(CHECK)
        self.get_source(source_id)
        with database.transaction(self._db):
            self._db.execute(
                "INSERT INTO check_runs (id, source_id, status, started_at) "
                "VALUES (?, ?, ?, ?)",
                (check_id, source_id, RUNNING, started),
            )
        return self.get_check(check_id)

    def fail_check(
        self,
        check_id,
        *,
        stage,
        reason,
        error,
        outcome=FAILED,
        detail=None,
        finished_at=None,
    ):
        """Finish a running check without a snapshot.

        ``outcome`` is ``failed``, or ``ambiguous`` when discovery found
        several candidate contracts (put them in ``detail``). ``stage`` says
        where the check stopped (one of ``FAILURE_STAGES``); ``reason`` is a
        short code such as ``inaccessible`` or ``not_found``; ``error`` is a
        human-readable message, truncated if very long.

        The source's latest valid snapshot is not changed. The check records
        which snapshot was the reference at the time, so history shows what
        a failure was measured against.
        """
        if outcome not in FAILURE_OUTCOMES:
            raise ValueError(f"Not a failure outcome: {outcome!r}.")
        if stage not in FAILURE_STAGES:
            raise ValueError(f"Unknown failure stage: {stage!r}.")
        if not isinstance(reason, str) or not _REASON_PATTERN.fullmatch(reason):
            raise ValueError(
                "reason must be a short lowercase code such as 'inaccessible'."
            )
        error = _check_error(error)
        detail_json = _to_json(detail if detail is not None else {}, "detail", dict)
        finished = finished_at if finished_at is not None else utc_now()
        finished_text = format_timestamp(finished)

        with database.transaction(self._db):
            check = self._running_check(check_id)
            if finished < check.started_at:
                raise ValueError("finished_at is earlier than the check's start.")
            reference = self._db.execute(
                "SELECT latest_snapshot_id FROM sources WHERE id = ?", (check.source_id,)
            ).fetchone()["latest_snapshot_id"]
            self._db.execute(
                "UPDATE check_runs SET status = ?, finished_at = ?, "
                "before_snapshot_id = ?, failure_stage = ?, failure_reason = ?, "
                "error = ?, detail_json = ? WHERE id = ?",
                (outcome, finished_text, reference, stage, reason, error, detail_json,
                 check_id),
            )
        return self.get_check(check_id)

    def get_check(self, check_id):
        return _check_run(self._require("check_runs", check_id, CHECK))

    def list_checks(self, source_id, limit=None):
        """A source's checks, newest first, whatever their outcome."""
        self.get_source(source_id)
        return [
            _check_run(row)
            for row in self._query(
                "SELECT * FROM check_runs WHERE source_id = ? "
                "ORDER BY started_at DESC, rowid DESC" + _limit(limit),
                (source_id,),
            )
        ]

    def list_running_checks(self):
        """Checks that started but never finished, oldest first.

        After a restart these are checks that were interrupted; the execution
        component decides whether to fail them (stage ``interrupted``) or
        resume them.
        """
        return [
            _check_run(row)
            for row in self._query(
                "SELECT * FROM check_runs WHERE status = ? ORDER BY started_at, rowid",
                (RUNNING,),
            )
        ]

    # Snapshots and contract content

    def get_snapshot(self, snapshot_id):
        return _snapshot(self._require("snapshots", snapshot_id, SNAPSHOT))

    def latest_snapshot(self, source_id):
        """The source's current valid reference, or None before its first capture."""
        latest = self.get_source(source_id).latest_snapshot_id
        return None if latest is None else self.get_snapshot(latest)

    def list_snapshots(self, source_id, limit=None):
        """A source's snapshots, newest first."""
        self.get_source(source_id)
        return [
            _snapshot(row)
            for row in self._query(
                "SELECT * FROM snapshots WHERE source_id = ? "
                "ORDER BY captured_at DESC, rowid DESC" + _limit(limit),
                (source_id,),
            )
        ]

    def snapshot_documents(self, snapshot_id):
        """Where each document of a snapshot came from: root first, then by URL."""
        self.get_snapshot(snapshot_id)
        return [
            _snapshot_document(row)
            for row in self._query(
                "SELECT * FROM snapshot_documents WHERE snapshot_id = ? "
                "ORDER BY is_root DESC, source_url",
                (snapshot_id,),
            )
        ]

    def read_contract(self, snapshot_id):
        """The snapshot's original documents and normalized form, integrity-checked.

        Raises ``StoreError`` if the stored content is missing or damaged,
        rather than returning anything that does not match what was captured.
        """
        return self._files.read(self.get_snapshot(snapshot_id).fingerprint)

    # Changes

    def get_change(self, change_id):
        return _change(self._require("changes", change_id, CHANGE))

    def list_changes(self, *, check_id=None, source_id=None, limit=None):
        """Changes found by one check (in order), or by every check of a source
        (newest check first)."""
        if (check_id is None) == (source_id is None):
            raise ValueError("Pass exactly one of check_id or source_id.")
        if check_id is not None:
            self.get_check(check_id)
            sql = "SELECT * FROM changes WHERE check_run_id = ? ORDER BY ordinal"
            params = (check_id,)
        else:
            self.get_source(source_id)
            sql = (
                "SELECT c.* FROM changes c JOIN check_runs r ON r.id = c.check_run_id "
                "WHERE c.source_id = ? "
                "ORDER BY r.started_at DESC, r.rowid DESC, c.ordinal"
            )
            params = (source_id,)
        return [_change(row) for row in self._query(sql + _limit(limit), params)]

    # Internals

    def _require(self, table, record_id, kind):
        if not is_valid_id(record_id, kind):
            raise NotFoundError(f"No {_KIND_NAMES[kind]} with id {record_id!r}.")
        rows = self._query(f"SELECT * FROM {table} WHERE id = ?", (record_id,))
        if not rows:
            raise NotFoundError(f"No {_KIND_NAMES[kind]} with id {record_id!r}.")
        return rows[0]

    def _running_check(self, check_id):
        check = self.get_check(check_id)
        if check.finished:
            raise ConflictError(f"Check {check_id} has already finished as {check.status}.")
        return check

    def _query(self, sql, params):
        try:
            return self._db.execute(sql, params).fetchall()
        except sqlite3.Error as error:
            raise StoreError(f"Could not read from the Radar store: {error}") from error


_KIND_NAMES = {SOURCE: "source", CHECK: "check", SNAPSHOT: "snapshot", CHANGE: "change"}


def _check_locator(locator):
    if not isinstance(locator, str):
        raise ValueError("A source locator must be a string.")
    locator = locator.strip()
    if not locator:
        raise ValueError("A source locator cannot be empty.")
    if len(locator) > MAX_LOCATOR_LENGTH:
        raise ValueError(f"A source locator is limited to {MAX_LOCATOR_LENGTH} characters.")
    if any(ord(c) < 32 or c == "\x7f" for c in locator):
        raise ValueError("A source locator cannot contain control characters.")
    return locator


def _check_error(error):
    if not isinstance(error, str) or not error.strip():
        raise ValueError("A failed check needs an error message.")
    error = error.strip()
    if len(error) > MAX_ERROR_LENGTH:
        error = error[: MAX_ERROR_LENGTH - len(_TRUNCATION_MARK)] + _TRUNCATION_MARK
    return error


def _to_json(value, name, expected_type):
    if not isinstance(value, expected_type):
        raise ValueError(f"{name} must be a {expected_type.__name__}.")
    try:
        text = json.dumps(value, sort_keys=True, allow_nan=False, ensure_ascii=False)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must contain only JSON values: {error}") from None
    if len(text.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError(f"{name} is larger than {MAX_JSON_BYTES} bytes.")
    return text


def _limit(limit):
    if limit is None:
        return ""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer.")
    return f" LIMIT {limit}"


def _optional_time(text):
    return None if text is None else parse_timestamp(text)


def _source(row):
    return Source(
        id=row["id"],
        locator=row["locator"],
        config=json.loads(row["config_json"]),
        created_at=parse_timestamp(row["created_at"]),
        latest_snapshot_id=row["latest_snapshot_id"],
    )


def _check_run(row):
    return CheckRun(
        id=row["id"],
        source_id=row["source_id"],
        status=row["status"],
        started_at=parse_timestamp(row["started_at"]),
        finished_at=_optional_time(row["finished_at"]),
        before_snapshot_id=row["before_snapshot_id"],
        snapshot_id=row["snapshot_id"],
        failure_stage=row["failure_stage"],
        failure_reason=row["failure_reason"],
        error=row["error"],
        detail=json.loads(row["detail_json"]),
        limitations=json.loads(row["limitations_json"]),
    )


def _snapshot(row):
    return Snapshot(
        id=row["id"],
        source_id=row["source_id"],
        fingerprint=row["fingerprint"],
        captured_at=parse_timestamp(row["captured_at"]),
        normalization_version=row["normalization_version"],
        openapi_version=row["openapi_version"],
        content_fingerprint=row["content_fingerprint"],
        media_type=row["media_type"],
        provenance=json.loads(row["provenance_json"]),
    )


def _snapshot_document(row):
    return SnapshotDocument(
        snapshot_id=row["snapshot_id"],
        source_url=row["source_url"],
        path=row["path"],
        retrieved_at=parse_timestamp(row["retrieved_at"]),
        media_type=row["media_type"],
        is_root=bool(row["is_root"]),
    )


def _change(row):
    return ChangeRecord(
        id=row["id"],
        check_id=row["check_run_id"],
        source_id=row["source_id"],
        before_snapshot_id=row["before_snapshot_id"],
        after_snapshot_id=row["after_snapshot_id"],
        ordinal=row["ordinal"],
        change_type=row["change_type"],
        location=row["location"],
        direction=row["direction"],
        classification=row["classification"],
        summary=row["summary"],
        rationale=row["rationale"],
        before_value=_optional_json(row["before_value_json"]),
        after_value=_optional_json(row["after_value_json"]),
        details=json.loads(row["details_json"]),
    )


def _optional_json(text):
    return None if text is None else json.loads(text)
