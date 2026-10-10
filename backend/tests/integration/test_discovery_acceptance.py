"""Issue #9 acceptance scenarios against controlled local HTTP servers.

Nothing is patched: discovery runs through the real fetcher, redirects, timeouts and refused
connections. `allow_loopback=True` is the only concession, because the controlled server is local.
Each test is named for the acceptance criterion it demonstrates. No LLM exists in this path.
"""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time

import pytest

from radar.discovery.capture import CaptureLimits
from radar.discovery.limits import FetchLimits
from radar.discovery.orchestrator import discover, select_candidate
from radar.domain.discovery import DiscoveryRequest, DiscoveryStatus

V, A, R, I, N = (DiscoveryStatus.VALIDATED, DiscoveryStatus.AMBIGUOUS, DiscoveryStatus.REJECTED,
                 DiscoveryStatus.INACCESSIBLE, DiscoveryStatus.NOT_FOUND)
HTML = (200, {'Content-Type': 'text/html'}, b'<!doctype html><html><body>Single page app</body></html>')


@contextmanager
def serve(routes):
    """routes: path -> bytes (200 JSON) | (status, headers, body) | callable(handler). Yields (base, log)."""
    log = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            log.append(self.path)
            route = routes.get(self.path)
            if callable(route):
                return route(self)
            status, headers, body = (200, {}, route) if isinstance(route, bytes) else route or (404, {}, b'')
            self.send_response(status)
            for key, value in {'Content-Type': 'application/json', **headers}.items():
                self.send_header(key, value)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads, server.block_on_close = True, False
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', log
    finally:
        server.shutdown()
        server.server_close()


def contract(base, title='Pets API', **extra):
    document = {'openapi': '3.0.3', 'info': {'title': title, 'version': '1.0.0'},
                'servers': [{'url': f'{base}/v1'}],
                'paths': {'/pets': {'get': {'responses': {'200': {'description': 'ok'}}}, 'post': {}},
                          '/pets/{id}': {'get': {}}}}
    document.update(extra)
    return document


def raw(document):
    return json.dumps(document).encode()


def run(target, **options):
    hints = {k: options.pop(k) for k in ('method', 'api_version', 'product') if k in options}
    return discover(DiscoveryRequest(target, **hints), allow_loopback=True, **options)


# --- criterion: a controlled API with a discoverable contract returns a validated candidate -------------

def test_discoverable_contract_returns_a_validated_candidate_with_source_and_evidence():
    server_routes = {}
    with serve(server_routes) as (base, log):
        server_routes['/openapi.json'] = raw(contract(base))
        outcome = run(f'{base}/v1/pets', method='GET')
        print('\nstatus:', outcome.status.value)
        for e in outcome.package.candidate.evidence:
            if e.outcome:
                print(f'  {e.outcome:14} {e.criterion}: {e.description[:80]}')
    assert outcome.status is V and outcome.package is not None and outcome.packages == ()
    candidate = outcome.package.candidate
    assert candidate.source_url == f'{base}/openapi.json' and candidate.discovery_method == 'common_location'
    assert outcome.package.openapi_version == '3.0.3' and outcome.package.root_document.content == raw(contract(base))
    assert outcome.package.root_document.retrieved_at is not None
    assert {e.criterion: e.outcome for e in candidate.evidence if e.outcome} == {
        'server_host': 'match', 'operation': 'match', 'api_version': 'not_requested', 'product': 'not_requested',
        'provenance': 'match'}
    assert any(a.outcome == 'retrieved' for a in outcome.attempts)


def test_documentation_links_and_redirects_find_the_contract_too():
    routes = {}
    with serve(routes) as (base, log):
        routes['/docs'] = (200, {'Content-Type': 'text/html'},
                           b"<script>SwaggerUIBundle({url: '/spec/pets.json', dom_id: '#ui'})</script>")
        routes['/spec/pets.json'] = (301, {'Location': '/spec/v1/pets.json'}, b'')
        routes['/spec/v1/pets.json'] = raw(contract(base))
        outcome = run(base)
    assert outcome.status is V
    assert outcome.package.candidate.discovery_method == 'swagger_ui_config'
    assert outcome.package.candidate.source_url == f'{base}/spec/v1/pets.json'  # the redirect target is the identity


