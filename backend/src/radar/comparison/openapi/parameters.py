"""Request parameters of the operations two contracts share, and what changed between them.

A parameter is identified by where it travels (`in`) and its name. Header names ignore case. A path parameter is
identified by its position in the path template, because a template rename (`{id}` to `{petId}`) is not a different
parameter. Path-level parameters apply to every operation of the path; an operation-level parameter with the same
identity overrides them.

Rules (all `direction: request`), from the point of view of a client written against the earlier contract:

- `parameter.added`            required: potentially breaking (old clients do not send it); optional: non-breaking.
- `parameter.removed`          potentially breaking: an old client may still send it (conservative).
- `parameter.required_changed` optional to required: potentially breaking; required to optional: non-breaking.
- `parameter.schema_changed`   uncertain: the parameter's schema (or `content`) differs. Classifying the difference
                               needs the schema rules, which are a later increment.
- `parameter.encoding_changed` uncertain: `style`, `explode` or `allowReserved` differ, which changes how a value is
                               written on the wire.

Descriptions, examples and deprecation are documentation and are not assessed here.
"""

from dataclasses import dataclass
import json
import re
from typing import Any

from radar.comparison.openapi.operations import Operation, normalize_path
from radar.comparison.openapi.references import Documents, Unresolved
from radar.discovery.references import json_pointer
from radar.domain.comparison import (
    ADDED, CHANGED, NON_BREAKING, POTENTIALLY_BREAKING, REMOVED, REQUEST, UNCERTAIN, ComparisonLimitation,
    ContractFinding, SourceLocation,
)

LOCATIONS = ("path", "query", "header", "cookie")
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_DEFAULT_STYLE = {"path": "simple", "header": "simple", "query": "form", "cookie": "form"}
_UNKNOWN = object()  # a schema that could not be read: not comparable, never equal


@dataclass(frozen=True)
class Parameter:
    identity: tuple  # (in, lower-cased name) or ("path", position)
    name: str
    location_in: str
    required: bool
    shape: Any  # expanded schema or content, or _UNKNOWN
    encoding: tuple  # (style, explode, allowReserved) with the specification defaults applied
    where: SourceLocation


class _Skip(Exception):
    """The parameters of one operation cannot be read on one side; the operation is skipped with a limitation."""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code, self.reason = code, reason


def _identity(location_in: str, name: str, declared_path: str):
    if location_in == "path":
        names = _PLACEHOLDER.findall(declared_path)
        return ("path", names.index(name) if name in names else f"name:{name}")
    return (location_in, name.lower() if location_in == "header" else name)


def _encoding(node: dict, location_in: str):
    style = node.get("style", _DEFAULT_STYLE[location_in])
    explode = node.get("explode", style == "form")
    return style, explode, bool(node.get("allowReserved", False))


def _shape(docs: Documents, node: dict, url: str):
    key = "schema" if "schema" in node else "content" if "content" in node else None
    if key is None:
        return None
    try:
        return {key: docs.expand(node[key], url)}
    except Unresolved:
        return _UNKNOWN


def _read(docs: Documents, entry: Any, url: str, tokens: tuple, declared_path: str) -> Parameter:
    try:
        node, node_url, node_tokens = docs.follow(entry, url, tokens)
    except Unresolved as error:
        raise _Skip("parameter_unresolved", str(error)) from None
    if not isinstance(node, dict) or not isinstance(node.get("name"), str) or node.get("in") not in LOCATIONS:
        raise _Skip("parameter_unreadable", f"The parameter at {json_pointer(tokens)!r} has no readable name and location.")
    location_in, name = node["in"], node["name"]
    return Parameter(
        _identity(location_in, name, declared_path), name, location_in,
        True if location_in == "path" else bool(node.get("required", False)),  # path parameters are always required
        _shape(docs, node, node_url), _encoding(node, location_in), SourceLocation(node_url, json_pointer(node_tokens)))


