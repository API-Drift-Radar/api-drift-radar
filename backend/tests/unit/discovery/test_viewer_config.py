import json

import pytest
from hypothesis import given, settings, strategies as st

from radar.discovery.viewer_config import (
    ViewerRef, is_initializer, scan_page, scan_requirejs_main, scan_swagger_config_json, scan_viewer_script,
)

PAGE = 'https://api.acme.com/docs/index.html'


def refs(scan):
    return [(r.url, r.mechanism) for r in scan.description_urls]


# --- Swagger UI in script text ---------------------------------------------------------

def test_standard_initializer_script():
    scan = scan_viewer_script('''window.onload = function() {
      window.ui = SwaggerUIBundle({ url: "https://petstore.swagger.io/v2/swagger.json", dom_id: '#swagger-ui',
        deepLinking: true, presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset], layout: "StandaloneLayout" });
    };''')
    assert refs(scan) == [('https://petstore.swagger.io/v2/swagger.json', 'swagger_ui_config')] and scan.unsupported == ()


def test_urls_list_config_url_and_embedded_spec_are_all_extracted():
    scan = scan_viewer_script('''SwaggerUIBundle({urls: [{url: '/v1/openapi.json', name: 'v1'}, {url: "/v2/openapi.json"}],
        configUrl: "/docs/swagger-config", spec: {"openapi": "3.0.3", "info": {"title": "T", "version": "1"}, "paths": {}}})''')
    assert refs(scan) == [('/v1/openapi.json', 'swagger_ui_config'), ('/v2/openapi.json', 'swagger_ui_config')]
    assert scan.config_urls == ('/docs/swagger-config',) and len(scan.embedded_specs) == 1
    assert json.loads(scan.embedded_specs[0])['openapi'] == '3.0.3' and scan.unsupported == ()


@pytest.mark.parametrize('script,code', [
    ("SwaggerUIBundle({url: BASE + '/openapi.json'})", 'url_not_literal'),
    ('SwaggerUIBundle({url: getUrl()})', 'url_not_literal'),
    ('SwaggerUIBundle({urls: buildUrls()})', 'urls_not_literal'),
    ('SwaggerUIBundle({configUrl: window.cfg})', 'configUrl_not_literal'),
    ('SwaggerUIBundle({spec: loadSpec()})', 'spec_not_literal'),
    ('SwaggerUIBundle({spec: {openapi: spec_version}})', 'spec_not_literal'),  # object, but not static JSON
    ('SwaggerUIBundle({dom_id: "#ui"})', 'no_literal_source'),
    ('SwaggerUIBundle(config)', 'configuration_not_literal'),
    ("SwaggerUIBundle({url: '/x.json'", 'configuration_not_literal'),
    ("SwaggerUIBundle({queryConfigEnabled: true, url: '/x.json'})", 'query_config_enabled'),
])
def test_dynamic_configuration_is_reported_with_a_code_and_never_guessed(script, code):
    scan = scan_viewer_script(script)
    assert code in {c for c, _ in scan.unsupported} and all(detail for _, detail in scan.unsupported)
    if code != 'query_config_enabled':
        assert scan.description_urls == () and scan.embedded_specs == ()


def test_a_literal_url_is_still_used_when_query_configuration_is_enabled_and_noted():
    scan = scan_viewer_script("SwaggerUIBundle({queryConfigEnabled: true, url: '/x.json'})")
    assert refs(scan) == [('/x.json', 'swagger_ui_config')] and [c for c, _ in scan.unsupported] == ['query_config_enabled']
    assert scan_viewer_script("SwaggerUIBundle({queryConfigEnabled: false, url: '/x.json'})").unsupported == ()


def test_redoc_init_and_the_call_cap():
    assert refs(scan_viewer_script("Redoc.init('/api/spec.yaml', {}, document.body)")) == [('/api/spec.yaml', 'redoc_config')]
    assert scan_viewer_script('Redoc.init(specUrl, {})').unsupported[0][0] == 'url_not_literal'
    many = ';'.join(["SwaggerUI({url: '/s.json'})"] * 25)
    assert 'too_many_calls' in {c for c, _ in scan_viewer_script(many).unsupported}


def test_scripts_without_viewer_calls_yield_nothing():
    scan = scan_viewer_script("analytics.url = '/openapi.json'; var SwaggerUIBundleX = 1;")
    assert scan == scan_viewer_script('') and scan.description_urls == ()


# --- the JSON a configUrl serves ---------------------------------------------------------

def test_springdoc_style_swagger_config():
    config = {'configUrl': '/v3/api-docs/swagger-config', 'oauth2RedirectUrl': 'x', 'validatorUrl': '',
              'urls': [{'url': '/v3/api-docs/orders', 'name': 'orders'}, {'url': '/v3/api-docs/billing', 'name': 'billing'}]}
    scan = scan_swagger_config_json(json.dumps(config).encode())
    assert refs(scan) == [('/v3/api-docs/orders', 'swagger_config_url_entry'), ('/v3/api-docs/billing', 'swagger_config_url_entry')]
    assert scan.config_urls == ()  # a configuration file does not chain to another


def test_config_json_with_url_and_embedded_spec():
    scan = scan_swagger_config_json(json.dumps({'url': '/openapi.json', 'spec': {'openapi': '3.1.0'}}).encode())
    assert refs(scan) == [('/openapi.json', 'swagger_config_url_entry')] and json.loads(scan.embedded_specs[0]) == {'openapi': '3.1.0'}


