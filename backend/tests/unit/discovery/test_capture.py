from datetime import datetime, timezone
import json
from unittest.mock import patch

import pytest

from radar.discovery.capture import CaptureFailure, CaptureLimits, CaptureResult, capture_references
from radar.discovery.fetch import FetchAttempt, FetchFailure, FetchResult
from radar.discovery.limits import DiscoveryBudget, FetchLimits
from radar.discovery.references import ReferenceLimits
from radar.discovery.validation import ValidationLimits, ValidationRejection


ROOT = 'https://specs.acme.com/api/openapi.json'
SITE = 'https://specs.acme.com/api/'


class Site:
    """Fake network: URL -> bytes | ('fail', code, status) | ('redirect', final_url, bytes)."""

    def __init__(self, files):
        self.files = {SITE + path if '://' not in path else path: value for path, value in files.items()}
        self.calls = []
        self.options = []

    def fetch(self, url, budget, **options):
        self.calls.append(url)
        self.options.append(options)
        budget.claim_request()
        value = self.files.get(url, ('fail', 'http_error', 404))
        if isinstance(value, tuple) and value[0] == 'fail':
            return FetchResult(url, url, value[2], None, None, None, (FetchAttempt(url, value[2]),),
                               FetchFailure(value[1], f'{value[1]} for test'))
        final, content = (value[1], value[2]) if isinstance(value, tuple) else (url, value)
        return FetchResult(url, final, 200, 'application/json', content, datetime.now(timezone.utc),
                           (FetchAttempt(url, 200),))


def run(root, files=None, limits=None, **options):
    site = Site(files or {})
    budget = options.pop('budget', DiscoveryBudget())
    with patch('radar.discovery.capture.fetch_document', side_effect=site.fetch):
        result = capture_references(ROOT, root, budget, limits=limits, **options)
    return result, site


def doc(**extra):
    return {'openapi': '3.0.3', 'info': {'title': 'T', 'version': '1'}, 'paths': {}, **extra}


def j(value):
    return json.dumps(value).encode()


def name(url):
    return url.rsplit('/', 1)[-1]


# --- success -----------------------------------------------------------------

def test_no_external_references():
    result, site = run(doc(a={'$ref': '#/b'}, b={}))
    assert result.ok and result.documents == () and result.references == () and result.internal_reference_count == 1
    assert site.calls == []


def test_external_file_is_captured_with_original_bytes_and_an_edge():
    pet = b'{ "Pet": {"type": "object"},\n  "x": 1 }\n'  # unusual formatting must survive untouched
    result, site = run(doc(a={'$ref': './pet.json#/Pet'}), {'pet.json': pet})
    assert result.ok and site.calls == [SITE + 'pet.json']
    (captured,) = result.documents
    assert captured.source_url == SITE + 'pet.json' and captured.content == pet
    assert captured.media_type == 'application/json' and captured.retrieved_at.tzinfo is not None
    (edge,) = result.references
    assert (edge.referrer_url, edge.location, edge.ref, edge.document_url, edge.pointer) == (
        ROOT, '/a', './pet.json#/Pet', SITE + 'pet.json', '/Pet')


def test_nested_chain_is_captured_in_breadth_first_order():
    files = {'a.json': j({'A': {'$ref': 'b.json#/B'}, 'A2': {'$ref': 'c.json'}}),
             'b.json': j({'B': {'$ref': 'd.json'}}), 'c.json': j({}), 'd.json': j({'ok': True})}
    result, _ = run(doc(x={'$ref': 'a.json#/A'}, y={'$ref': 'a.json#/A2'}), files)
    assert [name(d.source_url) for d in result.documents] == ['a.json', 'b.json', 'c.json', 'd.json']
    assert result.unprocessed == 0


