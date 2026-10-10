"""Bounded extraction of specification hints from configured documentation.

No JavaScript execution, recursive crawling, or automatic trust in linked hosts.
"""

from dataclasses import dataclass, replace
from html.parser import HTMLParser
import json
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

from radar.discovery.candidates import CandidateSearchResult, fetch_candidates
from radar.discovery.fetch import FetchResult
from radar.discovery.input import DiscoveryInputError, normalize_target
from radar.discovery.limits import BudgetExceeded, DiscoveryBudget
from radar.domain.discovery import ContractCandidate, DiscoveryRequest, MatchingEvidence


LIMITATIONS = (
    'Only bounded documentation seeds and explicit supported links/configurations were examined.',
    'No recursive crawling, external scripts, JavaScript execution, or external configUrl retrieval; only static string literals are read.',
    'Documentation provenance is a discovery hint; OpenAPI validity and API relevance are not assessed.',
)


@dataclass(frozen=True)
class DocumentationLimits:
    max_pages: int = 3
    max_candidates: int = 10
    max_page_bytes: int = 1024 * 1024

    def __post_init__(self):
        for value in (self.max_pages, self.max_candidates, self.max_page_bytes):
            if type(value) is not int or value <= 0:
                raise ValueError('Documentation limits must be positive integers.')


@dataclass(frozen=True)
class DocumentationNote:
    url: str
    code: str
    reason: str


@dataclass(frozen=True)
class DocumentationSearchResult:
    documents: tuple[FetchResult, ...]
    search: CandidateSearchResult
    skipped_document_urls: tuple[str, ...]
    notes: tuple[DocumentationNote, ...]


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.scripts = []
        self.anchor = None
        self.script = None
        self.spec_urls = []
        self.has_base = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('redoc', 'rapi-doc') and attrs.get('spec-url'):
            self.spec_urls.append(attrs['spec-url'])
        if tag == 'base':
            self.has_base = True
        elif tag == 'a':
            self.anchor = [attrs.get('href') or '', attrs.get('title') or '']
        elif tag == 'script':
            self.script = [] if not attrs.get('src') else None

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        elif self.anchor is not None:
            self.anchor[1] += data

    def handle_endtag(self, tag):
        if tag == 'a' and self.anchor is not None:
            self.links.append(tuple(self.anchor))
            self.anchor = None
        elif tag == 'script' and self.script is not None:
            self.scripts.append(''.join(self.script))
            self.script = None


class _Unsupported(Exception):
    """The configuration is not a static literal this parser understands."""


_IDENTIFIER = re.compile(r'[A-Za-z_$][\w$]*')
_DYNAMIC = ('raw', None)
MAX_CONFIG_CHARS = 50_000
MAX_CONFIG_CALLS = 20


class _Literal:
    """Read a JavaScript object literal without evaluating it.

    Understands quoted/bare keys, single/double-quoted strings, comments and
    balanced nesting. Anything else (expressions, calls, spreads) is recorded as
    a non-literal value, so a URL is only taken from a plain string literal.
    """

    def __init__(self, text):
        self.text = text
        self.pos = 0

    def peek(self):
        text = self.text
        while self.pos < len(text):
            if text[self.pos].isspace():
                self.pos += 1
            elif text.startswith('//', self.pos):
                end = text.find('\n', self.pos)
                self.pos = len(text) if end < 0 else end + 1
            elif text.startswith('/*', self.pos):
                end = text.find('*/', self.pos + 2)
                if end < 0:
                    raise _Unsupported
                self.pos = end + 2
            else:
                break
        return text[self.pos] if self.pos < len(text) else ''

    def string(self):
        text, quote, index, out = self.text, self.text[self.pos], self.pos + 1, []
        if quote == '`':
            closing = text.find('`', index)
            if closing < 0 or '${' in text[index:closing]:
                raise _Unsupported  # template expressions are not static
        while index < len(text):
            char = text[index]
            if char == quote:
                self.pos = index + 1
                return ''.join(out)
            if char == '\\':
                if index + 1 >= len(text) or text[index + 1] not in '\\\'"`/':
                    raise _Unsupported
                out.append(text[index + 1])
                index += 2
                continue
            if char == '\n' and quote != '`':
                raise _Unsupported
            out.append(char)
            index += 1
        raise _Unsupported

    def skip_value(self):
        """Skip one balanced value; stop before a top-level ',' '}' or ']'."""
        start, stack = self.pos, []
        pairs = {'(': ')', '[': ']', '{': '}'}
        while True:
            char = self.peek()
            if not char:
                raise _Unsupported
            if char in '"\'`':
                self.string()
                continue
            if char in pairs:
                stack.append(pairs[char])
            elif char in ')]}':
                if not stack:
                    return self.text[start:self.pos].strip()
                if stack.pop() != char:
                    raise _Unsupported
            elif char == ',' and not stack:
                return self.text[start:self.pos].strip()
            self.pos += 1

    def value(self, key):
        char = self.peek()
        if char in '"\'':
            saved = self.pos
            literal = self.string()
            if self.peek() in (',', '}', ']'):
                return ('str', literal)
            self.pos = saved  # e.g. 'a' + suffix: not a plain literal
        elif key == 'urls' and char == '[':
            return ('urls', self.url_array())
        return ('raw', self.skip_value())

    def url_array(self):
        self.pos += 1
        urls = []
        while True:
            if self.peek() == ']':
                self.pos += 1
                return urls
            entry = self.object().get('url')
            if entry is None or entry[0] != 'str':
                raise _Unsupported
            urls.append(entry[1])
            if self.peek() == ',':
                self.pos += 1
            elif self.peek() != ']':
                raise _Unsupported

    def object(self):
        if self.peek() != '{':
            raise _Unsupported
        self.pos += 1
        members = {}
        while True:
            char = self.peek()
            if char == '}':
                self.pos += 1
                return members
            if char in '"\'':
                key = self.string()
            else:
                match = _IDENTIFIER.match(self.text, self.pos)
                if not match:
                    raise _Unsupported
                key, self.pos = match.group(), match.end()
            if self.peek() == ':':
                self.pos += 1
                members[key] = self.value(key)
            else:  # shorthand property or method
                members[key] = ('raw', self.skip_value())
            if self.peek() == ',':
                self.pos += 1
            elif self.peek() != '}':
                raise _Unsupported


