from datetime import datetime, timezone
import json
from unittest.mock import patch

import pytest

from radar.discovery.candidates import RetrievedCandidate
from radar.discovery.evaluation import CandidateEvaluation, evaluate_candidate, package_fingerprint
from radar.discovery.fetch import FetchAttempt, FetchFailure, FetchResult
from radar.discovery.input import normalize_target
from radar.discovery.limits import DiscoveryBudget, FetchLimits
from radar.discovery.providers import ProviderMapping
from radar.domain.discovery import ContractCandidate, DiscoveryRequest, MatchingEvidence


SPEC_URL = 'https://api.acme.com/openapi.json'


def contract(**extra):
    document = {'openapi': '3.0.3', 'info': {'title': 'Acme Payments API', 'version': '1.0.0'},
                'servers': [{'url': 'https://api.acme.com/v1'}],
                'paths': {'/customers': {'get': {'responses': {'200': {'description': 'ok', 'content': {
                    'application/json': {'schema': {'$ref': '#/components/schemas/Customer'}}}}}}}},
                'components': {'schemas': {'Customer': {'type': 'object'}}}}
    document.update(extra)
    return document


def retrieved(content, url=SPEC_URL, method='common_location', source=None, evidence=()):
    content = content if isinstance(content, bytes) else json.dumps(content).encode()
    fetch = FetchResult(url, url, 200, 'application/json', content, datetime(2026, 10, 9, tzinfo=timezone.utc),
                        (FetchAttempt(url, 200),))
    candidate = ContractCandidate(url, method, source or url, evidence,
                                  limitations=('OpenAPI validity and relevance have not been assessed.',))
    return RetrievedCandidate(candidate, fetch)


def target(text='api.acme.com', **hints):
    return normalize_target(DiscoveryRequest(text, **hints))


class Network:
    def __init__(self, files=None):
        self.files, self.calls = files or {}, []

    def fetch(self, url, budget, **options):
        self.calls.append(url)
        budget.claim_request()
        value = self.files.get(url)
        if value is None:
            return FetchResult(url, url, 404, None, None, None, (), FetchFailure('http_error', 'HTTP status 404.'))
        if isinstance(value, tuple):
            return FetchResult(url, url, None, None, None, None, (), FetchFailure(value[0], 'test'))
        return FetchResult(url, url, 200, 'application/json', value, datetime(2026, 10, 9, tzinfo=timezone.utc), ())


def evaluate(candidate, files=None, tgt=None, budget=None, **options):
    network = Network(files)
    with patch('radar.discovery.capture.fetch_document', side_effect=network.fetch):
        result = evaluate_candidate(candidate, tgt or target(), budget or DiscoveryBudget(), **options)
    return result, network


# --- accepted ----------------------------------------------------------------

def test_a_valid_matching_candidate_becomes_a_package():
    raw = json.dumps(contract(), indent=2).encode()  # formatting must be preserved byte for byte
    result, network = evaluate(retrieved(raw))
    assert result.accepted and result.rejection is None and network.calls == []
    package = result.package
    assert package.root_document.content == raw and package.root_document.source_url == SPEC_URL
    assert package.root_document.media_type == 'application/json' and package.root_document.retrieved_at.year == 2026
    assert package.openapi_version == '3.0.3' and package.parsed_contract['info']['title'] == 'Acme Payments API'
    assert package.referenced_documents == () and package.candidate is result.candidate
    assert result.match.accepted


def test_evidence_keeps_the_discovery_hint_and_adds_every_check_with_its_outcome():
    hint = MatchingEvidence('documentation_reference', 'linked from docs', 'https://api.acme.com/docs')
    result, _ = evaluate(retrieved(contract(), evidence=(hint,)))
    evidence = result.package.candidate.evidence
    assert evidence[0] == hint and evidence[0].outcome is None
    assert [(e.criterion, e.outcome) for e in evidence[1:]] == [
        ('server_host', 'match'), ('operation', 'not_requested'), ('api_version', 'not_requested'),
        ('product', 'not_requested'), ('provenance', 'match')]
    assert all(e.source_url == SPEC_URL for e in evidence[1:])


def test_limitations_are_replaced_not_accumulated():
    result, _ = evaluate(retrieved(contract()))
    notes = result.package.limitations
    assert result.package.candidate.limitations == notes
    assert 'OpenAPI validity and relevance have not been assessed.' not in notes
    assert any('official OpenAPI JSON Schema' in n for n in notes) and any('does not prove identity' in n for n in notes)
    assert len(set(notes)) == len(notes)


def test_requested_context_is_checked_and_recorded():
    result, _ = evaluate(retrieved(contract()), tgt=target('https://api.acme.com/v1/customers', method='GET',
                                                           api_version='v1', product='payments'))
    assert result.accepted
    assert {e.criterion: e.outcome for e in result.package.candidate.evidence if e.outcome} == {
        'server_host': 'match', 'operation': 'match', 'api_version': 'match', 'product': 'match', 'provenance': 'match'}


