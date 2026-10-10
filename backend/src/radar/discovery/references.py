"""Find, classify and verify `$ref` references in a parsed document. Pure: no I/O.

A contract is only complete if every reference it depends on resolves. This
module answers the offline half of that question: which references exist, which
are internal (must resolve inside the same document), which point at other
documents (for the capture step to fetch), and which cannot be followed at all.

Only `$ref` is followed. `operationRef`, `discriminator.mapping` URIs and
`externalValue` are not, and every scan says so in its limitations.
"""

from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

from radar.discovery.validation import ValidationRejection


STAGE = 'reference_capture'
MAX_REPORTED_FAILURES = 20
# Data-valued keywords are skipped so a literal `{"$ref": ...}` inside an example is
# not followed. Anything uncertain is followed instead: a spurious reference fails
# visibly, a missed real one would silently produce an incomplete contract.
DATA_KEYS = frozenset({'example', 'default', 'const', 'enum'})
# Objects whose keys are user-chosen names, so a key such as `example` or `default`
# there names a schema/response/etc. and must not be skipped.
NAME_MAPS = frozenset({
    'properties', 'patternProperties', 'dependentSchemas', '$defs', 'definitions', 'schemas', 'responses',
    'parameters', 'headers', 'requestBodies', 'securitySchemes', 'links', 'callbacks', 'pathItems',
    'examples', 'mediaTypes', 'content', 'paths', 'webhooks', 'encoding',
})
_INDEX = re.compile(r'0|[1-9][0-9]*')
_BAD_ESCAPE = re.compile(r'~(?![01])')
LIMITATION_SCOPE = ('Only $ref references are followed; operationRef, discriminator.mapping and externalValue '
                    'targets are not captured.')


@dataclass(frozen=True)
class ReferenceLimits:
    max_nodes: int = 5_000_000
    max_external_references: int = 2000

    def __post_init__(self):
        for name in ('max_nodes', 'max_external_references'):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f'Invalid {name}.')


@dataclass(frozen=True)
class LocalReference:
    """Points inside the document that contains it (`#/components/schemas/Pet`)."""
    tokens: tuple[str, ...]


@dataclass(frozen=True)
class AnchorReference:
    """`#name`-style fragment (JSON Schema `$anchor`); cannot be verified as a pointer."""
    name: str
    url: str | None = None  # None: the same document


@dataclass(frozen=True)
class ExternalReference:
    """Points into another document; `tokens` is the JSON Pointer inside it."""
    url: str
    tokens: tuple[str, ...]


@dataclass(frozen=True)
class UnsupportedReference:
    reason: str


@dataclass(frozen=True)
class ExternalUse:
    ref: str
    url: str
    tokens: tuple[str, ...]
    location: str  # JSON pointer of the first object that holds this reference
    occurrences: int = 1


@dataclass(frozen=True)
class ReferenceScan:
    external: tuple[ExternalUse, ...] = ()
    local_targets: tuple[tuple[str, ...], ...] = ()  # distinct internal pointers, document order
    failures: tuple[ValidationRejection, ...] = ()
    failure_count: int = 0
    local_count: int = 0
    anchor_count: int = 0
    id_count: int = 0
    node_count: int = 0
    limitations: tuple[str, ...] = ()

    @property
    def ok(self):
        return self.failure_count == 0

    @property
    def rejection(self) -> ValidationRejection | None:
        """The first failure in document order, noting how many others there were."""
        if not self.failures:
            return None
        first = self.failures[0]
        if self.failure_count == 1:
            return first
        more = self.failure_count - 1
        return ValidationRejection(first.code, f'{first.reason} ({more} more unresolved or unsupported reference(s).)',
                                   first.location, first.stage)


def json_pointer(tokens) -> str:
    return ''.join('/' + str(t).replace('~', '~0').replace('/', '~1') for t in tokens)


