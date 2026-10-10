import time

import pytest
from hypothesis import given, settings, strategies as st

from radar.discovery.references import (
    AnchorReference, ExternalReference, LocalReference, ReferenceLimits, UnsupportedReference,
    classify_reference, json_pointer, resolve_pointer, scan_references,
)


BASE = 'https://specs.acme.com/api/openapi.yaml'


# --- JSON Pointer ------------------------------------------------------------

DOCUMENT = {'a': {'b': [10, {'c': 'x'}]}, 'a/b': 1, 'm~n': 2, '': 3, 'list': [], '200': 'ok'}


@pytest.mark.parametrize('tokens,expected', [
    ((), DOCUMENT), (('a', 'b', '1', 'c'), 'x'), (('a', 'b', '0'), 10), (('a/b',), 1), (('m~n',), 2), (('',), 3),
    (('200',), 'ok'),
])
def test_resolve_pointer_found(tokens, expected):
    assert resolve_pointer(DOCUMENT, tokens) == (True, expected)


@pytest.mark.parametrize('tokens', [
    ('missing',), ('a', 'x'), ('a', 'b', '2'), ('a', 'b', '-'), ('a', 'b', '01'), ('a', 'b', 'x'), ('a', 'b', '1.0'),
    ('a', 'b', '0', 'deeper'), ('list', '0'), ('200', 'x'), ('a', 'b', '-1'), ('a', 'b', ' 0'),
])
def test_resolve_pointer_missing(tokens):
    assert resolve_pointer(DOCUMENT, tokens) == (False, None)


def test_json_pointer_escaping_round_trips():
    tokens = ('paths', '/pets/{id}', 'm~n')
    assert json_pointer(tokens) == '/paths/~1pets~1{id}/m~0n'
    assert classify_reference('#' + json_pointer(tokens), BASE) == LocalReference(tokens)


# --- classification ----------------------------------------------------------

@pytest.mark.parametrize('ref,expected', [
    ('#/components/schemas/Pet', LocalReference(('components', 'schemas', 'Pet'))),
    ('#', LocalReference(())),
    ('#/', LocalReference(('',))),
    ('#/paths/~1pets~1%7Bid%7D/get', LocalReference(('paths', '/pets/{id}', 'get'))),
    ('#/a%20b/m~0n', LocalReference(('a b', 'm~n'))),
    ('#/caf%C3%A9', LocalReference(('café',))),
    ('#foo', AnchorReference('foo')),
    ('./schemas/pet.yaml', ExternalReference('https://specs.acme.com/api/schemas/pet.yaml', ())),
    ('schemas/pet.yaml#/Pet', ExternalReference('https://specs.acme.com/api/schemas/pet.yaml', ('Pet',))),
    ('../common/error.json#/definitions/Error',
     ExternalReference('https://specs.acme.com/common/error.json', ('definitions', 'Error'))),
    ('/abs/x.yaml', ExternalReference('https://specs.acme.com/abs/x.yaml', ())),
    ('//cdn.acme.com/x.yaml#/A', ExternalReference('https://cdn.acme.com/x.yaml', ('A',))),
    ('https://other.test:8443/x.json?v=2#/A', ExternalReference('https://other.test:8443/x.json?v=2', ('A',))),
    ('http://other.test/x.json', ExternalReference('http://other.test/x.json', ())),
    ('x.yaml#frag', AnchorReference('frag', 'https://specs.acme.com/api/x.yaml')),
    # A reference back to the containing document is internal.
    ('openapi.yaml#/components/schemas/Pet', LocalReference(('components', 'schemas', 'Pet'))),
    ('https://specs.acme.com/api/openapi.yaml', LocalReference(())),
    ('./openapi.yaml#', LocalReference(())),
])
def test_classification(ref, expected):
    assert classify_reference(ref, BASE) == expected