def test_provider_mapping_finds_a_contract_at_an_unguessable_location(tmp_path):
    routes = {}
    with serve(routes) as (base, log):
        routes['/specs/private/pets-2024.yaml'] = raw(contract(base))
        registry = tmp_path / 'providers.json'
        registry.write_text(json.dumps({'schema_version': 1, 'providers': [{
            'id': 'local-pets', 'hosts': ['127.0.0.1'], 'spec_url': f'{base}/specs/private/pets-2024.yaml',
            'provenance_url': f'{base}/about'}]}))
        outcome = run(base, registry_path=registry)
    assert outcome.status is V and outcome.package.candidate.discovery_method == 'provider_mapping'
    assert outcome.package.candidate.discovery_source == f'{base}/about'


# --- criterion: invalid or unrelated documents are rejected with reasons ------------------------------

def test_invalid_and_unrelated_documents_are_rejected_with_reasons():
    routes = {}
    with serve(routes) as (base, log):
        routes['/openapi.json'] = raw(contract('http://api.other.test'))  # a real contract for another API
        routes['/swagger.json'] = raw({'swagger': '2.0', 'info': {'title': 't', 'version': '1'}, 'paths': {}})
        routes['/openapi.yaml'] = raw({'detail': 'Not Found'})  # a JSON error body, not a contract
        outcome = run(base)
    assert outcome.status is R and outcome.package is None
    reasons = sorted(r for c in outcome.candidates for r in c.rejection_reasons)
    assert len(reasons) == 2
    assert reasons[0].startswith('matching:server_host_mismatch:') and reasons[1].startswith('validation:unsupported_version:')
    miss = [a for a in outcome.attempts if a.outcome == 'not_a_contract']
    assert len(miss) == 1 and miss[0].reason.startswith('not_openapi:')  # unrelated, with its reason


@pytest.mark.parametrize('document,stage,code', [
    (lambda b: raw(contract(b, openapi='4.0.0')), 'validation', 'unsupported_version'),
    (lambda b: raw({**contract(b), 'info': {'title': 'x'}}), 'validation', 'invalid_structure'),
    (lambda b: b'{"openapi": "3.0.3", "openapi": "3.1.0"}', 'validation', 'duplicate_key'),
    (lambda b: raw(contract(b, servers=[{'url': 'https://api.elsewhere.test'}])), 'matching', 'server_host_mismatch'),
])
def test_each_kind_of_invalid_contract_names_its_stage_and_code(document, stage, code):
    routes = {}
    with serve(routes) as (base, _):
        routes['/openapi.json'] = document(base)
        outcome = run(base)
    assert outcome.status is R
    assert outcome.candidates[0].rejection_reasons[0].startswith(f'{stage}:{code}:')


def test_a_contract_that_lacks_the_requested_operation_or_version_is_rejected():
    routes = {}
    with serve(routes) as (base, _):
        routes['/openapi.json'] = raw(contract(base))
        no_op = run(f'{base}/v1/orders', method='GET')
        wrong_version = run(base, api_version='v2')
        wrong_product = run(base, product='shipping')
    assert [o.status for o in (no_op, wrong_version, wrong_product)] == [R, R, R]
    codes = [o.candidates[0].rejection_reasons[0].split(':')[1] for o in (no_op, wrong_version, wrong_product)]
    assert codes == ['operation_not_found', 'version_mismatch', 'product_mismatch']


# --- criterion: multiple plausible contracts produce an ambiguous result -------------------------------

