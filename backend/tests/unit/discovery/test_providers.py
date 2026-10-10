import json
from unittest.mock import patch
from datetime import datetime, timezone

import pytest

from radar.discovery.fetch import FetchResult, FetchFailure
from radar.discovery.limits import DiscoveryBudget, FetchLimits
from radar.discovery.providers import load_registry, parse_registry, search_provider_mappings
from radar.domain.discovery import DiscoveryRequest


def entry(**changes):
    return dict(id='payments', hosts=['api.example.com'], spec_url='https://docs.example.com/payments.yaml',
                provenance_url='https://docs.example.com/payments', **changes)


def save(tmp_path, entries):
    path = tmp_path / 'providers.json'
    path.write_text(json.dumps({'schema_version': 1, 'providers': entries}))
    return path


# Each bundled entry was checked against the provider's own repository or
# documentation (see docs/component-interfaces.md). Add an entry only after the
# same check, and extend this table with its verified facts.
GITHUB_SPECS = ('https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/'
                'api.github.com/api.github.com.')
GITHUB_PROVENANCE = 'https://github.com/github/rest-api-description/tree/main/descriptions/api.github.com'
VERIFIED_PROVIDERS = {
    'stripe': (('api.stripe.com',), None,
               'https://raw.githubusercontent.com/stripe/openapi/master/latest/openapi.spec3.json',
               'https://github.com/stripe/openapi/tree/master/latest', 8 * 1024 * 1024),
    # One entry per documented API version: all share info.version 1.1.4, so the
    # file name is the only version identity and a bare host stays ambiguous.
    'github-rest-2022-11-28': (('api.github.com',), '2022-11-28', GITHUB_SPECS + '2022-11-28.json',
                               GITHUB_PROVENANCE, 16 * 1024 * 1024),
    'github-rest-2026-03-10': (('api.github.com',), '2026-03-10', GITHUB_SPECS + '2026-03-10.json',
                               GITHUB_PROVENANCE, 16 * 1024 * 1024),
}


def test_bundled_registry_contains_only_verified_providers():
    registry = load_registry()
    assert {m.id: (m.hosts, m.api_version, m.spec_url, m.provenance_url, m.max_document_bytes)
            for m in registry} == VERIFIED_PROVIDERS


def test_bundled_stripe_mapping_selects_only_its_exact_host():
    for host, expected in (('api.stripe.com', 1), ('API.STRIPE.COM', 1), ('stripe.com', 0), ('evil-api.stripe.com', 0),
                           ('api.stripe.com.evil.test', 0), ('api.github.com', 2), ('github.com', 0)):
        with patch('radar.discovery.providers.fetch_candidates') as fetch:
            fetch.return_value = None
            result = search_provider_mappings(DiscoveryRequest(host), DiscoveryBudget())
        assert len(result.mappings) == expected, host


@pytest.mark.parametrize('changes', [
    {'id': ''}, {'hosts': []}, {'hosts': ['*.example.com']}, {'hosts': ['example.com:80']},
    {'hosts': ['https://example.com']}, {'hosts': ['example.com/path']}, {'hosts': ['bad host']},
    {'spec_url': '/openapi.json'}, {'spec_url': 'ftp://example.com/spec'},
    {'provenance_url': 'https://user:password@example.com'}, {'api_version': ''}, {'product': 3}, {'extra': 1},
])
def test_invalid_entries_rejected(changes):
    record = entry()
    record.update(changes)
    with pytest.raises(ValueError):
        parse_registry({'schema_version': 1, 'providers': [record]})


@pytest.mark.parametrize('value', [0, -1, True, 1.5, '1024', 32 * 1024 * 1024 + 1])
def test_invalid_size_allowance(value):
    with pytest.raises(ValueError):
        parse_registry({'schema_version': 1, 'providers': [entry(max_document_bytes=value)]})


def test_size_allowance_is_optional_and_bounded():
    assert parse_registry({'schema_version': 1, 'providers': [entry()]})[0].max_document_bytes is None
    allowed = parse_registry({'schema_version': 1, 'providers': [entry(max_document_bytes=32 * 1024 * 1024)]})
    assert allowed[0].max_document_bytes == 32 * 1024 * 1024


def test_allowance_applies_only_to_its_own_spec_url(tmp_path):
    path = save(tmp_path, [entry(max_document_bytes=4096), dict(entry(), id='other', spec_url='https://docs.example.com/other.yaml')])
    calls = {}
    def fetch(url, shared, **kwargs):
        calls[url] = kwargs
        shared.claim_request()
        return FetchResult(url, url, 200, 'text/html', b'<html>', datetime.now(timezone.utc), ())
    with patch('radar.discovery.candidates.fetch_document', side_effect=fetch):
        search_provider_mappings(DiscoveryRequest('api.example.com'), DiscoveryBudget(), registry_path=path)
    assert calls['https://docs.example.com/payments.yaml']['document_byte_limit'] == 4096
    assert 'document_byte_limit' not in calls['https://docs.example.com/other.yaml']