@pytest.mark.parametrize('ref', [
    '', ' ', ' #/a', '#/a ', '#/a\n', '#/a\x00b', 'a\\b.yaml', 'file:///etc/passwd', 'ftp://acme.com/x', 'urn:acme:x',
    'javascript:alert(1)', 'data:text/plain,x', 'mailto:a@b.c', 'https://user:pw@acme.com/x.yaml', 'https://acme.com:99999/x',
    'https://acme.com:abc/x', 'http://', 'https:///x.yaml', '#/a~2b', '#/a~', '#/%ff',
])
def test_unsupported_references(ref):
    assert isinstance(classify_reference(ref, BASE), UnsupportedReference), ref


def test_stray_percent_is_kept_literally_and_resolves_only_if_the_key_exists():
    assert classify_reference('#/a%', BASE) == LocalReference(('a%',))
    assert scan_references(spec(**{'a%': {}, 'r': {'$ref': '#/a%'}}), BASE).ok
    assert not scan_references(spec(r={'$ref': '#/a%'}), BASE).ok


def test_non_string_reference_is_unsupported():
    assert isinstance(classify_reference(None, BASE), UnsupportedReference)


# --- internal verification ---------------------------------------------------

def spec(**extra):
    return {'openapi': '3.0.3', 'info': {'title': 'T', 'version': '1'}, 'paths': {}, **extra}


def test_resolving_internal_references_pass():
    document = spec(paths={'/pets': {'get': {'responses': {'200': {'content': {'application/json': {
        'schema': {'$ref': '#/components/schemas/Pet'}}}}}}}},
        components={'schemas': {'Pet': {'type': 'object', 'properties': {'owner': {'$ref': '#/components/schemas/Owner'}}},
                                'Owner': {'type': 'object'}}})
    scan = scan_references(document, BASE)
    assert scan.ok and scan.rejection is None and scan.local_count == 2 and scan.external == ()


def test_missing_target_is_reported_with_location_and_reference():
    document = spec(paths={'/pets/{id}': {'get': {'responses': {'200': {'schema': {'$ref': '#/components/schemas/Missing'}}}}}})
    scan = scan_references(document, BASE)
    rejection = scan.rejection
    assert not scan.ok and rejection.stage == 'reference_capture' and rejection.code == 'unresolvable_reference'
    assert rejection.location == '/paths/~1pets~1{id}/get/responses/200/schema/$ref'
    assert '#/components/schemas/Missing' in rejection.reason


def test_failures_are_in_document_order_and_counted():
    document = spec(a={'$ref': '#/nope/1'}, b={'$ref': '#/components'}, c={'$ref': '#/nope/3'}, components={})
    scan = scan_references(document, BASE)
    assert scan.failure_count == 2 and [f.location for f in scan.failures] == ['/a/$ref', '/c/$ref']
    assert scan.rejection.location == '/a/$ref' and '1 more' in scan.rejection.reason


def test_pointer_forms_that_must_resolve():
    document = spec(paths={'/pets/{id}': {'get': {}}}, components={'schemas': {'a/b': {}, 'm~n': {}}},
                    x=[{'k': 1}, {'k': 2}], y={'$ref': '#/x/1/k'})
    refs = ['#', '#/paths/~1pets~1{id}/get', '#/paths/~1pets~1%7Bid%7D/get', '#/components/schemas/a~1b',
            '#/components/schemas/m~0n', '#/x/0']
    for ref in refs:
        document['y'] = {'$ref': ref}
        assert scan_references(document, BASE).ok, ref
    for ref in ['#/x/2', '#/x/01', '#/x/-', '#/components/schemas/a/b', '#/Paths']:
        document['y'] = {'$ref': ref}
        assert not scan_references(document, BASE).ok, ref


def test_external_references_are_collected_not_resolved_and_deduplicated():
    document = spec(a={'$ref': './common.yaml#/Error'}, b={'$ref': './common.yaml#/Error'},
                    c={'$ref': 'common.yaml#/Error'}, d={'$ref': 'https://other.test/x.json'})
    scan = scan_references(document, BASE)
    assert scan.ok
    uses = {(u.ref, u.url, u.tokens): (u.occurrences, u.location) for u in scan.external}
    assert uses == {
        ('./common.yaml#/Error', 'https://specs.acme.com/api/common.yaml', ('Error',)): (2, '/a'),
        ('common.yaml#/Error', 'https://specs.acme.com/api/common.yaml', ('Error',)): (1, '/c'),
        ('https://other.test/x.json', 'https://other.test/x.json', ()): (1, '/d'),
    }
    assert scan.local_count == 0


