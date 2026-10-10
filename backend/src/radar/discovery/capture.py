"""Capture the external documents a validated contract depends on.

Starting from a validated root document, follow every external `$ref`: fetch each
referenced file once (through the shared discovery budget), parse it with the
same bounds as validation, check that each pointer exists, and follow only the
parts of those files that are actually reachable. The result is either the
complete set of referenced files with their original bytes, or one rejection.
An incomplete capture is never returned as a success: on any failure no documents
are returned.

Cross-origin references are rejected unless explicitly allowed. The origin is
always the ROOT document's, so a chain of references cannot walk to another host.
Only `$ref` is followed (see references.py for what is not).
"""

from collections import deque
from dataclasses import dataclass
from urllib.parse import urlsplit

from radar.discovery.fetch import fetch_document
from radar.discovery.input import DiscoveryInputError, normalize_target
from radar.discovery.limits import DiscoveryBudget
from radar.discovery.references import (
    STAGE, ExternalUse, ReferenceLimits, json_pointer, resolve_pointer, scan_references,
)
from radar.discovery.validation import ValidationLimits, ValidationRejection, parse_document
from radar.domain.discovery import CapturedDocument, DiscoveryRequest


BUDGET_CODES = {'request_limit', 'deadline_exceeded', 'total_size_limit'}


@dataclass(frozen=True)
class CaptureLimits:
    max_documents: int = 50  # distinct external files fetched
    max_depth: int = 10  # hops from the root document
    max_total_nodes: int = 10_000_000  # values parsed or scanned across the whole capture
    max_references: int = 5000  # distinct reference uses processed; a backstop on top of deduplication
    allow_cross_origin: bool = False
    references: ReferenceLimits = ReferenceLimits()
    parsing: ValidationLimits = ValidationLimits()

    def __post_init__(self):
        for name in ('max_documents', 'max_depth', 'max_total_nodes', 'max_references'):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f'Invalid {name}.')
        if type(self.allow_cross_origin) is not bool:
            raise ValueError('Invalid allow_cross_origin.')


@dataclass(frozen=True)
class ResolvedReference:
    """One distinct external reference and the captured document it resolved to."""
    referrer_url: str
    location: str  # JSON pointer in the referring document
    ref: str
    requested_url: str
    document_url: str  # final URL of the captured document (after redirects)
    pointer: str  # JSON pointer inside the captured document; '' = the whole file
    occurrences: int = 1


@dataclass(frozen=True)
class CaptureFailure:
    """Machine-readable detail for a rejection."""
    ref: str
    target_url: str
    referrer_url: str
    fetch_code: str | None = None  # underlying fetch failure code, when the fetch failed
    http_status: int | None = None
    documents_fetched: int = 0


@dataclass(frozen=True)
class CaptureResult:
    documents: tuple[CapturedDocument, ...] = ()
    references: tuple[ResolvedReference, ...] = ()
    rejection: ValidationRejection | None = None
    failure: CaptureFailure | None = None
    unprocessed: int = 0  # references still queued when capture stopped on a failure
    internal_reference_count: int = 0
    limitations: tuple[str, ...] = ()

    @property
    def ok(self):
        return self.rejection is None

    def __post_init__(self):
        if self.rejection is not None and (self.documents or self.references):
            raise ValueError('A failed capture must not expose partial documents.')


class _Stop(Exception):
    def __init__(self, code, reason, location, failure):
        self.code, self.reason, self.location, self.failure = code, reason, location, failure
        super().__init__(code)


@dataclass
class _State:
    value: object
    final_url: str
    depth: int
    root: bool = False


def _origin(url):
    parts = urlsplit(url)
    default = 443 if parts.scheme.lower() == 'https' else 80
    return parts.scheme.lower(), (parts.hostname or '').lower(), parts.port or default


def _text(value, limit=160):
    value = ''.join(' ' if ord(c) < 32 or ord(c) == 127 else c for c in str(value))
    return value if len(value) <= limit else value[:limit - 1] + '…'


