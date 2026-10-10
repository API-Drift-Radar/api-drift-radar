"""Reduce a fetched documentation page to the few items that could point to a spec.

Pure and deterministic: no network, no model, no JavaScript execution. The
output is the only page content a later LLM step may see, and the only text
against which its suggestions may be verified. The page is untrusted data.
"""

from dataclasses import dataclass
from html.parser import HTMLParser
import re
from urllib.parse import urldefrag, urljoin, urlsplit

from radar.discovery.fetch import FetchResult


KEYWORDS = re.compile(
    r'openapi|swagger|redoc|rapidoc|api[-_ ]?docs?\b|\bspec(?:s|ification)?\b|spec-?url', re.I)
SPEC_EXTENSION = re.compile(r'\.(?:json|ya?ml)$', re.I)
SPEC_ATTRIBUTES = ('spec-url', 'data-url', 'api-description-url', 'apidescriptionurl')
SPEC_RELATIONS = {'service-desc', 'describedby'}
HTML_TYPES = {'text/html', 'application/xhtml+xml'}
SNIPPET_WINDOW = 200


@dataclass(frozen=True)
class ReductionLimits:
    max_page_bytes: int = 1024 * 1024
    max_chars: int = 12_000
    max_items: int = 100
    max_snippet_chars: int = 500
    max_label_chars: int = 120

    def __post_init__(self):
        for name in ('max_page_bytes', 'max_chars', 'max_items', 'max_snippet_chars', 'max_label_chars'):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f'Invalid {name}.')


@dataclass(frozen=True)
class ReducedItem:
    kind: str  # 'link', 'script', 'script_src', 'tag' or 'attribute'
    text: str  # the exact line shown to a model
    url: str | None = None
    label: str | None = None


@dataclass(frozen=True)
class ReductionNote:
    code: str
    reason: str


@dataclass(frozen=True)
class ReducedPage:
    page_url: str
    items: tuple[ReducedItem, ...] = ()
    text: str = ''
    truncated: bool = False
    notes: tuple[ReductionNote, ...] = ()


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.entries = []  # document order: (kind, payload)
        self.base = None
        self._anchor = None
        self._script = None
        self._ignored = None  # style/title content is never label text

    def _flush_anchor(self):
        if self._anchor is not None:
            href, title, parts = self._anchor
            self.entries.append(('link', (href, ' '.join([title, *parts]))))
            self._anchor = None

    def handle_starttag(self, tag, attrs):
        attrs = {name: value or '' for name, value in attrs}
        if tag == 'base' and self.base is None and attrs.get('href'):
            self.base = attrs['href']
        elif tag == 'a':
            self._flush_anchor()
            if attrs.get('href'):
                self._anchor = (attrs['href'], attrs.get('title') or attrs.get('aria-label') or '', [])
        elif tag == 'script':
            if attrs.get('src'):
                self.entries.append(('script_src', attrs['src']))
            else:
                self._script = []
        elif tag in ('style', 'title'):
            self._ignored = tag
        elif tag == 'link' and attrs.get('href'):
            self.entries.append(('tag_link', attrs))
        elif tag == 'meta':
            self.entries.append(('tag_meta', attrs))
        for name in SPEC_ATTRIBUTES:
            if attrs.get(name):
                self.entries.append(('attribute', (tag, name, attrs[name])))

    def handle_data(self, data):
        if self._script is not None:
            self._script.append(data)
        elif self._ignored is None and self._anchor is not None:
            self._anchor[2].append(data)

    def handle_endtag(self, tag):
        if tag == 'a':
            self._flush_anchor()
        elif tag == 'script' and self._script is not None:
            self.entries.append(('script', ''.join(self._script)))
            self._script = None
        elif tag == self._ignored:
            self._ignored = None

    def close(self):
        super().close()
        self._flush_anchor()
        if self._script is not None:
            self.entries.append(('script', ''.join(self._script)))
            self._script = None


def _clean(text, limit):
    text = ''.join(' ' if ord(c) < 32 or ord(c) == 127 else c for c in text)
    text = ' '.join(text.split()).replace('"', "'")
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _snippets(script, limit):
    """Windows around keyword matches, merged when they overlap, in order."""
    windows = []
    for match in KEYWORDS.finditer(script):
        start, end = max(0, match.start() - SNIPPET_WINDOW), min(len(script), match.end() + SNIPPET_WINDOW)
        if windows and start <= windows[-1][1]:
            windows[-1][1] = end
        else:
            windows.append([start, end])
    return [_clean(script[start:end], limit) for start, end in windows]