def test_shared_target_is_fetched_once_but_every_reference_is_recorded():
    files = {'a.json': j({'A': {'$ref': 'common.json#/E'}}), 'b.json': j({'B': {'$ref': 'common.json#/E'}}),
             'common.json': j({'E': {}})}
    result, site = run(doc(x={'$ref': 'a.json#/A'}, y={'$ref': 'b.json#/B'}, z={'$ref': 'common.json#/E'}), files)
    assert site.calls.count(SITE + 'common.json') == 1 and len(result.documents) == 3
    edges = sorted((name(e.referrer_url), e.pointer) for e in result.references if 'common' in e.document_url)
    assert edges == [('a.json', '/E'), ('b.json', '/E'), ('openapi.json', '/E')]


def test_cycles_terminate():
    files = {'a.json': j({'A': {'$ref': 'b.json#/B'}}), 'b.json': j({'B': {'$ref': 'a.json#/A'}})}
    result, site = run(doc(x={'$ref': 'a.json#/A'}), files)
    assert result.ok and len(result.documents) == 2 and len(site.calls) == 2


def test_same_file_with_different_pointers_is_fetched_once_and_each_pointer_checked():
    files = {'defs.json': j({'A': {}, 'B': {}})}
    result, site = run(doc(x={'$ref': 'defs.json#/A'}, y={'$ref': 'defs.json#/B'}), files)
    assert result.ok and site.calls == [SITE + 'defs.json'] and len(result.references) == 2
    failed, _ = run(doc(x={'$ref': 'defs.json#/A'}, y={'$ref': 'defs.json#/C'}), files)
    assert failed.rejection.code == 'unresolvable_reference' and failed.rejection.location == '/y'


def test_yaml_and_list_documents_are_accepted():
    files = {'defs.yaml': b'Pet:\n  type: object\n', 'list.json': j([{'x': 1}])}
    assert run(doc(a={'$ref': 'defs.yaml#/Pet'}, b={'$ref': 'list.json#/0'}), files)[0].ok


def test_query_strings_make_distinct_documents():
    files = {'f.json?v=1': j({}), 'f.json?v=2': j({})}
    result, site = run(doc(a={'$ref': 'f.json?v=1'}, b={'$ref': 'f.json?v=2'}), files)
    assert result.ok and len(result.documents) == 2 and len(site.calls) == 2


def test_references_back_into_the_root_resolve_without_refetching():
    files = {'a.json': j({'A': {'$ref': 'openapi.json#/components/schemas/Pet'}})}
    result, site = run(doc(x={'$ref': 'a.json#/A'}, components={'schemas': {'Pet': {}}}), files)
    assert result.ok and site.calls == [SITE + 'a.json']
    missing, _ = run(doc(x={'$ref': 'a.json#/A'}), files)
    assert missing.rejection.code == 'unresolvable_reference' and missing.rejection.location == f'{SITE}a.json#/A'


def test_redirect_within_the_origin_is_followed_and_identity_is_the_final_url():
    files = {'old.json': ('redirect', SITE + 'new.json', j({'X': {}})), 'new.json': j({'X': {}})}
    result, _ = run(doc(a={'$ref': 'old.json#/X'}, b={'$ref': 'new.json#/X'}), files)
    assert result.ok and [d.source_url for d in result.documents] == [SITE + 'new.json']
    assert {e.document_url for e in result.references} == {SITE + 'new.json'}
    assert {e.requested_url for e in result.references} == {SITE + 'old.json', SITE + 'new.json'}


def test_options_reach_the_fetcher():
    _, site = run(doc(a={'$ref': 'f.json'}), {'f.json': j({})}, allow_loopback=True)
    assert site.options == [{'allow_loopback': True}]


def test_limitations_are_merged_without_duplicates():
    files = {'a.json': j({'A': {'$ref': '#node', '$id': 'x'}})}
    result, _ = run(doc(a={'$ref': 'a.json#/A'}, b={'$ref': '#top'}), files)
    assert result.ok and len(set(result.limitations)) == len(result.limitations)
    assert any('anchor-style' in note for note in result.limitations)
    assert any('operationRef' in note for note in result.limitations)


def test_capture_is_deterministic():
    files = {'a.json': j({'A': {'$ref': 'b.json'}}), 'b.json': j({})}
    root = doc(x={'$ref': 'a.json#/A'})
    first, second = run(root, files)[0], run(root, files)[0]
    assert first.references == second.references
    assert [d.source_url for d in first.documents] == [d.source_url for d in second.documents]


