from datetime import datetime, timezone
import json
from unittest.mock import patch

import pytest

from radar.discovery.fetch import FetchAttempt, FetchFailure, FetchResult
from radar.discovery.limits import BudgetExceeded, DiscoveryBudget, FetchLimits
from radar.discovery.link_scoring import MIN_SCORE, score_link
from radar.discovery.navigation import FRAMEWORK_PROBES, Lead, NavigationLimits, Navigator, navigate
from radar.domain.discovery import DiscoveryRequest

HOST = 'https://api.acme.com'
NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)


def page(body, **headers):
    return ('html', body, headers)


class Site:
    """Fake network. URL -> ('html', body, headers) | ('js', text) | ('json', obj) | ('status', code) | bytes."""

    def __init__(self, files):
        self.files = {(HOST + k) if k.startswith('/') else k: v for k, v in files.items()}
        self.calls, self.options = [], []

    def fetch(self, url, budget, **options):
        self.calls.append(url)
        self.options.append((url, options))
        try:
            budget.claim_request(url.split('/')[2] + ':443')
        except BudgetExceeded as error:
            return FetchResult(url, url, None, None, None, None, (), FetchFailure(error.code, 'limit'))
        value = self.files.get(url, ('status', 404))
        if isinstance(value, tuple) and value[0] == 'status':
            return FetchResult(url, url, value[1], None, None, None, (FetchAttempt(url, value[1]),),
                               FetchFailure('http_error', f'HTTP status {value[1]}.'))
        kind, headers = 'application/json', {}
        if isinstance(value, bytes):
            content = value
        elif isinstance(value, dict):
            content = json.dumps(value).encode()
        elif value[0] == 'html':
            kind, content, headers = 'text/html', value[1].encode(), value[2]
        elif value[0] == 'js':
            kind, content = 'application/javascript', value[1].encode()
        else:
            content = json.dumps(value[1]).encode()
        return FetchResult(url, url, 200, kind, content, NOW, (FetchAttempt(url, 200),), link_header=headers.get('link'))


def run(files, target='api.acme.com', explore=True, limits=None, budget=None, seed=(), **kwargs):
    site = Site(files)
    budget = budget or DiscoveryBudget(FetchLimits(max_requests=60, discovery_timeout=60))
    with patch('radar.discovery.candidates.fetch_document', side_effect=site.fetch):
        nav = Navigator(DiscoveryRequest(target), budget, limits=limits, explore=explore, **kwargs)
        nav.seed(seed)
        nav.run()
    return nav, nav.result(), site


def fetched(result):
    return [r.url.replace(HOST, '') for r in result.trail if r.outcome == 'fetched']


# --- ordering: explicit evidence before navigation before guessing ---------------------------------

def test_explicit_publication_is_examined_before_page_navigation_and_both_before_probes():
    files = {'/': page('<a href="/developers/api">Developer API</a>', link='</meta/desc.json>; rel="service-desc"'),
             '/meta/desc.json': {'openapi': '3.0.3'}, '/developers/api': page('<p>hello</p>')}
    _, result, site = run(files)
    order = [u.replace(HOST, '') for u in site.calls]
    assert order[:2] == ['/.well-known/api-catalog', '/']  # catalogue (published) first, then the root that may name more
    assert order.index('/meta/desc.json') < order.index('/developers/api') < order.index('/v3/api-docs')
    assert order.index('/v3/api-docs') >= order.index('/developers/api')


def test_every_lead_records_its_parent_and_mechanism():
    files = {'/': page('<a href="/developers/api">Developer API</a>', link='</meta/desc.json>; rel="service-desc"'),
             '/meta/desc.json': {'openapi': '3.0.3'}, '/developers/api': page('<p>x</p>')}
    _, result, _ = run(files)
    steps = {r.url.replace(HOST, ''): r for r in result.trail}
    assert (steps['/meta/desc.json'].mechanism, steps['/meta/desc.json'].parent_url, steps['/meta/desc.json'].kind) == (
        'service_desc_link', HOST + '/', 'description')
    assert (steps['/developers/api'].mechanism, steps['/developers/api'].parent_url, steps['/developers/api'].depth) == (
        'documentation_navigation', HOST + '/', 1)
    assert (steps['/'].mechanism, steps['/'].parent_url, steps['/'].depth) == ('origin_root', None, 0)


