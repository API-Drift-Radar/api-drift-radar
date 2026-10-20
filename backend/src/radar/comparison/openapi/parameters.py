"""Request parameters of the operations two contracts share, and what changed between them.

A parameter is identified by where it travels (`in`) and its name. Header names ignore case. A path parameter is
identified by its position in the path template, because a template rename (`{id}` to `{petId}`) is not a different
parameter. Path-level parameters apply to every operation of the path; an operation-level parameter with the same
identity overrides them.

Rules (all `direction: request`), from the point of view of a client written against the earlier contract:

- `parameter.added`            required: potentially breaking (old clients do not send it); optional: non-breaking.
- `parameter.removed`          potentially breaking: an old client may still send it (conservative).
- `parameter.required_changed` optional to required: potentially breaking; required to optional: non-breaking.
- `parameter.schema_changed`   uncertain: the parameter switched between `schema` and `content`, or its `content` media
                               types differ, so the schemas cannot be paired.
- `schema.*`                   the parameter's schema (or each `content` schema) compared with the request-direction
                               schema rules in `schemas.py`.
- `parameter.encoding_changed` uncertain: `style`, `explode` or `allowReserved` differ, which changes how a value is
                               written on the wire.

Descriptions, examples and deprecation are documentation and are not assessed here.
"""

from dataclasses import dataclass, field
import re
from typing import Any

from radar.comparison.openapi.documentation import PARAMETER_FIELDS, compare_fields
from radar.comparison.openapi.operations import Operation, normalize_path
from radar.comparison.openapi.references import Documents, Unresolved
from radar.comparison.openapi.schemas import SchemaRef, schema_findings
from radar.discovery.references import json_pointer
from radar.domain.comparison import (
    ADDED, CHANGED, NON_BREAKING, POTENTIALLY_BREAKING, REMOVED, REQUEST, UNCERTAIN, ComparisonLimitation,
    ContractFinding, SourceLocation,
)

LOCATIONS = ("path", "query", "header", "cookie")
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_DEFAULT_STYLE = {"path": "simple", "header": "simple", "query": "form", "cookie": "form"}


@dataclass(frozen=True)
class Parameter:
    identity: tuple  # (in, lower-cased name) or ("path", position)
    name: str
    location_in: str
    required: bool
    schema: SchemaRef | None  # the `schema`, as written
    content: dict | None  # media type -> SchemaRef, when the parameter uses `content` instead of `schema`
    encoding: tuple  # (style, explode, allowReserved) with the specification defaults applied
    where: SourceLocation
    node: dict = field(compare=False, default_factory=dict)  # the parameter object, for its documentation


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


def _schemas(docs: Documents, node: dict, url: str, tokens: tuple):
    """(schema, content) of a parameter node, each as written. A malformed `content` is treated as unreadable."""
    schema = SchemaRef(docs, node["schema"], url, (*tokens, "schema")) if "schema" in node else None
    content = None
    if isinstance(node.get("content"), dict):
        content = {str(name).lower().replace(" ", ""): SchemaRef(docs, (value or {}).get("schema", {}), url,
                                                                  (*tokens, "content", name, "schema"))
                   for name, value in node["content"].items() if isinstance(value, dict)}
    return schema, content


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
        *_schemas(docs, node, node_url, node_tokens), _encoding(node, location_in),
        SourceLocation(node_url, json_pointer(node_tokens)), node)


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
        explanation=explanation, subject=f"{parameter.location_in} parameter {parameter.name}")


def _schema_pairs(old: Parameter, new: Parameter):
    """((media or None), before SchemaRef, after SchemaRef) pairs, or None if the two cannot be paired."""
    if old.schema is not None and new.schema is not None:
        return [(None, old.schema, new.schema)]
    if old.content is not None and new.content is not None and old.content.keys() == new.content.keys():
        return [(name, old.content[name], new.content[name]) for name in sorted(old.content)]
    if old.schema is None and new.schema is None and old.content is None and new.content is None:
        return []
    return None


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
            pairs = _schema_pairs(old, new)
            if pairs is None:
                findings.append(_finding(
                    "parameter.schema_changed", UNCERTAIN, CHANGED, after_op, old, new,
                    f"The {where} switched between `schema` and `content`, or its media types differ, so its "
                    "schemas cannot be compared. Whether that affects clients is not classified."))
            for media, old_schema, new_schema in pairs or []:
                scope = f"parameter:{identity[0]}:{identity[1]}" + (f":{media}" if media else "")
                findings.extend(schema_findings(
                    after_op.method, after_op.declared_path, normalize_path(after_op.declared_path), scope,
                    f"{where}" + (f" ({media})" if media else ""), old_schema, new_schema, REQUEST, limitations))
            findings.extend(compare_fields(
                after_op.method, after_op.declared_path, f"parameter:{identity[0]}:{identity[1]}", where, old.node, new.node,
                old.where, new.where, PARAMETER_FIELDS))
            if old.encoding != new.encoding:
                findings.append(_finding(
                    "parameter.encoding_changed", UNCERTAIN, CHANGED, after_op, old, new,
                    f"The style, explode or allowReserved setting of the {where} changed, which changes how a value "
                    "is written in the request.", list(old.encoding), list(new.encoding)))
    return findings
