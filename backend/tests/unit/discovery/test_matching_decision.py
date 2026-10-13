import json

import pytest

from radar.discovery.input import normalize_target
from radar.discovery.matching import (
    GENERAL_LIMITATION, INDETERMINATE, MATCH, MISMATCH, NO_EVIDENCE, NOT_REQUESTED, MatchContext, MatchResult,
    assess_match, check_provenance,
)
from radar.discovery.validation import ValidationRejection, validate_document
from radar.domain.discovery import DiscoveryRequest


def contract(servers=('https://api.acme.com/v1',), title='Acme Payments API', **extra):
    document = {'openapi': '3.0.3', 'info': {'title': title, 'version': '1.0.0'},
                'paths': {'/customers': {'get': {}, 'post': {}}, '/customers/{id}': {'get': {}}}, **extra}
    if servers is not None:
        document['servers'] = [{'url': u} for u in servers]
    return validate_document(json.dumps(document).encode())


def context(target='api.acme.com', method=None, version=None, product=None, source='https://api.acme.com/openapi.json',
            how='common_location', via=None, **mapping):
    return MatchContext(normalize_target(DiscoveryRequest(target, method=method, api_version=version, product=product)),
                        source, how, via or source, **mapping)


def outcomes(result):
    return {c.criterion: c.outcome for c in result.checks}


# --- provenance --------------------------------------------------------------

@pytest.mark.parametrize('kwargs,outcome,words', [
    (dict(how='provider_mapping', source='https://raw.githubusercontent.com/x/spec.json',
          via='https://github.com/x/tree'), MATCH, 'explicitly lists'),
    (dict(source='https://api.acme.com/openapi.json'), MATCH, 'the same host as'),
    (dict(source='https://acme.com/openapi.json'), MATCH, 'a parent domain of'),
    (dict(source='https://eu.api.acme.com/spec'), MATCH, 'a subdomain of'),
    # A link alone is a recorded connection, not provenance: the target's own docs page linking to a contract
    # hosted elsewhere does not establish that the provider published it or that it applies.
    (dict(how='documentation_link', source='https://cdn.example.net/spec.json', via='https://api.acme.com/docs'),
     INDETERMINATE, 'A link alone does not establish ownership or applicability'),
    (dict(how='documentation_link', source='https://cdn.example.net/spec.json', via='https://docs.acme.com/api'),
     INDETERMINATE, 'does not establish'),  # a sibling subdomain is not related to api.acme.com
    (dict(how='swagger_ui_config', source='https://cdn.example.net/spec.json', via='https://api.acme.com/docs'),
     INDETERMINATE, 'the connection is recorded'),
    (dict(how='api_catalog', source='https://cdn.example.net/spec.json', via='https://api.acme.com/.well-known/api-catalog'),
     INDETERMINATE, 'A link alone does not establish ownership or applicability'),
    (dict(how='documentation_link', source='https://cdn.example.net/spec.json', via='https://blog.other.test/post'),
     INDETERMINATE, 'does not establish'),
    (dict(how='llm_suggestion', source='https://cdn.example.net/spec.json', via='https://docs.acme.com/api'),
     INDETERMINATE, 'does not establish'),
    (dict(source='https://evil-acme.com/openapi.json'), INDETERMINATE, 'does not establish'),
    (dict(how='common_location', source='https://other.test/openapi.json'), INDETERMINATE, 'does not establish'),
])
def test_provenance(kwargs, outcome, words):
    result = check_provenance(context(**kwargs))
    assert result.criterion == 'provenance' and result.outcome == outcome and words in result.description


def test_provenance_never_mismatches():
    for how in ('common_location', 'documentation_link', 'llm_suggestion', 'unknown', ''):
        assert check_provenance(context(how=how, source='https://x.other.test/s.json')).outcome != MISMATCH


# --- decision ----------------------------------------------------------------

def test_accepts_with_all_evidence_in_a_fixed_order():
    result = assess_match(context(method='GET', version='v1', product='payments',
                                  target='https://api.acme.com/v1/customers'), contract())
    assert result.accepted and result.rejection is None and isinstance(result, MatchResult)
    assert [c.criterion for c in result.checks] == ['server_host', 'operation', 'api_version', 'product', 'provenance']
    assert set(outcomes(result).values()) == {MATCH}
    assert result.positive == ('server_host', 'operation', 'api_version', 'product', 'provenance')
    assert result.limitations == (GENERAL_LIMITATION,)


