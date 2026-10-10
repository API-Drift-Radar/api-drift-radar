import json

import pytest
from hypothesis import given, settings, strategies as st

from radar.discovery.catalog import MAX_CONTEXTS, parse_linkset
from radar.discovery.formats import DOCUMENTATION_ONLY, UNSUPPORTED_DESCRIPTION, identify_description, recognize_artifact
from radar.discovery.links import TypedLink, html_typed_links, parse_link_header
from radar.discovery.validation import validate_document

BASE = 'https://api.acme.com/v1/'


# --- HTTP Link header -------------------------------------------------------------

def test_link_header_relations_of_interest_are_resolved_against_the_response_url():
    header = ('</openapi.json>; rel="service-desc"; type="application/openapi+json", '
              '<docs/>; rel="service-doc"; title="Docs", </.well-known/api-catalog>; rel="api-catalog", '
              '<https://cdn.example.net/style.css>; rel="stylesheet"')
    assert parse_link_header(header, BASE) == (
        TypedLink('https://api.acme.com/openapi.json', 'service-desc', 'application/openapi+json', None),
        TypedLink('https://api.acme.com/v1/docs/', 'service-doc', None, 'Docs'),
        TypedLink('https://api.acme.com/.well-known/api-catalog', 'api-catalog', None, None))


def test_a_link_with_several_relations_yields_each_and_commas_inside_values_do_not_split():
    links = parse_link_header('<https://x.test/a,b.json>; rel="service-desc service-doc"; title="A, B"', BASE)
    assert [(l.rel, l.href, l.title) for l in links] == [('service-desc', 'https://x.test/a,b.json', 'A, B'),
                                                         ('service-doc', 'https://x.test/a,b.json', 'A, B')]


@pytest.mark.parametrize('value', [None, '', 'junk', '<>; rel="service-desc"', '<javascript:alert(1)>; rel=service-desc',
                                   '<ftp://x.test/a>; rel="service-desc"', '</a>; rel=', '</a>', '<unterminated; rel="service-desc"',
                                   '<mailto:a@b.c>; rel="service-doc"', 42, b'bytes'])
def test_unusable_link_headers_yield_nothing_and_never_raise(value):
    assert parse_link_header(value, BASE) == ()


def test_link_header_is_bounded_and_deduplicated():
    many = ', '.join(f'</s{i}.json>; rel="service-desc"' for i in range(100))
    assert len(parse_link_header(many, BASE)) == 20
    assert len(parse_link_header('</a>; rel="service-desc", </a>; rel="service-desc"', BASE)) == 1
    assert parse_link_header('</late>; rel="service-desc"', BASE) and not parse_link_header(' ' * 9000 + '</late>; rel="service-desc"', BASE)


def test_unquoted_rel_and_parameter_case_are_accepted():
    assert [l.rel for l in parse_link_header('</a>; REL=service-desc', BASE)] == ['service-desc']


# --- HTML typed links -------------------------------------------------------------

def test_html_link_and_anchor_relations():
    html = ('<link rel="service-desc" type="application/yaml" href="spec.yaml"><link rel="stylesheet" href="x.css">'
            '<a rel="service-doc" href="/guide">Guide</a><a href="/plain">plain</a><link rel="api-catalog" href="/cat">')
    assert html_typed_links(html, BASE) == (
        TypedLink('https://api.acme.com/v1/spec.yaml', 'service-desc', 'application/yaml', None),
        TypedLink('https://api.acme.com/guide', 'service-doc', None, None),
        TypedLink('https://api.acme.com/cat', 'api-catalog', None, None))


def test_html_base_element_and_garbage():
    assert html_typed_links('<base href="https://cdn.example.net/x/"><link rel="service-desc" href="s.json">', BASE)[0].href == \
        'https://cdn.example.net/x/s.json'
    for junk in ('', '<<<>>>', '<link rel="service-desc"', '<link rel="service-desc" href="javascript:x">'):
        assert html_typed_links(junk, BASE) == ()


# --- RFC 9727 linkset -------------------------------------------------------------

CATALOG_URL = 'https://api.acme.com/.well-known/api-catalog'


def catalog(*contexts):
    return json.dumps({'linkset': list(contexts)}).encode()


def test_a_linkset_yields_descriptions_documentation_metadata_and_nested_catalogues():
    parsed = parse_linkset(catalog({
        'anchor': 'https://api.acme.com/v1',
        'service-desc': [{'href': '/v1/openapi.json', 'type': 'application/openapi+json'}],
        'service-doc': [{'href': 'https://docs.acme.com/v1', 'title': 'Docs'}],
        'service-meta': [{'href': '/v1/meta'}],
        'item': [{'href': '/other-catalog'}], 'api-catalog': [{'href': '/nested'}]}), CATALOG_URL)
    (entry,) = parsed.entries
    assert entry.anchor == 'https://api.acme.com/v1'
    assert [l.href for l in entry.descriptions] == ['https://api.acme.com/v1/openapi.json']
    assert entry.descriptions[0].type == 'application/openapi+json' and entry.documentation[0].title == 'Docs'
    assert [l.href for l in entry.metadata] == ['https://api.acme.com/v1/meta']
    assert [l.href for l in entry.items] == ['https://api.acme.com/other-catalog', 'https://api.acme.com/nested']


@pytest.mark.parametrize('content', [b'', b'not json', b'[]', b'{}', b'{"linkset": "x"}', b'{"linkset": {"a": 1}}', b'\xff\xfe', b'null'])
def test_anything_that_is_not_a_linkset_is_none(content):
    assert parse_linkset(content, CATALOG_URL) is None


