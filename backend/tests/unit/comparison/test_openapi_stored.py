"""Normalization and the stored comparison: what storage (#10) calls."""

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from radar.comparison.openapi import compare_contracts
from radar.comparison.openapi.evaluation import FIXTURES, HOLDOUT, build_package, load_cases
from radar.comparison.openapi.stored import (
    NORMALIZATION_VERSION, compare_stored, location_text, normalize_contract, package_from_normalized, to_change_input,
)
from radar.domain.comparison import ContractFinding
from radar.domain.discovery import CapturedDocument
from radar.storage.records import ABSENT, CLASSIFICATIONS, DIRECTIONS, ChangeInput

CONTRACT = {'openapi': '3.0.3', 'info': {'title': 'T', 'version': '1'},
            'paths': {'/pets': {'get': {'summary': 'List', 'responses': {'200': {'description': 'ok'}}}}}}


def stored(package):
    normalized, version = normalize_contract(package)
    return SimpleNamespace(normalized=normalized, normalization_version=version)


# --- normalization ---------------------------------------------------------------------------------

def test_normalization_is_canonical_independent_of_key_order_and_retrieval_time():
    reordered = {'paths': CONTRACT['paths'], 'info': {'version': '1', 'title': 'T'}, 'openapi': '3.0.3'}
    a, b = build_package(CONTRACT), build_package(reordered)
    later = CapturedDocument(b.root_document.source_url, b.root_document.content, b.root_document.retrieved_at + timedelta(days=9),
                             'application/yaml')
    b = type(b)(b.candidate, later, b.referenced_documents, b.parsed_contract, b.openapi_version)
    assert normalize_contract(a) == normalize_contract(b) and normalize_contract(a)[1] == NORMALIZATION_VERSION


def test_a_real_change_changes_the_bytes_and_so_does_a_referenced_document():
    changed = json.loads(json.dumps(CONTRACT))
    changed['paths']['/pets']['get']['summary'] = 'List all'
    assert normalize_contract(build_package(CONTRACT))[0] != normalize_contract(build_package(changed))[0]
    ref = 'https://api.acme.com/s.json'
    one = build_package(CONTRACT, referenced={ref: {'type': 'string'}})
    two = build_package(CONTRACT, referenced={ref: {'type': 'integer'}})
    assert normalize_contract(one)[0] != normalize_contract(two)[0]


def test_the_package_is_not_modified_and_normalizing_twice_gives_the_same_bytes():
    package = build_package(CONTRACT)
    before = json.dumps(package.parsed_contract, sort_keys=True)
    assert normalize_contract(package) == normalize_contract(package)
    assert json.dumps(package.parsed_contract, sort_keys=True) == before


def test_an_unparsable_referenced_document_is_left_out_like_an_uncaptured_one():
    package = build_package(CONTRACT)
    broken = CapturedDocument('https://api.acme.com/bad.json', b'{"a": ', package.root_document.retrieved_at, 'application/json')
    with_broken = type(package)(package.candidate, package.root_document, (broken,), package.parsed_contract, package.openapi_version)
    assert normalize_contract(with_broken) == normalize_contract(package)


@pytest.mark.parametrize('bad', [b'not json', b'[]', b'{"format": "other"}',
                                 json.dumps({'format': 'radar.openapi.normalized', 'normalization_version': 'old', 'root': 'x', 'documents': {}}).encode(),
                                 json.dumps({'format': 'radar.openapi.normalized', 'normalization_version': NORMALIZATION_VERSION, 'root': 'x', 'documents': {}}).encode()])
def test_normalized_bytes_that_are_not_ours_are_refused_clearly(bad):
    with pytest.raises(ValueError):
        package_from_normalized(bad)
    with pytest.raises(ValueError):
        compare_stored(SimpleNamespace(normalized=bad, normalization_version='x'), normalize_contract(build_package(CONTRACT))[0], NORMALIZATION_VERSION)


def test_an_unsupported_version_argument_is_refused():
    reference = stored(build_package(CONTRACT))
    with pytest.raises(ValueError):
        compare_stored(reference, reference.normalized, 'something-else')


# --- the stored comparison agrees with the direct one ---------------------------------------------

