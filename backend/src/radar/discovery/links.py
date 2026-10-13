"""Typed links that publishers use to point at API descriptions, documentation and catalogues.

`service-desc` is a machine-readable description, `service-doc` human documentation (RFC 8631), and
`api-catalog` a catalogue of APIs (RFC 9727). They arrive in HTTP `Link` headers (RFC 8288) and in HTML
`<link>`/`<a rel>` elements. Pure parsers: nothing here fetches or trusts a link; each is only a lead.
"""

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

RELATIONS = ('service-desc', 'service-doc', 'api-catalog')
MAX_LINKS = 20
MAX_HEADER_CHARS = 8192


@dataclass(frozen=True)
class TypedLink:
    href: str  # absolute http(s) URL
    rel: str  # one of RELATIONS
    type: str | None = None
    title: str | None = None


def _absolute(base_url, href):
    if not isinstance(href, str) or not href.strip():
        return None  # an empty reference would resolve to the page itself
    try:
        url = urljoin(base_url, href.strip())
        parts = urlsplit(url)
    except ValueError:
        return None
    return url.split('#', 1)[0] if parts.scheme.lower() in ('http', 'https') and parts.hostname else None


def _split_outside(text, separator, quotes='"', brackets=('<', '>')):
    """Split on `separator` where it is not inside quotes or <...>."""
    parts, current, quoted, bracketed = [], [], False, False
    for char in text:
        if char in quotes and not bracketed:
            quoted = not quoted
        elif char == brackets[0] and not quoted:
            bracketed = True
        elif char == brackets[1] and not quoted:
            bracketed = False
        if char == separator and not quoted and not bracketed:
            parts.append(''.join(current))
            current = []
        else:
            current.append(char)
    parts.append(''.join(current))
    return parts


def parse_link_header(value: str | None, base_url: str, *, max_links: int = MAX_LINKS) -> tuple[TypedLink, ...]:
    """The links of interest in an HTTP Link header, resolved against the response URL. Never raises."""
    if not value or not isinstance(value, str):
        return ()
    links = []
    for member in _split_outside(value[:MAX_HEADER_CHARS], ','):
        member = member.strip()
        if not member.startswith('<') or '>' not in member:
            continue
        target, _, rest = member[1:].partition('>')
        params = {}
        for item in _split_outside(rest, ';')[1:]:
            name, _, raw = item.strip().partition('=')
            if name:
                params.setdefault(name.strip().lower(), raw.strip().strip('"'))
        url = _absolute(base_url, target)
        if not url:
            continue
        for rel in params.get('rel', '').lower().split():
            if rel in RELATIONS and len(links) < max_links:
                links.append(TypedLink(url, rel, params.get('type') or None, params.get('title') or None))
    return tuple(dict.fromkeys(links))


class _LinkCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found = []
        self.base = None

    def handle_starttag(self, tag, attrs):
        attrs = {name: value or '' for name, value in attrs}
        if tag == 'base' and self.base is None and attrs.get('href'):
            self.base = attrs['href']
        elif tag in ('link', 'a') and attrs.get('href') and attrs.get('rel'):
            self.found.append((attrs['href'], attrs['rel'], attrs.get('type'), attrs.get('title')))


def html_typed_links(html: str, base_url: str, *, max_links: int = MAX_LINKS) -> tuple[TypedLink, ...]:
    """`<link rel>` and `<a rel>` elements naming a relation of interest. Never raises."""
    collector = _LinkCollector()
    try:
        collector.feed(html)
        collector.close()
    except (ValueError, AssertionError, RecursionError):
        return ()
    base = urljoin(base_url, collector.base) if collector.base else base_url
    links = []
    for href, rel, kind, title in collector.found:
        url = _absolute(base, href)
        for relation in rel.lower().split():
            if url and relation in RELATIONS and len(links) < max_links:
                links.append(TypedLink(url, relation, kind or None, title or None))
    return tuple(dict.fromkeys(links))
