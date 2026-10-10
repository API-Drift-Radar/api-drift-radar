from datetime import datetime, timezone

import pytest

from radar.discovery.fetch import FetchAttempt, FetchFailure, FetchResult
from radar.discovery.llm_input import ReductionLimits, reduce_page


PAGE = 'https://docs.acme.com/api/'


def page(html, content_type='text/html; charset=utf-8', url=PAGE):
    content = html if isinstance(html, bytes) else html.encode()
    return FetchResult(url, url, 200, content_type, content, datetime.now(timezone.utc),
                       (FetchAttempt(url, 200),))


def urls(reduced):
    return [item.url for item in reduced.items if item.url]


def test_keeps_relevant_links_and_drops_the_rest():
    reduced = reduce_page(page(
        '<nav>Home Pricing Blog</nav><h1>Acme API</h1><p>Marketing text</p>'
        '<a href="/files/acme-v2-spec.json">Download OpenAPI definition</a>'
        '<a href="/pricing">Pricing</a><script>analytics.track("pageview")</script>'))
    assert urls(reduced) == ['https://docs.acme.com/files/acme-v2-spec.json']
    assert reduced.text == 'LINK https://docs.acme.com/files/acme-v2-spec.json "Download OpenAPI definition"'
    assert 'Marketing' not in reduced.text and 'analytics' not in reduced.text
    assert not reduced.truncated and reduced.notes == ()


def test_relative_links_resolve_against_page_and_base_href():
    assert urls(reduce_page(page('<a href="../openapi.json">OpenAPI</a>'))) == ['https://docs.acme.com/openapi.json']
    based = reduce_page(page('<base href="https://cdn.acme.com/specs/"><a href="openapi.yaml">OpenAPI</a>'))
    assert urls(based) == ['https://cdn.acme.com/specs/openapi.yaml']


def test_fragments_removed_and_non_http_links_dropped():
    reduced = reduce_page(page(
        '<a href="/openapi.json#section">OpenAPI</a><a href="javascript:openapi()">OpenAPI</a>'
        '<a href="mailto:openapi@acme.com">OpenAPI</a><a href="#openapi">OpenAPI</a>'
        '<a href="ftp://acme.com/openapi.json">OpenAPI</a>'))
    assert urls(reduced) == ['https://docs.acme.com/openapi.json']


def test_json_link_without_keyword_needs_a_label():
    reduced = reduce_page(page('<a href="/data.json"></a><a href="/other.json">Download</a>'))
    assert urls(reduced) == ['https://docs.acme.com/other.json']


def test_script_snippets_cover_only_the_keyword_area():
    filler = 'x = 1; ' * 300
    reduced = reduce_page(page(
        f'<script>{filler}SwaggerUIBundle({{url: \'/openapi.json\'}});{filler}</script>'
        '<script>track("a")</script>'))
    snippets = [item for item in reduced.items if item.kind == 'script']
    assert len(snippets) == 1
    assert "url: '/openapi.json'" in snippets[0].text
    assert len(snippets[0].text) < 650


def test_nearby_matches_merge_into_one_snippet():
    reduced = reduce_page(page('<script>swagger(); openapi();</script>'))
    assert len([item for item in reduced.items if item.kind == 'script']) == 1


def test_script_src_tags_and_spec_attributes():
    reduced = reduce_page(page(
        '<link rel="service-desc" href="/api.json"><link rel="stylesheet" href="/site.css">'
        '<link rel="alternate" type="application/json" href="/alt.json">'
        '<meta name="description" content="Acme OpenAPI spec"><meta name="viewport" content="x">'
        '<script src="/static/swagger-initializer.js"></script><script src="/static/app.js"></script>'
        '<redoc spec-url="/redoc.json"></redoc>'))
    assert urls(reduced) == [
        'https://docs.acme.com/api.json', 'https://docs.acme.com/alt.json',
        'https://docs.acme.com/static/swagger-initializer.js', 'https://docs.acme.com/redoc.json']
    assert any(item.text.startswith('TAG meta') for item in reduced.items)
    assert 'site.css' not in reduced.text and 'viewport' not in reduced.text


