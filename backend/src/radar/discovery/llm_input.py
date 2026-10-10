"""Reduce a fetched documentation page to what a model needs to pick the next link to examine.

Pure and deterministic: no network, no model, no JavaScript execution. The output is the only page content a
model may see: the title and headings for context, the links (specification links, and navigation links that
may lead toward one indirectly) each with an identifier such as L3 that a model can choose, and the snippets
of viewer configuration. A model chooses identifiers; it never supplies a URL. The page is untrusted data.
"""

from dataclasses import dataclass
from html.parser import HTMLParser
import re
from urllib.parse import urldefrag, urljoin, urlsplit

from radar.discovery.fetch import FetchResult
from radar.discovery.link_scoring import MODEL_MIN_SCORE, score_link


KEYWORDS = re.compile(
    r'openapi|swagger|redoc|rapidoc|api[-_ ]?docs?\b|\bspec(?:s|ification)?\b|spec-?url', re.I)
# Inline scripts are searched with a stricter pattern than links: words like "spec" appear in any page-state blob.
SCRIPT_KEYWORDS = re.compile(r'openapi|swagger|redoc|rapidoc|spec-?url|api-?description', re.I)
MAX_SCRIPT_SNIPPETS = 4
# <link> relations that point at assets, never at a contract.
ASSET_RELATIONS = frozenset({'stylesheet', 'icon', 'shortcut', 'apple-touch-icon', 'mask-icon', 'preload', 'prefetch',
                             'preconnect', 'dns-prefetch', 'manifest', 'modulepreload'})
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
    max_links: int = 30  # selectable links kept; specification links first, then the most promising navigation links
    max_headings: int = 6

    def __post_init__(self):
        for name in ('max_page_bytes', 'max_chars', 'max_items', 'max_snippet_chars', 'max_label_chars', 'max_links',
                     'max_headings'):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f'Invalid {name}.')