@pytest.mark.parametrize('content', [b'', b'not json', b'[]', b'{}', b'{"theme": "dark"}', b'\xff', b'null'])
def test_other_json_is_not_a_swagger_configuration(content):
    assert scan_swagger_config_json(content) is None


def test_malformed_entries_in_a_configuration_are_reported():
    scan = scan_swagger_config_json(json.dumps({'urls': [{'url': '/a.json'}, {'name': 'x'}, 'junk', {'url': ' '}], 'spec': 'x'}).encode())
    assert refs(scan) == [('/a.json', 'swagger_config_url_entry')]
    assert {c for c, _ in scan.unsupported} == {'urls_entry_invalid', 'spec_not_an_object'}
    assert scan_swagger_config_json(b'{"urls": "x"}').unsupported[0][0] == 'urls_not_a_list'


# --- RequireJS / apiDoc ---------------------------------------------------------------

def test_requirejs_main_that_names_apidoc_modules():
    main = ("require.config({paths: {'jquery': './vendor/jquery.min'}});\n"
            "require(['./api_project.js', './api_data.js', 'jquery'], function(project, data) {});")
    assert scan_requirejs_main(main, 'https://www.fruityvice.com/doc/main.js') == (
        'https://www.fruityvice.com/doc/api_project.js', 'https://www.fruityvice.com/doc/api_data.js')


def test_module_ids_without_an_extension_resolve_against_the_data_main_directory():
    assert scan_requirejs_main("define(['api_data']);", 'https://x.test/doc/js/main.js') == ('https://x.test/doc/js/api_data.js',)


@pytest.mark.parametrize('script', ['', "require(['jquery', 'lodash']);", 'var api_data = 1;', "require(['https://evil/x.js'])"])
def test_requirejs_scripts_that_do_not_name_apidoc_yield_nothing(script):
    assert scan_requirejs_main(script, 'https://x.test/main.js') == ()


# --- initializer file names ---------------------------------------------------------------

@pytest.mark.parametrize('url,expected', [
    ('https://x.test/swagger-initializer.js', True), ('https://x.test/docs/swagger-ui-init.js', True),
    ('https://x.test/swagger-config.js', True), ('https://x.test/redoc-init.js', True), ('https://x.test/api-docs-config.js', True),
    ('https://x.test/openapi-setup.min.js', True), ('https://x.test/swagger-ui-bundle.js', False),
    ('https://x.test/swagger-ui-standalone-preset.js', False), ('https://x.test/app.4f3a.js', False),
    ('https://x.test/vendor.js', False), ('https://x.test/swagger-config.json', False), ('https://x.test/', False),
])
def test_only_small_initializer_scripts_are_candidates_for_fetching(url, expected):
    assert is_initializer(url) is expected


# --- page structure -----------------------------------------------------------------------

def test_a_page_is_scanned_for_everything_navigation_needs():
    html = ('<html><head><title> Acme  API </title><base href="/docs/"></head><body><h1>Acme API</h1><h2>Reference</h2>'
            '<a href="guide">Guide</a><a href="https://other.test/x">Other</a><a href="#top">top</a><a href="mailto:a@b.c">mail</a>'
            '<script src="vendor/require.min.js" data-main="main"></script><script src="swagger-initializer.js"></script>'
            '<script>SwaggerUIBundle({url: "/openapi.json"})</script><redoc spec-url="/r.json"></redoc>'
            '<div id="swagger-ui" data-url="spec.yaml"></div></body></html>')
    page = scan_page(html, PAGE)
    assert page.title == 'Acme API' and page.headings == ('Acme API', 'Reference') and page.base_url == 'https://api.acme.com/docs/'
    assert page.anchors == (('https://api.acme.com/docs/guide', 'Guide'), ('https://other.test/x', 'Other'))
    assert page.script_srcs == ('https://api.acme.com/docs/vendor/require.min.js', 'https://api.acme.com/docs/swagger-initializer.js')
    assert page.data_main == ('https://api.acme.com/docs/main.js',)  # RequireJS adds the .js extension
    assert page.inline_scripts == ('SwaggerUIBundle({url: "/openapi.json"})',)
    assert page.spec_attributes == ('https://api.acme.com/r.json', 'https://api.acme.com/docs/spec.yaml')


@pytest.mark.parametrize('html', ['', '<<<>>>', '<a href="/x"', '<script>', '<script data-main="main.js"', '<h1>unclosed', '\x00'])
def test_malformed_pages_never_raise(html):
    assert scan_page(html, PAGE).page_url == PAGE


def test_page_scanning_is_bounded():
    html = ''.join(f'<a href="/p{i}">Page {i}</a>' for i in range(600)) + ''.join(f'<script>x{i}()</script>' for i in range(50))
    page = scan_page(html, PAGE)
    assert len(page.anchors) == 400 and len(page.inline_scripts) == 20
    big = scan_page('<script>' + 'x' * 500_000 + '</script>', PAGE)
    assert len(big.inline_scripts[0]) == 200_000


@settings(max_examples=150, deadline=None)
@given(st.text(max_size=400))
def test_arbitrary_text_never_breaks_the_scanners(text):
    scan_page(text, PAGE)
    scan_viewer_script(text)
    scan_requirejs_main(text, PAGE)
    scan_swagger_config_json(text.encode('utf-8', 'replace'))
