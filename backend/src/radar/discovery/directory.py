"""Look up the target's provider in the public APIs.guru directory (https://apis.guru).

The directory is a lead generator, nothing more. It lists, per provider, the places each OpenAPI description is
published; this module turns those places into candidates. Its entries are never trusted: a copy kept by the
directory may be years old, and an entry may describe a different API of the same provider. Every candidate is
fetched, validated and matched to the target exactly like one found any other way, and a contract reached this way
never has positive provenance, because a directory is a third party.

The provider's own source URL (`x-origin`) is preferred over the directory's copy, so the contract examined is the
one the provider currently publishes. The copy is used only when an entry names no usable origin.
"""

from dataclasses import dataclass
import json
import re

from radar.discovery.candidates import CandidateSearchResult, fetch_candidates
from radar.discovery.input import normalize_target
from radar.discovery.limits import DiscoveryBudget
from radar.discovery.suffix import is_icann_suffix, registrable_domain
from radar.domain.discovery import ContractCandidate, DiscoveryRequest, MatchingEvidence

DIRECTORY_BASE = 'https://api.apis.guru/v2'
MAX_KEYS = 3  # provider keys looked up: the host and up to two parent domains
MAX_ENTRIES = 8  # candidates taken from one provider's listing
MAX_INDEX_BYTES = 4 * 1024 * 1024
@dataclass(frozen=True)
class DirectoryStep:
    search: CandidateSearchResult  # the contracts fetched in this step
    index_fetches: tuple  # the directory's own listing requests
    listed: bool  # the directory has a listing for this provider
    has_secondary: bool  # the listing has entries beyond the primary one


LIMITATIONS = (
    'The public APIs.guru directory is a third-party index and can be out of date; it is a discovery hint only.',
    'OpenAPI validity and relevance to the target have not been assessed.',
)
_LABEL = re.compile(r'[a-z0-9-]+')


def provider_keys(hostname: str) -> tuple[str, ...]:
    """Directory provider names to try for a host: `api.twilio.com` -> `api.twilio.com`, `twilio.com`.

    The walk stops at the registrable domain, so `api.acme.co.uk` never looks up `co.uk`.
    A host that is itself a private suffix (`github.io`) is looked up as itself only."""
    host = (hostname or '').rstrip('.').lower()
    labels = host.split('.')
    if len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels) or labels[-1].isdigit() or is_icann_suffix(host):
        return ()
    registrable = registrable_domain(host) or host  # a private suffix such as httpbin.org is still a provider itself
    depth = len(labels) - len(registrable.split('.'))  # labels above the registrable domain; never a bare suffix
    return tuple('.'.join(labels[i:]) for i in range(0, depth + 1))[:MAX_KEYS]


def _quote(url):
    return url.replace(' ', '%20')


def _entries(document, key):
    """(entry id, title, version, origin url or None, copy url or None) per listed API, best first."""
    apis = document.get('apis') if isinstance(document, dict) else None
    if not isinstance(apis, dict):
        return []
    found = []
    for entry_id, entry in apis.items():
        if not isinstance(entry, dict) or not isinstance(entry.get('info'), dict):
            continue
        info = entry['info']
        origin = None
        for source in info.get('x-origin') if isinstance(info.get('x-origin'), list) else []:
            if (isinstance(source, dict) and isinstance(source.get('url'), str) and source.get('format') == 'openapi'
                    and str(source.get('version', '')).startswith('3') and source['url'].startswith(('http://', 'https://'))):
                origin = _quote(source['url'])
                break
        copy = entry.get('swaggerUrl')
        copy = _quote(copy) if isinstance(copy, str) and str(entry.get('openapiVer', '')).startswith('3') else None
        if origin or copy:
            found.append((str(entry_id), str(info.get('title', ''))[:80], str(info.get('version', ''))[:40], origin, copy))
    # the provider's own primary entry (no `:service` suffix) first, then the rest in a stable order
    return sorted(found, key=lambda e: (e[0] != key, e[0]))[:MAX_ENTRIES]


def search_directory(request: DiscoveryRequest, budget: DiscoveryBudget, *, allow_loopback=False, cache=None,
                     base_url: str = DIRECTORY_BASE, copies: bool = False, scope: str = 'primary') -> DirectoryStep:
    """One step of the directory search.

    A provider's listing has a primary entry (named exactly like the provider) and possibly secondary ones
    (`provider:service`). The steps, in order, are: `scope='primary'` with the provider's own sources, then
    with `copies=True` the directory's copies of the primary entry (for a source that has moved or is in a format the
    validator does not take), then `scope='secondary'` for the other entries, again sources then copies. A listing
    with no primary entry treats all its entries as primary. The caller stops at the first step that yields a
    contract, so a secondary entry (a plugin manifest, one sub-service) cannot stand in for the provider's main API.
    The listing is cached: later steps cost no new lookup.
    """
    if scope not in ('primary', 'secondary'):
        raise ValueError('Invalid scope.')
    target = normalize_target(request)
    cache = {} if cache is None else cache
    index_fetches, locations, listed, secondary = [], [], False, False
    for key in provider_keys(target.hostname):
        index_url = f'{base_url}/{key}.json'
        placeholder = ContractCandidate(index_url, 'directory_index', index_url)
        listing = fetch_candidates(target, [placeholder], budget, allow_loopback=allow_loopback, cache=cache,
                                   document_limits={index_url: MAX_INDEX_BYTES})
        index_fetches.extend(listing.fetches)
        if listing.stop_reason or not listing.candidates:
            if listing.stop_reason:
                break
            continue
        try:
            document = json.loads(listing.candidates[0].retrieval.content.decode('utf-8-sig'))
        except (ValueError, UnicodeError, RecursionError):
            continue
        entries = _entries(document, key)
        has_primary = any(e[0] == key for e in entries)
        chosen = [e for e in entries if (e[0] == key or not has_primary) == (scope == 'primary')]
        listed, secondary = bool(entries), has_primary and len(entries) > 1
        for entry_id, title, version, origin, copy in chosen:
            url, is_origin = ((copy, False) if origin and copy else (None, False)) if copies else \
                             ((origin, True) if origin else (copy, False))
            if not url:
                continue
            where = ("the provider's own published source" if is_origin
                     else "the directory's own copy, which may be older than the provider's current contract")
            locations.append(ContractCandidate(
                source_url=url, discovery_method='directory', discovery_source=index_url,
                evidence=(MatchingEvidence(
                    'directory',
                    f'The APIs.guru directory lists {entry_id!r} ({title!r}, version {version!r}) for provider {key}, '
                    f'at {where}. This is a hint: the entry has not been verified against the document.', index_url),),
                limitations=LIMITATIONS, source_kind='provider' if is_origin else 'directory_copy'))
        if entries:
            break  # the most specific provider key that has a listing is the one used
    search = fetch_candidates(target, locations, budget, allow_loopback=allow_loopback, limitations=LIMITATIONS,
                              cache=cache)
    return DirectoryStep(search, tuple(index_fetches), listed, secondary)
