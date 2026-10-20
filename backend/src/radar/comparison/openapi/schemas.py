"""Compare two schemas in one direction and classify what changed, for a client written against the earlier one.

`direction` is `request` (what a client sends) or `response` (what it receives). The same change can be safe in one
direction and breaking in the other:

| change                         | request                | response               |
|--------------------------------|------------------------|------------------------|
| type or enum accepts more      | non-breaking           | potentially breaking   |
| type or enum accepts less      | potentially breaking   | non-breaking           |
| incompatible type              | potentially breaking   | potentially breaking   |
| allows `null` (was forbidden)  | non-breaking           | potentially breaking   |
| forbids `null` (was allowed)   | potentially breaking   | non-breaking           |
| optional property -> required  | potentially breaking   | non-breaking           |
| required property -> optional  | non-breaking           | potentially breaking   |
| property added                 | breaking if required   | non-breaking (*)       |
| property removed               | potentially breaking   | potentially breaking   |

(*) Under a tolerant-reader assumption: a client ignores response fields it does not know. If the earlier object was
closed (`additionalProperties: false`) the change is `uncertain` instead. `integer` is contained in `number`. Request
and response rules assume nothing else about the receiving side.

Anything this module does not classify is reported as `uncertain` and blocks a confident "non-breaking" conclusion
for that schema and everything below it: changed constraints (`minimum`, `pattern`, `format`, `default`, ...), changed
unsupported keywords (`allOf`, `oneOf`, `anyOf`, `not`, `additionalProperties`, ...), and any non-breaking change
under a schema that uses composition. Properties that are `readOnly` are not sent in requests and `writeOnly` ones
are not received in responses, so changes to them are ignored in that direction.
"""

from dataclasses import dataclass
import json
from typing import Any

from radar.comparison.openapi.references import Documents, Unresolved
from radar.discovery.references import json_pointer
from radar.domain.comparison import (
    ADDED, CHANGED, NON_BREAKING, POTENTIALLY_BREAKING, REMOVED, REQUEST, UNCERTAIN, ComparisonLimitation,
    ContractFinding, SourceLocation,
)

MAX_CHANGES = 200
HANDLED = frozenset({"type", "nullable", "enum", "properties", "required", "items", "readOnly", "writeOnly"})
CONSTRAINTS = frozenset({
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength", "maxLength", "pattern",
    "minItems", "maxItems", "uniqueItems", "minProperties", "maxProperties", "format", "default"})
COMPOSITION = frozenset({"allOf", "oneOf", "anyOf", "not", "if", "then", "else"})
_MISSING = object()
BLOCKED_NOTE = (" Constraints or constructs that this comparison does not classify also apply here, so this is not "
                "confidently safe.")


@dataclass(frozen=True)
class SchemaChange:
    rule_id: str
    category: str
    change_kind: str
    steps: tuple  # ("p", name) for a property, ("i",) for array items, from the schema root
    keyword: str | None
    before_present: bool
    after_present: bool
    before_value: Any
    after_value: Any
    explanation: str


def _canon(value):
    return json.dumps(value, sort_keys=True) if value is not _MISSING else _MISSING


def _node(value):
    if isinstance(value, dict):
        return value
    return {"not": {}} if value is False else {}  # boolean schemas: true accepts anything, false nothing


def _types(node):
    declared = node.get("type")
    if declared is None:
        return None  # any type
    found = set(declared) if isinstance(declared, list) else {declared}
    if node.get("nullable") is True:
        found.add("null")
    return frozenset(found)


def _contains(wide, narrow):
    """Does a schema with types `wide` accept everything one with types `narrow` accepts?"""
    if wide is None:
        return True
    if narrow is None:
        return False
    return all(t in wide or (t == "integer" and "number" in wide) for t in narrow)


def _path_text(steps):
    return json_pointer(tuple(step[1] if step[0] == "p" else "[]" for step in steps)) or "/"


def _where(steps):
    return "the schema" if not steps else "`" + ".".join(step[1] if step[0] == "p" else "[]" for step in steps) + "`"


