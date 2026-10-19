"""Resolve `$ref` references inside one contract package, offline, from the documents discovery already captured.

Nothing is fetched. A reference that cannot be followed raises `Unresolved` with a reason, so the caller can report a
limitation for exactly the scope affected instead of guessing. Expansion is bounded in depth and size, and a cycle
is cut with a deterministic marker rather than followed forever.
"""

import json
from typing import Any
from urllib.parse import urldefrag

from radar.discovery.references import (
    ExternalReference, LocalReference, classify_reference, json_pointer, resolve_pointer,
)
from radar.discovery.validation import parse_document
from radar.domain.discovery import ValidatedContractPackage

MAX_REF_CHAIN = 8
MAX_EXPAND_DEPTH = 32
MAX_EXPAND_NODES = 20_000
# Keys whose values are data (a default, an enum member), never a schema, so nothing inside them is read as one.
DATA_KEYS = frozenset({"enum", "default", "const", "example", "examples"})
# Keys whose values are maps from a user-chosen name to a schema.
NAME_MAPS = frozenset({"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"})
# Documentation annotations: a change to one is documentation, not a change to what a schema accepts.
ANNOTATIONS = frozenset({"title", "description", "example", "examples", "externalDocs"})


class Unresolved(Exception):
    """A reference or document could not be read offline. The message is a plain-language reason."""


class Documents:
    """The root and captured referenced documents of one package. Each is parsed once and never modified."""

    def __init__(self, package: ValidatedContractPackage):
        self.root_url = package.root_document.source_url
        self._captured = {d.source_url: d for d in package.referenced_documents}
        self._parsed: dict[str, Any] = {self.root_url: package.parsed_contract}

    def get(self, url: str):
        url = urldefrag(url)[0]
        if url in self._parsed:
            return self._parsed[url]
        captured = self._captured.get(url)
        if captured is None:
            raise Unresolved(f"The referenced document {url!r} was not captured.")
        result = parse_document(captured.content)
        if not result.ok:
            raise Unresolved(f"The referenced document {url!r} could not be parsed.")
        self._parsed[url] = result.value
        return result.value

    def resolve(self, ref: Any, base_url: str):
        """Follow one `$ref`. Returns (value, url, tokens) of its target; the target may itself be a `$ref`."""
        reference = classify_reference(ref, base_url) if isinstance(ref, str) else None
        if isinstance(reference, LocalReference):
            url, tokens = base_url, reference.tokens
        elif isinstance(reference, ExternalReference):
            url, tokens = reference.url, reference.tokens
        else:
            raise Unresolved(f"The reference {str(ref)[:80]!r} is not supported.")
        found, value = resolve_pointer(self.get(url), tokens)
        if not found:
            raise Unresolved(f"The reference target {json_pointer(tokens)!r} does not exist.")
        return value, urldefrag(url)[0], tokens

    def follow(self, node: Any, url: str, tokens: tuple):
        """Follow a chain of `$ref` objects to the first object that is not one. Returns (node, url, tokens)."""
        seen = set()
        for _ in range(MAX_REF_CHAIN):
            if not isinstance(node, dict) or "$ref" not in node:
                return node, url, tokens
            node, url, tokens = self.resolve(node["$ref"], url)
            if (url, tokens) in seen:
                raise Unresolved("The references form a cycle.")
            seen.add((url, tokens))
        raise Unresolved("The reference chain is too long.")

    def expand(self, node: Any, url: str):
        """A comparable copy of `node` with every `$ref` replaced by its target and annotations removed.

        Names of referenced components do not appear in the result, so renaming a component without changing its
        content is not a difference. A cycle is cut with a marker naming its target.
        """
        budget = [MAX_EXPAND_NODES]
        return self._expand(node, url, (), budget, 0, False)

    def _expand(self, node, url, stack, budget, depth, in_name_map):
        budget[0] -= 1
        if budget[0] < 0 or depth > MAX_EXPAND_DEPTH:
            raise Unresolved("The schema is too large or too deeply nested to compare.")
        if isinstance(node, list):
            return [self._expand(child, url, stack, budget, depth + 1, False) for child in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node and not in_name_map:
            target, target_url, tokens = self.resolve(node["$ref"], url)
            marker = (target_url, tokens)
            if marker in stack:
                return {"$cycle": target_url + json_pointer(tokens)}
            return self._expand(target, target_url, (*stack, marker), budget, depth + 1, False)
        result = {}
        for key, child in node.items():
            if not in_name_map and (key in ANNOTATIONS or (isinstance(key, str) and key.startswith("$comment"))):
                continue
            if not in_name_map and key in DATA_KEYS:
                result[key] = child  # data, kept verbatim
            else:
                result[key] = self._expand(child, url, stack, budget, depth + 1, not in_name_map and key in NAME_MAPS)
        if not in_name_map:  # `required` and `enum` are sets: their order is not a difference
            if isinstance(result.get("required"), list) and all(isinstance(n, str) for n in result["required"]):
                result["required"] = sorted(result["required"])
            if isinstance(result.get("enum"), list):
                result["enum"] = sorted(result["enum"], key=lambda member: json.dumps(member, sort_keys=True))
        return result