def test_largest_allowance_wins_when_entries_share_a_url(tmp_path):
    path = save(tmp_path, [entry(max_document_bytes=4096), dict(entry(), id='again', max_document_bytes=8192)])
    seen = []
    def fetch(url, shared, **kwargs):
        seen.append(kwargs['document_byte_limit'])
        shared.claim_request()
        return FetchResult(url, url, 200, 'text/html', b'<html>', datetime.now(timezone.utc), ())
    with patch('radar.discovery.candidates.fetch_document', side_effect=fetch):
        search_provider_mappings(DiscoveryRequest('api.example.com'), DiscoveryBudget(), registry_path=path)
    assert seen == [8192]


@pytest.mark.parametrize('document', [{}, {'schema_version': True, 'providers': []},
    {'schema_version': 2, 'providers': []}, {'schema_version': 1, 'providers': {}},
    {'schema_version': 1, 'providers': [entry(), entry()]}])
def test_invalid_registry(document):
    with pytest.raises(ValueError):
        parse_registry(document)


def test_file_size_limit(tmp_path):
    path = tmp_path / 'large.json'
    path.write_bytes(b' ' * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match='exceeds'):
        load_registry(path)


@pytest.mark.parametrize('host', ['unknown.example.com', 'api.example.com.attacker.test', 'sub.api.example.com'])
def test_unknown_and_lookalike_hosts_do_not_fetch(tmp_path, host):
    path = save(tmp_path, [entry()])
    with patch('radar.discovery.candidates.fetch_document') as fetch:
        result = search_provider_mappings(DiscoveryRequest(host), DiscoveryBudget(), registry_path=path)
    assert result.mappings == () and result.search.candidates == ()
    fetch.assert_not_called()


def test_dedup_preserves_both_mapping_records(tmp_path):
    first = entry(product='payments', api_version='v1')
    second = dict(first, id='another-product', product='other', api_version='v2')
    path = save(tmp_path, [first, second])
    budget = DiscoveryBudget()
    def fetch(url, shared, **kwargs):
        assert shared is budget
        shared.claim_request()
        return FetchResult(url, 'https://cdn.example.com/spec', 200, 'text/html', b'<html>',
                           datetime.now(timezone.utc), ())
    with patch('radar.discovery.candidates.fetch_document', side_effect=fetch) as mocked:
        result = search_provider_mappings(DiscoveryRequest('API.EXAMPLE.COM.', product='payments', api_version='v1'), budget, registry_path=path)
    assert mocked.call_count == 1
    assert len(result.mappings) == len(result.search.candidates) == 2
    assert len(result.search.fetches) == 1
    assert result.search.candidates[0].retrieval is result.search.candidates[1].retrieval
    for mapping, candidate in zip(result.mappings, result.search.candidates):
        assert mapping.id in candidate.candidate.evidence[0].description
        assert candidate.candidate.source_url == 'https://cdn.example.com/spec'
        assert candidate.candidate.discovery_source == first['provenance_url']


def test_failure_retained_and_shared_budget_used(tmp_path):
    path = save(tmp_path, [entry(), dict(entry(), id='other', spec_url='https://docs.example.com/other.yaml')])
    budget = DiscoveryBudget(FetchLimits(max_requests=2))
    budget.claim_request()
    def fetch(url, shared, **kwargs):
        assert kwargs['allow_loopback'] is True
        shared.claim_request()
        return FetchResult(url, url, 403, None, None, None, (), FetchFailure('http_error', 'HTTP status 403.'))
    with patch('radar.discovery.candidates.fetch_document', side_effect=fetch):
        result = search_provider_mappings(DiscoveryRequest('api.example.com'), budget, registry_path=path, allow_loopback=True)
    assert result.search.fetches[0].failure.reason == 'HTTP status 403.'
    assert result.search.stop_reason == 'request_limit'
    assert result.search.skipped_urls == ('https://docs.example.com/other.yaml',)
    assert len(result.mappings) == 2 and result.search.candidates == ()


def test_malformed_registry_fails_before_fetch(tmp_path):
    path = save(tmp_path, [entry(), dict(entry(), id='invalid', hosts=['*'])])
    with patch('radar.discovery.candidates.fetch_document') as fetch:
        with pytest.raises(ValueError):
            search_provider_mappings(DiscoveryRequest('api.example.com'), DiscoveryBudget(), registry_path=path)
    fetch.assert_not_called()