def test_unrequested_hints_are_not_requested_and_do_not_block():
    result = assess_match(context(), contract())
    assert result.accepted
    assert outcomes(result) == {'server_host': MATCH, 'operation': NOT_REQUESTED, 'api_version': NOT_REQUESTED,
                                'product': NOT_REQUESTED, 'provenance': MATCH}


@pytest.mark.parametrize('kwargs,doc,code', [
    (dict(target='api.other.test', source='https://api.other.test/o.json'), {}, 'server_host_mismatch'),
    (dict(target='https://api.acme.com/v1/orders', method='GET'), {}, 'operation_not_found'),
    (dict(target='https://api.acme.com/v1/customers', method='DELETE'), {}, 'operation_not_found'),
    (dict(version='v2'), {}, 'version_mismatch'),
    (dict(product='shipping'), {}, 'product_mismatch'),
])
def test_any_mismatch_rejects_with_its_code(kwargs, doc, code):
    result = assess_match(context(**kwargs), contract(**doc))
    assert not result.accepted and result.rejection.code == code and result.rejection.stage == 'matching'
    assert isinstance(result.rejection, ValidationRejection) and len(result.checks) == 5
    assert GENERAL_LIMITATION in result.limitations


def test_the_first_mismatch_sets_the_code_and_all_are_explained():
    result = assess_match(context(target='https://api.acme.com/v1/orders', method='GET', version='v9', product='shipping'),
                          contract())
    assert result.rejection.code == 'operation_not_found'
    assert all(word in result.rejection.reason for word in ('No operation', 'v1', "'shipping'"))
    assert sorted(c.criterion for c in result.checks if c.outcome == MISMATCH) == ['api_version', 'operation', 'product']


def test_a_rejected_result_still_shows_what_passed():
    result = assess_match(context(product='shipping'), contract())
    assert outcomes(result)['server_host'] == MATCH and outcomes(result)['provenance'] == MATCH


def test_accepted_without_any_positive_evidence_is_flagged():
    relative = contract(servers=('/v1',))
    result = assess_match(context(source='https://cdn.example.net/spec.json', how='documentation_link',
                                  via='https://blog.other.test/post'), relative)
    assert result.accepted and result.positive == () and NO_EVIDENCE in result.limitations
    assert set(outcomes(result).values()) <= {INDETERMINATE, NOT_REQUESTED}


def test_an_accepted_match_with_evidence_is_not_flagged():
    assert NO_EVIDENCE not in assess_match(context(), contract()).limitations


def test_indeterminate_checks_do_not_reject():
    result = assess_match(context(method='GET', target='https://api.acme.com/customers', version='2026-01-01'),
                          contract(servers=('https://api.acme.com',)))
    assert result.accepted and outcomes(result)['api_version'] == INDETERMINATE


def test_only_validated_contracts_can_be_matched():
    with pytest.raises(ValueError):
        assess_match(context(), validate_document(b'<html>'))


def test_result_invariants():
    with pytest.raises(ValueError):
        MatchResult(True, (), ValidationRejection('x', 'y'))
    with pytest.raises(ValueError):
        MatchResult(False, ())


def test_matching_is_deterministic():
    one = assess_match(context(method='GET', target='https://api.acme.com/v1/customers', version='v1'), contract())
    two = assess_match(context(method='GET', target='https://api.acme.com/v1/customers', version='v1'), contract())
    assert one == two


# --- the GitHub-style disambiguation the design relies on --------------------

def mapping_candidate(version_label, hint=None):
    return assess_match(context(target='api.github.com', version=hint, how='provider_mapping',
                                source=f'https://raw.example.net/{version_label}.json', via='https://github.com/x',
                                mapping_api_version=version_label, mapping_product='GitHub REST API'),
                        contract(servers=('https://api.github.com',), title='GitHub v3 REST API'))


def test_without_a_version_hint_both_dated_candidates_are_accepted_so_the_caller_sees_ambiguity():
    assert mapping_candidate('2022-11-28').accepted and mapping_candidate('2026-03-10').accepted


def test_a_version_hint_rejects_only_the_other_candidate():
    assert mapping_candidate('2022-11-28', '2022-11-28').accepted
    other = mapping_candidate('2026-03-10', '2022-11-28')
    assert not other.accepted and other.rejection.code == 'version_mismatch'


def test_hostile_values_stay_bounded_in_reasons():
    result = assess_match(context(product='x' * 3000), contract(title='y' * 3000))
    assert not result.accepted and len(result.rejection.reason) < 900