def _resolve(base, href):
    """Absolute HTTP(S) URL without fragment, or None for anything else."""
    href = href.strip()
    if not href or href.startswith('#'):
        return None  # a same-page anchor, not a separate document
    try:
        url, _ = urldefrag(urljoin(base, href))
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in ('http', 'https') or not parsed.hostname:
            return None
    except ValueError:
        return None
    return url


def _relevant(url, label=''):
    path = urlsplit(url).path
    return bool(KEYWORDS.search(url) or KEYWORDS.search(label) or (SPEC_EXTENSION.search(path) and label))


def reduce_page(document: FetchResult, limits: ReductionLimits | None = None) -> ReducedPage:
    """Keep spec-related links, script snippets and tags; drop everything else."""
    limits = limits or ReductionLimits()
    page_url = document.final_url
    if not document.ok or document.content is None:
        return ReducedPage(page_url, notes=(ReductionNote('no_content', 'The page was not retrieved.'),))
    if len(document.content) > limits.max_page_bytes:
        return ReducedPage(page_url, notes=(ReductionNote('page_size_limit', 'Page exceeds the reduction size limit.'),))
    media_type = (document.content_type or '').split(';')[0].strip().lower()
    if media_type not in HTML_TYPES:
        return ReducedPage(page_url, notes=(ReductionNote('unsupported_media_type', 'Only HTML pages are reduced.'),))
    try:
        html = document.content.decode('utf-8-sig')
    except UnicodeError:
        return ReducedPage(page_url, notes=(ReductionNote('unsupported_encoding', 'Only UTF-8 pages are supported.'),))
    collector = _Collector()
    try:
        collector.feed(html)
        collector.close()
    except (ValueError, AssertionError, RecursionError):
        return ReducedPage(page_url, notes=(ReductionNote('parse_error', 'The page could not be parsed.'),))

    base = urljoin(page_url, collector.base) if collector.base else page_url
    candidates = []
    for kind, payload in collector.entries:
        if kind == 'link':
            url = _resolve(base, payload[0])
            label = _clean(payload[1], limits.max_label_chars)
            if url and _relevant(url, label):
                candidates.append(ReducedItem('link', f'LINK {url} "{label}"', url, label))
        elif kind == 'script_src':
            url = _resolve(base, payload)
            if url and KEYWORDS.search(url):
                candidates.append(ReducedItem('script_src', f'SCRIPT_SRC {url}', url))
        elif kind == 'script':
            for snippet in _snippets(payload, limits.max_snippet_chars):
                candidates.append(ReducedItem('script', f'SCRIPT {snippet}'))
        elif kind == 'tag_link':
            url = _resolve(base, payload['href'])
            relations = set(payload.get('rel', '').lower().split())
            declared = (payload.get('type') or '').lower()
            if url and (relations & SPEC_RELATIONS or KEYWORDS.search(url)
                        or ('alternate' in relations and ('json' in declared or 'yaml' in declared))):
                rel = _clean(payload.get('rel', ''), limits.max_label_chars)
                candidates.append(ReducedItem('tag', f'TAG link rel="{rel}" {url}', url))
        elif kind == 'tag_meta':
            text = _clean(f"{payload.get('name') or payload.get('property') or ''} {payload.get('content', '')}",
                          limits.max_snippet_chars)
            if KEYWORDS.search(text):
                candidates.append(ReducedItem('tag', f'TAG meta {text}'))
        else:
            tag, name, value = payload
            url = _resolve(base, value)
            if url:
                candidates.append(ReducedItem('attribute', f'ATTRIBUTE <{tag} {name}> {url}', url))

    items, lines, size, seen = [], [], 0, set()
    truncated = False
    notes = []
    for item in candidates:
        if item.text in seen:
            continue
        seen.add(item.text)
        if len(items) >= limits.max_items or size + len(item.text) + 1 > limits.max_chars:
            truncated = True
            break
        items.append(item)
        lines.append(item.text)
        size += len(item.text) + 1
    if truncated:
        notes.append(ReductionNote('truncated', 'Additional relevant items were omitted by the reduction limits.'))
    return ReducedPage(page_url, tuple(items), '\n'.join(lines), truncated, tuple(notes))
