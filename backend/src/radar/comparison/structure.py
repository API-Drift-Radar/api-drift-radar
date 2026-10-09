"""Extract the structure of JSON documents and compare two structures.

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
themselves contain dots. The reserved segment ``"[]"`` stands for "every
element of the array at this path".
"""

Structure = dict[Path, str]
"""Maps every field path to its JSON type name.

A path that holds several types (e.g. a nullable field, or an array of
mixed values) maps to the sorted type names joined with ``"|"``, such as
``"null|string"``.
"""

ARRAY_ELEMENT = "[]"
"""Path segment that stands for the elements of an array."""

UNKNOWN = "unknown"
"""Type of an array's elements when the array was empty, so nothing was seen."""


class UnsupportedStructureError(ValueError):
    """Raised when the input contains a non-JSON value or an unusable key."""


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
    if isinstance(value, list):
        return "array"
    location = "/".join(path) or "<root>"
    raise UnsupportedStructureError(
        f"unsupported value of type {type(value).__name__} (at {location})"
    )


def extract_structure(data: Any) -> Structure:
    """Return every nested field path in ``data`` with its JSON type.

    ``data`` must be a JSON object (a dict) or array (a list). Nested
    objects and arrays appear with type "object" or "array", and what
    they contain appears underneath them.

    Arrays are described by the shape of their elements, merged across
    all elements and written under the ``"[]"`` segment. Element order
    and count never matter. An empty array has no elements to learn
    from, so its elements get the type "unknown".

    >>> extract_structure({"id": 1, "user": {"name": "Ana"}})
    {('id',): 'number', ('user',): 'object', ('user', 'name'): 'string'}
    >>> extract_structure({"tags": ["a", "b"]})
    {('tags',): 'array', ('tags', '[]'): 'string'}
    >>> extract_structure([{"id": 1}, {"id": 2, "name": None}])
    {('[]',): 'object', ('[]', 'id'): 'number', ('[]', 'name'): 'null'}
    """
    if not isinstance(data, (dict, list)):
        _json_type(data, ())  # raises for non-JSON values
        raise UnsupportedStructureError("the top level must be a JSON object or array")

    types: dict[Path, set[str]] = {}
    _visit([data], (), types)
    return {path: "|".join(sorted(names)) for path, names in types.items()}


def _visit(values: list, path: Path, types: dict[Path, set[str]]) -> None:
    """Record everything seen at ``path``, where ``values`` were all found.

    The root has an empty path and is not recorded itself.
    """
    objects: list[dict] = []
    elements: list = []
    saw_array = False
    for value in values:
        type_name = _json_type(value, path)
        if path:
            types.setdefault(path, set()).add(type_name)
        if type_name == "object":
            objects.append(value)
        elif type_name == "array":
            saw_array = True
            elements.extend(value)

    keys: dict[str, list] = {}
    for obj in objects:
        for key, value in obj.items():
            if not isinstance(key, str):
                raise UnsupportedStructureError(
                    f"object keys must be strings, got {type(key).__name__}"
                )
            if key == ARRAY_ELEMENT:
                raise UnsupportedStructureError(
                    f'the key "{ARRAY_ELEMENT}" is reserved (at {"/".join(path) or "<root>"})'
                )
            keys.setdefault(key, []).append(value)
    for key, found in keys.items():
        _visit(found, path + (key,), types)

    if elements:
        _visit(elements, path + (ARRAY_ELEMENT,), types)
    elif saw_array:
        types[path + (ARRAY_ELEMENT,)] = {UNKNOWN}


def compare_structures(old: Any, new: Any) -> list[Finding]:
    """Return the structural differences between two JSON documents.

    Only field paths and types are compared, so changed values, key
    order, array order and formatting never produce findings. Inputs
    are not modified. Findings are sorted by path, so the result is
    always in the same order for the same inputs.

    When a field changes type (e.g. object -> null), the field itself
    is reported as a type change, and any nested fields that appear or
    disappear because of it are reported too.
    """
    return compare_extracted(extract_structure(old), extract_structure(new))


def compare_extracted(old: Structure, new: Structure) -> list[Finding]:
    """Compare two structures already produced by ``extract_structure``.

    This is what ``compare_structures`` uses, exposed so a saved
    structure can be compared without rebuilding JSON from it.

    An array that was empty on either side tells us nothing about its
    elements, so everything under that array's ``"[]"`` is skipped
    instead of being reported as added or absent.
    """
    unseen = [path for path in old.keys() | new.keys()
              if UNKNOWN in (old.get(path), new.get(path))]

    findings: list[Finding] = []
    for path in old.keys() | new.keys():
        if any(path[: len(prefix)] == prefix for prefix in unseen):
            continue
        old_type = old.get(path)
        new_type = new.get(path)
        if old_type is None:
            findings.append(Finding(ChangeKind.ADDED, path, None, new_type))
        elif new_type is None:
            findings.append(Finding(ChangeKind.ABSENT, path, old_type, None))
        elif old_type != new_type:
            findings.append(Finding(ChangeKind.TYPE_CHANGED, path, old_type, new_type))

    # Each path appears at most once, so sorting by path is a total order.
    findings.sort(key=lambda f: f.path)
    return findings
