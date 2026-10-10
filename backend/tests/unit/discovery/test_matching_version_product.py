import json

import pytest

from radar.discovery.input import normalize_target
from radar.discovery.matching import (
    INDETERMINATE, MATCH, MISMATCH, NOT_REQUESTED, check_api_version, check_product, versions_agree,
)
from radar.discovery.validation import validate_document
from radar.domain.discovery import DiscoveryRequest


SOURCE = 'https://specs.acme.com/openapi.json'


def spec(servers=None, info_version='1.0.0', title='Acme Payments API', **extra):
    info = {'title': title, 'version': info_version, **extra.pop('info', {})}
    document = {'openapi': '3.0.3', 'info': info, 'paths': {'/x': {'get': {}}}, **extra}
    if servers is not None:
        document['servers'] = [{'url': u} if isinstance(u, str) else u for u in servers]
    return document


def validated(document):
    result = validate_document(json.dumps(document).encode())
    assert result.ok, result.rejection
    return result


def version(hint, document=None, mapping=None, host='api.acme.com'):
    result = validated(document or spec(['https://api.acme.com']))
    target = normalize_target(DiscoveryRequest(host, api_version=hint))
    return check_api_version(target, result.summary, result.document, SOURCE, mapping_api_version=mapping)


def product(hint, document=None, mapping=None):
    result = validated(document or spec(['https://api.acme.com']))
    target = normalize_target(DiscoveryRequest('api.acme.com', product=hint))
    return check_product(target, result.summary, result.document, SOURCE, mapping_product=mapping)


# --- version comparison ------------------------------------------------------

@pytest.mark.parametrize('a,b,expected', [
    ('v1', 'v1', True), ('V1', 'v1', True), ('1', 'v1', True), ('v2', '2.1.0', True), ('2.1', 'v2', True),
    ('2026-09-30', '2026-09-30.endive', True), ('2022-11-28', '2022-11-28', True),
    ('1', 'v10', False), ('1', '1.5', True), ('10', '1', False), ('2022-11-28', '2026-03-10', False),
    ('2026-09', '2026-09-30', False), ('latest', 'latest', True), ('latest', 'v1', False), ('v1beta', 'v1', False),
    (' v1 ', 'v1', True),
])
def test_versions_agree(a, b, expected):
    assert versions_agree(a, b) is expected


# --- API version check -------------------------------------------------------

def test_no_hint_is_not_requested():
    result = version(None)
    assert (result.criterion, result.outcome, result.source_url) == ('api_version', NOT_REQUESTED, SOURCE)


def test_provider_mapping_version_is_authoritative():
    assert version('2022-11-28', mapping='2022-11-28').outcome == MATCH
    result = version('2026-03-10', mapping='2022-11-28')
    assert result.outcome == MISMATCH and '2022-11-28' in result.description and 'contradicts' in result.description
    # info.version does not rescue a contradicted mapping
    assert version('2026-03-10', spec(['https://api.acme.com'], info_version='2026-03-10'), mapping='2022-11-28').outcome == MISMATCH
    assert version('latest', mapping='stable').outcome == MISMATCH


def test_mapping_beats_a_server_path_that_would_have_matched():
    assert version('v1', spec(['https://api.acme.com/v1']), mapping='v2').outcome == MISMATCH


@pytest.mark.parametrize('hint,path,expected', [
    ('v1', '/v1', MATCH), ('1', '/v1', MATCH), ('v1', '/api/v1/', MATCH), ('2', '/v2', MATCH), ('2.1', '/v2', MATCH),
    ('v2', '/v1', MISMATCH), ('1', '/v10', MISMATCH), ('2022-11-28', '/2022-11-28', MATCH),
    ('2022-11-28', '/2023-01-01', MISMATCH), ('v1beta', '/v1beta', MATCH), ('v1', '/v1beta', MISMATCH),
    ('latest', '/v1', INDETERMINATE),  # a non-version hint cannot be contradicted by a path
])
def test_server_path_versions(hint, path, expected):
    assert version(hint, spec([f'https://api.acme.com{path}'])).outcome == expected


def test_any_matching_server_path_supports_the_hint():
    assert version('v2', spec(['https://api.acme.com/v1', 'https://api.acme.com/v2'])).outcome == MATCH


def test_a_server_path_version_beats_a_coincidental_info_version():
    # info.version 1.0.0 is a common default; it must not make hint "1" fit a /v10 API.
    assert version('1', spec(['https://api.acme.com/v10'], info_version='1.0.0')).outcome == MISMATCH
    assert version('v2', spec(['https://api.acme.com/v1'], info_version='2.4.0')).outcome == MISMATCH
    assert version('v2', spec(['https://api.acme.com/v1', 'https://api.acme.com/v2'], info_version='1.0.0')).outcome == MATCH


