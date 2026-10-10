"""Static extraction from documentation pages and the viewer-configuration assets they link to.

Nothing is executed. A page is parsed as HTML; configuration is read only where it is a static literal (a
quoted string, a JSON-like object) using the same literal scanner as documentation.py; anything dynamic is
reported as unsupported instead of guessed. Relative URLs found in a script or in a Swagger configuration
file resolve against the PAGE that loads them, as a browser would, not against the script's own URL.

Recognised: Swagger UI `url`, `urls`, `configUrl` and embedded `spec` (inline, or in the JSON a `configUrl`
serves), `Redoc.init('...')`, `<redoc spec-url>`, linked initializer files by file name, and RequireJS
`data-main` scripts that name apiDoc's `api_data` / `api_project` modules. Bundles are never fetched.
"""

from dataclasses import dataclass
from html.parser import HTMLParser
import json
import re
from urllib.parse import urljoin, urlsplit

from radar.discovery.documentation import MAX_CONFIG_CALLS, MAX_CONFIG_CHARS, _Literal, _Unsupported

MAX_ANCHORS = 400
MAX_INLINE_SCRIPTS = 20
MAX_INLINE_SCRIPT_CHARS = 200_000
MAX_HEADINGS = 12
HEADING_CHARS = 160
# File names of viewer initializers: small files that set up Swagger UI or Redoc. Bundles are excluded.
INITIALIZER = re.compile(
    r'(?i)^(?:swagger|redoc|openapi|api-?docs?)[\w.-]*?(?:init|initializer|config|setup)(?:\.min)?\.js$')
NOT_INITIALIZER = re.compile(r'(?i)bundle|standalone|preset|chunk|vendor|polyfill')
APIDOC_MODULE = re.compile(r"""['"]([^'"\s]*api_(?:data|project)[^'"\s]*)['"]""")
SPEC_ATTRIBUTES = ('spec-url', 'data-url', 'apidescriptionurl', 'api-description-url')


@dataclass(frozen=True)
class ViewerRef:
    url: str  # as written; resolve against the page that loads the script
    mechanism: str  # swagger_ui_config, swagger_config_url_entry, redoc_config


@dataclass(frozen=True)
class ViewerScan:
    description_urls: tuple[ViewerRef, ...] = ()
    config_urls: tuple[str, ...] = ()
    embedded_specs: tuple[bytes, ...] = ()
    unsupported: tuple[tuple[str, str], ...] = ()  # (code, detail)


def _json_object(raw):
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def scan_viewer_script(script: str) -> ViewerScan:
    """Static Swagger UI / Redoc configuration in script text. Dynamic values are reported, never guessed."""
    refs, configs, specs, unsupported = [], [], [], []
    calls = list(re.finditer(r'\bSwaggerUI(?:Bundle)?\s*\(', script))
    for match in calls[:MAX_CONFIG_CALLS]:
        try:
            config = _Literal(script[match.end():match.end() + MAX_CONFIG_CHARS]).object()
        except (_Unsupported, RecursionError):
            unsupported.append(('configuration_not_literal', 'SwaggerUI is not called with a static object literal.'))
            continue
        found, source_problem = False, False
        for key, label in (('url', 'url'), ('urls', 'urls'), ('configUrl', 'configUrl'), ('spec', 'spec')):
            if key not in config:
                continue
            kind, value = config[key]
            if key == 'url' and kind == 'str':
                refs.append(ViewerRef(value, 'swagger_ui_config'))
            elif key == 'urls' and kind == 'urls':
                refs.extend(ViewerRef(url, 'swagger_ui_config') for url in value)
            elif key == 'configUrl' and kind == 'str':
                configs.append(value)
            elif key == 'spec' and kind == 'raw' and _json_object(value) is not None:
                specs.append(value.encode('utf-8'))
            else:
                unsupported.append((f'{label}_not_literal', f'Swagger UI "{label}" is not a static literal.'))
                source_problem = True
                continue
            found = True
        if config.get('queryConfigEnabled', ('raw', 'false')) != ('raw', 'false'):
            unsupported.append(('query_config_enabled', 'Swagger UI may take configuration from query parameters.'))
        if not found and not source_problem:  # a named-but-dynamic source is already explained above
            unsupported.append(('no_literal_source', 'The Swagger UI call names no static url, urls, configUrl or spec.'))
    if len(calls) > MAX_CONFIG_CALLS:
        unsupported.append(('too_many_calls', 'More Swagger UI calls than are read.'))
    for match in list(re.finditer(r'\bRedoc\.init\s*\(', script))[:MAX_CONFIG_CALLS]:
        reader = _Literal(script[match.end():match.end() + MAX_CONFIG_CHARS])
        try:
            if reader.peek() not in ('"', "'"):
                raise _Unsupported
            refs.append(ViewerRef(reader.string(), 'redoc_config'))
        except _Unsupported:
            unsupported.append(('url_not_literal', 'Redoc.init is not given a static URL.'))
    return ViewerScan(tuple(dict.fromkeys(refs)), tuple(dict.fromkeys(configs)), tuple(specs), tuple(dict.fromkeys(unsupported)))


