"""Structural validation of one retrieved document as an OpenAPI 3.0/3.1 contract.

Pure: bytes in, typed result out. No network, no files, no scoring. A document is
untrusted input, so parsing is bounded (size, nesting, visited nodes) and
ambiguous constructs are rejected instead of interpreted: duplicate keys,
recursive YAML aliases, non-JSON YAML types. Every rejection carries the stage
that failed, a stable code, a short reason and, where known, a location.

This is a structural check of the fields discovery and matching depend on. It is
not validation against the official OpenAPI JSON Schema; the result says so.
"""

from dataclasses import dataclass
import json
import re
from typing import Any, Mapping

import yaml


STAGE = 'validation'
SUPPORTED_VERSIONS = '3.0.x and 3.1.x'
HTTP_METHODS = ('get', 'put', 'post', 'delete', 'options', 'head', 'patch', 'trace')
_SUPPORTED = re.compile(r'3\.[01]\.\d+(?:-[0-9A-Za-z.-]+)?')
_VERSION_SHAPE = re.compile(r'\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?')
_SCALARS = {str, int, float, bool, type(None)}
STRUCTURAL_ONLY = 'Structural validation only; the document was not checked against the official OpenAPI JSON Schema.'


@dataclass(frozen=True)
class ValidationLimits:
    """Defaults leave ~8x node and ~2.5x depth headroom over large real specs."""

    max_bytes: int = 32 * 1024 * 1024
    max_nodes: int = 2_000_000
    max_depth: int = 64

    def __post_init__(self):
        for name in ('max_bytes', 'max_nodes', 'max_depth'):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f'Invalid {name}.')


@dataclass(frozen=True)
class ValidationRejection:
    code: str
    reason: str
    location: str | None = None
    stage: str = STAGE


@dataclass(frozen=True)
class ContractSummary:
    openapi_version: str
    title: str
    info_version: str  # the document's own version; not the provider's API version
    server_urls: tuple[str, ...]  # raw, variables unexpanded
    operations: tuple[tuple[str, str], ...]  # (METHOD, path template), document order
    webhook_operation_count: int
    path_count: int
    node_count: int


@dataclass(frozen=True)
class ValidationResult:
    summary: ContractSummary | None = None
    document: Mapping[str, Any] | None = None
    rejection: ValidationRejection | None = None
    limitations: tuple[str, ...] = ()

    def __post_init__(self):
        accepted = self.rejection is None
        if accepted != (self.summary is not None and self.document is not None):
            raise ValueError('An accepted result needs a summary and document; a rejection has neither.')

    @property
    def ok(self):
        return self.rejection is None


class _Rejected(Exception):
    def __init__(self, code, reason, location=None):
        self.rejection = ValidationRejection(code, _bounded(reason), _bounded(location))
        super().__init__(code)


def _bounded(text, limit=200):
    """Untrusted text in messages is single-line and length-bounded."""
    if text is None:
        return None
    text = ''.join(' ' if ord(c) < 32 or ord(c) == 127 else c for c in str(text))
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _pointer(*parts):
    return '/' + '/'.join(str(p).replace('~', '~0').replace('/', '~1') for p in parts)


# --- parsing -----------------------------------------------------------------

def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _Rejected('duplicate_key', f'Duplicate key {_bounded(key, 80)!r}; the document is ambiguous.')
        result[key] = value
    return result


def _reject_constant(name):
    raise ValueError(f'{name} is not valid JSON')


def _key_text(key):
    """YAML allows `200:` as an integer key; JSON object keys are always strings."""
    if isinstance(key, str):
        return key
    if isinstance(key, bool):
        return 'true' if key else 'false'
    if key is None:
        return 'null'
    if isinstance(key, (int, float)):
        return str(key)
    raise _Rejected('invalid_structure', 'Mapping keys must be scalars.')