def _bounded(text, limit=160):
    text = ''.join(' ' if ord(c) < 32 or ord(c) == 127 else c for c in str(text))
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _tokens(fragment):
    """RFC 6901 tokens of an already percent-decoded fragment; ValueError if malformed."""
    if fragment == '':
        return ()
    if not fragment.startswith('/'):
        raise ValueError('not a pointer')
    tokens = []
    for raw in fragment[1:].split('/'):
        if _BAD_ESCAPE.search(raw):
            raise ValueError('invalid escape')
        tokens.append(raw.replace('~1', '/').replace('~0', '~'))
    return tuple(tokens)


def resolve_pointer(document: Any, tokens) -> tuple[bool, Any]:
    """Follow JSON Pointer tokens. Returns (found, value); never raises."""
    value = document
    for token in tokens:
        if isinstance(value, dict):
            if token not in value:
                return False, None
            value = value[token]
        elif isinstance(value, list):
            if not _INDEX.fullmatch(token) or int(token) >= len(value):
                return False, None
            value = value[int(token)]
        else:
            return False, None
    return True, value


def _fragment_reference(fragment, url):
    try:
        decoded = unquote(fragment, errors='strict')
    except ValueError:
        return UnsupportedReference('The reference fragment is not valid percent-encoded UTF-8.')
    if decoded == '' or decoded.startswith('/'):
        try:
            tokens = _tokens(decoded)
        except ValueError:
            return UnsupportedReference('The reference fragment is not a valid JSON Pointer.')
        return LocalReference(tokens) if url is None else ExternalReference(url, tokens)
    return AnchorReference(decoded, url)


def classify_reference(ref: str, base_url: str):
    """Classify one `$ref` string against the URL of the document containing it.

    A reference to the containing document's own URL is internal. `base_url` must be
    an absolute HTTP(S) URL (the document's final retrieval URL).
    """
    if not isinstance(ref, str) or not ref or ref != ref.strip() or '\\' in ref \
            or any(ord(c) < 32 or ord(c) == 127 for c in ref):
        return UnsupportedReference('The reference is empty or malformed.')
    if ref.startswith('#'):
        return _fragment_reference(ref[1:], None)
    try:
        given = urlsplit(ref)
        scheme = given.scheme.lower()
        if scheme and scheme not in ('http', 'https'):
            return UnsupportedReference(f'Reference scheme {_bounded(scheme, 20)!r} is not supported.')
        if scheme and not given.netloc:  # urljoin would silently borrow the base host
            return UnsupportedReference('The absolute reference has no host.')
        target, fragment = urldefrag(urljoin(base_url, ref))
        parts = urlsplit(target)
        hostname, username, password = parts.hostname, parts.username, parts.password
        parts.port  # noqa: B018 - raises ValueError for an invalid port
    except ValueError:
        return UnsupportedReference('The reference is not a valid URL.')
    if parts.scheme not in ('http', 'https') or not hostname:
        return UnsupportedReference('The reference does not resolve to an HTTP(S) URL.')
    if username is not None or password is not None:
        return UnsupportedReference('References containing credentials are not supported.')
    own = urldefrag(base_url)[0]
    return _fragment_reference(fragment, None if target == own else target)


def _walk(root, start, limits):
    """Yield ('ref'|'id', location tokens, value) in document order, then ('end', nodes).

    Iterative, with every visited value counted, so aliased YAML or huge documents
    cannot exhaust time or memory beyond `max_nodes`.
    """
    link = None
    for token in start:
        link = (link, token)
    stack = [(root, link, bool(start) and start[-1] in NAME_MAPS)]
    nodes = 0
    while stack:
        node, link, is_name_map = stack.pop()
        nodes += 1
        if nodes > limits.max_nodes:
            yield 'limit', None, nodes
            return
        if isinstance(node, dict):
            for kind, key in (('ref', '$ref'), ('id', '$id')):
                if isinstance(node.get(key), str):
                    yield kind, _link_tokens(link), node[key]
            for key, child in reversed(list(node.items())):
                if key in DATA_KEYS and not is_name_map:
                    continue
                if isinstance(child, (dict, list)):
                    stack.append((child, (link, key), key in NAME_MAPS))
        elif isinstance(node, list):
            for index in range(len(node) - 1, -1, -1):
                if isinstance(node[index], (dict, list)):
                    stack.append((node[index], (link, str(index)), False))
    yield 'end', None, nodes