def test_distinct_internal_targets_are_returned_for_subtree_followers():
    document = spec(a={'$ref': '#/x'}, b={'$ref': '#/x'}, c={'$ref': '#/y/z'}, d={'$ref': 'ext.yaml'}, x={}, y={'z': {}})
    scan = scan_references(document, BASE)
    assert scan.local_targets == (('x',), ('y', 'z')) and scan.local_count == 3


def test_unsupported_reference_is_a_failure_with_its_own_code():
    scan = scan_references(spec(a={'$ref': 'file:///etc/passwd'}), BASE)
    assert not scan.ok and scan.rejection.code == 'reference_unsupported' and scan.rejection.location == '/a/$ref'


def test_anchor_references_and_ids_become_limitations_not_failures():
    scan = scan_references(spec(a={'$ref': '#node'}, b={'$ref': 'x.yaml#node'}, c={'$id': 'https://x.test/schema'}), BASE)
    assert scan.ok and scan.anchor_count == 2 and scan.id_count == 1 and scan.external == ()
    text = ' '.join(scan.limitations)
    assert 'anchor-style' in text and '$id' in text and 'operationRef' in text


def test_scope_limitation_is_always_present():
    assert 'operationRef' in scan_references(spec(), BASE).limitations[0]


# --- what counts as a reference ---------------------------------------------

def test_literal_ref_inside_example_data_is_not_followed():
    document = spec(a={'example': {'$ref': '#/missing'}, 'default': {'$ref': '#/missing'}, 'const': {'$ref': '#/missing'},
                       'enum': [{'$ref': '#/missing'}]})
    assert scan_references(document, BASE).ok


def test_properties_named_like_data_keys_are_followed():
    for container in ('properties', 'schemas', 'responses', 'parameters', 'headers'):
        for name in ('example', 'default', 'enum', 'const'):
            document = spec(x={container: {name: {'$ref': '#/missing'}}})
            assert not scan_references(document, BASE).ok, (container, name)


def test_data_keys_inside_list_items_are_skipped():
    document = spec(parameters=[{'name': 'p', 'example': {'$ref': '#/missing'}}])
    assert scan_references(document, BASE).ok


def test_ref_siblings_and_nested_refs_are_all_scanned():
    document = spec(a={'$ref': '#/b', 'x-more': {'$ref': '#/missing'}}, b={})
    scan = scan_references(document, BASE)
    assert scan.failure_count == 1 and scan.rejection.location == '/a/x-more/$ref'


def test_refs_in_lists_report_index_in_location():
    scan = scan_references(spec(schema={'allOf': [{'$ref': '#/ok'}, {'$ref': '#/missing'}]}, ok={}), BASE)
    assert scan.rejection.location == '/schema/allOf/1/$ref'


def test_non_string_ref_values_and_properties_named_ref_are_ignored():
    document = spec(a={'$ref': 3}, b={'$ref': None}, c={'properties': {'$ref': {'type': 'string'}}}, d={'$ref': ['#/missing']})
    assert scan_references(document, BASE).ok


def test_extension_subtrees_are_scanned():
    assert not scan_references(spec(**{'x-hooks': {'k': {'$ref': '#/missing'}}}), BASE).ok


# --- scanning a subtree (used for fetched files) ----------------------------

def test_start_scans_only_a_subtree_but_resolves_against_the_whole_document():
    document = {'shared': {'Name': {'type': 'string'}},
                'Pet': {'properties': {'name': {'$ref': '#/shared/Name'}, 'bad': {'$ref': '#/nope'}}},
                'Unrelated': {'$ref': '#/also-missing', 'x': {'$ref': 'other.yaml#/Y'}}}
    whole = scan_references(document, BASE)
    assert whole.failure_count == 2 and len(whole.external) == 1
    pet = scan_references(document, BASE, start=('Pet',))
    assert pet.failure_count == 1 and pet.rejection.location == '/Pet/properties/bad/$ref' and pet.external == ()
    shared = scan_references(document, BASE, start=('shared', 'Name'))
    assert shared.ok and shared.local_count == 0