def _yaml_loader():
    """YAML 1.2-style core scalars on top of the safe loader.

    PyYAML implements YAML 1.1, where `on`/`no` are booleans, `2022-11-28` is a
    date and `1:30` is a number. OpenAPI documents are JSON-compatible, so those
    would silently turn strings (including `info.version`) into other types.
    """
    base = getattr(yaml, 'CSafeLoader', yaml.SafeLoader)
    drop = {'tag:yaml.org,2002:bool', 'tag:yaml.org,2002:int', 'tag:yaml.org,2002:float',
            'tag:yaml.org,2002:timestamp'}

    class Loader(base):
        def construct_mapping(self, node, deep=False):
            seen = set()
            for key_node, _ in node.value:  # explicit keys only; `<<` merges may be overridden
                if key_node.tag == 'tag:yaml.org,2002:merge':
                    continue
                if not isinstance(key_node, yaml.ScalarNode):
                    raise _Rejected('invalid_structure', 'Mapping keys must be scalars.')
                key = _key_text(self.construct_object(key_node, deep=True))
                if key in seen:
                    raise _Rejected('duplicate_key', f'Duplicate key {_bounded(key, 80)!r}; the document is ambiguous.')
                seen.add(key)
            return {_key_text(key): value for key, value in super().construct_mapping(node, deep).items()}

    Loader.yaml_implicit_resolvers = {
        first: [(tag, pattern) for tag, pattern in resolvers if tag not in drop]
        for first, resolvers in base.yaml_implicit_resolvers.items()
    }
    Loader.add_implicit_resolver(
        'tag:yaml.org,2002:int', re.compile(r'^(?:[-+]?(?:0|[1-9][0-9]*)|0o[0-7]+|0x[0-9a-fA-F]+)$'),
        list('-+0123456789'))
    Loader.add_implicit_resolver(
        'tag:yaml.org,2002:float',
        re.compile(r'^(?:[-+]?(?:\.[0-9]+|[0-9]+\.[0-9]*|[0-9]+(?=[eE]))(?:[eE][-+]?[0-9]+)?'
                   r'|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$'), list('-+0123456789.'))
    Loader.add_implicit_resolver(
        'tag:yaml.org,2002:bool', re.compile(r'^(?:true|True|TRUE|false|False|FALSE)$'), list('tTfF'))
    return Loader


_YAML_LOADER = _yaml_loader()


def _parse(content, limits):
    if not content or not content.strip():
        raise _Rejected('empty_document', 'The document is empty.')
    if len(content) > limits.max_bytes:
        raise _Rejected('size_limit_exceeded', f'The document exceeds {limits.max_bytes} bytes.')
    try:
        text = content.decode('utf-8-sig')
    except UnicodeError:
        raise _Rejected('unsupported_encoding', 'The document is not valid UTF-8.') from None
    if '\x00' in text:
        raise _Rejected('not_json_or_yaml', 'The document contains NUL characters.')
    start = text.lstrip()
    if not start:
        raise _Rejected('empty_document', 'The document is empty.')
    if start.startswith('<'):
        head = start[:1024].lower()
        if '<!doctype html' in head or '<html' in head:
            raise _Rejected('html_document', 'The document is an HTML page, not an OpenAPI definition.')
        raise _Rejected('not_json_or_yaml', 'The document is markup, not JSON or YAML.')
    try:
        if start[0] in '{[':
            return json.loads(text, object_pairs_hook=_json_pairs, parse_constant=_reject_constant)
        return yaml.load(text, Loader=_YAML_LOADER)  # noqa: S506 - restricted safe loader subclass
    except _Rejected:
        raise
    except RecursionError:
        raise _Rejected('nesting_limit_exceeded', 'The document is nested too deeply to parse.') from None
    except json.JSONDecodeError as error:
        raise _Rejected('not_json_or_yaml', f'Invalid JSON at line {error.lineno}, column {error.colno}.') from None
    except yaml.YAMLError as error:
        mark = getattr(error, 'problem_mark', None)
        where = f' at line {mark.line + 1}, column {mark.column + 1}' if mark else ''
        raise _Rejected('not_json_or_yaml', f'Invalid YAML{where}.') from None
    except ValueError as error:
        raise _Rejected('not_json_or_yaml', _bounded(f'The document could not be parsed: {error}')) from None


def _inspect(root, limits):
    """Iteratively bound visited nodes and depth, and reject cycles and odd types.

    Shared YAML aliases are visited each time they are reached, so an alias
    expansion bomb exhausts the node budget instead of memory or time.
    """
    nodes = 0
    active = set()
    stack = [(root, 1, False)]
    while stack:
        node, depth, leaving = stack.pop()
        if leaving:
            active.discard(id(node))
            continue
        if depth > limits.max_depth:
            raise _Rejected('nesting_limit_exceeded', f'Nesting exceeds {limits.max_depth} levels.')
        if id(node) in active:
            raise _Rejected('circular_reference', 'The document contains a recursive YAML alias.')
        active.add(id(node))
        stack.append((node, depth, True))
        children = node.values() if isinstance(node, dict) else node
        for child in children:
            nodes += 1
            if nodes > limits.max_nodes:
                raise _Rejected('node_limit_exceeded', f'The document has more than {limits.max_nodes} values.')
            if isinstance(child, (dict, list)):
                stack.append((child, depth + 1, False))
            elif type(child) not in _SCALARS:
                raise _Rejected('invalid_structure', f'Unsupported value type {type(child).__name__}.')
    return nodes + 1


# --- OpenAPI structure -------------------------------------------------------

def _require(value, kind, code, reason, location):
    if not isinstance(value, kind):
        raise _Rejected(code, reason, location)
    return value