def _link_tokens(link):
    tokens = []
    while link is not None:
        link, token = link
        tokens.append(token)
    return tuple(reversed(tokens))


def scan_references(document: Any, base_url: str, *, start=(), limits: ReferenceLimits | None = None) -> ReferenceScan:
    """Scan `document` (from the node at `start`) and verify its internal references.

    Internal pointers are resolved against the whole `document`, so scanning only
    a reachable subtree of a fetched file still checks pointers in that file.
    External references are returned for the caller to fetch, and the distinct
    internal targets as `local_targets`: a caller scanning only a subtree must also
    scan those targets, because they are reachable from it. Neither is followed here. Unsupported or unresolvable references are failures, reported
    in document order with the first one as `rejection`.
    """
    limits = limits or ReferenceLimits()
    start = tuple(start)
    found, root = resolve_pointer(document, start)
    if not found:
        raise ValueError('start does not exist in the document.')
    classified = {}
    resolved = {}
    external = {}
    local_targets = {}
    failures = []
    counts = {'failures': 0, 'local': 0, 'anchor': 0, 'id': 0, 'nodes': 0}
    stopped = None

    def fail(code, reason, location):
        counts['failures'] += 1
        if len(failures) < MAX_REPORTED_FAILURES:
            failures.append(ValidationRejection(code, reason, location, STAGE))

    for kind, location, value in _walk(root, start, limits):
        if kind == 'end':
            counts['nodes'] = value
        elif kind == 'limit':
            counts['nodes'] = value
            stopped = f'The document has more than {limits.max_nodes} values to scan.'
        elif kind == 'id':
            counts['id'] += 1
        else:
            where = json_pointer((*location, '$ref'))
            reference = classified.get(value)
            if reference is None:
                reference = classified[value] = classify_reference(value, base_url)
            if isinstance(reference, UnsupportedReference):
                fail('reference_unsupported', f'{reference.reason} Reference: {_bounded(value)!r}.', where)
            elif isinstance(reference, AnchorReference):
                counts['anchor'] += 1
            elif isinstance(reference, LocalReference):
                counts['local'] += 1
                local_targets.setdefault(reference.tokens)
                if reference.tokens not in resolved:
                    resolved[reference.tokens] = resolve_pointer(document, reference.tokens)[0]
                if not resolved[reference.tokens]:
                    fail('unresolvable_reference',
                         f'Reference {_bounded(value)!r} does not point to anything in the document.', where)
            else:
                key = (reference.url, reference.tokens, value)
                if key in external:
                    previous = external[key]
                    external[key] = ExternalUse(previous.ref, previous.url, previous.tokens, previous.location,
                                                previous.occurrences + 1)
                elif len(external) >= limits.max_external_references:
                    stopped = f'More than {limits.max_external_references} distinct external references.'
                else:
                    external[key] = ExternalUse(value, reference.url, reference.tokens, json_pointer(location))
        if stopped:
            counts['failures'] += 1
            failures.insert(0, ValidationRejection('reference_limit_exceeded', stopped, None, STAGE))
            break

    limitations = [LIMITATION_SCOPE]
    if counts['anchor']:
        limitations.append(f"{counts['anchor']} anchor-style reference(s) (#name) were not verified.")
    if counts['id']:
        limitations.append(f"{counts['id']} $id value(s) present; base-URI changes from $id are not applied.")
    return ReferenceScan(
        external=tuple(external.values()), local_targets=tuple(local_targets), failures=tuple(failures), failure_count=counts['failures'],
        local_count=counts['local'], anchor_count=counts['anchor'], id_count=counts['id'],
        node_count=counts['nodes'], limitations=tuple(limitations))