def test_a_url_reached_by_two_routes_is_fetched_once():
    files = {'/': page('<a href="/developers/a">Developer API a</a><a href="/developers/b">Developer API b</a>'),
             '/developers/a': page('<a href="/developers/b">Developer API b</a><a href="/">home api</a>'),
             '/developers/b': page('<a href="/developers/a">Developer API a</a>')}
    _, _, site = run(files)
    assert all(site.calls.count(u) == 1 for u in set(site.calls))


def test_candidates_carry_the_navigation_path_as_evidence():
    files = {'/': page('<a href="/developers/api">Developer API</a>'),
             '/developers/api': page('<a href="/files/spec.json">OpenAPI definition</a>'), '/files/spec.json': {'openapi': '3.0.3'}}
    nav, result, _ = run(files)
    (candidate,) = result.candidates
    assert candidate.candidate.discovery_method == 'documentation_link'
    path = candidate.candidate.evidence[0].description
    assert path.index('origin_root') < path.index('documentation_navigation') < path.index('documentation_link')
    assert candidate.candidate.discovery_source == HOST + '/developers/api'


def test_the_trail_keeps_the_first_route_that_reached_a_url_and_does_not_rewrite_it():
    files = {'/': page('<a href="/developers/a">Developer API a</a><a href="/developers/b">Developer API b</a>'),
             '/developers/a': page('<a href="/developers/c">Developer API c</a>'),
             '/developers/b': page('<a href="/developers/c">Developer API c</a>'), '/developers/c': page('<p>x</p>')}
    nav, result, _ = run(files)
    steps = [r for r in result.trail if r.url == HOST + '/developers/c']
    assert len(steps) == 1 and steps[0].parent_url == HOST + '/developers/a'  # a was the first to reach it
    assert nav._leads[HOST + '/developers/c'].parent == HOST + '/developers/a'


def test_adding_a_known_url_again_never_rewrites_its_recorded_parent_or_mechanism():
    nav = Navigator(DiscoveryRequest('api.acme.com'), DiscoveryBudget(FetchLimits(max_requests=60)))
    url = HOST + '/meta/desc.json'
    assert nav.add(url, 'description', 'service_desc_link', HOST + '/a', 1, 5) is True
    assert nav.add(url, 'description', 'api_catalog', HOST + '/b', 1, 4) is False  # another route, even a better one
    lead = nav._leads[url]
    assert (lead.parent, lead.mechanism, lead.priority) == (HOST + '/a', 'service_desc_link', 5)
    nav._visited.add(url)  # once fetched, it can never be queued or re-attributed either
    assert nav.add(url, 'description', 'framework_probe', None, 0, 90) is False and nav._leads[url].parent == HOST + '/a'


def test_descendants_report_the_path_through_the_first_route_that_reached_their_parent():
    files = {'/': page('<link rel="service-doc" href="/guide"><a href="/developers/a">Developer API a</a>'),
             '/developers/a': page('<link rel="service-doc" href="/guide">'),
             '/guide': page('<a href="/files/spec.json">OpenAPI definition</a>'), '/files/spec.json': {'openapi': '3.0.3'}}
    _, result, _ = run(files)
    (candidate,) = result.candidates
    path = candidate.candidate.evidence[0].description
    assert path.count('service_doc_link') == 1 and '/developers/a' not in path  # via the root's link, found first


# --- limits -------------------------------------------------------------------------------------------

def chain(depth_count):
    files = {'/': page('<a href="/developers/1">Developer API 1</a>')}
    for i in range(1, depth_count + 1):
        files[f'/developers/{i}'] = page(f'<a href="/developers/{i + 1}">Developer API {i + 1}</a>')
    return files


