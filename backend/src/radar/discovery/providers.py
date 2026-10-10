"""Explicit provider mappings; registry hints never establish contract validity."""

from dataclasses import dataclass
from importlib.resources import files
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from radar.discovery.candidates import CandidateSearchResult, fetch_candidates
from radar.discovery.input import normalize_target
from radar.discovery.limits import DiscoveryBudget
from radar.domain.discovery import ContractCandidate, DiscoveryRequest, MatchingEvidence


MAX_REGISTRY_BYTES = 1024 * 1024
MAX_MAPPINGS = 100
PROVIDER_LIMITATIONS = (
    "Only explicit provider mappings were searched; an unknown host is not proof of no contract.",
    "Mapping metadata is a discovery hint, not proof of product, version, or contract relevance.",
    "OpenAPI validation, required reference capture, and relevance matching have not been performed.",
)


@dataclass(frozen=True)
class ProviderMapping:
    id: str
    hosts: tuple[str, ...]
    spec_url: str
    provenance_url: str
    product: str | None = None
    api_version: str | None = None


@dataclass(frozen=True)
class ProviderSearchResult:
    mappings: tuple[ProviderMapping, ...]
    search: CandidateSearchResult


def _absolute_url(value):
    if not isinstance(value, str) or urlsplit(value).scheme.lower() not in ('http', 'https'):
        raise ValueError('Mapping URLs must be absolute HTTP(S) URLs.')
    return normalize_target(DiscoveryRequest(value)).normalized_url


def parse_registry(document) -> tuple[ProviderMapping, ...]:
    """Validate the entire registry before any network requests are possible."""
    if not isinstance(document, dict) or set(document) != {'schema_version', 'providers'}:
        raise ValueError('Registry requires schema_version and providers only.')
    if type(document['schema_version']) is not int or document['schema_version'] != 1:
        raise ValueError('Unsupported provider registry schema version.')
    entries = document['providers']
    if not isinstance(entries, list) or len(entries) > MAX_MAPPINGS:
        raise ValueError('providers must be a list of at most 100 mappings.')
    mappings = []
    ids = set()
    required = {'id', 'hosts', 'spec_url', 'provenance_url'}
    for entry in entries:
        if not isinstance(entry, dict) or not required <= set(entry) or set(entry) - required - {'product', 'api_version'}:
            raise ValueError('Invalid provider mapping fields.')
        identifier = entry['id']
        if not isinstance(identifier, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}', identifier) or identifier in ids:
            raise ValueError('Mapping IDs must be unique lowercase identifiers.')
        ids.add(identifier)
        raw_hosts = entry['hosts']
        if not isinstance(raw_hosts, list) or not 1 <= len(raw_hosts) <= 20:
            raise ValueError('Each mapping requires 1 to 20 exact hostnames.')
        hosts = []
        for host in raw_hosts:
            if not isinstance(host, str) or not host or host != host.strip() or any(c in host for c in '/:?#[ ]@*'):
                raise ValueError('Mapping hosts must be plain exact hostnames, without ports or wildcards.')
            normalized = normalize_target(DiscoveryRequest(host)).hostname.rstrip('.')
            if normalized not in hosts:
                hosts.append(normalized)
        optional = {}
        for name in ('product', 'api_version'):
            value = entry.get(name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f'{name} must be a nonempty string or null.')
            optional[name] = value.strip() if value is not None else None
        mappings.append(ProviderMapping(identifier, tuple(hosts), _absolute_url(entry['spec_url']),
                                        _absolute_url(entry['provenance_url']), **optional))
    return tuple(mappings)


def load_registry(path=None) -> tuple[ProviderMapping, ...]:
    """Load the bundled registry or an explicit local JSON registry for tests."""
    resource = Path(path) if path is not None else files('radar.discovery').joinpath('data/providers.json')
    with resource.open('rb') as stream:
        content = stream.read(MAX_REGISTRY_BYTES + 1)
    if len(content) > MAX_REGISTRY_BYTES:
        raise ValueError('Provider registry exceeds 1 MiB.')
    return parse_registry(json.loads(content))


def search_provider_mappings(
    request: DiscoveryRequest,
    budget: DiscoveryBudget,
    *,
    registry_path=None,
    allow_loopback=False,
) -> ProviderSearchResult:
    """Keep all exact-host matches, without choosing products or latest versions.

    Registry paths are caller-controlled local configuration, never fetched URLs.
    An empty match performs no requests. Other strategies are invoked separately.
    """
    target = normalize_target(request)
    mappings = tuple(mapping for mapping in load_registry(registry_path)
                     if target.hostname.rstrip('.') in mapping.hosts)
    locations = tuple(ContractCandidate(
        source_url=mapping.spec_url,
        discovery_method='provider_mapping',
        discovery_source=mapping.provenance_url,
        evidence=(MatchingEvidence(
            criterion='provider_mapping',
            description=f'Registry entry {mapping.id} explicitly lists host {target.hostname}. '
                        f'Product hint: {mapping.product!r}; API version hint: {mapping.api_version!r}. '
                        'These hints have not been verified against the document.',
            source_url=mapping.provenance_url,
        ),),
        limitations=PROVIDER_LIMITATIONS,
    ) for mapping in mappings)
    search = fetch_candidates(target, locations, budget, allow_loopback=allow_loopback,
                              limitations=PROVIDER_LIMITATIONS)
    return ProviderSearchResult(mappings, search)
