# cli

`radar` saves the structure of a JSON API response, then later checks whether the live response still has the same fields and types. Values are never stored or compared, only field paths and their types.

## Install

Requires Python 3.10 or newer. From the repository root:

```sh
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
cd cli
pip install -r requirements.txt  # run from cli/: it installs ../engine too
pip install pytest               # only needed to run the tests
```

## Usage

Run from anywhere with the virtual environment active:

```sh
python cli/radar.py baseline https://api.example.com/data
python cli/radar.py check https://api.example.com/data
python cli/radar.py --help
```

| Command | What it does |
|---|---|
| `baseline URL` | Fetches the URL and saves its structure. Refuses to replace an existing file. |
| `check URL` | Fetches the URL and compares it with the saved baseline. |

| Option | Applies to | Meaning |
|---|---|---|
| `--baseline FILE` | both | Baseline file to write or read. Default: `baseline.json` in the current directory. |
| `--overwrite` | `baseline` | Replace the baseline file if it already exists. |
| `--help` | all | Show help. Also works after a command, e.g. `baseline --help`. |

A baseline file belongs to one URL. Checking a different URL against it is an error, so use `--baseline` to keep one file per API.

### Example

```text
$ python cli/radar.py baseline https://api.example.com/data
Saved baseline for https://api.example.com/data to baseline.json (12 fields).

$ python cli/radar.py check https://api.example.com/data
Found 2 changes in https://api.example.com/data since the baseline was captured at 2026-10-07T22:47:35+00:00:
  + user.email (added, string)
  ~ id (type changed: number -> string)
```

Findings use `+` for an added field, `-` for a field that is absent now, and `~` for a type change.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success, no differences |
| 1 | Differences found |
| 2 | Failure: bad usage, network error, invalid response, or a missing or unreadable baseline |

Errors are printed to stderr as `error: ...`, so a CI step can fail a build on any non-zero code.

### How arrays are handled

An array is described by the shape of its elements, merged across all of them and shown as `[]`, for example `users[].email`.

- Element order, count, and values never produce findings.
- A field that appears in only some elements still counts as present.
- Mixed types are reported as a union, such as `null|string`.
- An empty array says nothing about its elements, so differences under it are skipped instead of reported as added or absent.
- A top-level array works too; its fields appear as `[].id`.

### Limits

- The response must be a JSON object or array.
- An object key named `[]` is reserved and rejected.
- Redirects are not followed. Use the final URL.

## Tests

```sh
cd cli
python -m pytest
```

`test_radar.py` tests the commands with stubbed components, then runs the full baseline and check workflow with the real engine and baseline files, stubbing only the network.

## Layout

| File | Role |
|---|---|
| `radar.py` | Command parsing, coordination, and output |
| `fetch.py` | Fetches JSON from a URL |
| `baseline.py` | Saves and loads baseline files |
| `atomic_write.py` | Safe file writes used by `baseline.py` |
| `../engine` | `drift_engine`: structure extraction and comparison |