def _operations(path_items, base, *, paths):
    """List (METHOD, key) pairs. Path keys must start with "/"; webhook keys are free-form names."""
    operations = []
    unexpanded = 0
    for path, item in path_items.items():
        if paths and not path.startswith(('/', 'x-')):
            raise _Rejected('invalid_structure', 'Path keys must start with "/".', _pointer(base, path))
        if paths and path.startswith('x-'):
            continue
        item = _require(item, dict, 'invalid_structure', 'A path item must be an object.', _pointer(base, path))
        if '$ref' in item:
            unexpanded += 1
        for method in HTTP_METHODS:
            if method in item:
                _require(item[method], dict, 'invalid_structure', 'An operation must be an object.',
                         _pointer(base, path, method))
                operations.append((method.upper(), path))
    return operations, unexpanded


def validate_document(content: bytes, limits: ValidationLimits | None = None) -> ValidationResult:
    """Return an accepted summary plus parsed document, or one rejection.

    Accepts OpenAPI 3.0.x and 3.1.x only. Never raises for document content.
    """
    limits = limits or ValidationLimits()
    if not isinstance(content, (bytes, bytearray)):
        raise TypeError('content must be bytes.')
    try:
        document = _parse(bytes(content), limits)
        if not isinstance(document, dict):
            raise _Rejected('not_an_object', 'The document is not a JSON/YAML object.')
        nodes = _inspect(document, limits)
        summary, limitations = _check_openapi(document, nodes)
    except _Rejected as rejected:
        return ValidationResult(rejection=rejected.rejection)
    return ValidationResult(summary=summary, document=document, limitations=limitations)


def _check_openapi(document, nodes):
    if 'openapi' not in document:
        if 'swagger' in document:
            raise _Rejected('unsupported_version',
                            f'Swagger/OpenAPI 2.x is not supported; supported versions are {SUPPORTED_VERSIONS}.', '/swagger')
        raise _Rejected('not_openapi', 'The object has no "openapi" field, so it is not an OpenAPI document.')
    version = document['openapi']
    if not isinstance(version, str) or not _VERSION_SHAPE.fullmatch(version):
        raise _Rejected('invalid_version', 'The "openapi" field must be a version string such as "3.0.3" (quote it in YAML).', '/openapi')
    if not _SUPPORTED.fullmatch(version):
        raise _Rejected('unsupported_version', f'OpenAPI {_bounded(version, 40)} is not supported; supported versions are {SUPPORTED_VERSIONS}.', '/openapi')

    info = _require(document.get('info'), dict, 'invalid_structure', 'The "info" object is required.', '/info')
    title = _require(info.get('title'), str, 'invalid_structure', 'info.title must be a string.', '/info/title')
    info_version = _require(info.get('version'), str, 'invalid_structure',
                            'info.version must be a string (quote it in YAML).', '/info/version')

    is_30 = version.startswith('3.0.')
    sections = {name: document[name] for name in ('paths', 'components', 'webhooks') if name in document}
    if is_30 and 'paths' not in sections:
        raise _Rejected('invalid_structure', 'OpenAPI 3.0 requires a "paths" object.', '/paths')
    if not is_30 and not sections:
        raise _Rejected('invalid_structure', 'OpenAPI 3.1 requires at least one of "paths", "components" or "webhooks".', '/')
    for name, value in sections.items():
        _require(value, dict, 'invalid_structure', f'"{name}" must be an object.', _pointer(name))

    servers = document.get('servers', [])
    _require(servers, list, 'invalid_structure', '"servers" must be a list.', '/servers')
    server_urls = []
    for index, server in enumerate(servers):
        server = _require(server, dict, 'invalid_structure', 'A server must be an object.', _pointer('servers', index))
        server_urls.append(_require(server.get('url'), str, 'invalid_structure', 'A server requires a string "url".',
                                    _pointer('servers', index, 'url')))
        if 'variables' in server:
            _require(server['variables'], dict, 'invalid_structure', 'Server variables must be an object.',
                     _pointer('servers', index, 'variables'))

    paths = sections.get('paths', {})
    operations, unexpanded = _operations(paths, 'paths', paths=True)
    webhook_operations, webhook_unexpanded = _operations(sections.get('webhooks', {}), 'webhooks', paths=False)

    limitations = [STRUCTURAL_ONLY]
    if not operations and not webhook_operations:
        limitations.append('The document defines no operations, so there is nothing to monitor or match.')
    if unexpanded or webhook_unexpanded:
        limitations.append(f'{unexpanded + webhook_unexpanded} path item(s) use $ref; their operations are not listed until references are captured.')
    summary = ContractSummary(
        openapi_version=version, title=title, info_version=info_version, server_urls=tuple(server_urls),
        operations=tuple(operations), webhook_operation_count=len(webhook_operations),
        path_count=sum(1 for path in paths if not path.startswith('x-')), node_count=nodes)
    return summary, tuple(limitations)