@dataclass(frozen=True)
class ReducedItem:
    kind: str  # 'link', 'script', 'tag', 'attribute', 'heading' or 'title'
    text: str  # the exact line shown to a model
    url: str | None = None
    label: str | None = None
    id: str | None = None  # L1, L2, ...: set on selectable items (those with a URL); a model chooses these


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
        self._heading = None
        self._title = None

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
            if tag == 'title':
                self._title = []
        elif tag in ('h1', 'h2', 'h3'):
            self._heading = []
        elif tag == 'link' and attrs.get('href'):
            self.entries.append(('tag_link', attrs))
        elif tag == 'meta':
            self.entries.append(('tag_meta', attrs))
        for name in SPEC_ATTRIBUTES:
            if attrs.get(name):
                self.entries.append(('attribute', (tag, name, attrs[name])))

    def handle_data(self, data):
        if self._heading is not None:
            self._heading.append(data)
        if self._title is not None:
            self._title.append(data)
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
            if tag == 'title' and self._title is not None:
                self.entries.append(('title', ''.join(self._title)))
                self._title = None
            self._ignored = None
        if tag in ('h1', 'h2', 'h3') and self._heading is not None:
            self.entries.append(('heading', ''.join(self._heading)))
            self._heading = None

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
    for match in SCRIPT_KEYWORDS.finditer(script):
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
    """Keep the title and headings, specification and navigation links (with identifiers), and viewer configuration."""
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
    link_rank = {}  # candidate index -> (is a specification link, navigation score)
    headings = 0
    for kind, payload in collector.entries:
        if kind == 'title':
            text = _clean(payload, limits.max_label_chars)
            if text:
                candidates.append(ReducedItem('title', f'TITLE "{text}"'))
        elif kind == 'heading':
            text = _clean(payload, limits.max_label_chars)
            if text and headings < limits.max_headings:
                headings += 1
                candidates.append(ReducedItem('heading', f'HEADING "{text}"'))
        elif kind == 'link':
            url = _resolve(base, payload[0])
            label = _clean(payload[1], limits.max_label_chars)
            if url:
                specification = _relevant(url, label)
                score = score_link(url, label)
                if specification or score >= MODEL_MIN_SCORE:
                    link_rank[len(candidates)] = (specification, score)
                    candidates.append(ReducedItem('link', f'LINK {url} "{label}"', url, label))
        elif kind == 'script_src':
            continue  # external scripts are never fetched or run, so a model cannot use them; explain_page lists them
        elif kind == 'script':
            for snippet in _snippets(payload, limits.max_snippet_chars):
                if sum(1 for c in candidates if c.kind == 'script') < MAX_SCRIPT_SNIPPETS:
                    candidates.append(ReducedItem('script', f'SCRIPT {snippet}'))
        elif kind == 'tag_link':
            url = _resolve(base, payload['href'])
            relations = set(payload.get('rel', '').lower().split())
            declared = (payload.get('type') or '').lower()
            asset = bool(relations & ASSET_RELATIONS) and not relations & SPEC_RELATIONS
            if url and not asset and (relations & SPEC_RELATIONS or KEYWORDS.search(url)
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

    truncated = False
    notes = []
    if len(link_rank) > limits.max_links:  # keep specification links first, then the highest navigation scores
        keep = set(sorted(link_rank, key=lambda i: (not link_rank[i][0], -link_rank[i][1], i))[:limits.max_links])
        candidates = [c for i, c in enumerate(candidates) if i not in link_rank or i in keep]
        truncated = True
    items, lines, size, seen, selectable = [], [], 0, set(), 0
    for item in candidates:
        if item.text in seen:
            continue
        seen.add(item.text)
        if item.url:  # selectable: give it an identifier the model can choose
            selectable += 1
            item = ReducedItem(item.kind, f'L{selectable} {item.text}', item.url, item.label, f'L{selectable}')
        if len(items) >= limits.max_items or size + len(item.text) + 1 > limits.max_chars:
            truncated = True
            break
        items.append(item)
        lines.append(item.text)
        size += len(item.text) + 1
    if truncated:
        notes.append(ReductionNote('truncated', 'Additional relevant items were omitted by the reduction limits.'))
    return ReducedPage(page_url, tuple(items), '\n'.join(lines), truncated, tuple(notes))


# --- diagnosis: why did the reducer keep or drop things? ------------------------------------------------

@dataclass(frozen=True)
class PageReport:
    page_url: str
    reduced: ReducedPage
    anchors_total: int = 0
    anchors_kept: int = 0
    dropped_links: tuple[tuple[str, str], ...] = ()  # (url, label), the first few the reducer discarded
    inline_scripts: int = 0
    inline_script_chars: int = 0
    script_srcs: tuple[str, ...] = ()  # external scripts: they are never fetched or run
    script_srcs_total: int = 0
    link_tags: int = 0
    app_shell: bool = False
    diagnosis: str = ''


def explain_page(document: FetchResult, limits: ReductionLimits | None = None, *, examples: int = 12) -> PageReport:
    """Say what the reducer did with a page and why. Pure; calls no model and spends nothing."""
    limits = limits or ReductionLimits()
    reduced = reduce_page(document, limits)
    if reduced.notes and not reduced.items and (not document.ok or document.content is None
                                                or reduced.notes[0].code != 'truncated'):
        return PageReport(document.final_url, reduced, diagnosis=f'The page was not reduced: {reduced.notes[0].reason}')
    collector = _Collector()
    try:
        collector.feed(document.content.decode('utf-8-sig'))
        collector.close()
    except (ValueError, AssertionError, RecursionError, UnicodeError):
        return PageReport(document.final_url, reduced, diagnosis='The page could not be parsed.')
    base = urljoin(document.final_url, collector.base) if collector.base else document.final_url
    anchors = [e for e in collector.entries if e[0] == 'link']
    kept_urls = {item.url for item in reduced.items if item.kind == 'link'}
    dropped = []
    for _, (href, text) in anchors:
        url = _resolve(base, href)
        if url and url not in kept_urls and (url, _clean(text, limits.max_label_chars)) not in dropped:
            dropped.append((url, _clean(text, limits.max_label_chars)))
    scripts = [e[1] for e in collector.entries if e[0] == 'script']
    srcs = []
    for _, payload in (e for e in collector.entries if e[0] == 'script_src'):
        url = _resolve(base, payload)
        if url and url not in srcs:
            srcs.append(url)
    report = dict(anchors_total=len(anchors), anchors_kept=len(kept_urls), dropped_links=tuple(dropped[:examples]),
                  inline_scripts=len(scripts), inline_script_chars=sum(len(s) for s in scripts),
                  script_srcs=tuple(srcs[:examples]), script_srcs_total=len(srcs),
                  link_tags=sum(1 for e in collector.entries if e[0] == 'tag_link'))
    shell = len(anchors) <= 5 and (len(srcs) >= 1 or sum(len(s) for s in scripts) > 2000)
    if reduced.items:
        diagnosis = f'{len(reduced.items)} item(s) would be sent to the model ({len(reduced.text)} characters).'
    elif shell:
        diagnosis = ('Looks like a JavaScript app shell: only %d link(s) in the HTML and %d external script(s). The real '
                     'content is probably loaded by scripts, which Radar does not run or fetch.' % (len(anchors), len(srcs)))
    elif not anchors:
        diagnosis = 'The page has no links and nothing resembling a specification reference.'
    else:
        diagnosis = (f'{len(anchors)} link(s), but none mentions a specification (openapi, swagger, redoc, spec, a '
                     '.json/.yaml link with a label). If one of the dropped links below is the right one, the reducer is too strict.')
    return PageReport(document.final_url, reduced, diagnosis=diagnosis, app_shell=shell, **report)


def main(argv=None) -> int:
    """python -m radar.discovery.llm_input PAGE_URL  — show what the language-model step would see (spends nothing)."""
    import argparse
    from radar.discovery.fetch import fetch_document
    from radar.discovery.limits import DiscoveryBudget

    parser = argparse.ArgumentParser(prog='python -m radar.discovery.llm_input', description=main.__doc__)
    parser.add_argument('url', help='a documentation page')
    parser.add_argument('--allow-loopback', action='store_true')
    parser.add_argument('--dropped', type=int, default=12, help='how many discarded links to list')
    args = parser.parse_args(argv)
    result = fetch_document(args.url, DiscoveryBudget(), allow_loopback=args.allow_loopback)
    if not result.ok:
        print(f'Could not fetch the page: {result.failure.code} ({result.failure.reason})')
        return 1
    report = explain_page(result, examples=args.dropped)
    print(f'Page: {report.page_url}  ({result.status}, {result.content_type}, {len(result.content):,} bytes)')
    print(f'Diagnosis: {report.diagnosis}')
    print(f'Links: {report.anchors_total} total, {report.anchors_kept} kept | inline scripts: {report.inline_scripts} '
          f'({report.inline_script_chars:,} chars) | external scripts: {report.script_srcs_total} (never fetched or run) '
          f'| <link> tags: {report.link_tags}')
    if report.reduced.items:
        print('\nWhat the model would see:')
        for line in report.reduced.text.splitlines():
            print(f'  {line[:200]}')
    if report.dropped_links:
        print('\nLinks the reducer discarded (first few):')
        for url, label in report.dropped_links:
            print(f'  "{label}"  ->  {url}')
    if report.script_srcs:
        print('\nExternal scripts on the page:')
        for url in report.script_srcs:
            print(f'  {url}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
