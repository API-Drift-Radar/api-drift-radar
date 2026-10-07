"""Extract the structure of JSON objects and compare two structures.

This module is pure logic: it makes no HTTP requests, reads no files,
and prints nothing. Callers pass in already-parsed JSON (e.g. from
``json.loads``) and get back plain Python data.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

Path = tuple[str, ...]
"""A field's location, e.g. ("user", "address", "city").

Tuples are used instead of dotted strings because JSON keys may
themselves contain dots.
"""

Structure = dict[Path, str]
"""Maps every field path to its JSON type name."""


class UnsupportedStructureError(ValueError):
    """Raised when the input contains an array or a non-JSON value."""


class ChangeKind(str, Enum):
    ADDED = "added"
    ABSENT = "absent"
    TYPE_CHANGED = "type_changed"


@dataclass(frozen=True)
class Finding:
    """One structural difference between two JSON objects."""

    kind: ChangeKind
    path: Path
    old_type: str | None  # None when the field was added
    new_type: str | None  # None when the field is absent


def _json_type(value: Any, path: Path) -> str:
    """Return the JSON type name of a single value."""
    # bool must be checked before int: in Python, True is an int.
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"  # JSON has one number type, so 1 and 1.0 match
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    location = "/".join(path) or "<root>"
    if isinstance(value, list):
        raise UnsupportedStructureError(f"arrays are not supported (at {location})")
    raise UnsupportedStructureError(
        f"unsupported value of type {type(value).__name__} (at {location})"
    )


def extract_structure(data: Any) -> Structure:
    """Return every nested field path in ``data`` with its JSON type.

    ``data`` must be a JSON object (a dict). Nested objects appear with
    type "object", and their own fields appear underneath them.

    >>> extract_structure({"id": 1, "user": {"name": "Ana"}})
    {('id',): 'number', ('user',): 'object', ('user', 'name'): 'string'}
    """
    if not isinstance(data, dict):
        _json_type(data, ())  # raises for arrays/unsupported values
        raise UnsupportedStructureError("the top level must be a JSON object")

    structure: Structure = {}
    _walk(data, (), structure)
    return structure


def _walk(obj: dict, prefix: Path, out: Structure) -> None:
    for key, value in obj.items():
        if not isinstance(key, str):
            raise UnsupportedStructureError(
                f"object keys must be strings, got {type(key).__name__}"
            )
        path = prefix + (key,)
        type_name = _json_type(value, path)
        out[path] = type_name
        if type_name == "object":
            _walk(value, path, out)


def compare_structures(old: Any, new: Any) -> list[Finding]:
    """Return the structural differences between two JSON objects.

    Only field paths and types are compared, so changed values, key
    order and formatting never produce findings. Inputs are not
    modified. Findings are sorted by path, so the result is always in
    the same order for the same inputs.

    When a field changes type (e.g. object -> null), the field itself
    is reported as a type change, and any nested fields that appear or
    disappear because of it are reported too.
    """
    old_structure = extract_structure(old)
    new_structure = extract_structure(new)

    findings: list[Finding] = []
    for path in old_structure.keys() | new_structure.keys():
        old_type = old_structure.get(path)
        new_type = new_structure.get(path)
        if old_type is None:
            findings.append(Finding(ChangeKind.ADDED, path, None, new_type))
        elif new_type is None:
            findings.append(Finding(ChangeKind.ABSENT, path, old_type, None))
        elif old_type != new_type:
            findings.append(Finding(ChangeKind.TYPE_CHANGED, path, old_type, new_type))

    # Each path appears at most once, so sorting by path is a total order.
    findings.sort(key=lambda f: f.path)
    return findings