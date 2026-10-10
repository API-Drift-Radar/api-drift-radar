import json
import time

import pytest

from radar.discovery.validation import (
    ContractSummary, ValidationLimits, ValidationRejection, ValidationResult, validate_document,
)


def spec(**changes):
    document = {'openapi': '3.0.3', 'info': {'title': 'Acme', 'version': '1.0.0'},
                'servers': [{'url': 'https://api.acme.com/v1'}],
                'paths': {'/pets': {'get': {}, 'post': {}}, '/pets/{id}': {'get': {}, 'parameters': []}}}
    document.update(changes)
    return {key: value for key, value in document.items() if value is not None}


def check(document):
    content = document if isinstance(document, bytes) else json.dumps(document).encode()
    return validate_document(content)


def rejected(content, code, location=None):
    result = validate_document(content if isinstance(content, bytes) else json.dumps(content).encode())
    assert not result.ok and result.summary is None and result.document is None
    assert result.rejection.stage == 'validation' and result.rejection.code == code, result.rejection
    assert result.rejection.reason and '\n' not in result.rejection.reason
    if location is not None:
        assert result.rejection.location == location
    return result.rejection


# --- accepted documents ------------------------------------------------------

def test_valid_30_document_summary():
    result = check(spec())
    assert result.ok and result.rejection is None
    assert result.summary == ContractSummary(
        openapi_version='3.0.3', title='Acme', info_version='1.0.0', server_urls=('https://api.acme.com/v1',),
        operations=(('GET', '/pets'), ('POST', '/pets'), ('GET', '/pets/{id}')),
        webhook_operation_count=0, path_count=2, node_count=result.summary.node_count)
    assert result.document['openapi'] == '3.0.3'
    assert 'official OpenAPI JSON Schema' in result.limitations[0]


@pytest.mark.parametrize('version', ['3.0.0', '3.0.3', '3.1.0', '3.1.1', '3.1.0-rc1'])
def test_supported_versions(version):
    assert check(spec(openapi=version)).ok


def test_31_accepts_webhooks_only_and_components_only_with_a_no_operations_note():
    webhooks = check(spec(openapi='3.1.0', paths=None, webhooks={'ping': {'post': {}}}))
    assert webhooks.ok and webhooks.summary.webhook_operation_count == 1 and webhooks.summary.operations == ()
    assert not any('no operations' in note for note in webhooks.limitations)
    components = check(spec(openapi='3.1.0', paths=None, components={'schemas': {}}))
    assert components.ok and any('no operations' in note for note in components.limitations)


def test_30_empty_paths_is_valid_but_flagged():
    result = check(spec(paths={}))
    assert result.ok and result.summary.operations == () and any('no operations' in n for n in result.limitations)


def test_only_real_operations_and_paths_are_listed():
    result = check(spec(paths={'/a': {'summary': 'x', 'parameters': [], 'servers': [], 'x-foo': {}, 'put': {}},
                               'x-internal': {'not': 'a path'}}))
    assert result.summary.operations == (('PUT', '/a'),) and result.summary.path_count == 1


def test_path_item_refs_are_flagged_and_not_expanded():
    result = check(spec(paths={'/a': {'$ref': 'paths/a.yaml'}}))
    assert result.ok and result.summary.operations == ()
    assert any('$ref' in note for note in result.limitations)


def test_servers_are_optional_and_kept_raw():
    assert check(spec(servers=None)).summary.server_urls == ()
    result = check(spec(servers=[{'url': 'https://{env}.acme.com', 'variables': {'env': {'default': 'api'}}}, {'url': '/v2'}]))
    assert result.summary.server_urls == ('https://{env}.acme.com', '/v2')


def test_utf8_bom_and_whitespace_are_accepted():
    assert validate_document(b'\xef\xbb\xbf  \n' + json.dumps(spec()).encode() + b'\n').ok


def test_json_and_yaml_forms_give_identical_summaries():
    yaml_text = b"""
openapi: 3.0.3
info: {title: Acme, version: 1.0.0}
servers:
  - url: https://api.acme.com/v1
paths:
  /pets: {get: {}, post: {}}
  /pets/{id}: {get: {}, parameters: []}
"""
    from_json, from_yaml = check(spec()), validate_document(yaml_text)
    assert from_yaml.ok
    assert from_yaml.summary == from_json.summary and from_yaml.document == from_json.document


def test_result_is_deterministic():
    assert check(spec()) == check(spec())


# --- YAML scalar semantics ---------------------------------------------------

YAML_HEAD = b'openapi: 3.0.3\npaths: {}\n'


