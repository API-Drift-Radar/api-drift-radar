"""Request-parameter comparison (ruleset parameters-1)."""

import copy
import json

from radar.comparison.openapi import compare_contracts, result_to_dict
from radar.comparison.openapi.evaluation import build_package
from radar.comparison.openapi.references import Documents, Unresolved

import pytest

CONTRACT = {'openapi': '3.0.3', 'info': {'title': 'T', 'version': '1'},
            'paths': {'/pets': {'get': {'parameters': [{'name': 'limit', 'in': 'query', 'schema': {'type': 'integer'}}]}},
                      '/pets/{id}': {'get': {'parameters': [{'name': 'id', 'in': 'path', 'required': True,
                                                             'schema': {'type': 'integer'}}]}}}}


def compare(before, after, **options):
    return compare_contracts(build_package(before, **options), build_package(after))


def edited(path=None, method='get', **changes):
    contract = copy.deepcopy(CONTRACT)
    if changes:
        contract['paths'][path][method].update(changes)
    return contract


def test_findings_say_which_parameter_where_and_in_which_direction():
    after = edited('/pets', parameters=[{'name': 'limit', 'in': 'query', 'required': True, 'schema': {'type': 'integer'}}])
    (finding,) = compare(CONTRACT, after).findings
    assert (finding.rule_id, finding.direction, finding.method, finding.path) == (
        'parameter.required_changed', 'request', 'GET', '/pets')
    assert finding.key == 'parameter.required_changed|GET|/pets|query:limit'
    assert (finding.before_value, finding.after_value) == (
        {'name': 'limit', 'in': 'query', 'required': False}, {'name': 'limit', 'in': 'query', 'required': True})
    assert finding.before_location.pointer == finding.after_location.pointer == '/paths/~1pets/get/parameters/0'
    assert 'may now be rejected' in finding.explanation


def test_a_path_parameter_is_matched_by_position_and_is_always_required():
    after = copy.deepcopy(CONTRACT)
    after['paths']['/pets/{petId}'] = after['paths'].pop('/pets/{id}')
    after['paths']['/pets/{petId}']['get']['parameters'] = [{'name': 'petId', 'in': 'path', 'schema': {'type': 'integer'}}]  # `required` omitted
    assert compare(CONTRACT, after).findings == ()


def test_parameters_of_an_added_or_removed_operation_are_not_reported_separately():
    after = copy.deepcopy(CONTRACT)
    del after['paths']['/pets/{id}']
    assert [f.rule_id for f in compare(CONTRACT, after).findings] == ['operation.removed']


def test_an_unreadable_parameter_skips_only_its_operation():
    after = edited('/pets', parameters=[{'name': 'limit'}])  # no location
    result = compare(CONTRACT, after)
    assert result.findings == ()
    limitation = next(l for l in result.limitations if l.code == 'parameter_unreadable')
    assert limitation.blocks_classification and limitation.scope == 'GET /pets'


def test_other_operations_are_still_compared_when_one_is_skipped():
    after = edited('/pets', parameters=[{'name': 'limit'}])
    after['paths']['/pets/{id}']['get']['parameters'].append({'name': 'expand', 'in': 'query', 'required': True})
    result = compare(CONTRACT, after)
    assert [f.key for f in result.findings] == ['parameter.added|GET|/pets/{}|query:expand']
    assert any(l.code == 'parameter_unreadable' for l in result.limitations)


def test_a_cyclic_parameter_reference_is_a_limitation_not_a_hang():
    after = copy.deepcopy(CONTRACT)
    after['components'] = {'parameters': {'A': {'$ref': '#/components/parameters/A'}}}
    after['paths']['/pets']['get']['parameters'] = [{'$ref': '#/components/parameters/A'}]
    result = compare(CONTRACT, after)
    assert result.findings == () and any(l.code == 'parameter_unresolved' for l in result.limitations)


