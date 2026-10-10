from datetime import datetime, timezone
import json
from unittest.mock import patch

import pytest

from radar.discovery.candidates import looks_like_spec_url, search_direct_url
from radar.discovery.fetch import FetchAttempt, FetchFailure, FetchResult
from radar.discovery.input import normalize_target
from radar.discovery.limits import DiscoveryBudget
from radar.discovery.matching import INDETERMINATE, MATCH, MISMATCH, NOT_REQUESTED, MatchContext, assess_match
from radar.discovery.orchestrator import discover
from radar.discovery.validation import validate_document
from radar.domain.discovery import DiscoveryRequest, DiscoveryStatus

V, R, I, N = (DiscoveryStatus.VALIDATED, DiscoveryStatus.REJECTED, DiscoveryStatus.INACCESSIBLE, DiscoveryStatus.NOT_FOUND)
CDN = 'https://cdn.example.net'
SPEC = CDN + '/specs/pets.json'
API = 'https://api.acme.com'


def contract(server=API + '/v1', title='Acme Pets API', **extra):
    return {'openapi': '3.0.3', 'info': {'title': title, 'version': '1.0.0'}, 'servers': [{'url': server}],
            'paths': {'/pets': {'get': {}, 'post': {}}}, **extra}


def target(url, **hints):
    return normalize_target(DiscoveryRequest(url, **hints))


# --- recognising a specification URL ------------------------------------------

@pytest.mark.parametrize('url,expected', [
    ('https://x.test/openapi.json', True), ('https://x.test/a/b/spec.YAML', True), ('https://x.test/spec.yml?v=2', True),
    ('http://127.0.0.1:8080/s.json', True), ('https://x.test/', False), ('https://x.test', False),
    ('https://x.test/openapi', False), ('https://x.test/docs', False), ('https://x.test/spec.jsonp', False),
    ('https://x.test/spec.json/extra', False), ('https://x.test/v1/pets?format=.json', False),
])
def test_only_paths_ending_in_a_spec_extension_are_direct_urls(url, expected):
    assert looks_like_spec_url(target(url)) is expected


# --- the search ----------------------------------------------------------------

def result_for(url, content=b'{}'):
    return FetchResult(url, url, 200, 'application/json', content, datetime(2026, 10, 9, tzinfo=timezone.utc),
                       (FetchAttempt(url, 200),))


def test_a_non_spec_target_fetches_nothing():
    with patch('radar.discovery.candidates.fetch_document') as fetch:
        result = search_direct_url(DiscoveryRequest('https://api.acme.com/v1/pets'), DiscoveryBudget())
    assert result.candidates == () and result.fetches == () and not fetch.called


def test_a_spec_url_is_fetched_exactly_once_including_its_query():
    seen = []

    def fetch(url, budget, **options):
        seen.append(url)
        budget.claim_request()
        return result_for(url)

    cache = {}
    with patch('radar.discovery.candidates.fetch_document', side_effect=fetch):
        first = search_direct_url(DiscoveryRequest(SPEC + '?rev=7'), DiscoveryBudget(), cache=cache)
        second = search_direct_url(DiscoveryRequest(SPEC + '?rev=7'), DiscoveryBudget(), cache=cache)
    assert seen == [SPEC + '?rev=7']  # the second call is served from the shared cache
    (retrieved,) = first.candidates
    assert (retrieved.candidate.discovery_method, retrieved.candidate.discovery_source) == ('direct_url', SPEC + '?rev=7')
    assert second.candidates[0].retrieval is retrieved.retrieval


# --- matching a directly requested contract --------------------------------------

def match(document, url=SPEC, **hints):
    validation = validate_document(json.dumps(document).encode())
    assert validation.ok
    context = MatchContext(target(url, **hints), url, 'direct_url', url)
    return assess_match(context, validation), {c.criterion: c.outcome for c in
                                               assess_match(context, validation).checks}


def test_servers_on_other_hosts_are_not_held_against_a_direct_url():
    result, outcomes = match(contract())
    assert result.accepted and outcomes['server_host'] == INDETERMINATE
    assert 'requested directly' in result.checks[0].description and 'api.acme.com' in result.checks[0].description
    assert outcomes['provenance'] == MATCH and 'supplied this exact contract URL' in result.checks[4].description
    assert result.positive == ('provenance',)


def test_a_related_server_host_is_still_positive_evidence():
    _, outcomes = match(contract(server='https://cdn.example.net/v1'))
    assert outcomes['server_host'] == MATCH


def test_the_url_path_is_the_document_not_an_endpoint():
    result, outcomes = match(contract(), method='GET')
    assert result.accepted and outcomes['operation'] == INDETERMINATE
    assert 'without an endpoint path' in result.checks[1].description
    assert match(contract())[1]['operation'] == NOT_REQUESTED


def test_hints_still_reject_a_direct_contract():
    version, _ = match(contract(server=API + '/v1'), api_version='v2')
    assert not version.accepted and version.rejection.code == 'version_mismatch'
    product, _ = match(contract(), product='shipping')
    assert not product.accepted and product.rejection.code == 'product_mismatch'
    assert match(contract(server=API + '/v2'), api_version='v2')[0].accepted


# --- the orchestration policy -------------------------------------------------------