def test_page_depth_is_bounded_and_the_skipped_lead_is_recorded():
    _, result, _ = run(chain(8), limits=NavigationLimits(max_depth=2))
    assert fetched(result) == ['/.well-known/api-catalog', '/', '/developers/1', '/developers/2'] or \
        {'/', '/developers/1', '/developers/2'} <= set(fetched(result))
    assert '/developers/3' not in fetched(result)
    assert any(r.outcome == 'skipped_depth' for r in result.trail) and 'depth_limit' in result.limits_reached
    assert result.unexamined and all(r.outcome in ('skipped_depth', 'skipped_limit', 'not_examined') for r in result.unexamined)


def test_descriptions_may_sit_one_hop_beyond_the_deepest_page():
    files = chain(2)
    files['/developers/2'] = page('<a href="/files/spec.json">OpenAPI definition</a>')
    files['/files/spec.json'] = {'openapi': '3.0.3'}
    _, result, _ = run(files, limits=NavigationLimits(max_depth=2))
    assert len(result.candidates) == 1 and 'depth_limit' not in result.limits_reached


def test_the_page_limit_stops_fetching_pages_but_not_descriptions():
    files = {'/': page(''.join(f'<a href="/developers/{i}">Developer API {i}</a>' for i in range(5)))}
    for i in range(5):
        files[f'/developers/{i}'] = page('<p>x</p>')
    _, result, _ = run(files, limits=NavigationLimits(max_pages=3, max_links_per_page=5))
    assert sum(1 for r in result.trail if r.outcome == 'fetched' and r.kind == 'page') <= 3
    assert 'page_limit' in result.limits_reached and any(r.outcome == 'skipped_limit' for r in result.trail)


def test_the_lead_limit_bounds_how_many_leads_are_ever_queued():
    files = {'/': page(''.join(f'<a href="/developers/{i}">Developer API {i}</a>' for i in range(30)))}
    nav, result, _ = run(files, limits=NavigationLimits(max_leads=10, max_links_per_page=30))
    assert 'lead_limit' in result.limits_reached and len(nav._leads) <= 10


def test_links_followed_from_one_page_are_capped_and_best_first():
    links = ''.join(f'<a href="/developers/{i}">Developer API {i}</a>' for i in range(10))
    links += '<a href="/z/openapi-spec">OpenAPI specification</a>'
    _, result, _ = run({'/': page(links)}, limits=NavigationLimits(max_links_per_page=3))
    followed = [r.url for r in result.trail if r.mechanism == 'documentation_navigation']
    assert len(followed) == 3 and HOST + '/z/openapi-spec' in followed  # the best-scoring link is always among them


def test_the_budget_reserve_and_stop_are_respected_and_unexamined_leads_are_recorded():
    budget = DiscoveryBudget(FetchLimits(max_requests=8, reference_reserve=4))
    files = {'/': page(''.join(f'<a href="/developers/{i}">Developer API {i}</a>' for i in range(5)))}
    nav, result, site = run(files, budget=budget)
    assert budget.requests_used == 4 and 'navigation_limit' in result.limits_reached
    assert any(r.outcome == 'not_examined' for r in result.trail) and result.unexamined
    assert nav._stopped


def test_the_host_limit_refuses_new_hosts_only_and_the_run_continues():
    others = [f'https://docs{i}.example.net' for i in range(3)]
    files = {'/': page(''.join(f'<a href="{o}/developers/api">Developer API {i}</a>' for i, o in enumerate(others)) + '<a href="/developers/own">Developer API own</a>'),
             '/developers/own': page('<p>x</p>')}
    for o in others:
        files[f'{o}/developers/api'] = page('<p>x</p>')
    budget = DiscoveryBudget(FetchLimits(max_requests=40, max_hosts=2))
    _, result, _ = run(files, budget=budget)
    assert len(budget.hosts) == 2 and 'host_limit' in result.limits_reached
    assert '/developers/own' in fetched(result)  # a host already contacted is never refused


# --- explore: explicit evidence always, guessing only on request ---------------------------------------------

