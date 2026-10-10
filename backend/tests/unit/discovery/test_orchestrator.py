from datetime import datetime, timezone
import json
from unittest.mock import patch

import pytest

from radar.discovery.fetch import FetchAttempt, FetchFailure, FetchResult
from radar.discovery.input import DiscoveryInputError
from radar.discovery.limits import DiscoveryBudget, FetchLimits
from radar.discovery.orchestrator import discover, select_candidate
from radar.domain.discovery import DiscoveryOutcome, DiscoveryRequest, DiscoveryStatus

HOST = 'https://api.acme.com'
V, A, R, I, N = (DiscoveryStatus.VALIDATED, DiscoveryStatus.AMBIGUOUS, DiscoveryStatus.REJECTED,
                 DiscoveryStatus.INACCESSIBLE, DiscoveryStatus.NOT_FOUND)


def contract(title='Acme Payments API', server=HOST + '/v1', **extra):
    return {'openapi': '3.0.3', 'info': {'title': title, 'version': '1.0.0'}, 'servers': [{'url': server}],
            'paths': {'/customers': {'get': {}, 'post': {}}}, **extra}


def body(value):
    return value if isinstance(value, bytes) else json.dumps(value).encode()


class Network:
    """URL -> bytes | ('status', code) | ('fail', code) | ('html', text)."""

    def __init__(self, files=None):
        self.files = {k if '://' in k else HOST + k: v for k, v in (files or {}).items()}
        self.calls = []

    def fetch(self, url, budget, **options):
        self.calls.append(url)
        if budget.requests_used >= budget.limits.max_requests:
            return FetchResult(url, url, None, None, None, None, (), FetchFailure('request_limit', 'budget'))
        budget.claim_request()
        value = self.files.get(url, ('status', 404))
        if isinstance(value, tuple) and value[0] == 'status':
            return FetchResult(url, url, value[1], None, None, None, (FetchAttempt(url, value[1]),),
                               FetchFailure('http_error', f'HTTP status {value[1]}.'))
        if isinstance(value, tuple) and value[0] == 'fail':
            return FetchResult(url, url, None, None, None, None, (), FetchFailure(value[1], 'test failure'))
        kind, content = ('text/html', value[1].encode()) if isinstance(value, tuple) else ('application/json', body(value))
        return FetchResult(url, url, 200, kind, content, datetime(2026, 10, 9, tzinfo=timezone.utc), (FetchAttempt(url, 200),))


def run(files=None, target='api.acme.com', registry=None, tmp_path=None, **kwargs):
    network = Network(files)
    hints = {k: kwargs.pop(k) for k in ('method', 'api_version', 'product') if k in kwargs}
    if registry is not None:
        path = tmp_path / 'providers.json'
        path.write_text(json.dumps({'schema_version': 1, 'providers': registry}))
        kwargs['registry_path'] = path
    with patch('radar.discovery.candidates.fetch_document', side_effect=network.fetch), \
            patch('radar.discovery.capture.fetch_document', side_effect=network.fetch):
        outcome = discover(DiscoveryRequest(target, **hints), **kwargs)
    return outcome, network


def mapping(id, url, version=None, host='api.acme.com', **extra):
    return dict(id=id, hosts=[host], spec_url=url, provenance_url='https://github.com/acme/specs',
                **({'api_version': version} if version else {}), **extra)


# --- the five outcomes -------------------------------------------------------

def test_validated_from_a_common_location():
    outcome, network = run({'/openapi.json': contract()})
    assert outcome.status is V and outcome.package is not None and outcome.packages == ()
    assert outcome.package.candidate.discovery_method == 'common_location'
    assert outcome.package.candidate.source_url == HOST + '/openapi.json'
    assert outcome.package.root_document.content == body(contract())
    assert any(a.outcome == 'not_found' for a in outcome.attempts) and any(a.outcome == 'retrieved' for a in outcome.attempts)
    assert outcome.limitations[0].startswith('Discovery is bounded') and len(set(outcome.limitations)) == len(outcome.limitations)


