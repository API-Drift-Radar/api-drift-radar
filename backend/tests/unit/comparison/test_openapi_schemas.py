"""The schema rules in both directions, and request-body behaviour."""

import copy

import pytest

from radar.comparison.openapi import compare_contracts
from radar.comparison.openapi.evaluation import build_package
from radar.comparison.openapi.references import Documents
from radar.comparison.openapi.schemas import MAX_CHANGES, SchemaRef, compare_schemas, schema_findings

URL = 'https://api.acme.com/openapi.json'
DOCS = Documents(build_package({'openapi': '3.0.3', 'info': {}, 'paths': {}}))
PB, NB, UN = 'potentially_breaking', 'non_breaking_under_rules', 'uncertain'


def obj(props, required=(), **extra):
    return {'type': 'object', 'properties': props, **({'required': list(required)} if required else {}), **extra}


def changes(before, after, direction):
    found, unclassified, truncated = compare_schemas(
        SchemaRef(DOCS, before, URL, ()), SchemaRef(DOCS, after, URL, ()), direction)
    return found, unclassified, truncated


def outcome(before, after, direction):
    found, *_ = changes(before, after, direction)
    return [(c.rule_id, c.category) for c in found]


STR, INT, NUM = {'type': 'string'}, {'type': 'integer'}, {'type': 'number'}
NULLABLE = {'type': 'string', 'nullable': True}
CAT_DOG = {'type': 'string', 'enum': ['cat', 'dog']}
CAT_DOG_BIRD = {'type': 'string', 'enum': ['cat', 'dog', 'bird']}
CAT_BIRD = {'type': 'string', 'enum': ['cat', 'bird']}


def prop(schema, required=False):
    return obj({'p': schema}, ['p'] if required else ())


# (name, before, after, expected category when SENT, expected category when RECEIVED, rule)
RULES = [
    ('type widened', prop(INT), prop(NUM), NB, PB, 'schema.type_changed'),
    ('type narrowed', prop(NUM), prop(INT), PB, NB, 'schema.type_changed'),
    ('type incompatible', prop(STR), prop(INT), PB, PB, 'schema.type_changed'),
    ('null allowed', prop(STR), prop(NULLABLE), NB, PB, 'schema.nullability_changed'),
    ('null forbidden', prop(NULLABLE), prop(STR), PB, NB, 'schema.nullability_changed'),
    ('enum value added', prop(CAT_DOG), prop(CAT_DOG_BIRD), NB, PB, 'schema.enum_changed'),
    ('enum value removed', prop(CAT_DOG_BIRD), prop(CAT_DOG), PB, NB, 'schema.enum_changed'),
    ('enum values swapped', prop(CAT_DOG), prop(CAT_BIRD), PB, PB, 'schema.enum_changed'),
    ('enum restriction added', prop(STR), prop(CAT_DOG), PB, NB, 'schema.enum_changed'),
    ('enum restriction removed', prop(CAT_DOG), prop(STR), NB, PB, 'schema.enum_changed'),
    ('became required', prop(STR), prop(STR, True), PB, NB, 'schema.required_changed'),
    ('became optional', prop(STR, True), prop(STR), NB, PB, 'schema.required_changed'),
    ('required property added', obj({}), prop(STR, True), PB, NB, 'schema.property_added'),
    ('optional property added', obj({}), prop(STR), NB, NB, 'schema.property_added'),
    ('property removed', prop(STR), obj({}), PB, PB, 'schema.property_removed'),
]


@pytest.mark.parametrize('name,before,after,sent,received,rule', RULES, ids=[r[0] for r in RULES])
def test_each_rule_in_both_directions(name, before, after, sent, received, rule):
    assert outcome(before, after, 'request') == [(rule, sent)]
    assert outcome(before, after, 'response') == [(rule, received)]


def test_the_reverse_of_a_change_for_requests_is_the_change_for_responses():
    """A request that accepts more is safe; a response that returns more is not. So the reverse of a request change
    classifies like the original change does for a response. (Property rules are not strict inverses.)"""
    checked = 0
    for name, before, after, sent, received, rule in RULES:
        if rule.startswith('schema.property') or sent == received:
            continue
        assert outcome(after, before, 'request') == [(rule, received)], name
        checked += 1
    assert checked >= 8


def test_a_response_property_added_to_a_closed_object_is_uncertain_not_safe():
    before, after = obj({}, additionalProperties=False), obj({'p': STR}, additionalProperties=False)
    assert outcome(before, after, 'response') == [('schema.property_added', UN)]
    assert 'did not allow extra properties' in changes(before, after, 'response')[0][0].explanation
    assert outcome(before, after, 'request') == [('schema.property_added', NB)]  # a client may omit an optional property


def test_read_only_and_write_only_properties_are_ignored_in_the_direction_where_they_do_not_apply():
    read_only = obj({'id': {'type': 'integer', 'readOnly': True}})
    write_only = obj({'secret': {'type': 'string', 'writeOnly': True}})
    assert outcome(obj({}), read_only, 'request') == [] and outcome(obj({}), read_only, 'response') != []
    assert outcome(obj({}), write_only, 'response') == [] and outcome(obj({}), write_only, 'request') != []