def test_malformed_contexts_and_links_are_skipped_and_counted():
    parsed = parse_linkset(catalog('junk', 3, {'anchor': 42, 'service-desc': [{'href': 'javascript:x'}, 'x', {'href': ''}, {'href': '/ok.json'}]}),
                           CATALOG_URL)
    assert len(parsed.entries) == 1 and parsed.entries[0].anchor is None
    assert [l.href for l in parsed.entries[0].descriptions] == ['https://api.acme.com/ok.json'] and parsed.skipped == 5


def test_a_linkset_is_bounded():
    big = catalog(*[{'service-desc': [{'href': f'/s{i}.json'}]} for i in range(MAX_CONTEXTS + 50)])
    parsed = parse_linkset(big, CATALOG_URL)
    assert len(parsed.entries) == MAX_CONTEXTS and parsed.skipped == 50
    many_links = catalog({'service-desc': [{'href': f'/s{i}.json'} for i in range(100)]})
    assert len(parse_linkset(many_links, CATALOG_URL).entries[0].descriptions) == 20


@settings(max_examples=200, deadline=None)
@given(st.binary(max_size=300))
def test_arbitrary_bytes_never_break_the_catalog_or_link_parsers(content):
    parse_linkset(content, CATALOG_URL)
    parse_link_header(content.decode('latin-1'), BASE)
    html_typed_links(content.decode('latin-1'), BASE)


# --- artifact recognition ---------------------------------------------------------

@pytest.mark.parametrize('document,kind,category', [
    ({'swagger': '2.0', 'info': {}, 'paths': {}}, 'swagger_2', UNSUPPORTED_DESCRIPTION),
    ({'swagger': 2.0}, 'swagger_2', UNSUPPORTED_DESCRIPTION),
    ({'asyncapi': '3.0.0', 'info': {}}, 'asyncapi', UNSUPPORTED_DESCRIPTION),
    ({'smithy': '2.0', 'shapes': {'a#B': {}}}, 'smithy', UNSUPPORTED_DESCRIPTION),
    ({'kind': 'discovery#restDescription', 'name': 'x', 'resources': {}}, 'google_discovery', UNSUPPORTED_DESCRIPTION),
    ({'discoveryVersion': 'v1', 'resources': {}}, 'google_discovery', UNSUPPORTED_DESCRIPTION),
    ({'info': {'schema': 'https://schema.getpostman.com/json/collection/v2.1.0/collection.json'}, 'item': []},
     'postman_collection', DOCUMENTATION_ONLY),
    ({'name': 'fruit', 'version': '0.1', 'apidoc': '0.3.0'}, 'apidoc', DOCUMENTATION_ONLY),
])
def test_artifacts_are_recognised_from_parsed_documents(document, kind, category):
    info = identify_description(document)
    assert (info.kind, info.category) == (kind, category) and info.label


@pytest.mark.parametrize('document', [{}, {'name': 'package', 'version': '1'}, {'openapi': '3.0.3'}, {'smithy': '2.0'},
                                     {'kind': 'storage#object'}, {'info': {'schema': 'x'}, 'item': 3}, [], 'x', None])
def test_unrelated_documents_are_not_misrecognised(document):
    assert identify_description(document) is None


def test_apidoc_javascript_wrappers_and_json_are_recognised_from_bytes():
    data_js = b'define({ "api": [ { "type": "get", "url": "/api/fruit/:name", "title": "Fruit", "group": "Fruit" } ] });'
    project_js = b'define({ "name": "Fruityvice", "version": "1.0.0", "apidoc": "0.3.0", "generator": {"name": "apidoc"} });'
    assert recognize_artifact(data_js).kind == 'apidoc' and recognize_artifact(project_js).kind == 'apidoc'
    assert recognize_artifact(json.dumps([{'type': 'get', 'url': '/a', 'name': 'A', 'group': 'G'}]).encode()).kind == 'apidoc'
    assert recognize_artifact(json.dumps({'name': 'x', 'apidoc': '0.3.0'}).encode()).kind == 'apidoc'
    assert recognize_artifact(json.dumps({'smithy': '2.0', 'shapes': {}}).encode()).kind == 'smithy'


@pytest.mark.parametrize('content', [b'', b'<html></html>', b'plain text', b'define(function(){});', b'[1, 2]', b'{"a": 1}',
                                    b'\xff\xfe', b'{"truncated": ', json.dumps([{'type': 'x'}]).encode()])
def test_other_content_is_not_an_artifact(content):
    assert recognize_artifact(content) is None


# --- validation reports recognised formats distinctly -------------------------------

@pytest.mark.parametrize('document,code', [
    ({'smithy': '2.0', 'shapes': {}}, 'unsupported_format'),
    ({'kind': 'discovery#restDescription', 'resources': {}}, 'unsupported_format'),
    ({'asyncapi': '3.0.0', 'info': {}}, 'unsupported_format'),
    ({'swagger': '2.0', 'info': {}, 'paths': {}}, 'unsupported_version'),
    ({'name': 'package'}, 'not_openapi'),
    ({'name': 'fruit', 'apidoc': '0.3.0'}, 'not_openapi'),  # documentation data is not a rejected description
])
def test_validation_distinguishes_unsupported_formats_from_unrelated_documents(document, code):
    result = validate_document(json.dumps(document).encode())
    assert not result.ok and result.rejection.code == code
    if code == 'unsupported_format':
        assert 'not supported' in result.rejection.reason and '3.0.x and 3.1.x' in result.rejection.reason