def yaml_doc(info=b'info: {title: T, version: "1"}\n', extra=b''):
    return validate_document(YAML_HEAD + info + extra)


def test_unquoted_date_versions_stay_strings():
    result = yaml_doc(info=b'info:\n  title: T\n  version: 2022-11-28\n')
    assert result.ok and result.summary.info_version == '2022-11-28'


def test_yaml_11_booleans_and_sexagesimals_stay_strings():
    result = yaml_doc(extra=b'x-values: [on, off, yes, no, NO, y, "1:30", 1:30, 007, true, False]\n')
    assert result.document['x-values'] == ['on', 'off', 'yes', 'no', 'NO', 'y', '1:30', '1:30', '007', True, False]
    keys = yaml_doc(extra=b'x-flags:\n  on: 1\n  no: 2\n').document['x-flags']
    assert keys == {'on': 1, 'no': 2}


def test_yaml_numbers_follow_core_schema():
    result = yaml_doc(extra=b'x-n: [1, -2, 0, 0x1F, 0o17, 1.5, 1e3, .5, -.inf]\n')
    assert result.document['x-n'][:8] == [1, -2, 0, 31, 15, 1.5, 1000.0, 0.5]


def test_numeric_and_boolean_mapping_keys_become_strings():
    result = validate_document(b'openapi: 3.0.3\ninfo: {title: T, version: "1"}\n'
                               b'paths:\n  /a:\n    get:\n      responses:\n        200: {description: ok}\n        default: {}\n')
    assert result.ok and set(result.document['paths']['/a']['get']['responses']) == {'200', 'default'}


def test_yaml_merge_keys_may_override_without_being_duplicates():
    result = validate_document(
        b'openapi: 3.0.3\ninfo: {title: T, version: "1"}\npaths: {}\n'
        b'x-base: &base {a: 1, b: 2}\nx-derived:\n  <<: *base\n  b: 3\n')
    assert result.ok and result.document['x-derived'] == {'a': 1, 'b': 3}


def test_unquoted_float_version_numbers_are_rejected_with_advice():
    rejection = rejected(b'openapi: 3.0\ninfo: {title: T, version: "1"}\npaths: {}\n', 'invalid_version', '/openapi')
    assert 'quote' in rejection.reason
    rejected(YAML_HEAD + b'info: {title: T, version: 1.0}\n', 'invalid_structure', '/info/version')


# --- not a usable document ---------------------------------------------------

@pytest.mark.parametrize('content', [b'', b'   \n\t', b'\xef\xbb\xbf'])
def test_empty(content):
    rejected(content, 'empty_document')


def test_encoding_and_binary():
    rejected(b'{"openapi": "\xff"}', 'unsupported_encoding')
    rejected(b'{"a": 1}\x00', 'not_json_or_yaml')


@pytest.mark.parametrize('content,code', [
    (b'<!DOCTYPE html><html><body>Not found</body></html>', 'html_document'),
    (b'  <HTML lang="en">', 'html_document'),
    (b'<?xml version="1.0"?><root/>', 'not_json_or_yaml'),
    (b'Not Found', 'not_an_object'),
    (b'404: Not Found', 'not_openapi'),  # valid YAML: a mapping with the key 404
    (b'- a\n- b\n', 'not_an_object'),
    (b'[1, 2, 3]', 'not_an_object'),
    (b'42', 'not_an_object'),
    (b'"openapi"', 'not_an_object'),
    (b'null', 'not_an_object'),
    (b'{"a": ', 'not_json_or_yaml'),
    (b'{"a": NaN}', 'not_json_or_yaml'),
    (b'{"a": Infinity}', 'not_json_or_yaml'),
    (b"{'a': 1}", 'not_json_or_yaml'),
    (b'a: [1, 2\nb: 3', 'not_json_or_yaml'),
    (b'a: 1\n---\nb: 2\n', 'not_json_or_yaml'),
    (b'a: !!python/object/apply:os.system ["echo hi"]', 'not_json_or_yaml'),
    (b'{"n": ' + b'9' * 6000 + b'}', 'not_json_or_yaml'),
])
def test_not_a_usable_document(content, code):
    rejected(content, code)


def test_parse_errors_report_position_without_echoing_content():
    rejection = rejected(b'{"secret-token-abc": ', 'not_json_or_yaml')
    assert 'line 1' in rejection.reason and 'secret-token-abc' not in rejection.reason


def test_unrelated_json_is_not_openapi():
    rejected({'name': 'package', 'version': '1.0.0'}, 'not_openapi')
    rejected({'info': {'title': 'x', 'version': '1'}, 'paths': {}}, 'not_openapi')


