"""Bounded extraction of specification hints from configured documentation.

No JavaScript execution, recursive crawling, or automatic trust in linked hosts.
"""

from dataclasses import dataclass
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
    'No recursive crawling, external scripts, JavaScript execution, or external configUrl retrieval.',
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
        self.has_base = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
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


def _static_swagger_urls(script):
    """Support literal JSON object arguments only; never evaluate JavaScript.

    SwaggerUI({"url": "..."}) and SwaggerUIBundle({"urls": [...]}) are
    accepted. Typical JS-only values, unquoted keys and runtime expressions are
    intentionally outside this first parser subset.
    """
    urls = []
    unsupported = False
    calls = list(re.finditer(r'\bSwaggerUI(?:Bundle)?\s*\(', script))
    for call in calls[:20]:
        text = script[call.end():].lstrip()
        try:
            config, end = json.JSONDecoder().raw_decode(text)
        except (ValueError, RecursionError):
            unsupported = True
            continue
        if not isinstance(config, dict) or not text[end:].lstrip().startswith(')'):
            unsupported = True
            continue
        # These mechanisms can override url/urls, so do not guess which wins.
        if any(key in config for key in ('spec', 'configUrl')) or config.get('queryConfigEnabled'):
            unsupported = True
            continue
        if 'urls' in config:
            choices = config['urls']
            if not isinstance(choices, list) or not all(isinstance(item, dict) and isinstance(item.get('url'), str) for item in choices):
                unsupported = True
                continue
            urls.extend(item['url'] for item in choices)
        elif isinstance(config.get('url'), str):
            urls.append(config['url'])
        else:
            unsupported = True
    return urls, unsupported or len(calls) > 20


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
        explicit_label = bool(re.search(r'\b(?:openapi|swagger)\b', label, re.I))
        if explicit_name or explicit_label:
            raw.append((href, 'documentation_link'))
    for script in parser.scripts:
        urls, unsupported = _static_swagger_urls(script)
        raw.extend((url, 'swagger_ui_config') for url in urls)
        if unsupported:
            note('unsupported_configuration', 'Swagger UI configuration exceeds the supported static JSON subset.')
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
        candidates.append(ContractCandidate(
            source_url=url, discovery_method=method, discovery_source=page,
            evidence=(MatchingEvidence('documentation_reference',
                f'This document explicitly references the candidate via {method}; relevance is unverified.', page),),
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
                            budget, allow_loopback=allow_loopback, limitations=LIMITATIONS)
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
    search = fetch_candidates(target, locations, budget, allow_loopback=allow_loopback, limitations=LIMITATIONS)
    if docs.stop_reason and search.stop_reason is None:
        from dataclasses import replace
        search = replace(search, stop_reason=docs.stop_reason)
    return DocumentationSearchResult(docs.fetches, search,
                                     docs.skipped_urls + seeds[limits.max_pages:], tuple(notes))