def test_provider_mapping_hints_reach_matching():
    mapping = ProviderMapping('m', ('api.acme.com',), SPEC_URL, 'https://github.com/acme', 'Acme Payments API', '2022-11-28')
    accepted, _ = evaluate(retrieved(contract(), method='provider_mapping', source=mapping.provenance_url),
                           tgt=target(api_version='2022-11-28'), mapping=mapping)
    assert accepted.accepted
    rejected, _ = evaluate(retrieved(contract(), method='provider_mapping', source=mapping.provenance_url),
                           tgt=target(api_version='2026-03-10'), mapping=mapping)
    assert rejected.rejection.code == 'version_mismatch' and rejected.rejection.stage == 'matching'


def test_a_candidate_with_no_positive_evidence_is_kept_but_flagged():
    relative = contract(servers=[{'url': '/v1'}])
    result, _ = evaluate(retrieved(relative, url='https://cdn.example.net/spec.json', method='documentation_link',
                                   source='https://blog.other.test/post'))
    assert result.accepted and any('No check produced positive evidence' in n for n in result.package.limitations)


# --- referenced documents ----------------------------------------------------

def test_external_references_are_captured_into_the_package():
    root = contract(paths={'/customers': {'get': {'responses': {'200': {'content': {'application/json': {
        'schema': {'$ref': 'schemas/customer.json#/Customer'}}}}}}}})
    files = {'https://api.acme.com/schemas/customer.json': b'{"Customer": {"type": "object"}}'}
    result, network = evaluate(retrieved(root), files)
    assert result.accepted and network.calls == ['https://api.acme.com/schemas/customer.json']
    (referenced,) = result.package.referenced_documents
    assert referenced.content == files['https://api.acme.com/schemas/customer.json']


def test_a_missing_reference_rejects_at_the_capture_stage_and_keeps_the_match_evidence():
    root = contract(paths={'/customers': {'get': {'x': {'$ref': 'gone.json'}}}})
    result, _ = evaluate(retrieved(root))
    assert not result.accepted and result.package is None
    assert (result.rejection.stage, result.rejection.code) == ('reference_capture', 'reference_unavailable')
    assert result.capture_failure.http_status == 404 and result.match.accepted
    assert any(e.criterion == 'server_host' for e in result.candidate.evidence)
    assert result.candidate.rejection_reasons[0].startswith('reference_capture:reference_unavailable:')


def test_budget_exhaustion_is_reported_with_its_own_code():
    root = contract(paths={'/customers': {'get': {'x': {'$ref': 'f.json'}}}})
    result, _ = evaluate(retrieved(root), {'https://api.acme.com/f.json': ('request_limit',)})
    assert result.rejection.code == 'capture_budget_exhausted'


# --- rejections --------------------------------------------------------------

@pytest.mark.parametrize('content,code', [
    (b'<html><body>Not found</body></html>', 'html_document'),
    (b'{"name": "package"}', 'not_openapi'),
    (b'{"swagger": "2.0", "info": {"title": "t", "version": "1"}, "paths": {}}', 'unsupported_version'),
    (b'', 'empty_document'),
])
def test_invalid_documents_stop_at_validation_without_any_fetch(content, code):
    result, network = evaluate(retrieved(content))
    assert (result.rejection.stage, result.rejection.code) == ('validation', code)
    assert result.match is None and result.package is None and network.calls == []
    assert result.candidate.rejection_reasons == (f'validation:{code}: {result.rejection.reason}',)


def test_a_non_matching_contract_never_spends_budget_on_its_references():
    root = contract(paths={'/customers': {'get': {'x': {'$ref': 'big.json'}}}})
    budget = DiscoveryBudget()
    result, network = evaluate(retrieved(root), tgt=target('api.other.test'), budget=budget)
    assert (result.rejection.stage, result.rejection.code) == ('matching', 'server_host_mismatch')
    assert network.calls == [] and budget.requests_used == 0
    assert result.match is not None and not result.match.accepted
    assert {e.criterion for e in result.candidate.evidence} >= {'server_host', 'operation', 'provenance'}


def test_rejection_reasons_are_recorded_once_per_stage():
    result, _ = evaluate(retrieved(contract()), tgt=target(product='shipping'))
    assert len(result.candidate.rejection_reasons) == 1 and result.candidate.rejection_reasons[0].startswith('matching:')


def test_only_retrieved_candidates_can_be_evaluated():
    failed = FetchResult(SPEC_URL, SPEC_URL, 404, None, None, None, (), FetchFailure('http_error', 'x'))
    with pytest.raises(ValueError):
        evaluate_candidate(RetrievedCandidate(ContractCandidate(SPEC_URL, 'x', SPEC_URL), failed), target(), DiscoveryBudget())