def test_swagger_2_is_unsupported_not_unrelated():
    rejection = rejected({'swagger': '2.0', 'info': {'title': 'x', 'version': '1'}, 'paths': {}}, 'unsupported_version', '/swagger')
    assert '3.0.x and 3.1.x' in rejection.reason


@pytest.mark.parametrize('version', ['3.2.0', '4.0.0', '2.0.0', '3.10.0'])
def test_unsupported_versions(version):
    rejected(spec(openapi=version), 'unsupported_version', '/openapi')


@pytest.mark.parametrize('version', [3, 3.0, '3', '3.0', 'v3.0.0', '', ['3.0.0'], '3.0.0\n'])
def test_invalid_versions(version):
    rejected(spec(openapi=version), 'invalid_version', '/openapi')


def test_null_version_is_invalid_not_missing():
    rejected(b'{"openapi": null, "info": {"title": "T", "version": "1"}, "paths": {}}', 'invalid_version', '/openapi')


# --- OpenAPI structure -------------------------------------------------------

@pytest.mark.parametrize('changes,location', [
    ({'info': None}, '/info'), ({'info': []}, '/info'), ({'info': {'version': '1'}}, '/info/title'),
    ({'info': {'title': 3, 'version': '1'}}, '/info/title'), ({'info': {'title': 'x'}}, '/info/version'),
    ({'info': {'title': 'x', 'version': 1}}, '/info/version'),
    ({'paths': None}, '/paths'), ({'paths': []}, '/paths'), ({'paths': 'x'}, '/paths'),
    ({'paths': {'pets': {}}}, '/paths/pets'), ({'paths': {'/a': []}}, '/paths/~1a'),
    ({'paths': {'/a': {'get': 'x'}}}, '/paths/~1a/get'),
    ({'servers': {}}, '/servers'), ({'servers': ['x']}, '/servers/0'), ({'servers': [{}]}, '/servers/0/url'),
    ({'servers': [{'url': 3}]}, '/servers/0/url'),
    ({'servers': [{'url': '/', 'variables': []}]}, '/servers/0/variables'),
    ({'components': []}, '/components'),
])
def test_invalid_structure(changes, location):
    rejected(spec(**changes), 'invalid_structure', location)


def test_31_needs_at_least_one_section_and_each_must_be_an_object():
    rejected(spec(openapi='3.1.0', paths=None), 'invalid_structure', '/')
    rejected(spec(openapi='3.1.0', paths=None, webhooks=[]), 'invalid_structure', '/webhooks')


def test_30_requires_paths_even_with_components():
    rejected(spec(paths=None, components={}), 'invalid_structure', '/paths')


def test_webhook_structure_is_checked():
    rejected(spec(openapi='3.1.0', webhooks={'hook': {'post': 1}}), 'invalid_structure', '/webhooks/hook/post')


# --- ambiguity and resource bounds ------------------------------------------

def test_duplicate_json_keys_are_rejected():
    content = b'{"openapi": "3.0.3", "info": {"title": "a", "version": "1"}, "paths": {}, "paths": {"/x": {"get": {}}}}'
    assert 'paths' in rejected(content, 'duplicate_key').reason


def test_duplicate_yaml_keys_are_rejected_including_after_normalisation():
    rejected(YAML_HEAD + b'info: {title: T, version: "1"}\nx-a: {k: 1, k: 2}\n', 'duplicate_key')
    rejected(YAML_HEAD + b'info: {title: T, version: "1"}\nx-a: {200: 1, "200": 2}\n', 'duplicate_key')


def test_non_scalar_yaml_keys_and_types_are_rejected():
    rejected(YAML_HEAD + b'info: {title: T, version: "1"}\nx-a:\n  ? [1, 2]\n  : v\n', 'invalid_structure')
    rejected(YAML_HEAD + b'info: {title: T, version: "1"}\nx-a: !!binary aGk=\n', 'invalid_structure')
    rejected(YAML_HEAD + b'info: {title: T, version: "1"}\nx-a: !!set {a, b}\n', 'invalid_structure')


def test_hostile_keys_are_bounded_in_messages():
    key = ('k' * 500 + '\n').encode()
    content = b'{"' + key[:-1] + b'": 1, "' + key[:-1] + b'": 2}'
    rejection = rejected(content, 'duplicate_key')
    assert len(rejection.reason) <= 200