# --- reachability ------------------------------------------------------------

def test_unreachable_parts_of_a_referenced_file_are_not_required():
    files = {'defs.json': j({'Good': {'type': 'string'}, 'Unused': {'$ref': 'missing.json'}, 'Broken': {'$ref': '#/nope'}})}
    result, site = run(doc(a={'$ref': 'defs.json#/Good'}), files)
    assert result.ok and site.calls == [SITE + 'defs.json']


def test_reachable_failures_inside_a_referenced_file_are_reported_with_the_file():
    files = {'defs.json': j({'Pet': {'owner': {'$ref': '#/Owner'}}})}
    result, _ = run(doc(a={'$ref': 'defs.json#/Pet'}), files)
    assert result.rejection.code == 'unresolvable_reference' and result.rejection.stage == 'reference_capture'
    assert result.rejection.location == f'{SITE}defs.json#/Pet/owner/$ref' and 'defs.json' in result.rejection.reason


def test_whole_file_reference_requires_everything_in_it():
    files = {'defs.json': j({'Broken': {'$ref': '#/nope'}})}
    assert run(doc(a={'$ref': 'defs.json'}), files)[0].rejection.code == 'unresolvable_reference'


def test_internal_references_inside_a_fetched_file_are_followed_to_their_own_external_references():
    # Regression: Pet -> #/Error -> another file. Checking that #/Error exists is not enough.
    files = {'defs.json': j({'Pet': {'e': {'$ref': '#/Error'}}, 'Error': {'$ref': 'err.json'}}), 'err.json': j({'ok': 1})}
    result, site = run(doc(a={'$ref': 'defs.json#/Pet'}), files)
    assert result.ok and site.calls == [SITE + 'defs.json', SITE + 'err.json']
    broken, _ = run(doc(a={'$ref': 'defs.json#/Pet'}), {'defs.json': files['defs.json']})
    assert broken.rejection.code == 'reference_unavailable' and 'err.json' in broken.rejection.reason


def test_internal_chains_cycles_and_nested_targets_inside_a_fetched_file():
    files = {'defs.json': j({'A': {'$ref': '#/B'}, 'B': {'$ref': '#/C/inner'}, 'C': {'inner': {'$ref': '#/A'}, 'x': {'$ref': 'x.json'}},
                             'Unreached': {'$ref': 'never.json'}}), 'x.json': j({})}
    result, site = run(doc(a={'$ref': 'defs.json#/A'}), files)
    assert result.ok and site.calls == [SITE + 'defs.json']  # C/inner is reached, C/x is a sibling and is not
    through_parent = {'defs.json': j({'A': {'$ref': '#/C'}, 'C': {'inner': {'$ref': '#/A'}, 'x': {'$ref': 'x.json'}}}),
                      'x.json': j({})}
    both, site = run(doc(a={'$ref': 'defs.json#/A'}), through_parent)
    assert both.ok and site.calls == [SITE + 'defs.json', SITE + 'x.json']


def test_internal_edges_are_not_reported_as_external_references():
    files = {'defs.json': j({'Pet': {'$ref': '#/Name'}, 'Name': {}})}
    result, _ = run(doc(a={'$ref': 'defs.json#/Pet'}), files)
    assert [(e.ref, e.pointer) for e in result.references] == [('defs.json#/Pet', '/Pet')]


# --- failures ----------------------------------------------------------------

def test_missing_file_rejects_and_exposes_no_partial_documents():
    files = {'a.json': j({}), 'c.json': j({})}
    result, _ = run(doc(a={'$ref': 'a.json'}, b={'$ref': 'gone.json'}, c={'$ref': 'c.json'}), files)
    assert not result.ok and result.documents == () and result.references == ()
    assert result.rejection.code == 'reference_unavailable' and result.rejection.stage == 'reference_capture'
    assert result.rejection.location == '/b' and 'gone.json' in result.rejection.reason
    assert result.failure == CaptureFailure('gone.json', SITE + 'gone.json', ROOT, 'http_error', 404, 1)
    assert result.unprocessed == 1


