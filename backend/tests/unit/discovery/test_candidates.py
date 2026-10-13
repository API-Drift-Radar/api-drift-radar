from datetime import datetime, timezone
import time
from unittest.mock import patch

import pytest

from radar.discovery.candidates import common_candidate_urls, search_common_locations
from radar.discovery.fetch import FetchAttempt, FetchFailure, FetchResult
from radar.discovery.input import DiscoveryInputError, normalize_target
from radar.discovery.limits import DiscoveryBudget, FetchLimits
from radar.domain.discovery import DiscoveryRequest


@pytest.mark.parametrize('target,origin', [
    ('api.example.com', 'https://api.example.com'),
    ('https://API.example.com/v1/customers?token=test', 'https://api.example.com'),
    ('http://localhost:8765/v1/customers', 'http://localhost:8765'),
    ('http://[::1]:8765/v1/customers', 'http://[::1]:8765'),
])
def test_origin_locations(target, origin):
    result = common_candidate_urls(normalize_target(DiscoveryRequest(target)))
    assert result == tuple(origin + path for path in ('/openapi.json', '/openapi.yaml', '/swagger.json'))


def test_duplicate_locations_removed():
    with patch('radar.discovery.candidates.COMMON_SPEC_PATHS', ('/openapi.json', '/openapi.json', '/openapi.yaml')):
        urls = common_candidate_urls(normalize_target(DiscoveryRequest('example.com')))
    assert len(urls) == 2


def response(url, status=200, body=b'{}', failure=None, final=None):
    return FetchResult(url, final or url, status, 'application/json', body if failure is None else None,
                       datetime.now(timezone.utc) if failure is None else None,
                       (FetchAttempt(url, status),), failure)


def test_collects_multiple_unvalidated_documents_and_keeps_context():
    budget = DiscoveryBudget()
    request = DiscoveryRequest('https://example.com/v1/customers', method='get', product='Payments')
    # A successful HTML download is deliberately NOT certified as OpenAPI.
    def fetch(url, shared, **options):
        assert shared is budget and options == {'allow_loopback': False}
        shared.claim_request()
        return response(url, body=b'<html>Not a specification</html>')
    with patch('radar.discovery.candidates.fetch_document', side_effect=fetch):
        result = search_common_locations(request, budget)
    assert len(result.candidates) == len(result.fetches) == budget.requests_used == 3
    assert result.target.method == 'GET' and result.target.product == 'Payments'
    assert result.skipped_urls == () and result.stop_reason is None
    assert all(c.candidate.limitations for c in result.candidates)


def test_redirect_provenance_and_failed_locations_retained():
    def fetch(url, budget, **options):
        budget.claim_request()
        if url.endswith('.yaml'):
            return response(url, final='https://docs.example.com/spec.yaml')
        return response(url, status=404, failure=FetchFailure('http_error', 'HTTP status 404.'))
    with patch('radar.discovery.candidates.fetch_document', side_effect=fetch):
        result = search_common_locations(DiscoveryRequest('example.com'), DiscoveryBudget())
    assert len(result.candidates) == 1 and len(result.fetches) == 3
    candidate = result.candidates[0].candidate
    assert candidate.source_url == 'https://docs.example.com/spec.yaml'
    assert candidate.discovery_source == 'https://example.com/openapi.yaml'
    assert candidate.discovery_method == 'common_location'


@pytest.mark.parametrize('code,status', [('http_error', 404), ('http_error', 403), ('timeout', None), ('blocked_destination', None)])
def test_unsuccessful_search_preserves_failure_reasons(code, status):
    with patch('radar.discovery.candidates.fetch_document', side_effect=lambda url, *a, **k: response(url, status, failure=FetchFailure(code, 'reason'))):
        result = search_common_locations(DiscoveryRequest('example.com'), DiscoveryBudget())
    assert result.candidates == ()
    assert len(result.fetches) == 3
    assert all(f.failure.code == code and f.status == status for f in result.fetches)


def test_shared_request_budget_stops_and_reports_skipped_locations():
    budget = DiscoveryBudget(FetchLimits(max_requests=2))
    budget.claim_request()  # Simulate a request consumed by a preceding strategy.
    def fetch(url, shared, **options):
        shared.claim_request()
        return response(url)
    with patch('radar.discovery.candidates.fetch_document', side_effect=fetch) as mocked:
        result = search_common_locations(DiscoveryRequest('example.com'), budget, allow_loopback=True)
    assert mocked.call_count == 1
    assert mocked.call_args.kwargs['allow_loopback'] is True
    assert len(result.skipped_urls) == 2 and result.stop_reason == 'request_limit'


@pytest.mark.parametrize('code', ['deadline_exceeded', 'request_limit', 'total_size_limit'])
def test_budget_failure_during_fetch_stops_search(code):
    with patch('radar.discovery.candidates.fetch_document', side_effect=lambda url, *a, **k: response(url, None, failure=FetchFailure(code, 'exhausted'))) as mocked:
        result = search_common_locations(DiscoveryRequest('example.com'), DiscoveryBudget())
    assert mocked.call_count == 1 and len(result.skipped_urls) == 2
    assert result.stop_reason == code and result.fetches[0].failure.code == code


@pytest.mark.parametrize('kind', ['time', 'bytes'])
def test_already_exhausted_budget_does_not_fetch(kind):
    budget = DiscoveryBudget()
    if kind == 'time':
        budget.deadline = time.monotonic() - 1
    else:
        budget.bytes_used = budget.limits.max_total_bytes
    with patch('radar.discovery.candidates.fetch_document') as mocked:
        result = search_common_locations(DiscoveryRequest('example.com'), budget)
    mocked.assert_not_called()
    assert len(result.skipped_urls) == 3 and result.stop_reason is not None


def test_invalid_input_does_not_fetch():
    with patch('radar.discovery.candidates.fetch_document') as mocked:
        with pytest.raises(DiscoveryInputError):
            search_common_locations(DiscoveryRequest('ftp://example.com'), DiscoveryBudget())
    mocked.assert_not_called()