def test_info_version_supports_the_hint_only_when_paths_are_silent():
    assert version('2', spec(['https://api.acme.com'], info_version='2.4.0')).outcome == MATCH
    # a non-version hint is never compared with paths, so info.version may still support it
    assert version('latest', spec(['https://api.acme.com/v1'], info_version='latest')).outcome == MATCH


def test_info_version_alone_never_contradicts():
    assert version('3', spec(['https://api.acme.com'], info_version='1.0.0')).outcome == INDETERMINATE
    assert version('2026-09-30', spec(['https://api.acme.com'], info_version='2026-09-30.endive')).outcome == MATCH
    assert version('2024-01-01', spec(['https://api.acme.com'], info_version='2026-09-30.endive')).outcome == INDETERMINATE


def test_indeterminate_explains_that_info_version_is_not_the_provider_version():
    assert "document's own version" in version('7').description


def test_servers_on_unrelated_hosts_are_ignored():
    document = spec(['https://api.other.test/v9', 'https://api.acme.com'])
    assert version('v9', document).outcome == INDETERMINATE


def test_variable_paths_are_expanded_and_unknown_ones_block_a_contradiction():
    document = spec([{'url': 'https://api.acme.com/{v}', 'variables': {'v': {'default': 'v1', 'enum': ['v1', 'v2']}}}])
    assert version('v2', document).outcome == MATCH and version('v3', document).outcome == MISMATCH
    unknown = spec(['https://api.acme.com/v1', 'https://api.acme.com/{unknown}'])
    assert version('v2', unknown).outcome == INDETERMINATE


def test_relative_server_paths_count():
    assert version('v1', spec(['/v1'])).outcome == MATCH


def test_hostile_values_are_bounded():
    result = version('x' * 5000, spec(['https://api.acme.com/v1'], info_version='1' * 5000))
    assert len(result.description) < 500


# --- product check -----------------------------------------------------------

def test_no_product_is_not_requested():
    assert product(None).outcome == NOT_REQUESTED


@pytest.mark.parametrize('hint', ['payments', 'Payments', 'PAYMENTS API', 'acme payments', 'payment', 'Payment API'])
def test_product_found_in_the_title(hint):
    result = product(hint)
    assert result.outcome == MATCH and result.criterion == 'product' and 'title' in result.description


def test_product_found_in_other_text():
    assert 'description' in product('billing', spec(['https://api.acme.com'], info={'description': 'Billing and invoices.'})).description
    assert 'server URL' in product('sandbox', spec(['https://sandbox.api.acme.com'])).description
    assert 'tag' in product('refunds', spec(['https://api.acme.com'], tags=[{'name': 'Refunds'}])).description
    assert 'provider mapping' in product('checkout', mapping='Checkout API').description


@pytest.mark.parametrize('hint', ['pay', 'billing', 'ments', 'payments api v2', 'paymen'])
def test_product_not_in_the_contract_is_a_mismatch(hint):
    result = product(hint)
    assert result.outcome == MISMATCH and 'Acme Payments API' in result.description


def test_punctuation_case_and_plurals_fold():
    document = spec(['https://api.acme.com'], title='E-Commerce Orders')
    assert product('e commerce order', document).outcome == MATCH and product('orders', document).outcome == MATCH
    assert product('Ecommerce', document).outcome == MISMATCH  # different tokenisation: a documented limit


def test_words_must_be_whole_and_in_order():
    document = spec(['https://api.acme.com'], title='Acme Payments Gateway')
    assert product('payments gateway', document).outcome == MATCH
    assert product('gateway payments', document).outcome == MISMATCH
    assert product('gate', document).outcome == MISMATCH


def test_odd_documents_do_not_break_the_check():
    base = spec(['https://api.acme.com'])
    for extra in ({'tags': 'x'}, {'tags': [1, None, {'name': 3}]}, {'info': {'description': 42}}):
        assert product('widgets', {**base, **extra} if 'info' not in extra else {**base, 'info': {**base['info'], **extra['info']}}).outcome == MISMATCH


def test_only_the_start_of_a_huge_description_is_searched():
    document = spec(['https://api.acme.com'], info={'description': 'x ' * 4000 + 'needle'})
    assert product('needle', document).outcome == MISMATCH


def test_checks_are_deterministic():
    assert version('v1', spec(['https://api.acme.com/v1'])) == version('v1', spec(['https://api.acme.com/v1']))
    assert product('payments') == product('payments')