def test_without_explore_only_explicit_evidence_is_followed():
    seed = FetchResult(HOST + '/docs', HOST + '/docs', 200, 'text/html',
                       b'<a href="/developers/api">Developer API</a><a href="/files/spec.json">OpenAPI definition</a>', NOW, ())
    files = {'/files/spec.json': {'openapi': '3.0.3'}, '/developers/api': page('x')}
    _, result, site = run(files, explore=False, seed=[seed])
    calls = [u.replace(HOST, '') for u in site.calls]
    assert calls == ['/.well-known/api-catalog', '/files/spec.json']  # no origin root, no probes, no navigation link
    assert len(result.candidates) == 1


def test_explore_adds_the_origin_root_and_a_capped_set_of_framework_probes():
    _, _, site = run({}, target='https://api.acme.com/platform/api/orders')
    calls = [u.replace(HOST, '') for u in site.calls]
    probes = [c for c in calls if any(c.endswith(p) for p in FRAMEWORK_PROBES)]
    assert set(probes) == {ctx + p for ctx in ('', '/platform') for p in FRAMEWORK_PROBES} and len(probes) == 8
    assert not any(c.startswith('/platform/api') for c in calls) and '/' in calls


def test_a_target_without_a_path_gets_only_root_probes():
    _, _, site = run({})
    probes = [u.replace(HOST, '') for u in site.calls if any(u.endswith(p) for p in FRAMEWORK_PROBES)]
    assert probes == list(FRAMEWORK_PROBES)


def test_the_probe_set_is_configurable_and_validated():
    _, _, site = run({}, limits=NavigationLimits(framework_probes=('/custom/spec',), max_probe_contexts=1))
    assert [u for u in site.calls if 'custom' in u] == [HOST + '/custom/spec']
    for bad in (('v3/api-docs',), ['/x'], (3,)):
        with pytest.raises(ValueError):
            NavigationLimits(framework_probes=bad)


def test_the_targets_own_endpoint_is_never_fetched():
    _, _, site = run({}, target='https://api.acme.com/v1/orders/42')
    assert HOST + '/v1/orders/42' not in site.calls


# --- scripts, configuration and bundles ------------------------------------------------------------------------

SWAGGER_PAGE = ('<script src="/docs/swagger-ui-bundle.js"></script><script src="/docs/swagger-ui-standalone-preset.js"></script>'
                '<script src="/docs/swagger-initializer.js"></script><script src="/static/app.4f3a.js"></script>'
                '<script src="/docs/redoc-init.js"></script><script src="/docs/swagger-config.js"></script>')


def test_only_small_initializer_files_are_fetched_never_bundles_and_at_most_two_per_page():
    seed = FetchResult(HOST + '/docs', HOST + '/docs', 200, 'text/html', SWAGGER_PAGE.encode(), NOW, ())
    files = {'/docs/swagger-initializer.js': ('js', 'SwaggerUIBundle({url: "/v1/openapi.json"})'),
             '/docs/redoc-init.js': ('js', "Redoc.init('/v2/openapi.json')"), '/docs/swagger-config.js': ('js', ''),
             '/v1/openapi.json': {'openapi': '3.0.3'}, '/v2/openapi.json': {'openapi': '3.0.3'}}
    _, result, site = run(files, explore=False, seed=[seed])
    fetched_scripts = [u.replace(HOST, '') for u in site.calls if u.endswith('.js')]
    assert fetched_scripts == ['/docs/swagger-initializer.js', '/docs/redoc-init.js']  # two, in page order
    assert all('bundle' not in u and 'preset' not in u and 'app.4f3a' not in u for u in site.calls)
    assert {c.candidate.discovery_method for c in result.candidates} == {'swagger_ui_config', 'redoc_config'}