def test_multiple_plausible_contracts_are_ambiguous_and_nothing_is_silently_chosen():
    routes = {}
    with serve(routes) as (base, _):
        routes['/openapi.json'] = raw(contract(base))
        routes['/swagger.json'] = raw(contract(base, title='Pets Admin API', paths={'/admin': {'get': {}}}))
        outcome = run(base)
        chosen = select_candidate(outcome, f'{base}/swagger.json')
    assert outcome.status is A and outcome.package is None and len(outcome.packages) == 2
    assert {p.candidate.source_url for p in outcome.packages} == {f'{base}/openapi.json', f'{base}/swagger.json'}
    assert any('none was chosen' in note for note in outcome.limitations)
    assert chosen.status is V and chosen.package.candidate.source_url == f'{base}/swagger.json'


def test_provider_versions_stay_ambiguous_until_the_caller_supplies_one(tmp_path):
    routes = {}
    with serve(routes) as (base, _):
        routes['/v2022.json'] = raw(contract(base))
        routes['/v2026.json'] = raw(contract(base, paths={'/pets': {'get': {}}, '/owners': {'get': {}}}))
        registry = tmp_path / 'providers.json'
        registry.write_text(json.dumps({'schema_version': 1, 'providers': [
            {'id': 'old', 'hosts': ['127.0.0.1'], 'api_version': '2022-11-28', 'spec_url': f'{base}/v2022.json',
             'provenance_url': f'{base}/about'},
            {'id': 'new', 'hosts': ['127.0.0.1'], 'api_version': '2026-03-10', 'spec_url': f'{base}/v2026.json',
             'provenance_url': f'{base}/about'}]}))
        bare = run(base, registry_path=registry)
        hinted = run(base, registry_path=registry, api_version='2026-03-10')
    assert bare.status is A and len(bare.packages) == 2
    assert hinted.status is V and hinted.package.candidate.source_url == f'{base}/v2026.json'


def test_the_same_contract_in_two_formats_is_one_result_with_both_locations_kept():
    import yaml
    routes = {}
    with serve(routes) as (base, _):
        document = contract(base)
        routes['/openapi.json'] = json.dumps(document, indent=2).encode()
        routes['/openapi.yaml'] = (200, {'Content-Type': 'application/yaml'}, yaml.safe_dump(document).encode())
        outcome = run(base)
    assert outcome.status is V
    also = [e.source_url for e in outcome.package.candidate.evidence if e.criterion == 'also_found']
    assert also == [f'{base}/openapi.yaml']


# --- criterion: inaccessible sources and unsuccessful discovery are distinguishable -------------------

def test_every_source_a_clean_miss_is_not_found_and_says_that_is_not_proof():
    with serve({'/openapi.json': HTML, '/openapi.yaml': HTML, '/swagger.json': HTML}) as (base, _):
        outcome = run(base)
    assert outcome.status is N and outcome.candidates == () and outcome.package is None
    assert 'not proof' in ' '.join(outcome.limitations)
    # each page is recorded as fetched, then judged: a catch-all page is not a contract
    assert {a.outcome for a in outcome.attempts} <= {'retrieved', 'not_a_contract', 'not_found'}
    assert sum(a.outcome == 'not_a_contract' for a in outcome.attempts) == 3


@pytest.mark.parametrize('route', [(503, {}, b'down'), (403, {}, b'forbidden'), (429, {'Retry-After': '30'}, b''),
                                   (500, {}, b'')])
def test_a_source_that_errors_makes_the_result_inaccessible_not_not_found(route):
    with serve({'/openapi.json': route}) as (base, _):
        outcome = run(base)
    assert outcome.status is I and any(a.outcome == 'inaccessible' for a in outcome.attempts)
    assert any('could not be reached' in note for note in outcome.limitations)


def test_a_refused_connection_is_inaccessible():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    outcome = run(f'http://127.0.0.1:{port}')
    assert outcome.status is I and all(a.outcome == 'inaccessible' for a in outcome.attempts)


