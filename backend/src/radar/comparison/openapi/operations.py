"""Read the operations and servers of one contract package, offline.

Operations are identified by HTTP method and path with template parameter names normalized, so `/pets/{id}` and
`/pets/{petId}` are the same operation. A path item given as a `$ref` is resolved from the documents the package
already captured; if that is impossible the path is reported as unresolved, never as empty.
"""

from dataclasses import dataclass, field
import re
from typing import Any
from urllib.parse import urldefrag

from radar.discovery.references import (
    ExternalReference, LocalReference, classify_reference, json_pointer, resolve_pointer,
)
from radar.discovery.validation import HTTP_METHODS, parse_document
from radar.domain.comparison import ComparisonLimitation, SourceLocation
from radar.domain.discovery import ValidatedContractPackage

MAX_REF_CHAIN = 8
_TEMPLATE = re.compile(r"\{[^{}]*\}")


def normalize_path(path: str) -> str:
    """`/pets/{id}` and `/pets/{petId}` -> `/pets/{}`. Nothing else about a path is altered.

    >>> normalize_path('/pets/{id}/toys/{toyId}')
    '/pets/{}/toys/{}'
    >>> normalize_path('/pets/') == normalize_path('/pets')
    False
    """
    return _TEMPLATE.sub("{}", path)


@dataclass(frozen=True)
class Operation:
    method: str  # upper case
    declared_path: str
    location: SourceLocation


@dataclass
class ContractView:
    operations: dict[tuple[str, str], Operation] = field(default_factory=dict)  # (METHOD, normalized path)
    unresolved: dict[str, str] = field(default_factory=dict)  # normalized path -> why its operations are unknown
    limitations: list[ComparisonLimitation] = field(default_factory=list)
    servers: list | None = None  # canonical servers; the OpenAPI default [{"url": "/"}] when none are declared
    servers_location: SourceLocation | None = None
    has_webhooks: bool = False


class _Documents:
    def __init__(self, package: ValidatedContractPackage):
        self.root_url = package.root_document.source_url
        self._captured = {d.source_url: d for d in package.referenced_documents}
        self._parsed: dict[str, Any] = {self.root_url: package.parsed_contract}

    def get(self, url: str):
        """(found, parsed document); documents are parsed once and never modified."""
        if url in self._parsed:
            return True, self._parsed[url]
        captured = self._captured.get(url)
        if captured is None:
            return False, None
        result = parse_document(captured.content)
        if not result.ok:
            return False, None
        self._parsed[url] = result.value
        return True, result.value


def _resolve_item(docs: _Documents, url: str, item: Any, tokens: tuple[str, ...], three_one: bool):
    """Follow a path item's `$ref` chain. Returns (item, url, tokens, reason); reason is set on failure."""
    seen = set()
    for _ in range(MAX_REF_CHAIN):
        if not isinstance(item, dict):
            return None, url, tokens, "The path item is not an object."
        ref = item.get("$ref")
        if ref is None:
            return item, url, tokens, None
        reference = classify_reference(ref, url) if isinstance(ref, str) else None
        if isinstance(reference, LocalReference):
            target_url, target_tokens = url, reference.tokens
        elif isinstance(reference, ExternalReference):
            target_url, target_tokens = reference.url, reference.tokens
        else:
            return None, url, tokens, f"The path item reference {str(ref)[:80]!r} is not supported."
        if (target_url, target_tokens) in seen:
            return None, url, tokens, "The path item references form a cycle."
        seen.add((target_url, target_tokens))
        found, document = docs.get(urldefrag(target_url)[0])
        if not found:
            return None, url, tokens, f"The referenced document {urldefrag(target_url)[0]!r} was not captured."
        found, target = resolve_pointer(document, target_tokens)
        if not found:
            return None, url, tokens, f"The reference target {json_pointer(target_tokens)!r} does not exist."
        siblings = {k: v for k, v in item.items() if k != "$ref"} if three_one else {}  # 3.0 ignores siblings
        if siblings and isinstance(target, dict):
            target = {**target, **siblings}
        item, url, tokens = target, urldefrag(target_url)[0], target_tokens
    return None, url, tokens, "The path item reference chain is too long."


def _server(server: Any):
    if not isinstance(server, dict) or not isinstance(server.get("url"), str):
        return None
    variables = server.get("variables")
    canonical = {}
    if isinstance(variables, dict):
        for name in sorted(variables):
            spec = variables[name]
            if isinstance(spec, dict):
                canonical[name] = {"default": spec.get("default"), "enum": spec.get("enum")}
    return {"url": server["url"], "variables": canonical}


def read_contract(package: ValidatedContractPackage, side: str) -> ContractView:
    docs = _Documents(package)
    contract = package.parsed_contract
    view = ContractView()
    three_one = str(contract.get("openapi", "")).startswith("3.1")
    root = docs.root_url

    paths = contract.get("paths")
    for declared, item in (paths.items() if isinstance(paths, dict) else ()):
        if not isinstance(declared, str) or not declared.startswith("/"):
            continue  # `x-*` extensions
        normalized = normalize_path(declared)
        if normalized in view.unresolved:
            continue
        resolved, url, tokens, reason = _resolve_item(docs, root, item, ("paths", declared), three_one)
        if resolved is None:
            view.unresolved[normalized] = reason
            view.limitations.append(ComparisonLimitation(
                "path_item_unresolved", f"The {side} contract's path {declared!r} could not be read: {reason} "
                "Its operations were not compared.", declared, True))
            continue
        for method in HTTP_METHODS:
            if not isinstance(resolved.get(method), dict):
                continue
            key = (method.upper(), normalized)
            if key in view.operations:
                existing = view.operations[key].declared_path
                view.unresolved[normalized] = f"Both {existing!r} and {declared!r} are the same path template."
                view.limitations.append(ComparisonLimitation(
                    "ambiguous_path_template", f"The {side} contract declares {existing!r} and {declared!r}, which "
                    "differ only in parameter names. Its operations on this path were not compared.", declared, True))
                continue
            view.operations[key] = Operation(method.upper(), declared, SourceLocation(url, json_pointer((*tokens, method))))
    for normalized in view.unresolved:  # an ambiguous path must not leave one half of its operations behind
        for key in [k for k in view.operations if k[1] == normalized]:
            del view.operations[key]

    if "servers" not in contract:
        view.servers = [{"url": "/", "variables": {}}]
    else:
        servers = contract["servers"]
        canonical = [_server(s) for s in servers] if isinstance(servers, list) else None
        if canonical is not None and None not in canonical:
            view.servers, view.servers_location = canonical, SourceLocation(root, "/servers")
    view.has_webhooks = "webhooks" in contract
    return view