def _static_config_urls(script):
    """Return ((url, method), ...) and whether any configuration was unsupported.

    Supports Swagger UI `url`/`urls` and `Redoc.init('url', ...)` given as plain
    string literals. Options that can replace or override the URL (`spec`,
    `configUrl`, `queryConfigEnabled`) make the call unsupported rather than
    guessed. Dynamic values, external scripts and JavaScript evaluation are not
    supported and are reported to the caller.
    """
    found = []
    unsupported = False
    swagger = list(re.finditer(r'\bSwaggerUI(?:Bundle)?\s*\(', script))
    redoc = list(re.finditer(r'\bRedoc\.init\s*\(', script))
    for match in swagger[:MAX_CONFIG_CALLS]:
        try:
            config = _Literal(script[match.end():match.end() + MAX_CONFIG_CHARS]).object()
        except (_Unsupported, RecursionError):
            unsupported = True
            continue
        if 'spec' in config or 'configUrl' in config or config.get('queryConfigEnabled', ('raw', 'false')) != ('raw', 'false'):
            unsupported = True
            continue
        found_here = False
        for key in ('url', 'urls'):
            kind, value = config.get(key, (None, None))
            if kind == 'str':
                found.append((value, 'swagger_ui_config'))
                found_here = True
            elif kind == 'urls':
                found.extend((url, 'swagger_ui_config') for url in value)
                found_here = found_here or bool(value)
            elif kind is not None:
                unsupported = True
        unsupported = unsupported or not found_here
    for match in redoc[:MAX_CONFIG_CALLS]:
        reader = _Literal(script[match.end():match.end() + MAX_CONFIG_CHARS])
        try:
            if reader.peek() not in ('"', "'"):
                raise _Unsupported
            found.append((reader.string(), 'redoc_config'))
        except _Unsupported:
            unsupported = True
    return tuple(found), unsupported or len(swagger) > MAX_CONFIG_CALLS or len(redoc) > MAX_CONFIG_CALLS


def _origin(url):
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)


def _absolute_url(url):
    if not isinstance(url, str) or urlsplit(url).scheme.lower() not in ('http', 'https'):
        raise ValueError('Documentation seeds require absolute HTTP(S) URLs.')
    return normalize_target(DiscoveryRequest(url)).normalized_url


