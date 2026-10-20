"""Documentation, deprecation and operation-id changes."""

import copy

import pytest

from radar.comparison.openapi import compare_contracts
from radar.comparison.openapi.documentation import MAX_SHOWN, comparable, shown
from radar.comparison.openapi.evaluation import build_package

PB, NB, UN, DOC = 'potentially_breaking', 'non_breaking_under_rules', 'uncertain', 'documentation_only'
BASE = {'openapi': '3.0.3', 'info': {'title': 'T', 'version': '1'},
        'paths': {'/pets': {'get': {'summary': 'List', 'responses': {'200': {'description': 'ok'}}},
                            'post': {'summary': 'Create', 'responses': {'201': {'description': 'ok'}}}}}}


def compare(before, after):
    return compare_contracts(build_package(before), build_package(after))


def edited(**fields):
    contract = copy.deepcopy(BASE)
    contract['paths']['/pets']['get'].update(fields)
    return contract


def test_a_documentation_finding_says_what_changed_and_where():
    (finding,) = compare(BASE, edited(summary='List all pets')).findings
    assert (finding.rule_id, finding.category, finding.change_kind, finding.direction) == (
        'documentation.changed', DOC, 'changed', 'none')
    assert (finding.before_value, finding.after_value) == ('List', 'List all pets')
    assert finding.before_location.pointer == '/paths/~1pets/get/summary'
    assert finding.key == 'documentation.changed|GET|/pets|operation|summary' and finding.subject == 'GET /pets > summary'
    assert 'does not change what the API accepts' in finding.explanation


def test_added_and_removed_documentation_are_documented_as_such():
    added = compare(BASE, edited(description='More')).findings[0]
    removed = compare(edited(description='More'), BASE).findings[0]
    assert (added.change_kind, added.before_present, added.after_present) == ('added', False, True)
    assert (removed.change_kind, removed.before_present, removed.after_present) == ('removed', True, False)
    assert added.before_location is None and removed.after_location is None


@pytest.mark.parametrize('before,after', [('Returns every pet,\nnewest first.', 'Returns   every pet, newest first. '),
                                          ('a b', 'a  b'), ('x', 'x\n')])
def test_whitespace_and_wrapping_are_not_a_change(before, after):
    assert compare(edited(description=before), edited(description=after)).findings == ()
    assert comparable(before) == comparable(after)


def test_examples_compare_as_data_not_as_text():
    assert compare(edited(parameters=[{'name': 'q', 'in': 'query', 'example': {'a': 1, 'b': 2}}]),
                   edited(parameters=[{'name': 'q', 'in': 'query', 'example': {'b': 2, 'a': 1}}])).findings == ()
    assert comparable(1) != comparable(True) and comparable('1') != comparable(1)  # types are not confused


def test_deprecation_is_a_signal_not_a_break_in_either_direction():
    for before, after in ((BASE, edited(deprecated=True)), (edited(deprecated=True), BASE)):
        (finding,) = compare(before, after).findings
        assert (finding.rule_id, finding.category) == ('deprecation.changed', NB)
    assert compare(edited(deprecated=False), BASE).findings == ()  # false is the default


def test_an_operation_id_change_is_uncertain_because_generated_clients_use_it():
    (finding,) = compare(edited(operationId='a'), edited(operationId='b')).findings
    assert (finding.rule_id, finding.category) == ('operation.id_changed', UN)
    assert 'method name' in finding.explanation and finding.key == 'operation.id_changed|GET|/pets'


def test_a_path_item_shared_by_two_operations_is_reported_once():
    before, after = copy.deepcopy(BASE), copy.deepcopy(BASE)
    before['paths']['/pets']['description'] = 'a'
    after['paths']['/pets']['description'] = 'b'
    findings = compare(before, after).findings
    assert [f.key for f in findings] == ['documentation.changed||/pets|path-item|description']
    assert findings[0].method is None


def test_info_changes_are_documentation_and_the_version_label_says_so():
    after = copy.deepcopy(BASE)
    after['info'] = {'title': 'T2', 'version': '2'}
    findings = {f.key.rsplit('|', 1)[-1]: f for f in compare(BASE, after).findings}
    assert set(findings) == {'title', 'version'} and all(f.category == DOC for f in findings.values())
    assert 'does not by itself say anything about compatibility' in findings['version'].explanation
    assert findings['title'].method is None and findings['title'].path is None


def test_schema_documentation_points_at_the_annotation_in_the_defining_document():
    def contract(text):
        c = copy.deepcopy(BASE)
        c['components'] = {'schemas': {'Pet': {'type': 'object', 'description': text}}}
        c['paths']['/pets']['get']['responses']['200'] = {'description': 'ok', 'content': {
            'application/json': {'schema': {'$ref': '#/components/schemas/Pet'}}}}
        return c
    (finding,) = compare(contract('a'), contract('b')).findings
    assert finding.after_location.pointer == '/components/schemas/Pet/description'
    assert finding.subject == 'response 200 (application/json) > description'


def test_documentation_does_not_hide_or_downgrade_structural_findings():
    def contract(kind, text):
        c = copy.deepcopy(BASE)
        c['paths']['/pets']['post']['requestBody'] = {'content': {'application/json': {'schema': {
            'type': 'object', 'properties': {'n': {'type': kind, 'description': text}}}}}}
        return c
    result = compare(contract('integer', 'x'), contract('string', 'y'))
    assert sorted((f.rule_id, f.category) for f in result.findings) == [('documentation.changed', DOC), ('schema.type_changed', PB)]


def test_a_property_called_description_is_a_property_not_documentation():
    def contract(kind):
        c = copy.deepcopy(BASE)
        c['paths']['/pets']['post']['requestBody'] = {'content': {'application/json': {'schema': {
            'type': 'object', 'properties': {'description': {'type': kind}}}}}}
        return c
    assert [f.rule_id for f in compare(contract('string'), contract('integer')).findings] == ['schema.type_changed']


def test_long_documentation_is_bounded_in_the_finding_but_the_location_points_at_the_whole():
    long = 'x ' * 2000
    (finding,) = compare(BASE, edited(description=long)).findings
    assert len(finding.after_value) == MAX_SHOWN + 1 and finding.after_value.endswith('…')
    assert shown('short') == 'short' and shown(None) is None


def test_documentation_findings_are_deterministic_and_unique():
    after = edited(summary='B', description='C', externalDocs={'url': 'u'}, deprecated=True, operationId='x')
    first, second = compare(BASE, after), compare(BASE, after)
    assert first == second and len({f.key for f in first.findings}) == len(first.findings)
    assert [f.rule_id for f in first.findings].count('documentation.changed') == 3