def test_ambiguous_when_two_distinct_contracts_fit_and_nothing_is_chosen():
    other = contract(title='Acme Billing API', paths={'/invoices': {'get': {}}})
    outcome, _ = run({'/openapi.json': contract(), '/swagger.json': other})
    assert outcome.status is A and outcome.package is None and len(outcome.packages) == 2
    assert {p.candidate.source_url for p in outcome.packages} == {HOST + '/openapi.json', HOST + '/swagger.json'}
    assert any('none was chosen' in n for n in outcome.limitations)


def test_rejected_when_documents_are_found_but_none_qualifies():
    swagger2 = {'swagger': '2.0', 'info': {'title': 't', 'version': '1'}, 'paths': {}}
    outcome, _ = run({'/swagger.json': swagger2})
    assert outcome.status is R and outcome.package is None
    (candidate,) = outcome.candidates
    assert candidate.rejection_reasons[0].startswith('validation:unsupported_version:')


def test_inaccessible_when_nothing_qualifies_and_a_source_cannot_be_reached():
    outcome, _ = run({'/openapi.json': ('status', 503)})
    assert outcome.status is I and any(a.outcome == 'inaccessible' for a in outcome.attempts)
    assert any('could not be reached' in n for n in outcome.limitations)
    for failure in (('status', 403), ('fail', 'timeout'), ('fail', 'tls_error'), ('fail', 'connection_error')):
        assert run({'/openapi.json': failure})[0].status is I, failure


def test_not_found_when_every_source_is_a_clean_miss():
    outcome, _ = run({})
    assert outcome.status is N and outcome.candidates == () and outcome.package is None
    assert {a.outcome for a in outcome.attempts} == {'not_found'}
    assert 'not proof' in ' '.join(outcome.limitations)
    assert run({'/openapi.json': ('status', 410)})[0].status is N


def test_a_rejected_document_outranks_an_unreachable_source():
    swagger2 = {'swagger': '2.0', 'info': {'title': 't', 'version': '1'}, 'paths': {}}
    outcome, _ = run({'/swagger.json': swagger2, '/openapi.json': ('status', 503)})
    assert outcome.status is R and any(a.outcome == 'inaccessible' for a in outcome.attempts)


def test_inaccessible_is_distinguished_from_not_found_even_among_many_misses():
    assert run({'/swagger.json': ('status', 500)})[0].status is I


# --- catch-all pages and candidate rejections --------------------------------

def test_catch_all_pages_on_guessed_locations_are_a_miss_not_a_rejected_contract():
    page = ('html', '<!doctype html><html><body>App</body></html>')
    outcome, _ = run({'/openapi.json': page, '/openapi.yaml': page, '/swagger.json': page})
    assert outcome.status is N and outcome.candidates == ()
    assert sum(a.outcome == 'not_a_contract' for a in outcome.attempts) == 3


def test_html_named_by_a_provider_mapping_is_a_real_rejection(tmp_path):
    url = 'https://specs.example.net/acme.json'
    outcome, _ = run({url: ('html', '<html>moved</html>')}, registry=[mapping('acme', url)], tmp_path=tmp_path)
    assert outcome.status is R and outcome.candidates[0].rejection_reasons[0].startswith('validation:html_document')


def test_a_contract_for_another_api_is_rejected_by_matching():
    outcome, _ = run({'/openapi.json': contract(server='https://api.other.test/v1')})
    assert outcome.status is R
    assert outcome.candidates[0].rejection_reasons[0].startswith('matching:server_host_mismatch')


def test_incomplete_references_reject_the_candidate():
    outcome, _ = run({'/openapi.json': contract(paths={'/c': {'get': {'x': {'$ref': 'gone.json'}}}})})
    assert outcome.status is R and outcome.candidates[0].rejection_reasons[0].startswith('reference_capture:')


def test_one_good_and_one_bad_candidate_is_validated_and_both_are_reported():
    swagger2 = {'swagger': '2.0', 'info': {'title': 't', 'version': '1'}, 'paths': {}}
    outcome, _ = run({'/openapi.json': contract(), '/swagger.json': swagger2})
    assert outcome.status is V
    assert [bool(c.rejection_reasons) for c in outcome.candidates] == [False, True]