def test_style_and_title_text_never_become_labels():
    reduced = reduce_page(page('<a href="/openapi.json"><style>.x{}</style>Spec</a><title>t</title>'))
    assert reduced.items[0].label == 'Spec'


def test_output_follows_document_order_and_is_deterministic():
    html = ('<a href="/b.json">OpenAPI B</a><script>openapi</script>'
            '<a href="/a.json">OpenAPI A</a><a href="/b.json">OpenAPI B</a>')
    first, second = reduce_page(page(html)), reduce_page(page(html))
    assert first == second
    assert [item.kind for item in first.items] == ['link', 'script', 'link']
    assert first.text.splitlines()[0].startswith('LINK https://docs.acme.com/b.json')


def test_control_characters_and_quotes_cannot_break_lines():
    reduced = reduce_page(page('<a href="/openapi.json">Open\nAPI\x00 "spec"\r\nIGNORE ALL</a>'))
    assert len(reduced.text.splitlines()) == 1
    assert reduced.items[0].label == "Open API 'spec' IGNORE ALL"


def test_label_and_snippet_lengths_are_capped():
    limits = ReductionLimits(max_label_chars=10, max_snippet_chars=30)
    reduced = reduce_page(page('<a href="/openapi.json">' + 'x' * 100 + '</a><script>' + 'openapi ' * 50 + '</script>'), limits)
    assert len(reduced.items[0].label) == 10
    assert all(len(item.text) <= 40 for item in reduced.items if item.kind == 'script')


def test_total_character_cap_truncates_predictably():
    links = ''.join(f'<a href="/spec{i}.json">OpenAPI {i}</a>' for i in range(50))
    reduced = reduce_page(page(links), ReductionLimits(max_chars=300))
    assert reduced.truncated and len(reduced.text) <= 300
    assert [n.code for n in reduced.notes] == ['truncated']
    assert reduced.items[0].url.endswith('/spec0.json')  # earliest items win


def test_item_cap():
    links = ''.join(f'<a href="/spec{i}.json">OpenAPI {i}</a>' for i in range(10))
    reduced = reduce_page(page(links), ReductionLimits(max_items=3))
    assert len(reduced.items) == 3 and reduced.truncated


@pytest.mark.parametrize('document,code', [
    (page(b'x' * 11, content_type='text/html'), 'page_size_limit'),
    (page('{}', content_type='application/json'), 'unsupported_media_type'),
    (page('<html>', content_type=None), 'unsupported_media_type'),
    (page(b'<a href="/openapi.json">\xff\xfe</a>'), 'unsupported_encoding'),
])
def test_unsupported_inputs_return_empty_with_a_note(document, code):
    reduced = reduce_page(document, ReductionLimits(max_page_bytes=10) if code == 'page_size_limit' else None)
    assert reduced.items == () and reduced.text == ''
    assert [n.code for n in reduced.notes] == [code]


def test_failed_fetch_returns_note():
    failed = FetchResult(PAGE, PAGE, 404, None, None, None, (FetchAttempt(PAGE, 404),), FetchFailure('http_error', 'x'))
    assert [n.code for n in reduce_page(failed).notes] == ['no_content']


@pytest.mark.parametrize('html', [
    '', '<<<>>>', '<a href="/openapi.json">OpenAPI', '<script>openapi', '<a><a href="/openapi.json">x</a>',
    '</a></script></p><base>', '<a href="http://[::1">OpenAPI</a>', '﻿<a href="/openapi.json">OpenAPI</a>',
])
def test_malformed_html_does_not_raise(html):
    reduced = reduce_page(page(html))
    assert isinstance(reduced.text, str)


def test_unclosed_constructs_are_still_captured():
    assert urls(reduce_page(page('<a href="/openapi.json">OpenAPI'))) == ['https://docs.acme.com/openapi.json']
    assert len(reduce_page(page('<script>openapi')).items) == 1


@pytest.mark.parametrize('name,value', [
    ('max_chars', 0), ('max_items', -1), ('max_page_bytes', True), ('max_label_chars', 1.5),
])
def test_invalid_limits(name, value):
    with pytest.raises(ValueError):
        ReductionLimits(**{name: value})