class Network:
    def __init__(self, files):
        self.files, self.calls = files, []

    def fetch(self, url, budget, **options):
        self.calls.append(url)
        budget.claim_request()
        value = self.files.get(url, ('status', 404))
        if isinstance(value, tuple) and value[0] == 'fail':
            return FetchResult(url, url, None, None, None, None, (), FetchFailure(value[1], 'test failure'))
        if isinstance(value, tuple) and value[0] == 'status':
            return FetchResult(url, url, value[1], None, None, None, (FetchAttempt(url, value[1]),),
                               FetchFailure('http_error', f'HTTP status {value[1]}.'))
        content = value if isinstance(value, bytes) else json.dumps(value).encode()
        return result_for(url, content)


def run(files, url, **hints):
    network = Network(files)
    with patch('radar.discovery.candidates.fetch_document', side_effect=network.fetch), \
            patch('radar.discovery.capture.fetch_document', side_effect=network.fetch):
        return discover(DiscoveryRequest(url, **hints)), network


def test_a_valid_direct_contract_is_the_answer_and_nothing_else_is_searched():
    other = contract(title='Another API', paths={'/x': {'get': {}}})
    outcome, network = run({SPEC: contract(), CDN + '/openapi.json': other}, SPEC)
    assert outcome.status is V and outcome.packages == ()
    assert outcome.package.candidate.discovery_method == 'direct_url' and outcome.package.candidate.source_url == SPEC
    assert network.calls == [SPEC]
    assert any('requested directly' in n and 'no other locations' in n for n in outcome.limitations)
    assert [a.stage for a in outcome.attempts] == ['direct_url']


def test_its_references_are_captured_and_an_incomplete_one_is_rejected():
    root = contract(paths={'/pets': {'get': {'x': {'$ref': 'defs.json#/A'}}}})
    ok, network = run({SPEC: root, CDN + '/specs/defs.json': {'A': {'type': 'object'}}}, SPEC)
    assert ok.status is V and len(ok.package.referenced_documents) == 1
    broken, _ = run({SPEC: root}, SPEC)
    assert broken.status is R and broken.candidates[0].rejection_reasons[0].startswith('reference_capture:')


def test_an_endpoint_that_merely_ends_in_json_falls_back_to_normal_discovery():
    files = {API + '/users.json': {'users': [1, 2]}, API + '/openapi.json': contract()}
    outcome, network = run(files, API + '/users.json')
    assert outcome.status is V and outcome.package.candidate.discovery_method == 'common_location'
    miss = [a for a in outcome.attempts if a.outcome == 'not_a_contract']
    assert [a.stage for a in miss] == ['direct_url'] and miss[0].reason.startswith('not_openapi:')
    assert network.calls[0] == API + '/users.json' and API + '/openapi.json' in network.calls


def test_a_missing_direct_url_falls_back_and_is_not_found_if_nothing_else_exists():
    outcome, network = run({}, API + '/gone.yaml')
    assert outcome.status is N and network.calls[0] == API + '/gone.yaml' and len(network.calls) > 1


def test_an_unreachable_direct_url_is_reported_as_inaccessible_after_the_fallbacks():
    outcome, _ = run({API + '/spec.json': ('status', 503)}, API + '/spec.json')
    assert outcome.status is I and outcome.attempts[0].stage == 'direct_url' and outcome.attempts[0].outcome == 'inaccessible'


def test_a_rejected_direct_contract_stays_in_the_candidates_and_the_search_continues():
    swagger2 = {'swagger': '2.0', 'info': {'title': 't', 'version': '1'}, 'paths': {}}
    outcome, network = run({SPEC: swagger2}, SPEC)
    assert outcome.status is R
    assert outcome.candidates[0].source_url == SPEC and outcome.candidates[0].rejection_reasons[0].startswith('validation:unsupported_version')
    assert len(network.calls) > 1  # the normal strategies still ran


def test_a_direct_contract_that_contradicts_a_hint_is_rejected_then_the_search_continues():
    files = {SPEC: contract(server=API + '/v1'), CDN + '/openapi.json': contract(server=CDN + '/v2')}
    outcome, network = run(files, SPEC, api_version='v2')
    assert outcome.status is V and outcome.package.candidate.source_url == CDN + '/openapi.json'
    assert any('version_mismatch' in c.rejection_reasons[0] for c in outcome.candidates if c.rejection_reasons)


def test_a_bare_host_does_no_direct_fetch():
    outcome, network = run({}, 'https://api.acme.com')
    assert 'direct_url' not in {a.stage for a in outcome.attempts} and API + '/' not in network.calls


def test_direct_discovery_is_deterministic():
    one, _ = run({SPEC: contract()}, SPEC)
    two, _ = run({SPEC: contract()}, SPEC)
    assert one == two


def test_a_document_refused_for_its_size_says_how_to_raise_the_limit():
    outcome, _ = run({SPEC: ('fail', 'document_size_limit')}, SPEC)
    assert outcome.status is I
    note = next(n for n in outcome.limitations if 'refused for exceeding' in n)
    assert SPEC in note and 'max_document_bytes' in note
    assert not any('refused for exceeding' in n for n in run({SPEC: contract()}, SPEC)[0].limitations)
