"""Check that an installed Radar wheel works on its own, without the source tree.

CI builds the wheel, installs it, and runs this from outside the checkout:

    python -I wheel_smoke.py <path to the checkout>

It fails if ``radar`` is imported from the checkout instead of the installed
wheel (which would hide packaging mistakes), if the SQL migrations are
missing from the wheel, or if storage cannot record and read back a check.
Failures raise SystemExit rather than using assert, so they are reported
even when Python runs with -O.
"""

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def fail(message):
    raise SystemExit(f"wheel smoke test failed: {message}")


def main(checkout):
    import radar
    from radar.domain.discovery import CapturedDocument, ContractCandidate, ValidatedContractPackage
    from radar.storage import database
    from radar.storage.records import INITIAL
    from radar.storage.repository import Store

    installed = os.path.normcase(os.path.realpath(radar.__file__))
    source = os.path.normcase(os.path.realpath(checkout))
    if installed.startswith(source + os.sep):
        fail(f"radar was imported from the checkout ({installed}), not the wheel")

    migrations = database.load_migrations(database.MIGRATIONS_DIRECTORY)
    if not migrations:
        fail("the wheel contains no migrations")

    url = "https://api.example.com/openapi.yaml"
    root = b"openapi: 3.1.0\npaths: {}\n"
    now = datetime.now(timezone.utc)
    package = ValidatedContractPackage(
        candidate=ContractCandidate(url, "direct_url", url),
        root_document=CapturedDocument(url, root, now, "application/yaml"),
        referenced_documents=(),
        parsed_contract={},
        openapi_version="3.1.0",
    )

    with tempfile.TemporaryDirectory() as directory:
        data = Path(directory) / ".radar"
        with Store(data) as store:
            source_record = store.add_source("https://api.example.com")
            check = store.begin_check(source_record.id)
            check = store.complete_check(
                check.id, outcome=INITIAL, package=package,
                normalized=b'{"paths":{}}', normalization_version="smoke-1",
            )
        with Store(data) as store:  # reopened, as after a restart
            if store.read_contract(check.snapshot_id).original != root:
                fail("the stored contract did not read back unchanged")
        connection = sqlite3.connect(os.fspath(data / "radar.db"))
        try:
            version = database.schema_version(connection)
        finally:
            connection.close()
        if version != len(migrations):
            fail(f"database is at schema {version}, expected {len(migrations)}")

    print(f"ok: radar {installed}, {len(migrations)} migration(s), storage round trip")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -I wheel_smoke.py <path to the checkout>")
    main(sys.argv[1])
