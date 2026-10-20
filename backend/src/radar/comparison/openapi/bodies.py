"""The request body of the operations two contracts share, and what changed between them. Direction is `request`.

Rules, from the point of view of a client written against the earlier contract:

- `request_body.added`            required: potentially breaking; optional: non-breaking under our rules.
- `request_body.removed`          potentially breaking: an existing client may still send a body (conservative).
- `request_body.required_changed` optional to required: potentially breaking; required to optional: non-breaking.
- `request_body.media_added`      non-breaking: clients may keep using the media types they know.
- `request_body.media_removed`    potentially breaking: a client may be sending that media type.
- schemas under a media type present in both are compared with the `schemas` rules (`schema.*`).

Media types are compared case-insensitively. Media-type `encoding` and `examples` are not assessed.
"""

from dataclasses import dataclass, field

from radar.comparison.openapi.documentation import BODY_FIELDS, MEDIA_FIELDS, compare_fields
from radar.comparison.openapi.operations import Operation, normalize_path
from radar.comparison.openapi.references import Unresolved
from radar.comparison.openapi.schemas import media_schema_ref, schema_findings
from radar.discovery.references import json_pointer
from radar.domain.comparison import (
    ADDED, CHANGED, NON_BREAKING, POTENTIALLY_BREAKING, REMOVED, REQUEST, ComparisonLimitation, ContractFinding,
    SourceLocation,
)


@dataclass
class RequestBody:
    required: bool
    media: dict  # normalized media type -> (media object, url, tokens)
    where: SourceLocation
    node: dict = field(compare=False, default_factory=dict)


def read_request_body(operation: Operation) -> RequestBody | None:
    """The operation's request body, or None if it declares none. Raises Unresolved if it cannot be read."""
    entry = operation.node.get("requestBody")
    if entry is None:
        return None
    tokens = (*operation.item_tokens, operation.method.lower(), "requestBody")
    node, url, node_tokens = operation.documents.follow(entry, operation.item_url, tokens)
    if not isinstance(node, dict) or not isinstance(node.get("content", {}), dict):
        raise Unresolved("The request body is not an object with a content map.")
    media = {}
    for name, value in node.get("content", {}).items():
        key = str(name).lower().replace(" ", "")
        if key in media:
            raise Unresolved(f"The media type {name!r} is declared twice.")
        media[key] = (value, url, (*node_tokens, "content", name))
    return RequestBody(node.get("required") is True, media, SourceLocation(url, json_pointer(node_tokens)), node)


def _finding(rule, category, kind, operation, label, before, after, explanation, suffix=""):
    return ContractFinding(
        key=f"{rule}|{operation.method}|{normalize_path(operation.declared_path)}|request-body{suffix}", rule_id=rule,
        category=category, change_kind=kind, direction=REQUEST, method=operation.method, path=operation.declared_path,
        before_present=before is not None, after_present=after is not None,
        before_value=None if before is None else {"required": before.required, "media_types": sorted(before.media)},
        after_value=None if after is None else {"required": after.required, "media_types": sorted(after.media)},
        before_location=None if before is None else before.where, after_location=None if after is None else after.where,
        explanation=explanation, subject=label)


def _media_finding(rule, category, kind, operation, name, before, after, explanation):
    location = lambda body: None if body is None else SourceLocation(  # noqa: E731
        body.media[name][1], json_pointer(body.media[name][2]))
    return ContractFinding(
        key=f"{rule}|{operation.method}|{normalize_path(operation.declared_path)}|request-body:{name}", rule_id=rule,
        category=category, change_kind=kind, direction=REQUEST, method=operation.method, path=operation.declared_path,
        before_present=before is not None, after_present=after is not None, before_value=None, after_value=None,
        before_location=location(before), after_location=location(after), explanation=explanation,
        subject=f"request body ({name})")


def compare_request_bodies(before_op: Operation, after_op: Operation, limitations: list) -> list[ContractFinding]:
    label_scope = f"{after_op.method} {after_op.declared_path}"
    try:
        before, after = read_request_body(before_op), read_request_body(after_op)
    except Unresolved as error:
        limitations.append(ComparisonLimitation(
            "request_body_unresolved", f"The request body of {label_scope} could not be read: {error} It was not compared.",
            label_scope, True))
        return []
    if before is None and after is None:
        return []
    operation = after_op
    if before is None:
        category = POTENTIALLY_BREAKING if after.required else NON_BREAKING
        effect = ("Existing clients do not send one, so they may be rejected." if after.required
                  else "Existing clients may omit it, so they are not affected.")
        return [_finding("request_body.added", category, ADDED, operation, "request body", None, after,
                         f"{label_scope} now declares a {'required' if after.required else 'optional'} request body. {effect}")]
    if after is None:
        return [_finding("request_body.removed", POTENTIALLY_BREAKING, REMOVED, operation, "request body", before, None,
                         f"{label_scope} no longer declares a request body. Existing clients may still send one.")]
    findings = []
    if before.required != after.required:
        category = POTENTIALLY_BREAKING if after.required else NON_BREAKING
        effect = ("Clients that omit it may now be rejected." if after.required
                  else "Clients that send it are still accepted.")
        findings.append(_finding(
            "request_body.required_changed", category, CHANGED, operation, "request body", before, after,
            f"The request body of {label_scope} changed from {'required' if before.required else 'optional'} to "
            f"{'required' if after.required else 'optional'}. {effect}"))
    findings.extend(compare_fields(
        after_op.method, after_op.declared_path, "request-body", f"the request body of {label_scope}", before.node,
        after.node, before.where, after.where, BODY_FIELDS))
    for name in sorted(before.media.keys() | after.media.keys()):
        if name not in before.media:
            findings.append(_media_finding(
                "request_body.media_added", NON_BREAKING, ADDED, operation, name, None, after,
                f"{label_scope} now also accepts {name}. Existing clients can keep using the media types they know."))
        elif name not in after.media:
            findings.append(_media_finding(
                "request_body.media_removed", POTENTIALLY_BREAKING, REMOVED, operation, name, before, None,
                f"{label_scope} no longer accepts {name}. Existing clients may be sending it."))
        else:
            try:
                old_ref = media_schema_ref(before_op.documents, before.media[name])
                new_ref = media_schema_ref(after_op.documents, after.media[name])
            except Unresolved as error:
                limitations.append(ComparisonLimitation(
                    "schema_unresolved", f"The {name} request body of {label_scope} could not be read: {error}",
                    label_scope, True))
                continue
            old_media, new_media = before.media[name], after.media[name]
            findings.extend(compare_fields(
                after_op.method, after_op.declared_path, f"request-body:{name}", f"the {name} request body of {label_scope}",
                old_media[0], new_media[0], SourceLocation(old_media[1], json_pointer(old_media[2])),
                SourceLocation(new_media[1], json_pointer(new_media[2])), MEDIA_FIELDS))
            findings.extend(schema_findings(
                after_op.method, after_op.declared_path, normalize_path(after_op.declared_path), f"request-body:{name}",
                f"request body ({name})", old_ref, new_ref, REQUEST, limitations))
    return findings
