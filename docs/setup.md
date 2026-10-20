# Local setup

Run from the repository root with Python 3.10 or later:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e './backend[test]'
radar --help
python -m radar.cli --help
python -m pytest -c backend/pyproject.toml backend/tests backend/src
```

All Python dependencies are declared in `backend/pyproject.toml`. Previous separate engine/discovery installations are no longer used by this checkout; a fresh virtual environment avoids stale editable-install paths.

The former `python cli/radar.py` command is now `radar` or `python -m radar.cli`. Existing baseline files and command options remain supported.

`.env.example` documents the optional `RADAR_DATA_DIR` variable. Export it in your shell if needed; automatic dotenv loading is not implemented.

Radar's monitoring data lives in that folder (default `.radar`, relative to the current directory):

- `.radar/radar.db`: the SQLite database of sources, checks, snapshots and changes. It is created, and its schema upgraded, the first time storage opens it; there is no separate setup command. Do not create the file by hand: an empty file is not a valid database.
- `.radar/contracts/`: every captured contract, stored once per distinct content and never modified.

Keep this folder on a persistent local disk, not a network drive, and back up both parts together. Set `RADAR_DATA_DIR` to an absolute path when processes start from different directories, so they all use the same folder. Run one monitoring runner per data folder. See the storage section of [component interfaces](component-interfaces.md#storage-contract-snapshots-and-check-history-issue-10) for details and known limitations.

The frontend, controlled API, and full-system acceptance tests currently contain scaffolding only. There is no web/API/runner startup command yet.