def test_a_slow_server_times_out_and_is_inaccessible():
    def slow(handler):
        time.sleep(1.5)
        handler.send_error(500)

    with serve({'/openapi.json': slow, '/openapi.yaml': slow, '/swagger.json': slow, '/docs': slow,
                '/documentation': slow}) as (base, _):
        started = time.monotonic()
        outcome = run(base, limits=FetchLimits(read_timeout=0.2, discovery_timeout=3))
        elapsed = time.monotonic() - started
    assert outcome.status is I and elapsed < 4
    assert any('timeout' in (a.reason or '') or 'deadline' in (a.reason or '') for a in outcome.attempts)


def test_a_rejected_document_is_reported_even_when_another_source_is_down():
    with serve({'/swagger.json': raw({'swagger': '2.0', 'info': {'title': 't', 'version': '1'}, 'paths': {}}),
                '/openapi.json': (503, {}, b'')}) as (base, _):
        outcome = run(base)
    assert outcome.status is R


def test_private_addresses_are_refused_by_default_and_the_server_is_never_contacted():
    with serve({'/openapi.json': b'{}'}) as (base, log):
        outcome = discover(DiscoveryRequest(base))
    assert outcome.status is I and log == []
    assert any('blocked_destination' in (a.reason or '') for a in outcome.attempts)


# --- criterion: required external references are captured; incomplete ones are never a success --------

PET_SCHEMA = b'{"Pet": {"type": "object", "properties": {"owner": {"$ref": "owner.json"}}}}'
OWNER = b'{"type": "object", "properties": {"name": {"type": "string"}}}'


def with_ref(base):
    return contract(base, paths={'/pets': {'get': {'responses': {'200': {'description': 'ok', 'content': {
        'application/json': {'schema': {'$ref': 'schemas/pet.json#/Pet'}}}}}}}})


def test_required_external_references_are_captured_into_the_package():
    routes = {}
    with serve(routes) as (base, log):
        routes.update({'/openapi.json': raw(with_ref(base)), '/schemas/pet.json': PET_SCHEMA, '/schemas/owner.json': OWNER})
        outcome = run(base)
    assert outcome.status is V
    documents = {d.source_url.replace(base, ''): d.content for d in outcome.package.referenced_documents}
    assert documents == {'/schemas/pet.json': PET_SCHEMA, '/schemas/owner.json': OWNER}  # original bytes


def test_an_incomplete_contract_is_never_returned_as_a_successful_capture():
    routes = {}
    with serve(routes) as (base, _):
        routes.update({'/openapi.json': raw(with_ref(base)), '/schemas/pet.json': PET_SCHEMA})  # owner.json missing
        outcome = run(base)
    assert outcome.status is R and outcome.package is None and outcome.packages == ()
    assert outcome.candidates[0].rejection_reasons[0].startswith('reference_capture:reference_unavailable:')


def test_a_broken_internal_reference_is_an_incomplete_contract():
    routes = {}
    with serve(routes) as (base, _):
        routes['/openapi.json'] = raw(contract(base, paths={'/pets': {'get': {'x': {'$ref': '#/components/schemas/Gone'}}}}))
        outcome = run(base)
    assert outcome.status is R
    assert outcome.candidates[0].rejection_reasons[0].startswith('reference_capture:unresolvable_reference:')


def test_references_to_another_origin_are_refused_unless_explicitly_allowed():
    other_routes = {'/pet.json': b'{"Pet": {"type": "object"}}'}
    with serve(other_routes) as (other, other_log):
        routes = {}
        with serve(routes) as (base, _):
            routes['/openapi.json'] = raw(contract(base, paths={'/pets': {'get': {'x': {'$ref': f'{other}/pet.json#/Pet'}}}}))
            refused = run(base)
            allowed = run(base, capture_limits=CaptureLimits(allow_cross_origin=True))
    assert refused.status is R and refused.candidates[0].rejection_reasons[0].startswith('reference_capture:reference_cross_origin')
    assert allowed.status is V and other_log == ['/pet.json']  # the other origin was only contacted when allowed


# --- criterion: discovery has explicit request, timeout and traversal limits -------------------------

