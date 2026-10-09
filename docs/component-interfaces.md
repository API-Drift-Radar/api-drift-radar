# Component interfaces

## Existing Python interfaces

- `radar.comparison`: `extract_structure`, `compare_structures`, `compare_extracted`, and existing structural types.
- `radar.storage.baseline`: `baseline_path`, `save_baseline`, `load_baseline`.
- `radar.storage.atomic_write`: `write_file_atomically`.
- `radar.cli.fetch`: `fetch_json`.
- `radar.cli.main`: `main(argv=None)` returns exit code 0 (no differences), 1 (differences), or 2 (failure).

## Milestone 2 boundaries to implement

Discovery returns a validated contract package or explicit ambiguity/failure outcome. Comparison consumes two validated packages and returns findings and coverage limitations. Neither owns persistence or scheduling.

Storage persists sources, snapshots, checks, findings, and artifacts. Monitoring coordinates these components and advances references only after successful processing. The API returns a check identifier while work continues in the runner. The web frontend uses the HTTP API.

Concrete shared types and request/response examples must be agreed before implementing those components. No HTTP endpoint or wire schema is implemented by this restructure.