def test_scripts_and_configuration_are_fetched_with_a_small_size_cap():
    seed = FetchResult(HOST + '/docs', HOST + '/docs', 200, 'text/html',
                       b'<script src="/docs/swagger-initializer.js"></script>', NOW, ())
    files = {'/docs/swagger-initializer.js': ('js', 'SwaggerUIBundle({configUrl: "/docs/c.json"})'),
             '/docs/c.json': ('json', {'urls': [{'url': '/v1/openapi.json'}]}), '/v1/openapi.json': {'openapi': '3.0.3'}}
    _, result, site = run(files, explore=False, seed=[seed], limits=NavigationLimits(max_asset_bytes=4096))
    caps = {u.replace(HOST, ''): o.get('document_byte_limit') for u, o in site.options}
    assert caps['/docs/swagger-initializer.js'] == caps['/docs/c.json'] == 4096 and caps['/v1/openapi.json'] is None


def test_inline_embedded_specifications_become_candidates_whose_source_is_the_containing_page():
    spec = {'openapi': '3.0.3', 'info': {'title': 'Inline', 'version': '1'}, 'paths': {}}
    seed = FetchResult(HOST + '/docs', HOST + '/docs', 200, 'text/html',
                       f'<script>SwaggerUIBundle({{spec: {json.dumps(spec)}}})</script>'.encode(), NOW, ())
    _, result, _ = run({}, explore=False, seed=[seed])
    (candidate,) = result.candidates
    assert candidate.candidate.discovery_method == 'embedded_spec' and candidate.candidate.source_url == HOST + '/docs'
    assert json.loads(candidate.retrieval.content)['info']['title'] == 'Inline'
    assert 'embedded_spec in ' + HOST + '/docs' in candidate.candidate.evidence[0].description


def test_unsupported_dynamic_viewer_configuration_is_reported_once_per_source_with_every_reason():
    seed = FetchResult(HOST + '/docs', HOST + '/docs', 200, 'text/html',
                       b'<script>SwaggerUIBundle({url: getUrl(), queryConfigEnabled: true})</script>', NOW, ())
    _, result, _ = run({}, explore=False, seed=[seed])
    (finding,) = [a for a in result.artifacts if a.category == 'unsupported_dynamic_configuration']
    assert finding.kind == 'swagger_ui_configuration' and finding.url == HOST + '/docs'
    assert 'url_not_literal' in finding.detail and 'query_config_enabled' in finding.detail and 'no_literal_source' not in finding.detail


def test_requirejs_data_main_names_apidoc_data_without_downloading_it():
    seed = FetchResult(HOST + '/doc/index.html', HOST + '/doc/index.html', 200, 'text/html',
                       b'<script src="vendor/require.min.js" data-main="main"></script>', NOW, ())
    files = {'/doc/main.js': ('js', "require(['./api_project.js', './api_data.js']);")}
    _, result, site = run(files, explore=False, seed=[seed])
    assert {a.url.replace(HOST, '') for a in result.artifacts if a.kind == 'apidoc'} == {'/doc/api_project.js', '/doc/api_data.js'}
    assert all(a.category == 'documentation_only' and a.parent_url == HOST + '/doc/main.js' for a in result.artifacts)
    assert [u.replace(HOST, '') for u in site.calls] == ['/.well-known/api-catalog', '/doc/main.js']


# --- barriers and failures ------------------------------------------------------------------------------------------

@pytest.mark.parametrize('status', [401, 403])
def test_authentication_barriers_are_recorded_structurally(status):
    _, result, _ = run({'/v3/api-docs': ('status', status)})
    (barrier,) = [a for a in result.artifacts if a.category == 'authentication_required' and a.url.endswith('/v3/api-docs')]
    assert (barrier.status, barrier.kind, barrier.discovery_method) == (status, 'authentication', 'framework_probe')


def test_other_failures_are_not_barriers_and_do_not_stop_navigation():
    _, result, _ = run({'/v3/api-docs': ('status', 500), '/openapi/v1.json': ('status', 404), '/': page('<p>ok</p>')})
    assert not [a for a in result.artifacts if a.category == 'authentication_required']
    assert '/' in fetched(result)


def test_the_catalogue_is_probed_even_when_nothing_else_is():
    nav, result, site = run({}, explore=False)
    assert [u.replace(HOST, '') for u in site.calls] == ['/.well-known/api-catalog']


# --- hostile content ---------------------------------------------------------------------------------------------------

