from datetime import datetime, timezone

import pytest

from radar.discovery.documentation import (
    DocumentationLimits, _static_config_urls, extract_document_links,
)
from radar.discovery.fetch import FetchAttempt, FetchFailure, FetchResult


PAGE = 'https://api.acme.com/docs'


def page(html, content_type='text/html; charset=utf-8', url=PAGE):
    content = html if isinstance(html, bytes) else html.encode()
    return FetchResult(url, url, 200, content_type, content, datetime.now(timezone.utc),
                       (FetchAttempt(url, 200),))


def extract(html, **options):
    candidates, notes = extract_document_links(page(html, **options), DocumentationLimits())
    return [(c.source_url, c.discovery_method) for c in candidates], [n.code for n in notes]


def test_explicit_filename_link_is_a_candidate_with_provenance_and_origin():
    candidates, notes = extract_document_links(page('<a href="/v2/openapi.json">Download</a>'), DocumentationLimits())
    (candidate,) = candidates
    assert candidate.source_url == 'https://api.acme.com/v2/openapi.json'
    assert candidate.discovery_method == 'documentation_link' and candidate.discovery_source == PAGE
    assert [e.criterion for e in candidate.evidence] == ['documentation_reference', 'candidate_origin']
    assert 'same-origin' in candidate.evidence[1].description
    assert candidate.limitations and notes == ()


def test_cross_origin_candidate_is_marked_but_still_returned():
    (candidate,), _ = extract_document_links(
        page('<a href="https://raw.githubusercontent.com/acme/specs/main/openapi.yaml">spec</a>'), DocumentationLimits())
    assert 'cross-origin' in candidate.evidence[1].description


def test_label_alone_is_not_enough_but_label_plus_spec_extension_is():
    assert extract('<a href="/swagger-ui/index.html">Swagger UI</a><a href="https://openapis.org">OpenAPI</a>') == ([], [])
    assert extract('<a href="/files/acme.yaml">OpenAPI definition</a>')[0] == [
        ('https://api.acme.com/files/acme.yaml', 'documentation_link')]


def test_plain_json_downloads_are_ignored():
    assert extract('<a href="/data/users.json">Sample users</a><a href="/changelog.json">Changelog</a>') == ([], [])


def test_relative_resolution_and_duplicates():
    found, _ = extract('<a href="openapi.json">a</a><a href="/openapi.json">b</a><a href="../docs/openapi.json">c</a>')
    assert found == [('https://api.acme.com/openapi.json', 'documentation_link'),
                     ('https://api.acme.com/docs/openapi.json', 'documentation_link')]


@pytest.mark.parametrize('href', ['#openapi.json', '', 'mailto:a@b.c/openapi.json', 'ftp://acme.com/openapi.json',
                                  'https://user:pw@acme.com/openapi.json', 'https://acme.com/openapi.json#top'])
def test_unsafe_or_unsupported_links_are_ignored_with_a_note(href):
    found, notes = extract(f'<a href="{href}">spec</a>')
    assert found == []
    assert notes in ([], ['invalid_link'])


def test_base_element_prevents_guessing_relative_urls():
    found, notes = extract('<base href="/other/"><a href="openapi.json">x</a><a href="https://x.test/openapi.json">y</a>')
    assert found == [('https://x.test/openapi.json', 'documentation_link')]
    assert notes == ['unsupported_base_url']


def test_swagger_ui_javascript_literal_as_generated_by_common_frameworks():
    found, notes = extract("""<script>
        const ui = SwaggerUIBundle({
          url: '/openapi.json', dom_id: '#swagger-ui', // trailing comment
          presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
          layout: "BaseLayout", deepLinking: true, showExtensions: true,
          oauth2RedirectUrl: window.location.origin + '/docs/oauth2-redirect',
          requestInterceptor: function (r) { r.headers['x'] = "}"; return r; },
        })</script>""")
    assert found == [('https://api.acme.com/openapi.json', 'swagger_ui_config')] and notes == []


def test_swagger_ui_strict_json_and_urls_list():
    found, notes = extract('<script>SwaggerUI({"url": "/a.json"}); '
                           "SwaggerUIBundle({urls: [{url: '/v1.json', name: 'v1'}, {url: \"/v2.json\"}]})</script>")
    assert [url for url, _ in found] == [
        'https://api.acme.com/a.json', 'https://api.acme.com/v1.json', 'https://api.acme.com/v2.json']
    assert notes == []


def test_url_and_urls_together_are_both_reported_without_choosing():
    urls, unsupported = _static_config_urls("SwaggerUIBundle({url: '/a.json', urls: [{url: '/b.json'}]})")
    assert [u for u, _ in urls] == ['/a.json', '/b.json'] and not unsupported


