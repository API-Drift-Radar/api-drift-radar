"""SQLite connection, schema migrations and transactions for Radar storage.

Migrations are plain SQL files in ``backend/migrations/`` named
``NNNN_description.sql`` and numbered from 0001 without gaps. Each one runs
in its own transaction together with the update of SQLite's ``user_version``,
so a database is always at exactly one schema version: a failed or
interrupted migration leaves it at the previous one.

Migrations are located relative to this source tree, which suits the
editable install described in docs/setup.md.
"""

import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from radar import config
from radar.storage.errors import StoreError

# Older Pythons report some SQLite problems as sqlite3.Warning, which is not a
# subclass of sqlite3.Error. Both mean the database refused the operation.
DATABASE_ERRORS = (sqlite3.Error, sqlite3.Warning)

MIGRATIONS_DIRECTORY = Path(__file__).resolve().parents[3] / "migrations"

_MIGRATION_NAME = re.compile(r"(\d{4})_[a-z0-9_]+\.sql")
_BUSY_TIMEOUT_SECONDS = 30


def connect(path=None, migrations_directory=None):
    """Open the Radar database, creating and migrating it as needed.

    Returns a ``sqlite3.Connection`` in autocommit mode with rows as
    ``sqlite3.Row``; group writes with ``transaction``. The parent
    directory is created, owner-only, if it does not exist.
    """
    path = Path(path if path is not None else config.DATABASE_PATH)
    migrations = load_migrations(migrations_directory or MIGRATIONS_DIRECTORY)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as error:
        raise StoreError(f"Could not create {path.parent}: {error}") from error

    try:
        connection = sqlite3.connect(
            os.fspath(path), timeout=_BUSY_TIMEOUT_SECONDS, isolation_level=None
        )
    except DATABASE_ERRORS as error:
        raise StoreError(f"Could not open the Radar database {path}: {error}") from error

    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        # WAL lets the API read while the runner writes; FULL makes each
        # commit durable before it returns.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        _migrate(connection, migrations)
    except BaseException as error:
        connection.close()
        if isinstance(error, DATABASE_ERRORS):
            raise StoreError(f"Could not prepare the Radar database: {error}") from error
        raise
    return connection


def schema_version(connection):
    return connection.execute("PRAGMA user_version").fetchone()[0]


@contextmanager
def transaction(connection):
    """Run a block as one write transaction: all of it commits, or none.

    ``BEGIN IMMEDIATE`` takes the write lock up front, so two writers wait
    for each other instead of failing halfway. SQLite errors are raised as
    StoreError; any other exception rolls back and propagates unchanged.
    """
    if connection.in_transaction:
        raise StoreError("A transaction is already open on this connection.")
    try:
        connection.execute("BEGIN IMMEDIATE")
    except DATABASE_ERRORS as error:
        raise StoreError(f"Could not start a database transaction: {error}") from error
    try:
        yield connection
    except BaseException as error:
        _rollback(connection)
        if isinstance(error, DATABASE_ERRORS):
            raise StoreError(f"Database write failed: {error}") from error
        raise
    try:
        connection.execute("COMMIT")
    except DATABASE_ERRORS as error:
        _rollback(connection)
        raise StoreError(f"Could not commit to the database: {error}") from error


def load_migrations(directory):
    """Return ``[(version, name, sql)]`` in order, checking the numbering."""
    directory = Path(directory)
    if not directory.is_dir():
        raise StoreError(f"Migrations directory not found: {directory}")
    migrations = []
    for path in sorted(directory.iterdir()):
        if path.suffix != ".sql":
            continue
        match = _MIGRATION_NAME.fullmatch(path.name)
        if not match:
            raise StoreError(f"Badly named migration: {path.name}")
        migrations.append((int(match.group(1)), path.name, path.read_text("utf-8")))
    for expected, (version, name, _) in enumerate(migrations, start=1):
        if version != expected:
            raise StoreError(f"Migration {name} is out of sequence; expected {expected:04d}.")
    if not migrations:
        raise StoreError(f"No migrations found in {directory}")
    return migrations


def _migrate(connection, migrations):
    latest = migrations[-1][0]
    current = schema_version(connection)
    if current > latest:
        raise StoreError(
            f"The database is at schema version {current}, newer than this "
            f"Radar supports ({latest}). Update Radar before using it."
        )
    for version, name, sql in migrations[current:]:
        with transaction(connection):
            # Another process may have migrated while this one waited.
            if schema_version(connection) >= version:
                continue
            try:
                for statement in _statements(sql):
                    connection.execute(statement)
            except DATABASE_ERRORS as error:
                raise StoreError(f"Migration {name} failed: {error}") from error
            connection.execute(f"PRAGMA user_version = {version}")


def _statements(sql):
    """Split a script into single statements, keeping trigger bodies intact.

    Every ``;`` is a possible end of statement; SQLite's own parser
    (``complete_statement``) decides whether it really is one, so semicolons
    inside strings, comments and ``BEGIN ... END`` trigger bodies are kept.
    Two statements on one line are split correctly.
    """
    *pieces, tail = sql.split(";")
    pending = ""
    for piece in pieces:
        pending += piece + ";"
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            pending = ""
            if not _only_comments(statement):
                yield statement
    # Text after the last ";" (or an unfinished statement) must be comments only.
    if not _only_comments(pending + tail):
        raise StoreError("Migration ends with an incomplete statement.")


def _only_comments(text):
    """True if ``text`` holds nothing but whitespace, ``;`` and ``--`` comments."""
    for line in text.splitlines():
        line = line.strip().strip(";").strip()
        if line and not line.startswith("--"):
            return False
    return True


def _rollback(connection):
    if connection.in_transaction:
        try:
            connection.execute("ROLLBACK")
        except DATABASE_ERRORS:
            pass