def test_request_limit_stops_the_search_and_never_claims_absence():
    with serve({'/swagger.json': raw(contract('http://127.0.0.1'))}) as (base, log):
        outcome = run(base, limits=FetchLimits(max_requests=2))
    assert outcome.status is N and len(log) == 2
    assert any('cut short' in n and 'other contracts may exist' in n for n in outcome.limitations)


def test_document_size_limit():
    with serve({'/openapi.json': b'{"x": "' + b'a' * 5000 + b'"}'}) as (base, _):
        outcome = run(base, limits=FetchLimits(max_document_bytes=1000))
    assert outcome.status is I and any('document_size_limit' in (a.reason or '') for a in outcome.attempts)


def test_redirect_limit():
    routes = {'/openapi.json': (302, {'Location': '/hop1'}, b''), '/hop1': (302, {'Location': '/hop2'}, b''),
              '/hop2': (302, {'Location': '/hop3'}, b''), '/hop3': (302, {'Location': '/end'}, b''), '/end': b'{}'}
    with serve(routes) as (base, _):
        outcome = run(base, limits=FetchLimits(max_redirects=2))
    assert outcome.status is I and any('redirect_limit' in (a.reason or '') for a in outcome.attempts)


def test_reference_traversal_limits():
    routes = {}
    with serve(routes) as (base, _):
        for i in range(6):
            routes[f'/f{i}.json'] = raw({'n': {'$ref': f'f{i + 1}.json'}}) if i < 5 else b'{}'
        routes['/openapi.json'] = raw(contract(base, paths={'/pets': {'get': {'x': {'$ref': 'f0.json'}}}}))
        deep = run(base, capture_limits=CaptureLimits(max_depth=3))
        wide_ok = run(base)
        few = run(base, capture_limits=CaptureLimits(max_documents=2))
    assert deep.status is R and 'deeper' in deep.candidates[0].rejection_reasons[0]
    assert wide_ok.status is V and len(wide_ok.package.referenced_documents) == 6
    assert few.status is R and 'reference_limit_exceeded' in few.candidates[0].rejection_reasons[0]


def test_the_total_byte_budget_is_shared_across_everything():
    big = raw(contract('http://127.0.0.1', **{'x-pad': 'p' * 3000}))
    with serve({'/openapi.json': big, '/openapi.yaml': big, '/swagger.json': big}) as (base, _):
        outcome = run(base, limits=FetchLimits(max_total_bytes=4000, max_document_bytes=4000))
    assert any('total_size_limit' in (a.reason or '') for a in outcome.attempts)
    assert any('cut short' in n for n in outcome.limitations)


# --- criterion: the deterministic workflow works without an LLM --------------------------------------

def test_discovery_makes_no_network_calls_except_to_the_controlled_server(monkeypatch):
    resolved = set()
    real = socket.getaddrinfo

    def spy(host, *args, **kwargs):
        resolved.add(host)
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, 'getaddrinfo', spy)
    routes = {}
    with serve(routes) as (base, _):
        routes['/openapi.json'] = raw(contract(base))
        outcome = run(base)
    assert outcome.status is V and resolved == {'127.0.0.1'}


def test_no_model_client_is_involved():
    import sys
    run_modules = {name.split('.')[0] for name in sys.modules}
    assert not run_modules & {'anthropic', 'openai', 'google', 'ollama', 'langchain'}
    import inspect
    from radar.discovery import orchestrator
    assert 'llm' not in inspect.signature(orchestrator.discover).parameters


def test_repeated_discovery_gives_the_same_decision_and_the_same_contract_bytes():
    routes = {}
    with serve(routes) as (base, _):
        routes['/openapi.json'] = raw(contract(base))
        routes['/docs'] = HTML
        first, second = run(base), run(base)
    assert first.status is second.status is V
    assert first.package.root_document.content == second.package.root_document.content
    assert [(a.url, a.outcome) for a in first.attempts] == [(a.url, a.outcome) for a in second.attempts]
    assert first.limitations == second.limitations