def scan_swagger_config_json(content: bytes) -> ViewerScan | None:
    """The JSON a Swagger UI `configUrl` serves (springdoc's /v3/api-docs/swagger-config, for example).

    None if it is not such an object. An embedded `spec` object is returned as JSON bytes.
    """
    try:
        data = json.loads(content.decode('utf-8-sig'))
    except (ValueError, UnicodeError, RecursionError):
        return None
    if not isinstance(data, dict) or not ({'url', 'urls', 'spec'} & set(data)):
        return None
    refs, specs, unsupported = [], [], []
    if isinstance(data.get('url'), str) and data['url'].strip():
        refs.append(ViewerRef(data['url'], 'swagger_config_url_entry'))
    if isinstance(data.get('urls'), list):
        for entry in data['urls'][:50]:
            if isinstance(entry, dict) and isinstance(entry.get('url'), str) and entry['url'].strip():
                refs.append(ViewerRef(entry['url'], 'swagger_config_url_entry'))
            else:
                unsupported.append(('urls_entry_invalid', 'A urls entry has no url string.'))
    elif 'urls' in data:
        unsupported.append(('urls_not_a_list', 'The configuration "urls" is not a list.'))
    if isinstance(data.get('spec'), dict):
        specs.append(json.dumps(data['spec'], ensure_ascii=False).encode('utf-8'))
    elif 'spec' in data:
        unsupported.append(('spec_not_an_object', 'The configuration "spec" is not an object.'))
    return ViewerScan(tuple(dict.fromkeys(refs)), (), tuple(specs), tuple(dict.fromkeys(unsupported)))


def scan_requirejs_main(script: str, script_url: str) -> tuple[str, ...]:
    """URLs of apiDoc's `api_data` / `api_project` modules named by a RequireJS data-main script.

    RequireJS resolves module ids against the data-main script's directory, so they are resolved there.
    Only recognition: the data files themselves are not fetched.
    """
    urls = []
    for module in APIDOC_MODULE.findall(script[:MAX_INLINE_SCRIPT_CHARS]):
        name = module if re.search(r'\.\w+$', module) else module + '.js'
        try:
            url = urljoin(script_url, name)
        except ValueError:
            continue
        if urlsplit(url).scheme in ('http', 'https') and url not in urls:
            urls.append(url)
    return tuple(urls[:4])


def is_initializer(url: str) -> bool:
    """A small viewer-initializer script by file name (not a bundle)."""
    try:
        name = urlsplit(url).path.rsplit('/', 1)[-1]
    except ValueError:
        return False
    return bool(INITIALIZER.match(name)) and not NOT_INITIALIZER.search(name)