class _Differ:
    def __init__(self, direction: str):
        self.direction = direction
        self.changes: list[SchemaChange] = []
        self.unclassified = False
        self.truncated = False

    # -- helpers -----------------------------------------------------------------------------------
    def _emit(self, rule, category, kind, steps, blocked, explanation, before=None, after=None, *, keyword=None,
              before_present=True, after_present=True):
        if len(self.changes) >= MAX_CHANGES:
            self.truncated = True
            return
        if blocked and category == NON_BREAKING:
            category, explanation = UNCERTAIN, explanation + BLOCKED_NOTE
            self.unclassified = True
        self.changes.append(SchemaChange(rule, category, kind, tuple(steps), keyword, before_present, after_present,
                                         before, after, explanation))

    def _by_direction(self, after_accepts_more: bool):
        """Category for a change that makes the schema accept more (or less)."""
        if self.direction == REQUEST:
            return NON_BREAKING if after_accepts_more else POTENTIALLY_BREAKING
        return POTENTIALLY_BREAKING if after_accepts_more else NON_BREAKING

    def _effect(self, after_accepts_more: bool):
        sent = self.direction == REQUEST
        if after_accepts_more:
            return ("Clients that send values the old schema allowed are still accepted." if sent
                    else "Clients may now receive values they were not written to handle.")
        return ("Clients that send values the old schema allowed may now be rejected." if sent
                else "Clients receive a subset of what they already handled.")

    # -- the walk ----------------------------------------------------------------------------------
    def diff(self, before, after, steps=(), blocked=False):
        before, after = _node(before), _node(after)
        if "$cycle" in before or "$cycle" in after:
            if before.get("$cycle") != after.get("$cycle"):
                self._emit("schema.unsupported_changed", UNCERTAIN, CHANGED, steps, blocked,
                           f"A recursive reference at {_where(steps)} changed.", keyword="$cycle")
                self.unclassified = True
            return
        blocked = blocked or bool((COMPOSITION & before.keys()) or (COMPOSITION & after.keys()))
        if blocked:
            self.unclassified = True
        blocked = self._residual(before, after, steps, blocked)
        before_types, after_types = _types(before), _types(after)
        incompatible = self._types(before_types, after_types, steps, blocked)
        self._enum(before, after, steps, blocked)
        if incompatible:
            return
        if self._has(before_types, "object") and self._has(after_types, "object"):
            self._properties(before, after, steps, blocked)
        if self._has(before_types, "array") and self._has(after_types, "array") and ("items" in before or "items" in after):
            self.diff(before.get("items", {}), after.get("items", {}), (*steps, ("i",)), blocked)

    @staticmethod
    def _has(types, name):
        return types is None or name in types

    def _residual(self, before, after, steps, blocked):
        keys = (before.keys() | after.keys()) - HANDLED
        for key in sorted(keys, key=str):
            if _canon(before.get(key, _MISSING)) == _canon(after.get(key, _MISSING)):
                continue
            blocked = True
            self.unclassified = True
            constraint = key in CONSTRAINTS
            self._emit("schema.constraint_changed" if constraint else "schema.unsupported_changed", UNCERTAIN, CHANGED,
                       steps, blocked,
                       f"`{key}` changed at {_where(steps)}. This comparison does not classify "
                       f"{'constraint' if constraint else 'such'} changes yet.",
                       before.get(key), after.get(key), keyword=key,
                       before_present=key in before, after_present=key in after)
        return blocked

    def _types(self, before, after, steps, blocked) -> bool:
        """Report a type change. Returns True when the types are incompatible (nothing below is comparable)."""
        if before == after:
            return False
        no_null = lambda t: None if t is None else frozenset(t - {"null"})  # noqa: E731
        nullability_only = (before is not None and after is not None and no_null(before) == no_null(after))
        shown = lambda t: "any type" if t is None else "/".join(sorted(t))  # noqa: E731
        listed = lambda t: None if t is None else sorted(t)  # noqa: E731
        if _contains(after, before) or _contains(before, after):
            more = _contains(after, before)
            rule = "schema.nullability_changed" if nullability_only else "schema.type_changed"
            what = ("`null` is now allowed" if more else "`null` is no longer allowed") if nullability_only else \
                f"The accepted type changed from {shown(before)} to {shown(after)}"
            self._emit(rule, self._by_direction(more), CHANGED, steps, blocked,
                       f"At {_where(steps)}: {what}. {self._effect(more)}", listed(before), listed(after))
            return False
        self._emit("schema.type_changed", POTENTIALLY_BREAKING, CHANGED, steps, blocked,
                   f"At {_where(steps)} the type changed from {shown(before)} to {shown(after)}, which are not "
                   "compatible. Existing clients are likely affected.", listed(before), listed(after))
        return True

    def _enum(self, before, after, steps, blocked):
        old, new = before.get("enum"), after.get("enum")
        if _canon(old if old is not None else _MISSING) == _canon(new if new is not None else _MISSING):
            return
        key = lambda member: json.dumps(member, sort_keys=True)  # noqa: E731
        if old is None or new is None:
            more = old is not None  # no enum afterwards: anything is accepted
            self._emit("schema.enum_changed", self._by_direction(more), CHANGED, steps, blocked,
                       f"At {_where(steps)} the allowed values are {'no longer restricted' if more else 'now restricted'}. "
                       f"{self._effect(more)}", old, new)
            return
        gone = [m for m in old if key(m) not in {key(n) for n in new}]
        came = [m for m in new if key(m) not in {key(o) for o in old}]
        if not gone and not came:
            return
        if gone and came:
            self._emit("schema.enum_changed", POTENTIALLY_BREAKING, CHANGED, steps, blocked,
                       f"At {_where(steps)} allowed values were both removed ({len(gone)}) and added ({len(came)}), so "
                       "existing clients may be affected in either direction.", old, new)
            return
        more = bool(came)
        names = ", ".join(json.dumps(m) for m in (came or gone)[:5])
        self._emit("schema.enum_changed", self._by_direction(more), CHANGED, steps, blocked,
                   f"At {_where(steps)} the allowed values {'gained' if more else 'lost'} {names}. {self._effect(more)}",
                   old, new)

    def _skipped(self, prop_before, prop_after):
        """A read-only property is never sent and a write-only one never received: changes to it are not compared."""
        flag = "readOnly" if self.direction == REQUEST else "writeOnly"
        present = [p for p in (prop_before, prop_after) if p is not None]
        return bool(present) and all(_node(p).get(flag) is True for p in present)

    def _properties(self, before, after, steps, blocked):
        old_props = before.get("properties") if isinstance(before.get("properties"), dict) else {}
        new_props = after.get("properties") if isinstance(after.get("properties"), dict) else {}
        old_required = set(before["required"]) if isinstance(before.get("required"), list) else set()
        new_required = set(after["required"]) if isinstance(after.get("required"), list) else set()
        closed = before.get("additionalProperties") is False or before.get("unevaluatedProperties") is False
        sent = self.direction == REQUEST
        for name in sorted((old_props.keys() | new_props.keys()), key=str):
            old, new = old_props.get(name), new_props.get(name)
            here = (*steps, ("p", name))
            if self._skipped(old, new):
                continue
            if old is not None and new is not None:
                flag = "readOnly" if sent else "writeOnly"
                if _node(old).get(flag) != _node(new).get(flag):
                    self._emit("schema.access_changed", UNCERTAIN, CHANGED, here, blocked,
                               f"`{flag}` changed at {_where(here)}, which changes whether it applies in this direction.",
                               _node(old).get(flag), _node(new).get(flag))
                    continue
            if old is None:
                required = name in new_required
                if sent:
                    category = POTENTIALLY_BREAKING if required else NON_BREAKING
                    effect = ("Existing clients do not send it, so they may be rejected." if required
                              else "Existing clients may omit it, so they are not affected.")
                elif closed:
                    category, effect = UNCERTAIN, ("The earlier object did not allow extra properties, so clients that "
                                                   "validate strictly may reject it.")
                else:
                    category, effect = NON_BREAKING, ("Assumes clients ignore properties they do not know; clients "
                                                      "that validate strictly may reject it.")
                self._emit("schema.property_added", category, ADDED, here, blocked,
                           f"The {'required' if required else 'optional'} property {_where(here)} is newly declared. {effect}",
                           None, {"required": required}, before_present=False)
            elif new is None:
                self._emit("schema.property_removed", POTENTIALLY_BREAKING, REMOVED, here, blocked,
                           f"The property {_where(here)} is no longer declared. "
                           f"{'Existing clients may still send it.' if sent else 'Existing clients may read it.'}",
                           {"required": name in old_required}, None, after_present=False)
            else:
                was, now = name in old_required, name in new_required
                if was != now:
                    stronger = now  # became required
                    category = (POTENTIALLY_BREAKING if stronger else NON_BREAKING) if sent else \
                        (NON_BREAKING if stronger else POTENTIALLY_BREAKING)
                    effect = {(True, True): "Clients that omit it may now be rejected.",
                              (True, False): "Clients now always receive it.",
                              (False, True): "Clients that send it are still accepted.",
                              (False, False): "Clients can no longer rely on receiving it."}[(sent, stronger)]
                    self._emit("schema.required_changed", category, CHANGED, here, blocked,
                               f"{_where(here)} changed from {'required' if was else 'optional'} to "
                               f"{'required' if now else 'optional'}. {effect}", was, now)
                self.diff(old, new, here, blocked)
        for name in sorted((old_required ^ new_required) - old_props.keys() - new_props.keys(), key=str):
            here = (*steps, ("p", name))
            self._emit("schema.required_changed", UNCERTAIN, CHANGED, here, blocked,
                       f"`{name}` is listed as required but not declared as a property, and that changed.",
                       name in old_required, name in new_required)
            self.unclassified = True


