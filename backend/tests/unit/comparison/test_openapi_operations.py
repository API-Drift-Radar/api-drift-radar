"""Operation-level comparison of two validated contract packages (ruleset operations-1)."""

import copy
import json

import pytest

from radar.comparison.openapi import RULESET_VERSION, compare_contracts, result_to_dict
from radar.comparison.openapi.evaluation import Report, build_package, evaluate, load_cases
from radar.comparison.openapi.operations import normalize_path
from radar.domain.comparison import (
    CATEGORIES, ComparisonCoverage, ContractFinding, SourceLocation,
)

PETS = {'openapi': '3.0.3', 'info': {'title': 'T', 'version': '1'}, 'servers': [{'url': 'https://api.acme.com/v1'}],
        'paths': {'/pets': {'get': {}, 'post': {}}, '/pets/{id}': {'get': {}, 'delete': {}}}}


def compare(before, after, **options):
    return compare_contracts(build_package(before, **options), build_package(after))


def without(contract, path, method):
    changed = copy.deepcopy(contract)
    del changed['paths'][path][method]
    return changed


# --- the labeled fixtures are the acceptance gate -------------------------------------------------

def test_every_labeled_fixture_matches_exactly():
    cases = load_cases()
    report = evaluate(cases)
    print(f'\n{report.cases} cases | precision {report.precision} | recall {report.recall} | '
          f'classification accuracy {report.classification_accuracy}')
    assert report.failures == []
    assert (report.precision, report.recall, report.classification_accuracy) == (1.0, 1.0, 1.0)
    assert len(cases) >= 85 and report.expected >= 65


def test_the_fixtures_cover_every_rule():
    wanted = {rule for case in load_cases() for rule, *_ in case.expected_findings}
    assert wanted == {'operation.removed', 'operation.added', 'servers.changed', 'parameter.added', 'parameter.removed',
                      'parameter.required_changed', 'parameter.schema_changed', 'parameter.encoding_changed',
                      'request_body.added', 'request_body.removed', 'request_body.required_changed',
                      'request_body.media_added', 'request_body.media_removed', 'schema.type_changed',
                      'schema.nullability_changed', 'schema.enum_changed', 'schema.property_added',
                      'schema.property_removed', 'schema.required_changed', 'schema.constraint_changed',
                      'schema.unsupported_changed', 'schema.access_changed'}
    assert any(not case.expected_findings for case in load_cases())  # unchanged contracts are covered


def test_the_harness_reports_misses_unexpected_findings_and_misclassifications():
    def nothing(before, after):
        return compare_contracts(build_package(PETS), build_package(PETS))

    def wrong(before, after):
        result = compare_contracts(before, after)
        return type(result)(tuple(type(f)(**{**f.__dict__, 'category': 'uncertain'}) for f in result.findings),
                            result.limitations, result.coverage, result.ruleset_version)

    cases = load_cases()
    missed = evaluate(cases, compare=nothing)
    assert missed.recall < 1.0 and any('missed' in f for f in missed.failures)
    assert evaluate(cases, compare=wrong).classification_accuracy < 1.0
    assert evaluate([], compare=nothing) == Report(cases=0) and Report().precision is None  # N/A, not 1.0


# --- behaviour ------------------------------------------------------------------------------------

def test_a_removed_operation_is_potentially_breaking_with_its_location():
    result = compare(PETS, without(PETS, '/pets/{id}', 'delete'))
    (finding,) = result.findings
    assert (finding.rule_id, finding.category, finding.method, finding.path) == (
        'operation.removed', 'potentially_breaking', 'DELETE', '/pets/{id}')
    assert (finding.before_present, finding.after_present, finding.direction) == (True, False, 'none')
    assert finding.before_location == SourceLocation('https://api.acme.com/openapi.json', '/paths/~1pets~1{id}/delete')
    assert finding.after_location is None and 'no longer declared' in finding.explanation


