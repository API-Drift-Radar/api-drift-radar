"""Controlled registry and HTTP server; no real provider claims."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

from radar.discovery.providers import search_provider_mappings
from radar.discovery.limits import DiscoveryBudget
from radar.domain.discovery import DiscoveryRequest


def test_print_provider_results(tmp_path):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            if self.path == '/missing.yaml':
                self.send_error(404)
                return
            body = b'openapi: 3.0.3\n'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f'http://127.0.0.1:{server.server_port}'
    registry = tmp_path / 'providers.json'
    registry.write_text(json.dumps({'schema_version': 1, 'providers': [
        {'id': identifier, 'hosts': ['api.example.test'], 'spec_url': origin + path,
         'provenance_url': 'https://docs.example.test/' + identifier, 'product': identifier}
        for identifier, path in [('payments', '/payments.yaml'), ('billing', '/payments.yaml'), ('analytics', '/missing.yaml')]
    ]}))
    try:
        result = search_provider_mappings(DiscoveryRequest('https://api.example.test/v1/customers'),
            DiscoveryBudget(), registry_path=registry, allow_loopback=True)
        print(f'\nRecognized host: {len(result.mappings)} mappings; {len(result.search.fetches)} distinct fetches')
        for fetch in result.search.fetches:
            print(f'{fetch.requested_url}: HTTP {fetch.status}; ' + ('unvalidated candidate' if fetch.ok else fetch.failure.code))
        print(f'Successful mapping records retained: {len(result.search.candidates)}')
        assert requests == ['/payments.yaml', '/missing.yaml']
        assert len(result.search.candidates) == 2
        for host in ['unknown.example.test', 'api.example.test.attacker.test']:
            empty = search_provider_mappings(DiscoveryRequest(host), DiscoveryBudget(), registry_path=registry)
            print(f'{host}: {len(empty.mappings)} mappings; {len(empty.search.fetches)} requests')
            assert not empty.mappings and not empty.search.fetches
        print('Contract validation: not performed')
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