def locate(docs: Documents, node: Any, url: str, tokens: tuple, steps: tuple) -> SourceLocation:
    """Where the schema at `steps` really lives in its document, following references on the way."""
    try:
        for step in steps:
            node, url, tokens = docs.follow(node, url, tokens)
            if step[0] == "p":
                node, tokens = node["properties"][step[1]], (*tokens, "properties", step[1])
            else:
                node, tokens = node["items"], (*tokens, "items")
        node, url, tokens = docs.follow(node, url, tokens)
    except (Unresolved, KeyError, TypeError):
        pass
    return SourceLocation(url, json_pointer(tokens))


@dataclass
class SchemaRef:
    """A schema as written in a document: the node, the document it is in, and its tokens there."""

    docs: Documents
    node: Any
    url: str
    tokens: tuple


def compare_schemas(before: SchemaRef, after: SchemaRef, direction: str) -> tuple[list[SchemaChange], bool, bool]:
    """(changes, unclassified, truncated). Raises Unresolved if either schema cannot be expanded offline."""
    differ = _Differ(direction)
    differ.diff(before.docs.expand(before.node, before.url), after.docs.expand(after.node, after.url))
    return differ.changes, differ.unclassified, differ.truncated


def schema_findings(operation_method: str, declared_path: str, normalized_path: str, scope: str, label: str,
                    before: SchemaRef, after: SchemaRef, direction: str, limitations: list) -> list[ContractFinding]:
    """Findings for one pair of schemas. `scope` goes into the key, `label` into the human subject."""
    try:
        changes, unclassified, truncated = compare_schemas(before, after, direction)
    except Unresolved as error:
        limitations.append(ComparisonLimitation(
            "schema_unresolved", f"The schema of {label} could not be read in full ({error}) so it was not compared.",
            f"{operation_method} {declared_path}", True))
        return []
    scope_name = f"{operation_method} {declared_path}"
    if unclassified:
        limitations.append(ComparisonLimitation(
            "schema_construct_not_classified",
            f"The schema of {label} uses constraints or constructs (composition, formats, limits, ...) that this "
            "comparison does not classify, so changes there are uncertain and nothing below them is confidently safe.",
            scope_name))
    if truncated:
        limitations.append(ComparisonLimitation(
            "schema_findings_truncated", f"The schema of {label} has more than {MAX_CHANGES} differences; the rest "
            "were not listed.", scope_name))
    findings = []
    for change in changes:
        tail = _path_text(change.steps) + (f"|{change.keyword}" if change.keyword else "")
        subject = f"{label} > {_where(change.steps).strip('`')}" if change.steps else label
        findings.append(ContractFinding(
            key=f"{change.rule_id}|{operation_method}|{normalized_path}|{scope}|{tail}", rule_id=change.rule_id,
            category=change.category, change_kind=change.change_kind, direction=direction, method=operation_method,
            path=declared_path, before_present=change.before_present, after_present=change.after_present,
            before_value=change.before_value, after_value=change.after_value,
            before_location=locate(before.docs, before.node, before.url, before.tokens, change.steps)
            if change.before_present else None,
            after_location=locate(after.docs, after.node, after.url, after.tokens, change.steps)
            if change.after_present else None,
            explanation=change.explanation, subject=subject))
    return findings
