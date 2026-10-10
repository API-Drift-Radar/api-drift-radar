import json

import pytest

from radar.discovery.input import normalize_target
from radar.discovery.matching import (
    INDETERMINATE, MATCH, MISMATCH, NOT_REQUESTED, check_operation, path_matches,
)
from radar.discovery.validation import validate_document
from radar.domain.discovery import DiscoveryRequest


SOURCE = 'https://specs.acme.com/openapi.json'
PATHS = {
    '/pets': {'get': {}, 'post': {}},
    '/pets/{id}': {'get': {}, 'delete': {}},
    '/pets/{id}/photos/{photo}': {'get': {}},
    '/files/{name}.json': {'get': {}},
    '/a.b/{x}': {'get': {}},
    '/v1/special': {'get': {}},
}


def spec(paths=None, servers=None, **extra):
    document = {'openapi': '3.0.3', 'info': {'title': 'T', 'version': '1'},
                'paths': PATHS if paths is None else paths, **extra}
    if servers is not None:
        document['servers'] = servers
    return document


def check(url, method=None, document=None):
    document = document or spec(servers=[{'url': 'https://api.acme.com'}])
    validation = validate_document(json.dumps(document).encode())
    assert validation.ok, validation.rejection
    target = normalize_target(DiscoveryRequest(url, method=method))
    return check_operation(target, validation.summary, validation.document, SOURCE)


# --- template matching -------------------------------------------------------

@pytest.mark.parametrize('template,path,expected', [
    ('/pets', '/pets', True), ('/pets', '/Pets', False), ('/pets', '/pets/x', False), ('/pets/{id}', '/pets/42', True),
    ('/pets/{id}', '/pets/', False), ('/pets/{id}', '/pets//x', False), ('/pets/{id}', '/pets/a/b', False),
    ('/pets/{id}/photos/{p}', '/pets/1/photos/2', True), ('/pets/{id}/photos/{p}', '/pets/1/photos', False),
    ('/files/{name}.json', '/files/report.json', True), ('/files/{name}.json', '/files/.json', False),
    ('/files/{name}.json', '/files/report.xml', False), ('/files/{a}.{b}', '/files/x.y', True),
    ('/files/{a}.{b}', '/files/xy', False), ('/files/{a}{b}', '/files/xy', True), ('/files/{a}{b}', '/files/x', False),
    ('/x/pre-{id}-post', '/x/pre-1-post', True), ('/x/pre-{id}-post', '/x/pre--post', False),
    ('/x/{a}-{b}-{c}', '/x/1-2-3', True), ('/x/{a}-{b}-{c}', '/x/1-2', False),
    ('/a.b/{x}', '/a.b/1', True), ('/a.b/{x}', '/aXb/1', False),   # dots are literal, not wildcards
    ('/(x)/{y}', '/(x)/1', True), ('/a+b', '/aab', False), ('/', '/', True),
])
def test_path_templates(template, path, expected):
    assert path_matches(template, path) is expected


def test_pathological_templates_stay_fast():
    template = '/' + '{a}' * 40 + 'z'
    assert path_matches(template, '/' + 'x' * 5000) is False
    assert path_matches('/{a}x{b}x{c}x{d}x{e}x{f}', '/' + 'xx' * 3000) is True
    assert path_matches('/{a}x{b}x{c}x{d}x{e}x{f}', '/' + 'y' * 6000) is False
    assert path_matches('/{a}x{b}x{c}x{d}x{e}x{f}', '/xxxxx') is False  # six parameters need six characters


# --- method and path ---------------------------------------------------------

def test_method_and_path_match():
    result = check('https://api.acme.com/pets/42', 'get')
    assert (result.criterion, result.outcome, result.source_url) == ('operation', MATCH, SOURCE)
    assert 'GET /pets/{id}' in result.description


@pytest.mark.parametrize('url,method', [
    ('https://api.acme.com/pets', None), ('https://api.acme.com/pets/', 'GET'), ('https://api.acme.com/pets?limit=5', 'POST'),
    ('https://api.acme.com/pets/1/photos/9', 'GET'), ('https://api.acme.com/files/a.json', 'GET'),
    ('https://api.acme.com/pets/my%20dog', 'GET'), ('https://api.acme.com/pets/a%2Fb', 'GET'),
])
def test_matching_forms(url, method):
    assert check(url, method).outcome == MATCH


def test_wrong_method_is_a_mismatch_and_names_the_methods_that_exist():
    result = check('https://api.acme.com/pets', 'DELETE')
    assert result.outcome == MISMATCH and 'GET, POST' in result.description