def test_unrelated_json_at_a_guessed_location_is_a_miss_recorded_with_its_reason():
    outcome, _ = run({'/openapi.json': {'detail': 'Not Found'}})
    assert outcome.status is N and outcome.candidates == ()
    (attempt,) = [a for a in outcome.attempts if a.outcome == 'not_a_contract']
    assert attempt.reason.startswith('not_openapi:')


def test_accepted_package_carries_the_referenced_files():
    root = contract(paths={'/c': {'get': {'x': {'$ref': 'defs.json#/A'}}}})
    outcome, _ = run({'/openapi.json': root, '/defs.json': {'A': {'type': 'object'}}})
    assert outcome.status is V and len(outcome.package.referenced_documents) == 1


# --- provider mappings, versions and de-duplication --------------------------

def test_same_contract_found_two_ways_is_one_validated_result_with_both_recorded(tmp_path):
    url = 'https://specs.example.net/acme.json'
    outcome, _ = run({'/openapi.json': contract(), url: contract()}, registry=[mapping('acme', url)], tmp_path=tmp_path)
    assert outcome.status is V and outcome.package.candidate.discovery_method == 'provider_mapping'
    assert any(e.criterion == 'also_found' and 'common_location' in e.description for e in outcome.package.candidate.evidence)
    assert len(outcome.candidates) == 1


def test_dated_mappings_are_ambiguous_until_a_version_hint_selects_one(tmp_path):
    a, b = 'https://specs.example.net/2022.json', 'https://specs.example.net/2026.json'
    files = {a: contract(), b: contract(paths={'/other': {'get': {}}})}
    registry = [mapping('v1', a, '2022-11-28'), mapping('v2', b, '2026-03-10')]
    assert run(files, registry=registry, tmp_path=tmp_path)[0].status is A
    outcome, _ = run(files, registry=registry, tmp_path=tmp_path, api_version='2026-03-10')
    assert outcome.status is V and outcome.package.candidate.source_url == b
    assert any(c.rejection_reasons and 'version_mismatch' in c.rejection_reasons[0] for c in outcome.candidates)


def test_the_same_url_is_never_fetched_twice_across_strategies(tmp_path):
    url = HOST + '/openapi.json'
    outcome, network = run({url: contract()}, registry=[mapping('acme', url)], tmp_path=tmp_path)
    assert outcome.status is V and network.calls.count(url) == 1


def test_json_and_yaml_copies_of_one_contract_are_one_result_and_both_locations_are_kept():
    import yaml
    document = contract()
    yaml_bytes = yaml.safe_dump(document, sort_keys=False).encode()
    json_bytes = json.dumps(document, indent=2).encode()
    assert yaml_bytes != json_bytes
    outcome, _ = run({'/openapi.json': json_bytes, '/openapi.yaml': yaml_bytes})
    assert outcome.status is V and outcome.packages == () and len(outcome.candidates) == 1
    package = outcome.package
    assert package.candidate.source_url == HOST + '/openapi.json' and package.root_document.content == json_bytes
    also = [e for e in package.candidate.evidence if e.criterion == 'also_found']
    assert [e.source_url for e in also] == [HOST + '/openapi.yaml']
    assert select_candidate_urls(package) == {HOST + '/openapi.json', HOST + '/openapi.yaml'}


def select_candidate_urls(package):
    return {package.candidate.source_url, *(e.source_url for e in package.candidate.evidence if e.criterion == 'also_found')}


def test_two_copies_that_really_differ_stay_ambiguous():
    import yaml
    changed = contract()
    changed['paths']['/customers']['delete'] = {}
    outcome, _ = run({'/openapi.json': contract(), '/openapi.yaml': yaml.safe_dump(changed).encode()})
    assert outcome.status is A and len(outcome.packages) == 2


# --- documentation links -----------------------------------------------------

DOCS = ('html', '<a href="/files/acme.json">Download OpenAPI</a>')


def test_documentation_links_are_followed():
    outcome, _ = run({'/docs': DOCS, '/files/acme.json': contract()})
    assert outcome.status is V and outcome.package.candidate.discovery_method == 'documentation_link'
    assert outcome.package.candidate.discovery_source == HOST + '/docs'