def test_start_context_decides_name_map_handling():
    document = {'properties': {'example': {'$ref': '#/missing'}}}
    assert not scan_references(document, BASE, start=('properties',)).ok
    assert scan_references(document, BASE, start=('properties', 'example')).failure_count == 1


def test_start_must_exist():
    with pytest.raises(ValueError):
        scan_references({'a': 1}, BASE, start=('missing',))


# --- limits and hostile input ------------------------------------------------

def test_node_limit_stops_the_scan_with_a_distinct_code():
    document = spec(**{f'x{i}': {'k': {}} for i in range(50)})
    scan = scan_references(document, BASE, limits=ReferenceLimits(max_nodes=20))
    assert not scan.ok and scan.rejection.code == 'reference_limit_exceeded'


def test_distinct_external_reference_limit():
    document = spec(**{f'x{i}': {'$ref': f'f{i}.yaml'} for i in range(10)})
    scan = scan_references(document, BASE, limits=ReferenceLimits(max_external_references=3))
    assert scan.rejection.code == 'reference_limit_exceeded' and len(scan.external) == 3
    repeated = spec(**{f'x{i}': {'$ref': 'same.yaml'} for i in range(50)})
    assert scan_references(repeated, BASE, limits=ReferenceLimits(max_external_references=3)).ok


def test_shared_subtree_expansion_is_bounded():
    node = {'leaf': {'k': 1}}
    for _ in range(40):
        node = {'a': node, 'b': node}  # 2**40 paths through shared objects
    started = time.monotonic()
    scan = scan_references(spec(x=node), BASE, limits=ReferenceLimits(max_nodes=100_000))
    assert scan.rejection.code == 'reference_limit_exceeded' and time.monotonic() - started < 5


def test_very_deep_documents_do_not_overflow_the_stack():
    node = {'$ref': '#/missing'}
    for _ in range(20_000):
        node = {'n': node}
    scan = scan_references(spec(x=node), BASE)
    assert scan.failure_count == 1 and len(scan.rejection.location.split('/')) > 20_000


def test_many_failures_are_counted_but_reported_up_to_a_cap():
    document = spec(**{f'x{i}': {'$ref': '#/missing'} for i in range(100)})
    scan = scan_references(document, BASE)
    assert scan.failure_count == 100 and len(scan.failures) == 20


def test_hostile_reference_text_is_bounded_in_messages():
    scan = scan_references(spec(a={'$ref': '#/' + 'x' * 5000 + '\n'}), BASE)
    assert len(scan.rejection.reason) < 300 and '\n' not in scan.rejection.reason


def test_scan_is_deterministic():
    document = spec(a={'$ref': 'x.yaml#/A'}, b={'$ref': '#/c'}, c={})
    assert scan_references(document, BASE) == scan_references(document, BASE)


@pytest.mark.parametrize('name,value', [('max_nodes', 0), ('max_external_references', -1), ('max_nodes', True)])
def test_invalid_limits(name, value):
    with pytest.raises(ValueError):
        ReferenceLimits(**{name: value})


JSON_LIKE = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=12),
    lambda children: st.lists(children, max_size=4) | st.dictionaries(
        st.sampled_from(['$ref', '$id', 'example', 'properties', 'a', 'b', '']), children, max_size=4),
    max_leaves=30)


@settings(max_examples=300, deadline=None)
@given(JSON_LIKE, st.lists(st.sampled_from(['a', 'b', '$ref', '0']), max_size=3))
def test_scan_never_raises_on_arbitrary_structures(document, start):
    if not resolve_pointer(document, start)[0]:
        return
    scan = scan_references(document, BASE, start=start)
    assert scan.ok == (scan.rejection is None)
