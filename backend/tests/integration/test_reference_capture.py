"""Reference capture over real HTTP against controlled local servers."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

from radar.discovery.capture import CaptureLimits, capture_references
from radar.discovery.limits import DiscoveryBudget, FetchLimits
from radar.discovery.validation import validate_document


@contextmanager
def serve(routes):
    """routes: path -> bytes | (status, headers, body) | callable(handler)."""
    requested = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requested.append(self.path)
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
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', requested
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def contract(**paths):
    return {'openapi': '3.0.3', 'info': {'title': 'Acme', 'version': '1'}, 'servers': [{'url': 'http://api.test'}],
            'paths': {'/pets': {'get': {'responses': {'200': {'description': 'ok', 'content': {'application/json': {
                'schema': {'$ref': './schemas/pet.yaml#/Pet'}}}}}}}}, **paths}


PET = b"Pet:\n  type: object\n  properties:\n    name: {type: string}\n    owner: {$ref: '../common/owner.json'}\n    err: {$ref: '#/Error'}\nError:\n  $ref: '../common/error.json#/Error'\n"
OWNER = b'{"type": "object", "properties": {"id": {"type": "integer"}}}'
ERROR = b'{"Error": {"type": "object", "required": ["message"]}, "Unused": {"$ref": "nowhere.json"}}'


def capture(base, raw_root, budget=None, **options):
    validation = validate_document(raw_root)
    assert validation.ok, validation.rejection
    return capture_references(f'{base}/api/openapi.json', validation.document, budget or DiscoveryBudget(),
                              allow_loopback=True, **options)


def test_multi_file_contract_is_captured_completely_over_http():
    root = json.dumps(contract()).encode()
    routes = {'/api/openapi.json': root, '/api/schemas/pet.yaml': (200, {'Content-Type': 'application/yaml'}, PET),
              '/api/common/owner.json': OWNER, '/api/common/error.json': ERROR}
    with serve(routes) as (base, requested):
        budget = DiscoveryBudget()
        result = capture(base, root, budget)
        print('\nCaptured documents:')
        for document in result.documents:
            print(f'  {document.source_url} ({len(document.content)} bytes, {document.media_type})')
        print('References:', [(e.ref, e.pointer) for e in result.references])
    assert result.ok and result.unprocessed == 0
    assert [d.source_url.replace(base, '') for d in result.documents] == [
        '/api/schemas/pet.yaml', '/api/common/owner.json', '/api/common/error.json']
    assert {d.source_url.replace(base, ''): d.content for d in result.documents} == {
        '/api/schemas/pet.yaml': PET, '/api/common/owner.json': OWNER, '/api/common/error.json': ERROR}
    assert requested == ['/api/schemas/pet.yaml', '/api/common/owner.json', '/api/common/error.json']
    assert budget.requests_used == 3 and result.internal_reference_count == 1
    # The unrelated broken reference inside error.json is not required by the contract.
    assert all('nowhere' not in path for path in requested)


def test_missing_referenced_file_makes_the_whole_capture_fail():
    root = json.dumps(contract()).encode()
    routes = {'/api/openapi.json': root, '/api/schemas/pet.yaml': PET, '/api/common/owner.json': OWNER}
    with serve(routes) as (base, _):
        result = capture(base, root)
    assert not result.ok and result.documents == () and result.references == ()
    assert result.rejection.code == 'reference_unavailable' and result.rejection.stage == 'reference_capture'
    assert result.failure.http_status == 404 and result.failure.fetch_code == 'http_error'
    assert result.failure.documents_fetched == 2


def test_redirect_inside_the_origin_is_followed_and_relative_references_use_the_final_url():
    root = json.dumps(contract()).encode()
    # From /api/schemas/ the path ../../common would miss /api/common; only the redirect target's depth makes it right.
    moved = PET.replace(b'../common', b'../../common')
    routes = {'/api/openapi.json': root, '/api/schemas/pet.yaml': (301, {'Location': '/api/v2/x/pet.yaml'}, b''),
              '/api/v2/x/pet.yaml': moved, '/api/common/owner.json': OWNER, '/api/common/error.json': ERROR}
    with serve(routes) as (base, _):
        result = capture(base, root)
    assert result.ok and result.documents[0].source_url == f'{base}/api/v2/x/pet.yaml'
    assert result.references[0].requested_url == f'{base}/api/schemas/pet.yaml'
    assert result.references[0].document_url == f'{base}/api/v2/x/pet.yaml'
    assert len(result.documents) == 3


def test_cross_origin_reference_is_refused_unless_allowed():
    with serve({'/shared.json': b'{"Pet": {"type": "object"}}'}) as (other, other_requested):
        root = json.dumps(contract()).encode().replace(b'./schemas/pet.yaml#/Pet', f'{other}/shared.json#/Pet'.encode())
        with serve({'/api/openapi.json': root}) as (base, _):
            refused = capture(base, root)
            allowed = capture(base, root, limits=CaptureLimits(allow_cross_origin=True))
    assert refused.rejection.code == 'reference_cross_origin' and other_requested == ['/shared.json']
    assert allowed.ok and allowed.documents[0].source_url == f'{other}/shared.json'


def test_private_destinations_are_blocked_without_the_loopback_opt_in():
    root = json.dumps(contract()).encode()
    with serve({'/api/openapi.json': root, '/api/schemas/pet.yaml': PET}) as (base, requested):
        validation = validate_document(root)
        result = capture_references(f'{base}/api/openapi.json', validation.document, DiscoveryBudget())
    assert result.rejection.code == 'reference_unavailable' and result.failure.fetch_code == 'blocked_destination'
    assert requested == []


def test_slow_reference_times_out_with_its_own_cause():
    root = json.dumps(contract()).encode()

    def slow(handler):
        time.sleep(1.5)
        handler.send_error(500)

    with serve({'/api/openapi.json': root, '/api/schemas/pet.yaml': slow}) as (base, _):
        result = capture(base, root, DiscoveryBudget(FetchLimits(read_timeout=0.3)))
    assert result.rejection.code == 'reference_unavailable' and result.failure.fetch_code == 'timeout'


def test_oversized_reference_is_refused_by_the_shared_size_cap():
    root = json.dumps(contract()).encode()
    big = b'{"Pet": {"x": "' + b'a' * 5000 + b'"}}'
    with serve({'/api/openapi.json': root, '/api/schemas/pet.yaml': big}) as (base, _):
        result = capture(base, root, DiscoveryBudget(FetchLimits(max_document_bytes=1000)))
    assert result.rejection.code == 'reference_unavailable' and result.failure.fetch_code == 'document_size_limit'


def test_shared_request_budget_stops_capture_with_a_budget_code():
    root = json.dumps(contract()).encode()
    routes = {'/api/openapi.json': root, '/api/schemas/pet.yaml': PET, '/api/common/owner.json': OWNER,
              '/api/common/error.json': ERROR}
    with serve(routes) as (base, requested):
        result = capture(base, root, DiscoveryBudget(FetchLimits(max_requests=2)))
    assert result.rejection.code == 'capture_budget_exhausted' and result.failure.fetch_code == 'request_limit'
    assert len(requested) == 2 and result.documents == ()


def test_catch_all_page_served_for_a_missing_reference_is_not_accepted():
    root = json.dumps(contract()).encode()
    page = (200, {'Content-Type': 'text/html'}, b'<!doctype html><html><body>Page not found</body></html>')
    with serve({'/api/openapi.json': root, '/api/schemas/pet.yaml': page}) as (base, _):
        result = capture(base, root)
    assert result.rejection.code == 'reference_invalid_document' and 'html_document' in result.rejection.reason