@pytest.mark.parametrize('root,minimum', [(FIXTURES, 150), (HOLDOUT, 22)], ids=['development', 'held-out'])
def test_comparing_stored_forms_gives_the_same_findings_as_comparing_packages(root, minimum):
    """Every labeled case, rebuilt from normalized bytes only, yields the same findings, in the same order."""
    checked = 0
    for case in load_cases(root):
        direct = compare_contracts(case.before, case.after)
        changes, limitations = compare_stored(stored(case.before), *normalize_contract(case.after))
        assert [c.details['key'] for c in changes] == [f.key for f in direct.findings], case.name
        assert [c.details['rule'] for c in changes] == [f.rule_id for f in direct.findings], case.name
        codes = {l['code'] for l in limitations}
        assert {l.code for l in direct.limitations} - {'directory_copy_source', 'unconfirmed_contract'} <= codes, case.name
        checked += 1
    assert checked >= minimum


# --- the mapping to storage's ChangeInput ----------------------------------------------------------

def finding(**kw):
    base = dict(key='k', rule_id='operation.removed', category='potentially_breaking', change_kind='removed', direction='none',
                method='GET', path='/pets', before_present=True, after_present=False,
                explanation='GET /pets is no longer declared. Existing clients calling this operation may be affected.',
                before_value={'method': 'GET', 'path': '/pets'})
    base.update(kw)
    return ContractFinding(**base)


def test_a_finding_maps_to_a_change_storage_accepts():
    change = to_change_input(finding())
    assert isinstance(change, ChangeInput)
    assert (change.change_type, change.location, change.classification, change.direction) == (
        'operation_removed', 'GET /pets', 'potentially_breaking', None)
    assert change.summary == 'GET /pets is no longer declared.'
    assert change.rationale == 'Existing clients calling this operation may be affected.'
    assert change.before == {'method': 'GET', 'path': '/pets'} and change.after is ABSENT
    assert change.details['rule'] == 'operation.removed' and change.details['key'] == 'k'


def test_names_and_directions_use_storages_vocabulary():
    assert to_change_input(finding(category='non_breaking_under_rules')).classification == 'non_breaking'
    for category in ('potentially_breaking', 'documentation_only', 'uncertain'):
        assert to_change_input(finding(category=category)).classification == category
    assert to_change_input(finding(direction='request')).direction == 'request'
    assert to_change_input(finding(direction='response')).direction == 'response'
    assert {to_change_input(finding(direction=d)).direction for d in ('request', 'response', 'none')} <= DIRECTIONS | {None}


def test_absent_and_null_stay_apart():
    added = to_change_input(finding(change_kind='added', before_present=False, after_present=True, before_value=None, after_value={'required': True}))
    assert added.before is ABSENT and added.after == {'required': True}
    null_default = to_change_input(finding(rule_id='schema.constraint_changed', change_kind='changed', before_present=True,
                                           after_present=True, before_value=None, after_value=0))
    assert null_default.before is None and null_default.after == 0  # present, and null


def test_locations_read_like_a_path_through_the_contract():
    assert location_text(finding()) == 'GET /pets'
    assert location_text(finding(subject='response 200 (application/json) > name')) == 'GET /pets > response 200 (application/json) > name'
    assert location_text(finding(subject='GET /pets > summary')) == 'GET /pets > summary'
    assert location_text(finding(method=None, path=None, subject="the contract's info > title")) == "contract > the contract's info > title"
    assert len(location_text(finding(subject='x' * 9000))) == 4000


def test_servers_and_parameters_read_clearly_in_a_location():
    from radar.comparison.openapi.evaluation import load_cases as cases
    named = {c.name: c for c in cases(FIXTURES)}
    servers = compare_contracts(named['servers_changed'].before, named['servers_changed'].after).findings[0]
    assert location_text(servers) == 'contract > servers'
    parameter = compare_contracts(named['param_became_required'].before, named['param_became_required'].after).findings[0]
    assert location_text(parameter) == 'GET /pets > query parameter limit'


@pytest.mark.parametrize('root,minimum', [(FIXTURES, 130), (HOLDOUT, 30)], ids=['development', 'held-out'])
def test_every_finding_in_every_fixture_maps_to_a_valid_change(root, minimum):
    from radar.storage.repository import _check_change
    count = 0
    for case in load_cases(root):
        for index, f in enumerate(compare_contracts(case.before, case.after).findings):
            change = to_change_input(f)
            assert change.classification in CLASSIFICATIONS
            _check_change(change, index)  # storage's own validation: text limits, JSON-only values, known vocabulary
            count += 1
    assert count >= minimum