def test_an_unreadable_schema_is_a_limitation_and_the_other_checks_still_run():
    after = edited('/pets', parameters=[{'name': 'limit', 'in': 'query', 'required': True,
                                          'schema': {'$ref': 'missing.json#/S'}}])
    result = compare(CONTRACT, after)
    assert [f.rule_id for f in result.findings] == ['parameter.required_changed']
    assert any(l.code == 'parameter_schema_unresolved' for l in result.limitations)


def test_a_schema_that_is_too_large_to_expand_is_reported_not_crashed():
    deep = {'type': 'integer'}
    for _ in range(40):
        deep = {'type': 'array', 'items': deep}
    after = edited('/pets', parameters=[{'name': 'limit', 'in': 'query', 'schema': deep}])
    result = compare(CONTRACT, after)
    assert result.findings == () and any(l.code == 'parameter_schema_unresolved' for l in result.limitations)


def test_boolean_and_number_enum_members_are_not_confused():
    before = edited('/pets', parameters=[{'name': 'f', 'in': 'query', 'schema': {'enum': [1, 2]}}])
    after = edited('/pets', parameters=[{'name': 'f', 'in': 'query', 'schema': {'enum': [True, 2]}}])
    assert [f.rule_id for f in compare(before, after).findings] == ['parameter.schema_changed']


def test_content_instead_of_schema_is_compared_too():
    before = edited('/pets', parameters=[{'name': 'f', 'in': 'query', 'content': {'application/json': {'schema': {'type': 'string'}}}}])
    after = edited('/pets', parameters=[{'name': 'f', 'in': 'query', 'content': {'application/json': {'schema': {'type': 'object'}}}}])
    assert [f.rule_id for f in compare(before, after).findings] == ['parameter.schema_changed']


def test_parameter_results_are_deterministic_and_serializable():
    after = edited('/pets', parameters=[{'name': 'a', 'in': 'query', 'required': True}, {'name': 'b', 'in': 'header'}])
    first, second = compare(CONTRACT, after), compare(CONTRACT, after)
    assert first == second
    assert [f.key.split('|')[-1] for f in first.findings] == ['header:b', 'query:a', 'query:limit']
    json.dumps(result_to_dict(first))


def test_coverage_now_includes_parameters_but_not_bodies_or_responses():
    coverage = compare(CONTRACT, copy.deepcopy(CONTRACT)).coverage
    assert 'parameters' in coverage.assessed and coverage.complete is False
    assert {'request bodies', 'response schemas'} <= set(coverage.not_assessed)


# --- reference expansion ---------------------------------------------------------------------------

def _docs(contract):
    return Documents(build_package(contract))


def test_expansion_replaces_references_and_drops_annotations_but_keeps_property_names():
    contract = {'openapi': '3.0.3', 'info': {}, 'paths': {}, 'components': {'schemas': {
        'Pet': {'type': 'object', 'description': 'A pet', 'properties': {'description': {'type': 'string', 'title': 'x'}}}}}}
    expanded = _docs(contract).expand({'$ref': '#/components/schemas/Pet'}, 'https://api.acme.com/openapi.json')
    assert expanded == {'type': 'object', 'properties': {'description': {'type': 'string'}}}


def test_expansion_cuts_a_cycle_with_a_deterministic_marker():
    contract = {'openapi': '3.0.3', 'info': {}, 'paths': {}, 'components': {'schemas': {
        'N': {'properties': {'next': {'$ref': '#/components/schemas/N'}}}}}}
    docs = _docs(contract)
    first = docs.expand({'$ref': '#/components/schemas/N'}, 'https://api.acme.com/openapi.json')
    assert first == docs.expand({'$ref': '#/components/schemas/N'}, 'https://api.acme.com/openapi.json')
    assert first['properties']['next'] == {'$cycle': 'https://api.acme.com/openapi.json/components/schemas/N'}


def test_expansion_reports_what_cannot_be_read():
    docs = _docs({'openapi': '3.0.3', 'info': {}, 'paths': {}})
    for ref in ('other.json#/S', '#/components/schemas/Missing', 'ftp://x/y', 5):
        with pytest.raises(Unresolved):
            docs.expand({'$ref': ref}, 'https://api.acme.com/openapi.json')
