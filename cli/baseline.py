import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from drift_engine import Structure

from atomic_write import write_file_atomically

FORMAT_VERSION = 1

# Where baselines live by default, relative to the working directory.
DEFAULT_DIRECTORY = os.path.join(".radar", "baselines")

# Every type name drift_engine can produce. Stored structures hold only
# these names, so response values can never end up in a baseline file.
JSON_TYPES = frozenset({"string", "number", "boolean", "null", "object"})


@dataclass(frozen=True)
class Baseline:
    url: str
    captured_at: datetime
    structure: Structure


def baseline_path(url, directory=DEFAULT_DIRECTORY):
    """Return the file that holds the baseline for ``url``: one file per URL.

    The name is the host (for readability) plus a hash of the exact URL, so
    different URLs never share a file and a URL can never influence the
    directory part of the path.
    """
    _check_url(url)
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        host = ""
    host = re.sub(r"[^a-z0-9.-]+", "-", host.lower()).strip(".-")[:50] or "endpoint"
    return os.path.join(os.fspath(directory), f"{host}-{digest}.json")


def save_baseline(path, url, structure, replace=False):
    """Write a baseline file for ``url``.

    Refuses to touch an existing file unless ``replace`` is True, and a
    failed save leaves any existing baseline unchanged (see
    ``write_file_atomically``). The parent directory is created, owner-only,
    if it does not exist.
    """
    _check_url(url)
    _check_structure(structure)

    document = {
        "version": FORMAT_VERSION,
        "url": url,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "structure": [
            {"path": list(field), "type": structure[field]}
            for field in sorted(structure)
        ],
    }

    _make_directory(os.path.dirname(os.fspath(path)))
    write_file_atomically(path, json.dumps(document, indent=2) + "\n", replace)


def load_baseline(path, expected_url):
    """Read and validate a baseline file, checking it belongs to ``expected_url``."""
    _check_url(expected_url)
    path = os.fspath(path)

    try:
        with open(path, encoding="utf-8") as baseline_file:
            document = json.load(baseline_file)
    except FileNotFoundError:
        raise FileNotFoundError(f"No baseline found at {path}.") from None
    except json.JSONDecodeError as error:
        raise ValueError(f"Baseline file is not valid JSON: {error}") from error
    except (OSError, UnicodeDecodeError) as error:
        raise RuntimeError(f"Could not read baseline: {error}") from error

    if not isinstance(document, dict):
        raise ValueError("Baseline must be a JSON object.")
    if document.get("version") != FORMAT_VERSION:
        raise ValueError(f"Unsupported baseline version: {document.get('version')!r}.")

    url = document.get("url")
    if not isinstance(url, str):
        raise ValueError("Baseline is missing a valid url.")
    if url != expected_url:
        raise ValueError(
            f"Baseline is for {url}, not {expected_url}."
        )

    return Baseline(
        url=url,
        captured_at=_parse_timestamp(document.get("captured_at")),
        structure=_parse_structure(document.get("structure")),
    )


def _make_directory(directory):
    if not directory:
        return
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    except OSError as error:
        raise RuntimeError(f"Could not create baseline directory: {error}") from error


def _check_url(url):
    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL must be a nonempty string.")


def _check_structure(structure):
    if not isinstance(structure, dict):
        raise ValueError("Structure must be a dict of field paths to type names.")
    for field, type_name in structure.items():
        if (
            not isinstance(field, tuple)
            or not field
            or not all(isinstance(part, str) for part in field)
        ):
            raise ValueError(f"Invalid field path: {field!r}.")
        if not isinstance(type_name, str) or type_name not in JSON_TYPES:
            raise ValueError(f"Invalid type for {field!r}: {type_name!r}.")


def _parse_timestamp(value):
    try:
        captured_at = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("Baseline has an invalid captured_at timestamp.") from None
    if captured_at.tzinfo is None:
        raise ValueError("Baseline captured_at must include a timezone.")
    return captured_at


def _parse_structure(entries):
    if not isinstance(entries, list):
        raise ValueError("Baseline structure must be a list.")

    structure = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Baseline structure entries must be objects.")
        path, type_name = entry.get("path"), entry.get("type")
        if not isinstance(path, list):
            raise ValueError("Baseline structure entry has an invalid path.")
        field = tuple(path)
        try:
            _check_structure({field: type_name})
        except ValueError as error:
            raise ValueError(f"Baseline structure entry is invalid: {error}") from None
        if field in structure:
            raise ValueError(f"Baseline structure repeats the path {field!r}.")
        structure[field] = type_name
    return structure