def test_nested_changes_carry_their_path():
    deep = lambda t: obj({'a': obj({'b': {'type': 'array', 'items': t}})})  # noqa: E731
    (change,), *_ = changes(deep(STR), deep(INT), 'request')
    assert change.steps == (('p', 'a'), ('p', 'b'), ('i',)) and change.rule_id == 'schema.type_changed'


def test_changes_below_an_incompatible_type_are_not_reported_separately():
    assert outcome(obj({'a': STR}), {'type': 'string'}, 'request') == [('schema.type_changed', PB)]


def test_booleans_and_numbers_are_different_enum_members():
    assert outcome({'enum': [1]}, {'enum': [True]}, 'request') == [('schema.enum_changed', PB)]


def test_constraints_and_unsupported_keywords_are_uncertain_and_block_safe_conclusions():
    found, unclassified, _ = changes(obj({'a': {'type': 'string', 'maxLength': 5}}), obj({'a': {'type': 'string', 'maxLength': 3}}), 'request')
    assert [(c.rule_id, c.category, c.keyword) for c in found] == [('schema.constraint_changed', UN, 'maxLength')] and unclassified
    unsupported = changes({'type': 'object', 'not': {}}, {'type': 'object', 'not': {'type': 'string'}}, 'request')[0]
    assert [(c.rule_id, c.category) for c in unsupported] == [('schema.unsupported_changed', UN)]


def test_a_safe_change_under_a_node_with_changed_constraints_is_downgraded():
    before = obj({'profile': obj({}, minProperties=1)})
    after = obj({'profile': obj({'nick': STR}, minProperties=2)})
    found, *_ = changes(before, after, 'request')
    categories = {c.rule_id: c.category for c in found}
    assert categories == {'schema.constraint_changed': UN, 'schema.property_added': UN}
    assert 'not confidently safe' in next(c for c in found if c.rule_id == 'schema.property_added').explanation


def test_boolean_schemas_are_compared_without_crashing():
    assert outcome(True, True, 'request') == []
    assert outcome(True, False, 'request') == [('schema.unsupported_changed', UN)]  # `false` accepts nothing


def test_a_runaway_number_of_differences_is_capped_and_reported():
    before = obj({f'p{i}': STR for i in range(MAX_CHANGES + 50)})
    after = obj({f'p{i}': INT for i in range(MAX_CHANGES + 50)})
    found, _, truncated = changes(before, after, 'request')
    assert len(found) == MAX_CHANGES and truncated
    limitations = []
    schema_findings('POST', '/p', '/p', 'request-body:application/json', 'request body', SchemaRef(DOCS, before, URL, ()),
                    SchemaRef(DOCS, after, URL, ()), 'request', limitations)
    assert any(l.code == 'schema_findings_truncated' for l in limitations)


# --- request bodies ------------------------------------------------------------------------------

def contract(**operation):
    return {'openapi': '3.0.3', 'info': {'title': 'T', 'version': '1'}, 'paths': {'/pets': {'post': {
        'responses': {'201': {'description': 'ok'}}, **operation}}}}


def compare(before, after):
    return compare_contracts(build_package(before), build_package(after))


def test_body_findings_say_what_and_where():
    before = contract(requestBody={'content': {'application/json': {'schema': obj({'a': STR})}}})
    after = contract(requestBody={'content': {'application/json': {'schema': obj({'a': INT})}}})
    (finding,) = compare(before, after).findings
    assert (finding.rule_id, finding.category, finding.direction) == ('schema.type_changed', PB, 'request')
    assert finding.subject == 'request body (application/json) > a'
    assert finding.before_location.pointer == '/paths/~1pets/post/requestBody/content/application~1json/schema/properties/a'
    assert finding.key == 'schema.type_changed|POST|/pets|request-body:application/json|/a'


def test_a_finding_inside_a_referenced_schema_points_into_the_component():
    def with_component(kind):
        c = contract(requestBody={'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Pet'}}}})
        c['components'] = {'schemas': {'Pet': obj({'a': kind})}}
        return c
    (finding,) = compare(with_component(STR), with_component(INT)).findings
    assert finding.after_location.pointer == '/components/schemas/Pet/properties/a'


def test_an_unreadable_request_body_is_a_limitation_and_the_operation_is_otherwise_compared():
    before = contract(requestBody={'content': {'application/json': {}}})
    after = contract(requestBody={'$ref': 'bodies.json#/Pet'})
    result = compare(before, after)
    assert result.findings == () and any(l.code == 'request_body_unresolved' and l.blocks_classification for l in result.limitations)


def test_a_media_type_without_a_schema_means_any_value():
    before = contract(requestBody={'content': {'application/json': {}}})
    after = contract(requestBody={'content': {'application/json': {'schema': obj({'a': STR})}}})
    assert [(f.rule_id, f.category) for f in compare(before, after).findings] == [
        ('schema.property_added', NB), ('schema.type_changed', PB)]  # any value before; now it must be an object


def test_bodies_are_deterministic_and_do_not_modify_their_inputs():
    before = contract(requestBody={'content': {'application/json': {'schema': obj({'a': STR, 'b': INT})}}})
    after = contract(requestBody={'content': {'application/json': {'schema': obj({'a': INT, 'c': STR})}}})
    snapshot = copy.deepcopy((before, after))
    first, second = compare(before, after), compare(before, after)
    assert first == second and (before, after) == snapshot
    assert [f.key.rsplit('|', 1)[-1] for f in first.findings] == ['/c', '/b', '/a']  # ordered by rule, then key