@pytest.mark.parametrize('url', ['https://api.acme.com/unknown', 'https://api.acme.com/pets/1/2/3',
                                 'https://api.acme.com/pets//x', 'https://api.acme.com/PETS'])
def test_missing_path_with_a_method_is_a_mismatch(url):
    assert check(url, 'GET').outcome == MISMATCH


def test_path_without_a_method_can_match_but_never_mismatches():
    assert check('https://api.acme.com/pets').outcome == MATCH
    result = check('https://api.acme.com/docs/getting-started')
    assert result.outcome == INDETERMINATE and 'may not be an API endpoint' in result.description


def test_no_endpoint_path():
    assert check('https://api.acme.com').outcome == NOT_REQUESTED
    assert check('https://api.acme.com/').outcome == NOT_REQUESTED
    assert check('https://api.acme.com/', 'GET').outcome == INDETERMINATE


# --- server base paths -------------------------------------------------------

def servers(*urls):
    return spec(servers=[{'url': url} for url in urls])


def test_server_base_path_is_removed_first():
    document = servers('https://api.acme.com/v1')
    result = check('https://api.acme.com/v1/pets', 'GET', document)
    assert result.outcome == MATCH and 'server base path /v1' in result.description


def test_path_without_the_base_path_still_matches_as_given():
    assert check('https://api.acme.com/pets', 'GET', servers('https://api.acme.com/v1')).outcome == MATCH


def test_a_path_that_exists_only_under_a_different_base_does_not_match():
    document = servers('https://api.acme.com/v1')
    assert check('https://api.acme.com/v2/pets', 'GET', document).outcome == MISMATCH


def test_literal_path_beginning_like_a_base_path_is_not_confused():
    # '/v1/special' is itself a path; with base '/v1' the remainder '/special' does not exist, the full path does.
    assert check('https://api.acme.com/v1/special', 'GET', servers('https://api.acme.com/v1')).outcome == MATCH


def test_unrelated_servers_base_paths_are_ignored():
    document = servers('https://other.test/pets-api')
    assert check('https://api.acme.com/pets-api/pets', 'GET', document).outcome == MISMATCH
    assert check('https://api.acme.com/pets', 'GET', document).outcome == MATCH


def test_relative_and_nested_base_paths():
    assert check('https://api.acme.com/api/v1/pets', 'GET', servers('/api/v1')).outcome == MATCH
    assert check('https://api.acme.com/api/v1/pets', 'GET', servers('https://api.acme.com/api/v1/')).outcome == MATCH
    assert check('https://api.acme.com/a/pets', 'GET', servers('/a', '/b')).outcome == MATCH


# --- things that hide an operation -------------------------------------------

def test_ref_only_path_items_prevent_a_mismatch():
    document = spec(paths={'/pets': {'get': {}}, '/orders': {'$ref': 'orders.yaml'}}, servers=[{'url': 'https://api.acme.com'}])
    result = check('https://api.acme.com/orders', 'GET', document)
    assert result.outcome == INDETERMINATE and '$ref' in result.description
    assert check('https://api.acme.com/pets', 'GET', document).outcome == MATCH


@pytest.mark.parametrize('server', [{'url': 'https://api.acme.com/{version}'}, {'url': 'v2'}])
def test_unknown_base_paths_prevent_a_mismatch(server):
    document = spec(servers=[server])
    assert check('https://api.acme.com/nope', 'GET', document).outcome == INDETERMINATE


def test_variable_base_paths_are_expanded_when_they_have_values():
    document = spec(servers=[{'url': 'https://api.acme.com/{v}', 'variables': {'v': {'default': 'v1', 'enum': ['v1', 'v2']}}}])
    assert check('https://api.acme.com/v2/pets', 'GET', document).outcome == MATCH
    assert check('https://api.acme.com/v3/pets', 'GET', document).outcome == MISMATCH


def test_webhooks_are_not_endpoints():
    document = {'openapi': '3.1.0', 'info': {'title': 'T', 'version': '1'}, 'paths': {'/pets': {'get': {}}},
                'webhooks': {'/hook': {'post': {}}}}
    assert check('https://api.acme.com/hook', 'POST', document).outcome == MISMATCH


def test_hostile_path_text_is_bounded():
    result = check('https://api.acme.com/' + 'a' * 5000, 'GET')
    assert result.outcome == MISMATCH and len(result.description) < 400


def test_check_is_deterministic():
    assert check('https://api.acme.com/pets/1', 'GET') == check('https://api.acme.com/pets/1', 'GET')