@pytest.mark.parametrize('fetch_code', ['timeout', 'blocked_destination', 'connection_error', 'tls_error',
                                        'document_size_limit', 'unsupported_encoding'])
def test_fetch_failures_keep_their_underlying_code(fetch_code):
    result, _ = run(doc(a={'$ref': 'f.json'}), {'f.json': ('fail', fetch_code, None)})
    assert result.rejection.code == 'reference_unavailable' and result.failure.fetch_code == fetch_code


@pytest.mark.parametrize('fetch_code', ['request_limit', 'deadline_exceeded', 'total_size_limit'])
def test_budget_exhaustion_is_distinct_from_a_broken_reference(fetch_code):
    result, _ = run(doc(a={'$ref': 'f.json'}), {'f.json': ('fail', fetch_code, None)})
    assert result.rejection.code == 'capture_budget_exhausted' and result.failure.fetch_code == fetch_code


@pytest.mark.parametrize('content,detail', [
    (b'<html><body>Not found</body></html>', 'html_document'),
    (b'Not Found', 'not a JSON/YAML object'),
    (b'404', 'not a JSON/YAML object'),
    (b'{"a": 1, "a": 2}', 'duplicate_key'),
    (b'{"a": ', 'not_json_or_yaml'),
    (b'', 'empty_document'),
    (b'\xff\xfe', 'unsupported_encoding'),
])
def test_unusable_referenced_documents_are_rejected(content, detail):
    result, _ = run(doc(a={'$ref': 'f.json'}), {'f.json': content})
    assert result.rejection.code == 'reference_invalid_document' and detail in result.rejection.reason


def test_referenced_documents_get_the_same_parsing_bounds():
    bomb = 'a: &a0 [x, x, x, x, x, x, x, x, x]\n' + ''.join(
        f'b{i}: &a{i} [' + ', '.join([f'*a{i - 1}'] * 9) + ']\n' for i in range(1, 10))
    limits = CaptureLimits(parsing=ValidationLimits(max_nodes=10_000))
    result, _ = run(doc(a={'$ref': 'bomb.yaml'}), {'bomb.yaml': bomb.encode()}, limits=limits)
    assert result.rejection.code == 'reference_invalid_document' and 'node_limit_exceeded' in result.rejection.reason


def test_root_level_reference_problems_reject_before_any_fetch():
    for root in (doc(a={'$ref': '#/missing'}), doc(a={'$ref': 'file:///etc/passwd'}, b={'$ref': 'f.json'})):
        result, site = run(root, {'f.json': j({})})
        assert not result.ok and site.calls == [] and result.rejection.stage == 'reference_capture'


def test_unsupported_url_inside_a_referenced_file_is_reported_with_the_file():
    files = {'a.json': j({'A': {'$ref': 'ftp://x.test/y'}})}
    result, _ = run(doc(a={'$ref': 'a.json#/A'}), files)
    assert result.rejection.code == 'reference_unsupported' and result.rejection.location.startswith(SITE + 'a.json#')


# --- origin policy -----------------------------------------------------------

@pytest.mark.parametrize('ref', ['https://cdn.acme.com/x.json', 'https://specs.acme.com:8443/x.json',
                                 'http://specs.acme.com/x.json', 'https://SPECS.acme.com./x.json'])
def test_cross_origin_references_are_rejected_by_default(ref):
    result, site = run(doc(a={'$ref': ref}), {ref: j({})})
    assert result.rejection.code == 'reference_cross_origin' and site.calls == []


def test_cross_origin_can_be_allowed_explicitly():
    files = {'https://cdn.acme.com/x.json': j({'X': {}})}
    result, _ = run(doc(a={'$ref': 'https://cdn.acme.com/x.json#/X'}), files, limits=CaptureLimits(allow_cross_origin=True))
    assert result.ok and result.documents[0].source_url == 'https://cdn.acme.com/x.json'