@dataclass(frozen=True)
class PageScan:
    page_url: str
    base_url: str
    title: str = ''
    headings: tuple[str, ...] = ()
    anchors: tuple[tuple[str, str], ...] = ()  # (absolute url, label)
    script_srcs: tuple[str, ...] = ()
    data_main: tuple[str, ...] = ()
    inline_scripts: tuple[str, ...] = ()
    spec_attributes: tuple[str, ...] = ()


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.base = None
        self.title_parts, self.headings, self.anchors, self.srcs, self.main, self.inline, self.attrs = [], [], [], [], [], [], []
        self._anchor = self._script = self._heading = self._title = None

    def handle_starttag(self, tag, attrs):
        a = {k: v or '' for k, v in attrs}
        if tag == 'base' and self.base is None and a.get('href'):
            self.base = a['href']
        elif tag == 'a' and a.get('href'):
            self._anchor = [a['href'], a.get('title') or a.get('aria-label') or '', []]
        elif tag == 'script':
            if a.get('src'):
                self.srcs.append(a['src'])
            if a.get('data-main'):
                self.main.append((a['data-main'], a.get('src', '')))
            self._script = None if a.get('src') else []
        elif tag == 'title':
            self._title = []
        elif tag in ('h1', 'h2', 'h3'):
            self._heading = []
        for name in SPEC_ATTRIBUTES:
            if a.get(name) and tag != 'a':
                self.attrs.append(a[name])

    def handle_data(self, data):
        if self._script is not None:
            self._script.append(data)
        elif self._anchor is not None:
            self._anchor[2].append(data)
        if self._heading is not None:
            self._heading.append(data)
        if self._title is not None:
            self._title.append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self._anchor is not None:
            self.anchors.append((self._anchor[0], ' '.join([self._anchor[1], *self._anchor[2]])))
            self._anchor = None
        elif tag == 'script' and self._script is not None:
            self.inline.append(''.join(self._script))
            self._script = None
        elif tag in ('h1', 'h2', 'h3') and self._heading is not None:
            self.headings.append(''.join(self._heading))
            self._heading = None
        elif tag == 'title' and self._title is not None:
            self.title_parts.append(''.join(self._title))
            self._title = None


def _clean(text, limit):
    text = ' '.join(''.join(' ' if ord(c) < 32 or ord(c) == 127 else c for c in text).split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _resolve(base, href):
    href = href.strip()
    if not href or href.startswith('#'):
        return None
    try:
        url = urljoin(base, href).split('#', 1)[0]
        parts = urlsplit(url)
    except ValueError:
        return None
    return url if parts.scheme.lower() in ('http', 'https') and parts.hostname else None


def scan_page(html: str, page_url: str) -> PageScan:
    """Structure of one HTML page for navigation. Pure; never raises; bounded."""
    parser = _Page()
    try:
        parser.feed(html)
        parser.close()
    except (ValueError, AssertionError, RecursionError):
        return PageScan(page_url, page_url)
    base = urljoin(page_url, parser.base) if parser.base else page_url
    anchors = []
    for href, label in parser.anchors[:MAX_ANCHORS]:
        url = _resolve(base, href)
        if url:
            anchors.append((url, _clean(label, 160)))
    srcs = tuple(dict.fromkeys(u for u in (_resolve(base, s) for s in parser.srcs) if u))
    main = []
    for data_main, src in parser.main:
        # RequireJS: data-main names the entry script; its directory is the baseUrl for module ids.
        url = _resolve(base, data_main)
        if url and not re.search(r'\.\w+$', urlsplit(url).path.rsplit('/', 1)[-1]):
            url += '.js'
        if url:
            main.append(url)
    return PageScan(
        page_url, base, _clean(''.join(parser.title_parts), HEADING_CHARS),
        tuple(_clean(h, HEADING_CHARS) for h in parser.headings[:MAX_HEADINGS] if h.strip()), tuple(anchors), srcs,
        tuple(dict.fromkeys(main)),
        tuple(s[:MAX_INLINE_SCRIPT_CHARS] for s in parser.inline[:MAX_INLINE_SCRIPTS] if s.strip()),
        tuple(dict.fromkeys(u for u in (_resolve(base, a) for a in parser.attrs) if u)))