class _Capture:
    def __init__(self, root_url, root_document, budget, limits, allow_loopback):
        self.budget, self.limits, self.allow_loopback = budget, limits, allow_loopback
        self.root_url = normalize_target(DiscoveryRequest(root_url)).normalized_url
        self.root_origin = _origin(self.root_url)
        self.states = {self.root_url: _State(root_document, self.root_url, 0, True)}
        self.documents = []
        self.edges = []
        self.scanned = set()
        self.limitations = []
        self.internal = 0
        self.nodes = 0
        self.processed = 0
        self.queue = deque()

    def add_limitations(self, notes):
        self.limitations.extend(note for note in notes if note not in self.limitations)

    def fail(self, code, reason, location, use, referrer, **detail):
        raise _Stop(code, reason, location, CaptureFailure(
            use.ref, use.url, referrer, documents_fetched=len(self.documents), **detail))

    def count(self, nodes, location, use, referrer):
        self.nodes += nodes
        if self.nodes > self.limits.max_total_nodes:
            self.fail('reference_limit_exceeded',
                      f'More than {self.limits.max_total_nodes} values across referenced documents.',
                      location, use, referrer)

    def run(self):
        root = self.states[self.root_url]
        scan = scan_references(root.value, self.root_url, limits=self.limits.references)
        self.add_limitations(scan.limitations)
        self.internal, self.nodes = scan.local_count, scan.node_count
        if not scan.ok:
            return CaptureResult(rejection=scan.rejection, internal_reference_count=self.internal,
                                 limitations=tuple(self.limitations))
        self.queue.extend((use, self.root_url, 1, False) for use in scan.external)
        try:
            while self.queue:
                self.process(*self.queue.popleft())
        except _Stop as stopped:
            return CaptureResult(
                rejection=ValidationRejection(stopped.code, stopped.reason, stopped.location, STAGE),
                failure=stopped.failure, unprocessed=len(self.queue), internal_reference_count=self.internal,
                limitations=tuple(self.limitations))
        return CaptureResult(documents=tuple(self.documents), references=tuple(self.edges),
                             internal_reference_count=self.internal, limitations=tuple(self.limitations))

    def covered(self, url, tokens):
        """True if this subtree, or an ancestor of it, was already scanned."""
        return any((url, tokens[:i]) in self.scanned for i in range(len(tokens) + 1))

    def process(self, use, referrer, depth, internal):
        location = use.location if referrer == self.root_url else f'{referrer}#{use.location}'
        self.processed += 1
        if self.processed > self.limits.max_references:
            self.fail('reference_limit_exceeded', f'More than {self.limits.max_references} references to process.',
                      location, use, referrer)
        try:
            key = normalize_target(DiscoveryRequest(use.url)).normalized_url
        except DiscoveryInputError as error:
            self.fail('reference_unsupported', f'Reference {_text(use.ref)!r} is not a fetchable URL: {error}',
                      location, use, referrer)
        state = self.states.get(key) or self.load(key, depth, use, referrer, location)
        found, _ = resolve_pointer(state.value, use.tokens)
        if not found:
            self.fail('unresolvable_reference',
                      f'Reference {_text(use.ref)!r} does not point to anything in {_text(state.final_url)}.',
                      location, use, referrer)
        if not internal:
            self.edges.append(ResolvedReference(referrer, use.location, use.ref, key, state.final_url,
                                                json_pointer(use.tokens), use.occurrences))
        if state.root or self.covered(state.final_url, use.tokens):
            return  # the root was scanned in full; a covered subtree adds nothing new
        self.scanned.add((state.final_url, use.tokens))
        scan = scan_references(state.value, state.final_url, start=use.tokens, limits=self.limits.references)
        self.internal += scan.local_count
        self.count(scan.node_count, location, use, referrer)
        if not scan.ok:
            inner = scan.rejection
            self.fail(inner.code, f'In {_text(state.final_url)}: {inner.reason}',
                      f'{state.final_url}#{inner.location or ""}', use, referrer)
        self.add_limitations(scan.limitations)
        self.queue.extend((inner_use, state.final_url, state.depth + 1, False) for inner_use in scan.external)
        # Internal pointers lead to other parts of this same file that the contract also depends on.
        for tokens in scan.local_targets:
            if not self.covered(state.final_url, tokens):
                target = ExternalUse(f'#{json_pointer(tokens)}', state.final_url, tokens, json_pointer(tokens))
                self.queue.append((target, state.final_url, state.depth, True))

    def load(self, key, depth, use, referrer, location):
        limits = self.limits
        if not limits.allow_cross_origin and _origin(key) != self.root_origin:
            self.fail('reference_cross_origin',
                      f'Reference {_text(use.ref)!r} points to another origin ({_text(key)}); '
                      'cross-origin references are not allowed.', location, use, referrer)
        if len(self.documents) >= limits.max_documents:
            self.fail('reference_limit_exceeded', f'More than {limits.max_documents} referenced documents.',
                      location, use, referrer)
        if depth > limits.max_depth:
            self.fail('reference_limit_exceeded', f'References nest deeper than {limits.max_depth} documents.',
                      location, use, referrer)
        fetched = fetch_document(key, self.budget, allow_loopback=self.allow_loopback)
        if not fetched.ok:
            failure = fetched.failure
            code = 'capture_budget_exhausted' if failure.code in BUDGET_CODES else 'reference_unavailable'
            self.fail(code, f'Could not retrieve {_text(key)}: {failure.code} ({failure.reason})',
                      location, use, referrer, fetch_code=failure.code, http_status=fetched.status)
        final = fetched.final_url
        if not limits.allow_cross_origin and _origin(final) != self.root_origin:
            self.fail('reference_cross_origin', f'{_text(key)} redirected to another origin ({_text(final)}); '
                      'cross-origin references are not allowed.', location, use, referrer)
        known = self.states.get(final)
        if known is not None:  # a different URL already led to this document
            self.states[key] = known
            return known
        parsed = parse_document(fetched.content, limits.parsing)
        if not parsed.ok:
            self.fail('reference_invalid_document',
                      f'{_text(final)} is not a usable referenced document: {parsed.rejection.code} '
                      f'({parsed.rejection.reason})', location, use, referrer)
        if not isinstance(parsed.value, (dict, list)):
            # A catch-all "Not Found" page parses as a bare YAML string; never accept that as a definition.
            self.fail('reference_invalid_document',
                      f'{_text(final)} is not a JSON/YAML object or list, so it cannot be a referenced definition.',
                      location, use, referrer)
        self.count(parsed.node_count, location, use, referrer)
        state = self.states[key] = self.states[final] = _State(parsed.value, final, depth)
        self.documents.append(CapturedDocument(final, fetched.content, fetched.retrieved_at, fetched.content_type))
        return state


def capture_references(root_url: str, root_document, budget: DiscoveryBudget, *,
                       limits: CaptureLimits | None = None, allow_loopback=False) -> CaptureResult:
    """Capture every external document reachable from `root_document` via `$ref`.

    `root_url` is the root's final retrieval URL; `root_document` is the parsed,
    already-validated root. The budget is shared with the rest of discovery.
    Stops at the first failure and reports how many references were left unprocessed.
    """
    return _Capture(root_url, root_document, budget, limits or CaptureLimits(), allow_loopback).run()