def effective_parameters(operation: Operation) -> dict[tuple, Parameter]:
    """The parameters that apply to an operation, path-level overridden by operation-level."""
    docs = operation.documents
    merged: dict[tuple, Parameter] = {}
    levels = ((operation.item, operation.item_tokens, operation.item_url),
              (operation.node, (*operation.item_tokens, operation.method.lower()), operation.item_url))
    for container, tokens, url in levels:
        entries = container.get("parameters", [])
        if not isinstance(entries, list):
            raise _Skip("parameter_unreadable", f"The parameters at {json_pointer(tokens)!r} are not a list.")
        seen = set()
        for index, entry in enumerate(entries):
            parameter = _read(docs, entry, url, (*tokens, "parameters", str(index)), operation.declared_path)
            if parameter.identity in seen:
                raise _Skip("parameter_duplicate",
                            f"{parameter.location_in} parameter {parameter.name!r} is declared more than once.")
            seen.add(parameter.identity)
            merged[parameter.identity] = parameter  # the operation level is read second, so it overrides
    return merged


def _shown(parameter: Parameter):
    return {"name": parameter.name, "in": parameter.location_in, "required": parameter.required}


def _key(rule: str, operation: Operation, identity: tuple):
    return f"{rule}|{operation.method}|{normalize_path(operation.declared_path)}|{identity[0]}:{identity[1]}"


def _finding(rule, category, kind, operation, before, after, explanation, before_value=None, after_value=None):
    parameter = after or before
    return ContractFinding(
        key=_key(rule, operation, parameter.identity), rule_id=rule, category=category, change_kind=kind,
        direction=REQUEST, method=operation.method, path=operation.declared_path,
        before_present=before is not None, after_present=after is not None,
        before_value=before_value if before_value is not None else (_shown(before) if before else None),
        after_value=after_value if after_value is not None else (_shown(after) if after else None),
        before_location=before.where if before else None, after_location=after.where if after else None,
        explanation=explanation)


def _same_shape(before: Parameter, after: Parameter):
    if before.shape is _UNKNOWN or after.shape is _UNKNOWN:
        return None
    return json.dumps(before.shape, sort_keys=True) == json.dumps(after.shape, sort_keys=True)


def compare_parameters(before_op: Operation, after_op: Operation, limitations: list) -> list[ContractFinding]:
    """Findings for one operation present in both contracts. A side that cannot be read yields a limitation."""
    label = f"{after_op.method} {after_op.declared_path}"
    try:
        before, after = effective_parameters(before_op), effective_parameters(after_op)
    except _Skip as skip:
        limitations.append(ComparisonLimitation(
            skip.code, f"The parameters of {label} could not be read: {skip.reason} They were not compared.", label, True))
        return []
    findings = []
    for identity in sorted(before.keys() | after.keys(), key=str):
        old, new = before.get(identity), after.get(identity)
        where = f"{new.location_in if new else old.location_in} parameter {(new or old).name!r} of {label}"
        if old is None:
            category = POTENTIALLY_BREAKING if new.required else NON_BREAKING
            effect = ("Existing clients do not send it, so they may be rejected." if new.required
                      else "Existing clients may omit it, so they are not affected.")
            findings.append(_finding("parameter.added", category, ADDED, after_op, None, new,
                                     f"The {'required' if new.required else 'optional'} {where} is newly declared. {effect}"))
        elif new is None:
            findings.append(_finding("parameter.removed", POTENTIALLY_BREAKING, REMOVED, after_op, old, None,
                                     f"The {where} is no longer declared. Existing clients may still send it."))
        else:
            if old.required != new.required:
                category = POTENTIALLY_BREAKING if new.required else NON_BREAKING
                effect = ("Existing clients that omit it may now be rejected." if new.required
                          else "Existing clients that send it are still accepted.")
                findings.append(_finding(
                    "parameter.required_changed", category, CHANGED, after_op, old, new,
                    f"The {where} changed from {'required' if old.required else 'optional'} to "
                    f"{'required' if new.required else 'optional'}. {effect}"))
            same = _same_shape(old, new)
            if same is None:
                limitations.append(ComparisonLimitation(
                    "parameter_schema_unresolved", f"The schema of the {where} could not be read in full, so it was "
                    "not compared.", label, True))
            elif not same:
                findings.append(_finding(
                    "parameter.schema_changed", UNCERTAIN, CHANGED, after_op, old, new,
                    f"The schema of the {where} differs. Whether that affects clients is not classified yet.",
                    old.shape, new.shape))
            if old.encoding != new.encoding:
                findings.append(_finding(
                    "parameter.encoding_changed", UNCERTAIN, CHANGED, after_op, old, new,
                    f"The style, explode or allowReserved setting of the {where} changed, which changes how a value "
                    "is written in the request.", list(old.encoding), list(new.encoding)))
    return findings