def test_an_evaluation_is_a_package_or_a_rejection_never_both_or_neither():
    good, _ = evaluate(retrieved(contract()))
    with pytest.raises(ValueError):
        CandidateEvaluation(good.candidate)
    with pytest.raises(ValueError):
        CandidateEvaluation(good.candidate, package=good.package, rejection=object())


def test_evaluation_is_deterministic():
    assert evaluate(retrieved(contract()))[0] == evaluate(retrieved(contract()))[0]


# --- fingerprint -------------------------------------------------------------

def fingerprint(content, url=SPEC_URL, files=None, **ref):
    result, _ = evaluate(retrieved(content, url=url), files)
    assert result.accepted, result.rejection
    return package_fingerprint(result.package)


def test_same_content_has_the_same_fingerprint_wherever_it_was_found():
    raw = json.dumps(contract()).encode()
    mirror = 'https://api.acme.com/mirror/spec.json'
    assert fingerprint(raw) == fingerprint(raw, url=mirror)


def test_different_content_or_different_referenced_content_changes_it():
    base = json.dumps(contract()).encode()
    assert fingerprint(base) != fingerprint(json.dumps(contract(info={'title': 'Acme', 'version': '2'})).encode())
    root = contract(paths={'/customers': {'get': {'x': {'$ref': 'a.json'}}}})
    url = 'https://api.acme.com/a.json'
    assert fingerprint(root, files={url: b'{"a": 1}'}) != fingerprint(root, files={url: b'{"a": 2}'})
    assert fingerprint(root, files={url: b'{"a": 1}'}) == fingerprint(root, files={url: b'{"a": 1}'})


def test_fingerprint_does_not_depend_on_reference_order():
    one = contract(paths={'/a': {'get': {'x': {'$ref': 'a.json'}, 'y': {'$ref': 'b.json'}}}})
    two = contract(paths={'/a': {'get': {'y': {'$ref': 'b.json'}, 'x': {'$ref': 'a.json'}}}})
    files = {'https://api.acme.com/a.json': b'{"a": 1}', 'https://api.acme.com/b.json': b'{"b": 2}'}
    a, _ = evaluate(retrieved(one), files)
    b, _ = evaluate(retrieved(two), files)
    swapped = type(a.package)(b.package.candidate, a.package.root_document, tuple(reversed(a.package.referenced_documents)),
                              a.package.parsed_contract, a.package.openapi_version)
    assert package_fingerprint(a.package) == package_fingerprint(swapped)


# --- serialization-independent identity ---------------------------------------

def as_yaml(document):
    import yaml
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False).encode()


def test_json_and_yaml_forms_of_one_contract_have_one_fingerprint():
    document = contract()
    compact = json.dumps(document, separators=(',', ':')).encode()
    pretty = json.dumps(document, indent=4).encode()
    reordered = json.dumps(dict(reversed(list(document.items()))), sort_keys=False).encode()
    prints = {fingerprint(raw) for raw in (compact, pretty, reordered, as_yaml(document))}
    assert len(prints) == 1
    assert compact != pretty != as_yaml(document)  # the bytes really do differ


def test_real_differences_still_change_the_fingerprint():
    base = contract()
    different = [
        contract(info={'title': 'Acme Payments API', 'version': '1.0.1'}),
        contract(servers=[{'url': 'https://api.acme.com/v2'}]),
        contract(paths={'/customers': {'get': {}, 'post': {}}}),
        contract(extra_flag=True),
    ]
    assert len({fingerprint(d) for d in [base, *different]}) == 5


def test_list_order_is_meaningful_but_mapping_order_is_not():
    one = contract(**{'x-order': [1, 2, 3]})
    two = contract(**{'x-order': [3, 2, 1]})
    assert fingerprint(one) != fingerprint(two)
    assert fingerprint(contract(**{'x-m': {'a': 1, 'b': 2}})) == fingerprint(contract(**{'x-m': {'b': 2, 'a': 1}}))


def test_booleans_and_numbers_that_python_calls_equal_stay_distinct():
    assert fingerprint(contract(**{'x-v': True})) != fingerprint(contract(**{'x-v': 1}))
    assert fingerprint(contract(**{'x-v': 1})) != fingerprint(contract(**{'x-v': 1.0}))
    assert fingerprint(contract(**{'x-v': 0})) != fingerprint(contract(**{'x-v': False}))
    assert fingerprint(contract(**{'x-v': '1'})) != fingerprint(contract(**{'x-v': 1}))


def test_referenced_files_are_compared_by_content_not_formatting():
    root = contract(paths={'/customers': {'get': {'x': {'$ref': 'a.json'}}}})
    url = 'https://api.acme.com/a.json'
    compact = fingerprint(root, files={url: b'{"a":1,"b":[1,2]}'})
    assert compact == fingerprint(root, files={url: b'{\n  "b": [1, 2],\n  "a": 1\n}'})
    assert compact == fingerprint(root, files={url: b'a: 1\nb: [1, 2]\n'})
    assert compact != fingerprint(root, files={url: b'{"a":1,"b":[2,1]}'})