@pytest.mark.parametrize('script', [
    "SwaggerUIBundle({url: BASE + '/openapi.json'})",
    "SwaggerUIBundle({url: getUrl()})",
    "SwaggerUIBundle({url: `${base}/openapi.json`})",
    "SwaggerUIBundle({url: 'a.json' + suffix})",
    "SwaggerUIBundle({urls: buildUrls()})",
    "SwaggerUIBundle({urls: [{name: 'x'}]})",
    "SwaggerUIBundle({configUrl: '/swagger-config.json', url: '/openapi.json'})",
    "SwaggerUIBundle({spec: {openapi: '3.0.0'}, url: '/openapi.json'})",
    "SwaggerUIBundle({queryConfigEnabled: true, url: '/openapi.json'})",
    "SwaggerUIBundle({dom_id: '#ui'})",
    "SwaggerUIBundle(config)",
    "SwaggerUIBundle({url: '/openapi.json'",  # truncated
    "SwaggerUIBundle({url: 'line\nbreak.json'})",
    "SwaggerUIBundle({url: '\\u0041.json'})",
    "SwaggerUIBundle({/* unterminated comment url: '/openapi.json'})",
])
def test_dynamic_or_overridden_configuration_is_reported_not_guessed(script):
    found, notes = extract(f'<script>{script}</script>')
    assert found == [] and notes == ['unsupported_configuration']


def test_query_config_disabled_is_allowed():
    urls, unsupported = _static_config_urls("SwaggerUIBundle({queryConfigEnabled: false, url: '/openapi.json'})")
    assert urls == (('/openapi.json', 'swagger_ui_config'),) and not unsupported


def test_swagger_calls_and_config_size_are_bounded():
    many = ';'.join(["SwaggerUI({url: '/s.json'})"] * 25)
    urls, unsupported = _static_config_urls(many)
    assert len(urls) == 20 and unsupported
    huge = "SwaggerUIBundle({junk: '" + 'x' * 60_000 + "', url: '/late.json'})"
    assert _static_config_urls(huge) == ((), True)


def test_other_script_text_is_not_executed_or_matched():
    assert extract("<script>const SwaggerUIBundleX = 1; analytics.url = '/openapi.json'</script>") == ([], [])
    assert extract('<script src="/swagger-initializer.js"></script>') == ([], [])


def test_redoc_element_and_init():
    found, notes = extract('<redoc spec-url="/redoc.json"></redoc><rapi-doc spec-url="https://cdn.acme.com/r.yaml"></rapi-doc>'
                           "<script>Redoc.init('/init.json', {}, document.body)</script>")
    assert found == [
        ('https://api.acme.com/redoc.json', 'spec_url_attribute'),
        ('https://cdn.acme.com/r.yaml', 'spec_url_attribute'),
        ('https://api.acme.com/init.json', 'redoc_config'),
    ] and notes == []
    assert extract('<script>Redoc.init(specUrl, {})</script>') == ([], ['unsupported_configuration'])


def test_candidate_limit_per_page_is_reported():
    links = ''.join(f'<a href="/s{i}/openapi.json">x</a>' for i in range(5))
    candidates, notes = extract_document_links(page(links), DocumentationLimits(max_candidates=2))
    assert len(candidates) == 2 and [n.code for n in notes] == ['candidate_limit']


@pytest.mark.parametrize('document,code', [
    (page(b'{}', content_type='application/json'), 'unsupported_media_type'),
    (page('<a>', content_type=None), 'unsupported_media_type'),
    (page(b'<a href="/openapi.json">\xff</a>'), 'unsupported_encoding'),
    (page(b'x' * 20), 'page_size_limit'),
])
def test_unsupported_pages_are_noted(document, code):
    limits = DocumentationLimits(max_page_bytes=10) if code == 'page_size_limit' else DocumentationLimits()
    candidates, notes = extract_document_links(document, limits)
    assert candidates == () and [n.code for n in notes] == [code]


def test_failed_fetch_produces_nothing():
    failed = FetchResult(PAGE, PAGE, 404, None, None, None, (FetchAttempt(PAGE, 404),), FetchFailure('http_error', 'x'))
    assert extract_document_links(failed, DocumentationLimits()) == ((), ())


@pytest.mark.parametrize('html', ['', '<<<>>>', '<a href="/openapi.json">', '<script>SwaggerUI(', '<a><a href="x">',
                                  '<script>' + 'SwaggerUI({' * 5000 + '</script>'])
def test_malformed_html_never_raises(html):
    extract(html)


def test_deeply_nested_literal_does_not_crash():
    script = 'SwaggerUIBundle({a: ' + '[' * 3000 + ']' * 3000 + ", url: '/openapi.json'})"
    urls, _ = _static_config_urls(script)
    assert urls in ((), (('/openapi.json', 'swagger_ui_config'),))