def test_hostile_links_are_never_followed():
    body = ('<a href="javascript:fetch(1)">Developer API</a><a href="mailto:a@b.c">API docs</a><a href="ftp://x.test/api">API docs</a>'
            '<a href="https://user:pw@evil.test/developers/api">Developer API</a><a href="data:text/html,x">API reference</a>'
            '<a href="http://[bad/api">API docs</a><a href="/img/api-docs.png">API docs</a><a href="/login/api-docs">API docs</a>')
    _, result, site = run({'/': page(body)})
    assert [r.mechanism for r in result.trail if r.mechanism == 'documentation_navigation'] == []


def test_a_page_full_of_links_and_scripts_is_bounded():
    body = ''.join(f'<a href="/developers/{i}">Developer API {i}</a>' for i in range(2000)) + '<script>' + 'x' * 400_000 + '</script>'
    nav, result, _ = run({'/': page(body)}, limits=NavigationLimits(max_links_per_page=4))
    assert len(nav._leads) < 40


def test_a_catalogue_that_is_not_json_or_has_other_content_is_ignored():
    _, result, _ = run({'/.well-known/api-catalog': page('<html>not a catalogue</html>')}, explore=False)
    assert result.candidates == () and result.artifacts == ()


def test_navigation_is_deterministic():
    files = {'/': page('<a href="/developers/a">Developer API a</a><a href="/developers/b">Developer API b</a>')}
    first, second = run(files)[1], run(files)[1]
    assert first.trail == second.trail and first.fetch_log == second.fetch_log


# --- continuing a session with further leads (used for model-chosen links) ---------------------------------------

def test_further_leads_can_be_added_after_a_run_and_keep_their_depth_and_provenance():
    files = {'/': page('<p>x</p>'), '/portal': page('<a href="/files/spec.json">OpenAPI definition</a>'),
             '/files/spec.json': {'openapi': '3.0.3'}}
    site = Site(files)
    budget = DiscoveryBudget(FetchLimits(max_requests=60, discovery_timeout=60))
    with patch('radar.discovery.candidates.fetch_document', side_effect=site.fetch):
        nav = Navigator(DiscoveryRequest('api.acme.com'), budget)
        nav.seed()
        nav.run()
        assert nav.take_candidates() == () and nav.depth_of(HOST + '/') == 0
        nav.add_leads([Lead(HOST + '/portal', 'page', 'llm_suggestion', HOST + '/', 1, 12)])
        nav.run()
    (candidate,) = nav.take_candidates()
    assert nav.take_candidates() == ()  # each candidate is handed over once
    assert candidate.candidate.discovery_source == HOST + '/portal'
    path = candidate.candidate.evidence[0].description
    assert path.index('origin_root') < path.index('llm_suggestion') < path.index('documentation_link')


# --- link scoring ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('url,label,at_least', [
    ('https://x.test/openapi.json', '', 10), ('https://x.test/docs/api-reference', 'API Reference', 11),
    ('https://x.test/developers', 'Developers', MIN_SCORE), ('https://x.test/specification', '', 8),
    ('https://x.test/guides/start', 'Getting started', 1),
])
def test_promising_links_score_up(url, label, at_least):
    assert score_link(url, label) >= at_least


@pytest.mark.parametrize('url,label', [
    ('https://x.test/pricing', 'Pricing'), ('https://x.test/blog/api-news', 'API news'), ('https://x.test/login', 'Developer login'),
    ('https://x.test/img/api.png', 'API'), ('https://x.test/about', 'About us'), ('https://x.test/static/openapi.js', ''),
    ('http://[bad/x', 'API docs')])
def test_unpromising_links_score_zero(url, label):
    assert score_link(url, label) == 0


def test_navigation_limits_are_validated():
    for kwargs in ({'max_pages': 0}, {'max_depth': -1}, {'max_leads': True}, {'max_asset_bytes': 0}, {'max_probe_contexts': 0},
                   {'max_links_per_page': -1}):
        with pytest.raises(ValueError):
            NavigationLimits(**kwargs)
