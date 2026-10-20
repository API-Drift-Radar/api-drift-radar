"""Documentation and deprecation changes, at the places where OpenAPI documents things.

Rules, none of which depends on direction:

- `documentation.changed`  documentation only: a `summary`, `description`, `title`, `example(s)`, `externalDocs` or
                           `info` text was added, removed or reworded. It does not change what the API accepts or
                           returns. Whitespace and line wrapping are not a change.
- `deprecation.changed`    non-breaking under our rules: a `deprecated` flag was set or cleared. The thing still works
                           today; it is a signal, not a break.
- `operation.id_changed`   uncertain: `operationId` is not documentation. Generated clients may use it as a method
                           name, so a change can break code that is generated from the contract.

Defaults and formats are constraints and are handled by the schema rules, not here. Tags and security requirements are
not assessed.
"""

import json
from typing import Any

from radar.comparison.openapi.operations import normalize_path
from radar.domain.comparison import (
    ADDED, CHANGED, DOCUMENTATION_ONLY, NO_DIRECTION, NON_BREAKING, REMOVED, UNCERTAIN, ContractFinding,
    SourceLocation,
)

MAX_SHOWN = 1000
MISSING = object()
OPERATION_FIELDS = ("summary", "description", "externalDocs", "deprecated")
PATH_ITEM_FIELDS = ("summary", "description")
PARAMETER_FIELDS = ("description", "example", "examples", "deprecated")
BODY_FIELDS = ("description",)
RESPONSE_FIELDS = ("description",)
MEDIA_FIELDS = ("example", "examples")
INFO_FIELDS = ("title", "summary", "description", "version", "termsOfService")


def comparable(value):
    """Texts compare without regard to whitespace or wrapping; everything else compares as canonical JSON."""
    if isinstance(value, str):
        return "t:" + " ".join(value.split())
    return "j:" + json.dumps(value, sort_keys=True, default=str)


def shown(value: Any):
    """A bounded copy for display; the location points at the full value."""
    if value is MISSING:
        return None
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    if len(text) <= MAX_SHOWN:
        return value
    return text[:MAX_SHOWN] + "…"


def at(location: SourceLocation | None, field: str) -> SourceLocation | None:
    if location is None:
        return None
    return SourceLocation(location.document_url, location.pointer + "/" + field.replace("~", "~0").replace("/", "~1"))


def finding(method, path, scope, field, label, old, new, before_loc, after_loc) -> ContractFinding | None:
    """A documentation or deprecation finding for one field, or None if the field did not change."""
    key_tail = f"{scope}|{field}"
    base = f"{method or ''}|{normalize_path(path) if path else ''}|{key_tail}"
    if field == "deprecated":
        was, now = old is True, new is True
        if was == now:
            return None
        return ContractFinding(
            key=f"deprecation.changed|{base}", rule_id="deprecation.changed", category=NON_BREAKING, change_kind=CHANGED,
            direction=NO_DIRECTION, method=method, path=path, before_present=True, after_present=True,
            before_value=was, after_value=now, before_location=at(before_loc, field) if old is not MISSING else before_loc,
            after_location=at(after_loc, field) if new is not MISSING else after_loc,
            explanation=(f"{label} is now deprecated. It still works today but may be removed later."
                         if now else f"{label} is no longer deprecated."),
            subject=f"{label} > deprecated")
    if old is MISSING and new is MISSING:
        return None
    if old is not MISSING and new is not MISSING and comparable(old) == comparable(new):
        return None
    kind = ADDED if old is MISSING else REMOVED if new is MISSING else CHANGED
    verb = {ADDED: "was added", REMOVED: "was removed", CHANGED: "changed"}[kind]
    extra = " The API's declared version label does not by itself say anything about compatibility." \
        if scope == "info" and field == "version" else ""
    return ContractFinding(
        key=f"documentation.changed|{base}", rule_id="documentation.changed", category=DOCUMENTATION_ONLY,
        change_kind=kind, direction=NO_DIRECTION, method=method, path=path, before_present=old is not MISSING,
        after_present=new is not MISSING, before_value=shown(old), after_value=shown(new),
        before_location=at(before_loc, field) if old is not MISSING else None,
        after_location=at(after_loc, field) if new is not MISSING else None,
        explanation=f"The {field} of {label} {verb}. This is documentation; it does not change what the API accepts "
                    f"or returns.{extra}",
        subject=f"{label} > {field}")


def compare_fields(method, path, scope, label, old_node, new_node, before_loc, after_loc, fields):
    """Findings for the `fields` of two objects (dicts, or None when absent)."""
    old_node = old_node if isinstance(old_node, dict) else {}
    new_node = new_node if isinstance(new_node, dict) else {}
    found = []
    for field in fields:
        result = finding(method, path, scope, field, label, old_node.get(field, MISSING), new_node.get(field, MISSING),
                         before_loc, after_loc)
        if result is not None:
            found.append(result)
    return found


def compare_info(before: dict, after: dict, before_url: str, after_url: str):
    return compare_fields(None, None, "info", "the contract's info", before.get("info"), after.get("info"),
                          SourceLocation(before_url, "/info"), SourceLocation(after_url, "/info"), INFO_FIELDS)


def compare_operation_id(before_op, after_op) -> list[ContractFinding]:
    old, new = before_op.node.get("operationId", MISSING), after_op.node.get("operationId", MISSING)
    if old == new:
        return []
    label = f"{after_op.method} {after_op.declared_path}"
    return [ContractFinding(
        key=f"operation.id_changed|{after_op.method}|{normalize_path(after_op.declared_path)}",
        rule_id="operation.id_changed", category=UNCERTAIN, change_kind=CHANGED, direction=NO_DIRECTION,
        method=after_op.method, path=after_op.declared_path, before_present=old is not MISSING,
        after_present=new is not MISSING, before_value=None if old is MISSING else old,
        after_value=None if new is MISSING else new,
        before_location=at(before_op.location, "operationId") if old is not MISSING else None,
        after_location=at(after_op.location, "operationId") if new is not MISSING else None,
        explanation=f"The operationId of {label} changed. Clients generated from the contract may use it as a method "
                    "name, so code generated against the earlier contract may break.",
        subject=f"{label} > operationId")]


def compare_operation_documentation(before_op, after_op) -> list[ContractFinding]:
    """Operation-level and path-item-level documentation and deprecation, plus the operation id."""
    label = f"{after_op.method} {after_op.declared_path}"
    found = compare_fields(after_op.method, after_op.declared_path, "operation", label, before_op.node, after_op.node,
                           before_op.location, after_op.location, OPERATION_FIELDS)
    found += compare_operation_id(before_op, after_op)
    item_label = f"the path {after_op.declared_path}"
    found += compare_fields(None, after_op.declared_path, "path-item", item_label, before_op.item, after_op.item,
                            SourceLocation(before_op.item_url, _pointer(before_op.item_tokens)),
                            SourceLocation(after_op.item_url, _pointer(after_op.item_tokens)), PATH_ITEM_FIELDS)
    return found


def _pointer(tokens):
    return "".join("/" + str(t).replace("~", "~0").replace("/", "~1") for t in tokens)
