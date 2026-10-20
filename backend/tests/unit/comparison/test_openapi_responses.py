"""Response comparison (direction: response)."""

import copy

import pytest

from radar.comparison.openapi import compare_contracts, result_to_dict
from radar.comparison.openapi.evaluation import build_package

PB, NB, UN = 'potentially_breaking', 'non_breaking_under_rules', 'uncertain'
JSON = 'application/json'
PET = {'type': 'object', 'properties': {'id': {'type': 'integer'}, 'name': {'type': 'string'}}, 'required': ['id']}


def contract(responses):
    return {'openapi': '3.0.3', 'info': {'title': 'T', 'version': '1'}, 'paths': {'/pets': {'get': {'responses': responses}}}}


def ok(schema=PET, **extra):
    return {'200': {'description': 'ok', 'content': {JSON: {'schema': schema}}}, **extra}


def compare(before, after):
    return compare_contracts(build_package(before), build_package(after))


def summary(result):
    return [(f.rule_id, f.category) for f in result.findings]


def test_response_findings_say_what_where_and_in_which_direction():
    after = ok({**PET, 'required': ['id', 'name']})
    (finding,) = compare(contract(ok()), contract(after)).findings
    assert (finding.rule_id, finding.category, finding.direction) == ('schema.required_changed', NB, 'response')
    assert finding.subject == 'response 200 (application/json) > name'
    assert finding.key == 'schema.required_changed|GET|/pets|response:200:application/json|/name'
    assert finding.after_location.pointer == '/paths/~1pets/get/responses/200/content/application~1json/schema/properties/name'


def test_status_added_and_removed_are_classified_by_kind():
    base = contract(ok(**{'404': {'description': 'nf'}}))
    assert summary(compare(base, contract(ok()))) == [('response.removed', UN)]                  # an error branch
    assert summary(compare(base, contract({'404': {'description': 'nf'}}))) == [('response.removed', PB)]  # the success
    assert summary(compare(contract(ok()), contract(ok(**{'202': {'description': 'a'}})))) == [('response.added', UN)]


@pytest.mark.parametrize('status,expected', [('200', PB), ('204', PB), ('2XX', PB), ('301', UN), ('404', UN), ('500', UN),
                                             ('default', UN)])
def test_only_a_removed_2xx_is_potentially_breaking(status, expected):
    before = contract({status: {'description': 'x'}})
    after = contract({'418': {'description': 'x'}}) if status != '418' else contract({})
    (removed,) = [f for f in compare(before, after).findings if f.rule_id == 'response.removed']
    assert removed.category == expected and removed.key.endswith(f'response:{status.upper()}')


def test_a_media_type_removed_is_breaking_and_added_is_uncertain():
    both = contract({'200': {'description': 'ok', 'content': {JSON: {'schema': PET}, 'application/xml': {'schema': {'type': 'string'}}}}})
    only_json = contract(ok())
    assert summary(compare(both, only_json)) == [('response.media_removed', PB)]
    assert summary(compare(only_json, both)) == [('response.media_added', UN)]


def test_a_response_that_loses_its_whole_body_reports_the_media_type_as_removed():
    assert summary(compare(contract(ok()), contract({'200': {'description': 'ok'}}))) == [('response.media_removed', PB)]


def test_an_unreadable_response_is_a_limitation_and_the_operation_is_otherwise_compared():
    before = contract(ok())
    after = contract({'200': {'$ref': 'responses.json#/Pet'}})
    after['paths']['/pets']['get']['parameters'] = [{'name': 'q', 'in': 'query', 'required': True}]
    result = compare(before, after)
    assert summary(result) == [('parameter.added', PB)]
    assert any(l.code == 'response_unresolved' and l.blocks_classification for l in result.limitations)


def test_a_response_media_type_that_is_a_reference_is_a_limitation():
    after = contract({'200': {'description': 'ok', 'content': {JSON: {'$ref': '#/components/x'}}}})
    result = compare(contract(ok()), after)
    assert result.findings == () and any(l.code == 'schema_unresolved' for l in result.limitations)


def test_an_upgrade_that_changes_both_directions_is_classified_per_direction():
    ref = {'$ref': '#/components/schemas/Pet'}

    def shared(kind):
        c = {'openapi': '3.0.3', 'info': {}, 'components': {'schemas': {'Pet': {'type': 'object', 'properties': {'age': kind}}}},
             'paths': {'/pets': {'post': {'requestBody': {'content': {JSON: {'schema': ref}}},
                                          'responses': {'201': {'description': 'ok', 'content': {JSON: {'schema': ref}}}}}}}}
        return c
    result = compare(shared({'type': 'integer'}), shared({'type': 'number'}))
    assert {(f.direction, f.category) for f in result.findings} == {('request', NB), ('response', PB)}


def test_added_operations_are_not_also_reported_response_by_response():
    after = contract(ok())
    after['paths']['/owners'] = {'get': {'responses': ok()}}
    assert summary(compare(contract(ok()), after)) == [('operation.added', NB)]


def test_responses_are_deterministic_serializable_and_leave_inputs_untouched():
    before = contract(ok(**{'404': {'description': 'x'}}))
    after = contract(ok({**PET, 'required': ['id', 'name']}, **{'202': {'description': 'a'}}))
    snapshot = copy.deepcopy((before, after))
    first, second = compare(before, after), compare(before, after)
    assert first == second and (before, after) == snapshot
    assert [f['direction'] for f in result_to_dict(first)['findings']] == ['response'] * 3  # added 202, removed 404, required
