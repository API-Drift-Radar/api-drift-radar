"""Reader for RFC 9727 API catalogues: a JSON Linkset (RFC 9264) at /.well-known/api-catalog.

Each linkset context may name, for an API (its `anchor`), a machine-readable description (`service-desc`),
documentation (`service-doc`), metadata (`service-meta`) and further catalogues or APIs (`item`, `api-catalog`).
Pure and bounded; every href is only a lead for the navigator to consider, never proof of anything.
"""

from dataclasses import dataclass
import json
from urllib.parse import urljoin, urlsplit

MAX_CONTEXTS = 200
MAX_LINKS_PER_RELATION = 20
RELATION_FIELDS = ('service-desc', 'service-doc', 'service-meta', 'item', 'api-catalog', 'describedby')


@dataclass(frozen=True)
class CatalogLink:
    href: str
    type: str | None = None
    title: str | None = None


@dataclass(frozen=True)
class CatalogEntry:
    anchor: str | None
    descriptions: tuple[CatalogLink, ...] = ()
    documentation: tuple[CatalogLink, ...] = ()
    metadata: tuple[CatalogLink, ...] = ()
    items: tuple[CatalogLink, ...] = ()  # nested catalogues and further APIs


@dataclass(frozen=True)
class Catalog:
    url: str
    entries: tuple[CatalogEntry, ...] = ()
    skipped: int = 0  # contexts or links beyond the bounds, or malformed


def _resolve(base, value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        url = urljoin(base, value.strip())
        parts = urlsplit(url)
    except ValueError:
        return None
    return url.split('#', 1)[0] if parts.scheme.lower() in ('http', 'https') and parts.hostname else None


def _links(base, raw):
    links, skipped = [], 0
    for entry in raw[:MAX_LINKS_PER_RELATION] if isinstance(raw, list) else []:
        href = _resolve(base, entry.get('href')) if isinstance(entry, dict) else None
        if href:
            links.append(CatalogLink(href, entry.get('type') if isinstance(entry.get('type'), str) else None,
                                     entry.get('title') if isinstance(entry.get('title'), str) else None))
        else:
            skipped += 1
    return tuple(links), skipped + (max(0, len(raw) - MAX_LINKS_PER_RELATION) if isinstance(raw, list) else 0)


def parse_linkset(content: bytes, catalog_url: str) -> Catalog | None:
    """The catalogue in `content`, or None if it is not a JSON Linkset. Never raises."""
    try:
        data = json.loads(content.decode('utf-8-sig'))
    except (ValueError, UnicodeError, RecursionError):
        return None
    contexts = data.get('linkset') if isinstance(data, dict) else None
    if not isinstance(contexts, list):
        return None
    entries, skipped = [], max(0, len(contexts) - MAX_CONTEXTS)
    for context in contexts[:MAX_CONTEXTS]:
        if not isinstance(context, dict):
            skipped += 1
            continue
        anchor = _resolve(catalog_url, context.get('anchor'))
        found = {}
        for field in RELATION_FIELDS:
            found[field], more = _links(catalog_url, context.get(field))
            skipped += more
        entries.append(CatalogEntry(
            anchor, found['service-desc'] + found['describedby'], found['service-doc'], found['service-meta'],
            found['item'] + found['api-catalog']))
    return Catalog(catalog_url, tuple(entries), skipped)