def test_yaml_alias_bomb_hits_the_node_budget_quickly():
    levels = ['a: &a0 [x, x, x, x, x, x, x, x, x]']
    levels += [f'b{i}: &a{i} [' + ', '.join([f'*a{i - 1}'] * 9) + ']' for i in range(1, 10)]
    bomb = (YAML_HEAD + b'info: {title: T, version: "1"}\nx-bomb:\n  ' + '\n  '.join(levels).encode() + b'\n')
    started = time.monotonic()
    rejected(bomb, 'node_limit_exceeded')
    assert time.monotonic() - started < 5


def test_recursive_yaml_alias_is_rejected():
    rejected(YAML_HEAD + b'info: {title: T, version: "1"}\nx-loop: &a [*a]\n', 'circular_reference')
    rejected(YAML_HEAD + b'info: {title: T, version: "1"}\nx-loop: &a {self: *a}\n', 'circular_reference')


def test_shared_but_acyclic_aliases_are_fine():
    result = validate_document(YAML_HEAD + b'info: {title: T, version: "1"}\nx-a: &s {k: v}\nx-b: *s\nx-c: [*s, *s]\n')
    assert result.ok


def test_nesting_limits():
    limits = ValidationLimits(max_depth=10)
    deep = {'openapi': '3.0.3', 'info': {'title': 'T', 'version': '1'}, 'paths': {}, 'x-deep': {}}
    node = deep['x-deep']
    for _ in range(12):
        node['n'] = {}
        node = node['n']
    result = validate_document(json.dumps(deep).encode(), limits)
    assert result.rejection.code == 'nesting_limit_exceeded'
    assert validate_document(json.dumps(deep).encode()).ok  # within the default depth
    rejected(b'{"a": ' + b'[' * 100_000 + b']' * 100_000 + b'}', 'nesting_limit_exceeded')
    rejected(b'a: ' + b'[' * 50_000 + b']' * 50_000, 'nesting_limit_exceeded')


def test_node_and_size_limits():
    many = spec(**{'x-list': list(range(50))})
    assert validate_document(json.dumps(many).encode(), ValidationLimits(max_nodes=20)).rejection.code == 'node_limit_exceeded'
    content = json.dumps(spec()).encode()
    result = validate_document(content, ValidationLimits(max_bytes=len(content) - 1))
    assert result.rejection.code == 'size_limit_exceeded'
    assert validate_document(content, ValidationLimits(max_bytes=len(content))).ok


def test_large_document_within_limits():
    paths = {f'/items/{i}': {'get': {'responses': {'200': {'description': 'ok'}}}} for i in range(5000)}
    result = check(spec(paths=paths))
    assert result.ok and len(result.summary.operations) == 5000


# --- API contract ------------------------------------------------------------

def test_content_must_be_bytes():
    with pytest.raises(TypeError):
        validate_document('{"openapi": "3.0.3"}')


def test_accepts_bytearray():
    assert validate_document(bytearray(json.dumps(spec()).encode())).ok


def test_result_invariants():
    with pytest.raises(ValueError):
        ValidationResult()
    with pytest.raises(ValueError):
        ValidationResult(rejection=ValidationRejection('x', 'y'), summary=check(spec()).summary, document={})


@pytest.mark.parametrize('name,value', [('max_bytes', 0), ('max_nodes', -1), ('max_depth', True), ('max_depth', 1.5)])
def test_invalid_limits(name, value):
    with pytest.raises(ValueError):
        ValidationLimits(**{name: value})


def test_malformed_input_never_raises():
    for content in (b'{', b'}', b'[', b'---', b'&a', b'*a', b'!!', b'? ', b'{"a":1}}', b'\xff\xfe', b'%YAML 1.1\n---\na: 1'):
        result = validate_document(content)
        assert not result.ok and result.rejection.code


# --- robustness properties ---------------------------------------------------

from hypothesis import given, settings, strategies as st  # noqa: E402

JSON_VALUES = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=20),
    lambda children: st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4),
    max_leaves=25)


@settings(max_examples=300, deadline=None)
@given(st.binary(max_size=400))
def test_arbitrary_bytes_never_raise_and_always_explain(content):
    result = validate_document(content)
    assert result.ok or (result.rejection.stage == 'validation' and result.rejection.code and result.rejection.reason)


@settings(max_examples=300, deadline=None)
@given(JSON_VALUES)
def test_arbitrary_json_values_never_raise(value):
    result = validate_document(json.dumps(value).encode())
    assert result.ok == (result.rejection is None)
    if result.ok:  # whatever is accepted must be a well-formed 3.x document
        assert result.document['openapi'].startswith('3.') and isinstance(result.document['info']['title'], str)


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=300))
def test_arbitrary_text_as_yaml_never_raises(text):
    validate_document(text.encode())
