"""Read the operations and servers of one contract package, offline.

Operations are identified by HTTP method and path with template parameter names normalized, so `/pets/{id}` and
`/pets/{petId}` are the same operation. A path item given as a `$ref` is resolved from the documents the package
already captured; if that is impossible the path is reported as unresolved, never as empty.
"""

from dataclasses import dataclass, field
import re
from typing import Any

from radar.comparison.openapi.references import Documents, Unresolved
from radar.discovery.references import json_pointer
from radar.discovery.validation import HTTP_METHODS
from radar.domain.comparison import ComparisonLimitation, SourceLocation
from radar.domain.discovery import ValidatedContractPackage

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
    # What parameter comparison reads: the operation object, its path item, and where the path item is defined.
    node: dict = field(compare=False, default_factory=dict)
    item: dict = field(compare=False, default_factory=dict)
    item_url: str = ""
    item_tokens: tuple = field(compare=False, default=())
    documents: Documents | None = field(compare=False, default=None)


@dataclass
class ContractView:
    operations: dict[tuple[str, str], Operation] = field(default_factory=dict)  # (METHOD, normalized path)
    unresolved: dict[str, str] = field(default_factory=dict)  # normalized path -> why its operations are unknown
    limitations: list[ComparisonLimitation] = field(default_factory=list)
    servers: list | None = None  # canonical servers; the OpenAPI default [{"url": "/"}] when none are declared
    servers_location: SourceLocation | None = None
    has_webhooks: bool = False


def _resolve_item(docs: Documents, url: str, item: Any, tokens: tuple[str, ...], three_one: bool):
    """Follow a path item's `$ref` chain. Returns (item, url, tokens, reason); reason is set on failure."""
    try:
        for _ in range(8):
            if not isinstance(item, dict):
                return None, url, tokens, "The path item is not an object."
            if "$ref" not in item:
                return item, url, tokens, None
            siblings = {k: v for k, v in item.items() if k != "$ref"} if three_one else {}  # 3.0 ignores siblings
            target, url, tokens = docs.resolve(item["$ref"], url)
            item = {**target, **siblings} if siblings and isinstance(target, dict) else target
        return None, url, tokens, "The path item reference chain is too long."
    except Unresolved as error:
        return None, url, tokens, str(error)


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
    docs = Documents(package)
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
            view.operations[key] = Operation(
                method.upper(), declared, SourceLocation(url, json_pointer((*tokens, method))),
                resolved[method], resolved, url, tokens, docs)
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
