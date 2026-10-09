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

`.env.example` documents the optional `RADAR_DATA_DIR` variable. Export it in your shell if needed; automatic dotenv loading is not implemented. The configuration reserves `.radar/radar.db` and `.radar/contracts/` for monitoring. No database is created until persistence is implemented; an empty file would not be a valid initialized application database.

The frontend, controlled API, migrations, integration tests, and acceptance tests currently contain scaffolding only. There is no web/API/runner startup command yet.