def extract_document_links(document: FetchResult, limits: DocumentationLimits):
    """Pure extraction from a retrieved HTML page; return hints and limitations."""
    page = document.final_url
    notes = []
    def note(code, reason):
        item = DocumentationNote(page, code, reason)
        if item not in notes:
            notes.append(item)
    if not document.ok or document.content is None:
        return (), ()
    if len(document.content) > limits.max_page_bytes:
        return (), (DocumentationNote(page, 'page_size_limit', 'Document exceeds HTML parsing limit.'),)
    media_type = (document.content_type or '').split(';')[0].strip().lower()
    if media_type != 'text/html':
        return (), (DocumentationNote(page, 'unsupported_media_type', 'Only text/html documentation is parsed.'),)
    try:
        text = document.content.decode('utf-8-sig')
    except UnicodeError:
        return (), (DocumentationNote(page, 'unsupported_encoding', 'Only UTF-8 HTML is supported.'),)
    parser = _PageParser()
    parser.feed(text)
    parser.close()
    raw = []
    for href, label in parser.links:
        # Plain JSON downloads are not necessarily API definitions.
        try:
            filename = urlsplit(href).path.rsplit('/', 1)[-1].lower()
        except ValueError:
            note('invalid_link', 'A malformed link was ignored.')
            continue
        explicit_name = filename in {'openapi.json', 'openapi.yaml', 'openapi.yml', 'swagger.json', 'swagger.yaml', 'swagger.yml'}
        # A label alone is not enough: "OpenAPI" often links to an HTML page.
        explicit_label = bool(re.search(r'\b(?:openapi|swagger)\b', label, re.I)) and \
            urlsplit(href).path.lower().endswith(('.json', '.yaml', '.yml'))
        if explicit_name or explicit_label:
            raw.append((href, 'documentation_link'))
    raw.extend((href, 'spec_url_attribute') for href in parser.spec_urls)
    for script in parser.scripts:
        configured, unsupported = _static_config_urls(script)
        raw.extend(configured)
        if unsupported:
            note('unsupported_configuration', 'Swagger UI or Redoc configuration exceeds the supported static literal subset.')
    candidates = []
    seen = set()
    for href, method in raw:
        try:
            if not href.strip() or href.strip().startswith('#'):
                continue
            if parser.has_base and not urlsplit(href).scheme:
                note('unsupported_base_url', 'Relative links on pages with <base> are not resolved.')
                continue
            url = _absolute_url(urljoin(page, href))
        except (ValueError, DiscoveryInputError):
            note('invalid_link', 'A non-HTTP, credential-bearing, fragmented, or malformed link was ignored.')
            continue
        if (url, method) in seen:
            continue
        seen.add((url, method))
        if len(candidates) >= limits.max_candidates:
            note('candidate_limit', 'Additional extracted links were omitted by the per-page candidate limit.')
            break
        relation = 'same-origin' if _origin(url) == _origin(page) else 'cross-origin'
        candidates.append(ContractCandidate(
            source_url=url, discovery_method=method, discovery_source=page,
            evidence=(
                MatchingEvidence('documentation_reference',
                    f'This document explicitly references the candidate via {method}; relevance is unverified.', page),
                MatchingEvidence('candidate_origin',
                    f'The candidate is {relation} relative to the documentation page.', page),
            ),
            limitations=LIMITATIONS,
        ))
    return tuple(candidates), tuple(notes)


def search_documentation(
    request: DiscoveryRequest,
    budget: DiscoveryBudget,
    *,
    documentation_urls=None,
    limits=None,
    allow_loopback=False,
    cache=None,
) -> DocumentationSearchResult:
    """Default to origin /docs and /documentation; accept explicit trusted seeds.

    Cross-origin seeds must be supplied by caller configuration (e.g. verified
    provider metadata), not by arbitrary links in retrieved pages. No automatic
    claim of official provenance is made. Redirected HTML is parsed only if its
    final origin is one of the configured seed origins.
    """
    limits = limits or DocumentationLimits()
    target = normalize_target(request)
    parsed = urlsplit(target.normalized_url)
    if documentation_urls is None:
        documentation_urls = tuple(urlunsplit((parsed.scheme, parsed.netloc, path, '', ''))
                                   for path in ('/docs', '/documentation'))
    if not isinstance(documentation_urls, (tuple, list)) or len(documentation_urls) > 100:
        raise ValueError('Supply at most 100 explicit documentation seed URLs.')
    seeds = tuple(dict.fromkeys(_absolute_url(url) for url in documentation_urls))
    chosen = seeds[:limits.max_pages]
    trusted_origins = {_origin(url) for url in chosen}
    docs = fetch_candidates(target, tuple(ContractCandidate(url, 'documentation_seed', url) for url in chosen),
                            budget, allow_loopback=allow_loopback, limitations=LIMITATIONS, cache=cache)
    notes = []
    locations = []
    unique_urls = set()
    if len(seeds) > len(chosen):
        notes.append(DocumentationNote(target.normalized_url, 'page_limit', 'Additional documentation seeds were skipped.'))
    for document in docs.fetches:
        if not document.ok:
            continue
        if _origin(document.final_url) not in trusted_origins:
            notes.append(DocumentationNote(document.final_url, 'unconfigured_documentation_origin',
                                           'Redirected page is outside configured documentation origins; it was not parsed.'))
            continue
        try:
            budget.remaining()
        except BudgetExceeded as error:
            notes.append(DocumentationNote(document.final_url, error.code, 'Deadline reached before parsing.'))
            break
        candidates, page_notes = extract_document_links(document, limits)
        notes.extend(page_notes)
        for candidate in candidates:
            if candidate.source_url not in unique_urls and len(unique_urls) >= limits.max_candidates:
                note = DocumentationNote(document.final_url, 'candidate_limit', 'Additional candidate URLs were omitted by the search limit.')
                if note not in notes:
                    notes.append(note)
                continue
            unique_urls.add(candidate.source_url)
            locations.append(candidate)
    search = fetch_candidates(target, locations, budget, allow_loopback=allow_loopback, limitations=LIMITATIONS,
                              cache=cache)
    if docs.stop_reason and search.stop_reason is None:
        search = replace(search, stop_reason=docs.stop_reason)
    return DocumentationSearchResult(docs.fetches, search,
                                     docs.skipped_urls + seeds[limits.max_pages:], tuple(notes))
