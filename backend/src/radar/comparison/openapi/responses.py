"""The responses of the operations two contracts share, and what changed between them. Direction is `response`.

Responses are matched by status code (`2XX` and `200` are different keys; `default` is its own key) and then by media
type (case-insensitive). Rules, from the point of view of a client written against the earlier contract:

- `response.removed`      a removed `2xx` is potentially breaking: the success outcome the client relies on is gone.
                          Any other removed status (an error, a redirect, `default`) is uncertain.
- `response.added`        uncertain: the client may not expect a new outcome, and nothing says whether it matters.
- `response.media_removed` potentially breaking: the client may be asking for that format.
- `response.media_added`  uncertain.
- schemas under a media type present in both are compared with the `schemas` rules for the response direction.

Response headers and links are not assessed.
"""

from dataclasses import dataclass

from radar.comparison.openapi.operations import Operation, normalize_path
from radar.comparison.openapi.references import Unresolved
from radar.comparison.openapi.schemas import media_schema_ref, schema_findings
from radar.discovery.references import json_pointer
from radar.domain.comparison import (
    ADDED, POTENTIALLY_BREAKING, REMOVED, RESPONSE, UNCERTAIN, ComparisonLimitation, ContractFinding, SourceLocation,
)


@dataclass
class Response:
    status: str
    media: dict  # normalized media type -> (media object, url, tokens)
    where: SourceLocation


def read_responses(operation: Operation) -> dict[str, Response]:
    """The operation's responses by normalized status. Raises Unresolved if they cannot be read."""
    responses = operation.node.get("responses", {})
    if not isinstance(responses, dict):
        raise Unresolved("The responses are not an object.")
    base = (*operation.item_tokens, operation.method.lower(), "responses")
    found = {}
    for raw, entry in responses.items():
        status = str(raw).upper()
        if status in found:
            raise Unresolved(f"The status {raw!r} is declared twice.")
        node, url, tokens = operation.documents.follow(entry, operation.item_url, (*base, raw))
        if not isinstance(node, dict) or not isinstance(node.get("content", {}), dict):
            raise Unresolved(f"The response {raw!r} is not an object with a content map.")
        media = {}
        for name, value in node.get("content", {}).items():
            key = str(name).lower().replace(" ", "")
            if key in media:
                raise Unresolved(f"The media type {name!r} of response {raw!r} is declared twice.")
            media[key] = (value, url, (*tokens, "content", name))
        found[status] = Response(status, media, SourceLocation(url, json_pointer(tokens)))
    return found


def _finding(rule, category, kind, operation, status, before, after, explanation, media=None):
    scope = f"response:{status}" + (f":{media}" if media else "")
    where = lambda r: None if r is None else (  # noqa: E731
        SourceLocation(r.media[media][1], json_pointer(r.media[media][2])) if media else r.where)
    return ContractFinding(
        key=f"{rule}|{operation.method}|{normalize_path(operation.declared_path)}|{scope}", rule_id=rule,
        category=category, change_kind=kind, direction=RESPONSE, method=operation.method, path=operation.declared_path,
        before_present=before is not None, after_present=after is not None,
        before_value=None if before is None or media else {"media_types": sorted(before.media)},
        after_value=None if after is None or media else {"media_types": sorted(after.media)},
        before_location=where(before), after_location=where(after), explanation=explanation,
        subject=f"response {status}" + (f" ({media})" if media else ""))


def compare_responses(before_op: Operation, after_op: Operation, limitations: list) -> list[ContractFinding]:
    label = f"{after_op.method} {after_op.declared_path}"
    try:
        before, after = read_responses(before_op), read_responses(after_op)
    except Unresolved as error:
        limitations.append(ComparisonLimitation(
            "response_unresolved", f"The responses of {label} could not be read: {error} They were not compared.", label, True))
        return []
    findings = []
    for status in sorted(before.keys() | after.keys()):
        old, new = before.get(status), after.get(status)
        if old is None:
            findings.append(_finding(
                "response.added", UNCERTAIN, ADDED, after_op, status, None, new,
                f"{label} now declares a {status} response. A client may not expect that outcome; whether it matters "
                "is not classified."))
            continue
        if new is None:
            success = status.startswith("2")
            findings.append(_finding(
                "response.removed", POTENTIALLY_BREAKING if success else UNCERTAIN, REMOVED, after_op, status, old, None,
                f"{label} no longer declares a {status} response. " + (
                    "Clients rely on the success outcome." if success else
                    "Clients may handle it, or the API may now report this outcome differently.")))
            continue
        for name in sorted(old.media.keys() | new.media.keys()):
            if name not in old.media:
                findings.append(_finding(
                    "response.media_added", UNCERTAIN, ADDED, after_op, status, None, new,
                    f"The {status} response of {label} can now also return {name}. Whether clients are affected is "
                    "not classified.", name))
            elif name not in new.media:
                findings.append(_finding(
                    "response.media_removed", POTENTIALLY_BREAKING, REMOVED, after_op, status, old, None,
                    f"The {status} response of {label} no longer returns {name}. Clients may be asking for it.", name))
            else:
                try:
                    old_ref = media_schema_ref(before_op.documents, old.media[name])
                    new_ref = media_schema_ref(after_op.documents, new.media[name])
                except Unresolved as error:
                    limitations.append(ComparisonLimitation(
                        "schema_unresolved", f"The {status} {name} response of {label} could not be read: {error}",
                        label, True))
                    continue
                findings.extend(schema_findings(
                    after_op.method, after_op.declared_path, normalize_path(after_op.declared_path),
                    f"response:{status}:{name}", f"response {status} ({name})", old_ref, new_ref, RESPONSE, limitations))
    return findings