def test_an_added_operation_is_non_breaking_under_rules():
    result = compare(without(PETS, '/pets', 'post'), PETS)
    (finding,) = result.findings
    assert (finding.rule_id, finding.category, finding.method, finding.path) == (
        'operation.added', 'non_breaking_under_rules', 'POST', '/pets')
    assert finding.after_location.pointer == '/paths/~1pets/post' and finding.before_location is None


def test_the_path_is_reported_as_declared_where_the_operation_exists():
    renamed = copy.deepcopy(PETS)
    renamed['paths']['/pets/{petId}'] = renamed['paths'].pop('/pets/{id}')
    assert compare(PETS, renamed).findings == ()
    removed = without(renamed, '/pets/{petId}', 'delete')
    assert compare(renamed, removed).findings[0].path == '/pets/{petId}'


def test_findings_are_ordered_and_keyed_deterministically():
    after = copy.deepcopy(PETS)
    after['paths'] = {'/z': {'get': {}}, '/a': {'get': {}, 'post': {}}, '/pets': {'get': {}}}
    result = compare(PETS, after)
    assert [(f.path, f.method, f.rule_id) for f in result.findings] == [
        ('/a', 'GET', 'operation.added'), ('/a', 'POST', 'operation.added'), ('/pets', 'POST', 'operation.removed'),
        ('/pets/{id}', 'GET', 'operation.removed'), ('/pets/{id}', 'DELETE', 'operation.removed'),  # OpenAPI method order
        ('/z', 'GET', 'operation.added')]
    assert result.findings[4].key == 'operation.removed|DELETE|/pets/{}'
    assert len({f.key for f in result.findings}) == len(result.findings)


def test_repeated_comparisons_are_identical_and_inputs_are_not_modified():
    before, after = build_package(PETS), build_package(without(PETS, '/pets', 'get'))
    snapshot = copy.deepcopy((before.parsed_contract, after.parsed_contract))
    first, second = compare_contracts(before, after), compare_contracts(before, after)
    assert first == second and result_to_dict(first) == result_to_dict(second)
    assert (before.parsed_contract, after.parsed_contract) == snapshot


def test_insertion_order_of_mappings_does_not_change_the_result():
    shuffled = {'paths': dict(reversed(list(PETS['paths'].items()))), 'servers': PETS['servers'],
                'info': PETS['info'], 'openapi': PETS['openapi']}
    changed = without(PETS, '/pets', 'post')
    assert compare(PETS, changed) == compare(shuffled, changed)


def test_no_findings_never_means_the_whole_contract_is_unchanged():
    result = compare(PETS, copy.deepcopy(PETS))
    assert result.findings == () and result.coverage.complete is False
    assert 'response schemas' in result.coverage.not_assessed and 'documentation (descriptions, examples, deprecation)' in result.coverage.not_assessed
    assert {'parameters', 'request bodies'} <= set(result.coverage.assessed)
    scope = next(l for l in result.limitations if l.code == 'scope_not_assessed')
    assert 'does not mean the rest of the contract is unchanged' in scope.explanation


def test_a_changed_servers_list_is_uncertain_and_carries_both_values():
    after = copy.deepcopy(PETS)
    after['servers'] = [{'url': 'https://api.acme.com/v2'}]
    (finding,) = compare(PETS, after).findings
    assert (finding.rule_id, finding.category, finding.key) == ('servers.changed', 'uncertain', 'servers.changed')
    assert (finding.before_value, finding.after_value) == (['https://api.acme.com/v1'], ['https://api.acme.com/v2'])
    assert finding.before_location.pointer == '/servers'


def test_server_variables_count_as_a_change():
    before, after = copy.deepcopy(PETS), copy.deepcopy(PETS)
    before['servers'] = [{'url': 'https://{region}.acme.com', 'variables': {'region': {'default': 'eu'}}}]
    after['servers'] = [{'url': 'https://{region}.acme.com', 'variables': {'region': {'default': 'us'}}}]
    assert [f.rule_id for f in compare(before, after).findings] == ['servers.changed']