def test_a_chain_cannot_walk_to_another_host():
    files = {'a.json': j({'A': {'$ref': 'https://evil.test/x.json'}})}
    result, site = run(doc(a={'$ref': 'a.json#/A'}), files)
    assert result.rejection.code == 'reference_cross_origin' and site.calls == [SITE + 'a.json']


def test_redirect_to_another_origin_is_rejected():
    files = {'a.json': ('redirect', 'https://evil.test/x.json', j({}))}
    result, _ = run(doc(a={'$ref': 'a.json'}), files)
    assert result.rejection.code == 'reference_cross_origin' and 'redirected' in result.rejection.reason


# --- limits ------------------------------------------------------------------

def test_document_count_limit():
    files = {f'f{i}.json': j({}) for i in range(5)}
    root = doc(**{f'r{i}': {'$ref': f'f{i}.json'} for i in range(5)})
    result, _ = run(root, files, limits=CaptureLimits(max_documents=3))
    assert result.rejection.code == 'reference_limit_exceeded' and result.failure.documents_fetched == 3
    assert run(root, files, limits=CaptureLimits(max_documents=5))[0].ok


def test_depth_limit():
    files = {f'f{i}.json': j({'n': {'$ref': f'f{i + 1}.json'}}) for i in range(6)}
    files['f6.json'] = j({})
    root = doc(a={'$ref': 'f0.json'})
    assert run(root, files, limits=CaptureLimits(max_depth=7))[0].ok
    result, _ = run(root, files, limits=CaptureLimits(max_depth=3))
    assert result.rejection.code == 'reference_limit_exceeded' and 'deeper' in result.rejection.reason


def test_total_node_limit_across_documents():
    files = {'a.json': j({'x': [{'k': i} for i in range(200)]})}
    result, _ = run(doc(a={'$ref': 'a.json'}), files, limits=CaptureLimits(max_total_nodes=100))
    assert result.rejection.code == 'reference_limit_exceeded' and 'values' in result.rejection.reason


def test_per_scan_reference_limits_apply():
    root = doc(**{f'r{i}': {'$ref': f'f{i}.json'} for i in range(5)})
    result, _ = run(root, {}, limits=CaptureLimits(references=ReferenceLimits(max_external_references=2)))
    assert result.rejection.code == 'reference_limit_exceeded'


def test_budget_exhaustion_mid_capture_stops_with_a_budget_code_and_no_partial_documents():
    files = {'a.json': j({})}
    root = doc(a={'$ref': 'a.json'}, b={'$ref': 'b.json'})

    def fetch(url, budget, **options):
        if url.endswith('b.json'):
            return FetchResult(url, url, None, None, None, None, (), FetchFailure('request_limit', 'budget'))
        return FetchResult(url, url, 200, 'application/json', files['a.json'], datetime.now(timezone.utc), ())

    with patch('radar.discovery.capture.fetch_document', side_effect=fetch):
        result = capture_references(ROOT, root, DiscoveryBudget())
    assert result.rejection.code == 'capture_budget_exhausted' and result.documents == ()
    assert result.failure.documents_fetched == 1


def test_reference_processing_cap_is_a_backstop():
    files = {f'f{i}.json': j({}) for i in range(10)}
    root = doc(**{f'r{i}': {'$ref': f'f{i}.json'} for i in range(10)})
    result, _ = run(root, files, limits=CaptureLimits(max_references=4))
    assert result.rejection.code == 'reference_limit_exceeded' and 'to process' in result.rejection.reason
    assert run(root, files, limits=CaptureLimits(max_references=10))[0].ok


@pytest.mark.parametrize('kwargs', [{'max_documents': 0}, {'max_depth': -1}, {'max_total_nodes': True},
                                    {'max_references': 0},
                                    {'allow_cross_origin': 'yes'}])
def test_invalid_limits(kwargs):
    with pytest.raises(ValueError):
        CaptureLimits(**kwargs)


def test_a_failed_result_cannot_carry_documents():
    with pytest.raises(ValueError):
        CaptureResult(documents=(object(),), rejection=ValidationRejection('x', 'y'))