def test_a_link_to_a_private_address_does_not_make_the_result_inaccessible():
    page = ('html', '<a href="http://169.254.169.254/openapi.json">Download OpenAPI</a>')
    outcome, _ = run({'/docs': page, 'http://169.254.169.254/openapi.json': ('fail', 'blocked_destination')})
    assert outcome.status is N and any(a.outcome == 'blocked' for a in outcome.attempts)


def test_a_documentation_page_that_is_unreachable_counts_as_inaccessible():
    assert run({'/docs': ('status', 403)})[0].status is I


# --- limits ------------------------------------------------------------------

def test_budget_exhaustion_with_nothing_found_says_so_and_never_claims_absence():
    outcome, _ = run({'/swagger.json': contract()}, limits=FetchLimits(max_requests=2))
    assert outcome.status is N
    assert any('cut short' in n and 'other contracts may exist' in n for n in outcome.limitations)


def test_a_result_found_before_the_budget_ran_out_is_flagged_incomplete():
    outcome, _ = run({'/openapi.json': contract()}, limits=FetchLimits(max_requests=1))
    assert outcome.status is V and any('cut short' in n for n in outcome.limitations)


def test_an_explicit_budget_is_shared_and_consumed():
    budget = DiscoveryBudget()
    run({'/openapi.json': contract()}, budget=budget)
    assert budget.requests_used > 0


# --- selection ---------------------------------------------------------------

def ambiguous():
    other = contract(title='Acme Billing API', paths={'/invoices': {'get': {}}})
    return run({'/openapi.json': contract(), '/swagger.json': other})[0]


def test_selection_resolves_an_ambiguous_outcome_without_refetching():
    outcome = ambiguous()
    chosen = select_candidate(outcome, HOST + '/swagger.json')
    assert chosen.status is V and chosen.package.candidate.source_url == HOST + '/swagger.json' and chosen.packages == ()
    assert chosen.candidates == outcome.candidates and any('Selected explicitly' in n for n in chosen.limitations)


def test_selection_accepts_an_alternative_location_of_the_same_contract(tmp_path):
    url = 'https://specs.example.net/acme.json'
    other = contract(title='Acme Billing API', paths={'/invoices': {'get': {}}})
    files = {HOST + '/openapi.json': contract(), url: contract(), HOST + '/swagger.json': other}
    outcome, _ = run(files, registry=[mapping('acme', url)], tmp_path=tmp_path)
    assert outcome.status is A
    assert select_candidate(outcome, HOST + '/openapi.json').package.candidate.source_url == url


def test_invalid_selections_are_errors():
    with pytest.raises(ValueError):
        select_candidate(ambiguous(), 'https://elsewhere.test/spec.json')
    with pytest.raises(ValueError):
        select_candidate(run({'/openapi.json': contract()})[0], HOST + '/openapi.json')


# --- contract of the function ------------------------------------------------

def test_invalid_input_is_an_error_not_an_outcome():
    for bad in ('', 'api.acme.com/path', 'ftp://x.test', 'a b'):
        with pytest.raises(DiscoveryInputError):
            run({}, target=bad)


def test_packages_only_belong_to_ambiguous_outcomes():
    outcome = ambiguous()
    with pytest.raises(ValueError):
        DiscoveryOutcome(V, packages=outcome.packages, package=outcome.packages[0])
    with pytest.raises(ValueError):
        DiscoveryOutcome(V)


def test_discovery_is_deterministic():
    files = {'/openapi.json': contract(), '/docs': DOCS, '/files/acme.json': contract(title='Other', paths={'/x': {'get': {}}})}
    assert run(files)[0] == run(files)[0]


def test_operation_context_is_used():
    outcome, _ = run({'/openapi.json': contract()}, target='https://api.acme.com/v1/customers', method='GET')
    assert outcome.status is V
    assert {e.criterion: e.outcome for e in outcome.package.candidate.evidence if e.outcome}['operation'] == 'match'
    missing, _ = run({'/openapi.json': contract()}, target='https://api.acme.com/v1/orders', method='GET')
    assert missing.status is R and 'operation_not_found' in missing.candidates[0].rejection_reasons[0]