def test_a_path_item_reference_to_an_uncaptured_document_is_not_a_removal():
    after = copy.deepcopy(PETS)
    after['paths']['/pets'] = {'$ref': 'paths/pets.json'}
    result = compare(PETS, after)
    assert result.findings == ()
    limitation = next(l for l in result.limitations if l.code == 'path_item_unresolved')
    assert limitation.blocks_classification and limitation.scope == '/pets'


def test_a_local_path_item_reference_is_followed():
    before = copy.deepcopy(PETS)
    before['components'] = {'pathItems': {'Pets': {'get': {}, 'post': {}}}}
    before['paths']['/pets'] = {'$ref': '#/components/pathItems/Pets'}
    assert compare(before, PETS).findings == ()  # same operations, found through the reference
    (finding,) = compare(before, without(PETS, '/pets', 'post')).findings
    assert finding.before_location.pointer == '/components/pathItems/Pets/post'  # located where it is defined


def test_3_1_merges_reference_siblings_but_3_0_ignores_them():
    for version, expected in (('3.1.0', ['operation.added']), ('3.0.3', [])):
        contract = copy.deepcopy(PETS)
        contract['openapi'] = version
        contract['components'] = {'pathItems': {'Pets': {'get': {}}}}
        contract['paths'] = {'/pets': {'$ref': '#/components/pathItems/Pets', 'post': {}}}
        plain = copy.deepcopy(contract)
        plain['paths'] = {'/pets': {'get': {}}}
        assert [f.rule_id for f in compare(plain, contract).findings] == expected


def test_normalize_path_leaves_everything_but_parameter_names_alone():
    assert normalize_path('/a/{x}/b/{y}') == '/a/{}/b/{}'
    assert normalize_path('/a/') != normalize_path('/a') and normalize_path('/A') != normalize_path('/a')


# --- models and serialization ---------------------------------------------------------------------

def test_the_result_serializes_to_plain_json():
    result = compare(PETS, without(PETS, '/pets/{id}', 'delete'))
    document = json.loads(json.dumps(result_to_dict(result)))
    assert document['ruleset_version'] == RULESET_VERSION == 'request-schemas-1'
    assert document['coverage']['complete'] is False
    assert document['findings'][0]['before_location'] == {
        'document_url': 'https://api.acme.com/openapi.json', 'pointer': '/paths/~1pets~1{id}/delete'}
    assert document['findings'][0]['after_location'] is None and document['findings'][0]['after_present'] is False


def test_the_four_agreed_categories_exist_and_findings_reject_unknown_values():
    assert CATEGORIES == ('potentially_breaking', 'non_breaking_under_rules', 'documentation_only', 'uncertain')
    base = dict(key='k', rule_id='r', category='uncertain', change_kind='changed', direction='none', method=None,
                path=None, before_present=True, after_present=True, explanation='x')
    ContractFinding(**base)
    for field, value in (('category', 'bad'), ('change_kind', 'bad'), ('direction', 'bad')):
        with pytest.raises(ValueError):
            ContractFinding(**{**base, field: value})
    with pytest.raises(ValueError):
        ContractFinding(**{**base, 'before_present': False, 'after_present': False})
    assert ComparisonCoverage(('a',), ()).complete and not ComparisonCoverage(('a',), ('b',)).complete


# --- the example for the storage and API owners ---------------------------------------------------

@pytest.mark.parametrize('fixture,filename', [('mixed_changes', 'mixed-changes.json'),
                                               ('body_property_required_added', 'request-body-property-added.json')])
def test_the_documented_examples_match_what_the_code_produces(fixture, filename):
    """docs/examples/comparison/*.json; regenerate deliberately with UPDATE_SAMPLES=1."""
    import os
    from pathlib import Path
    case = next(c for c in load_cases() if c.name == fixture)
    text = json.dumps({'description': case.rationale, 'result': result_to_dict(compare_contracts(case.before, case.after))},
                      indent=2, ensure_ascii=False) + '\n'
    path = Path(__file__).resolve().parents[4] / 'docs' / 'examples' / 'comparison' / filename
    if os.environ.get('UPDATE_SAMPLES') == '1':
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    assert path.read_text(encoding='utf-8') == text, 'stale; run with UPDATE_SAMPLES=1'
